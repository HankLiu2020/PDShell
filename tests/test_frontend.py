import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
PDSHELL = PROJECT_DIR / "pdshell.py"
CLIENT = PROJECT_DIR / "pdshell_client.sh"
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

        transport = ScpTransport("user@example:/persist/tasks", self.base / "cache", runner=runner)
        with self.assertRaises(TransportError):
            transport.submit_script(self.write_script(), "scp-job")
        transport.sync_metadata()
        self.assertEqual(calls[0][:3], ["scp", "-q", "-r"])

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
        subprocess.run(["bash", "-n", str(CLIENT)], check=True, capture_output=True, text=True)
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


if __name__ == "__main__":
    unittest.main()
