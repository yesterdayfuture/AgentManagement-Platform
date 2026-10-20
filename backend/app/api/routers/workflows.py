"""工作流定义、运行、暂停、停止和执行记录。流式运行通过服务端事件推送步骤。"""

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
from app.services.artifacts import project_files, project_zip
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

def _workflow_dict(db, row: Workflow) -> dict:
    latest = (
        db.query(WorkflowRun).filter_by(workflow_id=row.id).order_by(WorkflowRun.id.desc()).first()
    )
    definition = load(row.definition_json, {})
    return {
        "id": row.id,
        "name": row.name,
        "agent_id": row.agent_id,
        "status": row.status,
        "release": row.release or "draft",
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


def _can_run_release(ctx, row: Workflow) -> None:
    """操作者只能执行已发布图；构建者与管理员可测草稿。"""
    release = row.release or "draft"
    if release == "published":
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
    row = Workflow(
        space_id=agent.space_id,
        origin=origin,
        agent_id=agent.id,
        name=body.name,
        definition_json=dump(body.definition),
        enabled=body.enabled,
        release=release,
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    if origin == "department":
        grant(db, agent.space_id, "workflow", row.id)
    return _workflow_dict(db, row)


@router.patch("/api/workflows/{workflow_id}")
def patch_workflow(workflow_id: int, body: WorkflowPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_workflow(db, ctx, workflow_id, write=True)
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
    if body.release is not None:
        release = body.release.strip()
        if release not in {"draft", "published"}:
            raise HTTPException(400, "发布状态只能是 draft 或 published")
        row.release = release
    if body.schedule_token_budget is not None:
        row.schedule_token_budget = max(0, int(body.schedule_token_budget))
    if any(item is not None for item in (body.schedule_enabled, body.schedule_minutes, body.schedule_message, body.schedule_nodes)):
        _apply_schedule(db, row, body)
    return _workflow_dict(db, row)


@router.post("/api/workflows/{workflow_id}/publish")
def publish_workflow(workflow_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_workflow(db, ctx, workflow_id, write=True)
    definition = load(row.definition_json, {})
    if not definition.get("nodes"):
        raise HTTPException(400, "工作流没有节点，不能发布")
    row.release = "published"
    audit(db, ctx.space.id, ctx.user.username, "workflow.publish", row.name)
    return _workflow_dict(db, row)


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
    _can_run_release(ctx, row)
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

