"""Exercise setup's process ownership without launching routers or real services."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup_env(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "venv/bin").mkdir(parents=True)
    for name in ("setup.sh", "service.sh"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # A controlled PATH keeps these tests independent of host systemd/sudo.
    for name in ("bash", "dirname", "sed", "head", "seq", "mkdir", "cat"):
        (bin_dir / name).symlink_to(shutil.which(name))

    def executable(path, body):
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    executable(repo / "venv/bin/python", 'if [ "$1" = "-" ]; then echo 1; fi\n')
    executable(bin_dir / "sleep", "/bin/sleep 0.01\n")
    executable(bin_dir / "id", 'if [ "$1" = "-u" ]; then echo "${TEST_UID:-0}"; else echo test; fi\n')
    executable(bin_dir / "loginctl", "exit 0\n")
    executable(bin_dir / "tee", 'cat > "$TEST_UNIT"\n')
    executable(bin_dir / "curl", '[ -f "$TEST_HEALTHY" ]\n')
    executable(bin_dir / "nohup", '''echo "manual:$PORT" >> "$TEST_EVENTS"
: > "$TEST_HEALTHY"
''')
    executable(bin_dir / "systemctl", '''echo "systemctl:$*" >> "$TEST_EVENTS"
scope=system
if [ "${1:-}" = "--user" ]; then scope=user; shift; fi
case "$1" in
  cat) [ "$scope" = "${TEST_SCOPE:-}" ] ;;
  start|restart)
    [ "${TEST_START_FAIL:-0}" = 0 ] || exit 1
    : > "$TEST_HEALTHY" ;;
  *) exit 0 ;;
esac
''')
    env = os.environ.copy()
    env.update({
        "PATH": str(bin_dir), "HOME": str(tmp_path), "PORT": "18319",
        "HERMES_ROUTER_SERVICE": "hermes-setup-test", "TEST_SCOPE": "",
        "TEST_EVENTS": str(tmp_path / "events"), "TEST_UNIT": str(tmp_path / "unit"),
        "TEST_HEALTHY": str(tmp_path / "healthy"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    })
    (tmp_path / "events").touch()

    def run(answers="n\ny\n", *, service=False):
        result = subprocess.run(
            [shutil.which("bash"), str(repo / "scripts" / ("service.sh" if service else "setup.sh")),
             *(["install"] if service else [])],
            input=answers, text=True, capture_output=True, env=env, timeout=10,
        )
        events = (tmp_path / "events").read_text().splitlines()
        return result, events

    return repo, bin_dir, env, run


def test_service_selected_before_any_manual_launch(setup_env):
    repo, _, env, run = setup_env
    result, events = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemctl:restart hermes-setup-test.service" in events
    assert not any(event.startswith("manual:") for event in events)
    assert not (repo / "router.pid").exists()
    assert 'Environment="PORT=18319"' in Path(env["TEST_UNIT"]).read_text()


def test_declining_boot_service_starts_only_manual_router(setup_env):
    repo, _, _, run = setup_env
    result, events = run("n\nn\ny\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert events.count("manual:18319") == 1
    assert not any(":restart " in event for event in events)
    assert (repo / "router.pid").exists()


def test_declining_both_leaves_router_stopped(setup_env):
    _, _, _, run = setup_env
    result, events = run("n\nn\nn\n")
    assert result.returncode == 0
    assert not any(event.startswith("manual:") or ":restart " in event for event in events)


def test_without_systemd_starts_manual_router(setup_env):
    _, bin_dir, _, run = setup_env
    (bin_dir / "systemctl").unlink()
    result, events = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == ["manual:18319"]


@pytest.mark.parametrize("scope", ["system", "user"])
def test_existing_stopped_service_is_reused(setup_env, scope):
    repo, _, env, run = setup_env
    env["TEST_SCOPE"] = scope
    result, events = run()
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = "--user " if scope == "user" else ""
    assert f"systemctl:{prefix}start hermes-setup-test.service" in events
    assert not any(event.startswith("manual:") or ":restart " in event for event in events)
    assert not (repo / "router.pid").exists()


def test_existing_manual_router_is_not_converted_while_running(setup_env):
    _, _, env, run = setup_env
    Path(env["TEST_HEALTHY"]).touch()
    result, events = run()
    assert result.returncode == 0
    assert "stop the running router" in result.stdout
    assert not any(event.startswith("manual:") or ":restart " in event for event in events)


@pytest.mark.parametrize("scope", ["", "system", "user"])
def test_service_failure_never_falls_back_to_manual(setup_env, scope):
    repo, _, env, run = setup_env
    env.update(TEST_SCOPE=scope, TEST_START_FAIL="1")
    result, events = run()
    assert result.returncode == 1
    assert not any(event.startswith("manual:") for event in events)
    assert not (repo / "router.pid").exists()


def test_successful_system_service_install_returns_success(setup_env):
    _, _, _, run = setup_env
    result, events = run(service=True)
    assert "installed + started" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemctl:restart hermes-setup-test.service" in events


def test_existing_system_service_uses_sudo_for_non_root(setup_env):
    _, bin_dir, env, run = setup_env
    env.update(TEST_SCOPE="system", TEST_UID="1000")
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/bash\necho "sudo:$*" >> "$TEST_EVENTS"\nexec "$@"\n')
    sudo.chmod(0o755)
    result, events = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sudo:systemctl start hermes-setup-test.service" in events
    assert not any(event.startswith("manual:") for event in events)


def test_non_root_service_install_without_sudo_uses_user_unit(setup_env):
    _, _, env, run = setup_env
    env["TEST_UID"] = "1000"
    result, events = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemctl:--user restart hermes-setup-test.service" in events
    assert not any(event.startswith("manual:") for event in events)
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd/user/hermes-setup-test.service"
    assert 'Environment="PORT=18319"' in unit.read_text()
