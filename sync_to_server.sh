#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEPLOY_TARGET=${PDSHELL_DEPLOY_TARGET:-}
REMOTE_PROJECT_DIR=${PDSHELL_REMOTE_PROJECT_DIR:-}
SSH_HOST=${PDSHELL_SSH_HOST:-}
SSH_USER=${PDSHELL_SSH_USER:-}
SSH_PORT=${PDSHELL_SSH_PORT:-22}
SSH_PASSWORD=${PDSHELL_SSH_PASSWORD:-}
LAST_SYNC_FILE=${PDSHELL_LAST_SYNC_FILE:-"$SCRIPT_DIR/.last_sync_target"}

if [[ -z "$DEPLOY_TARGET" && -n "$SSH_HOST" && -n "$REMOTE_PROJECT_DIR" ]]; then
    DEPLOY_TARGET=${SSH_USER:+$SSH_USER@}${SSH_HOST}:${REMOTE_PROJECT_DIR}
fi

usage() {
    cat <<'EOF'
用法:
  PDSHELL_DEPLOY_TARGET=user@host:/path/to/PDShell ./sync_to_server.sh

也可以分别设置:
  PDSHELL_SSH_HOST / PDSHELL_SSH_USER / PDSHELL_SSH_PORT
  PDSHELL_REMOTE_PROJECT_DIR
  PDSHELL_SSH_PASSWORD（可选，通过 sshpass -e 使用）

脚本不会同步 .git、tasks、缓存、.env 或上次同步记录。
EOF
}

[[ -n "$DEPLOY_TARGET" ]] || { usage >&2; exit 2; }
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || { printf '非法 SSH 端口: %s\n' "$SSH_PORT" >&2; exit 2; }

is_remote_target() {
    [[ "$DEPLOY_TARGET" == *:* ]]
}

require_sshpass() {
    [[ -z "$SSH_PASSWORD" ]] && return 0
    command -v sshpass >/dev/null 2>&1 || {
        printf '已设置 PDSHELL_SSH_PASSWORD，但系统没有 sshpass\n' >&2
        return 2
    }
}

run_rsync() {
    local command_args=(rsync)
    if is_remote_target; then
        command_args+=(-e "ssh -p $SSH_PORT -o ServerAliveInterval=30")
    fi
    if [[ -n "$SSH_PASSWORD" ]]; then
        require_sshpass
        SSHPASS=$SSH_PASSWORD sshpass -e "${command_args[@]}" "$@"
    else
        "${command_args[@]}" "$@"
    fi
}

run_ssh() {
    local command_args=(ssh -p "$SSH_PORT" -o ServerAliveInterval=30)
    if [[ -n "$SSH_PASSWORD" ]]; then
        require_sshpass
        SSHPASS=$SSH_PASSWORD sshpass -e "${command_args[@]}" "$@"
    else
        "${command_args[@]}" "$@"
    fi
}

if is_remote_target; then
    remote_host=${DEPLOY_TARGET%%:*}
    remote_dir=${DEPLOY_TARGET#*:}
    printf -v mkdir_command 'mkdir -p %q' "$remote_dir"
    run_ssh "$remote_host" "$mkdir_command"
else
    mkdir -p "$DEPLOY_TARGET"
fi

run_rsync -az \
    --exclude='.git/' \
    --exclude='tasks/' \
    --exclude='.pdshell-cache/' \
    --exclude='.last_sync_target' \
    --exclude='.env' \
    --exclude='env.sh' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    "$SCRIPT_DIR/" "${DEPLOY_TARGET%/}/"

if is_remote_target; then
    printf -v chmod_command 'chmod +x %q %q %q' \
        "$remote_dir/docker-entrypoint.sh" \
        "$remote_dir/pdshell.py" \
        "$remote_dir/pdshell_client.sh"
    run_ssh "$remote_host" "$chmod_command"
    printf '同步完成。服务器启动命令:\n'
    printf '  cd %q && nohup bash docker-entrypoint.sh > worker.nohup.log 2>&1 &\n' "$remote_dir"
else
    chmod +x \
        "$DEPLOY_TARGET/docker-entrypoint.sh" \
        "$DEPLOY_TARGET/pdshell.py" \
        "$DEPLOY_TARGET/pdshell_client.sh"
    printf '本地同步完成: %s\n' "$DEPLOY_TARGET"
fi

printf '%s\n' "$DEPLOY_TARGET" > "$LAST_SYNC_FILE"
