"""Tests for Hermes Router instance registry and Docker-safe controls."""
import router
import time
import pytest


@pytest.fixture(autouse=True)
def finish_background_probes(monkeypatch):
    yield
    deadline = time.monotonic() + 3
    while router._INSTANCE_PROBING and time.monotonic() < deadline:
        time.sleep(.005)
    assert not router._INSTANCE_PROBING


AUTH = {"Authorization": "Bearer sk-test-admin"}


@pytest.mark.parametrize("field", ["HR_ENV_FILE", "HR_USAGE_DB", "HERMES_INSTANCES_FILE"])
def test_instance_persistence_paths_are_launcher_managed(field):
    env, error = router._parse_instance_env({field: "/tmp/nonpersistent"})
    assert env == {}
    assert error == f"{field} is managed by the instance launcher"


def test_legacy_instance_path_overrides_do_not_block_metadata_updates():
    existing = {"id": "legacy", "name": "old", "mode": "docker", "host_port": 8321,
                "env": {"HR_ENV_FILE": "/old/.env", "HR_USAGE_DB": "/old/usage.db",
                        "HERMES_INSTANCES_FILE": "/old/instances.json", "OPENAI_API_KEYS": "test"}}
    updated, error = router._build_instance_from_body({"name": "renamed"}, existing)
    assert error is None
    assert updated["name"] == "renamed"
    assert updated["env"] == {"OPENAI_API_KEYS": "test"}
    assert existing["env"]["HR_USAGE_DB"] == "/old/usage.db"


@pytest.mark.parametrize("action", ["start", "restart"])
def test_recreated_instance_keeps_all_state_on_named_volume(monkeypatch, action):
    entry = {"id": "persistence-fixture", "mode": "docker", "container_name": "fixture",
             "host_port": 8321, "container_port": 8319, "api_key": "test-app",
             "image": "hermes-router:latest",
             "env": {"HR_USAGE_DB": "/old/override", "GEMINI_API_KEYS": "test-upstream"}}
    monkeypatch.setattr(router, "_docker_state", lambda _: {"available": True, "exists": False})
    calls = []
    def command(args, timeout):
        calls.append(args)
        return True, "fixture-container", 0
    monkeypatch.setattr(router, "_docker_cmd", command)
    assert router._docker_action(entry, action) == (True, "fixture-container")
    args, = calls
    assert args[0] == "run"
    assert args[args.index("-v") + 1] == "fixture-data:/data"
    env = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-e")
    assert {key: env[key] for key in (
        "HR_ENV_FILE", "HR_USAGE_DB", "HERMES_INSTANCES_FILE", "ROUTER_AUTH_FILE",
        "ROUTER_STATE_FILE", "CACHE_DB_PATH")} == {
            "HR_ENV_FILE": "/data/.env", "HR_USAGE_DB": "/data/usage.db",
            "HERMES_INSTANCES_FILE": "/data/instances.json", "ROUTER_AUTH_FILE": "/data/auth.json",
            "ROUTER_STATE_FILE": "/data/router_state.json", "CACHE_DB_PATH": "/data/cache.db"}
    assert env["PROXY_API_KEYS"] == "test-app"
    assert env["GEMINI_API_KEYS"] == "test-upstream"


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
    assert body["instance"]["freshness"]["state"] in {"checking", "fresh"}
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        current = client.get("/v1/instances", headers=AUTH).get_json()["instances"][0]
        if current["freshness"]["state"] == "fresh":
            break
        time.sleep(.005)
    assert current["live"]["status"] == "healthy"
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


def test_instance_snapshot_never_blocks_list_and_deduplicates(monkeypatch, tmp_path):
    import threading
    import time
    monkeypatch.setattr(router, 'INSTANCE_FILE', tmp_path / 'snapshots.json')
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocked(entry):
        calls.append(entry['id'])
        entered.set()
        assert release.wait(2)
        return {'status': 'healthy'}
    monkeypatch.setattr(router, '_probe_instance', blocked)
    entries = [{'id': str(i), 'mode': 'external', 'base_url': 'http://unused/v1', 'api_key': 'secret-api-key', 'env': {'TOKEN': 'secret-env'}} for i in range(10)]
    with router._INSTANCE_LOCK:
        router._write_instances_doc({'instances': entries})
    try:
        started = time.monotonic()
        for _ in range(3):
            response = router.app.test_client().get('/v1/instances', headers=AUTH)
            assert response.status_code == 200
            assert 'secret-api-key' not in response.text and 'secret-env' not in response.text
        assert time.monotonic() - started < .5
        assert entered.wait(.2)
        assert len(calls) <= 4
        assert len(calls) == len(set(calls))
        assert response.json['instances'][0]['freshness']['state'] == 'checking'
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while router._INSTANCE_PROBING and time.monotonic() < deadline:
            time.sleep(.005)



@pytest.mark.parametrize('replacement', ['changed', 'deleted', 'action', 'recreated'])
def test_pending_probe_is_invalidated_by_mutation(monkeypatch, tmp_path, replacement):
    import threading
    monkeypatch.setattr(router, 'INSTANCE_FILE', tmp_path / 'mutation.json')
    entered, release = threading.Event(), threading.Event()
    def blocked(entry):
        entered.set()
        assert release.wait(2)
        return {'status': 'old'}
    monkeypatch.setattr(router, '_probe_instance', blocked)
    entry = {'id': 'pending', 'mode': 'external', 'base_url': 'http://unused/v1'}
    with router._INSTANCE_LOCK:
        router._write_instances_doc({'instances': [entry]})
    identity = router._instance_identity(entry)
    router._instance_public(entry)
    assert entered.wait(.2)
    try:
        with router._INSTANCE_LOCK:
            if replacement == 'action':
                router._invalidate_instance_snapshot(entry)
            elif replacement == 'recreated':
                router._write_instances_doc({'instances': []})
                router._write_instances_doc({'instances': [entry]})
            else:
                entries = [] if replacement == 'deleted' else [{**entry, 'api_key': 'changed-key'}]
                router._write_instances_doc({'instances': entries})
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while identity in router._INSTANCE_PROBING and time.monotonic() < deadline:
        time.sleep(.005)
    assert identity not in router._INSTANCE_SNAPSHOTS


def test_snapshot_stale_state_and_public_without_live_do_not_leak_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'INSTANCE_FILE', tmp_path / 'stale-snapshot.json')
    entry = {'id': 'stale', 'api_key': 'secret-api-value', 'env': {'TOKEN': 'secret-env-value'}}
    identity = router._instance_identity(entry)
    with router._INSTANCE_SNAPSHOT_LOCK:
        router._INSTANCE_SNAPSHOTS[identity] = {'live': {'status': 'healthy'}, 'checked_ts': time.monotonic() - 60, 'checked_at': 'yesterday'}
    class Full:
        def acquire(self, *_): return False
    monkeypatch.setattr(router, '_INSTANCE_PROBE_SLOTS', Full())
    public = router._instance_public(entry)
    assert public['live']['status'] == 'healthy'
    assert public['freshness']['state'] == 'stale'
    assert public['freshness']['age_seconds'] >= 60
    assert 'secret-api-value' not in str(public) and 'secret-env-value' not in str(public)
    assert 'live' not in router._instance_public(entry, include_live=False)
