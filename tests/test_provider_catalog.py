import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import router
from provider_catalog import ADDITIONAL_DEFAULTS, PROVIDER_PRESETS

ADMIN = {"Authorization": "Bearer sk-test-admin"}
ROOT = Path(__file__).resolve().parents[1]


def test_all_presets_are_wired_into_runtime_and_configuration(monkeypatch):
    monkeypatch.setattr(router, "_keys_for", lambda name, env: ["test-key"])
    configured = {p["name"]: p for p in router._build_providers()}
    for name, preset in PROVIDER_PRESETS.items():
        assert name in router.KEY_SETTABLE_PROVIDERS
        assert urlparse(preset["key_url"]).scheme == "https"
        assert urlparse(preset["base_url"]).scheme == "https"
        assert preset["models"]
        assert configured[name]["base_url"] == preset["base_url"]
        if name in ADDITIONAL_DEFAULTS:
            assert configured[name]["model"] == ADDITIONAL_DEFAULTS[name]
            assert name in router._MODEL_DISCOVERY_SKIP
    names = subprocess.check_output([sys.executable, str(ROOT / "provider_catalog.py"), "names"], text=True)
    assert set(names.split()) == set(ADDITIONAL_DEFAULTS)
    assert subprocess.check_output([sys.executable, str(ROOT / "provider_catalog.py"), "canonical", "kilo-gateway"], text=True).strip() == "kilo_gateway"


def test_save_key_and_multiple_models_survives_reload(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    client = router.app.test_client()
    assert client.post('/v1/config/keys/kilo_gateway', headers=ADMIN, json={"key": "test-secret"}).status_code == 200
    model = "kilo-auto/free,nvidia/nemotron-3-super-120b-a12b:free"
    assert client.post('/v1/config/model/kilo_gateway', headers=ADMIN, json={"model": model}).status_code == 200
    assert json.loads((tmp_path / 'auth.json').read_text())["providers"]["kilo_gateway"] == ['test-secret']
    monkeypatch.setenv('KILO_GATEWAY_MODEL', router._env_read_line('KILO_GATEWAY_MODEL'))
    monkeypatch.setattr(router, '_keys_for', lambda name, env: ['test-secret'] if name == 'kilo_gateway' else [])
    provider = next(p for p in router._build_providers() if p['name'] == 'kilo_gateway')
    assert provider['models'] == model.split(',')
    result = client.get('/v1/config/providers', headers=ADMIN).get_json()
    assert result['configured_models']['kilo_gateway'] == model
    assert 'test-secret' not in json.dumps(result)


class Response:
    status_code = 200

    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def iter_content(self, size):
        yield json.dumps({'data': [{'id': 'models/gemini-test'}, {'id': '<script>'}, {'id': 'models/gemini-test'}, None]}).encode()


def test_catalog_lookup_is_admin_only_fixed_host_and_never_saves_key(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'AUTH_FILE', tmp_path / 'auth.json')
    calls = []
    response = Response()
    monkeypatch.setattr(router._HTTP, 'get', lambda url, **kw: calls.append((url, kw)) or response)
    client = router.app.test_client()
    path = '/v1/config/providers/gemini/models'
    assert client.post(path, json={'key': 'secret'}).status_code == 401
    result = client.post(path, headers=ADMIN, json={'key': 'secret', 'base_url': 'https://evil.example'})
    assert result.get_json()['models'] == ['gemini-test']
    assert calls[0][0] == PROVIDER_PRESETS['gemini']['base_url'] + '/models'
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['headers']['Authorization'] == 'Bearer secret'
    assert response.closed
    assert not (tmp_path / 'auth.json').exists()
    assert 'secret' not in result.get_data(as_text=True)


@pytest.mark.parametrize('body', [[], None, {'key': []}, {'key': 'bad\nkey'}, {'key': 'a' * 16385}])
def test_catalog_rejects_invalid_inputs(body):
    assert router.app.test_client().post('/v1/config/providers/groq/models', headers=ADMIN, json=body).status_code == 400


def test_catalog_refuses_redirect_and_oversized_response(monkeypatch):
    response = Response()
    monkeypatch.setattr(router._HTTP, 'get', lambda *a, **k: response)
    client = router.app.test_client()
    response.status_code = 302
    assert client.post('/v1/config/providers/groq/models', headers=ADMIN, json={'key': 'secret'}).status_code == 502
    response.status_code = 200
    response.iter_content = lambda size: iter([b'x' * 2_000_001])
    result = client.post('/v1/config/providers/groq/models', headers=ADMIN, json={'key': 'secret'})
    assert result.status_code == 502
    assert 'secret' not in result.get_data(as_text=True)


def test_new_provider_never_falls_back_to_an_unselected_paid_model(monkeypatch):
    response = Response()
    response.status_code = 404
    monkeypatch.setattr(router._HTTP, 'post', lambda *a, **k: response)
    monkeypatch.setattr(router, '_discover_best_model', lambda *a, **k: pytest.fail('Unselected model discovery'))
    provider = {'name': 'kilo_gateway', 'base_url': PROVIDER_PRESETS['kilo_gateway']['base_url'], 'model': 'retired/free'}
    assert router._probe_provider(provider, 'secret')[0] is False
