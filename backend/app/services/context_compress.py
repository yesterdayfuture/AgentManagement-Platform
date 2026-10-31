"""分层上下文压缩：轻量剪枝 → 重量级摘要，按 Token 预算按需触发。

保护高重要性消息；代码块、数学公式等结构化内容自动绕过压缩。
"""

from __future__ import annotations

import re
from typing import Any

from app.services.gateway import complete_with_tools, select_model
from app.util import dump, load

STRUCTURED_RE = re.compile(
    r"(```[\s\S]*?```)|(\$\$.*?\$\$)|(\$(?:\\.|[^$])+\$)|(\\begin\{[a-z*]+\}[\s\S]*?\\end\{[a-z*]+\})",
    re.I,
)
CODE_HINT_RE = re.compile(r"(def |class |function |import |SELECT |\{[\s\S]{20,}\})")


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(str(item.get("content") or "")) + 4 for item in messages)


def context_budget(model) -> int:
    window = int(getattr(model, "context_tokens", None) or 1000000)
    return max(1024, int(window * 0.8))


def is_structured(content: str) -> bool:
    text = content or ""
    if STRUCTURED_RE.search(text):
        return True
    if CODE_HINT_RE.search(text) and len(text) > 80:
        return True
    return False


def importance(item: dict, *, index: int, total: int) -> float:
    """分数越高越应保留。"""
    role = item.get("role") or ""
    content = item.get("content") or ""
    score = 0.0
    if role == "system":
        score += 100
    if role == "summary":
        score += 90
    if index == 0 or index >= total - 2:
        score += 40
    if index >= total - 4:
        score += 20
    if is_structured(content):
        score += 80
    if any(mark in content for mark in ("【工具", "tool", "错误", "结论", "必须", "约束")):
        score += 15
    if len(content) < 40 and role == "user":
        score += 10
    if "触发敏感词" in content:
        score += 50
    return score


def light_prune(messages: list[dict], budget: int) -> list[dict]:
    """删掉中间低分消息，始终保留 system/summary/结构化/首尾。"""
    if messages_tokens(messages) <= budget:
        return list(messages)
    total = len(messages)
    ranked = sorted(
        ((importance(item, index=i, total=total), i, item) for i, item in enumerate(messages)),
        key=lambda row: (-row[0], row[1]),
    )
    keep = set()
    for score, index, item in ranked:
        role = item.get("role") or ""
        if role in {"system", "summary"} or is_structured(item.get("content") or "") or index < 2 or index >= total - 2:
            keep.add(index)
    for score, index, item in ranked:
        candidate = [messages[i] for i in sorted(keep | {index})]
        if messages_tokens(candidate) <= budget:
            keep.add(index)
        elif score < 30 and index not in keep:
            continue
        else:
            # 已超预算且分不高则跳过
            if index in keep:
                continue
            probe = keep | {index}
            if messages_tokens([messages[i] for i in sorted(probe)]) <= budget:
                keep.add(index)
    return [messages[i] for i in sorted(keep)]


_PROMPT_LEAK = re.compile(
    r"^(已收到请求|请将以下内容压缩|请把下面内容压缩|你是严谨的对话摘要|只输出摘要)",
    re.I,
)


def _extractive_summary(prior_summary: str, chunk: list[dict], level: int) -> str:
    """无可用 LLM 时的抽取式摘要，绝不回显压缩提示词。"""
    bits = []
    if prior_summary and not _PROMPT_LEAK.search((prior_summary or "").strip()):
        bits.append(f"承前：{(prior_summary or '').strip()[:180]}")
    for item in (chunk or [])[-8:]:
        role = "用户" if item.get("role") == "user" else "助手" if item.get("role") == "assistant" else (item.get("role") or "")
        content = (item.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        bits.append(f"{role}:{content[:100]}")
    body = "；".join(bits)[:900]
    return f"L{level}摘要：{body}" if body else f"L{level}摘要：（无可压缩内容）"


def _summarize(db, model, prior_summary: str, chunk: list[dict], level: int) -> str:
    parts = []
    if prior_summary:
        parts.append(f"【之前的摘要】\n{prior_summary}")
    lines = []
    for item in chunk or []:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        role = "用户" if item.get("role") == "user" else "助手" if item.get("role") == "assistant" else item.get("role")
        lines.append(f"{role}: {content}")
    if not lines and not (prior_summary or "").strip():
        return _extractive_summary(prior_summary, chunk, level)
    if lines:
        parts.append("【新的对话】\n" + "\n".join(lines))
    prompt = (
        f"请将以下内容压缩为第 {level} 层对话摘要，保留关键信息："
        "用户目标与约束、已确认事实、未解决问题、重要工具结果。"
        "代码块与数学公式用一句话说明其作用，不要展开全文。只输出摘要正文。\n\n"
        + "\n\n".join(parts)
    )
    # 规则/无密钥模型会把整段 prompt 回显成「已收到请求。问题：…」，不可用作摘要
    if model is None or (getattr(model, "provider", "") or "") == "rules":
        return _extractive_summary(prior_summary, chunk, level)
    result = complete_with_tools(
        db,
        model,
        [
            {"role": "system", "content": "你是严谨的对话摘要助手，只输出摘要。"},
            {"role": "user", "content": prompt},
        ],
        [],
    )
    text = (result.get("text") or "").strip()
    if result.get("fallback") or not text or _PROMPT_LEAK.search(text) or "请将以下内容压缩" in text:
        return _extractive_summary(prior_summary, chunk, level)
    return text


def compress_messages(
    db,
    messages: list[dict],
    *,
    model=None,
    space_id: int | None = None,
    force: bool = False,
    prior_summary: str = "",
    summary_level: int = 0,
) -> dict[str, Any]:
    """按需压缩。返回 history、mode、summary 文本与被折叠的消息。"""
    rows = [dict(item) for item in messages if (item.get("content") or "").strip() or item.get("role") == "system"]
    if not rows:
        return {"history": [], "mode": "none", "summary": "", "folded": [], "tokens_before": 0, "tokens_after": 0, "level": summary_level}
    reason_model = model or select_model(db, space_id or 0, "reason", allow_rules=False)
    budget = context_budget(reason_model)
    before = messages_tokens(rows)
    if not force and before <= budget:
        return {
            "history": rows,
            "mode": "none",
            "summary": prior_summary,
            "folded": [],
            "tokens_before": before,
            "tokens_after": before,
            "level": summary_level,
        }

    pruned = light_prune(rows, budget)
    after_prune = messages_tokens(pruned)
    if not force and after_prune <= budget:
        return {
            "history": pruned,
            "mode": "prune",
            "summary": prior_summary,
            "folded": [item for item in rows if item not in pruned],
            "tokens_before": before,
            "tokens_after": after_prune,
            "level": summary_level,
        }

    # 重量级：折叠中间可压缩段，保留首尾与结构化
    protected = []
    foldable = []
    total = len(pruned)
    for index, item in enumerate(pruned):
        role = item.get("role") or ""
        if role in {"system", "summary"} or is_structured(item.get("content") or "") or index < 1 or index >= total - 2:
            protected.append((index, item))
        else:
            foldable.append(item)
    if not foldable and not force:
        return {
            "history": pruned,
            "mode": "prune",
            "summary": prior_summary,
            "folded": [],
            "tokens_before": before,
            "tokens_after": after_prune,
            "level": summary_level,
        }
    level = (summary_level or 0) + 1
    # 短对话强制压缩时，首尾保护会使 foldable / pruned[1:-1] 都为空，
    # 必须回退到整段 user/assistant，否则摘要只会看到空的「【新的对话】」。
    chunk = list(foldable)
    if not chunk:
        chunk = [
            item
            for item in pruned
            if (item.get("role") or "") in {"user", "assistant"} and (item.get("content") or "").strip()
        ]
    if not chunk:
        chunk = [
            item
            for item in pruned
            if (item.get("role") or "") not in {"system", "summary"} and (item.get("content") or "").strip()
        ]
    summary_text = _summarize(db, reason_model, prior_summary, chunk, level)
    summary_msg = {"role": "summary", "content": f"【对话摘要 L{level}】\n{summary_text}"}
    kept = [item for _, item in protected if (item.get("role") or "") != "summary"]
    # 摘要放在 system 之后、近期消息之前
    systems = [item for item in kept if item.get("role") == "system"]
    others = [item for item in kept if item.get("role") != "system"]
    history = systems + [summary_msg] + others
    # 若仍超预算，再剪一次
    if messages_tokens(history) > budget:
        history = light_prune(history, budget)
    return {
        "history": history,
        "mode": "summary",
        "summary": summary_text,
        "folded": foldable or chunk,
        "tokens_before": before,
        "tokens_after": messages_tokens(history),
        "level": level,
    }
