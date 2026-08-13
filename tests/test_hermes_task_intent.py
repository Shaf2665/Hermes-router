"""Hall → hermes_agent runtime → Hermes Router task-intent routing hint."""

import io
import itertools
import json

import router

from hermes_agent import __main__ as agent_main
from hermes_agent.__main__ import read_task_input
from hermes_agent.client import HermesInferenceClient
from hermes_router_client import RouterConfig


def _stdin(monkeypatch, document: dict) -> None:
    buffer = io.BytesIO(json.dumps(document).encode("utf-8"))
    monkeypatch.setattr(agent_main.sys, "stdin", type("Stdin", (), {"buffer": buffer})())


def _client(recorder) -> HermesInferenceClient:
    def request(_config, _method, _path, _payload=None, extra_headers=None):
        recorder.append(extra_headers or {})
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    return HermesInferenceClient(
        RouterConfig("http://127.0.0.1:8319/v1", "sk-test", "hermes-router", 10), request
    )


# ── runtime: stdin parsing ────────────────────────────────────────────────────

def test_hall_style_stdin_task_intent_is_parsed(monkeypatch):
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-1", "task_intent": "coding"})

    assert read_task_input() == ("task", "hall-run-1", "coding")


def test_missing_task_intent_preserves_old_behaviour(monkeypatch):
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-2"})

    assert read_task_input() == ("task", "hall-run-2", None)


def test_invalid_task_intent_falls_back_without_failing_the_task(monkeypatch):
    for bogus in ("refactoring", "CODING", "", 7, None, {"a": 1}):
        _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-3", "task_intent": bogus})
        assert read_task_input() == ("task", "hall-run-3", None)


def test_general_intent_is_carried_but_routes_as_today(monkeypatch):
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-4", "task_intent": "general"})

    assert read_task_input()[2] == "general"
    assert router._task_intent("general") is None


# ── client: the intent reaches the router as a header ─────────────────────────

def test_intent_reaches_router_as_header_alongside_existing_ones():
    sent: list = []
    client = _client(sent)
    client.task_intent = "review"

    client.complete([{"role": "user", "content": "hi"}], [], "run-9")

    assert sent[0] == {
        "X-Hermes-Profile": "agent",
        "X-Hermes-Agent-Run": "run-9",
        "X-Hermes-Task-Intent": "review",
    }


def test_client_without_intent_sends_exactly_the_old_headers():
    sent: list = []

    _client(sent).complete([{"role": "user", "content": "hi"}], [], "run-10")

    assert sent[0] == {"X-Hermes-Profile": "agent", "X-Hermes-Agent-Run": "run-10"}


# ── router: validation and candidate ordering ─────────────────────────────────

def test_router_validates_the_intent_header():
    assert router._task_intent("coding") == "coding"
    assert router._task_intent("  Debug ") == "debug"
    assert router._task_intent("general") is None
    assert router._task_intent("nonsense") is None
    assert router._task_intent(None) is None


def _pin_round_robin(monkeypatch):
    """_get_smart_ordered rotates PROVIDERS per call for load spreading; pin the
    rotation so a before/after ordering comparison is about intent, not luck."""
    monkeypatch.setattr(router, "_rr_counter", itertools.repeat(0))


def _providers():
    return [
        {"name": "cheap", "model": "cheap-model", "models": ["cheap-model"], "keys": ["k"]},
        {"name": "smart", "model": "smart-model", "models": ["smart-model"], "keys": ["k"]},
    ]


def _names(ordered):
    return [candidate["provider"]["name"] for candidate in ordered]


def test_reasoning_intent_changes_candidate_preference(monkeypatch):
    _pin_round_robin(monkeypatch)
    monkeypatch.setattr(
        router,
        "_model_state",
        {
            ("cheap", "cheap-model"): {"rating": 3, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": False},
            ("smart", "smart-model"): {"rating": 3, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": True},
        },
    )
    providers = _providers()

    baseline = _names(router._get_smart_ordered(providers, complexity=3))
    planning = _names(router._get_smart_ordered(providers, complexity=3, intent="planning"))
    review = _names(router._get_smart_ordered(providers, complexity=3, intent="review"))

    assert planning[0] == "smart"
    assert review == planning
    assert sorted(baseline) == sorted(planning)  # a reorder, never a filter


def test_coding_intent_demotes_a_model_known_not_to_support_tools(monkeypatch):
    _pin_round_robin(monkeypatch)
    monkeypatch.setattr(
        router,
        "_model_state",
        {
            ("cheap", "cheap-model"): {"rating": 3, "supports_tools": False,
                                       "tools_confirmed": True, "reasoning": False},
            ("smart", "smart-model"): {"rating": 3, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": False},
        },
    )
    providers = _providers()

    assert _names(router._get_smart_ordered(providers, complexity=3, intent="coding"))[0] == "smart"
    # Still only a preference: the tool-less candidate stays in the cascade.
    assert sorted(_names(router._get_smart_ordered(providers, complexity=3, intent="coding"))) == [
        "cheap",
        "smart",
    ]


def test_general_and_missing_intent_leave_ordering_identical(monkeypatch):
    _pin_round_robin(monkeypatch)
    monkeypatch.setattr(
        router,
        "_model_state",
        {
            ("cheap", "cheap-model"): {"rating": 3, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": False},
            ("smart", "smart-model"): {"rating": 3, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": True},
        },
    )
    providers = _providers()

    baseline = _names(router._get_smart_ordered(providers, complexity=3))

    assert _names(router._get_smart_ordered(providers, complexity=3, intent=None)) == baseline
    assert _names(
        router._get_smart_ordered(providers, complexity=3, intent=router._task_intent("general"))
    ) == baseline


def test_intent_never_promotes_a_too_weak_model_over_a_capable_one(monkeypatch):
    _pin_round_robin(monkeypatch)
    monkeypatch.setattr(
        router,
        "_model_state",
        {
            # Reasoning, but too weak for this request — tier must still win.
            ("cheap", "cheap-model"): {"rating": 5, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": True},
            ("smart", "smart-model"): {"rating": 1, "supports_tools": True,
                                       "tools_confirmed": True, "reasoning": False},
        },
    )

    ordered = router._get_smart_ordered(_providers(), complexity=1, intent="planning")

    assert _names(ordered)[0] == "smart"


def test_debug_intent_reuses_the_existing_fast_route_preference(monkeypatch):
    # Two otherwise identical candidates (same model → same price/quality/rating),
    # so only the pre-existing low-latency term can separate them.
    _pin_round_robin(monkeypatch)
    monkeypatch.setattr(router, "_FAST_PROVIDERS", {"beta"})
    monkeypatch.setattr(router, "_model_state", {})
    providers = [
        {"name": "alpha", "model": "shared-model", "models": ["shared-model"], "keys": ["k"]},
        {"name": "beta", "model": "shared-model", "models": ["shared-model"], "keys": ["k"]},
    ]

    baseline = _names(router._get_smart_ordered(providers, complexity=3, est_tokens=0))
    debug = _names(router._get_smart_ordered(providers, complexity=3, est_tokens=0,
                                             intent="debug"))

    assert baseline == ["alpha", "beta"]
    assert debug == ["beta", "alpha"]


# ── vision: hint alone must not make a text request multimodal ────────────────

def test_vision_intent_alone_does_not_make_a_text_request_multimodal(monkeypatch):
    seen: list = []
    monkeypatch.setattr(router, "PROVIDERS", _providers())
    monkeypatch.setattr(
        router,
        "_get_smart_ordered",
        lambda _p, _c, _t=0, _local=False, intent=None: seen.append(intent) or [],
    )
    text_payload = {"messages": [{"role": "user", "content": "no images here"}]}
    image_payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}],
            }
        ]
    }

    router._ordered_providers(text_payload, False, "vision")
    router._ordered_providers(image_payload, False, "vision")

    assert seen == [None, "vision"]
    assert router._payload_has_image(text_payload) is False


# ── end-to-end: header → routing → normal fallback ────────────────────────────

class _Pool:
    def key_count(self, _provider, _model):
        return 1

    def get_key(self, _provider, _model):
        return "provider-key"

    def mark_rate_limited(self, *_args, **_kwargs):
        pass

    def mark_key_down(self, *_args, **_kwargs):
        pass


class _Response:
    headers: dict = {}
    text = ""

    def __init__(self, status_code=200):
        self.status_code = status_code

    @staticmethod
    def json():
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "done"},
                             "finish_reason": "stop"}]}


def _agent_payload():
    return {
        "model": "hermes-router",
        "messages": [{"role": "user", "content": "task"}],
        "tools": [{"type": "function", "function": {"name": "project_read"}}],
    }


def test_intent_header_reaches_the_routing_pipeline(monkeypatch):
    seen: list = []
    provider = {"name": "agent-test", "model": "tool-model", "models": ["tool-model"],
                "keys": ["provider-key"], "base_url": "https://invalid.example/v1"}
    candidate = {"provider": provider, "model": "tool-model", "list_index": 0}
    monkeypatch.setattr(
        router,
        "_ordered_providers",
        lambda _payload, _local=False, intent=None: seen.append(intent) or [candidate],
    )
    monkeypatch.setattr(router, "_model_has_confirmed_tool_support", lambda *_args: True)
    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(router, "forward", lambda *_args: _Response())
    router._agent_affinity.clear()

    with router.app.test_request_context(
        headers={"X-Hermes-Profile": "agent", "X-Hermes-Agent-Run": "run-i",
                 "X-Hermes-Task-Intent": "coding"}
    ):
        assert router._route_completion(_agent_payload(), False, "test")[0] == "json"
    with router.app.test_request_context(
        headers={"X-Hermes-Profile": "agent", "X-Hermes-Agent-Run": "run-j",
                 "X-Hermes-Task-Intent": "bogus"}
    ):
        assert router._route_completion(_agent_payload(), False, "test")[0] == "json"
    with router.app.test_request_context(headers={"X-Hermes-Profile": "agent"}):
        assert router._route_completion(_agent_payload(), False, "test")[0] == "json"

    assert seen == ["coding", None, None]


def test_provider_fallback_still_cascades_with_an_intent(monkeypatch):
    first = {"name": "first", "model": "m1", "models": ["m1"], "keys": ["k"],
             "base_url": "https://invalid.example/v1"}
    second = {"name": "second", "model": "m2", "models": ["m2"], "keys": ["k"],
              "base_url": "https://invalid.example/v1"}
    ordered = [
        {"provider": first, "model": "m1", "list_index": 0},
        {"provider": second, "model": "m2", "list_index": 0},
    ]
    attempts: list = []

    def forward(provider, _key, _payload, *_args):
        attempts.append(provider["name"])
        return _Response(429 if provider["name"] == "first" else 200)

    monkeypatch.setattr(router, "_ordered_providers", lambda *_args, **_kw: ordered)
    monkeypatch.setattr(router, "_model_has_confirmed_tool_support", lambda *_args: True)
    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(router, "forward", forward)
    router._agent_affinity.clear()

    with router.app.test_request_context(
        headers={"X-Hermes-Profile": "agent", "X-Hermes-Agent-Run": "run-f",
                 "X-Hermes-Task-Intent": "coding"}
    ):
        result = router._route_completion(_agent_payload(), False, "test")

    assert result[0] == "json"
    assert attempts == ["first", "second"]
