"""按空间、用户和智能体隔离的记忆。导出和导入不携带密钥。"""

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

@router.get("/api/memories")
def memories(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), agent_id: int | None = None, project_id: str = ""):
    if is_platform(ctx.user):
        rows = db.query(Memory).order_by(Memory.id.desc()).all()
    else:
        rows = list_memories(
            db,
            ctx.policy,
            space_id=ctx.space.id,
            user_id=ctx.user.id,
            agent_id=agent_id,
            project_id=project_id,
            role_user_id=ctx.user.id,
        )
    if desk.q:
        rows = [row for row in rows if desk.q in (row.content or "")]
    if desk.exact and desk.exact_field == "kind":
        rows = [row for row in rows if row.kind == desk.exact]
    if desk.exact and desk.exact_field == "content":
        rows = [row for row in rows if row.content == desk.exact]
    start, end, exclusive = window(desk)
    if start:
        rows = [row for row in rows if row.created_at and row.created_at >= start]
    if end:
        rows = [row for row in rows if row.created_at and (row.created_at < end if exclusive else row.created_at <= end)]
    return finish_rows(rows, lambda row: _memory_dict(db, row), desk)


def _memory_dict(db, row: Memory) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "content": row.content,
        "agent_id": row.agent_id,
        "project_id": row.project_id,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": stamp(row.created_at),
        **_attribution(db, row.created_by or row.user_id, row.space_id),
    }


@router.post("/api/memories")
def create_memory(body: MemoryIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    if body.ttl_seconds is None:
        expires = datetime.utcnow() + timedelta(days=ctx.policy.retention_days)
    elif body.ttl_seconds <= 0:
        expires = datetime.utcnow() - timedelta(seconds=1)
    else:
        expires = datetime.utcnow() + timedelta(seconds=body.ttl_seconds)
    row = Memory(
        space_id=_place(db, ctx, body.space_id),
        agent_id=body.agent_id,
        user_id=ctx.user.id,
        project_id=body.project_id,
        kind=body.kind,
        content=body.content,
        created_by=ctx.user.id,
        expires_at=expires,
    )
    db.add(row)
    db.flush()
    return _memory_dict(db, row)


@router.patch("/api/memories/{memory_id}")
def patch_memory(memory_id: int, body: MemoryPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = db.get(Memory, memory_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "记忆不存在")
    if row.user_id != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "不能修改他人的记忆")
    if body.content is not None:
        row.content = body.content
    if body.kind is not None:
        row.kind = body.kind
    return _memory_dict(db, row)


@router.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    row = db.get(Memory, memory_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "记忆不存在")
    _drop_memory(db, ctx, memory_id)
    return {"ok": True}


def _drop_memory(db, ctx, memory_id: int) -> None:
    row = db.get(Memory, memory_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "记忆不存在")
    if row.user_id != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "不能删除他人的记忆")
    db.delete(row)


@router.post("/api/memories/bulk-delete")
def delete_memories(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_memory(db, ctx, item_id))


@router.get("/api/memories/export")
def export_memories(ctx=Depends(get_context), db=Depends(get_db)):
    rows = list_memories(
        db,
        ctx.policy,
        space_id=ctx.space.id,
        user_id=ctx.user.id,
        agent_id=None,
        project_id="",
        role_user_id=ctx.user.id,
    )
    return {"items": [_memory_dict(db, row) for row in rows]}


@router.post("/api/memories/import")
def import_memories(body: MemoryImport, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    created = []
    for item in body.items:
        expires = datetime.utcnow() + timedelta(days=ctx.policy.retention_days)
        row = Memory(
            space_id=_place(db, ctx, item.space_id),
            agent_id=item.agent_id,
            user_id=ctx.user.id,
            project_id=item.project_id,
            kind=item.kind,
            content=item.content,
            created_by=ctx.user.id,
            expires_at=expires,
        )
        db.add(row)
        db.flush()
        created.append(_memory_dict(db, row))
    return {"items": created}

