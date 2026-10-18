"""MCP Streamable HTTP 客户端，协议版本 2025-03-26。

连接顺序是 initialize、notifications/initialized、tools/list 或 tools/call。
initialized 通知失败不影响后续调用。只允许 http/https，并拒绝元数据地址。
超时 8 秒，不跟随重定向。令牌放在 Authorization，不写入日志。
"""

import json
from urllib.parse import urlparse

import httpx


class McpError(Exception):
    """kind 为 argument（参数不对）、transient（抖动或对方拒绝）或 other。"""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


def connect(url: str, token: str = "") -> dict:
    _check_url(url)
    result, session = _rpc(
        url,
        token,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "limen", "version": "1.0"},
        },
    )
    try:
        _rpc(url, token, "notifications/initialized", session_id=session, notify=True)
    except McpError:
        pass
    listed, _session = _rpc(url, token, "tools/list", {}, session_id=session)
    tools = [_tool_brief(item) for item in listed.get("tools") or []]
    server = result.get("serverInfo") or {}
    return {
        "ok": True,
        "protocol": result.get("protocolVersion") or "",
        "server": server.get("name") or "",
        "tools": tools,
    }


def call_tool(url: str, token: str, name: str, arguments: dict | None) -> dict:
    _check_url(url)
    _result, session = _rpc(
        url,
        token,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "limen", "version": "1.0"},
        },
    )
    try:
        _rpc(url, token, "notifications/initialized", session_id=session, notify=True)
    except McpError:
        pass
    result, _session = _rpc(
        url,
        token,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        session_id=session,
    )
    content = result.get("content") or []
    text = "\n".join(str(item.get("text") or item) for item in content) if isinstance(content, list) else str(content)
    return {"ok": not result.get("isError"), "name": name, "text": text, "raw": result}


def _tool_brief(item: dict) -> dict:
    return {
        "name": item.get("name") or "",
        "description": item.get("description") or "",
        "input_schema": item.get("inputSchema") or {"type": "object"},
    }


def _check_url(url: str) -> None:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise McpError("只接受 http 或 https 地址")
    if host in {"169.254.169.254", "metadata.google.internal"} or host.startswith("169.254."):
        raise McpError("不能连接元数据地址")


def _rpc(url: str, token: str, method: str, params: dict | None = None, session_id: str | None = None, notify: bool = False):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = {"initialize": 1, "tools/list": 2, "tools/call": 3}.get(method, 4)
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=8.0, follow_redirects=False)
    except httpx.TimeoutException as exc:
        raise McpError("MCP 服务超时", "transient") from exc
    except httpx.HTTPError as exc:
        raise McpError("MCP 服务连不上", "transient") from exc
    session = response.headers.get("mcp-session-id") or session_id
    if notify:
        if response.status_code >= 400:
            raise McpError(f"MCP 返回 {response.status_code}", _status_kind(response.status_code))
        return {}, session
    if response.status_code >= 400:
        raise McpError(f"MCP 返回 {response.status_code}", _status_kind(response.status_code))
    body = _parse_body(response.text, response.headers.get("content-type", ""))
    if isinstance(body, dict) and body.get("error"):
        error = body["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        raise McpError(message or "MCP 调用失败", _rpc_kind(code, message or ""))
    return (body.get("result") if isinstance(body, dict) else {}) or {}, session


def _status_kind(code: int) -> str:
    """400 是参数问题。401、403、429 和 5xx 视为对方拒绝或暂时不可用。"""
    if code == 400:
        return "argument"
    if code in {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}:
        return "transient"
    return "other"


def _rpc_kind(code, message: str) -> str:
    if code == -32602 or any(word in message for word in ("参数", "invalid", "required", "missing")):
        return "argument"
    if code in {-32000, -32001} or any(word in message.lower() for word in ("timeout", "unavailable", "拒绝")):
        return "transient"
    return "other"


def _parse_body(text: str, content_type: str):
    raw = text or ""
    if "text/event-stream" in content_type or raw.lstrip().startswith(("event:", "data:")):
        data = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                piece = line[5:].strip()
                if piece and piece != "[DONE]":
                    data = json.loads(piece)
        if data is None:
            raise McpError("MCP 没有返回数据", "transient")
        return data
    if not raw.strip():
        raise McpError("MCP 没有返回数据", "transient")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpError("MCP 返回的不是 JSON", "transient") from exc
