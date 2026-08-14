#!/usr/bin/env python3
"""PDShell: a tiny filesystem-backed asynchronous shell worker."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

JOB_ID_RE = re.compile(r"^(?!.*\.sh$)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RESERVED_JOB_IDS = {
    "heartbeat",
    "worker.log",
    "worker.lock",
    "inbox",
    "jobs",
    "running",
    "done",
    "failed",
    "logs",
    "rejected",
}
LEGACY_LAYOUT_NAMES = {"inbox", "jobs", "running", "done", "failed", "logs", "rejected"}
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = Path(os.environ.get("PDSHELL_ROOT", SCRIPT_DIR / "tasks"))


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(
            "job id 只能以字母或数字开头，只能包含字母、数字、点、下划线和短横线，"
            "不能以 .sh 结尾，且长度不超过 128"
        )
    if job_id in RESERVED_JOB_IDS:
        raise ValueError(f"job id 保留给 PDShell 控制文件: {job_id}")
    return job_id


class Layout:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.heartbeat = self.root / "heartbeat"
        self.worker_log = self.root / "worker.log"
        self.worker_lock = self.root / "worker.lock"

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        legacy = sorted(name for name in LEGACY_LAYOUT_NAMES if (self.root / name).exists())
        if legacy:
            joined = ", ".join(legacy)
            raise RuntimeError(f"检测到旧版分桶目录: {joined}；请清空或使用新的 tasks 目录")

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def source_script(self, job_id: str) -> Path:
        return self.root / f"{job_id}.sh"

    def marker(self, job_id: str, state: str) -> Path:
        return self.job_dir(job_id) / f".{state}"

    def ready(self, job_id: str) -> Path:
        return self.marker(job_id, "ready")

    def running(self, job_id: str) -> Path:
        return self.marker(job_id, "running")

    def done(self, job_id: str) -> Path:
        return self.marker(job_id, "done")

    def failed(self, job_id: str) -> Path:
        return self.marker(job_id, "failed")

    def stdout(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "log"

    def stderr(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stderr.log"

    def exitcode(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "exitcode"


def atomic_write(path: Path, content: str, mode: int = 0o666) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def copy_file_fsync(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, destination.open("wb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    os.chmod(destination, mode)


def submit(root: Path, script: Path, job_id: str | None = None) -> str:
    layout = Layout(root)
    layout.create()
    if job_id is None:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    validate_job_id(job_id)

    if not script.is_file():
        raise FileNotFoundError(f"脚本不存在: {script}")

    job_dir = layout.job_dir(job_id)
    source_copy = layout.source_script(job_id)
    if job_dir.exists() or source_copy.exists():
        raise FileExistsError(f"任务 ID 已存在: {job_id}")

    token = f"{os.getpid()}.{uuid.uuid4().hex}"
    temp_dir = layout.root / f".{job_id}.{token}.tmp"
    temp_source = layout.root / f".{job_id}.sh.{token}.tmp"
    published_dir = False
    published_source = False
    try:
        temp_dir.mkdir()
        copy_file_fsync(script, temp_dir / "run.sh", 0o755)
        copy_file_fsync(script, temp_source, 0o666)
        os.replace(temp_source, source_copy)
        published_source = True
        os.replace(temp_dir, job_dir)
        published_dir = True
        atomic_write(layout.ready(job_id), f"READY\nsubmitted_at={time.time():.6f}\n")
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        if temp_source.exists():
            temp_source.unlink()
        if published_dir and not any(job_dir.glob(".*")):
            shutil.rmtree(job_dir)
        if published_source and not job_dir.exists():
            try:
                source_copy.unlink()
            except FileNotFoundError:
                pass
        raise

    return job_id


class Worker:
    def __init__(self, root: Path, poll_interval: float = 1.0):
        self.layout = Layout(root)
        self.poll_interval = poll_interval
        self.hostname = socket.gethostname()
        self.current_job = ""
        self.current_process: subprocess.Popen[bytes] | None = None
        self.stop_requested = False
        self.lock_handle = None

    def log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.layout.worker_log.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
            handle.flush()

    def acquire_lock(self) -> None:
        self.lock_handle = self.layout.worker_lock.open("a+")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有一个 PDShell Worker 正在使用该目录") from exc
        self.lock_handle.seek(0)
        self.lock_handle.truncate()
        self.lock_handle.write(f"pid={os.getpid()}\nhostname={self.hostname}\n")
        self.lock_handle.flush()

    def write_heartbeat(self) -> None:
        now = time.time()
        atomic_write(
            self.layout.heartbeat,
            (
                f"timestamp={now:.6f}\n"
                f"iso_time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                f"hostname={self.hostname}\n"
                f"pid={os.getpid()}\n"
                f"current_job={self.current_job}\n"
            ),
        )

    def recover(self) -> None:
        recovered = 0
        for marker in sorted(self.layout.root.glob("*/.running")):
            if not marker.is_file():
                continue
            job_id = marker.parent.name
            failed = self.layout.failed(job_id)
            done = self.layout.done(job_id)
            if done.exists() or failed.exists():
                marker.unlink()
                self.log(f"清理冲突的遗留 running 标记: {job_id}")
                continue
            atomic_write(self.layout.exitcode(job_id), "-1\n")
            atomic_write(marker, "WORKER_LOST\n")
            os.replace(marker, failed)
            self.log(f"恢复任务 {job_id}: RUNNING -> WORKER_LOST")
            recovered += 1

        if recovered == 0:
            self.log("启动恢复扫描完成: 没有遗留 RUNNING 任务")

    def consume_duplicate_ready(self, ready: Path, message: str) -> None:
        try:
            ready.unlink()
        except FileNotFoundError:
            return
        self.log(message)

    def reject_invalid_ready(self, ready: Path, job_id: str) -> None:
        failed = ready.parent / ".failed"
        if failed.exists() or (ready.parent / ".done").exists():
            self.consume_duplicate_ready(ready, f"清理已有终态的非法 ready: {job_id}")
            return
        atomic_write(ready.parent / "stderr.log", f"PDShell 拒绝非法任务 ID: {job_id}\n")
        atomic_write(ready.parent / "exitcode", "2\n")
        atomic_write(ready, "FAILED\nreason=INVALID_JOB_ID\n")
        os.replace(ready, failed)
        self.log(f"非法 ready 已转为 FAILED: {job_id}")

    def claim(self, ready: Path) -> str | None:
        if ready.name != ".ready" or not ready.is_file():
            return None
        job_id = ready.parent.name
        try:
            validate_job_id(job_id)
        except ValueError:
            self.reject_invalid_ready(ready, job_id)
            return None

        done = self.layout.done(job_id)
        failed = self.layout.failed(job_id)
        running = self.layout.running(job_id)
        if done.exists() or failed.exists():
            self.consume_duplicate_ready(ready, f"清理已有终态的重复 ready: {job_id}")
            return None
        if running.exists():
            self.consume_duplicate_ready(ready, f"清理已有 RUNNING 的重复 ready: {job_id}")
            return None

        try:
            os.replace(ready, running)
        except FileNotFoundError:
            return None
        atomic_write(running, "RUNNING\n")
        self.log(f"领取任务 {job_id}")
        return job_id

    def finish(self, job_id: str, exitcode: int) -> None:
        running = self.layout.running(job_id)
        outcome = "SUCCEEDED" if exitcode == 0 else "FAILED"
        destination = self.layout.done(job_id) if exitcode == 0 else self.layout.failed(job_id)
        atomic_write(self.layout.exitcode(job_id), f"{exitcode}\n")
        atomic_write(running, outcome + "\n")
        os.replace(running, destination)
        self.log(f"任务 {job_id} 结束: {outcome}, exitcode={exitcode}")

    def execute(self, job_id: str) -> None:
        job_dir = self.layout.job_dir(job_id)
        run_script = job_dir / "run.sh"
        self.current_job = job_id
        self.write_heartbeat()

        env = os.environ.copy()
        env.update(
            {
                "PDSHELL_ROOT": str(self.layout.root),
                "PDSHELL_JOB_ID": job_id,
                "PDSHELL_JOB_DIR": str(job_dir),
            }
        )

        exitcode = 127
        with self.layout.stdout(job_id).open("ab", buffering=0) as stdout_handle, self.layout.stderr(
            job_id
        ).open("ab", buffering=0) as stderr_handle:
            try:
                self.current_process = subprocess.Popen(
                    ["bash", str(run_script)],
                    cwd=job_dir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                terminate_sent_at = None
                while self.current_process.poll() is None:
                    self.write_heartbeat()
                    if self.stop_requested and terminate_sent_at is None:
                        try:
                            os.killpg(self.current_process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        terminate_sent_at = time.monotonic()
                        self.log(f"Worker 停止，向任务 {job_id} 发送 SIGTERM")
                    elif terminate_sent_at is not None and time.monotonic() - terminate_sent_at > 10:
                        try:
                            os.killpg(self.current_process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    time.sleep(min(self.poll_interval, 1.0))
                exitcode = int(self.current_process.returncode)
            except (OSError, TypeError, ValueError) as exc:
                stderr_handle.write(f"PDShell 无法执行任务: {exc}\n".encode())
                self.log(f"任务 {job_id} 执行异常: {exc!r}")
            finally:
                self.current_process = None

        self.finish(job_id, exitcode)
        self.current_job = ""
        self.write_heartbeat()

    def request_stop(self, signum: int, _frame: object) -> None:
        self.stop_requested = True
        self.log(f"收到信号 {signum}，准备安全停止")

    def run(self, once: bool = False) -> None:
        self.layout.create()
        self.acquire_lock()
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.recover()
        self.log(f"Worker 启动: pid={os.getpid()}, root={self.layout.root}")

        try:
            while not self.stop_requested:
                self.write_heartbeat()
                ready_files = sorted(
                    marker
                    for marker in self.layout.root.glob("*/.ready")
                    if marker.is_file()
                )
                claimed_any = False
                for ready in ready_files:
                    job_id = self.claim(ready)
                    if job_id is None:
                        continue
                    claimed_any = True
                    self.execute(job_id)
                    if self.stop_requested:
                        break
                if once and not claimed_any:
                    break
                if not self.stop_requested:
                    time.sleep(self.poll_interval)
        finally:
            self.log("Worker 已停止")
            try:
                self.layout.heartbeat.unlink()
            except FileNotFoundError:
                pass
            if self.lock_handle is not None:
                fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
                self.lock_handle.close()
                self.lock_handle = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent Directory Shell")
    subparsers = parser.add_subparsers(dest="command", required=True)

    worker_parser = subparsers.add_parser("worker", help="启动 Worker")
    worker_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    worker_parser.add_argument("--poll-interval", type=float, default=1.0)
    worker_parser.add_argument("--once", action="store_true", help="处理当前队列后退出")

    submit_parser = subparsers.add_parser("submit", help="提交一个 Shell 脚本")
    submit_parser.add_argument("script", type=Path)
    submit_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    submit_parser.add_argument("--job-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "worker":
            if args.poll_interval <= 0:
                raise ValueError("poll interval 必须大于 0")
            Worker(args.root, args.poll_interval).run(once=args.once)
        elif args.command == "submit":
            print(submit(args.root, args.script, args.job_id))
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"PDShell: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
