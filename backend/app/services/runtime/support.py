"""运行时的共用记录。

轨迹步骤、委托跳数、版本选择和内置工具执行放在这里。
智能体循环和工作流都从这里记步骤，避免两套轨迹格式。
"""

import contextvars
import json
import re
import time
import uuid
import zlib
from datetime import datetime, timedelta

from app.models import (
    Agent,
    AgentVersion,
    Approval,
    DelegationHop,
    LedgerAccount,
    Memory,
    ModelEndpoint,
    Tool,
    Trace,
    TraceStep,
    Workflow,
    WorkflowRun,
)
from app.services.grants import ids_for
from app.services.gateway import (
    GateError,
    authorize_tool,
    charge_model,
    classify_intent,
    complete,
    complete_with_tools,
    cost_parts,
    find_tool,
    quota_open,
    guard_url,
    select_model,
)
from app.services.files import list_directory
from app.services.policy import apply_context_policy, isolation_keys
from app.services.retrieval import search_knowledge
from app.services.sandbox import evaluate_expression, run_sandbox
from app.util import dump, load

_EMIT = contextvars.ContextVar("wf_emit", default=None)
_DEFINITION = contextvars.ContextVar("wf_definition", default=None)
EXPR = re.compile(r"(\d+(?:\.\d+)?(?:\s*[\+\-\*/]\s*\d+(?:\.\d+)?)+)")
FOLDER = re.compile(r"(?:文件夹|目录)\s*[:：]?\s*([^\s，。；]+)")
AMOUNT = re.compile(r"(\d+(?:\.\d+)?)")


def audit(db, space_id, actor: str, action: str, target: str = "", detail: str = "") -> None:
    from app.models import AuditLog

    db.add(AuditLog(space_id=space_id, actor=actor, action=action, target=target, detail=detail))


def purge_memories(db, space_id: int) -> None:
    now = datetime.utcnow()
    db.query(Memory).filter(Memory.space_id == space_id, Memory.expires_at.is_not(None), Memory.expires_at <= now).delete()


def list_memories(db, policy, *, space_id, user_id, agent_id, project_id, role_user_id):
    purge_memories(db, space_id)
    keys = isolation_keys(policy)
    query = db.query(Memory).filter(Memory.space_id == space_id)
    if "user" in keys:
        query = query.filter(Memory.user_id == role_user_id)
    if "agent" in keys and agent_id:
        query = query.filter((Memory.agent_id == agent_id) | (Memory.agent_id.is_(None)))
    if "project" in keys:
        query = query.filter(Memory.project_id == (project_id or ""))
    if "team" in keys:
        query = query.filter(Memory.space_id == space_id)
    rows = query.order_by(Memory.id.desc()).all()
    return rows


def _version(db, version_id):
    return db.get(AgentVersion, version_id) if version_id else None


def pick_version(db, agent: Agent, user_id: int, environment: str) -> AgentVersion:
    """选版本。测试环境用最新版。生产按用户编号和灰度百分比决定走灰版本还是稳定版。"""
    latest = (
        db.query(AgentVersion)
        .filter(AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.number.desc())
        .first()
    )
    if environment == "test":
        if latest is None:
            raise GateError("denied", "智能体没有可运行的版本")
        return latest
    if agent.status == "offline":
        raise GateError("offline", "智能体已下线")
    stable = _version(db, agent.stable_version_id)
    gray = _version(db, agent.gray_version_id)
    if gray and agent.gray_percent > 0:
        bucket = zlib.crc32(str(user_id).encode()) % 100
        if bucket < agent.gray_percent:
            return gray
    if stable is None:
        raise GateError("denied", "没有已发布版本")
    return stable


def references_of(db, agent: Agent) -> list[dict]:
    rows = db.query(Workflow).filter(Workflow.agent_id == agent.id, Workflow.status != "archived").all()
    return [{"type": "workflow", "id": row.id, "name": row.name} for row in rows]


def _step(db, trace: Trace, kind: str, **kwargs) -> TraceStep:
    idx = db.query(TraceStep).filter(TraceStep.trace_id == trace.id).count()
    step = TraceStep(trace_id=trace.id, idx=idx, kind=kind, **kwargs)
    db.add(step)
    db.flush()
    return step


def _hop(db, trace: Trace, subject_user_id: int, actor: str, target: str, credential: str) -> None:
    seq = db.query(DelegationHop).filter(DelegationHop.trace_id == trace.id).count()
    db.add(
        DelegationHop(
            trace_id=trace.id,
            seq=seq,
            subject_user_id=subject_user_id,
            actor=actor,
            target=target,
            credential=credential,
        )
    )


def _account(db, space_id: int, name: str = "现金") -> LedgerAccount:
    row = db.query(LedgerAccount).filter_by(space_id=space_id, name=name).one_or_none()
    if row is None:
        row = LedgerAccount(space_id=space_id, name=name, balance=0)
        db.add(row)
        db.flush()
    return row


def execute_builtin(db, tool, space, user, message: str, args: dict, role: str):
    """按登记名称执行内置函数。

    登记记录只决定能不能调用。真正的函数在这个分支里：
    calculator、knowledge.search、ledger.transfer、python.sandbox、files.list。
    名称对不上就拒绝。http 只检查地址是否属于 allow.internal。
    """
    if tool.kind == "http":
        path = guard_url(args.get("url") or tool.endpoint)
        return {"path": path, "ok": True}
    if tool.kind == "sandbox" or tool.name == "python.sandbox":
        return run_sandbox(args.get("code") or "")
    if tool.name == "calculator":
        expression = args.get("expression")
        if not expression:
            found = EXPR.search(message or "")
            if not found:
                raise ValueError("没有可计算的表达式")
            expression = found.group(1)
        value = evaluate_expression(expression.replace(" ", ""))
        return value
    if tool.name == "knowledge.search":
        bases = args.get("base_ids")
        if bases is None:
            bases = ids_for(db, space.id, "knowledge")
        return search_knowledge(db, space.id, args.get("query") or message, role, base_ids=bases)
    if tool.name == "files.list":
        folder = args.get("path") or args.get("folder") or ""
        if not folder:
            found = FOLDER.search(message or "")
            folder = found.group(1) if found else "."
        return list_directory(folder)
    if tool.name == "ledger.transfer":
        amount = float(args.get("amount"))
        account = _account(db, space.id)
        if amount <= 0:
            raise ValueError("金额必须大于 0")
        if amount > account.balance:
            raise ValueError("余额不足，已由账本规则拒绝")
        account.balance = round(account.balance - amount, 2)
        return {"balance": account.balance, "amount": amount}
    raise GateError("denied", "未知工具实现")


def parse_amount(message: str) -> float | None:
    found = AMOUNT.search(message or "")
    return float(found.group(1)) if found else None



__all__ = [name for name in globals() if not name.startswith('__')]
