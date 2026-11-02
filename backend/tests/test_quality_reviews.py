import json

from tests.conftest import auth


def test_quality_review_collection_filters_exports_and_permissions(client):
    builder, space = auth(client, "builder")
    admin, _ = auth(client, "admin")
    viewer, _ = auth(client, "viewer")
    agent = next(row for row in client.get("/api/agents", headers=builder).json() if row["key"] == "support-agent")
    execution = client.post(f"/api/agents/{agent['id']}/run", headers=builder, json={"message": "计算 1+1", "environment": "test"})
    assert execution.status_code == 200, execution.text
    trace_id = execution.json()["trace_id"]
    assert client.get("/api/quality-reviews", headers=builder).status_code == 403
    created = client.post(f"/api/traces/{trace_id}/quality", headers=builder, json={"score": 4, "note": "答案正确"})
    assert created.status_code == 200, created.text
    assert created.json()["agent_id"] == agent["id"]
    assert client.post(f"/api/traces/{trace_id}/quality", headers=builder, json={"score": 5}).status_code == 409
    assert client.get(f"/api/quality-reviews/{trace_id}", headers=viewer).status_code == 403
    listed = client.get("/api/quality-reviews", headers=admin, params={"page": 1, "page_size": 1, "space_id": space["id"], "agent_id": agent["id"], "q": "答案正确"})
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert client.get("/api/quality-reviews", headers=admin, params={"page": 1, "workflow_id": 99999}).json()["total"] == 0
    assert client.get("/api/quality-reviews/export", headers=admin, params={"purpose": "evaluation"}).status_code == 400
    selected = {"purpose": "evaluation", "ids": trace_id}
    exported = client.get("/api/quality-reviews/export", headers=admin, params=selected)
    assert exported.status_code == 200, exported.text
    dataset = exported.json()
    assert dataset["cases"][0]["input_text"] == "计算 1+1"
    assert dataset["cases"][0]["expected"]
    parsed = client.post("/api/evals/parse", headers=builder, files={"file": ("quality-evaluation.json", exported.content, "application/json")})
    assert parsed.status_code == 200 and parsed.json()["cases"][0]["expected"] == dataset["cases"][0]["expected"]
    evaluation_set = client.post("/api/evals/datasets", headers=builder, json={**dataset, "cases": parsed.json()["cases"]})
    assert evaluation_set.status_code == 200, evaluation_set.text
    selected_set = client.get(f"/api/evals/datasets/{evaluation_set.json()['id']}", headers=builder)
    assert selected_set.status_code == 200 and selected_set.json()["case_count"] == 1
    fine = client.get("/api/quality-reviews/export", headers=admin, params={"purpose": "finetune", "ids": trace_id})
    assert fine.status_code == 200 and json.loads(fine.text.strip())["messages"][1]["role"] == "assistant"
    assert client.get("/api/quality-reviews/export", headers=builder, params=selected).status_code == 403
    assert client.get("/api/quality-reviews/export", headers=admin, params={"purpose": "evaluation", "ids": "missing"}).status_code == 404
    edited = client.patch(f"/api/quality-reviews/{trace_id}", headers=admin, json={"score": 5, "note": "复核通过"})
    assert edited.status_code == 200 and edited.json()["score"] == 5
    assert client.delete(f"/api/quality-reviews/{trace_id}", headers=viewer).status_code == 403
    assert client.delete(f"/api/quality-reviews/{trace_id}", headers=admin).status_code == 200
    assert client.get(f"/api/quality-reviews/{trace_id}", headers=admin).status_code == 404
    assert client.get(f"/api/traces/{trace_id}", headers=admin).status_code == 200


def test_workflow_execution_can_be_reviewed(client):
    builder, _ = auth(client, "builder")
    admin, _ = auth(client, "admin")
    agent = next(row for row in client.get("/api/agents", headers=builder).json() if row["key"] == "support-agent")
    flow = client.post("/api/workflows", headers=builder, json={"name": "质量评价工作流", "agent_id": agent["id"], "definition": {"kind": "agents", "entry": "n1", "nodes": [{"id": "n1", "type": "agent", "agent_id": agent["id"]}], "edges": []}}).json()
    result = client.post(f"/api/workflows/{flow['id']}/run", headers=builder, json={"message": "计算 1+1", "environment": "test"})
    assert result.status_code == 200, result.text
    trace_id = result.json()["trace_id"]
    response = client.post(f"/api/traces/{trace_id}/quality", headers=builder, json={"score": 3, "note": "需改进"})
    assert response.status_code == 200, response.text
    rows = client.get("/api/quality-reviews", headers=admin, params={"page": 1, "workflow_id": flow["id"]}).json()
    assert rows["total"] == 1 and rows["items"][0]["workflow_name"] == flow["name"]
    assert client.get("/api/quality-reviews/export", headers=admin, params={"purpose": "evaluation", "ids": trace_id}).status_code == 422
