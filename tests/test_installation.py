"""Installer failures must not report success or replace existing settings."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def bash_path():
    if os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin/bash.exe" if git else Path("missing")
        if candidate.is_file():
            return str(candidate)
        pytest.skip("Git Bash is needed for shell installer tests on Windows")
    return shutil.which("bash") or pytest.skip("bash unavailable")


def test_remote_install_stops_when_clone_fails(tmp_path):
    script = tmp_path / "get.sh"
    shutil.copyfile(ROOT / "get.sh", script)
    result = subprocess.run(
        [bash_path(), "-c", 'git() { return 23; }; export -f git; export HERMES_ROUTER_DIR="$PWD/install"; bash "$1"',
         "bash", str(script)], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 23
    assert "Downloaded" not in result.stdout
    assert "Installation complete" not in result.stdout


def test_venv_failure_preserves_config_and_stops(tmp_path):
    script = tmp_path / "install.sh"
    shutil.copyfile(ROOT / "install.sh", script)
    settings = tmp_path / ".env"
    settings.write_text("HOST=127.0.0.1\nPORT=8999\n")
    command = '''
python3() { case "$1" in -c) echo 3.12;; --version) echo 'Python 3.12';; *) return 19;; esac; }
uv() { return 19; }
export -f python3 uv
bash "$1"
'''
    result = subprocess.run([bash_path(), "-c", command, "bash", str(script)],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 19
    assert "venv created" not in result.stdout
    assert "Installation complete" not in result.stdout
    assert settings.read_text() == "HOST=127.0.0.1\nPORT=8999\n"


def service_run(tmp_path, fail='', action='install', port='8999', exported_port=None):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    script = scripts / 'service.sh'
    shutil.copyfile(ROOT / 'scripts/service.sh', script)
    settings = tmp_path / '.env'
    settings.write_text(f'HOST=127.0.0.1\nPORT={port}\n')
    if action == 'uninstall':
        unit = tmp_path / 'config/systemd/user/hermes-router.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('existing unit')
    command = r'''
command() { if [ "$1" = '-v' ] && [ "$2" = 'sudo' ]; then return 1; fi; builtin command "$@"; }
id() { if [ "$1" = '-u' ]; then echo 1001; else echo test-user; fi; }
systemctl() { printf '%s\n' "$*" >> "$TEST_LOG"; case "$*" in *"$TEST_FAIL"*) [ -n "$TEST_FAIL" ] && return 27;; esac; return 0; }
loginctl() { [ "$TEST_FAIL" != "linger" ]; }
curl() { printf '%s\n' "$*" >> "$TEST_LOG"; [ "$TEST_FAIL" != "health" ]; }
sleep() { :; }
export -f command id systemctl loginctl curl sleep
export XDG_CONFIG_HOME="$PWD/config"
export TEST_LOG="$PWD/commands.txt"
export HR_ENV_FILE="$PWD/.env"
bash "$1" "$TEST_SERVICE_ACTION"
'''
    env = os.environ.copy()
    env.pop('PORT', None)
    if exported_port is not None:
        env['PORT'] = exported_port
    env.pop('HERMES_ROUTER_SERVICE', None)
    env['TEST_FAIL'] = fail
    env['TEST_SERVICE_ACTION'] = action
    result = subprocess.run([bash_path(), '-c', command, 'bash', str(script)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding='utf-8')
    return result, tmp_path / 'config/systemd/user/hermes-router.service', tmp_path / 'commands.txt'


@pytest.mark.parametrize('failure', ['daemon-reload', 'enable', 'restart'])
def test_service_install_propagates_systemctl_failure(tmp_path, failure):
    result, _, commands = service_run(tmp_path, failure)
    assert result.returncode != 0
    assert 'installed + started' not in result.stdout
    assert 'installed + enabled' not in result.stdout
    calls = commands.read_text()
    if failure != 'restart':
        assert '--user restart' not in calls


def test_service_unit_quotes_paths_and_preserves_selected_config(tmp_path):
    result, unit, commands = service_run(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    text = unit.read_text(encoding='utf-8')
    assert 'ExecStart="' in text
    assert '" "' in text
    assert 'Environment="PORT=' not in text  # future .env edits remain effective
    assert 'Environment="HR_ENV_FILE=' in text
    assert 'http://localhost:8999/health' in commands.read_text()
    assert (tmp_path / '.env').read_text() == 'HOST=127.0.0.1\nPORT=8999\n'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows PowerShell coverage')
@pytest.mark.parametrize('shell', ['powershell', 'pwsh'])
@pytest.mark.parametrize('failure', ['', 'venv', 'pip', 'verify'])
def test_powershell_installer_success_and_failure_preserve_config(tmp_path, shell, failure):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f'{shell} unavailable')
    shutil.copyfile(ROOT / 'install.ps1', tmp_path / 'install.ps1')
    (tmp_path / 'requirements.txt').write_text('mock-only\n')
    settings = tmp_path / '.env'
    settings.write_text('HOST=127.0.0.1\nPORT=8999\n')
    harness = tmp_path / 'verify.ps1'
    harness.write_text(r'''
$ErrorActionPreference = 'Stop'
function global:MockPython {
    [System.IO.File]::AppendAllText((Join-Path $PSScriptRoot 'python-calls.txt'), (($args | ConvertTo-Json -Compress) + "`n"))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq '-m' -and $args[1] -eq 'venv') {
        if ($env:TEST_INSTALL_FAIL -eq 'venv') { $global:LASTEXITCODE = 19; return }
        $mockScripts = Join-Path $args[2] 'Scripts'
        New-Item -ItemType Directory -Force -Path $mockScripts | Out-Null
        $mockPython = Join-Path $mockScripts 'python.exe'
        [System.IO.File]::WriteAllText($mockPython, '')
        Set-Alias -Scope Global -Name $mockPython -Value MockPython
    }
    if ($args[0] -eq '-m' -and $args[1] -eq 'pip' -and $env:TEST_INSTALL_FAIL -eq 'pip') {
        $global:LASTEXITCODE = 17
    }
    if ($args[0] -eq '-c' -and $args[1] -like 'import flask*' -and $env:TEST_INSTALL_FAIL -eq 'verify') {
        $global:LASTEXITCODE = 21
    }
}
function Get-Command {
    param([string]$Name)
    if ($Name -eq 'python') { return [pscustomobject]@{Source = { MockPython @args }} }
    Microsoft.PowerShell.Core\Get-Command $Name
}
try { & (Join-Path $PSScriptRoot 'install.ps1') }
catch { Write-Error $_; exit 1 }
''', encoding='utf-8')
    env = os.environ.copy()
    env['TEST_INSTALL_FAIL'] = failure
    result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert settings.read_text() == 'HOST=127.0.0.1\nPORT=8999\n'
    calls = (tmp_path / 'python-calls.txt').read_text()
    assert 'sys.version_info' in calls
    if failure:
        assert result.returncode != 0
        assert 'Installed. Existing configuration was preserved.' not in result.stdout
        if failure == 'venv':
            assert 'pip' not in calls
    else:
        assert result.returncode == 0, result.stderr + result.stdout
        assert 'Installed. Existing configuration was preserved.' in result.stdout
        assert 'pip' in calls and 'import flask, waitress, requests, tiktoken' in calls


def test_service_failed_health_is_not_reported_as_started(tmp_path):
    result, _, _ = service_run(tmp_path, 'health')
    assert result.returncode != 0
    assert 'installed + started' not in result.stdout
    assert '/health didn' in result.stderr


def test_service_linger_failure_reports_login_only(tmp_path):
    result, _, _ = service_run(tmp_path, 'linger')
    assert result.returncode == 0
    assert 'enabled for login' in result.stdout
    assert 'enabled on boot' not in result.stdout


def test_service_disable_failure_retains_installed_unit(tmp_path):
    result, unit, _ = service_run(tmp_path, 'disable', 'uninstall')
    assert result.returncode != 0
    assert unit.read_text() == 'existing unit'
    assert 'unit retained' in result.stderr
    assert "removed 'hermes-router'" not in result.stdout


@pytest.mark.parametrize('port', ['0', '65536', '99999999999999999999', '8319; echo bad'])
def test_service_rejects_invalid_port_before_writing_unit(tmp_path, port):
    result, unit, commands = service_run(tmp_path, port=port)
    assert result.returncode != 0
    assert 'PORT must be an integer' in result.stderr
    assert not unit.exists() and not commands.exists()


def test_service_persists_explicit_port_override(tmp_path):
    result, unit, commands = service_run(tmp_path, exported_port='9001')
    assert result.returncode == 0, result.stderr
    assert 'Environment="PORT=9001"' in unit.read_text()
    assert 'http://localhost:9001/health' in commands.read_text()


def restart_run(tmp_path, scope='', tracked=False, matching=True, owned_unit=True):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copyfile(ROOT / 'scripts/restart.sh', scripts / 'restart.sh')
    (tmp_path / '.env').write_text('PORT=8999\n')
    if tracked:
        (tmp_path / 'router.pid').write_text('123\n')
    command = r'''
systemctl() { printf 'systemctl %s\n' "$*" >> "$TEST_LOG"; case "$TEST_SCOPE:$*" in system:cat*) return 0;; user:'--user cat'*) return 0;; system:show*|user:'--user show'*) if [ "$TEST_UNIT_MATCH" = '1' ]; then echo "$PWD"; else echo /another/install; fi; return 0;; *restart*) return 0;; *) return 1;; esac; }
id() { echo 0; }
curl() { printf 'curl %s\n' "$*" >> "$TEST_LOG"; if [ "$TEST_TRACKED" = '1' ] && [ -e "$PWD/stopped" ] && [ ! -e "$PWD/launched" ]; then return 1; fi; return 0; }
pgrep() { printf 'pgrep %s\n' "$*" >> "$TEST_LOG"; echo 987; }
ps() { if [ "$4" = "comm=" ]; then echo python; return; fi; if [ "$TEST_MATCH" = '1' ]; then printf 'python %s/router.py\n' "$PWD"; else echo 'python /another/router.py'; fi; }
kill() { printf 'kill %s\n' "$*" >> "$TEST_LOG"; if [ "$1" = '-0' ]; then [ "$2" != '123' ] || [ ! -e "$PWD/stopped" ]; else touch "$PWD/stopped"; fi; }
nohup() { printf 'nohup %s\n' "$*" >> "$TEST_LOG"; touch "$PWD/launched"; }
sleep() { /usr/bin/sleep .01; }
export -f systemctl id curl pgrep ps kill nohup sleep
export TEST_LOG="$PWD/commands.txt"
export HR_ENV_FILE="$PWD/.env"
bash "$1"
'''
    env = os.environ.copy()
    env.pop('PORT', None)
    env['TEST_SCOPE'] = scope
    env['TEST_TRACKED'] = '1' if tracked else '0'
    env['TEST_MATCH'] = '1' if matching else '0'
    env['TEST_UNIT_MATCH'] = '1' if owned_unit else '0'
    result = subprocess.run([bash_path(), '-c', command, 'bash', str(scripts / 'restart.sh')],
                            cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8')
    return result, (tmp_path / 'commands.txt').read_text()


def test_restart_manages_existing_user_service(tmp_path):
    result, calls = restart_run(tmp_path, scope='user')
    assert result.returncode == 0, result.stderr
    assert 'systemctl --user restart hermes-router' in calls
    assert 'nohup ' not in calls and 'kill ' not in calls


def test_restart_does_not_kill_untracked_router_or_launch_over_occupied_port(tmp_path):
    result, calls = restart_run(tmp_path)
    assert result.returncode != 0
    assert 'kill ' not in calls and 'nohup ' not in calls
    assert 'pgrep ' not in calls


def test_restart_refuses_reused_pid_from_another_install(tmp_path):
    result, calls = restart_run(tmp_path, tracked=True, matching=False)
    assert result.returncode != 0
    assert 'kill 123' not in calls and 'nohup ' not in calls


def test_restart_stops_only_verified_tracked_process(tmp_path):
    result, calls = restart_run(tmp_path, tracked=True)
    assert result.returncode == 0, result.stderr
    assert 'kill 123' in calls
    assert 'kill 987' not in calls and 'pgrep ' not in calls
    assert 'nohup ' in calls
    assert (tmp_path / 'router.pid').read_text().strip() != '123'


def setup_run(tmp_path, existing=False, service_failure=False):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copyfile(ROOT / 'scripts/setup.sh', scripts / 'setup.sh')
    (scripts / 'service.sh').write_text('#!/usr/bin/env bash\nprintf "service-install\\n" >> "$TEST_LOG"\ntouch "$PWD/service-installed"\nexit "${TEST_SERVICE_FAILURE:-0}"\n')
    python = tmp_path / 'venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\nif [ "$1" = "-" ]; then echo 1; fi\n')
    python.chmod(0o755)
    command = r'''
systemctl() { return 1; }
curl() {
  if [ "$TEST_EXISTING" = '1' ]; then return 0; fi
  if [ -e "$PWD/service-installed" ]; then return 0; fi
  if [ -e "$PWD/stopped" ]; then return 1; fi
  if [ -e "$PWD/launched" ]; then return 0; fi
  return 1
}
nohup() { printf 'standalone-start\n' >> "$TEST_LOG"; touch "$PWD/launched"; }
kill() { printf 'kill %s\n' "$*" >> "$TEST_LOG"; if [ "$1" = '-0' ]; then [ ! -e "$PWD/stopped" ]; else touch "$PWD/stopped"; fi; }
sleep() { /usr/bin/sleep .01; }
export -f systemctl curl nohup kill sleep
export TEST_LOG="$PWD/commands.txt"
bash "$1"
'''
    env = os.environ.copy()
    env['TEST_EXISTING'] = '1' if existing else '0'
    env['TEST_SERVICE_FAILURE'] = '25' if service_failure else '0'
    result = subprocess.run([bash_path(), '-c', command, 'bash', str(scripts / 'setup.sh')],
                            input='n\n\n\n', cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8')
    log = tmp_path / 'commands.txt'
    return result, log.read_text() if log.exists() else ''


def test_setup_stops_its_child_before_installing_service(tmp_path):
    result, calls = setup_run(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert calls.index('kill ') < calls.index('service-install')
    assert not (tmp_path / 'router.pid').exists()


def test_setup_refuses_service_install_over_untracked_process(tmp_path):
    result, calls = setup_run(tmp_path, existing=True)
    assert result.returncode != 0
    assert 'service-install' not in calls and 'kill ' not in calls


def test_setup_propagates_service_install_failure(tmp_path):
    result, calls = setup_run(tmp_path, service_failure=True)
    assert result.returncode != 0
    assert 'service-install' in calls
    assert 'Installation checks finished' not in result.stdout


@pytest.mark.parametrize('scope', ['system', 'user'])
def test_restart_never_targets_service_owned_by_another_install(tmp_path, scope):
    result, calls = restart_run(tmp_path, scope=scope, owned_unit=False)
    assert result.returncode != 0
    assert 'restart hermes-router' not in calls
    assert 'nohup ' not in calls and 'kill ' not in calls
