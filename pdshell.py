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


JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(
            "job id 只能包含字母、数字、点、下划线和短横线，且长度不超过 128"
        )
    return job_id


class Layout:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.inbox = self.root / "inbox"
        self.jobs = self.root / "jobs"
        self.running = self.root / "running"
        self.done = self.root / "done"
        self.failed = self.root / "failed"
        self.logs = self.root / "logs"
        self.heartbeat = self.root / "heartbeat"
        self.worker_log = self.root / "worker.log"
        self.worker_lock = self.root / "worker.lock"

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (
            self.inbox,
            self.jobs,
            self.running,
            self.done,
            self.failed,
            self.logs,
        ):
            path.mkdir(exist_ok=True)

    def status(self, job_id: str) -> Path:
        return self.logs / f"{job_id}.status"

    def exitcode(self, job_id: str) -> Path:
        return self.logs / f"{job_id}.exitcode"

    def stdout(self, job_id: str) -> Path:
        return self.logs / f"{job_id}.log"

    def stderr(self, job_id: str) -> Path:
        return self.logs / f"{job_id}.stderr.log"


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
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


def submit(root: Path, script: Path, job_id: str | None = None) -> str:
    layout = Layout(root)
    layout.create()
    if job_id is None:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    validate_job_id(job_id)

    if not script.is_file():
        raise FileNotFoundError(f"脚本不存在: {script}")

    job_dir = layout.jobs / job_id
    markers = [
        layout.inbox / f"{job_id}.ready",
        layout.running / job_id,
        layout.done / job_id,
        layout.failed / job_id,
    ]
    if job_dir.exists() or any(marker.exists() for marker in markers):
        raise FileExistsError(f"任务 ID 已存在: {job_id}")

    temp_dir = layout.jobs / f".{job_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        temp_dir.mkdir()
        run_script = temp_dir / "run.sh"
        with script.open("rb") as source, run_script.open("wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(run_script, 0o755)
        os.replace(temp_dir, job_dir)

        ready = layout.inbox / f"{job_id}.ready"
        atomic_write(ready, f"READY\nsubmitted_at={time.time():.6f}\n")
        atomic_write(layout.status(job_id), "READY\n")
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
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
        for marker in sorted(self.layout.running.iterdir()):
            if not marker.is_file():
                continue
            job_id = marker.name
            try:
                validate_job_id(job_id)
            except ValueError:
                self.log(f"忽略非法 running 标记: {marker.name}")
                continue

            atomic_write(marker, "WORKER_LOST\n")
            os.replace(marker, self.layout.failed / job_id)
            atomic_write(self.layout.status(job_id), "WORKER_LOST\n")
            atomic_write(self.layout.exitcode(job_id), "-1\n")
            self.log(f"恢复任务 {job_id}: RUNNING -> WORKER_LOST")
            recovered += 1

        for ready in sorted(self.layout.inbox.glob("*.ready")):
            job_id = ready.name[: -len(".ready")]
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            if not (self.layout.done / job_id).exists() and not (self.layout.failed / job_id).exists():
                atomic_write(self.layout.status(job_id), "READY\n")

        for marker in sorted(self.layout.done.iterdir()):
            if marker.is_file() and JOB_ID_RE.fullmatch(marker.name):
                atomic_write(self.layout.status(marker.name), "SUCCEEDED\n")

        for marker in sorted(self.layout.failed.iterdir()):
            if not marker.is_file() or not JOB_ID_RE.fullmatch(marker.name):
                continue
            outcome = marker.read_text(encoding="utf-8").splitlines()[0:1]
            status = outcome[0] if outcome and outcome[0] in {"FAILED", "WORKER_LOST"} else "FAILED"
            atomic_write(self.layout.status(marker.name), status + "\n")
            if status == "WORKER_LOST":
                atomic_write(self.layout.exitcode(marker.name), "-1\n")

        if recovered == 0:
            self.log("启动恢复扫描完成: 没有遗留 RUNNING 任务")

    def claim(self, ready: Path) -> str | None:
        if not ready.name.endswith(".ready"):
            return None
        job_id = ready.name[: -len(".ready")]
        try:
            validate_job_id(job_id)
        except ValueError:
            self.log(f"忽略非法 ready 文件: {ready.name}")
            return None

        if (self.layout.done / job_id).exists() or (self.layout.failed / job_id).exists():
            self.log(f"忽略已有终态的重复 ready: {job_id}")
            return None

        running = self.layout.running / job_id
        try:
            os.replace(ready, running)
        except FileNotFoundError:
            return None
        atomic_write(running, "RUNNING\n")
        atomic_write(self.layout.status(job_id), "RUNNING\n")
        self.log(f"领取任务 {job_id}")
        return job_id

    def finish(self, job_id: str, exitcode: int) -> None:
        running = self.layout.running / job_id
        outcome = "SUCCEEDED" if exitcode == 0 else "FAILED"
        destination = self.layout.done / job_id if exitcode == 0 else self.layout.failed / job_id
        atomic_write(self.layout.exitcode(job_id), f"{exitcode}\n")
        atomic_write(running, outcome + "\n")
        os.replace(running, destination)
        atomic_write(self.layout.status(job_id), outcome + "\n")
        self.log(f"任务 {job_id} 结束: {outcome}, exitcode={exitcode}")

    def execute(self, job_id: str) -> None:
        job_dir = self.layout.jobs / job_id
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
            except Exception as exc:
                stderr_handle.write(f"PDShell 无法执行任务: {exc}\n".encode("utf-8"))
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
                ready_files = sorted(self.layout.inbox.glob("*.ready"))
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
    worker_parser.add_argument("--root", type=Path, default=Path("/persist/fshell"))
    worker_parser.add_argument("--poll-interval", type=float, default=1.0)
    worker_parser.add_argument("--once", action="store_true", help="处理当前队列后退出")

    submit_parser = subparsers.add_parser("submit", help="提交一个 Shell 脚本")
    submit_parser.add_argument("script", type=Path)
    submit_parser.add_argument("--root", type=Path, default=Path("/persist/fshell"))
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
