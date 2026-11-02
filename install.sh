#!/usr/bin/env bash
#
# hermes-router installer — one command sets everything up:
#   creates a venv, installs dependencies, and puts `hr` on your PATH.
#
# Usage:
#   ./install.sh
#
# One-liner (from scratch):
#   git clone https://github.com/Shaf2665/Hermes-router.git && cd Hermes-router && ./install.sh
#
set -euo pipefail

cd "$(dirname "$0")" || { echo "cannot cd to script dir"; exit 1; }
REPO="$(pwd)"

log() { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }

echo ""
echo "  ┌──────────────────────────────────┐"
echo "  │   hermes-router  ·  installer    │"
echo "  └──────────────────────────────────┘"
echo ""

# ── 1. Python 3.10+ ──────────────────────────────────────────────────────────
PYTHON=""
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then
    _ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    _maj="${_ver%%.*}"
    _min="${_ver#*.}"
    if [ "$_maj" -ge 3 ] && [ "$_min" -ge 10 ]; then
      PYTHON="$py"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  err "Python 3.10+ is required but not found on PATH."
  err "  Ubuntu/Debian:  sudo apt install python3"
  err "  macOS:          brew install python"
  exit 1
fi

ok "Python $("$PYTHON" --version 2>&1 | awk '{print $2}') found"

# ── 2. Create virtual environment ─────────────────────────────────────────────
if [ -f "$REPO/venv/bin/python" ]; then
  ok "venv already exists — skipping creation"
else
  log "Creating virtual environment..."
  if command -v uv >/dev/null 2>&1; then
    uv venv "$REPO/venv" --python "$PYTHON" --quiet
    ok "venv created (via uv)"
  else
    "$PYTHON" -m venv "$REPO/venv"
    ok "venv created"
  fi
fi

VENV_PYTHON="$REPO/venv/bin/python"

# ── 3. Install dependencies ───────────────────────────────────────────────────
log "Checking dependencies against requirements.txt..."
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_PYTHON" -r "$REPO/requirements.txt" --quiet
else
  "$VENV_PYTHON" -m pip install -r "$REPO/requirements.txt"
fi
"$VENV_PYTHON" -c "import flask, waitress, requests, tiktoken"
ok "Dependencies ready"

# Start locally without sample provider credentials; never replace operator settings.
if [ ! -e "$REPO/.env" ]; then
  (umask 077; set -C; printf 'HOST=127.0.0.1\nPORT=8319\n' > "$REPO/.env")
fi

# ── 4. Make scripts executable ────────────────────────────────────────────────
chmod +x "$REPO/hermes-router" "$REPO/install.sh" \
         "$REPO/scripts/auth.sh" "$REPO/scripts/status.sh" "$REPO/scripts/restart.sh" \
         "$REPO/scripts/setup.sh" "$REPO/scripts/doctor.sh" \
         "$REPO/scripts/update.sh" "$REPO/scripts/model.sh" \
         "$REPO/scripts/mode.sh" 2>/dev/null || true

# ── 5. Symlink `hr` and `hermes-router` on PATH ───────────────────────────────
BINDIR=""
for d in "$HOME/.local/bin" "/usr/local/bin"; do
  case ":$PATH:" in *":$d:"*) BINDIR="$d"; break ;; esac
done
[ -n "$BINDIR" ] || BINDIR="$HOME/.local/bin"
mkdir -p "$BINDIR"

_symlink() {
  local name="$1"
  local link="$BINDIR/$name"
  if ln -sf "$REPO/hermes-router" "$link" 2>/dev/null; then
    ok "symlinked: $link"
  elif command -v sudo >/dev/null 2>&1; then
    log "need elevated permission for $BINDIR..."
    sudo ln -sf "$REPO/hermes-router" "$link" && ok "symlinked: $link" || { err "failed to symlink $name"; exit 1; }
  else
    err "can't write to $BINDIR — run manually:"
    err "  ln -sf \"$REPO/hermes-router\" ~/.local/bin/$name"
    exit 1
  fi
}

_symlink hermes-router
_symlink hr

# ── 6. PATH auto-fix ─────────────────────────────────────────────────────────
case ":$PATH:" in
  *":$BINDIR:"*)
    ;;
  *)
    SHELL_RC=""
    case "${SHELL:-}" in
      */zsh)  SHELL_RC="$HOME/.zshrc" ;;
      */bash) SHELL_RC="$HOME/.bashrc" ;;
    esac

    EXPORT_LINE="export PATH=\"$BINDIR:\$PATH\""

    if [ -n "$SHELL_RC" ] && ! grep -qF "$BINDIR" "$SHELL_RC" 2>/dev/null; then
      printf '\n# hermes-router\n%s\n' "$EXPORT_LINE" >> "$SHELL_RC"
      log "Added $BINDIR to PATH in $SHELL_RC"
      log "Run: source $SHELL_RC  (or open a new terminal)"
    elif [ -z "$SHELL_RC" ]; then
      log "Add this to your shell config (~/.bashrc or ~/.zshrc):"
      echo "    $EXPORT_LINE"
    fi
    ;;
esac

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
ok "Installation complete!"
echo ""
echo "  Next steps:"
echo "    hr setup                 ← interactive setup wizard (recommended)"
echo "    hr auth add openrouter   ← or add a key directly"
echo ""
