"""送进模型之前的上下文边界。

身份证号整段换成拦截标记。手机号保留前三后四。策略里的 deny 词整段替换，
mask 字段只替换「字段名: 值」里的值。隔离键决定记忆按用户、智能体还是项目分开读。
"""

import re

from app.util import load

PHONE = re.compile(r"1[3-9]\d{9}")
ID_CARD = re.compile(r"\d{17}[\dXx]")


def apply_context_policy(text: str, policy) -> tuple[str, list[str]]:
    notes: list[str] = []
    masked = text or ""
    if ID_CARD.search(masked):
        masked = ID_CARD.sub("[已拦截]", masked)
        notes.append("身份证号已拦截")
    deny_words = load(policy.deny_json, []) if policy else []
    for word in deny_words:
        if word and word in masked:
            masked = masked.replace(word, "[已拦截]")
            notes.append(f"{word}禁止进入模型")
    if PHONE.search(masked):
        masked = PHONE.sub(lambda match: match.group(0)[:3] + "****" + match.group(0)[-4:], masked)
        notes.append("手机号已脱敏")
    for field in load(policy.mask_json, []) if policy else []:
        pattern = re.compile(rf"({re.escape(field)}\s*[:：]\s*)(\S+)")
        if pattern.search(masked):
            masked = pattern.sub(r"\1[已脱敏]", masked)
            notes.append(f"{field}已脱敏")
    return masked, notes


def isolation_keys(policy) -> set[str]:
    if not policy or not policy.isolation:
        return {"user", "agent"}
    return {part.strip() for part in policy.isolation.split(",") if part.strip()}
