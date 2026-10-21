#!/usr/bin/env python3
"""远程智能体演示：同步 HTTP / 异步 HTTP / A2A，推理走本库 deepseek 配置。

用法（先启动本机 API :8000，并设置 LIMEN_PUBLIC_BASE）：

  # 注册三种远程智能体、跑通一次后退出
  backend/.venv/bin/python scripts/remote_agent_demo.py

  # 只拉起远端服务并保持运行（供控制台试运行）
  backend/.venv/bin/python scripts/remote_agent_demo.py --serve

环境变量：
  LIMEN_BASE / LIMEN_PUBLIC_BASE / REMOTE_PORT / LIMEN_USER / LIMEN_PASSWORD
  REMOTE_TOKEN          远端 Bearer，默认 demo-remote-token
  DEEPSEEK_MODEL_NAME   优先匹配的模型名，默认 deepseek_pro
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

LIMEN_BASE = (os.environ.get("LIMEN_BASE") or "http://127.0.0.1:8000").rstrip("/")
PUBLIC_BASE = (os.environ.get("LIMEN_PUBLIC_BASE") or LIMEN_BASE).rstrip("/")
REMOTE_PORT = int(os.environ.get("REMOTE_PORT") or "18765")
REMOTE_BASE = f"http://127.0.0.1:{REMOTE_PORT}"
USER = os.environ.get("LIMEN_USER") or "builder"
PASSWORD = os.environ.get("LIMEN_PASSWORD") or "builder123"
TOKEN = os.environ.get("REMOTE_TOKEN") or "demo-remote-token"
MODEL_PREF = os.environ.get("DEEPSEEK_MODEL_NAME") or "deepseek_pro"

REMOTE_SYSTEM = (
    "你是垣平台接入的远程智能体。用简洁中文回答用户问题。"
    "若问题涉及计算，直接给出结果；不确定时说明限制。"
)

AGENT_SPECS = [
    {
        "key": "remote-http-sync",
        "name": "远程-同步HTTP",
        "protocol": "http_sync",
        "endpoint": lambda: f"{REMOTE_BASE}/run/sync",
        "intro": (
            "同步 HTTP 远程智能体。平台把问题 POST 到远端后阻塞等待回答。"
            "后端用本库 deepseek 配置推理；适合短问答、联调与即时试运行。"
            "能力：自然语言回答、简单计算说明。无本机工具/技能，依赖远端模型。"
        ),
    },
    {
        "key": "remote-http-async",
        "name": "远程-异步HTTP",
        "protocol": "http_async",
        "endpoint": lambda: f"{REMOTE_BASE}/run/async",
        "intro": (
            "异步 HTTP 远程智能体。平台提交任务后先返回「等待回调」，"
            "远端用 deepseek 生成结果再回调完成轨迹。适合稍长推理或模拟慢任务。"
            "能力同同步版；总览可看健康探测与窗口调用。"
        ),
    },
    {
        "key": "remote-a2a",
        "name": "远程-A2A",
        "protocol": "a2a",
        "endpoint": lambda: REMOTE_BASE,
        "intro": (
            "A2A 协议远程智能体。平台读取 Agent Card 后以 JSON-RPC SendMessage 调用。"
            "远端同样使用本库 deepseek 配置生成文本回复。适合对接标准 Agent-to-Agent 生态。"
            "能力：文本对话；声明技能 echo（实际由大模型作答）。"
        ),
    },
]

_MODEL = {"name": "", "model": "", "base_url": "", "api_key": ""}


def load_deepseek_config() -> dict:
    """从 backend/data/app.db 读取 deepseek 接入点与密钥。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import ModelEndpoint, Secret
    from app.services.vault import decrypt

    db_path = BACKEND / "data" / "app.db"
    if not db_path.exists():
        raise RuntimeError(f"找不到数据库 {db_path}")
    engine = create_engine(f"sqlite:///{db_path}")
    db = sessionmaker(bind=engine)()
    try:
        rows = db.query(ModelEndpoint).filter(ModelEndpoint.enabled.is_(True)).all()
        preferred = None
        for row in rows:
            if row.name == MODEL_PREF:
                preferred = row
                break
        if preferred is None:
            for row in rows:
                blob = f"{row.name} {row.model} {row.provider}".lower()
                if "deepseek" in blob:
                    preferred = row
                    break
        if preferred is None:
            raise RuntimeError("库中没有 deepseek 模型，请先在「模型管理」中配置")
        if not preferred.secret_id:
            raise RuntimeError(f"模型 {preferred.name} 没有 API Key")
        secret = db.get(Secret, preferred.secret_id)
        if secret is None:
            raise RuntimeError(f"模型 {preferred.name} 的密钥不存在")
        api_key = decrypt(secret.ciphertext)
        if not api_key.strip():
            raise RuntimeError(f"模型 {preferred.name} 的密钥为空")
        return {
            "name": preferred.name,
            "model": preferred.model,
            "base_url": (preferred.base_url or "https://api.deepseek.com").rstrip("/"),
            "api_key": api_key.strip(),
            "price_in": preferred.price_in or 0,
            "price_out": preferred.price_out or 0,
        }
    finally:
        db.close()


def call_deepseek(message: str, *, protocol: str) -> dict:
    """OpenAI 兼容 Chat Completions。返回 output / tokens / cost。"""
    import httpx

    base = _MODEL["base_url"]
    if not base.endswith("/v1"):
        url = f"{base}/v1/chat/completions"
    else:
        url = f"{base}/chat/completions"
    payload = {
        "model": _MODEL["model"],
        "messages": [
            {"role": "system", "content": REMOTE_SYSTEM},
            {"role": "user", "content": message or "你好"},
        ],
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {_MODEL['api_key']}", "Content-Type": "application/json"}
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"deepseek 返回 {response.status_code}: {(response.text or '')[:240]}")
    body = response.json()
    choice = (body.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if not text:
        text = "（模型没有返回文本）"
    usage = body.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    tokens = tin + tout
    cost = round((tin * float(_MODEL.get("price_in") or 0) + tout * float(_MODEL.get("price_out") or 0)) / 1_000_000, 6)
    return {
        "output": f"[{protocol} · {_MODEL['name']}] {text}",
        "status": "completed",
        "tokens": tokens or max(1, len(message or "") // 4 + len(text) // 4),
        "cost": cost,
    }


def _json_response(handler: BaseHTTPRequestHandler, code: int, body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    return json.loads(raw.decode())


def _check_auth(handler: BaseHTTPRequestHandler) -> bool:
    header = handler.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return False
    return header.split(" ", 1)[1].strip() == TOKEN


def _async_callback(callback_url: str, callback_token: str, trace_id: str, message: str) -> None:
    try:
        result = call_deepseek(message, protocol="http_async")
        payload = {
            "trace_id": trace_id,
            "output": result["output"],
            "status": "completed",
            "tokens": result["tokens"],
            "cost": result["cost"],
        }
    except Exception as exc:
        payload = {
            "trace_id": trace_id,
            "output": "",
            "status": "failed",
            "error": str(exc),
        }
    req = Request(
        callback_url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {callback_token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            print(f"  async callback -> {resp.status}", flush=True)
    except Exception as exc:
        print(f"  async callback failed: {exc}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: N802
        print(f"  remote {self.command} {self.path} " + (fmt % args), flush=True)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/.well-known/agent-card.json":
            _json_response(
                self,
                200,
                {
                    "name": "LIMEN DeepSeek Remote",
                    "description": "使用垣库内 deepseek 配置的远程智能体，支持同步/异步 HTTP 与 A2A。",
                    "version": "1.0.0",
                    "url": f"{REMOTE_BASE}/a2a",
                    "capabilities": {"streaming": False, "pushNotifications": True},
                    "defaultInputModes": ["text"],
                    "defaultOutputModes": ["text"],
                    "skills": [
                        {
                            "id": "chat",
                            "name": "Chat",
                            "description": f"通过 {_MODEL.get('name') or 'deepseek'} 回答问题",
                            "tags": ["demo", "deepseek"],
                            "examples": ["用一句话介绍你自己"],
                        }
                    ],
                },
            )
            return
        if self.path.rstrip("/") == "/health":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "model": _MODEL.get("name") or "",
                    "model_id": _MODEL.get("model") or "",
                },
            )
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not _check_auth(self):
            _json_response(self, 401, {"error": "unauthorized"})
            return
        path = self.path.rstrip("/") or "/"
        body = _read_json(self)
        if path == "/run/sync":
            message = str(body.get("message") or "")
            try:
                result = call_deepseek(message, protocol="http_sync")
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 502, {"output": str(exc), "status": "failed", "error": str(exc)})
            return
        if path == "/run/async":
            message = str(body.get("message") or "")
            callback_url = body.get("callback_url") or ""
            callback_token = body.get("callback_token") or ""
            trace_id = body.get("trace_id") or ""
            if not callback_url or not callback_token:
                _json_response(self, 400, {"accepted": False, "error": "缺少 callback"})
                return
            threading.Thread(
                target=_async_callback,
                args=(callback_url, callback_token, trace_id, message),
                daemon=True,
            ).start()
            _json_response(self, 202, {"accepted": True, "message": "已接受，正在调用 deepseek"})
            return
        if path == "/a2a":
            if body.get("method") not in {"SendMessage", "message/send"}:
                _json_response(
                    self,
                    200,
                    {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "Method not found"}},
                )
                return
            params = body.get("params") or {}
            message = params.get("message") or {}
            parts = message.get("parts") or []
            text = ""
            for part in parts:
                if isinstance(part, dict) and part.get("text"):
                    text = str(part["text"])
                    break
            try:
                result = call_deepseek(text, protocol="a2a")
                reply = result["output"]
            except Exception as exc:
                _json_response(
                    self,
                    200,
                    {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": str(exc)}},
                )
                return
            _json_response(
                self,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "message": {
                            "messageId": uuid.uuid4().hex,
                            "role": "agent",
                            "parts": [{"kind": "text", "text": reply}],
                        }
                    },
                },
            )
            return
        _json_response(self, 404, {"error": "not found"})


def start_remote() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", REMOTE_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def api(method: str, path: str, token: str = "", space_id: int | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if space_id is not None:
        headers["X-Space-Id"] = str(space_id)
    data = None if body is None else json.dumps(body).encode()
    req = Request(f"{LIMEN_BASE}{path}", data=data, headers=headers, method=method)
    with urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def login() -> tuple[str, int]:
    data = api("POST", "/api/auth/login", body={"username": USER, "password": PASSWORD})
    token = data["access_token"]
    me = api("GET", "/api/me", token=token)
    spaces = me.get("spaces") or []
    if not spaces:
        raise RuntimeError("登录成功但没有部门")
    return token, spaces[0]["id"]


def ensure_agent(token: str, space_id: int, spec: dict) -> dict:
    rows = api("GET", "/api/agents?page=1&page_size=100", token=token, space_id=space_id)
    items = rows.get("items") if isinstance(rows, dict) else rows
    found = next((item for item in items or [] if item.get("key") == spec["key"]), None)
    payload = {
        "name": spec["name"],
        "key": spec["key"],
        "kind": "remote",
        "system_prompt": spec["intro"],
        "remote_protocol": spec["protocol"],
        "remote_endpoint": spec["endpoint"](),
        "remote_token": TOKEN,
        "remote_timeout_ms": 90000,
        "remote_health_url": f"{REMOTE_BASE}/health",
        "remote_health_method": "GET",
    }
    if found:
        agent = api("PATCH", f"/api/agents/{found['id']}", token=token, space_id=space_id, body=payload)
    else:
        agent = api("POST", "/api/agents", token=token, space_id=space_id, body=payload)
    if agent.get("status") != "published":
        agent = api(
            "POST",
            f"/api/agents/{agent['id']}/publish",
            token=token,
            space_id=space_id,
            body={"gray_percent": 100},
        )
    return agent


def wait_trace(token: str, space_id: int, trace_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = api("GET", f"/api/traces/{trace_id}", token=token, space_id=space_id)
        if row.get("status") not in {"waiting", "running"}:
            return row
        time.sleep(0.5)
    return api("GET", f"/api/traces/{trace_id}", token=token, space_id=space_id)


def run_case(token: str, space_id: int, agent: dict, label: str) -> None:
    print(f"\n== {label} ==")
    result = api(
        "POST",
        f"/api/agents/{agent['id']}/run",
        token=token,
        space_id=space_id,
        body={"message": f"用一句话介绍你自己，并说明协议是{label}", "environment": "test"},
    )
    print(f"  immediate status={result.get('status')} output={((result.get('output') or '')[:160])!r}")
    if result.get("status") == "waiting":
        final = wait_trace(token, space_id, result["trace_id"])
        text = final.get("output_text") or final.get("output") or ""
        print(f"  final status={final.get('status')} output={text[:160]!r}")
        if final.get("status") != "completed":
            raise SystemExit(f"{label} 异步未完成")
    elif result.get("status") != "completed":
        raise SystemExit(f"{label} 失败: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description="远程智能体 + deepseek 演示")
    parser.add_argument("--serve", action="store_true", help="只启动远端并保持运行")
    parser.add_argument("--skip-run", action="store_true", help="只注册三种智能体，不试运行")
    args = parser.parse_args()

    os.environ.setdefault("LIMEN_PUBLIC_BASE", PUBLIC_BASE)
    global _MODEL
    try:
        _MODEL = load_deepseek_config()
    except Exception as exc:
        print(f"读取 deepseek 配置失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"LIMEN={LIMEN_BASE} PUBLIC={PUBLIC_BASE} REMOTE={REMOTE_BASE}\n"
        f"model={_MODEL['name']} ({_MODEL['model']}) base={_MODEL['base_url']}"
    )

    # 释放旧端口占用
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        busy = sock.connect_ex(("127.0.0.1", REMOTE_PORT)) == 0
        sock.close()
        if busy:
            print(f"端口 {REMOTE_PORT} 已被占用，请改 REMOTE_PORT 或结束旧进程", file=sys.stderr)
            return 1
    except Exception:
        pass

    server = start_remote()
    time.sleep(0.2)
    try:
        with urlopen(f"{REMOTE_BASE}/health", timeout=3) as resp:
            assert resp.status == 200
    except Exception as exc:
        print(f"远端未起来: {exc}", file=sys.stderr)
        return 1

    if args.serve:
        print("远端已启动，Ctrl+C 结束。可在控制台对三种远程智能体试运行。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n已停止")
        finally:
            server.shutdown()
        return 0

    try:
        token, space_id = login()
    except Exception as exc:
        print(f"登录失败，请先启动 API: {exc}", file=sys.stderr)
        server.shutdown()
        return 1

    print("提示：异步流程要求 API 进程设置 LIMEN_PUBLIC_BASE=" + PUBLIC_BASE)
    agents = []
    for spec in AGENT_SPECS:
        agent = ensure_agent(token, space_id, spec)
        agents.append((agent, spec["name"]))
        print(f"  已登记 {spec['name']} ({spec['protocol']}) id={agent['id']}")

    if not args.skip_run:
        for agent, label in agents:
            run_case(token, space_id, agent, label)
        print("\n三种远程流程均已走通（deepseek）。")
    else:
        print("\n已写入简介并发布，跳过试运行。")
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
