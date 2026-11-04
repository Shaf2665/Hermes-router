"""Regression tests for auto-discover refresh bounding."""
import router


class _FakeModelsResp:
    def __init__(self, catalog):
        self._catalog = catalog
        self.status_code = 200

    def json(self):
        return {"data": self._catalog}


class _FakePool:
    def ensure_model(self, name, model, keys):
        pass

    def rename_model(self, old, new, model):
        pass


def test_refresh_bounds_extras_when_kept_meets_limit(monkeypatch):
    """Configured models at/above the limit must not pull in the full catalog."""
    monkeypatch.setattr(router, "AUTO_DISCOVER_MODELS", True)
    monkeypatch.setattr(router, "AUTO_DISCOVER_FREE_MODELS", False)
    monkeypatch.setattr(router, "AUTO_DISCOVER_MODEL_LIMIT", 2)
    monkeypatch.setenv("GEMINI_AUTO_DISCOVER_MODELS", "1")

    catalog = [{"id": f"models/gemini-{i}"} for i in range(1, 12)]
    # Two configured IDs that match after models/ stripping.
    configured = ["gemini-1", "gemini-2"]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    provider = {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "headers": {},
        "model": configured[0],
        "models": list(configured),
        "keys": ["sk-test"],
    }
    router._refresh_discovered_models(provider, "sk-test", _FakePool())

    assert provider["models"] == configured
    assert len(provider["models"]) == 2


def test_refresh_keeps_configured_above_limit_and_caps_extras(monkeypatch):
    monkeypatch.setattr(router, "AUTO_DISCOVER_MODELS", True)
    monkeypatch.setattr(router, "AUTO_DISCOVER_FREE_MODELS", False)
    monkeypatch.setattr(router, "AUTO_DISCOVER_MODEL_LIMIT", 2)
    monkeypatch.setenv("GEMINI_AUTO_DISCOVER_MODELS", "1")

    catalog = [{"id": f"models/gemini-{i}"} for i in range(1, 8)]
    configured = ["gemini-1", "gemini-2", "gemini-3"]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    provider = {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "headers": {},
        "model": configured[0],
        "models": list(configured),
        "keys": ["sk-test"],
    }
    router._refresh_discovered_models(provider, "sk-test", _FakePool())

    assert provider["models"][:3] == configured
    assert len(provider["models"]) == 3
    assert "gemini-4" not in provider["models"]
