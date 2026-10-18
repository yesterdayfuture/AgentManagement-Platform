"""部门对模型、工作流、知识库、工具和技能的使用授权。

平台管理员新建的资源不会自动授权。部门内新建的资源只授给当前部门。
已有数据在授权表还是空的时候补一次，避免升级后原部门突然看不见内置工具和模型。
技能按所属部门补授权，已有技能继续给创建它的部门使用。
"""

from app.models import DepartmentGrant, KnowledgeBase, ModelEndpoint, Skill, Space, Tool, Workflow

KINDS = ("model", "workflow", "knowledge", "tool", "skill")


def is_platform(user) -> bool:
    return bool(getattr(user, "is_platform_admin", False))


def ids_for(db, space_id: int, kind: str) -> list[int]:
    rows = db.query(DepartmentGrant.resource_id).filter_by(space_id=space_id, kind=kind).all()
    return [row[0] for row in rows]


def grant(db, space_id: int, kind: str, resource_id: int) -> None:
    exists = (
        db.query(DepartmentGrant)
        .filter_by(space_id=space_id, kind=kind, resource_id=resource_id)
        .one_or_none()
    )
    if exists is None:
        db.add(DepartmentGrant(space_id=space_id, kind=kind, resource_id=resource_id))


def replace_grants(db, space_id: int, kind: str, resource_ids: list[int]) -> None:
    db.query(DepartmentGrant).filter_by(space_id=space_id, kind=kind).delete(synchronize_session=False)
    seen = set()
    for resource_id in resource_ids:
        if resource_id in seen:
            continue
        seen.add(resource_id)
        db.add(DepartmentGrant(space_id=space_id, kind=kind, resource_id=resource_id))


def drop_resource(db, kind: str, resource_id: int) -> None:
    db.query(DepartmentGrant).filter_by(kind=kind, resource_id=resource_id).delete(synchronize_session=False)


def visible_ids(db, space_ids: list[int], kind: str) -> list[int]:
    if not space_ids:
        return []
    rows = (
        db.query(DepartmentGrant.resource_id)
        .filter(DepartmentGrant.kind == kind, DepartmentGrant.space_id.in_(space_ids))
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def ensure_backfill(db) -> None:
    """授权表为空时，按升级前的可见范围补授权。之后新建的平台资源不再自动补。"""
    if db.query(DepartmentGrant).first() is not None:
        return
    spaces = db.query(Space).all()
    for space in spaces:
        models = db.query(ModelEndpoint).filter(
            (ModelEndpoint.space_id == space.id) | (ModelEndpoint.space_id.is_(None))
        )
        tools = db.query(Tool).filter((Tool.space_id == space.id) | (Tool.space_id.is_(None)))
        for row in models:
            grant(db, space.id, "model", row.id)
        for row in tools:
            grant(db, space.id, "tool", row.id)
        for row in db.query(Workflow).filter_by(space_id=space.id):
            grant(db, space.id, "workflow", row.id)
        for row in db.query(KnowledgeBase).filter_by(space_id=space.id):
            grant(db, space.id, "knowledge", row.id)
    ensure_skill_grants(db)


def ensure_skill_grants(db) -> None:
    """每个技能至少授权给创建它的部门。重复调用不会新增重复记录。"""
    for row in db.query(Skill).all():
        if row.space_id:
            grant(db, row.space_id, "skill", row.id)
