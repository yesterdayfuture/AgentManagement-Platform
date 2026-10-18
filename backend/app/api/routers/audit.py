"""操作审计。成功的写请求另由中间件记录，这里提供查询和清理。"""

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

def _audit_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "actor": row.actor,
        "action": row.action,
        "target": row.target,
        "detail": row.detail,
        "created_at": stamp(row.created_at),
    }


@router.get("/api/audit")
def audit_list(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(AuditLog).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        _audit_dict,
        desk,
        order=AuditLog.id.desc(),
        text=[AuditLog.actor, AuditLog.action, AuditLog.target, AuditLog.detail],
        exact={"actor": AuditLog.actor, "action": AuditLog.action},
        time_col=AuditLog.created_at,
        limit_unpaged=200,
    )


def _drop_audit(db, ctx, log_id: int) -> None:
    require(ctx, "admin")
    row = db.get(AuditLog, log_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "审计记录不存在")
    db.delete(row)


@router.delete("/api/audit/{log_id}")
def delete_audit(log_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_audit(db, ctx, log_id)
    return {"ok": True}


@router.post("/api/audit/bulk-delete")
def delete_audits(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_audit(db, ctx, item_id))

