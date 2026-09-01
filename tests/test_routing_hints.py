"""Generic, inference-only routing hint coverage."""

import itertools

import router


class _Pool:
    def key_count(self, _provider, _model):
        return 1

    def get_key(self, _provider, _model):
        return "provider-key"

    def mark_key_down(self, *_args, **_kwargs):
        pass


class _Response:
    status_code = 200
    headers = {}
    text = ""

    @staticmethod
    def json():
        return {"choices": [{"message": {"role": "assistant", "content": "done", "tool_calls": []}}]}


def test_workload_hint_is_bounded_and_generic():
    assert router._workload_hint("coding") == "coding"
    assert router._workload_hint("  Debug ") == "debug"
    assert router._workload_hint("general") is None
    assert router._workload_hint("unknown") is None


def test_workload_hint_prefers_capability_without_filtering(monkeypatch):
    monkeypatch.setattr(router, "_rr_counter", itertools.repeat(0))
    monkeypatch.setattr(
        router,
        "_model_state",
        {
            ("plain", "plain-model"): {"rating": 3, "supports_tools": False, "reasoning": False},
            ("tools", "tools-model"): {"rating": 3, "supports_tools": True, "reasoning": False},
        },
    )
    providers = [
        {"name": "plain", "model": "plain-model", "models": ["plain-model"], "keys": ["k"]},
        {"name": "tools", "model": "tools-model", "models": ["tools-model"], "keys": ["k"]},
    ]
    ordered = router._get_smart_ordered(providers, complexity=3, workload_hint="coding")
    assert [candidate["provider"]["name"] for candidate in ordered] == ["tools", "plain"]


def test_tool_loop_bypasses_cache_and_records_session_affinity(monkeypatch):
    provider = {"name": "tool-test", "model": "tool-model", "models": ["tool-model"], "keys": ["key"]}
    candidate = {"provider": provider, "model": "tool-model", "list_index": 0}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [candidate])
    monkeypatch.setattr(router, "_model_has_confirmed_tool_support", lambda *_args: True)
    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(router, "forward", lambda *_args: _Response())
    monkeypatch.setattr(router.cache, "get", lambda *_args: (_ for _ in ()).throw(AssertionError("cache read")))
    monkeypatch.setattr(router.cache, "set", lambda *_args: (_ for _ in ()).throw(AssertionError("cache write")))
    router._session_affinity.clear()
    payload = {"model": "hermes-router", "messages": [{"role": "user", "content": "task"}], "tools": [{"type": "function", "function": {"name": "read"}}]}

    with router.app.test_request_context(headers={"X-Hermes-Tool-Loop": "true", "X-Hermes-Session-Affinity": "session-1"}):
        result = router._route_completion(payload, False, "test")

    assert result[0] == "json"
    assert router._session_affinity_get("session-1") == ("tool-test", "tool-model")


def test_non_streaming_request_records_session_affinity(monkeypatch):
    provider = {"name": "aff-test", "model": "aff-model", "models": ["aff-model"], "keys": ["key"]}
    candidate = {"provider": provider, "model": "aff-model", "list_index": 0}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [candidate])
    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(router, "forward", lambda *_args: _Response())
    monkeypatch.setattr(router.cache, "get", lambda *_args: None)
    monkeypatch.setattr(router.cache, "set", lambda *_args: None)
    router._session_affinity.clear()
    payload = {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]}

    with router.app.test_request_context(headers={"X-Hermes-Session-Affinity": "session-2"}):
        result = router._route_completion(payload, False, "test")

    assert result[0] == "json"
    assert router._session_affinity_get("session-2") == ("aff-test", "aff-model")


def test_tool_loop_requires_tool_definitions(monkeypatch):
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [])
    with router.app.test_request_context(headers={"X-Hermes-Tool-Loop": "true"}):
        result = router._route_completion({"model": "hermes-router", "messages": []}, False, "test")
    assert result[2] == 400


def test_normal_routing_fails_over_to_the_next_provider(monkeypatch):
    first = {"name": "first", "model": "first-model", "models": ["first-model"], "keys": ["key"]}
    second = {"name": "second", "model": "second-model", "models": ["second-model"], "keys": ["key"]}
    candidates = [
        {"provider": first, "model": "first-model", "list_index": 0},
        {"provider": second, "model": "second-model", "list_index": 1},
    ]
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: candidates)
    monkeypatch.setattr(router, "pool", _Pool())
    attempts = []
    failure = type("Failure", (), {"status_code": 500, "headers": {}, "text": "upstream unavailable"})()

    def forward(provider, *_args):
        attempts.append(provider["name"])
        return failure if provider["name"] == "first" else _Response()

    monkeypatch.setattr(router, "forward", forward)
    result = router._route_completion({"model": "hermes-router", "messages": []}, False, "test")

    assert result[0] == "json"
    assert attempts == ["first", "second"]


def test_vision_payload_skips_text_only_candidates(monkeypatch):
    text = {"name": "text", "model": "text-model", "models": ["text-model"], "keys": ["key"]}
    vision = {"name": "vision", "model": "vision-model", "models": ["vision-model"], "keys": ["key"]}
    candidates = [
        {"provider": text, "model": "text-model", "list_index": 0},
        {"provider": vision, "model": "vision-model", "list_index": 1},
    ]
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: candidates)
    monkeypatch.setattr(router, "_model_supports_vision", lambda _provider, model: model == "vision-model")
    monkeypatch.setattr(router, "pool", _Pool())
    attempts = []
    monkeypatch.setattr(router, "forward", lambda provider, *_args: (attempts.append(provider["name"]) or _Response()))
    payload = {"model": "hermes-router", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]}

    result = router._route_completion(payload, False, "test")

    assert result[0] == "json"
    assert attempts == ["vision"]
