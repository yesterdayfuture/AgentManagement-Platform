"""一次智能体执行。

连得上的模型走工具循环，最多 _LOOP_TURNS 轮。
同一段对话里，同一个工具用相同参数成功执行过后不再调用。
最后一轮仍是工具调用时记为 limited，并保存对话，供下一次继续。
规则模型不进入循环，按关键词分流到计算、检索、列目录、搜索或账本。
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
from app.services.grants import ids_for, is_platform
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

from app.services.runtime.support import *

def _facade():
    """运行时门面。循环通过它读取可被测试替换的模型调用、轮次和退避。"""
    import app.services.runtime as api
    return api


def _overlay(version, setup: dict | None):
    """本次运行临时盖住提示词、工具和知识库，不写回已保存的版本。"""
    if not setup:
        return version
    view = type("VersionView", (), {})()
    view.id = version.id
    view.number = version.number
    view.model_id = version.model_id
    view.child_ids_json = version.child_ids_json
    view.mcp_ids_json = dump(setup["mcp_ids"]) if "mcp_ids" in setup else version.mcp_ids_json
    view.skill_ids_json = dump(setup["skill_ids"]) if "skill_ids" in setup else getattr(version, "skill_ids_json", "[]")
    view.system_prompt = setup.get("prompt") or version.system_prompt
    view.tool_ids_json = dump(setup["tool_ids"]) if "tool_ids" in setup else version.tool_ids_json
    view.knowledge_ids = setup.get("knowledge_ids")
    return view


def execute_agent(
    db,
    *,
    agent: Agent,
    user,
    space,
    message: str,
    environment: str,
    project_id: str = "",
    role: str,
    depth: int = 0,
    credential: str = "user-session",
    policy=None,
    setup: dict | None = None,
    history: list | None = None,
    resume_trace_id: str = "",
) -> dict:
    """跑一个智能体。depth 用来限制转交只发生一层，避免子智能体再转交。"""
    if space.token_used >= space.token_budget:
        raise GateError("quota", "空间令牌额度已用尽")
    started = time.perf_counter()
    prior = _prior_dialogue(db, space, agent, history or [], resume_trace_id, policy)
    masked, notes = apply_context_policy(message, policy)
    if not masked and not prior:
        raise GateError("denied", "请填写输入")
    version = _overlay(pick_version(db, agent, user.id, environment), setup)
    trace = Trace(
        id=uuid.uuid4().hex,
        space_id=space.id,
        agent_id=agent.id,
        user_id=user.id,
        version_id=version.id,
        environment=environment,
        status="running",
        input_text=masked or "继续上一段对话",
    )
    db.add(trace)
    db.flush()
    _hop(db, trace, user.id, agent.workload_id, agent.key, credential)
    classify_model = select_model(db, space.id, "classify")
    intent = classify_intent(masked)
    classify_in = max(1, len(masked) // 4)
    classify_out = 1
    cost_in, cost_out, classify_cost = cost_parts(classify_model, classify_in, classify_out)
    charge_model(classify_model, classify_cost)
    _step(
        db,
        trace,
        "perceive",
        name="intent",
        model=classify_model.name if classify_model else "",
        prompt=masked,
        output_text=intent,
        tokens=classify_in + classify_out,
        tokens_in=classify_in,
        tokens_out=classify_out,
        cost=classify_cost,
        cost_in=cost_in,
        cost_out=cost_out,
    )
    if notes:
        _step(db, trace, "policy", name="context", output_text="；".join(notes))
    tool_ids = load(version.tool_ids_json, [])
    citations = []
    output = ""
    status = "completed"
    reason_model = _pick_reason_model(db, version, space, user, agent)
    try:
        if intent == "delegate" and depth < 1:
            output, status = _delegate(db, trace, version, user, space, masked, environment, project_id, role, policy)
        elif _can_loop(db, reason_model):
            output, status, citations = _agent_loop(
                db, trace, agent, version, space, user, masked, environment, role, reason_model, tool_ids, prior
            )
            if status == "fallback":
                output, status, citations = _route(
                    db, trace, agent, version, space, user, masked, environment, project_id, role, policy, intent, tool_ids
                )
        else:
            output, status, citations = _route(
                db, trace, agent, version, space, user, masked, environment, project_id, role, policy, intent, tool_ids
            )
        if status in {"completed", "limited"}:
            prefix = f"【v{version.number}】"
            output = f"{prefix}{output}"
    except GateError as exc:
        status = exc.status if exc.status != "offline" else "denied"
        output = exc.message
        trace.error = exc.message
        _step(db, trace, "gate", name="tool-gateway", output_text=exc.message)
    except ValueError as exc:
        status = "failed"
        output = str(exc)
        trace.error = str(exc)
        _step(db, trace, "error", name="execute", output_text=str(exc))
    trace.status = status
    trace.output_text = output
    trace.citations_json = dump(citations)
    trace.latency_ms = int((time.perf_counter() - started) * 1000)
    steps = db.query(TraceStep).filter(TraceStep.trace_id == trace.id).all()
    trace.tokens_in = sum(step.tokens_in or 0 for step in steps) or max(1, len(masked) // 4)
    trace.tokens_out = sum(step.tokens_out or 0 for step in steps) or max(1, len(output) // 4)
    trace.cost = round(sum(step.cost for step in steps), 6)
    space.token_used += trace.tokens_in + trace.tokens_out
    _remember(db, policy, space, agent, user, project_id, masked)
    db.flush()
    return {
        "trace_id": trace.id,
        "status": status,
        "output": output,
        "citations": citations,
        "version": version.number,
        "cost": trace.cost,
        "tokens": trace.tokens_in + trace.tokens_out,
        "tool_calls": _tool_records(steps),
        "limited": status == "limited",
    }


def _pick_reason_model(db, version, space, user=None, agent=None):
    pinned = db.get(ModelEndpoint, version.model_id) if version.model_id else None
    if agent is not None and agent.space_id is None and is_platform(user):
        if pinned and pinned.enabled and quota_open(pinned):
            return pinned
        rows = [row for row in db.query(ModelEndpoint).filter(ModelEndpoint.enabled.is_(True)).all() if quota_open(row)]
        matched = [row for row in rows if "reason" in load(row.task_types_json, [])]
        pool = matched or rows
        if not pool:
            return None
        pool.sort(key=lambda row: (row.price_in or 0, row.priority or 0))
        return pool[0]
    allowed = set(ids_for(db, space.id, "model"))
    if pinned and pinned.id in allowed and pinned.enabled and quota_open(pinned):
        return pinned
    return select_model(db, space.id, "reason")


def _can_loop(db, model) -> bool:
    from app.services.gateway import _model_key

    return bool(model and model.provider != "rules" and _model_key(db, model))


def _route(db, trace, agent, version, space, user, masked, environment, project_id, role, policy, intent, tool_ids):
    """规则模型的单步分流。连得上模型时不走这里。"""
    citations = []
    if intent == "calculate":
        output = _call_tool(db, trace, agent, version, space, user, "calculator", masked, {}, role, environment, tool_ids)
        return output, "completed", citations
    if intent == "list":
        found = _call_tool(db, trace, agent, version, space, user, "files.list", masked, {}, role, environment, tool_ids)
        names = "、".join(item["name"] for item in found.get("entries", [])) or "空文件夹"
        return f"{found.get('path')}：{names}", "completed", citations
    if intent == "retrieve":
        query_args = {"query": masked}
        if getattr(version, "knowledge_ids", None) is not None:
            query_args["base_ids"] = version.knowledge_ids
        found = _call_tool(
            db, trace, agent, version, space, user, "knowledge.search", masked, query_args, role, environment, tool_ids
        )
        citations = found if isinstance(found, list) else []
        output = citations[0]["excerpt"] if citations else "知识库没有命中"
        return output, "completed", citations
    if intent == "write":
        output, status = _write_flow(db, trace, agent, version, space, user, masked, role, environment, tool_ids)
        return output, status, citations
    if intent == "search" and load(version.mcp_ids_json, []):
        return _search_web(db, trace, version, masked), "completed", citations
    output, status = _chat(db, trace, version, space, user, masked, project_id, policy)
    return output, status, citations


_LOOP_TURNS = 20
_SEARCH_BUDGET = 3
_LIMIT_TEXT = "循环已达到上限（20 轮），最后一轮仍是工具调用，已停止继续调用。可以继续执行，继续时会带上当前对话和已有工具结果。"


def _api_tool_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]", "_", name or "tool")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned[:64]


def _prompt_for_model(text: str) -> str:
    """系统提示词留给智能体自己的说明。工具介绍改由接口的 tools 参数传递。"""
    source = text or ""
    start = source.find("【可用工具】")
    end = source.find("【可用工具结束】")
    if start >= 0 and end > start:
        source = f"{source[:start]}{source[end + len('【可用工具结束】'):]}"
    return source.strip()


def _introduce(summary: str, parameters: list, example: str = "") -> str:
    lines = [summary] if summary else []
    for item in parameters or []:
        name = item.get("name") or ""
        if not name:
            continue
        mark = "必填" if item.get("required") else "可选"
        lines.append(f"{name}（{mark}）：{item.get('description') or ''}")
    if example:
        lines.append(f"示例：{example}")
    return "\n".join(lines)[:4000]


def _tool_catalog(db, version, space):
    """当前版本能调用的函数，名称换成接口允许的形式。"""
    from app.models import McpServer, Secret
    from app.services.builtins import detail_of, schema_of
    from app.services.mcp_client import McpError, connect
    from app.services.vault import decrypt

    specs = []
    bound = {}
    for tool_id in load(version.tool_ids_json, []):
        tool = db.get(Tool, tool_id)
        if tool is None or not tool.enabled:
            continue
        api_name = _api_tool_name(tool.name)
        if api_name in bound:
            continue
        detail = detail_of(tool.name, load(tool.parameters_json, {}))
        schema = schema_of(tool.name) if tool.name else {"type": "object", "properties": {}}
        if not schema.get("properties"):
            schema = load(tool.parameters_json, {}) or {"type": "object", "properties": {}}
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": api_name,
                    "description": _introduce(detail.get("summary") or tool.description or tool.name, detail.get("parameters") or [], detail.get("example") or ""),
                    "parameters": schema if schema.get("type") else {"type": "object", "properties": {}},
                },
            }
        )
        bound[api_name] = {"kind": "builtin", "name": tool.name}
    for server in db.query(McpServer).filter(McpServer.id.in_(load(version.mcp_ids_json, []) or [-1]), McpServer.enabled.is_(True)):
        token = ""
        if server.secret_id:
            secret = db.get(Secret, server.secret_id)
            token = decrypt(secret.ciphertext) if secret else ""
        try:
            listed = connect(server.url, token)
        except McpError:
            continue
        for item in listed.get("tools") or []:
            api_name = _api_tool_name(item.get("name") or "")
            if not api_name or api_name in bound:
                continue
            schema = item.get("input_schema") or {"type": "object", "properties": {}}
            fields = []
            required = set(schema.get("required") or [])
            for key, spec in (schema.get("properties") or {}).items():
                fields.append({"name": key, "required": key in required, "description": (spec or {}).get("description") or ""})
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": api_name,
                        "description": _introduce(item.get("description") or item.get("name") or "", fields),
                        "parameters": schema if schema.get("type") else {"type": "object", "properties": {}},
                    },
                }
            )
            bound[api_name] = {
                "kind": "mcp",
                "name": item.get("name") or "",
                "server_id": server.id,
                "secret_id": server.secret_id,
                "token": token,
                "url": server.url,
            }
    # 技能共用一个函数。说明里写清何时使用和脚本名，模型据此判断要不要调用、调用哪一个。
    skills = _selected_skills(db, version)
    if skills:
        order = {item: index for index, item in enumerate(load(getattr(version, "skill_ids_json", "[]"), []))}
        skills.sort(key=lambda row: order.get(row.id, 0))
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": "skill_script",
                    "description": _skill_call_description(skills),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string", "enum": [item.key for item in skills], "description": "与用户意图匹配的技能标识"},
                            "script": {"type": "string", "description": "该技能说明里列出的脚本文件名"},
                            "args": {"type": "array", "items": {"type": "string"}, "description": "按该技能说明传给脚本的参数"},
                        },
                        "required": ["skill", "script"],
                    },
                },
            }
        )
        bound["skill_script"] = {"kind": "skill", "name": "skill_script"}
    return specs, bound


def _selected_skills(db, version) -> list:
    """本次运行允许使用的技能。编号来自版本，或来自工作流节点的临时覆盖。停用的不返回。"""
    from app.models import Skill

    ids = load(getattr(version, "skill_ids_json", "[]"), [])
    if not ids:
        return []
    return db.query(Skill).filter(Skill.id.in_(ids), Skill.enabled.is_(True)).all()


SKILL_START = "【可用技能】"
SKILL_END = "【可用技能结束】"


def _strip_marked(text: str, start: str, end: str) -> str:
    source = text or ""
    left = source.find(start)
    right = source.find(end)
    if left >= 0 and right > left:
        source = f"{source[:left]}{source[right + len(end):]}"
    return source.strip()


def _skill_piece(skill) -> str:
    """单个技能的介绍。正文过长时截断，保留何时使用和脚本名，避免挤掉其他技能。"""
    scripts = "、".join(load(skill.scripts_json, [])) or "无"
    lines = [f"【{skill.name} / {skill.key}】", f"何时使用：{skill.description or '没有说明'}"]
    body = (skill.body or "").strip()
    if len(body) > 600:
        body = body[:600].rstrip() + "…"
    if body:
        lines.append(body)
    lines.append(f"可用脚本：{scripts}")
    return "\n".join(lines)


def _skill_call_description(skills) -> str:
    """函数说明本身带上匹配条件，模型不靠用户点名也能选择技能和脚本。"""
    lines = ["先判断用户要办的事是否匹配某个技能的「何时使用」。匹配时才调用，并使用该技能列出的脚本。都不匹配时不要调用。"]
    for skill in skills:
        scripts = "、".join(load(skill.scripts_json, [])) or "无"
        lines.append(f"{skill.key}（{skill.name}）：{skill.description or '没有说明'}。脚本：{scripts}。")
    return "\n".join(lines)[:4000]


def skill_section(skills) -> str:
    """勾选技能的介绍。脚本正文不放进来，只能通过 skill_script 执行。"""
    if not skills:
        return ""
    body = "\n\n".join(_skill_piece(skill) for skill in skills)
    if len(body) > 3500:
        body = body[:3500].rstrip() + "\n…"
    intro = "先根据用户要办的事对照下面的「何时使用」。只有匹配时才调用函数 skill_script：skill 填该技能标识，script 填该技能列出的文件名，args 填参数。没有匹配的技能时不要调用，也不要自己猜测脚本名。"
    return f"{SKILL_START}\n{intro}\n\n{body}\n\n{SKILL_END}"


def compose_skill_prompt(prompt: str, skills) -> str:
    """把技能介绍放进提示词的「可用技能」一段。已有这段时先去掉，避免重复。"""
    base = _strip_marked(prompt, SKILL_START, SKILL_END)
    section = skill_section(skills)
    if not section:
        return base
    return f"{base}\n\n{section}".strip() if base else section


def _with_skills(db, version, prompt: str) -> str:
    """运行时用当前技能说明替换提示词里的「可用技能」一段。

    模型在第一轮就能读到名称、何时使用和正文。技能已在保存时写入提示词；
    这里再按数据库里的最新说明生成一次，避免技能改过之后模型仍读旧介绍。
    """
    skills = _selected_skills(db, version)
    order = {item: index for index, item in enumerate(load(getattr(version, "skill_ids_json", "[]"), []))}
    skills.sort(key=lambda row: order.get(row.id, 0))
    return compose_skill_prompt(prompt, skills)


def _lookup_tool(bound: dict, name: str):
    if name in bound:
        return bound[name]
    target = _api_tool_name(name)
    for key, spec in bound.items():
        if key == target or _api_tool_name(spec.get("name") or "") == target:
            return spec
    return None


def _record_generate(db, trace, model, prompt, text, usage, spent_model, fallback=False):
    tokens_in, tokens_out = usage
    cost_in, cost_out, spent = cost_parts(spent_model, tokens_in, tokens_out)
    charge_model(spent_model, spent)
    _step(
        db,
        trace,
        "generate",
        name="reply",
        model=model.name if model else "",
        prompt=prompt,
        output_text=text,
        tokens=tokens_in + tokens_out,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=spent,
        cost_in=cost_in,
        cost_out=cost_out,
        fallback=fallback,
        secret_id=model.secret_id if model else None,
    )


_MODEL_TOOL_CHARS = 1800
_TOOL_ATTEMPTS = 3
_TOOL_BACKOFF = (0.5, 1.0)


class ToolFault(Exception):
    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _failure_kind(exc: BaseException) -> str:
    kind = getattr(exc, "kind", "")
    if kind in {"argument", "transient", "other"}:
        return kind
    text = str(exc)
    if isinstance(exc, (TypeError, KeyError)) or _argument_text(text):
        return "argument"
    if _transient_text(text):
        return "transient"
    return "other"


def _argument_text(text: str) -> bool:
    return any(word in text for word in ("参数", "表达式", "搜索词", "请填写", "过长", "没有可计算", "金额必须", "金额不合法", "未知工具"))


def _transient_text(text: str) -> bool:
    """连接被掐断才重试。搜索源已经返回「暂时不可用」时，工具内部试过备用源，不再整段重跑。"""
    if "暂时不可用" in (text or ""):
        return False
    folded = text.lower()
    return any(
        word in folded
        for word in ("timeout", "timed out", "超时", "连不上", "connection", "unavailable", "重置")
    )


def _fault_for_model(kind: str, message: str, attempts: int) -> str:
    if kind == "argument":
        return f"参数错误：{message}。请按工具说明重新输出调用参数。"
    if kind == "transient":
        return f"工具暂时失败，已退避重试 {attempts} 次：{message}。请更换参数，或根据已有结果直接回答，不要用相同参数再次调用。"
    return message


def _for_model(text: str) -> str:
    body = text or ""
    if len(body) <= _MODEL_TOOL_CHARS:
        return body
    return body[:_MODEL_TOOL_CHARS] + "\n…结果已截断"


def _run_skill(db, trace, version, args: dict):
    """执行模型通过 function calling 点名的那一个脚本。

    skill 必须是本次已选技能的标识，script 必须在该技能的脚本清单里。
    通过之后才把文件送进容器。参数错误记成轨迹步骤，再退回给模型。
    """
    from app.services.skillbox import run_script

    skill_key = str((args or {}).get("skill") or "").strip()
    script = str((args or {}).get("script") or "").strip()
    skill = next((item for item in _selected_skills(db, version) if item.key == skill_key), None)
    if skill is None:
        raise ToolFault("技能不在本次可用范围内", "argument")
    scripts = load(skill.scripts_json, [])
    if script not in scripts:
        raise ToolFault("这个技能没有该脚本", "argument")
    started = time.perf_counter()
    try:
        text = run_script(skill.id, script, (args or {}).get("args") if isinstance((args or {}).get("args"), list) else [])
    except (ValueError, RuntimeError) as exc:
        _step(db, trace, "execute", name=f"{skill.key}/{script}", tool_args=dump(args), output_text=_clip(str(exc)), latency_ms=int((time.perf_counter() - started) * 1000))
        kind = "argument" if isinstance(exc, ValueError) else "other"
        raise ToolFault(str(exc), kind) from exc
    _step(db, trace, "execute", name=f"{skill.key}/{script}", tool_args=dump(args), output_text=_clip(text), latency_ms=int((time.perf_counter() - started) * 1000))
    return _clip(text), "completed", []


def _run_bound(db, trace, agent, version, space, user, message, args, role, environment, tool_ids, spec):
    if spec["kind"] == "skill":
        return _run_skill(db, trace, version, args)
    if spec["kind"] == "mcp":
        from app.services.gateway import chat_emit
        from app.services.mcp_client import McpError, call_tool

        chat_emit({"type": "tool", "name": spec["name"]})
        started = time.perf_counter()
        try:
            result = call_tool(spec["url"], spec.get("token") or "", spec["name"], args)
        except McpError as exc:
            _step(
                db,
                trace,
                "execute",
                name=spec["name"],
                tool_args=dump(args),
                output_text=_clip(str(exc)),
                latency_ms=int((time.perf_counter() - started) * 1000),
                secret_id=spec.get("secret_id"),
            )
            raise
        text = (result.get("text") or "").strip() or "没有结果"
        if result.get("ok") is False:
            _step(
                db,
                trace,
                "execute",
                name=spec["name"],
                tool_args=dump(args),
                output_text=_clip(text),
                latency_ms=int((time.perf_counter() - started) * 1000),
                secret_id=spec.get("secret_id"),
            )
            kind = "argument" if _argument_text(text) else "transient" if _transient_text(text) else "other"
            raise ToolFault(text, kind)
        _step(db, trace, "execute", name=spec["name"], tool_args=dump(args), output_text=_clip(text), secret_id=spec.get("secret_id"))
        return _clip(text), "completed", []
    name = spec["name"]
    if name == "ledger.transfer":
        amount = args.get("amount")
        text = message if amount is None else f"转账 {amount} 元"
        output, status = _write_flow(db, trace, agent, version, space, user, text, role, environment, tool_ids)
        return output, status, []
    result = _call_tool(db, trace, agent, version, space, user, name, message, args, role, environment, tool_ids)
    citations = result if name == "knowledge.search" and isinstance(result, list) else []
    return _clip(result), "completed", citations


def _invoke_tool(db, trace, agent, version, space, user, message, item, role, environment, tool_ids, spec):
    """参数错误只回给模型。抖动和对方拒绝按 0.5 秒、1 秒退避，最多三次。

    返回值最后一项表示这次是否真正成功。失败不记入成功缓存，模型还可以用同样的参数再试。
    """
    from app.services.mcp_client import McpError

    if spec is None:
        return f"参数错误：没有这个工具：{item['name']}。请按工具说明重新输出调用参数。", "completed", [], False
    last_kind = "other"
    last_message = ""
    for attempt in range(_TOOL_ATTEMPTS):
        if attempt:
            _facade()._pause(_TOOL_BACKOFF[attempt - 1])
        try:
            observed, call_status, found = _run_bound(
                db, trace, agent, version, space, user, message, item["arguments"], role, environment, tool_ids, spec
            )
            return observed, call_status, found, call_status == "completed"
        except ToolFault as exc:
            last_kind, last_message = exc.kind, str(exc)
        except (McpError, GateError, ValueError, TypeError, KeyError) as exc:
            last_kind, last_message = _failure_kind(exc), str(exc)
        if last_kind != "transient":
            break
    return _fault_for_model(last_kind, last_message, _TOOL_ATTEMPTS), "completed", [], False


def _call_key(name: str, args: dict) -> str:
    return name + "\n" + json.dumps(args or {}, ensure_ascii=False, sort_keys=True)


def _search_tool(name: str) -> bool:
    folded = (name or "").lower().replace("_", ".")
    return folded == "web.search" or folded.endswith(".web.search")


def _tool_succeeded(text: str) -> bool:
    body = (text or "").lstrip()
    return not body.startswith("参数错误") and not body.startswith("工具暂时失败")


def _successful_calls(messages: list[dict]) -> dict:
    """从已有对话里找出成功过的工具和参数。继续执行时沿用这份记录。"""
    pending = {}
    done = {}
    for item in messages or []:
        if item.get("role") == "assistant":
            for call in item.get("tool_calls") or []:
                function = call.get("function") or {}
                name = function.get("name") or call.get("name") or ""
                raw = function.get("arguments") if "function" in call else call.get("arguments")
                if isinstance(raw, str):
                    try:
                        args = load(raw, {})
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw or {}
                if name and call.get("id"):
                    pending[call["id"]] = (name, args if isinstance(args, dict) else {})
        if item.get("role") != "tool":
            continue
        pair = pending.get(item.get("tool_call_id"))
        content = item.get("content") or ""
        if pair and _tool_succeeded(content):
            done[_call_key(pair[0], pair[1])] = (content, "completed", [])
    return done


def _close_with_answer(db, trace, model, messages, message, observations):
    """工具循环没有留下正文时，再要一次自然语言回答。"""
    follow = list(messages)
    follow.append(
        {
            "role": "user",
            "content": "请根据上面已经返回的工具结果，直接回答最初的问题。不要再调用工具，不要输出工具调用标记。结果不足或互相矛盾时，说明哪些结论无法确认。",
        }
    )
    result = _facade().complete_with_tools(db, model, follow, [])
    shown = result.get("error") or result["text"] or "、".join(item["name"] for item in result["tool_calls"])
    _record_generate(db, trace, model, message, shown, result["usage"], model, fallback=bool(result["fallback"]))
    text = (result["text"] or "").strip()
    if text and not result["fallback"]:
        return text, "completed"
    notes = []
    for item in observations[-4:]:
        piece = item.strip()
        if piece and piece not in notes:
            notes.append(piece[:240])
    detail = "；".join(notes) if notes else "没有拿到工具结果"
    return f"现有工具结果不足以直接回答「{message}」。可以确认的只有这些返回要点：{detail}", "completed"


def _prior_dialogue(db, space, agent, history: list, resume_trace_id: str, policy) -> list[dict]:
    """继续执行时带回整段工具对话。普通追问则带回已经说过的用户和助手内容。"""
    if resume_trace_id:
        previous = db.get(Trace, resume_trace_id)
        if previous is None or previous.space_id != space.id or previous.agent_id != agent.id:
            raise GateError("denied", "找不到可继续的对话")
        stored = load(previous.dialogue_json, [])
        return [item for item in stored if isinstance(item, dict) and item.get("role") != "system"]
    turns = []
    for item in history[-40:]:
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        content, _notes = apply_context_policy(content, policy)
        turns.append({"role": role, "content": content})
    return turns


def _remember_dialogue(trace, messages: list[dict]) -> None:
    trace.dialogue_json = dump([item for item in messages if item.get("role") != "system"])


def _agent_loop(db, trace, agent, version, space, user, message, environment, role, model, tool_ids, prior=None):
    """模型、工具、再模型。没有函数调用且不再点名工具时，这一轮就是最终回答。"""
    specs, bound = _facade()._tool_catalog(db, version, space)
    prompt = _with_skills(db, version, _prompt_for_model(version.system_prompt or ""))
    messages = []
    if prompt:
        messages.append({"role": "system", "content": prompt})
    messages.extend(prior or [])
    if message:
        messages.append({"role": "user", "content": message})
    citations = []
    used_tool = False
    observations = []
    answered = _successful_calls(messages)
    failed = {}
    searches = 0
    capped = False
    repeated = False
    for turn in range(_facade()._LOOP_TURNS):
        result = _facade().complete_with_tools(db, model, messages, specs)
        shown = result.get("error") or result["text"] or "、".join(item["name"] for item in result["tool_calls"])
        _record_generate(db, trace, model, message, shown, result["usage"], model, fallback=bool(result["fallback"]))
        if result["fallback"]:
            if not used_tool:
                return "", "fallback", []
            break
        calls = result["tool_calls"]
        if not calls:
            text = (result["text"] or "").strip()
            if text:
                return text, "completed", citations
            break
        messages.append(
            {
                "role": "assistant",
                "content": result["text"] or "",
                "tool_calls": [
                    {
                        "id": item["id"],
                        "type": "function",
                        "function": {"name": item["name"], "arguments": json.dumps(item["arguments"], ensure_ascii=False)},
                    }
                    for item in calls
                ],
            }
        )
        fresh = 0
        for item in calls:
            key = _call_key(item["name"], item["arguments"])
            searching = _search_tool(item["name"]) or _search_tool((_lookup_tool(bound, item["name"]) or {}).get("name", ""))
            if key in answered:
                observed, call_status, found = answered[key]
                observed = f"{observed}\n相同参数已经成功执行过，不能再次调用。请根据已有结果直接回答。"
            elif key in failed:
                observed, call_status, found = failed[key], "completed", []
                observed = f"{observed}\n相同参数已经失败过，不要再用同样的参数重试。请根据已有结果直接回答。"
            elif searching and searches >= _SEARCH_BUDGET:
                observed, call_status, found = "搜索次数已够。请根据已经返回的网页结果直接回答，不要再搜索。", "completed", []
            else:
                spec = _lookup_tool(bound, item["name"])
                observed, call_status, found, ok = _invoke_tool(
                    db, trace, agent, version, space, user, message, item, role, environment, tool_ids, spec
                )
                fresh += 1
                if ok:
                    answered[key] = (observed, call_status, found)
                    if searching:
                        searches += 1
                else:
                    failed[key] = observed
            used_tool = True
            if found:
                citations = found
            brief = _for_model(observed)
            observations.append(f"{item['name']}：{brief}")
            messages.append({"role": "tool", "tool_call_id": item["id"], "content": brief})
            if call_status != "completed":
                return observed, call_status, citations
        if fresh == 0:
            repeated = True
            break
        if turn == _facade()._LOOP_TURNS - 1:
            capped = True
    if capped and not repeated:
        _remember_dialogue(trace, messages)
        return _LIMIT_TEXT, "limited", citations
    if observations:
        text, status = _close_with_answer(db, trace, model, messages, message, observations)
        return text, status, citations
    return "模型没有返回回答", "failed", citations


def _remember(db, policy, space, agent, user, project_id, masked: str) -> None:
    days = policy.retention_days if policy else 30
    db.add(
        Memory(
            space_id=space.id,
            agent_id=agent.id,
            user_id=user.id,
            project_id=project_id or "",
            kind="short",
            content=masked[:500],
            expires_at=datetime.utcnow() + timedelta(days=max(days, 0)),
        )
    )


_DETAIL = 20000


def _clip(value) -> str:
    text = value if isinstance(value, str) else dump(value)
    return text[:_DETAIL]


def _fill_args(db, tool, space, message: str, args: dict) -> dict:
    """把真正交给工具的参数补全，方便轨迹记下入参。"""
    payload = dict(args or {})
    if tool.name == "calculator" and not payload.get("expression"):
        found = EXPR.search(message or "")
        if found:
            payload["expression"] = found.group(1)
    elif tool.name == "files.list" and not (payload.get("path") or payload.get("folder")):
        found = FOLDER.search(message or "")
        payload["path"] = found.group(1) if found else "."
    elif tool.name == "knowledge.search":
        payload.setdefault("query", message)
        if payload.get("base_ids") is None:
            payload["base_ids"] = ids_for(db, space.id, "knowledge")
    elif tool.name == "ledger.transfer" and payload.get("amount") is None:
        amount = parse_amount(message)
        if amount is not None:
            payload["amount"] = amount
    elif tool.kind == "http" and not payload.get("url") and getattr(tool, "endpoint", ""):
        payload["url"] = tool.endpoint
    return payload


def _tool_records(steps) -> list[dict]:
    records = []
    for step in sorted(steps, key=lambda item: item.idx):
        if step.kind != "execute" or not step.name:
            continue
        records.append(
            {
                "name": step.name,
                "input": step.tool_args or "",
                "output": step.output_text or "",
                "latency_ms": step.latency_ms or 0,
            }
        )
    return records


def _call_tool(db, trace, agent, version, space, user, name, message, args, role, environment, tool_ids):
    """先过工具闸门，再执行。tool_ids 是当前版本开放的编号，不在里面就不能调用。"""
    from app.services.gateway import chat_emit

    chat_emit({"type": "tool", "name": name})
    tool = find_tool(db, space.id, name)
    authorize_tool(db, tool, version_tool_ids=tool_ids, environment=environment, agent_id=agent.id)
    payload = _fill_args(db, tool, space, message, args)
    _hop(db, trace, user.id, agent.workload_id, tool.name, "tool-gateway")
    started = time.perf_counter()
    try:
        result = execute_builtin(db, tool, space, user, message, payload, role)
        rendered = _clip(result)
    except Exception as exc:
        _step(
            db,
            trace,
            "execute",
            name=tool.name,
            tool_args=dump(payload),
            output_text=_clip(str(exc)),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    _step(
        db,
        trace,
        "execute",
        name=tool.name,
        tool_args=dump(payload),
        output_text=rendered,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def _write_flow(db, trace, agent, version, space, user, message, role, environment, tool_ids):
    """账本写入。高风险工具或智能体要求计划时，先挂审批，不直接扣款。"""
    amount = parse_amount(message)
    _step(db, trace, "plan", name="planner", output_text=f"计划调用 ledger.transfer，金额 {amount}")
    tool = find_tool(db, space.id, "ledger.transfer")
    authorize_tool(db, tool, version_tool_ids=tool_ids, environment=environment, agent_id=agent.id)
    if amount is None or amount <= 0:
        _step(db, trace, "review", name="reviewer", output_text="金额不合法，拒绝执行")
        raise ValueError("金额不合法，拒绝执行")
    account = _account(db, space.id)
    if amount > account.balance:
        _step(db, trace, "review", name="reviewer", output_text="余额不足，账本规则拒绝")
        raise ValueError("余额不足，已由账本规则拒绝")
    if tool.risk == "high" or agent.require_plan:
        _step(db, trace, "review", name="reviewer", output_text="高风险写入必须经过审批，禁止跳过")
        approval = Approval(
            space_id=space.id,
            trace_id=trace.id,
            agent_id=agent.id,
            tool_name=tool.name,
            payload_json=dump({"amount": amount, "account": "现金"}),
            status="pending",
            channel="web",
        )
        db.add(approval)
        db.flush()
        return f"已挂起，等待审批 #{approval.id}", "suspended"
    result = execute_builtin(db, tool, space, user, message, {"amount": amount, "account": "现金"}, role)
    rendered = _clip(result)
    _step(db, trace, "execute", name=tool.name, tool_args=dump({"amount": amount, "account": "现金"}), output_text=rendered)
    return rendered, "completed"


def _search_web(db, trace, version, message):
    """版本挂了 MCP 且句子是网上搜索时，调用 web.search。没有服务则交给对话。"""
    from app.models import McpServer, Secret
    from app.services.vault import decrypt
    from app.services.mcp_client import McpError, call_tool

    ids = load(version.mcp_ids_json, [])
    server = (
        db.query(McpServer)
        .filter(McpServer.id.in_(ids), McpServer.enabled.is_(True))
        .order_by(McpServer.id.asc())
        .first()
    )
    if server is None:
        raise GateError("denied", "没有可用的网上搜索服务")
    token = ""
    if server.secret_id:
        secret = db.get(Secret, server.secret_id)
        token = decrypt(secret.ciphertext) if secret else ""
    query = message.strip()[:200] or "搜索"
    try:
        result = call_tool(server.url, token, "web.search", {"query": query, "limit": 3})
    except McpError as exc:
        raise GateError("denied", str(exc)) from exc
    text = (result.get("text") or "").strip() or "没有搜索结果"
    _step(
        db,
        trace,
        "execute",
        name="web.search",
        tool_args=dump({"query": query, "limit": 3}),
        output_text=_clip(text),
        secret_id=server.secret_id,
    )
    return _clip(text)


def _chat(db, trace, version, space, user, message, project_id, policy):
    """对话。版本钉了模型并且该模型启用时只用它；否则按 reason 任务从池子里选。"""
    memories = [
        row.content
        for row in list_memories(
            db, policy, space_id=space.id, user_id=user.id, agent_id=trace.agent_id, project_id=project_id, role_user_id=user.id
        )
        if row.kind == "long"
    ][:3]
    pinned = db.get(ModelEndpoint, version.model_id) if version.model_id else None
    allowed = set(ids_for(db, space.id, "model"))
    model = pinned if pinned and pinned.id in allowed and pinned.enabled and quota_open(pinned) else select_model(db, space.id, "reason")
    text, model_name, fallback, usage = complete(db, model, _with_skills(db, version, version.system_prompt or ""), message, memories, [])
    tokens_in, tokens_out = usage
    cost_in, cost_out, spent = cost_parts(model, tokens_in, tokens_out)
    charge_model(model, spent)
    _step(
        db,
        trace,
        "generate",
        name="reply",
        model=model_name,
        prompt=message,
        output_text=text,
        tokens=tokens_in + tokens_out,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=spent,
        cost_in=cost_in,
        cost_out=cost_out,
        fallback=fallback,
        secret_id=model.secret_id if model else None,
    )
    return text, "completed"


def _delegate(db, trace, version, user, space, message, environment, project_id, role, policy):
    child_ids = load(version.child_ids_json, [])
    if not child_ids:
        raise GateError("denied", "没有可转交的子智能体")
    child = db.get(Agent, child_ids[0])
    if child is None or child.space_id != space.id:
        raise GateError("denied", "子智能体不在本空间")
    _step(db, trace, "delegate", name=child.key, output_text="转交子智能体")
    result = execute_agent(
        db,
        agent=child,
        user=user,
        space=space,
        message=message,
        environment=environment,
        project_id=project_id,
        role=role,
        depth=1,
        credential="delegation",
        policy=policy,
    )
    _hop(db, trace, user.id, child.workload_id, child.key, "delegation")
    return f"子智能体返回：{result['output']}", result["status"]


def _approve_workflow(db, approval, trace, payload: dict, channel: str, actor: str, space_id: int) -> dict:
    """审批通过后才把草案写成工作流。拒绝不会进工作流列表。"""
    from app.models import Workflow
    from app.services.grants import grant

    definition = payload.get("definition") or {}
    nodes = definition.get("nodes") or []
    if not nodes:
        raise GateError("denied", "工作流草案没有节点")
    row = Workflow(
        space_id=space_id,
        origin="department",
        agent_id=int(nodes[0].get("agent_id") or approval.agent_id),
        name=(payload.get("name") or "未命名工作流")[:120],
        definition_json=dump(definition),
        enabled=True,
        created_by=trace.user_id,
    )
    db.add(row)
    db.flush()
    grant(db, space_id, "workflow", row.id)
    approval.status = "approved"
    trace.status = "completed"
    trace.output_text = f"已加入工作流「{row.name}」"
    _step(db, trace, "approval", name=channel, output_text=f"通过并加入工作流 #{row.id}")
    audit(db, space_id, actor, "approval.approve", "工作流", row.name)
    return {"status": trace.status, "output": trace.output_text, "workflow_id": row.id}


def resume_approval(db, approval: Approval, *, decision: str, channel: str, comment: str, actor: str) -> dict:
    trace = db.get(Trace, approval.trace_id)
    agent = db.get(Agent, approval.agent_id)
    space_id = approval.space_id
    approval.channel = channel
    approval.comment = comment
    if decision == "rejected":
        approval.status = "rejected"
        trace.status = "cancelled"
        trace.output_text = "已拒绝。工作流未加入管理列表。" if approval.tool_name == "工作流" else "已拒绝。补偿：账本未变更。"
        _step(db, trace, "approval", name=channel, output_text="拒绝")
        audit(db, space_id, actor, "approval.reject", approval.tool_name, comment)
        return {"status": trace.status, "output": trace.output_text}
    if decision != "approved":
        raise GateError("denied", "未知审批决定")
    payload = load(approval.payload_json, {})
    if approval.tool_name == "工作流":
        return _approve_workflow(db, approval, trace, payload, channel, actor, space_id)
    from app.models import Space, User

    space = db.get(Space, space_id)
    user = db.get(User, trace.user_id)
    tool = find_tool(db, space.id, approval.tool_name)
    result = execute_builtin(db, tool, space, user, "", payload, "operator")
    approval.status = "approved"
    trace.status = "completed"
    rendered = _clip(result)
    trace.output_text = f"【已审批】{rendered}"
    _step(db, trace, "execute", name=tool.name, tool_args=dump(payload), output_text=rendered)
    _step(db, trace, "approval", name=channel, output_text="通过并恢复执行")
    audit(db, space_id, actor, "approval.approve", approval.tool_name, channel)
    return {"status": trace.status, "output": trace.output_text, "result": result}


RUN_CONTROL: dict[int, str] = {}



__all__ = [name for name in globals() if not name.startswith('__')]
