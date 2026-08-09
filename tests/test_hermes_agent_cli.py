import json
import os
from pathlib import Path
import subprocess
import sys

import hermes_agent.__main__ as cli


def test_detect_accepts_confirmed_multi_model_capability(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda _config, _method, path, **_kwargs: (
            {"data": [{"id": "hermes-router"}]}
            if path == "/models"
            else {
                "providers": {
                    "test": {
                        "available": True,
                        "model_caps": [
                            {"model": "plain", "supports_tools": False, "tools_confirmed": False},
                            {"model": "test", "supports_tools": True, "tools_confirmed": True}
                        ],
                    }
                }
            }
        ),
    )

    detected = cli.detect_document()
    capabilities = cli.capabilities_document()

    assert detected["available"] is True
    assert detected["protocol"] == "hermes-agent/v1"
    assert detected["runtime_version"] == "0.1.0"
    assert detected["capabilities"] == capabilities["capabilities"]
    assert detected["execution_trust"] == "trusted_local"


def test_detect_accepts_confirmed_single_model_provider(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda _config, _method, path, **_kwargs: (
            {"data": [{"id": "hermes-router"}]}
            if path == "/models"
            else {
                "providers": {
                    "test": {
                        "available": True,
                        "supports_tools": True,
                        "tools_confirmed": True,
                    }
                }
            }
        ),
    )

    assert cli.detect_document()["available"] is True


def test_detect_rejects_unconfirmed_single_model_provider(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda _config, _method, path, **_kwargs: (
            {"data": [{"id": "hermes-router"}]}
            if path == "/models"
            else {"providers": {"test": {"available": True, "supports_tools": True}}}
        ),
    )

    detected = cli.detect_document()

    assert detected["available"] is False
    assert detected["code"] == "HERMES_AGENT_TOOLS_UNAVAILABLE"


def test_detect_rejects_router_without_tool_capable_model(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda _config, _method, path, **_kwargs: (
            {"data": [{"id": "hermes-router"}]}
            if path == "/models"
            else {
                "providers": {
                    "test": {
                        "available": True,
                        "model_caps": [{"model": "test", "supports_tools": False, "tools_confirmed": False}],
                    }
                }
            }
        ),
    )

    detected = cli.detect_document()

    assert detected["available"] is False
    assert detected["code"] == "HERMES_AGENT_TOOLS_UNAVAILABLE"


def test_detect_rejects_unconfirmed_tool_support(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda _config, _method, path, **_kwargs: (
            {"data": [{"id": "hermes-router"}]}
            if path == "/models"
            else {
                "providers": {
                    "test": {
                        "available": True,
                        "model_caps": [{"model": "test", "supports_tools": True}],
                    }
                }
            }
        ),
    )

    detected = cli.detect_document()

    assert detected["available"] is False
    assert detected["code"] == "HERMES_AGENT_TOOLS_UNAVAILABLE"


def test_run_cli_emits_started_then_safe_failure_for_invalid_configuration(tmp_path):
    environment = dict(os.environ)
    environment.pop("HERMES_ROUTER_API_KEY", None)
    runner = Path(cli.__file__).resolve().parents[1] / "hermes_agent_runner.py"

    completed = subprocess.run(
        [sys.executable, str(runner), "run"],
        cwd=tmp_path,
        env=environment,
        input=json.dumps({"run_id": "cli-failure", "prompt": "Do the task."}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        timeout=10,
        check=False,
    )
    events = [json.loads(line) for line in completed.stdout.splitlines()]

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert [event["type"] for event in events] == ["run.started", "run.failed"]
    assert events[-1]["payload"]["code"] == "HERMES_ROUTER_CONFIG_INVALID"
