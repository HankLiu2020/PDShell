# PDShell MVP

PDShell（Persistent Directory Shell）把一个长期运行的 Docker 容器变成基于共享目录的异步 Shell。它只有一个 Python 标准库脚本，不需要数据库、Redis、HTTP、SSH 或 RPC。

## 目录协议

```text
/persist/fshell/
├── inbox/<id>.ready          # READY，提交的最后一步
├── jobs/<id>/run.sh          # 任务脚本，上传完成后保持不动
├── running/<id>              # RUNNING，Worker 原子领取后的标记
├── done/<id>                 # 成功终态标记
├── failed/<id>               # FAILED 或 WORKER_LOST 终态标记
├── logs/<id>.log             # stdout
├── logs/<id>.stderr.log      # stderr
├── logs/<id>.status          # READY/RUNNING/SUCCEEDED/FAILED/WORKER_LOST
├── logs/<id>.exitcode        # 进程退出码；WORKER_LOST 为 -1
├── heartbeat                 # timestamp/hostname/pid/current_job
├── worker.log
└── worker.lock               # 防止同一目录同时启动两个 Worker
```

状态目录是唯一真相。`logs/<id>.status` 是给 GUI 轮询的状态快照，可以在 Worker 启动时由状态目录修复。

## 本机快速验证

```bash
cd PDShell
python3 -m unittest discover -s tests -v
```

启动 Worker：

```bash
python3 pdshell.py worker --root /tmp/fshell
```

另一个终端提交脚本：

```bash
cat > /tmp/hello.sh <<'SH'
#!/usr/bin/env bash
echo "hello from $(hostname)"
echo "stderr example" >&2
SH

python3 pdshell.py submit /tmp/hello.sh --root /tmp/fshell --job-id hello-001
tail -f /tmp/fshell/logs/hello-001.log
cat /tmp/fshell/logs/hello-001.status
```

平台外部提交时也可以不运行 `submit` 子命令，只要严格遵循两步协议：先完整上传 `jobs/<id>/run.sh`，最后创建 `inbox/<id>.ready`。不要反过来。

## Docker 接入

`Dockerfile.example` 可以直接构建独立演示镜像：

```bash
docker build -f Dockerfile.example -t pdshell-mvp .
docker run --rm -v /宿主机/持久化目录:/persist pdshell-mvp
```

接入已有训练镜像时，把 `pdshell.py` 和 `docker-entrypoint.sh` 复制到 `/opt/pdshell/`，并把 `docker-entrypoint.sh` 设为 Entrypoint。只依赖 Python 3 和 bash。

## 重启恢复语义

Worker 启动时先扫描 `running/`。其中每个任务都被原子移动到 `failed/`，状态写为 `WORKER_LOST`，退出码写为 `-1`。它绝不会猜测脚本是否已经执行完，也绝不会自动重跑。

这个 MVP 采用 **at-most-once（至多一次）** 语义：宁可让异常中断的训练等待人工判断，也不因 Docker 重启而重复启动训练。人工确认后若需重跑，应使用新的任务 ID 重新提交。

关键落盘顺序如下：

```text
上传 jobs/<id>/run.sh
        ↓ 最后创建 ready
inbox/<id>.ready
        ↓ os.replace 原子领取
running/<id>
        ↓ bash 在独立进程会话中执行
写 exitcode → 写终态标记内容 → os.replace 到 done/failed → 写 status
```

若 Worker 收到 SIGTERM/SIGINT，会终止当前脚本的整个进程组并将其记为 `FAILED`。若发生 SIGKILL、OOM 或整容器消失，下一次启动会通过残留的 `running/<id>` 将其记为 `WORKER_LOST`。

## MVP 边界

- 单 Worker、串行执行任务；文件锁阻止同一共享目录误启多个 Worker。
- 不提供优先级、DAG、GPU 调度、自动重试、取消队列或 Web GUI。
- `jobs/<id>` 已完整上传但尚未创建 `.ready` 时不算已提交任务，Worker 不会执行或清理它。
- GUI 每 2 秒读取 `.status` 和 `.log` 即可；连续 30 秒没有新 heartbeat 可判断 Worker 已失联。
- PDShell 会执行任意 Shell 命令，只应向受信任的用户开放共享目录写权限。
