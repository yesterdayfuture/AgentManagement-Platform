"""内置工具的说明和参数。

执行仍在 runtime.execute_builtin。这里只描述每个名称接受什么参数，
供工具详情展示，并在启动时补齐旧登记里空的 parameters_json。
"""

CATALOG = {
    "calculator": {
        "summary": "做四则运算。金额和表达式由代码计算，不交给模型心算。",
        "example": "计算 19*7",
        "parameters": [
            {"name": "expression", "type": "string", "required": False, "description": "四则表达式，例如 19*7。省略时从用户消息里提取。"},
        ],
    },
    "knowledge.search": {
        "summary": "按当前角色检索本空间知识库，并返回带来源的片段。",
        "example": "退款规定是什么",
        "parameters": [
            {"name": "query", "type": "string", "required": False, "description": "检索词。省略时使用整句消息。"},
        ],
    },
    "ledger.transfer": {
        "summary": "从本空间现金账户扣减。高风险写入会先挂起，等审批通过后才改余额。",
        "example": "转账 10 元",
        "parameters": [
            {"name": "amount", "type": "number", "required": True, "description": "扣减金额，必须大于 0，且不能超过现金余额。"},
        ],
    },
    "python.sandbox": {
        "summary": "在隔离进程里执行赋值表达式。不能导入、调用或访问属性。",
        "example": "result = 1 + 2",
        "parameters": [
            {"name": "code", "type": "string", "required": True, "description": "必须给 result 赋值，例如 result = 1 + 2。"},
        ],
    },
    "files.list": {
        "summary": "列出允许根目录下某个文件夹的这一层条目，不读取文件内容。",
        "example": "列出文件夹 box",
        "parameters": [
            {"name": "path", "type": "string", "required": False, "description": "相对允许根目录的文件夹。省略或写 . 时列出根目录。绝对路径也必须落在根目录内。"},
        ],
    },
}


def schema_of(name: str) -> dict:
    """把目录里的参数写成 JSON Schema，存进 tools.parameters_json。"""
    spec = CATALOG.get(name) or {}
    properties = {}
    required = []
    for item in spec.get("parameters") or []:
        properties[item["name"]] = {"type": item["type"], "description": item["description"]}
        if item.get("required"):
            required.append(item["name"])
    body = {"type": "object", "properties": properties}
    if required:
        body["required"] = required
    return body


def detail_of(name: str, stored: dict | None) -> dict:
    """详情优先用代码里的目录。未登记的名称则从已保存的 JSON Schema 读参数。"""
    spec = CATALOG.get(name)
    if spec:
        return {"summary": spec["summary"], "example": spec["example"], "parameters": spec["parameters"]}
    schema = stored or {}
    required = set(schema.get("required") or [])
    parameters = [
        {
            "name": key,
            "type": (value or {}).get("type") or "string",
            "required": key in required,
            "description": (value or {}).get("description") or "",
        }
        for key, value in (schema.get("properties") or {}).items()
    ]
    return {"summary": "", "example": "", "parameters": parameters}
