import router


class FakePool:
    def key_count(self, _provider, _model):
        return 1

    def get_key(self, _provider, _model):
        return "provider-key"


class FakeResponse:
    status_code = 200
    headers = {}
    text = ""

    @staticmethod
    def json():
        return {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ]
        }


def test_agent_profile_bypasses_cache_and_records_affinity(monkeypatch):
    provider = {
        "name": "agent-test",
        "model": "tool-model",
        "models": ["tool-model"],
        "keys": ["provider-key"],
        "base_url": "https://invalid.example/v1",
    }
    candidate = {"provider": provider, "model": "tool-model", "list_index": 0}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [candidate])
    monkeypatch.setattr(router, "_model_supports_tools", lambda *_args: True)
    monkeypatch.setattr(router, "pool", FakePool())
    monkeypatch.setattr(router, "forward", lambda *_args: FakeResponse())
    monkeypatch.setattr(
        router.cache,
        "get",
        lambda *_args: (_ for _ in ()).throw(AssertionError("agent cache read")),
    )
    monkeypatch.setattr(
        router.cache,
        "set",
        lambda *_args: (_ for _ in ()).throw(AssertionError("agent cache write")),
    )
    router._agent_affinity.clear()
    payload = {
        "model": "hermes-router",
        "messages": [{"role": "user", "content": "task"}],
        "tools": [{"type": "function", "function": {"name": "project.read"}}],
    }

    with router.app.test_request_context(
        headers={"X-Hermes-Profile": "agent", "X-Hermes-Agent-Run": "run-1"}
    ):
        result = router._route_completion(payload, False, "test")

    assert result[0] == "json"
    assert router._agent_affinity_get("run-1") == ("agent-test", "tool-model")


def test_agent_profile_requires_tools_and_tool_capable_model(monkeypatch):
    provider = {"name": "plain", "model": "plain-model"}
    candidate = {"provider": provider, "model": "plain-model", "list_index": 0}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [candidate])
    monkeypatch.setattr(router, "_model_supports_tools", lambda *_args: False)

    with router.app.test_request_context(headers={"X-Hermes-Profile": "agent"}):
        no_tools = router._route_completion(
            {"model": "hermes-router", "messages": []}, False, "test"
        )
        no_capable_model = router._route_completion(
            {
                "model": "hermes-router",
                "messages": [],
                "tools": [{"type": "function", "function": {"name": "project.read"}}],
            },
            False,
            "test",
        )

    assert no_tools[2] == 400
    assert no_capable_model[2] == 503


def test_agent_affinity_prefers_previous_candidate_and_is_bounded():
    first = {"provider": {"name": "first"}, "model": "one"}
    second = {"provider": {"name": "second"}, "model": "two"}
    router._agent_affinity.clear()
    router._agent_affinity_set("run-affinity", "second", "two")

    ordered = router._agent_affinity_order([first, second], "run-affinity")

    assert ordered == [second, first]
