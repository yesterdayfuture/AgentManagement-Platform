"""敏感词监测。

空间内启用的清单合并成词表；在智能体、工作流、模型问答的输入与模型输出上做子串匹配。
流式路径通过 ContextVar 把词表传给 gateway，命中后保留已推片段并追加停止说明。
"""

from __future__ import annotations

import contextvars
import re

from app.util import load

_SENSITIVE_WORDS = contextvars.ContextVar("sensitive_words", default=None)


def parse_words(raw) -> list[str]:
    """把列表或顿号/逗号/换行分隔的文本收成去重词表，长词优先。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        pieces = [str(item).strip() for item in raw]
    else:
        text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
        pieces = re.split(r"[\n,，、;；]+", text)
    seen: set[str] = set()
    words: list[str] = []
    for item in pieces:
        word = item.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        words.append(word)
    words.sort(key=len, reverse=True)
    return words


def load_words(db, space_id: int | None) -> list[str]:
    if not space_id:
        return []
    from app.models import SensitiveWordList

    rows = (
        db.query(SensitiveWordList)
        .filter(SensitiveWordList.space_id == space_id, SensitiveWordList.enabled.is_(True))
        .all()
    )
    seen: set[str] = set()
    words: list[str] = []
    for row in rows:
        for word in parse_words(load(row.words_json, [])):
            if word not in seen:
                seen.add(word)
                words.append(word)
    words.sort(key=len, reverse=True)
    return words


def bind_words(db, space_id: int | None):
    """绑定当前执行上下文的词表，返回 ContextVar token，结束后须 reset。"""
    return _SENSITIVE_WORDS.set(load_words(db, space_id))


def reset_words(token) -> None:
    if token is not None:
        _SENSITIVE_WORDS.reset(token)


def current_words() -> list[str]:
    return list(_SENSITIVE_WORDS.get() or [])


def find_hit(text: str, words: list[str] | None = None) -> str | None:
    blob = text or ""
    if not blob:
        return None
    for word in words if words is not None else current_words():
        if word and word in blob:
            return word
    return None


def stop_message(word: str) -> str:
    return f"触发敏感词「{word}」，已停止输出"


class SensitiveHit(Exception):
    """输出监测命中。流式路径保留已输出正文，再追加 stop_message 后抛出。"""

    def __init__(self, word: str, prefix: str = ""):
        self.word = word
        notice = stop_message(word)
        text = (prefix or "").rstrip()
        self.message = f"{text}\n{notice}" if text else notice
        super().__init__(self.message)
