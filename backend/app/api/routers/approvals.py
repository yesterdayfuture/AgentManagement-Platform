"""高风险动作的审批。站内决定和外部渠道回调都落到同一条审批记录。"""

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

@router.get("/api/approvals")
def list_approvals(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(Approval).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        _approval_dict,
        desk,
        order=Approval.id.desc(),
        text=[Approval.tool_name, Approval.status, Approval.channel, Approval.comment],
        exact={"status": Approval.status, "tool_name": Approval.tool_name, "channel": Approval.channel},
        time_col=Approval.created_at,
    )


def _approval_dict(row: Approval) -> dict:
    payload = load(row.payload_json, {})
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "agent_id": row.agent_id,
        "tool_name": row.tool_name,
        "payload": payload,
        "chat_session_id": payload.get("chat_session_id") or "",
        "workflow_id": payload.get("workflow_id"),
        "task": payload.get("task") or "",
        "status": row.status,
        "channel": row.channel,
        "comment": row.comment,
        "created_at": stamp(row.created_at),
    }


def _refresh_draft_summary(db, payload: dict) -> dict:
    """改名或改节点后重算摘要，方便审批列表一眼看懂。"""
    definition = payload.get("definition") or {}
    nodes = definition.get("nodes") or []
    names = []
    for node in nodes:
        agent = db.get(Agent, int(node.get("agent_id") or 0))
        label = (agent.name if agent else None) or node.get("agent_name") or node.get("name") or f"智能体 {node.get('agent_id')}"
        names.append(str(label))
    kind = definition.get("kind") or payload.get("kind") or "agents"
    mode = "自主调度" if kind == "autonomous" else "固定步骤"
    summary = f"{mode} · {' → '.join(names)}" if names else mode
    payload["summary"] = summary
    payload["kind"] = kind
    return payload


@router.post("/api/approvals/{approval_id}/decision")
def decide(approval_id: int, body: DecisionIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = db.get(Approval, approval_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "审批不存在")
    if row.status != "pending":
        raise HTTPException(400, "审批已结束")
    try:
        return resume_approval(db, row, decision=body.decision, channel=body.channel, comment=body.comment, actor=ctx.user.username)
    except GateError as exc:
        _raise_gate(exc, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/approvals/inbound")
def inbound(body: InboundIn, db=Depends(get_db), x_channel_secret: str | None = Header(default=None)):
    if not x_channel_secret or not hmac_equal(x_channel_secret, channel_secret()):
        raise HTTPException(401, "渠道密钥无效")
    row = db.get(Approval, body.approval_id)
    if row is None:
        raise HTTPException(404, "审批不存在")
    if row.status != "pending":
        raise HTTPException(400, "审批已结束")
    try:
        return resume_approval(db, row, decision=body.decision, channel=body.channel, comment=body.comment, actor=body.channel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/api/approvals/{approval_id}")
def patch_approval(approval_id: int, body: ApprovalPatch, ctx=Depends(get_context), db=Depends(get_db)):
    """待审的工作流草案可改名、改节点；通过后才写入工作流列表。"""
    require(ctx, "operator")
    row = db.get(Approval, approval_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "审批不存在")
    if body.comment is not None:
        row.comment = body.comment
    drafting = body.name is not None or body.definition is not None
    if drafting:
        if row.tool_name != "工作流":
            raise HTTPException(400, "只有工作流草案可以改节点")
        if row.status != "pending":
            raise HTTPException(400, "已结束的审批不能再改草案")
        payload = load(row.payload_json, {})
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise HTTPException(400, "名称不能为空")
            payload["name"] = name[:120]
        if body.definition is not None:
            nodes = body.definition.get("nodes") or []
            if not nodes:
                raise HTTPException(400, "工作流草案没有节点")
            payload["definition"] = body.definition
            payload["kind"] = body.definition.get("kind") or payload.get("kind") or "agents"
        payload = _refresh_draft_summary(db, payload)
        row.payload_json = dump(payload)
        row.comment = payload.get("summary") or row.comment
        trace = db.get(Trace, row.trace_id)
        if trace is not None:
            trace.output_text = payload.get("summary") or trace.output_text
    return _approval_dict(row)


def _drop_approval(db, ctx, approval_id: int) -> None:
    require(ctx, "operator")
    row = db.get(Approval, approval_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "审批不存在")
    if row.status == "pending":
        raise HTTPException(400, "未完成的审批不能删除")
    db.delete(row)


@router.delete("/api/approvals/{approval_id}")
def delete_approval(approval_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_approval(db, ctx, approval_id)
    return {"ok": True}


@router.post("/api/approvals/bulk-delete")
def delete_approvals(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_approval(db, ctx, item_id))

