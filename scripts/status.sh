#!/usr/bin/env bash
#
# hr status — show a live health dashboard for the running router
#
# Queries the router's /v1/status endpoint on localhost and pretty-prints each
# provider's rating, health (circuit-breaker state), key pool, and latency —
# so you can glance at how things are doing without curl + an API key.
#
# Usage:
#   hr status            Show the dashboard
#   hr status --json     Print the raw JSON (for scripts)
#
# Reads PORT and the proxy API key from .env (or override with env vars).
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
ENV_FILE="${HR_ENV_FILE:-$REPO/.env}"

err() { printf '\033[1;31m[status]\033[0m %s\n' "$*" >&2; }

# Pull a value from .env (first match), stripping quotes/whitespace.
from_env() {
  [ -f "$ENV_FILE" ] || return 1
  local line
  line="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1)" || return 1
  [ -n "$line" ] || return 1
  printf '%s' "${line#*=}" | sed 's/^[[:space:]"'"'"']*//; s/[[:space:]"'"'"']*$//'
}

PORT="${PORT:-$(from_env PORT || echo 8319)}"
# Proxy key: env override → generated/configured PROXY_API_KEYS in .env.
KEY="${PROXY_API_KEYS:-$(from_env PROXY_API_KEYS || true)}"
KEY="${KEY%%,*}"   # first key if comma-separated

JSON_ONLY=0
[ "${1:-}" = "--json" ] && JSON_ONLY=1

command -v curl >/dev/null 2>&1 || { err "curl is not installed."; exit 1; }

raw="$(curl -fsS -H "Authorization: Bearer ${KEY}" "http://localhost:${PORT}/v1/status" 2>/dev/null)"
if [ -z "$raw" ]; then
  err "couldn't reach the router on http://localhost:${PORT}"
  err "is it running? start it with:  hr start    (check:  hr status)"
  err "if it's running on another port, set PORT, or the key with PROXY_API_KEYS."
  exit 1
fi

if [ "$JSON_ONLY" = "1" ]; then
  printf '%s\n' "$raw" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$raw"
  exit 0
fi

HR_STATUS_JSON="$raw" python3 - "$PORT" <<'PY'
import json, os, sys

port = sys.argv[1] if len(sys.argv) > 1 else "?"
try:
    d = json.loads(os.environ.get("HR_STATUS_JSON", ""))
except Exception:
    print("could not parse router response"); sys.exit(1)

providers = d.get("providers", {})
RST="\033[0m"; BOLD="\033[1m"; GRN="\033[1;32m"; RED="\033[1;31m"; YEL="\033[1;33m"; DIM="\033[2m"

print()
print(f"  {BOLD}hermes-router{RST} {DIM}— localhost:{port}{RST}")
print()
print(f"  {BOLD}{'Provider':<15} {'Rating':<7} {'Health':<22} {'Keys':<16} {'Latency':<9}{RST}")
print(f"  {'─'*15} {'─'*7} {'─'*22} {'─'*16} {'─'*9}")

# Order providers by rating (best first), then name, for a stable readable list.
def rating_of(v): return v.get("rating", 9) if isinstance(v, dict) else 9
for name in sorted(providers, key=lambda n: (rating_of(providers[n]), n)):
    p = providers[name]
    rating = p.get("rating", "?")

    # Health column: breaker open > down > error-rate > ok
    br = p.get("breaker", {}) or {}
    avail = p.get("available", True)
    err_rate = (p.get("stats", {}) or {}).get("error_rate", 0) or 0
    if br.get("open"):
        health = f"{RED}⨂ open{RST} {DIM}(probes in {br.get('opens_in_s','?')}s){RST}"
    elif not avail:
        health = f"{YEL}⚠ unavailable{RST}"
    elif err_rate >= 0.10:
        health = f"{YEL}⚠ degraded{RST} {DIM}({int(err_rate*100)}% err){RST}"
    else:
        health = f"{GRN}✅ ok{RST}"

    # Keys column: ready vs cooling
    keys = p.get("keys", []) or []
    total = len(keys)
    cooling = sum(1 for k in keys if k.get("status") == "cooling")
    if total == 0:
        keys_col = f"{RED}no keys{RST}"
    elif cooling:
        keys_col = f"{total} {DIM}({cooling} cooling){RST}"
    else:
        keys_col = f"{GRN}{total} ready{RST}"

    lat = p.get("latency_ms")
    lat_col = f"{int(lat)}ms" if isinstance(lat, (int, float)) else "—"

    # Visible-width padding (ANSI codes don't count toward column width).
    def pad(s, w):
        import re
        vis = len(re.sub(r"\033\[[0-9;]*m", "", s))
        return s + " " * max(0, w - vis)

    print(f"  {name:<15} {str(rating):<7} {pad(health,22)} {pad(keys_col,16)} {lat_col:<9}")

# Footer: cache + breaker config, compactly.
cache = d.get("cache", {})
br_cfg = d.get("circuit_breaker", {})
rotation = d.get("rotation", {})
limits = d.get("limits", {})
feats = d.get("features", {})
total_tokens = sum(p.get("tokens", 0) for p in providers.values())
total_cost   = sum(p.get("cost_usd", 0) for p in providers.values())
print()
if rotation:
    print(f"  {DIM}rotation: {rotation.get('mode','?')}{RST}")
if cache:
    sem = cache.get("semantic", {})
    sem_str = (f" · semantic ≥{sem.get('threshold')} ({sem.get('hits',0)} hits)"
               if sem.get("enabled") else "")
    persist_str = " · persistent" if cache.get("persistent") else ""
    print(f"  {DIM}cache: {'on' if cache.get('enabled') else 'off'} · "
          f"hit-rate {cache.get('hit_rate',0)} · {cache.get('size',0)}/{cache.get('max_size','?')} entries{persist_str}{sem_str}{RST}")
if total_tokens or total_cost:
    cost_str = f" · ${total_cost:.4f} spent" if total_cost else ""
    print(f"  {DIM}tokens served: {total_tokens}{cost_str}{RST}")
on_addons = [a["name"] for a in feats.get("addons", []) if a.get("enabled")]
if on_addons:
    print(f"  {DIM}add-ons: {', '.join(on_addons)}  (hr features){RST}")
if limits.get("enabled"):
    for k in limits.get("keys", []):
        lim, use = k.get("limits", {}), k.get("usage", {})
        parts = []
        if lim.get("rpm"):            parts.append(f"{use.get('rpm_window',0)}/{lim['rpm']} rpm")
        if lim.get("req_per_day"):    parts.append(f"{use.get('req_today',0)}/{lim['req_per_day']} req/day")
        if lim.get("tokens_per_day"): parts.append(f"{use.get('tokens_today',0)}/{lim['tokens_per_day']} tok/day")
        if lim.get("cost_per_day"):   parts.append(f"${use.get('cost_today',0):.4f}/${lim['cost_per_day']:g} cost/day")
        print(f"  {DIM}limit …{k.get('key_tail','?')}: {' · '.join(parts) if parts else 'unlimited'}{RST}")
if br_cfg:
    print(f"  {DIM}breaker: trips at {int(br_cfg.get('error_rate',0)*100)}% fails over last "
          f"{br_cfg.get('window','?')} · opens {br_cfg.get('cooldown_s','?')}s{RST}")
print()
PY
