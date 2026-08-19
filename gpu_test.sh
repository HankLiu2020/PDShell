#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CUDA_VISIBLE_DEVICES_VALUE=${CUDA_VISIBLE_DEVICES-}
CUDA_VISIBLE_DEVICES_SET=0
if declare -p CUDA_VISIBLE_DEVICES >/dev/null 2>&1; then
    CUDA_VISIBLE_DEVICES_SET=1
fi
if [[ -f "$SCRIPT_DIR/env.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/env.sh"
fi
if [[ "$CUDA_VISIBLE_DEVICES_SET" -eq 1 ]]; then
    export CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES_VALUE"
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    printf 'nvidia-smi not found; GPU runtime is unavailable.\n' >&2
    exit 1
fi

nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader

if ! command -v python3 >/dev/null 2>&1; then
    printf 'python3 not found; CUDA driver probe passed but framework probe was skipped.\n' >&2
    exit 2
fi

python3 - <<'PY'
try:
    import torch
except ImportError:
    print("PyTorch is not installed; CUDA driver probe passed.")
    raise SystemExit(2)

if not torch.cuda.is_available():
    print("PyTorch cannot see a CUDA device.")
    raise SystemExit(1)

print(f"torch={torch.__version__}")
print(f"cuda={torch.version.cuda}")
for index in range(torch.cuda.device_count()):
    print(f"device[{index}]={torch.cuda.get_device_name(index)}")
PY
