import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
PDSHELL = PROJECT_DIR / "pdshell.py"
CLIENT = PROJECT_DIR / "pdshell_client.sh"
SYNC_TO_SERVER = PROJECT_DIR / "sync_to_server.sh"
sys.path.insert(0, str(PROJECT_DIR))

from frontend.transport import (
    RsyncTransport,
    ScpTransport,
    TransportError,
    list_snapshots,
    tail_text,
)


class FrontendTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_script(self, content="echo frontend-output\n"):
        script = self.base / "task.sh"
        script.write_text(content, encoding="utf-8")
        return script

    def test_rsync_transport_submits_and_syncs_v2_layout(self):
        remote = self.base / "remote"
        cache = self.base / "cache"
        remote.mkdir()
        transport = RsyncTransport(str(remote), cache)
        job_id = transport.submit_script(self.write_script(), "rsync-job")

        subprocess.run(
            [sys.executable, str(PDSHELL), "worker", "--root", str(remote), "--once"],
            check=True,
            capture_output=True,
            text=True,
        )
        transport.sync_metadata()
        snapshots = transport.snapshots()
        self.assertEqual([(item.job_id, item.state) for item in snapshots], [(job_id, "SUCCEEDED")])
        transport.sync_job(job_id)
        stdout, stderr = transport.logs(job_id)
        self.assertIn("frontend-output", stdout)
        self.assertEqual(stderr, "")

    def test_tail_text_limits_rendered_log(self):
        log = self.base / "large.log"
        log.write_bytes(b"a" * 300)
        rendered = tail_text(log, max_bytes=100)
        self.assertTrue(rendered.startswith("[仅显示最后 0 KB]"))
        self.assertEqual(len(rendered.splitlines()[-1]), 100)

    def test_scp_transport_is_read_only(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        transport = ScpTransport(
            "user@example:/persist/tasks",
            self.base / "cache",
            runner=runner,
            ssh_port=30901,
        )
        with self.assertRaises(TransportError):
            transport.submit_script(self.write_script(), "scp-job")
        transport.sync_metadata()
        self.assertEqual(calls[0][:5], ["scp", "-P", "30901", "-q", "-r"])

    def test_remote_transport_uses_port_and_hides_password_from_arguments(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            if "--list-only" in args:
                return subprocess.CompletedProcess(args, 0, "drwx------ 1 .\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        transport = RsyncTransport(
            "user@example:/persist/tasks",
            self.base / "cache",
            runner=runner,
            ssh_port=30901,
            password="not-on-command-line",
        )
        transport.sync_metadata()
        command = calls[0]
        self.assertEqual(command[:3], ["sshpass", "-e", "rsync"])
        self.assertIn("ssh -p 30901 -o ServerAliveInterval=30", command)
        self.assertNotIn("not-on-command-line", command)

        transport.submit_script(self.write_script(), "ordered-ready")
        self.assertIn("--exclude=.ready", calls[-2])
        self.assertTrue(calls[-1][-1].endswith("/ordered-ready/.ready"))

    def test_rsync_transport_rejects_every_existing_job_shape(self):
        for existing in ("ready", "running", "done", "failed", "incomplete", "audit"):
            with self.subTest(existing=existing):
                remote = self.base / existing
                remote.mkdir()
                if existing == "audit":
                    (remote / "same-id.sh").write_text("old audit\n", encoding="utf-8")
                else:
                    job_dir = remote / "same-id"
                    job_dir.mkdir()
                    (job_dir / "run.sh").write_text("old script\n", encoding="utf-8")
                    if existing in {"ready", "running", "done", "failed"}:
                        (job_dir / f".{existing}").write_text(existing.upper() + "\n", encoding="utf-8")

                transport = RsyncTransport(str(remote), self.base / f"cache-{existing}")
                with self.assertRaisesRegex(TransportError, "任务 ID 已存在"):
                    transport.submit_script(self.write_script("echo replacement\n"), "same-id")

    def test_rsync_transport_does_not_treat_probe_errors_as_missing(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 12, "", "protocol error")

        transport = RsyncTransport(
            "user@example:/persist/tasks",
            self.base / "cache",
            runner=runner,
        )
        with self.assertRaisesRegex(TransportError, "预检失败"):
            transport.submit_script(self.write_script(), "probe-error")
        self.assertEqual(len(calls), 1)
        self.assertIn("--list-only", calls[0])

    def test_rsync_transport_reuses_worker_job_id_validation(self):
        transport = RsyncTransport(str(self.base / "remote"), self.base / "cache")
        with self.assertRaises(TransportError):
            transport.submit_script(self.write_script(), "invalid job")

    def test_job_state_priority_and_unknown_directory(self):
        root = self.base / "tasks"
        done = root / "done-job"
        done.mkdir(parents=True)
        (done / ".done").write_text("SUCCEEDED\n", encoding="utf-8")
        (done / ".ready").write_text("READY\n", encoding="utf-8")
        incomplete = root / "partial-job"
        incomplete.mkdir(parents=True)
        snapshots = list_snapshots(root)
        self.assertEqual([(item.job_id, item.state) for item in snapshots], [
            ("done-job", "SUCCEEDED"),
            ("partial-job", "INCOMPLETE"),
        ])


class ShellClientTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bash_syntax_and_local_rsync_submission(self):
        subprocess.run(
            ["bash", "-n", str(CLIENT), str(SYNC_TO_SERVER)],
            check=True,
            capture_output=True,
            text=True,
        )
        remote = self.base / "tasks"
        remote.mkdir()
        script = self.base / "client-task.sh"
        script.write_text("echo client-output\n", encoding="utf-8")
        environment = os.environ | {
            "PDSHELL_REMOTE": str(remote),
            "PDSHELL_TRANSPORT": "rsync",
            "PDSHELL_CACHE": str(self.base / "cache"),
        }
        submitted = subprocess.run(
            [str(CLIENT), "submit", str(script), "client-job"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(submitted.stdout.strip(), "client-job")

        replacement = self.base / "replacement.sh"
        replacement.write_text("echo replacement-output\n", encoding="utf-8")
        duplicate = subprocess.run(
            [str(CLIENT), "submit", str(replacement), "client-job"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("任务 ID 已存在", duplicate.stderr)
        self.assertIn("client-output", (remote / "client-job" / "run.sh").read_text())

        subprocess.run(
            [sys.executable, str(PDSHELL), "worker", "--root", str(remote), "--once"],
            check=True,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            [str(CLIENT), "status", "client-job"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertIn("SUCCEEDED", status.stdout)
        logs = subprocess.run(
            [str(CLIENT), "logs", "client-job"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertIn("client-output", logs.stdout)

    def test_shell_client_rejects_incomplete_remote_job(self):
        remote = self.base / "tasks"
        incomplete = remote / "partial-job"
        incomplete.mkdir(parents=True)
        (incomplete / "run.sh").write_text("echo original\n", encoding="utf-8")
        replacement = self.base / "replacement.sh"
        replacement.write_text("echo replacement\n", encoding="utf-8")
        result = subprocess.run(
            [str(CLIENT), "submit", str(replacement), "partial-job"],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "PDSHELL_REMOTE": str(remote),
                "PDSHELL_TRANSPORT": "rsync",
                "PDSHELL_CACHE": str(self.base / "cache"),
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("任务 ID 已存在", result.stderr)
        self.assertEqual((incomplete / "run.sh").read_text(), "echo original\n")

    def test_project_sync_excludes_runtime_and_repository_data(self):
        destination = self.base / "deployed"
        last_sync = self.base / "last-sync-target"
        subprocess.run(
            [str(SYNC_TO_SERVER)],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "PDSHELL_DEPLOY_TARGET": str(destination),
                "PDSHELL_LAST_SYNC_FILE": str(last_sync),
            },
        )
        self.assertTrue((destination / "pdshell.py").is_file())
        self.assertTrue(os.access(destination / "docker-entrypoint.sh", os.X_OK))
        self.assertFalse((destination / ".git").exists())
        self.assertFalse((destination / "tasks").exists())
        self.assertEqual(last_sync.read_text(encoding="utf-8").strip(), str(destination))

    def test_tracked_text_files_use_lf_line_endings(self):
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=PROJECT_DIR,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        for relative in tracked:
            path = PROJECT_DIR / relative
            if path.suffix in {".py", ".sh", ".md"} or path.name.startswith("Dockerfile"):
                self.assertNotIn(b"\r\n", path.read_bytes(), relative)


if __name__ == "__main__":
    unittest.main()
