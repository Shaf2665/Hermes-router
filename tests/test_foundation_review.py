"""Regressions reproduced during the independent foundation review."""
import time
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import router
from usage_store import KeyUsage

APP = {'Authorization': 'Bearer sk-test'}
ADMIN = {'Authorization': 'Bearer sk-test-admin'}


def test_embedding_request_replaces_previous_worker_deadline(monkeypatch, tmp_path):
    provider = {'name': 'openai', 'embed_model': 'text-embedding-3-small', 'base_url': 'http://mock'}
    monkeypatch.setattr(router, '_embed_ordered', lambda: [provider])
    monkeypatch.setattr(router, 'pool', Mock(get_key=Mock(return_value='fake'), key_count=Mock(return_value=1)))
    monkeypatch.setattr(router, 'stats', router.ProviderStats())
    monkeypatch.setattr(router, 'cache', router.ResponseCache(ttl=0))
    monkeypatch.setattr(router, 'key_usage', KeyUsage(tmp_path/'usage.db'))
    monkeypatch.setattr(router, 'request_log', router.RequestRingBuffer(maxlen=10))
    monkeypatch.setattr(router, 'KEY_LIMITS', {})
    response = Mock(status_code=200, headers={})
    response.json.return_value = {'data': [], 'usage': {'total_tokens': 1}}
    post = Mock(return_value=response)
    monkeypatch.setattr(router._HTTP, 'post', post)
    router._req_ctx.deadline = time.monotonic() - 1
    result = router.app.test_client().post('/v1/embeddings', headers=APP, json={'input': 'hello'})
    assert result.status_code == 200
    assert post.call_count == 1
    response.close.assert_called_once()
    assert router._req_ctx.deadline > time.monotonic()


def test_token_budget_rejects_unbounded_codex_and_maps_anthropic_limit(monkeypatch):
    monkeypatch.setattr(router, 'KEY_LIMITS', {'sk-test': {'tokens_per_day': 100000}})
    payload = {'messages': [{'role': 'user', 'content': 'hello'}], 'max_completion_tokens': 1}
    with router.app.test_request_context(headers=APP):
        assert router._auth_check() is None
        assert not router._budget_candidate_allowed({'name': 'codex'}, 'gpt-5', payload)
        assert router._budget_candidate_allowed({'name': 'anthropic'}, 'claude-sonnet-4', payload)
        assert router._to_anthropic_body(payload, 'claude-sonnet-4')['max_tokens'] == 1


def test_anthropic_wire_limit_matches_reserved_provider_cap(monkeypatch):
    provider = {'name': 'anthropic', 'protocol': 'anthropic', 'model': 'claude-sonnet-4', 'max_output_tokens': 1}
    payload = {'messages': [{'role': 'user', 'content': 'hello'}], 'max_completion_tokens': 1000}
    post = Mock(return_value=Mock())
    monkeypatch.setattr(router._HTTP, 'post', post)
    router._req_ctx.deadline = time.monotonic() + 30
    router.forward(provider, 'fake', payload, False)
    assert post.call_args.kwargs['json']['max_tokens'] == router._output_bound(payload, provider, provider['model']) == 1


def test_metrics_auth_cannot_be_disabled():
    client = router.app.test_client()
    assert client.get('/metrics').status_code == 401
    result = client.post('/v1/config/features/metrics_auth', headers=ADMIN, json={'enabled': False})
    assert result.status_code == 400


def test_features_cli_uses_admin_credential(tmp_path):
    from test_installation import bash_path
    scripts = tmp_path/'scripts'
    scripts.mkdir()
    script = scripts/'features.sh'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'scripts/features.sh', script)
    config = tmp_path/'router.env'
    config.write_text('ADMIN_API_KEY=test-owner\nPROXY_API_KEYS=test-app\n')
    env = dict(os.environ, HR_ENV_FILE=str(config), PYTHON=sys.executable)
    env.pop('ADMIN_API_KEY', None)
    command = """
curl() { [[ "$*" == *"Bearer test-owner"* ]] || return 22; printf '%s' '{"features":{"core":[],"addons":[]}}'; }
export -f curl
bash "$1" list
"""
    result = subprocess.run([bash_path(), '-c', command, 'bash', str(script)],
                            env=env, cwd=tmp_path, text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'Core features' in result.stdout


def test_browser_restart_requires_own_tracked_standalone_process(monkeypatch, tmp_path):
    (tmp_path/'scripts').mkdir()
    (tmp_path/'scripts/restart.sh').write_text('unused fixture')
    monkeypatch.setattr(router, '__file__', str(tmp_path/'router.py'))
    monkeypatch.setattr(router, 'os', SimpleNamespace(name='posix', environ={}, getpid=lambda: 1234))
    monkeypatch.setattr(router, 'Path', lambda p: tmp_path/'container-marker' if str(p) == '/.dockerenv' else Path(p))
    monkeypatch.setattr(router.shutil, 'which', lambda _: '/bin/bash')
    timer = Mock()
    monkeypatch.setattr(router.threading, 'Timer', timer)
    assert not router._trigger_restart()
    (tmp_path/'router.pid').write_text('9876')
    assert not router._trigger_restart()
    (tmp_path/'router.pid').write_text('1234')
    assert router._trigger_restart()
    timer.return_value.start.assert_called_once()
    router.os.environ['INVOCATION_ID'] = 'service-id'
    assert not router._trigger_restart()
