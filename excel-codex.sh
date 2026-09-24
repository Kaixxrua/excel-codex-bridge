#!/usr/bin/env sh
# excel-codex: run Codex on your own ChatGPT Excel add-in session (macOS, Linux, WSL).
#   From a project folder:  /path/to/excel-codex.sh [options] [-- codex arguments]
# First run creates .venv next to this file and installs the dependencies.
set -e
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[excel-codex] First run: creating a Python environment in $VENV ..."
  PY=$(command -v python3 || command -v python || true)
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "[excel-codex] Python 3.10 or newer was not found." >&2
    exit 1
  fi
  "$PY" -m venv "$VENV"
fi
if ! cmp -s "$ROOT/requirements.txt" "$VENV/requirements.installed"; then
  echo "[excel-codex] Installing dependencies ..."
  "$VENV/bin/python" -m pip install --disable-pip-version-check -q -r "$ROOT/requirements.txt"
  cp "$ROOT/requirements.txt" "$VENV/requirements.installed"
fi

PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec "$VENV/bin/python" -m excel_codex_bridge "$@"
