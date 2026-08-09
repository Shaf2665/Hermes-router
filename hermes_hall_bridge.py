#!/usr/bin/env python3
"""A credential-isolated structured-CLI bridge for Hall of Wisdom.

This bridge is deliberately advisory-only: it can ask Hermes Router for a
response, but it never reads a project, changes files, or executes commands.
Its stdout is a small JSONL protocol intended for a Hall adapter to translate
through Hall's EventFactory. Configuration is read only from the local process
environment; task input can never supply a router URL or router API key.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Iterable, Mapping

from hermes_router_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_SECONDS,
    RouterClientError,
    RouterConfig,
    environment_value,
    load_router_config,
    normalise_base_url,
    request_json,
    safe_router_error,
)


BRIDGE_PROTOCOL = "hermes-hall-bridge/v1"
BRIDGE_VERSION = "0.1.0"
MAX_PROMPT_BYTES = 100_000
MAX_MESSAGE_CHARS = 16_000
MAX_OUTPUT_CHARS = 128_000

SYSTEM_PROMPT = """You are Hermes Router operating as an advisory agent for Hall of Wisdom.
Provide analysis, a plan, code-review feedback, or implementation guidance for the user task.
You have no filesystem, shell, browser, or external-tool access in this mode. Do not claim to
have inspected, changed, executed, or verified anything outside the text in this conversation.
Do not ask for or reveal credentials. Keep the response concise and actionable."""


BridgeError = RouterClientError
BridgeConfig = RouterConfig
_environment_value = environment_value
_normalise_base_url = normalise_base_url
_safe_router_error = safe_router_error
load_config = load_router_config


def detect(config: BridgeConfig, request: Callable[..., Any] = request_json) -> dict[str, Any]:
    try:
        result = request(config, "GET", "/models")
        models = result.get("data") if isinstance(result, dict) else None
        model_ids = {item.get("id") for item in models if isinstance(item, dict)} if isinstance(models, list) else set()
        if config.model not in model_ids:
            raise BridgeError("HERMES_ROUTER_MODEL_UNAVAILABLE", "Hermes Router does not advertise the configured model.")
    except BridgeError as error:
        return {"protocol": BRIDGE_PROTOCOL, "available": False, "code": error.code, "message": error.message}
    return {
        "protocol": BRIDGE_PROTOCOL,
        "available": True,
        "capabilities": ["structured.events"],
    }


def _response_text(response: Any) -> str:
    try:
        choices = response["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise BridgeError("HERMES_ROUTER_INVALID_RESPONSE", "Hermes Router returned an invalid response.") from error
    if not isinstance(content, str) or not content.strip():
        raise BridgeError("HERMES_ROUTER_INVALID_RESPONSE", "Hermes Router returned an empty response.")
    if len(content) > MAX_OUTPUT_CHARS:
        raise BridgeError("HERMES_ROUTER_RESPONSE_TOO_LARGE", "Hermes Router returned too much data.")
    return content


def run_advisory(prompt: str, config: BridgeConfig, request: Callable[..., Any] = request_json) -> Iterable[dict[str, Any]]:
    if not prompt.strip():
        raise BridgeError("HERMES_ROUTER_PROMPT_INVALID", "The Hall task prompt is empty.")
    payload = {
        "model": config.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    response = request(config, "POST", "/chat/completions", payload)
    text = _response_text(response)
    yield {"protocol": BRIDGE_PROTOCOL, "type": "run.started"}
    for start in range(0, len(text), MAX_MESSAGE_CHARS):
        yield {"protocol": BRIDGE_PROTOCOL, "type": "message.delta", "text": text[start : start + MAX_MESSAGE_CHARS]}
    yield {
        "protocol": BRIDGE_PROTOCOL,
        "type": "run.completed",
        "summary": "Hermes Router advisory response completed.",
    }


def _emit(event: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_prompt() -> str:
    data = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
    if len(data) > MAX_PROMPT_BYTES:
        raise BridgeError("HERMES_ROUTER_PROMPT_TOO_LARGE", "The Hall task prompt is too large.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BridgeError("HERMES_ROUTER_PROMPT_INVALID", "The Hall task prompt is invalid.") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Router bridge for Hall of Wisdom")
    parser.add_argument("command", choices=("detect", "run", "version"))
    args = parser.parse_args(argv)
    if args.command == "version":
        _emit({"protocol": BRIDGE_PROTOCOL, "version": BRIDGE_VERSION})
        return 0
    try:
        if args.command == "detect":
            try:
                _emit(detect(load_config()))
            except BridgeError as error:
                _emit({"protocol": BRIDGE_PROTOCOL, "available": False, "code": error.code, "message": error.message})
            return 0
        config = load_config()
        for event in run_advisory(_read_prompt(), config):
            _emit(event)
        return 0
    except BridgeError as error:
        _emit({"protocol": BRIDGE_PROTOCOL, "type": "run.failed", "code": error.code, "message": error.message})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
