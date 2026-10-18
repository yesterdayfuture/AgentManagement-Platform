import time
import zlib

from app.security import read_token, sign_sso
from tests.conftest import auth


def _agent_id(client, headers, key="support-agent"):
    rows = client.get("/api/agents", headers=headers).json()
    return next(row["id"] for row in rows if row["key"] == key)


def test_login_space_isolation_and_roles(client):
    headers, _space = auth(client, "builder")
    denied = client.get("/api/knowledge", headers={**headers, "X-Space-Id": "999"})
    assert denied.status_code == 403
    finance_headers, _finance = auth(client, "finance", space_key="finance")
    names = [item["name"] for item in client.get("/api/knowledge", headers=finance_headers).json()]
    assert names == []
    viewer, _space = auth(client, "viewer")
    publish = client.post("/api/agents/1/publish", headers=viewer, json={"gray_percent": 100})
    assert publish.status_code == 403
    run = client.post("/api/agents/1/run", headers=viewer, json={"message": "你好"})
    assert run.status_code == 403


def test_department_list_member_password_and_self_service(client):
    admin, support = auth(client, "admin")
    builder, _space = auth(client, "builder")
    builder_spaces = client.get("/api/me", headers={"Authorization": builder["Authorization"]}).json()["spaces"]
    assert [item["key"] for item in builder_spaces] == ["support"]
    created = client.post("/api/spaces", headers=admin, json={"name": "市场部"})
    assert created.status_code == 200, created.text
    market = created.json()
    admin_spaces = client.get("/api/me", headers={"Authorization": admin["Authorization"]}).json()["spaces"]
    assert any(item["name"] == "市场部" for item in admin_spaces)
    builder_again = client.get("/api/me", headers={"Authorization": builder["Authorization"]}).json()["spaces"]
    assert all(item["name"] != "市场部" for item in builder_again)
    denied = client.post("/api/spaces", headers=builder, json={"name": "不能建"})
    assert denied.status_code == 403
    visible = client.get("/api/spaces/current", headers={**admin, "X-Space-Id": str(market["id"])})
    assert visible.status_code == 200
    assert visible.json()["name"] == "市场部"
    added = client.post(
        "/api/spaces/current/members",
        headers={**admin, "X-Space-Id": str(support["id"])},
        json={"username": "li", "role": "operator"},
    )
    assert added.status_code == 200, added.text
    assert added.json()["initial_password"] == "li123"
    login = client.post("/api/auth/login", json={"username": "li", "password": "li123"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    changed = client.post(
        "/api/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": "li123", "new_password": "li1234"},
    )
    assert changed.status_code == 200, changed.text
    assert client.post("/api/auth/login", json={"username": "li", "password": "li123"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "li", "password": "li1234"}).status_code == 200


def test_agent_model_dropdown_uses_department_grants(client):
    admin, support = auth(client, "admin")
    created = client.post(
        "/api/models",
        headers=admin,
        json={"name": "未授权模型", "provider": "rules", "model": "ungranted-model", "task_types": ["reason"], "price_in": 1, "price_out": 1, "priority": 3},
    )
    assert created.status_code == 200, created.text
    model_id = created.json()["id"]
    usable = client.get("/api/models", headers=admin, params={"usable_space": support["id"]}).json()
    assert all(item["id"] != model_id for item in usable)
    listed = client.get("/api/models", headers=admin).json()
    assert any(item["id"] == model_id for item in listed)
    builder, _space = auth(client, "builder")
    denied = client.post(
        "/api/agents",
        headers=builder,
        json={"name": "越权模型", "key": "ungranted-agent", "model_id": model_id, "require_plan": False},
    )
    assert denied.status_code == 400
    assert "不能使用" in denied.text


def test_department_permissions_follow_admin_kind(client):
    admin, support = auth(client, "admin")
    finance, treasury = auth(client, "finance", space_key="finance")
    platform = client.post(
        "/api/models",
        headers=admin,
        json={"name": "平台模型", "provider": "rules", "model": "platform-model", "task_types": ["reason"], "price_in": 1, "price_out": 1, "priority": 8},
    )
    assert platform.status_code == 200, platform.text
    platform_id = platform.json()["id"]
    assert platform.json()["origin"] == "platform"
    before = client.get("/api/models?page=1", headers=finance).json()["items"]
    assert all(item["id"] != platform_id for item in before)
    added = client.post("/api/spaces/current/members", headers=admin, json={"username": "deptboss", "role": "admin"})
    assert added.status_code == 200, added.text
    boss, boss_space = auth(client, "deptboss", "deptboss123")
    assert boss_space["id"] == support["id"]
    profile = client.get("/api/me", headers={"Authorization": boss["Authorization"]}).json()
    assert profile["platform_admin"] is False
    assert [item["key"] for item in profile["spaces"]] == ["support"]
    builder_login, _builder_space = auth(client, "builder")
    assert client.post("/api/spaces/current/members", headers=builder_login, json={"username": "outsider", "role": "viewer"}).status_code == 403
    assert client.get("/api/departments?page=1", headers=builder_login).status_code == 403
    own = client.get("/api/departments?page=1", headers=boss).json()
    assert own["total"] == 1
    assert own["items"][0]["id"] == support["id"]
    assert client.get(f"/api/departments/{treasury['id']}", headers=boss).status_code == 404
    assert client.post("/api/departments", headers=boss, json={"name": "部门管理员新建"}).status_code == 403
    assert client.post("/api/spaces/current/members", headers=boss, json={"username": "another-admin", "role": "admin"}).status_code == 403
    local = client.post(
        "/api/models",
        headers=boss,
        json={"name": "部门模型", "provider": "rules", "model": "dept-model", "task_types": ["reason"], "price_in": 1, "price_out": 1, "priority": 9},
    )
    assert local.status_code == 200, local.text
    local_id = local.json()["id"]
    assert local.json()["origin"] == "department"
    builder, _space = auth(client, "builder")
    support_models = client.get("/api/models?page=1", headers=builder).json()["items"]
    assert any(item["id"] == local_id for item in support_models)
    assert all(item["id"] != platform_id for item in support_models)
    finance_models = client.get("/api/models?page=1", headers=finance).json()["items"]
    assert all(item["id"] != local_id for item in finance_models)
    detail = client.get(f"/api/departments/{treasury['id']}", headers=admin).json()
    opened = client.patch(
        f"/api/departments/{treasury['id']}",
        headers=admin,
        json={
            "name": detail["name"],
            "model_ids": detail["model_ids"] + [platform_id],
            "workflow_ids": detail["workflow_ids"],
            "knowledge_ids": detail["knowledge_ids"],
            "tool_ids": detail["tool_ids"],
        },
    )
    assert opened.status_code == 200, opened.text
    after = client.get("/api/models?page=1", headers=finance).json()["items"]
    assert any(item["id"] == platform_id for item in after)
    assert all(item["id"] != local_id for item in after)
    scoped = client.get(f"/api/models?page=1&usable_space={treasury['id']}", headers=admin).json()["items"]
    assert any(item["id"] == platform_id for item in scoped)
    assert all(item["id"] != local_id for item in scoped)
    assert client.get(f"/api/models?page=1&usable_space={support['id']}", headers=finance).status_code == 403
    people = client.get(f"/api/spaces/current/members?page=1&space_id={treasury['id']}", headers=admin).json()["items"]
    assert people and all(item["space_id"] == treasury["id"] for item in people)


def test_department_skill_grant_limits_who_can_see_it(client):
    admin, support = auth(client, "admin")
    finance, treasury = auth(client, "finance", space_key="finance")
    created = client.post(
        "/api/skills",
        headers=admin,
        json={"name": "客服核对", "key": "support-check", "description": "核对", "body": "只给客服", "enabled": True},
    )
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]
    hidden = client.get("/api/skills?page=1", headers=finance).json()["items"]
    assert all(item["id"] != skill_id for item in hidden)
    detail = client.get(f"/api/departments/{treasury['id']}", headers=admin).json()
    granted = client.patch(
        f"/api/departments/{treasury['id']}",
        headers=admin,
        json={
            "name": detail["name"],
            "model_ids": detail["model_ids"],
            "workflow_ids": detail["workflow_ids"],
            "knowledge_ids": detail["knowledge_ids"],
            "tool_ids": detail["tool_ids"],
            "skill_ids": detail["skill_ids"] + [skill_id],
        },
    )
    assert granted.status_code == 200, granted.text
    visible = client.get("/api/skills?page=1", headers=finance).json()["items"]
    assert any(item["id"] == skill_id and item["owned"] is False for item in visible)
    assert client.patch(f"/api/skills/{skill_id}", headers=finance, json={"body": "改掉"}).status_code == 404
    support_list = client.get("/api/skills?page=1", headers=admin).json()["items"]
    assert any(item["id"] == skill_id and item["owned"] is True for item in support_list)
    boss_added = client.post("/api/spaces/current/members", headers=admin, json={"username": "skillboss", "role": "admin"})
    assert boss_added.status_code == 200, boss_added.text
    boss, _space = auth(client, "skillboss", "skillboss123")
    foreign = client.post(
        "/api/skills",
        headers=finance,
        json={"name": "财务核对", "key": "finance-check", "description": "财务", "body": "只给财务", "enabled": True},
    )
    assert foreign.status_code == 200, foreign.text
    again = client.get("/api/departments/catalog", headers=boss).json()
    assert any(item["id"] == skill_id for item in again["skills"])
    assert all(item["id"] != foreign.json()["id"] for item in again["skills"])
    assert client.get("/api/departments/catalog", headers=finance).status_code == 403


def test_model_quota_history_and_operation_log(client):
    admin, _space = auth(client, "admin")
    created = client.post(
        "/api/models",
        headers=admin,
        json={
            "name": "限额模型",
            "provider": "rules",
            "model": "capped-model",
            "task_types": ["reason"],
            "price_in": 0,
            "price_out": 1,
            "priority": 0,
            "quota_limit": 100,
        },
    )
    assert created.status_code == 200, created.text
    model_id = created.json()["id"]
    assert created.json()["quota_remaining"] == 100
    detail = client.get(f"/api/departments/{_space['id']}", headers=admin).json()
    granted = client.patch(
        f"/api/departments/{_space['id']}",
        headers=admin,
        json={
            "name": detail["name"],
            "model_ids": detail["model_ids"] + [model_id],
            "workflow_ids": detail["workflow_ids"],
            "knowledge_ids": detail["knowledge_ids"],
            "tool_ids": detail["tool_ids"],
        },
    )
    assert granted.status_code == 200, granted.text
    builder, _space = auth(client, "builder")
    denied = client.patch(f"/api/models/{model_id}", headers=builder, json={"quota_limit": 5})
    assert denied.status_code == 403
    adjusted = client.patch(f"/api/models/{model_id}", headers=admin, json={"quota_limit": 1})
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["quota_limit"] == 1
    agent_id = _agent_id(client, admin)
    run = client.post(f"/api/agents/{agent_id}/run", headers=admin, json={"message": "你好，额度", "environment": "test"})
    assert run.status_code == 200, run.text
    calls = client.get(f"/api/models/{model_id}/calls", headers=admin).json()
    matched = [item for item in calls if "你好，额度" in item["input"] and item["username"] == "admin" and item["output"]]
    assert matched
    assert matched[0]["tokens_in"] > 0
    assert matched[0]["tokens_out"] > 0
    assert matched[0]["cost"] == round(matched[0]["cost_in"] + matched[0]["cost_out"], 6)
    summary = client.get("/api/observability/summary?scope=all", headers=admin).json()
    assert summary["quota_used_total"] > 0
    assert any(item["username"] == "admin" for item in summary["by_user"])
    assert any("你好，额度" in item["question"] for item in summary["hot_questions"])
    logs = client.get("/api/audit", headers=admin).json()
    assert any(item["target"] == "/api/models" for item in logs)


def test_platform_admin_sees_model_calls_from_another_department(client):
    finance, finance_space = auth(client, "finance", space_key="finance")
    admin, _support = auth(client, "admin", space_key="support")
    builder, _same = auth(client, "builder")
    created = client.post(
        "/api/agents",
        headers=finance,
        json={"name": "财务问答", "key": "finance-ask-model", "require_plan": False, "tool_names": [], "model_id": 1},
    )
    assert created.status_code == 200, created.text
    run = client.post(
        f"/api/agents/{created.json()['id']}/run",
        headers=finance,
        json={"message": "问答记到模型历史", "environment": "test"},
    )
    assert run.status_code == 200, run.text
    calls = client.get("/api/models/1/calls", headers=admin).json()
    matched = [item for item in calls if item["input"] == "问答记到模型历史"]
    assert matched
    assert matched[0]["department"] == finance_space["name"]
    hidden = client.get("/api/models/1/calls", headers=builder).json()
    assert all(item["input"] != "问答记到模型历史" for item in hidden)


def test_registry_review_and_minimum_exposure(client):
    headers, _space = auth(client, "builder")
    missing = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "空工具", "key": "empty-tool", "tool_names": ["not-registered"]},
    )
    assert missing.status_code == 400
    assert missing.json()["detail"] == "工具未注册"
    created = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "只读助手", "key": "read-agent", "tool_names": ["calculator"], "require_plan": False},
    )
    assert created.status_code == 200
    agent_id = created.json()["id"]
    blocked = client.post("/api/agents", headers=headers, json={"name": "x", "key": "read-agent"})
    assert blocked.status_code == 400
    write = client.post(
        f"/api/agents/{agent_id}/run",
        headers=headers,
        json={"message": "付款 10", "environment": "test"},
    )
    assert write.status_code == 200
    assert write.json()["status"] == "denied"


def test_external_tool_review_and_url_guard(client):
    headers, _space = auth(client, "builder")
    admin, _space = auth(client, "admin")
    tool = client.post(
        "/api/tools",
        headers=headers,
        json={
            "name": "crm.lookup",
            "kind": "http",
            "access": "read",
            "risk": "medium",
            "data_flow": "external",
            "endpoint": "https://allow.internal/ping",
            "owner": "集成组",
        },
    ).json()
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "集成", "key": "crm-agent", "tool_names": ["crm.lookup"], "require_plan": False},
    ).json()
    dataset = client.post("/api/evals/datasets", headers=headers, json={"name": "集成评测"}).json()
    client.post(
        f"/api/evals/datasets/{dataset['id']}/cases",
        headers=headers,
        json={"input_text": "计算 1+1", "expected": "2"},
    )
    # 工具未审核时，工作流不能调用。
    flow = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "外呼",
            "agent_id": agent["id"],
            "definition": {
                "entry": "call",
                "nodes": [{"id": "call", "type": "tool", "tool": "crm.lookup", "args": {"url": "https://allow.internal/ping"}}],
                "edges": [],
            },
        },
    ).json()
    denied = client.post(f"/api/workflows/{flow['id']}/run", headers=headers, json={"message": "查询", "environment": "test"})
    assert denied.json()["status"] == "denied"
    assert "审核" in denied.json()["output"]
    bare = client.post(f"/api/tools/{tool['id']}/review", headers=admin, json={"decision": "approved", "comment": ""})
    assert bare.status_code == 400
    reviewed = client.post(
        f"/api/tools/{tool['id']}/review",
        headers=admin,
        json={"decision": "approved", "comment": "只读客户主数据，不出域"},
    )
    assert reviewed.status_code == 200
    allowed = client.post(f"/api/workflows/{flow['id']}/run", headers=headers, json={"message": "查询", "environment": "test"})
    assert allowed.json()["status"] == "completed"
    meta = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "元数据",
            "agent_id": agent["id"],
            "definition": {
                "entry": "call",
                "nodes": [{"id": "call", "type": "tool", "tool": "crm.lookup", "args": {"url": "http://169.254.169.254/latest"}}],
                "edges": [],
            },
        },
    ).json()
    blocked = client.post(f"/api/workflows/{meta['id']}/run", headers=headers, json={"message": "查询", "environment": "test"})
    assert blocked.json()["status"] == "denied"
    assert "元数据" in blocked.json()["output"]


def test_approval_gateway_keeps_ledger_deterministic(client):
    headers, _space = auth(client, "builder")
    operator, _space = auth(client, "operator")
    agent_id = _agent_id(client, headers)
    too_big = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "付款 99999"})
    assert too_big.json()["status"] == "failed"
    assert "余额" in too_big.json()["output"]
    assert client.get("/api/ledger", headers=headers).json()[0]["balance"] == 1000
    suspended = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "付款 10"})
    assert suspended.json()["status"] == "suspended"
    assert client.get("/api/ledger", headers=headers).json()[0]["balance"] == 1000
    approval = client.get("/api/approvals", headers=operator).json()[0]
    assert approval["status"] == "pending"
    decided = client.post(
        f"/api/approvals/{approval['id']}/decision",
        headers=operator,
        json={"decision": "approved", "channel": "web", "comment": "值班确认"},
    )
    assert decided.status_code == 200
    assert client.get("/api/ledger", headers=headers).json()[0]["balance"] == 990
    again = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "付款 5"})
    approval_id = client.get("/api/approvals", headers=operator).json()[0]["id"]
    inbound = client.post(
        "/api/approvals/inbound",
        json={"approval_id": approval_id, "decision": "rejected", "channel": "feishu"},
        headers={"X-Channel-Secret": "channel-test"},
    )
    assert inbound.status_code == 200
    assert "补偿" in inbound.json()["output"]
    assert client.get("/api/ledger", headers=headers).json()[0]["balance"] == 990
    assert again.json()["status"] == "suspended"


def test_context_policy_and_trace(client):
    headers, _space = auth(client, "builder")
    agent_id = _agent_id(client, headers)
    message = "客户身份证号110101199001011234 手机13812345678 工资保密，退款规定是什么"
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": message})
    assert result.status_code == 200
    assert "7天" in result.json()["output"]
    trace = client.get(f"/api/traces/{result.json()['trace_id']}", headers=headers).json()
    blob = str(trace)
    assert "110101199001011234" not in blob
    assert "13812345678" not in blob
    assert "工资" not in trace["input"]
    kinds = [step["kind"] for step in trace["steps"]]
    assert "perceive" in kinds
    assert "policy" in kinds
    assert trace["steps"][0]["model"] == "rules-small"


def test_hybrid_calc_model_route_and_openai_api(client):
    headers, _space = auth(client, "builder")
    agent_id = _agent_id(client, headers)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 19*7"})
    assert result.json()["output"] == "【v1】133"
    trace = client.get(f"/api/traces/{result.json()['trace_id']}", headers=headers).json()
    assert [step["kind"] for step in trace["steps"][:2]] == ["perceive", "execute"]
    assert trace["steps"][1]["name"] == "calculator"
    assert "19*7" in trace["steps"][1]["tool_args"]
    assert result.json()["tool_calls"][0]["name"] == "calculator"
    assert "133" in result.json()["tool_calls"][0]["output"]
    chat = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "你好"})
    chat_trace = client.get(f"/api/traces/{chat.json()['trace_id']}", headers=headers).json()
    generate = next(step for step in chat_trace["steps"] if step["kind"] == "generate")
    assert generate["model"] == "rules-large"
    compat = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "support-agent", "messages": [{"role": "user", "content": "计算 6*7"}]},
    )
    assert compat.status_code == 200
    assert "42" in compat.json()["choices"][0]["message"]["content"]


def test_model_fallback_and_secret_not_leaked(client):
    headers, _space = auth(client, "builder")
    admin, _space = auth(client, "admin")
    secret = client.post("/api/secrets", headers=admin, json={"name": "openai", "value": "SUPERSECRET-VALUE"}).json()
    listed = client.get("/api/secrets", headers=admin).text
    assert "SUPERSECRET-VALUE" not in listed
    assert secret["name"] == "openai"
    client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "remote-reason",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "task_types": ["reason"],
            "price_in": 0,
            "price_out": 0,
            "priority": 0,
        },
    )
    agent_id = _agent_id(client, headers)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "你好呀"})
    trace = client.get(f"/api/traces/{result.json()['trace_id']}", headers=headers).json()
    generate = next(step for step in trace["steps"] if step["kind"] == "generate")
    assert generate["model"] == "remote-reason"
    assert generate["fallback"] is True
    assert "SUPERSECRET-VALUE" not in str(trace)


def test_gray_rollback_offline_and_eval_gate(client):
    headers, _space = auth(client, "builder")
    agent_id = _agent_id(client, headers)
    patched = client.patch(f"/api/agents/{agent_id}", headers=headers, json={"system_prompt": "灰度提示。"})
    assert patched.json()["versions"][-1]["number"] == 2
    blocked = client.post(f"/api/agents/{agent_id}/publish", headers=headers, json={"gray_percent": 0})
    assert blocked.status_code == 409
    dataset = client.post("/api/evals/datasets", headers=headers, json={"name": "灰度"}).json()
    client.post(
        f"/api/evals/datasets/{dataset['id']}/cases",
        headers=headers,
        json={"input_text": "计算 8*7", "expected": "56"},
    )
    report = client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=headers, json={"agent_id": agent_id})
    assert report.json()["passed"] is True
    staged = client.post(f"/api/agents/{agent_id}/publish", headers=headers, json={"gray_percent": 0})
    assert staged.status_code == 200
    stable = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 8*7"})
    assert stable.json()["output"].startswith("【v1】")
    client.post(f"/api/agents/{agent_id}/gray", headers=headers, json={"percent": 100})
    gray = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 8*7"})
    assert gray.json()["output"].startswith("【v2】")
    client.post(f"/api/agents/{agent_id}/rollback", headers=headers)
    rolled = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 8*7"})
    assert rolled.json()["output"].startswith("【v1】")
    offline = client.post(f"/api/agents/{agent_id}/offline", headers=headers)
    assert offline.status_code == 200
    assert any(item["name"] == "问答与计算" for item in offline.json()["references"])
    prod = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 1+1", "environment": "prod"})
    assert prod.status_code == 409
    tested = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 1+1", "environment": "test"})
    assert tested.status_code == 200


def test_promote_chosen_version_online(client):
    headers, _space = auth(client, "builder")
    agent_id = _agent_id(client, headers)
    client.patch(f"/api/agents/{agent_id}", headers=headers, json={"system_prompt": "指定上线。"})
    dataset = client.post("/api/evals/datasets", headers=headers, json={"name": "指定版本"}).json()
    client.post(
        f"/api/evals/datasets/{dataset['id']}/cases",
        headers=headers,
        json={"input_text": "计算 8*7", "expected": "56"},
    )
    assert client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=headers, json={"agent_id": agent_id}).json()["passed"] is True
    staged = client.post(f"/api/agents/{agent_id}/publish", headers=headers, json={"gray_percent": 0})
    assert staged.json()["stable_version"] == 1
    assert staged.json()["gray_version"] == 2
    detail = staged.json()["versions"][-1]
    assert detail["system_prompt"] == "指定上线。"
    assert "model_name" in detail
    promoted = client.post(f"/api/agents/{agent_id}/versions/2/promote", headers=headers, json={"target": "stable"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["stable_version"] == 2
    assert promoted.json()["gray_version"] is None
    live = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 8*7"})
    assert live.json()["output"].startswith("【v2】")
    client.post(f"/api/agents/{agent_id}/online", headers=headers)
    still = client.get(f"/api/agents/{agent_id}", headers=headers).json()
    assert still["stable_version"] == 2
    draft = client.patch(f"/api/agents/{agent_id}", headers=headers, json={"system_prompt": "还不能上。"})
    assert draft.json()["versions"][-1]["number"] == 3
    blocked = client.post(f"/api/agents/{agent_id}/versions/3/promote", headers=headers, json={"target": "stable"})
    assert blocked.status_code == 409


def test_memory_isolation_export_and_expiry(client):
    headers, _space = auth(client, "builder")
    other, _space = auth(client, "operator")
    created = client.post("/api/memories", headers=headers, json={"kind": "long", "content": "客户偏好绿色包装"})
    assert created.status_code == 200
    agent_id = _agent_id(client, headers)
    answer = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "你好"})
    assert "客户偏好绿色包装" in answer.json()["output"]
    other_answer = client.post(f"/api/agents/{agent_id}/run", headers=other, json={"message": "你好"})
    assert "客户偏好绿色包装" not in other_answer.json()["output"]
    exported = client.get("/api/memories/export", headers=headers).json()["items"]
    assert any(item["content"] == "客户偏好绿色包装" for item in exported)
    assert client.get("/api/memories/export", headers=other).json()["items"] == [] or all(
        item["content"] != "客户偏好绿色包装" for item in client.get("/api/memories/export", headers=other).json()["items"]
    )
    client.post("/api/memories", headers=headers, json={"kind": "long", "content": "马上过期", "ttl_seconds": 0})
    contents = [item["content"] for item in client.get("/api/memories", headers=headers).json()]
    assert "马上过期" not in contents


def test_subagent_delegation_sso_and_token_exchange(client):
    headers, space = auth(client, "builder")
    agent_id = _agent_id(client, headers)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "转交退款专员 订单已签收"})
    assert result.status_code == 200, result.text
    assert "退款专员已接收" in result.json()["output"]
    hops = client.get(f"/api/traces/{result.json()['trace_id']}", headers=headers).json()["hops"]
    assert any(hop["credential"] == "delegation" for hop in hops)
    exchanged = client.post("/api/auth/token-exchange", headers=headers, json={"agent_id": agent_id})
    token = exchanged.json()["access_token"]
    claims = read_token(token)
    assert claims["typ"] == "delegation"
    assert claims["space_id"] == space["id"]
    assert claims["exp"] - int(time.time()) <= 300
    delegated = client.post(
        f"/api/agents/{agent_id}/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "计算 4*5"},
    )
    assert delegated.status_code == 200
    assert "20" in delegated.json()["output"]
    delegated_hops = client.get(f"/api/traces/{delegated.json()['trace_id']}", headers=headers).json()["hops"]
    assert delegated_hops[0]["credential"] == "exchanged"
    refund_id = _agent_id(client, headers, "refund-agent")
    wrong = client.post("/api/auth/token-exchange", headers=headers, json={"agent_id": refund_id}).json()["access_token"]
    mismatch = client.post(
        f"/api/agents/{agent_id}/run",
        headers={"Authorization": f"Bearer {wrong}"},
        json={"message": "计算 1+1"},
    )
    assert mismatch.status_code == 403
    ts = int(time.time())
    signature = sign_sso("e-9", "new@corp", "support", ts)
    sso = client.post(
        "/api/auth/sso",
        json={"external_id": "e-9", "email": "new@corp", "name": "新同事", "space_key": "support", "ts": ts, "signature": signature},
    )
    assert sso.status_code == 200
    bad = client.post(
        "/api/auth/sso",
        json={"external_id": "e-9", "email": "new@corp", "name": "新同事", "space_key": "support", "ts": ts, "signature": "0" * 64},
    )
    assert bad.status_code == 401


def test_sandbox_workflow_breaker_quota_and_observability(client):
    headers, _space = auth(client, "builder")
    admin, _space = auth(client, "admin")
    ok = client.post("/api/sandbox/run", headers=headers, json={"code": "result = 6 * 7"})
    assert ok.json()["output"] == "42"
    blocked = client.post("/api/sandbox/run", headers=headers, json={"code": "import os\nresult = 1"})
    assert blocked.status_code == 400
    assert "blocked" in blocked.json()["detail"]
    agent_id = _agent_id(client, headers)
    tools = client.get("/api/tools", headers=admin).json()
    calculator = next(item for item in tools if item["name"] == "calculator")
    client.patch(f"/api/tools/{calculator['id']}", headers=admin, json={"rate_limit_per_min": 1})
    limited = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "限流",
            "agent_id": agent_id,
            "definition": {
                "entry": "a",
                "nodes": [
                    {"id": "a", "type": "tool", "tool": "calculator", "args": {"expression": "1+1"}},
                    {"id": "b", "type": "tool", "tool": "calculator", "args": {"expression": "2+2"}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            },
        },
    ).json()
    limited_run = client.post(f"/api/workflows/{limited['id']}/run", headers=headers, json={"message": "算", "environment": "test"})
    assert limited_run.json()["status"] == "blocked"
    assert "频率" in limited_run.json()["output"]
    client.patch(f"/api/tools/{calculator['id']}", headers=admin, json={"rate_limit_per_min": 30})
    loop = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "循环",
            "agent_id": agent_id,
            "definition": {
                "entry": "a",
                "nodes": [{"id": "a", "type": "tool", "tool": "calculator", "args": {"expression": "1+1"}}],
                "edges": [{"src": "a", "dst": "a"}],
            },
        },
    ).json()
    loop_run = client.post(f"/api/workflows/{loop['id']}/run", headers=headers, json={"message": "算", "environment": "test"})
    assert loop_run.json()["status"] == "blocked"
    assert "熔断" in loop_run.json()["output"]
    client.put("/api/spaces/current/quota", headers=admin, json={"token_budget": 1, "token_used": 1})
    quota = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "你好"})
    assert quota.status_code == 429
    client.put("/api/spaces/current/quota", headers=admin, json={"token_budget": 1000000, "token_used": 0})
    client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 2*3", "environment": "staging"})
    finance, _space = auth(client, "finance", space_key="finance")
    finance_agent = _agent_id(client, finance, "finance-agent")
    client.post(f"/api/agents/{finance_agent}/run", headers=finance, json={"message": "计算 2*3"})
    overview = client.get("/api/observability/summary?scope=all", headers=admin).json()
    assert overview["traffic"] >= 2
    assert "latency_ms" in overview and "quality" in overview
    assert len(overview["by_department"]) == 2
    assert any(item["model"] == "rules-small" for item in overview["by_model"])
    assert overview["resources"]["tools"] >= 5
    assert overview["resources"]["knowledge_bases"] >= 1
    assert overview["resources"]["documents"] >= 1
    assert overview["resources"]["memories"] >= 1
    own = client.get("/api/observability/summary?scope=all", headers=headers).json()
    assert len(own["by_department"]) == 1
    info = client.get("/api/system").json()
    assert info["mode"] == "private"
    assert info["gateways"] == ["model", "tool"]


def test_knowledge_acl_and_bucket(client):
    headers, _space = auth(client, "builder")
    operator, _space = auth(client, "operator")
    hidden = client.post("/api/knowledge/search", headers=operator, json={"query": "内部底价"})
    assert hidden.json()["hits"] == []
    visible = client.post("/api/knowledge/search", headers=headers, json={"query": "内部底价"})
    assert visible.json()["hits"]
    assert "42" in visible.json()["hits"][0]["excerpt"]
    me = client.get("/api/me", headers=headers).json()
    bucket = zlib.crc32(str(me["id"]).encode()) % 100
    assert 0 <= bucket <= 99


def test_paging_filters_and_model_connection(client):
    headers, _space = auth(client, "builder")
    created = client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "keyed-model",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "base_url": "https://example.internal/v1",
            "api_key": "sk-live-secret",
            "task_types": ["reason"],
            "price_in": 9,
            "price_out": 9,
            "priority": 80,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["has_api_key"] is True
    assert body["base_url"] == "https://example.internal/v1"
    assert "sk-live-secret" not in created.text
    fuzzy = client.get("/api/models", headers=headers, params={"page": 1, "page_size": 5, "q": "keyed"}).json()
    assert fuzzy["total"] >= 1
    assert fuzzy["items"][0]["name"] == "keyed-model"
    assert "sk-live-secret" not in str(fuzzy)
    exact = client.get("/api/models", headers=headers, params={"page": 1, "exact_field": "name", "exact": "keyed-model"}).json()
    assert exact["total"] == 1
    future = client.get("/api/models", headers=headers, params={"page": 1, "q": "keyed", "time_from": "2099-01-01T00:00"}).json()
    assert future["total"] == 0
    patched = client.patch(
        f"/api/models/{body['id']}",
        headers=headers,
        json={"base_url": "https://relay.internal/v1", "api_key": ""},
    )
    assert patched.json()["base_url"] == "https://relay.internal/v1"
    assert patched.json()["has_api_key"] is True
    removed = client.delete(f"/api/models/{body['id']}", headers=headers)
    assert removed.status_code == 200
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "可删助手", "key": "disposable-agent", "tool_names": ["calculator"], "require_plan": False},
    ).json()
    other = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "另一助手", "key": "disposable-two", "tool_names": ["calculator"], "require_plan": False},
    ).json()
    page = client.get("/api/agents", headers=headers, params={"page": 1, "page_size": 2, "exact_field": "status", "exact": "draft"}).json()
    assert page["page_size"] == 2
    assert page["total"] >= 2
    bulk = client.post("/api/agents/bulk-delete", headers=headers, json={"ids": [agent["id"], other["id"]]})
    assert bulk.status_code == 200
    assert set(bulk.json()["deleted"]) == {agent["id"], other["id"]}
    summary = client.get("/api/observability/summary", headers=headers, params={"time_from": "2099-01-01"}).json()
    assert summary["traffic"] == 0
    assert "by_day" in summary and "by_status" in summary


def _docx(paragraphs):
    import io
    import zipfile
    from xml.sax.saxutils import escape

    body = "".join(f"<w:p><w:r><w:t>{escape(item)}</w:t></w:r></w:p>" for item in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def _pdf(text):
    stream = f"BT ({text}) Tj ET".encode()
    body = (
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj\n"
        + f"4 0 obj<< /Length {len(stream)} >>stream\n".encode()
        + stream
        + b"\nendstream\nendobj\n"
    )
    return b"%PDF-1.1\n" + body + b"trailer<< /Root 1 0 R >>\n%%EOF\n"


def test_knowledge_upload_parse_and_chunk(client):
    headers, _space = auth(client, "builder")
    operator, _space = auth(client, "operator")
    denied = client.post(
        "/api/knowledge/parse",
        headers=operator,
        files={"file": ("note.txt", "退款须在七天内".encode(), "text/plain")},
    )
    assert denied.status_code == 403
    markdown = "# 退款\n\n签收后七天内可以退款。\n\n# 运费\n\n运费由买家承担。\n"
    parsed = client.post(
        "/api/knowledge/parse",
        headers=headers,
        files={"file": ("制度.md", markdown.encode(), "text/markdown")},
    )
    assert parsed.status_code == 200, parsed.text
    body = parsed.json()
    assert body["source_kind"] == "md"
    assert body["suggested"] == "heading"
    assert "签收后七天内可以退款" in body["content"]
    preview = client.post(
        "/api/knowledge/preview",
        headers=headers,
        json={"content": body["content"], "strategy": "heading", "chunk_size": 200, "chunk_overlap": 0},
    ).json()
    assert preview["applied"] == "heading"
    assert preview["count"] == 2
    base_id = client.get("/api/knowledge", headers=headers).json()[0]["id"]
    saved = client.post(
        f"/api/knowledge/{base_id}/documents",
        headers=headers,
        json={
            "title": body["title"],
            "content": body["content"],
            "strategy": "auto",
            "chunk_size": 200,
            "chunk_overlap": 0,
            "source_name": body["source_name"],
            "source_kind": body["source_kind"],
        },
    )
    assert saved.status_code == 200, saved.text
    stored = saved.json()
    assert stored["applied_strategy"] == "heading"
    assert stored["chunk_count"] == 2
    assert "sk-" not in stored["content"]
    found = client.post("/api/knowledge/search", headers=headers, json={"query": "运费买家"}).json()
    assert any("运费" in hit["excerpt"] for hit in found["hits"])
    docx = client.post(
        "/api/knowledge/parse",
        headers=headers,
        files={"file": ("价格.docx", _docx(["内部说明", "底价只对构建者开放"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert docx.status_code == 200
    assert "底价只对构建者开放" in docx.json()["content"]
    pdf = client.post(
        "/api/knowledge/parse",
        headers=headers,
        files={"file": ("notice.pdf", _pdf("refund within seven days"), "application/pdf")},
    )
    assert pdf.status_code == 200, pdf.text
    assert "seven days" in pdf.json()["content"]
    page = client.post(
        "/api/knowledge/preview",
        headers=headers,
        json={"content": "甲" * 120, "strategy": "fixed", "chunk_size": 50, "chunk_overlap": 10},
    ).json()
    assert page["count"] >= 3
    assert page["applied"] == "fixed"
    empty = client.post("/api/knowledge/parse", headers=headers, files={"file": ("empty.txt", b"   ", "text/plain")})
    assert empty.status_code == 400


def test_agent_workflow_handoff_pause_and_separate_runs(client):
    headers, _space = auth(client, "builder")
    agents = client.get("/api/agents", headers=headers).json()
    first = next(item for item in agents if item["key"] == "support-agent")
    second = next(item for item in agents if item["key"] == "refund-agent")
    created = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "客服交给退款",
            "agent_id": first["id"],
            "definition": {
                "kind": "agents",
                "entry": "a",
                "nodes": [
                    {"id": "a", "type": "agent", "agent_id": first["id"], "x": 20, "y": 20},
                    {"id": "b", "type": "agent", "agent_id": second["id"], "x": 220, "y": 20},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            },
        },
    )
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    paused = client.post(
        f"/api/workflows/{flow_id}/run",
        headers=headers,
        json={"message": "计算 19*7", "environment": "test", "pause_after": 1},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    run_id = paused.json()["run_id"]
    detail = client.get(f"/api/workflow-runs/{run_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["logs"][0]["agent"] == "客服助手"
    assert "133" in body["logs"][0]["output"]
    assert body["duration_ms"] >= 0
    resumed = client.post(f"/api/workflows/{flow_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["run_id"] == run_id
    finished = client.get(f"/api/workflow-runs/{run_id}", headers=headers).json()
    assert finished["logs"][-1]["event"] == "调用"
    assert finished["logs"][-1]["agent"] == "退款专员"
    assert "133" in finished["logs"][-1]["input"]
    again = client.post(f"/api/workflows/{flow_id}/run", headers=headers, json={"message": "你好", "environment": "test"})
    assert again.json()["run_id"] != run_id
    listed = client.get(
        "/api/workflow-runs",
        headers=headers,
        params={"page": 1, "exact_field": "workflow_id", "exact": str(flow_id)},
    ).json()
    assert listed["total"] == 2
    held = client.post(
        f"/api/workflows/{flow_id}/run",
        headers=headers,
        json={"message": "计算 2*3", "environment": "test", "pause_after": 1},
    )
    stopped = client.post(f"/api/workflows/{flow_id}/stop", headers=headers)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"
    client.patch(f"/api/workflows/{flow_id}", headers=headers, json={"enabled": False})
    denied = client.post(f"/api/workflows/{flow_id}/run", headers=headers, json={"message": "你好", "environment": "test"})
    assert denied.status_code == 409
    assert "未启用" in denied.text


def test_autonomous_workflow_splits_and_assigns(client):
    headers, _space = auth(client, "builder")
    agents = client.get("/api/agents", headers=headers).json()
    lead = next(item for item in agents if item["key"] == "support-agent")
    created_child = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "接待", "key": "greeter-agent", "system_prompt": "接待已收到。", "require_plan": False},
    )
    assert created_child.status_code == 200, created_child.text
    child = created_child.json()
    created = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "自主拆分",
            "agent_id": lead["id"],
            "definition": {
                "kind": "autonomous",
                "entry": "lead",
                "nodes": [
                    {"id": "lead", "type": "agent", "agent_id": lead["id"]},
                    {"id": "hand", "type": "agent", "agent_id": child["id"]},
                ],
                "edges": [{"src": "lead", "dst": "hand"}],
            },
        },
    )
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    paused = client.post(
        f"/api/workflows/{flow_id}/run",
        headers=headers,
        json={"message": "计算 19*7；请接待说你好", "environment": "test", "pause_after": 1},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    run_id = paused.json()["run_id"]
    detail = client.get(f"/api/workflow-runs/{run_id}", headers=headers).json()
    plan = detail["logs"][0]
    assert plan["event"] == "规划"
    assert "计算 19*7" in plan["output"]
    assert "接待" in plan["output"]
    tool = next(item for item in detail["logs"] if item["event"] == "工具")
    assert tool["tool"] == "calculator"
    assert "19*7" in tool["input"]
    assert "133" in tool["output"]
    done = next(item for item in detail["logs"] if item["event"] == "自己执行")
    assert "133" in done["output"]
    resumed = client.post(f"/api/workflows/{flow_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed"
    finished = client.get(f"/api/workflow-runs/{run_id}", headers=headers).json()
    assert finished["logs"][-1]["event"] == "分发"
    assert finished["logs"][-1]["agent"] == "接待"
    assert "接待已收到" in finished["output"]


def test_workflow_node_prompt_override(client):
    headers, _space = auth(client, "builder")
    lead = _agent_id(client, headers)
    created = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "节点配置",
            "agent_id": lead,
            "definition": {
                "kind": "agents",
                "entry": "a",
                "nodes": [{"id": "a", "type": "agent", "agent_id": lead, "prompt": "节点提示已生效", "tool_names": [], "knowledge_ids": []}],
                "edges": [],
            },
        },
    )
    assert created.status_code == 200, created.text
    run = client.post(
        f"/api/workflows/{created.json()['id']}/run",
        headers=headers,
        json={"message": "你好", "environment": "test"},
    )
    assert run.status_code == 200, run.text
    assert "节点提示已生效" in run.json()["output"]


def test_agent_selects_model_and_mcp_tools(client):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.append(self.headers.get("Authorization"))
            method = body.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("mcp-session-id", "sess")
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "stub"}, "capabilities": {}}
            elif method == "tools/list":
                result = {"tools": [{"name": "ping", "description": "应答", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "pong"}]}
            else:
                result = {}
            payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("mcp-session-id", "sess")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers, _space = auth(client, "builder")
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        operator, _space = auth(client, "operator")
        denied = client.post("/api/mcp/probe", headers=operator, json={"url": url, "token": "secret-token"})
        assert denied.status_code == 403
        blocked = client.post("/api/mcp/probe", headers=headers, json={"url": "http://169.254.169.254/latest"})
        assert blocked.status_code == 400
        probed = client.post("/api/mcp/probe", headers=headers, json={"url": url, "token": "secret-token"})
        assert probed.status_code == 200, probed.text
        assert probed.json()["tools"][0]["name"] == "ping"
        created = client.post("/api/mcp", headers=headers, json={"name": "本地应答", "url": url, "token": "secret-token"})
        assert created.status_code == 200, created.text
        server_id = created.json()["id"]
        assert created.json()["has_token"] is True
        assert "secret-token" not in created.text
        listed = client.get(f"/api/mcp/{server_id}/tools", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["tools"][0]["description"] == "应答"
        called = client.post(f"/api/mcp/{server_id}/call", headers=headers, json={"name": "ping", "arguments": {}})
        assert called.status_code == 200, called.text
        assert called.json()["text"] == "pong"
        models = client.get("/api/models", headers=headers).json()
        model_id = next(row["id"] for row in models if row["name"] == "rules-large")
        agent = client.post(
            "/api/agents",
            headers=headers,
            json={
                "name": "选型助手",
                "key": "picked-agent",
                "tool_names": ["calculator"],
                "model_id": model_id,
                "mcp_ids": [server_id],
                "require_plan": False,
            },
        )
        assert agent.status_code == 200, agent.text
        version = agent.json()["versions"][-1]
        assert version["model_id"] == model_id
        assert version["mcp_ids"] == [server_id]
        assert "calculator" in version["tools"]
        kept = client.patch(f"/api/agents/{agent.json()['id']}", headers=headers, json={"system_prompt": "保持选型。"})
        assert kept.status_code == 200, kept.text
        latest = kept.json()["versions"][-1]
        assert latest["model_id"] == model_id
        assert latest["mcp_ids"] == [server_id]
        busy = client.delete(f"/api/mcp/{server_id}", headers=headers)
        assert busy.status_code == 409
        assert "Bearer secret-token" in seen
    finally:
        server.shutdown()
        server.server_close()


def test_builtin_tool_detail_lists_parameters(client):
    headers, _space = auth(client, "builder")
    rows = client.get("/api/tools", headers=headers).json()
    calculator = next(item for item in rows if item["name"] == "calculator")
    names = [item["name"] for item in calculator["detail"]["parameters"]]
    assert names == ["expression"]
    assert "四则" in calculator["detail"]["parameters"][0]["description"]
    files = next(item for item in rows if item["name"] == "files.list")
    assert files["detail"]["parameters"][0]["name"] == "path"


def test_files_list_reads_only_the_allowed_root(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FILES_LIST_ROOT", str(tmp_path))
    folder = tmp_path / "box"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    headers, _space = auth(client, "builder")
    created = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "文件员",
            "key": "file-agent",
            "system_prompt": "文件员已接收。",
            "require_plan": False,
            "tool_names": ["files.list"],
        },
    )
    assert created.status_code == 200, created.text
    listed = client.post(
        f"/api/agents/{created.json()['id']}/run",
        headers=headers,
        json={"message": "列出文件夹 box", "environment": "test"},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["status"] == "completed"
    assert "a.txt" in listed.json()["output"]
    blocked = client.post(
        f"/api/agents/{created.json()['id']}/run",
        headers=headers,
        json={"message": "列出文件夹 /etc", "environment": "test"},
    )
    assert blocked.json()["status"] == "denied"
    assert "允许目录" in blocked.json()["output"]


def test_platform_admin_sees_every_department(client):
    admin, support = auth(client, "admin")
    finance, treasury = auth(client, "finance", space_key="finance")
    builder, _space = auth(client, "builder")
    agents = client.get("/api/agents", headers=admin).json()
    keys = {row["key"] for row in agents}
    assert {"support-agent", "finance-agent"} <= keys
    support_row = next(row for row in agents if row["key"] == "support-agent")
    assert support_row["department"] == "客服中心"
    assert support_row["creator"]
    own = {row["key"] for row in client.get("/api/agents", headers=builder).json()}
    assert "finance-agent" not in own
    added = client.post(
        "/api/spaces/current/members",
        headers=admin,
        json={"username": "market-user", "role": "viewer", "space_id": treasury["id"]},
    )
    assert added.status_code == 200, added.text
    assert added.json()["department"] == "财务部"
    assert added.json()["initial_password"] == "market-user123"
    members = client.get("/api/spaces/current/members?page=1&page_size=50", headers=admin).json()["items"]
    assert any(item["username"] == "market-user" and item["department"] == "财务部" for item in members)
    platform_rows = [item for item in members if item["username"] == "admin"]
    assert len(platform_rows) == 1
    assert platform_rows[0]["role"] == "platform"
    assert platform_rows[0]["department"] == "全部部门"
    denied = client.post(
        "/api/spaces/current/members",
        headers=finance,
        json={"username": "outsider", "role": "viewer", "space_id": support["id"]},
    )
    assert denied.status_code == 403
    board = client.get("/api/observability/summary", headers=admin, params={"scope": "all"}).json()
    assert "tokens_in" in board and "tokens_out" in board
    assert any(item["department"] == "客服中心" and "agents" in item for item in board["by_department"])
    assert any("tokens" in item for item in board["by_model"]) or board["by_model"] == []
    narrowed = client.get("/api/observability/summary", headers=builder, params={"scope": "all"}).json()
    assert [item["department"] for item in narrowed["by_department"]] == ["客服中心"]
    picked = client.get("/api/observability/summary", headers=admin, params={"space_id": support["id"]}).json()
    assert [item["department"] for item in picked["by_department"]] == ["客服中心"]
    sneak = client.get("/api/observability/summary", headers=builder, params={"space_id": treasury["id"]}).json()
    assert [item["department"] for item in sneak["by_department"]] == ["客服中心"]


def test_eval_excel_cases_and_metrics(client):
    headers, _space = auth(client, "builder")
    sample = client.get("/api/evals/sample.xlsx", headers=headers)
    assert sample.status_code == 200
    parsed = client.post(
        "/api/evals/parse",
        headers=headers,
        files={"file": ("sample.xlsx", sample.content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert parsed.status_code == 200, parsed.text
    first = parsed.json()["cases"][0]
    assert first["input_text"] == "计算 19*7"
    assert first["expected_tool"] == "calculator"
    created = client.post(
        "/api/evals/datasets",
        headers=headers,
        json={"name": "表格评测", "cases": [first]},
    )
    assert created.status_code == 200, created.text
    dataset_id = created.json()["id"]
    assert created.json()["case_count"] == 1
    page = client.get(
        f"/api/evals/datasets/{dataset_id}/cases",
        headers=headers,
        params={"page": 1, "page_size": 10, "q": "19", "exact_field": "expected", "exact": "133"},
    ).json()
    assert page["total"] == 1
    agent = next(item for item in client.get("/api/agents", headers=headers).json() if item["key"] == "support-agent")
    report = client.post(
        f"/api/evals/datasets/{dataset_id}/run",
        headers=headers,
        json={"agent_id": agent["id"], "metrics": ["contains", "exact", "completion", "tool"]},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["passed"] is True
    assert {item["key"] for item in body["metrics"]} == {"contains", "exact", "completion", "tool"}


def test_live_model_loops_until_the_tool_finishes(client, monkeypatch):
    headers, _space = auth(client, "builder")
    model = client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "loop-model",
            "provider": "openai",
            "model": "demo",
            "api_key": "test-key",
            "task_types": ["reason"],
            "price_in": 1,
            "price_out": 1,
            "priority": 9,
        },
    )
    assert model.status_code == 200, model.text
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "循环助手",
            "key": "loop-agent",
            "system_prompt": "你是助手。\n\n【可用工具】\n- calculator：旧介绍\n【可用工具结束】",
            "require_plan": False,
            "tool_names": ["calculator"],
            "model_id": model.json()["id"],
        },
    )
    assert agent.status_code == 200, agent.text
    state = {"n": 0}

    def fake(db, chosen, messages, tools):
        state["n"] += 1
        user_messages = [item["content"] for item in messages if item["role"] == "user"]
        assert user_messages == ["请算出 19*7"]
        assert all("【可用工具】" not in (item.get("content") or "") for item in messages)
        calculator = next(item["function"] for item in tools if item["function"]["name"] == "calculator")
        assert "做四则运算" in calculator["description"]
        if state["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{"id": "call-1", "name": "calculator", "arguments": {"expression": "19*7"}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (9, 4),
            }
        assert messages[-1]["role"] == "tool"
        assert "133" in messages[-1]["content"]
        return {"text": "133", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (10, 2)}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(
        f"/api/agents/{agent.json()['id']}/run",
        headers=headers,
        json={"message": "请算出 19*7", "environment": "test"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["output"] == "【v1】133"
    assert result.json()["tool_calls"][0]["name"] == "calculator"
    assert "133" in result.json()["tool_calls"][0]["output"]
    assert state["n"] == 2


def test_tool_results_stay_in_the_dialogue_when_the_model_keeps_calling(client, monkeypatch):
    headers, _space = auth(client, "builder")
    model = client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "loop-keep",
            "provider": "openai",
            "model": "demo",
            "api_key": "test-key",
            "task_types": ["reason"],
            "price_in": 1,
            "price_out": 1,
            "priority": 9,
        },
    )
    assert model.status_code == 200, model.text
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "连续检索",
            "key": "loop-keep",
            "system_prompt": "检索。",
            "require_plan": False,
            "tool_names": ["calculator"],
            "model_id": model.json()["id"],
        },
    )
    assert agent.status_code == 200, agent.text
    monkeypatch.setattr("app.services.runtime._LOOP_TURNS", 2)
    state = {"n": 0}

    def fake(db, chosen, messages, tools):
        state["n"] += 1
        users = [item["content"] for item in messages if item["role"] == "user"]
        assert users[0] == "请算出 19*7"
        if state["n"] > 2:
            assert any(item["role"] == "tool" and "133" in item["content"] for item in messages)
            return {"text": "计算结果是 133。", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (2, 2), "error": ""}
        if not tools:
            assert any(item["role"] == "tool" and "133" in item["content"] for item in messages)
            return {"text": "", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (2, 1), "error": ""}
        return {
            "text": "",
            "tool_calls": [{"id": f"call-{len(messages)}", "name": "calculator", "arguments": {"expression": "19*7" if state["n"] == 1 else f"20+{state['n']}"}}],
            "model_name": chosen.name,
            "fallback": False,
            "usage": (2, 1),
            "error": "",
        }

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(
        f"/api/agents/{agent.json()['id']}/run",
        headers=headers,
        json={"message": "请算出 19*7", "environment": "test"},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "limited"
    assert body["limited"] is True
    assert "循环已达到上限" in body["output"]
    assert "133" in body["tool_calls"][0]["output"]
    assert "已收到请求" not in body["output"]
    continued = client.post(
        f"/api/agents/{agent.json()['id']}/run",
        headers=headers,
        json={"message": "", "environment": "test", "resume_trace_id": body["trace_id"]},
    )
    assert continued.status_code == 200, continued.text
    assert "计算结果是 133" in continued.json()["output"]
    assert continued.json()["limited"] is False


def test_dsml_markup_is_executed_as_web_search(client, monkeypatch):
    from app.services.gateway import parse_text_tool_calls

    sample = (
        "下面开始检索。\n"
        "<｜｜DSML｜｜tool_calls>\n"
        "<｜｜DSML｜｜invoke name=\"web_search\">\n"
        "<｜｜DSML｜｜parameter name=\"query\" string=\"true\">2027国考 职位表 限应届毕业生 岗位数量 往届生</｜｜DSML｜｜parameter>\n"
        "</｜｜DSML｜｜invoke>\n"
        "<｜｜DSML｜｜invoke name=\"web_search\">\n"
        "<｜｜DSML｜｜parameter name=\"query\" string=\"true\">2027国考 职位分析 应届生 招录</｜｜DSML｜｜parameter>\n"
        "</｜｜DSML｜｜invoke>\n"
        "</｜｜DSML｜｜tool_calls>"
    )
    clean, calls = parse_text_tool_calls(sample)
    assert "DSML" not in clean
    assert clean == "下面开始检索。"
    assert [item["arguments"]["query"] for item in calls] == [
        "2027国考 职位表 限应届毕业生 岗位数量 往届生",
        "2027国考 职位分析 应届生 招录",
    ]
    headers, _space = auth(client, "builder")
    model = client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "dsml-model",
            "provider": "openai",
            "model": "demo",
            "api_key": "test-key",
            "task_types": ["reason"],
            "price_in": 1,
            "price_out": 1,
            "priority": 9,
        },
    )
    assert model.status_code == 200, model.text
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "检索循环",
            "key": "dsml-agent",
            "system_prompt": "检索助手。",
            "require_plan": False,
            "tool_names": [],
            "model_id": model.json()["id"],
        },
    )
    assert agent.status_code == 200, agent.text
    monkeypatch.setattr(
        "app.services.runtime._tool_catalog",
        lambda db, version, space: (
            [{"type": "function", "function": {"name": "web_search", "description": "网上搜索", "parameters": {"type": "object", "properties": {}}}}],
            {"web_search": {"kind": "mcp", "name": "web.search", "url": "http://127.0.0.1:9/mcp", "token": ""}},
        ),
    )
    seen = []

    def fake_call(url, token, name, arguments):
        seen.append((name, arguments))
        return {"text": f"检索到 {arguments.get('query')}"}

    monkeypatch.setattr("app.services.mcp_client.call_tool", fake_call)
    state = {"n": 0}

    def fake_complete(db, chosen, messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            text, parsed = parse_text_tool_calls(sample)
            return {"text": text, "tool_calls": parsed, "model_name": chosen.name, "fallback": False, "usage": (4, 4)}
        assert len([item for item in messages if item["role"] == "tool"]) == 2
        return {"text": "应届与往届岗位需要分开统计。", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (6, 3)}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake_complete)
    result = client.post(
        f"/api/agents/{agent.json()['id']}/run",
        headers=headers,
        json={"message": "2027年国考岗位共有多少", "environment": "test"},
    )
    assert result.status_code == 200, result.text
    assert "DSML" not in result.json()["output"]
    assert "应届与往届" in result.json()["output"]
    assert [item[0] for item in seen] == ["web.search", "web.search"]
    assert seen[0][1]["query"].startswith("2027国考 职位表")


def _loop_agent(client, headers, name, key, tools):
    model = client.post(
        "/api/models",
        headers=headers,
        json={
            "name": name,
            "provider": "openai",
            "model": "demo",
            "api_key": "test-key",
            "task_types": ["reason"],
            "price_in": 1,
            "price_out": 1,
            "priority": 9,
        },
    )
    assert model.status_code == 200, model.text
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": name,
            "key": key,
            "system_prompt": "助手。",
            "require_plan": False,
            "tool_names": tools,
            "model_id": model.json()["id"],
        },
    )
    assert agent.status_code == 200, agent.text
    return agent.json()["id"]


def test_argument_errors_go_back_to_the_model_without_retry(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "参数修正", "fix-args", ["calculator"])
    pauses = []
    monkeypatch.setattr("app.services.runtime._pause", lambda seconds: pauses.append(seconds))
    state = {"n": 0}

    def fake(db, chosen, messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{"id": "call-1", "name": "calculator", "arguments": {"expression": "abs(1)"}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (3, 2),
                "error": "",
            }
        tool_text = [item["content"] for item in messages if item["role"] == "tool"]
        if state["n"] == 2:
            assert tool_text and tool_text[-1].startswith("参数错误：")
            assert "重新输出调用参数" in tool_text[-1]
            return {
                "text": "",
                "tool_calls": [{"id": "call-2", "name": "calculator", "arguments": {"expression": "19*7"}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (4, 2),
                "error": "",
            }
        assert "133" in tool_text[-1]
        return {"text": "133", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (4, 1), "error": ""}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "算一下", "environment": "test"})
    assert result.status_code == 200, result.text
    assert result.json()["output"] == "【v1】133"
    assert pauses == []
    assert state["n"] == 3


def test_transient_tool_errors_back_off_three_times(client, monkeypatch):
    from app.services.mcp_client import McpError

    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "抖动重试", "retry-net", [])
    monkeypatch.setattr(
        "app.services.runtime._tool_catalog",
        lambda db, version, space: (
            [{"type": "function", "function": {"name": "web_search", "description": "网上搜索", "parameters": {"type": "object", "properties": {}}}}],
            {"web_search": {"kind": "mcp", "name": "web.search", "url": "http://127.0.0.1:9/mcp", "token": ""}},
        ),
    )
    pauses = []
    monkeypatch.setattr("app.services.runtime._pause", lambda seconds: pauses.append(seconds))
    calls = {"n": 0}

    def fake_call(url, token, name, arguments):
        calls["n"] += 1
        if calls["n"] < 3:
            raise McpError("MCP 返回 503", "transient")
        return {"ok": True, "text": "职位表尚未公布"}

    monkeypatch.setattr("app.services.mcp_client.call_tool", fake_call)

    def fake(db, chosen, messages, tools):
        if not any(item["role"] == "tool" for item in messages):
            return {
                "text": "",
                "tool_calls": [{"id": "call-1", "name": "web_search", "arguments": {"query": "2027国考"}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (2, 1),
                "error": "",
            }
        assert "职位表尚未公布" in messages[-1]["content"]
        assert "已退避重试" not in messages[-1]["content"]
        return {"text": "职位表尚未公布。", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (3, 2), "error": ""}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "查一下", "environment": "test"})
    assert result.status_code == 200, result.text
    assert "职位表尚未公布" in result.json()["output"]
    assert calls["n"] == 3
    assert pauses == [0.5, 1.0]
    assert sum(1 for item in result.json()["tool_calls"] if "503" in item["output"]) == 2


def test_parameter_error_from_mcp_is_not_retried(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "搜索参数", "search-args", [])
    monkeypatch.setattr(
        "app.services.runtime._tool_catalog",
        lambda db, version, space: (
            [{"type": "function", "function": {"name": "web_search", "description": "网上搜索", "parameters": {"type": "object", "properties": {}}}}],
            {"web_search": {"kind": "mcp", "name": "web.search", "url": "http://127.0.0.1:9/mcp", "token": ""}},
        ),
    )
    pauses = []
    monkeypatch.setattr("app.services.runtime._pause", lambda seconds: pauses.append(seconds))
    calls = {"n": 0}

    def fake_call(url, token, name, arguments):
        calls["n"] += 1
        if not arguments.get("query"):
            return {"ok": False, "text": "请填写搜索词"}
        return {"ok": True, "text": "找到 3 条"}

    monkeypatch.setattr("app.services.mcp_client.call_tool", fake_call)
    state = {"n": 0}

    def fake(db, chosen, messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{"id": "call-1", "name": "web_search", "arguments": {}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (2, 1),
                "error": "",
            }
        if state["n"] == 2:
            assert messages[-1]["content"].startswith("参数错误：请填写搜索词")
            return {
                "text": "",
                "tool_calls": [{"id": "call-2", "name": "web_search", "arguments": {"query": "国考"}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (3, 1),
                "error": "",
            }
        assert "找到 3 条" in messages[-1]["content"]
        return {"text": "找到 3 条。", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (3, 1), "error": ""}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "搜索", "environment": "test"})
    assert result.status_code == 200, result.text
    assert calls["n"] == 2
    assert pauses == []
    assert "找到 3 条" in result.json()["output"]


def test_successful_tool_is_not_called_again_with_the_same_arguments(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "重复调用", "no-repeat", ["calculator"])
    rounds = {"n": 0}

    def fake(db, chosen, messages, tools):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {
                "text": "",
                "tool_calls": [
                    {"id": "call-1", "name": "calculator", "arguments": {"expression": "2+2"}},
                    {"id": "call-2", "name": "calculator", "arguments": {"expression": "2+2"}},
                ],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (2, 1),
                "error": "",
            }
        if tools:
            assert any("不能再次调用" in (item.get("content") or "") for item in messages if item.get("role") == "tool")
            return {
                "text": "",
                "tool_calls": [{"id": "call-3", "name": "calculator", "arguments": {"expression": "2+2"}}],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (3, 1),
                "error": "",
            }
        return {"text": "结果是 4。", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (3, 1), "error": ""}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "计算 2+2", "environment": "test"})
    assert result.status_code == 200, result.text
    assert "结果是 4" in result.json()["output"]
    executed = [item for item in result.json()["tool_calls"] if item["name"] == "calculator"]
    assert len(executed) == 1
    assert rounds["n"] == 3


def test_follow_up_sends_the_previous_dialogue(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "连续对话", "chat-history", ["calculator"])
    seen = []

    def fake(db, chosen, messages, tools):
        seen.append([(item["role"], item["content"]) for item in messages if item["role"] in {"user", "assistant"}])
        return {"text": "好的", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (2, 1), "error": ""}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    first = client.post(
        f"/api/agents/{agent_id}/run",
        headers=headers,
        json={"message": "先算 1+1", "environment": "test"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/agents/{agent_id}/run",
        headers=headers,
        json={
            "message": "再算 2+2",
            "environment": "test",
            "history": [
                {"role": "user", "content": "先算 1+1"},
                {"role": "assistant", "content": first.json()["output"]},
            ],
        },
    )
    assert second.status_code == 200, second.text
    assert seen[0] == [("user", "先算 1+1")]
    assert seen[1][0] == ("user", "先算 1+1")
    assert seen[1][1][0] == "assistant"
    assert "好的" in seen[1][1][1]
    assert seen[1][2] == ("user", "再算 2+2")


def test_loop_answers_the_question_after_tools_instead_of_dumping_them(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "检索作答", "search-answer", [])
    monkeypatch.setattr(
        "app.services.runtime._tool_catalog",
        lambda db, version, space: (
            [{"type": "function", "function": {"name": "web_search", "description": "网上搜索", "parameters": {"type": "object", "properties": {}}}}],
            {"web_search": {"kind": "mcp", "name": "web.search", "url": "http://127.0.0.1:9/mcp", "token": ""}},
        ),
    )
    calls = {"n": 0}

    def fake_call(url, token, name, arguments):
        calls["n"] += 1
        return {"ok": True, "text": "没有找到 2027 年国考职位表"}

    monkeypatch.setattr("app.services.mcp_client.call_tool", fake_call)
    monkeypatch.setattr("app.services.runtime._LOOP_TURNS", 2)
    state = {"n": 0}

    def fake(db, chosen, messages, tools):
        state["n"] += 1
        if state["n"] > 2:
            assert any(item["role"] == "tool" and "没有找到" in item["content"] for item in messages)
            return {
                "text": "公开检索没有找到 2027 年国考职位表，因此无法统计岗位数量，也无法区分应届生要求。",
                "tool_calls": [],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (4, 4),
                "error": "",
            }
        return {
            "text": "",
            "tool_calls": [{"id": f"call-{len(messages)}", "name": "web_search", "arguments": {"query": "2027国考职位表"}}],
            "model_name": chosen.name,
            "fallback": False,
            "usage": (2, 1),
            "error": "",
        }

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(
        f"/api/agents/{agent_id}/run",
        headers=headers,
        json={"message": "2027年国考岗位共有多少", "environment": "test"},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["limited"] is False
    assert "无法统计岗位数量" in body["output"]
    assert "循环已达到上限" not in body["output"]
    assert calls["n"] == 1


def test_search_budget_stops_extra_queries(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent_id = _loop_agent(client, headers, "搜索限额", "search-budget", [])
    monkeypatch.setattr(
        "app.services.runtime._tool_catalog",
        lambda db, version, space: (
            [{"type": "function", "function": {"name": "web_search", "description": "网上搜索", "parameters": {"type": "object", "properties": {}}}}],
            {"web_search": {"kind": "mcp", "name": "web.search", "url": "http://127.0.0.1:9/mcp", "token": ""}},
        ),
    )
    calls = {"n": 0}

    def fake_call(url, token, name, arguments):
        calls["n"] += 1
        return {"ok": True, "text": f"结果 {arguments.get('query')}"}

    monkeypatch.setattr("app.services.mcp_client.call_tool", fake_call)
    state = {"n": 0}

    def fake(db, chosen, messages, tools):
        state["n"] += 1
        if state["n"] > 3:
            assert any("搜索次数已够" in (item.get("content") or "") for item in messages if item.get("role") == "tool")
            return {
                "text": "根据已有检索，林正英的代表僵尸片包括僵尸先生。",
                "tool_calls": [],
                "model_name": chosen.name,
                "fallback": False,
                "usage": (4, 4),
                "error": "",
            }
        return {
            "text": "",
            "tool_calls": [
                {"id": f"a-{state['n']}", "name": "web_search", "arguments": {"query": f"词{state['n']}甲"}},
                {"id": f"b-{state['n']}", "name": "web_search", "arguments": {"query": f"词{state['n']}乙"}},
            ],
            "model_name": chosen.name,
            "fallback": False,
            "usage": (2, 1),
            "error": "",
        }

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    result = client.post(f"/api/agents/{agent_id}/run", headers=headers, json={"message": "林正英老师都演过哪些僵尸片", "environment": "test"})
    assert result.status_code == 200, result.text
    assert "僵尸先生" in result.json()["output"]
    assert calls["n"] == 3


def test_chat_desk_keeps_history_and_respects_roles(client, monkeypatch):
    headers, _space = auth(client, "builder")
    viewer, _same = auth(client, "viewer")
    finance, _other = auth(client, "finance", space_key="finance")
    model = client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "问答模型",
            "provider": "openai",
            "model": "demo",
            "api_key": "test-key",
            "task_types": ["reason"],
            "price_in": 1,
            "price_out": 1,
            "priority": 9,
        },
    )
    assert model.status_code == 200, model.text
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "问答助手",
            "key": "ask-agent",
            "system_prompt": "回答问题。",
            "require_plan": False,
            "tool_names": ["calculator"],
            "model_id": model.json()["id"],
        },
    )
    assert agent.status_code == 200, agent.text
    agent_id = agent.json()["id"]
    assert client.post("/api/chats", headers=viewer, json={"agent_id": agent_id}).status_code == 403
    created = client.post("/api/chats", headers=headers, json={"agent_id": agent_id})
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    seen = []

    def fake(db, chosen, messages, tools):
        seen.append([(item["role"], item["content"]) for item in messages if item["role"] in {"user", "assistant"}])
        return {"text": "已记录。", "tool_calls": [], "model_name": chosen.name, "fallback": False, "usage": (2, 1), "error": ""}

    monkeypatch.setattr("app.services.runtime.complete_with_tools", fake)
    first = client.post(f"/api/chats/{session_id}/messages", headers=headers, json={"message": "岗位有多少"})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/chats/{session_id}/messages", headers=headers, json={"message": "应届生呢"})
    assert second.status_code == 200, second.text
    assert seen[0] == [("user", "岗位有多少")]
    assert ("user", "岗位有多少") in seen[1]
    assert ("user", "应届生呢") in seen[1]
    assert client.post(f"/api/chats/{session_id}/messages", headers=viewer, json={"message": "不能发"}).status_code == 403
    found = client.get("/api/chats", headers=headers, params={"q": "岗位"})
    assert found.status_code == 200
    assert session_id in [item["id"] for item in found.json()]
    assert client.get(f"/api/chats/{session_id}", headers=finance).status_code == 404
    assert client.get(f"/api/chats/{session_id}", headers=viewer).status_code == 404
    pinned = client.patch(f"/api/chats/{session_id}", headers=headers, json={"pinned": True})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned"] is True
    listed = client.get("/api/chats", headers=headers)
    assert listed.json()[0]["id"] == session_id
    streamed = client.post(
        f"/api/chats/{session_id}/messages",
        headers=headers,
        json={"message": "应届生要求呢", "stream": True},
    )
    assert streamed.status_code == 200, streamed.text
    assert "text/event-stream" in streamed.headers["content-type"]
    assert '"type":"delta"' in streamed.text
    assert '"type":"done"' in streamed.text
    assert "已记录" in streamed.text


def test_platform_admin_can_ask_an_agent_from_another_department(client, monkeypatch):
    finance, finance_space = auth(client, "finance", space_key="finance")
    support, _support_space = auth(client, "builder")
    admin, _admin_space = auth(client, "admin", space_key="support")
    created = client.post(
        "/api/agents",
        headers=finance,
        json={"name": "无工具智能体", "key": "no-tools-ask", "require_plan": False, "tool_names": []},
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    denied = client.post("/api/chats", headers=support, json={"agent_id": agent_id})
    assert denied.status_code == 404
    assert denied.json()["detail"] == "智能体不存在"

    def fake_execute(db, **kwargs):
        assert kwargs["space"].id == finance_space["id"]
        return {"output": "已回答", "status": "completed", "trace_id": "", "tool_calls": []}

    monkeypatch.setattr("app.api.routers.conversations.execute_agent", fake_execute)
    opened = client.post("/api/chats", headers=admin, json={"agent_id": agent_id})
    assert opened.status_code == 200, opened.text
    session_id = opened.json()["id"]
    sent = client.post(
        f"/api/chats/{session_id}/messages",
        headers=admin,
        json={"message": "林正英老师都演过哪些电影"},
    )
    assert sent.status_code == 200, sent.text
    assert "已回答" in sent.text
    listed = client.get("/api/chats", headers=admin).json()
    assert session_id in [item["id"] for item in listed]


def test_platform_admin_can_ask_an_unassigned_agent(client, monkeypatch):
    support, support_space = auth(client, "builder")
    admin, _admin_space = auth(client, "admin", space_key="support")
    created = client.post(
        "/api/agents",
        headers=admin,
        json={"name": "平台助手", "key": "platform-ask", "require_plan": False, "tool_names": [], "unassigned": True},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["space_id"] is None
    assert body["department"] == "平台"
    agent_id = body["id"]
    denied = client.post("/api/chats", headers=support, json={"agent_id": agent_id})
    assert denied.status_code == 404
    kept = client.post(
        "/api/agents",
        headers=support,
        json={"name": "仍在部门", "key": "stay-home", "require_plan": False, "tool_names": [], "unassigned": True},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["space_id"] == support_space["id"]

    def fake_execute(db, **kwargs):
        assert kwargs["space"].id == support_space["id"]
        return {"output": "平台可以问", "status": "completed", "trace_id": "", "tool_calls": []}

    monkeypatch.setattr("app.api.routers.conversations.execute_agent", fake_execute)
    opened = client.post("/api/chats", headers=admin, json={"agent_id": agent_id})
    assert opened.status_code == 200, opened.text
    sent = client.post(
        f"/api/chats/{opened.json()['id']}/messages",
        headers=admin,
        json={"message": "未分配部门也能问吗"},
    )
    assert sent.status_code == 200, sent.text
    assert "平台可以问" in sent.text


def test_secret_usage_counts_and_overview(client):
    admin, support = auth(client, "admin")
    finance, finance_space = auth(client, "finance", space_key="finance")
    model = client.post(
        "/api/models",
        headers=admin,
        json={"name": "带钥匙", "provider": "openai", "model": "demo-key", "api_key": "sk-test", "task_types": ["reason"], "price_in": 1, "price_out": 2},
    )
    assert model.status_code == 200, model.text
    secret_id = model.json()["secret_id"]
    mcp = client.post("/api/mcp", headers=admin, json={"name": "检索连接", "url": "http://127.0.0.1:9/mcp", "token": "mcp-token"})
    assert mcp.status_code == 200, mcp.text
    listed = client.get("/api/secrets", headers=admin, params={"page": 1, "page_size": 50}).json()
    model_secret = next(item for item in listed["items"] if item["id"] == secret_id)
    assert model_secret["model_count"] == 1
    assert model_secret["models"][0]["name"] == "带钥匙"
    mcp_secret = next(item for item in listed["items"] if item["mcp_count"] == 1 and item["mcp_servers"][0]["name"] == "检索连接")
    blocked = client.delete(f"/api/secrets/{secret_id}", headers=admin)
    assert blocked.status_code == 409
    filtered = client.get("/api/models", headers=admin, params={"page": 1, "exact_field": "secret_id", "exact": str(secret_id)}).json()
    assert [item["name"] for item in filtered["items"]] == ["带钥匙"]
    mcp_filtered = client.get("/api/mcp", headers=admin, params={"page": 1, "exact_field": "secret_id", "exact": str(mcp_secret["id"])}).json()
    assert [item["name"] for item in mcp_filtered["items"]] == ["检索连接"]

    from app.models import Trace, TraceStep

    db = client.app.state.session_factory()
    support_trace = Trace(id="secretusage01", space_id=support["id"], user_id=1, status="completed", input_text="客服提问", tokens_in=10, tokens_out=4, cost=0.2)
    finance_trace = Trace(id="secretusage02", space_id=finance_space["id"], user_id=1, status="completed", input_text="财务提问", tokens_in=6, tokens_out=2, cost=0.1)
    db.add_all([support_trace, finance_trace])
    db.flush()
    db.add_all([
        TraceStep(trace_id=support_trace.id, idx=0, kind="generate", name="reply", model="带钥匙", secret_id=secret_id, tokens_in=10, tokens_out=4, cost=0.2),
        TraceStep(trace_id=finance_trace.id, idx=0, kind="generate", name="reply", model="带钥匙", secret_id=secret_id, tokens_in=6, tokens_out=2, cost=0.1),
        TraceStep(trace_id=support_trace.id, idx=1, kind="execute", name="web.search", secret_id=mcp_secret["id"]),
    ])
    db.commit()
    db.close()

    board = client.get("/api/observability/summary", headers=admin, params={"scope": "all"}).json()
    model_row = next(item for item in board["model_secrets"]["items"] if item["secret_id"] == secret_id)
    assert model_row["calls"] == 2
    assert model_row["tokens"] == 22
    assert round(model_row["cost"], 6) == 0.3
    assert {item["department"] for item in model_row["by_department"]} == {support["name"], finance_space["name"]}
    assert board["model_secrets"]["calls"] >= 2
    mcp_row = next(item for item in board["mcp_secrets"]["items"] if item["secret_id"] == mcp_secret["id"])
    assert mcp_row["calls"] == 1
    assert mcp_row["by_department"][0]["department"] == support["name"]
    narrowed = client.get("/api/observability/summary", headers=finance, params={"scope": "all"}).json()
    assert all(item["space_id"] == finance_space["id"] for item in narrowed["model_secrets"]["by_department"])
    assert narrowed["mcp_secrets"]["calls"] == 0


def test_workflow_schedule_runs_saved_question(client, app):
    from datetime import datetime, timedelta

    from app.models import Workflow
    from app.services.scheduler import tick

    headers, _space = auth(client, "builder")
    agent_id = _agent_id(client, headers, "refund-agent")
    created = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "定时退款",
            "agent_id": agent_id,
            "definition": {
                "kind": "agents",
                "entry": "a",
                "nodes": [{"id": "a", "type": "agent", "agent_id": agent_id, "x": 20, "y": 20}],
                "edges": [],
            },
        },
    )
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    scheduled = client.patch(
        f"/api/workflows/{flow_id}",
        headers=headers,
        json={
            "schedule_enabled": True,
            "schedule_minutes": 15,
            "schedule_message": "只回复已收到",
            "schedule_nodes": [{"agent_id": agent_id, "node_id": "a", "prompt": "只回复已收到", "tool_names": [], "knowledge_ids": [], "mcp_ids": []}],
        },
    )
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["schedule_enabled"] is True
    assert scheduled.json()["schedule_next_at"]
    db = app.state.session_factory()
    row = db.get(Workflow, flow_id)
    row.schedule_next_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()
    assert tick(app.state.session_factory) == 1
    listed = client.get("/api/workflow-runs", headers=headers, params={"page": 1, "exact_field": "workflow_id", "exact": str(flow_id)}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["environment"] == "schedule"
    assert listed["items"][0]["message"] == "只回复已收到"


def test_chat_workflow_stays_out_until_approved(client, monkeypatch):
    headers, _space = auth(client, "builder")
    model = client.post(
        "/api/models",
        headers=headers,
        json={"name": "起草模型", "provider": "openai", "model": "demo", "api_key": "test-key", "task_types": ["reason"], "price_in": 1, "price_out": 1},
    )
    assert model.status_code == 200, model.text
    first = client.post("/api/agents", headers=headers, json={"name": "规划者", "key": "draft-plan", "system_prompt": "整理步骤。", "model_id": model.json()["id"]})
    second = client.post("/api/agents", headers=headers, json={"name": "写作者", "key": "draft-write", "system_prompt": "写成说明。", "model_id": model.json()["id"]})
    assert first.status_code == 200 and second.status_code == 200
    left, right = first.json()["id"], second.json()["id"]

    def fake_complete(_db, _model, _messages, _tools):
        return {
            "text": '{"name":"入职交接","kind":"agents","nodes":[{"agent_id":%d},{"agent_id":%d}],"edges":[{"src":"n1","dst":"n2"}]}' % (left, right),
            "tool_calls": [],
            "fallback": False,
            "usage": (12, 20),
        }

    monkeypatch.setattr("app.api.routers.conversations.complete_with_tools", fake_complete)
    session = client.post("/api/chats", headers=headers, json={"agent_id": left, "title": "起草"})
    assert session.status_code == 200, session.text
    drafted = client.post(f"/api/chats/{session.json()['id']}/workflow", headers=headers, json={"message": "先整理步骤，再写成交接说明"})
    assert drafted.status_code == 200, drafted.text
    approval_id = drafted.json()["approval_id"]
    assert "入职交接" in drafted.json()["messages"][-1]["content"]
    listed = client.get("/api/workflows", headers=headers, params={"page": 1, "q": "入职交接"})
    assert listed.json()["total"] == 0
    decided = client.post(f"/api/approvals/{approval_id}/decision", headers=headers, json={"decision": "approved", "channel": "web", "comment": "核对通过"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["workflow_id"]
    again = client.get("/api/workflows", headers=headers, params={"page": 1, "q": "入职交接"})
    assert again.json()["total"] == 1
    assert again.json()["items"][0]["name"] == "入职交接"


def test_skill_zip_reads_markdown(client):
    import io
    import zipfile

    headers, _space = auth(client, "builder")
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(
            "demo/SKILL.md",
            "---\nname: zip-skill\ndescription: 从说明读取\n---\n\n# 压缩包技能\n\n按说明执行。\n",
        )
        archive.writestr("demo/scripts/add.py", "print('from-zip')\n")
        archive.writestr("../escape.py", "print('no')\n")
    slipped = client.post("/api/skills/import", headers=headers, files={"file": ("skill.zip", raw.getvalue(), "application/zip")})
    assert slipped.status_code == 400
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(
            "demo/SKILL.md",
            "---\nname: zip-skill\ndescription: 从说明读取\n---\n\n# 压缩包技能\n\n按说明执行。\n",
        )
        archive.writestr("demo/scripts/add.py", "print('from-zip')\n")
    created = client.post("/api/skills/import", headers=headers, files={"file": ("skill.zip", raw.getvalue(), "application/zip")})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["key"] == "zip-skill"
    assert body["description"] == "从说明读取"
    assert "按说明执行" in body["body"]
    assert body["scripts"] == ["add.py"]
    detail = client.get(f"/api/skills/{body['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "from-zip" in detail.json()["script_sources"]["add.py"]
    from app.services.skillbox import storage_path

    assert detail.json()["storage_path"] == storage_path(body["id"])


def test_running_skill_jobs_are_listed():
    from app.services import skillbox

    token = skillbox._begin_job(41, "hold.py")
    try:
        rows = [item for item in skillbox.running_jobs() if item["id"] == token]
        assert len(rows) == 1
        assert rows[0]["skill_id"] == 41
        assert rows[0]["script"] == "hold.py"
        assert rows[0]["state"] == "running"
        assert rows[0]["started_at"] > 0
        skillbox._end_job(token)
        done = [item for item in skillbox.running_jobs() if item["id"] == token]
        assert done[0]["state"] == "finished"
        assert done[0]["ended_at"] >= done[0]["started_at"]
        with skillbox._JOB_LOCK:
            skillbox._JOBS[token]["ended_at"] = skillbox.time.time() - skillbox._RECENT_SECONDS - 1
        assert all(item["id"] != token for item in skillbox.running_jobs())
    finally:
        with skillbox._JOB_LOCK:
            skillbox._JOBS.pop(token, None)


def test_skill_script_does_not_run_on_the_host(client):
    headers, _space = auth(client, "builder")
    created = client.post(
        "/api/skills",
        headers=headers,
        json={
            "name": "金额复核",
            "key": "amount-check",
            "description": "核对两个金额",
            "body": "把参数相加。",
            "script_name": "add.py",
            "script_source": "print(1)\n",
        },
    )
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "复核员", "key": "amount-agent", "system_prompt": "核对金额。", "skill_ids": [skill_id]},
    )
    assert agent.status_code == 200, agent.text
    assert agent.json()["skill_ids"] == [skill_id]
    stored = agent.json()["versions"][-1]["system_prompt"]
    assert "核对金额。" in stored
    assert "【可用技能】" in stored
    assert "amount-check" in stored
    assert "核对两个金额" in stored
    assert "add.py" in stored
    from app.services.skillbox import _script_name, run_script

    try:
        _script_name("../add.py")
        raised = False
    except ValueError:
        raised = True
    assert raised
    try:
        run_script(skill_id, "add.py", ["80", "48"])
        ran = False
    except RuntimeError as exc:
        ran = True
        assert "容器" in str(exc)
    assert ran


def test_workflow_run_archive_collects_project_files(client, monkeypatch):
    headers, _space = auth(client, "builder")
    agent = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "后端开发", "key": "archive-backend", "system_prompt": "写后端。", "require_plan": False},
    )
    assert agent.status_code == 200, agent.text
    agent_id = agent.json()["id"]

    def fake_execute(*_args, **_kwargs):
        return {
            "output": "<<<FILE backend/app/main.py>>>\nprint('students')\n<<<END>>>\n```js frontend/src/main.js\nconsole.log(1)\n```\n<<<FILE ../escape.py>>>\nprint('no')\n<<<END>>>",
            "status": "completed",
            "trace_id": "",
        }

    monkeypatch.setattr("app.services.runtime.workflow.execute_agent", fake_execute)
    created = client.post(
        "/api/workflows",
        headers=headers,
        json={
            "name": "打包试验",
            "agent_id": agent_id,
            "definition": {
                "kind": "autonomous",
                "entry": "lead",
                "nodes": [{"id": "lead", "type": "agent", "agent_id": agent_id}],
                "edges": [],
            },
        },
    )
    assert created.status_code == 200, created.text
    ran = client.post(f"/api/workflows/{created.json()['id']}/run", headers=headers, json={"message": "做学生系统", "environment": "test"})
    assert ran.status_code == 200, ran.text
    assert "backend/app/main.py" in ran.json()["files"]
    assert "frontend/src/main.js" in ran.json()["files"]
    archive = client.get(f"/api/workflow-runs/{ran.json()['run_id']}/archive", headers=headers)
    assert archive.status_code == 200, archive.text
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        names = set(bundle.namelist())
        assert names == {"backend/app/main.py", "frontend/src/main.js"}
        assert b"students" in bundle.read("backend/app/main.py")
