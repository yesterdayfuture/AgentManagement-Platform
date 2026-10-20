"""模型端点的登记、授权范围和调用记录。密钥加密后写入保险库，响应里不回明文。"""

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

def _model_dict(db, row: ModelEndpoint) -> dict:
    secret = db.get(Secret, row.secret_id) if row.secret_id else None
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url or "",
        "task_types": load(row.task_types_json, []),
        "price_in": row.price_in,
        "price_out": row.price_out,
        "priority": row.priority,
        "enabled": row.enabled,
        "quota_limit": row.quota_limit or 0,
        "quota_used": round(row.quota_used or 0, 6),
        "quota_remaining": None if not (row.quota_limit or 0) else round(max(0, row.quota_limit - (row.quota_used or 0)), 6),
        "secret_id": row.secret_id,
        "has_api_key": bool(row.secret_id),
        "secret_name": secret.name if secret else "",
        "space_id": row.space_id,
        "origin": row.origin or "department",
        "created_at": stamp(row.created_at),
        **_attribution(db, row.created_by, row.space_id),
    }


def _store_key(db, ctx, name: str, api_key: str, space_id: int | None = None) -> int:
    """密钥落在模型所属部门；平台模型没有部门时记入当前查看的部门保险库。"""
    home = space_id if space_id is not None else ctx.space.id
    row = Secret(space_id=home, name=f"{name}-key", ciphertext=encrypt(api_key), created_by=ctx.user.id)
    db.add(row)
    db.flush()
    audit(db, home, ctx.user.username, "secret.create", row.name)
    return row.id


def _drop_orphan_secret(db, secret_id: int | None) -> None:
    """密钥被替换后，若没有模型或 MCP 再引用，则删掉，避免孤儿密文。"""
    if not secret_id:
        return
    if db.query(ModelEndpoint).filter_by(secret_id=secret_id).count():
        return
    if db.query(McpServer).filter_by(secret_id=secret_id).count():
        return
    row = db.get(Secret, secret_id)
    if row is not None:
        audit(db, row.space_id, "system", "secret.delete", row.name, "替换后清理")
        db.delete(row)


def _load_model(db, ctx, model_id: int, write: bool = False) -> ModelEndpoint:
    row = db.get(ModelEndpoint, model_id)
    if not _can_read(db, ctx, "model", row):
        raise HTTPException(404, "模型不存在")
    if write and not _can_write(ctx, row):
        raise HTTPException(403, "平台模型只有平台管理员可以修改")
    return row


@router.get("/api/models")
def list_models(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), usable_space: int | None = Query(default=None)):
    query = scope_grants(db, ctx, _visible(db, ctx, ModelEndpoint, "model"), ModelEndpoint, "model", usable_space)
    return finish(
        query,
        lambda row: _model_dict(db, row),
        desk,
        order=ModelEndpoint.id.desc(),
        text=[ModelEndpoint.name, ModelEndpoint.provider, ModelEndpoint.model, ModelEndpoint.base_url],
        exact={"name": ModelEndpoint.name, "provider": ModelEndpoint.provider, "model": ModelEndpoint.model, "secret_id": ModelEndpoint.secret_id},
        time_col=ModelEndpoint.created_at,
    )


@router.post("/api/models")
def create_model(body: ModelIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    space_id, origin = _own(ctx)
    secret_id = body.secret_id
    if body.api_key.strip():
        secret_id = _store_key(db, ctx, body.name, body.api_key.strip(), space_id=space_id)
    row = ModelEndpoint(
        space_id=space_id,
        origin=origin,
        name=body.name,
        provider=body.provider,
        model=body.model,
        base_url=body.base_url.strip(),
        secret_id=secret_id,
        task_types_json=dump(body.task_types),
        price_in=body.price_in,
        price_out=body.price_out,
        priority=body.priority,
        enabled=body.enabled,
        quota_limit=max(0, body.quota_limit),
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    if origin == "department":
        grant(db, ctx.space.id, "model", row.id)
    audit(db, ctx.space.id, ctx.user.username, "model.create", body.name, origin)
    return _model_dict(db, row)


@router.patch("/api/models/{model_id}")
def patch_model(model_id: int, body: ModelPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load_model(db, ctx, model_id, write=True)
    for field in ("name", "provider", "model", "base_url", "price_in", "price_out", "priority", "enabled"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value.strip() if isinstance(value, str) else value)
    if body.task_types is not None:
        row.task_types_json = dump(body.task_types)
    if body.api_key and body.api_key.strip():
        old_secret = row.secret_id
        row.secret_id = _store_key(db, ctx, row.name, body.api_key.strip(), space_id=row.space_id)
        db.flush()
        _drop_orphan_secret(db, old_secret)
    if body.quota_limit is not None and body.quota_limit != (row.quota_limit or 0):
        require(ctx, "admin")
        row.quota_limit = max(0, body.quota_limit)
    audit(db, ctx.space.id, ctx.user.username, "model.patch", row.name, f"额度 {row.quota_limit}")
    return _model_dict(db, row)


def _drop_model(db, ctx, model_id: int) -> None:
    require(ctx, "builder")
    row = _load_model(db, ctx, model_id, write=True)
    old_secret = row.secret_id
    audit(db, ctx.space.id, ctx.user.username, "model.delete", row.name)
    drop_resource(db, "model", row.id)
    db.delete(row)
    db.flush()
    _drop_orphan_secret(db, old_secret)


@router.get("/api/models/{model_id}/calls")
def model_calls(model_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    """这个模型的调用历史：问题、回复和调用者。

    部门用户只看本部门的轨迹。平台管理员看所有部门，包含问答里产生的调用。
    """
    row = db.get(ModelEndpoint, model_id)
    if not _can_read(db, ctx, "model", row):
        raise HTTPException(404, "模型不存在")
    query = (
        db.query(TraceStep, Trace)
        .join(Trace, Trace.id == TraceStep.trace_id)
        .filter(TraceStep.model == row.name)
    )
    if not is_platform(ctx.user):
        query = query.filter(Trace.space_id == ctx.space.id)
    pairs = query.order_by(Trace.created_at.desc(), TraceStep.id.desc()).limit(50).all()
    users = {user.id: user.username for user in db.query(User).filter(User.id.in_({trace.user_id for _step, trace in pairs} or {-1})).all()}
    spaces = {space.id: space.name for space in db.query(Space).filter(Space.id.in_({trace.space_id for _step, trace in pairs} or {-1})).all()}
    return [
        {
            "id": step.id,
            "trace_id": trace.id,
            "at": stamp(trace.created_at),
            "username": users.get(trace.user_id, ""),
            "department": spaces.get(trace.space_id, ""),
            "kind": step.kind,
            "input": trace.input_text,
            "prompt": step.prompt,
            "output": step.output_text,
            "status": trace.status,
            "cost": step.cost,
            "cost_in": step.cost_in or 0,
            "cost_out": step.cost_out or 0,
            "tokens": step.tokens,
            "tokens_in": step.tokens_in or 0,
            "tokens_out": step.tokens_out or 0,
        }
        for step, trace in pairs
    ]


@router.delete("/api/models/{model_id}")
def delete_model(model_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_model(db, ctx, model_id)
    return {"ok": True}


@router.post("/api/models/bulk-delete")
def delete_models(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_model(db, ctx, item_id))

