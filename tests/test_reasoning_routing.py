import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import router
import reasoning_policy as rp

APP = {"Authorization": "Bearer sk-test"}
ADMIN = {"Authorization": "Bearer sk-test-admin"}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    providers = [{"name": "groq", "model": "openai/gpt-oss-120b", "models": ["openai/gpt-oss-120b"],
                  "base_url": "https://api.groq.com/openai/v1", "keys": ["key"]},
                 {"name": "openai", "model": "o3", "models": ["o3"],
                  "base_url": "https://api.openai.com/v1", "keys": ["key"]}]
    monkeypatch.setattr(router, "PROVIDERS", providers)
    monkeypatch.setattr(router, "_REASONING_CONFIG", rp.validate_config({}))
    monkeypatch.setattr(router, "_CONTEXT_CONFIG", {"context_window": 0, "model_limits": {}})
    monkeypatch.setattr(router, "_model_state", {(p["name"], p["model"]): {
        "context_window": 128000, "max_output_tokens": 32768, "supports_tools": True} for p in providers})
    monkeypatch.setattr(router, "_DISABLED_MODELS", frozenset())
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {})
    monkeypatch.setattr(router, "KEY_MODEL_SCOPE", {})
    monkeypatch.setattr(router, "KEY_LIMITS", {})
    monkeypatch.setattr(router, "ENV_FILE_PATH", tmp_path / "router.env")
    monkeypatch.setenv("ROUTER_REASONING_CONFIG", "{}")
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer())
    monkeypatch.setattr(router, "_ordered_providers", lambda *_a: [
        {"provider": p, "model": p["model"], "list_index": i} for i, p in enumerate(providers)])
    monkeypatch.setattr(router, "pool", Mock(key_count=Mock(return_value=1), get_key=Mock(return_value="key")))
    monkeypatch.setattr(router, "stats", Mock(breaker_open=Mock(return_value=False)))
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=0))
    sent = []

    def upstream(url, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        response = Mock(status_code=200)
        response.json.return_value = {"model": kwargs["json"]["model"], "choices": [{"message": {
            "role": "assistant", "content": "answer", "reasoning_content": "PRIVATE"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 12}}}
        response.iter_content.return_value = iter([b'data: {"choices":[{"delta":{"content":"answer","reasoning_content":"PRIVATE"}}]}\n\n',
                                                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', b'data: [DONE]\n\n'])
        return response
    monkeypatch.setattr(router._HTTP, "post", upstream)
    return router.app.test_client(), sent, providers


def chat(client, headers=None, **fields):
    return client.post("/v1/chat/completions", headers={**APP, **(headers or {})}, json={
        "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4096, **fields})


def test_controls_apply_and_hide_raw_reasoning(setup):
    client, sent, _ = setup
    response = chat(client, {"X-Hermes-Reasoning": "high"})
    assert response.status_code == 200
    assert sent[0]["reasoning_effort"] == "high"
    assert sent[0]["max_tokens"] == 4096
    assert "PRIVATE" not in response.text
    assert response.headers["X-Hermes-Reasoning-Requested"] == "high"


def test_invalid_and_private_inputs_are_rejected_before_upstream(setup):
    client, sent, _ = setup
    assert chat(client, {"X-Hermes-Reasoning": "bogus"}).status_code == 400
    assert chat(client, {"X-Hermes-Reasoning": "high"}, reasoning_effort="low").status_code == 400
    response = chat(client, {"X-Hermes-Reasoning": "high"}, messages=[{"role": "assistant", "reasoning_content": "PRIVATE"}])
    assert response.status_code == 400
    assert response.json["error"]["code"] == "reasoning_continuity_unsupported"
    assert not sent


def test_persisted_defaults_and_explicit_auto(setup):
    client, sent, _ = setup
    response = client.post("/v1/config/reasoning", headers=ADMIN, json={"default": "low"})
    assert response.status_code == 200
    assert json.loads(router._env_read_line("ROUTER_REASONING_CONFIG"))["default"] == "low"
    assert chat(client).status_code == 200
    assert sent[-1]["reasoning_effort"] == "low"
    assert chat(client, {"X-Hermes-Reasoning": "auto"}).status_code == 200
    assert "reasoning_effort" not in sent[-1]


def test_settings_auth_and_failed_persistence(setup, monkeypatch):
    client, _, _ = setup
    assert client.post("/v1/config/reasoning", headers=APP, json={"default": "high"}).status_code == 401
    monkeypatch.setattr(router, "_env_write_line", Mock(side_effect=OSError("disk")))
    assert client.post("/v1/config/reasoning", headers=ADMIN, json={"default": "high"}).status_code == 500
    assert router._REASONING_CONFIG["default"] == "auto"


def test_discovery_scopes_and_revision(setup, monkeypatch):
    client, _, _ = setup
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, "sk-test", {"groq"})
    assert client.get("/v1/reasoning").status_code == 401
    response = client.get("/v1/reasoning", headers=APP)
    assert response.status_code == 200
    assert [m["provider"] for m in response.json["models"]] == ["groq"]
    first = client.get("/v1/client-config", headers=APP).json["revision"]
    client.post("/v1/config/reasoning", headers=ADMIN, json={"default": "low"})
    assert client.get("/v1/client-config", headers=APP).json["revision"] != first


def test_native_responses_effort_survives_translation(setup):
    client, sent, _ = setup
    response = client.post("/v1/responses", headers=APP, json={"input": "hi", "reasoning": {"effort": "low"}})
    assert response.status_code == 200
    assert sent[0]["reasoning_effort"] == "low"


def test_streaming_strips_private_state(setup):
    client, _, _ = setup
    response = chat(client, {"X-Hermes-Reasoning": "low"}, stream=True)
    assert response.status_code == 200
    assert "answer" in response.text and "PRIVATE" not in response.text


def test_failover_rechecks_policy_and_snapshot(setup, monkeypatch):
    client, sent, _ = setup
    upstream = router._HTTP.post
    def fail_first(url, **kwargs):
        response = upstream(url, **kwargs)
        if len(sent) == 1:
            router._REASONING_CONFIG = rp.validate_config({"default": "high"})
            response.status_code = 429
            response.headers = {}
        return response
    monkeypatch.setattr(router._HTTP, "post", fail_first)
    monkeypatch.setattr(router, "_REASONING_CONFIG", rp.validate_config({"default": "low"}))
    response = chat(client)
    assert response.status_code == 200
    assert [p["reasoning_effort"] for p in sent] == ["low", "low"]
    assert response.headers["X-Provider"] == "openai"


@pytest.mark.parametrize("wire", [
    b'data:{"choices":[{"delta":{"reasoning_content":"PRIVATE"}}]}',
    b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
    b'data: {"choices":[{"delta":{"thinking":"PRIVATE"}}]}\n\n',
])
def test_malformed_truncated_or_empty_stream_is_error(setup, monkeypatch, wire):
    client, _, _ = setup
    upstream = router._HTTP.post
    def changed(url, **kwargs):
        response = upstream(url, **kwargs)
        response.iter_content.return_value = iter([wire])
        return response
    monkeypatch.setattr(router._HTTP, "post", changed)
    response = chat(client, {"X-Hermes-Reasoning": "low"}, stream=True)
    assert "reasoning_incomplete" in response.text
    assert "PRIVATE" not in response.text


def test_truncated_nonstream_retains_usage_charge(setup, monkeypatch):
    client, _, _ = setup
    upstream = router._HTTP.post
    settle = Mock()
    monkeypatch.setattr(router, "_settle_request_usage", settle)
    def changed(url, **kwargs):
        response = upstream(url, **kwargs)
        response.json.return_value["choices"][0]["finish_reason"] = "length"
        return response
    monkeypatch.setattr(router._HTTP, "post", changed)
    response = chat(client, {"X-Hermes-Reasoning": "low"})
    assert response.status_code == 502
    assert settle.call_args.kwargs["failed"] is False
    assert settle.call_args.args[1]["completion_tokens"] == 20


def test_scope_and_disabled_model_cannot_be_bypassed(setup, monkeypatch):
    client, sent, _ = setup
    monkeypatch.setitem(router.KEY_PROVIDER_SCOPE, "sk-test", {"groq"})
    monkeypatch.setattr(router, "_model_enabled", lambda name, model: name != "groq")
    assert chat(client, {"X-Hermes-Reasoning": "low"}).status_code != 200
    assert not sent


def test_strict_policy_cannot_reuse_auto_cache(setup, monkeypatch):
    client, sent, _ = setup
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=60))
    assert chat(client).status_code == 200
    assert chat(client).status_code == 200
    assert len(sent) == 1
    assert chat(client, {"X-Hermes-Reasoning": "low"}).status_code == 200
    assert len(sent) == 2


def test_settings_browser_csrf_and_invalid_models(setup):
    client, _, _ = setup
    session = client.post("/v1/admin/session", json={"api_key": "sk-test-admin"})
    assert session.status_code == 200
    assert client.post("/v1/config/reasoning", json={"default": "high"}).status_code == 403
    assert client.post("/v1/config/reasoning", headers=ADMIN, json={"models": {"fake::o3": "low"}}).status_code == 400


def test_codex_does_not_promise_an_unenforceable_output_ceiling():
    provider = {"name": "codex", "protocol": "codex"}
    payload = {"reasoning_effort": "low", "max_tokens": 4096}
    with router.app.test_request_context():
        assert router._output_bound(payload, provider, "gpt-5") is None
        with pytest.raises(rp.PolicyError) as exc:
            router._reasoning_settings(payload, provider, "gpt-5")
        assert exc.value.code == "reasoning_budget_exceeded"
        assert router._reasoning_settings({"reasoning_effort": "low"}, provider, "gpt-5")["fields"] == {"reasoning": {"effort": "low"}}


def test_strict_stream_deadline_emits_error(setup, monkeypatch):
    now = [0]
    monkeypatch.setattr(router.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(router._req_ctx, "started", 0, raising=False)
    monkeypatch.setattr(router._req_ctx, "deadline", 1, raising=False)
    monkeypatch.setattr(router._req_ctx, "reasoning", {"strict": True}, raising=False)
    def source():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        now[0] = 2
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    wire = b"".join(router._streaming_with_usage(source(), "openai", "o3"))
    assert b'reasoning_incomplete' in wire


@pytest.mark.parametrize("terminal", [None, "response.incomplete", "response.failed"])
def test_codex_strict_requires_completion_event(terminal):
    events = [{"type": "response.output_text.delta", "delta": "partial"}]
    if terminal:
        events.append({"type": terminal, "response": {"usage": {"input_tokens": 10, "output_tokens": 20}}})
    data = router._from_codex_response(events, strict=True)
    assert router._strict_completion(data, {"strict": True})[1][2] == 502
    if terminal:
        assert data["usage"]["completion_tokens"] == 20
    else:
        assert data["usage"] == {}
    response = Mock(iter_lines=Mock(return_value=[("data: " + json.dumps(event)).encode() for event in events]))
    wire = "".join(router._codex_streaming_generator(response, strict=True))
    assert '"error"' in wire
    assert '[DONE]' not in wire


def test_codex_completed_usage_is_reported_without_inventing_reasoning_counts():
    usage = {"input_tokens": 10, "output_tokens": 20}
    normalized = router._codex_usage(usage, strict=True)
    assert normalized["total_tokens"] == 30
    assert "completion_tokens_details" not in normalized
    usage["output_tokens_details"] = {"reasoning_tokens": 12}
    data = router._from_codex_response([
        {"type": "response.output_text.delta", "delta": "answer"},
        {"type": "response.completed", "response": {"status": "completed", "usage": usage}},
    ], strict=True)
    assert router._strict_completion(data, {"strict": True})[1] is None
    assert data["usage"]["completion_tokens_details"]["reasoning_tokens"] == 12


def test_settings_reload_and_invalid_startup_fail_closed(setup, tmp_path):
    client, _, _ = setup
    assert client.post("/v1/config/reasoning", headers=ADMIN, json={"default": "low"}).status_code == 200
    env = os.environ.copy()
    env.pop("ROUTER_REASONING_CONFIG", None)
    env["HR_ENV_FILE"] = str(router.ENV_FILE_PATH)
    env["PYTHONPATH"] = str(Path(router.__file__).parent)
    command = [sys.executable, "-c", "import router; print(router._REASONING_CONFIG['default'])"]
    process = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "low"
    env["ROUTER_REASONING_CONFIG"] = '{"default":"unsupported"}'
    process = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert process.returncode != 0
    assert "Invalid reasoning default" in process.stderr
