"""智能体版本、发布、灰度和试运行。提示词和工具挂在版本上，发布选择的是版本。"""

import queue
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.deps import get_context, get_db, get_principal, require
from app.models import (
    Agent, AgentVersion, Approval, AuditLog, Chunk, ContextPolicy, DelegationHop,
    DepartmentGrant, Document, EvalCase, EvalDataset, EvalRun, KnowledgeBase,
    LedgerAccount, Membership, McpServer, Memory, ModelEndpoint, Secret, Space,
    Tool, ToolGrant, Trace, TraceStep, User, Workflow, WorkflowRun,
)
from app.paging import Desk, apply_desk, desk_params, finish, finish_rows, stamp, window
from app.services.grants import drop_resource, grant, ids_for, is_platform, replace_grants, visible_ids
from app.security import channel_secret, hash_password, issue_token, sign_sso, verify_password
from app.services.builtins import detail_of
from app.services.gateway import GateError
from app.services.mcp_client import McpError, call_tool, connect
from app.services.ingest import IngestError, extract_text, split_text, strategy_catalog, suggest_strategy
from app.services.retrieval import index_document, search_knowledge
from app.services.runtime import (
    _DEFINITION, _EMIT, audit, execute_agent, list_memories, references_of,
    request_control, resume_approval, run_eval, run_workflow,
)
from app.services.sandbox import run_sandbox
from app.services.vault import decrypt, encrypt
from app.util import dump, load
from app.api.schemas import *
from app.api.support import *

router = APIRouter()

def _tools(db, space_id: int | None):
    if space_id is None:
        return db.query(Tool).filter(Tool.enabled.is_(True)).all()
    allowed = ids_for(db, space_id, "tool") or [-1]
    return db.query(Tool).filter(Tool.id.in_(allowed)).all()


def _resolve_tools(db, space_id: int | None, names: list[str]) -> list[int]:
    ids = []
    for name in names:
        tool = next((item for item in _tools(db, space_id) if item.name == name), None)
        if tool is None:
            raise HTTPException(400, "工具未注册")
        ids.append(tool.id)
    return ids


def _resolve_model(db, space_id: int | None, model_id: int | None) -> int | None:
    """挂在部门上的智能体只能绑定该部门已授权的模型。未分配部门时，平台管理员可以绑定任意启用的模型。"""
    if not model_id:
        return None
    row = db.get(ModelEndpoint, model_id)
    if row is None or not row.enabled:
        raise HTTPException(400, "模型不存在")
    if space_id is not None and row.id not in set(ids_for(db, space_id, "model")):
        raise HTTPException(400, "当前部门不能使用这个模型")
    return row.id


def _resolve_skills(db, space_id: int | None, ids: list[int]) -> list[int]:
    from app.models import Skill

    found = []
    for item in ids:
        row = db.get(Skill, item)
        if row is None or not row.enabled or (space_id is not None and row.id not in set(ids_for(db, space_id, "skill"))):
            raise HTTPException(400, "技能不存在")
        found.append(row.id)
    return found


def _resolve_mcp(db, space_id: int | None, ids: list[int]) -> list[int]:
    found = []
    for item in ids:
        row = db.get(McpServer, item)
        if row is None or (space_id is not None and row.space_id != space_id):
            raise HTTPException(400, "MCP 服务不存在")
        found.append(row.id)
    return found


def _resolve_children(db, space_id: int | None, keys: list[str]) -> list[int]:
    ids = []
    for key in keys:
        query = db.query(Agent).filter_by(key=key)
        agent = query.filter(Agent.space_id.is_(None)).one_or_none() if space_id is None else query.filter_by(space_id=space_id).one_or_none()
        if agent is None:
            raise HTTPException(400, "子智能体不存在")
        ids.append(agent.id)
    return ids


def _prompt_with_skills(db, prompt: str, skill_ids: list[int]) -> str:
    """保存时把已选技能的介绍写入提示词。没有技能时去掉原有的「可用技能」一段。"""
    from app.models import Skill
    from app.services.runtime.agent import compose_skill_prompt

    rows = db.query(Skill).filter(Skill.id.in_(skill_ids or [-1]), Skill.enabled.is_(True)).all() if skill_ids else []
    order = {item: index for index, item in enumerate(skill_ids or [])}
    rows.sort(key=lambda row: order.get(row.id, 0))
    return compose_skill_prompt(prompt, rows)


def _save_version(
    db,
    agent: Agent,
    prompt: str,
    tool_ids: list[int],
    child_ids: list[int],
    model_id: int | None = None,
    mcp_ids: list[int] | None = None,
    skill_ids: list[int] | None = None,
) -> AgentVersion:
    """每次修改智能体都新写一个版本，旧版本留着给发布和回滚。

    调用方必须传入 model_id 和 mcp_ids。省略补丁时由 patch_agent 先沿用上一版，再传进来。
    """
    latest = _latest(db, agent.id)
    if latest and latest.status == "draft":
        latest.system_prompt = prompt
        latest.tool_ids_json = dump(tool_ids)
        latest.child_ids_json = dump(child_ids)
        latest.model_id = model_id
        latest.mcp_ids_json = dump(mcp_ids or [])
        latest.skill_ids_json = dump(skill_ids or [])
        latest.eval_passed = False
        return latest
    number = (latest.number + 1) if latest else 1
    version = AgentVersion(
        agent_id=agent.id,
        number=number,
        system_prompt=prompt,
        tool_ids_json=dump(tool_ids),
        child_ids_json=dump(child_ids),
        model_id=model_id,
        mcp_ids_json=dump(mcp_ids or []),
        skill_ids_json=dump(skill_ids or []),
        status="draft",
        eval_passed=False,
    )
    db.add(version)
    db.flush()
    return version


def _agent_brief(db, agent: Agent) -> dict:
    stable = db.get(AgentVersion, agent.stable_version_id) if agent.stable_version_id else None
    gray = db.get(AgentVersion, agent.gray_version_id) if agent.gray_version_id else None
    latest = _latest(db, agent.id)
    model = db.get(ModelEndpoint, latest.model_id) if latest and latest.model_id else None
    return {
        "id": agent.id,
        "space_id": agent.space_id,
        "name": agent.name,
        "key": agent.key,
        "workload_id": agent.workload_id,
        "status": agent.status,
        "require_plan": agent.require_plan,
        "gray_percent": agent.gray_percent,
        "stable_version": stable.number if stable else None,
        "gray_version": gray.number if gray else None,
        "model_id": latest.model_id if latest else None,
        "model_name": model.name if model else "",
        "mcp_ids": load(latest.mcp_ids_json, []) if latest else [],
        "skill_ids": load(latest.skill_ids_json, []) if latest else [],
        "created_at": stamp(agent.created_at),
        **_attribution(db, agent.created_by, agent.space_id),
    }


@router.get("/api/agents")
def list_agents(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(Agent) if is_platform(ctx.user) else db.query(Agent).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        lambda row: _agent_brief(db, row),
        desk,
        order=Agent.id.desc(),
        text=[Agent.name, Agent.key, Agent.workload_id, Agent.status],
        exact={"name": Agent.name, "key": Agent.key, "status": Agent.status},
        time_col=Agent.created_at,
    )


@router.post("/api/agents")
def create_agent(body: AgentIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    space_id = None if is_platform(ctx.user) and body.unassigned else _place(db, ctx, body.space_id)
    existing = db.query(Agent).filter_by(key=body.key)
    existing = existing.filter(Agent.space_id.is_(None)) if space_id is None else existing.filter_by(space_id=space_id)
    if existing.one_or_none():
        raise HTTPException(400, "智能体标识已存在")
    agent = Agent(
        space_id=space_id,
        name=body.name,
        key=body.key,
        workload_id="wl_" + uuid.uuid4().hex[:16],
        status="draft",
        require_plan=body.require_plan,
        created_by=ctx.user.id,
    )
    db.add(agent)
    db.flush()
    skill_ids = _resolve_skills(db, space_id, body.skill_ids)
    _save_version(
        db,
        agent,
        _prompt_with_skills(db, body.system_prompt, skill_ids),
        _resolve_tools(db, space_id, body.tool_names),
        _resolve_children(db, space_id, body.child_keys),
        _resolve_model(db, space_id, body.model_id),
        _resolve_mcp(db, space_id, body.mcp_ids),
        skill_ids,
    )
    audit(db, space_id, ctx.user.username, "agent.create", agent.key)
    return _agent_detail(db, agent)


def _agent_detail(db, agent: Agent) -> dict:
    versions = db.query(AgentVersion).filter_by(agent_id=agent.id).order_by(AgentVersion.number.asc()).all()
    tool_map = {tool.id: tool.name for tool in db.query(Tool).all()}
    child_ids = load(versions[-1].child_ids_json, []) if versions else []
    all_child_ids = {item for version in versions for item in load(version.child_ids_json, [])}
    children = db.query(Agent).filter(Agent.id.in_(child_ids or [-1])).all() if child_ids else []
    child_names = {row.id: row.name for row in db.query(Agent).filter(Agent.id.in_(all_child_ids or [-1])).all()}
    model_ids = {version.model_id for version in versions if version.model_id}
    model_names = {row.id: row.name for row in db.query(ModelEndpoint).filter(ModelEndpoint.id.in_(model_ids or [-1])).all()}
    from app.models import Skill

    mcp_map = {row.id: row.name for row in db.query(McpServer).filter_by(space_id=agent.space_id).all()}
    granted = set(ids_for(db, agent.space_id, "skill")) if agent.space_id else set()
    skill_map = {row.id: row.name for row in db.query(Skill).filter(Skill.id.in_(granted or [-1])).all()} if agent.space_id else {}
    data = _agent_brief(db, agent)
    data.update(
        {
            "versions": [
                {
                    "id": version.id,
                    "number": version.number,
                    "status": version.status,
                    "eval_passed": version.eval_passed,
                    "system_prompt": version.system_prompt,
                    "tools": [tool_map.get(tool_id, str(tool_id)) for tool_id in load(version.tool_ids_json, [])],
                    "child_ids": load(version.child_ids_json, []),
                    "child_names": [child_names.get(item, str(item)) for item in load(version.child_ids_json, [])],
                    "model_id": version.model_id,
                    "model_name": model_names.get(version.model_id, ""),
                    "mcp_ids": load(version.mcp_ids_json, []),
                    "mcp_names": [mcp_map.get(item, str(item)) for item in load(version.mcp_ids_json, [])],
                    "skill_ids": load(version.skill_ids_json, []),
                    "skill_names": [skill_map.get(item, str(item)) for item in load(version.skill_ids_json, [])],
                }
                for version in versions
            ],
            "children": [{"id": child.id, "name": child.name, "key": child.key} for child in children],
            "references": references_of(db, agent),
        }
    )
    return data


def _drop_agent(db, ctx, agent_id: int) -> None:
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    refs = references_of(db, agent)
    if refs:
        raise HTTPException(409, "仍被工作流引用，不能删除")
    db.query(ToolGrant).filter_by(agent_id=agent.id).delete()
    db.query(AgentVersion).filter_by(agent_id=agent.id).delete()
    db.delete(agent)


@router.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_agent(db, ctx, agent_id)
    return {"ok": True}


@router.post("/api/agents/bulk-delete")
def delete_agents(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_agent(db, ctx, item_id))


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    return _agent_detail(db, _load_agent(db, ctx, agent_id))


@router.patch("/api/agents/{agent_id}")
def patch_agent(agent_id: int, body: AgentPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    latest = _latest(db, agent.id)
    if body.name is not None:
        agent.name = body.name
    if body.require_plan is not None:
        agent.require_plan = body.require_plan
    prompt = body.system_prompt if body.system_prompt is not None else (latest.system_prompt if latest else "")
    tool_ids = (
        _resolve_tools(db, ctx.space.id, body.tool_names)
        if body.tool_names is not None
        else load(latest.tool_ids_json, [])
        if latest
        else []
    )
    child_ids = (
        _resolve_children(db, ctx.space.id, body.child_keys)
        if body.child_keys is not None
        else load(latest.child_ids_json, [])
        if latest
        else []
    )
    model_id = (
        _resolve_model(db, agent.space_id, body.model_id)
        if "model_id" in body.model_fields_set
        else (latest.model_id if latest else None)
    )
    mcp_ids = (
        _resolve_mcp(db, ctx.space.id, body.mcp_ids)
        if body.mcp_ids is not None
        else load(latest.mcp_ids_json, [])
        if latest
        else []
    )
    skill_ids = (
        _resolve_skills(db, agent.space_id, body.skill_ids)
        if body.skill_ids is not None
        else load(latest.skill_ids_json, [])
        if latest
        else []
    )
    _save_version(db, agent, _prompt_with_skills(db, prompt, skill_ids), tool_ids, child_ids, model_id, mcp_ids, skill_ids)
    audit(db, ctx.space.id, ctx.user.username, "agent.patch", agent.key)
    return _agent_detail(db, agent)


def _publish(agent: Agent, version: AgentVersion, space: Space, percent: int) -> None:
    if percent < 0 or percent > 100:
        raise HTTPException(400, "灰度比例无效")
    if version.status != "published":
        if space.require_eval and not version.eval_passed:
            raise HTTPException(409, "评测未通过，不能发布")
        version.status = "published"
    if agent.stable_version_id is None or percent >= 100:
        agent.stable_version_id = version.id
        agent.gray_version_id = None
        agent.gray_percent = 0
    elif percent <= 0:
        agent.gray_version_id = version.id
        agent.gray_percent = 0
    else:
        agent.gray_version_id = version.id
        agent.gray_percent = percent
    agent.status = "published"


@router.post("/api/agents/{agent_id}/publish")
def publish_agent(agent_id: int, body: PublishIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    version = _latest(db, agent.id)
    if version is None:
        raise HTTPException(400, "没有版本")
    _publish(agent, version, ctx.space, body.gray_percent)
    audit(db, ctx.space.id, ctx.user.username, "agent.publish", agent.key, str(body.gray_percent))
    return _agent_detail(db, agent)


@router.post("/api/agents/{agent_id}/gray")
def set_gray(agent_id: int, body: GrayIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    if body.percent < 0 or body.percent > 100:
        raise HTTPException(400, "灰度比例无效")
    if agent.gray_version_id is None:
        raise HTTPException(400, "没有灰度版本")
    agent.gray_percent = body.percent
    audit(db, ctx.space.id, ctx.user.username, "agent.gray", agent.key, str(body.percent))
    return _agent_brief(db, agent)


@router.post("/api/agents/{agent_id}/rollback")
def rollback(agent_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    traffic_id = agent.gray_version_id if agent.gray_percent >= 100 and agent.gray_version_id else agent.stable_version_id
    traffic = db.get(AgentVersion, traffic_id) if traffic_id else None
    if traffic is None:
        raise HTTPException(400, "没有可回滚版本")
    previous = (
        db.query(AgentVersion)
        .filter(
            AgentVersion.agent_id == agent.id,
            AgentVersion.status == "published",
            AgentVersion.number < traffic.number,
        )
        .order_by(AgentVersion.number.desc())
        .first()
    )
    if previous is None:
        raise HTTPException(400, "没有可回滚版本")
    agent.stable_version_id = previous.id
    agent.gray_version_id = None
    agent.gray_percent = 0
    if traffic.id != previous.id:
        traffic.status = "rolled_back"
    audit(db, ctx.space.id, ctx.user.username, "agent.rollback", agent.key, str(previous.number))
    return _agent_detail(db, agent)


@router.post("/api/agents/{agent_id}/offline")
def offline(agent_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    agent.status = "offline"
    refs = references_of(db, agent)
    audit(db, ctx.space.id, ctx.user.username, "agent.offline", agent.key)
    return {"status": agent.status, "references": refs}


@router.post("/api/agents/{agent_id}/online")
def online(agent_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    if agent.stable_version_id is None:
        raise HTTPException(400, "没有已发布版本")
    agent.status = "published"
    audit(db, ctx.space.id, ctx.user.username, "agent.online", agent.key)
    return _agent_brief(db, agent)


def _release_version(db, agent: Agent, version: AgentVersion) -> None:
    """草稿要先通过评测才能进入线上或灰度。已经发布过的版本直接切换。"""
    if version.status == "published":
        return
    space = db.get(Space, agent.space_id)
    if space and space.require_eval and not version.eval_passed:
        raise HTTPException(409, "评测未通过，不能发布")
    version.status = "published"


@router.post("/api/agents/{agent_id}/versions/{number}/promote")
def promote_version(agent_id: int, number: int, body: PromoteIn, ctx=Depends(get_context), db=Depends(get_db)):
    """把指定版本设为线上，或设为灰度。恢复上线不会改线上版本，调整灰度也不会。"""
    require(ctx, "builder")
    agent = _load_agent(db, ctx, agent_id)
    version = db.query(AgentVersion).filter_by(agent_id=agent.id, number=number).first()
    if version is None:
        raise HTTPException(404, "版本不存在")
    if body.target == "stable":
        _release_version(db, agent, version)
        agent.stable_version_id = version.id
        if agent.gray_version_id == version.id:
            agent.gray_version_id = None
            agent.gray_percent = 0
        agent.status = "published"
    elif body.target == "gray":
        if version.id == agent.stable_version_id:
            raise HTTPException(400, "这一版已经是线上版本")
        if body.percent < 0 or body.percent > 100:
            raise HTTPException(400, "灰度比例无效")
        _release_version(db, agent, version)
        agent.gray_version_id = version.id
        agent.gray_percent = body.percent
        agent.status = "published"
    else:
        raise HTTPException(400, "上线目标无效")
    audit(db, ctx.space.id, ctx.user.username, "agent.promote", agent.key, f"v{number}:{body.target}")
    return _agent_detail(db, agent)


@router.post("/api/agents/{agent_id}/run")
def run_agent(agent_id: int, body: RunIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    _check_env(body.environment)
    agent = _load_agent(db, ctx, agent_id)
    credential = "user-session"
    if ctx.payload.get("typ") == "delegation":
        if ctx.payload.get("act") != agent.workload_id:
            raise HTTPException(403, "委托身份与智能体不一致")
        credential = "exchanged"
    if not body.message.strip() and not body.resume_trace_id:
        raise HTTPException(400, "请填写输入")
    try:
        return execute_agent(
            db,
            agent=agent,
            user=ctx.user,
            space=ctx.space,
            message=body.message,
            environment=body.environment,
            project_id=body.project_id,
            role=ctx.role,
            credential=credential,
            policy=ctx.policy,
            history=[item.model_dump() for item in body.history],
            resume_trace_id=body.resume_trace_id,
        )
    except GateError as exc:
        _raise_gate(exc, db, agent)

