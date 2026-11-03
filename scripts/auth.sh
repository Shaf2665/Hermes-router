#!/usr/bin/env bash
#
# hr auth — manage API keys for hermes-router providers
#
# Usage:
#   hr auth add <provider>   Add one or more API keys for a provider
#   hr auth import-codex     Import a ChatGPT-subscription login from the Codex CLI
#   hr auth list             Show all providers and how many keys are configured
#   hr auth help             Show this help
#
# Supported providers:
#   gemini  openrouter  sambanova  github_models  cerebras
#   groq  mistral  cohere  zai  naga  nvidia  huggingface  kimi
#   opencode  opencode_go  openai  anthropic
#
# Codex (ChatGPT subscription) uses OAuth, not an API key. Log in once with the
# official Codex CLI (`codex login`), then run `hr auth import-codex` to copy the
# tokens into auth.json. The router refreshes the access token automatically.
#
# Keys are stored in auth.json next to this script (override with ROUTER_AUTH_FILE).
# This is the router's own credential store — self-contained, independent of any
# host application. The router reads auth.json first, then any .env keys as fallback.
#
#   { "providers": { "openrouter": ["key1", "key2"], "gemini": ["key"] } }
#
# Multiple keys per provider are round-robined and individually cooled on rate-limits.
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
AUTH_FILE="${ROUTER_AUTH_FILE:-$REPO/auth.json}"
PYTHON="${PYTHON:-python3}"

log()  { printf '\033[1;36m[auth]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[auth]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[auth]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[auth]\033[0m %s\n' "$*"; }

PROVIDERS_LIST="gemini openrouter sambanova github_models cerebras groq mistral cohere zai naga nvidia huggingface kimi opencode opencode_go openai anthropic"
PROVIDERS_LIST="$PROVIDERS_LIST $("$PYTHON" "$REPO/provider_catalog.py" names)"

# Normalize a provider name to its canonical form (accepts a couple of aliases).
canonical_provider() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    gemini|google)         echo "gemini" ;;
    openrouter|or)         echo "openrouter" ;;
    sambanova|samba)       echo "sambanova" ;;
    github_models|github)  echo "github_models" ;;
    cerebras)              echo "cerebras" ;;
    groq)                  echo "groq" ;;
    mistral)               echo "mistral" ;;
    cohere)                echo "cohere" ;;
    zai|glm|z.ai)          echo "zai" ;;
    naga)                  echo "naga" ;;
    nvidia|nim)            echo "nvidia" ;;
    huggingface|hf|hugging_face) echo "huggingface" ;;
    kimi|moonshot)         echo "kimi" ;;
    opencode|opencode_zen|zen) echo "opencode" ;;
    opencode_go|opencodego|opencode-go|go) echo "opencode_go" ;;
    openai|gpt)            echo "openai" ;;
    anthropic|claude)      echo "anthropic" ;;
    *) "$PYTHON" "$REPO/provider_catalog.py" canonical "$1" ;;
  esac
}

# Count keys stored for a provider in auth.json.
count_keys() {
  local provider="$1"
  "$PYTHON" - "$AUTH_FILE" "$provider" <<'PY'
import json, sys, os
path, provider = sys.argv[1], sys.argv[2]
try:
    doc = json.load(open(path))
except Exception:
    doc = {}
print(len(doc.get("providers", {}).get(provider, [])))
PY
}

# Append one key to auth.json for a provider. Prints the new total, or "DUPLICATE".
append_key() {
  local provider="$1"
  local key="$2"
  "$PYTHON" - "$AUTH_FILE" "$provider" "$key" <<'PY'
import json, sys, os
path, provider, key = sys.argv[1], sys.argv[2], sys.argv[3]
doc = {}
if os.path.exists(path):
    try:
        doc = json.load(open(path))
    except Exception:
        doc = {}
if not isinstance(doc, dict):
    doc = {}
providers = doc.setdefault("providers", {})
keys = providers.setdefault(provider, [])
if key in keys:
    print("DUPLICATE")
else:
    keys.append(key)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.chmod(path, 0o600)  # keys are secrets — owner read/write only
    print(len(keys))
PY
}

# ── hr auth add <provider> ────────────────────────────────────────────────────

cmd_add() {
  local raw="${1:-}"
  if [ -z "$raw" ]; then
    err "Usage: hr auth add <provider>"
    err "Providers: $PROVIDERS_LIST"
    exit 1
  fi

  local provider
  provider=$(canonical_provider "$raw")
  if [ -z "$provider" ]; then
    err "Unknown provider: '$raw'"
    err "Supported: $PROVIDERS_LIST"
    exit 1
  fi

  local existing
  existing=$(count_keys "$provider")
  if [ "$existing" -gt 0 ]; then
    log "$provider already has $existing key(s). New keys will be added to the pool."
  else
    log "No keys found for $provider yet. Adding the first one."
  fi
  log "Keys will be saved to: $AUTH_FILE"
  echo ""

  while true; do
    local key=""
    printf '\033[1;36m[auth]\033[0m Enter API key (input hidden): '
    read -rs key
    echo ""

    if [ -z "$key" ]; then
      warn "Empty key — skipped."
    else
      local result
      result=$(append_key "$provider" "$key")
      if [ "$result" = "DUPLICATE" ]; then
        warn "That key is already stored for $provider — skipped."
      else
        ok "Saved  (ends in: ...${key: -8})  — $provider now has $result key(s)"
      fi
    fi

    echo ""
    printf '\033[1;36m[auth]\033[0m Add another key for %s? [y/N]: ' "$provider"
    read -r again
    echo ""
    case "$again" in
      [yY]|[yY][eE][sS]) continue ;;
      *) break ;;
    esac
  done

  local total
  total=$(count_keys "$provider")
  ok "$provider now has $total key(s) in the credential pool."
  log "Apply the change with:  hr restart"
}

# ── hr auth list ─────────────────────────────────────────────────────────────

cmd_list() {
  if [ ! -f "$AUTH_FILE" ]; then
    warn "No auth.json found at: $AUTH_FILE"
    warn "Run 'hr auth add <provider>' to create one."
    exit 0
  fi

  echo ""
  printf '  %-16s  %s\n' "Provider" "Keys"
  printf '  %-16s  %s\n' "────────────────" "──────"

  local total_keys=0
  for provider in $PROVIDERS_LIST; do
    local count
    count=$(count_keys "$provider")
    total_keys=$((total_keys + count))
    if [ "$count" -eq 0 ]; then
      printf '  %-16s  \033[1;31m%s\033[0m\n' "$provider" "none"
    else
      printf '  %-16s  \033[1;32m%s\033[0m\n' "$provider" "$count key(s)"
    fi
  done

  # Codex accounts (OAuth, stored separately from string keys)
  local codex_count
  codex_count=$("$PYTHON" - "$AUTH_FILE" <<'PY'
import json, sys, os
path = sys.argv[1]
try:
    doc = json.load(open(path))
except Exception:
    doc = {}
print(len(doc.get("codex_accounts", [])))
PY
)
  if [ "$codex_count" -eq 0 ]; then
    printf '  %-16s  \033[1;31m%s\033[0m\n' "codex" "none"
  else
    printf '  %-16s  \033[1;32m%s\033[0m\n' "codex" "$codex_count account(s)"
  fi

  echo ""
  log "$total_keys total key(s) across all providers — stored in: $AUTH_FILE"
}

# ── hr auth import-codex ──────────────────────────────────────────────────────

cmd_import_codex() {
  local src="${CODEX_HOME:-$HOME/.codex}/auth.json"
  if [ ! -f "$src" ]; then
    err "No Codex login found at: $src"
    err "Log in first with the official Codex CLI:  codex login"
    exit 1
  fi
  log "Importing Codex login from: $src"

  local result
  result=$("$PYTHON" - "$src" "$AUTH_FILE" <<'PY'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    s = json.load(open(src))
except Exception as e:
    print("ERR could not read codex auth.json: %s" % e); raise SystemExit
toks = s.get("tokens") or {}
acct = {
    "account_id":    toks.get("account_id", ""),
    "access_token":  toks.get("access_token", ""),
    "refresh_token": toks.get("refresh_token", ""),
    "last_refresh":  s.get("last_refresh", ""),
}
if not acct["account_id"] or not acct["refresh_token"]:
    print("ERR codex auth.json missing account_id/refresh_token (is auth_mode 'chatgpt'?)")
    raise SystemExit
try:
    doc = json.load(open(dst)) if os.path.exists(dst) else {}
except Exception:
    doc = {}
if not isinstance(doc, dict):
    doc = {}
accts = doc.setdefault("codex_accounts", [])
# de-dupe / update by account_id
accts = [a for a in accts if a.get("account_id") != acct["account_id"]]
accts.append(acct)
doc["codex_accounts"] = accts
with open(dst, "w") as f:
    json.dump(doc, f, indent=2); f.write("\n")
os.chmod(dst, 0o600)
print("OK %s %d" % (acct["account_id"][-6:], len(accts)))
PY
)
  case "$result" in
    OK*)  local tail count
          tail=$(echo "$result" | awk '{print $2}')
          count=$(echo "$result" | awk '{print $3}')
          ok "Imported Codex account (…${tail}) — total: ${count} account(s)."
          log "Apply with:  hr restart" ;;
    *)    err "${result#ERR }"; exit 1 ;;
  esac
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

subcmd="${1:-help}"
shift 2>/dev/null || true

case "$subcmd" in
  add)                   cmd_add "$@" ;;
  import-codex|codex)    cmd_import_codex ;;
  list)                  cmd_list ;;
  help|-h|--help)        awk 'NR>1 && /^#/ {sub(/^#[[:space:]]?/,""); print; next} NR>1 {exit}' "$0" ;;
  *)
    err "unknown auth subcommand: '$subcmd'"
    err "Usage: hr auth add <provider>  |  hr auth import-codex  |  hr auth list"
    exit 1
    ;;
esac
