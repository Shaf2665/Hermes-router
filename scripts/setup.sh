#!/usr/bin/env bash
#
# hr setup — interactive first-run wizard
#
# Checks your install, walks you through adding a provider key,
# and optionally starts the router and confirms it's alive.
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }

log()  { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1;35m── Step %s: %s\033[0m\n\n' "$1" "$2"; }

ENV_FILE="${HR_ENV_FILE:-$REPO/.env}"
configured_port=""
if [ -f "$ENV_FILE" ]; then configured_port="$(sed -n 's/^[[:space:]]*PORT=//p' "$ENV_FILE" | head -1)"; fi
PORT="${PORT:-${configured_port:-8319}}"
AUTH_FILE="${ROUTER_AUTH_FILE:-$REPO/auth.json}"
VENV_PYTHON="$REPO/venv/bin/python"

echo ""
echo "  ┌──────────────────────────────────┐"
echo "  │   hermes-router  ·  setup        │"
echo "  └──────────────────────────────────┘"
echo ""

# ── Step 1: Verify install ────────────────────────────────────────────────────
step 1 "Checking installation"

if [ ! -f "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c "import flask, waitress, requests" 2>/dev/null; then
  warn "venv or dependencies missing — running install.sh first..."
  bash "$REPO/install.sh" || { err "install.sh failed. Fix errors above, then re-run: hr setup"; exit 1; }
else
  ok "Installation OK"
fi

# ── Step 2: API keys ─────────────────────────────────────────────────────────
step 2 "API keys"

total_keys=$("$VENV_PYTHON" - "$AUTH_FILE" 2>/dev/null <<'PY'
import json, sys, os
path = sys.argv[1]
try:
    doc = json.load(open(path))
    print(sum(len(v) for v in doc.get("providers", {}).values()))
except Exception:
    print(0)
PY
)

if [ "${total_keys:-0}" -gt 0 ]; then
  ok "$total_keys key(s) already configured"
  printf '\n\033[1;36m[setup]\033[0m Add keys for another provider? [y/N]: '
  read -r ans
  echo ""
  case "${ans:-n}" in
    [yY]*)
      printf '\033[1;36m[setup]\033[0m Provider name: '
      read -r provider
      echo ""
      if [ -n "$provider" ]; then
        bash "$REPO/scripts/auth.sh" add "$provider" || { err "Could not save the provider key."; exit 1; }
      fi
      ;;
  esac
else
  warn "No provider keys found in auth.json. Environment keys or a local model may already be configured."
  echo ""
  echo "  Free providers (sign up and get a key):"
  echo "    gemini       aistudio.google.com   (model/project limits)"
  echo "    openrouter   openrouter.ai          (rate-limited free models)"
  echo "    groq         console.groq.com       (model-specific free limits)"
  echo "    cerebras     cloud.cerebras.ai      (developer access)"
  echo "    sambanova    cloud.sambanova.ai     (developer access)"
  echo ""
  printf '\033[1;36m[setup]\033[0m Which provider do you have a key for? (or Enter to skip): '
  read -r provider
  echo ""
  if [ -n "$provider" ]; then
    bash "$REPO/scripts/auth.sh" add "$provider" || { err "Could not save the provider key."; exit 1; }
  else
    warn "Skipped — connect a provider in the dashboard, configure a local model, or run 'hr auth add <provider>'."
  fi
fi

# ── Step 3: Start the router ──────────────────────────────────────────────────
step 3 "Start the router"

if curl -sf --max-time 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  ok "Router is already running on port $PORT"
else
  printf '\033[1;36m[setup]\033[0m Start the router now? [Y/n]: '
  read -r ans
  echo ""
  case "${ans:-y}" in
    [nN]*)
      warn "Skipped — run 'hr start' when ready."
      ;;
    *)
      log "Starting router (logs → $REPO/router.log)..."
      PORT="$PORT" nohup "$VENV_PYTHON" "$REPO/router.py" \
        >> "$REPO/router.log" 2>&1 &
      ROUTER_PID=$!
      echo "$ROUTER_PID" > "$REPO/router.pid"

      # Wait up to 6s for the router to bind
      alive=0
      for i in 1 2 3 4 5 6; do
        sleep 1
        if kill -0 "$ROUTER_PID" 2>/dev/null && curl -sf --max-time 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
          ok "Router started on port $PORT (PID $ROUTER_PID)"
          alive=1
          break
        fi
      done
      if [ "$alive" -eq 0 ]; then
        err "Router didn't respond after 6s."
        err "Check logs:  tail -20 $REPO/router.log"
        exit 1
      fi
      ;;
  esac
fi

# ── Step 3b: Survive reboots (Linux/systemd) ──────────────────────────────────
# A plain `hr start` is a foreground/background process that does NOT come back
# after a server reboot. Offer to install a systemd service so it does.
if command -v systemctl >/dev/null 2>&1 \
   && ! systemctl cat "${HERMES_ROUTER_SERVICE:-hermes-router}.service" >/dev/null 2>&1 \
   && ! systemctl --user cat "${HERMES_ROUTER_SERVICE:-hermes-router}.service" >/dev/null 2>&1; then
  step 3b "Start on boot (recommended for servers)"
  printf '\033[1;36m[setup]\033[0m Start hermes-router automatically on boot? [Y/n]: '
  read -r ans
  echo ""
  case "${ans:-y}" in
    [nN]*) warn "Skipped — your router will NOT restart after a reboot. Run 'hr service install' anytime." ;;
    *)
      if [ -n "${ROUTER_PID:-}" ]; then
        if kill -0 "$ROUTER_PID" 2>/dev/null; then
          kill "$ROUTER_PID" || { err "Could not stop the router started by setup; service installation cancelled."; exit 1; }
          for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$ROUTER_PID" 2>/dev/null || break
            sleep 1
          done
          if kill -0 "$ROUTER_PID" 2>/dev/null; then err "Router did not stop; service installation cancelled."; exit 1; fi
        fi
        wait "$ROUTER_PID" 2>/dev/null || true
        rm -f "$REPO/router.pid"
      fi
      if curl -sf --max-time 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
        err "An existing process still uses port $PORT. Stop it before running 'hr service install'."
        exit 1
      fi
      bash "$REPO/scripts/service.sh" install || { err "Could not install the boot service — see the message above."; exit 1; }
      ;;
  esac
fi

# ── Step 4: Verify ────────────────────────────────────────────────────────────
step 4 "Verifying"

if curl -sf --max-time 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  ok "Router is alive at http://localhost:${PORT}"
  echo ""
  echo "  Quick check:  curl http://localhost:${PORT}/health"
  echo "  Live status:  hr status"
  echo ""
  echo "  Open http://localhost:${PORT}/dashboard and sign in with ADMIN_API_KEY from .env."
  echo "  Use Connect to send a test message and create an application key for your client."
  echo "  Administrator credentials stay separate from your application's key."
  echo "  Local and server instructions: documentation/setup.md"
  echo ""
else
  warn "Router isn't responding on port $PORT."
  echo "  Start it with:  hr start"
fi

echo ""
ok "Installation checks finished. Inference is verified only after a successful test message."
echo ""
