"""Real Unix sockets, separate broker process, kernel identity and crash restart."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from claude_transport import discover
from store import Store


class DaemonTests(unittest.TestCase):
    def test_roundtrip_duplicate_frames_and_restart_keep_unread_reply(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="orch-wire-") as temp:
            base = Path(temp)
            sockets = base / "sockets"
            sockets.mkdir(mode=0o700)
            config = base / "claude"
            (config / "sessions").mkdir(parents=True)
            endpoint = sockets / (str(os.getpid()) + ".sock")
            broker_path = sockets / "broker.sock"
            session = str(uuid.uuid4())
            start = subprocess.check_output(
                ["ps", "-p", str(os.getpid()), "-o", "lstart="],
                text=True,
                env=dict(os.environ, LC_ALL="C", TZ="UTC"),
            ).strip()
            record = {
                "pid": os.getpid(),
                "sessionId": session,
                "cwd": str(base),
                "procStart": start,
                "version": "2.1.261",
                "peerProtocol": 1,
                "pidDomain": sys.platform,
                "messagingSocketPath": str(endpoint),
            }
            (config / "sessions" / (str(os.getpid()) + ".json")).write_text(json.dumps(record))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as native:
                native.bind(str(endpoint))
                endpoint.chmod(0o600)
                native.listen(2)
                native.settimeout(5)
                peer = discover(config)[0]
                state = base / "state"
                store = Store(state)
                store.register(peer)
                root = {
                    "id": "codex:root",
                    "provider": "codex",
                    "session_id": str(uuid.uuid4()),
                    "endpoint": "local-inbox",
                    "cwd": str(base),
                }
                store.register(root)
                original = store.enqueue(root["id"], peer["id"], "One bounded operation")
                argv = [
                    sys.executable,
                    str(ROOT / "scripts/orchestrator.py"),
                    "--state-dir",
                    str(state),
                    "serve",
                    "--socket",
                    str(broker_path),
                ]
                proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
                try:
                    with native.accept()[0] as conn:
                        frame = json.loads(conn.makefile("rb").readline())
                    self.assertEqual(frame["session_id"], session)
                    self.assertEqual(frame["msg_id"], original["id"])
                    reply = {
                        "msgV": 1,
                        "msg_id": str(uuid.uuid4()),
                        "type": "user",
                        "from": "uds:" + str(endpoint),
                        "message": {
                            "role": "user",
                            "content": "ORCH_REPLY_TO " + original["id"] + "\nVerified result",
                        },
                    }
                    receipt = {
                        "type": "control",
                        "action": "peer_message_status",
                        "status": "delivered",
                        "orig_msg_id": original["id"],
                        "from": "uds:" + str(endpoint),
                    }
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as out:
                        out.connect(str(broker_path))
                        out.sendall(
                            (
                                json.dumps(receipt)
                                + "\n"
                                + json.dumps(receipt)
                                + "\n"
                                + "invalid json\n"
                                + json.dumps(reply)
                                + "\n"
                                + json.dumps(reply)
                                + "\n"
                            ).encode()
                        )
                    until = time.monotonic() + 5
                    while time.monotonic() < until:
                        rows = store.messages(root["id"], unread=True)
                        if rows and rows[0]["status"] == "delivered":
                            break
                        time.sleep(0.05)
                    if not rows:
                        proc.terminate()
                        output, error = proc.communicate(timeout=5)
                        self.fail("Reply missing; broker output: " + output + error)
                    self.assertEqual(len(rows), 1)
                    self.assertIn("Verified result", rows[0]["body"])
                    self.assertIsNone(rows[0]["acked_at"])
                    proc.kill()
                    proc.communicate(timeout=5)
                    proc = subprocess.Popen(
                        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    time.sleep(0.3)
                    self.assertIsNone(proc.poll())
                    self.assertEqual(len(Store(state).messages(root["id"], unread=True)), 1)
                    native.settimeout(0.2)
                    with self.assertRaises(socket.timeout):
                        native.accept()  # Completed native send was not replayed after crash.
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    stdout, stderr = proc.communicate(timeout=5)
                    self.assertNotIn("Traceback", stderr, stdout + stderr)


if __name__ == "__main__":
    unittest.main()
