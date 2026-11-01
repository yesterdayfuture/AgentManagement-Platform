"""评测运行。

同一批用例跑在一个智能体或一个工作流上，按选定指标计分。
全部用例都通过时，智能体当前版本记为评测通过。
"""

import contextvars
import hashlib
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
from app.services.runtime.support import _DEFINITION
from app.services.memory_layers import evaluation_scope


def dataset_fingerprint(db, dataset_id: int) -> str:
    from app.models import EvalCase
    cases = db.query(EvalCase).filter_by(dataset_id=dataset_id).order_by(EvalCase.id.asc()).all()
    payload = [[item.id, item.input_text, item.expected, item.expected_tool] for item in cases]
    return hashlib.sha256(dump(payload).encode()).hexdigest()


def workflow_agent_fingerprint(db, workflow) -> str:
    nodes = load(workflow.definition_json, {}).get("nodes") or []
    versions = []
    for node in nodes:
        agent_id = int(node.get("agent_id") or 0)
        version = db.query(AgentVersion).filter_by(agent_id=agent_id).order_by(AgentVersion.number.desc()).first() if agent_id else None
        versions.append([node.get("id"), agent_id, version.id if version else None])
    return hashlib.sha256(dump(versions).encode()).hexdigest()


def workflow_release_current(db, workflow) -> bool:
    from app.models import EvalRun
    if not workflow.eval_dataset_id:
        return True  # Existing published workflows predate the evaluation gate.
    report = db.query(EvalRun).filter_by(workflow_id=workflow.id, workflow_revision=workflow.published_revision, dataset_id=workflow.eval_dataset_id).order_by(EvalRun.id.desc()).first()
    return bool(report and report.passed and report.agent_fingerprint == workflow_agent_fingerprint(db, workflow) and report.dataset_fingerprint == dataset_fingerprint(db, workflow.eval_dataset_id) and report.completion >= float(workflow.eval_min_pass_rate or 1))

@evaluation_scope()
def run_eval(db, dataset, user, space, role, policy, *, agent=None, workflow=None, agent_version_id: int | None = None, metrics: list[str] | None = None, min_pass_rate: float = 1.0, progress=None) -> dict:
    from app.models import EvalCase, EvalRun
    from app.services.evalkit import names, score_case

    chosen = metrics or ["contains"]
    version = None
    if agent is not None:
        version = db.get(AgentVersion, agent_version_id) if agent_version_id is not None else db.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).order_by(AgentVersion.number.desc()).first()
        if version is None or version.agent_id != agent.id:
            raise GateError("denied", "所选智能体版本不存在")
    elif workflow is not None and workflow.agent_id:
        version = db.query(AgentVersion).filter_by(agent_id=workflow.agent_id).order_by(AgentVersion.number.desc()).first()
    cases = db.query(EvalCase).filter(EvalCase.dataset_id == dataset.id).order_by(EvalCase.id.asc()).all()
    fingerprint = dataset_fingerprint(db, dataset.id)
    workflow_revision = int(workflow.revision or 1) if workflow else None
    agent_fingerprint = workflow_agent_fingerprint(db, workflow) if workflow else ""
    workflow_definition = load(workflow.definition_json, {}) if workflow else None
    details = []
    passed_count = 0
    cost = 0.0
    totals = {key: 0 for key in chosen}
    for case in cases:
        try:
            if workflow is not None:
                token = _DEFINITION.set(workflow_definition)
                try:
                    result = run_workflow(
                        db, workflow, user=user, space=space, message=case.input_text,
                        environment="test", role=role, policy=policy,
                    )
                finally:
                    _DEFINITION.reset(token)
                spent = 0.0
            else:
                result = execute_agent(
                    db, agent=agent, user=user, space=space, message=case.input_text,
                    environment="test", role=role, policy=policy,
                    version_id=version.id,
                )
                spent = result.get("cost") or 0
        except GateError as exc:
            result = {"status": exc.status, "output": exc.message, "trace_id": None}
            spent = 0.0
        trace = db.get(Trace, result.get("trace_id")) if result.get("trace_id") else None
        trace_ids = {trace.id} if trace else set()
        if workflow is not None and result.get("run_id"):
            recorded = db.get(WorkflowRun, result["run_id"])
            trace_ids.update(item.get("trace_id") for item in load(recorded.log_json, []) if isinstance(item, dict) and item.get("trace_id"))
        steps = db.query(TraceStep).filter(TraceStep.trace_id.in_(trace_ids), TraceStep.kind == "execute").all() if trace_ids else []
        generations = db.query(TraceStep).filter(TraceStep.trace_id.in_(trace_ids), TraceStep.kind == "generate").all() if trace_ids else []
        model_online = bool(generations) and not any(step.fallback for step in generations)
        tool_names = [step.name for step in steps if step.name]
        tool_text = "\n".join(step.output_text or "" for step in steps)
        latency = result.get("duration_ms") or (trace.latency_ms if trace else 0)
        if trace_ids:
            spent = sum((db.get(Trace, item).cost or 0) for item in trace_ids if db.get(Trace, item))
        scores = score_case(chosen, case, result.get("output") or "", result.get("status") or "", tool_names, tool_text, latency, spent, model_online=model_online)
        ok = all(scores.values()) if scores else False
        passed_count += int(ok)
        cost += spent
        for key, value in scores.items():
            totals[key] += int(value)
        details.append(
            {
                "id": case.id,
                "run_id": result.get("run_id"),
                "trace_id": result.get("trace_id"),
                "input": case.input_text,
                "expected": case.expected,
                "expected_tool": case.expected_tool or "",
                "output": result.get("output") or "",
                "status": result.get("status") or "",
                "latency_ms": latency,
                "cost": spent,
                "tools": tool_names,
                "model_online": model_online,
                "scores": scores,
                "passed": ok,
            }
        )
        # Each case has its own durable execution record. Releasing SQLite's
        # write transaction here keeps other users' requests responsive during
        # long benchmark runs; the final EvalRun is written after all cases.
        db.commit()
        if progress is not None:
            progress(len(details), len(cases), details[-1])
    completion = (passed_count / len(cases)) if cases else 0
    passed = bool(cases) and completion >= min_pass_rate
    if version is not None and agent is not None:
        version.eval_passed = passed
    label = names()
    summary = [
        {"key": key, "name": label.get(key, key), "passed": totals[key], "total": len(cases), "rate": (totals[key] / len(cases)) if cases else 0}
        for key in chosen
    ]
    row = EvalRun(
        dataset_id=dataset.id,
        agent_id=agent.id if agent else (workflow.agent_id if workflow else None),
        workflow_id=workflow.id if workflow else None,
        workflow_revision=workflow_revision,
        dataset_fingerprint=fingerprint,
        agent_fingerprint=agent_fingerprint,
        version_id=version.id if version else None,
        target_space_id=space.id,
        platform_only=(workflow.space_id if workflow else agent.space_id) != dataset.space_id,
        metrics_json=dump(chosen),
        completion=completion,
        quality=completion,
        min_pass_rate=min_pass_rate,
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
        "min_pass_rate": min_pass_rate,
        "metrics": summary,
        "details": details,
        "version_id": version.id if version else None,
        "target_version": workflow_revision if workflow else (version.number if version else None),
        "target_space_id": space.id,
        "note": "",
        "workflow_revision": workflow_revision,
        "target": {
            "kind": "workflow" if workflow is not None else "agent",
            "id": workflow.id if workflow is not None else agent.id,
            "name": workflow.name if workflow is not None else agent.name,
        },
        "agent": {"id": workflow.agent_id if workflow else agent.id, "name": (db.get(Agent, workflow.agent_id).name if workflow and workflow.agent_id else agent.name if agent else ""), "version": version.number if version else None, "model": (db.get(ModelEndpoint, version.model_id).name if version and version.model_id and db.get(ModelEndpoint, version.model_id) else "按任务选择")} if (workflow or agent) else None,
    }

__all__ = [name for name in globals() if not name.startswith('__')]
