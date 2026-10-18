"""空间密钥。只写入密文，列表和详情都不返回明文。"""

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

def _secret_dict(db, row: Secret) -> dict:
    """列出引用这条密钥的模型和 MCP 服务。明文不出现在响应里。"""
    models = db.query(ModelEndpoint).filter(ModelEndpoint.secret_id == row.id).all()
    servers = db.query(McpServer).filter(McpServer.secret_id == row.id).all()
    space_ids = {item.space_id for item in (*models, *servers) if item.space_id}
    names = {space.id: space.name for space in db.query(Space).filter(Space.id.in_(space_ids or {-1})).all()}

    def place(item) -> str:
        return names.get(item.space_id, "平台") if item.space_id else "平台"

    return {
        "id": row.id,
        "name": row.name,
        "created_at": stamp(row.created_at),
        "model_count": len(models),
        "mcp_count": len(servers),
        "models": [{"id": item.id, "name": item.name, "department": place(item)} for item in models],
        "mcp_servers": [{"id": item.id, "name": item.name, "department": place(item)} for item in servers],
    }


@router.get("/api/secrets")
def list_secrets(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    require(ctx, "builder")
    query = db.query(Secret).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        lambda row: _secret_dict(db, row),
        desk,
        order=Secret.id.desc(),
        text=[Secret.name],
        exact={"name": Secret.name},
        time_col=Secret.created_at,
    )


@router.post("/api/secrets")
def create_secret(body: SecretIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "admin")
    row = Secret(space_id=ctx.space.id, name=body.name, ciphertext=encrypt(body.value), created_by=ctx.user.id)
    db.add(row)
    db.flush()
    audit(db, ctx.space.id, ctx.user.username, "secret.create", body.name)
    return _secret_dict(db, row)


@router.patch("/api/secrets/{secret_id}")
def patch_secret(secret_id: int, body: SecretPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "admin")
    row = db.get(Secret, secret_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "密钥不存在")
    if body.name:
        row.name = body.name
    if body.value:
        row.ciphertext = encrypt(body.value)
    audit(db, ctx.space.id, ctx.user.username, "secret.patch", row.name)
    return _secret_dict(db, row)


def _drop_secret(db, ctx, secret_id: int) -> None:
    require(ctx, "admin")
    row = db.get(Secret, secret_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "密钥不存在")
    used_model = db.query(ModelEndpoint).filter_by(secret_id=row.id).first()
    used_mcp = db.query(McpServer).filter_by(secret_id=row.id).first()
    if used_model is not None or used_mcp is not None:
        raise HTTPException(409, "密钥仍被模型或 MCP 服务使用")
    db.delete(row)


@router.delete("/api/secrets/{secret_id}")
def delete_secret(secret_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_secret(db, ctx, secret_id)
    return {"ok": True}


@router.post("/api/secrets/bulk-delete")
def delete_secrets(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_secret(db, ctx, item_id))

