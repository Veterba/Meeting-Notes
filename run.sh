#!/bin/bash
# Run straight from the source tree, without building the app.
#
#   ./run.sh
#
# Sets up whatever is missing — virtualenv, dependencies, Ollama, the model —
# and then puts the icon in the menu bar. Safe to run again at any time.
# To get a real Meeting Notes.app instead, use ./build.sh.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
say() { printf "\033[1m==>\033[0m %s\n" "$1"; }
die() { printf "\033[31m==> %s\033[0m\n" "$1" >&2; exit 1; }

# --- virtualenv ---------------------------------------------------------
if [ ! -x "$PY" ]; then
  say "Creating the virtualenv"
  BASE_PY=$(command -v python3.11 || command -v python3 || true)
  [ -n "$BASE_PY" ] || die "python3 not found. Install it: brew install python"
  "$BASE_PY" -m venv .venv
fi

# --- dependencies -------------------------------------------------------
if ! "$PY" -c "import rumps, WebKit, watchdog, jinja2, dotenv, docx, yaml, requests" 2>/dev/null; then
  say "Installing dependencies"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
fi

# Settings, notes and logs live in the data folder, not here — the first start
# creates it from .env.example, or from this folder's .env if there is one.
# MEETING_NOTES_DATA overrides it, for a dev run that must not touch the real one.

# --- Ollama -------------------------------------------------------------
command -v ollama >/dev/null || die "Ollama not found. Install it: brew install ollama"
if ! curl -sf -m 3 http://localhost:11434/api/tags >/dev/null; then
  say "Starting Ollama"
  if command -v brew >/dev/null && brew services list 2>/dev/null | grep -q '^ollama'; then
    brew services start ollama >/dev/null
  else
    nohup ollama serve >/dev/null 2>&1 &
  fi
  for _ in $(seq 1 30); do
    curl -sf -m 2 http://localhost:11434/api/tags >/dev/null && break
    sleep 1
  done
  curl -sf -m 2 http://localhost:11434/api/tags >/dev/null \
    || die "Ollama did not come up. Try: ollama serve"
fi

# --- model --------------------------------------------------------------
MODEL=$("$PY" -c "import llm; print(llm.default_model())")
if ! "$PY" -c "import llm,sys; sys.exit(0 if llm.has_model('$MODEL') else 1)"; then
  say "Model $MODEL is not downloaded"
  read -r -p "    Download it now? That is several GB. [Y/n] " answer
  case "${answer:-Y}" in
    [Nn]*) echo "    Skipped. Pick another model from the menu, or run: ollama pull $MODEL" ;;
    *)     ollama pull "$MODEL" ;;
  esac
fi

# --- go -----------------------------------------------------------------
pkill -f "menubar.py" 2>/dev/null || true
say "Model: $MODEL"
say "Data:     $("$PY" -c "import paths; print(paths.DATA)")"
say "Watching: $("$PY" -c "import paths; print(paths.inbox())")"
say "The icon is in the menu bar. Ctrl-C here, or Quit from the menu, to stop."
exec "$PY" menubar.py
