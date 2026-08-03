import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
PDSHELL = PROJECT_DIR / "pdshell.py"
STATES = (".ready", ".running", ".done", ".failed")


class PDShellIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "tasks"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_script(self, name, content):
        path = Path(self.temp_dir.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def run_cli(self, *args, check=True):
        return subprocess.run(
            [sys.executable, str(PDSHELL), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def submit(self, script, job_id):
        self.run_cli("submit", str(script), "--root", str(self.root), "--job-id", job_id)

    def run_worker_once(self):
        self.run_cli("worker", "--root", str(self.root), "--poll-interval", "0.01", "--once")

    def state_files(self, job_id):
        job_dir = self.root / job_id
        return [name for name in STATES if (job_dir / name).exists()]

    def test_success_and_failure_complete_the_full_loop(self):
        success = self.write_script("success.sh", "echo standard-output\necho standard-error >&2\n")
        failure = self.write_script("failure.sh", "echo before-failure\nexit 7\n")
        self.submit(success, "success-job")
        self.submit(failure, "failure-job")

        self.run_worker_once()

        success_dir = self.root / "success-job"
        self.assertEqual(self.state_files("success-job"), [".done"])
        self.assertEqual((success_dir / "exitcode").read_text(), "0\n")
        self.assertIn("standard-output", (success_dir / "log").read_text())
        self.assertIn("standard-error", (success_dir / "stderr.log").read_text())
        self.assertEqual((self.root / "success-job.sh").read_bytes(), (success_dir / "run.sh").read_bytes())

        failure_dir = self.root / "failure-job"
        self.assertEqual(self.state_files("failure-job"), [".failed"])
        self.assertEqual((failure_dir / "exitcode").read_text(), "7\n")

    def test_external_client_can_submit_with_files_only(self):
        worker = subprocess.Popen(
            [sys.executable, str(PDSHELL), "worker", "--root", str(self.root), "--poll-interval", "0.02"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "heartbeat").exists():
                if time.monotonic() >= deadline:
                    self.fail("Worker 未在 3 秒内写入 heartbeat")
                time.sleep(0.02)

            staging_job = self.root / ".file-job.uploading"
            staging_job.mkdir(parents=True)
            (staging_job / "run.sh").write_text(
                "echo file-protocol-stdout\n"
                "echo file-protocol-stderr >&2\n"
                "echo \"$PDSHELL_JOB_ID\" > result.txt\n"
                "sleep 0.2\n",
                encoding="utf-8",
            )
            os.replace(staging_job, self.root / "file-job")

            staging_ready = self.root / "file-job" / ".ready.uploading"
            staging_ready.write_text("READY\n", encoding="utf-8")
            os.replace(staging_ready, self.root / "file-job" / ".ready")

            deadline = time.monotonic() + 3
            while not (self.root / "file-job" / ".done").exists():
                if time.monotonic() >= deadline:
                    self.fail("文件协议任务未在 3 秒内完成")
                time.sleep(0.02)

            job_dir = self.root / "file-job"
            self.assertEqual(self.state_files("file-job"), [".done"])
            self.assertEqual((job_dir / "exitcode").read_text(), "0\n")
            self.assertIn("file-protocol-stdout", (job_dir / "log").read_text())
            self.assertIn("file-protocol-stderr", (job_dir / "stderr.log").read_text())
            self.assertEqual((job_dir / "result.txt").read_text(), "file-job\n")
        finally:
            worker.terminate()
            try:
                worker.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate(timeout=3)

    def test_twenty_file_only_jobs_run_once_without_overlap(self):
        self.root.mkdir(parents=True)
        for index in range(20):
            job_id = f"batch-{index:02d}"
            job_dir = self.root / job_id
            job_dir.mkdir()
            (job_dir / "run.sh").write_text(
                'mkdir "$PDSHELL_ROOT/active" || { echo "$PDSHELL_JOB_ID" '
                '>> "$PDSHELL_ROOT/overlap"; exit 91; }\n'
                'echo "$PDSHELL_JOB_ID" >> "$PDSHELL_ROOT/executions"\n'
                'echo "$PDSHELL_JOB_ID stdout"\n'
                'echo "$PDSHELL_JOB_ID stderr" >&2\n'
                "sleep 0.01\n"
                'rmdir "$PDSHELL_ROOT/active"\n',
                encoding="utf-8",
            )
            (job_dir / ".ready").write_text("READY\n", encoding="utf-8")

        self.run_worker_once()

        executions = (self.root / "executions").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(executions), 20)
        self.assertEqual(len(set(executions)), 20)
        self.assertFalse((self.root / "overlap").exists())
        self.assertEqual(len([p for p in self.root.iterdir() if p.is_dir()]), 20)
        for job_id in executions:
            self.assertEqual(self.state_files(job_id), [".done"])
            self.assertEqual((self.root / job_id / "exitcode").read_text(), "0\n")
            self.assertIn(job_id + " stdout", (self.root / job_id / "log").read_text())
            self.assertIn(job_id + " stderr", (self.root / job_id / "stderr.log").read_text())

    def test_script_without_ready_marker_is_not_executed(self):
        job_dir = self.root / "partial-upload"
        job_dir.mkdir(parents=True)
        (job_dir / "run.sh").write_text("touch executed\n", encoding="utf-8")

        self.run_worker_once()

        self.assertFalse((job_dir / "executed").exists())
        self.assertEqual(self.state_files("partial-upload"), [])

    def test_recovery_marks_running_as_worker_lost_and_never_reexecutes(self):
        job_dir = self.root / "lost-job"
        job_dir.mkdir(parents=True)
        (job_dir / "run.sh").write_text("touch should-not-exist\n", encoding="utf-8")
        (job_dir / ".running").write_text("RUNNING\n", encoding="utf-8")

        self.run_worker_once()
        self.run_worker_once()

        self.assertFalse((job_dir / "should-not-exist").exists())
        self.assertEqual(self.state_files("lost-job"), [".failed"])
        self.assertEqual((job_dir / ".failed").read_text(), "WORKER_LOST\n")
        self.assertEqual((job_dir / "exitcode").read_text(), "-1\n")
        self.assertFalse((job_dir / ".status").exists())

    def test_duplicate_job_id_is_rejected_and_audit_copy_is_checked(self):
        script = self.write_script("duplicate.sh", "true\n")
        self.submit(script, "same-id")

        result = self.run_cli(
            "submit", str(script), "--root", str(self.root), "--job-id", "same-id", check=False
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("任务 ID 已存在", result.stderr)
        self.assertTrue((self.root / "same-id.sh").is_file())

    def test_duplicate_ready_is_consumed_once_without_reexecution(self):
        script = self.write_script(
            "run-once.sh", 'echo "$PDSHELL_JOB_ID" >> "$PDSHELL_ROOT/executions"\n'
        )
        self.submit(script, "completed-job")
        self.run_worker_once()

        duplicate = self.root / "completed-job" / ".ready"
        duplicate.write_text("READY\n", encoding="utf-8")
        self.run_worker_once()
        first_log = (self.root / "worker.log").read_text(encoding="utf-8")
        self.run_worker_once()
        second_log = (self.root / "worker.log").read_text(encoding="utf-8")

        self.assertFalse(duplicate.exists())
        self.assertEqual(self.state_files("completed-job"), [".done"])
        self.assertEqual((self.root / "executions").read_text(), "completed-job\n")
        self.assertEqual(first_log.count("清理已有终态的重复 ready"), 1)
        self.assertEqual(second_log.count("清理已有终态的重复 ready"), 1)

    def test_invalid_ready_becomes_failed_once(self):
        job_dir = self.root / "invalid job"
        job_dir.mkdir(parents=True)
        (job_dir / ".ready").write_text("READY\n", encoding="utf-8")

        self.run_worker_once()
        first_log = (self.root / "worker.log").read_text(encoding="utf-8")
        self.run_worker_once()
        second_log = (self.root / "worker.log").read_text(encoding="utf-8")

        self.assertEqual(self.state_files("invalid job"), [".failed"])
        self.assertEqual((job_dir / ".failed").read_text(), "FAILED\nreason=INVALID_JOB_ID\n")
        self.assertEqual((job_dir / "exitcode").read_text(), "2\n")
        self.assertEqual(first_log.count("非法 ready 已转为 FAILED"), 1)
        self.assertEqual(second_log.count("非法 ready 已转为 FAILED"), 1)

    def test_missing_run_script_fails_instead_of_stopping_worker(self):
        job_dir = self.root / "missing-script"
        job_dir.mkdir(parents=True)
        (job_dir / ".ready").write_text("READY\n", encoding="utf-8")

        self.run_worker_once()

        self.assertEqual(self.state_files("missing-script"), [".failed"])
        self.assertEqual((job_dir / "exitcode").read_text(), "127\n")
        self.assertIn("No such file", (job_dir / "stderr.log").read_text())

    def test_invalid_suffix_and_reserved_ids_are_rejected(self):
        script = self.write_script("id.sh", "true\n")
        for job_id in ("train.sh", "heartbeat"):
            result = self.run_cli(
                "submit", str(script), "--root", str(self.root), "--job-id", job_id, check=False
            )
            self.assertEqual(result.returncode, 2)

    def test_legacy_layout_is_rejected(self):
        (self.root / "inbox").mkdir(parents=True)

        result = self.run_cli("worker", "--root", str(self.root), "--once", check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("旧版分桶目录", result.stderr)

    def test_heartbeat_and_single_worker_lock(self):
        worker = subprocess.Popen(
            [sys.executable, str(PDSHELL), "worker", "--root", str(self.root), "--poll-interval", "0.05"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "heartbeat").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            heartbeat = (self.root / "heartbeat").read_text(encoding="utf-8")
            self.assertIn("timestamp=", heartbeat)
            self.assertIn("hostname=", heartbeat)
            self.assertIn("pid=", heartbeat)
            self.assertIn("current_job=", heartbeat)

            second = self.run_cli(
                "worker", "--root", str(self.root), "--poll-interval", "0.01", "--once", check=False
            )
            self.assertEqual(second.returncode, 2)
            self.assertIn("已有一个 PDShell Worker", second.stderr)
        finally:
            worker.terminate()
            try:
                worker.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate(timeout=3)

    def test_sigterm_stops_current_process_group_and_records_failure(self):
        script = self.write_script("long-running.sh", "echo started\nsleep 30\necho finished\n")
        self.submit(script, "interrupt-job")
        worker = subprocess.Popen(
            [sys.executable, str(PDSHELL), "worker", "--root", str(self.root), "--poll-interval", "0.05"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "interrupt-job" / ".running").exists():
                if time.monotonic() >= deadline:
                    self.fail("Worker 未在 3 秒内领取任务")
                time.sleep(0.02)
            worker.terminate()
            worker.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate(timeout=3)
            self.fail("Worker 收到 SIGTERM 后未在 3 秒内退出")

        self.assertEqual(worker.returncode, 0)
        self.assertEqual(self.state_files("interrupt-job"), [".failed"])
        self.assertNotEqual((self.root / "interrupt-job" / "exitcode").read_text(), "0\n")


if __name__ == "__main__":
    unittest.main()
