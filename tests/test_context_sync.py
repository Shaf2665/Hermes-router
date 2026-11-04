"""Context sync, request races, persistence and application/admin boundaries."""
import json
import subprocess
import sys

import pytest
import router
import context_policy

APP = {"Authorization": "Bearer sk-test"}
ADMIN = {"Authorization": "Bearer sk-test-admin"}


@pytest.fixture
def context_router(monkeypatch, tmp_path):
    providers = [
        {"name": "one", "model": "large", "models": ["large"], "keys": ["secret-provider-key"]},
        {"name": "two", "model": "small", "models": ["small"], "keys": ["secret-other-key"]},
    ]
    monkeypatch.setattr(router, "PROVIDERS", providers)
    monkeypatch.setattr(router, "_CONTEXT_CONFIG", {"context_window": 0, "model_limits": {}})
    monkeypatch.setattr(router, "_DISCOVERED_CONTEXT_LIMITS", {})
    monkeypatch.setattr(router, "_model_state", {
        ("one", "large"): {"context_window": 512000, "max_output_tokens": 32000, "supports_tools": True},
        ("two", "small"): {"context_window": 128000, "max_output_tokens": 4096, "supports_tools": True},
    })
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {})
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    monkeypatch.setenv("ROUTER_CONTEXT_CONFIG", "{}")
    return router.app.test_client()


def get_config(client):
    response = client.get("/v1/client-config", headers=APP)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    return response.get_json()


def test_increase_decrease_auto_and_output_reservation(context_router):
    client = context_router
    first = get_config(client)
    assert first["model"]["max_input_tokens"] == 128000 - 4096
    assert first["model"]["max_output_tokens"] == 4096
    assert client.post("/v1/config/context", headers=ADMIN, json={"context_window": 256000}).status_code == 200
    high = get_config(client)
    assert high["model"]["context_window"] == 256000
    assert high["model"]["max_input_tokens"] == 256000 - 8192
    assert high["revision"] != first["revision"]
    client.post("/v1/config/context", headers=ADMIN, json={"context_window": 16000})
    low = get_config(client)
    assert low["model"]["context_window"] <= 16000
    assert low["model"]["max_input_tokens"] + low["model"]["max_output_tokens"] <= 16000
    client.post("/v1/config/context", headers=ADMIN, json={"context_window": 0})
    assert get_config(client) == first


def test_scopes_unknown_models_and_secret_redaction(context_router, monkeypatch):
    client = context_router
    assert client.get("/v1/client-config").status_code == 401
    assert client.get("/v1/client-config", headers=ADMIN).status_code == 401
    assert client.post("/v1/config/context", headers=APP, json={"context_window": 256000}).status_code == 401
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, "sk-test", {"one"})
    config = get_config(client)
    assert config["model"]["context_window"] == 512000
    text = json.dumps(config)
    for secret in ("secret-provider-key", "secret-other-key", "one", "two", "large", "small", "sk-test"):
        assert secret not in text
    monkeypatch.setitem(router.KEY_MODEL_SCOPE, "sk-test", {"small"})
    assert get_config(client)["model"] is None
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    monkeypatch.setattr(router, "_model_state", {})
    assert get_config(client)["model"] is None


@pytest.mark.parametrize("value", [True, -1, 1.5, "256000", None, 16000001, [], {}])
def test_invalid_minimum_does_not_modify_policy(context_router, value):
    before = get_config(context_router)
    response = context_router.post("/v1/config/context", headers=ADMIN, json={"context_window": value})
    assert response.status_code == 400
    assert get_config(context_router) == before
    assert not router.ENV_FILE_PATH.exists()


@pytest.mark.parametrize("patch", [
    {"unknown::model": {"context_window": 10000, "max_output_tokens": 1000}},
    {"one::large": {"context_window": 1000, "max_output_tokens": 1000}},
    {"one::large": {"context_window": 256000, "max_output_tokens": True}},
    {"one::large": {"context_window": 256000, "max_output_tokens": 1000, "api_key": "x"}},
    [], "bad",
])
def test_invalid_model_overrides_rejected(context_router, patch):
    assert context_router.post("/v1/config/context", headers=ADMIN, json={"model_limits": patch}).status_code == 400
    assert router._CONTEXT_CONFIG["model_limits"] == {}


def test_override_persistence_and_reset(context_router, monkeypatch):
    patch = {"context_window": 256000, "model_limits": {
        "one::large": {"context_window": 1000000, "max_output_tokens": 2000}}}
    response = context_router.post("/v1/config/context", headers=ADMIN, json=patch)
    assert response.get_json()["restart_required"] is False
    config = get_config(context_router)
    assert config["model"]["max_output_tokens"] == 2000
    assert router._env_read_line("ROUTER_CONTEXT_CONFIG") == json.dumps(router._CONTEXT_CONFIG, separators=(",", ":"))
    # Re-import in a new process to check restart behavior, not just in-memory state.
    root = str(__import__("pathlib").Path(router.__file__).parent)
    restored = subprocess.check_output([sys.executable, "-c",
        "import sys,json;sys.path.insert(0,sys.argv[1]);import router;print(json.dumps(router._CONTEXT_CONFIG))", root], text=True)
    assert json.loads(restored) == router._CONTEXT_CONFIG
    context_router.post("/v1/config/context", headers=ADMIN, json={"model_limits": {"one::large": None}})
    assert get_config(context_router)["model"]["max_output_tokens"] == 8192


def test_write_failure_keeps_live_revision(context_router, monkeypatch):
    before = get_config(context_router)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(router, "_env_write_line", fail)
    assert context_router.post("/v1/config/context", headers=ADMIN, json={"context_window": 256000}).status_code == 500
    assert get_config(context_router) == before


def test_session_cannot_change_context_without_csrf(context_router):
    login = context_router.post("/v1/admin/session", json={"api_key": "sk-test-admin"})
    assert login.status_code == 200
    assert context_router.post("/v1/config/context", json={"context_window": 256000}).status_code == 403
    csrf = login.get_json()["csrf_token"]
    assert context_router.post("/v1/config/context", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
                               json={"context_window": 256000}).status_code == 403


def test_stale_and_oversized_requests_never_reach_provider(context_router, monkeypatch):
    calls = []
    monkeypatch.setattr(router, "_admit_request", lambda *_: calls.append("admitted"))
    previous = get_config(context_router)
    context_router.post("/v1/config/context", headers=ADMIN, json={"context_window": 16000})
    headers = {**APP, "X-Hermes-Config-Revision": previous["revision"]}
    payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 4096}
    assert context_router.post("/v1/chat/completions", headers=headers, json=payload).status_code == 409
    current = get_config(context_router)
    headers["X-Hermes-Config-Revision"] = current["revision"]
    payload["tools"] = [{"type": "function", "function": {"name": "tool", "description": "அ" * 10000}}]
    assert context_router.post("/v1/chat/completions", headers=headers, json=payload).status_code == 400
    payload.pop("tools")
    payload["max_tokens"] = 100000
    assert context_router.post("/v1/chat/completions", headers=headers, json=payload).status_code == 400
    assert calls == []


def test_routing_floor_applies_to_fallbacks(context_router, monkeypatch):
    context_router.post("/v1/config/context", headers=ADMIN, json={"context_window": 256000})
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: [
        {"provider": p, "model": p["model"]} for p in reversed(router.PROVIDERS)])
    monkeypatch.setattr(router.cache, "get", lambda *_: None)
    monkeypatch.setattr(router.cache, "set", lambda *_: None)
    monkeypatch.setattr(router, "pool", type("Pool", (), {"key_count": lambda *_: 1, "get_key": lambda *_: "secret"})())
    calls = []
    class Reply:
        status_code, headers = 200, {}
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}
        def close(self): pass
    def forward(provider, *args):
        calls.append(provider["name"])
        return Reply()
    monkeypatch.setattr(router, "forward", forward)
    with router.app.test_request_context(headers=APP):
        router._auth_check()
        result = router._route_completion({"messages": [], "max_tokens": 100}, False)
    assert result[0] == "json"
    assert calls == ["one"]


def test_discovery_validation_and_provider_ceiling(context_router, monkeypatch):
    assert context_policy.discovered_limits({"context_length": 200000, "top_provider": {"max_completion_tokens": 8000}}) == {
        "context_window": 200000, "max_output_tokens": 8000}
    assert context_policy.discovered_limits({"context_length": "1000000"}) is None
    router.PROVIDERS[0]["skip_if_tokens_over"] = 6000
    router.PROVIDERS[0]["max_output_tokens"] = 1000
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, "sk-test", {"one"})
    model = get_config(context_router)["model"]
    assert model["max_input_tokens"] == 6000
    assert model["max_output_tokens"] == 1000


def test_reasoning_reserve_does_not_expand_client_context_budget(context_router, monkeypatch):
    provider = router.PROVIDERS[0]
    provider['base_url'] = 'https://provider.invalid/v1'
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, 'sk-test', {'one'})
    router._model_state[('one', 'large')]['reasoning'] = True
    monkeypatch.setenv('REASONING_TOKEN_RESERVE', '4096')
    model = get_config(context_router)['model']
    sent = []
    monkeypatch.setattr(router._HTTP, 'post', lambda *args, **kwargs: sent.append(kwargs['json']))
    payload = {'messages': [], 'max_tokens': model['max_output_tokens']}
    revision = get_config(context_router)['revision']
    with router.app.test_request_context(headers={**APP, 'X-Hermes-Config-Revision': revision}):
        router._auth_check()
        assert router._check_client_context(payload) is None
        router.forward(provider, 'fake-key', payload, False, model='large')
    assert sent[0]['max_tokens'] == model['max_output_tokens']


def test_unbounded_codex_protocol_is_not_advertised(context_router, monkeypatch):
    router.PROVIDERS[0]['protocol'] = 'codex'
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, 'sk-test', {'one'})
    assert get_config(context_router)['model'] is None


@pytest.mark.parametrize("alternate", [4097, True, 0, -1, "4096", None])
def test_alternate_output_field_cannot_bypass_client_budget(context_router, alternate):
    config = get_config(context_router)
    with router.app.test_request_context(headers={**APP, 'X-Hermes-Config-Revision': config['revision']}):
        router._auth_check()
        error = router._check_client_context({'messages': [],
            'max_tokens': config['model']['max_output_tokens'],
            'max_completion_tokens': alternate})
        assert error is not None
        assert error[1] == 400


def test_legacy_reasoning_request_retains_its_reserve(context_router, monkeypatch):
    provider = router.PROVIDERS[0]
    provider['base_url'] = 'https://provider.invalid/v1'
    router._model_state[('one', 'large')]['reasoning'] = True
    monkeypatch.setenv('REASONING_TOKEN_RESERVE', '4096')
    sent = []
    monkeypatch.setattr(router._HTTP, 'post', lambda *args, **kwargs: sent.append(kwargs['json']))
    with router.app.test_request_context(headers=APP):
        router._auth_check()
        assert router._check_client_context({'messages': [], 'max_tokens': 8192}) is None
        router.forward(provider, 'fake-key', {'messages': [], 'max_tokens': 8192}, False, model='large')
    assert sent[0]['max_tokens'] == 12288


def test_valid_alternate_output_budget_is_accepted(context_router):
    config = get_config(context_router)
    with router.app.test_request_context(headers={**APP, 'X-Hermes-Config-Revision': config['revision']}):
        router._auth_check()
        assert router._check_client_context({'messages': [], 'max_tokens': 100,
                                            'max_completion_tokens': 200}) is None


def test_context_cache_is_separate_from_legacy_and_old_pool(context_router, monkeypatch):
    namespaces = []
    monkeypatch.setattr(router.cache, 'get', lambda payload, ns: namespaces.append(ns) or {'choices': []})
    payload = {'messages': [], 'max_tokens': 100}
    def route(revision=None):
        headers = {**APP, **({'X-Hermes-Config-Revision': revision} if revision else {})}
        with router.app.test_request_context(headers=headers):
            router._auth_check()
            assert router._check_client_context(payload) is None
            assert router._route_completion(payload, False)[0] == 'json'
    route()
    original = get_config(context_router)
    route(original['revision'])
    # Changing discovered capacity changes the client revision but not persisted settings.
    router._model_state[('two', 'small')]['context_window'] = 64000
    updated = get_config(context_router)
    assert original['revision'] != updated['revision']
    route(updated['revision'])
    assert len(set(namespaces)) == 3
