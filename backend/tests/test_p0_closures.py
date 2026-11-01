import json
from pathlib import Path

from tests.conftest import auth


def _flow(client, headers, agent_id, dataset_id=None, node_agent_id=None):
    response = client.post('/api/workflows', headers=headers, json={
        'name': 'P0 闭环验证', 'agent_id': agent_id, 'eval_dataset_id': dataset_id,
        'definition': {'kind': 'agents', 'entry': 'n1', 'nodes': [{'id': 'n1', 'type': 'agent', 'agent_id': node_agent_id or agent_id}], 'edges': []},
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_workflow_version_gate_and_dataset_change(client):
    headers, _ = auth(client, 'builder')
    agent_id = next(row['id'] for row in client.get('/api/agents', headers=headers).json() if row['key'] == 'support-agent')
    source = json.loads((Path(__file__).parents[1] / 'fixtures/bigbench-simple-arithmetic.json').read_text())
    dataset = client.post('/api/evals/datasets', headers=headers, json={'name': source['name'], 'source_url': source['source'], 'license': source['license'], 'selection_note': source['selection'], 'cases': source['cases'][:1]}).json()
    assert dataset['source_url'] == source['source']
    flow = _flow(client, headers, agent_id, dataset['id'])
    base = f"/api/workflows/{flow['id']}"
    assert client.post(f'{base}/publish', headers=headers, json={'revision': 1}).status_code == 409
    report = client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=headers, json={'workflow_id': flow['id'], 'metrics': source['metrics']})
    assert report.status_code == 200, report.text
    assert report.json()['workflow_revision'] == 1
    assert client.get(f'{base}/eval-gate', headers=headers).json()['ready'] == report.json()['passed']
    if report.json()['passed']:
        assert client.post(f'{base}/publish', headers=headers, json={'revision': 1}).status_code == 200
    changed = client.patch(base, headers=headers, json={'revision': 1, 'name': 'P0 更新后版本'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['revision'] == 2
    assert changed.json()['release'] == 'draft'
    assert client.patch(base, headers=headers, json={'revision': 1, 'name': '过期编辑'}).status_code == 409
    versions = client.get(f'{base}/versions', headers=headers).json()
    assert [row['number'] for row in versions] == [2, 1]
    assert client.get(f'{base}/versions/2/diff', headers=headers).status_code == 200
    restored = client.post(f'{base}/versions/1/restore', headers=headers, json={'revision': 2})
    assert restored.status_code == 200, restored.text
    assert restored.json()['revision'] == 3
    assert restored.json()['name'] == flow['name']
    assert client.get(f'{base}/eval-gate', headers=headers).json()['ready'] is False
    client.post(f"/api/evals/datasets/{dataset['id']}/cases", headers=headers, json={'input_text': '4 + 9 =', 'expected': '13'})
    assert client.get(f'{base}/eval-gate', headers=headers).json()['ready'] is False


def test_failed_run_retry_original_and_current(client):
    headers, _ = auth(client, 'builder')
    agent_id = next(row['id'] for row in client.get('/api/agents', headers=headers).json() if row['key'] == 'support-agent')
    flow = _flow(client, headers, agent_id, node_agent_id=999999)
    base = f"/api/workflows/{flow['id']}"
    failed = client.post(f'{base}/run', headers=headers, json={'message': '1 + 4 =', 'environment': 'test'})
    assert failed.status_code == 200, failed.text
    assert failed.json()['status'] == 'failed'
    first_id = failed.json()['run_id']
    old = client.get(f'/api/workflow-runs/{first_id}', headers=headers).json()
    assert old['workflow_revision'] == 1
    assert old['cursor'] == 'n1'
    node_retry = client.post(f'/api/workflow-runs/{first_id}/retry', headers=headers, json={'version': 'original', 'from_failed_node': True})
    assert node_retry.status_code == 200, node_retry.text
    assert node_retry.json()['status'] == 'failed'
    assert node_retry.json()['retry_of_id'] == first_id
    definition = flow['definition']
    definition['nodes'][0]['agent_id'] = agent_id
    fixed = client.patch(base, headers=headers, json={'revision': 1, 'definition': definition})
    assert fixed.status_code == 200, fixed.text
    current_retry = client.post(f'/api/workflow-runs/{first_id}/retry', headers=headers, json={'version': 'current'})
    assert current_retry.status_code == 200, current_retry.text
    assert current_retry.json()['status'] == 'completed'
    assert current_retry.json()['workflow_revision'] == 2
    assert current_retry.json()['retry_of_id'] == first_id


def test_eval_stream_reports_progress_and_result(client):
    headers, _ = auth(client, 'builder')
    agent_id = next(row['id'] for row in client.get('/api/agents', headers=headers).json() if row['key'] == 'support-agent')
    dataset = client.post('/api/evals/datasets', headers=headers, json={'name': '流式门禁', 'cases': [{'input_text': '计算 1+1', 'expected': '2'}]}).json()
    flow = _flow(client, headers, agent_id, dataset['id'])
    response = client.post(f"/api/evals/datasets/{dataset['id']}/run-stream", headers=headers, json={'workflow_id': flow['id'], 'metrics': ['completion']})
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert [item['event'] for item in events] == ['progress', 'complete']
    assert events[0]['done'] == 1 and events[0]['total'] == 1
    assert events[1]['report']['workflow_revision'] == 1
    history = client.get(f"/api/evals/datasets/{dataset['id']}/runs", headers=headers, params={'page': 1})
    assert history.status_code == 200 and history.json()['total'] == 1
    saved = client.get(f"/api/evals/runs/{events[1]['report']['id']}", headers=headers)
    assert saved.status_code == 200
    assert saved.json()['agent']['name']
    assert len(saved.json()['details']) == 1


def test_paused_run_resumes_its_original_definition(client):
    headers, _ = auth(client, 'builder')
    agent_id = next(row['id'] for row in client.get('/api/agents', headers=headers).json() if row['key'] == 'support-agent')
    definition = {'kind': 'agents', 'entry': 'a', 'nodes': [
        {'id': 'a', 'type': 'agent', 'agent_id': agent_id},
        {'id': 'b', 'type': 'agent', 'agent_id': agent_id},
    ], 'edges': [{'src': 'a', 'dst': 'b'}]}
    flow = client.post('/api/workflows', headers=headers, json={'name': '快照继续', 'agent_id': agent_id, 'definition': definition}).json()
    paused = client.post(f"/api/workflows/{flow['id']}/run", headers=headers, json={'message': '计算 1+1', 'environment': 'test', 'pause_after': 1})
    assert paused.status_code == 200 and paused.json()['status'] == 'paused', paused.text
    new_definition = {**definition, 'nodes': definition['nodes'][:1], 'edges': []}
    changed = client.patch(f"/api/workflows/{flow['id']}", headers=headers, json={'revision': 1, 'definition': new_definition})
    assert changed.status_code == 200, changed.text
    resumed = client.post(f"/api/workflows/{flow['id']}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['status'] == 'completed'
    detail = client.get(f"/api/workflow-runs/{paused.json()['run_id']}", headers=headers).json()
    assert detail['workflow_revision'] == 1
    assert any(item.get('node') == 'b' and item.get('event') == '调用' for item in detail['logs'])


def test_home_space_members_see_platform_workflow_and_admin_only_governance(client, app):
    from app.models import Membership, Space, User

    admin, support = auth(client, 'admin')
    builder, _ = auth(client, 'builder')
    agent_id = next(row['id'] for row in client.get('/api/agents', headers=admin).json() if row['key'] == 'support-agent')
    flow = _flow(client, admin, agent_id)
    listed = client.get('/api/workflows', headers=builder).json()
    assert any(row['id'] == flow['id'] for row in listed)
    assert client.get(f"/api/workflows/{flow['id']}", headers=builder).status_code == 200
    assert client.put('/api/spaces/current/policy', headers=builder, json={
        'deny': [], 'mask': [], 'isolation': 'user,agent', 'retention_days': 30,
    }).status_code == 403

    # Membership in a second space should extend read lists without switching context.
    with app.state.session_factory() as db:
        finance = db.query(Space).filter_by(key='finance').one()
        user = db.query(User).filter_by(username='builder').one()
        db.add(Membership(user_id=user.id, space_id=finance.id, role='viewer'))
        db.commit()
    finance_headers, _ = auth(client, 'finance', space_key='finance')
    finance_agent = next(row['id'] for row in client.get('/api/agents', headers=finance_headers).json() if row['key'] == 'finance-agent')
    finance_flow = _flow(client, finance_headers, finance_agent)
    finance_dataset = client.post('/api/evals/datasets', headers=finance_headers, json={
        'name': '财务空间评测集', 'cases': [{'input_text': '1+1', 'expected': '2'}],
    }).json()
    visible = client.get('/api/workflows', headers=builder).json()
    assert {flow['id'], finance_flow['id']}.issubset({row['id'] for row in visible})
    assert client.get(f"/api/workflows/{finance_flow['id']}", headers=builder).status_code == 200
    assert any(row['id'] == finance_dataset['id'] for row in client.get('/api/evals/datasets', headers=builder).json())
    assert any(row['id'] == finance_dataset['id'] for row in client.get('/api/evals/datasets', headers=admin).json())
    assert client.patch(f"/api/evals/datasets/{finance_dataset['id']}", headers=admin, json={
        'name': '财务空间评测集', 'default_metrics': ['completion'],
    }).status_code == 200
    assert client.patch(f"/api/evals/datasets/{finance_dataset['id']}", headers=builder, json={
        'name': '无权编辑', 'default_metrics': ['completion'],
    }).status_code == 403
    assert client.post(f"/api/evals/datasets/{finance_dataset['id']}/run", headers=builder, json={
        'agent_id': finance_agent, 'metrics': ['completion'],
    }).status_code == 403


def test_agent_draft_edits_create_immutable_versions(client):
    builder, _ = auth(client, 'builder')
    row = client.post('/api/agents', headers=builder, json={'name': '版本测试', 'key': 'immutable-version-check', 'system_prompt': '原始提示词'}).json()
    first = client.get(f"/api/agents/{row['id']}", headers=builder).json()['versions']
    assert first[0]['number'] == 1
    changed = client.patch(f"/api/agents/{row['id']}", headers=builder, json={'system_prompt': '更新提示词'})
    assert changed.status_code == 200, changed.text
    versions = client.get(f"/api/agents/{row['id']}", headers=builder).json()['versions']
    assert [item['number'] for item in versions] == [1, 2]
    assert versions[0]['system_prompt'] == '原始提示词'


def test_eval_pass_ratio_and_version_report_history(client):
    builder, _ = auth(client, 'builder')
    agent_id = next(row['id'] for row in client.get('/api/agents', headers=builder).json() if row['key'] == 'support-agent')
    dataset = client.post('/api/evals/datasets', headers=builder, json={
        'name': '比例门槛验证',
        'cases': [
            {'input_text': '计算 1+1', 'expected': '2'},
            {'input_text': '计算 1+1', 'expected': '__IMPOSSIBLE_MATCH__'},
        ],
    }).json()
    run_url = f"/api/evals/datasets/{dataset['id']}/run"
    lower = client.post(run_url, headers=builder, json={'agent_id': agent_id, 'metrics': ['contains'], 'min_pass_rate': 0.5})
    assert lower.status_code == 200, lower.text
    assert lower.json()['completion'] == 0.5 and lower.json()['passed'] is True
    strict = client.post(run_url, headers=builder, json={'agent_id': agent_id, 'metrics': ['contains'], 'min_pass_rate': 1.0})
    assert strict.status_code == 200, strict.text
    assert strict.json()['completion'] == 0.5 and strict.json()['passed'] is False
    version = client.get(f'/api/agents/{agent_id}', headers=builder).json()['versions'][-1]['number']
    history = client.get(f'/api/evals/targets/agent/{agent_id}/runs', headers=builder, params={'version': version, 'page': 1}).json()
    assert {lower.json()['id'], strict.json()['id']}.issubset({row['id'] for row in history['items']})
    assert all(row['target_version'] == version for row in history['items'])


def test_eval_history_version_note_search_and_delete(client):
    builder, _ = auth(client, 'builder')
    agent = client.post('/api/agents', headers=builder, json={
        'name': '版本评测智能体', 'key': 'eval-version-choice', 'system_prompt': '请回答计算问题',
    }).json()
    first = agent['versions'][0]['id']
    edited = client.patch(f"/api/agents/{agent['id']}", headers=builder, json={'system_prompt': '新版提示词'})
    assert edited.status_code == 200, edited.text
    assert edited.json()['versions'][-1]['id'] != first
    dataset = client.post('/api/evals/datasets', headers=builder, json={
        'name': '版本评测数据', 'cases': [{'input_text': '计算 1+1', 'expected': '2'}],
    }).json()
    run = client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=builder, json={
        'agent_id': agent['id'], 'agent_version_id': first, 'metrics': ['contains'], 'min_pass_rate': 0.8,
    })
    assert run.status_code == 200, run.text
    assert run.json()['version_id'] == first
    report_id = run.json()['id']
    detail = client.get(f'/api/evals/runs/{report_id}', headers=builder).json()
    assert detail['target_version'] == 1
    assert detail['details'][0]['input'] == '计算 1+1'
    assert detail['details'][0]['expected'] == '2'
    streamed = client.post(f"/api/evals/datasets/{dataset['id']}/run-stream", headers=builder, json={
        'agent_id': agent['id'], 'agent_version_id': first, 'metrics': ['contains'], 'min_pass_rate': 0.8,
    })
    assert streamed.status_code == 200, streamed.text
    events = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith('data: ')]
    assert events[-1]['report']['version_id'] == first
    invalid = client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=builder, json={
        'agent_id': agent['id'], 'agent_version_id': 99999, 'metrics': ['contains'],
    })
    assert invalid.status_code == 400
    updated = client.patch(f'/api/evals/runs/{report_id}', headers=builder, json={'note': '人工复核完成'})
    assert updated.status_code == 200 and updated.json()['note'] == '人工复核完成'
    viewer, _ = auth(client, 'viewer')
    assert client.patch(f'/api/evals/runs/{report_id}', headers=viewer, json={'note': '无权修改'}).status_code == 403
    assert client.delete(f'/api/evals/runs/{report_id}', headers=viewer).status_code == 403
    history = client.get(f"/api/evals/datasets/{dataset['id']}/runs", headers=builder, params={'page': 1, 'page_size': 1, 'q': '人工复核'}).json()
    assert history['total'] == 1 and history['items'][0]['id'] == report_id
    passed = client.get(f"/api/evals/datasets/{dataset['id']}/runs", headers=builder, params={'page': 1, 'passed': 'true'}).json()
    assert all(item['passed'] for item in passed['items'])
    deleted = client.delete(f'/api/evals/runs/{report_id}', headers=builder)
    assert deleted.status_code == 200
    assert client.get(f'/api/evals/runs/{report_id}', headers=builder).status_code == 404
    assert client.get(f"/api/evals/datasets/{dataset['id']}/runs", headers=builder, params={'page': 1}).json()['total'] == 1


def test_platform_can_evaluate_other_space_without_exposing_report(client):
    admin, _ = auth(client, 'admin')
    builder, _ = auth(client, 'builder')
    finance, _ = auth(client, 'finance', space_key='finance')
    finance_agent = next(row for row in client.get('/api/agents', headers=finance).json() if row['key'] == 'finance-agent')
    dataset = client.post('/api/evals/datasets', headers=builder, json={
        'name': '跨空间管理员评测', 'cases': [{'input_text': '计算 1+1', 'expected': '2'}],
    }).json()
    report = client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=admin, json={
        'agent_id': finance_agent['id'], 'metrics': ['completion'],
    })
    assert report.status_code == 200, report.text
    report_id = report.json()['id']
    assert report.json()['target_space_id'] == finance_agent['space_id']
    assert client.get(f'/api/evals/runs/{report_id}', headers=admin).status_code == 200
    assert client.get(f'/api/evals/runs/{report_id}', headers=builder).status_code == 404
    assert client.get(f"/api/evals/datasets/{dataset['id']}/runs", headers=builder, params={'page': 1}).json()['total'] == 0


def test_unassigned_platform_agent_requires_version_eval_before_release(client):
    admin, _ = auth(client, 'admin')
    builder, _ = auth(client, 'builder')
    agent_response = client.post('/api/agents', headers=admin, json={
        'name': '平台待评测智能体', 'key': 'platform-eval-gate', 'unassigned': True,
        'require_plan': False, 'tool_names': ['calculator'],
    })
    assert agent_response.status_code == 200, agent_response.text
    agent = agent_response.json()
    assert agent['space_id'] is None
    assert any(row['id'] == agent['id'] for row in client.get('/api/agents', headers=admin).json())
    assert all(row['id'] != agent['id'] for row in client.get('/api/agents', headers=builder).json())
    version_id = agent['versions'][0]['id']
    dataset = client.post('/api/evals/datasets', headers=builder, json={
        'name': '平台待评测数据', 'cases': [{'input_text': '计算 1+1', 'expected': '2'}],
    }).json()
    base = f"/api/agents/{agent['id']}"
    assert client.post(f'{base}/publish', headers=admin, json={'gray_percent': 0}).status_code == 409
    assert client.post(f'{base}/versions/1/promote', headers=admin, json={'target': 'stable'}).status_code == 409
    assert client.post(f'{base}/versions/1/promote', headers=admin, json={'target': 'gray', 'percent': 10}).status_code == 409
    assert client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=builder, json={
        'agent_id': agent['id'], 'agent_version_id': version_id, 'metrics': ['contains'],
    }).status_code == 403
    report = client.post(f"/api/evals/datasets/{dataset['id']}/run", headers=admin, json={
        'agent_id': agent['id'], 'agent_version_id': version_id, 'metrics': ['contains'],
    })
    assert report.status_code == 200, report.text
    assert report.json()['passed'] is True, report.json()['details'][0]['output']
    report_id = report.json()['id']
    assert client.get(f'/api/evals/runs/{report_id}', headers=admin).json()['platform_only'] is True
    assert client.get(f'/api/evals/runs/{report_id}', headers=builder).status_code == 404
    assert client.get(f"/api/evals/datasets/{dataset['id']}/runs", headers=builder, params={'page': 1}).json()['total'] == 0
    own_dataset = next(row for row in client.get('/api/evals/datasets', headers=builder).json() if row['id'] == dataset['id'])
    assert own_dataset['latest_agent'] == ''
    assert client.get(f"/api/evals/targets/agent/{agent['id']}/runs", headers=admin, params={'page': 1}).json()['total'] == 1
    promoted = client.post(f'{base}/versions/1/promote', headers=admin, json={'target': 'stable'})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()['stable_version'] == 1


def test_startup_withdraws_legacy_unassessed_platform_release(app):
    from app.core import create_app
    from app.models import Agent, AgentVersion

    with app.state.session_factory() as db:
        agent = Agent(name='旧版误上线', key='legacy-unassessed', workload_id='wl_legacy_unassessed', space_id=None, status='published', require_plan=False)
        db.add(agent)
        db.flush()
        version = AgentVersion(agent_id=agent.id, number=1, system_prompt='保留内容', status='published', eval_passed=False)
        db.add(version)
        db.flush()
        agent.stable_version_id = version.id
        agent.gray_version_id = version.id
        agent.gray_percent = 20
        agent_id = agent.id
        db.commit()

    restarted = create_app()
    with restarted.state.session_factory() as db:
        agent = db.get(Agent, agent_id)
        version = db.query(AgentVersion).filter_by(agent_id=agent_id).one()
        assert (agent.status, agent.stable_version_id, agent.gray_version_id, agent.gray_percent) == ('draft', None, None, 0)
        assert (version.status, version.system_prompt) == ('draft', '保留内容')
