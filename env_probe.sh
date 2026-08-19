#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

load_private_env() {
    local name index
    local -a explicit_names=() explicit_values=()
    for name in PDSHELL_ROOT PDSHELL_REMOTE_ROOT PDSHELL_ENDPOINT PDSHELL_REMOTE \
        PDSHELL_TRANSPORT PDSHELL_CACHE PDSHELL_POLL_INTERVAL PDSHELL_PORT \
        PDSHELL_SSH_HOST PDSHELL_SSH_USER PDSHELL_SSH_PORT PDSHELL_SSH_PASSWORD \
        PDSHELL_REMOTE_PROJECT_DIR PDSHELL_DEPLOY_TARGET CUDA_VISIBLE_DEVICES; do
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

printf 'PDShell environment probe\n'
printf 'script_dir=%s\n' "$SCRIPT_DIR"
printf 'PDSHELL_ROOT=%s\n' "${PDSHELL_ROOT:-<unset>}"
printf 'PDSHELL_TRANSPORT=%s\n' "${PDSHELL_TRANSPORT:-local}"
printf 'PDSHELL_ENDPOINT=%s\n' "${PDSHELL_ENDPOINT:-<derived from SSH settings>}"
printf 'PDSHELL_REMOTE=%s\n' "${PDSHELL_REMOTE:-<derived from SSH settings>}"
printf 'PDSHELL_REMOTE_ROOT=%s\n' "${PDSHELL_REMOTE_ROOT:-<unset>}"
printf 'PDSHELL_CACHE=%s\n' "${PDSHELL_CACHE:-<default>}"
printf 'PDSHELL_POLL_INTERVAL=%s\n' "${PDSHELL_POLL_INTERVAL:-<default>}"
printf 'PDSHELL_PORT=%s\n' "${PDSHELL_PORT:-7860}"
printf 'PDSHELL_SSH_HOST=%s\n' "${PDSHELL_SSH_HOST:-<unset>}"
printf 'PDSHELL_SSH_USER=%s\n' "${PDSHELL_SSH_USER:-<unset>}"
printf 'PDSHELL_SSH_PORT=%s\n' "${PDSHELL_SSH_PORT:-22}"
if [[ -n "${PDSHELL_SSH_PASSWORD:-}" ]]; then
    printf 'PDSHELL_SSH_PASSWORD=<configured>\n'
else
    printf 'PDSHELL_SSH_PASSWORD=<unset>\n'
fi
printf 'PDSHELL_REMOTE_PROJECT_DIR=%s\n' "${PDSHELL_REMOTE_PROJECT_DIR:-<unset>}"
printf 'PDSHELL_DEPLOY_TARGET=%s\n' "${PDSHELL_DEPLOY_TARGET:-<unset>}"
printf 'CUDA_VISIBLE_DEVICES=%s\n' "${CUDA_VISIBLE_DEVICES:-<unset>}"
printf 'python3=%s\n' "$(command -v python3 || printf '<missing>')"
printf 'rsync=%s\n' "$(command -v rsync || printf '<missing>')"
printf 'nvidia-smi=%s\n' "$(command -v nvidia-smi || printf '<missing>')"
