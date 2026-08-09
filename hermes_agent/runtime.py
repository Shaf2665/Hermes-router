"""Minimal tool-calling coding loop for Hall-provided worktrees."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from hermes_router_client import RouterClientError

from .commands import CommandExecutor
from .errors import AgentCancelled, AgentError
from .protocol import EventEmitter
from .workspace import ToolOutcome, Workspace


MAX_AGENT_TURNS = 20
MAX_TOOL_CALLS = 50
MAX_FINAL_CHARS = 128_000

SYSTEM_PROMPT = """You are Hermes Coding Runtime operating inside a Hall-owned worktree.
Use the provided tools to inspect and modify only this worktree. Use project_apply_patch for edits.
Commands must be structured argv arrays; never construct shell command strings. Do not access .git,
delete files, request credentials, or claim a tool action succeeded unless its result says so.
Finish with a concise summary of the completed work and verification."""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "project_read",
            "description": "Read one UTF-8 text file inside the worktree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_search",
            "description": "Search for a literal text string inside worktree files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_apply_patch",
            "description": (
                "Replace exactly one old_text occurrence in a UTF-8 file, or create a new file "
                "with create=true. Deletion is unsupported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "create": {"type": "boolean"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["path", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "command_execute",
            "description": "Run a structured argv command in the worktree without a shell.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 64,
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        },
    },
]


class AgentRuntime:
    def __init__(
        self,
        client: Any,
        worktree: str | Path,
        emitter: EventEmitter,
        command_executor: CommandExecutor | None = None,
        cancelled: threading.Event | None = None,
    ):
        self.client = client
        self.workspace = Workspace(worktree)
        self.emitter = emitter
        self.cancelled = cancelled or threading.Event()
        self.commands = command_executor or CommandExecutor(
            self.workspace.root, cancelled=self.cancelled
        )
        self._terminal = False

    def cancel(self) -> None:
        self.cancelled.set()
        self.commands.cancel()

    def _emit_terminal(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._terminal:
            return
        self._terminal = True
        self.emitter.emit(event_type, payload)

    def run(self, prompt: str) -> str:
        self.emitter.emit("run.started")
        try:
            self._run_loop(prompt)
            return "completed"
        except AgentCancelled:
            self._emit_terminal(
                "run.cancelled",
                {"cancelled_by": "orchestrator", "reason": "Runtime cancellation requested."},
            )
            return "cancelled"
        except RouterClientError as error:
            self._emit_terminal("run.failed", {"code": error.code, "message": error.message})
            return "failed"
        except AgentError as error:
            self._emit_terminal("run.failed", {"code": error.code, "message": error.message})
            return "failed"
        except Exception:
            self._emit_terminal(
                "run.failed",
                {
                    "code": "HERMES_AGENT_INTERNAL_ERROR",
                    "message": "Hermes coding runtime failed unexpectedly.",
                },
            )
            return "failed"

    def _run_loop(self, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise AgentError("HERMES_AGENT_INPUT_INVALID", "Task prompt is empty.")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        tool_count = 0
        for _turn in range(MAX_AGENT_TURNS):
            if self.cancelled.is_set():
                raise AgentCancelled()
            message = self.client.complete(messages, TOOLS, self.emitter.run_id)
            content = message.get("content")
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                if not isinstance(tool_calls, list) or len(tool_calls) > 8:
                    raise AgentError(
                        "HERMES_AGENT_INVALID_RESPONSE",
                        "Hermes Router returned invalid tool calls.",
                    )
                assistant_message = {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else None,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_message)
                for call in tool_calls:
                    tool_count += 1
                    if tool_count > MAX_TOOL_CALLS:
                        raise AgentError(
                            "HERMES_AGENT_TOOL_LIMIT", "Hermes coding runtime exceeded its tool limit."
                        )
                    result_message = self._execute_tool_call(call)
                    messages.append(result_message)
                continue
            if not isinstance(content, str) or not content.strip():
                raise AgentError(
                    "HERMES_AGENT_INVALID_RESPONSE",
                    "Hermes Router returned no final response or tool call.",
                )
            if len(content) > MAX_FINAL_CHARS:
                raise AgentError(
                    "HERMES_AGENT_RESPONSE_TOO_LARGE", "Hermes coding response exceeded its limit."
                )
            self.emitter.message(content)
            self._emit_terminal(
                "run.completed", {"summary": "Hermes coding runtime completed the task."}
            )
            return
        raise AgentError("HERMES_AGENT_TURN_LIMIT", "Hermes coding runtime exceeded its turn limit.")

    def _execute_tool_call(self, call: Any) -> dict[str, Any]:
        if not isinstance(call, dict):
            raise AgentError("HERMES_AGENT_INVALID_RESPONSE", "Tool call is invalid.")
        tool_call_id = call.get("id")
        function = call.get("function")
        if (
            not isinstance(tool_call_id, str)
            or not 1 <= len(tool_call_id) <= 200
            or not isinstance(function, dict)
        ):
            raise AgentError("HERMES_AGENT_INVALID_RESPONSE", "Tool call is invalid.")
        tool_name = function.get("name")
        if tool_name not in {
            "project_read",
            "project_search",
            "project_apply_patch",
            "command_execute",
        }:
            raise AgentError("HERMES_AGENT_TOOL_UNSUPPORTED", "Requested tool is unsupported.")
        raw_arguments = function.get("arguments", "{}")
        try:
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except json.JSONDecodeError as error:
            arguments = None
            parse_error: AgentError | None = AgentError(
                "HERMES_AGENT_TOOL_ARGUMENTS_INVALID", "Tool arguments are invalid."
            )
        else:
            parse_error = None
        if not isinstance(arguments, dict):
            parse_error = parse_error or AgentError(
                "HERMES_AGENT_TOOL_ARGUMENTS_INVALID", "Tool arguments are invalid."
            )

        self.emitter.emit(
            "tool.started", {"tool_call_id": tool_call_id, "tool_name": tool_name}
        )
        outcome: ToolOutcome | None = None
        try:
            if parse_error is not None:
                raise parse_error
            if self.cancelled.is_set():
                raise AgentCancelled()
            if tool_name == "project_read":
                outcome = self.workspace.read(arguments)
            elif tool_name == "project_search":
                outcome = self.workspace.search(arguments)
            elif tool_name == "project_apply_patch":
                outcome = self.workspace.apply_patch(arguments)
            else:
                outcome = self.commands.execute(arguments)
            success = not (
                tool_name == "command_execute" and outcome.result.get("exit_code") != 0
            )
            model_result = outcome.result
        except AgentCancelled:
            raise
        except AgentError as error:
            success = False
            model_result = {"error": {"code": error.code, "message": error.message}}

        self.emitter.emit(
            "tool.completed",
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "success": success,
            },
        )
        if outcome is not None and outcome.changed_path is not None:
            self.emitter.emit(
                "file.changed",
                {"path": outcome.changed_path, "operation": outcome.change_operation},
            )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(model_result, separators=(",", ":"), ensure_ascii=False),
        }
