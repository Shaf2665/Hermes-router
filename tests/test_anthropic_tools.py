"""Native Claude tool calls must survive both directions of the Copilot loop."""
import copy
import json
import time
from unittest.mock import Mock

import pytest
import requests
import router


TOOLS = [{"type": "function", "function": {"name": "read_file",
    "description": "Read a project file", "parameters": {"type": "object",
    "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def call(identifier="a", arguments='{"path":"README.md"}'):
    return {"id": identifier, "type": "function",
            "function": {"name": "read_file", "arguments": arguments}}


def test_request_and_parallel_tool_history_are_preserved_without_mutation():
    payload = {"tools": TOOLS, "parallel_tool_calls": False, "messages": [
        {"role": "system", "content": "Be brief."},
        {"role": "developer", "content": "Use project files."},
        {"role": "user", "content": "Explain the code"},
        {"role": "assistant", "content": "Reading.", "tool_calls": [call(), call("b", {})]},
        {"role": "tool", "tool_call_id": "a", "content": "A test runner"},
        {"role": "tool", "tool_call_id": "b", "content": [{"type": "text", "text": "Python"}]},
        {"role": "user", "content": "Now summarize."},
    ]}
    original = copy.deepcopy(payload)
    body = router._to_anthropic_body(payload, "claude-test")
    assert body["system"] == "Be brief.\nUse project files."
    assert body["tools"] == [{"name": "read_file", "description": "Read a project file",
                              "input_schema": TOOLS[0]["function"]["parameters"]}]
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "Reading."},
        {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "README.md"}},
        {"type": "tool_use", "id": "b", "name": "read_file", "input": {}},
    ]
    assert body["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "A test runner"},
        {"type": "tool_result", "tool_use_id": "b", "content": "Python"},
        {"type": "text", "text": "Now summarize."},
    ]}
    assert payload == original


@pytest.mark.parametrize("choice,expected", [
    ("auto", {"type": "auto"}), ("required", {"type": "any"}),
    ("none", {"type": "none"}),
    ({"type": "function", "function": {"name": "read_file"}}, {"type": "tool", "name": "read_file"}),
])
def test_tool_choice_is_not_silently_downgraded(choice, expected):
    body = router._to_anthropic_body({"tools": TOOLS, "tool_choice": choice}, "claude-test")
    assert body["tool_choice"] == expected


def test_empty_content_and_image_tool_results():
    body = router._to_anthropic_body({"messages": [
        {"role": "assistant", "content": None, "tool_calls": [call()]},
        {"role": "tool", "tool_call_id": "a", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    ]}, "claude-test")
    assert len(body["messages"][0]["content"]) == 1
    assert body["messages"][1]["content"][0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]


@pytest.mark.parametrize("args", ['{"secret":"do not echo"', '[]', 'null', 'true', 7, []])
def test_malformed_arguments_fail_without_echoing_payload(args):
    with pytest.raises(ValueError, match="Tool arguments must be a JSON object"):
        router._to_anthropic_body({"messages": [
            {"role": "assistant", "tool_calls": [call(arguments=args)]}]}, "claude-test")


@pytest.mark.parametrize("patch", [
    {"tools": "bad"}, {"tools": [None]}, {"tools": [{"type": "function", "function": "bad"}]},
    {"tools": [{"type": "custom", "function": {"name": "x"}}]},
    {"tools": [{"type": "function", "function": {"name": "x", "parameters": [1]}}]},
    {"tool_choice": {"type": "function", "function": "bad"}},
    {"messages": [{"role": "assistant", "tool_calls": [None]}]},
    {"messages": [{"role": "assistant", "tool_calls": "bad"}]},
    {"messages": [{"role": "tool", "content": "missing id"}]},
])
def test_invalid_tool_shapes_are_rejected(patch):
    with pytest.raises(ValueError):
        router._to_anthropic_body({"tools": TOOLS, **patch}, "claude-test")


def test_nonstream_tool_calls_round_trip_to_next_request():
    native = {"id": "msg_1", "model": "claude-test", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "private"},
        {"type": "text", "text": "Reading."},
        {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "README.md"}},
        {"type": "tool_use", "id": "b", "name": "read_file", "input": {}},
    ], "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5}}
    response = router._from_anthropic_response(native)
    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Reading."
    assert [c["id"] for c in choice["message"]["tool_calls"]] == ["a", "b"]
    assert response["usage"]["cache_read_input_tokens"] == 5
    assert "private" not in json.dumps(response)
    body = router._to_anthropic_body({"messages": [choice["message"]]}, "claude-test")
    assert body["messages"][0]["content"] == native["content"][1:]
    native["content"] = native["content"][2:]
    assert router._from_anthropic_response(native)["choices"][0]["message"]["content"] is None


def stream(events, fragmented=False):
    wire = "\n\n".join("event: " + e["type"] + "\ndata: " + json.dumps(e, ensure_ascii=False) for e in events).encode()
    response = requests.Response()
    # Exercise network chunks including split UTF-8 and no final LF.
    response.iter_content = lambda **kwargs: iter(bytes([b]) for b in wire) if fragmented else iter([wire])
    chunks = list(router._anthropic_streaming_generator(response))
    assert chunks[-1] == b"data: [DONE]\n\n"
    return [json.loads(c[5:]) for c in chunks[:-1]]


def test_streamed_tools_use_contiguous_indexes_and_preserve_fragmented_arguments():
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "model": "claude-test", "usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "Reading "}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "தமிழ்"}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "a", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"தமிழ்.md"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "b", "name": "read_file", "input": {}}},
        {"type": "content_block_stop", "index": 3},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]
    chunks = stream(events, fragmented=True)
    deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
    assert "".join(d.get("content", "") for d in deltas) == "Reading தமிழ்"
    calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
    assert [c["index"] for c in calls if "id" in c] == [0, 1]
    assert json.loads("".join(c["function"].get("arguments", "") for c in calls if c["index"] == 0)) == {"path": "தமிழ்.md"}
    assert json.loads("".join(c["function"].get("arguments", "") for c in calls if c["index"] == 1)) == {}
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["total_tokens"] == 30
    assert "private" not in json.dumps(chunks)


@pytest.mark.parametrize("reason,expected", [("end_turn", "stop"), ("stop_sequence", "stop"),
    ("max_tokens", "length"), ("tool_use", "tool_calls"), ("refusal", "content_filter")])
def test_stop_reasons_match_stream_and_nonstream(reason, expected):
    chunks = stream([{"type": "message_delta", "delta": {"stop_reason": reason}}, {"type": "message_stop"}])
    assert chunks[-1]["choices"][0]["finish_reason"] == expected
    assert router._from_anthropic_response({"stop_reason": reason})["choices"][0]["finish_reason"] == expected


def test_literal_xml_never_becomes_an_executable_tool_call():
    text = '<invoke name="delete"><parameter name="path">/</parameter></invoke>'
    output = router._from_anthropic_response({"content": [{"type": "text", "text": text}]})
    assert output["choices"][0]["message"] == {"role": "assistant", "content": text}
    chunks = stream([{"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}, {"type": "message_stop"}])
    assert "tool_calls" not in json.dumps(chunks)


@pytest.mark.parametrize("events", [[], [{"type": "error", "error": {"message": "private-upstream-detail"}}]])
def test_failed_streams_do_not_claim_success_or_leak_upstream_details(events):
    chunks = stream(events)
    assert chunks[-1]["error"]["type"] == "upstream_error"
    assert "private-upstream-detail" not in json.dumps(chunks)
    assert not any(c.get("choices") for c in chunks)


def test_forward_sends_native_tools_and_history(monkeypatch):
    provider = {"name": "anthropic", "protocol": "anthropic", "model": "claude-test"}
    post = Mock(return_value=Mock())
    monkeypatch.setattr(router._HTTP, "post", post)
    router._req_ctx.deadline = time.monotonic() + 30
    router.forward(provider, "fake", {"tools": TOOLS, "messages": [
        {"role": "assistant", "tool_calls": [call()]},
        {"role": "tool", "tool_call_id": "a", "content": "Project details"},
    ], "stream": True}, True)
    body = post.call_args.kwargs["json"]
    assert body["tools"][0]["name"] == "read_file"
    assert body["messages"][1]["content"][0]["type"] == "tool_result"
    assert post.call_args.kwargs["stream"] is True


def test_invalid_tool_history_returns_400_without_network_call(monkeypatch):
    provider = {"name": "anthropic", "protocol": "anthropic", "model": "claude-test"}
    monkeypatch.setattr(router, "_ordered_providers", lambda *_: [{"provider": provider, "model": "claude-test"}])
    monkeypatch.setattr(router, "_CONTEXT_CONFIG", {"context_window": 0, "model_limits": {}})
    monkeypatch.setattr(router, "_model_supports_tools", lambda *_: True)
    monkeypatch.setattr(router, "pool", Mock(key_count=Mock(return_value=1), get_key=Mock(return_value="fake")))
    monkeypatch.setattr(router, "stats", router.ProviderStats())
    monkeypatch.setattr(router, "cache", router.ResponseCache(ttl=0))
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(maxlen=10))
    post = Mock()
    monkeypatch.setattr(router._HTTP, "post", post)
    response = router.app.test_client().post("/v1/chat/completions", headers={"Authorization": "Bearer sk-test"}, json={
        "model": "hermes-router", "tools": TOOLS, "messages": [
            {"role": "assistant", "tool_calls": [call(arguments="secret-invalid-json")]}],
    })
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "invalid_request_error"
    assert "secret-invalid-json" not in response.get_data(as_text=True)
    post.assert_not_called()
