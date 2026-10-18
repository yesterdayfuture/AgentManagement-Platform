"""多个路由共用的权限、归属和加载规则。路由模块不要互相引用，共用规则放在这里。"""

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

def _bulk(db, ids, remover):
    """逐条删除，每条一个保存点。一条失败不影响已经成功的删除。

    全部失败时返回 409，让前端知道这次没有删掉任何东西。
    """
    if not ids:
        raise HTTPException(400, "没有选择记录")
    deleted, blocked = [], []
    for item_id in ids:
        nested = db.begin_nested()
        try:
            remover(item_id)
            nested.commit()
            deleted.append(item_id)
        except HTTPException as exc:
            nested.rollback()
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            blocked.append({"id": item_id, "detail": detail})
    if not deleted and blocked:
        raise HTTPException(409, blocked[0]["detail"])
    return {"deleted": deleted, "blocked": blocked}


def _spaces_for(db, user: User):
    """平台管理员看全部部门。部门管理员和普通成员只看自己加入的部门。"""
    rows = (
        db.query(Membership, Space)
        .join(Space, Space.id == Membership.space_id)
        .filter(Membership.user_id == user.id)
        .order_by(Space.id.asc())
        .all()
    )
    found = {
        space.id: {"id": space.id, "name": space.name, "key": space.key, "role": membership.role}
        for membership, space in rows
    }
    if is_platform(user):
        for space in db.query(Space).order_by(Space.id.asc()).all():
            found.setdefault(
                space.id,
                {"id": space.id, "name": space.name, "key": space.key, "role": "admin"},
            )
    return [found[key] for key in sorted(found)]


def _visible(db, ctx, model, kind: str):
    """管理列表：平台管理员看全部，部门用户只看已授权的。"""
    query = db.query(model)
    if is_platform(ctx.user):
        return query
    allowed = ids_for(db, ctx.space.id, kind) or [-1]
    return query.filter(model.id.in_(allowed))


def scope_grants(db, ctx, query, model, kind: str, usable_space: int | None):
    """按某个部门的授权收窄列表。部门用户只能收窄到当前部门。"""
    if usable_space is None:
        return query
    if not is_platform(ctx.user) and usable_space != ctx.space.id:
        raise HTTPException(403, "只能查看本部门已授权的资源")
    allowed = ids_for(db, usable_space, kind) or [-1]
    return query.filter(model.id.in_(allowed))


def _can_read(db, ctx, kind: str, row) -> bool:
    if row is None:
        return False
    if is_platform(ctx.user):
        return True
    return row.id in ids_for(db, ctx.space.id, kind)


def _can_write(ctx, row) -> bool:
    """平台资源只有平台管理员能改。部门资源由所属部门里有构建权限的人修改。"""
    if is_platform(ctx.user):
        return True
    if row is None or getattr(row, "origin", "department") == "platform" or row.space_id is None:
        return False
    return row.space_id == ctx.space.id


def _own(ctx) -> tuple[int | None, str]:
    """平台管理员新建的模型或工具不挂在某个部门上，也不自动授权。"""
    if is_platform(ctx.user):
        return None, "platform"
    return ctx.space.id, "department"


def _place(db, ctx, space_id: int | None) -> int:
    """平台管理员新建资源时可以指定部门。部门用户始终落在当前部门。"""
    if is_platform(ctx.user) and space_id:
        space = db.get(Space, space_id)
        if space is None:
            raise HTTPException(400, "部门不存在")
        return space.id
    return ctx.space.id


def _attribution(db, created_by: int | None, space_id: int | None) -> dict:
    user = db.get(User, created_by) if created_by else None
    space = db.get(Space, space_id) if space_id else None
    return {
        "creator": (user.display_name or user.username) if user else "",
        "department": space.name if space else "平台",
    }


def _home(ctx) -> tuple[int, str]:
    """工作流和知识库要留在创建时所在部门，方便智能体和文档归属。平台来源不自动授权。"""
    if is_platform(ctx.user):
        return ctx.space.id, "platform"
    return ctx.space.id, "department"


def _latest(db, agent_id: int) -> AgentVersion | None:
    return db.query(AgentVersion).filter_by(agent_id=agent_id).order_by(AgentVersion.number.desc()).first()


def _load_agent(db, ctx, agent_id: int) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or (not is_platform(ctx.user) and agent.space_id != ctx.space.id):
        raise HTTPException(404, "智能体不存在")
    return agent


def _check_env(environment: str) -> None:
    if environment not in {"test", "staging", "prod"}:
        raise HTTPException(400, "环境无效")


def _raise_gate(exc: GateError, db, agent: Agent | None = None):
    if exc.status == "quota":
        raise HTTPException(429, exc.message) from exc
    if exc.status == "offline":
        refs = references_of(db, agent) if agent else []
        raise HTTPException(409, detail={"message": exc.message, "references": refs}) from exc
    raise HTTPException(400, exc.message) from exc


def hmac_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _load_workflow(db, ctx, workflow_id: int, write: bool = False) -> Workflow:
    row = db.get(Workflow, workflow_id)
    if not _can_read(db, ctx, "workflow", row):
        raise HTTPException(404, "工作流不存在")
    if write and not _can_write(ctx, row):
        raise HTTPException(403, "平台工作流只有平台管理员可以修改")
    return row


__all__ = [name for name in globals() if not name.startswith("__")]

