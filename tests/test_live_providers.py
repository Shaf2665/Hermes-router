import json
import threading
import pytest
import router

ADMIN = {'Authorization': 'Bearer sk-test-admin'}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'AUTH_FILE', tmp_path / 'auth.json')
    monkeypatch.setattr(router, 'ENV_FILE_PATH', tmp_path / '.env')
    monkeypatch.setattr(router, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(router, '_AUTH_KEYS', {})
    monkeypatch.setattr(router, 'PROVIDERS', [])
    monkeypatch.setattr(router, 'pool', router.CredentialPool([]))
    monkeypatch.setattr(router, 'ROTATION_MODE', 'round-robin')
    monkeypatch.setenv('ROTATION_MODE', 'round-robin')
    monkeypatch.setattr(router, '_provider_state', {})
    monkeypatch.setattr(router, '_model_state', {})
    monkeypatch.setattr(router, 'cache', router.ResponseCache())
    monkeypatch.setenv('OPENAI_MODEL', 'previous-startup-model')
    monkeypatch.setenv('LOCAL_MODEL', '')
    monkeypatch.setenv('LOCAL_BASE_URL', '')
    monkeypatch.setattr(router, '_probe_provider', lambda p, key, **kw: (True, 1, p['model'], 'ok'))
    monkeypatch.setattr(router, '_resolve_caps', lambda *args: {'rating': 1, 'supports_tools': True, 'tools_confirmed': True, 'reasoning': False})
    yield router.app.test_client()


def test_add_key_and_models_live_preserves_cooldowns_and_inflight_snapshot(client):
    added = client.post('/v1/config/keys/openai', headers=ADMIN, json={'key': 'fake-one', 'model': 'model-a,model-b'})
    assert added.status_code == 200 and added.json['restart_required'] is False
    assert router.PROVIDERS[0]['models'] == ['model-a', 'model-b']
    assert router.pool.get_key('openai', 'model-a') == 'fake-one'
    router.pool.mark_rate_limited('openai', 'fake-one', 'model-a', 60)
    old = router.PROVIDERS[0]
    added = client.post('/v1/config/keys/openai', headers=ADMIN, json={'key': 'fake-two'})
    assert added.json['restart_required'] is False
    assert router.pool.get_key('openai', 'model-a') == 'fake-two'
    assert old['keys'] == ['fake-one']
    assert router.pool.key_requests_for('openai', 'fake-one') == 1
    duplicate = client.post('/v1/config/keys/openai', headers=ADMIN, json={'key': 'fake-two'})
    assert duplicate.json['duplicate'] is True
    assert router.pool.key_count('openai', 'model-a') == 2
    changed = client.post('/v1/config/model/openai', headers=ADMIN, json={'model': 'model-c'})
    assert changed.json['restart_required'] is False
    assert router.PROVIDERS[0]['models'] == ['model-c']
    assert old['models'] == ['model-a', 'model-b']
    assert router.pool.get_key('openai', 'model-c') in ('fake-one', 'fake-two')
    assert router._build_providers()[0]['model'] == 'model-c'
    assert client.delete('/v1/config/model/openai', headers=ADMIN).json['restart_required'] is False
    assert router.PROVIDERS[0]['model'] == router.PROVIDER_MODEL_DEFAULT['openai']
    assert json.loads(router.AUTH_FILE.read_text())['providers']['openai'] == ['fake-one', 'fake-two']


def test_invalid_or_failed_save_leaves_runtime_unchanged(client, monkeypatch):
    assert client.post('/v1/config/keys/openai', headers=ADMIN, json={'key':'fake', 'model': ', ,'}).status_code == 400
    assert not router.AUTH_FILE.exists()
    def fail(*args): raise OSError('read-only')
    monkeypatch.setattr(router, '_atomic_write_text', fail)
    assert client.post('/v1/config/keys/openai', headers=ADMIN, json={'key':'fake'}).status_code == 500
    assert client.post('/v1/config/model/openai', headers=ADMIN, json={'model':'new'}).status_code == 500
    assert router.PROVIDERS == []
    assert router._AUTH_KEYS == {}


def test_application_key_cannot_change_provider(client):
    for endpoint, body in [('keys', {'key':'fake'}), ('model', {'model':'new'})]:
        assert client.post('/v1/config/'+endpoint+'/openai', headers={'Authorization':'Bearer sk-test'}, json=body).status_code == 401


def test_model_change_invalidates_old_cached_answer(client):
    client.post('/v1/config/keys/openai', headers=ADMIN, json={'key':'fake'})
    payload = {'messages': [{'role':'user','content':'hi'}]}
    router.cache.set(payload, {'model':'old'})
    assert client.post('/v1/config/model/openai', headers=ADMIN, json={'model':'new'}).status_code == 200
    assert router.cache.get(payload) is None


def test_stale_capability_results_cannot_overwrite_new_model(client):
    client.post('/v1/config/keys/openai', headers=ADMIN, json={'key':'fake', 'model':'old'})
    old = router.PROVIDERS[0]
    client.post('/v1/config/model/openai', headers=ADMIN, json={'model':'new'})
    router._publish_ratings([old], {'openai': {'model':'old'}}, {('openai','old'): {'rating':99}})
    assert router._provider_state['openai']['model'] == 'new'
    assert ('openai','old') not in router._model_state


def test_key_save_failure_rolls_back_model_override(client, monkeypatch):
    router._env_write_line('OPENAI_MODEL', 'before')
    def fail(*args): raise OSError('read-only auth')
    monkeypatch.setattr(router, '_auth_json_add_key', fail)
    result = client.post('/v1/config/keys/openai', headers=ADMIN, json={'key':'fake', 'model':'after'})
    assert result.status_code == 500
    assert router._env_read_line('OPENAI_MODEL') == 'before'
    assert router.PROVIDERS == []


def test_rotation_applies_live_without_resetting_pool(client):
    provider = {'name': 'test', 'model': 'chat', 'keys': ['one', 'two', 'three']}
    router.pool = router.CredentialPool([provider], mode='round-robin')
    original_pool = router.pool
    assert router.pool.get_key('test', 'chat') == 'one'
    router.pool.mark_rate_limited('test', 'three', 'chat', 60)
    bucket = router.pool.pools['test']['chat']
    cooling = next(entry['cool_until'] for entry in bucket if entry['key'] == 'three')
    result = client.post('/v1/config/rotation', headers=ADMIN, json={'mode': 'seq'})
    assert result.status_code == 200 and result.json['restart_required'] is False
    assert router.ROTATION_MODE == router.pool.mode == 'sequential'
    assert router._env_read_line('ROTATION_MODE') == 'sequential'
    assert client.get('/v1/status', headers=ADMIN).json['rotation']['mode'] == 'sequential'
    assert [router.pool.get_key('test', 'chat') for _ in range(2)] == ['two', 'two']
    result = client.post('/v1/config/rotation', headers=ADMIN, json={'mode': 'round-robin'})
    assert result.status_code == 200 and result.json['restart_required'] is False
    assert [router.pool.get_key('test', 'chat') for _ in range(3)] == ['two', 'one', 'two']
    assert router.pool is original_pool and router.pool.pools['test']['chat'] is bucket
    assert next(entry['cool_until'] for entry in bucket if entry['key'] == 'three') == cooling
    assert router.pool.key_requests_for('test', 'one') == 2
    assert router.pool.key_requests_for('test', 'two') == 4


@pytest.mark.parametrize('body', [[], {'mode': []}, {'mode': 1}, {'mode': 'unknown'}, {}])
def test_invalid_rotation_is_rejected(client, body):
    result = client.post('/v1/config/rotation', headers=ADMIN, json=body)
    assert result.status_code == 400
    assert router.ROTATION_MODE == router.pool.mode == 'round-robin'
    assert not router.ENV_FILE_PATH.exists()


def test_rotation_failed_save_and_application_access_leave_runtime_unchanged(client, monkeypatch):
    body = {'mode': 'sequential'}
    assert client.post('/v1/config/rotation', headers={'Authorization': 'Bearer sk-test'}, json=body).status_code == 401
    def fail(*args): raise OSError('read-only')
    monkeypatch.setattr(router, '_env_write_line', fail)
    result = client.post('/v1/config/rotation', headers=ADMIN, json=body)
    assert result.status_code == 500
    assert router.ROTATION_MODE == router.pool.mode == 'round-robin'
