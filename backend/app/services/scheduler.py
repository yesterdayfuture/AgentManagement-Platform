"""到点的工作流按保存的问题和节点配置再跑一次。

只处理已启用、并且下次时间已到的定时。正在执行或暂停的不重复开工，
把下次时间往后推。测试进程不启动这条循环，需要时直接调用 tick。
"""

import sys
import threading
import time
from datetime import datetime, timedelta


def start(factory) -> None:
    if "pytest" in sys.modules:
        return
    threading.Thread(target=_loop, args=(factory,), daemon=True).start()


def _loop(factory) -> None:
    while True:
        time.sleep(30)
        try:
            tick(factory)
        except Exception:
            continue


def tick(factory) -> int:
    from app.models import Workflow

    db = factory()
    try:
        now = datetime.utcnow()
        due = (
            db.query(Workflow)
            .filter(
                Workflow.schedule_enabled.is_(True),
                Workflow.enabled.is_(True),
                Workflow.schedule_next_at.is_not(None),
                Workflow.schedule_next_at <= now,
            )
            .all()
        )
        ids = [row.id for row in due]
    finally:
        db.close()
    started = 0
    for workflow_id in ids:
        session = factory()
        try:
            if _run_due(session, workflow_id):
                started += 1
        except Exception:
            session.rollback()
            row = session.get(Workflow, workflow_id)
            if row is not None and row.schedule_enabled:
                row.schedule_next_at = datetime.utcnow() + timedelta(minutes=max(1, int(row.schedule_minutes or 1)))
                session.commit()
        finally:
            session.close()
    return started


def _run_due(db, workflow_id: int) -> bool:
    from app.api.routers.workflows import _merge_node_setup
    from app.api.schemas import NodeSetupIn
    from app.models import ContextPolicy, Membership, Space, Trace, User, Workflow, WorkflowRun
    from app.services.grants import is_platform
    from app.services.runtime import _DEFINITION, run_workflow
    from app.util import load

    row = db.get(Workflow, workflow_id)
    if row is None or not row.schedule_enabled or not row.enabled:
        return False
    minutes = max(1, int(row.schedule_minutes or 1))
    row.schedule_next_at = datetime.utcnow() + timedelta(minutes=minutes)
    if (row.release or "draft") != "published":
        row.schedule_enabled = False
        db.commit()
        return False
    space = db.get(Space, row.space_id)
    if space is not None and space.token_used >= space.token_budget:
        row.schedule_enabled = False
        db.commit()
        return False
    budget = int(row.schedule_token_budget or 0)
    if budget > 0 and int(row.schedule_token_used or 0) >= budget:
        row.schedule_enabled = False
        db.commit()
        return False
    active = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.workflow_id == row.id, WorkflowRun.status.in_(["running", "paused"]))
        .first()
    )
    if active is not None:
        db.commit()
        return False
    user = db.get(User, int(row.created_by or 0))
    policy = db.query(ContextPolicy).filter_by(space_id=row.space_id).one_or_none()
    if user is None or space is None or policy is None or not (row.schedule_message or "").strip():
        db.commit()
        return False
    membership = db.query(Membership).filter_by(user_id=user.id, space_id=space.id).one_or_none()
    role = membership.role if membership else ("admin" if is_platform(user) else "builder")
    raw_nodes = [item for item in load(row.schedule_nodes_json, []) if isinstance(item, dict) and item.get("agent_id")]
    nodes = [NodeSetupIn(**item) for item in raw_nodes]
    definition = _merge_node_setup(load(row.definition_json, {}), nodes) if nodes else None
    message = row.schedule_message
    db.commit()
    token = _DEFINITION.set(definition)
    try:
        result = run_workflow(
            db,
            row,
            user=user,
            space=space,
            message=message,
            environment="schedule",
            role=role,
            policy=policy,
        )
        used = 0
        trace_id = (result or {}).get("trace_id") or ""
        if trace_id:
            trace = db.get(Trace, trace_id)
            if trace is not None:
                used = int(trace.tokens_in or 0) + int(trace.tokens_out or 0)
        if used:
            fresh = db.get(Workflow, workflow_id)
            if fresh is not None:
                fresh.schedule_token_used = int(fresh.schedule_token_used or 0) + used
                if int(fresh.schedule_token_budget or 0) > 0 and fresh.schedule_token_used >= fresh.schedule_token_budget:
                    fresh.schedule_enabled = False
                db.commit()
    finally:
        _DEFINITION.reset(token)
    return True
