"""Custom and local OpenAI-compatible providers configured live from the dashboard."""
import itertools
import json
from unittest.mock import Mock

import pytest
import router

APP = {"Authorization": "Bearer sk-test"}
ADMIN = {"Authorization": "Bearer sk-test-admin"}
URL = "/v1/config/custom-providers/"
PAYLOAD = {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]}


class Reply:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, model="m1"):
        self.model = model
        self.close = Mock()

    def json(self):
        return {"model": self.model, "choices": [{"message": {
            "role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    monkeypatch.setattr(router, "_AUTH_KEYS", {})
    monkeypatch.setattr(router, "PROVIDERS", [])
    monkeypatch.setattr(router, "pool", router.CredentialPool([]))
    monkeypatch.setattr(router, "_provider_state", {})
    monkeypatch.setattr(router, "_model_state", {})
    monkeypatch.setattr(router, "_DISABLED_MODELS", frozenset())
    monkeypatch.setattr(router, "_CONTEXT_CONFIG", {"context_window": 0, "model_limits": {}})
    monkeypatch.setattr(router, "_INITIALIZATION_STATE", {"state": "ready"})
    monkeypatch.setattr(router, "_rr_counter", itertools.repeat(0))
    monkeypatch.setattr(router, "stats", router.ProviderStats())
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=0))
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(maxlen=10))
    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router, "KEY_LIMITS", {})
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {})
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    for name in ("LOCAL_BASE_URL", "LOCAL_MODEL", "LOCAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(router, "_trigger_restart", Mock(side_effect=AssertionError("must not restart")))
    return router.app.test_client()


def save(client, name="custom_lab", **fields):
    body = {"label": "Lab", "base_url": "https://lab.example/v1", "model": "m1, m2", "key": "sk-lab-secret", **fields}
    return client.post(URL + name, headers=ADMIN, json=body)


def live(name):
    return next((p for p in router.PROVIDERS if p["name"] == name), None)


def capture_upstream(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Reply(kwargs["json"]["model"])
    monkeypatch.setattr(router._HTTP, "post", post)
    return calls


def test_create_applies_live_persists_and_hides_the_secret(client):
    result = save(client, create=True)
    assert result.status_code == 200 and result.json["restart_required"] is False
    provider = live("custom_lab")
    assert provider["base_url"] == "https://lab.example/v1" and provider["custom"] is True
    assert provider["models"] == ["m1", "m2"] and provider["keys"] == ["sk-lab-secret"]
    assert json.loads(router.AUTH_FILE.read_text())["custom_providers"]["custom_lab"]["base_url"] == "https://lab.example/v1"
    # A restart builds the same provider from auth.json.
    assert any(p["name"] == "custom_lab" for p in router._build_providers())
    listing = client.get("/v1/config/providers", headers=ADMIN)
    assert "sk-lab-secret" not in listing.get_data(as_text=True)
    preset = listing.json["presets"]["custom_lab"]
    assert preset["editable"] is True and preset["has_key"] is True and preset["label"] == "Lab"
    assert "custom_lab" in listing.json["key_settable"] and "custom_lab" not in listing.json["model_settable"]
    assert save(client, create=True).status_code == 409


def test_inference_uses_the_saved_address_and_key_without_redirects(client, monkeypatch):
    save(client)
    calls = capture_upstream(monkeypatch)
    assert client.post("/v1/chat/completions", headers=APP, json=PAYLOAD).status_code == 200
    assert calls[0]["url"] == "https://lab.example/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-lab-secret"
    assert calls[0]["allow_redirects"] is False


def test_keyless_local_server_gets_no_authorization_header(client, monkeypatch):
    result = save(client, "local", label="Local", base_url="http://localhost:11434/v1", model="llama3.1",
                  key="", clear_key=True)
    assert result.status_code == 200 and result.json["total_keys"] == 0
    calls = capture_upstream(monkeypatch)
    assert client.post("/v1/chat/completions", headers=APP, json=PAYLOAD).status_code == 200
    assert calls[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert not calls[0]["headers"].get("Authorization")


def test_blank_key_is_kept_only_at_the_same_address(client):
    save(client)
    assert save(client, key="", model="m3").status_code == 200
    assert live("custom_lab")["keys"] == ["sk-lab-secret"] and live("custom_lab")["models"] == ["m3"]
    moved = save(client, key="", base_url="https://elsewhere.example/v1")
    assert moved.status_code == 400 and live("custom_lab")["base_url"] == "https://lab.example/v1"
    assert save(client, key="", base_url="https://elsewhere.example/v1", clear_key=True).status_code == 200
    assert live("custom_lab")["headers"] == {"Authorization": None}


def test_in_flight_snapshot_never_pairs_keys_with_another_address(client):
    save(client)
    old = live("custom_lab")
    save(client, key="sk-new-secret", base_url="https://new.example/v1")
    new = live("custom_lab")
    assert new is not old and old["base_url"] == "https://lab.example/v1"
    for _ in range(3):
        assert router.pool.get_key("custom_lab", "m1", old["keys"]) == "sk-lab-secret"
        assert router.pool.get_key("custom_lab", "m1", new["keys"]) == "sk-new-secret"
    # Retired keys may finish in-flight requests but are not listed as active.
    listed = client.get("/v1/status", headers=ADMIN).json["providers"]["custom_lab"]["keys"]
    assert [k["key_tail"] for k in listed] == ["sk-new-secret"[-6:]]
    assert router.pool.key_requests_for("custom_lab", "sk-new-secret") == 3


@pytest.mark.parametrize("name", ["lab", "custom_", "custom_UPPER", "custom_" + "x" * 60, "gemini"])
def test_invalid_names_are_rejected(client, name):
    assert save(client, name).status_code == 400
    assert router.PROVIDERS == []


@pytest.mark.parametrize("fields", [
    {"base_url": "ftp://lab.example/v1"}, {"base_url": "https://user:pw@lab.example/v1"},
    {"base_url": "https://lab.example/v1?x=1"}, {"base_url": "https://lab .example/v1"},
    {"base_url": "https://lab.example:99999/v1"}, {"base_url": 5}, {"model": ""}, {"model": "bad model"},
    {"key": "line\nbreak"}, {"label": ""}, {"clear_key": "yes"}])
def test_invalid_fields_are_rejected(client, fields):
    assert save(client, **fields).status_code == 400
    assert router.PROVIDERS == [] and not router.AUTH_FILE.exists()


def test_admin_only_and_failed_save_leaves_routing_unchanged(client, monkeypatch):
    assert client.post(URL + "custom_lab", headers=APP, json={}).status_code in (401, 403)
    def fail(*args):
        raise OSError("read-only")
    monkeypatch.setattr(router, "_atomic_write_text", fail)
    assert save(client).status_code == 500
    assert router.PROVIDERS == []


def test_custom_models_are_edited_in_the_setup_form_and_scopes_accept_the_name(client):
    save(client)
    assert client.post("/v1/config/model/custom_lab", headers=ADMIN, json={"model": "x"}).status_code == 400
    assert router._parse_allowed_providers({"allowed_providers": ["custom_lab"]}) == ({"allowed_providers": ["custom_lab"]}, None)


def test_catalog_lookup_reuses_the_saved_key_only_at_the_same_address(client, monkeypatch):
    save(client)
    requests_seen = []
    class Catalog:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, size): yield json.dumps({"data": [{"id": "m9"}]}).encode()
    def get(url, **kwargs):
        requests_seen.append((url, kwargs["headers"], kwargs["allow_redirects"]))
        return Catalog()
    monkeypatch.setattr(router._HTTP, "get", get)
    path = "/v1/config/providers/custom_lab/models"
    assert client.post(path, headers=ADMIN, json={"base_url": "https://lab.example/v1"}).json["models"] == ["m9"]
    assert requests_seen[-1] == ("https://lab.example/v1/models", {"Authorization": "Bearer sk-lab-secret"}, False)
    client.post(path, headers=ADMIN, json={"base_url": "https://other.example/v1"})
    assert requests_seen[-1] == ("https://other.example/v1/models", {}, False)
    assert client.post("/v1/config/providers/custom/models", headers=ADMIN,
                       json={"base_url": "ftp://x"}).status_code == 400


def test_custom_servers_never_auto_discover_or_repair_models(client, monkeypatch):
    save(client)
    provider = live("custom_lab")
    assert router._provider_model_discovery_enabled(provider) is False
    monkeypatch.setattr(router._HTTP, "post", lambda *a, **k: Mock(status_code=404))
    monkeypatch.setattr(router, "_discover_best_model", Mock(side_effect=AssertionError("must not repair")))
    ok, _latency, actual, status = router._probe_provider(provider, "sk-lab-secret")
    assert (ok, actual, status) == (False, "m1", "http")
