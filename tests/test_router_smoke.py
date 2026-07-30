"""Smoke tests for the Flask router without external provider credentials."""
import router


AUTH = {"Authorization": "Bearer sk-test"}


def test_health_is_public_and_reports_ok():
    client = router.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "providers": []}


def test_models_requires_auth_and_returns_router_model():
    client = router.app.test_client()

    assert client.get("/v1/models").status_code == 401

    resp = client.get("/v1/models", headers=AUTH)
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "hermes-router"


def test_status_usage_and_logs_are_authenticated():
    client = router.app.test_client()

    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/usage").status_code == 401
    assert client.get("/v1/logs").status_code == 401

    status = client.get("/v1/status", headers=AUTH)
    usage = client.get("/v1/usage", headers=AUTH)
    logs = client.get("/v1/logs", headers=AUTH)

    assert status.status_code == 200
    assert status.get_json()["providers"] == {}
    assert status.get_json()["rotation"]["mode"] in {"round-robin", "sequential"}

    assert usage.status_code == 200
    assert usage.get_json()["providers"] == {}
    assert usage.get_json()["keys"][0]["key_tail"] == "k-test"

    assert logs.status_code == 200
    assert logs.get_json()["entries"] == []


def test_chat_without_providers_fails_cleanly_and_is_logged():
    client = router.app.test_client()
    payload = {
        "model": "hermes-router",
        "messages": [{"role": "user", "content": "Say hi"}],
    }

    resp = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"]["type"] == "router_error"
    assert "All providers exhausted" in body["error"]["message"]

    logs = client.get("/v1/logs?limit=1", headers=AUTH).get_json()["entries"]
    assert len(logs) == 1
    assert logs[0]["endpoint"] == "chat"
    assert logs[0]["status"] == "error"
    assert logs[0]["provider"] is None
