"""Rotation mode changes apply to the running pool without a restart."""
import pytest
import router

ADMIN = {"Authorization": "Bearer sk-test-admin"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    monkeypatch.setattr(router, "ROTATION_MODE", "round-robin")
    monkeypatch.setenv("ROTATION_MODE", "round-robin")
    monkeypatch.setattr(router, "pool", router.CredentialPool([], mode="round-robin"))
    return router.app.test_client()


def test_rotation_applies_live_without_resetting_pool(client):
    provider = {"name": "test", "model": "chat", "keys": ["one", "two", "three"]}
    router.pool = router.CredentialPool([provider], mode="round-robin")
    original_pool = router.pool
    assert router.pool.get_key("test", "chat") == "one"
    router.pool.mark_rate_limited("test", "three", "chat", 60)
    bucket = router.pool.pools["test"]["chat"]
    cooling = next(entry["cool_until"] for entry in bucket if entry["key"] == "three")
    result = client.post("/v1/config/rotation", headers=ADMIN, json={"mode": "seq"})
    assert result.status_code == 200 and result.json["restart_required"] is False
    assert router.ROTATION_MODE == router.pool.mode == "sequential"
    assert router._env_read_line("ROTATION_MODE") == "sequential"
    assert client.get("/v1/status", headers=ADMIN).json["rotation"]["mode"] == "sequential"
    assert [router.pool.get_key("test", "chat") for _ in range(2)] == ["two", "two"]
    result = client.post("/v1/config/rotation", headers=ADMIN, json={"mode": "round-robin"})
    assert result.status_code == 200 and result.json["restart_required"] is False
    assert [router.pool.get_key("test", "chat") for _ in range(3)] == ["two", "one", "two"]
    # Same pool, same buckets: cooldowns and usage counters survive the switch.
    assert router.pool is original_pool and router.pool.pools["test"]["chat"] is bucket
    assert next(entry["cool_until"] for entry in bucket if entry["key"] == "three") == cooling
    assert router.pool.key_requests_for("test", "one") == 2
    assert router.pool.key_requests_for("test", "two") == 4


@pytest.mark.parametrize("body", [[], {"mode": []}, {"mode": 1}, {"mode": "unknown"}, {}])
def test_invalid_rotation_is_rejected(client, body):
    result = client.post("/v1/config/rotation", headers=ADMIN, json=body)
    assert result.status_code == 400
    assert router.ROTATION_MODE == router.pool.mode == "round-robin"
    assert not router.ENV_FILE_PATH.exists()


def test_rotation_failed_save_and_application_access_leave_runtime_unchanged(client, monkeypatch):
    body = {"mode": "sequential"}
    assert client.post("/v1/config/rotation", headers={"Authorization": "Bearer sk-test"}, json=body).status_code in (401, 403)
    def fail(*args):
        raise OSError("read-only")
    monkeypatch.setattr(router, "_env_write_line", fail)
    result = client.post("/v1/config/rotation", headers=ADMIN, json=body)
    assert result.status_code == 500
    assert router.ROTATION_MODE == router.pool.mode == "round-robin"
