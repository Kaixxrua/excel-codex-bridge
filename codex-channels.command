#!/usr/bin/env sh
CHANNEL_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$CHANNEL_ROOT/codex-channels.sh" "$@"
CHANNEL_RESULT=$?
if [ "$#" -eq 0 ] && [ -t 0 ]; then
  printf '\nPress Enter to close. '
  read -r CHANNEL_INPUT
fi
exit "$CHANNEL_RESULT"
