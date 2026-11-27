"""Dashboard feature switches apply to the running router without a restart."""
import sqlite3
from unittest.mock import Mock

import pytest
import router

ADMIN = {"Authorization": "Bearer sk-test-admin"}
APP = {"Authorization": "Bearer sk-test"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    monkeypatch.setattr(router, "CACHE_DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setattr(router, "CACHE_TTL", 300)
    monkeypatch.setattr(router, "CACHE_PERSIST", False)
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=300, max_size=10))
    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router, "FAST_ROUTE_TOKENS", 0)
    monkeypatch.setattr(router, "COST_FX_RATE", 0.0)
    monkeypatch.setattr(router, "REQUEST_LOG_SIZE", 500)
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(500))
    monkeypatch.setattr(router, "_trigger_restart", Mock(side_effect=AssertionError("must not restart")))
    yield router.app.test_client()
    if router.cache._db is not None:
        router.cache._db.close()


def toggle(client, name, enabled):
    return client.post("/v1/config/features/" + name, headers=ADMIN, json={"enabled": enabled})


def addon(client, name):
    addons = client.get("/v1/status", headers=ADMIN).json["features"]["addons"]
    return next(a for a in addons if a["name"] == name)


@pytest.mark.parametrize("name, env, check_off, check_on", [
    ("response_cache", "CACHE_TTL_SECONDS", lambda: router.cache.ttl == 0, lambda: router.cache.ttl == 300),
    ("semantic_cache", "SEMANTIC_CACHE", lambda: router.SEMANTIC_CACHE is False, lambda: router.SEMANTIC_CACHE is True),
    ("fast_routing", "FAST_ROUTE_THRESHOLD", lambda: router.FAST_ROUTE_TOKENS == 0, lambda: router.FAST_ROUTE_TOKENS == 200),
    ("cost_currency", "COST_FX_RATE", lambda: router.COST_FX_RATE == 0, lambda: router.COST_FX_RATE == 83),
    ("request_log", "REQUEST_LOG_SIZE", lambda: not router.request_log.enabled, lambda: router.request_log.enabled),
])
def test_flags_apply_live_and_persist(client, name, env, check_off, check_on):
    for enabled, check in ((False, check_off), (True, check_on), (False, check_off)):
        result = toggle(client, name, enabled)
        assert result.status_code == 200 and result.json["restart_required"] is False
        assert check() and addon(client, name)["enabled"] is enabled
        expected = next(a for a in router._features_snapshot()["addons"] if a["name"] == name)
        assert router._env_read_line(env) == (expected["on"] if enabled else expected["off"])


def test_response_cache_switch_stops_and_resumes_serving(client):
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    router.cache.set(payload, {"answer": 1})
    toggle(client, "response_cache", False)
    assert router.cache.get(payload) is None
    toggle(client, "response_cache", True)
    router.cache.set(payload, {"answer": 2})
    assert router.cache.get(payload) == {"answer": 2}


def test_request_log_disable_clears_history_and_reenable_records(client):
    router.request_log.append({"provider": "x"})
    toggle(client, "request_log", False)
    assert router.request_log.snapshot() == [] and router.request_log.size == 0
    router.request_log.append({"provider": "ignored"})
    toggle(client, "request_log", True)
    router.request_log.append({"provider": "y"})
    assert [e["provider"] for e in router.request_log.snapshot()] == ["y"]


def test_persistent_cache_opens_mirrors_and_closes(client):
    payload = {"messages": [{"role": "user", "content": "kept"}]}
    router.cache.set(payload, {"answer": "memory"})
    result = toggle(client, "persistent_cache", True)
    assert result.status_code == 200 and result.json["restart_required"] is False
    assert router.cache.persistent and addon(client, "persistent_cache")["enabled"] is True
    rows = sqlite3.connect(router.CACHE_DB_PATH).execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    assert rows == 1   # entries cached before the switch are mirrored to disk
    assert toggle(client, "persistent_cache", False).status_code == 200
    assert not router.cache.persistent and router._env_read_line("CACHE_PERSIST") == "0"
    assert router.cache.get(payload) == {"answer": "memory"}   # memory cache keeps working


def test_persistent_cache_needs_the_response_cache_and_never_wipes_the_store(client):
    toggle(client, "response_cache", False)
    result = toggle(client, "persistent_cache", True)
    assert result.status_code == 400 and not router.cache.persistent
    assert router._env_read_line("CACHE_PERSIST") is None


def test_unwritable_cache_store_is_reported_and_not_saved(client, monkeypatch):
    monkeypatch.setattr(router, "CACHE_DB_PATH", str(router.ENV_FILE_PATH.parent / "missing" / "cache.db"))
    result = toggle(client, "persistent_cache", True)
    assert result.status_code == 500 and not router.cache.persistent
    assert router._env_read_line("CACHE_PERSIST") is None


def test_failed_save_leaves_runtime_unchanged(client, monkeypatch):
    def fail(*args):
        raise OSError("read-only")
    monkeypatch.setattr(router, "_env_write_line", fail)
    assert toggle(client, "semantic_cache", True).status_code == 500
    assert router.SEMANTIC_CACHE is False
    assert toggle(client, "persistent_cache", True).status_code == 500
    assert not router.cache.persistent


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, []])
def test_invalid_bodies_and_application_keys_are_rejected(client, body):
    assert client.post("/v1/config/features/semantic_cache", headers=ADMIN, json=body).status_code == 400
    assert client.post("/v1/config/features/semantic_cache", headers=APP, json={"enabled": True}).status_code in (401, 403)
    assert router.SEMANTIC_CACHE is False
