"""评测运行。

同一批用例跑在一个智能体或一个工作流上，按选定指标计分。
全部用例都通过时，智能体当前版本记为评测通过。
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

from app.services.runtime.agent import execute_agent
from app.services.runtime.workflow import run_workflow

def run_eval(db, dataset, user, space, role, policy, *, agent=None, workflow=None, metrics: list[str] | None = None) -> dict:
    from app.models import EvalCase, EvalRun
    from app.services.evalkit import names, score_case

    chosen = metrics or ["contains"]
    version = None
    if agent is not None:
        version = (
            db.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).order_by(AgentVersion.number.desc()).first()
        )
        if version is None:
            raise GateError("denied", "智能体还没有版本")
    cases = db.query(EvalCase).filter(EvalCase.dataset_id == dataset.id).order_by(EvalCase.id.asc()).all()
    details = []
    passed_count = 0
    cost = 0.0
    totals = {key: 0 for key in chosen}
    for case in cases:
        if workflow is not None:
            result = run_workflow(
                db,
                workflow,
                user=user,
                space=space,
                message=case.input_text,
                environment="test",
                role=role,
                policy=policy,
            )
            spent = 0.0
        else:
            result = execute_agent(
                db,
                agent=agent,
                user=user,
                space=space,
                message=case.input_text,
                environment="test",
                role=role,
                policy=policy,
            )
            spent = result.get("cost") or 0
        trace = db.get(Trace, result.get("trace_id")) if result.get("trace_id") else None
        steps = db.query(TraceStep).filter_by(trace_id=trace.id, kind="execute").all() if trace else []
        tool_names = [step.name for step in steps if step.name]
        tool_text = "\n".join(step.output_text or "" for step in steps)
        latency = trace.latency_ms if trace else result.get("duration_ms") or 0
        if trace and not spent:
            spent = trace.cost or 0
        scores = score_case(chosen, case, result.get("output") or "", result.get("status") or "", tool_names, tool_text, latency, spent)
        ok = all(scores.values()) if scores else False
        passed_count += int(ok)
        cost += spent
        for key, value in scores.items():
            totals[key] += int(value)
        details.append(
            {
                "id": case.id,
                "input": case.input_text,
                "expected": case.expected,
                "expected_tool": case.expected_tool or "",
                "output": result.get("output") or "",
                "status": result.get("status") or "",
                "latency_ms": latency,
                "cost": spent,
                "tools": tool_names,
                "scores": scores,
                "passed": ok,
            }
        )
    completion = (passed_count / len(cases)) if cases else 0
    passed = bool(cases) and passed_count == len(cases)
    if version is not None:
        version.eval_passed = passed
    label = names()
    summary = [
        {"key": key, "name": label.get(key, key), "passed": totals[key], "total": len(cases), "rate": (totals[key] / len(cases)) if cases else 0}
        for key in chosen
    ]
    row = EvalRun(
        dataset_id=dataset.id,
        agent_id=agent.id if agent else None,
        workflow_id=workflow.id if workflow else None,
        version_id=version.id if version else None,
        metrics_json=dump(chosen),
        completion=completion,
        quality=completion,
        cost=cost,
        passed=passed,
        detail_json=dump(details),
    )
    db.add(row)
    db.flush()
    return {
        "id": row.id,
        "completion": completion,
        "quality": completion,
        "cost": cost,
        "passed": passed,
        "metrics": summary,
        "details": details,
        "version_id": version.id if version else None,
        "target": {
            "kind": "workflow" if workflow is not None else "agent",
            "id": workflow.id if workflow is not None else agent.id,
            "name": workflow.name if workflow is not None else agent.name,
        },
    }

__all__ = [name for name in globals() if not name.startswith('__')]
