from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import router
from usage_store import KeyUsage

ADMIN = {'Authorization': 'Bearer sk-test-admin'}
APP = {'Authorization': 'Bearer sk-test'}


def test_owner_boundary_and_caller_usage():
    client = router.app.test_client()
    for path in ['/v1/status', '/v1/logs', '/v1/config/providers', '/v1/config/proxy-keys', '/v1/instances', '/metrics']:
        assert client.get(path, headers=APP).status_code == 401
        assert client.get(path, headers=ADMIN).status_code == 200
    own = client.get('/v1/usage', headers=APP).get_json()
    assert [k['key_tail'] for k in own['keys']] == ['k-test']
    assert own['providers'] == {}
    assert client.get('/v1/models', headers=ADMIN).status_code == 401


def test_session_csrf_origin_expiry(monkeypatch):
    client = router.app.test_client()
    assert client.post('/v1/admin/session', json={'api_key': 'sk-test'}).status_code == 401
    assert client.post('/v1/admin/session', json={'api_key': 'sk-test-admin'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    login = client.post('/v1/admin/session', json={'api_key': 'sk-test-admin'})
    assert login.status_code == 200
    assert 'HttpOnly' in login.headers['Set-Cookie'] and 'SameSite=Strict' in login.headers['Set-Cookie']
    csrf = login.get_json()['csrf_token']
    assert client.get('/v1/admin/session').get_json()['csrf_token'] == csrf
    assert client.post('/v1/config/proxy-keys', json={}).status_code == 403
    assert client.delete('/v1/admin/session', headers={'X-CSRF-Token': csrf, 'Origin': 'https://evil.example'}).status_code == 403
    monkeypatch.setattr(router.time, 'time', lambda: time.monotonic()+10**12)
    assert client.get('/v1/status').status_code == 401


def test_create_scope_revoke_without_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'ENV_FILE_PATH', tmp_path/'router.env')
    monkeypatch.setattr(router, 'AUTH_FILE', tmp_path/'auth.json')
    monkeypatch.setattr(router, 'PROXY_API_KEYS', list(router.PROXY_API_KEYS))
    monkeypatch.setattr(router, 'KEY_LIMITS', dict(router.KEY_LIMITS))
    monkeypatch.setattr(router, 'KEY_PROVIDER_SCOPE', dict(router.KEY_PROVIDER_SCOPE))
    monkeypatch.setattr(router, 'KEY_MODEL_SCOPE', dict(router.KEY_MODEL_SCOPE))
    monkeypatch.setenv('PROXY_API_KEYS', ','.join(router.PROXY_API_KEYS))
    client = router.app.test_client()
    made = client.post('/v1/config/proxy-keys', headers=ADMIN, json={'name': 'test', 'allowed_providers': ['gemini']})
    assert made.status_code == 200
    assert made.get_json()['restart_required'] is False
    key = made.get_json()['key']; tail = key[-6:]
    assert client.get('/v1/models', headers={'Authorization': 'Bearer '+key}).status_code == 200
    assert router.KEY_PROVIDER_SCOPE[key] == {'gemini'}
    assert client.post('/v1/config/proxy-keys/'+tail, headers=ADMIN, json={'allowed_providers': ['openai']}).status_code == 200
    assert router.KEY_PROVIDER_SCOPE[key] == {'openai'}
    assert client.delete('/v1/config/proxy-keys/'+tail, headers=ADMIN).status_code == 200
    assert client.get('/v1/models', headers={'Authorization': 'Bearer '+key}).status_code == 401


def test_durable_concurrent_reservations_and_idempotent_settlement(tmp_path):
    path = tmp_path/'usage.db'
    ledger = KeyUsage(path)
    def reserve(_):
        return ledger.reserve('secret', {'tokens_per_day': 10, 'cost_per_day': 1}, 6, .6)[0]
    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(reserve, range(8)))
    assert len([rid for rid in ids if rid]) == 1
    restarted = KeyUsage(path)
    assert restarted.snapshot('secret')['cost_today'] == .6
    rid = next(rid for rid in ids if rid)
    restarted.settle(rid, 4, .4)
    restarted.settle(rid, 9, .9)
    assert restarted.snapshot('secret')['cost_today'] == .4
    rid, _, _ = restarted.reserve('secret', {}, 5, .5)
    restarted.settle(rid, failed=True)
    assert restarted.snapshot('secret')['tokens_today'] == 4


def test_budget_unknown_units_fail_closed(monkeypatch):
    monkeypatch.setitem(router.KEY_LIMITS, 'sk-test', {'cost_per_day': 1})
    client = router.app.test_client()
    response = client.post('/v1/chat/completions', headers=APP, json={'messages': [{'role': 'user', 'content': 'hi'}]})
    assert response.status_code == 400
    assert 'bound' in response.get_json()['error']['message']


def test_settlement_missing_usage_cached_and_failure(monkeypatch, tmp_path):
    ledger = KeyUsage(tmp_path/'usage.db')
    monkeypatch.setattr(router, 'key_usage', ledger)
    rid, _, _ = ledger.reserve('key', {}, 50, .5)
    router._settle_request_usage(rid, None, 'gpt-4o-mini')
    own = ledger.snapshot('key')
    assert own['cost_today'] == .5 and own['estimated_requests'] == 1
    rid, _, _ = ledger.reserve('key', {}, 50, .5)
    router._settle_request_usage(rid, {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}, 'gpt-4o-mini', cached=True)
    assert ledger.snapshot('key')['cost_today'] == .5
    assert ledger.snapshot('key')['cached_requests'] == 1
    rid, _, _ = ledger.reserve('key', {}, 50, .5)
    router._settle_request_usage(rid, None, '', failed=True)
    assert ledger.snapshot('key')['tokens_today'] == 55
    # Malformed upstream usage cannot release a conservative reservation.
    rid, _, _ = ledger.reserve('key', {}, 50, .5)
    router._settle_request_usage(rid, {'total_tokens': -1, 'prompt_tokens': 'oops'}, 'gpt-4o-mini')
    assert ledger.snapshot('key')['cost_today'] == 1


def test_budget_candidate_unknown_prices_and_multiple_outputs(monkeypatch):
    monkeypatch.setitem(router.KEY_LIMITS, 'sk-test', {'cost_per_day': 1})
    with router.app.test_request_context(headers=APP):
        assert not router._budget_candidate_allowed({'name': 'openai'}, 'unknown-2026-model', {'max_tokens': 20})
        assert router._budget_candidate_allowed({'name': 'openai'}, 'gpt-4o-mini', {'max_tokens': 20})
    response = router.app.test_client().post('/v1/chat/completions', headers=APP,
                json={'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 20, 'n': 5})
    assert response.status_code == 400


def test_session_logout_success_and_secure_proxy(monkeypatch):
    client = router.app.test_client()
    response = client.post('/v1/admin/session', json={'api_key': 'sk-test-admin'},
                           headers={'X-Forwarded-Proto': 'https'})
    assert 'Secure;' not in response.headers['Set-Cookie']
    csrf = response.get_json()['csrf_token']
    assert client.delete('/v1/admin/session', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/v1/status').status_code == 401
    from werkzeug.middleware.proxy_fix import ProxyFix
    monkeypatch.setattr(router.app, 'wsgi_app', ProxyFix(router.app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0))
    response = client.post('/v1/admin/session', json={'api_key': 'sk-test-admin'},
                 headers={'X-Forwarded-Proto': 'https', 'Origin': 'https://localhost'})
    assert response.status_code == 200 and 'Secure;' in response.headers['Set-Cookie']


def test_bootstrap_custom_env_preserves_process_precedence_and_no_secret_logs(tmp_path):
    root = Path(router.__file__).parent
    env_file = tmp_path/'state'/'router.env'
    env_file.parent.mkdir()
    env_file.write_text('PROXY_API_KEYS=file-app-key\nADMIN_API_KEY=file-owner-key\nPORT=9000\n')
    env = os.environ.copy()
    env.pop('PROXY_API_KEYS', None); env.pop('ADMIN_API_KEY', None)
    env.update(HR_ENV_FILE=str(env_file), HR_USAGE_DB=str(tmp_path/'usage.db'),
               ROUTER_AUTH_FILE=str(tmp_path/'auth.json'), PYTHONPATH=str(root), PORT='9123')
    script = 'import router,json;print(json.dumps([router.PROXY_API_KEYS,router.ADMIN_API_KEY,router.PORT]))'
    first = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(first.stdout) == [['file-app-key'], 'file-owner-key', 9123]
    env_file.write_text('# preserved\n')
    boot = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path, capture_output=True, text=True, check=True)
    keys, admin, port = json.loads(boot.stdout)
    assert admin.startswith('sk-admin-') and admin != keys[0]
    assert admin in env_file.read_text() and keys[0] in env_file.read_text()
    assert admin not in boot.stderr and keys[0] not in boot.stderr
    second = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(second.stdout) == [keys, admin, port]


def test_restart_unsupported_is_explicit(monkeypatch):
    monkeypatch.setattr(router, '_trigger_restart', lambda: False)
    response = router.app.test_client().post('/v1/config/restart', headers=ADMIN)
    assert response.status_code == 409 and response.get_json()['supported'] is False


def test_concurrent_key_creations_and_last_revocation(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'ENV_FILE_PATH', tmp_path/'router.env')
    monkeypatch.setattr(router, 'AUTH_FILE', tmp_path/'auth.json')
    monkeypatch.setattr(router, 'PROXY_API_KEYS', [])
    monkeypatch.setattr(router, 'KEY_LIMITS', {})
    monkeypatch.setattr(router, 'KEY_PROVIDER_SCOPE', {})
    monkeypatch.setattr(router, 'KEY_MODEL_SCOPE', {})
    monkeypatch.setenv('PROXY_API_KEYS', '')
    def create(index):
        with router.app.test_client() as client:
            response = client.post('/v1/config/proxy-keys', headers=ADMIN, json={'name': str(index)})
            assert response.status_code == 200
            return response.get_json()['key']
    with ThreadPoolExecutor(max_workers=6) as executor:
        keys = list(executor.map(create, range(6)))
    doc = json.loads(router.AUTH_FILE.read_text())
    assert set(doc['access_keys']) == set(keys)
    assert len(doc['proxy_keys']) == 6
    for key in keys:
        assert router.app.test_client().delete('/v1/config/proxy-keys/'+key[-6:], headers=ADMIN).status_code == 200
    assert router.PROXY_API_KEYS == []
    assert router.app.test_client().get('/v1/config/proxy-keys', headers=ADMIN).status_code == 200


def test_scope_snapshot_and_cache_policy_identity(monkeypatch):
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, 'sk-test', {'openai'})
    monkeypatch.setitem(router.KEY_MODEL_SCOPE, 'sk-test', {'gpt-4o-mini'})
    with router.app.test_request_context(headers=APP):
        assert router._auth_check() is None
        old = router._cache_ns()
        assert not router._budget_candidate_allowed({'name': 'openai'}, 'gpt-4o', {'max_tokens': 20})
        router.KEY_PROVIDER_SCOPE['sk-test'] = {'gemini'}
        assert router._caller_scope() == {'openai'}  # admitted authorization remains stable
    with router.app.test_request_context(headers=APP):
        assert router._auth_check() is None
        assert router._cache_ns() != old


def test_invalid_mutation_and_corrupt_store_do_not_destroy_config(monkeypatch, tmp_path):
    client = router.app.test_client()
    assert client.post('/v1/config/proxy-keys', headers=ADMIN, json=['unexpected']).status_code == 400
    assert client.post('/v1/config/proxy-keys', headers=ADMIN, json={'allowed_providers': False}).status_code == 400
    assert client.post('/v1/config/proxy-keys', headers=ADMIN, json={'cost_per_day': 'NaN'}).status_code == 400
    path = tmp_path/'auth.json'; path.write_text('{broken')
    monkeypatch.setattr(router, 'AUTH_FILE', path)
    import pytest
    with pytest.raises(json.JSONDecodeError):
        router._auth_json_add_key('openai', 'test-provider')
    assert path.read_text() == '{broken'


def test_http_stream_settles_usage_once_and_disconnect_retains_estimate(monkeypatch, tmp_path):
    ledger = KeyUsage(tmp_path/'usage.db')
    monkeypatch.setattr(router, 'key_usage', ledger)
    monkeypatch.setattr(router, 'request_log', router.RequestRingBuffer(maxlen=10))
    usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
    def chunks():
        yield ('data: '+json.dumps({'choices': [{'delta': {'content': 'hello'}}]})+'\n\n').encode()
        yield ('data: '+json.dumps({'choices': [], 'usage': usage})+'\n\n').encode()
    monkeypatch.setattr(router, '_route_completion', lambda *_: ('stream', router._streaming_with_usage(chunks(), 'mock', 'gpt-4o-mini'), 'mock'))
    router._req_ctx.deadline = time.monotonic()+30
    response = router.app.test_client().post('/v1/chat/completions', headers=APP,
                 json={'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True, 'max_tokens': 20})
    assert response.status_code == 200
    assert b'hello' in response.get_data()
    response.close(); response.close()
    assert ledger.snapshot('sk-test')['tokens_total'] == 5
    assert ledger.snapshot('sk-test')['pending_reservations'] == 0
    response = router.app.test_client().post('/v1/chat/completions', headers=APP,
                 json={'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True, 'max_tokens': 20}, buffered=False)
    response.close()
    assert ledger.snapshot('sk-test')['tokens_total'] > 5
    assert ledger.snapshot('sk-test')['pending_reservations'] == 0
