"""模型问答会话。

会话属于智能体所在部门。普通成员只能看自己在当前部门的会话，部门管理员可以看本部门全部会话。
平台管理员可以选用任意部门的智能体，会话记在该智能体的部门，并在那个部门的额度和模型授权下执行。
发起和继续问答需要操作者及以上。只读账号可以查阅和搜索，不能发送。
构建者和管理员使用最新草稿，操作者使用已发布版本。
"""

import queue
import threading
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import ChatMessageIn, ChatSessionIn, ChatSessionPatch
from app.deps import get_context, get_db, require
from app.models import Agent, AgentVersion, Approval, ChatMessage, ChatSession, ContextPolicy, ModelEndpoint, Space, Trace, User
from app.services.gateway import _CHAT_EMIT, charge_model, complete_with_tools, cost_parts
from app.paging import stamp
from app.services.gateway import GateError
from app.services.grants import ids_for, is_platform
from app.services.runtime import execute_agent
from app.services.runtime.support import pick_version
from app.util import dump, load

router = APIRouter()


def _sees_all(ctx) -> bool:
    return is_platform(ctx.user) or ctx.role == "admin"


def _agent_in_space(db, ctx, agent_id: int) -> Agent:
    """问答可以使用当前部门的智能体。平台管理员可以选任意部门的智能体，并在该部门里执行。"""
    agent = db.get(Agent, agent_id)
    if agent is None or (agent.space_id != ctx.space.id and not is_platform(ctx.user)):
        raise HTTPException(404, "智能体不存在")
    return agent


def _sessions(db, ctx):
    query = db.query(ChatSession)
    if not is_platform(ctx.user):
        query = query.filter(ChatSession.space_id == ctx.space.id)
        if not _sees_all(ctx):
            query = query.filter(ChatSession.user_id == ctx.user.id)
    return query


def _load_session(db, ctx, session_id: str) -> ChatSession:
    row = db.get(ChatSession, session_id)
    if row is None or (row.space_id != ctx.space.id and not is_platform(ctx.user)):
        raise HTTPException(404, "会话不存在")
    if row.user_id != ctx.user.id and not _sees_all(ctx):
        raise HTTPException(404, "会话不存在")
    return row


def _brief(db, row: ChatSession, snippet: str = "", viewer_id: int | None = None) -> dict:
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    owner = db.get(User, row.user_id)
    return {
        "id": row.id,
        "title": row.title,
        "agent_id": row.agent_id,
        "agent_name": agent.name if agent else "",
        "owner": (owner.display_name or owner.username) if owner else "",
        "mine": viewer_id is None or row.user_id == viewer_id,
        "pinned": bool(row.pinned),
        "snippet": snippet,
        "created_at": stamp(row.created_at),
        "updated_at": stamp(row.updated_at),
    }


def _message_dict(row: ChatMessage) -> dict:
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "trace_id": row.trace_id,
        "tool_calls": load(row.tool_calls_json, []),
        "status": row.status,
        "limited": row.limited,
        "created_at": stamp(row.created_at),
    }


@router.get("/api/chats")
def list_chats(ctx=Depends(get_context), db=Depends(get_db), q: str = ""):
    """按标题或用户问题模糊查找当前空间里可见的会话。"""
    require(ctx, "viewer")
    needle = q.strip()
    rows = _sessions(db, ctx).order_by(ChatSession.pinned.desc(), ChatSession.updated_at.desc(), ChatSession.id.desc()).all()
    found = []
    for row in rows:
        snippet = ""
        if needle:
            hit = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == row.id, ChatMessage.role == "user", ChatMessage.content.contains(needle))
                .order_by(ChatMessage.id.desc())
                .first()
            )
            if needle not in (row.title or "") and hit is None:
                continue
            snippet = hit.content if hit else ""
        found.append(_brief(db, row, snippet[:80], ctx.user.id))
    return found


@router.post("/api/chats")
def create_chat(body: ChatSessionIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    agent = _agent_in_space(db, ctx, body.agent_id)
    now = datetime.utcnow()
    row = ChatSession(
        id=uuid.uuid4().hex,
        space_id=agent.space_id or ctx.space.id,
        user_id=ctx.user.id,
        agent_id=agent.id,
        title=(body.title or "新的问答")[:120],
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return _brief(db, row, viewer_id=ctx.user.id)


@router.get("/api/chats/{session_id}")
def get_chat(session_id: str, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "viewer")
    row = _load_session(db, ctx, session_id)
    messages = db.query(ChatMessage).filter_by(session_id=row.id).order_by(ChatMessage.id.asc()).all()
    data = _brief(db, row, viewer_id=ctx.user.id)
    data["messages"] = [_message_dict(item) for item in messages]
    return data


@router.patch("/api/chats/{session_id}")
def patch_chat(session_id: str, body: ChatSessionPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = _load_session(db, ctx, session_id)
    if row.user_id != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "只能修改自己的会话")
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "标题不能为空")
        row.title = title[:120]
    if body.agent_id is not None:
        row.agent_id = _agent_in_space(db, ctx, body.agent_id).id
    if body.pinned is not None:
        row.pinned = body.pinned
    row.updated_at = datetime.utcnow()
    return _brief(db, row, viewer_id=ctx.user.id)


@router.delete("/api/chats/{session_id}")
def delete_chat(session_id: str, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "operator")
    row = _load_session(db, ctx, session_id)
    if row.user_id != ctx.user.id and ctx.role != "admin" and not is_platform(ctx.user):
        raise HTTPException(403, "只能删除自己的会话")
    db.query(ChatMessage).filter_by(session_id=row.id).delete()
    db.delete(row)
    return {"ok": True}


def _turn(db, ctx, row: ChatSession, body: ChatMessageIn) -> tuple[str, str, list, dict]:
    """校验这一问，并算出要送给智能体的历史。真正执行由调用方完成。"""
    if row.user_id != ctx.user.id:
        raise HTTPException(403, "只能在自己的会话里提问")
    if row.agent_id is None:
        raise HTTPException(400, "请先选择智能体")
    text = body.message.strip()
    prior = db.query(ChatMessage).filter_by(session_id=row.id).order_by(ChatMessage.id.asc()).all()
    last = prior[-1] if prior else None
    resume = ""
    if body.continue_run:
        if last is None or last.role != "assistant" or not last.limited or not last.trace_id:
            raise HTTPException(400, "这次会话没有停在轮次上限")
        resume = last.trace_id
    elif not text:
        raise HTTPException(400, "请填写问题")
    history = [{"role": item.role, "content": item.content} for item in prior if item.role in {"user", "assistant"} and item.content]
    if resume:
        history = []
    environment = "test" if ctx.role in {"builder", "admin"} or is_platform(ctx.user) else "prod"
    return text, resume, history, {"environment": environment, "agent_id": row.agent_id}


def _save_turn(db, row: ChatSession, text: str, result: dict, viewer_id: int) -> dict:
    now = datetime.utcnow()
    if text:
        db.add(ChatMessage(session_id=row.id, role="user", content=text, created_at=now))
        if row.title == "新的问答":
            row.title = text[:24]
    db.add(
        ChatMessage(
            session_id=row.id,
            role="assistant",
            content=result.get("output") or "",
            trace_id=result.get("trace_id") or "",
            tool_calls_json=dump(result.get("tool_calls") or []),
            status=result.get("status") or "",
            limited=bool(result.get("limited")),
            created_at=now,
        )
    )
    row.updated_at = now
    db.flush()
    messages = db.query(ChatMessage).filter_by(session_id=row.id).order_by(ChatMessage.id.asc()).all()
    data = _brief(db, row, viewer_id=viewer_id)
    data["messages"] = [_message_dict(item) for item in messages]
    data["result"] = {
        "trace_id": result.get("trace_id") or "",
        "status": result.get("status") or "",
        "limited": bool(result.get("limited")),
        "cost": result.get("cost") or 0,
        "tokens": result.get("tokens") or 0,
    }
    return data


def _run_turn(db, ctx, row, text, resume, history, plan) -> dict:
    agent = _agent_in_space(db, ctx, plan["agent_id"])
    if agent.space_id and agent.space_id != ctx.space.id:
        space = db.get(Space, agent.space_id)
        policy = db.query(ContextPolicy).filter_by(space_id=space.id).one()
    else:
        space = ctx.space
        policy = ctx.policy
    try:
        return execute_agent(
            db,
            agent=agent,
            user=ctx.user,
            space=space,
            message=text,
            environment=plan["environment"],
            role=ctx.role,
            policy=policy,
            history=history,
            resume_trace_id=resume,
        )
    except GateError as exc:
        status = 429 if exc.status == "quota" else 409 if exc.status == "offline" else 400
        raise HTTPException(status, exc.message) from exc


def _stream_turn(request: Request, ctx, session_id: str, text: str, resume: str, history: list, plan: dict):
    """在独立会话里执行，并把回答片段按 SSE 推出去。"""
    factory = request.app.state.session_factory
    user_id = ctx.user.id
    space_id = ctx.space.id
    role = ctx.role
    events: queue.Queue = queue.Queue()

    def worker() -> None:
        session = factory()
        emitted = {"n": 0}

        def sink(event):
            if event.get("type") == "delta":
                emitted["n"] += 1
            events.put(event)

        token = _CHAT_EMIT.set(sink)
        try:
            user = session.get(User, user_id)
            space = session.get(Space, space_id)
            policy = session.query(ContextPolicy).filter_by(space_id=space_id).one()
            row = session.get(ChatSession, session_id)
            holder = type("Holder", (), {"user": user, "space": space, "role": role, "policy": policy})()
            result = _run_turn(session, holder, row, text, resume, history, plan)
            output = result.get("output") or ""
            if not emitted["n"]:
                for index in range(0, max(len(output), 1), 12):
                    events.put({"type": "delta", "text": output[index : index + 12]})
                    time.sleep(0.012)
            data = _save_turn(session, row, text, result, user_id)
            session.commit()
            events.put({"type": "done", "session": data})
        except HTTPException as exc:
            session.rollback()
            detail = exc.detail if isinstance(exc.detail, str) else "回答失败"
            events.put({"type": "error", "message": detail})
        except Exception as exc:
            session.rollback()
            events.put({"type": "error", "message": str(exc)})
        finally:
            _CHAT_EMIT.reset(token)
            session.close()

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            item = events.get()
            yield f"data: {dump(item)}\n\n"
            if item.get("type") in {"done", "error"}:
                break

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/chats/{session_id}/messages")
def send_message(session_id: str, body: ChatMessageIn, request: Request, ctx=Depends(get_context), db=Depends(get_db)):
    """把已有问答作为历史送入智能体。stream 为真时按片段返回回答。"""
    require(ctx, "operator")
    row = _load_session(db, ctx, session_id)
    text, resume, history, plan = _turn(db, ctx, row, body)
    if body.stream:
        return _stream_turn(request, ctx, row.id, text, resume, history, plan)
    result = _run_turn(db, ctx, row, text, resume, history, plan)
    return _save_turn(db, row, text, result, ctx.user.id)


def _draft_model(db, space_id: int, agent: Agent, environment: str):
    """草案要用能写结构的模型。优先智能体自己的模型，否则用本部门已授权里能力更强的那个。"""
    version = pick_version(db, agent, 0, environment)
    allowed = set(ids_for(db, space_id, "model"))
    pinned = db.get(ModelEndpoint, version.model_id) if version and version.model_id else None
    if pinned and pinned.id in allowed and pinned.enabled and pinned.provider != "rules":
        return pinned
    rows = (
        db.query(ModelEndpoint)
        .filter(ModelEndpoint.enabled.is_(True), ModelEndpoint.id.in_(allowed or [-1]), ModelEndpoint.provider != "rules")
        .all()
    )
    rows.sort(key=lambda item: ((item.price_in or 0) + (item.price_out or 0)), reverse=True)
    return rows[0] if rows else None


def _parse_workflow(text: str, catalog: dict[int, str]) -> dict:
    raw = (text or "").strip()
    if "```" in raw:
        raw = raw.split("```", 2)[1]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有给出可检查的工作流")
    data = load(raw[start : end + 1], {})
    if not isinstance(data, dict):
        raise ValueError("模型没有给出可检查的工作流")
    kind = "autonomous" if data.get("kind") == "autonomous" else "agents"
    nodes = []
    for index, item in enumerate(data.get("nodes") or []):
        if not isinstance(item, dict):
            continue
        agent_id = int(item.get("agent_id") or 0)
        if agent_id not in catalog:
            continue
        nodes.append({"id": f"n{index + 1}", "type": "agent", "agent_id": agent_id, "x": 40 + index * 210, "y": 48})
    if len(nodes) < 2:
        raise ValueError("工作流至少要有两个本部门的智能体")
    known = {item["id"] for item in nodes}
    edges = []
    for item in data.get("edges") or []:
        if not isinstance(item, dict):
            continue
        src, dst = str(item.get("src") or ""), str(item.get("dst") or "")
        if src in known and dst in known and src != dst:
            edges.append({"src": src, "dst": dst})
    if not edges:
        if kind == "autonomous":
            edges = [{"src": nodes[0]["id"], "dst": item["id"]} for item in nodes[1:]]
        else:
            edges = [{"src": nodes[index]["id"], "dst": nodes[index + 1]["id"]} for index in range(len(nodes) - 1)]
    name = str(data.get("name") or "生成的工作流").strip()[:120] or "生成的工作流"
    definition = {"kind": kind, "entry": nodes[0]["id"], "nodes": nodes, "edges": edges}
    lines = [f"{catalog[item['agent_id']]}" for item in nodes]
    summary = ("调度：" if kind == "autonomous" else "固定：") + " → ".join(lines)
    return {"name": name, "kind": kind, "definition": definition, "summary": summary}


@router.post("/api/chats/{session_id}/workflow")
def propose_workflow(session_id: str, body: ChatMessageIn, ctx=Depends(get_context), db=Depends(get_db)):
    """按任务描述起草工作流，只生成待审批的草案，不写入工作流列表。"""
    require(ctx, "operator")
    row = _load_session(db, ctx, session_id)
    if row.user_id != ctx.user.id:
        raise HTTPException(403, "只能在自己的会话里提问")
    if row.agent_id is None:
        raise HTTPException(400, "请先选择智能体")
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "请填写任务描述")
    agent = _agent_in_space(db, ctx, row.agent_id)
    space = db.get(Space, agent.space_id) if agent.space_id else ctx.space
    environment = "test" if ctx.role in {"builder", "admin"} or is_platform(ctx.user) else "prod"
    model = _draft_model(db, space.id, agent, environment)
    if model is None:
        raise HTTPException(400, "当前部门没有可用来生成工作流的模型")
    agents = db.query(Agent).filter_by(space_id=space.id).order_by(Agent.id.asc()).all()
    catalog = {}
    lines = []
    for item in agents:
        version = db.query(AgentVersion).filter_by(agent_id=item.id).order_by(AgentVersion.number.desc()).first()
        duty = ((version.system_prompt if version else "") or "").strip().split("\n", 1)[0][:80]
        catalog[item.id] = item.name
        lines.append(f"- {item.id} {item.name}：{duty}")
    prompt = (
        "根据任务描述设计一条工作流，只输出一个 JSON 对象，不要解释。\n"
        "kind 用 agents 表示按连线依次执行，用 autonomous 表示第一个节点调度后面的成员。\n"
        "nodes 里只写 agent_id，必须来自下面的编号。至少两个节点。edges 用 src、dst，节点编号按出现顺序写成 n1、n2。\n"
        "可用智能体：\n" + "\n".join(lines)
    )
    result = complete_with_tools(
        db,
        model,
        [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        [],
    )
    tokens_in, tokens_out = result["usage"]
    cost_in, cost_out, cost = cost_parts(model, tokens_in, tokens_out)
    charge_model(model, cost)
    if result.get("fallback"):
        raise HTTPException(400, result.get("error") or "模型没有生成工作流")
    try:
        draft = _parse_workflow(result.get("text") or "", catalog)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    draft["task"] = text
    trace = Trace(
        id=uuid.uuid4().hex,
        space_id=space.id,
        agent_id=agent.id,
        user_id=ctx.user.id,
        environment=environment,
        status="paused",
        input_text=text,
        output_text=draft["summary"],
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
    )
    db.add(trace)
    db.flush()
    approval = Approval(
        space_id=space.id,
        trace_id=trace.id,
        agent_id=agent.id,
        tool_name="工作流",
        payload_json=dump(draft),
        status="pending",
        channel="web",
        comment=draft["summary"],
    )
    db.add(approval)
    db.flush()
    output = (
        f"已按任务起草工作流「{draft['name']}」，尚未加入工作流管理。\n\n"
        f"{draft['summary']}\n\n"
        f"请到审批中心核对节点和连线。审批单 #{approval.id} 通过后，才会出现在工作流列表。"
    )
    saved = _save_turn(
        db,
        row,
        text,
        {"output": output, "trace_id": trace.id, "status": "paused", "limited": False, "cost": cost, "tokens": tokens_in + tokens_out, "tool_calls": []},
        ctx.user.id,
    )
    saved["approval_id"] = approval.id
    saved["workflow_draft"] = draft
    return saved
