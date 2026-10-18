"""内置工具登记、审核，以及 MCP 服务的探测、列出和试调用。这里只保存登记信息，真正执行在运行时。"""

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

@router.get("/api/tools")
def list_tools(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), usable_space: int | None = Query(default=None)):
    query = scope_grants(db, ctx, _visible(db, ctx, Tool, "tool"), Tool, "tool", usable_space)
    return finish(
        query,
        lambda row: _tool_dict(db, row),
        desk,
        order=Tool.id.desc(),
        text=[Tool.name, Tool.description, Tool.owner, Tool.review_status, Tool.kind],
        exact={"name": Tool.name, "review_status": Tool.review_status, "risk": Tool.risk, "access": Tool.access},
        time_col=Tool.created_at,
    )


def _tool_dict(db, row: Tool) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "kind": row.kind,
        "access": row.access,
        "risk": row.risk,
        "owner": row.owner,
        "environment": row.environment,
        "endpoint": row.endpoint,
        "data_flow": row.data_flow,
        "review_status": row.review_status,
        "review_note": row.review_note,
        "rate_limit_per_min": row.rate_limit_per_min,
        "enabled": row.enabled,
        "space_id": row.space_id,
        "origin": row.origin or "department",
        "parameters": load(row.parameters_json, {}),
        "detail": detail_of(row.name, load(row.parameters_json, {})),
        "created_at": stamp(row.created_at),
        **_attribution(db, row.created_by, row.space_id),
    }


@router.post("/api/tools")
def create_tool(body: ToolIn, ctx=Depends(get_context), db=Depends(get_db)):
    """登记一条工具。这里不创建函数，只记录名称和发布信息，并进入待审核。"""
    require(ctx, "builder")
    if body.access not in {"read", "write"} or body.risk not in {"low", "medium", "high"}:
        raise HTTPException(400, "工具分类无效")
    space_id, origin = _own(ctx)
    row = Tool(
        space_id=space_id,
        origin=origin,
        name=body.name,
        description=body.description,
        kind=body.kind,
        access=body.access,
        risk=body.risk,
        owner=body.owner or ctx.user.username,
        environment=body.environment,
        endpoint=body.endpoint,
        data_flow=body.data_flow,
        review_status="pending",
        rate_limit_per_min=body.rate_limit_per_min,
        parameters_json=dump(body.parameters),
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    if origin == "department":
        grant(db, ctx.space.id, "tool", row.id)
    audit(db, ctx.space.id, ctx.user.username, "tool.register", body.name, body.risk)
    return _tool_dict(db, row)


def _load_tool(db, ctx, tool_id: int, write: bool = False) -> Tool:
    row = db.get(Tool, tool_id)
    if not _can_read(db, ctx, "tool", row):
        raise HTTPException(404, "工具不存在")
    if write:
        require(ctx, "builder")
        if not _can_write(ctx, row):
            raise HTTPException(403, "平台工具只有平台管理员可以修改")
    return row


@router.patch("/api/tools/{tool_id}")
def patch_tool(tool_id: int, body: ToolPatch, ctx=Depends(get_context), db=Depends(get_db)):
    row = _load_tool(db, ctx, tool_id, write=True)
    for field in ("name", "description", "kind", "access", "risk", "owner", "environment", "endpoint", "data_flow", "rate_limit_per_min", "enabled"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    audit(db, ctx.space.id, ctx.user.username, "tool.patch", row.name)
    return _tool_dict(db, row)


def _drop_tool(db, ctx, tool_id: int) -> None:
    row = _load_tool(db, ctx, tool_id, write=True)
    db.query(ToolGrant).filter_by(tool_id=row.id).delete()
    drop_resource(db, "tool", row.id)
    db.delete(row)


@router.delete("/api/tools/{tool_id}")
def delete_tool(tool_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_tool(db, ctx, tool_id)
    return {"ok": True}


@router.post("/api/tools/bulk-delete")
def delete_tools(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_tool(db, ctx, item_id))


@router.post("/api/tools/{tool_id}/review")
def review_tool(tool_id: int, body: ReviewIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "admin")
    row = db.get(Tool, tool_id)
    if not _can_read(db, ctx, "tool", row):
        raise HTTPException(404, "工具不存在")
    if not _can_write(ctx, row):
        raise HTTPException(403, "平台工具只有平台管理员可以审核")
    if body.decision not in {"approved", "rejected"}:
        raise HTTPException(400, "审核决定无效")
    if row.data_flow == "external" and body.decision == "approved" and not body.comment.strip():
        raise HTTPException(400, "外部数据流必须填写审核说明")
    row.review_status = body.decision
    row.review_note = body.comment
    audit(db, ctx.space.id, ctx.user.username, "tool.review", row.name, body.decision)
    return _tool_dict(db, row)


def _mcp_dict(db, row: McpServer) -> dict:
    secret = db.get(Secret, row.secret_id) if row.secret_id else None
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "enabled": row.enabled,
        "has_token": bool(row.secret_id),
        "secret_name": secret.name if secret else "",
        "created_at": stamp(row.created_at),
    }


def _mcp_token(db, row: McpServer) -> str:
    if not row.secret_id:
        return ""
    secret = db.get(Secret, row.secret_id)
    return decrypt(secret.ciphertext) if secret else ""


def _store_mcp_token(db, ctx, name: str, token: str) -> int:
    row = Secret(space_id=ctx.space.id, name=f"{name}-mcp", ciphertext=encrypt(token), created_by=ctx.user.id)
    db.add(row)
    db.flush()
    return row.id


@router.get("/api/mcp")
def list_mcp(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(McpServer).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        lambda row: _mcp_dict(db, row),
        desk,
        order=McpServer.id.desc(),
        text=[McpServer.name, McpServer.url],
        exact={"name": McpServer.name, "secret_id": McpServer.secret_id},
        time_col=McpServer.created_at,
    )


@router.post("/api/mcp/probe")
def probe_mcp(body: McpProbe, ctx=Depends(get_context)):
    require(ctx, "builder")
    try:
        return connect(body.url.strip(), body.token.strip())
    except McpError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/mcp")
def create_mcp(body: McpIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    if not body.name.strip() or not body.url.strip():
        raise HTTPException(400, "名称和地址不能为空")
    row = McpServer(space_id=ctx.space.id, name=body.name.strip(), url=body.url.strip(), enabled=True)
    if body.token.strip():
        row.secret_id = _store_mcp_token(db, ctx, row.name, body.token.strip())
    db.add(row)
    db.flush()
    audit(db, ctx.space.id, ctx.user.username, "mcp.create", row.name)
    return _mcp_dict(db, row)


def _load_mcp(db, ctx, server_id: int) -> McpServer:
    row = db.get(McpServer, server_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "MCP 服务不存在")
    return row


@router.patch("/api/mcp/{server_id}")
def patch_mcp(server_id: int, body: McpPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_mcp(db, ctx, server_id)
    if body.name is not None:
        row.name = body.name.strip()
    if body.url is not None:
        row.url = body.url.strip()
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.token and body.token.strip():
        row.secret_id = _store_mcp_token(db, ctx, row.name, body.token.strip())
    return _mcp_dict(db, row)


def _drop_mcp(db, ctx, server_id: int) -> None:
    require(ctx, "builder")
    row = _load_mcp(db, ctx, server_id)
    for agent in db.query(Agent).filter_by(space_id=ctx.space.id).all():
        latest = _latest(db, agent.id)
        if latest and row.id in load(latest.mcp_ids_json, []):
            raise HTTPException(409, "仍被智能体使用，不能删除")
    db.delete(row)


@router.delete("/api/mcp/{server_id}")
def delete_mcp(server_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_mcp(db, ctx, server_id)
    return {"ok": True}


@router.post("/api/mcp/bulk-delete")
def delete_mcp_many(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_mcp(db, ctx, item_id))


@router.post("/api/mcp/{server_id}/test")
def test_mcp(server_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_mcp(db, ctx, server_id)
    try:
        return connect(row.url, _mcp_token(db, row))
    except McpError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/mcp/{server_id}/tools")
def mcp_tools(server_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_mcp(db, ctx, server_id)
    try:
        found = connect(row.url, _mcp_token(db, row))
    except McpError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": row.id, "name": row.name, "url": row.url, "server": found["server"], "tools": found["tools"]}


@router.post("/api/mcp/{server_id}/call")
def mcp_call(server_id: int, body: McpCall, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_mcp(db, ctx, server_id)
    try:
        return call_tool(row.url, _mcp_token(db, row), body.name, body.arguments)
    except McpError as exc:
        raise HTTPException(400, str(exc)) from exc

