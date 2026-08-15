"""Hall → hermes_agent runtime: materialized-attachment manifest compatibility.

Covers the same seam `test_hermes_task_intent.py` covers for `task_intent` —
Hall's isolated worktree already materializes attachments under
`.hall-attachments/<id>/<filename>` and sends their manifest additively on
the same stdin JSON object `run_id`/`prompt`/`task_intent` already use. This
file proves the runtime parses that manifest defensively, turns it into
bounded "Attached files" prompt context using only the already-materialized
relative paths, and never fails or changes behavior for a payload that omits
it entirely.
"""

import io
import json

from hermes_agent import __main__ as agent_main
from hermes_agent.__main__ import (
    build_prompt_with_attachments,
    read_task_input,
)


def _stdin(monkeypatch, document: dict) -> None:
    buffer = io.BytesIO(json.dumps(document).encode("utf-8"))
    monkeypatch.setattr(agent_main.sys, "stdin", type("Stdin", (), {"buffer": buffer})())


def _attachment(**overrides) -> dict:
    entry = {
        "relative_path": ".hall-attachments/11111111-1111-4111-8111-111111111111/spec.txt",
        "filename": "spec.txt",
        "mime_type": "text/plain",
        "kind": "file",
    }
    entry.update(overrides)
    return entry


# ── read_task_input: backward compatibility ───────────────────────────────────

def test_old_style_payload_with_no_attachments_key_is_unchanged(monkeypatch):
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-1", "task_intent": "coding"})

    assert read_task_input() == ("task", "hall-run-1", "coding", [])


def test_old_style_payload_with_no_task_intent_or_attachments_is_unchanged(monkeypatch):
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-2"})

    assert read_task_input() == ("task", "hall-run-2", None, [])


# ── read_task_input: attachment parsing ────────────────────────────────────────

def test_one_normal_attachment_is_parsed(monkeypatch):
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-3", "attachments": [_attachment()]})

    _, _, _, attachments = read_task_input()

    assert attachments == [_attachment()]


def test_multiple_attachments_are_parsed_in_order(monkeypatch):
    a = _attachment(relative_path=".hall-attachments/aaa/a.txt", filename="a.txt")
    b = _attachment(
        relative_path=".hall-attachments/bbb/b.png",
        filename="b.png",
        mime_type="image/png",
        kind="image",
    )
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-4", "attachments": [a, b]})

    _, _, _, attachments = read_task_input()

    assert attachments == [a, b]


def test_task_intent_propagates_unchanged_alongside_an_attachments_manifest(monkeypatch):
    _stdin(
        monkeypatch,
        {
            "prompt": "task",
            "run_id": "hall-run-5",
            "task_intent": "coding",
            "attachments": [_attachment()],
        },
    )

    prompt, run_id, task_intent, attachments = read_task_input()

    assert (prompt, run_id, task_intent) == ("task", "hall-run-5", "coding")
    assert len(attachments) == 1


# ── read_task_input: malformed manifests degrade safely ───────────────────────

def test_non_list_attachments_value_degrades_to_no_attachments(monkeypatch):
    for bogus in ("not-a-list", 7, {"a": 1}, True, None):
        _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-6", "attachments": bogus})
        assert read_task_input() == ("task", "hall-run-6", None, [])


def test_malformed_entries_are_dropped_but_valid_siblings_survive(monkeypatch):
    good = _attachment()
    malformed = [
        "not-a-dict",
        {},
        {"relative_path": "spec.txt"},  # missing filename/mime_type
        _attachment(relative_path=123),  # wrong type
        _attachment(filename=""),  # empty required field
        _attachment(mime_type="x" * 300),  # exceeds bound
        _attachment(relative_path="/etc/passwd"),  # absolute (POSIX)
        _attachment(relative_path="C:\\Windows\\win.ini"),  # absolute (drive)
        _attachment(relative_path="\\\\server\\share\\file"),  # UNC
        _attachment(relative_path="../../escape.txt"),  # traversal
        _attachment(relative_path=".hall-attachments/../../escape.txt"),  # embedded traversal
        _attachment(relative_path="bad\0path.txt"),  # NUL byte
    ]
    _stdin(
        monkeypatch,
        {"prompt": "task", "run_id": "hall-run-7", "attachments": [*malformed, good]},
    )

    _, _, _, attachments = read_task_input()

    assert attachments == [good]


def test_attachment_list_is_capped_rather_than_unbounded(monkeypatch):
    many = [
        _attachment(
            relative_path=f".hall-attachments/{'0' * 8}-1111-4111-8111-111111111111/f{i}.txt",
            filename=f"f{i}.txt",
        )
        for i in range(agent_main.MAX_ATTACHMENT_ENTRIES + 10)
    ]
    _stdin(monkeypatch, {"prompt": "task", "run_id": "hall-run-8", "attachments": many})

    _, _, _, attachments = read_task_input()

    assert len(attachments) == agent_main.MAX_ATTACHMENT_ENTRIES


# ── prompt construction ─────────────────────────────────────────────────────────

def test_build_prompt_with_attachments_is_unchanged_for_no_attachments():
    assert build_prompt_with_attachments("Fix the bug.", []) == "Fix the bug."


def test_build_prompt_with_attachments_lists_relative_paths_only():
    prompt = build_prompt_with_attachments(
        "Fix the bug.",
        [_attachment(), _attachment(relative_path=".hall-attachments/bbb/b.png", filename="b.png")],
    )

    assert prompt.startswith("Fix the bug.")
    assert ".hall-attachments/11111111-1111-4111-8111-111111111111/spec.txt" in prompt
    assert ".hall-attachments/bbb/b.png" in prompt
    assert "spec.txt" in prompt
    # Never an absolute host path — only the already-materialized relative path.
    assert "C:\\" not in prompt
    assert "/home/" not in prompt
    assert "/etc/" not in prompt


def test_image_kind_attachments_are_listed_identically_to_file_kind_no_vision_handling():
    # Requirement: an image-kind entry is ordinary file-path context for now
    # — no different formatting, no multimodal content, no vision routing.
    file_prompt = build_prompt_with_attachments("Task.", [_attachment(kind="file")])
    image_prompt = build_prompt_with_attachments(
        "Task.", [_attachment(kind="image", mime_type="image/png")]
    )

    file_line = file_prompt.split("\n")[-1]
    image_line = image_prompt.split("\n")[-1]
    # Same template shape (relative_path, filename, mime_type) — "kind" never
    # appears in the rendered text at all, for either kind.
    assert file_line == "- .hall-attachments/11111111-1111-4111-8111-111111111111/spec.txt (spec.txt, text/plain)"
    assert image_line == "- .hall-attachments/11111111-1111-4111-8111-111111111111/spec.txt (spec.txt, image/png)"
    # The MIME type itself is free-text data ("image/png" legitimately
    # contains "image"); what must never appear is vision-specific verbiage
    # this runtime would add if it started treating the kind specially.
    assert "vision" not in image_prompt.lower()
    assert "multimodal" not in image_prompt.lower()


def test_no_vision_task_intent_or_multimodal_content_is_introduced():
    # The attachments manifest never touches task_intent's own vocabulary or
    # the messages the model receives — this stays file-path context only.
    assert "attachments" not in agent_main.TASK_INTENTS
    prompt = build_prompt_with_attachments(
        "Task.", [_attachment(kind="image", mime_type="image/png")]
    )
    assert isinstance(prompt, str)  # never a multimodal content-parts list


# ── run_command: the prompt AgentRuntime actually receives ────────────────────

def test_attachment_paths_appear_in_the_prompt_agentruntime_receives(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    _stdin(
        monkeypatch,
        {
            "prompt": "Fix the bug.",
            "run_id": "hall-run-9",
            "attachments": [_attachment()],
        },
    )
    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt):
            captured["prompt"] = prompt
            return "completed"

        def cancel(self):
            pass

    monkeypatch.setattr(agent_main, "AgentRuntime", _FakeRuntime)

    exit_code = agent_main.run_command()

    assert exit_code == 0
    assert captured["prompt"].startswith("Fix the bug.")
    assert ".hall-attachments/11111111-1111-4111-8111-111111111111/spec.txt" in captured["prompt"]
    assert "spec.txt" in captured["prompt"]


def test_run_command_prompt_is_byte_identical_with_no_attachments(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    _stdin(monkeypatch, {"prompt": "Fix the bug.", "run_id": "hall-run-10"})
    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt):
            captured["prompt"] = prompt
            return "completed"

        def cancel(self):
            pass

    monkeypatch.setattr(agent_main, "AgentRuntime", _FakeRuntime)

    agent_main.run_command()

    assert captured["prompt"] == "Fix the bug."
