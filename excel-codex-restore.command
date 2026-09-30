#!/bin/sh
# Double-click (macOS): put Codex back on its own setup, as it was before the bridge.
# The bridge's settings come out of ~/.codex/config.toml (also ones pasted in by hand),
# and its own conversations move into Codex's list. Linux: run this file, too.
here=$(dirname "$0")
if [ -f "$here/excel-codex" ] && [ -x "$here/excel-codex" ]; then
  exec "$here/excel-codex" restore "$@"
fi
exec "$here/excel-codex.sh" restore "$@"
