# PDShell

PDShell（Persistent Directory Shell）把一个长期运行、没有 SSH 的容器变成基于共享目录的异步 Shell：外部写入 `run.sh` 和 `.ready`，容器内的 Worker 串行执行脚本，并把日志、状态、退出码和心跳写回同一目录。

核心运行时只有一个 Python 标准库脚本。不需要数据库、Redis、HTTP、SSH、RPC 或常驻客户端。

它适合单机、可信用户、小规模训练任务；它不是 Kubernetes、Slurm、Celery，也不提供资源调度、优先级、DAG 或自动重试。

## 要求

- Python 3.10 或更高版本
- Bash
- 支持原子 rename 的共享文件系统
- 单个共享目录只运行一个 Worker

## 快速开始

在仓库根目录启动 Worker：

```bash
python3 pdshell.py worker --root /tmp/fshell
```

另一个终端准备并提交任务：

```bash
cat > /tmp/hello.sh <<'SH'
#!/usr/bin/env bash
echo "hello from $(hostname)"
echo "stderr example" >&2
SH

python3 pdshell.py submit /tmp/hello.sh \
  --root /tmp/fshell \
  --job-id hello-001
```

查看结果：

```bash
cat /tmp/fshell/logs/hello-001.status
cat /tmp/fshell/logs/hello-001.exitcode
tail -f /tmp/fshell/logs/hello-001.log
```

## 文件协议

```text
/persist/fshell/
├── inbox/<id>.ready          # READY：提交的最后一步
├── jobs/<id>/run.sh          # 完整上传的任务脚本
├── running/<id>              # RUNNING：Worker 原子领取后的标记
├── done/<id>                 # 成功终态标记
├── failed/<id>               # FAILED 或 WORKER_LOST 终态标记
├── rejected/<id>.ready       # 被拒绝的重复或非法 ready
├── logs/<id>.log             # stdout
├── logs/<id>.stderr.log      # stderr
├── logs/<id>.status          # 给客户端读取的状态快照
├── logs/<id>.exitcode        # 进程退出码；WORKER_LOST 为 -1
├── heartbeat                 # timestamp/hostname/pid/current_job
├── worker.log
└── worker.lock               # 防止同一目录同时启动两个 Worker
```

状态目录是唯一真相：

- `inbox/<id>.ready` 存在表示 READY。
- `running/<id>` 存在表示 RUNNING。
- `done/<id>` 存在表示 SUCCEEDED。
- `failed/<id>` 的内容区分 FAILED 和 WORKER_LOST。

`logs/<id>.status` 只是方便 GUI 读取的快照。使用纯文件协议时，Worker 可能很快领取任务，因此 `.status` 可能从不存在直接变成 `RUNNING`；客户端应通过 `inbox/<id>.ready` 判断 READY，而不能要求一定观察到 `.status=READY`。

GUI 或查询脚本应每次重新读取文件，并按终态优先的顺序推导状态：`done/` → `failed/` → `running/` → `inbox/*.ready` → 未知。

已有终态的重复 `.ready` 和非法任务 ID 会被原子移入 `rejected/`，不会重复执行，也不会永久留在 `inbox/` 反复刷日志。

### 不使用提交命令

平台外部可以完全不运行 Python 提交命令，只要严格遵循以下顺序：

```bash
job_id=hello-002
root=/persist/fshell

mkdir -p "$root/jobs/$job_id"
cp /path/to/run.sh "$root/jobs/$job_id/run.sh"

# run.sh 完整落盘以后，最后创建 ready；临时文件和目标文件必须位于同一文件系统。
printf 'READY\n' > "$root/inbox/.$job_id.ready.tmp"
mv "$root/inbox/.$job_id.ready.tmp" "$root/inbox/$job_id.ready"
```

不要先创建 `.ready`，否则 Worker 可能读取尚未上传完成的脚本。

## Docker 接入

`Dockerfile.example` 可用于构建演示镜像：

```bash
docker build -f Dockerfile.example -t pdshell-mvp .
docker run --rm -v /宿主机/持久化目录:/persist pdshell-mvp
```

接入已有训练镜像时，把 `pdshell.py` 和 `docker-entrypoint.sh` 复制到 `/opt/pdshell/`，并把 `docker-entrypoint.sh` 设为 Entrypoint。

Docker 镜像、bind mount、GPU runtime、OOM 和宿主机重启尚未在真实训练平台验证。部署前必须使用目标镜像和目标持久化文件系统补测。

## 重启恢复

Worker 启动时先扫描 `running/`。每个遗留任务都会被原子移动到 `failed/`，状态写为 `WORKER_LOST`，退出码写为 `-1`。Worker 不猜测脚本是否已经执行完，也不会自动重跑。

```text
上传 jobs/<id>/run.sh
        ↓ 最后创建 ready
inbox/<id>.ready
        ↓ os.replace 原子领取
running/<id>
        ↓ bash 在独立进程会话中执行
写 exitcode → 写终态标记内容 → os.replace 到 done/failed → 写 status
```

这是 **at-most-once（至多一次）** 语义：宁可让异常中断的训练等待人工判断，也不因容器重启重复训练。脚本可能已经执行完成，但如果 Worker 在终态落盘前崩溃，恢复后仍会标记为 `WORKER_LOST`。人工确认后若需重跑，应使用新的任务 ID。

Worker 收到 SIGTERM 或 SIGINT 时，会终止当前任务的整个进程组并将任务记为 `FAILED`。SIGKILL、OOM 或整容器消失时，由下次启动执行 WORKER_LOST 恢复。

重建置换 Worker 之前，平台必须先确认旧容器及其 cgroup 已完全退出。只杀掉 Worker 会释放 `worker.lock`，但已启动的 Shell 和训练子进程可能仍在运行；此时立即启动新 Worker 会破坏单执行者假设。

Worker 收到停止信号后会先等待任务进程组退出，约 10 秒后才升级为 SIGKILL。平台或 Docker 的停止宽限期应明显更长，建议至少 30 秒，否则 Worker 可能来不及写入 `FAILED`，重启后任务将被恢复为 `WORKER_LOST`。

## 验证状态

已在 x86_64 Linux、Python 3.12、Bash 5.2 和本地 ext4 文件系统的进程模型中动态验证：

- 11 项集成测试全部通过。
- 纯文件创建任务在 `.ready` 前不会执行，提交后只执行一次。
- heartbeat、stdout、stderr、exitcode 和终态标记正确。
- 人工构造的遗留 RUNNING marker 可转为 WORKER_LOST，随后连续启动 5 次均未重跑。
- 单 Worker 文件锁、SIGTERM 进程组终止和含空格路径通过。
- 真实硬杀 Worker 后，任务 Shell 和子进程仍存活；额外终止任务进程组并重建 Worker 后，任务恢复为 WORKER_LOST 且未二次执行。
- 20 个批量投递的纯文件任务全部串行完成，无失败、重复执行、执行重叠或日志串扰；提交与扫描并发时不保证全局 FIFO 或全局字典序。
- 任务期间高频读取 heartbeat 319687 次，本地 ext4 上未观察到空文件、半截快照或读取错误。
- 忽略 SIGTERM 的任务在约 10.131 秒后被 SIGKILL，并正确落盘为 `FAILED/-9`；2 秒停止宽限测试复现了重启后转为 `WORKER_LOST` 的竞争。

进程组硬杀只是对容器 cgroup 销毁的近似，不等价于真实容器测试。当前尚未验证 Docker、PID 1 信号语义、cgroup、bind mount、NFS/CephFS、UID/GID 和 OOM 行为。

本地运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 边界与风险

- 单 Worker、串行执行；队列按文件名字典序处理，不保证提交时间 FIFO。
- 不提供优先级、DAG、GPU 调度、自动重试、取消队列或 Web GUI。
- `jobs/<id>` 已上传但尚未创建 `.ready` 时不算任务，Worker 不执行也不清理它。
- `.ready` 创建后任务目录仍可被外部修改；当前没有脚本哈希或冻结机制。
- 已有终态的任务不会因重复 `.ready` 重跑，但重复 marker 会留在 `inbox/`。
- 文件已执行 `fsync`，但父目录没有 `fsync`，不提供机器掉电级事务保证。
- `os.replace()` 和 `flock()` 必须在目标 NFS、CIFS、CephFS 或其他共享存储上实测。
- 进程组终止无法覆盖主动脱离会话的子进程；真实隔离与清理应依赖容器 cgroup。
- 重定向到文件后，部分程序会缓冲 stdout；训练脚本可使用 `python -u` 或 `PYTHONUNBUFFERED=1`。
- PDShell 会执行任意 Shell 命令，只应向受信任用户开放共享目录写权限，并尽量使用非 root 用户运行。
