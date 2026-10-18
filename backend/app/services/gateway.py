"""模型选择、工具放行和意图分类。

select_model 在「本空间 + 全局」的启用模型里，先按任务类型过滤，再按单价和优先级取最便宜的。
价格为 0 的启用模型会抢走对应任务。authorize_tool 是调用前的闸门：必须已登记、
已挂到当前智能体版本、已审核、环境匹配，并且没有超过每分钟次数。
http 工具的地址只允许 allow.internal，并拒绝云厂商元数据地址。
classify_intent 是规则分类，不调用模型。关键词决定后面走计算、检索、写入、转交还是对话。
"""

import contextvars
import json
import re
import uuid
from datetime import datetime
from urllib.parse import urlparse

from app.models import ModelEndpoint, RateBucket, Tool
from app.services.grants import ids_for
from app.services.vault import decrypt
from app.util import load


_CHAT_EMIT = contextvars.ContextVar("chat_emit", default=None)


def chat_emit(event: dict) -> None:
    """模型问答的流式出口。没有监听时什么都不做，普通执行不受影响。"""
    sink = _CHAT_EMIT.get()
    if sink:
        sink(event)


class GateError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def quota_open(row: ModelEndpoint) -> bool:
    """额度为 0 表示不限制。用尽后不再被选中。"""
    limit = row.quota_limit or 0
    return limit <= 0 or (row.quota_used or 0) < limit


def charge_model(model: ModelEndpoint | None, cost: float) -> None:
    """把这次调用的费用累加到模型已用额度。"""
    if model is None or cost <= 0:
        return
    model.quota_used = round((model.quota_used or 0) + cost, 6)


def select_model(db, space_id: int, task: str) -> ModelEndpoint | None:
    allowed = ids_for(db, space_id, "model") or [-1]
    rows = (
        db.query(ModelEndpoint)
        .filter(ModelEndpoint.enabled.is_(True), ModelEndpoint.id.in_(allowed))
        .all()
    )
    matched = [row for row in rows if task in load(row.task_types_json, []) and quota_open(row)]
    pool = matched or [row for row in rows if quota_open(row)]
    if not pool:
        return None
    pool.sort(key=lambda row: (row.price_in, row.priority))
    return pool[0]


def rules_reply(system_prompt: str, message: str, memories: list[str], citations: list[dict]) -> str:
    if citations:
        return citations[0]["excerpt"]
    if memories:
        return memories[0]
    line = (system_prompt or "已收到请求").split("。")[0]
    return f"{line}。问题：{message[:80]}"


def complete(db, model: ModelEndpoint | None, system_prompt: str, message: str, memories: list[str], citations: list[dict]):
    fallback = False
    if model is None or model.provider == "rules":
        text = rules_reply(system_prompt, message, memories, citations)
        return text, model.name if model else "rules", fallback, _usage(message, text)
    key = ""
    if model.secret_id:
        from app.models import Secret

        secret = db.get(Secret, model.secret_id)
        if secret:
            key = decrypt(secret.ciphertext)
    if not key:
        fallback = True
        text = rules_reply(system_prompt, message, memories, citations)
        return text, model.name, fallback, _usage(message, text)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=model.base_url or None, timeout=90)
        response = client.chat.completions.create(
            model=model.model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
        )
        text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else max(1, len(message) // 4)
        tokens_out = usage.completion_tokens if usage else max(1, len(text) // 4)
        return text, model.name, False, (tokens_in, tokens_out)
    except Exception:
        fallback = True
        text = rules_reply(system_prompt, message, memories, citations)
        return text, model.name, fallback, _usage(message, text)


def _usage(message: str, text: str) -> tuple[int, int]:
    return max(1, len(message) // 4), max(1, len(text) // 4)


def _model_key(db, model: ModelEndpoint | None) -> str:
    if model is None or not model.secret_id:
        return ""
    from app.models import Secret

    secret = db.get(Secret, model.secret_id)
    return decrypt(secret.ciphertext) if secret else ""


def parse_text_tool_calls(text: str) -> tuple[str, list[dict]]:
    """把写进正文里的函数调用标记拆出来。

    部分模型不会填接口的 tool_calls，而是把调用写成 DSML 标记。
    标记用全角竖线或半角竖线，竖线可能重复。拆出后正文里不再保留这段标记。
    """
    raw = text or ""
    normalized = raw.replace("｜", "|").replace("＜", "<").replace("＞", ">")
    block = re.compile(r"<\|+\s*DSML\s*\|+\s*tool_calls\s*>(.*?)</\|+\s*DSML\s*\|+\s*tool_calls\s*>", re.I | re.S)
    invoke = re.compile(
        r"<\|+\s*DSML\s*\|+\s*invoke\s+name=\"([^\"]+)\"\s*>(.*?)</\|+\s*DSML\s*\|+\s*invoke\s*>",
        re.I | re.S,
    )
    param = re.compile(
        r"<\|+\s*DSML\s*\|+\s*parameter\s+name=\"([^\"]+)\"([^>]*)>(.*?)</\|+\s*DSML\s*\|+\s*parameter\s*>",
        re.I | re.S,
    )
    calls = []
    for region in block.findall(normalized):
        for name, body in invoke.findall(region):
            arguments = {}
            for key, attrs, value in param.findall(body):
                piece = value.strip()
                if re.search(r"string\s*=\s*\"false\"", attrs, re.I):
                    try:
                        arguments[key] = json.loads(piece)
                    except json.JSONDecodeError:
                        arguments[key] = piece
                else:
                    arguments[key] = piece
            calls.append({"id": f"dsml-{uuid.uuid4().hex[:12]}", "name": name.strip(), "arguments": arguments})
    if not calls:
        return raw, []
    cleaned = block.sub("", normalized).strip()
    return cleaned, calls


def _parse_tool_arguments(raw: str):
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {"value": args}
    return args


def _stream_completion(client, payload: dict):
    """把模型正文按片段推给问答页。出现函数调用后不再把正文当回答往外推。"""
    streamed = client.chat.completions.create(**payload, stream=True, stream_options={"include_usage": True})
    parts = []
    slots = {}
    tokens_in = None
    tokens_out = None
    pushed = False
    for chunk in streamed:
        usage = getattr(chunk, "usage", None)
        if usage:
            tokens_in = usage.prompt_tokens
            tokens_out = usage.completion_tokens
        choices = chunk.choices or []
        if not choices:
            continue
        delta = choices[0].delta
        if delta is None:
            continue
        for tool_delta in delta.tool_calls or []:
            slot = slots.setdefault(tool_delta.index, {"id": "", "name": "", "arguments": ""})
            if tool_delta.id:
                slot["id"] = tool_delta.id
            function = tool_delta.function
            if function is None:
                continue
            if function.name:
                slot["name"] += function.name
            if function.arguments:
                slot["arguments"] += function.arguments
        if delta.content:
            parts.append(delta.content)
            if not slots:
                chat_emit({"type": "delta", "text": delta.content})
                pushed = True
    text = "".join(parts)
    calls = [
        {"id": slot["id"] or f"call-{index}", "name": slot["name"], "arguments": _parse_tool_arguments(slot["arguments"])}
        for index, slot in sorted(slots.items())
        if slot["name"]
    ]
    cleaned, embedded = parse_text_tool_calls(text)
    if embedded and not calls:
        calls = embedded
        text = cleaned
    elif not calls:
        text = cleaned
    if calls and pushed:
        chat_emit({"type": "reset"})
    joined = text or ""
    return joined, calls, tokens_in if tokens_in is not None else _usage("", joined)[0], tokens_out if tokens_out is not None else _usage("", joined)[1]


def complete_with_tools(db, model: ModelEndpoint | None, messages: list[dict], tools: list[dict]) -> dict:
    """一轮模型调用。返回文字，或模型要求执行的函数调用。

    没有可用密钥、规则模型或接口失败时 fallback 为真，调用方改走规则路径。
    """
    name = model.name if model else "rules"
    last_user = next((item.get("content") or "" for item in reversed(messages) if item.get("role") == "user"), "")
    key = _model_key(db, model)
    if model is None or model.provider == "rules" or not key:
        text = rules_reply("", last_user, [], [])
        return {"text": text, "tool_calls": [], "model_name": name, "fallback": True, "usage": _usage(last_user, text)}
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=model.base_url or None, timeout=90)
        payload = {"model": model.model, "temperature": 0, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if _CHAT_EMIT.get():
            text, calls, tokens_in, tokens_out = _stream_completion(client, payload)
        else:
            response = client.chat.completions.create(**payload)
            message = response.choices[0].message
            calls = []
            for call in message.tool_calls or []:
                raw = call.function.arguments or "{}"
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {"value": args}
                calls.append({"id": call.id, "name": call.function.name, "arguments": args})
            text, embedded = parse_text_tool_calls(message.content or "")
            if embedded and not calls:
                calls = embedded
            usage = response.usage
            tokens_in = usage.prompt_tokens if usage else _usage(last_user, text)[0]
            tokens_out = usage.completion_tokens if usage else _usage(last_user, text)[1]
        return {"text": text, "tool_calls": calls, "model_name": name, "fallback": False, "usage": (tokens_in, tokens_out)}
    except Exception as exc:
        detail = f"{exc.__class__.__name__}: {exc}"[:500]
        return {"text": "", "tool_calls": [], "model_name": name, "fallback": True, "usage": _usage(last_user, detail), "error": detail}


def cost_parts(model: ModelEndpoint | None, tokens_in: int, tokens_out: int) -> tuple[float, float, float]:
    """单价是每百万 token。返回输入花费、输出花费和合计。"""
    if model is None:
        return 0.0, 0.0, 0.0
    cost_in = round((tokens_in or 0) * (model.price_in or 0) / 1_000_000, 6)
    cost_out = round((tokens_out or 0) * (model.price_out or 0) / 1_000_000, 6)
    return cost_in, cost_out, round(cost_in + cost_out, 6)


def cost_of(model: ModelEndpoint | None, tokens_in: int, tokens_out: int) -> float:
    return cost_parts(model, tokens_in, tokens_out)[2]


def classify_intent(message: str) -> str:
    """规则意图。只看第一行，后面的参考材料不参与分类。顺序是转交、写入、计算、列目录、检索、网上搜索，其余是对话。含「退款」会判成检索。"""
    text = ((message or "").split("\n", 1)[0])
    if "转交" in text or "专员" in text:
        return "delegate"
    if any(word in text for word in ("转账", "付款", "写入", "发送", "删除")):
        return "write"
    if re.search(r"计算|[\d]+\s*[\+\-\*/]\s*[\d]+", text):
        return "calculate"
    if any(word in text for word in ("列出文件", "哪些文件", "文件夹", "目录")):
        return "list"
    if any(word in text for word in ("网上", "网页", "搜索")):
        return "search"
    if any(word in text for word in ("知识", "规定", "退款", "文档")):
        return "retrieve"
    return "chat"


def find_tool(db, space_id: int, name: str) -> Tool | None:
    allowed = ids_for(db, space_id, "tool") or [-1]
    return (
        db.query(Tool)
        .filter(Tool.name == name, Tool.id.in_(allowed))
        .order_by(Tool.space_id.is_(None).asc())
        .first()
    )


def authorize_tool(db, tool: Tool | None, *, version_tool_ids: list[int], environment: str, agent_id: int) -> Tool:
    """调用前的闸门。次数按智能体、工具和当前分钟计数，达到上限返回 blocked。"""
    if tool is None or not tool.enabled:
        raise GateError("denied", "工具未注册")
    if tool.id not in version_tool_ids:
        raise GateError("denied", "当前版本未开放该工具")
    if tool.review_status != "approved":
        raise GateError("denied", "工具尚未通过发布审核")
    if tool.environment not in ("all", environment):
        raise GateError("denied", "工具不适用于当前环境")
    window = datetime.utcnow().replace(second=0, microsecond=0)
    bucket = (
        db.query(RateBucket)
        .filter_by(agent_id=agent_id, tool_id=tool.id, window_start=window)
        .one_or_none()
    )
    if bucket is None:
        bucket = RateBucket(agent_id=agent_id, tool_id=tool.id, window_start=window, count=0)
        db.add(bucket)
        db.flush()
    if bucket.count >= tool.rate_limit_per_min:
        raise GateError("blocked", "工具调用频率已熔断")
    bucket.count += 1
    return tool


def guard_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host in {"169.254.169.254", "metadata.google.internal"} or host.startswith("169.254."):
        raise GateError("denied", "拒绝访问元数据地址")
    if host != "allow.internal":
        raise GateError("denied", "目标不在工具允许域")
    return parsed.path or "/"
