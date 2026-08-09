"""Shared, bounded client configuration for local Hermes integrations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:8319/v1"
DEFAULT_MODEL = "hermes-router"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
MAX_RESPONSE_BYTES = 2_000_000


class RouterClientError(Exception):
    """A bounded error safe to expose through a local integration protocol."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RouterConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int


def environment_value(environment: Mapping[str, str], name: str) -> str | None:
    """Read a variable case-insensitively for Windows."""
    target = name.casefold()
    for key, value in environment.items():
        if key.casefold() == target:
            return value
    return None


def normalise_base_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError as error:
        raise RouterClientError(
            "HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid."
        ) from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise RouterClientError(
            "HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid."
        )
    path = parts.path.rstrip("/")
    if path != "/v1":
        raise RouterClientError(
            "HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid."
        )
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def load_router_config(environment: Mapping[str, str] | None = None) -> RouterConfig:
    env = os.environ if environment is None else environment
    base_url = normalise_base_url(
        environment_value(env, "HERMES_ROUTER_BASE_URL") or DEFAULT_BASE_URL
    )
    api_key = (environment_value(env, "HERMES_ROUTER_API_KEY") or "").strip()
    model = (environment_value(env, "HERMES_ROUTER_MODEL") or DEFAULT_MODEL).strip()
    timeout_text = (
        environment_value(env, "HERMES_ROUTER_TIMEOUT_SECONDS")
        or str(DEFAULT_TIMEOUT_SECONDS)
    ).strip()
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
        raise RouterClientError(
            "HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid."
        )
    try:
        timeout_seconds = int(timeout_text)
    except ValueError as error:
        raise RouterClientError(
            "HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid."
        ) from error
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise RouterClientError(
            "HERMES_ROUTER_CONFIG_INVALID", "Hermes Router configuration is invalid."
        )
    return RouterConfig(base_url, api_key, model, timeout_seconds)


def safe_router_error(status_code: int) -> RouterClientError:
    if status_code in {401, 403}:
        return RouterClientError(
            "HERMES_ROUTER_AUTH_REJECTED", "Hermes Router rejected its local credentials."
        )
    if status_code == 429:
        return RouterClientError(
            "HERMES_ROUTER_RATE_LIMITED", "Hermes Router is rate limited."
        )
    if status_code in {404, 405}:
        return RouterClientError(
            "HERMES_ROUTER_PROTOCOL_UNAVAILABLE",
            "Hermes Router does not expose the required API.",
        )
    if 500 <= status_code <= 599:
        return RouterClientError(
            "HERMES_ROUTER_UNAVAILABLE", "Hermes Router is temporarily unavailable."
        )
    return RouterClientError(
        "HERMES_ROUTER_REQUEST_FAILED", "Hermes Router could not process the request."
    )


def request_json(
    config: RouterConfig,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> Any:
    """Make one bounded JSON request without returning raw error content."""
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Accept": "application/json",
        **dict(extra_headers or {}),
    }
    try:
        response = requests.request(
            method,
            f"{config.base_url}{path}",
            headers=headers,
            json=payload,
            timeout=(5, config.timeout_seconds),
            stream=True,
        )
    except requests.RequestException as error:
        raise RouterClientError(
            "HERMES_ROUTER_UNREACHABLE", "Hermes Router could not be reached."
        ) from error

    try:
        if not 200 <= response.status_code < 300:
            raise safe_router_error(response.status_code)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65_536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise RouterClientError(
                    "HERMES_ROUTER_RESPONSE_TOO_LARGE",
                    "Hermes Router returned too much data.",
                )
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RouterClientError(
                "HERMES_ROUTER_INVALID_RESPONSE",
                "Hermes Router returned an invalid response.",
            ) from error
    except requests.RequestException as error:
        raise RouterClientError(
            "HERMES_ROUTER_UNREACHABLE", "Hermes Router could not be reached."
        ) from error
    finally:
        response.close()
