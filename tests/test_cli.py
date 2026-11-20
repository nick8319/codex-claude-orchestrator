import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
import contextlib
import io
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from store import Store
import orchestrator


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name).resolve()
        self.thread = str(uuid.uuid4())
        self.peer = "codex:" + self.thread

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/orchestrator.py"),
                "--state-dir",
                str(self.state),
                *args,
            ],
            capture_output=True,
            text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_enroll_root_identity_and_read_ack_are_separate(self):
        row = self.run_cli("enroll-root", "--thread", self.thread, "--cwd", str(self.state))
        self.assertEqual(row["id"], self.peer)
        store = Store(self.state)
        store.register(dict(row, id="codex:other", session_id=str(uuid.uuid4())))
        msg = store.enqueue("codex:other", self.peer, "A result")
        store.claim()
        store.finish(msg["id"], "delivered")
        inbox = self.run_cli("inbox", "--peer", self.peer)
        self.assertEqual(len(inbox), 1)
        self.assertIsNone(store.get(msg["id"])["acked_at"])
        self.run_cli("ack", "--peer", self.peer, msg["id"])
        self.assertEqual(self.run_cli("inbox", "--peer", self.peer), [])

    def test_send_uses_file_body_and_idempotency(self):
        self.run_cli("enroll-root", "--thread", self.thread, "--cwd", str(self.state))
        other = str(uuid.uuid4())
        self.run_cli("enroll-root", "--thread", other, "--cwd", str(self.state))
        brief = self.state / "brief.txt"
        brief.write_text("literal $(no shell) `no command`\nsecond line")
        mid = str(uuid.uuid4())
        args = (
            "send",
            "--from",
            self.peer,
            "--to",
            "codex:" + other,
            "--body-file",
            str(brief),
            "--message-id",
            mid,
        )
        self.run_cli(*args)
        self.run_cli(*args)
        self.assertEqual(len(Store(self.state).messages()), 1)
        self.assertEqual(Store(self.state).get(mid)["body"], brief.read_text())
        self.run_cli("cancel", mid)
        self.assertEqual(Store(self.state).get(mid)["status"], "failed")

    def test_empty_wait_is_bounded_and_nonmutating(self):
        self.run_cli("enroll-root", "--thread", self.thread, "--cwd", str(self.state))
        self.assertEqual(self.run_cli("wait", "--peer", self.peer, "--timeout", "0"), [])

    def test_missing_thread_never_guesses_recent_session(self):
        env = dict(os.environ)
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_SESSION_ID", None)
        r = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/orchestrator.py"),
                "--state-dir",
                str(self.state),
                "enroll-root",
            ],
            env=env,
            capture_output=True,
        )
        self.assertNotEqual(r.returncode, 0)

    def test_headless_billing_policy_default_and_explicit_forwarded(self):
        base = ["headless", "--cwd", str(self.state), "--brief", "brief.md"]
        for flags, expected in [
            ([], "subscription-only"),
            (["--billing-policy", "native-metering"], "native-metering"),
        ]:
            args = orchestrator.parser().parse_args(base + flags)
            with patch("headless.run", return_value={"status": "blocked"}) as launch:
                orchestrator.execute(args)
            self.assertEqual(launch.call_args.kwargs["billing_policy"], expected)

    def test_headless_failure_never_returns_shell_success(self):
        argv = [
            "orchestrator",
            "--state-dir",
            str(self.state),
            "headless",
            "--cwd",
            str(self.state),
            "--brief",
            str(self.state / "brief.md"),
        ]
        for status in (
            "failed",
            "outcome_unknown",
            "model_mismatch",
            "session_mismatch",
            "completed",
        ):
            with (
                patch.object(sys, "argv", argv),
                patch("headless.run", return_value={"status": status}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(orchestrator.main(), 0 if status == "completed" else 1)


if __name__ == "__main__":
    unittest.main()
