"""Codex (ChatGPT OAuth) request translation: chat-completions → Responses API."""
import json

import router


TOOLS = [{"type": "function", "function": {
    "name": "get_time", "description": "Current time",
    "parameters": {"type": "object", "properties": {}}}}]


def test_plain_conversation_keeps_messages_and_instructions():
    body = router._to_codex_body({"messages": [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]}, "gpt-test")

    assert body["model"] == "gpt-test"
    assert body["instructions"] == "Be brief."
    assert body["input"] == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Hi"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Hello!"}]},
    ]


def test_tool_history_becomes_function_call_items():
    # Regression: a "tool" role message was forwarded verbatim and the Responses
    # API rejected the request with 400 "Invalid value: 'tool'".
    body = router._to_codex_body({"tools": TOOLS, "messages": [
        {"role": "user", "content": "What time is it?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "18:37"},
    ]}, "gpt-test")

    assert body["input"] == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "What time is it?"}]},
        {"type": "function_call", "call_id": "call_1",
         "name": "get_time", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "18:37"},
    ]
    assert all(item.get("role") != "tool" for item in body["input"])
    assert body["tools"][0]["name"] == "get_time"


def test_assistant_text_and_parallel_tool_calls_are_all_kept():
    body = router._to_codex_body({"messages": [
        {"role": "user", "content": "Weather in Berlin and Paris?"},
        {"role": "assistant", "content": "Checking both.", "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "weather", "arguments": '{"city": "Berlin"}'}},
            {"id": "b", "type": "function",
             "function": {"name": "weather", "arguments": {"city": "Paris"}}},
        ]},
        {"role": "tool", "tool_call_id": "a",
         "content": [{"type": "text", "text": "12°C"}]},
        {"role": "tool", "tool_call_id": "b", "content": "15°C"},
    ]}, "gpt-test")

    items = body["input"]
    assert items[1] == {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Checking both."}]}
    assert [i["call_id"] for i in items if i["type"] == "function_call"] == ["a", "b"]
    # dict arguments are serialised to the JSON string the API expects
    assert items[3]["arguments"] == '{"city": "Paris"}'
    assert items[4] == {"type": "function_call_output", "call_id": "a", "output": "12°C"}
    assert items[5] == {"type": "function_call_output", "call_id": "b", "output": "15°C"}


# ── Response side: Codex SSE events → chat-completions ────────────────────────
# With store=false the Codex backend leaves `output` empty in response.completed
# and delivers items only via response.output_item.done (observed live).
FUNCTION_CALL_EVENTS = [
    {"type": "response.output_item.done", "item": {
        "type": "function_call", "id": "fc_1", "status": "completed",
        "call_id": "call_9", "name": "get_time", "arguments": "{}"}},
    {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
]


class _FakeSSE:
    def __init__(self, events):
        self._lines = [("data: " + json.dumps(e)).encode() for e in events]

    def iter_lines(self):
        return iter(self._lines)


def test_non_streaming_reads_tool_calls_from_output_item_done():
    out = router._from_codex_response(FUNCTION_CALL_EVENTS)

    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [{
        "id": "call_9", "type": "function",
        "function": {"name": "get_time", "arguments": "{}"}}]


def test_streaming_reads_tool_calls_from_output_item_done():
    chunks = [c[len("data: "):].strip()
              for c in router._codex_streaming_generator(_FakeSSE(FUNCTION_CALL_EVENTS))]
    assert chunks[-1] == "[DONE]"
    parsed = [json.loads(c) for c in chunks[:-1]]

    tool_deltas = [p["choices"][0]["delta"]["tool_calls"] for p in parsed
                   if "tool_calls" in p["choices"][0]["delta"]]
    assert tool_deltas == [[{"index": 0, "id": "call_9", "type": "function",
                             "function": {"name": "get_time", "arguments": "{}"}}]]
    assert parsed[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_completed_output_still_wins_when_present():
    events = [{"type": "response.completed", "response": {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}]}}]
    out = router._from_codex_response(events)
    assert out["choices"][0]["message"]["content"] == "Hi"
    assert out["choices"][0]["finish_reason"] == "stop"
