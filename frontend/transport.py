from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

CONTROL_NAMES = {"heartbeat", "worker.log", "worker.lock"}
STATE_MARKERS = (".done", ".failed", ".running", ".ready")
DELETE_MARKER = ".delete"
MAX_LOG_BYTES = 200 * 1024
SUBMIT_CHMOD = "Du=rwx,Dgo=rwx,Fu=rw,Fgo=rw"
NFS_RSYNC_FLAGS = ("--no-owner", "--no-group", "--no-perms", "--no-times")


class TransportError(RuntimeError):
    pass


def validate_job_id(job_id: str) -> str:
    from pdshell import validate_job_id as worker_validate_job_id

    try:
        return worker_validate_job_id(job_id)
    except ValueError as exc:
        raise TransportError(str(exc)) from exc


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: str
    exitcode: str
    updated_at: float
    submitted_at: float = 0.0


class HeartbeatTracker:
    def __init__(self, clock: Callable[[], float] | None = None):
        self.clock = clock or time.monotonic
        self.last_timestamp: str | None = None
        self.last_progress_at: float | None = None

    def observe(self, values: dict[str, str]) -> float | None:
        timestamp = values.get("timestamp")
        now = self.clock()
        if not timestamp:
            self.last_timestamp = None
            self.last_progress_at = None
            return None
        if timestamp != self.last_timestamp or self.last_progress_at is None:
            self.last_timestamp = timestamp
            self.last_progress_at = now
        return max(0.0, now - self.last_progress_at)


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return default


def _atomic_write_marker(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o666)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _marker_state(job_dir: Path) -> str:
    running = (job_dir / ".running").is_file()
    deleting = (job_dir / DELETE_MARKER).is_file()
    if running and deleting:
        return "RUNNING"
    if deleting:
        return "DELETING"
    for marker in (".done", ".failed", ".running", ".ready"):
        if (job_dir / marker).is_file():
            if marker == ".failed":
                first_line = _read_text(job_dir / marker).splitlines()[:1]
                return first_line[0] if first_line and first_line[0] == "WORKER_LOST" else "FAILED"
            if marker == ".done":
                return "SUCCEEDED"
            return marker[1:].upper()
    return "INCOMPLETE"


def _submitted_at(job_dir: Path, fallback: float) -> float:
    for marker in STATE_MARKERS + (DELETE_MARKER,):
        content = _read_text(job_dir / marker)
        for line in content.splitlines():
            if not line.startswith("submitted_at="):
                continue
            try:
                return float(line.split("=", 1)[1])
            except ValueError:
                break
    try:
        return job_dir.stat().st_mtime or fallback
    except (FileNotFoundError, OSError):
        return fallback


def snapshot_job(job_dir: Path) -> JobSnapshot:
    mtimes = []
    for path in (
        job_dir / name for name in STATE_MARKERS + (DELETE_MARKER, "exitcode", "log", "stderr.log")
    ):
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            pass
    updated_at = max(mtimes, default=0.0)
    return JobSnapshot(
        job_id=job_dir.name,
        state=_marker_state(job_dir),
        exitcode=_read_text(job_dir / "exitcode").strip(),
        updated_at=updated_at,
        submitted_at=_submitted_at(job_dir, updated_at),
    )


def tail_text(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        return f"[仅显示最后 {max_bytes // 1024} KB]\n" + text
    return text


def parse_heartbeat(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_text(path).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def list_snapshots(root: Path) -> list[JobSnapshot]:
    if not root.is_dir():
        return []
    jobs = [
        path
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and path.name not in CONTROL_NAMES
    ]
    return sorted(
        (snapshot_job(path) for path in jobs),
        key=lambda item: (item.submitted_at, item.updated_at, item.job_id),
        reverse=True,
    )


def worker_summary(
    root: Path,
    offline_after: float = 30.0,
    tracker: HeartbeatTracker | None = None,
) -> str:
    heartbeat = root / "heartbeat"
    values = parse_heartbeat(heartbeat)
    if not values:
        if tracker is not None:
            tracker.observe({})
        return "**Worker：🔴 OFFLINE**  · 未发现 heartbeat"
    heartbeat_tracker = tracker or HeartbeatTracker()
    age = heartbeat_tracker.observe(values)
    if age is None:
        return "**Worker：🔴 OFFLINE**  · heartbeat 没有有效 timestamp"
    state = "🟢 ONLINE" if age <= offline_after else "🔴 OFFLINE"
    current = values.get("current_job") or "空闲"
    server_time = values.get("iso_time") or values.get("timestamp") or "未知"
    return (
        f"**Worker：{state}**  · heartbeat {age:.1f}s 前  · "
        f"服务器时间：`{server_time}`  · 当前任务：`{current}`"
    )


class FileTransport:
    read_only = False

    def __init__(self):
        self._heartbeat_tracker = HeartbeatTracker()

    def sync_metadata(self) -> None:
        raise NotImplementedError

    def sync_job(self, job_id: str) -> None:
        raise NotImplementedError

    def root(self) -> Path:
        raise NotImplementedError

    def submit_script(self, script: Path, job_id: str | None = None) -> str:
        raise NotImplementedError

    def delete_job(self, job_id: str) -> None:
        raise NotImplementedError

    def snapshots(self) -> list[JobSnapshot]:
        return list_snapshots(self.root())

    def logs(self, job_id: str) -> tuple[str, str]:
        job_dir = self.root() / job_id
        return tail_text(job_dir / "log"), tail_text(job_dir / "stderr.log")

    def health(self) -> str:
        return worker_summary(self.root(), tracker=self._heartbeat_tracker)


class LocalTransport(FileTransport):
    def __init__(self, root: Path):
        super().__init__()
        self._root = root.expanduser().resolve()

    def root(self) -> Path:
        return self._root

    def sync_metadata(self) -> None:
        return None

    def sync_job(self, job_id: str) -> None:
        return None

    def submit_script(self, script: Path, job_id: str | None = None) -> str:
        from pdshell import submit

        return submit(self._root, script, job_id)

    def delete_job(self, job_id: str) -> None:
        validate_job_id(job_id)
        job_dir = self._root / job_id
        if (job_dir / ".running").is_file():
            raise TransportError(f"任务正在运行，拒绝删除: {job_id}")
        if job_dir.exists() and job_dir.is_symlink():
            raise TransportError(f"任务目录不能是符号链接: {job_id}")
        if not job_dir.is_dir():
            raise TransportError(f"任务不存在或不是目录: {job_id}")
        _atomic_write_marker(
            job_dir / DELETE_MARKER,
            f"DELETE\nrequested_at={time.time():.6f}\n",
        )


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _is_remote_endpoint(endpoint: str) -> bool:
    return ":" in endpoint


class RsyncTransport(FileTransport):
    def __init__(
        self,
        remote: str,
        cache: Path,
        runner: CommandRunner | None = None,
        ssh_port: int = 22,
        password: str | None = None,
    ):
        super().__init__()
        self.remote = remote.rstrip("/")
        self._cache = cache.expanduser().resolve()
        self.runner = runner
        self.ssh_port = ssh_port
        self.environment = os.environ.copy()
        if password:
            self.environment["SSHPASS"] = password
        self.password_enabled = bool(password)

    def root(self) -> Path:
        return self._cache

    def _remote(self, relative: str = "") -> str:
        return f"{self.remote}/{relative}" if relative else f"{self.remote}/"

    def _rsync_command(self) -> list[str]:
        command = ["rsync"]
        if _is_remote_endpoint(self.remote):
            command.extend(
                [
                    "-e",
                    (
                        f"ssh -p {self.ssh_port} -o ServerAliveInterval=30 "
                        "-o ControlMaster=auto -o ControlPersist=60 "
                        "-o ControlPath=~/.ssh/pdshell-%C"
                    ),
                ]
            )
        if self.password_enabled:
            command = ["sshpass", "-e", *command]
        return command

    def _run(self, args: Sequence[str]) -> None:
        try:
            if self.runner is not None:
                result = self.runner(args)
                if result.returncode != 0:
                    raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
            else:
                subprocess.run(
                    args,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=self.environment,
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise TransportError(f"传输失败: {' '.join(args)}") from exc

    def _job_exists(self, job_id: str) -> bool:
        args = self._rsync_command() + [
            "--list-only",
            f"--include=/{job_id}/",
            f"--include=/{job_id}.sh",
            "--exclude=*",
            self._remote(),
        ]
        try:
            if self.runner is not None:
                result = self.runner(args)
            else:
                result = subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=self.environment,
                )
        except subprocess.CalledProcessError as exc:
            result = subprocess.CompletedProcess(args, exc.returncode, exc.stdout, exc.stderr)
        except OSError as exc:
            raise TransportError(f"远端任务预检失败: {' '.join(args)}") from exc
        if result.returncode != 0:
            raise TransportError(f"远端任务预检失败，rsync exitcode={result.returncode}")
        expected_names = {job_id, f"{job_id}.sh"}
        listed_names = {
            line.rsplit(maxsplit=1)[-1]
            for line in result.stdout.splitlines()
            if line.split()
        }
        return bool(expected_names & listed_names)

    def sync_metadata(self) -> None:
        self._cache.mkdir(parents=True, exist_ok=True)
        heartbeat_args = self._rsync_command() + [
            "-a",
            "--no-times",
            "--include=heartbeat",
            "--exclude=*",
            self._remote(),
            str(self._cache) + "/",
        ]
        self._run(heartbeat_args)
        args = self._rsync_command() + [
            "-a",
            "--include=*/",
            "--include=heartbeat",
            "--include=.ready",
            "--include=.running",
            "--include=.done",
            "--include=.failed",
            "--include=.delete",
            "--include=exitcode",
            "--delete",
            "--exclude=*",
            self._remote(),
            str(self._cache) + "/",
        ]
        self._run(args)

    def _sync_job(self, job_id: str, tolerate_errors: bool) -> None:
        self._cache.mkdir(parents=True, exist_ok=True)
        destination = self._cache / job_id
        destination.mkdir(parents=True, exist_ok=True)
        args = self._rsync_command() + [
            "-a",
            "--include=*/",
            "--include=.ready",
            "--include=.running",
            "--include=.done",
            "--include=.failed",
            "--include=.delete",
            "--include=exitcode",
            "--include=log",
            "--include=stderr.log",
            "--delete",
            "--exclude=*",
            self._remote(f"{job_id}/"),
            str(destination) + "/",
        ]
        try:
            self._run(args)
        except TransportError:
            if not tolerate_errors:
                raise

    def sync_job(self, job_id: str) -> None:
        self._sync_job(job_id, tolerate_errors=True)

    def delete_job(self, job_id: str) -> None:
        validate_job_id(job_id)
        if _is_remote_endpoint(self.remote):
            if not self._job_exists(job_id):
                raise TransportError(f"任务不存在，拒绝删除: {job_id}")
            self._sync_job(job_id, tolerate_errors=False)
            remote_job_dir = None
        else:
            remote_job_dir = Path(self.remote) / job_id
            if (remote_job_dir / ".running").is_file():
                raise TransportError(f"任务正在运行，拒绝删除: {job_id}")
            if remote_job_dir.exists() and remote_job_dir.is_symlink():
                raise TransportError(f"任务目录不能是符号链接: {job_id}")
            if not remote_job_dir.is_dir():
                raise TransportError(f"任务不存在或不是目录: {job_id}")

        cached_job = self._cache / job_id
        if (cached_job / ".running").is_file():
            raise TransportError(f"任务正在运行，拒绝删除: {job_id}")
        cached_job.mkdir(parents=True, exist_ok=True)
        marker_content = f"DELETE\nrequested_at={time.time():.6f}\n"
        cached_marker = cached_job / DELETE_MARKER
        _atomic_write_marker(cached_marker, marker_content)
        if remote_job_dir is not None:
            _atomic_write_marker(remote_job_dir / DELETE_MARKER, marker_content)
        else:
            try:
                self._run(
                    self._rsync_command()
                    + [
                        "-a",
                        *NFS_RSYNC_FLAGS,
                        f"--chmod={SUBMIT_CHMOD}",
                        str(cached_marker),
                        self._remote(f"{job_id}/{DELETE_MARKER}"),
                    ]
                )
            except TransportError:
                try:
                    cached_marker.unlink()
                except FileNotFoundError:
                    pass
                raise

    def submit_script(self, script: Path, job_id: str | None = None) -> str:
        if job_id is None:
            job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(4).hex()
        validate_job_id(job_id)
        if not script.is_file():
            raise FileNotFoundError(f"脚本不存在: {script}")
        if self._job_exists(job_id):
            raise TransportError(f"任务 ID 已存在，拒绝重复提交: {job_id}")
        with tempfile.TemporaryDirectory(prefix="pdshell-submit-") as temporary:
            staging = Path(temporary)
            shutil.copyfile(script, staging / "run.sh")
            ready = staging / ".ready"
            ready.write_text(f"READY\nsubmitted_at={time.time():.6f}\n", encoding="utf-8")
            self._run(
                self._rsync_command()
                + [
                    "-a",
                    *NFS_RSYNC_FLAGS,
                    f"--chmod={SUBMIT_CHMOD}",
                    str(script),
                    self._remote(f"{job_id}.sh"),
                ]
            )
            self._run(
                self._rsync_command()
                + [
                    "-a",
                    *NFS_RSYNC_FLAGS,
                    f"--chmod={SUBMIT_CHMOD}",
                    "--exclude=.ready",
                    str(staging) + "/",
                    self._remote(f"{job_id}/"),
                ]
            )
            self._run(
                self._rsync_command()
                + [
                    "-a",
                    *NFS_RSYNC_FLAGS,
                    f"--chmod={SUBMIT_CHMOD}",
                    str(ready),
                    self._remote(f"{job_id}/.ready"),
                ]
            )
        return job_id


class ScpTransport(FileTransport):
    read_only = True

    def __init__(
        self,
        remote: str,
        cache: Path,
        runner: CommandRunner | None = None,
        ssh_port: int = 22,
        password: str | None = None,
    ):
        super().__init__()
        self.remote = remote.rstrip("/")
        self._cache = cache.expanduser().resolve()
        self.runner = runner
        self.ssh_port = ssh_port
        self.environment = os.environ.copy()
        if password:
            self.environment["SSHPASS"] = password
        self.password_enabled = bool(password)

    def root(self) -> Path:
        return self._cache

    def _scp_command(self) -> list[str]:
        command = ["scp"]
        if _is_remote_endpoint(self.remote):
            command.extend(["-P", str(self.ssh_port)])
        if self.password_enabled:
            command = ["sshpass", "-e", *command]
        return command

    def _run(self, args: Sequence[str]) -> None:
        if self.runner is not None:
            self.runner(args)
        else:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                env=self.environment,
            )

    def sync_metadata(self) -> None:
        self._cache.mkdir(parents=True, exist_ok=True)
        try:
            self._run(self._scp_command() + ["-q", "-r", f"{self.remote}/.", str(self._cache)])
        except (OSError, subprocess.CalledProcessError) as exc:
            raise TransportError("读取 scp 目录失败；请改用 rsync 或确认远端路径") from exc

    def sync_job(self, job_id: str) -> None:
        destination = self._cache / job_id
        destination.mkdir(parents=True, exist_ok=True)
        try:
            self._run(
                self._scp_command()
                + ["-q", "-r", f"{self.remote}/{job_id}/.", str(destination)]
            )
        except (OSError, subprocess.CalledProcessError):
            return

    def submit_script(self, script: Path, job_id: str | None = None) -> str:
        raise TransportError("scp 模式只读，请切换为 rsync 后提交任务")

    def delete_job(self, job_id: str) -> None:
        raise TransportError("scp 模式只读，不能删除任务")


def make_transport(
    mode: str,
    endpoint: str,
    cache: Path,
    ssh_port: int = 22,
    password: str | None = None,
) -> FileTransport:
    if mode == "local":
        return LocalTransport(Path(endpoint))
    if mode == "rsync":
        return RsyncTransport(endpoint, cache, ssh_port=ssh_port, password=password)
    if mode == "scp":
        return ScpTransport(endpoint, cache, ssh_port=ssh_port, password=password)
    raise ValueError(f"不支持的传输模式: {mode}")
