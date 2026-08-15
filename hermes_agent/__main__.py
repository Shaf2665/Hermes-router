"""CLI entrypoint for ``python -m hermes_agent``."""

from __future__ import annotations

import argparse
import base64
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

# Optional materialized-attachment manifest an orchestrator (Hall of Wisdom) may
# send alongside the task. Purely additive: a missing, malformed, or oversized
# manifest degrades to no attachments rather than failing an otherwise valid
# task — the same "hint, not an instruction" discipline task_intent already
# uses. Every bound here is defensive: a caller that already validated its own
# payload should never hit these, but this runtime never trusts that.
MAX_ATTACHMENT_ENTRIES = 50
MAX_ATTACHMENT_RELATIVE_PATH_CHARS = 1024
MAX_ATTACHMENT_FILENAME_CHARS = 200
MAX_ATTACHMENT_MIME_TYPE_CHARS = 255
MAX_ATTACHMENTS_SECTION_CHARS = 8000

# Bounds for turning an "image"-kind attachment into real multimodal
# content (base64 data-URL image_url blocks). Deliberately conservative
# relative to Hall's own 64MB/20-attachment materialization cap. Unlike
# `_parse_attachment_entry`'s "degrade, never crash" discipline for a
# malformed *manifest entry*, a real image attachment that fails to
# prepare (missing, unreadable, oversized, or an unsupported image type)
# fails the whole run — see `build_image_content_parts`'s doc comment for
# why a required vision request must never silently become a smaller
# image set or fall back to text-only.
MAX_IMAGE_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_ATTACHMENT_BYTES = 40 * 1024 * 1024

# The image kinds Hall's own upload-time validation allows (see
# `ALLOWED_ATTACHMENT_MIME_TYPES` in `packages/protocol/src/attachment.ts`)
# — checked again here, independently, since this runtime never trusts a
# caller's own validation for anything it is about to send to a model.
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

IMAGE_ATTACHMENT_UNAVAILABLE_CODE = "HERMES_AGENT_IMAGE_ATTACHMENT_UNAVAILABLE"
IMAGE_ATTACHMENT_UNAVAILABLE_MESSAGE = (
    "A required image attachment could not be prepared for vision analysis."
)


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


def _status_has_vision_model(response: Any) -> bool:
    """Whether the router's live `/status` currently reports at least one
    available provider with a vision-capable model. Reuses `model_caps`'
    `supports_vision` field (router.py computes it from its own existing,
    non-hardcoded model-family pattern list — never duplicated here) the
    same way `_status_has_tool_model` reuses `supports_tools`. Unlike tool
    support, this is never a hard requirement for `detect_document()`
    overall availability — a missing vision model only means
    `vision_available` is omitted/false, never that the whole runtime is
    unavailable, so normal coding/review/general routing is unaffected."""
    if not isinstance(response, dict) or not isinstance(response.get("providers"), dict):
        return False
    for provider in response["providers"].values():
        if not isinstance(provider, dict) or provider.get("available") is False:
            continue
        model_caps = provider.get("model_caps")
        if isinstance(model_caps, list) and any(
            isinstance(capability, dict) and capability.get("supports_vision") is True
            for capability in model_caps
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
        # Additive, never a hard requirement — see _status_has_vision_model's
        # doc comment. `status` was already fetched above for the tool-model
        # check; this is not a second /status request.
        vision_available=_status_has_vision_model(status),
    )


def _is_safe_relative_attachment_path(relative_path: str) -> bool:
    """True only for a path Hall could plausibly have materialized inside the
    worktree this runtime's cwd already is — never an absolute host path, a
    UNC/drive-rooted path, or one containing a traversal segment. Hall's own
    TypeScript side already builds `relative_path` from validated components
    before ever sending it, but this runtime never trusts that by itself."""
    if not relative_path or len(relative_path) > MAX_ATTACHMENT_RELATIVE_PATH_CHARS:
        return False
    if "\0" in relative_path:
        return False
    if relative_path.startswith(("/", "\\")):
        return False
    if len(relative_path) >= 2 and relative_path[1] == ":":
        return False
    segments = relative_path.replace("\\", "/").split("/")
    return not any(segment in ("", ".", "..") for segment in segments)


def _parse_attachment_entry(raw: Any) -> dict[str, str] | None:
    """Returns a validated `{relative_path, filename, mime_type, kind}` entry,
    or `None` for anything that doesn't match — one malformed entry is
    dropped, never a reason to fail the whole task or the rest of the
    manifest. `kind` is carried through as opaque display data only; no
    branch anywhere treats an `"image"` kind differently (attachments —
    including images — are ordinary file-path context in this runtime)."""
    if not isinstance(raw, dict):
        return None
    relative_path = raw.get("relative_path")
    filename = raw.get("filename")
    mime_type = raw.get("mime_type")
    kind = raw.get("kind")
    if (
        not isinstance(relative_path, str)
        or not _is_safe_relative_attachment_path(relative_path)
        or not isinstance(filename, str)
        or not filename
        or len(filename) > MAX_ATTACHMENT_FILENAME_CHARS
        or "\0" in filename
        or not isinstance(mime_type, str)
        or not mime_type
        or len(mime_type) > MAX_ATTACHMENT_MIME_TYPE_CHARS
        or "\0" in mime_type
    ):
        return None
    return {
        "relative_path": relative_path,
        "filename": filename,
        "mime_type": mime_type,
        "kind": kind if isinstance(kind, str) and kind else "file",
    }


def _parse_attachments(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    parsed = [entry for entry in (_parse_attachment_entry(item) for item in raw) if entry is not None]
    return parsed[:MAX_ATTACHMENT_ENTRIES]


def _build_attachments_section(attachments: list[dict[str, str]]) -> str:
    """A bounded, fixed-shape block appended to the task prompt — never
    parsed as instructions, just descriptive file-path context using only
    the relative paths Hall already materialized inside this runtime's own
    worktree. Returns "" for no attachments, which is what keeps a text-only
    task's prompt byte-identical to before this function existed."""
    if not attachments:
        return ""
    lines = [
        f"- {entry['relative_path']} ({entry['filename']}, {entry['mime_type']})"
        for entry in attachments
    ]
    section = "\n\nAttached files (read-only copies inside your working directory):\n" + "\n".join(
        lines
    )
    if len(section) > MAX_ATTACHMENTS_SECTION_CHARS:
        section = section[:MAX_ATTACHMENTS_SECTION_CHARS] + "\n[... attachment list truncated ...]"
    return section


def build_prompt_with_attachments(prompt: str, attachments: list[dict[str, str]]) -> str:
    return prompt + _build_attachments_section(attachments)


def _unavailable_image_error() -> AgentError:
    return AgentError(IMAGE_ATTACHMENT_UNAVAILABLE_CODE, IMAGE_ATTACHMENT_UNAVAILABLE_MESSAGE)


def _read_image_bytes_or_fail(relative_path: str, cwd: str, remaining_total_budget: int) -> bytes:
    """Reads one already-materialized image straight from this runtime's
    own worktree (`cwd`) — never a second storage path. Raises
    `AgentError(IMAGE_ATTACHMENT_UNAVAILABLE_CODE, ...)` — never returns a
    partial result — for anything that isn't a plain, readable, in-budget
    file: missing (`os.path.getsize` fails), unreadable (`open` fails),
    or oversized (exceeds the per-image cap or the remaining total
    budget). `_is_safe_relative_attachment_path` already rejected
    traversal at manifest-parse time; this is a second, independent
    boundary. The raised error's message never includes the path,
    filename, or any OS error detail — see the "never raw process/OS
    output" discipline this module already applies to `AgentError`
    elsewhere."""
    absolute_path = os.path.join(cwd, *relative_path.split("/"))
    try:
        size = os.path.getsize(absolute_path)
    except OSError as error:
        raise _unavailable_image_error() from error
    if size <= 0 or size > MAX_IMAGE_ATTACHMENT_BYTES or size > remaining_total_budget:
        raise _unavailable_image_error()
    try:
        with open(absolute_path, "rb") as handle:
            data = handle.read(MAX_IMAGE_ATTACHMENT_BYTES + 1)
    except OSError as error:
        raise _unavailable_image_error() from error
    if len(data) > MAX_IMAGE_ATTACHMENT_BYTES:
        raise _unavailable_image_error()
    return data


def build_image_content_parts(
    attachments: list[dict[str, str]], cwd: str
) -> list[dict[str, Any]]:
    """Turns every `kind == "image"` attachment Hall already materialized
    into this worktree into an OpenAI-format `image_url` content block
    (a `data:` URL — the same format `router.py`'s existing
    `_openai_content_to_anthropic` already translates for Anthropic
    providers). Returns `[]` for no image attachments, which is what keeps
    `AgentRuntime`'s message content a plain string — unchanged — for
    every task that doesn't attach one.

    Fail-closed for a REQUIRED vision request (any image-kind attachment
    at all means vision was required — see `deriveTaskIntent` on Hall's
    side): if any single image is missing, unreadable, oversized, or an
    unsupported mime type, this raises immediately rather than returning
    a partial set — a vision request must never silently run with fewer
    images than were actually attached, and must never silently fall back
    to text-only. Called from `run_command()` strictly before `AgentRuntime.run()`,
    so a raise here means no router/model request is ever made for this
    run."""
    image_entries = [entry for entry in attachments if entry.get("kind") == "image"]
    if not image_entries:
        return []
    parts: list[dict[str, Any]] = []
    remaining_total_budget = MAX_TOTAL_IMAGE_ATTACHMENT_BYTES
    for entry in image_entries:
        if entry["mime_type"] not in SUPPORTED_IMAGE_MIME_TYPES:
            raise _unavailable_image_error()
        data = _read_image_bytes_or_fail(entry["relative_path"], cwd, remaining_total_budget)
        remaining_total_budget -= len(data)
        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{entry['mime_type']};base64,{encoded}"},
            }
        )
    return parts


def read_task_input() -> tuple[str, str, str | None, list[dict[str, str]]]:
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
    # Same discipline as task_intent: an unusable attachments manifest (wrong
    # type, malformed entries, too many entries) degrades to an empty list
    # rather than failing an otherwise valid task.
    attachments = _parse_attachments(value.get("attachments"))
    return prompt, run_id, task_intent, attachments


def run_command() -> int:
    emitter: EventEmitter | None = None
    runtime: AgentRuntime | None = None
    try:
        prompt, run_id, task_intent, attachments = read_task_input()
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
        image_parts = build_image_content_parts(attachments, os.getcwd())
        state = runtime.run(
            build_prompt_with_attachments(prompt, attachments), image_parts=image_parts
        )
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
