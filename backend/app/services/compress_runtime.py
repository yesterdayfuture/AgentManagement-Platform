"""对话压缩编排：触发分层压缩，并在摘要时写入三层记忆。"""

from __future__ import annotations

from app.models import Memory
from app.services.context_compress import compress_messages, messages_tokens, context_budget
from app.services.memory_layers import KIND_SUMMARY, extract_and_store, memories_for_prompt, visible_memories


def latest_summary(db, *, space_id: int, task_id: str) -> Memory | None:
    if not task_id:
        return None
    return (
        db.query(Memory)
        .filter(
            Memory.space_id == space_id,
            Memory.kind == KIND_SUMMARY,
            Memory.task_id == str(task_id),
        )
        .order_by(Memory.id.desc())
        .first()
    )


def prior_summary_texts(db, *, space_id: int, task_id: str, limit: int = 5) -> list[str]:
    if not task_id:
        return []
    rows = (
        db.query(Memory)
        .filter(
            Memory.space_id == space_id,
            Memory.kind == KIND_SUMMARY,
            Memory.task_id == str(task_id),
        )
        .order_by(Memory.id.desc())
        .limit(limit)
        .all()
    )
    return [row.content for row in reversed(rows)]


def inject_memories(
    db,
    messages: list[dict],
    *,
    space_id: int,
    user_id: int,
    task_id: str = "",
    source_kind: str = "",
    viewer_is_platform: bool = False,
) -> list[dict]:
    """新任务/会话：注入账户用户画像 + 长期记忆；同 task 再附短期。"""
    from app.services.memory_layers import KIND_LONG, KIND_SHORT, KIND_USER

    rows = visible_memories(
        db,
        space_id=space_id,
        user_id=user_id,
        task_id=task_id,
        source_kind=source_kind,
        viewer_is_platform=viewer_is_platform,
        viewer_id=user_id,
    )
    lasting = [row for row in rows if row.kind in {KIND_USER, KIND_LONG}]
    short = [row for row in rows if row.kind == KIND_SHORT] if task_id else []
    # 画像与长期优先，短期仅当前任务
    ordered = lasting[:8] + short[:4]
    block = memories_for_prompt(ordered, limit=12)
    if not block:
        return list(messages)
    out = [dict(item) for item in messages]
    note = f"【账户记忆·用户画像与长期约定】\n{block}"
    for item in out:
        if item.get("role") == "system":
            item["content"] = f"{item.get('content') or ''}\n\n{note}".strip()
            return out
    return [{"role": "system", "content": note}] + out


def prepare_history(
    db,
    messages: list[dict],
    *,
    model=None,
    space_id: int,
    user_id: int,
    force: bool = False,
    source_kind: str,
    source_id: str,
    source_name: str,
    task_id: str,
    agent_id: int | None = None,
    retention_days: int = 30,
) -> dict:
    """按需压缩历史，摘要时自动产出 short/long/user 记忆。"""
    prior = latest_summary(db, space_id=space_id, task_id=task_id)
    prior_text = prior.content if prior else ""
    level = prior.level if prior else 0
    result = compress_messages(
        db,
        messages,
        model=model,
        space_id=space_id,
        force=force,
        prior_summary=prior_text,
        summary_level=level or 0,
    )
    summary_row = None
    if result["mode"] == "summary" and result.get("summary"):
        priors = prior_summary_texts(db, space_id=space_id, task_id=task_id)
        summary_row = extract_and_store(
            db,
            space_id=space_id,
            user_id=user_id,
            dialogue=result.get("folded") or messages,
            summary_text=result["summary"],
            prior_summaries=priors,
            source_kind=source_kind,
            source_id=source_id,
            source_name=source_name,
            task_id=task_id,
            agent_id=agent_id,
            parent_summary_id=prior.id if prior else None,
            level=result.get("level") or (level or 0) + 1,
            retention_days=retention_days,
            model=model,
        )
        result["summary_id"] = summary_row.id
    else:
        result["summary_id"] = prior.id if prior else None
    result["budget"] = context_budget(model) if model else 0
    result["over_budget"] = messages_tokens(messages) > (result["budget"] or 10**12)
    return result
