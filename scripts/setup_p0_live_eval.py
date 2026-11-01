"""Create visible P0 benchmark agents, datasets and workflows in a dedicated space.

Idempotent. Uses the project's configured DeepSeek endpoint and calculator tool.
Does not run paid model calls; see run_p0_live_suite.py.
"""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from app.core import create_app
from app.models import User, Space, Agent, EvalDataset, Workflow, ModelEndpoint, Tool
from app.security import issue_token

root = Path(__file__).resolve().parents[1]
fixtures = [
    'bigbench-word-unscrambling-250.json', 'bigbench-text-editing-47.json',
    'bigbench-navigate-250.json', 'bigbench-calculation-250.json',
    'bigbench-tool_calling-250.json',
]
source = root / 'backend/data/app.db'
app = create_app(f'sqlite:///{source}')
db = app.state.session_factory()
try:
    admin = db.query(User).filter_by(username='admin').one()
    model = db.query(ModelEndpoint).filter_by(model='deepseek-flash').first()
    calculator = db.query(Tool).filter_by(name='calculator').first()
    if model is None or calculator is None:
        raise RuntimeError('This benchmark requires a configured deepseek-flash endpoint and calculator tool')
    space = db.query(Space).filter_by(name='P0 开源基准评测').first()
    token = issue_token({'sub': str(admin.id)})
    admin_headers = {'Authorization': f'Bearer {token}', 'X-Space-Id': '1'}
    with TestClient(app) as client:
        if space is None:
            result = client.post('/api/departments', headers=admin_headers, json={
                'name': 'P0 开源基准评测', 'model_ids': [model.id], 'tool_ids': [calculator.id],
                'workflow_ids': [], 'knowledge_ids': [], 'skill_ids': [],
            })
            result.raise_for_status()
            space_id = result.json()['id']
        else:
            space_id = space.id
        headers = {'Authorization': f'Bearer {token}', 'X-Space-Id': str(space_id)}
        db.expire_all()
        agent = db.query(Agent).filter_by(space_id=space_id, key='p0-deepseek-general').first()
        if agent is None:
            result = client.post('/api/agents', headers=headers, json={
                'name': 'DeepSeek 通用能力评测智能体', 'key': 'p0-deepseek-general',
                'space_id': space_id, 'model_id': model.id, 'tool_names': ['calculator'],
                'system_prompt': 'Follow the user instruction exactly. Use calculator for arithmetic when needed. Return only the requested answer format.',
                'require_plan': False,
            })
            result.raise_for_status()
            agent_id = result.json()['id']
        else:
            agent_id = agent.id
        mapping = []
        for name in fixtures:
            fixture = json.loads((root / 'backend/fixtures' / name).read_text())
            db.expire_all()
            dataset = db.query(EvalDataset).filter_by(space_id=space_id, name=fixture['name']).first()
            if dataset is None:
                result = client.post('/api/evals/datasets', headers=headers, json={'name': fixture['name'], 'source_url': fixture['source'], 'license': fixture['license'], 'selection_note': fixture['selection'], 'default_metrics': fixture['metrics'], 'cases': fixture['cases']})
                result.raise_for_status()
                dataset_id = result.json()['id']
            else:
                dataset_id = dataset.id
                client.patch(f'/api/evals/datasets/{dataset_id}', headers=headers, json={'name': fixture['name'], 'source_url': fixture['source'], 'license': fixture['license'], 'selection_note': fixture['selection'], 'default_metrics': fixture['metrics']}).raise_for_status()
            workflow_name = f"P0 {name.removeprefix('bigbench-').removesuffix('.json')}"
            db.expire_all()
            workflow = db.query(Workflow).filter_by(space_id=space_id, name=workflow_name).first()
            if workflow is None:
                prompt = {
                    'generation': 'Follow the task exactly. For unscrambling output the word only; for text editing output the edited text only.',
                    'task_decomposition': 'Break the navigation into numbered steps; end with Answer: True or Answer: False.',
                    'calculation': 'Use calculator for arithmetic. Output only the number.',
                    'tool_calling': 'Call calculator to solve the arithmetic expression. Output only the number.',
                }[fixture['category']]
                result = client.post('/api/workflows', headers=headers, json={
                    'name': workflow_name, 'agent_id': agent_id, 'eval_dataset_id': dataset_id,
                    'definition': {'kind': 'agents', 'entry': 'n1', 'nodes': [{
                        'id': 'n1', 'type': 'agent', 'agent_id': agent_id, 'prompt': prompt,
                        'tool_names': ['calculator'] if fixture['category'] in ('calculation', 'tool_calling') else [],
                    }], 'edges': []},
                })
                result.raise_for_status()
                workflow_id = result.json()['id']
            else:
                workflow_id = workflow.id
            mapping.append({'fixture': name, 'category': fixture['category'], 'dataset_id': dataset_id,
                            'workflow_id': workflow_id, 'case_count': len(fixture['cases'])})
        config = {'space_id': space_id, 'space_name': 'P0 开源基准评测', 'agent_id': agent_id,
                  'agent_name': 'DeepSeek 通用能力评测智能体', 'model_id': model.id,
                  'model_name': model.name, 'model_code': model.model, 'sets': mapping}
        path = root / 'backend/reports/p0-live-config.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(config, ensure_ascii=False, indent=2))
finally:
    db.close()
