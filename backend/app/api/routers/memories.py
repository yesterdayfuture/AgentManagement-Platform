"""三层记忆：短期 / 长期 / 用户画像。压缩内部摘要不在管理界面展示。"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_context, get_db, require
from app.models import Memory
from app.paging import Desk, desk_params, finish_rows, stamp, window
from app.services.grants import is_platform
from app.services.memory_layers import (
    KIND_LONG,
    KIND_SHORT,
    KIND_SUMMARY,
    KIND_USER,
    activate_profile_version,
    memory_detail,
    profile_versions,
    next_profile_version,
)
from app.services.runtime import list_memories
from app.util import load
from app.api.schemas import *
from app.api.support import *

router = APIRouter()

KIND_LABELS = {
    KIND_SHORT: "短期",
    KIND_LONG: "长期",
    KIND_USER: "用户画像",
}
SOURCE_LABELS = {"chat": "问答", "agent": "智能体", "workflow": "工作流", "manual": "手动"}


def _can_see(db, ctx, row: Memory) -> bool:
    if is_platform(ctx.user):
        return True
    if row.space_id != ctx.space.id:
        return False
    owner = row.user_id or row.created_by
    kind = row.kind or KIND_SHORT
    if kind == KIND_SUMMARY:
        return False
    if kind == KIND_USER:
        return owner == ctx.user.id
    if kind in {KIND_LONG, KIND_SHORT}:
        if owner and owner != ctx.user.id and ctx.role != "admin":
            return False
        return True
    return owner == ctx.user.id or ctx.role == "admin"


def _memory_dict(db, row: Memory) -> dict:
    produced = load(row.produced_json, {}) or {}
    refs = load(row.source_refs_json, {}) or {}
    has_lineage = bool(
        produced.get("long")
        or produced.get("user")
        or produced.get("notes")
        or refs.get("dialogues")
        or refs.get("prior_short")
        or refs.get("prior_long")
        or refs.get("summaries")
        or row.task_id
    )
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind, row.kind),
        "content": row.content,
        "agent_id": row.agent_id,
        "project_id": row.project_id,
        "source_kind": row.source_kind or "",
        "source_kind_label": SOURCE_LABELS.get(row.source_kind or "", row.source_kind or ""),
        "source_id": row.source_id or "",
        "source_name": row.source_name or "",
        "task_id": row.task_id or "",
        "level": row.level or 0,
        "active": bool(getattr(row, "active", True)),
        "parent_summary_id": row.parent_summary_id,
        "has_detail": row.kind == KIND_USER or has_lineage,
        "version_count": 0,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": stamp(row.created_at),
        # 展示所属用户（记忆主人），而非仅写入操作者
        **_attribution(db, row.user_id or row.created_by, row.space_id),
    }


def _drop_memory(db, ctx, memory_id: int) -> None:
    row = db.get(Memory, memory_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "记忆不存在")
    if row.user_id != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "不能删除他人的记忆")
    if row.kind == KIND_USER and getattr(row, "active", True):
        raise HTTPException(400, "不能删除启用中的用户画像，请先启用其他版本或编辑固化")
    if row.kind == KIND_SUMMARY:
        raise HTTPException(400, "内部压缩摘要不可删除")
    db.delete(row)


@router.get("/api/memories")
def memories(
    ctx=Depends(get_context),
    db=Depends(get_db),
    desk: Desk = Depends(desk_params),
    agent_id: int | None = None,
    project_id: str = "",
    source_name: str = "",
    source_kind: str = "",
    task_id: str = "",
    kind: str = "",
):
    require(ctx, "viewer")
    if is_platform(ctx.user):
        # 平台顶栏为「全部部门」：跨部门展示本人短/长期 + 全部用户画像
        # （问答可能选用其他部门智能体，压缩记忆会落在该部门 space_id 上）
        all_rows = db.query(Memory).order_by(Memory.id.desc()).all()
        rows = [
            row
            for row in all_rows
            if row.kind == KIND_USER or (row.user_id or row.created_by) == ctx.user.id
        ]
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
        rows = [row for row in rows if _can_see(db, ctx, row)]
    # 管理界面不展示内部压缩摘要；用户画像列表只展示启用中的版本
    rows = [row for row in rows if row.kind != KIND_SUMMARY]
    rows = [row for row in rows if row.kind != KIND_USER or getattr(row, "active", True)]
    if desk.q:
        needle = desk.q
        rows = [
            row
            for row in rows
            if needle in (row.content or "")
            or needle in (row.source_name or "")
            or needle in (row.task_id or "")
        ]
    exact_kind = kind or (desk.exact if desk.exact_field == "kind" else "")
    if exact_kind:
        if exact_kind == KIND_SUMMARY:
            rows = []
        else:
            rows = [row for row in rows if row.kind == exact_kind]
    if desk.exact and desk.exact_field == "content":
        rows = [row for row in rows if row.content == desk.exact]
    if desk.exact and desk.exact_field == "source_name":
        rows = [row for row in rows if (row.source_name or "") == desk.exact]
    if source_name:
        rows = [row for row in rows if source_name in (row.source_name or "")]
    if source_kind:
        rows = [row for row in rows if (row.source_kind or "") == source_kind]
    if task_id:
        rows = [row for row in rows if (row.task_id or "") == task_id]
    if agent_id:
        rows = [row for row in rows if row.agent_id == agent_id]
    start, end, exclusive = window(desk)
    if start:
        rows = [row for row in rows if row.created_at and row.created_at >= start]
    if end:
        rows = [row for row in rows if row.created_at and (row.created_at < end if exclusive else row.created_at <= end)]

    version_counts: dict[tuple[int, int], int] = {}
    user_rows = [row for row in rows if row.kind == KIND_USER and row.user_id]
    if user_rows:
        owners = {(row.space_id, row.user_id) for row in user_rows}
        all_user = (
            db.query(Memory)
            .filter(Memory.kind == KIND_USER, Memory.user_id.in_([uid for _, uid in owners if uid]))
            .all()
        )
        for item in all_user:
            key = (item.space_id, item.user_id)
            version_counts[key] = version_counts.get(key, 0) + 1

    def _row(row: Memory) -> dict:
        data = _memory_dict(db, row)
        if row.kind == KIND_USER and row.user_id:
            data["version_count"] = version_counts.get((row.space_id, row.user_id), 1)
        return data

    return finish_rows(rows, _row, desk)


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
    rows = [
        row
        for row in rows
        if _can_see(db, ctx, row) and row.kind != KIND_SUMMARY and (row.kind != KIND_USER or getattr(row, "active", True))
    ]
    return {"items": [_memory_dict(db, row) for row in rows]}


@router.post("/api/memories/import")
def import_memories(body: MemoryImport, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    created = []
    for item in body.items:
        kind = item.kind if item.kind in {KIND_SHORT, KIND_LONG, KIND_USER} else KIND_LONG
        expires = None if kind == KIND_USER else datetime.utcnow() + timedelta(days=ctx.policy.retention_days)
        row = Memory(
            space_id=ctx.space.id,
            agent_id=item.agent_id,
            user_id=ctx.user.id,
            project_id=item.project_id,
            kind=kind,
            content=item.content,
            source_kind="manual",
            source_name="导入",
            level=1 if kind == KIND_USER else 0,
            active=True,
            created_by=ctx.user.id,
            expires_at=expires,
        )
        if kind == KIND_USER:
            row.level = next_profile_version(db, space_id=row.space_id, user_id=ctx.user.id)
        db.add(row)
        db.flush()
        created.append(_memory_dict(db, row))
    return {"items": created}


@router.post("/api/memories/bulk-delete")
def delete_memories(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_memory(db, ctx, item_id))


@router.get("/api/memories/{memory_id}")
def get_memory(memory_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "viewer")
    row = db.get(Memory, memory_id)
    if row is None or row.kind == KIND_SUMMARY or not _can_see(db, ctx, row):
        raise HTTPException(404, "记忆不存在")
    # 历史画像版本对所有者/平台管理员可见
    if row.kind == KIND_USER and not getattr(row, "active", True):
        owner = row.user_id or row.created_by
        if owner != ctx.user.id and not is_platform(ctx.user) and ctx.role != "admin":
            raise HTTPException(404, "记忆不存在")
    data = _memory_dict(db, row)
    data["detail"] = memory_detail(db, row)
    if row.kind == KIND_USER and row.user_id:
        data["version_count"] = len(data["detail"].get("versions") or [])
    return data


@router.post("/api/memories/{memory_id}/activate")
def activate_memory(memory_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    """启用某一版用户画像。"""
    require(ctx, "operator")
    row = db.get(Memory, memory_id)
    if row is None or row.kind != KIND_USER:
        raise HTTPException(404, "用户画像不存在")
    if not is_platform(ctx.user) and row.space_id != ctx.space.id:
        raise HTTPException(404, "用户画像不存在")
    owner = row.user_id or row.created_by
    if owner != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "不能启用他人的用户画像")
    activate_profile_version(db, row)
    data = _memory_dict(db, row)
    data["detail"] = memory_detail(db, row)
    return data


@router.post("/api/memories")
def create_memory(body: MemoryIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    kind = body.kind if body.kind in {KIND_SHORT, KIND_LONG, KIND_USER} else KIND_LONG
    if body.ttl_seconds is None:
        expires = None if kind == KIND_USER else datetime.utcnow() + timedelta(days=ctx.policy.retention_days)
    elif body.ttl_seconds <= 0:
        expires = datetime.utcnow() - timedelta(seconds=1)
    else:
        expires = datetime.utcnow() + timedelta(seconds=body.ttl_seconds)
    # 记忆始终归属当前登录用户与当前部门上下文，忽略客户端传入的 space_id
    space_id = ctx.space.id
    level = 0
    if kind == KIND_USER:
        level = next_profile_version(db, space_id=space_id, user_id=ctx.user.id)
    row = Memory(
        space_id=space_id,
        agent_id=body.agent_id,
        user_id=ctx.user.id,
        project_id=body.project_id,
        kind=kind,
        content=body.content,
        source_kind="manual",
        source_name="手动录入",
        task_id="",
        level=level,
        active=True,
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
    if row is None or row.kind == KIND_SUMMARY or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "记忆不存在")
    if row.user_id != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "不能修改他人的记忆")
    # 用户画像：编辑内容则固化当前版并新建启用版
    if row.kind == KIND_USER and body.content is not None and body.content.strip() != (row.content or "").strip():
        new_content = body.content.strip()
        version = next_profile_version(db, space_id=row.space_id, user_id=row.user_id)
        fresh = Memory(
            space_id=row.space_id,
            agent_id=row.agent_id,
            user_id=row.user_id,
            project_id=row.project_id or "",
            kind=KIND_USER,
            content=new_content,
            source_kind="manual",
            source_name="手动编辑",
            task_id="",
            level=version,
            active=True,
            parent_summary_id=row.id,
            created_by=ctx.user.id,
            expires_at=None,
        )
        db.add(fresh)
        db.flush()
        data = _memory_dict(db, fresh)
        data["detail"] = memory_detail(db, fresh)
        return data
    if body.content is not None:
        row.content = body.content
    if body.kind is not None and body.kind in {KIND_SHORT, KIND_LONG} and row.kind != KIND_USER:
        row.kind = body.kind
    return _memory_dict(db, row)


@router.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    row = db.get(Memory, memory_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "记忆不存在")
    _drop_memory(db, ctx, memory_id)
    return {"ok": True}
