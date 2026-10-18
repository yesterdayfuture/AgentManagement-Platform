"""知识库、文档解析、切块和检索。上传文件先抽出正文，再按策略切块。"""

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

def _base_dict(db, row: KnowledgeBase) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "origin": row.origin or "department",
        "document_count": db.query(Document).filter_by(base_id=row.id).count(),
        "created_at": stamp(row.created_at),
        **_attribution(db, row.created_by, row.space_id),
    }


def _doc_dict(db, row: Document) -> dict:
    return {
        "id": row.id,
        "base_id": row.base_id,
        "title": row.title,
        "content": row.content,
        "visibility": row.visibility,
        "allowed_roles": load(row.allowed_roles_json, []),
        "source_name": row.source_name or "",
        "source_kind": row.source_kind or "text",
        "strategy": row.strategy or "paragraph",
        "applied_strategy": row.applied_strategy or row.strategy or "paragraph",
        "chunk_size": row.chunk_size or 500,
        "chunk_overlap": row.chunk_overlap or 0,
        "chunk_count": row.chunk_count or 0,
        "created_at": stamp(row.created_at),
        **_attribution(db, None, row.space_id),
    }


def _index_or_400(db, row: Document) -> None:
    try:
        index_document(db, row)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/knowledge")
def list_bases(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), usable_space: int | None = Query(default=None)):
    query = scope_grants(db, ctx, _visible(db, ctx, KnowledgeBase, "knowledge"), KnowledgeBase, "knowledge", usable_space)
    return finish(
        query,
        lambda row: _base_dict(db, row),
        desk,
        order=KnowledgeBase.id.desc(),
        text=[KnowledgeBase.name],
        exact={"name": KnowledgeBase.name},
        time_col=KnowledgeBase.created_at,
    )


@router.post("/api/knowledge")
def create_base(body: BaseIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    space_id = _place(db, ctx, body.space_id)
    origin = "platform" if is_platform(ctx.user) else "department"
    row = KnowledgeBase(space_id=space_id, name=body.name, origin=origin, created_by=ctx.user.id)
    db.add(row)
    db.flush()
    if origin == "department":
        grant(db, space_id, "knowledge", row.id)
    return _base_dict(db, row)


@router.patch("/api/knowledge/{base_id}")
def patch_base(base_id: int, body: BaseIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = db.get(KnowledgeBase, base_id)
    if not _can_read(db, ctx, "knowledge", row):
        raise HTTPException(404, "知识库不存在")
    if not _can_write(ctx, row):
        raise HTTPException(403, "平台知识库只有平台管理员可以修改")
    row.name = body.name
    return _base_dict(db, row)


def _drop_base(db, ctx, base_id: int) -> None:
    require(ctx, "builder")
    row = db.get(KnowledgeBase, base_id)
    if not _can_read(db, ctx, "knowledge", row):
        raise HTTPException(404, "知识库不存在")
    if not _can_write(ctx, row):
        raise HTTPException(403, "平台知识库只有平台管理员可以修改")
    doc_ids = [item.id for item in db.query(Document).filter_by(base_id=row.id).all()]
    if doc_ids:
        db.query(Chunk).filter(Chunk.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
    drop_resource(db, "knowledge", row.id)
    db.delete(row)


@router.delete("/api/knowledge/{base_id}")
def delete_base(base_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_base(db, ctx, base_id)
    return {"ok": True}


@router.post("/api/knowledge/bulk-delete")
def delete_bases(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_base(db, ctx, item_id))


@router.get("/api/knowledge/documents")
def list_documents(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), base_id: int | None = None):
    if is_platform(ctx.user):
        query = db.query(Document)
    else:
        allowed = ids_for(db, ctx.space.id, "knowledge") or [-1]
        query = db.query(Document).filter(Document.base_id.in_(allowed))
    if base_id:
        query = query.filter(Document.base_id == base_id)
    return finish(
        query,
        lambda row: _doc_dict(db, row),
        desk,
        order=Document.id.desc(),
        text=[Document.title, Document.content],
        exact={"title": Document.title, "visibility": Document.visibility, "base_id": Document.base_id},
        time_col=Document.created_at,
    )


@router.post("/api/knowledge/{base_id}/documents")
def create_document(base_id: int, body: DocIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    base = db.get(KnowledgeBase, base_id)
    if not _can_read(db, ctx, "knowledge", base):
        raise HTTPException(404, "知识库不存在")
    if not _can_write(ctx, base):
        raise HTTPException(403, "平台知识库只有平台管理员可以修改")
    row = Document(
        base_id=base.id,
        space_id=base.space_id,
        title=body.title,
        content=body.content,
        visibility=body.visibility,
        allowed_roles_json=dump(body.allowed_roles),
        source_name=body.source_name,
        source_kind=body.source_kind or "text",
        strategy=body.strategy or "paragraph",
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
    )
    db.add(row)
    db.flush()
    _index_or_400(db, row)
    return _doc_dict(db, row)


@router.post("/api/knowledge/parse")
async def parse_document(file: UploadFile = File(...), ctx=Depends(get_context)):
    require(ctx, "builder")
    data = await file.read()
    try:
        text, kind = extract_text(file.filename or "upload.txt", data)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    suggested = suggest_strategy(text)
    stem = (file.filename or "文档").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return {
        "title": stem or "文档",
        "source_name": file.filename or "",
        "source_kind": kind,
        "content": text,
        "suggested": suggested,
        "strategies": strategy_catalog(),
    }


@router.post("/api/knowledge/preview")
def preview_document(body: PreviewIn, ctx=Depends(get_context)):
    require(ctx, "builder")
    try:
        pieces, applied = split_text(body.content, body.strategy, body.chunk_size, body.chunk_overlap)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "applied": applied,
        "count": len(pieces),
        "chunks": [{"position": index, "text": text[:240]} for index, text in enumerate(pieces[:8])],
    }


@router.patch("/api/knowledge/documents/{document_id}")
def patch_document(document_id: int, body: DocPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = db.get(Document, document_id)
    base = db.get(KnowledgeBase, row.base_id) if row else None
    if row is None or not _can_read(db, ctx, "knowledge", base):
        raise HTTPException(404, "文档不存在")
    if not _can_write(ctx, base):
        raise HTTPException(403, "平台知识库只有平台管理员可以修改")
    if body.title is not None:
        row.title = body.title
    if body.content is not None:
        row.content = body.content
    if body.visibility is not None:
        row.visibility = body.visibility
    if body.allowed_roles is not None:
        row.allowed_roles_json = dump(body.allowed_roles)
    if body.source_name is not None:
        row.source_name = body.source_name
    if body.source_kind is not None:
        row.source_kind = body.source_kind
    if body.strategy is not None:
        row.strategy = body.strategy
    if body.chunk_size is not None:
        row.chunk_size = body.chunk_size
    if body.chunk_overlap is not None:
        row.chunk_overlap = body.chunk_overlap
    _index_or_400(db, row)
    return _doc_dict(db, row)


def _drop_document(db, ctx, document_id: int) -> None:
    require(ctx, "builder")
    row = db.get(Document, document_id)
    base = db.get(KnowledgeBase, row.base_id) if row else None
    if row is None or not _can_read(db, ctx, "knowledge", base):
        raise HTTPException(404, "文档不存在")
    if not _can_write(ctx, base):
        raise HTTPException(403, "平台知识库只有平台管理员可以修改")
    db.query(Chunk).filter_by(document_id=row.id).delete()
    db.delete(row)


@router.delete("/api/knowledge/documents/{document_id}")
def delete_document(document_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_document(db, ctx, document_id)
    return {"ok": True}


@router.post("/api/knowledge/documents/bulk-delete")
def delete_documents(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_document(db, ctx, item_id))


@router.post("/api/knowledge/search")
def search(body: SearchIn, ctx=Depends(get_context), db=Depends(get_db)):
    return {"hits": search_knowledge(db, ctx.space.id, body.query, ctx.role, body.base_id, ids_for(db, ctx.space.id, "knowledge"))}

