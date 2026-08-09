import hashlib
from pathlib import Path

import pytest

from hermes_agent.errors import AgentError
from hermes_agent.workspace import Workspace


def test_paths_stay_inside_worktree_and_reject_git(tmp_path):
    workspace = Workspace(tmp_path)
    outside = tmp_path.parent / "outside-hermes-agent.txt"

    with pytest.raises(AgentError) as traversal:
        workspace.read({"path": "../outside-hermes-agent.txt"})
    assert traversal.value.code == "WORKSPACE_PATH_OUTSIDE"

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("secret", encoding="utf-8")
    with pytest.raises(AgentError) as git_error:
        workspace.read({"path": ".git/config"})
    assert git_error.value.code == "WORKSPACE_GIT_PATH_REJECTED"
    assert not outside.exists()


def test_read_search_and_patch_are_bounded_and_deterministic(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\nprint(value)\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    read = workspace.read({"path": "app.py"}).result
    assert read["content"] == "value = 1\nprint(value)\n"
    assert read["sha256"] == hashlib.sha256(read["content"].encode()).hexdigest()

    search = workspace.search({"query": "print", "path": "."}).result
    assert search == {
        "matches": [{"path": "app.py", "line": 2, "text": "print(value)"}],
        "truncated": False,
    }

    changed = workspace.apply_patch(
        {
            "path": "app.py",
            "old_text": "value = 1",
            "new_text": "value = 2",
            "expected_sha256": read["sha256"],
        }
    )
    assert changed.changed_path == "app.py"
    assert changed.change_operation == "modified"
    assert source.read_text(encoding="utf-8") == "value = 2\nprint(value)\n"

    created = workspace.apply_patch(
        {"path": "new.py", "new_text": "created = True\n", "create": True}
    )
    assert created.change_operation == "created"
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "created = True\n"


def test_patch_rejects_ambiguous_or_stale_changes(tmp_path):
    path = tmp_path / "repeat.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    with pytest.raises(AgentError) as ambiguous:
        workspace.apply_patch(
            {"path": "repeat.txt", "old_text": "same", "new_text": "different"}
        )
    assert ambiguous.value.code == "PROJECT_PATCH_CONFLICT"

    with pytest.raises(AgentError) as stale:
        workspace.apply_patch(
            {
                "path": "repeat.txt",
                "old_text": "same\n",
                "new_text": "different\n",
                "expected_sha256": "0" * 64,
            }
        )
    assert stale.value.code == "PROJECT_PATCH_CONFLICT"

