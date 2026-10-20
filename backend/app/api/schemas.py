"""接口请求体。字段名和前端提交的 JSON 一致，缺省值保持旧客户端仍能调用。"""

from pydantic import BaseModel, Field


class ChatSessionIn(BaseModel):
    agent_id: int
    title: str = ""


class ChatSessionPatch(BaseModel):
    title: str | None = None
    agent_id: int | None = None
    pinned: bool | None = None


class ChatMessageIn(BaseModel):
    message: str = ""
    continue_run: bool = False
    stream: bool = False


class LoginIn(BaseModel):
    username: str
    password: str


class SsoIn(BaseModel):
    external_id: str
    email: str
    name: str
    space_key: str
    ts: int
    signature: str


class ExchangeIn(BaseModel):
    agent_id: int


class MemberIn(BaseModel):
    username: str
    role: str
    space_id: int | None = None


class SpaceIn(BaseModel):
    name: str


class DepartmentIn(BaseModel):
    name: str
    model_ids: list[int] = Field(default_factory=list)
    workflow_ids: list[int] = Field(default_factory=list)
    knowledge_ids: list[int] = Field(default_factory=list)
    tool_ids: list[int] = Field(default_factory=list)
    skill_ids: list[int] = Field(default_factory=list)


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class QuotaIn(BaseModel):
    token_budget: int
    token_used: int | None = None


class PolicyIn(BaseModel):
    deny: list[str] = Field(default_factory=list)
    mask: list[str] = Field(default_factory=list)
    isolation: str = "user,agent"
    retention_days: int = 30


class AgentIn(BaseModel):
    name: str
    key: str
    system_prompt: str = ""
    require_plan: bool = True
    tool_names: list[str] = Field(default_factory=list)
    child_keys: list[str] = Field(default_factory=list)
    model_id: int | None = None
    mcp_ids: list[int] = Field(default_factory=list)
    skill_ids: list[int] = Field(default_factory=list)
    space_id: int | None = None
    unassigned: bool = False


class AgentPatch(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    require_plan: bool | None = None
    tool_names: list[str] | None = None
    child_keys: list[str] | None = None
    model_id: int | None = None
    mcp_ids: list[int] | None = None
    skill_ids: list[int] | None = None


class PublishIn(BaseModel):
    gray_percent: int = 100


class GrayIn(BaseModel):
    percent: int


class PromoteIn(BaseModel):
    target: str
    percent: int = 0


class SkillIn(BaseModel):
    name: str
    key: str
    description: str = ""
    body: str = ""
    script_name: str = ""
    script_source: str = ""
    enabled: bool = True


class SkillPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    body: str | None = None
    script_name: str | None = None
    script_source: str | None = None
    script_files: dict[str, str] | None = None
    enabled: bool | None = None


class NodeSetupIn(BaseModel):
    agent_id: int
    node_id: str = ""
    prompt: str | None = None
    tool_names: list[str] | None = None
    knowledge_ids: list[int] | None = None
    mcp_ids: list[int] | None = None
    skill_ids: list[int] | None = None


class DialogueTurn(BaseModel):
    role: str
    content: str = ""


class RunIn(BaseModel):
    message: str = ""
    environment: str = "prod"
    project_id: str = ""
    pause_after: int | None = None
    stream: bool = False
    nodes: list[NodeSetupIn] | None = None
    history: list[DialogueTurn] = Field(default_factory=list)
    resume_trace_id: str = ""


class ToolIn(BaseModel):
    name: str
    description: str = ""
    kind: str = "http"
    access: str = "read"
    risk: str = "low"
    owner: str = ""
    environment: str = "all"
    endpoint: str = ""
    data_flow: str = "internal"
    rate_limit_per_min: int = 30
    parameters: dict = Field(default_factory=dict)


class ToolPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    kind: str | None = None
    access: str | None = None
    risk: str | None = None
    owner: str | None = None
    environment: str | None = None
    endpoint: str | None = None
    data_flow: str | None = None
    rate_limit_per_min: int | None = None
    enabled: bool | None = None


class ReviewIn(BaseModel):
    decision: str
    comment: str = ""


class McpIn(BaseModel):
    name: str
    url: str
    token: str = ""


class McpPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    token: str | None = None
    enabled: bool | None = None


class McpProbe(BaseModel):
    url: str
    token: str = ""


class McpCall(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)


class ModelIn(BaseModel):
    name: str
    provider: str = "rules"
    model: str = "rules"
    base_url: str = ""
    secret_id: int | None = None
    api_key: str = ""
    task_types: list[str]
    price_in: float = 0.001
    price_out: float = 0.002
    priority: int = 10
    enabled: bool = True
    quota_limit: float = 0


class ModelPatch(BaseModel):
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    task_types: list[str] | None = None
    price_in: float | None = None
    price_out: float | None = None
    priority: int | None = None
    enabled: bool | None = None
    quota_limit: float | None = None


class SecretIn(BaseModel):
    name: str
    value: str


class BaseIn(BaseModel):
    name: str
    space_id: int | None = None


class DocIn(BaseModel):
    title: str
    content: str
    visibility: str = "space"
    allowed_roles: list[str] = Field(default_factory=list)
    source_name: str = ""
    source_kind: str = "text"
    strategy: str = "paragraph"
    chunk_size: int = 500
    chunk_overlap: int = 40


class SearchIn(BaseModel):
    query: str
    base_id: int | None = None


class MemoryIn(BaseModel):
    content: str
    kind: str = "long"
    space_id: int | None = None
    agent_id: int | None = None
    project_id: str = ""
    ttl_seconds: int | None = None


class MemoryImport(BaseModel):
    items: list[MemoryIn]


class WorkflowIn(BaseModel):
    name: str
    agent_id: int
    definition: dict
    enabled: bool = True
    release: str = "draft"


class DecisionIn(BaseModel):
    decision: str
    channel: str = "web"
    comment: str = ""


class InboundIn(BaseModel):
    approval_id: int
    decision: str
    channel: str = "feishu"
    comment: str = ""


class CaseIn(BaseModel):
    input_text: str
    expected: str
    expected_tool: str = ""


class DatasetIn(BaseModel):
    name: str
    cases: list[CaseIn] = Field(default_factory=list)


class EvalRunIn(BaseModel):
    agent_id: int | None = None
    workflow_id: int | None = None
    metrics: list[str] = Field(default_factory=lambda: ["contains"])


class SandboxIn(BaseModel):
    code: str


class FeedbackIn(BaseModel):
    vote: int


class ChatIn(BaseModel):
    model: str
    messages: list[dict]


class IdsIn(BaseModel):
    ids: list[int] = Field(default_factory=list)


class TraceIdsIn(BaseModel):
    ids: list[str] = Field(default_factory=list)


class DocPatch(BaseModel):
    title: str | None = None
    content: str | None = None
    visibility: str | None = None
    allowed_roles: list[str] | None = None
    source_name: str | None = None
    source_kind: str | None = None
    strategy: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None


class PreviewIn(BaseModel):
    content: str
    strategy: str = "auto"
    chunk_size: int = 500
    chunk_overlap: int = 40


class WorkflowPatch(BaseModel):
    name: str | None = None
    agent_id: int | None = None
    status: str | None = None
    release: str | None = None
    definition: dict | None = None
    enabled: bool | None = None
    schedule_enabled: bool | None = None
    schedule_minutes: int | None = None
    schedule_message: str | None = None
    schedule_nodes: list[dict] | None = None
    schedule_token_budget: int | None = None


class MemoryPatch(BaseModel):
    content: str | None = None
    kind: str | None = None


class CasePatch(BaseModel):
    input_text: str | None = None
    expected: str | None = None
    expected_tool: str | None = None


class LedgerIn(BaseModel):
    name: str
    balance: float = 0


class LedgerPatch(BaseModel):
    name: str | None = None
    balance: float | None = None


class SecretPatch(BaseModel):
    name: str | None = None
    value: str | None = None


class ApprovalPatch(BaseModel):
    comment: str | None = None
    name: str | None = None
    definition: dict | None = None

