#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

load_private_env() {
    local name index
    local -a explicit_names=() explicit_values=()
    for name in PDSHELL_REMOTE PDSHELL_REMOTE_ROOT PDSHELL_SSH_HOST PDSHELL_SSH_USER \
        PDSHELL_SSH_PORT PDSHELL_SSH_PASSWORD PDSHELL_TRANSPORT PDSHELL_CACHE PDSHELL_POLL_INTERVAL; do
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

REMOTE=${PDSHELL_REMOTE:-}
REMOTE_ROOT=${PDSHELL_REMOTE_ROOT:-}
SSH_HOST=${PDSHELL_SSH_HOST:-}
SSH_USER=${PDSHELL_SSH_USER:-}
SSH_PORT=${PDSHELL_SSH_PORT:-22}
SSH_PASSWORD=${PDSHELL_SSH_PASSWORD:-}
MODE=${PDSHELL_TRANSPORT:-rsync}
CACHE=${PDSHELL_CACHE:-${TMPDIR:-/tmp}/pdshell-cache}
POLL_INTERVAL=${PDSHELL_POLL_INTERVAL:-2}
MAX_LOG_BYTES=204800
NFS_RSYNC_FLAGS=(--no-owner --no-group --no-perms --no-times)

if [[ -z "$REMOTE" && -n "$SSH_HOST" && -n "$REMOTE_ROOT" ]]; then
    REMOTE=${SSH_USER:+$SSH_USER@}${SSH_HOST}:${REMOTE_ROOT}
fi

usage() {
    cat <<'EOF'
用法:
  pdshell_client.sh submit <script> [job-id]
  pdshell_client.sh list
  pdshell_client.sh status <job-id>
  pdshell_client.sh watch <job-id>
  pdshell_client.sh logs <job-id> [stdout|stderr]
  pdshell_client.sh delete <job-id>

配置:
  PDSHELL_REMOTE=user@host:/persist/tasks 或本地 tasks 路径
  或使用 PDSHELL_SSH_HOST / PDSHELL_SSH_USER / PDSHELL_REMOTE_ROOT
  PDSHELL_SSH_PORT=SSH 端口（默认 22）
  PDSHELL_SSH_PASSWORD=可选；设置后通过 sshpass -e 读取，不进入命令行
  PDSHELL_TRANSPORT=rsync（默认）或 scp
  PDSHELL_CACHE=本地缓存目录
EOF
}

[[ -n "$REMOTE" ]] || { usage >&2; exit 2; }
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || { printf '非法 SSH 端口: %s\n' "$SSH_PORT" >&2; exit 2; }

is_remote_endpoint() {
    [[ "$REMOTE" == *:* ]]
}

require_sshpass() {
    [[ -z "$SSH_PASSWORD" ]] && return 0
    command -v sshpass >/dev/null 2>&1 || {
        printf '已设置 PDSHELL_SSH_PASSWORD，但系统没有 sshpass\n' >&2
        return 2
    }
}

run_rsync() {
    local command_args=(rsync "${NFS_RSYNC_FLAGS[@]}")
    if is_remote_endpoint; then
        command_args+=(-e "ssh -p $SSH_PORT -o ServerAliveInterval=30")
    fi
    if [[ -n "$SSH_PASSWORD" ]]; then
        require_sshpass
        SSHPASS=$SSH_PASSWORD sshpass -e "${command_args[@]}" "$@"
    else
        "${command_args[@]}" "$@"
    fi
}

run_scp() {
    local command_args=(scp)
    if is_remote_endpoint; then
        command_args+=(-P "$SSH_PORT")
    fi
    if [[ -n "$SSH_PASSWORD" ]]; then
        require_sshpass
        SSHPASS=$SSH_PASSWORD sshpass -e "${command_args[@]}" "$@"
    else
        "${command_args[@]}" "$@"
    fi
}

remote_path() {
    printf '%s/%s' "${REMOTE%/}" "$1"
}

remote_job_exists() {
    local job_id=$1
    local listing
    local exitcode
    if listing=$(run_rsync \
        --list-only \
        --include="/$job_id/" \
        --include="/$job_id.sh" \
        --exclude='*' \
        "$(remote_path '')" 2>&1); then
        :
    else
        exitcode=$?
        printf '远端任务预检失败，rsync exitcode=%s\n' "$exitcode" >&2
        return 2
    fi
    local line listed_name
    while IFS= read -r line; do
        listed_name=${line##* }
        if [[ "$listed_name" == "$job_id" || "$listed_name" == "$job_id.sh" ]]; then
            return 0
        fi
    done <<< "$listing"
    return 1
}

validate_job_id() {
    local job_id=$1
    [[ "$job_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 1
    [[ ! "$job_id" =~ \.sh$ ]] || return 1
    case "$job_id" in
        heartbeat|worker.log|worker.lock|inbox|jobs|running|done|failed|logs|rejected)
            return 1
            ;;
    esac
}

sync_metadata() {
    mkdir -p "$CACHE"
    if [[ "$MODE" == "rsync" ]]; then
        run_rsync -a \
            --include='*/' \
            --include='heartbeat' \
            --include='.ready' \
            --include='.running' \
            --include='.done' \
            --include='.failed' \
            --include='.delete' \
            --include='exitcode' \
            --delete \
            --exclude='*' \
            "$(remote_path '')" "$CACHE/"
        prune_cache_jobs
    elif [[ "$MODE" == "scp" ]]; then
        run_scp -q -r "$(remote_path '.')" "$CACHE"
    else
        printf '不支持的传输模式: %s\n' "$MODE" >&2
        return 2
    fi
}

prune_cache_jobs() {
    [[ "$MODE" == "rsync" ]] || return 0
    local listing
    if ! listing=$(run_rsync --list-only --include='*/' --exclude='*' "$(remote_path '')" 2>/dev/null); then
        return 0
    fi
    shopt -s nullglob
    local job_dir job_id line remote_job root_seen=1
    while IFS= read -r line; do
        remote_job=${line##* }
        if [[ "$remote_job" == "." ]]; then
            root_seen=0
            break
        fi
    done <<< "$listing"
    [[ "$root_seen" -eq 0 ]] || return 0
    for job_dir in "$CACHE"/*; do
        [[ -d "$job_dir" && ! -L "$job_dir" ]] || continue
        job_id=$(basename "$job_dir")
        [[ "$job_id" == .* ]] && continue
        [[ "$job_id" == "heartbeat" || "$job_id" == "worker.log" || "$job_id" == "worker.lock" ]] && continue
        local found=1
        while IFS= read -r line; do
            remote_job=${line##* }
            [[ "$remote_job" == "." || "$remote_job" == ".." ]] && continue
            if [[ "$remote_job" == "$job_id" ]]; then
                found=0
                break
            fi
        done <<< "$listing"
        if [[ "$found" -ne 0 ]]; then
            rm -rf -- "$job_dir"
        fi
    done
}

sync_job() {
    local job_id=$1
    mkdir -p "$CACHE/$job_id"
    if [[ "$MODE" == "rsync" ]]; then
        run_rsync -a \
            --include='*/' \
            --include='.ready' \
            --include='.running' \
            --include='.done' \
            --include='.failed' \
            --include='.delete' \
            --include='exitcode' \
            --include='log' \
            --include='stderr.log' \
            --delete \
            --exclude='*' \
            "$(remote_path "$job_id/")" "$CACHE/$job_id/" 2>/dev/null || true
    else
        run_scp -q -r "$(remote_path "$job_id/.")" "$CACHE/$job_id" 2>/dev/null || true
    fi
}

state_for() {
    local job_dir=$1
    if [[ -f "$job_dir/.running" && -f "$job_dir/.delete" ]]; then
        printf 'RUNNING'
    elif [[ -f "$job_dir/.delete" ]]; then
        printf 'DELETING'
    elif [[ -f "$job_dir/.done" ]]; then
        printf 'SUCCEEDED'
    elif [[ -f "$job_dir/.failed" ]]; then
        head -n 1 "$job_dir/.failed"
    elif [[ -f "$job_dir/.running" ]]; then
        printf 'RUNNING'
    elif [[ -f "$job_dir/.ready" ]]; then
        printf 'READY'
    else
        printf 'INCOMPLETE'
    fi
}

print_status() {
    local job_id=$1
    local job_dir=$CACHE/$job_id
    local state
    state=$(state_for "$job_dir")
    local exitcode='-'
    [[ -f "$job_dir/exitcode" ]] && exitcode=$(<"$job_dir/exitcode")
    printf '%s\t%s\t%s\n' "$job_id" "$state" "$exitcode"
}

submit() {
    [[ "$MODE" == "rsync" ]] || { printf 'scp 模式只读，不能提交任务\n' >&2; exit 2; }
    local script=$1
    local job_id=${2:-$(date +%Y%m%d-%H%M%S)-$$-${RANDOM}}
    [[ -f "$script" ]] || { printf '脚本不存在: %s\n' "$script" >&2; exit 2; }
    validate_job_id "$job_id" || { printf '非法任务 ID: %s\n' "$job_id" >&2; exit 2; }

    local precheck_exitcode
    if remote_job_exists "$job_id"; then
        printf '任务 ID 已存在，拒绝重复提交: %s\n' "$job_id" >&2
        exit 2
    else
        precheck_exitcode=$?
    fi
    [[ "$precheck_exitcode" -eq 1 ]] || exit "$precheck_exitcode"

    local staging
    staging=$(mktemp -d "${TMPDIR:-/tmp}/pdshell-submit.XXXXXX")
    trap 'rm -rf "$staging"' RETURN
    cp "$script" "$staging/run.sh"
    chmod 755 "$staging/run.sh"
    printf 'READY\nsubmitted_at=%s\n' "$(date +%s)" > "$staging/.ready"

    run_rsync -a --chmod=Du=rwx,Dgo=rwx,Fu=rw,Fgo=rw "$script" "$(remote_path "$job_id.sh")"
    run_rsync -a --chmod=Du=rwx,Dgo=rwx,Fu=rw,Fgo=rw --exclude='.ready' "$staging/" "$(remote_path "$job_id/")"
    run_rsync -a --chmod=Du=rwx,Dgo=rwx,Fu=rw,Fgo=rw "$staging/.ready" "$(remote_path "$job_id/.ready")"
    printf '%s\n' "$job_id"
}

list_jobs() {
    sync_metadata
    printf 'JOB_ID\tSTATE\tEXITCODE\n'
    shopt -s nullglob
    local job_dir job_id
    for job_dir in "$CACHE"/*; do
        [[ -d "$job_dir" ]] || continue
        job_id=$(basename "$job_dir")
        [[ "$job_id" == .* ]] && continue
        [[ "$job_id" == "heartbeat" || "$job_id" == "worker.log" || "$job_id" == "worker.lock" ]] && continue
        print_status "$job_id"
    done
}

status() {
    local job_id=$1
    validate_job_id "$job_id" || { printf '非法任务 ID: %s\n' "$job_id" >&2; exit 2; }
    sync_job "$job_id"
    print_status "$job_id"
}

logs() {
    local job_id=$1
    local stream=${2:-stdout}
    local stream_file
    case "$stream" in
        stdout) stream_file=log ;;
        stderr) stream_file=stderr.log ;;
        *) printf '日志类型只能是 stdout 或 stderr\n' >&2; exit 2 ;;
    esac
    validate_job_id "$job_id" || { printf '非法任务 ID: %s\n' "$job_id" >&2; exit 2; }
    sync_job "$job_id"
    [[ -f "$CACHE/$job_id/$stream_file" ]] || exit 0
    tail -c "$MAX_LOG_BYTES" "$CACHE/$job_id/$stream_file"
}

delete_job() {
    [[ "$MODE" == "rsync" ]] || { printf 'scp 模式只读，不能删除任务\n' >&2; exit 2; }
    local job_id=$1
    validate_job_id "$job_id" || { printf '非法任务 ID: %s\n' "$job_id" >&2; exit 2; }
    local precheck_exitcode
    if remote_job_exists "$job_id"; then
        :
    else
        precheck_exitcode=$?
        if [[ "$precheck_exitcode" -eq 1 ]]; then
            printf '任务不存在，拒绝删除: %s\n' "$job_id" >&2
        fi
        exit "$precheck_exitcode"
    fi
    sync_job "$job_id"
    local state
    state=$(state_for "$CACHE/$job_id")
    [[ "$state" == "RUNNING" ]] && {
        printf '任务正在运行，拒绝删除: %s\n' "$job_id" >&2
        exit 2
    }
    local staging
    staging=$(mktemp -d "${TMPDIR:-/tmp}/pdshell-delete.XXXXXX")
    trap 'rm -rf "$staging"' RETURN
    printf 'DELETE\njob_id=%s\nrequested_at=%s\n' "$job_id" "$(date +%s)" > "$staging/.delete"
    run_rsync -a --chmod=Du=rwx,Dgo=rwx,Fu=rw,Fgo=rw \
        "$staging/.delete" "$(remote_path "$job_id/.delete")"
    mkdir -p "$CACHE/$job_id"
    cp "$staging/.delete" "$CACHE/$job_id/.delete"
    printf '已提交删除请求: %s\n' "$job_id"
}

watch_job() {
    local job_id=$1
    while true; do
        printf '\033[2J\033[H'
        date '+%Y-%m-%d %H:%M:%S'
        status "$job_id"
        printf '\n--- stdout (tail) ---\n'
        logs "$job_id" stdout
        sleep "$POLL_INTERVAL"
    done
}

command_name=${1:-}
case "$command_name" in
    submit)
        [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 2; }
        submit "$2" "${3:-}"
        ;;
    list)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        list_jobs
        ;;
    status)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        status "$2"
        ;;
    watch)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        watch_job "$2"
        ;;
    logs)
        [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 2; }
        logs "$2" "${3:-stdout}"
        ;;
    delete)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        delete_job "$2"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
