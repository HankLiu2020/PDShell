import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PDSHELL = PROJECT_DIR / "pdshell.py"


class PDShellIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "fshell"

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
            timeout=10,
        )

    def submit(self, script, job_id):
        self.run_cli("submit", str(script), "--root", str(self.root), "--job-id", job_id)

    def run_worker_once(self):
        self.run_cli("worker", "--root", str(self.root), "--poll-interval", "0.01", "--once")

    def test_success_and_failure_complete_the_full_loop(self):
        success = self.write_script("success.sh", "echo standard-output\necho standard-error >&2\n")
        failure = self.write_script("failure.sh", "echo before-failure\nexit 7\n")
        self.submit(success, "success-job")
        self.submit(failure, "failure-job")

        self.run_worker_once()

        self.assertTrue((self.root / "done" / "success-job").is_file())
        self.assertEqual((self.root / "logs" / "success-job.status").read_text(), "SUCCEEDED\n")
        self.assertEqual((self.root / "logs" / "success-job.exitcode").read_text(), "0\n")
        self.assertIn("standard-output", (self.root / "logs" / "success-job.log").read_text())
        self.assertIn("standard-error", (self.root / "logs" / "success-job.stderr.log").read_text())

        self.assertTrue((self.root / "failed" / "failure-job").is_file())
        self.assertEqual((self.root / "logs" / "failure-job.status").read_text(), "FAILED\n")
        self.assertEqual((self.root / "logs" / "failure-job.exitcode").read_text(), "7\n")

    def test_external_client_can_submit_with_files_only(self):
        worker = subprocess.Popen(
            [
                sys.executable,
                str(PDSHELL),
                "worker",
                "--root",
                str(self.root),
                "--poll-interval",
                "0.02",
            ],
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

            staging_job = self.root / "jobs" / ".file-job.uploading"
            staging_job.mkdir()
            (staging_job / "run.sh").write_text(
                "echo file-protocol-stdout\n"
                "echo file-protocol-stderr >&2\n"
                "echo \"$PDSHELL_JOB_ID\" > result.txt\n"
                "sleep 0.2\n",
                encoding="utf-8",
            )
            os.replace(staging_job, self.root / "jobs" / "file-job")

            staging_ready = self.root / "inbox" / ".file-job.ready.uploading"
            staging_ready.write_text("READY\n", encoding="utf-8")
            os.replace(staging_ready, self.root / "inbox" / "file-job.ready")

            saw_running = False
            deadline = time.monotonic() + 3
            while not (self.root / "done" / "file-job").exists():
                status = self.root / "logs" / "file-job.status"
                if status.exists() and status.read_text(encoding="utf-8") == "RUNNING\n":
                    saw_running = True
                if time.monotonic() >= deadline:
                    self.fail("文件协议任务未在 3 秒内完成")
                time.sleep(0.02)

            self.assertTrue(saw_running)
            self.assertEqual((self.root / "logs" / "file-job.status").read_text(), "SUCCEEDED\n")
            self.assertEqual((self.root / "logs" / "file-job.exitcode").read_text(), "0\n")
            self.assertIn("file-protocol-stdout", (self.root / "logs" / "file-job.log").read_text())
            self.assertIn(
                "file-protocol-stderr",
                (self.root / "logs" / "file-job.stderr.log").read_text(),
            )
            self.assertEqual((self.root / "jobs" / "file-job" / "result.txt").read_text(), "file-job\n")
        finally:
            worker.terminate()
            try:
                worker.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate(timeout=3)

    def test_twenty_file_only_jobs_run_once_without_overlap(self):
        inbox = self.root / "inbox"
        jobs = self.root / "jobs"
        inbox.mkdir(parents=True)
        jobs.mkdir(parents=True)
        for index in range(20):
            job_id = f"batch-{index:02d}"
            job_dir = jobs / job_id
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
            (inbox / f"{job_id}.ready").write_text("READY\n", encoding="utf-8")

        self.run_worker_once()

        executions = (self.root / "executions").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(executions), 20)
        self.assertEqual(len(set(executions)), 20)
        self.assertFalse((self.root / "overlap").exists())
        self.assertEqual(len(list((self.root / "done").iterdir())), 20)
        self.assertEqual(len(list((self.root / "failed").iterdir())), 0)
        self.assertEqual(len(list((self.root / "running").iterdir())), 0)
        self.assertEqual(len(list((self.root / "inbox").glob("*.ready"))), 0)
        for job_id in executions:
            self.assertEqual((self.root / "logs" / f"{job_id}.status").read_text(), "SUCCEEDED\n")
            self.assertEqual((self.root / "logs" / f"{job_id}.exitcode").read_text(), "0\n")
            self.assertIn(job_id + " stdout", (self.root / "logs" / f"{job_id}.log").read_text())
            self.assertIn(
                job_id + " stderr",
                (self.root / "logs" / f"{job_id}.stderr.log").read_text(),
            )

    def test_script_without_ready_marker_is_not_executed(self):
        job_dir = self.root / "jobs" / "partial-upload"
        job_dir.mkdir(parents=True)
        (job_dir / "run.sh").write_text("touch executed\n", encoding="utf-8")

        self.run_worker_once()

        self.assertFalse((job_dir / "executed").exists())
        self.assertFalse((self.root / "logs" / "partial-upload.status").exists())

    def test_recovery_marks_running_as_worker_lost_and_never_reexecutes(self):
        job_dir = self.root / "jobs" / "lost-job"
        running_dir = self.root / "running"
        failed_dir = self.root / "failed"
        logs_dir = self.root / "logs"
        job_dir.mkdir(parents=True)
        running_dir.mkdir(parents=True)
        failed_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        (job_dir / "run.sh").write_text("touch should-not-exist\n", encoding="utf-8")
        (running_dir / "lost-job").write_text("RUNNING\n", encoding="utf-8")
        (failed_dir / "previously-lost").write_text("WORKER_LOST\n", encoding="utf-8")
        (logs_dir / "lost-job.status").write_text("RUNNING\n", encoding="utf-8")

        self.run_worker_once()
        self.run_worker_once()

        self.assertFalse((job_dir / "should-not-exist").exists())
        self.assertFalse((running_dir / "lost-job").exists())
        self.assertTrue((self.root / "failed" / "lost-job").is_file())
        self.assertEqual((logs_dir / "lost-job.status").read_text(), "WORKER_LOST\n")
        self.assertEqual((logs_dir / "lost-job.exitcode").read_text(), "-1\n")
        self.assertEqual((logs_dir / "previously-lost.status").read_text(), "WORKER_LOST\n")
        self.assertEqual((logs_dir / "previously-lost.exitcode").read_text(), "-1\n")

    def test_duplicate_job_id_is_rejected(self):
        script = self.write_script("duplicate.sh", "true\n")
        self.submit(script, "same-id")

        result = self.run_cli(
            "submit",
            str(script),
            "--root",
            str(self.root),
            "--job-id",
            "same-id",
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("任务 ID 已存在", result.stderr)

    def test_duplicate_ready_is_quarantined_once_without_reexecution(self):
        script = self.write_script(
            "run-once.sh",
            'echo "$PDSHELL_JOB_ID" >> "$PDSHELL_ROOT/executions"\n',
        )
        self.submit(script, "completed-job")
        self.run_worker_once()

        duplicate = self.root / "inbox" / "completed-job.ready"
        duplicate.write_text("READY\n", encoding="utf-8")
        self.run_worker_once()
        first_log = (self.root / "worker.log").read_text(encoding="utf-8")
        self.run_worker_once()
        second_log = (self.root / "worker.log").read_text(encoding="utf-8")

        self.assertFalse(duplicate.exists())
        self.assertEqual(
            (self.root / "rejected" / "completed-job.ready").read_text(encoding="utf-8"),
            "REJECTED\nreason=TERMINAL_STATE_EXISTS\n",
        )
        self.assertEqual((self.root / "executions").read_text(encoding="utf-8"), "completed-job\n")
        self.assertEqual(first_log.count("拒绝已有终态的重复 ready"), 1)
        self.assertEqual(second_log.count("拒绝已有终态的重复 ready"), 1)

    def test_invalid_ready_is_quarantined_once(self):
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True)
        invalid = inbox / "invalid job.ready"
        invalid.write_text("READY\n", encoding="utf-8")

        self.run_worker_once()
        first_log = (self.root / "worker.log").read_text(encoding="utf-8")
        self.run_worker_once()
        second_log = (self.root / "worker.log").read_text(encoding="utf-8")

        self.assertFalse(invalid.exists())
        self.assertEqual(
            (self.root / "rejected" / "invalid job.ready").read_text(encoding="utf-8"),
            "REJECTED\nreason=INVALID_JOB_ID\n",
        )
        self.assertEqual(first_log.count("拒绝非法 ready 文件"), 1)
        self.assertEqual(second_log.count("拒绝非法 ready 文件"), 1)

    def test_missing_run_script_fails_instead_of_stopping_worker(self):
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "missing-script.ready").write_text("READY\n", encoding="utf-8")

        self.run_worker_once()

        self.assertTrue((self.root / "failed" / "missing-script").is_file())
        self.assertEqual((self.root / "logs" / "missing-script.status").read_text(), "FAILED\n")
        self.assertEqual((self.root / "logs" / "missing-script.exitcode").read_text(), "127\n")

    def test_heartbeat_and_single_worker_lock(self):
        worker = subprocess.Popen(
            [
                sys.executable,
                str(PDSHELL),
                "worker",
                "--root",
                str(self.root),
                "--poll-interval",
                "0.05",
            ],
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
                "worker",
                "--root",
                str(self.root),
                "--poll-interval",
                "0.01",
                "--once",
                check=False,
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
            [
                sys.executable,
                str(PDSHELL),
                "worker",
                "--root",
                str(self.root),
                "--poll-interval",
                "0.05",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "running" / "interrupt-job").exists():
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
        self.assertTrue((self.root / "failed" / "interrupt-job").is_file())
        self.assertEqual((self.root / "logs" / "interrupt-job.status").read_text(), "FAILED\n")
        self.assertNotEqual((self.root / "logs" / "interrupt-job.exitcode").read_text(), "0\n")


if __name__ == "__main__":
    unittest.main()
