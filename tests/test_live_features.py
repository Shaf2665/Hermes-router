import pytest
import threading
import router

ADMIN = {'Authorization': 'Bearer sk-test-admin'}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'ENV_FILE_PATH', tmp_path / '.env')
    monkeypatch.setattr(router, 'CACHE_DB_PATH', str(tmp_path / 'cache.db'))
    for name in ('CACHE_TTL', 'CACHE_PERSIST', 'SEMANTIC_CACHE', 'FAST_ROUTE_TOKENS',
                 'AUTO_DISCOVER_MODELS', 'COST_FX_RATE', 'REQUEST_LOG_SIZE'):
        monkeypatch.setattr(router, name, getattr(router, name))
    monkeypatch.setattr(router, 'cache', router.ResponseCache())
    monkeypatch.setattr(router, 'request_log', router.RequestRingBuffer())
    monkeypatch.setattr(router, 'PROVIDERS', [])
    yield router.app.test_client()
    if router.cache._db:
        router.cache._db.close()


@pytest.mark.parametrize('name', ['response_cache', 'semantic_cache', 'persistent_cache',
    'fast_routing', 'model_discovery', 'cost_currency', 'request_log'])
def test_toggles_apply_and_persist_without_restart(client, name):
    for enabled in (False, True, False):
        response = client.post('/v1/config/features/' + name, headers=ADMIN, json={'enabled': enabled})
        assert response.status_code == 200
        assert response.json['restart_required'] is False
        addon = next(a for a in router._features_snapshot()['addons'] if a['name'] == name)
        assert addon['enabled'] is enabled
        assert router._env_read_line(addon['env']) == addon['on' if enabled else 'off']
        if name == 'response_cache':
            router.cache.set({'messages': []}, {'answer': 'cached'})
            assert bool(router.cache.get({'messages': []})) is enabled
        if name == 'request_log':
            router.request_log.append({'status': 'ok'})
            assert bool(router.request_log.snapshot()) is enabled


@pytest.mark.parametrize('body', [{}, {'enabled': 'false'}, {'enabled': 1}, [], True])
def test_toggle_requires_boolean(client, body):
    assert client.post('/v1/config/features/fast_routing', headers=ADMIN, json=body).status_code == 400


def test_failed_save_does_not_change_running_setting(client, monkeypatch):
    before = router.FAST_ROUTE_TOKENS
    def fail(*args):
        raise OSError('read-only')
    monkeypatch.setattr(router, '_env_write_line', fail)
    response = client.post('/v1/config/features/fast_routing', headers=ADMIN, json={'enabled': not bool(before)})
    assert response.status_code == 500
    assert router.FAST_ROUTE_TOKENS == before


def test_unwritable_cache_does_not_report_enabled(client, monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'CACHE_DB_PATH', str(tmp_path / 'missing' / 'cache.db'))
    response = client.post('/v1/config/features/persistent_cache', headers=ADMIN, json={'enabled': True})
    assert response.status_code == 500
    assert not router.cache.persistent
    assert router._env_read_line('CACHE_PERSIST') is None


def test_persistent_toggle_preserves_warm_cache_and_can_reopen(client):
    payload, answer = {'messages': ['hello']}, {'answer': 'cached'}
    router.cache.set(payload, answer, ns='app-a')
    for enabled in (True, False, True):
        result = client.post('/v1/config/features/persistent_cache', headers=ADMIN, json={'enabled': enabled})
        assert result.status_code == 200
        assert router.cache.get(payload, ns='app-a') == answer
        assert router.cache.get(payload, ns='app-b') is None
    restored = router.ResponseCache(persist=True, db_path=router.CACHE_DB_PATH)
    try:
        assert restored.get(payload, ns='app-a') == answer
    finally:
        restored._db.close()


def test_discovery_runs_when_enabled_without_restart(client, monkeypatch):
    done = threading.Event()
    provider = {'name': 'fake', 'keys': ['fake-key']}
    monkeypatch.setattr(router, 'PROVIDERS', [provider])
    monkeypatch.setattr(router, '_refresh_discovered_models', lambda *args: done.set())
    assert client.post('/v1/config/features/model_discovery', headers=ADMIN, json={'enabled': True}).status_code == 200
    assert done.wait(2)


def test_application_key_cannot_toggle(client):
    assert client.post('/v1/config/features/fast_routing', headers={'Authorization': 'Bearer sk-test'}, json={'enabled': True}).status_code == 401
