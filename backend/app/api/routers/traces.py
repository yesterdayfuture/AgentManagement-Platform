"""调用轨迹和运行总览。轨迹详情保留每一步的模型、工具入参和输出。"""

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

@router.get("/api/traces")
def list_traces(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), space_id: int = 0):
    query = db.query(Trace)
    if is_platform(ctx.user):
        if space_id:
            query = query.filter(Trace.space_id == space_id)
    else:
        query = query.filter(Trace.space_id == ctx.space.id)
    names = {space.id: space.name for space in db.query(Space).all()}

    def brief(row):
        data = _trace_brief(row)
        data["department"] = names.get(row.space_id, "")
        return data

    return finish(
        query,
        brief,
        desk,
        order=Trace.created_at.desc(),
        text=[Trace.input_text, Trace.output_text, Trace.status, Trace.environment],
        exact={"status": Trace.status, "environment": Trace.environment, "agent_id": Trace.agent_id},
        time_col=Trace.created_at,
        limit_unpaged=100,
    )


def _trace_brief(row: Trace) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "workflow_id": row.workflow_id,
        "environment": row.environment,
        "status": row.status,
        "input": row.input_text,
        "output": row.output_text,
        "tokens": row.tokens_in + row.tokens_out,
        "cost": row.cost,
        "latency_ms": row.latency_ms,
        "quality_vote": row.quality_vote,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/api/traces/{trace_id}")
def get_trace(trace_id: str, ctx=Depends(get_context), db=Depends(get_db)):
    row = db.get(Trace, trace_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "轨迹不存在")
    steps = db.query(TraceStep).filter_by(trace_id=row.id).order_by(TraceStep.idx.asc()).all()
    hops = db.query(DelegationHop).filter_by(trace_id=row.id).order_by(DelegationHop.seq.asc()).all()
    data = _trace_brief(row)
    data["citations"] = load(row.citations_json, [])
    data["error"] = row.error
    data["steps"] = [
        {
            "idx": step.idx,
            "kind": step.kind,
            "name": step.name,
            "model": step.model,
            "prompt": step.prompt,
            "output": step.output_text,
            "tool_args": step.tool_args,
            "latency_ms": step.latency_ms,
            "tokens": step.tokens,
            "tokens_in": step.tokens_in or 0,
            "tokens_out": step.tokens_out or 0,
            "cost": step.cost,
            "cost_in": step.cost_in or 0,
            "cost_out": step.cost_out or 0,
            "fallback": step.fallback,
        }
        for step in steps
    ]
    data["hops"] = [
        {
            "seq": hop.seq,
            "subject_user_id": hop.subject_user_id,
            "actor": hop.actor,
            "target": hop.target,
            "credential": hop.credential,
        }
        for hop in hops
    ]
    return data


@router.post("/api/traces/{trace_id}/feedback")
def feedback(trace_id: str, body: FeedbackIn, ctx=Depends(get_context), db=Depends(get_db)):
    if body.vote not in {-1, 1}:
        raise HTTPException(400, "反馈无效")
    row = db.get(Trace, trace_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "轨迹不存在")
    row.quality_vote = body.vote
    return {"id": row.id, "quality_vote": row.quality_vote}


def _drop_trace(db, ctx, trace_id: str) -> None:
    require(ctx, "builder")
    row = db.get(Trace, trace_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "轨迹不存在")
    db.query(TraceStep).filter_by(trace_id=row.id).delete()
    db.query(DelegationHop).filter_by(trace_id=row.id).delete()
    db.delete(row)


@router.delete("/api/traces/{trace_id}")
def delete_trace(trace_id: str, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_trace(db, ctx, trace_id)
    return {"ok": True}


@router.post("/api/traces/bulk-delete")
def delete_traces(body: TraceIdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_trace(db, ctx, item_id))


def _touch(bucket: dict, key: str, value, money: bool = False) -> None:
    if money:
        bucket[key] = round(bucket.get(key, 0) + value, 6)
    else:
        bucket[key] = bucket.get(key, 0) + value


def _secret_board(db, step_rows: list) -> dict:
    """按密钥汇总模型调用和 MCP 调用。

    步骤上记了 secret_id 时按当时的密钥归类。更早的模型步骤没有这列，
    就按模型当前绑定的密钥归类。MCP 旧步骤只在该版本恰好挂了一个带密钥的服务时归入。
    """
    endpoints = db.query(ModelEndpoint).all()
    by_name: dict[str, list] = {}
    for row in endpoints:
        by_name.setdefault(row.name, []).append(row)
    secrets = {row.id: row for row in db.query(Secret).all()}
    spaces = {space.id: space.name for space in db.query(Space).all()}
    builtin = {row.name for row in db.query(Tool).all()}
    version_ids = {trace.version_id for _step, trace in step_rows if trace.version_id} or {-1}
    versions = {row.id: row for row in db.query(AgentVersion).filter(AgentVersion.id.in_(version_ids)).all()}
    server_ids = set()
    for version in versions.values():
        server_ids.update(load(version.mcp_ids_json, []) or [])
    servers = {row.id: row for row in db.query(McpServer).filter(McpServer.id.in_(server_ids or {-1})).all()}
    model_buckets: dict[int, dict] = {}
    mcp_buckets: dict[int, dict] = {}

    def bucket(store: dict, secret_id: int) -> dict:
        secret = secrets.get(secret_id)
        return store.setdefault(
            secret_id,
            {
                "secret_id": secret_id,
                "name": secret.name if secret else f"已删除 #{secret_id}",
                "department": spaces.get(secret.space_id, "") if secret else "",
                "calls": 0,
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": 0,
                "by_day": {},
                "by_department": {},
            },
        )

    def add(row: dict, trace: Trace, tokens_in: int, tokens_out: int, cost: float) -> None:
        row["calls"] += 1
        row["tokens_in"] += tokens_in
        row["tokens_out"] += tokens_out
        row["tokens"] += tokens_in + tokens_out
        row["cost"] = round(row["cost"] + cost, 6)
        day_key = trace.created_at.strftime("%m-%d") if trace.created_at else "未知"
        day = row["by_day"].setdefault(day_key, {"day": day_key, "calls": 0, "tokens": 0, "cost": 0})
        day["calls"] += 1
        day["tokens"] += tokens_in + tokens_out
        day["cost"] = round(day["cost"] + cost, 6)
        dept = row["by_department"].setdefault(
            trace.space_id,
            {"space_id": trace.space_id, "department": spaces.get(trace.space_id, ""), "calls": 0, "tokens": 0, "cost": 0},
        )
        dept["calls"] += 1
        dept["tokens"] += tokens_in + tokens_out
        dept["cost"] = round(dept["cost"] + cost, 6)

    def model_secret(step: TraceStep, trace: Trace):
        if step.secret_id:
            return step.secret_id
        matches = by_name.get(step.model) or []
        ranked = sorted(matches, key=lambda item: (item.space_id != trace.space_id, item.secret_id is None, item.id))
        for item in ranked:
            if item.secret_id and (item.space_id in (trace.space_id, None) or len(matches) == 1):
                return item.secret_id
        return None

    def mcp_secret(step: TraceStep, trace: Trace):
        if step.secret_id:
            return step.secret_id
        if not step.name or step.name in builtin:
            return None
        version = versions.get(trace.version_id)
        linked = [servers[item] for item in load(version.mcp_ids_json, []) if item in servers] if version else []
        keyed = [item for item in linked if item.secret_id]
        if len(keyed) == 1:
            return keyed[0].secret_id
        return None

    for step, trace in step_rows:
        if step.model and not step.fallback:
            secret_id = model_secret(step, trace)
            if secret_id:
                add(bucket(model_buckets, secret_id), trace, step.tokens_in or 0, step.tokens_out or 0, step.cost or 0)
            continue
        if step.kind == "execute":
            secret_id = mcp_secret(step, trace)
            if secret_id:
                add(bucket(mcp_buckets, secret_id), trace, 0, 0, 0)

    def finish_rows(store: dict) -> list:
        rows = []
        for row in store.values():
            row["by_day"] = sorted(row["by_day"].values(), key=lambda item: item["day"])
            row["by_department"] = list(row["by_department"].values())
            rows.append(row)
        rows.sort(key=lambda item: (-item["calls"], item["name"]))
        return rows

    def rollup(rows: list, fields: tuple[str, ...]) -> dict:
        by_day: dict[str, dict] = {}
        by_dept: dict[int, dict] = {}
        for row in rows:
            for day in row["by_day"]:
                slot = by_day.setdefault(day["day"], {"day": day["day"], **{key: 0 for key in fields}})
                for key in fields:
                    _touch(slot, key, day.get(key, 0), money=key == "cost")
            for dept in row["by_department"]:
                slot = by_dept.setdefault(
                    dept["space_id"],
                    {"space_id": dept["space_id"], "department": dept["department"], **{key: 0 for key in fields}},
                )
                for key in fields:
                    _touch(slot, key, dept.get(key, 0), money=key == "cost")
        return {
            "calls": sum(row["calls"] for row in rows),
            "tokens": sum(row["tokens"] for row in rows),
            "cost": round(sum(row["cost"] for row in rows), 6),
            "by_day": sorted(by_day.values(), key=lambda item: item["day"]),
            "by_department": list(by_dept.values()),
            "items": rows,
        }

    return {
        "model_secrets": rollup(finish_rows(model_buckets), ("calls", "tokens", "cost")),
        "mcp_secrets": rollup(finish_rows(mcp_buckets), ("calls",)),
    }


def _scope_space_ids(db, ctx, scope: str, space_id: int) -> list[int]:
    if is_platform(ctx.user) and space_id:
        chosen = db.get(Space, space_id)
        if chosen is None:
            raise HTTPException(404, "部门不存在")
        return [chosen.id]
    if is_platform(ctx.user) and scope == "all":
        return [item["id"] for item in _spaces_for(db, ctx.user)]
    return [ctx.space.id]


def _agent_pulse(db, agent: Agent, traces: list[Trace], pending: int) -> dict:
    """单智能体监控卡片：生命周期、窗口用量、当前执行与告警。"""
    stable = db.get(AgentVersion, agent.stable_version_id) if agent.stable_version_id else None
    latest = (
        db.query(AgentVersion)
        .filter_by(agent_id=agent.id)
        .order_by(AgentVersion.number.desc())
        .first()
    )
    model = db.get(ModelEndpoint, latest.model_id) if latest and latest.model_id else None
    space = db.get(Space, agent.space_id) if agent.space_id else None
    running = [row for row in traces if row.status == "running"]
    finished = [row for row in traces if row.status in {"completed", "failed", "denied", "cancelled", "blocked"}]
    completed = [row for row in finished if row.status == "completed"]
    failed = [row for row in traces if row.status in {"failed", "denied", "blocked"}]
    cost = round(sum(row.cost or 0 for row in traces), 6)
    tokens = sum((row.tokens_in or 0) + (row.tokens_out or 0) for row in traces)
    latency = int(sum(row.latency_ms or 0 for row in traces) / len(traces)) if traces else 0
    success_rate = round(len(completed) / len(finished), 4) if finished else None
    last = max(traces, key=lambda row: row.created_at or datetime.min) if traces else None
    draft_ahead = bool(
        latest
        and (
            latest.status == "draft"
            or (stable is not None and latest.number > stable.number)
            or (stable is None and latest.status == "draft")
        )
    )
    if agent.status == "offline":
        pulse = "offline"
        pulse_label = "已下线"
    elif running:
        pulse = "running"
        pulse_label = f"执行中 · {len(running)}"
    elif agent.status == "published":
        pulse = "ready"
        pulse_label = "可服务"
    elif agent.status == "draft":
        pulse = "draft"
        pulse_label = "未发布"
    else:
        pulse = agent.status or "unknown"
        pulse_label = agent.status or "未知"
    alerts = []
    if agent.status == "offline":
        alerts.append("已下线，操作者不可用")
    if draft_ahead and agent.status == "published":
        alerts.append("有未发布草稿")
    if pending:
        alerts.append(f"{pending} 条待审批")
    if failed:
        alerts.append(f"窗口内失败 {len(failed)} 次")
    if model and (model.quota_limit or 0) > 0:
        remain = max(0, model.quota_limit - (model.quota_used or 0))
        if remain <= model.quota_limit * 0.2:
            alerts.append("绑定模型额度将尽")
    tool_count = len(load(latest.tool_ids_json, [])) if latest else 0
    skill_count = len(load(latest.skill_ids_json, [])) if latest else 0
    mcp_count = len(load(latest.mcp_ids_json, [])) if latest else 0
    duty = ((latest.system_prompt if latest else "") or "").strip().split("\n", 1)[0][:72]
    return {
        "id": agent.id,
        "name": agent.name,
        "key": agent.key,
        "space_id": agent.space_id,
        "department": space.name if space else "平台",
        "status": agent.status,
        "pulse": pulse,
        "pulse_label": pulse_label,
        "running": len(running),
        "stable_version": stable.number if stable else None,
        "latest_version": latest.number if latest else None,
        "draft_ahead": draft_ahead,
        "model_id": model.id if model else None,
        "model_name": model.name if model else "按任务自动选",
        "tool_count": tool_count,
        "skill_count": skill_count,
        "mcp_count": mcp_count,
        "duty": duty,
        "calls": len(traces),
        "cost": cost,
        "tokens": tokens,
        "latency_ms": latency,
        "success_rate": success_rate,
        "failed": len(failed),
        "pending_approvals": pending,
        "alerts": alerts,
        "attention": bool(alerts),
        "last_at": stamp(last.created_at) if last and last.created_at else "",
        "last_status": last.status if last else "",
        "last_input": ((last.input_text if last else "") or "").strip()[:72],
        "last_trace_id": last.id if last else "",
        "href": f"/agents/{agent.id}",
        "traces_href": f"/traces?exact_field=agent_id&exact={agent.id}",
    }


@router.get("/api/observability/agents")
def agent_monitor(
    ctx=Depends(get_context),
    db=Depends(get_db),
    scope: str = "space",
    space_id: int = 0,
    time_from: str = "",
    time_to: str = "",
):
    """总览用的智能体实时监控。窗口统计跟时间筛选走；执行中数量看当前库状态。"""
    require(ctx, "viewer")
    space_ids = _scope_space_ids(db, ctx, scope, space_id)
    if not space_ids:
        space_ids = [-1]
    query = db.query(Agent)
    if is_platform(ctx.user) and scope == "all" and not space_id:
        query = query.filter((Agent.space_id.in_(space_ids)) | (Agent.space_id.is_(None)))
    else:
        query = query.filter(Agent.space_id.in_(space_ids))
    agents = query.order_by(Agent.id.asc()).all()
    agent_ids = [row.id for row in agents] or [-1]
    desk = Desk(time_from=time_from, time_to=time_to)
    window_traces = apply_desk(
        db.query(Trace).filter(Trace.agent_id.in_(agent_ids)),
        desk,
        text=[],
        exact={},
        time_col=Trace.created_at,
    ).all()
    live_running = (
        db.query(Trace)
        .filter(Trace.agent_id.in_(agent_ids), Trace.status == "running")
        .all()
    )
    by_agent: dict[int, list[Trace]] = {item: [] for item in agent_ids if item != -1}
    for row in window_traces:
        if row.agent_id in by_agent:
            by_agent[row.agent_id].append(row)
    # 执行中不在时间窗内时仍并入，保证实时态
    for row in live_running:
        bucket = by_agent.get(row.agent_id)
        if bucket is None:
            continue
        if row.id not in {item.id for item in bucket}:
            bucket.append(row)
    from sqlalchemy import func

    pending_by = {
        int(agent_id): int(count)
        for agent_id, count in (
            db.query(Approval.agent_id, func.count(Approval.id))
            .filter(Approval.agent_id.in_(agent_ids), Approval.status == "pending")
            .group_by(Approval.agent_id)
            .all()
        )
    }
    items = [_agent_pulse(db, agent, by_agent.get(agent.id, []), pending_by.get(agent.id, 0)) for agent in agents]
    items.sort(key=lambda item: (-item["running"], -item["attention"], -item["calls"], item["name"]))
    return {
        "checked_at": stamp(datetime.utcnow()),
        "total": len(items),
        "running": sum(1 for item in items if item["running"]),
        "attention": sum(1 for item in items if item["attention"]),
        "ready": sum(1 for item in items if item["pulse"] == "ready"),
        "items": items,
    }


def _workflow_pulse(db, workflow: Workflow, runs: list[WorkflowRun]) -> dict:
    """单工作流监控卡片：发布态、定时、窗口执行量与告警。"""
    space = db.get(Space, workflow.space_id)
    definition = load(workflow.definition_json, {})
    nodes = [node for node in (definition.get("nodes") or []) if isinstance(node, dict)]
    agent_nodes = [node for node in nodes if node.get("type") == "agent" or node.get("agent_id")]
    kind = definition.get("kind") or "agents"
    kind_label = "自主调度" if kind == "autonomous" else "固定步骤"
    active = [row for row in runs if row.status in {"running", "paused"}]
    finished = [row for row in runs if row.status in {"completed", "failed", "denied", "cancelled", "blocked", "stopped"}]
    completed = [row for row in finished if row.status == "completed"]
    failed = [row for row in runs if row.status in {"failed", "denied", "blocked"}]
    duration = int(sum(row.duration_ms or 0 for row in runs) / len(runs)) if runs else 0
    success_rate = round(len(completed) / len(finished), 4) if finished else None
    last = max(runs, key=lambda row: row.started_at or datetime.min) if runs else None
    cost = 0.0
    tokens = 0
    for row in runs:
        if not row.trace_id:
            continue
        trace = db.get(Trace, row.trace_id)
        if trace is None:
            continue
        cost += float(trace.cost or 0)
        tokens += int(trace.tokens_in or 0) + int(trace.tokens_out or 0)
    cost = round(cost, 6)
    release = workflow.release or "draft"
    if not workflow.enabled:
        pulse = "offline"
        pulse_label = "已停用"
    elif active:
        pulse = "running"
        running_n = sum(1 for row in active if row.status == "running")
        paused_n = sum(1 for row in active if row.status == "paused")
        if running_n and paused_n:
            pulse_label = f"执行中 {running_n} · 暂停 {paused_n}"
        elif paused_n:
            pulse_label = f"已暂停 · {paused_n}"
        else:
            pulse_label = f"执行中 · {running_n}"
    elif release != "published":
        pulse = "draft"
        pulse_label = "草稿"
    else:
        pulse = "ready"
        pulse_label = "可执行"
    alerts = []
    if not workflow.enabled:
        alerts.append("已停用，不能执行")
    if release != "published":
        alerts.append("未发布，操作者不能执行")
    if failed:
        alerts.append(f"窗口内失败 {len(failed)} 次")
    if workflow.schedule_enabled:
        budget = int(workflow.schedule_token_budget or 0)
        used = int(workflow.schedule_token_used or 0)
        if budget > 0 and used >= budget:
            alerts.append("定时 token 额度已用尽")
        elif space is not None and space.token_used >= space.token_budget:
            alerts.append("部门 token 额度已用尽，定时将停")
    entry_agent = db.get(Agent, int(agent_nodes[0].get("agent_id") or 0)) if agent_nodes else None
    return {
        "id": workflow.id,
        "name": workflow.name,
        "space_id": workflow.space_id,
        "department": space.name if space else "",
        "enabled": bool(workflow.enabled),
        "release": release,
        "kind": kind,
        "kind_label": kind_label,
        "pulse": pulse,
        "pulse_label": pulse_label,
        "running": sum(1 for row in active if row.status == "running"),
        "paused": sum(1 for row in active if row.status == "paused"),
        "node_count": len(agent_nodes),
        "entry_agent": entry_agent.name if entry_agent else "",
        "schedule_enabled": bool(workflow.schedule_enabled),
        "schedule_minutes": int(workflow.schedule_minutes or 0),
        "schedule_next_at": stamp(workflow.schedule_next_at) if workflow.schedule_next_at else "",
        "schedule_token_budget": int(workflow.schedule_token_budget or 0),
        "schedule_token_used": int(workflow.schedule_token_used or 0),
        "calls": len(runs),
        "cost": cost,
        "tokens": tokens,
        "latency_ms": duration,
        "success_rate": success_rate,
        "failed": len(failed),
        "alerts": alerts,
        "attention": bool(alerts),
        "last_at": stamp(last.started_at) if last and last.started_at else "",
        "last_status": last.status if last else "",
        "last_input": ((last.message if last else "") or "").strip()[:72],
        "last_run_id": last.id if last else None,
        "href": f"/workflows?open={workflow.id}",
        "runs_href": f"/workflow-runs?workflow_id={workflow.id}",
    }


@router.get("/api/observability/workflows")
def workflow_monitor(
    ctx=Depends(get_context),
    db=Depends(get_db),
    scope: str = "space",
    space_id: int = 0,
    time_from: str = "",
    time_to: str = "",
):
    """总览用的工作流实时监控。窗口统计跟时间筛选走；执行中/暂停看当前库状态。"""
    require(ctx, "viewer")
    space_ids = _scope_space_ids(db, ctx, scope, space_id)
    if not space_ids:
        space_ids = [-1]
    workflows = (
        db.query(Workflow)
        .filter(Workflow.space_id.in_(space_ids))
        .order_by(Workflow.id.asc())
        .all()
    )
    workflow_ids = [row.id for row in workflows] or [-1]
    desk = Desk(time_from=time_from, time_to=time_to)
    window_runs = apply_desk(
        db.query(WorkflowRun).filter(WorkflowRun.workflow_id.in_(workflow_ids)),
        desk,
        text=[],
        exact={},
        time_col=WorkflowRun.started_at,
    ).all()
    live_active = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.workflow_id.in_(workflow_ids), WorkflowRun.status.in_(["running", "paused"]))
        .all()
    )
    by_flow: dict[int, list[WorkflowRun]] = {item: [] for item in workflow_ids if item != -1}
    for row in window_runs:
        if row.workflow_id in by_flow:
            by_flow[row.workflow_id].append(row)
    for row in live_active:
        bucket = by_flow.get(row.workflow_id)
        if bucket is None:
            continue
        if row.id not in {item.id for item in bucket}:
            bucket.append(row)
    items = [_workflow_pulse(db, row, by_flow.get(row.id, [])) for row in workflows]
    items.sort(key=lambda item: (-(item["running"] + item["paused"]), -item["attention"], -item["calls"], item["name"]))
    return {
        "checked_at": stamp(datetime.utcnow()),
        "total": len(items),
        "running": sum(1 for item in items if item["running"]),
        "paused": sum(1 for item in items if item["paused"]),
        "attention": sum(1 for item in items if item["attention"]),
        "ready": sum(1 for item in items if item["pulse"] == "ready"),
        "items": items,
    }


@router.get("/api/observability/summary")
def summary(ctx=Depends(get_context), db=Depends(get_db), scope: str = "space", space_id: int = 0, time_from: str = "", time_to: str = ""):
    space_ids = _scope_space_ids(db, ctx, scope, space_id)
    if not space_ids:
        space_ids = [-1]
    desk = Desk(time_from=time_from, time_to=time_to)
    traces = apply_desk(db.query(Trace).filter(Trace.space_id.in_(space_ids)), desk, text=[], exact={}, time_col=Trace.created_at).all()
    traffic = len(traces)
    cost = round(sum(row.cost for row in traces), 6)
    latency = int(sum(row.latency_ms for row in traces) / traffic) if traffic else 0
    finished = [row for row in traces if row.status in {"completed", "failed", "denied", "cancelled", "blocked"}]
    completed = [row for row in finished if row.status == "completed"]
    quality = round(len(completed) / len(finished), 4) if finished else 1
    votes = [row for row in traces if row.quality_vote is not None]
    positive = round(sum(1 for row in votes if row.quality_vote > 0) / len(votes), 4) if votes else None
    by_env: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_day: dict[str, dict] = {}
    by_space: dict[int, dict] = {}
    for row in traces:
        by_env[row.environment] = by_env.get(row.environment, 0) + 1
        by_status[row.status] = by_status.get(row.status, 0) + 1
        day_key = row.created_at.strftime("%m-%d") if row.created_at else "未知"
        day = by_day.setdefault(day_key, {"day": day_key, "traffic": 0, "cost": 0, "tokens": 0})
        day["traffic"] += 1
        day["cost"] = round(day["cost"] + row.cost, 6)
        day["tokens"] += (row.tokens_in or 0) + (row.tokens_out or 0)
        bucket = by_space.setdefault(row.space_id, {"space_id": row.space_id, "traffic": 0, "cost": 0, "tokens": 0})
        bucket["traffic"] += 1
        bucket["cost"] = round(bucket["cost"] + row.cost, 6)
        bucket["tokens"] += (row.tokens_in or 0) + (row.tokens_out or 0)
    spaces = {space.id: space.name for space in db.query(Space).filter(Space.id.in_(space_ids)).all()}
    for space_id in space_ids:
        bucket = by_space.setdefault(space_id, {"space_id": space_id, "traffic": 0, "cost": 0, "tokens": 0})
        bucket["department"] = spaces.get(space_id, "")
        bucket["agents"] = db.query(Agent).filter_by(space_id=space_id).count()
        bucket["workflows"] = db.query(Workflow).filter_by(space_id=space_id).count()
        bucket["knowledge_bases"] = db.query(KnowledgeBase).filter_by(space_id=space_id).count()
        bucket["memories"] = db.query(Memory).filter_by(space_id=space_id).count()
        bucket["models"] = len(ids_for(db, space_id, "model"))
        bucket["tools"] = len(ids_for(db, space_id, "tool"))
    step_query = db.query(TraceStep, Trace).join(Trace, Trace.id == TraceStep.trace_id).filter(Trace.space_id.in_(space_ids))
    step_rows = apply_desk(step_query, desk, text=[], exact={}, time_col=Trace.created_at).all()
    by_model: dict[str, dict] = {}
    for step, _trace in step_rows:
        if not step.model:
            continue
        bucket = by_model.setdefault(step.model, {"model": step.model, "steps": 0, "cost": 0, "tokens_in": 0, "tokens_out": 0})
        bucket["steps"] += 1
        bucket["cost"] = round(bucket["cost"] + step.cost, 6)
        bucket["tokens_in"] += step.tokens_in or 0
        bucket["tokens_out"] += step.tokens_out or 0
    model_ids = visible_ids(db, space_ids, "model") or [-1]
    endpoints = db.query(ModelEndpoint).filter(ModelEndpoint.id.in_(model_ids)).all()
    quota_by_name: dict[str, dict] = {}
    for endpoint in endpoints:
        quota = quota_by_name.setdefault(endpoint.name, {"quota_limit": 0, "quota_used": 0, "unlimited": False})
        quota["quota_used"] = round(quota["quota_used"] + (endpoint.quota_used or 0), 6)
        if not (endpoint.quota_limit or 0):
            quota["unlimited"] = True
        else:
            quota["quota_limit"] = round(quota["quota_limit"] + endpoint.quota_limit, 6)
    for bucket in by_model.values():
        quota = quota_by_name.get(bucket["model"], {"quota_used": bucket["cost"], "quota_limit": 0, "unlimited": True})
        bucket["calls"] = bucket["steps"]
        bucket["tokens"] = bucket["tokens_in"] + bucket["tokens_out"]
        bucket["quota_used"] = quota["quota_used"]
        bucket["quota_limit"] = None if quota["unlimited"] else quota["quota_limit"]
        bucket["quota_remaining"] = None if bucket["quota_limit"] is None else round(max(0, bucket["quota_limit"] - bucket["quota_used"]), 6)
    limited = [item for item in endpoints if (item.quota_limit or 0) > 0]
    quota_used_total = round(sum((item.quota_used or 0) for item in endpoints), 6)
    quota_remaining_total = round(sum(max(0, item.quota_limit - (item.quota_used or 0)) for item in limited), 6)
    user_ids = {row.user_id for row in traces} or {-1}
    usernames = {user.id: user.username for user in db.query(User).filter(User.id.in_(user_ids)).all()}
    by_user: dict[tuple, dict] = {}
    for row in traces:
        key = (row.user_id, row.space_id)
        person = by_user.setdefault(
            key,
            {
                "user_id": row.user_id,
                "username": usernames.get(row.user_id, ""),
                "department": spaces.get(row.space_id, ""),
                "traffic": 0,
                "cost": 0,
            },
        )
        person["traffic"] += 1
        person["cost"] = round(person["cost"] + row.cost, 6)
    questions: dict[str, int] = {}
    for row in traces:
        text = (row.input_text or "").strip()
        if not text:
            continue
        text = text[:80]
        questions[text] = questions.get(text, 0) + 1
    hot_questions = [{"question": text, "count": count} for text, count in sorted(questions.items(), key=lambda item: item[1], reverse=True)[:8]]
    # 资源数量按部门授权统计，不跟着调用时间窗口走。同一资源授给多个部门只计一次。
    tool_ids = visible_ids(db, space_ids, "tool") or [-1]
    knowledge_ids = visible_ids(db, space_ids, "knowledge") or [-1]
    resources = {
        "tools": db.query(Tool).filter(Tool.id.in_(tool_ids)).count(),
        "memories": db.query(Memory).filter(Memory.space_id.in_(space_ids)).count(),
        "knowledge_bases": db.query(KnowledgeBase).filter(KnowledgeBase.id.in_(knowledge_ids)).count(),
        "documents": db.query(Document).filter(Document.base_id.in_(knowledge_ids)).count(),
    }
    return {
        "traffic": traffic,
        "cost": cost,
        "latency_ms": latency,
        "quality": quality,
        "positive_feedback_rate": positive,
        "tokens": sum((row.tokens_in or 0) + (row.tokens_out or 0) for row in traces),
        "tokens_in": sum(row.tokens_in or 0 for row in traces),
        "tokens_out": sum(row.tokens_out or 0 for row in traces),
        "by_environment": by_env,
        "by_status": by_status,
        "by_day": sorted(by_day.values(), key=lambda item: item["day"]),
        "by_department": list(by_space.values()),
        "by_model": list(by_model.values()),
        "by_user": list(by_user.values()),
        "hot_questions": hot_questions,
        "quota_used_total": quota_used_total,
        "quota_remaining_total": quota_remaining_total,
        "resources": resources,
        **_secret_board(db, step_rows),
    }

