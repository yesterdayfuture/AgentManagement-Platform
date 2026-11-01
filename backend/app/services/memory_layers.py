"""三层记忆：短期（任务/会话）· 长期（用户跨任务）· 用户画像。

压缩摘要触发时，根据历史摘要与对话抽取三类记忆，并写入溯源。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from app.models import Memory
from app.services.gateway import complete_with_tools, select_model
from app.util import dump, load

KIND_SHORT = "short"
KIND_LONG = "long"
KIND_USER = "user"
KIND_SUMMARY = "summary"
_EVALUATING = ContextVar("limen_evaluating", default=False)


@contextmanager
def evaluation_scope():
    token = _EVALUATING.set(True)
    try:
        yield
    finally:
        _EVALUATING.reset(token)


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def visible_memories(
    db,
    *,
    space_id: int,
    user_id: int,
    task_id: str = "",
    source_kind: str = "",
    viewer_is_platform: bool = False,
    viewer_id: int | None = None,
) -> list[Memory]:
    """运行时注入用的可见记忆。"""
    if _EVALUATING.get():
        return []
    now = datetime.utcnow()
    query = db.query(Memory).filter(Memory.space_id == space_id)
    query = query.filter((Memory.expires_at.is_(None)) | (Memory.expires_at > now))
    rows = query.order_by(Memory.id.desc()).limit(200).all()
    out = []
    for row in rows:
        kind = row.kind or KIND_SHORT
        if kind == KIND_SUMMARY:
            continue
        if kind == KIND_SHORT:
            if not task_id or (row.task_id or "") != task_id:
                continue
            if row.user_id and row.user_id != user_id:
                continue
        elif kind == KIND_LONG:
            if row.user_id and row.user_id != user_id:
                continue
        elif kind == KIND_USER:
            if getattr(row, "active", True) is False:
                continue
            owner = row.user_id or row.created_by
            # 运行时注入：只用当前主体用户的启用中画像
            if owner and owner != user_id:
                continue
        else:
            continue
        out.append(row)
    return out


def memories_for_prompt(rows: list[Memory], *, limit: int = 8) -> str:
    # 短期（当前任务）优先，其次用户画像，再到长期
    rank = {KIND_SHORT: 0, KIND_USER: 1, KIND_LONG: 2}
    ordered = sorted(rows, key=lambda row: (rank.get(row.kind, 9), -(row.id or 0)))
    blocks = []
    for row in ordered[:limit]:
        body = _clean_memory_text(row.content or "") if row.kind in {KIND_LONG, KIND_USER} else (row.content or "").strip()
        if not body:
            continue
        label = {"short": "短期", "long": "长期", "user": "用户"}.get(row.kind, row.kind)
        name = row.source_name or row.task_id or ""
        head = f"[{label}]" + (f" {name}" if name else "")
        blocks.append(f"{head} {body}")
    return "\n".join(blocks)


_USER_CUES = ("我叫", "请叫我", "称呼我", "我是", "我喜欢", "偏好", "不要用", "必须用")
_LONG_CUES = ("请记住", "记得", "以后都", "以后默认", "默认走", "默认物流", "默认对账", "默认沟通", "默认抄送", "固定为", "结算日", "对账日")
# 任务指令句：含触发词也不写入长期/用户（例如「请综合说明…上述偏好…」）
_TASK_ASK = re.compile(
    r"^(请综合|请说明|请输出|请按|请先确认|请简要|请依次|不要调用|不要计算|不要检索)|"
    r"(如何发起|你会如何|还差什么|还缺什么|上述偏好|这些偏好)"
)

_ARTIFACT = re.compile(r"^(?:【v\d+】)?\s*\[(?:短期|长期|用户|short|long|user)\]", re.I)
_PROMPT_LEAK = re.compile(
    r"^(已收到请求|请将以下内容压缩|请把下面内容压缩|你是严谨的对话摘要|只输出摘要|你是记忆抽取助手)",
    re.I,
)

# 同主题冲突替换用：命中则视为同一画像/事实槽位
_TOPIC_RULES = (
    ("称呼", re.compile(r"我叫|请叫我|称呼|我是|身份")),
    ("所在地", re.compile(r"所在地|位于|在北京|在上海|在广州|在深圳")),
    ("职业", re.compile(r"职业|老师|工程师|产品经理")),
    ("兴趣", re.compile(r"兴趣|爱好|喜欢吃|喜欢逛")),
    ("沟通渠道", re.compile(r"企业微信|飞书|沟通渠道|抄送")),
    ("偏好", re.compile(r"偏好|语气|结论先行|编号列表|简洁|按条列出|按条")),
    ("物流", re.compile(r"物流|快递|顺丰|中通|京东|圆通")),
    ("发票", re.compile(r"发票|抬头")),
    ("对账", re.compile(r"对账|结算日")),
    ("方案流程", re.compile(r"先复述|再给方案|先确认需求")),
)

# 用户画像槽位：结构化标签与同义整句归一到同一 key
_PROFILE_KEY_ALIASES = {
    "地点": "所在地",
    "地区": "所在地",
    "所在": "所在地",
    "爱好": "兴趣",
    "喜好": "兴趣",
    "偏好": "沟通偏好",
    "沟通习惯": "沟通偏好",
    "沟通风格": "沟通偏好",
    "语气": "沟通偏好",
    "称呼": "身份",
    "角色": "身份",
}
_PROFILE_KV = re.compile(
    r"^(身份|称呼|角色|所在地|地点|地区|所在|兴趣|爱好|喜好|职业|偏好|沟通偏好|沟通习惯|沟通风格|语气)\s*[:：]\s*(.+)$"
)
_PROFILE_SENTENCE = (
    (re.compile(r"^(?:用户)?(?:的)?身份(?:是|为|:|：)\s*(.+)$"), "身份"),
    (re.compile(r"^称呼用户为\s*(.+)$"), "身份"),
    (re.compile(r"^称呼(?:请用|为|是|:|：)\s*[「『\"]?(.+?)[」』\"]?$"), "身份"),
    (re.compile(r"^(?:请)?称(?:呼)?我为\s*[「『\"]?(.+?)[」』\"]?$"), "身份"),
    (re.compile(r"^(?:用户)?是\s*(.+)$"), "身份"),
    (re.compile(r"^(?:用户)?(?:所在地|位于)\s*(?:为|是|:|：)?\s*(.+)$"), "所在地"),
    (re.compile(r"^(?:用户)?(?:目前)?在\s*(.+?)(?:[，,。；;]|$)"), "所在地"),
    (re.compile(r"^(?:用户)?职业(?:是|为|:|：)\s*(.+)$"), "职业"),
    (re.compile(r"^(?:用户)?(?:喜欢|爱)\s*(.+)$"), "兴趣"),
    (re.compile(r"^(?:用户)?偏好(?:沟通)?\s*(.+)$"), "沟通偏好"),
)


def _memory_topic(text: str) -> str:
    piece = (text or "").strip()
    for name, rule in _TOPIC_RULES:
        if rule.search(piece):
            return name
    return ""


def _canonical_profile_key(key: str) -> str:
    raw = (key or "").strip()
    return _PROFILE_KEY_ALIASES.get(raw, raw)


def _normalize_profile_fact(text: str) -> str:
    """画像条目归一为「标签：值」，去掉同义整句重复。"""
    piece = _clean_memory_text(text or "")
    if not piece:
        return ""
    piece = piece.strip().strip("。；;，,")
    if not piece:
        return ""
    matched = _PROFILE_KV.match(piece)
    if matched:
        key = _canonical_profile_key(matched.group(1))
        val = matched.group(2).strip().strip("。；;")
        return f"{key}：{val}" if val else ""
    for rule, key in _PROFILE_SENTENCE:
        hit = rule.match(piece)
        if hit:
            val = hit.group(1).strip().strip("。；;，,")
            # 「在北京，喜欢…」这类只取地点短值
            if key == "所在地":
                val = re.split(r"[，,、]", val, maxsplit=1)[0].strip()
            if key == "兴趣":
                val = val.replace("和", "、").replace("与", "、")
            if key == "沟通偏好":
                val = re.sub(r"^沟通", "", val).strip(" ：:")
            if key == "身份":
                val = re.sub(r"^(?:用户为|用户是|为)", "", val).strip()
            return f"{key}：{val}" if val else ""
    return piece


def _profile_slot(text: str) -> str:
    """画像行的槽位名：优先结构化标签，其次主题规则。"""
    piece = (text or "").strip()
    if not piece:
        return ""
    matched = _PROFILE_KV.match(piece)
    if matched:
        return _canonical_profile_key(matched.group(1))
    for rule, key in _PROFILE_SENTENCE:
        if rule.match(piece):
            return key
    topic = _memory_topic(piece)
    if topic == "称呼":
        return "身份"
    if topic == "偏好":
        return "沟通偏好"
    if topic in {"所在地", "职业", "兴趣"}:
        return topic
    return topic


def active_user_profile(db, *, space_id: int, user_id: int) -> Memory | None:
    return (
        db.query(Memory)
        .filter(
            Memory.space_id == space_id,
            Memory.user_id == user_id,
            Memory.kind == KIND_USER,
            Memory.active.is_(True),
        )
        .order_by(Memory.level.desc(), Memory.id.desc())
        .first()
    )


def merge_user_profile(existing: str, facts: list[str]) -> str:
    """把新抽出的用户信息合并进画像正文（按主题槽位覆盖，同义句去重）。"""
    slots: dict[str, str] = {}
    order: list[str] = []

    def _put(line: str):
        normalized = _normalize_profile_fact(line)
        if not normalized:
            return
        topic = _profile_slot(normalized) or _memory_topic(normalized) or normalized[:24]
        if topic not in slots:
            order.append(topic)
        # 已有结构化「标签：值」时，不被同义整句回写冲掉（若新值也是结构化则覆盖）
        prev = slots.get(topic) or ""
        if _PROFILE_KV.match(prev) and not _PROFILE_KV.match(normalized) and prev.split("：", 1)[-1] in normalized:
            return
        slots[topic] = normalized

    for part in re.split(r"[\n；;]+", existing or ""):
        _put(part)
    for fact in facts or []:
        for part in re.split(r"[\n；;]+", fact if isinstance(fact, str) else dump(fact)):
            _put(part)
    return "\n".join(slots[key] for key in order if slots.get(key)).strip()


def ensure_user_profile_backfill(db) -> None:
    """旧数据：每用户仅保留一条启用中的画像，其余标为历史版本；并压实同义重复行。"""
    rows = db.query(Memory).filter(Memory.kind == KIND_USER).order_by(Memory.user_id, Memory.space_id, Memory.id).all()
    groups: dict[tuple[int, int], list[Memory]] = {}
    for row in rows:
        key = (row.space_id or 0, row.user_id or row.created_by or 0)
        groups.setdefault(key, []).append(row)
    for (_space, _uid), items in groups.items():
        if not items:
            continue
        actives = [row for row in items if getattr(row, "active", True)]
        if len(actives) != 1:
            facts = [(row.content or "").strip() for row in items if (row.content or "").strip()]
            merged = merge_user_profile("", facts) if facts else (items[-1].content or "")
            for row in items:
                row.active = False
            latest = items[-1]
            if not any(row.parent_summary_id for row in items):
                latest.content = merged or latest.content
            latest.active = True
            latest.level = max((row.level or 0) for row in items) or 1
        # 保留历史内容、记录 ID 与溯源，只修正重复或倒退的编号。
        previous = 0
        for item in items:
            item.level = max(item.level or 0, previous + 1)
            previous = item.level
        # 压实启用中画像里的同义重复（如「身份：…」与「用户是…」并存）
        for row in items:
            if not getattr(row, "active", True):
                continue
            compacted = merge_user_profile(row.content or "", [])
            if compacted and compacted != (row.content or "").strip():
                row.content = compacted
        db.flush()


def profile_versions(db, *, space_id: int, user_id: int) -> list[Memory]:
    return (
        db.query(Memory)
        .filter(Memory.space_id == space_id, Memory.user_id == user_id, Memory.kind == KIND_USER)
        .order_by(Memory.level.desc(), Memory.id.desc())
        .all()
    )


def next_profile_version(db, *, space_id: int, user_id: int) -> int:
    """新画像总是接续完整历史，并在同一事务中停用所有旧版本。"""
    versions = profile_versions(db, space_id=space_id, user_id=user_id)
    number = max((item.level or 0 for item in versions), default=0) + 1
    for item in versions:
        item.active = False
    db.flush()
    return number


def activate_profile_version(db, row: Memory) -> Memory:
    """启用指定用户画像版本，同用户其余版本固化。"""
    if row.kind != KIND_USER:
        raise ValueError("仅用户画像可启用版本")
    siblings = (
        db.query(Memory)
        .filter(Memory.space_id == row.space_id, Memory.user_id == row.user_id, Memory.kind == KIND_USER)
        .all()
    )
    for item in siblings:
        item.active = item.id == row.id
    db.flush()
    return row


def _clean_memory_text(text: str) -> str:
    """去掉规则模型回显的记忆行前缀，避免再次写入污染。"""
    piece = (text or "").strip()
    if not piece:
        return ""
    if _ARTIFACT.match(piece):
        piece = re.sub(r"^(?:【v\d+】)?\s*\[[^\]]+\]\s*", "", piece).strip()
        # 去掉可选的来源名（到第一个空格后的正文）
        if " " in piece and not any(cue in piece.split(" ", 1)[0] for cue in _USER_CUES + _LONG_CUES):
            head, tail = piece.split(" ", 1)
            if len(head) <= 40 and not any(ch in head for ch in "，。；"):
                piece = tail.strip()
    if _ARTIFACT.match(piece) or piece.startswith("【v"):
        return ""
    return piece[:240]


def _is_lasting_candidate(text: str) -> bool:
    """长期/用户记忆候选：排除任务提问与动作禁令句。"""
    piece = (text or "").strip()
    if not piece or _TASK_ASK.search(piece):
        return False
    return True


def _cue_snippets(text: str, cues: tuple[str, ...]) -> list[str]:
    """只抽取含触发词的分句，避免整段任务描述被当成长期/用户记忆。"""
    text = (text or "").strip()
    if not text or not any(cue in text for cue in cues):
        return []
    parts = re.split(r"[。！？\n；;]+", text)
    found = []
    for part in parts:
        piece = _clean_memory_text(part.strip(" ，,"))
        if piece and _is_lasting_candidate(piece) and any(cue in piece for cue in cues):
            found.append(piece[:240])
    if found:
        return found
    whole = _clean_memory_text(text[:240])
    return [whole] if whole and _is_lasting_candidate(whole) else []


def _identity_markers(text: str) -> list[str]:
    """用户原文中的联调/会话标识（如 M1793…），抽取后应尽量保留。"""
    found = re.findall(r"[A-Za-z]*\d{8,}", text or "")
    # 去重且保序
    seen: set[str] = set()
    out: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _ensure_short_anchors(shorts: list[str], message: str, fallbacks: list[str]) -> list[str]:
    """模型改写短期要点时若丢掉用户标识，补一条带标识的原文锚点。"""
    markers = _identity_markers(message)
    if not markers:
        return shorts
    if any(any(m in (s or "") for m in markers) for s in shorts):
        return shorts
    for fb in fallbacks:
        if any(m in (fb or "") for m in markers):
            return [fb[:240]] + list(shorts)
    tip = (message or "").strip()[:200]
    if tip and any(m in tip for m in markers):
        return [tip] + list(shorts)
    return shorts


def _fallback_extract(message: str, output: str, dialogue: list[dict]) -> dict:
    """模型抽不出 JSON 时的兜底：短期记本轮；长期/用户只从本轮输入分句抽取。"""
    short, long_term, user = [], [], []
    tip = (message or "").strip()[:160]
    out = (output or "").strip()[:200]
    if tip and out:
        short.append(f"{tip} → {out}")
    elif tip or out:
        short.append(tip or out)
    if not short:
        for item in reversed(dialogue or []):
            content = (item.get("content") or "").strip()
            if item.get("role") in {"user", "assistant"} and content:
                short.append(content[:240])
                break
    # 仅看本轮用户输入，避免把历史偏好重复写成新的长期/用户记忆
    user.extend(_cue_snippets(message, _USER_CUES))
    long_term.extend(_cue_snippets(message, _LONG_CUES))
    return {"short_term": short, "long_term": long_term, "user_memory": user}


def _memory_brief(row: Memory | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "kind": row.kind,
        "content": (row.content or "")[:400],
        "active": bool(getattr(row, "active", True)),
        "level": row.level or 0,
        "source_name": (row.source_name or "")[:160],
        "task_id": row.task_id or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _lineage_snapshot(db, *, space_id: int, user_id: int, task_id: str = "") -> dict:
    """抽取前快照：历史短期 / 长期 / 启用中画像，供溯源展示。"""
    short_q = (
        db.query(Memory)
        .filter(Memory.space_id == space_id, Memory.user_id == user_id, Memory.kind == KIND_SHORT)
        .order_by(Memory.id.desc())
    )
    if task_id:
        same = short_q.filter(Memory.task_id == str(task_id)).limit(6).all()
        shorts = same or short_q.limit(6).all()
    else:
        shorts = short_q.limit(6).all()
    longs = (
        db.query(Memory)
        .filter(Memory.space_id == space_id, Memory.user_id == user_id, Memory.kind == KIND_LONG)
        .order_by(Memory.id.desc())
        .limit(8)
        .all()
    )
    profile = active_user_profile(db, space_id=space_id, user_id=user_id)
    return {
        "prior_short": [x for x in (_memory_brief(row) for row in shorts) if x],
        "prior_long": [x for x in (_memory_brief(row) for row in longs) if x],
        "prior_user": [x for x in [_memory_brief(profile)] if x],
    }


def _store_extracted(
    db,
    *,
    data: dict,
    space_id: int,
    user_id: int,
    agent_id: int | None,
    source_kind: str,
    source_id: str,
    source_name: str,
    task_id: str,
    retention_days: int,
    summary_text: str,
    dialogue: list[dict],
    prior_summaries: list[str],
    write_summary: bool,
    parent_summary_id: int | None,
    level: int,
) -> Memory | None:
    expires_short = datetime.utcnow() + timedelta(days=max(1, min(retention_days, 14)))
    expires_long = datetime.utcnow() + timedelta(days=max(retention_days, 90))
    produced = {"short": [], "long": [], "user": []}
    prior = _lineage_snapshot(db, space_id=space_id, user_id=user_id, task_id=task_id)
    source_refs = {
        "dialogues": [
            {"role": item.get("role"), "content": (item.get("content") or "")[:500]}
            for item in dialogue
            if item.get("role") in {"user", "assistant"}
        ][-20:],
        "summaries": [text for text in ((prior_summaries or [])[-5:] + ([summary_text] if summary_text else [])) if text],
        "prior_short": prior.get("prior_short") or [],
        "prior_long": prior.get("prior_long") or [],
        "prior_user": prior.get("prior_user") or [],
    }

    def _add_short(content: str):
        text = (content or "").strip()
        if not text or _PROMPT_LEAK.search(text) or "请将以下内容压缩" in text:
            return None
        row = Memory(
            space_id=space_id,
            agent_id=agent_id,
            user_id=user_id,
            project_id="",
            kind=KIND_SHORT,
            content=text[:2000],
            source_kind=source_kind,
            source_id=str(source_id or ""),
            source_name=(source_name or "")[:160],
            task_id=str(task_id or ""),
            level=0,
            active=True,
            parent_summary_id=None,
            source_refs_json="{}",
            produced_json="{}",
            created_by=user_id,
            expires_at=expires_short,
        )
        db.add(row)
        db.flush()
        return row

    notes: list[str] = []

    def _upsert_long(content: str):
        """长期：同主题更新，新主题新增。"""
        text = _clean_memory_text(content)
        if not text or _ARTIFACT.search(text) or not _is_lasting_candidate(text):
            return None
        text = text[:2000]
        existing = (
            db.query(Memory)
            .filter(Memory.space_id == space_id, Memory.user_id == user_id, Memory.kind == KIND_LONG)
            .order_by(Memory.id.desc())
            .limit(80)
            .all()
        )
        topic = _memory_topic(text)
        for row in existing:
            old = (row.content or "").strip()
            if old == text:
                notes.append(f"长期未变：{text[:80]}")
                return None
            if topic and _memory_topic(old) == topic:
                prev = old
                row.content = text
                row.source_kind = source_kind
                row.source_id = str(source_id or "")
                row.source_name = (source_name or "")[:160]
                row.task_id = str(task_id or "")
                row.agent_id = agent_id
                row.expires_at = expires_long
                row.active = True
                db.flush()
                notes.append(f"长期已调整（{topic}）：「{prev[:60]}」→「{text[:60]}」")
                return row
        row = Memory(
            space_id=space_id,
            agent_id=agent_id,
            user_id=user_id,
            project_id="",
            kind=KIND_LONG,
            content=text,
            source_kind=source_kind,
            source_id=str(source_id or ""),
            source_name=(source_name or "")[:160],
            task_id=str(task_id or ""),
            level=0,
            active=True,
            parent_summary_id=None,
            source_refs_json="{}",
            produced_json="{}",
            created_by=user_id,
            expires_at=expires_long,
        )
        db.add(row)
        db.flush()
        notes.append(f"长期新增：{text[:80]}")
        return row

    def _bump_user_profile(facts: list[str]):
        """用户画像：每用户一条启用中版本；有调整则固化旧版并新建版本。"""
        cleaned = []
        for item in facts:
            raw = item if isinstance(item, str) else dump(item)
            text = _normalize_profile_fact(raw)
            if text and _is_lasting_candidate(text) and not _ARTIFACT.search(text):
                cleaned.append(text[:500])
        if not cleaned:
            return None
        active = active_user_profile(db, space_id=space_id, user_id=user_id)
        # 先压实旧画像，再合并新事实，避免历史同义句残留
        base = merge_user_profile(active.content if active else "", [])
        merged = merge_user_profile(base, cleaned)
        if active and (active.content or "").strip() == merged.strip():
            notes.append("用户画像未变")
            return None
        version = next_profile_version(db, space_id=space_id, user_id=user_id)
        if active:
            active.active = False
            db.flush()
            notes.append(f"用户画像 v{active.level or 1} 已固化")
        row = Memory(
            space_id=space_id,
            agent_id=agent_id,
            user_id=user_id,
            project_id="",
            kind=KIND_USER,
            content=merged[:2000],
            source_kind=source_kind,
            source_id=str(source_id or ""),
            source_name=(source_name or "")[:160],
            task_id=str(task_id or ""),
            level=version,
            active=True,
            parent_summary_id=active.id if active else None,
            source_refs_json="{}",
            produced_json="{}",
            created_by=user_id,
            expires_at=None,
        )
        db.add(row)
        db.flush()
        notes.append(f"用户画像 v{version} 已启用：{merged[:80]}")
        return row

    for item in data.get("short_term") or []:
        row = _add_short(item if isinstance(item, str) else dump(item))
        if row:
            produced["short"].append(row.id)
    for item in data.get("long_term") or []:
        text = item if isinstance(item, str) else (item.get("content") if isinstance(item, dict) else dump(item))
        row = _upsert_long(text)
        if row:
            produced["long"].append(row.id)
    user_facts = []
    for item in data.get("user_memory") or []:
        text = item if isinstance(item, str) else (item.get("content") if isinstance(item, dict) else dump(item))
        if text:
            user_facts.append(text)
    if user_facts:
        row = _bump_user_profile(user_facts)
        if row:
            produced["user"].append(row.id)

    if not produced["short"] and summary_text and not _PROMPT_LEAK.search((summary_text or "").strip()):
        row = _add_short(summary_text[:500])
        if row:
            produced["short"].append(row.id)
    produced["notes"] = notes

    # 短/长期/画像均挂上溯源，便于管理界面点开查看
    refs_json = dump(source_refs)
    for item_id in produced["short"]:
        row = db.get(Memory, item_id)
        if row is None:
            continue
        row.source_refs_json = refs_json
        row.produced_json = dump(
            {
                "long": list(produced["long"]),
                "user": list(produced["user"]),
                "short": [sid for sid in produced["short"] if sid != item_id],
                "notes": notes,
            }
        )
    for item_id in produced["long"]:
        row = db.get(Memory, item_id)
        if row is None:
            continue
        row.source_refs_json = refs_json
        row.produced_json = dump({"notes": notes})
    for item_id in produced["user"]:
        row = db.get(Memory, item_id)
        if row is None:
            continue
        row.source_refs_json = refs_json
        row.produced_json = dump({"notes": notes})
    db.flush()

    if not write_summary:
        return None

    summary_row = Memory(
        space_id=space_id,
        agent_id=agent_id,
        user_id=user_id,
        project_id="",
        kind=KIND_SUMMARY,
        content=summary_text or "（空摘要）",
        source_kind=source_kind,
        source_id=str(source_id or ""),
        source_name=(source_name or "")[:160],
        task_id=str(task_id or ""),
        level=level or 1,
        active=True,
        parent_summary_id=parent_summary_id,
        source_refs_json=refs_json,
        produced_json=dump(produced),
        created_by=user_id,
        expires_at=None,
    )
    db.add(summary_row)
    db.flush()
    return summary_row


def _extract_layers(db, *, space_id: int, dialogue: list[dict], summary_text: str, prior_summaries: list[str], model=None) -> dict:
    reason = None
    if model is not None and (getattr(model, "provider", "") or "") != "rules":
        reason = model
    if reason is None:
        reason = select_model(db, space_id, "reason", allow_rules=False)
    if reason is None:
        # 测试环境或仅有规则模型时仍走 complete（可被 monkeypatch）；脏回退由下方拦截
        reason = model or select_model(db, space_id, "reason", allow_rules=True)
    if reason is None:
        return {}
    if summary_text and _PROMPT_LEAK.search(summary_text.strip()):
        summary_text = ""
    history_blob = []
    for item in dialogue[-30:]:
        role = item.get("role") or ""
        if role not in {"user", "assistant"}:
            continue
        history_blob.append(f"{role}: {item.get('content') or ''}")
    prior = "\n\n".join(prior_summaries[-3:]) if prior_summaries else "（无）"
    prompt = (
        "你是记忆抽取助手。根据【历史摘要】与【对话】输出严格 JSON，不要 markdown。\n"
        "{\n"
        '  "short_term": ["仅对当前会话/任务有用的要点"],\n'
        '  "long_term": ["跨会话仍有价值的业务事实/约定"],\n'
        '  "user_memory": ["身份：…", "所在地：…", "沟通偏好：…"]\n'
        "}\n"
        "要求：不要编造；任务提问与一次性问题不要写入长期/用户；"
        "与已有画像冲突时仍输出新值（系统会替换），无新信息则对应数组留空；"
        "user_memory 每条必须是单一槽位，格式严格为「标签：值」，标签只用："
        "身份、称呼、所在地、职业、兴趣、沟通偏好；同一标签只出现一次；"
        "禁止再用「用户是…」「用户所在地为…」「用户喜欢…」等整句重复已写出的槽位；"
        "用户原文中的专有称呼、编号、标签（如 M123…）必须原样保留在至少一条 short_term 或 lasting 条目中。\n\n"
        f"【历史摘要】\n{prior}\n\n"
        f"【本轮摘要】\n{summary_text or '（无）'}\n\n"
        f"【对话】\n" + "\n".join(history_blob)
    )
    result = complete_with_tools(
        db,
        reason,
        [
            {"role": "system", "content": "只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        [],
    )
    text = result.get("text") or ""
    if result.get("fallback") or _PROMPT_LEAK.search((text or "").strip()):
        return {}
    return _parse_json(text)


def capture_task_memories(
    db,
    *,
    space_id: int,
    user_id: int,
    message: str = "",
    output: str = "",
    dialogue: list[dict] | None = None,
    source_kind: str,
    source_id: str,
    source_name: str,
    task_id: str,
    agent_id: int | None = None,
    retention_days: int = 30,
    model=None,
) -> dict:
    """任务结束后落库短期/长期/用户记忆（问答、智能体、工作流共用）。

    不强制写 summary 行，避免每次执行刷屏；压缩摘要仍走 extract_and_store。
    """
    if _EVALUATING.get():
        return {"short": [], "long": [], "user": []}
    turns = list(dialogue or [])
    if message:
        turns = turns + [{"role": "user", "content": message}]
    if output:
        turns = turns + [{"role": "assistant", "content": output}]
    summary_text = ""
    if message or output:
        summary_text = f"{(message or '').strip()[:200]}"
        if output:
            summary_text = f"{summary_text} → {(output or '').strip()[:300]}".strip(" →")
    extracted = _extract_layers(
        db,
        space_id=space_id,
        dialogue=turns,
        summary_text=summary_text,
        prior_summaries=[],
        model=model,
    )
    fallback = _fallback_extract(message, output, turns)

    def _pick(key: str, *, lasting: bool) -> list:
        chosen = []
        for item in extracted.get(key) or []:
            raw = (item if isinstance(item, str) else dump(item)).strip()
            text = _clean_memory_text(raw) if lasting else raw
            if lasting and text and not _is_lasting_candidate(text):
                continue
            if text:
                chosen.append(text)
        if chosen:
            if key == "short_term":
                return _ensure_short_anchors(chosen, message, fallback.get(key) or [])
            return chosen
        return [x for x in (fallback.get(key) or []) if (not lasting or _is_lasting_candidate(x))]

    # 模型常只抽到 short，或抽到需清洗的脏行；缺层用规则兜底补齐
    data = {
        "short_term": _pick("short_term", lasting=False),
        "long_term": _pick("long_term", lasting=True),
        "user_memory": _pick("user_memory", lasting=True),
    }
    data = _dedupe_extracted(db, space_id=space_id, user_id=user_id, data=data)
    _store_extracted(
        db,
        data=data,
        space_id=space_id,
        user_id=user_id,
        agent_id=agent_id,
        source_kind=source_kind,
        source_id=source_id,
        source_name=source_name,
        task_id=task_id,
        retention_days=retention_days,
        summary_text=summary_text,
        dialogue=turns,
        prior_summaries=[],
        write_summary=False,
        parent_summary_id=None,
        level=0,
    )
    return data


def _dedupe_extracted(db, *, space_id: int, user_id: int, data: dict) -> dict:
    """同用户近期已有相同长期/用户正文时不再重复写入。"""
    recent = (
        db.query(Memory)
        .filter(
            Memory.space_id == space_id,
            Memory.user_id == user_id,
            Memory.kind.in_([KIND_LONG, KIND_USER]),
        )
        .order_by(Memory.id.desc())
        .limit(80)
        .all()
    )
    seen = {(row.kind, (row.content or "").strip()) for row in recent}

    def _fresh(kind: str, items: list) -> list:
        out = []
        for item in items or []:
            raw = (item if isinstance(item, str) else dump(item)).strip()
            text = _clean_memory_text(raw) if kind in {KIND_LONG, KIND_USER} else raw
            if not text or (kind, text) in seen:
                continue
            seen.add((kind, text))
            out.append(text)
        return out

    return {
        "short_term": list(data.get("short_term") or []),
        "long_term": _fresh(KIND_LONG, data.get("long_term") or []),
        "user_memory": _fresh(KIND_USER, data.get("user_memory") or []),
    }


def extract_and_store(
    db,
    *,
    space_id: int,
    user_id: int,
    dialogue: list[dict],
    summary_text: str,
    prior_summaries: list[str],
    source_kind: str,
    source_id: str,
    source_name: str,
    task_id: str,
    agent_id: int | None,
    parent_summary_id: int | None,
    level: int,
    retention_days: int = 30,
    model=None,
) -> Memory:
    """生成摘要记忆，并抽取短期/长期/用户记忆。"""
    extracted = _extract_layers(
        db,
        space_id=space_id,
        dialogue=dialogue,
        summary_text=summary_text,
        prior_summaries=prior_summaries or [],
        model=model,
    )
    cue_src = ""
    for item in reversed(dialogue or []):
        if item.get("role") == "user" and (item.get("content") or "").strip():
            cue_src = item.get("content") or ""
            break
    fallback = _fallback_extract(cue_src, summary_text, dialogue)

    def _pick(key: str, *, lasting: bool) -> list:
        chosen = []
        for item in extracted.get(key) or []:
            raw = (item if isinstance(item, str) else dump(item)).strip()
            text = _clean_memory_text(raw) if lasting else raw
            if lasting and text and not _is_lasting_candidate(text):
                continue
            if text:
                chosen.append(text)
        if chosen:
            if key == "short_term":
                return _ensure_short_anchors(chosen, cue_src, fallback.get(key) or [])
            return chosen
        return [x for x in (fallback.get(key) or []) if (not lasting or _is_lasting_candidate(x))]

    data = {
        "short_term": _pick("short_term", lasting=False),
        "long_term": _pick("long_term", lasting=True),
        "user_memory": _pick("user_memory", lasting=True),
    }
    summary_row = _store_extracted(
        db,
        data=data,
        space_id=space_id,
        user_id=user_id,
        agent_id=agent_id,
        source_kind=source_kind,
        source_id=source_id,
        source_name=source_name,
        task_id=task_id,
        retention_days=retention_days,
        summary_text=summary_text,
        dialogue=dialogue,
        prior_summaries=prior_summaries or [],
        write_summary=True,
        parent_summary_id=parent_summary_id,
        level=level,
    )
    return summary_row


def memory_detail(db, row: Memory) -> dict:
    refs = load(row.source_refs_json, {}) or {}
    produced = load(row.produced_json, {}) or {}

    # 旧数据未挂溯源时，尝试同 task 的压缩摘要补齐
    if row.kind in {KIND_SHORT, KIND_LONG, KIND_USER} and row.task_id and not (refs.get("dialogues") or refs.get("prior_short") or refs.get("prior_long")):
        summary = (
            db.query(Memory)
            .filter(
                Memory.space_id == row.space_id,
                Memory.user_id == row.user_id,
                Memory.kind == KIND_SUMMARY,
                Memory.task_id == str(row.task_id),
            )
            .order_by(Memory.id.desc())
            .first()
        )
        if summary is not None:
            refs = load(summary.source_refs_json, {}) or refs
            if row.kind == KIND_SHORT and not (produced.get("long") or produced.get("user")):
                batch = load(summary.produced_json, {}) or {}
                produced = {
                    "long": list(batch.get("long") or []),
                    "user": list(batch.get("user") or []),
                    "short": [sid for sid in (batch.get("short") or []) if sid != row.id],
                    "notes": list(batch.get("notes") or produced.get("notes") or []),
                }

    def _resolve(items) -> list[dict]:
        out = []
        for item in items or []:
            if isinstance(item, dict) and item.get("content"):
                out.append(
                    {
                        "id": item.get("id"),
                        "kind": item.get("kind") or "",
                        "content": item.get("content") or "",
                        "active": bool(item.get("active", True)),
                        "level": item.get("level") or 0,
                        "source_name": item.get("source_name") or "",
                        "created_at": item.get("created_at"),
                    }
                )
                continue
            item_id = item if isinstance(item, int) else (item.get("id") if isinstance(item, dict) else None)
            if not item_id:
                continue
            brief = _memory_brief(db.get(Memory, item_id))
            if brief:
                out.append(brief)
        return out

    long_ids = produced.get("long") or []
    user_ids = produced.get("user") or []
    short_ids = produced.get("short") or []

    detail = {
        "source_refs": {
            "dialogues": list(refs.get("dialogues") or []),
            "summaries": list(refs.get("summaries") or []),
            "prior_short": _resolve(refs.get("prior_short")),
            "prior_long": _resolve(refs.get("prior_long")),
            "prior_user": _resolve(refs.get("prior_user")),
        },
        "produced": {
            "short": _resolve(short_ids),
            "long": _resolve(long_ids),
            "user": _resolve(user_ids),
            "notes": list(produced.get("notes") or []),
        },
        "parent_summary_id": row.parent_summary_id,
        "level": row.level or 0,
        "active": bool(getattr(row, "active", True)),
    }
    if row.kind == KIND_USER and row.user_id:
        versions = profile_versions(db, space_id=row.space_id, user_id=row.user_id)
        detail["versions"] = [
            {
                "id": item.id,
                "level": item.level or 0,
                "active": bool(getattr(item, "active", True)),
                "content": item.content,
                "source_name": item.source_name or "",
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in versions
        ]
    return detail
