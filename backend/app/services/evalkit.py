"""评测指标。

指标来自智能体评测综述里反复出现、并且能在一次运行后直接算出来的几项：
任务完成率、精确匹配、期望包含、工具命中、端到端耗时、调用费用、检索依据是否忠实。
不另开一个模型来打分。
"""

import re

METRICS = [
    {
        "key": "steps_present",
        "name": "分步拆解",
        "summary": "输出至少包含两条编号步骤。",
    },
    {
        "key": "model_online",
        "name": "真实模型响应",
        "summary": "模型调用成功且未回退到规则回复。",
    },
    {
        "key": "final_answer",
        "name": "最终答案",
        "summary": "提取输出末尾的 Answer: 值，与用例期望比较；用于分步推理任务。",
    },
    {
        "key": "contains",
        "name": "期望包含",
        "summary": "输出里要出现用例填写的期望文本。对应关键词包含检查。",
    },
    {
        "key": "exact",
        "name": "精确匹配",
        "summary": "去掉版本前缀后，输出与期望完全一致。对应 Exact Match。",
    },
    {
        "key": "completion",
        "name": "任务完成",
        "summary": "这次运行状态是完成。对应 Task Success Rate。",
    },
    {
        "key": "tool",
        "name": "工具命中",
        "summary": "用例写了期望工具时，轨迹里要出现这个工具。对应 Tool Inclusion。",
    },
    {
        "key": "latency",
        "name": "端到端耗时",
        "summary": "从收到问题到给出结果不超过 30 秒。对应 End-to-End Latency。",
    },
    {
        "key": "cost",
        "name": "调用费用",
        "summary": "单条用例花费不超过 1 元。对应成本效率。",
    },
    {
        "key": "grounded",
        "name": "依据忠实",
        "summary": "若调用了知识检索或网页搜索，期望文本必须出现在检索结果里。对应 Faithfulness。",
    },
]

KEYS = {item["key"] for item in METRICS}
_PREFIX = re.compile(r"^【v\d+】")
_RETRIEVAL = {"knowledge.search", "web.search"}
LATENCY_LIMIT_MS = 30_000
COST_LIMIT = 1


def names() -> dict[str, str]:
    return {item["key"]: item["name"] for item in METRICS}


def score_case(metrics: list[str], case, output: str, status: str, tool_names: list[str], tool_text: str, latency_ms: int, cost: float, model_online: bool = True) -> dict[str, bool]:
    body = _PREFIX.sub("", output or "").strip()
    scores = {}
    if "model_online" in metrics:
        scores["model_online"] = model_online
    if "contains" in metrics:
        scores["contains"] = bool(case.expected) and case.expected in (output or "")
    if "exact" in metrics:
        scores["exact"] = body == (case.expected or "").strip()
    if "final_answer" in metrics:
        match = re.search(r"(?:^|\n)\s*(?:Final\s+)?Answer\s*[:：]\s*([^\n]+)\s*$", body, re.IGNORECASE)
        scores["final_answer"] = bool(match) and match.group(1).strip().rstrip(".").casefold() == (case.expected or "").strip().casefold()
    if "steps_present" in metrics:
        scores["steps_present"] = len(re.findall(r"(?m)^\s*\d+[.)、]\s*\S", body)) >= 2
    if "completion" in metrics:
        scores["completion"] = status == "completed"
    if "tool" in metrics:
        wanted = (getattr(case, "expected_tool", "") or "").strip()
        scores["tool"] = (not wanted) or wanted in tool_names
    if "latency" in metrics:
        scores["latency"] = (latency_ms or 0) <= LATENCY_LIMIT_MS
    if "cost" in metrics:
        scores["cost"] = (cost or 0) <= COST_LIMIT
    if "grounded" in metrics:
        used = [name for name in tool_names if name in _RETRIEVAL]
        scores["grounded"] = case.expected in (tool_text or "") if used else case.expected in (output or "")
    return scores
