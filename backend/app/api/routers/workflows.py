"""工作流定义、运行、暂停、停止和执行记录。流式运行通过服务端事件推送步骤。"""

import queue
import json
import threading
import time
import uuid
import zipfile
from difflib import unified_diff
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import update

from app.deps import get_context, get_db, get_principal, require
from app.models import (
    Agent, AgentVersion, Approval, AuditLog, Chunk, ContextPolicy, DelegationHop,
    DepartmentGrant, Document, EvalCase, EvalDataset, EvalRun, KnowledgeBase,
    LedgerAccount, Membership, McpServer, Memory, ModelEndpoint, Secret, Space,
    Tool, ToolGrant, Trace, TraceStep, User, Workflow, WorkflowRun, WorkflowVersion,
)
from app.paging import Desk, apply_desk, desk_params, finish, finish_rows, stamp, window
from app.services.grants import drop_resource, grant, ids_for, is_platform, replace_grants, visible_ids
from app.security import channel_secret, hash_password, issue_token, sign_sso, verify_password
from app.services.artifacts import project_files, project_zip
from app.services.builtins import detail_of
from app.services.gateway import GateError, _CHAT_EMIT
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

def _workflow_dict(db, row: Workflow) -> dict:
    latest = (
        db.query(WorkflowRun).filter_by(workflow_id=row.id).order_by(WorkflowRun.id.desc()).first()
    )
    definition = load(row.definition_json, {})
    return {
        "id": row.id,
        "space_id": row.space_id,
        "name": row.name,
        "agent_id": row.agent_id,
        "status": row.status,
        "release": row.release or "draft",
        "revision": int(row.revision or 1),
        "published_revision": row.published_revision,
        "eval_dataset_id": row.eval_dataset_id,
        "eval_min_pass_rate": float(row.eval_min_pass_rate if row.eval_min_pass_rate is not None else 1),
        "enabled": bool(row.enabled),
        "origin": row.origin or "department",
        "definition": definition,
        "node_count": len(definition.get("nodes") or []),
        "latest_status": latest.status if latest else "",
        "latest_run_id": latest.id if latest else None,
        "schedule_enabled": bool(row.schedule_enabled),
        "schedule_minutes": int(row.schedule_minutes or 0),
        "schedule_message": row.schedule_message or "",
        "schedule_nodes": load(row.schedule_nodes_json, []),
        "schedule_next_at": stamp(row.schedule_next_at) if row.schedule_next_at else "",
        "schedule_token_budget": int(row.schedule_token_budget or 0),
        "schedule_token_used": int(row.schedule_token_used or 0),
        "created_at": stamp(row.created_at),
        **_attribution(db, row.created_by, row.space_id),
    }


def _run_stop_reason(status: str, error: str = "") -> str:
    return {
        "completed": "正常结束",
        "limited": "达到轮次上限",
        "paused": "已暂停",
        "stopped": "已停止",
        "failed": error or "执行失败",
        "blocked": "已熔断",
        "denied": "网关拒绝",
        "suspended": "待审批",
        "cancelled": "已取消",
        "running": "执行中",
    }.get(status, status or "")


def _run_dict(db, row: WorkflowRun) -> dict:
    workflow = db.get(Workflow, row.workflow_id)
    logs = load(row.log_json, [])
    tool_count = sum(1 for item in logs if isinstance(item, dict) and item.get("event") == "工具")
    cost = 0.0
    tokens = 0
    version = None
    stop_reason = _run_stop_reason(row.status)
    if row.trace_id:
        trace = db.get(Trace, row.trace_id)
        if trace is not None:
            cost = round(float(trace.cost or 0), 6)
            tokens = int(trace.tokens_in or 0) + int(trace.tokens_out or 0)
            stop_reason = _run_stop_reason(row.status or trace.status, trace.error or "")
            if trace.version_id:
                ver = db.get(AgentVersion, trace.version_id)
                if ver is not None:
                    version = ver.number
            if not tool_count:
                tool_count = db.query(TraceStep).filter_by(trace_id=trace.id, kind="execute").count()
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "workflow_revision": row.workflow_revision,
        "retry_of_id": row.retry_of_id,
        "workflow_name": workflow.name if workflow else "",
        "trace_id": row.trace_id,
        "status": row.status,
        "environment": row.environment,
        "actor": row.actor,
        "message": row.message,
        "output": row.output_text,
        "cursor": row.cursor,
        "duration_ms": row.duration_ms,
        "logs": logs,
        "files": sorted(project_files(row)),
        "cost": cost,
        "tokens": tokens,
        "tool_count": tool_count,
        "version": version,
        "stop_reason": stop_reason,
        "started_at": stamp(row.started_at),
        "finished_at": stamp(row.finished_at),
    }


def _can_run_release(db, ctx, row: Workflow) -> None:
    """操作者只能执行已发布图；构建者与管理员可测草稿。"""
    release = row.release or "draft"
    if release == "published":
        from app.services.runtime.evaluation import workflow_release_current
        if not workflow_release_current(db, row):
            raise HTTPException(409, "已发布版本的评测依据已变化，请重新评测并发布")
        return
    if ctx.role in {"builder", "admin"} or is_platform(ctx.user):
        return
    raise HTTPException(403, "操作者只能执行已发布的工作流，请先由构建者发布")


def _estimate_workflow(db, ctx, row: Workflow) -> dict:
    definition = load(row.definition_json, {})
    nodes = [node for node in (definition.get("nodes") or []) if node.get("type") == "agent" or node.get("agent_id")]
    agents = []
    hint_cost = 0.0
    for node in nodes:
        agent = db.get(Agent, int(node.get("agent_id") or 0))
        if agent is None:
            continue
        version = _latest(db, agent.id)
        model = db.get(ModelEndpoint, version.model_id) if version and version.model_id else None
        if model is not None:
            # 与 gateway.cost_parts 一致：单价为元/百万 token；参考按每节点 1千入 + 1千出。
            hint_cost += (1000 * float(model.price_in or 0) + 1000 * float(model.price_out or 0)) / 1_000_000
        agents.append(
            {
                "node_id": node.get("id") or "",
                "agent_id": agent.id,
                "agent_name": agent.name,
                "version": version.number if version else None,
                "model_id": model.id if model else None,
                "model_name": model.name if model else "按任务自动选",
                "price_in": model.price_in if model else 0,
                "price_out": model.price_out if model else 0,
                "quota_remaining": None
                if not model or not (model.quota_limit or 0)
                else round(max(0, model.quota_limit - (model.quota_used or 0)), 6),
            }
        )
    space = db.get(Space, row.space_id) or ctx.space
    return {
        "workflow_id": row.id,
        "name": row.name,
        "release": row.release or "draft",
        "agents": agents,
        "estimated_cost_hint": round(hint_cost, 6),
        "space_token_budget": int(space.token_budget or 0),
        "space_token_used": int(space.token_used or 0),
        "space_token_remaining": max(0, int(space.token_budget or 0) - int(space.token_used or 0)),
        "schedule_token_budget": int(row.schedule_token_budget or 0),
        "schedule_token_used": int(row.schedule_token_used or 0),
        "note": "费用按元/百万 token 估算，每节点按约 1千入+1千出；实际以执行为准。",
    }


@router.get("/api/workflow-runs/{run_id}/archive")
def download_run_archive(run_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    """把这次执行里写出的项目文件打成 zip。没有文件时返回 404。"""
    row = db.get(WorkflowRun, run_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "执行结果不存在")
    payload = project_zip(row)
    if not payload:
        raise HTTPException(404, "这次执行没有可下载的项目文件")
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="workflow-run-{row.id}.zip"'},
    )


@router.get("/api/workflows")
def list_workflows(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), usable_space: int | None = Query(default=None)):
    query = scope_grants(db, ctx, _visible(db, ctx, Workflow, "workflow"), Workflow, "workflow", usable_space)
    return finish(
        query,
        lambda row: _workflow_dict(db, row),
        desk,
        order=Workflow.id.desc(),
        text=[Workflow.name, Workflow.status],
        exact={"name": Workflow.name, "status": Workflow.status, "release": Workflow.release},
        time_col=Workflow.created_at,
    )


@router.post("/api/workflows")
def create_workflow(body: WorkflowIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    agent = _load_agent(db, ctx, body.agent_id)
    if not body.definition.get("nodes"):
        raise HTTPException(400, "工作流没有节点")
    origin = "platform" if is_platform(ctx.user) else "department"
    release = (body.release or "draft").strip()
    if release not in {"draft", "published"}:
        raise HTTPException(400, "发布状态只能是 draft 或 published")
    if release == "published":
        raise HTTPException(409, "请先创建草稿、绑定评测集并通过评测")
    if body.eval_dataset_id is not None:
        dataset = db.get(EvalDataset, body.eval_dataset_id)
        if dataset is None or dataset.space_id != agent.space_id:
            raise HTTPException(404, "评测集不存在")
    if not 0 < body.eval_min_pass_rate <= 1:
        raise HTTPException(400, "通过率门槛必须大于 0 且不超过 1")
    row = Workflow(
        space_id=agent.space_id,
        origin=origin,
        agent_id=agent.id,
        name=body.name,
        definition_json=dump(body.definition),
        enabled=body.enabled,
        release=release,
        eval_dataset_id=body.eval_dataset_id,
        eval_min_pass_rate=body.eval_min_pass_rate,
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    db.add(WorkflowVersion(workflow_id=row.id, number=1, name=row.name, agent_id=row.agent_id, definition_json=row.definition_json, created_by=ctx.user.id))
    if agent.space_id is not None:
        grant(db, agent.space_id, "workflow", row.id)
    return _workflow_dict(db, row)


@router.patch("/api/workflows/{workflow_id}")
def patch_workflow(workflow_id: int, body: WorkflowPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_workflow(db, ctx, workflow_id, write=True)
    old_revision = int(row.revision or 1)
    if body.revision is not None and body.revision != old_revision:
        raise HTTPException(409, "工作流已被其他人修改，请刷新后重试")
    content_changed = (body.name is not None and body.name != row.name) or (body.agent_id is not None and body.agent_id != row.agent_id) or (body.definition is not None and dump(body.definition) != row.definition_json)
    if content_changed:
        updated = db.execute(update(Workflow).where(Workflow.id == row.id, Workflow.revision == old_revision).values(revision=old_revision + 1))
        if updated.rowcount != 1:
            raise HTTPException(409, "工作流已被其他人修改，请刷新后重试")
        row.revision = old_revision + 1
    if body.name is not None:
        row.name = body.name
    if body.status is not None:
        row.status = body.status
    if body.agent_id is not None:
        _load_agent(db, ctx, body.agent_id)
        row.agent_id = body.agent_id
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.definition is not None:
        if not body.definition.get("nodes"):
            raise HTTPException(400, "工作流没有节点")
        row.definition_json = dump(body.definition)
        if (row.release or "draft") == "published":
            row.release = "draft"
    if content_changed:
        row.release = "draft"
        row.schedule_enabled = False
        row.schedule_next_at = None
        db.add(WorkflowVersion(workflow_id=row.id, number=row.revision, name=row.name, agent_id=row.agent_id, definition_json=row.definition_json, created_by=ctx.user.id))
    if body.release is not None:
        release = body.release.strip()
        if release not in {"draft", "published"}:
            raise HTTPException(400, "发布状态只能是 draft 或 published")
        if release == "published":
            raise HTTPException(409, "请使用发布操作，完成版本评测门禁")
        row.release = release
    if body.schedule_token_budget is not None:
        row.schedule_token_budget = max(0, int(body.schedule_token_budget))
    if body.eval_dataset_id is not None:
        dataset = db.get(EvalDataset, body.eval_dataset_id)
        if dataset is None or dataset.space_id != row.space_id:
            raise HTTPException(404, "评测集不存在")
        row.eval_dataset_id = dataset.id
    if body.eval_min_pass_rate is not None:
        if not 0 < body.eval_min_pass_rate <= 1:
            raise HTTPException(400, "通过率门槛必须大于 0 且不超过 1")
        row.eval_min_pass_rate = body.eval_min_pass_rate
    if any(item is not None for item in (body.schedule_enabled, body.schedule_minutes, body.schedule_message, body.schedule_nodes)):
        _apply_schedule(db, row, body)
    return _workflow_dict(db, row)


class PublishWorkflowIn(BaseModel):
    revision: int | None = None


@router.post("/api/workflows/{workflow_id}/publish")
def publish_workflow(workflow_id: int, body: PublishWorkflowIn | None = None, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_workflow(db, ctx, workflow_id, write=True)
    if body and body.revision is not None and body.revision != row.revision:
        raise HTTPException(409, "工作流已被其他人修改，请刷新后重试")
    definition = load(row.definition_json, {})
    if not definition.get("nodes"):
        raise HTTPException(400, "工作流没有节点，不能发布")
    if row.eval_dataset_id is None:
        raise HTTPException(409, "请先为工作流选择评测集")
    latest = db.query(EvalRun).filter_by(dataset_id=row.eval_dataset_id, workflow_id=row.id, workflow_revision=row.revision).order_by(EvalRun.id.desc()).first()
    from app.services.runtime.evaluation import dataset_fingerprint, workflow_agent_fingerprint
    if latest is None or not latest.passed or latest.dataset_fingerprint != dataset_fingerprint(db, row.eval_dataset_id) or latest.agent_fingerprint != workflow_agent_fingerprint(db, row) or latest.completion < float(row.eval_min_pass_rate or 1) or not _eval_authentic(db, row, latest):
        raise HTTPException(409, "当前版本未达到评测通过率门槛，请先运行评测")
    updated = db.execute(update(Workflow).where(Workflow.id == row.id, Workflow.revision == row.revision).values(release="published", published_revision=row.revision))
    if updated.rowcount != 1:
        raise HTTPException(409, "工作流已被其他人修改，请刷新后重试")
    row.release = "published"
    row.published_revision = row.revision
    audit(db, ctx.space.id, ctx.user.username, "workflow.publish", row.name)
    return _workflow_dict(db, row)


def _eval_authentic(db, workflow: Workflow, report: EvalRun) -> bool:
    for node in load(workflow.definition_json, {}).get("nodes") or []:
        agent_id = int(node.get("agent_id") or 0)
        if not agent_id:
            continue
        version = db.query(AgentVersion).filter_by(agent_id=agent_id).order_by(AgentVersion.number.desc()).first()
        model = db.get(ModelEndpoint, version.model_id) if version and version.model_id else None
        if model is not None and model.provider != "rules":
            details = load(report.detail_json, [])
            return bool(details) and all(item.get("model_online") is True for item in details)
    return True


@router.get("/api/workflows/{workflow_id}/eval-gate")
def workflow_eval_gate(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    row = _load_workflow(db, ctx, workflow_id)
    if row.eval_dataset_id is None:
        return {"ready": False, "reason": "未选择评测集", "latest": None, "previous": None}
    from app.services.runtime.evaluation import dataset_fingerprint, workflow_agent_fingerprint
    runs = db.query(EvalRun).filter_by(workflow_id=row.id, dataset_id=row.eval_dataset_id).order_by(EvalRun.id.desc()).all()
    latest = next((item for item in runs if item.workflow_revision == row.revision), None)
    previous = next((item for item in runs if item.workflow_revision != row.revision), None)
    def summary(item):
        return None if item is None else {"id": item.id, "revision": item.workflow_revision, "pass_rate": item.completion, "cost": item.cost, "average_latency_ms": round(sum(d.get("latency_ms", 0) for d in load(item.detail_json, [])) / max(1, len(load(item.detail_json, [])))), "created_at": stamp(item.created_at)}
    ready = bool(latest and latest.passed and latest.dataset_fingerprint == dataset_fingerprint(db, row.eval_dataset_id) and latest.agent_fingerprint == workflow_agent_fingerprint(db, row) and latest.completion >= float(row.eval_min_pass_rate or 1) and _eval_authentic(db, row, latest))
    return {"ready": ready, "reason": "通过" if ready else "当前版本尚未达到评测门槛", "min_pass_rate": row.eval_min_pass_rate, "latest": summary(latest), "previous": summary(previous)}


@router.get("/api/workflows/{workflow_id}/run-estimate")
def run_estimate(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = _load_workflow(db, ctx, workflow_id)
    return _estimate_workflow(db, ctx, row)


@router.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "viewer")
    row = _load_workflow(db, ctx, workflow_id)
    return _workflow_dict(db, row)


@router.get("/api/workflows/{workflow_id}/versions")
def list_workflow_versions(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    row = _load_workflow(db, ctx, workflow_id)
    versions = db.query(WorkflowVersion).filter_by(workflow_id=row.id).order_by(WorkflowVersion.number.desc()).all()
    return [{"number": item.number, "name": item.name, "agent_id": item.agent_id, "definition": load(item.definition_json, {}), "created_at": stamp(item.created_at), "published": item.number == row.published_revision} for item in versions]


@router.get("/api/workflows/{workflow_id}/versions/{number}/diff")
def diff_workflow_version(workflow_id: int, number: int, ctx=Depends(get_context), db=Depends(get_db)):
    row = _load_workflow(db, ctx, workflow_id)
    current = db.query(WorkflowVersion).filter_by(workflow_id=row.id, number=number).first()
    previous = db.query(WorkflowVersion).filter_by(workflow_id=row.id, number=number - 1).first()
    if current is None:
        raise HTTPException(404, "版本不存在")
    before_obj = {"name": previous.name, "agent_id": previous.agent_id, "definition": load(previous.definition_json, {})} if previous else {}
    after_obj = {"name": current.name, "agent_id": current.agent_id, "definition": load(current.definition_json, {})}
    before = json.dumps(before_obj, ensure_ascii=False, indent=2, sort_keys=True)
    after = json.dumps(after_obj, ensure_ascii=False, indent=2, sort_keys=True)
    return {"number": number, "previous_number": previous.number if previous else None, "diff": "\n".join(unified_diff(before.splitlines(), after.splitlines(), fromfile=f"v{number-1}", tofile=f"v{number}"))}


class RestoreWorkflowIn(BaseModel):
    revision: int


@router.post("/api/workflows/{workflow_id}/versions/{number}/restore")
def restore_workflow_version(workflow_id: int, number: int, body: RestoreWorkflowIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_workflow(db, ctx, workflow_id, write=True)
    version = db.query(WorkflowVersion).filter_by(workflow_id=row.id, number=number).first()
    if version is None:
        raise HTTPException(404, "版本不存在")
    updated = db.execute(update(Workflow).where(Workflow.id == row.id, Workflow.revision == body.revision).values(revision=body.revision + 1))
    if updated.rowcount != 1:
        raise HTTPException(409, "工作流已被其他人修改，请刷新后重试")
    row.revision = body.revision + 1
    row.name, row.agent_id, row.definition_json = version.name, version.agent_id, version.definition_json
    row.release, row.schedule_enabled, row.schedule_next_at = "draft", False, None
    db.add(WorkflowVersion(workflow_id=row.id, number=row.revision, name=row.name, agent_id=row.agent_id, definition_json=row.definition_json, created_by=ctx.user.id))
    return _workflow_dict(db, row)


class RetryWorkflowIn(BaseModel):
    version: str = "original"
    from_failed_node: bool = False


@router.post("/api/workflow-runs/{run_id}/retry")
def retry_workflow_run(run_id: int, body: RetryWorkflowIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    original = db.get(WorkflowRun, run_id)
    if original is None or original.space_id != ctx.space.id:
        raise HTTPException(404, "执行记录不存在")
    if original.status not in {"failed", "blocked", "denied", "limited", "stopped"}:
        raise HTTPException(409, "只有失败或中断的执行可以重试")
    workflow = _load_workflow(db, ctx, original.workflow_id)
    if not workflow.enabled:
        raise HTTPException(409, "工作流未启用")
    if body.version not in {"original", "current"}:
        raise HTTPException(400, "版本只能是 original 或 current")
    _can_run_release(db, ctx, workflow)
    if body.version == "original":
        if original.definition_json == "{}":
            raise HTTPException(409, "旧执行没有版本快照，只能用当前版本重试")
        definition = load(original.definition_json, {})
    else:
        definition = load(workflow.definition_json, {})
    if body.from_failed_node and (body.version != "original" or definition.get("kind") != "agents"):
        raise HTTPException(400, "节点重试仅支持原版本的智能体连线工作流")
    retry = None
    if body.from_failed_node:
        retry = WorkflowRun(space_id=original.space_id, workflow_id=original.workflow_id, workflow_revision=original.workflow_revision, definition_json=original.definition_json, retry_of_id=original.id, status="paused", environment=original.environment, actor=ctx.user.username, message=original.message, carry_text=original.carry_text, cursor=original.cursor, log_json=original.log_json, started_at=datetime.utcnow())
        db.add(retry)
        db.flush()
    token = _DEFINITION.set(definition)
    try:
        result = run_workflow(db, workflow, user=ctx.user, space=ctx.space, message=original.message, environment=original.environment, role=ctx.role, policy=ctx.policy, resume=retry)
        rerun = db.get(WorkflowRun, result["run_id"])
        rerun.retry_of_id = original.id
        if body.version == "original":
            rerun.workflow_revision = original.workflow_revision
        db.commit()
        return _run_dict(db, rerun)
    except GateError as exc:
        if retry is not None:
            retry.retry_of_id = original.id
            db.commit()
        agent = db.get(Agent, workflow.agent_id) if workflow.agent_id else None
        _raise_gate(exc, db, agent)
    finally:
        _DEFINITION.reset(token)


def _apply_schedule(db, row: Workflow, body: WorkflowPatch) -> None:
    """打开定时后，按间隔重复执行这次填写的问题和节点配置。"""
    enabled = row.schedule_enabled if body.schedule_enabled is None else body.schedule_enabled
    minutes = int(row.schedule_minutes or 0) if body.schedule_minutes is None else int(body.schedule_minutes)
    message = row.schedule_message if body.schedule_message is None else body.schedule_message
    nodes = load(row.schedule_nodes_json, []) if body.schedule_nodes is None else body.schedule_nodes
    if enabled:
        if not str(message or "").strip():
            raise HTTPException(400, "定时执行需要填写问题")
        if minutes < 1:
            raise HTTPException(400, "定时间隔至少 1 分钟")
        if (row.release or "draft") != "published":
            raise HTTPException(400, "请先发布工作流，再开启定时")
        space = db.get(Space, row.space_id)
        if space is not None and space.token_used >= space.token_budget:
            raise HTTPException(409, "部门 token 额度已用尽，不能开启定时")
        budget = int(row.schedule_token_budget or 0)
        if budget > 0 and int(row.schedule_token_used or 0) >= budget:
            raise HTTPException(409, "定时 token 额度已用尽，请提高上限或清零用量后再开启")
        row.schedule_enabled = True
        row.schedule_minutes = minutes
        row.schedule_message = str(message).strip()
        row.schedule_nodes_json = dump(nodes or [])
        row.schedule_next_at = datetime.utcnow() + timedelta(minutes=minutes)
        return
    row.schedule_enabled = False
    row.schedule_next_at = None
    if body.schedule_minutes is not None:
        row.schedule_minutes = minutes
    if body.schedule_message is not None:
        row.schedule_message = message or ""
    if body.schedule_nodes is not None:
        row.schedule_nodes_json = dump(nodes or [])


def _drop_workflow(db, ctx, workflow_id: int) -> None:
    require(ctx, "builder")
    row = _load_workflow(db, ctx, workflow_id, write=True)
    db.query(WorkflowRun).filter_by(workflow_id=row.id).delete()
    db.query(WorkflowVersion).filter_by(workflow_id=row.id).delete()
    drop_resource(db, "workflow", row.id)
    db.delete(row)


@router.delete("/api/workflows/{workflow_id}")
def delete_workflow(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_workflow(db, ctx, workflow_id)
    return {"ok": True}


@router.post("/api/workflows/bulk-delete")
def delete_workflows(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_workflow(db, ctx, item_id))


def _active_run(db, workflow_id: int, statuses: set[str]) -> WorkflowRun | None:
    return (
        db.query(WorkflowRun)
        .filter(WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(statuses))
        .order_by(WorkflowRun.id.desc())
        .first()
    )


def _merge_node_setup(definition: dict, nodes: list[NodeSetupIn]) -> dict:
    merged = load(dump(definition), {})
    by_node = {item.node_id: item for item in nodes if item.node_id}
    by_agent: dict[int, NodeSetupIn] = {}
    for item in nodes:
        by_agent.setdefault(item.agent_id, item)
    for node in merged.get("nodes") or []:
        setup = by_node.get(node.get("id")) or by_agent.get(int(node.get("agent_id") or 0))
        if setup is None:
            continue
        if setup.prompt is not None:
            node["prompt"] = setup.prompt
        if setup.tool_names is not None:
            node["tool_names"] = setup.tool_names
        if setup.knowledge_ids is not None:
            node["knowledge_ids"] = list(setup.knowledge_ids)
        if setup.mcp_ids is not None:
            node["mcp_ids"] = list(setup.mcp_ids)
        if setup.skill_ids is not None:
            node["skill_ids"] = list(setup.skill_ids)
    return merged


def _stream_workflow(request: Request, ctx, workflow_id: int, body: RunIn, definition: dict | None):
    factory = request.app.state.session_factory
    user_id = ctx.user.id
    space_id = ctx.space.id
    role = ctx.role
    events: queue.Queue = queue.Queue()

    def worker() -> None:
        session = factory()
        def_token = _DEFINITION.set(definition)
        emit_token = _EMIT.set(events.put)

        def chat_sink(event):
            kind = event.get("type")
            if kind == "delta":
                events.put({"event": "delta", "text": event.get("text") or "", "status": "running", "output": "", "input": "", "agent": "", "node": "", "ms": 0})
            elif kind == "tool":
                events.put({"event": "模型工具", "tool": event.get("name") or "", "status": "running", "output": "", "input": "", "agent": "", "node": "", "ms": 0})
            elif kind == "reset":
                events.put({"event": "reset", "status": "running", "output": "", "input": "", "agent": "", "node": "", "ms": 0})

        chat_token = _CHAT_EMIT.set(chat_sink)
        try:
            user = session.get(User, user_id)
            space = session.get(Space, space_id)
            policy = session.query(ContextPolicy).filter_by(space_id=space_id).one()
            workflow = session.get(Workflow, workflow_id)
            result = run_workflow(
                session,
                workflow,
                user=user,
                space=space,
                message=body.message,
                environment=body.environment,
                role=role,
                policy=policy,
                pause_after=body.pause_after,
            )
            session.commit()
            events.put({"event": "完成", "status": result["status"], "output": result["output"], "run_id": result["run_id"], "duration_ms": result["duration_ms"], "input": body.message, "agent": "", "node": "", "ms": result["duration_ms"]})
        except GateError as exc:
            session.rollback()
            events.put({"event": "错误", "status": exc.status, "output": exc.message, "input": "", "agent": "", "node": "", "ms": 0})
        except Exception as exc:
            session.rollback()
            events.put({"event": "错误", "status": "failed", "output": str(exc), "input": "", "agent": "", "node": "", "ms": 0})
        finally:
            _DEFINITION.reset(def_token)
            _EMIT.reset(emit_token)
            _CHAT_EMIT.reset(chat_token)
            session.close()

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            item = events.get()
            yield f"data: {dump(item)}\n\n"
            if item.get("event") in {"完成", "错误"}:
                break

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/workflows/{workflow_id}/run")
def run_flow(workflow_id: int, body: RunIn, request: Request, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    _check_env(body.environment)
    row = _load_workflow(db, ctx, workflow_id)
    if not row.enabled:
        raise HTTPException(409, "工作流未启用")
    _can_run_release(db, ctx, row)
    definition = _merge_node_setup(load(row.definition_json, {}), body.nodes) if body.nodes else None
    if body.stream:
        return _stream_workflow(request, ctx, row.id, body, definition)
    token = _DEFINITION.set(definition)
    try:
        return run_workflow(
            db,
            row,
            user=ctx.user,
            space=ctx.space,
            message=body.message,
            environment=body.environment,
            role=ctx.role,
            policy=ctx.policy,
            pause_after=body.pause_after,
        )
    except GateError as exc:
        agent = db.get(Agent, row.agent_id) if row.agent_id else None
        _raise_gate(exc, db, agent)
    finally:
        _DEFINITION.reset(token)


@router.post("/api/workflows/{workflow_id}/pause")
def pause_flow(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = _load_workflow(db, ctx, workflow_id)
    run = _active_run(db, row.id, {"running"})
    if run is None:
        raise HTTPException(409, "没有正在运行的执行")
    request_control(run.id, "pause")
    return {"ok": True, "run_id": run.id, "status": "paused"}


@router.post("/api/workflows/{workflow_id}/stop")
def stop_flow(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = _load_workflow(db, ctx, workflow_id)
    run = _active_run(db, row.id, {"running", "paused"})
    if run is None:
        raise HTTPException(409, "没有可停止的执行")
    if run.status == "paused":
        run.status = "stopped"
        run.output_text = run.output_text or "已停止"
        if run.started_at:
            from datetime import datetime

            run.finished_at = datetime.utcnow()
            run.duration_ms = max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))
        return _run_dict(db, run)
    request_control(run.id, "stop")
    return {"ok": True, "run_id": run.id, "status": "stopped"}


@router.post("/api/workflows/{workflow_id}/resume")
def resume_flow(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = _load_workflow(db, ctx, workflow_id)
    if not row.enabled:
        raise HTTPException(409, "工作流未启用")
    run = _active_run(db, row.id, {"paused"})
    if run is None:
        raise HTTPException(409, "只有暂停的执行可以继续")
    token = _DEFINITION.set(load(run.definition_json, {}) if run.definition_json and run.definition_json != "{}" else None)
    try:
        return run_workflow(
            db,
            row,
            user=ctx.user,
            space=ctx.space,
            message=run.message,
            environment=run.environment,
            role=ctx.role,
            policy=ctx.policy,
            resume=run,
        )
    except GateError as exc:
        agent = db.get(Agent, row.agent_id) if row.agent_id else None
        _raise_gate(exc, db, agent)
    finally:
        _DEFINITION.reset(token)


@router.get("/api/workflow-runs")
def list_workflow_runs(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(WorkflowRun).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        lambda row: _run_dict(db, row),
        desk,
        order=WorkflowRun.id.desc(),
        text=[WorkflowRun.status, WorkflowRun.actor, WorkflowRun.message, WorkflowRun.output_text],
        exact={"status": WorkflowRun.status, "environment": WorkflowRun.environment, "workflow_id": WorkflowRun.workflow_id},
        time_col=WorkflowRun.started_at,
    )


@router.get("/api/workflow-runs/{run_id}")
def get_workflow_run(run_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    row = db.get(WorkflowRun, run_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "执行结果不存在")
    return _run_dict(db, row)


def _drop_run(db, ctx, run_id: int) -> None:
    require(ctx, "builder")
    row = db.get(WorkflowRun, run_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "执行结果不存在")
    if row.status == "running":
        raise HTTPException(409, "正在运行的执行不能删除")
    db.delete(row)


@router.delete("/api/workflow-runs/{run_id}")
def delete_workflow_run(run_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_run(db, ctx, run_id)
    return {"ok": True}


@router.post("/api/workflow-runs/bulk-delete")
def delete_workflow_runs(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_run(db, ctx, item_id))

