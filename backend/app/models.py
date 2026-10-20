"""表结构。

空间是隔离边界：智能体、知识、账本、工作流都属于某个空间。
space_id 为空的模型和工具是全局的，所有空间都能看见。
密钥、工具参数、版本上的工具编号和 MCP 编号都以 JSON 或密文放在 Text 列里。
智能体的提示词、工具、模型和 MCP 挂在 AgentVersion 上，发布和灰度选的是版本，不是智能体本身。
工作流定义在 definition_json：kind 为 agents 时按连线逐步执行，为 autonomous 时由调度者拆任务。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now():
    return datetime.utcnow()


class User(Base):
    """登录账号。external_id 留给单点登录，本地账号可以为空。"""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(120), default="")
    external_id: Mapped[str] = mapped_column(String(80), default="")
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Space(Base):
    """一个团队的运行空间。token_used 累加每次轨迹的令牌，到达 token_budget 后拒绝新的执行。"""
    __tablename__ = "spaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(64), unique=True)
    require_eval: Mapped[bool] = mapped_column(Boolean, default=True)
    token_budget: Mapped[int] = mapped_column(Integer, default=1_000_000)
    token_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DepartmentGrant(Base):
    """部门被允许使用的资源。kind 是 model、workflow、knowledge、tool。"""
    __tablename__ = "department_grants"
    __table_args__ = (UniqueConstraint("space_id", "kind", "resource_id", name="uq_department_grant"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    kind: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Membership(Base):
    """用户在某个部门里的角色：viewer、operator、builder、admin。admin 是部门管理员。"""
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "space_id", name="uq_member"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ContextPolicy(Base):
    """空间级上下文策略：禁止词、脱敏字段、记忆隔离维度和保留天数。"""
    __tablename__ = "context_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), unique=True)
    deny_json: Mapped[str] = mapped_column(Text, default="[]")
    mask_json: Mapped[str] = mapped_column(Text, default="[]")
    isolation: Mapped[str] = mapped_column(String(80), default="user,agent")
    retention_days: Mapped[int] = mapped_column(Integer, default=30)


class Agent(Base):
    """智能体身份。提示词和能力在版本表里。stable_version_id / gray_version_id 决定线上走哪一版。

    space_id 为空表示没有分配部门，只有平台管理员能在问答里使用。
    """
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("space_id", "key", name="uq_agent_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(80))
    workload_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    require_plan: Mapped[bool] = mapped_column(Boolean, default=True)
    stable_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gray_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gray_percent: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentVersion(Base):
    """一次可发布的快照。tool_ids_json、child_ids_json、mcp_ids_json 是编号列表。model_id 为空时按任务选模型。"""
    __tablename__ = "agent_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    number: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tool_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    child_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mcp_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    skill_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    eval_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Skill(Base):
    """部门技能。同一部门内 key 唯一。

    description 和 body 会进入模型的系统提示。scripts_json 只存文件名，
    文件在 backend/data/skills/{id}/，执行时复制进隔离容器。
    """
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("space_id", "key", name="uq_skill_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    scripts_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ModelEndpoint(Base):
    """模型接入点。provider 为 rules 时不访问外网。secret_id 指向密文，接口不回传密钥。"""
    __tablename__ = "model_endpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(20), default="rules")
    model: Mapped[str] = mapped_column(String(80), default="rules")
    base_url: Mapped[str] = mapped_column(String(200), default="")
    secret_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_types_json: Mapped[str] = mapped_column(Text, default="[]")
    price_in: Mapped[float] = mapped_column(Float, default=0.1)
    price_out: Mapped[float] = mapped_column(Float, default=0.2)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    origin: Mapped[str] = mapped_column(String(20), default="department")
    quota_limit: Mapped[float] = mapped_column(Float, default=0)
    quota_used: Mapped[float] = mapped_column(Float, default=0)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class Secret(Base):
    """密文。ciphertext 由 vault 加密，明文只在调用模型或 MCP 时短暂解密。"""
    __tablename__ = "secrets"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(80))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Tool(Base):
    """工具登记，不是函数本身。

    name 必须和 runtime.execute_builtin 里的实现一致，调用才会成功。
    space_id 为空表示平台内置的全局工具。界面新建的记录属于当前空间，审核前是 pending。
    kind 为 builtin / sandbox / http。http 只校验 allow.internal，不执行自定义代码。
    """
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_tool_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="builtin")
    access: Mapped[str] = mapped_column(String(10), default="read")
    risk: Mapped[str] = mapped_column(String(10), default="low")
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    owner: Mapped[str] = mapped_column(String(80), default="")
    environment: Mapped[str] = mapped_column(String(20), default="all")
    endpoint: Mapped[str] = mapped_column(String(200), default="")
    data_flow: Mapped[str] = mapped_column(String(20), default="internal")
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    review_note: Mapped[str] = mapped_column(Text, default="")
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    origin: Mapped[str] = mapped_column(String(20), default="department")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class McpServer(Base):
    """外部 MCP 服务连接。函数清单在远端，本表只存地址和令牌引用。"""
    __tablename__ = "mcp_servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(300))
    secret_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class ToolGrant(Base):
    __tablename__ = "tool_grants"
    __table_args__ = (UniqueConstraint("agent_id", "tool_id", name="uq_grant"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    tool_id: Mapped[int] = mapped_column(ForeignKey("tools.id"))


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(120))
    origin: Mapped[str] = mapped_column(String(20), default="department")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class Document(Base):
    """一篇知识。content 是解析后的全文，切块参数决定检索时怎么切开。"""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20), default="space")
    allowed_roles_json: Mapped[str] = mapped_column(Text, default="[]")
    source_name: Mapped[str] = mapped_column(String(240), default="")
    source_kind: Mapped[str] = mapped_column(String(20), default="text")
    strategy: Mapped[str] = mapped_column(String(20), default="paragraph")
    applied_strategy: Mapped[str] = mapped_column(String(20), default="paragraph")
    chunk_size: Mapped[int] = mapped_column(Integer, default=500)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=40)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)


class Memory(Base):
    """短期或长期记忆。读取时按策略里的隔离键过滤，过期记录不再返回。"""
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[str] = mapped_column(String(80), default="")
    kind: Mapped[str] = mapped_column(String(10), default="short")
    content: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Workflow(Base):
    """工作流。definition_json 的 kind 决定固定连线还是自主拆任务。enabled 为假时不能执行。

    release 区分草稿与已发布：操作者只能执行已发布图；构建者可测草稿。
    schedule_token_budget 为定时累计令牌上限，0 表示不另设上限（仍受部门额度约束）。
    """
    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    definition_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")
    release: Mapped[str] = mapped_column(String(20), default="draft")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_minutes: Mapped[int] = mapped_column(Integer, default=0)
    schedule_message: Mapped[str] = mapped_column(Text, default="")
    schedule_nodes_json: Mapped[str] = mapped_column(Text, default="[]")
    schedule_next_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    schedule_token_budget: Mapped[int] = mapped_column(Integer, default=0)
    schedule_token_used: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String(20), default="department")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class WorkflowRun(Base):
    """一次执行。cursor 是固定图的下一节点，或自主调度的下一任务下标。log_json 是步骤日志。"""
    __tablename__ = "workflow_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    workflow_id: Mapped[int] = mapped_column(Integer)
    trace_id: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="running")
    environment: Mapped[str] = mapped_column(String(20), default="prod")
    actor: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    output_text: Mapped[str] = mapped_column(Text, default="")
    cursor: Mapped[str] = mapped_column(String(80), default="")
    carry_text: Mapped[str] = mapped_column(Text, default="")
    log_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_ledger"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(40))
    balance: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class Approval(Base):
    """高风险写入的审批单。通过后才按 payload_json 执行原来的工具。"""
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    trace_id: Mapped[str] = mapped_column(String(40))
    agent_id: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    channel: Mapped[str] = mapped_column(String(20), default="web")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Trace(Base):
    """一次智能体或工作流执行的总记录。id 是十六进制字符串，不是自增整数。"""
    __tablename__ = "traces"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    space_id: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer)
    version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default="prod")
    status: Mapped[str] = mapped_column(String(20), default="running")
    input_text: Mapped[str] = mapped_column(Text, default="")
    output_text: Mapped[str] = mapped_column(Text, default="")
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    quality_vote: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    dialogue_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChatSession(Base):
    """一次模型问答。属于当前空间里的某一个用户，智能体可以中途更换。"""
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    space_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(120), default="新的问答")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChatMessage(Base):
    """会话中的一条发言。用户问题用于检索，助手回答保留轨迹编号和工具调用。"""
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(40), default="")
    tool_calls_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), default="")
    limited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TraceStep(Base):
    __tablename__ = "trace_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(40))
    idx: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    output_text: Mapped[str] = mapped_column(Text, default="")
    tool_args: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    cost_in: Mapped[float] = mapped_column(Float, default=0)
    cost_out: Mapped[float] = mapped_column(Float, default=0)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    secret_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DelegationHop(Base):
    __tablename__ = "delegation_hops"
    id: Mapped[int] = mapped_column(primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer)
    subject_user_id: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(80))
    credential: Mapped[str] = mapped_column(String(40), default="user-session")


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    __table_args__ = (UniqueConstraint("agent_id", "tool_id", "window_start", name="uq_rate"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer)
    tool_id: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(DateTime)
    count: Mapped[int] = mapped_column(Integer, default=0)


class AuditLog(Base):
    """管理操作留痕。只记录动作和对象，不记录密钥明文。"""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class EvalDataset(Base):
    __tablename__ = "eval_datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class EvalCase(Base):
    __tablename__ = "eval_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("eval_datasets.id"))
    input_text: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text)
    expected_tool: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="[]")
    completion: Mapped[float] = mapped_column(Float, default=0)
    quality: Mapped[float] = mapped_column(Float, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    detail_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
