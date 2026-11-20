"""Context eligibility must hold before routing, failover, affinity and caching."""
from unittest.mock import Mock

import pytest
import router


@pytest.fixture
def routed(monkeypatch, tmp_path):
    provider = {"name": "context-test", "model": "small", "models": ["small", "large", "unknown"],
                "keys": ["test-key"], "context_windows": {"small": 32000, "large": 128000}}
    candidates = [{"provider": provider, "model": model} for model in provider["models"]]
    monkeypatch.setattr(router, "PROVIDERS", [provider])
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: candidates)
    monkeypatch.setattr(router, "_model_state", {})
    monkeypatch.setattr(router, "stats", router.ProviderStats())
    monkeypatch.setattr(router, "pool", router.CredentialPool([provider]))
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=0))
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer())
    monkeypatch.setattr(router, "key_usage", router.KeyUsage(str(tmp_path / "usage.db")))
    monkeypatch.setattr(router, "_session_affinity", router.OrderedDict())
    monkeypatch.setattr(router, "_ENCODER", None)
    monkeypatch.delenv("ROUTER_CONTEXT_WINDOW", raising=False)
    response = Mock(status_code=200, headers={}, text="")
    response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    response.iter_content.return_value = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n']
    forward = Mock(return_value=response)
    monkeypatch.setattr(router, "forward", forward)
    return provider, forward


def request(window=None, **payload):
    headers = {"Authorization": "Bearer sk-test"}
    if window is not None:
        headers["X-Hermes-Context-Window"] = str(window)
    return router.app.test_client().post("/v1/chat/completions", headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8192, **payload}, buffered=True)


@pytest.mark.parametrize("stream", [False, True])
def test_configured_window_filters_models_on_same_provider(routed, stream):
    _, forward = routed
    assert request(128000, stream=stream).status_code == 200
    assert [call.args[4] for call in forward.call_args_list] == ["large"]


def test_actual_history_and_tools_override_small_configured_window(routed):
    _, forward = routed
    tool = {"type": "function", "function": {"name": "lookup", "description": "x" * 100000}}
    assert request(16000, tools=[tool]).status_code == 200
    assert forward.call_args.args[4] == "large"
    forward.reset_mock()
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call", "type": "function", "function": {"name": "lookup", "arguments": "x" * 100000}}]}]
    assert request(16000, messages=messages).status_code == 200
    assert forward.call_args.args[4] == "large"


def test_output_reserve_and_boundaries(routed, monkeypatch):
    provider, _ = routed
    payload = {"max_tokens": 8192}
    assert router._context_candidate_allowed(provider, "small", payload, 23808, 0)
    assert not router._context_candidate_allowed(provider, "small", payload, 23809, 0)
    monkeypatch.setattr(router, "_model_state", {(provider["name"], "small"): {"reasoning": True}})
    assert not router._context_candidate_allowed(provider, "small", payload, 23808, 0)
    provider["max_output_tokens"] = 4096
    assert router._context_candidate_allowed(provider, "small", payload, 23808, 0)


def test_no_fitting_model_is_clear_error_without_forwarding(routed):
    _, forward = routed
    response = request(200000)
    assert response.status_code == 400
    assert response.json["error"]["code"] == "context_length_exceeded"
    forward.assert_not_called()


@pytest.mark.parametrize("value", ["-1", "NaN", "1.5", "true", "128K", "2147483648", ""])
def test_invalid_window_rejected(routed, value):
    _, forward = routed
    assert request(value).status_code == 400
    forward.assert_not_called()


def test_unknown_capacity_preserves_legacy_but_cannot_meet_explicit_window(routed):
    provider, forward = routed
    provider["context_windows"] = {}
    assert request().status_code == 200
    forward.reset_mock()
    assert request(32000).status_code == 400
    forward.assert_not_called()


def test_server_minimum_cannot_be_lowered_by_request(routed, monkeypatch):
    _, forward = routed
    monkeypatch.setenv("ROUTER_CONTEXT_WINDOW", "200000")
    assert request().status_code == 400
    assert request(128000).status_code == 400
    assert request(0).status_code == 400
    forward.assert_not_called()
    monkeypatch.setenv("ROUTER_CONTEXT_WINDOW", "16000")
    assert request(128000).status_code == 200
    assert forward.call_args.args[4] == "large"


def test_dashboard_setting_saves_applies_and_survives_reload(routed, monkeypatch, tmp_path):
    provider, forward = routed
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / ".env")
    monkeypatch.setenv("ROUTER_CONTEXT_WINDOW", "0")
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=60))
    provider["context_windows"]["small"] = 16000
    forward.return_value.json.side_effect = lambda: {"model": forward.call_args.args[4], "choices": [{"message": {"content": "ok"}}]}
    client = router.app.test_client()
    admin = {"Authorization": "Bearer sk-test-admin"}
    for window, expected in [(16000, "small"), (128000, "large"), (16000, "small")]:
        result = client.post("/v1/config/context", headers=admin, json={"context_window": window})
        assert result.status_code == 200
        assert result.json == {"context_window": window, "restart_required": False}
        assert router._env_read_line("ROUTER_CONTEXT_WINDOW") == str(window)
        assert client.get("/v1/status", headers=admin).json["context_window"] == window
        models = client.get("/v1/models", headers={"Authorization": "Bearer sk-test"}).json["data"]
        assert models[0]["context_window"] == window
        response = request(0)
        assert response.status_code == 200
        assert response.json["model"] == expected
    monkeypatch.delenv("ROUTER_CONTEXT_WINDOW")
    router._load_env(str(router.ENV_FILE_PATH))
    assert router._configured_context_window() == 16000


@pytest.mark.parametrize("body", [{}, [], {"context_window": True}, {"context_window": "128000"},
    {"context_window": 128000.5}, {"context_window": -1}, {"context_window": 2147483648}])
def test_dashboard_context_rejects_invalid_values(routed, monkeypatch, tmp_path, body):
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / ".env")
    response = router.app.test_client().post("/v1/config/context",
        headers={"Authorization": "Bearer sk-test-admin"}, json=body)
    assert response.status_code == 400
    assert not router.ENV_FILE_PATH.exists()


def test_dashboard_context_failed_save_and_access_do_not_change_setting(routed, monkeypatch):
    monkeypatch.setenv("ROUTER_CONTEXT_WINDOW", "16000")
    save = Mock(side_effect=OSError("read-only"))
    monkeypatch.setattr(router, "_env_write_line", save)
    client = router.app.test_client()
    for headers in ({}, {"Authorization": "Bearer sk-test"}):
        assert client.post("/v1/config/context", headers=headers, json={"context_window": 128000}).status_code == 401
    save.assert_not_called()
    assert client.post("/v1/config/context", headers={"Authorization": "Bearer sk-test-admin"},
        json={"context_window": 128000}).status_code == 500
    assert router._configured_context_window() == 16000


def test_failover_cannot_use_smaller_or_unknown_models(routed):
    _, forward = routed
    forward.return_value.status_code = 400
    assert request(128000).status_code == 503
    assert [call.args[4] for call in forward.call_args_list] == ["large"]


@pytest.mark.parametrize("capability", ["tools", "vision"])
def test_context_filter_cannot_relax_other_capability_requirements(routed, monkeypatch, capability):
    _, forward = routed
    if capability == "tools":
        monkeypatch.setattr(router, "_model_supports_tools", lambda name, model: model == "small")
        payload = {"tools": [{"type": "function", "function": {"name": "read"}}]}
    else:
        monkeypatch.setattr(router, "_model_supports_vision", lambda provider, model: model == "small")
        payload = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}]}]}
    assert request(128000, **payload).status_code == 503
    forward.assert_not_called()


def test_scopes_and_affinity_cannot_bypass_context(routed, monkeypatch):
    _, forward = routed
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {"sk-test": {"small"}})
    assert request(128000).status_code == 400
    forward.assert_not_called()
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    router._session_affinity_set("context-test-session", "context-test", "small")
    with router.app.test_request_context(headers={"X-Hermes-Context-Window": "128000",
                                                  "X-Hermes-Session-Affinity": "context-test-session"}):
        assert router._route_completion({"messages": [], "max_tokens": 8192}, False)[0] == "json"
    assert forward.call_args.args[4] == "large"


def test_cache_isolated_by_window_and_model_capacity(routed, monkeypatch):
    provider, forward = routed
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=60))
    assert request(32000).status_code == 200
    assert request(32000).status_code == 200
    assert forward.call_count == 1
    assert request(128000).status_code == 200
    assert forward.call_count == 2
    provider["context_windows"]["large"] = 64000
    assert request(128000).status_code == 400
    assert forward.call_count == 2


def test_anthropic_endpoint_uses_same_context_gate(routed):
    _, forward = routed
    response = router.app.test_client().post("/v1/messages",
        headers={"Authorization": "Bearer sk-test", "X-Hermes-Context-Window": "200000"},
        json={"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64})
    assert response.status_code == 400
    assert "context requirement" in response.json["error"]["message"]
    forward.assert_not_called()


def test_catalog_and_override_precedence(monkeypatch):
    provider = {"name": "gemini", "base_url": "https://example.test/v1"}
    response = Mock(status_code=200)
    response.json.return_value = {"data": [{"id": "models/model-a", "context_length": 128000},
        {"id": "models/model-b", "context_length": True}]}
    monkeypatch.setattr(router._HTTP, "get", Mock(return_value=response))
    router._discover_models(provider, "test-key")
    assert router._model_context_window(provider, "model-a") == 128000
    assert router._model_context_window(provider, "model-b") == 0
    monkeypatch.setenv("GEMINI_CONTEXT_WINDOW", "64000")
    monkeypatch.setenv("GEMINI_MODEL_A_CONTEXT_WINDOW", "256000")
    assert router._model_context_window(provider, "model-a") == 256000
    assert router._model_context_window(provider, "model-b") == 64000
    monkeypatch.setenv("GEMINI_MODEL_A_CONTEXT_WINDOW", "0")
    assert router._model_context_window(provider, "model-a") == 0


def test_native_defaults_do_not_apply_to_custom_or_local_servers():
    assert router._model_context_window({"name": "anthropic"}, "claude-sonnet-5-5") == 1000000
    assert router._model_context_window({"name": "openai"}, "gpt-4o") == 128000
    assert router._model_context_window({"name": "openai", "custom": True}, "gpt-4o") == 0
    assert router._model_context_window({"name": "local"}, "gpt-4o") == 0


def test_special_token_text_is_counted_as_ordinary_input(monkeypatch):
    import tiktoken
    monkeypatch.setattr(router, "_ENCODER", tiktoken.get_encoding("o200k_base"))
    assert router._estimated_request_tokens({"messages": [{"content": "<|endoftext|>"}]}) > 0
