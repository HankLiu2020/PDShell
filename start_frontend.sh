#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

load_private_env() {
    local name index
    local -a explicit_names=() explicit_values=()
    for name in PDSHELL_ROOT PDSHELL_ENDPOINT PDSHELL_REMOTE_ROOT PDSHELL_REMOTE \
        PDSHELL_TRANSPORT PDSHELL_CACHE PDSHELL_POLL_INTERVAL PDSHELL_PORT \
        PDSHELL_SSH_HOST PDSHELL_SSH_USER PDSHELL_SSH_PORT PDSHELL_SSH_PASSWORD; do
        if declare -p "$name" >/dev/null 2>&1; then
            explicit_names+=("$name")
            explicit_values+=("${!name}")
        fi
    done
    if [[ -f "$SCRIPT_DIR/env.sh" ]]; then
        # shellcheck disable=SC1091
        source "$SCRIPT_DIR/env.sh"
    fi
    for index in "${!explicit_names[@]}"; do
        printf -v "${explicit_names[index]}" '%s' "${explicit_values[index]}"
        export "${explicit_names[index]}"
    done
}

load_private_env
unset -f load_private_env
export PDSHELL_CACHE=${PDSHELL_CACHE:-"$SCRIPT_DIR/.pdshell-cache"}

exec python3 "$SCRIPT_DIR/frontend/app.py" "$@"
