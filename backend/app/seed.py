"""空库演示数据。只在 users 表没有任何行时运行一次。

内置工具的 space_id 保持为空，审核直接是 approved，名称和 execute_builtin 对齐。
客服助手挂上计算器、知识检索和账本；退款专员是它的子智能体。
问答与计算是旧式意图图，用来保留 classify / tool / respond 这条路径。
"""

import uuid

from app.models import (
    Agent,
    AgentVersion,
    ContextPolicy,
    Document,
    EvalCase,
    EvalDataset,
    EvalRun,
    KnowledgeBase,
    LedgerAccount,
    Membership,
    ModelEndpoint,
    Space,
    Tool,
    User,
    Workflow,
)
from app.security import hash_password
from app.services.retrieval import index_document
from app.util import dump, load


def ensure_builtin_tools(db) -> None:
    """旧库不会重跑 seed。补齐缺失的内置工具，并给空参数补上说明。"""
    from app.models import Tool
    from app.services.builtins import CATALOG, schema_of

    specs = {
        "calculator": ("四则运算。金额和表达式由代码计算。", "builtin", "read", "low"),
        "knowledge.search": ("按权限检索本空间知识库，并返回引用。", "builtin", "read", "low"),
        "ledger.transfer": ("从现金账户扣减。高风险写入，必须审批。", "builtin", "write", "high"),
        "python.sandbox": ("在隔离进程里执行赋值表达式，禁止导入和调用。", "sandbox", "read", "medium"),
        "files.list": ("列出允许目录中指定文件夹的这一层文件，不读取内容。", "builtin", "read", "low"),
    }
    for name in CATALOG:
        row = db.query(Tool).filter(Tool.name == name, Tool.space_id.is_(None)).first()
        schema = dump(schema_of(name))
        if row is None:
            description, kind, access, risk = specs[name]
            _tool(
                db,
                name=name,
                description=description,
                kind=kind,
                access=access,
                risk=risk,
                owner="平台",
                environment="all",
                endpoint="",
                data_flow="internal",
                review_status="approved",
                parameters_json=schema,
                rate_limit_per_min=30,
                origin="platform",
            )
            continue
        saved = load(row.parameters_json, {})
        if not (saved.get("properties") or {}):
            row.parameters_json = schema


def _user(db, username, password, name, email=""):
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=name,
        email=email,
        is_platform_admin=username == "admin",
    )
    db.add(user)
    db.flush()
    return user


def _tool(db, **kwargs):
    row = Tool(**kwargs)
    db.add(row)
    db.flush()
    return row


def _agent(db, space, name, key, prompt, tool_ids, child_ids=None, require_plan=True, created_by=None):
    agent = Agent(
        space_id=space.id,
        name=name,
        key=key,
        workload_id="wl_" + uuid.uuid4().hex[:16],
        status="published",
        require_plan=require_plan,
        created_by=created_by,
    )
    db.add(agent)
    db.flush()
    version = AgentVersion(
        agent_id=agent.id,
        number=1,
        system_prompt=prompt,
        tool_ids_json=dump(tool_ids),
        child_ids_json=dump(child_ids or []),
        status="published",
        eval_passed=True,
    )
    db.add(version)
    db.flush()
    agent.stable_version_id = version.id
    return agent, version


def seed(db) -> None:
    admin = _user(db, "admin", "admin123", "平台管理员", "admin@limen.local")
    builder = _user(db, "builder", "builder123", "客服构建者", "builder@limen.local")
    operator = _user(db, "operator", "operator123", "客服值班", "operator@limen.local")
    viewer = _user(db, "viewer", "viewer123", "只读审计", "viewer@limen.local")
    finance = _user(db, "finance", "finance123", "财务构建者", "finance@limen.local")

    support = Space(name="客服中心", key="support", require_eval=True, token_budget=1_000_000)
    treasury = Space(name="财务部", key="finance", require_eval=True, token_budget=1_000_000)
    db.add_all([support, treasury])
    db.flush()

    for user, role in ((builder, "builder"), (operator, "operator"), (viewer, "viewer")):
        db.add(Membership(user_id=user.id, space_id=support.id, role=role))
    db.add(Membership(user_id=finance.id, space_id=treasury.id, role="builder"))

    for space in (support, treasury):
        db.add(
            ContextPolicy(
                space_id=space.id,
                deny_json=dump(["身份证号", "工资"]),
                mask_json=dump(["客户号"]),
                isolation="user,agent",
                retention_days=30,
            )
        )
        db.add(LedgerAccount(space_id=space.id, name="现金", balance=1000 if space.id == support.id else 500))

    db.add_all(
        [
            ModelEndpoint(
                name="rules-small",
                provider="rules",
                model="rules-small",
                task_types_json=dump(["classify", "extract"]),
                price_in=0.1,
                price_out=0.1,
                priority=1,
                origin="platform",
                created_by=admin.id,
            ),
            ModelEndpoint(
                name="rules-large",
                provider="rules",
                model="rules-large",
                task_types_json=dump(["reason"]),
                price_in=2,
                price_out=4,
                priority=2,
                origin="platform",
                created_by=admin.id,
            ),
        ]
    )
    specs = [
        ("calculator", "四则运算。金额和表达式由代码计算。", "builtin", "read", "low", "all", "", "internal"),
        ("knowledge.search", "按权限检索本空间知识库，并返回引用。", "builtin", "read", "low", "all", "", "internal"),
        ("ledger.transfer", "从现金账户扣减。高风险写入，必须审批。", "builtin", "write", "high", "all", "", "internal"),
        ("python.sandbox", "在隔离进程里执行赋值表达式，禁止导入和调用。", "sandbox", "read", "medium", "all", "", "internal"),
        ("files.list", "列出允许目录中指定文件夹的这一层文件，不读取内容。", "builtin", "read", "low", "all", "", "internal"),
    ]
    tools = {}
    for name, description, kind, access, risk, environment, endpoint, data_flow in specs:
        tools[name] = _tool(
            db,
            name=name,
            description=description,
            kind=kind,
            access=access,
            risk=risk,
            owner="平台",
            environment=environment,
            endpoint=endpoint,
            data_flow=data_flow,
            review_status="approved",
            parameters_json=dump({"type": "object"}),
            rate_limit_per_min=30,
            origin="platform",
            created_by=admin.id,
        )
    tools["files.list"].parameters_json = dump(
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    )

    base = KnowledgeBase(space_id=support.id, name="客服制度", created_by=admin.id)
    db.add(base)
    db.flush()
    public = Document(
        base_id=base.id,
        space_id=support.id,
        title="退款制度",
        content="退款须在签收后7天内申请。运费由买家承担。",
        visibility="space",
    )
    secret = Document(
        base_id=base.id,
        space_id=support.id,
        title="内部价格",
        content="内部底价42，仅构建者和管理员可见。",
        visibility="restricted",
        allowed_roles_json=dump(["builder", "admin"]),
    )
    db.add_all([public, secret])
    db.flush()
    index_document(db, public)
    index_document(db, secret)

    refund, _ = _agent(
        db,
        support,
        "退款专员",
        "refund-agent",
        "退款专员已接收。只处理退款问题。",
        [],
        require_plan=False,
        created_by=admin.id,
    )
    support_agent, support_version = _agent(
        db,
        support,
        "客服助手",
        "support-agent",
        "你是客服助手。金额计算必须走计算器，写入必须走账本。",
        [tools["calculator"].id, tools["knowledge.search"].id, tools["ledger.transfer"].id],
        [refund.id],
        require_plan=True,
        created_by=admin.id,
    )
    _agent(
        db,
        treasury,
        "报销助手",
        "finance-agent",
        "你是财务助手。计算交给代码。",
        [tools["calculator"].id],
        require_plan=True,
        created_by=admin.id,
    )
    db.add(
        Workflow(
            space_id=support.id,
            agent_id=support_agent.id,
            name="问答与计算",
            created_by=admin.id,
            definition_json=dump(
                {
                    "entry": "classify",
                    "nodes": [
                        {"id": "classify", "type": "classify"},
                        {"id": "calc", "type": "tool", "tool": "calculator"},
                        {"id": "reply", "type": "respond"},
                    ],
                    "edges": [
                        {"src": "classify", "dst": "calc", "when": "calculate"},
                        {"src": "calc", "dst": "reply"},
                        {"src": "classify", "dst": "reply", "when": "chat"},
                    ],
                }
            ),
        )
    )
    dataset = EvalDataset(space_id=support.id, name="客服冒烟")
    db.add(dataset)
    db.flush()
    db.add(EvalCase(dataset_id=dataset.id, input_text="退款规定是什么", expected="7天"))
    db.add(
        EvalRun(
            dataset_id=dataset.id,
            agent_id=support_agent.id,
            version_id=support_version.id,
            completion=1,
            quality=1,
            cost=0,
            passed=True,
            detail_json=dump([{"passed": True}]),
        )
    )
