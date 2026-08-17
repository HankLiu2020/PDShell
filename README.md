# PDShell

PDShell（Persistent Directory Shell）把一个没有 SSH 或交互终端的长期运行容器变成基于共享目录的异步 Shell：外部完整上传任务脚本，最后创建 `.ready`；容器内 Worker 领取并执行脚本，把状态、日志和退出码写回同一个任务文件夹。

运行时只有 Python 标准库、Bash 和文件系统。不需要数据库、Redis、HTTP、SSH 服务端、RPC、优先级、DAG 或资源调度。

## v2 目录协议

服务器直接运行时，默认根目录是 PDShell 脚本所在目录旁的 `tasks/`；Docker 示例通过环境变量使用 `/persist/tasks`。每个任务只使用自己的文件夹，状态切换全部发生在同一目录内：

```text
$PDSHELL_ROOT/
├── <id>.sh                  # 外部脚本的审计副本，可选；Worker 不执行此文件
├── <id>/
│   ├── run.sh               # 完整落盘后执行的脚本副本
│   ├── .ready               # READY：提交最后一步
│   ├── .running             # RUNNING：Worker 原子领取
│   ├── .done                # SUCCEEDED 终态
│   ├── .failed              # FAILED 或 WORKER_LOST 终态
│   ├── log                  # stdout
│   ├── stderr.log           # stderr
│   └── exitcode             # 退出码；WORKER_LOST 为 -1
├── heartbeat                # Worker 心跳
├── worker.log               # Worker 日志
└── worker.lock              # 单 Worker flock
```

同一时刻正常情况下每个任务目录只有一个状态文件。`.ready` 以及后续 marker 会保留 `submitted_at=<Unix 时间戳>`，供控制台按提交时间倒序展示；没有该字段的手工任务回退到目录更新时间。状态真相是 marker 文件名，而不是缓存快照：

```text
.ready → .running → .done
                  └→ .failed
```

客户端推导状态时使用终态优先顺序：`.done` → `.failed` → `.running` → `.ready` → `INCOMPLETE`。`.failed` 的首行是 `FAILED` 或 `WORKER_LOST`。

## Worker

```bash
bash docker-entrypoint.sh
```

`docker-entrypoint.sh` 会从任意当前工作目录定位自身和 `pdshell.py`，默认把任务写入脚本旁的 `tasks/`。`python3 pdshell.py worker` 和 `submit` 使用同一默认值；`PDSHELL_ROOT` 或显式 `--root` 均可覆盖。

Worker 启动时会拒绝检测到的旧版 `inbox/jobs/running/done/failed/logs/rejected` 分桶，避免把旧数据静默当成新协议。新部署请使用新的空任务根目录。

提交命令会复制脚本并在最后创建 ready：

```bash
python3 pdshell.py submit ./train.sh \
  --root /tmp/tasks \
  --job-id train-001
```

等价的纯文件提交顺序如下；外部系统不需要调用 Python：

```bash
job_id=train-002
root=/persist/tasks

mkdir -p "$root/$job_id"
cp /path/to/run.sh "$root/$job_id/run.sh"
cp /path/to/run.sh "$root/$job_id.sh"

# run.sh 完整落盘后，最后在任务目录内原子创建 .ready。
printf 'READY\n' > "$root/$job_id/.ready.tmp"
mv "$root/$job_id/.ready.tmp" "$root/$job_id/.ready"
```

Worker 只执行 `<id>/run.sh`；根目录 `<id>.sh` 仅供审计和下载，不是自动入队入口。

## 恢复与幂等

- Worker 用 `<id>/.ready → <id>/.running` 原子领取任务。
- 任务完成时先写 `exitcode`，再把 `.running` 原子移动到 `.done` 或 `.failed`。
- 重启发现遗留 `.running` 时写入 `exitcode=-1` 和 `WORKER_LOST`，移动到 `.failed`，不重新执行。
- 已有终态的重复 `.ready` 会被消费并只记录一次告警，不会重复执行或无限刷日志。
- 非法任务 ID 的 `.ready` 会转成同目录 `.failed`，退出码为 `2`。
- Worker 收到 SIGTERM/SIGINT 时终止任务进程组；约 10 秒后仍未退出则升级为 SIGKILL。
- 平台停止宽限期建议至少 30 秒，并且只有在旧容器/cgroup 完全退出后才能启动替代 Worker。

这套语义是 **at-most-once（至多一次）**：宁可将异常中断任务标记为 `WORKER_LOST` 等待人工判断，也不因重启重复训练。没有 `.ready` 的半上传任务会保持 `INCOMPLETE`，不会执行。

## Shell 客户端

`pdshell_client.sh` 是不依赖 Python 提交命令的文件传输客户端：

```bash
export PDSHELL_REMOTE=user@host:/persist/tasks
export PDSHELL_TRANSPORT=rsync
export PDSHELL_SSH_PORT=22

./pdshell_client.sh submit train.sh train-003
./pdshell_client.sh list
./pdshell_client.sh status train-003
./pdshell_client.sh logs train-003 stdout
./pdshell_client.sh watch train-003
```

rsync 模式提交前会检查远端 `<id>/` 和 `<id>.sh`，任一已经存在就拒绝，覆盖 READY、RUNNING、终态以及没有 marker 的半提交任务。检查通过后按“审计脚本 → `run.sh` → `.ready`”顺序上传，并依赖 rsync 接收端临时文件加 rename。这个预检用于阻止常见的重复提交，不是跨客户端事务锁；多个客户端同时提交同一新 ID 仍属于不支持的竞争场景，应使用客户端生成的随机 ID。scp 模式是只读监控模式；提交按钮和 `submit` 命令会被禁用。当前 scp 模式为兼容性实现，会拉取远端任务目录，适合小规模监控；任务较多或日志较大时应使用 rsync。客户端使用本地缓存，不在仓库或界面保存 SSH 密码。

也可以把连接拆成环境变量，适配非 22 端口：

```bash
export PDSHELL_SSH_HOST=cluster.example
export PDSHELL_SSH_USER=user
export PDSHELL_SSH_PORT=30901
export PDSHELL_REMOTE_ROOT=/path/to/PDShell/tasks

# 仅在无法使用 SSH key 时设置；要求本机安装 sshpass。
export PDSHELL_SSH_PASSWORD='从安全环境注入，不要写进脚本或 Git'
```

密码使用 `sshpass -e` 从进程环境读取，不出现在命令参数中。优先使用 SSH key；`.env` 已加入 `.gitignore`，但仍不应把凭据保存在项目目录。

## 同步到服务器

`sync_to_server.sh` 可把工程同步到普通 Linux 服务器，同时排除 `.git/`、`tasks/`、缓存、`.env` 和同步记录：

```bash
export PDSHELL_DEPLOY_TARGET=user@host:/path/to/PDShell
export PDSHELL_SSH_PORT=30901
./sync_to_server.sh
```

同步后脚本会修复 `docker-entrypoint.sh`、`pdshell.py` 和 `pdshell_client.sh` 的执行权限，并打印服务器端 `nohup` 启动提示。`.last_sync_target` 只是本地运行缓存，已被 Git 忽略。

集群连接参数可以放在仓库外的 `env.sh` 中，`env.sh` 已被 Git 和同步脚本排除；公开仓库只提供不含凭据的 [env.sh.example](env.sh.example) 模板。优先使用 SSH key，密码只从安全环境注入。

## NFS 多用户权限

Worker 入口使用 `umask 000`，状态文件和审计副本使用 `0666`，客户端提交通过 rsync `--chmod` 将任务目录和文件放宽到共享盘上其他 UID 可读写。这是为“Worker UID 与提交者 UID 不同”的 NFS 场景准备的；已有旧任务目录需要在停 Worker 后由管理员一次性修复权限，例如：

```bash
chmod -R 777 /path/to/PDShell/tasks
```

这条命令会放宽任务目录权限，只应在受控共享目录使用。真实 NFS 的 root-squash、UID 映射和删除权限仍需按平台策略验证。

## Gradio 控制台

Gradio 运行在用户自己的机器上，不在训练容器中启动远端服务。界面保持三个区域：左上上传脚本，左下 Worker 和任务监控，右侧选中任务的 stdout/stderr。

安装并启动本地目录模式：

```bash
python3 -m pip install -r frontend/requirements.txt
python3 frontend/app.py --mode local --endpoint /persist/tasks
```

rsync 模式：

```bash
python3 frontend/app.py \
  --mode rsync \
  --endpoint user@host:/persist/tasks \
  --ssh-port 30901 \
  --cache ~/.cache/pdshell
```

Gradio 与 Shell 客户端共用 `PDSHELL_SSH_*`、`PDSHELL_REMOTE_ROOT` 和可选 `PDSHELL_SSH_PASSWORD`。不传 `--endpoint` 时，本地模式默认使用仓库旁的 `tasks/`。Gradio 4.44–6.x 均以 `<7` 依赖范围支持；日志使用 Code 组件显示，兼容 Gradio 6 的交互行为。

控制台每 2 秒刷新 heartbeat 和任务 marker，heartbeat 使用本地接收 mtime 判断在线状态，并额外展示 Worker 写入的服务器时间，避免前后端时钟偏差造成误判。选中任务后一次 rsync 拉取该任务目录，SSH 使用 ControlMaster 复用连接；同步失败时继续显示已有缓存，不让整个表格刷新崩溃。界面只渲染日志末尾约 200 KB，完整内容保留在本地缓存。heartbeat 超过 30 秒显示 OFFLINE，但不会替远端任务修改状态。Gradio 默认只绑定 `127.0.0.1`，不启用公开分享链接。

提交区既支持上传 `.sh` 文件，也支持直接粘贴脚本。上传文件且任务 ID 留空时，默认使用“文件名去扩展名 + `YYYYMMDD-HHMMSS`”；手填 ID 优先。任务表按提交时间从新到旧显示，最右侧显示删除操作；先选中任务、勾选确认，再点击删除。删除会同时移除远端任务目录和 `<id>.sh` 审计副本，并拒绝删除 `.running` 任务；scp 只读模式不提供提交和删除。

## Docker 接入

`Dockerfile.example` 可用于构建演示镜像：

```bash
docker build -f Dockerfile.example -t pdshell-mvp .
docker run --rm -v /宿主机/持久化目录:/persist pdshell-mvp
```

接入已有训练镜像时，把 `pdshell.py` 和 `docker-entrypoint.sh` 复制到 `/opt/pdshell/`，并把 entrypoint 设为容器 Entrypoint。可通过 `PDSHELL_ROOT` 覆盖默认 `/persist/tasks`。

`Dockerfile.train` 是不含集群凭据的训练镜像接入示例，可通过 `--build-arg BASE_IMAGE=<已有训练镜像>` 替换基础镜像；真实 GPU runtime、原有训练 entrypoint 和持久化挂载仍由平台配置。

Docker 镜像、PID 1、cgroup、GPU runtime、OOM、bind mount 和宿主机重启尚未在真实训练平台验证。NFS、CIFS、CephFS 等目标共享存储也必须单独实测 `rename`、`flock`、权限和缓存一致性。

## 验证状态

当前本地动态验证包含 32 项测试，覆盖：

- v2 任务目录成功/失败闭环、纯文件提交、20 个批量任务串行执行。
- 单 marker、审计副本、缺脚本、重复 ID、非法 ID、旧布局拒绝。
- `.ready` 重复消费、`.running` 恢复为 `WORKER_LOST`、SIGTERM 进程组终止和单 Worker 锁。
- rsync 本地提交与缓存同步、scp 只读限制、日志尾部截断和 Shell 客户端闭环。
- 非 22 SSH 端口、sshpass 参数脱敏、任意工作目录入口启动和环境变量默认根目录。
- rsync 重复 ID 预检覆盖 READY、RUNNING、终态、半提交目录和审计副本；预检传输错误不会按“不存在”放行。
- NFS 多 UID 文件权限、heartbeat mtime 在线判断、服务器时间展示、一次任务目录同步和 SSH ControlMaster 参数。
- 工程同步排除运行数据、脚本执行权限和全仓库 LF 行尾检查。
- Gradio 粘贴脚本、上传文件名任务 ID、提交时间排序和任务删除安全闸门。

运行测试：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile pdshell.py frontend/transport.py frontend/app.py
bash -n docker-entrypoint.sh pdshell_client.sh sync_to_server.sh env.sh.example
```

仓库通过 `.gitattributes` 强制 Python、Shell、Markdown 和 Dockerfile 使用 LF，避免 Windows/IDE 的 CRLF 转换制造整文件伪 diff。

当前仍未验证真实 Docker/PID 1 生命周期、目标 NFS/CephFS、多节点缓存、UID/GID、OOM 和平台停止重建行为。

## 边界

- 单 Worker、串行执行；不保证提交时间 FIFO 或全局字典序。
- 不提供优先级、DAG、GPU 调度、自动重试、取消队列或多服务器聚合。
- 自定义任务 ID 的冲突检查是尽力而为；建议由客户端生成时间戳加随机串的 ID。
- 只应向受信任用户开放共享目录写权限，并尽量使用非 root 用户运行。
