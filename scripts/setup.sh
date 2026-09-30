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

PORT="${PORT:-8319}"
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
      [ -n "$provider" ] && bash "$REPO/scripts/auth.sh" add "$provider" || true
      ;;
  esac
else
  warn "No API keys found — you need at least one to get LLM responses."
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
    bash "$REPO/scripts/auth.sh" add "$provider" || true
  else
    warn "Skipped — run 'hr auth add <provider>' before starting the router."
  fi
fi

# ── Step 3: Choose one process owner before starting the router ───────────────
step 3 "Start the router"

SERVICE="${HERMES_ROUTER_SERVICE:-hermes-router}"
service_scope=""
if command -v systemctl >/dev/null 2>&1; then
  if systemctl cat "${SERVICE}.service" >/dev/null 2>&1; then
    service_scope="system"
  elif systemctl --user cat "${SERVICE}.service" >/dev/null 2>&1; then
    service_scope="user"
  fi
fi

if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  ok "Router is already running on port $PORT"
  if [ -z "$service_scope" ] && command -v systemctl >/dev/null 2>&1; then
    # Do not put a second, service-managed process behind a live manual router.
    warn "Boot service not installed: stop the running router before running 'hr service install'."
  fi
else
  # Installing the boot service starts the router too. Never launch nohup first,
  # or fall back to it after a partial service install (the unit may be retrying).
  if [ -z "$service_scope" ] && command -v systemctl >/dev/null 2>&1; then
    printf '\033[1;36m[setup]\033[0m Install the boot service and start the router now? [Y/n]: '
    read -r ans
    echo ""
    case "${ans:-y}" in
      [nN]*) warn "Skipped boot service — your router will NOT restart after a reboot." ;;
      *)
        PORT="$PORT" bash "$REPO/scripts/service.sh" install || {
          err "Could not install/start the boot service. Check 'hr service status' before retrying."
          exit 1
        }
        service_scope="installed"
        ;;
    esac
  fi

  if [ "$service_scope" != "installed" ]; then
    printf '\033[1;36m[setup]\033[0m Start the router now? [Y/n]: '
    read -r ans
    echo ""
    case "${ans:-y}" in
      [nN]*) warn "Skipped — run 'hr start' (or start your installed service) when ready." ;;
      *)
        if [ -n "$service_scope" ]; then
          log "Starting existing $service_scope service '$SERVICE'..."
          if [ "$service_scope" = "user" ]; then
            systemctl --user start "${SERVICE}.service"
          elif [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            sudo systemctl start "${SERVICE}.service"
          else
            systemctl start "${SERVICE}.service"
          fi
          if [ "$?" -ne 0 ]; then
            err "Could not start the service. Check: hr service status"
            exit 1
          fi
        else
          log "Starting router (logs → $REPO/router.log)..."
          PORT="$PORT" nohup "$VENV_PYTHON" "$REPO/router.py" \
            >> "$REPO/router.log" 2>&1 &
          ROUTER_PID=$!
          echo "$ROUTER_PID" > "$REPO/router.pid"
        fi

        # Wait up to 6s for the selected owner to start the router.
        alive=0
        for i in 1 2 3 4 5 6; do
          sleep 1
          if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
            ok "Router started on port $PORT"
            alive=1
            break
          fi
        done
        if [ "$alive" -eq 0 ]; then
          err "Router didn't respond after 6s."
          err "Check logs: tail -20 $REPO/router.log (or 'hr service status' for a service)."
          exit 1
        fi
        ;;
    esac
  fi
fi

# ── Step 4: Verify ────────────────────────────────────────────────────────────
step 4 "Verifying"

if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  ok "Router is alive at http://localhost:${PORT}"
  echo ""
  echo "  Quick check:  curl http://localhost:${PORT}/health"
  echo "  Live status:  hr status"
  echo ""
  echo "  Connect your app (Python):"
  echo "    from openai import OpenAI"
  router_key="$(sed -n 's/^PROXY_API_KEYS=//p' "$REPO/.env" 2>/dev/null | head -1)"
  router_key="${router_key%%,*}"
  echo "    client = OpenAI(base_url='http://localhost:${PORT}/v1', api_key='${router_key:-YOUR_ROUTER_KEY}')"
  echo ""
else
  warn "Router isn't responding on port $PORT."
  echo "  Start it with:  hr start"
fi

echo ""
ok "Setup complete!"
echo ""
