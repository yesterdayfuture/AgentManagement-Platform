"""把结构化数据存进 SQLite 的 Text 列。

模型里凡是 *_json 字段，写入用 dump，读出用 load。空串和 None 都当成“还没有值”，
返回调用方给的默认值，避免列表字段在旧数据上抛 JSON 错误。
"""

import json


def dump(value) -> str:
    """紧凑 JSON。中文不转义，方便审计和日志直接阅读。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load(value, default=None):
    """解析 JSON 列。空值返回 default，不把空串当成合法 JSON。"""
    if value in (None, ""):
        return default
    return json.loads(value)
