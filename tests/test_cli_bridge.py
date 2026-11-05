"""Launcher security and actual child-process behavior, without provider charges."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import urllib.error
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/hermes-router/scripts/hermes_bridge.py"
spec = importlib.util.spec_from_file_location("hermes_bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
LIMITS = {"context_window": 128000, "max_input_tokens": 120000, "max_output_tokens": 8000}
CONFIG = {"url": "https://router.example", "model": "hermes-router", "version": 1}


@pytest.mark.parametrize("value", ["http://evil.example", "https://user:secret@router.example", "https://router.example?key=secret",
    "https://router.example/#secret", "https://router.example\n.bad", "file:///tmp/file", "http://127.0.0.1.evil.example", "https://router.example:bad"])
def test_unsafe_url_is_rejected_without_echoing_secret(value):
    with pytest.raises(ValueError) as error:
        bridge.base_url(value)
    assert "secret" not in str(error.value)


def test_configuration_stores_no_key_or_native_settings(tmp_path, monkeypatch):
    monkeypatch.setenv(bridge.KEY_ENV, "private-key")
    path = tmp_path / "bridge.json"
    bridge.save_config(path, "http://127.0.0.1:8319/v1/", "hermes-router")
    assert bridge.read_config(path)["url"] == "http://127.0.0.1:8319"
    assert "private-key" not in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_exact_session_resume_and_no_replay_or_bypass_flags():
    session = "f3b32719-f126-4fc1-abce-f00aee0316cd"
    codex = bridge.command_for("codex", CONFIG, LIMITS, session)
    assert codex[-2:] == ["resume", session]
    assert "model_providers.hermes.wire_api=\"responses\"" in codex
    assert "model_providers.hermes.requires_openai_auth=false" in codex
    assert "model_providers.hermes.stream_max_retries=0" in codex
    assert not any("bypass" in arg or "approval" in arg or "--last" in arg for arg in codex)
    assert bridge.command_for("claude", CONFIG, LIMITS, picker=True)[-1] == "--resume"
    with pytest.raises(ValueError):
        bridge.command_for("codex", CONFIG, LIMITS, "--dangerously-bypass-approvals-and-sandbox")


def test_claude_overrides_are_child_only_and_do_not_contain_native_tokens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "native-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "native-oauth-token")
    env, settings = bridge.child_environment("claude", CONFIG, LIMITS, "application-key")
    assert env["ANTHROPIC_BASE_URL"] == "https://router.example/claude"
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "application-key"
    assert "native-oauth-token" not in json.dumps(settings)
    assert os.environ["ANTHROPIC_API_KEY"] == "native-api-key"


def test_private_settings_are_removed_and_key_never_enters_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.shutil, "which", lambda _: sys.executable)
    observed = {}
    def run(args, env):
        assert "private-key" not in json.dumps(args)
        path = Path(args[-1])
        observed["path"] = path
        assert json.loads(path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "private-key"
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        return 7
    monkeypatch.setattr(bridge.subprocess, "call", run)
    env, settings = bridge.child_environment("claude", CONFIG, LIMITS, "private-key")
    assert bridge.launch(["claude"], env, settings) == 7
    assert not observed["path"].exists()


def test_preflight_checks_scoped_claude_models_and_never_sends_inference(monkeypatch):
    get = Mock(side_effect=[{"version": 1, "responses": "stateless", "claude_messages": "/claude/v1/messages"}, {"model": LIMITS}])
    monkeypatch.setattr(bridge, "request_json", get)
    assert bridge.preflight(CONFIG, "key", "claude") == LIMITS
    assert get.call_args.args[0] == "https://router.example/claude/v1/client-config"


def test_dry_run_redacts_key_and_does_not_launch(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bridge.json"
    bridge.save_config(path, CONFIG["url"], CONFIG["model"])
    monkeypatch.setenv(bridge.KEY_ENV, "SECRET-KEY")
    monkeypatch.setattr(bridge, "preflight", lambda *_: LIMITS)
    launch = Mock()
    monkeypatch.setattr(bridge, "launch", launch)
    assert bridge.main(["--config", str(path), "claude", "--pick-session", "--dry-run"]) == 0
    assert "SECRET-KEY" not in capsys.readouterr().out
    launch.assert_not_called()


def test_redirect_handler_does_not_forward_credentials():
    assert bridge.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example") is None


def test_plugin_manifests_and_marketplace_paths_are_self_contained():
    plugin = ROOT / "plugins/hermes-router"
    for folder in (".claude-plugin", ".codex-plugin"):
        manifest = json.loads((plugin / folder / "plugin.json").read_text())
        assert manifest["version"] == bridge.VERSION and manifest["name"] == "hermes-router"
    assert (plugin / "skills/connect/SKILL.md").is_file()
    for catalog in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
        source = json.loads((ROOT / catalog).read_text())["plugins"][0]["source"]
        source = source if isinstance(source, str) else source["path"]
        assert (ROOT / source).resolve() == plugin.resolve()


def test_windows_batch_wrapper_never_receives_untrusted_arguments(tmp_path, monkeypatch):
    wrapper = tmp_path / "codex.cmd"
    script = tmp_path / "node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// test fixture")
    monkeypatch.setattr(bridge.shutil, "which", lambda name: str(wrapper) if name == "codex" else "node.exe")
    call = Mock(return_value=0)
    monkeypatch.setattr(bridge.subprocess, "call", call)
    bridge.launch(["codex", "https://router.example/path&calc"], {})
    assert call.call_args.args[0] == ["node.exe", str(script), "https://router.example/path&calc"]
    script.unlink()
    with pytest.raises(ValueError, match="safely launch"):
        bridge.launch(["codex", "anything"], {})
