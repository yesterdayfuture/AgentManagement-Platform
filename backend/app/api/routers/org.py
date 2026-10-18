"""部门、空间成员、额度和上下文策略。平台管理员管理全部部门，部门用户只看见自己的空间。"""

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
    Tool, ToolGrant, Trace, TraceStep, User, Skill, Workflow, WorkflowRun,
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

def _require_department_manager(ctx) -> None:
    if is_platform(ctx.user) or ctx.role == "admin":
        return
    raise HTTPException(403, "只有平台管理员和部门管理员可以管理部门")


def _managed_space_ids(db, ctx) -> list[int]:
    if is_platform(ctx.user):
        return [row[0] for row in db.query(Space.id).all()]
    return [row[0] for row in db.query(Membership.space_id).filter_by(user_id=ctx.user.id, role="admin").all()]


def _managed_space(db, ctx, space_id: int) -> Space:
    _require_department_manager(ctx)
    space = db.get(Space, space_id)
    if space is None or space.id not in set(_managed_space_ids(db, ctx)):
        raise HTTPException(404, "部门不存在")
    return space


def _insert_space(db, ctx, name: str) -> dict:
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(400, "请填写部门名称")
    space = Space(name=cleaned, key="dept-" + uuid.uuid4().hex[:8], require_eval=True, token_budget=1_000_000)
    db.add(space)
    db.flush()
    db.add(
        ContextPolicy(
            space_id=space.id,
            deny_json=dump(["身份证号", "工资"]),
            mask_json=dump(["客户号"]),
            isolation="user,agent",
            retention_days=30,
        )
    )
    db.add(LedgerAccount(space_id=space.id, name="现金", balance=0))
    if not is_platform(ctx.user):
        db.add(Membership(user_id=ctx.user.id, space_id=space.id, role="admin"))
    db.flush()
    audit(db, space.id, ctx.user.username, "space.create", space.name)
    return {"id": space.id, "name": space.name, "key": space.key, "role": "admin"}


@router.post("/api/spaces")
def create_space(body: SpaceIn, ctx=Depends(get_context), db=Depends(get_db)):
    """新增部门。只有平台管理员可以建。平台管理员不写入部门成员。"""
    if not is_platform(ctx.user):
        raise HTTPException(403, "只有平台管理员可以管理全部部门")
    return _insert_space(db, ctx, body.name)


def _grant_map(db, space_id: int) -> dict:
    rows = db.query(DepartmentGrant).filter_by(space_id=space_id).all()
    found = {kind: [] for kind in ("model", "workflow", "knowledge", "tool", "skill")}
    for row in rows:
        found.setdefault(row.kind, []).append(row.resource_id)
    return found


def _department_dict(db, space: Space) -> dict:
    grants = _grant_map(db, space.id)
    return {
        "id": space.id,
        "name": space.name,
        "key": space.key,
        "member_count": db.query(Membership).filter_by(space_id=space.id).count(),
        "model_count": len(grants.get("model", [])),
        "workflow_count": len(grants.get("workflow", [])),
        "knowledge_count": len(grants.get("knowledge", [])),
        "tool_count": len(grants.get("tool", [])),
        "skill_count": len(grants.get("skill", [])),
        "model_ids": grants.get("model", []),
        "workflow_ids": grants.get("workflow", []),
        "knowledge_ids": grants.get("knowledge", []),
        "tool_ids": grants.get("tool", []),
        "skill_ids": grants.get("skill", []),
        "created_at": stamp(space.created_at),
    }


def _allowed_grant_ids(db, ctx) -> dict[str, set[int]] | None:
    """平台管理员可以授权全部资源。部门管理员只能授权自己已经看得到的资源。"""
    if is_platform(ctx.user):
        return None
    space_ids = _managed_space_ids(db, ctx)
    skill_ids = set(visible_ids(db, space_ids, "skill"))
    skill_ids.update(row[0] for row in db.query(Skill.id).filter(Skill.space_id.in_(space_ids or [-1])).all())
    return {
        "model": set(visible_ids(db, space_ids, "model")),
        "workflow": set(visible_ids(db, space_ids, "workflow")),
        "knowledge": set(visible_ids(db, space_ids, "knowledge")),
        "tool": set(visible_ids(db, space_ids, "tool")),
        "skill": skill_ids,
    }


def _check_grant_ids(db, ctx, body: DepartmentIn) -> None:
    checks = [
        ("model", ModelEndpoint, body.model_ids),
        ("workflow", Workflow, body.workflow_ids),
        ("knowledge", KnowledgeBase, body.knowledge_ids),
        ("tool", Tool, body.tool_ids),
    ]
    if "skill_ids" in body.model_fields_set:
        checks.append(("skill", Skill, body.skill_ids))
    allowed = _allowed_grant_ids(db, ctx)
    for kind, model, ids in checks:
        if not ids:
            continue
        found = {row.id for row in db.query(model).filter(model.id.in_(ids)).all()}
        if set(ids) - found:
            raise HTTPException(400, "选择了不存在的资源")
        if allowed is not None and set(ids) - allowed[kind]:
            raise HTTPException(403, "不能授权当前看不到的资源")


def _apply_department_grants(db, space_id: int, body: DepartmentIn, ctx) -> None:
    _check_grant_ids(db, ctx, body)
    replace_grants(db, space_id, "model", body.model_ids)
    replace_grants(db, space_id, "workflow", body.workflow_ids)
    replace_grants(db, space_id, "knowledge", body.knowledge_ids)
    replace_grants(db, space_id, "tool", body.tool_ids)
    if "skill_ids" in body.model_fields_set:
        replace_grants(db, space_id, "skill", body.skill_ids)


def _catalog_rows(db, ctx, model, kind: str, name_field: str = "name"):
    query = db.query(model)
    allowed = _allowed_grant_ids(db, ctx)
    if allowed is not None:
        query = query.filter(model.id.in_(allowed[kind] or [-1]))
    rows = query.order_by(model.id.asc()).all()
    return [{"id": row.id, "name": getattr(row, name_field), "origin": getattr(row, "origin", None) or "department"} for row in rows]


@router.get("/api/departments/catalog")
def department_catalog(ctx=Depends(get_context), db=Depends(get_db)):
    """授权勾选清单。部门管理员只收到自己已经可以使用的资源。"""
    _require_department_manager(ctx)
    return {
        "models": _catalog_rows(db, ctx, ModelEndpoint, "model"),
        "workflows": _catalog_rows(db, ctx, Workflow, "workflow"),
        "knowledge": _catalog_rows(db, ctx, KnowledgeBase, "knowledge"),
        "tools": _catalog_rows(db, ctx, Tool, "tool"),
        "skills": _catalog_rows(db, ctx, Skill, "skill"),
    }


@router.get("/api/departments")
def list_departments(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    _require_department_manager(ctx)
    query = db.query(Space)
    if not is_platform(ctx.user):
        query = query.filter(Space.id.in_(_managed_space_ids(db, ctx) or [-1]))
    return finish(
        query,
        lambda row: _department_dict(db, row),
        desk,
        order=Space.id.desc(),
        text=[Space.name, Space.key],
        exact={"name": Space.name, "key": Space.key},
        time_col=Space.created_at,
    )


@router.post("/api/departments")
def create_department(body: DepartmentIn, ctx=Depends(get_context), db=Depends(get_db)):
    if not is_platform(ctx.user):
        raise HTTPException(403, "只有平台管理员可以新增部门")
    created = _insert_space(db, ctx, body.name)
    _apply_department_grants(db, created["id"], body, ctx)
    audit(db, created["id"], ctx.user.username, "department.grants", body.name)
    return _department_dict(db, db.get(Space, created["id"]))


@router.get("/api/departments/{space_id}")
def get_department(space_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    return _department_dict(db, _managed_space(db, ctx, space_id))


@router.patch("/api/departments/{space_id}")
def patch_department(space_id: int, body: DepartmentIn, ctx=Depends(get_context), db=Depends(get_db)):
    space = _managed_space(db, ctx, space_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "请填写部门名称")
    space.name = name
    _apply_department_grants(db, space.id, body, ctx)
    audit(db, space.id, ctx.user.username, "department.update", space.name)
    return _department_dict(db, space)


def _drop_department(db, ctx, space_id: int) -> None:
    if not is_platform(ctx.user):
        raise HTTPException(403, "只有平台管理员可以删除部门")
    space = db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "部门不存在")
    if db.query(Agent).filter_by(space_id=space.id).count():
        raise HTTPException(409, "部门里还有智能体，不能删除")
    if db.query(Workflow).filter_by(space_id=space.id).count():
        raise HTTPException(409, "部门里还有工作流，不能删除")
    if db.query(KnowledgeBase).filter_by(space_id=space.id).count():
        raise HTTPException(409, "部门里还有知识库，不能删除")
    if db.query(Tool).filter_by(space_id=space.id).count() or db.query(ModelEndpoint).filter_by(space_id=space.id).count():
        raise HTTPException(409, "部门里还有自建模型或工具，不能删除")
    db.query(DepartmentGrant).filter_by(space_id=space.id).delete(synchronize_session=False)
    db.query(Membership).filter_by(space_id=space.id).delete(synchronize_session=False)
    db.query(ContextPolicy).filter_by(space_id=space.id).delete(synchronize_session=False)
    db.query(LedgerAccount).filter_by(space_id=space.id).delete(synchronize_session=False)
    audit(db, space.id, ctx.user.username, "department.delete", space.name)
    db.delete(space)


@router.delete("/api/departments/{space_id}")
def delete_department(space_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_department(db, ctx, space_id)
    return {"ok": True}


@router.post("/api/departments/bulk-delete")
def delete_departments(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_department(db, ctx, item_id))


@router.get("/api/spaces/current")
def current_space(ctx=Depends(get_context)):
    space = ctx.space
    return {
        "id": space.id,
        "name": space.name,
        "key": space.key,
        "role": ctx.role,
        "platform_admin": is_platform(ctx.user),
        "require_eval": space.require_eval,
        "token_budget": space.token_budget,
        "token_used": space.token_used,
        "policy": {
            "deny": load(ctx.policy.deny_json, []),
            "mask": load(ctx.policy.mask_json, []),
            "isolation": ctx.policy.isolation,
            "retention_days": ctx.policy.retention_days,
        },
    }


def _member_space(db, ctx, space_id: int | None) -> Space:
    if not is_platform(ctx.user):
        require(ctx, "admin")
    chosen = space_id or ctx.space.id
    space = db.get(Space, chosen)
    if space is None:
        raise HTTPException(400, "部门不存在")
    if is_platform(ctx.user):
        return space
    membership = db.query(Membership).filter_by(user_id=ctx.user.id, space_id=space.id).one_or_none()
    if membership is None or membership.role != "admin":
        raise HTTPException(403, "只能向自己管理的部门添加用户")
    return space


def _member_dict(db, membership: Membership, user: User) -> dict:
    space = db.get(Space, membership.space_id)
    return {
        "id": membership.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": membership.role,
        "space_id": membership.space_id,
        "department": space.name if space else "",
        "created_at": stamp(membership.created_at),
    }


def _platform_member_rows(db, desk: Desk) -> list[dict]:
    """平台管理员不属于任何部门，成员列表里只出现一行。"""
    users = db.query(User).filter(User.is_platform_admin.is_(True)).order_by(User.id.asc()).all()
    rows = []
    for user in users:
        haystack = f"{user.username} {user.display_name}".lower()
        if desk.q and desk.q.lower() not in haystack:
            continue
        if desk.exact and desk.exact_field == "username" and user.username != desk.exact:
            continue
        if desk.exact and desk.exact_field == "role" and desk.exact not in {"platform", "平台管理员"}:
            continue
        rows.append(
            {
                "id": -user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": "platform",
                "space_id": None,
                "department": "全部部门",
                "created_at": stamp(user.created_at),
            }
        )
    return rows


@router.get("/api/spaces/current/members")
def members(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), space_id: int | None = Query(default=None)):
    query = db.query(Membership, User).join(User, User.id == Membership.user_id)
    if space_id is not None:
        if not is_platform(ctx.user) and space_id != ctx.space.id:
            raise HTTPException(403, "只能查看本部门的成员")
        query = query.filter(Membership.space_id == space_id)
    elif not is_platform(ctx.user):
        query = query.filter(Membership.space_id == ctx.space.id)
    query = query.filter(User.is_platform_admin.is_(False))
    result = finish(
        query,
        lambda row: _member_dict(db, row[0], row[1]),
        desk,
        order=Membership.id.desc(),
        text=[User.username, User.display_name, Membership.role],
        exact={"username": User.username, "role": Membership.role},
        time_col=Membership.created_at,
    )
    extra = [] if space_id is not None else _platform_member_rows(db, desk)
    if isinstance(result, list):
        return extra + result
    if int(desk.page or 1) == 1:
        result["items"] = extra + result["items"]
    result["total"] += len(extra)
    return result


@router.post("/api/spaces/current/members")
def add_member(body: MemberIn, ctx=Depends(get_context), db=Depends(get_db)):
    space = _member_space(db, ctx, body.space_id)
    if body.role not in {"viewer", "operator", "builder", "admin"}:
        raise HTTPException(400, "角色无效")
    if body.role == "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "只有平台管理员可以设置部门管理员")
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "请填写账号")
    user = db.query(User).filter_by(username=username).one_or_none()
    created = user is None
    if user is not None and is_platform(user):
        raise HTTPException(400, "平台管理员不是部门成员")
    if created:
        user = User(
            username=username,
            password_hash=hash_password(username + "123"),
            display_name=username,
        )
        db.add(user)
        db.flush()
    row = db.query(Membership).filter_by(user_id=user.id, space_id=space.id).one_or_none()
    if row is None:
        row = Membership(user_id=user.id, space_id=space.id, role=body.role)
        db.add(row)
    else:
        row.role = body.role
    db.flush()
    audit(db, space.id, ctx.user.username, "member.set", username, body.role)
    return {
        "id": row.id,
        "username": username,
        "role": body.role,
        "space_id": space.id,
        "department": space.name,
        "initial_password": f"{username}123" if created else "",
    }


def _drop_member(db, ctx, membership_id: int) -> None:
    if membership_id < 0:
        raise HTTPException(400, "不能移除平台管理员")
    if not is_platform(ctx.user):
        require(ctx, "admin")
    row = db.get(Membership, membership_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "成员不存在")
    if row.user_id == ctx.user.id:
        raise HTTPException(400, "不能移除自己")
    db.delete(row)


@router.delete("/api/spaces/current/members/{membership_id}")
def delete_member(membership_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_member(db, ctx, membership_id)
    audit(db, ctx.space.id, ctx.user.username, "member.delete", str(membership_id))
    return {"ok": True}


@router.post("/api/spaces/current/members/bulk-delete")
def delete_members(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_member(db, ctx, item_id))


@router.put("/api/spaces/current/quota")
def quota(body: QuotaIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "admin")
    ctx.space.token_budget = body.token_budget
    if body.token_used is not None:
        ctx.space.token_used = body.token_used
    audit(db, ctx.space.id, ctx.user.username, "quota.set", ctx.space.key, str(body.token_budget))
    return {"token_budget": ctx.space.token_budget, "token_used": ctx.space.token_used}


@router.put("/api/spaces/current/policy")
def update_policy(body: PolicyIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    ctx.policy.deny_json = dump(body.deny)
    ctx.policy.mask_json = dump(body.mask)
    ctx.policy.isolation = body.isolation
    ctx.policy.retention_days = body.retention_days
    audit(db, ctx.space.id, ctx.user.username, "policy.set", ctx.space.key, body.isolation)
    return {"ok": True}

