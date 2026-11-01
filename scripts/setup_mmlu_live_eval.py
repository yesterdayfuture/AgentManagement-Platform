"""Import MMLU subsets into an existing space with one visible DeepSeek agent."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from app.core import create_app
from app.models import Agent, EvalDataset, ModelEndpoint, Space, Tool, User
from app.security import issue_token

root = Path(__file__).resolve().parents[1]
names = ["mmlu-general-60.json", "mmlu-math-reasoning-60.json", "mmlu-explanation-40.json", "mmlu-tool-reasoning-40.json", "bigbench-text-editing-47.json"]
app = create_app(f"sqlite:///{root / 'backend/data/app.db'}")
db = app.state.session_factory()
try:
    admin = db.query(User).filter_by(username="admin").one()
    space = db.query(Space).filter_by(name="客服中心").first() or db.query(Space).order_by(Space.id).first()
    model = db.query(ModelEndpoint).filter_by(model="deepseek-flash").one()
    calculator = db.query(Tool).filter_by(name="calculator").one()
    if space is None:
        raise RuntimeError("No existing space is available")
    token = issue_token({"sub": str(admin.id)})
    headers = {"Authorization": f"Bearer {token}", "X-Space-Id": str(space.id)}
    with TestClient(app) as client:
        existing = db.query(Agent).filter_by(space_id=space.id, key="deepseek-general-mmlu").first()
        if existing is None:
            response = client.post("/api/agents", headers=headers, json={
                "name": "DeepSeek 综合能力评测智能体", "key": "deepseek-general-mmlu", "space_id": space.id,
                "model_id": model.id, "tool_names": [calculator.name], "require_plan": False,
                "system_prompt": "Follow the task format exactly. Solve carefully, use the calculator when asked, and do not reveal a final answer before checking it.",
            })
            response.raise_for_status()
            agent_id = response.json()["id"]
        else:
            agent_id = existing.id
        created = []
        for filename in names:
            fixture = json.loads((root / "backend/fixtures" / filename).read_text())
            db.expire_all()
            existing_dataset = db.query(EvalDataset).filter_by(space_id=space.id, name=fixture["name"]).first()
            body = {"name": fixture["name"], "source_url": fixture["source"], "license": fixture["license"],
                    "selection_note": fixture["selection"], "default_metrics": fixture["metrics"], "cases": fixture["cases"]}
            if existing_dataset is None:
                response = client.post("/api/evals/datasets", headers=headers, json=body)
                response.raise_for_status()
                dataset_id = response.json()["id"]
            else:
                dataset_id = existing_dataset.id
                response = client.patch(f"/api/evals/datasets/{dataset_id}", headers=headers, json=body)
                response.raise_for_status()
            created.append({"fixture": filename, "dataset_id": dataset_id, "case_count": len(fixture["cases"]), "category": fixture["category"]})
        config = {"space_id": space.id, "space_name": space.name, "agent_id": agent_id,
                  "agent_name": "DeepSeek 综合能力评测智能体", "model_id": model.id,
                  "model_code": model.model, "tool_id": calculator.id, "sets": created}
        output = root / "backend/reports/mmlu-live-config.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(config, ensure_ascii=False, indent=2))
finally:
    db.close()
