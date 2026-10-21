"""远程智能体调用：同步/异步 HTTP，以及 A2A（JSON-RPC SendMessage 子集）。

本地仍写 Trace；远端协议字段挂在 Agent 上。异步时先返回 waiting，
由远端回调 /api/agents/{id}/remote-complete 收尾。
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urljoin, urlparse

import httpx

from app.models import Agent, ChatMessage, Secret, Trace, TraceStep
from app.security import issue_token, read_token
from app.services.gateway import GateError
from app.services.runtime.support import _step
from app.services.vault import decrypt
from app.util import dump

REMOTE_PROTOCOLS = ("http_sync", "http_async", "a2a")


class RemoteError(Exception):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


def public_base() -> str:
    return (os.environ.get("LIMEN_PUBLIC_BASE") or "http://127.0.0.1:8000").rstrip("/")


def _check_http_url(url: str, *, label: str = "远程地址") -> None:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise GateError("denied", f"{label}只接受 http 或 https")
    if host in {"169.254.169.254", "metadata.google.internal"} or host.startswith("169.254."):
        raise GateError("denied", f"不能连接元数据地址")


def validate_remote_fields(*, protocol: str, endpoint: str) -> None:
    if protocol not in REMOTE_PROTOCOLS:
        raise GateError("denied", "远程协议无效，请使用 http_sync / http_async / a2a")
    _check_http_url(endpoint, label="远程地址")


def validate_health_fields(*, url: str, method: str) -> tuple[str, str]:
    """健康探测可选。有地址时校验方法为 GET/POST。"""
    url = (url or "").strip()
    method = ((method or "GET").strip() or "GET").upper()
    if not url:
        return "", "GET"
    if method not in {"GET", "POST"}:
        raise GateError("denied", "健康探测只支持 GET 或 POST")
    _check_http_url(url, label="健康探测地址")
    return url, method


_HEALTH_CACHE: dict[int, tuple[float, dict]] = {}


def probe_remote_health(db, agent: Agent, *, ttl: float = 8.0) -> dict:
    """探测远程健康。无探测地址时返回 live_monitor=False。结果短时缓存，减轻总览轮询压力。"""
    url = (getattr(agent, "remote_health_url", None) or "").strip()
    if not url:
        return {
            "live_monitor": False,
            "health": "unmonitored",
            "health_ok": None,
            "health_detail": "未配置健康探测",
            "health_latency_ms": 0,
        }
    cached = _HEALTH_CACHE.get(agent.id)
    now = time.time()
    if cached and now - cached[0] < ttl:
        return cached[1]
    method = ((getattr(agent, "remote_health_method", None) or "GET").strip() or "GET").upper()
    if method not in {"GET", "POST"}:
        method = "GET"
    started = time.perf_counter()
    try:
        headers = _auth_headers(db, agent)
        if method == "GET":
            response = httpx.get(url, headers=headers, timeout=3.0, follow_redirects=False)
        else:
            response = httpx.post(url, headers=headers, json={}, timeout=3.0, follow_redirects=False)
        latency = int((time.perf_counter() - started) * 1000)
        ok = 200 <= response.status_code < 400
        result = {
            "live_monitor": True,
            "health": "ok" if ok else "fail",
            "health_ok": ok,
            "health_detail": f"HTTP {response.status_code}",
            "health_latency_ms": latency,
        }
    except httpx.TimeoutException:
        result = {
            "live_monitor": True,
            "health": "fail",
            "health_ok": False,
            "health_detail": "健康探测超时",
            "health_latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except httpx.HTTPError as exc:
        result = {
            "live_monitor": True,
            "health": "fail",
            "health_ok": False,
            "health_detail": f"健康探测失败：{exc}",
            "health_latency_ms": int((time.perf_counter() - started) * 1000),
        }
    _HEALTH_CACHE[agent.id] = (now, result)
    return result


def _auth_headers(db, agent: Agent) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not agent.remote_secret_id:
        return headers
    secret = db.get(Secret, agent.remote_secret_id)
    if secret is None:
        return headers
    token = decrypt(secret.ciphertext)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _timeout(agent: Agent) -> float:
    return max(1.0, (agent.remote_timeout_ms or 30000) / 1000.0)


def _callback_url(agent: Agent) -> str:
    return f"{public_base()}/api/agents/{agent.id}/remote-complete"


def _callback_token(trace_id: str, agent_id: int) -> str:
    return issue_token({"typ": "remote_callback", "tid": trace_id, "aid": agent_id}, ttl=3600)


def execute_remote(
    db,
    *,
    agent: Agent,
    version,
    user,
    space,
    message: str,
    environment: str,
    history: list | None,
    credential: str,
    started: float,
) -> dict:
    """创建轨迹并按协议调用远端。异步协议返回 status=waiting。"""
    validate_remote_fields(protocol=agent.remote_protocol, endpoint=agent.remote_endpoint)
    from app.models import Trace
    from app.services.runtime.support import _hop

    prior = history or []
    trace = Trace(
        id=uuid.uuid4().hex,
        space_id=space.id,
        agent_id=agent.id,
        user_id=user.id,
        version_id=version.id,
        environment=environment,
        status="running",
        input_text=message or "继续上一段对话",
    )
    db.add(trace)
    db.flush()
    _hop(db, trace, user.id, agent.workload_id, agent.key, credential)
    _step(
        db,
        trace,
        "remote",
        name=agent.remote_protocol or "remote",
        prompt=message,
        output_text="正在调用远程智能体",
    )
    try:
        if agent.remote_protocol == "http_sync":
            result = _call_http_sync(db, agent, message, prior, trace.id, environment)
        elif agent.remote_protocol == "http_async":
            result = _call_http_async(db, agent, message, prior, trace.id, environment)
        else:
            result = _call_a2a(db, agent, message, prior, trace.id)
    except RemoteError as exc:
        return _finish_trace(db, space, agent, version, trace, started, output=str(exc), status=exc.status, error=str(exc))
    except httpx.TimeoutException:
        return _finish_trace(db, space, agent, version, trace, started, output="远程智能体超时", status="failed", error="远程智能体超时")
    except httpx.HTTPError as exc:
        return _finish_trace(db, space, agent, version, trace, started, output=f"远程智能体连不上：{exc}", status="failed", error=str(exc))

    if result.get("pending"):
        trace.status = "waiting"
        trace.output_text = result.get("output") or "已提交远程，等待回调"
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        _step(db, trace, "remote", name="async-accepted", output_text=trace.output_text)
        db.flush()
        return {
            "trace_id": trace.id,
            "status": "waiting",
            "output": trace.output_text,
            "citations": [],
            "version": version.number,
            "cost": 0,
            "tokens": 0,
            "tool_calls": [],
            "limited": False,
            "remote": True,
            "protocol": agent.remote_protocol,
        }

    return _finish_trace(
        db,
        space,
        agent,
        version,
        trace,
        started,
        output=result.get("output") or "",
        status=result.get("status") or "completed",
        tokens=int(result.get("tokens") or 0),
        cost=float(result.get("cost") or 0),
        error=result.get("error") or "",
    )


def complete_remote_trace(db, *, agent: Agent, token: str, body) -> dict:
    """异步 HTTP 回调：校验令牌后收尾轨迹，并回填问答消息。"""
    try:
        payload = read_token(token)
    except ValueError as exc:
        raise GateError("denied", "回调令牌无效") from exc
    if payload.get("typ") != "remote_callback" or int(payload.get("aid") or 0) != agent.id:
        raise GateError("denied", "回调令牌与智能体不匹配")
    trace_id = str(payload.get("tid") or body.trace_id or "")
    if not trace_id or (body.trace_id and body.trace_id != trace_id):
        raise GateError("denied", "轨迹编号不匹配")
    trace = db.get(Trace, trace_id)
    if trace is None or trace.agent_id != agent.id:
        raise GateError("denied", "轨迹不存在")
    if trace.status not in {"waiting", "running"}:
        return {"ok": True, "trace_id": trace.id, "status": trace.status, "already": True}
    status = (body.status or "completed").strip() or "completed"
    if status not in {"completed", "failed", "denied", "cancelled"}:
        status = "failed"
    output = body.output or body.error or ""
    if status != "completed" and not output:
        output = body.error or "远程执行失败"
    version_number = 0
    if trace.version_id:
        from app.models import AgentVersion

        version = db.get(AgentVersion, trace.version_id)
        version_number = version.number if version else 0
        if version_number and status == "completed" and output and not output.startswith("【v"):
            output = f"【v{version_number}】{output}"
    tokens = max(0, int(body.tokens or 0))
    cost = round(float(body.cost or 0), 6)
    tokens_in = tokens // 2
    tokens_out = tokens - tokens_in
    if tokens <= 0:
        tokens_in = max(1, len(trace.input_text or "") // 4)
        tokens_out = max(1, len(output) // 4)
        tokens = tokens_in + tokens_out
    _step(
        db,
        trace,
        "remote",
        name="async-complete",
        output_text=output,
        tokens=tokens,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
    )
    trace.status = status
    trace.output_text = output
    trace.error = body.error or ("" if status == "completed" else output)
    trace.tokens_in = tokens_in
    trace.tokens_out = tokens_out
    trace.cost = cost
    from app.models import Space

    space = db.get(Space, trace.space_id)
    if space:
        space.token_used += tokens
    for row in db.query(ChatMessage).filter_by(trace_id=trace.id, role="assistant").all():
        row.content = output
        row.status = status
    db.flush()
    return {"ok": True, "trace_id": trace.id, "status": status, "output": output}


def _finish_trace(db, space, agent, version, trace, started, *, output, status, tokens=0, cost=0.0, error=""):
    if status == "completed" and output and not str(output).startswith("【v"):
        output = f"【v{version.number}】{output}"
    tokens_in = tokens // 2 if tokens else max(1, len(trace.input_text or "") // 4)
    tokens_out = (tokens - tokens_in) if tokens else max(1, len(output or "") // 4)
    if not tokens:
        tokens = tokens_in + tokens_out
    _step(
        db,
        trace,
        "remote",
        name=agent.remote_protocol or "remote",
        output_text=output,
        tokens=tokens,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
    )
    trace.status = status
    trace.output_text = output
    trace.error = error
    trace.latency_ms = int((time.perf_counter() - started) * 1000)
    trace.tokens_in = tokens_in
    trace.tokens_out = tokens_out
    trace.cost = round(float(cost or 0), 6)
    space.token_used += tokens_in + tokens_out
    db.flush()
    steps = db.query(TraceStep).filter(TraceStep.trace_id == trace.id).all()
    return {
        "trace_id": trace.id,
        "status": status,
        "output": output,
        "citations": [],
        "version": version.number,
        "cost": trace.cost,
        "tokens": trace.tokens_in + trace.tokens_out,
        "tool_calls": [],
        "limited": False,
        "remote": True,
        "protocol": agent.remote_protocol,
        "steps": len(steps),
    }


def _call_http_sync(db, agent: Agent, message: str, history: list, trace_id: str, environment: str) -> dict:
    payload = {
        "message": message,
        "trace_id": trace_id,
        "agent_key": agent.key,
        "environment": environment,
        "history": history,
        "mode": "sync",
    }
    response = httpx.post(
        agent.remote_endpoint,
        headers=_auth_headers(db, agent),
        json=payload,
        timeout=_timeout(agent),
        follow_redirects=False,
    )
    if response.status_code >= 400:
        raise RemoteError(f"远程返回 {response.status_code}: {(response.text or '')[:200]}")
    body = response.json() if response.content else {}
    if not isinstance(body, dict):
        raise RemoteError("远程返回的不是 JSON 对象")
    output = body.get("output") or body.get("text") or body.get("result") or ""
    if isinstance(output, dict):
        output = dump(output)
    status = body.get("status") or "completed"
    if status not in {"completed", "failed", "denied", "cancelled"}:
        status = "completed"
    return {
        "output": str(output),
        "status": status,
        "tokens": int(body.get("tokens") or 0),
        "cost": float(body.get("cost") or 0),
        "error": body.get("error") or "",
    }


def _call_http_async(db, agent: Agent, message: str, history: list, trace_id: str, environment: str) -> dict:
    callback_token = _callback_token(trace_id, agent.id)
    payload = {
        "message": message,
        "trace_id": trace_id,
        "agent_key": agent.key,
        "environment": environment,
        "history": history,
        "mode": "async",
        "callback_url": _callback_url(agent),
        "callback_token": callback_token,
    }
    response = httpx.post(
        agent.remote_endpoint,
        headers=_auth_headers(db, agent),
        json=payload,
        timeout=_timeout(agent),
        follow_redirects=False,
    )
    if response.status_code >= 400:
        raise RemoteError(f"远程返回 {response.status_code}: {(response.text or '')[:200]}")
    body = response.json() if response.content else {}
    accepted = True if not isinstance(body, dict) else body.get("accepted", True)
    if not accepted:
        raise RemoteError(str(body.get("error") or "远程拒绝异步任务"))
    return {
        "pending": True,
        "output": body.get("message") if isinstance(body, dict) else "已提交远程，等待回调",
    }


def _resolve_a2a_url(db, agent: Agent) -> str:
    endpoint = (agent.remote_endpoint or "").rstrip("/")
    if endpoint.endswith(("/a2a", "/rpc", ":send")):
        return endpoint
    card_url = urljoin(endpoint + "/", ".well-known/agent-card.json")
    try:
        response = httpx.get(card_url, headers=_auth_headers(db, agent), timeout=min(8.0, _timeout(agent)), follow_redirects=False)
        if response.status_code < 400:
            card = response.json()
            if isinstance(card, dict):
                for key in ("url", "rpcUrl", "endpoint"):
                    if card.get(key):
                        return str(card[key])
                interfaces = card.get("supportedInterfaces") or card.get("interfaces") or []
                if isinstance(interfaces, list):
                    for item in interfaces:
                        if isinstance(item, dict) and item.get("url"):
                            return str(item["url"])
    except Exception:
        pass
    return f"{endpoint}/a2a"


def _call_a2a(db, agent: Agent, message: str, history: list, trace_id: str) -> dict:
    url = _resolve_a2a_url(db, agent)
    params = {
        "message": {
            "messageId": uuid.uuid4().hex,
            "role": "user",
            "parts": [{"kind": "text", "text": message}],
            "metadata": {"trace_id": trace_id, "agent_key": agent.key, "history": history},
        }
    }
    payload = {"jsonrpc": "2.0", "id": trace_id, "method": "SendMessage", "params": params}
    headers = _auth_headers(db, agent)
    headers["A2A-Version"] = "0.3"
    response = httpx.post(url, headers=headers, json=payload, timeout=_timeout(agent), follow_redirects=False)
    if response.status_code >= 400:
        raise RemoteError(f"A2A 返回 {response.status_code}: {(response.text or '')[:200]}")
    body = response.json() if response.content else {}
    if not isinstance(body, dict):
        raise RemoteError("A2A 返回的不是 JSON")
    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RemoteError(msg or "A2A 调用失败")
    result = body.get("result") or {}
    text = _a2a_text(result)
    if not text:
        raise RemoteError("A2A 没有返回文本")
    return {"output": text, "status": "completed"}


def _a2a_text(result: dict) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    message = result.get("message")
    status_obj = result.get("status")
    if message is None and isinstance(status_obj, dict):
        message = status_obj.get("message")
    if isinstance(result.get("task"), dict):
        task = result["task"]
        task_status = task.get("status") or {}
        message = task_status.get("message") or message
        artifacts = task.get("artifacts") or []
        if artifacts and isinstance(artifacts[0], dict):
            text = _parts_text(artifacts[0].get("parts") or [])
            if text:
                return text
    if isinstance(message, dict):
        return _parts_text(message.get("parts") or [])
    if isinstance(result.get("parts"), list):
        return _parts_text(result["parts"])
    return str(result.get("output") or result.get("text") or "")


def _parts_text(parts) -> str:
    chunks = []
    for part in parts or []:
        if isinstance(part, dict):
            text = part.get("text") or part.get("content") or ""
            if text:
                chunks.append(str(text))
        elif part:
            chunks.append(str(part))
    return "\n".join(chunks)
