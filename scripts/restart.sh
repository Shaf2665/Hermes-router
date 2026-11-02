#!/usr/bin/env bash
#
# hr restart — restart the router so config/key changes take effect
#
# Use this after `hr auth add` (or any .env change). It restarts cleanly:
#   • If a systemd service exists, it restarts that.
#   • Otherwise it finds the running router.py process, stops it, and relaunches
#     it in the background (logging to ./router.log).
# Either way it then health-checks the router and tells you the result.
#
# Usage:
#   hr restart
#
# Optional env overrides:
#   PORT=8319                      # port to health-check
#   PYTHON=python3                 # interpreter for the background fallback
#   HERMES_ROUTER_SERVICE=my-svc   # systemd unit name (default: hermes-router)
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
# Prefer the venv Python created by install.sh (where flask/deps live); fall back
# to system python3. Without this, the standalone restart path below relaunches
# with bare python3 and fails with ModuleNotFoundError: No module named 'flask'.
if [ -f "$REPO/venv/bin/python" ]; then
  PYTHON="${PYTHON:-$REPO/venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi
ENV_FILE="${HR_ENV_FILE:-$REPO/.env}"
configured_port=""
if [ -f "$ENV_FILE" ]; then configured_port="$(sed -n 's/^[[:space:]]*PORT=//p' "$ENV_FILE" | head -1)"; fi
PORT="${PORT:-${configured_port:-8319}}"
SERVICE="${HERMES_ROUTER_SERVICE:-hermes-router}"

log() { printf '\033[1;36m[restart]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[restart]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1;32m[restart]\033[0m %s\n' "$*"; }

# Wait up to ~15s for /health to come back. 0=healthy, 1=not, 2=can't check.
health_ok() {
  command -v curl >/dev/null 2>&1 || return 2
  for _ in $(seq 1 15); do
    if [ -n "${ROUTER_PID:-}" ] && ! kill -0 "$ROUTER_PID" 2>/dev/null; then return 1; fi
    curl -fsS --max-time 2 "http://localhost:${PORT}/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

report_health() {
  health_ok
  case $? in
    0) ok "router is healthy on :${PORT}.  (see:  hr status)" ;;
    2) ok "restarted (install 'curl' to enable health checks)." ;;
    *) err "router did not come back healthy on :${PORT} — check the logs."; exit 1 ;;
  esac
}

# 1) systemd path -----------------------------------------------------------------
scope=""
if command -v systemctl >/dev/null 2>&1; then
  if systemctl cat "${SERVICE}.service" >/dev/null 2>&1; then scope="system"
  elif systemctl --user cat "${SERVICE}.service" >/dev/null 2>&1; then scope="user"; fi
fi
if [ -n "$scope" ]; then
  if [ "$scope" = "user" ]; then
    unit_directory="$(systemctl --user show --property=WorkingDirectory --value "${SERVICE}.service" 2>/dev/null)"
  else
    unit_directory="$(systemctl show --property=WorkingDirectory --value "${SERVICE}.service" 2>/dev/null)"
  fi
  if [ "$unit_directory" != "$REPO" ]; then
    err "service '${SERVICE}' does not belong to this installation; choose its HERMES_ROUTER_SERVICE name"
    exit 1
  fi
  log "restarting systemd service '${SERVICE}'…"
  if [ "$scope" = "user" ]; then
    systemctl --user restart "${SERVICE}.service" || { err "systemctl --user restart failed."; exit 1; }
  elif [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    sudo systemctl restart "$SERVICE" || { err "systemctl restart failed."; exit 1; }
  else
    systemctl restart "$SERVICE" || { err "systemctl restart failed."; exit 1; }
  fi
  report_health
  exit 0
fi

# 2) standalone process path ------------------------------------------------------
# Only stop a tracked Python process whose command names this installation.
pid="$(cat "$REPO/router.pid" 2>/dev/null || true)"
if [ -n "$pid" ]; then
  if ! [[ "$pid" =~ ^[0-9]{1,10}$ ]] || [ "$pid" -le 1 ]; then
    err "invalid router.pid; inspect it before restarting"; exit 1
  fi
fi
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  executable="$(ps -p "$pid" -o comm= 2>/dev/null)"
  command_line="$(ps -p "$pid" -o args= 2>/dev/null)"
  case "${executable##*/}" in python*|pypy*) ;; *) err "tracked PID is not Python; refusing to stop it"; exit 1;; esac
  case " $command_line " in *" ${REPO}/router.py "*) ;; *) err "tracked PID belongs to another process; refusing to stop it"; exit 1;; esac
  log "stopping tracked router (pid: $pid)…"
  kill "$pid" || { err "could not stop tracked router"; exit 1; }
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then err "tracked router did not stop; inspect it before retrying"; exit 1; fi
else
  log "no live tracked router found."
fi
if curl -fsS --max-time 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  err "port $PORT has an untracked server; stop its foreground process or use its service manager"; exit 1
fi

log "launching router in the background (logging to ${REPO}/router.log)…"
nohup "$PYTHON" "$REPO/router.py" >> "$REPO/router.log" 2>&1 &
ROUTER_PID=$!
(umask 077; printf '%s\n' "$ROUTER_PID" > "$REPO/router.pid") || { err "could not save router.pid"; exit 1; }
disown 2>/dev/null || true

report_health
