"""Tests for Hermes Router instance registry and Docker-safe controls."""
import router


AUTH = {"Authorization": "Bearer sk-test"}


def test_instances_require_auth_and_register_external(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "INSTANCE_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(
        router,
        "_probe_instance",
        lambda entry: {
            "status": "healthy",
            "health_ok": True,
            "auth_ok": True,
            "latency_ms": 4.2,
            "message": "ok",
        },
    )
    monkeypatch.setattr(
        router,
        "_docker_state",
        lambda entry: {"available": True, "exists": False, "running": False, "status": "missing", "message": ""},
    )
    client = router.app.test_client()

    assert client.get("/v1/instances").status_code == 401

    resp = client.post(
        "/v1/instances",
        headers=AUTH,
        json={
            "name": "agent-a",
            "mode": "external",
            "base_url": "localhost:8320",
            "api_key": "sk-agent-a",
            "env": {"GEMINI_API_KEYS": "secret-gemini"},
        },
    )

    body = resp.get_json()
    assert resp.status_code == 201
    assert body["instance"]["name"] == "agent-a"
    assert body["instance"]["base_url"] == "http://localhost:8320/v1"
    assert body["instance"]["api_key"] == {"configured": True, "tail": "gent-a"}
    assert body["instance"]["env"]["keys"] == ["GEMINI_API_KEYS"]
    assert body["instance"]["live"]["status"] == "healthy"
    assert "secret-gemini" not in resp.get_data(as_text=True)

    listed = client.get("/v1/instances", headers=AUTH).get_json()["instances"]
    assert len(listed) == 1
    assert listed[0]["name"] == "agent-a"


def test_docker_instance_generates_key_and_invokes_start(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "INSTANCE_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(
        router,
        "_probe_instance",
        lambda entry: {"status": "unreachable", "health_ok": False, "auth_ok": None, "latency_ms": 1, "message": "down"},
    )
    monkeypatch.setattr(
        router,
        "_docker_state",
        lambda entry: {"available": True, "exists": False, "running": False, "status": "missing", "message": ""},
    )
    actions = []

    def fake_docker_action(entry, action):
        actions.append((entry["container_name"], action, entry["host_port"], entry["api_key"]))
        return True, "container-id"

    monkeypatch.setattr(router, "_docker_action", fake_docker_action)
    client = router.app.test_client()

    resp = client.post(
        "/v1/instances",
        headers=AUTH,
        json={"name": "agent docker", "mode": "docker", "host_port": 8321, "start": True},
    )

    body = resp.get_json()
    assert resp.status_code == 201
    assert body["instance"]["mode"] == "docker"
    assert body["instance"]["base_url"] == "http://localhost:8321/v1"
    assert body["generated_api_key"].startswith("sk-router-")
    assert actions == [(body["instance"]["container_name"], "start", 8321, body["generated_api_key"])]


def test_docker_instance_can_copy_existing_provider_keys(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"providers":{"gemini":["g-key-1","g-key-2"],"openai":["oa-key"]}}')
    monkeypatch.setattr(router, "AUTH_FILE", auth_file)
    monkeypatch.setattr(router, "INSTANCE_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(
        router,
        "_probe_instance",
        lambda entry: {"status": "unreachable", "health_ok": False, "auth_ok": None, "latency_ms": 1, "message": "down"},
    )
    monkeypatch.setattr(
        router,
        "_docker_state",
        lambda entry: {"available": True, "exists": False, "running": False, "status": "missing", "message": ""},
    )
    actions = []

    def fake_docker_action(entry, action):
        actions.append((action, entry["env"]))
        return True, "container-id"

    monkeypatch.setattr(router, "_docker_action", fake_docker_action)
    client = router.app.test_client()

    providers = client.get("/v1/config/providers", headers=AUTH).get_json()
    assert providers["copyable_provider_keys"]["gemini"] == 2
    assert providers["copyable_provider_keys"]["openai"] == 1

    resp = client.post(
        "/v1/instances",
        headers=AUTH,
        json={
            "name": "agent with copied keys",
            "mode": "docker",
            "host_port": 8324,
            "copy_provider_keys": ["gemini"],
            "start": True,
        },
    )

    body = resp.get_json()
    assert resp.status_code == 201
    assert body["instance"]["copy_provider_keys"] == ["gemini"]
    assert body["instance"]["env"]["keys"] == ["GEMINI_API_KEYS"]
    assert actions == [("start", {"GEMINI_API_KEYS": "g-key-1,g-key-2"})]
    assert "g-key-1" not in resp.get_data(as_text=True)
    assert "g-key-2" not in resp.get_data(as_text=True)


def test_instance_validation_and_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "INSTANCE_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(
        router,
        "_probe_instance",
        lambda entry: {"status": "healthy", "health_ok": True, "auth_ok": None, "latency_ms": 1, "message": "ok"},
    )
    client = router.app.test_client()

    bad = client.post(
        "/v1/instances",
        headers=AUTH,
        json={"name": "bad", "mode": "docker", "base_url": "http://localhost:8322/v1"},
    )
    assert bad.status_code == 400
    assert "host_port is required" in bad.get_json()["error"]["message"]

    created = client.post(
        "/v1/instances",
        headers=AUTH,
        json={"name": "temporary", "mode": "external", "base_url": "http://localhost:8323/v1"},
    ).get_json()["instance"]

    deleted = client.delete(f"/v1/instances/{created['id']}", headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted"] is True
    assert client.get("/v1/instances", headers=AUTH).get_json()["instances"] == []
