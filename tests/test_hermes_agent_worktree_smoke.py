import json
import subprocess
import sys

import pytest

from hermes_agent.errors import AgentError
from hermes_agent.protocol import EventEmitter
from hermes_agent.runtime import AgentRuntime
from hermes_agent.workspace import Workspace


class WorktreeSmokeClient:
    def __init__(self):
        self.responses = [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "patch",
                        "type": "function",
                        "function": {
                            "name": "project_apply_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "value.py",
                                    "old_text": "value = 1",
                                    "new_text": "value = 2",
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "verify",
                        "type": "function",
                        "function": {
                            "name": "command_execute",
                            "arguments": json.dumps(
                                {"argv": [sys.executable, "-m", "py_compile", "value.py"]}
                            ),
                        },
                    }
                ],
            },
            {"content": "Updated and verified value.py."},
        ]

    def complete(self, _messages, _tools, _run_id):
        return self.responses.pop(0)


def test_real_disposable_git_worktree_smoke(tmp_path):
    repository = tmp_path / "repository"
    worktree = tmp_path / "agent-worktree"
    repository.mkdir()

    def git(*args, cwd=repository):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            check=True,
        )

    git("init")
    git("config", "user.email", "hermes-smoke@example.invalid")
    git("config", "user.name", "Hermes Smoke")
    (repository / "value.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "value.py")
    git("commit", "-m", "fixture")
    git("worktree", "add", "--detach", str(worktree), "HEAD")

    workspace = Workspace(worktree)
    with pytest.raises(AgentError) as git_file:
        workspace.read({"path": ".git"})
    assert git_file.value.code == "WORKSPACE_GIT_PATH_REJECTED"

    events = []
    runtime = AgentRuntime(
        WorktreeSmokeClient(), worktree, EventEmitter("worktree-smoke", events.append)
    )

    assert runtime.run("Update and verify the value") == "completed"
    assert [event["type"] for event in events] == [
        "run.started",
        "tool.started",
        "tool.completed",
        "file.changed",
        "tool.started",
        "tool.completed",
        "message.delta",
        "run.completed",
    ]
    assert events[5]["payload"]["success"] is True
    assert (worktree / "value.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (repository / "value.py").read_text(encoding="utf-8") == "value = 1\n"
    assert workspace.search({"query": "gitdir:"}).result["matches"] == []
