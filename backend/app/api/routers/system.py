"""健康检查和运行环境。不读取业务数据，供探活和关于页使用。"""

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
    LedgerAccount, Membership, McpServer, Memory, ModelEndpoint, Secret, Skill, Space,
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
from app.services.skillbox import status as skillbox_status
from app.services.vault import decrypt, encrypt
from app.util import dump, load
from app.api.schemas import *
from app.api.support import *

router = APIRouter()

@router.get("/api/health")
def health():
    return {"ok": True, "name": "垣"}


def _named_jobs(db, box: dict) -> dict:
    """给正在执行的脚本补上技能名称。容器状态本身不查业务表。"""
    jobs = box.get("jobs") or []
    ids = [item["skill_id"] for item in jobs if item.get("skill_id")]
    rows = db.query(Skill).filter(Skill.id.in_(ids)).all() if ids else []
    named = {row.id: row for row in rows}
    for item in jobs:
        row = named.get(item.get("skill_id"))
        item["skill_name"] = row.name if row else ""
        item["skill_key"] = row.key if row else ""
    return box


@router.get("/api/system")
def system_info(db=Depends(get_db)):
    return {
        "name": "垣",
        "mode": "private",
        "data_residency": "local-sqlite",
        "control_plane": "catalog-auth-audit",
        "execution_plane": "runtime-sandbox",
        "gateways": ["model", "tool"],
        "controls": {
            "hybrid_execution": True,
            "workload_identity": True,
            "tool_registry": True,
            "context_policy": True,
            "model_pool": True,
            "cost_meter": True,
        },
        "skillbox": _named_jobs(db, skillbox_status()),
    }

