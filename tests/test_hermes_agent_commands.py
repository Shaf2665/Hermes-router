import os
import sys
import threading
import time

from hermes_agent.commands import CommandExecutor
from hermes_agent.errors import AgentCancelled


def test_command_uses_structured_argv_fixed_cwd_and_scrubbed_environment(tmp_path):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "SAFE_VALUE": "visible",
        "HERMES_ROUTER_API_KEY": "router-secret",
        "OPENAI_API_KEY": "provider-secret",
        "CUSTOM_TOKEN": "another-secret",
    }
    executor = CommandExecutor(tmp_path, environment=environment)
    script = (
        "import json, os; "
        "print(json.dumps({'cwd': os.getcwd(), 'safe': os.getenv('SAFE_VALUE'), "
        "'router': os.getenv('HERMES_ROUTER_API_KEY'), 'provider': os.getenv('OPENAI_API_KEY'), "
        "'token': os.getenv('CUSTOM_TOKEN')}))"
    )

    outcome = executor.execute({"argv": [sys.executable, "-c", script]})

    assert outcome.result["exit_code"] == 0
    assert str(tmp_path) in outcome.result["stdout"]
    assert '"safe": "visible"' in outcome.result["stdout"]
    assert '"router": null' in outcome.result["stdout"]
    assert '"provider": null' in outcome.result["stdout"]
    assert '"token": null' in outcome.result["stdout"]
    assert "router-secret" not in outcome.result["stdout"]


def test_cancellation_stops_active_command_process_tree(tmp_path):
    executor = CommandExecutor(tmp_path)
    observed = []

    def run_command():
        try:
            executor.execute(
                {
                    "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 60,
                }
            )
        except Exception as error:  # captured for assertion in the parent thread
            observed.append(error)

    thread = threading.Thread(target=run_command)
    thread.start()
    deadline = time.monotonic() + 5
    while not executor.has_active_process and time.monotonic() < deadline:
        time.sleep(0.02)
    assert executor.has_active_process

    executor.cancel()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert isinstance(observed[0], AgentCancelled)

