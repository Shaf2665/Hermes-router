"""Live model policy: failover, caches, persistence, auth and client metadata."""
import itertools
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import router

APP = {"Authorization": "Bearer sk-test"}
ADMIN = {"Authorization": "Bearer sk-test-admin"}
URL = "/v1/config/models/enabled"
PAYLOAD = {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]}


class Reply:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.close = Mock()

    def json(self):
        return {"model": self.model, "choices": [{"message": {
            "role": "assistant", "content": self.provider + "/" + self.model}, "finish_reason": "stop"}]}

    def iter_content(self, **_):
        yield ('data: ' + json.dumps({"choices": [{"delta": {"content": self.provider + "/" + self.model}}]}) + '\n\ndata: [DONE]\n\n').encode()


@pytest.fixture
def switches(monkeypatch, tmp_path):
    providers = [{"name": p, "model": "large", "models": ["large", "small"] if p == "alpha" else ["large"],
                  "keys": ["provider-secret"], "base_url": "http://mock", "protocol": ""} for p in ("alpha", "beta")]
    monkeypatch.setattr(router, "PROVIDERS", providers)
    monkeypatch.setattr(router, "MODEL_POLICY_FILE", tmp_path / "model-policy.json")
    monkeypatch.setattr(router, "_DISABLED_MODELS", frozenset())
    monkeypatch.setattr(router, "_CONTEXT_CONFIG", {"context_window": 0, "model_limits": {}})
    monkeypatch.setattr(router, "_model_state", {(p["name"], m): {
        "rating": 3, "supports_tools": True, "tools_confirmed": True,
        "context_window": 128000, "max_output_tokens": 8192} for p in providers for m in p["models"]})
    monkeypatch.setattr(router, "_provider_state", {p["name"]: {"available": True} for p in providers})
    monkeypatch.setattr(router, "_INITIALIZATION_STATE", {"state": "ready"})
    monkeypatch.setattr(router, "_rr_counter", itertools.repeat(0))
    monkeypatch.setattr(router, "pool", router.CredentialPool(providers))
    monkeypatch.setattr(router, "stats", router.ProviderStats())
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=300))
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(maxlen=10))
    monkeypatch.setattr(router, "KEY_LIMITS", {})
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {})
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router, "_trigger_restart", Mock(side_effect=AssertionError("must not restart")))
    return router.app.test_client()


def toggle(client, provider="alpha", model="large", enabled=False):
    return client.post(URL, headers=ADMIN, json={"provider": provider, "model": model, "enabled": enabled})


def test_switch_is_live_persistent_and_keeps_model_registered(switches):
    before = router.app.test_client().get("/health").json
    response = toggle(switches)
    assert response.status_code == 200 and response.json["restart_required"] is False
    assert router._load_disabled_models(router.MODEL_POLICY_FILE) == frozenset({("alpha", "large")})
    assert router._provider_models(router.PROVIDERS[0]) == ["large", "small"]
    assert router.PROVIDERS[0]["model"] == "large"
    assert router.pool.get_key("alpha", "large") == "provider-secret"
    rows = switches.get("/v1/status", headers=ADMIN).json["providers"]
    assert rows["alpha"]["model_states"][0]["enabled"] is False
    assert rows["beta"]["model_states"][0]["enabled"] is True
    assert before["ready"] and switches.get("/health").json["ready"]
    assert toggle(switches, enabled=True).status_code == 200
    assert router._load_disabled_models(router.MODEL_POLICY_FILE) == frozenset()
    router._trigger_restart.assert_not_called()


@pytest.mark.parametrize("streaming", [False, True])
def test_disabled_primary_is_skipped_without_restarting(switches, monkeypatch, streaming):
    attempts = []
    def forward(provider, key, payload, stream, model):
        attempts.append((provider["name"], model))
        return Reply(provider["name"], model)
    monkeypatch.setattr(router, "forward", forward)
    toggle(switches)
    toggle(switches, "beta")
    response = switches.post("/v1/chat/completions", headers=APP, json={**PAYLOAD, "stream": streaming})
    assert response.status_code == 200
    assert "alpha/small" in response.get_data(as_text=True)
    assert attempts == [("alpha", "small")]
    assert toggle(switches, enabled=True).status_code == 200
    response = switches.post("/v1/chat/completions", headers=APP, json={**PAYLOAD, "stream": streaming})
    assert "alpha/large" in response.get_data(as_text=True)


def test_cached_answer_cannot_bypass_disable(switches, monkeypatch):
    forward = Mock(side_effect=lambda p, k, b, s, m: Reply(p["name"], m))
    monkeypatch.setattr(router, "forward", forward)
    assert "alpha/large" in switches.post("/v1/chat/completions", headers=APP, json=PAYLOAD).get_data(as_text=True)
    switches.post("/v1/chat/completions", headers=APP, json=PAYLOAD)
    assert forward.call_count == 1
    toggle(switches)
    assert "beta/large" in switches.post("/v1/chat/completions", headers=APP, json=PAYLOAD).get_data(as_text=True)
    assert forward.call_count == 2


def test_disable_during_failed_request_skips_its_retry(switches, monkeypatch):
    monkeypatch.setattr(router.pool, "key_count", lambda *_: 2)
    monkeypatch.setattr(router.pool, "mark_key_down", lambda *args, **kwargs: None)
    attempts = []
    def forward(p, k, b, s, m):
        attempts.append((p["name"], m))
        reply = Reply(p["name"], m)
        if p["name"] == "alpha" and m == "large":
            assert toggle(switches).status_code == 200
            reply.status_code = 500
        return reply
    monkeypatch.setattr(router, "forward", forward)
    result = router._route_completion(PAYLOAD, False)
    assert result[0] == "json" and attempts == [("alpha", "large"), ("beta", "large")]


def test_disable_during_failover_skips_later_candidate(switches, monkeypatch):
    candidates = [{"provider": p, "model": "large"} for p in router.PROVIDERS]
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: candidates)
    attempts = []
    def forward(p, k, b, s, m):
        attempts.append(p["name"])
        assert toggle(switches, "beta").status_code == 200
        reply = Reply(p["name"], m); reply.status_code = 500
        return reply
    monkeypatch.setattr(router, "forward", forward)
    assert router._route_completion(PAYLOAD, False)[0] == "error"
    assert attempts == ["alpha"]


def test_all_disabled_fails_closed_and_reenable_restores_client_pool(switches, monkeypatch):
    initial = switches.get("/v1/client-config", headers=APP).json
    for p in router.PROVIDERS:
        for m in p["models"]:
            assert toggle(switches, p["name"], m).status_code == 200
    forward = Mock(side_effect=AssertionError("disabled upstream"))
    monkeypatch.setattr(router, "forward", forward)
    result = switches.post("/v1/chat/completions", headers=APP, json=PAYLOAD)
    assert result.status_code == 503 and result.json["error"]["code"] == "models_disabled"
    config = switches.get("/v1/client-config", headers=APP).json
    assert config["model"] is None and config["revision"] != initial["revision"]
    assert switches.get("/health").json["ready"] is False
    toggle(switches, enabled=True)
    assert switches.get("/v1/client-config", headers=APP).json["model"] is not None
    forward.assert_not_called()


def test_context_revision_changes_even_when_limits_stay_the_same(switches):
    before = switches.get("/v1/client-config", headers=APP).json
    toggle(switches)
    after = switches.get("/v1/client-config", headers=APP).json
    assert before["model"] == after["model"] and before["revision"] != after["revision"]
    response = switches.post("/v1/chat/completions", headers={**APP, "X-Hermes-Config-Revision": before["revision"]}, json={**PAYLOAD, "max_tokens": 1})
    assert response.status_code == 409


def test_cache_namespace_and_embedding_candidates_follow_switches(switches):
    router.PROVIDERS[0]["embed_model"] = "large"
    router.PROVIDERS[1]["embed_model"] = "large"
    with router.app.test_request_context(headers=APP):
        router._auth_check(); old = router._cache_ns()
        assert len(router._embed_ordered()) == 2
    toggle(switches)
    with router.app.test_request_context(headers=APP):
        router._auth_check()
        assert router._cache_ns() != old
        assert [p["name"] for p in router._embed_ordered()] == ["beta"]


def test_failed_save_does_not_change_live_routing(switches, monkeypatch):
    monkeypatch.setattr(router, "_atomic_write_text", Mock(side_effect=OSError("secret path")))
    response = toggle(switches)
    assert response.status_code == 503
    assert "secret path" not in response.get_data(as_text=True)
    assert router._model_enabled("alpha", "large")


def test_auth_and_csrf_are_required(switches):
    body = {"provider": "alpha", "model": "large", "enabled": False}
    assert switches.post(URL, json=body).status_code == 401
    assert switches.post(URL, headers=APP, json=body).status_code == 401
    session = switches.post("/v1/admin/session", json={"api_key": "sk-test-admin"}).json
    assert switches.post(URL, json=body).status_code == 403
    assert switches.post(URL, headers={"X-CSRF-Token": session["csrf_token"]}, json=body).status_code == 200


@pytest.mark.parametrize("patch", [{"enabled": "false"}, {"enabled": 0}, {"enabled": None},
    {"model": []}, {"provider": None}, {"extra": True}])
def test_invalid_requests_cannot_change_policy(switches, patch):
    response = switches.post(URL, headers=ADMIN, json={"provider": "alpha", "model": "large", "enabled": False, **patch})
    assert response.status_code == 400
    assert router._DISABLED_MODELS == frozenset()


def test_unknown_model_and_provider_cannot_change_policy(switches):
    assert toggle(switches, model="../../secret").status_code == 404
    assert toggle(switches, provider="unknown").status_code == 404
    assert not router.MODEL_POLICY_FILE.exists()


def test_parallel_switch_updates_are_not_lost(switches):
    def change(model):
        return toggle(router.app.test_client(), model=model).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(change, ["large", "small"])) == [200, 200]
    assert router._load_disabled_models(router.MODEL_POLICY_FILE) == frozenset({("alpha", "large"), ("alpha", "small")})


def test_new_process_loads_saved_switches(switches):
    toggle(switches)
    env = dict(os.environ, ROUTER_MODEL_POLICY_FILE=str(router.MODEL_POLICY_FILE))
    subprocess.run([sys.executable, "-c", "import router; assert not router._model_enabled('alpha','large'); assert router._model_enabled('beta','large')"],
                   cwd=os.path.dirname(router.__file__), env=env, check=True, capture_output=True)


@pytest.mark.parametrize("contents", ['{', '{}', '{"disabled_models":"all"}', '{"disabled_models":[["alpha"]]}'])
def test_corrupt_saved_policy_does_not_silently_enable_everything(tmp_path, contents):
    path = tmp_path / "policy.json"; path.write_text(contents)
    with pytest.raises(ValueError):
        router._load_disabled_models(path)


def test_disabled_model_is_not_probed(switches, monkeypatch):
    toggle(switches)
    post = Mock(side_effect=AssertionError("disabled model probe"))
    monkeypatch.setattr(router._HTTP, "post", post)
    assert router._probe_provider(router.PROVIDERS[0], "fake")[-1] == "disabled"
    assert router._probe_tools(router.PROVIDERS[0], "fake", "large") is None
    assert not router._probe_reasoning(router.PROVIDERS[0], "fake", "large")
    post.assert_not_called()
