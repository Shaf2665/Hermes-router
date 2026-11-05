"""Protocol, policy and adversarial regressions for coding-client compatibility."""
import copy
import json
from unittest.mock import Mock

import pytest
import router
import responses_api as api

TOOL = {"type": "function", "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}
CUSTOM = {"type": "custom", "name": "apply_patch", "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"}}
HEADERS = {"Authorization": "Bearer sk-test"}


@pytest.fixture(autouse=True)
def isolated_log(monkeypatch):
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(50))


def chunk(delta=None, finish=None, **kwargs):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **kwargs}


def wire(items):
    return ("".join("data: " + json.dumps(item, ensure_ascii=False) + "\r\n\r\n" for item in items) + "data: [DONE]\r\n\r\n").encode()


def events(items, fragmented=False, tools=("read_file",), custom=()):
    raw = wire(items)
    source = iter(bytes([b]) for b in raw) if fragmented else iter([raw])
    return [json.loads(event.split(b"\ndata: ", 1)[1]) for event in api.stream(source, "hermes-router", custom, tools)]


def test_text_tools_and_full_history_round_trip_without_mutation():
    body = {"model": "hermes-router", "instructions": "Be concise", "store": False, "tools": [TOOL, CUSTOM], "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "Read it"}]},
        {"type": "reasoning", "encrypted_content": "private provider state"},
        {"type": "function_call", "call_id": "one", "name": "read_file", "arguments": '{"path":"a.py"}'},
        {"type": "custom_tool_call", "call_id": "two", "name": "apply_patch", "input": "*** Begin Patch\n*** End Patch"},
        {"type": "function_call_output", "call_id": "one", "output": "abc"},
        {"type": "custom_tool_call_output", "call_id": "two", "output": [{"type": "input_text", "text": "done"}]},
        {"role": "user", "content": "Continue"}]}
    original = copy.deepcopy(body)
    payload, custom = api.to_chat(body, "hermes-router")
    assert body == original and custom == {"apply_patch"}
    assert "private provider state" not in json.dumps(payload)
    assert payload["messages"][2]["tool_calls"][1]["function"]["arguments"].startswith('{"input":')
    assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert payload["tools"][0]["function"]["name"] == "read_file"


@pytest.mark.parametrize("patch", [
    {"input": []}, {"input": None}, {"input": [None]}, {"stream": "true"}, {"max_output_tokens": True},
    {"max_output_tokens": -5}, {"temperature": float("nan")}, {"tools": "secret"},
    {"tools": [None]}, {"tools": [{"type": "web_search"}]}, {"tools": [TOOL, TOOL]},
    {"tools": [{"type": "custom", "name": "bad", "format": []}]},
    {"tools": [{"type": []}]}, {"input": [{"role": []}]}, {"input": [{"type": {}}]},
    {"tool_choice": {"type": "function", "name": {}}},
    {"previous_response_id": "private-response"}, {"store": True}, {"background": True},
    {"conversation": "private-conversation"}, {"tool_choice": {"type": "function", "name": "unknown"}},
    {"input": [{"type": "item_reference", "id": "private-item"}]},
    {"input": [{"type": "function_call_output", "call_id": "unknown", "output": "secret"}]},
    {"input": [{"type": "function_call", "call_id": "one", "name": "read_file", "arguments": "{}"}]},
    {"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "secret"}]}]},
])
def test_unsupported_or_malformed_input_fails_before_routing(monkeypatch, patch):
    route = Mock()
    monkeypatch.setattr(router, "_route_completion", route)
    response = router.app.test_client().post("/v1/responses", json={"input": "hello", **patch}, headers=HEADERS)
    assert response.status_code == 400
    assert "secret" not in response.get_data(as_text=True)
    route.assert_not_called()


def test_streamed_text_unicode_and_usage_have_ordered_lifecycle():
    result = events([chunk({"content": "தமிழ்"}), chunk({"content": " hello"}, "stop"),
                     {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}], fragmented=True)
    assert [e["sequence_number"] for e in result] == list(range(len(result)))
    assert result[0]["type"] == "response.created"
    assert result[-1]["type"] == "response.completed"
    response = result[-1]["response"]
    assert response["output"][0]["content"][0]["text"] == "தமிழ் hello"
    assert response["usage"]["total_tokens"] == 12
    assert result[2]["item"]["content"] == []  # no mutable event aliasing


def test_fragmented_parallel_function_and_custom_calls_round_trip():
    result = events([
        chunk({"tool_calls": [{"index": 0, "id": "one", "function": {"name": "read_", "arguments": '{"path":'}},
                              {"index": 1, "id": "two", "function": {"name": "apply_patch", "arguments": '{"input":'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '"a.py"}'}},
                              {"index": 1, "function": {"arguments": '"patch"}'}}]}, "tool_calls")],
        custom={"apply_patch"}, tools={"read_file", "apply_patch"})
    output = result[-1]["response"]["output"]
    assert [item["type"] for item in output] == ["function_call", "custom_tool_call"]
    assert output[0]["arguments"] == '{"path":"a.py"}' and output[1]["input"] == "patch"
    next_input = [{"role": "user", "content": "work"}, *output,
                  {"type": "function_call_output", "call_id": "one", "output": "content"},
                  {"type": "custom_tool_call_output", "call_id": "two", "output": "applied"}]
    assert len(api.to_chat({"input": next_input, "tools": [TOOL, CUSTOM]}, "hermes-router")[0]["messages"]) == 4


@pytest.mark.parametrize("items", [[], [chunk({"content": "partial"})],
    [chunk({"content": "partial"}), {"error": {"message": "SECRET-UPSTREAM"}}],
    [chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read_file", "arguments": "{"}}]}, "tool_calls")],
    [chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "undeclared", "arguments": "{}"}}]}, "tool_calls")],
])
def test_failure_never_completes_or_emits_executable_tool(items):
    result = events(items)
    assert result[-1]["type"] == "response.failed"
    assert not any(e["type"] in {"response.completed", "response.output_item.done"} for e in result)
    assert "SECRET-UPSTREAM" not in json.dumps(result)


def test_length_is_incomplete_and_xml_stays_text():
    text = '<invoke name="delete_all" />'
    result = events([chunk({"content": text}, "length")])
    assert result[-1]["type"] == "response.incomplete"
    assert result[-1]["response"]["incomplete_details"]["reason"] == "max_output_tokens"
    assert result[-1]["response"]["output"][0]["type"] == "message"


def test_stream_closes_upstream_on_disconnect_and_exception():
    source = Mock()
    source.__iter__ = Mock(return_value=iter([wire([chunk({"content": "hi"}, "stop")])]))
    gen = api.stream(source, "hermes-router")
    next(gen)
    gen.close()
    source.close.assert_called_once()


def test_responses_auth_budget_admission_and_json_response(monkeypatch):
    route = Mock(return_value=("json", {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 2, "completion_tokens": 1}}))
    monkeypatch.setattr(router, "_route_completion", route)
    client = router.app.test_client()
    assert client.post("/v1/responses", json={"input": "hello"}).status_code == 401
    assert client.get("/v1/bridge-info").status_code == 401
    response = client.post("/v1/responses", json={"input": "hello", "max_output_tokens": 20}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json["output"][0]["content"][0]["text"] == "done"
    assert route.call_args.args[0]["max_tokens"] == 20
    assert client.get("/v1/bridge-info", headers=HEADERS).json["automatic_handoff"] is False


def test_tool_requests_fail_closed_without_confirmed_models(monkeypatch):
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: [])
    monkeypatch.setattr(router.cache, "get", Mock(side_effect=AssertionError("must bypass tool cache")))
    response = router.app.test_client().post("/v1/responses", json={"input": "do work", "tools": [TOOL]}, headers=HEADERS)
    assert response.status_code == 503


def test_claude_only_endpoint_cannot_route_to_non_claude(monkeypatch):
    from test_routing_hints import _Pool, _Response
    provider = {"name": "groq", "model": "qwen-test", "models": ["qwen-test"], "keys": ["k"]}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: [{"provider": provider, "model": "qwen-test"}])
    forward = Mock(return_value=_Response())
    monkeypatch.setattr(router, "forward", forward)
    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(router.cache, "get", lambda *_: None)
    response = router.app.test_client().post("/claude/v1/messages", headers=HEADERS,
        json={"model": "claude-test", "max_tokens": 10, "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503
    forward.assert_not_called()


def test_scoped_and_disabled_models_still_excluded(monkeypatch):
    from test_routing_hints import _Pool
    provider = {"name": "anthropic", "protocol": "anthropic", "model": "claude-test", "models": ["claude-test"], "keys": ["k"]}
    monkeypatch.setattr(router, "PROVIDERS", [provider])
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: [{"provider": provider, "model": "claude-test"}])
    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(router.cache, "get", lambda *_: None)
    forward = Mock()
    monkeypatch.setattr(router, "forward", forward)
    client = router.app.test_client()
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {"sk-test": ["groq"]})
    assert client.post("/v1/responses", json={"input": "hello"}, headers=HEADERS).status_code == 503
    monkeypatch.setattr(router, "KEY_PROVIDER_SCOPE", {})
    monkeypatch.setattr(router, "_model_enabled", lambda *_: False)
    assert client.post("/v1/responses", json={"input": "hello"}, headers=HEADERS).status_code == 503
    forward.assert_not_called()


def test_tool_namespaces_round_trip_without_colliding():
    tools = [TOOL, {"type": "namespace", "name": "files", "tools": [TOOL, CUSTOM]},
             {"type": "namespace", "name": "other", "tools": [TOOL]}]
    payload, registry = api.to_chat({"input": "work", "tools": tools,
        "tool_choice": {"type": "custom", "name": "apply_patch", "namespace": "files"}}, "hermes-router")
    names = [t["function"]["name"] for t in payload["tools"]]
    assert len(set(names)) == 4
    assert payload["tool_choice"]["function"]["name"] == names[2]
    response = api.from_chat({"choices": [{"message": {"tool_calls": [{"id": "a", "function": {
        "name": names[2], "arguments": '{"input":"patch"}'}}]}, "finish_reason": "tool_calls"}]},
        "hermes-router", registry, names)
    call = response["output"][0]
    assert call["name"] == "apply_patch" and call["namespace"] == "files" and call["input"] == "patch"
    next_payload, _ = api.to_chat({"input": [call, {"type": "custom_tool_call_output", "call_id": "a", "output": "done"}], "tools": tools}, "hermes-router")
    assert next_payload["messages"][0]["tool_calls"][0]["function"]["name"] == names[2]
