#!/bin/sh
# Double-click (macOS): the Codex desktop app / IDE extension use the Excel bridge
# while this window stays open; closing it puts your Codex config back.
here=$(dirname "$0")
if [ -f "$here/excel-codex" ] && [ -x "$here/excel-codex" ]; then
  exec "$here/excel-codex" desktop "$@"
fi
exec "$here/excel-codex.sh" desktop "$@"
