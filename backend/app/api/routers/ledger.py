"""部门现金账户。转账执行在运行时的账本工具里，这里只维护账户余额。"""

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

def _ledger_dict(row: LedgerAccount) -> dict:
    return {"id": row.id, "name": row.name, "balance": row.balance, "created_at": stamp(row.created_at)}


@router.get("/api/ledger")
def ledger(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(LedgerAccount).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        _ledger_dict,
        desk,
        order=LedgerAccount.id.asc(),
        text=[LedgerAccount.name],
        exact={"name": LedgerAccount.name},
        time_col=LedgerAccount.created_at,
    )


@router.post("/api/ledger")
def create_ledger(body: LedgerIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "admin")
    if db.query(LedgerAccount).filter_by(space_id=ctx.space.id, name=body.name).first():
        raise HTTPException(400, "账户已存在")
    row = LedgerAccount(space_id=ctx.space.id, name=body.name, balance=body.balance)
    db.add(row)
    db.flush()
    audit(db, ctx.space.id, ctx.user.username, "ledger.create", body.name, str(body.balance))
    return _ledger_dict(row)


@router.patch("/api/ledger/{account_id}")
def patch_ledger(account_id: int, body: LedgerPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "admin")
    row = db.get(LedgerAccount, account_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "账户不存在")
    if body.name is not None:
        row.name = body.name
    if body.balance is not None:
        row.balance = body.balance
    audit(db, ctx.space.id, ctx.user.username, "ledger.patch", row.name, str(row.balance))
    return _ledger_dict(row)


def _drop_ledger(db, ctx, account_id: int) -> None:
    require(ctx, "admin")
    row = db.get(LedgerAccount, account_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "账户不存在")
    if db.query(LedgerAccount).filter_by(space_id=ctx.space.id).count() <= 1:
        raise HTTPException(400, "至少保留一个账户")
    db.delete(row)


@router.delete("/api/ledger/{account_id}")
def delete_ledger(account_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_ledger(db, ctx, account_id)
    return {"ok": True}


@router.post("/api/ledger/bulk-delete")
def delete_ledgers(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_ledger(db, ctx, item_id))

