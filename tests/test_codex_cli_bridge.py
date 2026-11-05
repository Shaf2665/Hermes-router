"""Opt-in wire check with the installed official Codex binary and a local stub.

HERMES_RUN_CODEX_SMOKE=1 python -m pytest -q tests/test_codex_cli_bridge.py
No provider account or paid inference is used.
"""
import json
import os
import shutil
import subprocess
import threading

import pytest
from werkzeug.serving import make_server
import router
from test_cli_bridge import bridge, LIMITS

pytestmark = pytest.mark.skipif(os.environ.get("HERMES_RUN_CODEX_SMOKE") != "1" or not shutil.which("codex"),
                                reason="opt-in local Codex binary compatibility check")


def test_installed_codex_accepts_bridge_and_resumes_saved_history(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(50))
    received = []
    rejected = []
    original_translate = router.responses_api.to_chat
    def translate(body, model):
        try:
            return original_translate(body, model)
        except Exception as error:
            rejected.append({"error": str(error), "keys": list(body or {}),
                             "tool_types": [t.get("type") for t in (body or {}).get("tools", [])]})
            raise
    monkeypatch.setattr(router.responses_api, "to_chat", translate)
    def route(payload, streaming, _namespace):
        received.append(payload)
        def stream():
            for event in [
                {"choices": [{"index": 0, "delta": {"content": "BRIDGE_OK"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            ]:
                yield ("data: " + json.dumps(event) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return ("stream", stream(), "test")
    monkeypatch.setattr(router, "_route_completion", route)
    server = make_server("127.0.0.1", 0, router.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"url": "http://127.0.0.1:" + str(server.server_port), "model": "hermes-router"}
    base = bridge.command_for("codex", config, LIMITS)
    env, _ = bridge.child_environment("codex", config, LIMITS, "sk-test")
    # Give only the test child an empty client state directory. The workspace's
    # mounted Codex installation is read-only; never touch its auth/session files.
    client_state = tmp_path / "client-state"
    client_state.mkdir()
    env["CODEX_HOME"] = str(client_state)
    try:
        first = subprocess.run([*base, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json", "Say BRIDGE_OK without tools"],
                               env=env, cwd=tmp_path, capture_output=True, text=True, input="", timeout=50)
        if "bwrap: loopback: Failed to create NETLINK_ROUTE socket" in first.stderr:
            pytest.skip("Host does not permit a nested Codex sandbox; run on native Linux CI")
        if first.returncode:
            pytest.fail(first.stdout[-8000:] + "\n" + first.stderr[-3000:] + "\n" + json.dumps(rejected))
        events = [json.loads(line) for line in first.stdout.splitlines() if line.startswith("{")]
        session = next(e["thread_id"] for e in events if e["type"] == "thread.started")
        assert "BRIDGE_OK" in first.stdout and received
        second = subprocess.run([*base, "exec", "resume", "--skip-git-repo-check", "--json", session, "Say it again"],
                                env=env, cwd=tmp_path, capture_output=True, text=True, input="", timeout=50)
        assert second.returncode == 0, (second.stdout[-3000:], second.stderr[-3000:])
        assert len(received) == 2
        assert any(m.get("role") == "assistant" and "BRIDGE_OK" in str(m.get("content")) for m in received[1]["messages"])
    finally:
        server.shutdown()
        thread.join(timeout=5)
