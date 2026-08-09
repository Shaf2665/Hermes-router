"""Structured, bounded command execution for the local runtime."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .errors import AgentCancelled, AgentError
from .workspace import ToolOutcome


DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600
MAX_COMMAND_OUTPUT_BYTES = 32_000
MAX_ARGV_ITEMS = 64
MAX_ARG_CHARS = 4_096

_SENSITIVE_ENV_FRAGMENTS = (
    "API_KEY",
    "API_KEYS",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "PROXY_API_KEYS",
    "SECRET",
    "TOKEN",
)
_SENSITIVE_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CODEX_",
    "GEMINI_",
    "GOOGLE_",
    "HERMES_ROUTER_",
    "OPENAI_",
    "OPENROUTER_",
)


def scrub_environment(environment: Mapping[str, str]) -> dict[str, str]:
    scrubbed: dict[str, str] = {}
    for key, value in environment.items():
        upper = key.upper()
        if upper == "PYTHONPATH":
            continue
        if upper.startswith(_SENSITIVE_ENV_PREFIXES):
            continue
        if any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS):
            continue
        scrubbed[key] = value
    return scrubbed


class CommandExecutor:
    def __init__(
        self,
        worktree: Path,
        environment: Mapping[str, str] | None = None,
        cancelled: threading.Event | None = None,
    ):
        self.worktree = worktree
        self.environment = scrub_environment(os.environ if environment is None else environment)
        self.cancelled = cancelled or threading.Event()
        self._lock = threading.Lock()
        self._active: subprocess.Popen[bytes] | None = None

    @property
    def has_active_process(self) -> bool:
        with self._lock:
            return self._active is not None and self._active.poll() is None

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            process = self._active
        if process is not None:
            self._terminate_tree(process)

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=5,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _bounded_reader(pipe: Any, output: bytearray) -> None:
        while True:
            chunk = pipe.read(4_096)
            if not chunk:
                return
            remaining = MAX_COMMAND_OUTPUT_BYTES - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])

    def execute(self, arguments: dict[str, Any]) -> ToolOutcome:
        argv = arguments.get("argv")
        timeout_value = arguments.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT)
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > MAX_ARGV_ITEMS
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > MAX_ARG_CHARS
                or "\x00" in item
                for item in argv
            )
        ):
            raise AgentError("COMMAND_INVALID", "Command argv is invalid.")
        if isinstance(timeout_value, bool):
            raise AgentError("COMMAND_INVALID", "Command timeout is invalid.")
        try:
            timeout_seconds = int(timeout_value)
        except (TypeError, ValueError) as error:
            raise AgentError("COMMAND_INVALID", "Command timeout is invalid.") from error
        if not 1 <= timeout_seconds <= MAX_COMMAND_TIMEOUT:
            raise AgentError("COMMAND_INVALID", "Command timeout is invalid.")
        if self.cancelled.is_set():
            raise AgentCancelled()

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.worktree,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except (OSError, ValueError) as error:
            raise AgentError("COMMAND_START_FAILED", "Command could not be started.") from error

        stdout = bytearray()
        stderr = bytearray()
        stdout_thread = threading.Thread(
            target=self._bounded_reader, args=(process.stdout, stdout), daemon=True
        )
        stderr_thread = threading.Thread(
            target=self._bounded_reader, args=(process.stderr, stderr), daemon=True
        )
        with self._lock:
            self._active = process
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        try:
            while process.poll() is None:
                if self.cancelled.is_set():
                    self._terminate_tree(process)
                    raise AgentCancelled()
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_tree(process)
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                self._terminate_tree(process)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            with self._lock:
                self._active = None

        if self.cancelled.is_set():
            raise AgentCancelled()
        if timed_out:
            raise AgentError("COMMAND_TIMED_OUT", "Command exceeded its time limit.")
        stdout_text = bytes(stdout).decode("utf-8", "replace")
        stderr_text = bytes(stderr).decode("utf-8", "replace")
        return ToolOutcome(
            {
                "exit_code": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": len(stdout) >= MAX_COMMAND_OUTPUT_BYTES
                or len(stderr) >= MAX_COMMAND_OUTPUT_BYTES,
            }
        )
