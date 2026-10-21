# 用 Python + FastAPI 开发远程异步智能体

本文说明如何在垣（LIMEN）之外，用 **Python + FastAPI** 实现一个 **`http_async`（远程异步 HTTP）** 智能体：平台提交任务后立刻返回「等待回调」，远端完成推理后再回调平台收尾轨迹。

对照实现：

| 位置 | 作用 |
|---|---|
| `backend/app/services/runtime/remote.py` | 平台侧：下发任务、拼回调 URL/令牌、收尾轨迹 |
| `POST /api/agents/{id}/remote-complete` | 远端完成后的回调入口 |
| `scripts/remote_agent_demo.py` | 标准库 HTTP 演示（含异步路径）；本文给出同等能力的 FastAPI 写法 |

同步 HTTP（`http_sync`）与 A2A 不在本文展开；协议字段与登记方式相同，见主文档 [README.md](../README.md) / [README.en.md](../README.en.md)「智能体」一节。

---

## 1. 何时用异步

| 场景 | 建议协议 |
|---|---|
| 回答很快、可阻塞等结果 | `http_sync` |
| 推理较慢、要排队、要调外部批处理 | **`http_async`** |
| 对接 Agent-to-Agent 生态 | `a2a` |

异步路径下，平台**不会**长时间占用 `/run` 请求；轨迹先进入 `waiting`，等你回调后再变成 `completed` / `failed` 等。

---

## 2. 时序

```
操作者 / 工作室
       │  POST /api/agents/{id}/run
       ▼
    垣平台
       │  1. 建 Trace（running → waiting）
       │  2. POST 你的 remote_endpoint
       │     Body 含 message、trace_id、callback_url、callback_token …
       ▼
  你的 FastAPI 服务
       │  3. 校验 Bearer（平台登记的 remote_token）
       │  4. 立刻 202 + { "accepted": true }
       │  5. 后台跑任务（线程 / 队列 / Celery 均可）
       │  6. POST callback_url
       │     Header: Authorization: Bearer <callback_token>
       │     Body: { trace_id, output, status, tokens?, cost?, error? }
       ▼
    垣平台 remote-complete
       │  7. 校验回调令牌与 trace_id
       │  8. 写步骤、更新 Trace、回填问答消息
       ▼
  轨迹页 / 工作室可见最终结果
```

要点：

- **`callback_token` 不是登录 token**，是平台为这一次轨迹签发的短期 JWT（约 1 小时），类型 `remote_callback`，绑定 `trace_id` 与 `agent_id`。
- **`callback_url` 指向平台**，形如 `{LIMEN_PUBLIC_BASE}/api/agents/{agent_id}/remote-complete`。远端必须能访问到该地址（本机联调时两端都用 `127.0.0.1`，或把 `LIMEN_PUBLIC_BASE` 设成远端可达的主机名）。
- 平台发给你时带的 **`Authorization: Bearer <remote_token>`**，是你在创建远程智能体时填的访问令牌；回调时则用请求体里的 **`callback_token`**，两者不同。

---

## 3. 平台侧：登记远程异步智能体

前提：垣 API 已启动（默认 `http://127.0.0.1:8000`），并设置：

```bash
export LIMEN_PUBLIC_BASE=http://127.0.0.1:8000
```

若远端跑在另一台机器，`LIMEN_PUBLIC_BASE` 必须写成那台机器能访问到的垣地址。

### 3.1 控制台

1. 打开「智能体」→ 新建。
2. 类型选 **远程**。
3. 协议选 **异步 HTTP**（`http_async`）。
4. **远端地址**填你的接收 URL，例如 `http://127.0.0.1:19000/run/async`。
5. **访问令牌**填与远端校验一致的字符串（下文示例用 `demo-remote-token`）。
6. 可选：**健康探测** `GET http://127.0.0.1:19000/health`（总览实时监测需要）。
7. **简介**写给本平台用户看（职责、能力）；不挂本机工具/技能。
8. 保存草稿 → 发布（操作者跑 prod 需要线上版；工作室 `test` 可用最新草稿）。

### 3.2 API 登记示例

```bash
# 登录，拿到 token 与 space id
TOKEN=$(curl -s http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"builder","password":"builder123"}' | jq -r .access_token)
SPACE=1   # 按 /api/me 里实际部门改

curl -s http://127.0.0.1:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Space-Id: $SPACE" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "异步客服远端",
    "key": "remote-async-fastapi",
    "kind": "remote",
    "remote_protocol": "http_async",
    "remote_endpoint": "http://127.0.0.1:19000/run/async",
    "remote_token": "demo-remote-token",
    "remote_timeout_ms": 30000,
    "remote_health_url": "http://127.0.0.1:19000/health",
    "remote_health_method": "GET",
    "system_prompt": "远程异步智能体。平台提交后等待回调；适合稍长推理。"
  }'
```

记下返回的 `id`，发布：

```bash
AGENT_ID=...   # 上一步返回的 id
curl -s -X POST "http://127.0.0.1:8000/api/agents/$AGENT_ID/publish" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Space-Id: $SPACE" \
  -H 'Content-Type: application/json' \
  -d '{"gray_percent": 100}'
```

---

## 4. 协议约定（必须对齐）

### 4.1 平台 → 远端（任务下发）

`POST {remote_endpoint}`

请求头：

| 头 | 说明 |
|---|---|
| `Content-Type` | `application/json` |
| `Authorization` | `Bearer <remote_token>`（创建智能体时配置；未配置则可能无此头） |
| `Accept` | `application/json` |

请求体（平台实际发送字段）：

```json
{
  "message": "用户问题原文",
  "trace_id": "平台轨迹 id（十六进制）",
  "agent_key": "智能体 key",
  "environment": "test | staging | prod",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "mode": "async",
  "callback_url": "http://127.0.0.1:8000/api/agents/16/remote-complete",
  "callback_token": "<短期回调 JWT>"
}
```

你应：

1. 校验 Bearer 与本地配置的 token 一致，否则 `401`。
2. 缺少 `callback_url` / `callback_token` 时返回错误，例如 `400` + `{ "accepted": false, "error": "缺少 callback" }`。
3. **尽快**返回接受结果。HTTP 状态码常用 `202`；JSON 建议：

```json
{ "accepted": true, "message": "已接受，后台处理中" }
```

若 `accepted` 为 `false`（或缺省且业务上拒绝），平台会把这次运行标为失败。超时、4xx/5xx 同样会失败收尾。

`remote_timeout_ms` 只约束**这一次接受请求**的等待时间，不约束你后台跑多久；后台可以超过该时间，只要在回调令牌过期前完成回调。

### 4.2 远端 → 平台（完成回调）

`POST {callback_url}`

请求头：

| 头 | 说明 |
|---|---|
| `Content-Type` | `application/json` |
| `Authorization` | `Bearer <callback_token>`（任务体里带来的那个，不是 remote_token） |

请求体（`RemoteCompleteIn`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `trace_id` | string | 必填；须与令牌内 `tid` 一致 |
| `output` | string | 成功时的正文；失败时可空 |
| `status` | string | `completed` / `failed` / `denied` / `cancelled`（其他值会按失败处理） |
| `tokens` | int | 可选；用量计数，计入部门额度 |
| `cost` | float | 可选；花费（元） |
| `error` | string | 失败原因；`status != completed` 且无 output 时会用作正文 |

成功响应示例：`{ "ok": true, "trace_id": "...", "status": "completed", "output": "..." }`。重复回调同一已完成轨迹会返回 `already: true`，可当作幂等成功。

鉴权失败为 `401`/`403`；令牌与智能体或轨迹不匹配会拒绝。

---

## 5. FastAPI 最小实现

下面是一份可直接落地的服务：接受异步任务、后台线程调用模型（示例用固定回复，可换成任意 LLM）、再回调平台。依赖：`fastapi`、`uvicorn`、`httpx`。

### 5.1 目录与依赖

```bash
mkdir -p remote_async_agent && cd remote_async_agent
python3 -m venv .venv
.venv/bin/pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27' 'httpx>=0.27'
```

### 5.2 `main.py`

```python
"""垣平台远程异步智能体示例（http_async）。"""

from __future__ import annotations

import os
import threading
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

REMOTE_TOKEN = os.environ.get("REMOTE_TOKEN", "demo-remote-token")
# 后台任务模拟耗时（秒）；真实场景删掉 sleep，改为调模型
WORK_SECONDS = float(os.environ.get("WORK_SECONDS", "1.5"))

app = FastAPI(title="LIMEN remote async agent", version="1.0.0")


class AsyncJobIn(BaseModel):
    message: str = ""
    trace_id: str = ""
    agent_key: str = ""
    environment: str = "prod"
    history: list[dict[str, Any]] = Field(default_factory=list)
    mode: str = "async"
    callback_url: str = ""
    callback_token: str = ""


class CompleteOut(BaseModel):
    trace_id: str
    output: str = ""
    status: str = "completed"
    tokens: int = 0
    cost: float = 0
    error: str = ""


def _check_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "unauthorized")
    token = authorization.split(" ", 1)[1].strip()
    if token != REMOTE_TOKEN:
        raise HTTPException(401, "unauthorized")


def _run_inference(message: str) -> dict:
    """替换成你的模型 / 业务逻辑。"""
    text = (message or "").strip() or "（空问题）"
    # 示例：不调外网，直接回显。可改为 OpenAI 兼容 Chat Completions。
    output = f"[fastapi-async] 已处理：{text}"
    tokens = max(1, len(text) // 4 + len(output) // 4)
    return {"output": output, "status": "completed", "tokens": tokens, "cost": 0.0}


def _callback(job: AsyncJobIn) -> None:
    import time

    time.sleep(WORK_SECONDS)  # 演示用；生产环境用队列，不要 sleep 堵线程池
    try:
        result = _run_inference(job.message)
        payload = CompleteOut(
            trace_id=job.trace_id,
            output=result["output"],
            status=result["status"],
            tokens=int(result.get("tokens") or 0),
            cost=float(result.get("cost") or 0),
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        payload = CompleteOut(
            trace_id=job.trace_id,
            status="failed",
            error=str(exc),
        ).model_dump()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {job.callback_token}",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(job.callback_url, headers=headers, json=payload)
        print(f"callback -> {response.status_code} {response.text[:200]}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"callback failed: {exc}", flush=True)


@app.get("/health")
def health():
    return {"ok": True, "protocol": "http_async"}


@app.post("/run/async")
def run_async(body: AsyncJobIn, authorization: str | None = Header(default=None)):
    _check_bearer(authorization)
    if not body.callback_url or not body.callback_token:
        return {"accepted": False, "error": "缺少 callback"}
    if not body.trace_id:
        return {"accepted": False, "error": "缺少 trace_id"}

    threading.Thread(target=_callback, args=(body,), daemon=True).start()
    # 202 表示已接受；平台主要看 JSON 里的 accepted
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=202,
        content={"accepted": True, "message": "已接受，后台处理中"},
    )
```

### 5.3 启动

```bash
export REMOTE_TOKEN=demo-remote-token
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 19000
```

健康检查：`curl -s http://127.0.0.1:19000/health`

---

## 6. 联调步骤

1. 启动垣 API，并设置 `LIMEN_PUBLIC_BASE`（见第 3 节）。
2. 启动上文 FastAPI（`:19000`）。
3. 在控制台或 API 登记 `http_async` 智能体，endpoint 指向 `http://127.0.0.1:19000/run/async`，token 与 `REMOTE_TOKEN` 一致；建议填健康探测。
4. 发布后，在工作室选 **test**，输入问题并执行（远程智能体**不支持**流式输出）。
5. 第一次 `/run` 响应应类似：

```json
{ "status": "waiting", "output": "已提交远程，等待回调", "trace_id": "...", "remote": true, "protocol": "http_async" }
```

6. 约 `WORK_SECONDS` 后，打开「调用轨迹」查看该 `trace_id`，状态应为 `completed`，输出含 `[fastapi-async]`。
7. 总览「智能体实时监控」→ 筛 **远程**，可看健康与窗口调用（需配置健康探测）。

也可用仓库自带演示对照行为：

```bash
# 拉起标准库演示远端并注册三种协议（含异步）
LIMEN_PUBLIC_BASE=http://127.0.0.1:8000 \
  backend/.venv/bin/python scripts/remote_agent_demo.py --serve
```

---

## 7. 接入真实模型（可选）

把 `_run_inference` 换成 OpenAI 兼容接口即可，例如：

```python
def _run_inference(message: str) -> dict:
    import httpx

    api_key = os.environ["OPENAI_API_KEY"]
    base = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    url = f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是垣平台的远程异步智能体，用简洁中文回答。"},
            {"role": "user", "content": message or "你好"},
        ],
        "temperature": 0.3,
    }
    with httpx.Client(timeout=90.0) as client:
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    response.raise_for_status()
    body = response.json()
    text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    usage = body.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    return {
        "output": text or "（模型没有返回文本）",
        "status": "completed",
        "tokens": tin + tout,
        "cost": 0.0,  # 可按单价自行估算后回传
    }
```

`history` 字段可按需拼进 `messages`；平台已给出多轮结构。

---

## 8. 生产注意

| 项 | 建议 |
|---|---|
| 后台执行 | 不要用无限膨胀的线程；改用任务队列（RQ / Celery / arq），worker 内再回调 |
| 回调重试 | 网络失败时按指数退避重试；平台对已完成轨迹幂等 |
| 令牌寿命 | 回调 JWT 约 1 小时；长任务需在过期前完成或自行设计续期（当前协议无续期） |
| 超时 | `remote_timeout_ms` 只覆盖「接受请求」；接受后平台进入 `waiting`，不再等你的 HTTP 连接 |
| 网络 | 远端必须能访问 `LIMEN_PUBLIC_BASE`；容器/云上勿写死仅本机可达的地址给对方 |
| 安全 | `remote_token` 与回调 JWT 分离；勿把回调令牌写进日志全文；HTTPS 部署 |
| 额度 | 回调里的 `tokens` / `cost` 会计入部门用量；请如实回传 |
| 健康探测 | 填 `remote_health_url`，总览才「可实时监测」；探测同样带 Bearer |
| 与本地差异 | 远程不挂本机工具/技能/MCP；简介给人看；工作室不可勾选流式 |

---

## 9. 故障排查

| 现象 | 排查 |
|---|---|
| `/run` 直接 `failed`，提示连不上 | 查 `remote_endpoint`、防火墙、远端是否监听 |
| `/run` 为 `waiting` 后一直不变 | 远端是否回调成功；`LIMEN_PUBLIC_BASE` 是否对远端可达；回调是否误用了 `remote_token` |
| 回调 `401`/`403` | 是否用了任务里的 `callback_token`；令牌是否过期；`agent_id` 路径是否与登记一致 |
| 回调 `轨迹编号不匹配` | Body 的 `trace_id` 必须与下发时一致 |
| 总览「无法实时监测」 | 未配健康探测，属预期；补 `remote_health_url` |
| 接受请求超时 | 增大 `remote_timeout_ms`，并保证 `/run/async` 在超时前返回 202 |

平台实现入口：`backend/app/services/runtime/remote.py` 中 `_call_http_async`、`complete_remote_trace`；路由：`backend/app/api/routers/agents.py` 的 `remote_complete`。

---

## 10. 相关文档与脚本

- 主文档远程智能体总览：[README.md](../README.md) / [README.en.md](../README.en.md)
- 仓库演示（含异步）：`scripts/remote_agent_demo.py`
- 自主调度工作流（本地多智能体）：[autonomous-agent.md](autonomous-agent.md)
