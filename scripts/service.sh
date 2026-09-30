#!/usr/bin/env bash
#
# hr service — run hermes-router as a background service that survives reboots
#
# Installs a systemd unit that starts the router on boot and restarts it if it
# crashes. `hr restart` then manages this same unit.
#
#   • root            → system unit  /etc/systemd/system/<svc>.service
#   • non-root + sudo → system unit  (via sudo)
#   • otherwise       → user unit    ~/.config/systemd/user/<svc>.service
#                       (+ lingering, so it still starts at boot without a login)
#
# On macOS (no systemd) it prints ready-to-paste launchd instructions instead.
#
# Usage:
#   hr service install [--force]   Install + enable the boot service
#   hr service status              Show the service status
#   hr service uninstall           Disable + remove the boot service
#
# The unit name is HERMES_ROUTER_SERVICE (default: hermes-router) — the same name
# `hr restart` looks for.
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
ENV_FILE="${HR_ENV_FILE:-$REPO/.env}"
SERVICE="${HERMES_ROUTER_SERVICE:-hermes-router}"
PORT="${PORT:-8319}"

log()  { printf '\033[1;36m[service]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[service]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[service]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[service]\033[0m %s\n' "$*"; }

usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; }

# Prefer the venv python created by install.sh; fall back to system python3.
if [ -x "$REPO/venv/bin/python" ]; then PY="$REPO/venv/bin/python"; else PY="$(command -v python3 || echo python3)"; fi

# --- macOS / no-systemd guidance -------------------------------------------------
print_launchd_help() {
  local plist="$HOME/Library/LaunchAgents/com.hermes-router.${SERVICE}.plist"
  cat <<EOF
hermes-router uses systemd for boot survival, which this system doesn't have.

On macOS, use launchd. Create ${plist} with:

  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>com.hermes-router.${SERVICE}</string>
    <key>ProgramArguments</key>
      <array><string>${PY}</string><string>${REPO}/router.py</string></array>
    <key>WorkingDirectory</key><string>${REPO}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${REPO}/router.log</string>
    <key>StandardErrorPath</key><string>${REPO}/router.log</string>
  </dict></plist>

Then load it (starts now + on every login/boot):
  launchctl load -w ${plist}

To remove:  launchctl unload -w ${plist} && rm ${plist}
EOF
}

# --- unit file contents ----------------------------------------------------------
# $1 = "system" | "user"
unit_text() {
  local scope="$1" user_line="" wanted="multi-user.target"
  if [ "$scope" = "system" ]; then user_line="User=$(id -un)"; else wanted="default.target"; fi
  cat <<EOF
[Unit]
Description=hermes-router — free-tier AI load balancer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
${user_line}
WorkingDirectory=${REPO}
ExecStart=${PY} ${REPO}/router.py
Restart=always
RestartSec=5
Environment="HOME=${HOME}"
Environment="PORT=${PORT}"

[Install]
WantedBy=${wanted}
EOF
}

health_ok() {
  command -v curl >/dev/null 2>&1 || return 2
  for _ in $(seq 1 15); do
    curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

require_systemd() {
  command -v systemctl >/dev/null 2>&1 || { print_launchd_help; exit 1; }
}

# Decide install scope + the systemctl invocation for this environment.
#   sets: SCOPE, UNIT_PATH, SYSTEMCTL (array via function), and whether sudo is used.
resolve_scope() {
  if [ "$(id -u)" -eq 0 ]; then
    SCOPE="system"; SUDO=""; UNIT_PATH="/etc/systemd/system/${SERVICE}.service"
  elif command -v sudo >/dev/null 2>&1; then
    SCOPE="system"; SUDO="sudo"; UNIT_PATH="/etc/systemd/system/${SERVICE}.service"
  else
    SCOPE="user";  SUDO="";     UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${SERVICE}.service"
  fi
}

sctl() {  # run systemctl in the right scope
  if [ "$SCOPE" = "user" ]; then systemctl --user "$@"; else $SUDO systemctl "$@"; fi
}

cmd="${1:-help}"
shift 2>/dev/null || true

case "$cmd" in
  install)
    force=0; [ "${1:-}" = "--force" ] && force=1
    require_systemd
    resolve_scope
    if [ -e "$UNIT_PATH" ] && [ "$force" -ne 1 ]; then
      err "a unit already exists at ${UNIT_PATH}"
      err "refusing to overwrite it. Re-run with --force to replace, or pick another name:"
      err "  HERMES_ROUTER_SERVICE=my-name hr service install"
      exit 1
    fi
    log "installing ${SCOPE} service '${SERVICE}' → ${UNIT_PATH}"
    if [ "$SCOPE" = "user" ]; then
      mkdir -p "$(dirname "$UNIT_PATH")"
      unit_text user > "$UNIT_PATH"
      loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || warn "could not enable linger — service may not start until you log in."
    else
      unit_text system | $SUDO tee "$UNIT_PATH" >/dev/null
    fi
    sctl daemon-reload
    sctl enable "${SERVICE}.service" >/dev/null 2>&1 || true
    sctl restart "${SERVICE}.service" || { err "failed to start the service. Check: hr service status"; exit 1; }
    health_ok
    case $? in
      0) ok "installed + started '${SERVICE}', enabled on boot. (manage with: hr restart / hr service status)" ;;
      2) ok "installed + enabled on boot (install 'curl' to enable health checks)." ;;
      *) warn "service installed + enabled, but /health didn't respond yet — check: hr service status" ;;
    esac
    if [ "$SCOPE" = "user" ]; then
      log "user service: starts on boot via linger. Use 'systemctl --user' to inspect."
    fi
    ;;

  uninstall|remove)
    require_systemd
    resolve_scope
    if [ ! -e "$UNIT_PATH" ]; then warn "no unit at ${UNIT_PATH} — nothing to remove."; exit 0; fi
    log "removing service '${SERVICE}'…"
    sctl disable --now "${SERVICE}.service" >/dev/null 2>&1 || true
    if [ "$SCOPE" = "user" ]; then rm -f "$UNIT_PATH"; else $SUDO rm -f "$UNIT_PATH"; fi
    sctl daemon-reload
    ok "removed '${SERVICE}'. (the router process is stopped; run 'hr start' to run it manually)"
    ;;

  status)
    require_systemd
    resolve_scope
    if [ ! -e "$UNIT_PATH" ]; then
      warn "no boot service installed (${SERVICE}). Install one with:  hr service install"
      exit 0
    fi
    sctl is-enabled "${SERVICE}.service" 2>/dev/null | sed 's/^/  enabled: /'
    sctl --no-pager status "${SERVICE}.service" 2>&1 | head -12
    ;;

  help|--help|-h) usage ;;
  *) err "unknown subcommand: ${cmd}"; usage >&2; exit 1 ;;
esac
