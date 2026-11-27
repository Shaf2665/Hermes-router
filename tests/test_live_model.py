"""Dashboard model edits apply to the running router without a restart."""
import itertools
import json
from unittest.mock import Mock

import pytest
import router

APP = {"Authorization": "Bearer sk-test"}
ADMIN = {"Authorization": "Bearer sk-test-admin"}
PAYLOAD = {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]}


class Reply:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, model):
        self.model = model
        self.close = Mock()

    def json(self):
        return {"model": self.model, "choices": [{"message": {
            "role": "assistant", "content": "via " + self.model}, "finish_reason": "stop"}]}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    monkeypatch.setattr(router, "_AUTH_KEYS", {})
    monkeypatch.setenv("GROQ_API_KEYS", "groq-secret")
    for name in ("GROQ_MODEL", "GROQ_EXCLUDE_MODELS", "MISTRAL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    providers = [p for p in router._build_providers() if p["name"] == "groq"]
    monkeypatch.setattr(router, "PROVIDERS", providers)
    monkeypatch.setattr(router, "pool", router.CredentialPool(providers))
    monkeypatch.setattr(router, "_provider_state", {"groq": {"available": True, "model": providers[0]["model"]}})
    monkeypatch.setattr(router, "_model_state", {})
    monkeypatch.setattr(router, "_DISABLED_MODELS", frozenset())
    monkeypatch.setattr(router, "_CONTEXT_CONFIG", {"context_window": 0, "model_limits": {}})
    monkeypatch.setattr(router, "_INITIALIZATION_STATE", {"state": "ready"})
    monkeypatch.setattr(router, "_rr_counter", itertools.repeat(0))
    monkeypatch.setattr(router, "stats", router.ProviderStats())
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=300))
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(maxlen=10))
    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router, "KEY_LIMITS", {})
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {})
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    monkeypatch.setattr(router, "_trigger_restart", Mock(side_effect=AssertionError("must not restart")))
    return router.app.test_client()


def groq():
    return next(p for p in router.PROVIDERS if p["name"] == "groq")


def test_model_change_applies_live_to_the_running_provider(client):
    live = groq()
    result = client.post("/v1/config/model/groq", headers=ADMIN, json={"model": "alpha, beta"})
    assert result.status_code == 200 and result.json["restart_required"] is False
    assert groq() is live and live["keys"] == ["groq-secret"]
    assert live["model"] == "alpha" and live["models"] == ["alpha", "beta"]
    assert router.pool.get_key("groq", "beta") == "groq-secret"
    assert router._provider_state["groq"]["model"] == "alpha"
    assert router._env_read_line("GROQ_MODEL") == "alpha, beta"


def test_reset_restores_the_built_in_default_live(client):
    default = groq()["model"]
    client.post("/v1/config/model/groq", headers=ADMIN, json={"model": "alpha"})
    result = client.delete("/v1/config/model/groq", headers=ADMIN)
    assert result.status_code == 200 and result.json["restart_required"] is False
    assert groq()["model"] == default and groq()["models"] == [default]
    assert router._env_read_line("GROQ_MODEL") is None
    assert "GROQ_MODEL" not in router.os.environ


def test_exclusions_still_apply_to_live_edits(client, monkeypatch):
    monkeypatch.setenv("GROQ_EXCLUDE_MODELS", "beta")
    client.post("/v1/config/model/groq", headers=ADMIN, json={"model": "alpha,beta"})
    assert groq()["models"] == ["alpha"]


def test_provider_without_keys_only_saves(client):
    before = list(router.PROVIDERS)
    result = client.post("/v1/config/model/mistral", headers=ADMIN, json={"model": "mistral-small"})
    assert result.status_code == 200 and result.json["restart_required"] is False
    assert router.PROVIDERS == before
    assert router._env_read_line("MISTRAL_MODEL") == "mistral-small"


def test_failed_save_leaves_routing_unchanged(client, monkeypatch):
    before = dict(groq())
    def fail(*args):
        raise OSError("read-only")
    monkeypatch.setattr(router, "_env_write_line", fail)
    result = client.post("/v1/config/model/groq", headers=ADMIN, json={"model": "alpha"})
    assert result.status_code == 500
    assert groq()["model"] == before["model"] and groq()["models"] == before["models"]
    assert "GROQ_MODEL" not in router.os.environ


def test_application_keys_cannot_change_models(client):
    assert client.post("/v1/config/model/groq", headers=APP, json={"model": "alpha"}).status_code in (401, 403)
    assert groq()["model"] != "alpha"


def test_cached_answer_from_the_old_model_is_not_served(client, monkeypatch):
    forward = Mock(side_effect=lambda p, k, b, s, m: Reply(m))
    monkeypatch.setattr(router, "forward", forward)
    first = groq()["model"]
    assert ("via " + first) in client.post("/v1/chat/completions", headers=APP, json=PAYLOAD).get_data(as_text=True)
    client.post("/v1/chat/completions", headers=APP, json=PAYLOAD)
    assert forward.call_count == 1
    client.post("/v1/config/model/groq", headers=ADMIN, json={"model": "alpha"})
    assert "via alpha" in client.post("/v1/chat/completions", headers=APP, json=PAYLOAD).get_data(as_text=True)
    assert forward.call_count == 2


def test_free_discovery_is_requeued_with_the_new_manual_models(client, monkeypatch):
    queued = []
    monkeypatch.setattr(router, "_free_discovery_enabled", lambda p: True)
    monkeypatch.setattr(router, "_queue_free_discovery", lambda p: queued.append(list(p["_manual_models"])))
    client.post("/v1/config/model/groq", headers=ADMIN, json={"model": "alpha"})
    assert queued == [["alpha"]]
