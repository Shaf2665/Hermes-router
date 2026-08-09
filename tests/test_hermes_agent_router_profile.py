import json

import router

from hermes_agent.client import HermesInferenceClient
from hermes_agent.protocol import EventEmitter
from hermes_agent.runtime import AgentRuntime
from hermes_router_client import RouterConfig


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


class QueuedResponse:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, message):
        self.message = message

    def json(self):
        return {"choices": [{"message": self.message, "finish_reason": "stop"}]}


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
    monkeypatch.setattr(router, "_model_has_confirmed_tool_support", lambda *_args: True)
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
        "tools": [{"type": "function", "function": {"name": "project_read"}}],
    }

    with router.app.test_request_context(
        headers={"X-Hermes-Profile": "agent", "X-Hermes-Agent-Run": "run-1"}
    ):
        result = router._route_completion(payload, False, "test")

    assert result[0] == "json"
    assert router._agent_affinity_get("run-1") == ("agent-test", "tool-model")


def test_agent_profile_requires_tools_and_rejects_unknown_or_false_capability(monkeypatch):
    provider = {"name": "plain", "model": "plain-model"}
    candidate = {"provider": provider, "model": "plain-model", "list_index": 0}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [candidate])
    monkeypatch.setattr(router, "_model_state", {})

    with router.app.test_request_context(headers={"X-Hermes-Profile": "agent"}):
        no_tools = router._route_completion(
            {"model": "hermes-router", "messages": []}, False, "test"
        )
        unknown_capability = router._route_completion(
            {
                "model": "hermes-router",
                "messages": [],
                "tools": [{"type": "function", "function": {"name": "project_read"}}],
            },
            False,
            "test",
        )
        router._model_state[("plain", "plain-model")] = {
            "supports_tools": False,
            "tools_confirmed": False,
        }
        false_capability = router._route_completion(
            {
                "model": "hermes-router",
                "messages": [],
                "tools": [{"type": "function", "function": {"name": "project_read"}}],
            },
            False,
            "test",
        )

    assert no_tools[2] == 400
    assert unknown_capability[2] == 503
    assert false_capability[2] == 503


def test_normal_tool_routing_retains_optimistic_unknown_support(monkeypatch):
    monkeypatch.setattr(router, "_model_state", {})

    assert router._model_supports_tools("unknown", "unknown-model") is True
    assert router._model_has_confirmed_tool_support("unknown", "unknown-model") is False


def test_agent_runtime_routes_real_tool_call_through_router(monkeypatch, tmp_path):
    provider = {
        "name": "agent-test",
        "model": "tool-model",
        "models": ["tool-model"],
        "keys": ["provider-key"],
        "base_url": "https://invalid.example/v1",
    }
    candidate = {"provider": provider, "model": "tool-model", "list_index": 0}
    responses = [
        QueuedResponse(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "patch-1",
                        "type": "function",
                        "function": {
                            "name": "project_apply_patch",
                            "arguments": json.dumps(
                                {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}
                            ),
                        },
                    }
                ],
            }
        ),
        QueuedResponse({"role": "assistant", "content": "Updated app.py."}),
    ]
    forwarded = []

    def forward(_provider, _key, payload, *_args):
        forwarded.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: [candidate])
    monkeypatch.setattr(router, "_model_has_confirmed_tool_support", lambda *_args: True)
    monkeypatch.setattr(router, "pool", FakePool())
    monkeypatch.setattr(router, "forward", forward)
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer())
    router._agent_affinity.clear()
    client = router.app.test_client()

    def request(_config, method, path, payload=None, extra_headers=None):
        response = client.open(
            f"/v1{path}",
            method=method,
            headers={"Authorization": "Bearer sk-test", **(extra_headers or {})},
            json=payload,
        )
        assert response.status_code == 200
        return response.get_json()

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    events = []
    runtime = AgentRuntime(
        HermesInferenceClient(
            RouterConfig("http://127.0.0.1:8319/v1", "sk-test", "hermes-router", 10), request
        ),
        tmp_path,
        EventEmitter("router-loop", events.append),
    )

    assert runtime.run("Update app.py") == "completed"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert [tool["function"]["name"] for tool in forwarded[0]["tools"]] == [
        "project_read",
        "project_search",
        "project_apply_patch",
        "command_execute",
    ]
    assert forwarded[1]["messages"][-1]["role"] == "tool"
    assert [event["type"] for event in events][-2:] == ["message.delta", "run.completed"]


def test_agent_affinity_prefers_previous_candidate_and_is_bounded():
    first = {"provider": {"name": "first"}, "model": "one"}
    second = {"provider": {"name": "second"}, "model": "two"}
    router._agent_affinity.clear()
    router._agent_affinity_set("run-affinity", "second", "two")

    ordered = router._agent_affinity_order([first, second], "run-affinity")

    assert ordered == [second, first]
