#!/usr/bin/env bash
set -euo pipefail

REMOTE=${PDSHELL_REMOTE:-}
MODE=${PDSHELL_TRANSPORT:-rsync}
CACHE=${PDSHELL_CACHE:-${TMPDIR:-/tmp}/pdshell-cache}
POLL_INTERVAL=${PDSHELL_POLL_INTERVAL:-2}
MAX_LOG_BYTES=204800

usage() {
    cat <<'EOF'
用法:
  pdshell_client.sh submit <script> [job-id]
  pdshell_client.sh list
  pdshell_client.sh status <job-id>
  pdshell_client.sh watch <job-id>
  pdshell_client.sh logs <job-id> [stdout|stderr]

配置:
  PDSHELL_REMOTE=user@host:/persist/tasks 或本地 tasks 路径
  PDSHELL_TRANSPORT=rsync（默认）或 scp
  PDSHELL_CACHE=本地缓存目录
EOF
}

[[ -n "$REMOTE" ]] || { usage >&2; exit 2; }

remote_path() {
    printf '%s/%s' "${REMOTE%/}" "$1"
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
        rsync -a \
            --include='*/' \
            --include='heartbeat' \
            --include='.ready' \
            --include='.running' \
            --include='.done' \
            --include='.failed' \
            --include='exitcode' \
            --exclude='*' \
            "$(remote_path '')" "$CACHE/"
    elif [[ "$MODE" == "scp" ]]; then
        scp -q -r "$(remote_path '.')" "$CACHE"
    else
        printf '不支持的传输模式: %s\n' "$MODE" >&2
        return 2
    fi
}

sync_job() {
    local job_id=$1
    mkdir -p "$CACHE/$job_id"
    if [[ "$MODE" == "rsync" ]]; then
        local name
        for name in .ready .running .done .failed exitcode log stderr.log; do
            rsync -a "$(remote_path "$job_id/$name")" "$CACHE/$job_id/$name" 2>/dev/null || true
        done
    else
        scp -q -r "$(remote_path "$job_id/.")" "$CACHE/$job_id" 2>/dev/null || true
    fi
}

state_for() {
    local job_dir=$1
    if [[ -f "$job_dir/.done" ]]; then
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

    local staging
    staging=$(mktemp -d "${TMPDIR:-/tmp}/pdshell-submit.XXXXXX")
    trap 'rm -rf "$staging"' RETURN
    cp "$script" "$staging/run.sh"
    chmod 755 "$staging/run.sh"
    printf 'READY\nsubmitted_at=%s\n' "$(date +%s.%N)" > "$staging/.ready"

    rsync -a "$script" "$(remote_path "$job_id.sh")"
    rsync -a "$staging/" "$(remote_path "$job_id/")"
    rsync -a "$staging/.ready" "$(remote_path "$job_id/.ready")"
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
    *)
        usage >&2
        exit 2
        ;;
esac
