"""OpenAI 兼容的对话入口。model 字段填智能体标识，历史消息会一并送入。"""

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

@router.post("/v1/chat/completions")
def openai_chat(body: ChatIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    agent = db.query(Agent).filter_by(space_id=ctx.space.id, key=body.model).one_or_none()
    if agent is None:
        raise HTTPException(404, "模型对应的智能体不存在")
    message = ""
    history = []
    last_user = -1
    for index, item in enumerate(body.messages):
        if item.get("role") == "user":
            last_user = index
            message = item.get("content") or ""
    if last_user >= 0:
        for item in body.messages[:last_user]:
            if item.get("role") in {"user", "assistant"} and (item.get("content") or "").strip():
                history.append({"role": item["role"], "content": item.get("content") or ""})
    try:
        result = execute_agent(
            db,
            agent=agent,
            user=ctx.user,
            space=ctx.space,
            message=message,
            environment="prod",
            role=ctx.role,
            policy=ctx.policy,
            history=history,
        )
    except GateError as exc:
        _raise_gate(exc, db, agent)
    return {
        "id": result["trace_id"],
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["output"]},
                "finish_reason": "stop",
            }
        ],
    }

