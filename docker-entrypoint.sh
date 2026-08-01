#!/bin/sh
set -eu

exec python3 /opt/pdshell/pdshell.py worker \
  --root "${PDSHELL_ROOT:-/persist/fshell}" \
  --poll-interval "${PDSHELL_POLL_INTERVAL:-1}"
