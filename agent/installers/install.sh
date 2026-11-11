#!/usr/bin/env bash
# Nous Sync installer for macOS and Linux.
#   curl -fsSL <url>/install.sh | bash -s -- --code ABCD-EFGH --server https://drive.nous.example [--name "Nous MacBook Pro"]
set -euo pipefail
CODE="" SERVER="" NAME="" SRC="${NOUS_SYNC_SRC:-nous-sync}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2;;
    --server) SERVER="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --source) SRC="$2"; shift 2;;   # path or URL of the nous-sync package (default: from the package index)
    *) echo "unknown option $1"; exit 1;;
  esac
done
PY="$(command -v python3 || true)"
[[ -z "$PY" ]] && { echo "Python 3.11+ is required (macOS: xcode-select --install or python.org)"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ is required"; exit 1; }
APP="$HOME/.nous-sync/app"
mkdir -p "$APP"
"$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet "$SRC[content]"
mkdir -p "$HOME/.local/bin"
ln -sf "$APP/venv/bin/nous-sync" "$HOME/.local/bin/nous-sync"
echo "Installed nous-sync to $APP (linked at ~/.local/bin/nous-sync)"
if [[ -n "$CODE" && -n "$SERVER" ]]; then
  if [[ -n "$NAME" ]]; then "$APP/venv/bin/nous-sync" pair "$CODE" --server "$SERVER" --name "$NAME"
  else "$APP/venv/bin/nous-sync" pair "$CODE" --server "$SERVER"; fi
fi
"$APP/venv/bin/nous-sync" install
echo "Done. Add folders with: nous-sync add ~/Footage"
