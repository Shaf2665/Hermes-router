#!/usr/bin/env bash
#
# hermes-router remote installer
#
# Clones the repo and runs install.sh in one step.
#
# Usage (one-liner):
#   curl -fsSL https://raw.githubusercontent.com/Shaf2665/Hermes-router/main/get.sh | bash
#
# By default installs to ~/.local/share/hermes-router
# Override with: HERMES_ROUTER_DIR=~/mydir bash <(curl ...)
#
set -euo pipefail

REPO_URL="https://github.com/Shaf2665/Hermes-router.git"
INSTALL_DIR="${HERMES_ROUTER_DIR:-$HOME/.local/share/hermes-router}"

log() { printf '\033[1;36m[hermes-router]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[hermes-router]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1;32m[hermes-router]\033[0m %s\n' "$*"; }

echo ""
echo "  ┌──────────────────────────────────┐"
echo "  │   hermes-router  ·  installer    │"
echo "  └──────────────────────────────────┘"
echo ""

# ── git ──────────────────────────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  err "git is required but not found."
  err "  Ubuntu/Debian:  sudo apt install git"
  err "  macOS:          brew install git"
  exit 1
fi

# ── Clone or update ───────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
  log "Found existing install at $INSTALL_DIR — updating..."
  git -C "$INSTALL_DIR" pull --ff-only --quiet
  ok "Updated to latest version"
else
  log "Installing to $INSTALL_DIR ..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet "$REPO_URL" "$INSTALL_DIR"
  ok "Downloaded"
fi

# ── Run install.sh ────────────────────────────────────────────────────────────
bash "$INSTALL_DIR/install.sh"
