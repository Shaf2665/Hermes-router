import json
import threading

from hermes_agent import CAPABILITIES, PROTOCOL_VERSION, RUNTIME_VERSION
from hermes_agent.client import HermesInferenceClient
from hermes_agent.errors import AgentError
from hermes_agent.protocol import EventEmitter, MAX_EVENT_BYTES
from hermes_agent.runtime import AgentRuntime
from hermes_router_client import RouterConfig


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools, run_id):
        self.calls.append((messages, tools, run_id))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool_call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_agent_loop_emits_bounded_structured_lifecycle(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    responses = [
        {
            "content": None,
            "tool_calls": [
                tool_call(
                    "call-1",
                    "project.apply_patch",
                    {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
                )
            ],
        },
        {"content": "Updated the value and completed the task."},
    ]
    events = []
    emitter = EventEmitter("run-1", events.append)
    runtime = AgentRuntime(ScriptedClient(responses), tmp_path, emitter)

    assert runtime.run("Update the value") == "completed"

    assert [event["type"] for event in events] == [
        "run.started",
        "tool.started",
        "tool.completed",
        "file.changed",
        "message.delta",
        "run.completed",
    ]
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert all(event["protocol"] == PROTOCOL_VERSION for event in events)
    assert all(event["runtime_version"] == RUNTIME_VERSION for event in events)
    assert events[3]["payload"] == {"path": "app.py", "operation": "modified"}
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_runtime_emits_failure_and_cancellation_terminals(tmp_path):
    failed_events = []
    failed = AgentRuntime(
        ScriptedClient([AgentError("TEST_FAILURE", "Safe failure.")]),
        tmp_path,
        EventEmitter("run-failed", failed_events.append),
    )
    assert failed.run("task") == "failed"
    assert [event["type"] for event in failed_events] == ["run.started", "run.failed"]
    assert failed_events[-1]["payload"] == {"code": "TEST_FAILURE", "message": "Safe failure."}

    cancelled_events = []
    cancelled_flag = threading.Event()
    cancelled_flag.set()
    cancelled = AgentRuntime(
        ScriptedClient([]),
        tmp_path,
        EventEmitter("run-cancelled", cancelled_events.append),
        cancelled=cancelled_flag,
    )
    assert cancelled.run("task") == "cancelled"
    assert [event["type"] for event in cancelled_events] == [
        "run.started",
        "run.cancelled",
    ]


def test_inference_client_selects_agent_profile_and_run_affinity():
    observed = []

    def request(*args, **kwargs):
        observed.append((args, kwargs))
        return {"choices": [{"message": {"content": "done"}}]}

    config = RouterConfig("http://127.0.0.1:8319/v1", "secret", "hermes-router", 10)
    message = HermesInferenceClient(config, request).complete(
        [{"role": "user", "content": "task"}], [], "run-affinity"
    )

    assert message == {"content": "done"}
    payload = observed[0][0][3]
    assert payload["stream"] is False
    assert payload["parallel_tool_calls"] is False
    assert observed[0][1]["extra_headers"] == {
        "X-Hermes-Profile": "agent",
        "X-Hermes-Agent-Run": "run-affinity",
    }


def test_capability_vocabulary_is_exact():
    assert CAPABILITIES == (
        "project.read",
        "project.edit",
        "command.execute",
        "structured.events",
        "cancellation",
    )
    assert "git.inspect" not in CAPABILITIES


def test_message_events_remain_bounded_for_escaped_unicode():
    events = []
    text = ("\x00\U0001f680") * 5_000

    EventEmitter("bounded-message", events.append).message(text)

    assert "".join(event["payload"]["text"] for event in events) == text
    assert all(
        len(json.dumps(event, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        <= MAX_EVENT_BYTES
        for event in events
    )
