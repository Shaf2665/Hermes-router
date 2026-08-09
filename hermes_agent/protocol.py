"""Versioned JSONL protocol emitted by the local runtime."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Mapping

from . import PROTOCOL_VERSION, RUNTIME_VERSION


MAX_EVENT_BYTES = 24_000
# JSON may expand one Unicode code point to a 12-byte surrogate pair. This
# conservative character bound keeps message events below MAX_EVENT_BYTES even
# for adversarial text, including the event envelope and escaping.
MAX_MESSAGE_CHARS = 1_500
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def safe_run_id(value: object) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise ValueError("run_id must be a bounded safe identifier")
    return text


class EventEmitter:
    def __init__(
        self,
        run_id: str,
        sink: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        self.run_id = safe_run_id(run_id)
        self.sequence = 0
        self._sink = sink or self._write_stdout

    @staticmethod
    def _write_stdout(event: Mapping[str, Any]) -> None:
        sys.stdout.write(json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n")
        sys.stdout.flush()

    def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "protocol": PROTOCOL_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "type": event_type,
            "payload": dict(payload or {}),
        }
        encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ValueError("event exceeds protocol size limit")
        self.sequence += 1
        self._sink(event)
        return event

    def message(self, text: str) -> None:
        for start in range(0, len(text), MAX_MESSAGE_CHARS):
            self.emit("message.delta", {"text": text[start : start + MAX_MESSAGE_CHARS]})


def machine_document(**values: Any) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "runtime_version": RUNTIME_VERSION,
        **values,
    }
