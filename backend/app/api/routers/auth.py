"""登录、单点登录、改密和当前身份。令牌由 security 签发，密码只保存散列。"""

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

@router.post("/api/auth/login")
def login(body: LoginIn, db=Depends(get_db)):
    user = db.query(User).filter_by(username=body.username).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "账号或密码错误")
    audit(db, None, user.username, "auth.login", user.username)
    token = issue_token({"sub": user.id, "typ": "access"})
    return {"access_token": token, "token_type": "Bearer", "user": {"id": user.id, "username": user.username}}


@router.post("/api/auth/sso")
def sso(body: SsoIn, db=Depends(get_db)):
    if abs(int(time.time()) - body.ts) > 300:
        raise HTTPException(401, "SSO 时间窗无效")
    expected = sign_sso(body.external_id, body.email, body.space_key, body.ts)
    if not hmac_equal(expected, body.signature):
        raise HTTPException(401, "SSO 签名无效")
    space = db.query(Space).filter_by(key=body.space_key).one_or_none()
    if space is None:
        raise HTTPException(404, "空间不存在")
    user = db.query(User).filter_by(external_id=body.external_id).one_or_none()
    if user is None:
        user = User(
            username=f"sso-{body.external_id}",
            password_hash=hash_password(uuid.uuid4().hex),
            display_name=body.name,
            email=body.email,
            external_id=body.external_id,
        )
        db.add(user)
        db.flush()
    if db.query(Membership).filter_by(user_id=user.id, space_id=space.id).one_or_none() is None:
        db.add(Membership(user_id=user.id, space_id=space.id, role="operator"))
    audit(db, space.id, user.username, "auth.sso", body.external_id)
    token = issue_token({"sub": user.id, "typ": "access"})
    return {"access_token": token, "token_type": "Bearer", "space_id": space.id}


@router.get("/api/me")
def me(principal=Depends(get_principal), db=Depends(get_db)):
    user = principal.user
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "platform_admin": is_platform(user),
        "spaces": _spaces_for(db, user),
    }


@router.post("/api/me/password")
def change_password(body: PasswordIn, principal=Depends(get_principal), db=Depends(get_db)):
    """本人修改密码。不需要空间身份，任何已登录账号都可以改自己的。"""
    new_password = body.new_password.strip()
    if len(new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if not verify_password(body.old_password, principal.user.password_hash):
        raise HTTPException(400, "原密码不正确")
    if verify_password(new_password, principal.user.password_hash):
        raise HTTPException(400, "新密码与原密码相同")
    principal.user.password_hash = hash_password(new_password)
    audit(db, None, principal.user.username, "auth.password", principal.user.username)
    return {"ok": True}


@router.post("/api/auth/token-exchange")
def token_exchange(body: ExchangeIn, ctx=Depends(get_context), db=Depends(get_db)):
    agent = _load_agent(db, ctx, body.agent_id)
    token = issue_token(
        {
            "sub": ctx.user.id,
            "act": agent.workload_id,
            "space_id": ctx.space.id,
            "typ": "delegation",
            "aud": "tool-gateway",
        },
        ttl=300,
    )
    audit(db, ctx.space.id, ctx.user.username, "auth.token_exchange", agent.workload_id)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "expires_in": 300,
    }

