"""Durability and trust-boundary tests; real SQLite, filesystem, and workers."""

import concurrent.futures
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
STORE_PRESENT = importlib.util.find_spec("store") is not None
if STORE_PRESENT:
    from store import Store


class StoreContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(STORE_PRESENT, "durable store implementation is missing")
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "state"
        self.store = Store(self.directory)
        for name in ("alice", "bob", "eve"):
            self.store.register(
                dict(
                    id=name,
                    provider="claude",
                    session_id=name + "-session",
                    endpoint="/private/tmp/" + name + ".sock",
                    cwd="/private/tmp",
                )
            )

    def enqueue(self, **kwargs):
        return self.store.enqueue("alice", "bob", "Please inspect the artifact.", **kwargs)

    def test_durable_private_wal_database_and_reopen(self):
        message = self.enqueue()
        reopened = Store(self.directory)
        self.assertEqual(reopened.get(message["id"]), message)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        database = self.directory / "state.sqlite3"
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
        with closing(sqlite3.connect(str(database))) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA fullfsync").fetchone()[0], 1)
        for entry in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode) & 0o077, 0)
        json.dumps(reopened.peers())
        json.dumps(reopened.messages())

    def test_identity_cannot_be_rebound(self):
        original = self.store.peer("bob")
        self.assertEqual(self.store.register(original), original)
        for field, replacement in (
            ("provider", "codex"),
            ("session_id", "other"),
            ("endpoint", "/private/tmp/other.sock"),
            ("cwd", "/other"),
        ):
            changed = dict(original, **{field: replacement})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.register(changed)
        self.assertEqual(self.store.peer("bob"), original)

    def test_registered_process_identity_cannot_change(self):
        peer = dict(
            self.store.peer("bob"),
            id="bound",
            pid=123,
            proc_start="start-1",
            registry_path="/private/tmp/peer.json",
        )
        self.store.register(peer)
        for field, value in (
            ("pid", 124),
            ("proc_start", "start-2"),
            ("registry_path", "/private/tmp/other.json"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.register(dict(peer, **{field: value}))
        with self.assertRaises(ValueError):
            self.store.register({key: value for key, value in peer.items() if key != "pid"})
        refreshed = self.store.register(dict(peer, label="new display label"))
        self.assertEqual(refreshed["label"], "new display label")

    def test_duplicate_same_payload_is_idempotent_after_delivery(self):
        first = self.enqueue(message_id="m-1")
        self.store.claim()
        delivered = self.store.finish(first["id"], "delivered", "accepted by socket")
        self.assertEqual(self.enqueue(message_id="m-1"), delivered)
        self.assertEqual(len(self.store.messages()), 1)
        with self.assertRaises(ValueError):
            self.store.enqueue("alice", "bob", "Different instructions", message_id="m-1")

    def test_payload_validation_and_byte_limit(self):
        for body in ("", "   ", "é" * 8193, None):
            with self.subTest(body_type=type(body).__name__), self.assertRaises(ValueError):
                self.store.enqueue("alice", "bob", body)
        valid = self.store.enqueue("alice", "bob", "é" * 8192)
        self.assertEqual(len(valid["body"].encode("utf-8")), 16384)
        for bad in ("", "bad\nname", "x" * 129):
            with self.assertRaises(ValueError):
                self.enqueue(message_id=bad)
        with self.assertRaises(KeyError):
            self.store.enqueue("missing", "bob", "Hello")
        with self.assertRaises(KeyError):
            self.enqueue(reply_to="missing")

    def test_two_workers_never_claim_the_same_message(self):
        ids = {self.enqueue()["id"] for _ in range(24)}

        def worker(_):
            store = Store(self.directory)
            claimed = []
            while True:
                message = store.claim()
                if message is None:
                    return claimed
                claimed.append(message["id"])
                store.finish(message["id"], "delivered")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            claimed = [
                message_id for result in executor.map(worker, range(8)) for message_id in result
            ]
        self.assertEqual(set(claimed), ids)
        self.assertEqual(len(claimed), len(ids))

    def test_process_exit_after_claim_recovers_unknown_without_retry(self):
        message = self.enqueue()
        code = "import os,sys;from pathlib import Path;from store import Store;s=Store(Path(sys.argv[1]));s.claim();os._exit(19)"
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.directory)],
            env=dict(os.environ, PYTHONPATH=str(SCRIPTS)),
            capture_output=True,
        )
        self.assertEqual(result.returncode, 19, result.stderr)
        reopened = Store(self.directory)
        self.assertEqual(reopened.get(message["id"])["status"], "sending")
        self.assertEqual(reopened.recover(), 1)
        self.assertEqual(reopened.recover(), 0)
        self.assertEqual(reopened.get(message["id"])["status"], "unknown")
        self.assertIsNone(reopened.claim())

    def test_definitely_unsent_delivery_can_be_deferred_without_busy_loop(self):
        message = self.enqueue()
        self.store.claim()
        deferred = self.store.defer(message["id"], "Endpoint unavailable before write", delay=5)
        self.assertEqual(deferred["status"], "queued")
        self.assertGreater(deferred["next_attempt_at"], time.time())
        reopened = Store(self.directory)
        self.assertIsNone(reopened.claim())
        another = self.store.enqueue("alice", "eve", "Independent recipient")
        self.assertEqual(reopened.claim()["id"], another["id"])
        with patch("store.time.time", return_value=deferred["next_attempt_at"] + 1):
            self.assertEqual(reopened.claim()["id"], message["id"])

    def test_deferral_preserves_recipient_order_without_blocking_other_recipients(self):
        first = self.enqueue()
        self.store.claim()
        first = self.store.defer(first["id"], delay=30)
        second = self.enqueue()
        independent = self.store.enqueue("alice", "eve", "Can proceed")
        self.assertEqual(self.store.claim()["id"], independent["id"])
        self.assertIsNone(self.store.claim())
        with patch("store.time.time", return_value=first["next_attempt_at"] + 1):
            self.assertEqual(self.store.claim()["id"], first["id"])
            self.assertIsNone(self.store.claim())
            self.store.finish(first["id"], "unknown")
            self.assertEqual(self.store.claim()["id"], second["id"])

    def test_uncertain_or_finished_delivery_cannot_be_deferred(self):
        for status in ("unknown", "delivered", "held", "refused", "failed"):
            message = self.enqueue()
            self.store.claim()
            self.store.finish(message["id"], status)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.defer(message["id"])
        queued = self.enqueue()
        with self.assertRaises(ValueError):
            self.store.defer(queued["id"])
        self.store.claim()
        for delay in (0, -1, float("nan"), float("inf"), 3601, "5", True):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                self.store.defer(queued["id"], delay=delay)
        self.assertEqual(self.store.get(queued["id"])["status"], "sending")

    def test_cancel_only_pending_messages_before_delivery(self):
        message = self.enqueue()
        cancelled = self.store.cancel(message["id"])
        self.assertEqual(cancelled["status"], "failed")
        self.assertEqual(cancelled["detail"], "Cancelled before delivery")
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.cancel(message["id"])
        deferred = self.enqueue()
        self.store.claim()
        self.store.defer(deferred["id"], delay=30)
        self.assertEqual(self.store.cancel(deferred["id"])["status"], "failed")
        for status in ("sending", "unknown", "held", "delivered", "refused", "failed"):
            message = self.enqueue()
            self.store.claim()
            if status != "sending":
                self.store.finish(message["id"], status)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.cancel(message["id"])
            self.assertEqual(self.store.get(message["id"])["status"], status)
            if status == "sending":
                self.store.finish(message["id"], "unknown")

    def test_old_store_upgrades_without_losing_pending_message(self):
        legacy = Path(self.temp.name) / "legacy"
        legacy.mkdir(mode=0o700)
        database = legacy / "state.sqlite3"
        database.touch(mode=0o600)
        with closing(sqlite3.connect(str(database))) as connection:
            connection.executescript("""
                CREATE TABLE peers(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE messages(id TEXT PRIMARY KEY, sender TEXT, recipient TEXT,
                    body TEXT, reply_to TEXT, kind TEXT, status TEXT, detail TEXT,
                    created_at TEXT, updated_at TEXT, acked_at TEXT);
                INSERT INTO messages VALUES('legacy-id','alice','bob','Keep me',NULL,
                    'message','queued','','2026-09-05','2026-09-05',NULL);
                PRAGMA user_version=1;
            """)
        upgraded = Store(legacy)
        self.assertEqual(upgraded.get("legacy-id")["next_attempt_at"], 0)
        self.assertEqual(upgraded.claim()["body"], "Keep me")

    def test_only_legal_delivery_transitions(self):
        message = self.enqueue()
        with self.assertRaises(ValueError):
            self.store.finish(message["id"], "delivered")
        self.store.claim()
        for status in ("queued", "sending", "completed", "accepted", "nonsense"):
            with self.assertRaises(ValueError):
                self.store.finish(message["id"], status)
        held = self.store.finish(message["id"], "held", "Approval required")
        self.assertEqual(held["status"], "held")
        self.store.finish(message["id"], "delivered")
        with self.assertRaises(ValueError):
            self.store.finish(message["id"], "refused")
        self.assertIsNone(self.store.claim())

    def test_late_authoritative_receipts_resolve_unknown_and_held(self):
        message = self.enqueue()
        self.store.claim()
        self.store.finish(message["id"], "unknown", "Waiting for asynchronous receipt")
        self.store.finish(message["id"], "held", "Approval required")
        acked = self.store.ack(message["id"], "bob")
        self.assertEqual(acked["status"], "held")
        self.assertEqual(self.store.finish(message["id"], "delivered")["status"], "delivered")
        for outcome in ("delivered", "refused"):
            item = self.enqueue()
            self.store.claim()
            self.store.finish(item["id"], "unknown")
            self.assertEqual(self.store.finish(item["id"], outcome)["status"], outcome)
        self.assertIsNone(self.store.claim())

    def test_ack_requires_recipient_and_never_completes_work(self):
        message = self.enqueue()
        with self.assertRaises(ValueError):
            self.store.ack(message["id"], "bob")
        self.store.claim()
        with self.assertRaises(ValueError):
            self.store.ack(message["id"], "eve")
        acked = self.store.ack(message["id"], "bob")
        self.assertEqual(acked["status"], "sending")
        self.assertIsNotNone(acked["acked_at"])
        self.assertEqual(self.store.ack(message["id"], "bob"), acked)
        self.assertEqual(self.store.messages(recipient="bob", unread=True), [])
        self.assertEqual(len(self.store.messages(recipient="bob")), 1)
        self.assertEqual(self.store.messages(recipient="eve"), [])
        self.assertEqual(
            self.store.finish(message["id"], "delivered")["acked_at"], acked["acked_at"]
        )

    def test_correlated_replies_accept_only_sent_states_and_preserve_duplicates(self):
        for status in ("sending", "unknown", "held", "delivered"):
            with self.subTest(status=status):
                request = self.enqueue()
                # Other accepted replies may be queued; claim until this request.
                while True:
                    claimed = self.store.claim()
                    if claimed["id"] == request["id"]:
                        break
                    self.store.finish(claimed["id"], "delivered")
                if status != "sending":
                    self.store.finish(request["id"], status, "Previous receipt")
                row = self.store.receive_reply(
                    request["id"], "bob", "Result", message_id="reply-" + status
                )
                original = self.store.get(request["id"])
                self.assertEqual(original["status"], "delivered")
                self.assertIsNotNone(original["acked_at"])
                self.assertEqual(row["recipient"], "alice")
                self.assertEqual(row["status"], "queued")
                if status == "delivered":
                    self.assertEqual(original["detail"], "Previous receipt")
                self.assertEqual(
                    self.store.receive_reply(request["id"], "bob", "Result", message_id=row["id"]),
                    row,
                )
                self.assertEqual(self.store.get(request["id"]), original)
                before = self.store.messages()
                with self.assertRaises(ValueError):
                    self.store.receive_reply(request["id"], "bob", "Changed", message_id=row["id"])
                with self.assertRaises(ValueError):
                    self.store.receive_reply(request["id"], "eve", "Impostor", message_id="forged")
                self.assertEqual(self.store.messages(), before)
                self.assertEqual(self.store.claim()["id"], row["id"])
                delivered_reply = self.store.finish(row["id"], "delivered")
                self.assertEqual(
                    self.store.receive_reply(request["id"], "bob", "Result", message_id=row["id"]),
                    delivered_reply,
                )

    def test_process_exit_before_reply_commit_rolls_back_every_database_change(self):
        request = self.enqueue()
        self.store.claim()
        self.store.finish(request["id"], "unknown")
        before = self.store.messages()
        # Exit after the final read observes both writes, before context-manager commit.
        code = """
import os, sys
from pathlib import Path
from store import Store
s = Store(Path(sys.argv[1]))
read = s._get
def exit_before_commit(connection, message_id):
    row = read(connection, message_id)
    if message_id == 'crash-reply':
        original = read(connection, sys.argv[2])
        assert original['status'] == 'delivered' and original['acked_at']
        os._exit(23)
    return row
s._get = exit_before_commit
s.receive_reply(sys.argv[2], 'bob', 'Result', message_id='crash-reply')
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.directory), request["id"]],
            env=dict(os.environ, PYTHONPATH=str(SCRIPTS)),
            capture_output=True,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(Store(self.directory).messages(), before)
        self.store.receive_reply(request["id"], "bob", "Result", message_id="crash-reply")
        reopened = Store(self.directory)
        self.assertEqual(reopened.get(request["id"])["status"], "delivered")
        self.assertIsNotNone(reopened.get(request["id"])["acked_at"])
        self.assertEqual(reopened.get("crash-reply")["body"], "Result")

    def test_symlink_state_and_database_are_rejected_without_touching_target(self):
        root = Path(self.temp.name)
        target = root / "untouched"
        target.write_text("do not touch")
        alias = root / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            Store(alias)
        with self.assertRaises(ValueError):
            Store(alias / "nested")
        for filename in (
            "state.sqlite3",
            "state.sqlite3-wal",
            "state.sqlite3-shm",
            "state.sqlite3-journal",
        ):
            location = root / filename.replace(".", "_")
            location.mkdir(mode=0o700)
            (location / filename).symlink_to(target)
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                Store(location)
        self.assertEqual(target.read_text(), "do not touch")

    def test_insecure_existing_state_directory_is_rejected(self):
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError):
            Store(self.directory)

    def test_foreign_user_state_is_rejected(self):
        with patch("store.os.getuid", return_value=os.getuid() + 1000):
            with self.assertRaises(ValueError):
                Store(self.directory)

    def test_database_hardlinks_and_broad_permissions_are_rejected(self):
        database = self.directory / "state.sqlite3"
        database.chmod(0o644)
        with self.assertRaises(ValueError):
            Store(self.directory)
        database.chmod(0o600)
        os.link(database, Path(self.temp.name) / "hardlink")
        with self.assertRaises(ValueError):
            Store(self.directory)


if __name__ == "__main__":
    unittest.main()
