from tests.conftest import auth
from app.models import Memory
from app.services.memory_layers import ensure_user_profile_backfill, _store_extracted


def create(client, headers, content):
    response = client.post('/api/memories', headers=headers, json={'kind': 'user', 'content': content})
    assert response.status_code == 200, response.text
    return response.json()


def versions(client, headers, record_id):
    return client.get(f'/api/memories/{record_id}', headers=headers).json()['detail']['versions']


def test_edit_history_and_rollback_keep_increasing(client):
    headers, _ = auth(client, 'builder')
    first = create(client, headers, '所在地：北京')
    second = create(client, headers, '所在地：上海')
    edited = client.patch(f"/api/memories/{first['id']}", headers=headers, json={'content': '所在地：杭州'})
    assert edited.status_code == 200, edited.text
    fresh = edited.json()
    history = versions(client, headers, fresh['id'])
    assert [v['level'] for v in history] == [3, 2, 1]
    assert [v['id'] for v in history if v['active']] == [fresh['id']]
    assert history[1]['content'] == '所在地：上海'
    assert history[2]['content'] == '所在地：北京'
    client.post(f"/api/memories/{first['id']}/activate", headers=headers)
    fourth = create(client, headers, '所在地：深圳')
    assert [v['level'] for v in versions(client, headers, fourth['id'])] == [4, 3, 2, 1]
    unchanged = client.patch(f"/api/memories/{fourth['id']}", headers=headers, json={'content': '所在地：深圳'})
    assert unchanged.json()['id'] == fourth['id']
    assert len(versions(client, headers, fourth['id'])) == 4


def test_extraction_after_rollback_and_import(client):
    headers, space = auth(client, 'builder')
    first = create(client, headers, '所在地：北京')
    create(client, headers, '所在地：上海')
    client.post(f"/api/memories/{first['id']}/activate", headers=headers)
    with client.app.state.session_factory() as db:
        row = db.get(Memory, first['id'])
        _store_extracted(db, data={'user_memory': ['所在地：杭州']}, space_id=space['id'], user_id=row.user_id,
                         agent_id=None, source_kind='test', source_id='', source_name='test', task_id='', retention_days=30,
                         summary_text='', dialogue=[], prior_summaries=[], write_summary=False, parent_summary_id=None, level=0)
        db.commit()
    history = versions(client, headers, first['id'])
    assert [v['level'] for v in history] == [3, 2, 1]
    assert sum(v['active'] for v in history) == 1
    response = client.post('/api/memories/import', headers=headers, json={'items': [
        {'kind': 'user', 'content': '所在地：广州'}, {'kind': 'user', 'content': '所在地：天津'}]})
    assert response.status_code == 200, response.text
    history = versions(client, headers, first['id'])
    assert [v['level'] for v in history] == [5, 4, 3, 2, 1]
    assert sum(v['active'] for v in history) == 1


def test_backfill_repairs_duplicate_numbers_without_replacing_content(client):
    headers, _ = auth(client, 'builder')
    first = create(client, headers, '所在地：北京')
    second = create(client, headers, '所在地：上海')
    third = create(client, headers, '所在地：杭州')
    with client.app.state.session_factory() as db:
        old = db.get(Memory, second['id'])
        latest = db.get(Memory, third['id'])
        old.active = True
        latest.level = 2
        latest.parent_summary_id = first['id']
        ensure_user_profile_backfill(db)
        db.commit()
        assert old.content == '所在地：上海'
        assert latest.content == '所在地：杭州'
        assert latest.level == 3
        assert latest.active and not old.active
        ensure_user_profile_backfill(db)
        db.commit()
        assert latest.level == 3


def test_numbering_isolated_by_user_and_department(client):
    owner, _ = auth(client, 'builder')
    other, _ = auth(client, 'operator')
    finance, _ = auth(client, 'admin', space_key='finance')
    first = create(client, owner, '所在地：北京')
    create(client, owner, '所在地：上海')
    for headers in (other, finance):
        record = create(client, headers, '所在地：广州')
        assert [v['level'] for v in versions(client, headers, record['id'])] == [1]
    client.post(f"/api/memories/{first['id']}/activate", headers=owner)
    with client.app.state.session_factory() as db:
        ensure_user_profile_backfill(db)
        db.commit()
    history = versions(client, owner, first['id'])
    assert [v['level'] for v in history] == [2, 1]
    assert [v['id'] for v in history if v['active']] == [first['id']]
