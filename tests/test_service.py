import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import hashlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from store import Store
from service import Broker, parse_reply_id


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name).resolve()
        self.store = Store(self.state)
        self.root = {
            "id": "codex:root",
            "provider": "codex",
            "session_id": str(uuid.uuid4()),
            "endpoint": "local-inbox",
            "cwd": str(self.state),
        }
        self.peer = {
            "id": "claude:peer",
            "provider": "claude",
            "session_id": str(uuid.uuid4()),
            "endpoint": "/tmp/cc-socks/test.sock",
            "cwd": str(self.state),
            "pid": os.getpid(),
        }
        self.store.register(self.root)
        self.store.register(self.peer)
        self.broker = Broker(self.state)

    def tearDown(self):
        self.temp.cleanup()

    def outgoing(self):
        row = self.store.enqueue(self.root["id"], self.peer["id"], "Check one result")
        self.store.claim()
        self.store.finish(row["id"], "unknown")
        return row

    def test_reply_routes_by_original_message_and_deduplicates(self):
        sent = self.outgoing()
        f = {
            "msg_id": str(uuid.uuid4()),
            "type": "user",
            "from": "uds:" + self.peer["endpoint"],
            "message": {"role": "user", "content": "ORCH_REPLY_TO " + sent["id"] + "\nDone"},
        }
        self.broker.receive_verified(f, self.peer)
        self.broker.receive_verified(f, self.peer)
        rows = self.store.messages(recipient=self.root["id"], unread=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reply_to"], sent["id"])
        self.assertEqual(self.store.get(sent["id"])["status"], "delivered")
        self.assertIsNotNone(self.store.get(sent["id"])["acked_at"])

    def test_rejected_reply_leaves_no_inbox_or_artifact(self):
        for status in ("queued", "cancelled", "refused"):
            for long in (False, True):
                with self.subTest(status=status, long=long):
                    sent = self.store.enqueue(self.root["id"], self.peer["id"], "Check")
                    if status == "cancelled":
                        self.store.cancel(sent["id"])
                    elif status == "refused":
                        # Claim this request without older queued requests blocking it.
                        with self.store._connection(write=True) as connection:
                            connection.execute(
                                "UPDATE messages SET status='sending' WHERE id=?", (sent["id"],)
                            )
                        self.store.finish(sent["id"], "refused")
                    before = self.store.messages()
                    body = "ORCH_REPLY_TO " + sent["id"] + "\n" + "x" * (20000 if long else 10)
                    with self.assertRaises(ValueError):
                        self.broker.receive_verified(
                            {
                                "type": "user",
                                "msg_id": str(uuid.uuid4()),
                                "message": {"content": body},
                            },
                            self.peer,
                        )
                    self.assertEqual(self.store.messages(), before)
                    self.assertFalse((self.state / "artifacts").exists())

    def test_failed_original_ack_rolls_back_reply_and_delivery(self):
        sent = self.outgoing()
        before = self.store.messages()
        with self.store._connection(write=True) as connection:
            connection.execute("""CREATE TRIGGER fail_ack BEFORE UPDATE OF acked_at ON messages
                WHEN NEW.acked_at IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'injected ACK failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected ACK failure"):
            self.broker.receive_verified(
                {
                    "type": "user",
                    "msg_id": "rollback-reply",
                    "message": {"content": "ORCH_REPLY_TO " + sent["id"] + "\nDone"},
                },
                self.peer,
            )
        self.assertEqual(Store(self.state).messages(), before)

    def test_conflicting_long_duplicate_does_not_publish_artifact(self):
        sent = self.outgoing()
        frame = {
            "type": "user",
            "msg_id": "duplicate",
            "message": {"content": "ORCH_REPLY_TO " + sent["id"] + "\nDone"},
        }
        self.broker.receive_verified(frame, self.peer)
        before = self.store.messages()
        frame["message"]["content"] += "x" * 20000
        with self.assertRaises(ValueError):
            self.broker.receive_verified(frame, self.peer)
        self.assertEqual(self.store.messages(), before)
        self.assertFalse((self.state / "artifacts").exists())

    def test_reply_cannot_claim_different_recipient(self):
        sent = self.outgoing()
        impostor = dict(self.peer, id="claude:other")
        self.store.register(impostor)
        f = {
            "msg_id": str(uuid.uuid4()),
            "type": "user",
            "from": "uds:" + impostor["endpoint"],
            "message": {"content": "ORCH_REPLY_TO " + sent["id"] + "\nDone"},
        }
        with self.assertRaises(ValueError):
            self.broker.receive_verified(f, impostor)
        self.assertEqual(self.store.messages(recipient=self.root["id"]), [])

    def test_unattributed_reply_is_rejected_instead_of_guessed(self):
        self.outgoing()
        with self.assertRaises(ValueError):
            self.broker.receive_verified(
                {"type": "user", "msg_id": str(uuid.uuid4()), "message": {"content": "Done"}},
                self.peer,
            )

    def test_hold_and_release_are_distinct_without_reply_loop(self):
        sent = self.outgoing()
        for native, expected in [("held", "held"), ("delivered", "delivered")]:
            self.broker.receive_verified(
                {
                    "type": "control",
                    "action": "peer_message_status",
                    "orig_msg_id": sent["id"],
                    "status": native,
                    "reason": "native receipt",
                },
                self.peer,
            )
            self.assertEqual(self.store.get(sent["id"])["status"], expected)
        self.assertEqual(self.store.messages(recipient=self.root["id"]), [])
        self.assertIsNone(self.store.get(sent["id"])["acked_at"])

    def test_ambiguous_correlation_rejected(self):
        with self.assertRaises(ValueError):
            parse_reply_id(
                "ORCH_REPLY_TO " + str(uuid.uuid4()) + "\nORCH_REPLY_TO " + str(uuid.uuid4())
            )

    def test_codex_delivery_is_mailbox_only_and_still_unread(self):
        row = self.store.enqueue(self.peer["id"], self.root["id"], "Reply", kind="reply")
        self.broker.dispatch_one()
        got = self.store.get(row["id"])
        self.assertEqual(got["status"], "delivered")
        self.assertIn("not yet read", got["detail"])
        self.assertIsNone(got["acked_at"])

    def test_duplicate_and_stale_receipts_do_not_overwrite_delivery(self):
        sent = self.outgoing()
        f = {"type": "control", "action": "peer_message_status", "orig_msg_id": sent["id"]}
        for status in ("held", "delivered", "delivered", "held"):
            self.broker.receive_verified(dict(f, status=status), self.peer)
        self.assertEqual(self.store.get(sent["id"])["status"], "delivered")

    def test_late_receipt_wins_over_uncertain_send_failure(self):
        from claude_transport import TransportError

        sent = self.store.enqueue(self.root["id"], self.peer["id"], "Check")
        self.broker.reply_address = "uds:/tmp/cc-socks/broker.sock"

        def receipt_then_error(peer, row, address):
            self.store.finish(row["id"], "delivered", "Native receipt")
            raise TransportError("write error after receipt", uncertain=True)

        with patch("claude_transport.send", receipt_then_error):
            self.broker.dispatch_one()
        self.assertEqual(self.store.get(sent["id"])["status"], "delivered")

    def test_receipt_uses_original_recipient_with_multiple_enrolled_aliases(self):
        sent = self.outgoing()
        self.store.register(dict(self.peer, id="claude:peer:g2"))
        f = {
            "type": "control",
            "action": "peer_message_status",
            "orig_msg_id": sent["id"],
            "status": "delivered",
            "from": "uds:" + self.peer["endpoint"],
        }
        with patch("claude_transport.validate_peer"):
            self.broker.receive(f, os.getuid(), os.getpid())
        self.assertEqual(self.store.get(sent["id"])["status"], "delivered")

    def test_long_reply_is_preserved_as_private_artifact(self):
        sent = self.outgoing()
        body = "ORCH_REPLY_TO " + sent["id"] + "\n" + "x" * 20000
        frame = {"type": "user", "msg_id": str(uuid.uuid4()), "message": {"content": body}}
        row = self.broker.receive_verified(frame, self.peer)
        self.assertIn("artifact", row["body"])
        files = list((self.state / "artifacts").glob("*.txt"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), body)

    def test_artifact_publication_failure_does_not_accept_reply(self):
        sent = self.outgoing()
        before = self.store.messages()
        body = "ORCH_REPLY_TO " + sent["id"] + "\n" + "x" * 20000
        with patch("service.os.replace", side_effect=OSError("injected publication failure")):
            with self.assertRaisesRegex(OSError, "injected publication failure"):
                self.broker.receive_verified(
                    {"type": "user", "msg_id": "artifact-failure", "message": {"content": body}},
                    self.peer,
                )
        self.assertEqual(Store(self.state).messages(), before)
        self.assertEqual(list((self.state / "artifacts").iterdir()), [])

    def test_malformed_long_reply_does_not_publish_artifact(self):
        sent = self.outgoing()
        before = self.store.messages()
        for message_id, suffix in (("bad id", ""), ("valid-id", "\x00")):
            with self.subTest(message_id=message_id), self.assertRaises(ValueError):
                self.broker.receive_verified(
                    {
                        "type": "user",
                        "msg_id": message_id,
                        "message": {
                            "content": "ORCH_REPLY_TO " + sent["id"] + "\n" + "x" * 20000 + suffix
                        },
                    },
                    self.peer,
                )
        self.assertEqual(self.store.messages(), before)
        self.assertFalse((self.state / "artifacts").exists())

    def test_long_reply_recovers_partial_artifact_from_interrupted_write(self):
        sent = self.outgoing()
        body = "ORCH_REPLY_TO " + sent["id"] + "\n" + "z" * 20000
        directory = self.state / "artifacts"
        directory.mkdir(mode=0o700)
        path = directory / (hashlib.sha256(body.encode()).hexdigest() + ".txt")
        path.write_text(body[:50])
        path.chmod(0o600)
        self.broker.receive_verified(
            {"type": "user", "msg_id": str(uuid.uuid4()), "message": {"content": body}}, self.peer
        )
        self.assertEqual(path.read_text(), body)


if __name__ == "__main__":
    unittest.main()
