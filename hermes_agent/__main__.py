"""CLI entrypoint for ``python -m hermes_agent``."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import uuid
from typing import Any

from hermes_router_client import RouterClientError, load_router_config, request_json

from . import CAPABILITIES
from .client import HermesInferenceClient
from .errors import AgentCancelled, AgentError
from .protocol import EventEmitter, machine_document, safe_run_id
from .runtime import AgentRuntime


MAX_INPUT_BYTES = 100_000

# Optional workload hint an orchestrator (Hall of Wisdom) may send alongside the
# task. Purely additive: anything else — including a missing field — routes as
# "general", i.e. exactly as this runtime behaved before the field existed.
TASK_INTENTS = frozenset({"planning", "coding", "review", "debug", "vision", "general"})


def emit_document(document: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(document, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()


def capabilities_document() -> dict[str, Any]:
    return machine_document(
        capabilities=list(CAPABILITIES),
        integration_level="structured_cli",
        execution_trust="trusted_local",
    )


def _status_has_tool_model(response: Any) -> bool:
    if not isinstance(response, dict) or not isinstance(response.get("providers"), dict):
        return False
    for provider in response["providers"].values():
        if not isinstance(provider, dict) or provider.get("available") is False:
            continue
        model_caps = provider.get("model_caps")
        if isinstance(model_caps, list) and any(
            isinstance(capability, dict)
            and capability.get("supports_tools") is True
            and capability.get("tools_confirmed") is True
            for capability in model_caps
        ):
            return True
        if (
            provider.get("supports_tools") is True
            and provider.get("tools_confirmed") is True
        ):
            return True
    return False


def detect_document() -> dict[str, Any]:
    try:
        config = load_router_config()
        response = request_json(config, "GET", "/models")
        models = response.get("data") if isinstance(response, dict) else None
        model_ids = {
            item.get("id") for item in models if isinstance(item, dict)
        } if isinstance(models, list) else set()
        if config.model not in model_ids:
            raise AgentError(
                "HERMES_ROUTER_MODEL_UNAVAILABLE",
                "Hermes Router does not advertise the configured model.",
            )
        status = request_json(config, "GET", "/status")
        if not _status_has_tool_model(status):
            raise AgentError(
                "HERMES_AGENT_TOOLS_UNAVAILABLE",
                "Hermes Router has no available tool-capable model.",
            )
    except (RouterClientError, AgentError) as error:
        return machine_document(available=False, code=error.code, message=error.message)
    return machine_document(
        available=True,
        capabilities=list(CAPABILITIES),
        integration_level="structured_cli",
        execution_trust="trusted_local",
    )


def read_task_input() -> tuple[str, str, str | None]:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise AgentError("HERMES_AGENT_INPUT_TOO_LARGE", "Task input exceeds the runtime limit.")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentError("HERMES_AGENT_INPUT_INVALID", "Task input must be a JSON object.") from error
    if not isinstance(value, dict):
        raise AgentError("HERMES_AGENT_INPUT_INVALID", "Task input must be a JSON object.")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentError("HERMES_AGENT_INPUT_INVALID", "Task input requires a non-empty prompt.")
    supplied_run_id = value.get("run_id")
    try:
        run_id = safe_run_id(supplied_run_id or f"hermes-{uuid.uuid4()}")
    except ValueError as error:
        raise AgentError("HERMES_AGENT_INPUT_INVALID", "Task run_id is invalid.") from error
    # An unusable task_intent is never an error — the field is an optional hint,
    # so it degrades to "general" rather than failing an otherwise valid task.
    supplied_intent = value.get("task_intent")
    task_intent = supplied_intent if isinstance(supplied_intent, str) and \
        supplied_intent in TASK_INTENTS else None
    return prompt, run_id, task_intent


def run_command() -> int:
    emitter: EventEmitter | None = None
    runtime: AgentRuntime | None = None
    try:
        prompt, run_id, task_intent = read_task_input()
        emitter = EventEmitter(run_id)
        config = load_router_config()
        runtime = AgentRuntime(
            HermesInferenceClient(config, task_intent=task_intent),
            os.getcwd(),
            emitter,
        )

        def handle_signal(_signum: int, _frame: Any) -> None:
            if runtime is not None:
                runtime.cancel()
            raise AgentCancelled()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        state = runtime.run(prompt)
        return 0 if state in {"completed", "cancelled"} else 1
    except AgentCancelled:
        if emitter is not None:
            emitter.emit(
                "run.cancelled",
                {"cancelled_by": "orchestrator", "reason": "Runtime cancellation requested."},
            )
            return 0
        emit_document(machine_document(type="run.cancelled", payload={}))
        return 0
    except RouterClientError as error:
        if emitter is not None:
            if emitter.sequence == 0:
                emitter.emit("run.started")
            emitter.emit("run.failed", {"code": error.code, "message": error.message})
        else:
            emit_document(
                machine_document(
                    type="run.failed", payload={"code": error.code, "message": error.message}
                )
            )
        return 1
    except AgentError as error:
        if emitter is not None:
            if emitter.sequence == 0:
                emitter.emit("run.started")
            emitter.emit("run.failed", {"code": error.code, "message": error.message})
        else:
            emit_document(
                machine_document(
                    type="run.failed", payload={"code": error.code, "message": error.message}
                )
            )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Coding Runtime for Hall of Wisdom")
    parser.add_argument("command", choices=("detect", "capabilities", "run"))
    args = parser.parse_args(argv)
    if args.command == "detect":
        emit_document(detect_document())
        return 0
    if args.command == "capabilities":
        emit_document(capabilities_document())
        return 0
    return run_command()


if __name__ == "__main__":
    raise SystemExit(main())
