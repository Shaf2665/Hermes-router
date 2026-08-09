import pytest

import hermes_hall_bridge as bridge


def config(**overrides):
    values = {
        "HERMES_ROUTER_BASE_URL": "http://127.0.0.1:8319/v1",
        "HERMES_ROUTER_API_KEY": "router-secret-not-emitted",
        "HERMES_ROUTER_MODEL": "hermes-router",
    }
    values.update(overrides)
    return bridge.load_config(values)


def test_config_requires_local_secret_and_canonical_v1_base_url():
    with pytest.raises(bridge.BridgeError) as error:
        bridge.load_config({"HERMES_ROUTER_BASE_URL": "http://127.0.0.1:8319/v1"})
    assert error.value.code == "HERMES_ROUTER_CONFIG_INVALID"

    with pytest.raises(bridge.BridgeError):
        bridge.load_config({"HERMES_ROUTER_API_KEY": "secret", "HERMES_ROUTER_BASE_URL": "http://host/other"})


def test_detect_only_reports_safe_availability_metadata():
    observed = []

    def request(*args):
        observed.append(args)
        return {"data": [{"id": "hermes-router"}]}

    result = bridge.detect(config(), request)

    assert result == {
        "protocol": bridge.BRIDGE_PROTOCOL,
        "available": True,
        "capabilities": ["structured.events"],
    }
    assert observed[0][1:] == ("GET", "/models")
    assert "router-secret" not in str(result)


def test_detect_redacts_router_errors():
    def request(*_args):
        raise bridge.BridgeError("HERMES_ROUTER_AUTH_REJECTED", "Hermes Router rejected its local credentials.")

    assert bridge.detect(config(), request) == {
        "protocol": bridge.BRIDGE_PROTOCOL,
        "available": False,
        "code": "HERMES_ROUTER_AUTH_REJECTED",
        "message": "Hermes Router rejected its local credentials.",
    }


def test_run_uses_a_fixed_advisory_system_prompt_and_emits_bounded_jsonl_events():
    observed = []
    text = "x" * (bridge.MAX_MESSAGE_CHARS + 1)

    def request(*args):
        observed.append(args)
        return {"choices": [{"message": {"content": text}}]}

    events = list(bridge.run_advisory("Review this change", config(), request))

    assert [event["type"] for event in events] == ["run.started", "message.delta", "message.delta", "run.completed"]
    assert len(events[1]["text"]) == bridge.MAX_MESSAGE_CHARS
    assert events[2]["text"] == "x"
    payload = observed[0][3]
    assert observed[0][1:3] == ("POST", "/chat/completions")
    assert payload["stream"] is False
    assert "no filesystem" in payload["messages"][0]["content"].lower()
    assert payload["messages"][1] == {"role": "user", "content": "Review this change"}


def test_run_rejects_empty_or_malformed_router_responses():
    with pytest.raises(bridge.BridgeError) as empty:
        list(bridge.run_advisory("", config(), lambda *_args: {}))
    assert empty.value.code == "HERMES_ROUTER_PROMPT_INVALID"

    with pytest.raises(bridge.BridgeError) as malformed:
        list(bridge.run_advisory("task", config(), lambda *_args: {"choices": []}))
    assert malformed.value.code == "HERMES_ROUTER_INVALID_RESPONSE"

    oversized = "x" * (bridge.MAX_OUTPUT_CHARS + 1)
    with pytest.raises(bridge.BridgeError) as too_large:
        list(bridge.run_advisory("task", config(), lambda *_args: {"choices": [{"message": {"content": oversized}}]}))
    assert too_large.value.code == "HERMES_ROUTER_RESPONSE_TOO_LARGE"
