#!/usr/bin/env bash
set -euo pipefail
umask 000

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PDSHELL_ROOT=${PDSHELL_ROOT:-"$SCRIPT_DIR/tasks"}

exec python3 "$SCRIPT_DIR/pdshell.py" worker \
  --root "$PDSHELL_ROOT" \
  --poll-interval "${PDSHELL_POLL_INTERVAL:-1}"
