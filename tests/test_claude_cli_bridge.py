"""Opt-in official Claude CLI protocol check using a loopback provider stub."""
import json
import os
import shutil
import subprocess
import threading

import pytest
from werkzeug.serving import make_server
import router
from test_cli_bridge import bridge, LIMITS

pytestmark = pytest.mark.skipif(os.environ.get("HERMES_RUN_CLAUDE_SMOKE") != "1" or not shutil.which("claude"),
                                reason="opt-in local Claude binary compatibility check")


def test_installed_claude_accepts_messages_and_resumes_history(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "request_log", router.RequestRingBuffer(50))
    received = []
    def route(payload, streaming, _namespace):
        assert router.g.claude_only and router.g.strict_client_tools
        received.append(payload)
        if not streaming:
            return ("json", {"id": "chatcmpl_test", "model": "claude-test", "choices": [
                {"message": {"role": "assistant", "content": "BRIDGE_OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        def stream():
            for event in [
                {"id": "chatcmpl_test", "model": "claude-test", "choices": [{"index": 0, "delta": {"content": "BRIDGE_OK"}, "finish_reason": None}]},
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
    env, settings = bridge.child_environment("claude", config, LIMITS, "sk-test")
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "client-state")
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))  # synthetic key only, never a user credential
    base = bridge.command_for("claude", config, LIMITS)
    try:
        first = subprocess.run([*base, "--settings", str(path), "--output-format", "json", "--max-turns", "1", "-p", "Say BRIDGE_OK without tools"],
                               env=env, cwd=tmp_path, capture_output=True, text=True, input="", timeout=60)
        if first.returncode:
            pytest.fail(first.stdout[-8000:] + "\n" + first.stderr[-3000:])
        result = json.loads(first.stdout)
        assert result["result"] == "BRIDGE_OK" and not result.get("is_error")
        session = result["session_id"]
        resume = bridge.command_for("claude", config, LIMITS, resume=session)
        second = subprocess.run([*resume, "--settings", str(path), "--output-format", "json", "--max-turns", "1", "-p", "Say it again"],
                                env=env, cwd=tmp_path, capture_output=True, text=True, input="", timeout=60)
        if second.returncode:
            pytest.fail(second.stdout[-8000:] + "\n" + second.stderr[-3000:])
        assert json.loads(second.stdout)["result"] == "BRIDGE_OK"
        assert any(any(m.get("role") == "assistant" and "BRIDGE_OK" in str(m.get("content"))
                       for m in p["messages"]) for p in received)
    finally:
        server.shutdown()
        thread.join(timeout=5)
