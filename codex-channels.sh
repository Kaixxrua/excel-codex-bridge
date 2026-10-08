#!/usr/bin/env sh
set -e
CHANNEL_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$CHANNEL_ROOT/excel-codex" ]; then
  exec "$CHANNEL_ROOT/excel-codex" research "$@"
fi
exec "$CHANNEL_ROOT/excel-codex.sh" research "$@"
