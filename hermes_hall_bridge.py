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
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import requests


BRIDGE_PROTOCOL = "hermes-hall-bridge/v1"
BRIDGE_VERSION = "0.1.0"
DEFAULT_BASE_URL = "http://127.0.0.1:8319/v1"
DEFAULT_MODEL = "hermes-router"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
MAX_PROMPT_BYTES = 100_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_MESSAGE_CHARS = 16_000
MAX_OUTPUT_CHARS = 128_000

SYSTEM_PROMPT = """You are Hermes Router operating as an advisory agent for Hall of Wisdom.
Provide analysis, a plan, code-review feedback, or implementation guidance for the user task.
You have no filesystem, shell, browser, or external-tool access in this mode. Do not claim to
have inspected, changed, executed, or verified anything outside the text in this conversation.
Do not ask for or reveal credentials. Keep the response concise and actionable."""


class BridgeError(Exception):
    """A bounded error safe to emit to a local process supervisor."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BridgeConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int


def _environment_value(environment: Mapping[str, str], name: str) -> str | None:
    """Read a variable case-insensitively for Windows without duplicating keys."""
    target = name.casefold()
    for key, value in environment.items():
        if key.casefold() == target:
            return value
    return None


def _normalise_base_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError as error:
        raise BridgeError("HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid.") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise BridgeError("HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid.")
    path = parts.path.rstrip("/")
    if path != "/v1":
        raise BridgeError("HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid.")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def load_config(environment: Mapping[str, str] | None = None) -> BridgeConfig:
    env = os.environ if environment is None else environment
    base_url = _normalise_base_url(_environment_value(env, "HERMES_ROUTER_BASE_URL") or DEFAULT_BASE_URL)
    api_key = (_environment_value(env, "HERMES_ROUTER_API_KEY") or "").strip()
    model = (_environment_value(env, "HERMES_ROUTER_MODEL") or DEFAULT_MODEL).strip()
    timeout_text = (_environment_value(env, "HERMES_ROUTER_TIMEOUT_SECONDS") or str(DEFAULT_TIMEOUT_SECONDS)).strip()
    if (
        not api_key
        or not model
        or len(api_key) > 4096
        or len(model) > 200
        or "\x00" in api_key
        or "\x00" in model
        or "\r" in api_key
        or "\n" in api_key
    ):
        raise BridgeError("HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid.")
    try:
        timeout_seconds = int(timeout_text)
    except ValueError as error:
        raise BridgeError("HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid.") from error
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise BridgeError("HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid.")
    return BridgeConfig(base_url, api_key, model, timeout_seconds)


def _safe_router_error(status_code: int) -> BridgeError:
    if status_code in {401, 403}:
        return BridgeError("HERMES_ROUTER_AUTH_REJECTED", "Hermes Router rejected its local credentials.")
    if status_code == 429:
        return BridgeError("HERMES_ROUTER_RATE_LIMITED", "Hermes Router is rate limited.")
    if status_code in {404, 405}:
        return BridgeError("HERMES_ROUTER_PROTOCOL_UNAVAILABLE", "Hermes Router does not expose the required API.")
    if 500 <= status_code <= 599:
        return BridgeError("HERMES_ROUTER_UNAVAILABLE", "Hermes Router is temporarily unavailable.")
    return BridgeError("HERMES_ROUTER_REQUEST_FAILED", "Hermes Router could not process the request.")


def request_json(config: BridgeConfig, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """Make one bounded JSON request without ever returning raw error content."""
    try:
        response = requests.request(
            method,
            f"{config.base_url}{path}",
            headers={"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"},
            json=payload,
            timeout=(5, config.timeout_seconds),
            stream=True,
        )
    except requests.RequestException as error:
        raise BridgeError("HERMES_ROUTER_UNREACHABLE", "Hermes Router could not be reached.") from error

    try:
        if not 200 <= response.status_code < 300:
            raise _safe_router_error(response.status_code)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65_536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise BridgeError("HERMES_ROUTER_RESPONSE_TOO_LARGE", "Hermes Router returned too much data.")
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BridgeError("HERMES_ROUTER_INVALID_RESPONSE", "Hermes Router returned an invalid response.") from error
    except requests.RequestException as error:
        raise BridgeError("HERMES_ROUTER_UNREACHABLE", "Hermes Router could not be reached.") from error
    finally:
        response.close()


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
