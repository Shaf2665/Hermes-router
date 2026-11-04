import json
from collections import OrderedDict, defaultdict
from types import SimpleNamespace

import pytest
import model_discovery as discovery
import router

ADMIN = {"Authorization": "Bearer sk-test-admin"}


def row(model, **kw):
    return {"id": model, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, **kw}


def test_all_free_chat_models_no_eight_model_cap():
    items = [row(f"vendor/chat-{i}:free", pricing={"prompt": "0", "completion": "0"}) for i in range(40)]
    items += [row("vendor/paid", pricing={"prompt": "0.000001", "completion": "0"}), row("vendor/unknown"),
              row("whisper-large-v3:free"), row("vendor/content-safety:free"),
              row("voice:free", architecture={"input_modalities": ["text"], "output_modalities": ["audio"]}),
              row('<img src=x>:free')]
    assert len(discovery.free_chat_models("openrouter", items)) == 40


@pytest.mark.parametrize("price", [None, {}, {"prompt":"0"}, {"prompt":"NaN","completion":"0"},
    {"prompt":"-1","completion":"0"}, {"prompt":False,"completion":"0"},
    {"prompt":"0","completion":"0","request":"1"}, {"prompt":"Infinity","completion":"0"}])
def test_unknown_malformed_or_ancillary_price_is_not_free(price):
    assert not discovery.free_chat_models("openrouter", [row("qwen/test:free", pricing=price)])


def test_free_plan_requires_explicit_confirmation_and_documented_model():
    items = [row("openai/gpt-oss-120b"), row("qwen/qwen3.8-27b"), row("vendor/enterprise"), row("whisper-large-v3")]
    assert discovery.free_chat_models("groq", items) == {}
    assert set(discovery.free_chat_models("groq", items, True)) == {"openai/gpt-oss-120b", "qwen/qwen3.8-27b"}
    assert not discovery.free_chat_models("random_gateway", [row("vendor/foo/free"), row("vendor/chat:free")])


class Response:
    status_code = 200
    closed = False
    payload = {"data": [row("vendor/chat:free")]}
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def iter_content(self, size): yield json.dumps(self.payload).encode()


def test_catalog_security_redirect_limits_and_secret_safe_errors(monkeypatch):
    response = Response()
    calls = []
    session = SimpleNamespace(get=lambda *a, **k: calls.append((a,k)) or response)
    provider = {"name":"openrouter", "base_url":"https://openrouter.ai/api/v1"}
    assert len(discovery.fetch_catalog(session, provider, "secret")) == 1
    assert calls[0][1]["allow_redirects"] is False and response.closed
    response.status_code = 302
    with pytest.raises(discovery.CatalogError, match="could not list"):
        discovery.fetch_catalog(session, provider, "secret")
    response.status_code = 200
    response.payload = {"data": [], "has_more": True}
    with pytest.raises(discovery.CatalogError, match="paginated"):
        discovery.fetch_catalog(session, provider, "secret")
    monkeypatch.setattr(discovery, "MAX_CATALOG_BYTES", 5)
    with pytest.raises(discovery.CatalogError, match="safety limit"):
        discovery.fetch_catalog(session, provider, "secret")
    session.get = lambda *a, **k: (_ for _ in ()).throw(ValueError("secret"))
    with pytest.raises(discovery.CatalogError) as exc:
        discovery.fetch_catalog(session, provider, "secret")
    assert "secret" not in str(exc.value)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'AUTH_FILE', tmp_path/'auth.json')
    monkeypatch.setattr(router, 'ENV_FILE_PATH', tmp_path/'router.env')
    monkeypatch.setattr(router, '_AUTH_KEYS', {})
    monkeypatch.setattr(router, 'PROVIDERS', [])
    monkeypatch.setattr(router, '_DISCOVERY_QUEUE', OrderedDict())
    monkeypatch.setattr(router, '_DISCOVERY_GENERATIONS', defaultdict(int))
    monkeypatch.setattr(router, '_DISCOVERY_STATUS', {})
    monkeypatch.setattr(router, '_DISCOVERY_WORKER_RUNNING', True)  # manually run worker
    monkeypatch.setattr(router, '_INITIALIZATION_STATE', {"state":"ready"})
    monkeypatch.setattr(router, '_model_state', {})
    monkeypatch.setattr(router, '_provider_state', {})
    monkeypatch.setattr(router, '_DISCOVERED_CONTEXT_LIMITS', {})
    for name in ('OPENROUTER_AUTO_DISCOVER_MODELS','OPENROUTER_AUTO_DISCOVER_FREE_MODELS','OPENROUTER_MODEL'):
        monkeypatch.delenv(name, raising=False)
    yield router.app.test_client()


def test_add_key_activates_without_paid_default_or_restart(isolated, monkeypatch):
    monkeypatch.setenv('OPENROUTER_AUTO_DISCOVER_MODELS', '1')
    calls=[]
    selected={f"vendor/chat-{i}:free":row(f"vendor/chat-{i}:free", context_length=128000, supported_parameters=['tools']) for i in range(30)}
    monkeypatch.setattr(router, '_read_free_catalog', lambda *a: calls.append(a) or selected)
    assert isolated.post('/v1/config/keys/openrouter', json={"key":"secret", "discover_free":True}).status_code == 401
    r=isolated.post('/v1/config/keys/openrouter', headers=ADMIN, json={"key":"secret", "discover_free":True})
    assert r.status_code == 200 and r.json['restart_required'] is False
    assert not calls and router.PROVIDERS[0]['models'] == []
    disabled=frozenset({('openrouter','vendor/chat-0:free')})
    monkeypatch.setattr(router, '_DISABLED_MODELS', disabled)
    router._discovery_worker()
    assert len(router.PROVIDERS[0]['models']) == 30
    assert router._DISABLED_MODELS == disabled
    assert router._model_state[('openrouter','vendor/chat-0:free')]['tools_confirmed'] is True
    assert router._DISCOVERY_STATUS['openrouter']['state'] == 'complete'
    assert router._DISCOVERY_WORKER_RUNNING is False
    assert 'secret' not in isolated.get('/v1/config/providers',headers=ADMIN).get_data(as_text=True)


def test_stale_worker_cannot_overwrite_manual_changes(isolated, monkeypatch):
    p={'name':'openrouter', 'keys':['secret'], 'models':['manual'], 'model':'manual', '_manual_models':['manual']}
    router.PROVIDERS.append(p)
    monkeypatch.setenv('OPENROUTER_AUTO_DISCOVER_MODELS','1')
    def read(*args):
        r=isolated.post('/v1/config/model/openrouter',headers=ADMIN,json={'model':'new-manual'})
        assert r.status_code == 200
        return {'qwen/test:free':row('qwen/test:free')}
    monkeypatch.setattr(router,'_read_free_catalog',read)
    router._queue_free_discovery(p)
    router._discovery_worker()
    assert p['models'] == ['manual']
    assert router._DISCOVERY_STATUS['openrouter']['state'] == 'cancelled'


def test_catalog_failure_preserves_models_and_worker_continues(isolated, monkeypatch):
    p={'name':'openrouter','keys':['secret'],'models':['manual'], 'model':'manual'}
    router.PROVIDERS.append(p)
    monkeypatch.setenv('OPENROUTER_AUTO_DISCOVER_MODELS','1')
    monkeypatch.setattr(router,'_read_free_catalog',lambda *a: (_ for _ in ()).throw(ValueError('secret')))
    router._queue_free_discovery(p)
    router._discovery_worker()
    assert p['models'] == ['manual']
    assert router._DISCOVERY_STATUS['openrouter']['state'] == 'error'
    assert 'secret' not in json.dumps(router._DISCOVERY_STATUS)
    assert not router._DISCOVERY_WORKER_RUNNING


@pytest.mark.parametrize('body',[{'discover_free':'yes'}, {'discover_free':True,'free_tier_confirmed':True}, {'free_tier_confirmed':1}])
def test_invalid_discovery_options_never_save_key(isolated, body):
    assert isolated.post('/v1/config/keys/openrouter',headers=ADMIN,json={'key':'secret',**body}).status_code == 400
    assert not router.AUTH_FILE.exists()
