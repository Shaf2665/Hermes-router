"""Worktree-contained file tools for the Hermes coding runtime."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentError


MAX_FILE_BYTES = 512_000
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_OUTPUT_CHARS = 20_000


@dataclass(frozen=True)
class ToolOutcome:
    result: dict[str, Any]
    changed_path: str | None = None
    change_operation: str | None = None


class Workspace:
    def __init__(self, root: str | Path):
        path = Path(root)
        try:
            self.root = path.resolve(strict=True)
        except OSError as error:
            raise AgentError("WORKSPACE_INVALID", "The Hall-provided worktree is unavailable.") from error
        if not self.root.is_dir():
            raise AgentError("WORKSPACE_INVALID", "The Hall-provided worktree is unavailable.")

    @staticmethod
    def _reject_git(parts: tuple[str, ...]) -> None:
        if any(part.casefold() == ".git" for part in parts):
            raise AgentError("WORKSPACE_GIT_PATH_REJECTED", "Access to .git is not allowed.")

    def resolve(self, raw_path: object, *, for_write: bool = False) -> tuple[Path, str]:
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise AgentError("WORKSPACE_PATH_INVALID", "Project path is invalid.")
        relative = Path(raw_path)
        if relative.is_absolute():
            raise AgentError("WORKSPACE_PATH_OUTSIDE", "Project path must remain inside the worktree.")
        if ".." in relative.parts:
            raise AgentError("WORKSPACE_PATH_OUTSIDE", "Project path must remain inside the worktree.")
        self._reject_git(relative.parts)
        candidate = self.root.joinpath(relative)
        try:
            if for_write and not candidate.exists():
                resolved_parent = candidate.parent.resolve(strict=True)
                resolved = resolved_parent / candidate.name
            else:
                resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise AgentError("WORKSPACE_PATH_MISSING", "Project path does not exist.") from error
        try:
            common = Path(os.path.commonpath((str(self.root), str(resolved))))
        except ValueError as error:
            raise AgentError("WORKSPACE_PATH_OUTSIDE", "Project path must remain inside the worktree.") from error
        if common != self.root:
            raise AgentError("WORKSPACE_PATH_OUTSIDE", "Project path must remain inside the worktree.")
        relative_name = resolved.relative_to(self.root).as_posix()
        self._reject_git(Path(relative_name).parts)
        return resolved, relative_name or "."

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise AgentError("PROJECT_READ_FAILED", "Project file could not be read.") from error
        if len(data) > MAX_FILE_BYTES:
            raise AgentError("PROJECT_FILE_TOO_LARGE", "Project file exceeds the runtime size limit.")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AgentError("PROJECT_FILE_NOT_TEXT", "Project file is not UTF-8 text.") from error

    def read(self, arguments: dict[str, Any]) -> ToolOutcome:
        path, relative = self.resolve(arguments.get("path"))
        if not path.is_file():
            raise AgentError("PROJECT_READ_FAILED", "Project path is not a regular file.")
        text = self._read_text(path)
        return ToolOutcome(
            {
                "path": relative,
                "content": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )

    def search(self, arguments: dict[str, Any]) -> ToolOutcome:
        query = arguments.get("query")
        if not isinstance(query, str) or not query or len(query) > 1_000 or "\x00" in query:
            raise AgentError("PROJECT_SEARCH_INVALID", "Search query is invalid.")
        start, _ = self.resolve(arguments.get("path", "."))
        files: list[Path]
        if start.is_file():
            files = [start]
        elif start.is_dir():
            files = []
            for current, directories, names in os.walk(start, followlinks=False):
                directories[:] = [
                    name
                    for name in directories
                    if name.casefold() != ".git" and not (Path(current) / name).is_symlink()
                ]
                for name in names:
                    if name.casefold() == ".git":
                        continue
                    candidate = Path(current) / name
                    if not candidate.is_symlink() and candidate.is_file():
                        files.append(candidate)
                        if len(files) >= MAX_SEARCH_FILES:
                            break
                if len(files) >= MAX_SEARCH_FILES:
                    break
        else:
            raise AgentError("PROJECT_SEARCH_INVALID", "Search path is invalid.")

        matches: list[dict[str, Any]] = []
        output_chars = 0
        for path in files:
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                snippet = line[:500]
                entry = {
                    "path": path.relative_to(self.root).as_posix(),
                    "line": line_number,
                    "text": snippet,
                }
                encoded_chars = len(str(entry))
                if output_chars + encoded_chars > MAX_SEARCH_OUTPUT_CHARS:
                    return ToolOutcome({"matches": matches, "truncated": True})
                matches.append(entry)
                output_chars += encoded_chars
                if len(matches) >= MAX_SEARCH_MATCHES:
                    return ToolOutcome({"matches": matches, "truncated": True})
        return ToolOutcome({"matches": matches, "truncated": False})

    def apply_patch(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw_path = arguments.get("path")
        new_text = arguments.get("new_text")
        old_text = arguments.get("old_text")
        create = arguments.get("create", False)
        expected_sha256 = arguments.get("expected_sha256")
        if not isinstance(new_text, str) or len(new_text.encode("utf-8")) > MAX_FILE_BYTES:
            raise AgentError("PROJECT_PATCH_INVALID", "Patch replacement is invalid or too large.")
        if not isinstance(create, bool):
            raise AgentError("PROJECT_PATCH_INVALID", "Patch create flag is invalid.")
        path, relative = self.resolve(raw_path, for_write=True)
        existed = path.exists()
        if create:
            if existed:
                raise AgentError("PROJECT_PATCH_CONFLICT", "Create patch cannot overwrite an existing file.")
            updated = new_text
        else:
            if not existed or not path.is_file():
                raise AgentError("PROJECT_PATCH_CONFLICT", "Patch target is not an existing file.")
            current = self._read_text(path)
            if expected_sha256 is not None:
                actual_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
                if not isinstance(expected_sha256, str) or expected_sha256 != actual_hash:
                    raise AgentError("PROJECT_PATCH_CONFLICT", "Project file changed since it was read.")
            if not isinstance(old_text, str) or not old_text:
                raise AgentError("PROJECT_PATCH_INVALID", "Patch old_text must be non-empty.")
            if current.count(old_text) != 1:
                raise AgentError(
                    "PROJECT_PATCH_CONFLICT", "Patch old_text must match exactly once."
                )
            updated = current.replace(old_text, new_text, 1)
            if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
                raise AgentError("PROJECT_FILE_TOO_LARGE", "Patched file exceeds the runtime size limit.")

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix=f".{path.name}.",
                suffix=".hermes-tmp",
                dir=path.parent,
                delete=False,
            ) as temporary:
                temp_path = temporary.name
                temporary.write(updated)
                temporary.flush()
                os.fsync(temporary.fileno())
            if existed:
                os.chmod(temp_path, path.stat().st_mode)
            os.replace(temp_path, path)
            temp_path = None
        except OSError as error:
            raise AgentError("PROJECT_PATCH_FAILED", "Project patch could not be applied.") from error
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        return ToolOutcome(
            {"path": relative, "operation": "created" if create else "modified"},
            changed_path=relative,
            change_operation="created" if create else "modified",
        )
