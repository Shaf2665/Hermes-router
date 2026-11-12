"""Reasoning-content echo: DeepSeek / Kimi thinking models require an earlier
turn's `reasoning_content` to be passed back in later tool-carrying requests
(DeepSeek answers 400 otherwise), so the thinking-field strip must preserve
it for those model families — in forwarded history, in non-streaming
responses, and in streaming deltas — while still stripping it everywhere
else (Groq / Cerebras / OpenRouter reject the foreign field).
"""
import copy
import json
from unittest.mock import Mock

import pytest

import router


TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get weather",
    "parameters": {"type": "object",
                   "properties": {"city": {"type": "string"}}}}}]

HISTORY = [
    {"role": "user", "content": "Weather in Paris?"},
    {"role": "assistant", "content": None, "reasoning_content": "thinking…",
     "reasoning": "openrouter-style field", "think": "loose field",
     "tool_calls": [{"id": "call_1", "type": "function", "function": {
         "name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "sunny, 24C"},
]


class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {"choices": [{"message": {
            "role": "assistant", "content": "done"}, "finish_reason": "stop"}]}

    def json(self):
        return self._data

    def close(self):
        pass


def _capture_forward(monkeypatch, provider, model):
    sent = {}

    def fake_post(url, headers=None, json=None, stream=False, timeout=None):
        sent["body"] = json
        return _Resp()

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    resp = router.forward(provider, "sk-test",
                          {"messages": [dict(m) for m in HISTORY], "tools": TOOLS},
                          streaming=False, model=model)
    assert resp is not None and resp.status_code == 200
    return sent["body"]


def test_requires_reasoning_echo_matches_echo_families_only():
    for model in ("deepseek-v4-flash", "deepseek-v4-pro",
                  "deepseek-ai/deepseek-v4-flash", "DeepSeek-V3.2",
                  "kimi-for-coding", "kimi-k2.7-code", "moonshotai/Kimi-K2.6"):
        assert router._requires_reasoning_echo(model), model
    for model in ("gpt-4o", "openai/gpt-oss-120b", "gemini-2.5-flash-lite",
                  "glm-4.7-flash", "claude-sonnet-5", "", None):
        assert not router._requires_reasoning_echo(model), model


def test_forward_preserves_reasoning_content_for_deepseek(monkeypatch):
    provider = {"name": "deepseek", "base_url": "http://mock.invalid/v1",
                "model": "deepseek-v4-flash"}
    body = _capture_forward(monkeypatch, provider, "deepseek-v4-flash")
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "thinking…"
    # The other thinking spellings are still stripped — only the documented
    # echo field is preserved.
    assert "reasoning" not in assistant
    assert "think" not in assistant


def test_forward_preserves_reasoning_content_for_kimi(monkeypatch):
    provider = {"name": "kimi", "base_url": "http://mock.invalid/v1",
                "model": "kimi-for-coding"}
    body = _capture_forward(monkeypatch, provider, "kimi-for-coding")
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "thinking…"


def test_forward_still_strips_reasoning_content_for_other_providers(monkeypatch):
    provider = {"name": "groq", "base_url": "http://mock.invalid/v1",
                "model": "openai/gpt-oss-120b"}
    body = _capture_forward(monkeypatch, provider, "openai/gpt-oss-120b")
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert "reasoning_content" not in assistant
    assert "reasoning" not in assistant
    assert "think" not in assistant


def test_strip_response_preserves_reasoning_for_echo_models():
    data = {"choices": [{"message": {
        "role": "assistant", "content": None, "reasoning_content": "thinking…",
        "reasoning": "x", "tool_calls": []}, "finish_reason": "tool_calls"}]}
    router._strip_response(
        data, keep_reasoning=router._requires_reasoning_echo("deepseek-v4-flash"))
    msg = data["choices"][0]["message"]
    assert msg["reasoning_content"] == "thinking…"
    assert "reasoning" not in msg


def test_strip_response_default_still_strips():
    data = {"choices": [{"message": {
        "role": "assistant", "content": "hi", "reasoning_content": "thinking…"},
        "finish_reason": "stop"}]}
    router._strip_response(data)
    assert "reasoning_content" not in data["choices"][0]["message"]


class _StreamResp:
    def __init__(self, payload_bytes):
        self._payload = payload_bytes

    def iter_content(self, chunk_size=None):
        yield self._payload


def _sse_event(delta):
    return ("data: " + json.dumps({"choices": [{"delta": delta}]}, ensure_ascii=False) + "\n").encode()


def test_streaming_generator_preserves_reasoning_deltas_when_asked():
    raw = (_sse_event({"reasoning_content": "think", "reasoning": "x",
                       "content": "a"})
           + _sse_event({"content": "b"}) + b"data: [DONE]\n")
    out = b"".join(router._streaming_generator(_StreamResp(raw),
                                               keep_reasoning=True))
    first = json.loads(out.decode().splitlines()[0][len("data: "):])
    delta = first["choices"][0]["delta"]
    assert delta["reasoning_content"] == "think"
    assert "reasoning" not in delta


def test_streaming_generator_default_strips_reasoning_deltas():
    raw = _sse_event({"reasoning_content": "think", "content": "a"})
    out = b"".join(router._streaming_generator(_StreamResp(raw)))
    first = json.loads(out.decode().splitlines()[0][len("data: "):])
    delta = first["choices"][0]["delta"]
    assert "reasoning_content" not in delta
    assert delta["content"] == "a"


@pytest.mark.parametrize("name,model", [
    ("groq", "moonshotai/kimi-k2-instruct"),
    ("cerebras", "deepseek-test"),
    ("openrouter", "deepseek/deepseek-r1"),
])
def test_gateway_contract_takes_precedence_over_model_family(monkeypatch, name, model):
    provider = {"name": name, "base_url": "http://mock.invalid/v1", "model": model}
    body = _capture_forward(monkeypatch, provider, model)
    assert "reasoning_content" not in body["messages"][1]


@pytest.mark.parametrize("model", ["not-deepseek", "custom/kimiature", "moonshotai/other-model"])
def test_unrelated_names_do_not_enable_echo(model):
    assert not router._requires_reasoning_echo(model)


def _route_fixture(monkeypatch, providers):
    candidates = [{"provider": p, "model": p["model"], "list_index": i}
                  for i, p in enumerate(providers)]
    monkeypatch.setattr(router, "_ordered_providers", lambda *_args: candidates)
    monkeypatch.setattr(router, "pool", Mock(key_count=Mock(return_value=1),
                                           get_key=Mock(return_value="test-key")))
    monkeypatch.setattr(router.cache, "get", lambda *_args: None)
    monkeypatch.setattr(router.cache, "set", lambda *_args: None)


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "kimi-for-coding"])
@pytest.mark.parametrize("streaming", [False, True])
def test_tool_conversation_round_trip_through_router(monkeypatch, model, streaming):
    provider = {"name": "echo-test", "model": model, "base_url": "http://mock.invalid/v1"}
    _route_fixture(monkeypatch, [provider])
    assistant = copy.deepcopy(HISTORY[1])
    assistant["reasoning_content"] = "Check weather தமிழ்"
    seen = []

    def upstream(_url, **kwargs):
        body = kwargs["json"]
        seen.append(copy.deepcopy(body))
        if len(seen) == 2:
            assert body["messages"][1]["reasoning_content"] == assistant["reasoning_content"]
            assert body["messages"][1]["tool_calls"] == assistant["tool_calls"]
            return _Resp(data={"choices": [{"message": {"role": "assistant", "content": "Sunny"}}]})
        resp = _Resp(data={"choices": [{"message": copy.deepcopy(assistant), "finish_reason": "tool_calls"}]})
        wire = (_sse_event(assistant) + b"\ndata: [DONE]\n\n")
        # Split even inside UTF-8 characters, as a real connection can do.
        resp.iter_content = lambda **_kwargs: iter(bytes([b]) for b in wire)
        return resp

    monkeypatch.setattr(router._HTTP, "post", upstream)
    payload = {"model": "hermes-router", "messages": [copy.deepcopy(HISTORY[0])], "tools": TOOLS}
    first = router._route_completion(payload, streaming)
    if streaming:
        wire = b"".join(first[1]).decode()
        events = [json.loads(line[6:]) for line in wire.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        message = events[0]["choices"][0]["delta"]
    else:
        message = first[1]["choices"][0]["message"]
    payload["messages"].extend([message, copy.deepcopy(HISTORY[2])])
    original = copy.deepcopy(payload)
    second = router._route_completion(payload, False)
    assert second[0] == "json"
    assert second[1]["choices"][0]["message"]["content"] == "Sunny"
    assert payload == original


@pytest.mark.parametrize("streaming", [False, True])
def test_failover_to_gateway_rechecks_request_and_response_policy(monkeypatch, streaming):
    providers = [
        {"name": "deepseek", "model": "deepseek-v4-flash", "base_url": "http://first.invalid/v1"},
        {"name": "groq", "model": "moonshotai/kimi-k2-instruct", "base_url": "http://second.invalid/v1"},
    ]
    _route_fixture(monkeypatch, providers)
    sent = []

    def upstream(_url, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        if len(sent) == 1:
            return _Resp(status_code=500)
        resp = _Resp(data={"choices": [{"message": {
            "role": "assistant", "content": "done", "reasoning_content": "remove-me"}}]})
        wire = _sse_event({"content": "done", "reasoning_content": "remove-me"}) + b"\ndata: [DONE]\n\n"
        resp.iter_content = lambda **_kwargs: iter([wire])
        return resp

    monkeypatch.setattr(router._HTTP, "post", upstream)
    payload = {"model": "hermes-router", "messages": copy.deepcopy(HISTORY), "tools": TOOLS}
    original = copy.deepcopy(payload)
    result = router._route_completion(payload, streaming)
    assert len(sent) == 2
    assert sent[0]["messages"][1]["reasoning_content"] == "thinking…"
    assert "reasoning_content" not in sent[1]["messages"][1]
    output = b"".join(result[1]).decode() if streaming else json.dumps(result[1])
    assert "remove-me" not in output
    assert payload == original
