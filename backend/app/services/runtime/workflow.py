"""工作流执行。

agents 按连线把上一个智能体的输出交给下一个。
autonomous 先拆任务，再分给候选智能体或由调度者自己执行。
旧图按 classify、tool、respond 走条件边。暂停和停止写在进程内的控制表。
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
from app.services.artifacts import project_files
from app.services.files import list_directory
from app.services.policy import apply_context_policy, isolation_keys
from app.services.retrieval import search_knowledge
from app.services.sandbox import evaluate_expression, run_sandbox
from app.util import dump, load

from app.services.runtime.support import *
from app.services.runtime.agent import *

def request_control(run_id: int, action: str) -> None:
    RUN_CONTROL[run_id] = action


def _agent_graph(definition: dict) -> bool:
    return any((node.get("type") == "agent") for node in definition.get("nodes") or [])


def _entry_node(definition, nodes, edges):
    named = definition.get("entry")
    if named in nodes:
        return named
    incoming = {node_id: 0 for node_id in nodes}
    for edge in edges:
        incoming[edge.get("dst")] = incoming.get(edge.get("dst"), 0) + 1
    return next((node_id for node_id, count in incoming.items() if count == 0), next(iter(nodes)))


def _clock(run: WorkflowRun, terminal: bool) -> None:
    moment = datetime.utcnow()
    if run.started_at:
        run.duration_ms = max(0, int((moment - run.started_at).total_seconds() * 1000))
    if terminal:
        run.finished_at = moment


def _run_payload(run: WorkflowRun) -> dict:
    return {
        "trace_id": run.trace_id or "",
        "status": run.status,
        "output": run.output_text or "",
        "run_id": run.id,
        "duration_ms": run.duration_ms,
        "files": sorted(project_files(run)),
    }


def _definition_of(workflow: Workflow) -> dict:
    current = _DEFINITION.get()
    if current is not None:
        return current
    return load(workflow.definition_json, {})


def _node_setup(db, space_id: int, node: dict | None) -> dict | None:
    if not node:
        return None
    setup = {}
    prompt = str(node.get("prompt") or "").strip()
    if prompt:
        setup["prompt"] = prompt
    if isinstance(node.get("tool_names"), list):
        ids = []
        for name in node["tool_names"]:
            tool = find_tool(db, space_id, str(name))
            if tool is not None:
                ids.append(tool.id)
        setup["tool_ids"] = ids
    if isinstance(node.get("knowledge_ids"), list):
        setup["knowledge_ids"] = [int(item) for item in node["knowledge_ids"]]
    if isinstance(node.get("mcp_ids"), list):
        setup["mcp_ids"] = [int(item) for item in node["mcp_ids"]]
    # 节点上的技能列表盖住智能体版本。只影响这一次运行。
    if isinstance(node.get("skill_ids"), list):
        setup["skill_ids"] = [int(item) for item in node["skill_ids"]]
    return setup or None


def _setup_for(db, space_id: int, nodes: dict, agent_id: int) -> dict | None:
    node = next((item for item in nodes.values() if int(item.get("agent_id") or 0) == int(agent_id)), None)
    return _node_setup(db, space_id, node)


def _append_log(run: WorkflowRun, item: dict) -> None:
    logs = load(run.log_json, [])
    logs.append(item)
    run.log_json = dump(logs)
    emit = _EMIT.get()
    if emit:
        emit(dict(item))


def _log_tools(run: WorkflowRun, node_id: str, agent_name: str, result: dict) -> None:
    for call in result.get("tool_calls") or []:
        _append_log(
            run,
            {
                "at": datetime.utcnow().isoformat(),
                "node": node_id,
                "agent": agent_name,
                "event": "工具",
                "tool": call.get("name") or "",
                "status": "完成",
                "input": call.get("input") or "",
                "output": call.get("output") or "",
                "ms": call.get("latency_ms") or 0,
                "trace_id": result.get("trace_id") or "",
            },
        )


def _note(item: dict) -> None:
    emit = _EMIT.get()
    if emit:
        emit(item)


def _await_control(run_id: int) -> str | None:
    """节点之间最多等 0.8 秒。另一个请求可以在这段时间写入暂停或停止。"""
    for _ in range(8):
        flag = RUN_CONTROL.get(run_id)
        if flag:
            return flag
        time.sleep(0.1)
    return RUN_CONTROL.get(run_id)


def _run_agent_graph(db, workflow: Workflow, *, user, space, message: str, environment: str, role: str, policy, pause_after: int | None = None, resume: WorkflowRun | None = None) -> dict:
    definition = _definition_of(workflow)
    nodes = {node["id"]: node for node in definition.get("nodes", []) if node.get("id")}
    edges = definition.get("edges", [])
    if resume is None:
        run = WorkflowRun(
            space_id=space.id,
            workflow_id=workflow.id,
            status="running",
            environment=environment,
            actor=user.username,
            message=message,
            carry_text=message,
            cursor=_entry_node(definition, nodes, edges),
            log_json="[]",
            started_at=datetime.utcnow(),
        )
        db.add(run)
        db.commit()
    else:
        run = resume
        run.status = "running"
        _append_log(run, {"at": datetime.utcnow().isoformat(), "node": run.cursor, "agent": "", "event": "继续", "status": "running", "input": run.carry_text, "output": "", "ms": 0, "trace_id": ""})
        db.commit()
    seen: dict[str, int] = {}
    hops = 0
    while run.cursor and hops < 8:
        hops += 1
        flag = RUN_CONTROL.pop(run.id, None)
        if flag == "pause":
            run.status = "paused"
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": run.cursor, "agent": "", "event": "暂停", "status": "paused", "input": run.carry_text, "output": run.output_text, "ms": 0, "trace_id": run.trace_id})
            _clock(run, False)
            db.commit()
            return _run_payload(run)
        if flag == "stop":
            run.status = "stopped"
            run.output_text = run.output_text or "已停止"
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": run.cursor, "agent": "", "event": "停止", "status": "stopped", "input": run.carry_text, "output": run.output_text, "ms": 0, "trace_id": run.trace_id})
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        seen[run.cursor] = seen.get(run.cursor, 0) + 1
        if seen[run.cursor] > 2:
            run.status = "blocked"
            run.output_text = "相同节点重复执行，已熔断"
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": run.cursor, "agent": "", "event": "熔断", "status": "blocked", "input": run.carry_text, "output": run.output_text, "ms": 0, "trace_id": ""})
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        node = nodes.get(run.cursor)
        if node is None:
            run.status = "failed"
            run.output_text = "节点不存在"
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        started = time.perf_counter()
        inbound = run.carry_text or message
        agent = db.get(Agent, int(node.get("agent_id") or 0))
        if agent is None or agent.space_id != space.id:
            run.status = "failed"
            run.output_text = "智能体不存在"
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        _note({"at": datetime.utcnow().isoformat(), "node": node["id"], "agent": agent.name, "event": "开始", "status": "running", "input": inbound, "output": "", "ms": 0, "trace_id": ""})
        try:
            result = execute_agent(
                db,
                agent=agent,
                user=user,
                space=space,
                message=inbound,
                environment=environment,
                role=role,
                policy=policy,
                setup=_node_setup(db, space.id, node),
            )
        except GateError as exc:
            run.status = exc.status
            run.output_text = exc.message
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": node["id"], "agent": agent.name, "event": "调用", "status": exc.status, "input": run.carry_text, "output": exc.message, "ms": int((time.perf_counter() - started) * 1000), "trace_id": ""})
            _clock(run, True)
            db.commit()
            raise
        trace = db.get(Trace, result["trace_id"])
        if trace is not None:
            trace.workflow_id = workflow.id
        elapsed = int((time.perf_counter() - started) * 1000)
        run.trace_id = result["trace_id"]
        run.carry_text = result["output"] or run.carry_text
        run.output_text = run.carry_text
        _log_tools(run, node["id"], agent.name, result)
        _append_log(
            run,
            {
                "at": datetime.utcnow().isoformat(),
                "node": node["id"],
                "agent": agent.name,
                "event": "调用",
                "status": result["status"],
                "input": inbound,
                "output": result["output"],
                "prompt": str(node.get("prompt") or ""),
                "ms": elapsed,
                "trace_id": result["trace_id"],
            },
        )
        if result["status"] != "completed":
            run.status = result["status"]
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        nxt = _next_node(edges, node["id"], classify_intent(run.carry_text or ""))
        run.cursor = nxt or ""
        db.commit()
        if pause_after and hops >= pause_after and run.cursor:
            run.status = "paused"
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": run.cursor, "agent": "", "event": "暂停", "status": "paused", "input": run.carry_text, "output": run.output_text, "ms": 0, "trace_id": run.trace_id})
            _clock(run, False)
            db.commit()
            return _run_payload(run)
        if run.cursor:
            _await_control(run.id)
    else:
        if run.cursor and hops >= 8 and run.status == "running":
            run.status = "blocked"
            run.output_text = "步数超过上限，已熔断"
    if run.status == "running":
        run.status = "completed"
    _clock(run, run.status != "paused")
    db.commit()
    return _run_payload(run)


def _record_legacy_run(db, workflow, trace: Trace, message: str, environment: str, status: str, output: str, user) -> WorkflowRun:
    steps = db.query(TraceStep).filter(TraceStep.trace_id == trace.id).order_by(TraceStep.idx).all()
    logs = [
        {
            "at": stamp_text(step),
            "node": step.name or step.kind,
            "agent": "",
            "event": step.kind,
            "status": status,
            "input": step.prompt or "",
            "output": step.output_text or "",
            "ms": step.latency_ms or 0,
            "trace_id": trace.id,
        }
        for step in steps
    ]
    moment = datetime.utcnow()
    run = WorkflowRun(
        space_id=workflow.space_id,
        workflow_id=workflow.id,
        trace_id=trace.id,
        status=status,
        environment=environment,
        actor=getattr(user, "username", ""),
        message=message,
        output_text=str(output),
        log_json=dump(logs),
        started_at=trace.created_at or moment,
        finished_at=moment,
        duration_ms=trace.latency_ms or 0,
    )
    db.add(run)
    db.flush()
    return run


def stamp_text(step) -> str:
    return datetime.utcnow().isoformat()


def _split_clauses(message: str) -> list[str]:
    """把一句话拆成子任务。编号列表优先；否则按换行、分号、然后、并且、同时切开。"""
    text = (message or "").strip()
    if not text:
        return []
    numbered = [item.strip() for item in re.split(r"(?:^|\n)\s*\d+[\.、]\s*", text) if item.strip()]
    if len(numbered) > 1:
        return numbered
    parts = [item.strip(" 。") for item in re.split(r"[\n；;]+|\s*(?:然后|并且|同时)\s*", text)]
    return [item for item in parts if item] or [text]


def _match_candidate(clause: str, ranked: list[dict]) -> dict | None:
    chosen = next((item for item in ranked if item.get("name") and item["name"] in clause), None)
    if chosen is None:
        chosen = next((item for item in ranked if item.get("key") and item["key"] in clause), None)
    return chosen


def _duty(db, person: dict) -> str:
    custom = str(person.get("prompt") or "").strip()
    if custom:
        return custom[:180]
    version = (
        db.query(AgentVersion)
        .filter(AgentVersion.agent_id == person["id"])
        .order_by(AgentVersion.number.desc())
        .first()
    )
    text = ((version.system_prompt if version else "") or "").strip().split("\n", 1)[0]
    return text[:180]


def _planning_model(db, space_id: int, lead: dict, environment: str):
    """规划只用能对话的模型。规则模型会抢走最便宜的位置，不能用来拆任务。"""
    agent = db.get(Agent, lead["id"])
    version = pick_version(db, agent, 0, environment) if agent else None
    pinned = db.get(ModelEndpoint, version.model_id) if version and version.model_id else None
    allowed = set(ids_for(db, space_id, "model"))
    if pinned and pinned.id in allowed and pinned.enabled and pinned.provider != "rules" and quota_open(pinned):
        return pinned
    rows = (
        db.query(ModelEndpoint)
        .filter(ModelEndpoint.enabled.is_(True), ModelEndpoint.provider != "rules", ModelEndpoint.id.in_(allowed or [-1]))
        .all()
    )
    rows = [row for row in rows if quota_open(row)]
    rows.sort(key=lambda row: (row.price_in or 0, row.priority or 0))
    return rows[0] if rows else None


def _roster(db, lead: dict, candidates: list[dict]) -> dict[str, dict]:
    found = {lead["name"]: lead, lead["key"]: lead}
    for person in candidates:
        found[person["name"]] = person
        found[person["key"]] = person
    return found


def _read_plan(text: str, lead: dict, roster: dict[str, dict]) -> list[dict] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = load(raw[start : end + 1], None)
    except ValueError:
        return None
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        return None
    planned = []
    for item in tasks[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        agent_name = str(item.get("agent") or item.get("agent_name") or "").strip()
        owner = roster.get(agent_name)
        if not title or owner is None:
            continue
        planned.append(
            {
                "title": title[:500],
                "agent_id": owner["id"],
                "agent_name": owner["name"],
                "decision": "自己执行" if owner["id"] == lead["id"] else "分发",
                "use_prior": bool(item.get("needs_prior") or item.get("use_prior")) and len(planned) > 0,
            }
        )
    return planned or None


def _plan_with_model(db, space, message: str, lead: dict, candidates: list[dict], environment: str) -> list[dict] | None:
    model = _planning_model(db, space.id, lead, environment)
    if model is None:
        return None
    roster = _roster(db, lead, candidates)
    members = [f"- {lead['name']}（调度者，可自己执行）：{_duty(db, lead)}"]
    members.extend(f"- {person['name']}：{_duty(db, person)}" for person in candidates)
    system_prompt = (
        "你是工作流调度者。阅读用户的问题，以及每位成员的职责，把问题拆成可执行的子任务并指定负责人。"
        "只根据问题需要决定拆几步、先后和由谁做，不要因为句子里出现某个词就机械分配。"
        "负责人必须是下面列出的名称之一。某一步适合自己完成时，负责人填调度者的名称。"
        "后一步需要前一步结果时，needs_prior 为 true。"
        "只输出 JSON，不要解释。格式："
        '{"tasks":[{"title":"具体要做的事","agent":"成员名称","needs_prior":false}]}'
    )
    user_text = "成员：\n" + "\n".join(members) + f"\n\n用户问题：{message}"
    text, _name, fallback, usage = complete(db, model, system_prompt, user_text, [], [])
    tokens_in, tokens_out = usage
    _cost_in, _cost_out, spent = cost_parts(model, tokens_in, tokens_out)
    charge_model(model, spent)
    if fallback:
        return None
    return _read_plan(text, lead, roster)


def _plan_without_model(message: str, lead: dict, candidates: list[dict]) -> list[dict]:
    """没有可用对话模型时，才按子句和名称兜底。正常规划不走这里。"""
    ranked = sorted(candidates, key=lambda item: len(item.get("name") or ""), reverse=True)
    tasks = []
    for clause in _split_clauses(message):
        chosen = _match_candidate(clause, ranked)
        owner = chosen or lead
        tasks.append(
            {
                "title": clause,
                "agent_id": owner["id"],
                "agent_name": owner["name"],
                "decision": "分发" if chosen else "自己执行",
                "use_prior": False,
            }
        )
    return tasks


def _plan_tasks(db, space, message: str, lead: dict, candidates: list[dict], environment: str) -> list[dict]:
    """由调度者调用模型，按当前问题和成员职责拆任务。"""
    planned = _plan_with_model(db, space, message, lead, candidates, environment)
    if planned:
        return planned
    return _plan_without_model(message, lead, candidates)


def _person(db, space_id: int, node: dict) -> dict | None:
    agent = db.get(Agent, int(node.get("agent_id") or 0))
    if agent is None or agent.space_id != space_id:
        return None
    return {"id": agent.id, "name": agent.name, "key": agent.key, "node": node.get("id") or "", "prompt": node.get("prompt") or ""}


def _saved_plan(run: WorkflowRun) -> list[dict] | None:
    for item in load(run.log_json, []):
        if item.get("event") == "规划":
            plan = load(item.get("output") or "[]", [])
            if isinstance(plan, list) and plan:
                return plan
    return None


def _run_autonomous(db, workflow: Workflow, *, user, space, message: str, environment: str, role: str, policy, pause_after: int | None = None, resume: WorkflowRun | None = None) -> dict:
    definition = _definition_of(workflow)
    nodes = {node["id"]: node for node in definition.get("nodes", []) if node.get("id") and node.get("type") == "agent"}
    edges = definition.get("edges", [])
    if not nodes:
        raise GateError("denied", "工作流没有可调度的智能体")
    lead_node = nodes.get(_entry_node(definition, nodes, edges)) or next(iter(nodes.values()))
    lead = _person(db, space.id, lead_node)
    if lead is None:
        raise GateError("denied", "调度智能体不存在")
    linked = [edge.get("dst") for edge in edges if edge.get("src") == lead_node["id"] and not edge.get("when")]
    pool = [nodes[item] for item in linked if item in nodes] or [node for node in nodes.values() if node["id"] != lead_node["id"]]
    candidates = [person for node in pool if (person := _person(db, space.id, node))]
    if resume is None:
        _note({"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": lead["name"], "event": "规划中", "status": "running", "input": message, "output": "", "ms": 0, "trace_id": ""})
        planning_started = time.perf_counter()
        plan = _plan_tasks(db, space, message, lead, candidates, environment)
        planning_ms = int((time.perf_counter() - planning_started) * 1000)
        run = WorkflowRun(
            space_id=space.id,
            workflow_id=workflow.id,
            status="running",
            environment=environment,
            actor=user.username,
            message=message,
            carry_text=message,
            cursor="0",
            log_json="[]",
            started_at=datetime.utcnow(),
        )
        db.add(run)
        db.flush()
        _append_log(
            run,
            {
                "at": datetime.utcnow().isoformat(),
                "node": lead["node"],
                "agent": lead["name"],
                "event": "规划",
                "status": "completed",
                "input": message,
                "output": dump(plan),
                "prompt": lead.get("prompt") or "",
                "ms": planning_ms,
                "trace_id": "",
            },
        )
        db.commit()
    else:
        run = resume
        plan = _saved_plan(run) or _plan_tasks(db, space, message or run.message, lead, candidates, environment)
        run.status = "running"
        _append_log(run, {"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": lead["name"], "event": "继续", "status": "running", "input": run.carry_text, "output": "", "ms": 0, "trace_id": ""})
        db.commit()
    try:
        index = int(run.cursor or "0")
    except ValueError:
        index = 0
    while index < len(plan):
        flag = RUN_CONTROL.pop(run.id, None)
        if flag == "pause":
            run.status = "paused"
            run.cursor = str(index)
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": lead["name"], "event": "暂停", "status": "paused", "input": "", "output": run.output_text, "ms": 0, "trace_id": run.trace_id})
            _clock(run, False)
            db.commit()
            return _run_payload(run)
        if flag == "stop":
            run.status = "stopped"
            run.output_text = run.output_text or "已停止"
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": "", "event": "停止", "status": "stopped", "input": "", "output": run.output_text, "ms": 0, "trace_id": run.trace_id})
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        task = plan[index]
        agent = db.get(Agent, int(task["agent_id"]))
        if agent is None or agent.space_id != space.id:
            run.status = "failed"
            run.output_text = "子任务的智能体不存在"
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        started = time.perf_counter()
        task_message = task["title"]
        if task.get("use_prior") and run.carry_text and run.carry_text != message:
            task_message = f"{task['title']}\n参考材料：\n{run.carry_text[-1500:]}"
        _note({"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": agent.name, "event": "开始", "status": "running", "input": task["title"], "output": "", "ms": 0, "trace_id": ""})
        try:
            result = execute_agent(
                db,
                agent=agent,
                user=user,
                space=space,
                message=task_message,
                environment=environment,
                role=role,
                policy=policy,
                setup=_setup_for(db, space.id, nodes, agent.id),
            )
        except GateError as exc:
            run.status = exc.status
            run.output_text = exc.message
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": agent.name, "event": task["decision"], "status": exc.status, "input": task["title"], "output": exc.message, "ms": int((time.perf_counter() - started) * 1000), "trace_id": ""})
            _clock(run, True)
            db.commit()
            raise
        trace = db.get(Trace, result["trace_id"])
        if trace is not None:
            trace.workflow_id = workflow.id
        elapsed = int((time.perf_counter() - started) * 1000)
        run.trace_id = result["trace_id"]
        line = f"{task['decision']} · {agent.name}：{result['output']}"
        run.output_text = f"{run.output_text}\n{line}".strip() if run.output_text else line
        run.carry_text = result["output"] or run.carry_text
        _log_tools(run, lead["node"], agent.name, result)
        _append_log(
            run,
            {
                "at": datetime.utcnow().isoformat(),
                "node": lead["node"],
                "agent": agent.name,
                "event": task["decision"],
                "status": result["status"],
                "input": task["title"],
                "output": result["output"],
                "prompt": str((_setup_for(db, space.id, nodes, agent.id) or {}).get("prompt") or ""),
                "ms": elapsed,
                "trace_id": result["trace_id"],
            },
        )
        index += 1
        run.cursor = str(index) if index < len(plan) else ""
        if result["status"] != "completed":
            run.status = result["status"]
            _clock(run, True)
            db.commit()
            return _run_payload(run)
        db.commit()
        if pause_after and index >= pause_after and run.cursor:
            run.status = "paused"
            _append_log(run, {"at": datetime.utcnow().isoformat(), "node": lead["node"], "agent": "", "event": "暂停", "status": "paused", "input": "", "output": run.output_text, "ms": 0, "trace_id": run.trace_id})
            _clock(run, False)
            db.commit()
            return _run_payload(run)
        if run.cursor:
            _await_control(run.id)
    if run.status == "running":
        run.status = "completed"
        run.cursor = ""
    _clock(run, run.status != "paused")
    db.commit()
    return _run_payload(run)


def run_workflow(db, workflow: Workflow, *, user, space, message: str, environment: str, role: str, policy, pause_after: int | None = None, resume: WorkflowRun | None = None) -> dict:
    """按定义种类分发。自主调度优先于智能体连线图，因为自主图的节点类型也是 agent。"""
    if space.token_used >= space.token_budget:
        raise GateError("quota", "空间令牌额度已用尽")
    definition = _definition_of(workflow)
    if definition.get("kind") == "autonomous":
        return _run_autonomous(
            db,
            workflow,
            user=user,
            space=space,
            message=message,
            environment=environment,
            role=role,
            policy=policy,
            pause_after=pause_after,
            resume=resume,
        )
    if _agent_graph(definition):
        return _run_agent_graph(
            db,
            workflow,
            user=user,
            space=space,
            message=message,
            environment=environment,
            role=role,
            policy=policy,
            pause_after=pause_after,
            resume=resume,
        )
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    edges = definition.get("edges", [])
    if not nodes:
        raise GateError("denied", "工作流没有节点")
    incoming = {node_id: 0 for node_id in nodes}
    for edge in edges:
        if edge.get("when"):
            continue
        incoming[edge["dst"]] = incoming.get(edge["dst"], 0) + 1
    entry = definition.get("entry") or next(node_id for node_id, count in incoming.items() if count == 0)
    trace = Trace(
        id=uuid.uuid4().hex,
        space_id=space.id,
        agent_id=workflow.agent_id,
        workflow_id=workflow.id,
        user_id=user.id,
        environment=environment,
        status="running",
        input_text=apply_context_policy(message, policy)[0],
    )
    db.add(trace)
    db.flush()
    agent = db.get(Agent, workflow.agent_id) if workflow.agent_id else None
    version = pick_version(db, agent, user.id, "test" if environment == "test" else environment) if agent else None
    tool_ids = load(version.tool_ids_json, []) if version else []
    seen: dict[str, int] = {}
    current = entry
    output = ""
    status = "completed"
    intent = classify_intent(message)
    guard = 0
    while current and guard < 8:
        guard += 1
        seen[current] = seen.get(current, 0) + 1
        if seen[current] > 2:
            status = "blocked"
            output = "相同节点重复执行，已熔断"
            _step(db, trace, "breaker", name=current, output_text=output)
            break
        node = nodes[current]
        kind = node.get("type")
        try:
            if kind == "classify":
                intent = classify_intent(message)
                _step(db, trace, "perceive", name=node["id"], output_text=intent)
                output = intent
            elif kind == "tool":
                if agent is None:
                    raise GateError("denied", "工作流未绑定智能体")
                result = _call_tool(
                    db,
                    trace,
                    agent,
                    version,
                    space,
                    user,
                    node["tool"],
                    message,
                    node.get("args") or {},
                    role,
                    environment,
                    tool_ids,
                )
                output = result if isinstance(result, str) else dump(result)
            elif kind == "respond":
                _step(db, trace, "generate", name=node["id"], output_text=str(output))
            else:
                raise GateError("denied", "未知节点")
        except GateError as exc:
            status = exc.status
            output = exc.message
            _step(db, trace, "gate", name=node["id"], output_text=exc.message)
            break
        current = _next_node(edges, current, intent)
    else:
        if guard >= 8 and status == "completed":
            status = "blocked"
            output = "步数超过上限，已熔断"
    trace.status = status
    trace.output_text = str(output)
    trace.tokens_in = max(1, len(message) // 4)
    trace.tokens_out = max(1, len(str(output)) // 4)
    space.token_used += trace.tokens_in + trace.tokens_out
    db.flush()
    recorded = _record_legacy_run(db, workflow, trace, message, environment, status, trace.output_text, user)
    return {"trace_id": trace.id, "status": status, "output": trace.output_text, "run_id": recorded.id, "duration_ms": recorded.duration_ms}


def _next_node(edges, current, intent):
    conditional = [edge for edge in edges if edge["src"] == current and edge.get("when")]
    for edge in conditional:
        if edge["when"] == intent:
            return edge["dst"]
    if conditional:
        return None
    plain = [edge for edge in edges if edge["src"] == current and not edge.get("when")]
    return plain[0]["dst"] if plain else None



__all__ = [name for name in globals() if not name.startswith('__')]
