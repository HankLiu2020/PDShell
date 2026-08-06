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
MAX_LOG_BYTES = 200 * 1024


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


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return default


def _marker_state(job_dir: Path) -> str:
    for marker in STATE_MARKERS:
        if (job_dir / marker).is_file():
            if marker == ".failed":
                first_line = _read_text(job_dir / marker).splitlines()[:1]
                return first_line[0] if first_line and first_line[0] == "WORKER_LOST" else "FAILED"
            if marker == ".done":
                return "SUCCEEDED"
            return marker[1:].upper()
    return "INCOMPLETE"


def snapshot_job(job_dir: Path) -> JobSnapshot:
    mtimes = []
    for path in (job_dir / name for name in STATE_MARKERS + ("exitcode", "log", "stderr.log")):
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            pass
    return JobSnapshot(
        job_id=job_dir.name,
        state=_marker_state(job_dir),
        exitcode=_read_text(job_dir / "exitcode").strip(),
        updated_at=max(mtimes, default=0.0),
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
    return sorted((snapshot_job(path) for path in jobs), key=lambda item: item.job_id)


def worker_summary(root: Path, offline_after: float = 30.0) -> str:
    heartbeat = root / "heartbeat"
    values = parse_heartbeat(heartbeat)
    if not values:
        return "**Worker：🔴 OFFLINE**  · 未发现 heartbeat"
    try:
        age = max(0.0, time.time() - float(values.get("timestamp", "0")))
    except ValueError:
        age = offline_after + 1
    state = "🟢 ONLINE" if age <= offline_after else "🔴 OFFLINE"
    current = values.get("current_job") or "空闲"
    return f"**Worker：{state}**  · heartbeat {age:.1f}s 前  · 当前任务：`{current}`"


class FileTransport:
    read_only = False

    def sync_metadata(self) -> None:
        raise NotImplementedError

    def sync_job(self, job_id: str) -> None:
        raise NotImplementedError

    def root(self) -> Path:
        raise NotImplementedError

    def submit_script(self, script: Path, job_id: str | None = None) -> str:
        raise NotImplementedError

    def snapshots(self) -> list[JobSnapshot]:
        return list_snapshots(self.root())

    def logs(self, job_id: str) -> tuple[str, str]:
        job_dir = self.root() / job_id
        return tail_text(job_dir / "log"), tail_text(job_dir / "stderr.log")

    def health(self) -> str:
        return worker_summary(self.root())


class LocalTransport(FileTransport):
    def __init__(self, root: Path):
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
            command.extend(["-e", f"ssh -p {self.ssh_port} -o ServerAliveInterval=30"])
        if self.password_enabled:
            command = ["sshpass", "-e", *command]
        return command

    def _run(self, args: Sequence[str]) -> None:
        try:
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
        args = self._rsync_command() + [
            "-a",
            "--include=*/",
            "--include=heartbeat",
            "--include=.ready",
            "--include=.running",
            "--include=.done",
            "--include=.failed",
            "--include=exitcode",
            "--exclude=*",
            self._remote(),
            str(self._cache) + "/",
        ]
        self._run(args)

    def sync_job(self, job_id: str) -> None:
        self._cache.mkdir(parents=True, exist_ok=True)
        destination = self._cache / job_id
        destination.mkdir(parents=True, exist_ok=True)
        for name in (".ready", ".running", ".done", ".failed", "exitcode", "log", "stderr.log"):
            try:
                self._run(
                    self._rsync_command()
                    + ["-a", self._remote(f"{job_id}/{name}"), str(destination / name)]
                )
            except TransportError:
                continue

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
            self._run(self._rsync_command() + ["-a", str(script), self._remote(f"{job_id}.sh")])
            self._run(
                self._rsync_command()
                + [
                    "-a",
                    "--exclude=.ready",
                    str(staging) + "/",
                    self._remote(f"{job_id}/"),
                ]
            )
            self._run(
                self._rsync_command() + ["-a", str(ready), self._remote(f"{job_id}/.ready")]
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
