"""Local durable inbox. No credentials, model calls or protected Desktop IPC."""

import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import sys
import threading
import tempfile
from urllib.parse import unquote

from store import Store
from claude_transport import connection_identity

MAX_FRAME = 65536
REPLY_PATTERN = re.compile(r"ORCH_REPLY_TO\s+([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})")


def parse_reply_id(text):
    matches = set(REPLY_PATTERN.findall(text))
    if len(matches) != 1:
        raise ValueError("Reply needs exactly one ORCH_REPLY_TO message ID")
    return matches.pop()


def private_socket_directory(path):
    path = Path(path)
    # /tmp is the OS alias on macOS; no user-created aliases are accepted.
    if str(path).startswith("/tmp/"):
        path = Path("/private/tmp") / path.relative_to("/tmp") if sys.platform == "darwin" else path
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Socket directory must not contain symlinks")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Socket directory must be owned and mode 0700")
    return path


class Broker:
    def __init__(self, state_dir):
        self.store = Store(state_dir)
        self.reply_address = None
        self.stopping = threading.Event()
        self.receipt_lock = threading.RLock()

    def receive_verified(self, frame, peer):
        """Called only after kernel PID and enrolled registry validation."""
        with self.receipt_lock:
            return self._receive_verified(frame, peer)

    def _receive_verified(self, frame, peer):
        if frame.get("type") == "control" and frame.get("action") == "peer_message_status":
            original = self.store.get(frame.get("orig_msg_id"))
            if original["recipient"] != peer["id"]:
                raise ValueError("Receipt sender does not own this delivery")
            status = {
                "held": "held",
                "delivered": "delivered",
                "denied": "refused",
                "refused": "refused",
                "expired": "refused",
            }.get(frame.get("status"))
            if status is None:
                raise ValueError("Unknown native receipt status")
            if original["status"] == status or original["status"] in (
                "delivered",
                "refused",
                "failed",
            ):
                return original  # Duplicate/out-of-order receipts cannot regress delivery.
            return self.store.finish(original["id"], status, str(frame.get("reason", ""))[:2000])
        if frame.get("type") != "user" or not isinstance(frame.get("message"), dict):
            raise ValueError("Unsupported frame")
        body = frame["message"].get("content")
        if not isinstance(body, str):
            raise ValueError("Reply body must be text")
        original = self.store.get(parse_reply_id(body))
        if original["recipient"] != peer["id"]:
            raise ValueError("Reply sender does not own this delivery")
        if not frame.get("msg_id"):
            raise ValueError("Native reply must have a stable message ID")
        persist = None
        if len(body.encode("utf-8")) > 16384:
            raw_body = body
            body = self.artifact_reference(raw_body, original["id"])
            persist = partial(self.save_artifact, raw_body, original["id"])
        return self.store.receive_reply(
            original["id"], peer["id"], body, message_id=frame["msg_id"], before_insert=persist
        )

    def artifact_reference(self, body, original_id):
        raw = body.encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise ValueError("Reply exceeds native frame budget")
        if "\x00" in body:
            raise ValueError("Reply body must be text without NUL")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.store.state_dir / "artifacts" / (digest + ".txt")
        return (
            "ORCH_REPLY_TO "
            + original_id
            + "\nLong reply saved as private artifact: "
            + str(path)
            + "\nSHA256: "
            + digest
        )

    def save_artifact(self, body, original_id):
        raw = body.encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise ValueError("Reply exceeds native frame budget")
        directory = self.store.state_dir / "artifacts"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Unsafe artifact directory")
        digest = hashlib.sha256(raw).hexdigest()
        path = directory / (digest + ".txt")
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("Unsafe artifact file")
        fd, temporary = tempfile.mkstemp(prefix=".reply-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            # Only publish a fully durable body. Replaying the same content can
            # repair a partial legacy file; its destination is its exact digest.
            os.replace(temporary, path)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.artifact_reference(body, original_id)

    def mark_unknown_if_sending(self, message_id, detail):
        with self.receipt_lock:
            if self.store.get(message_id)["status"] == "sending":
                self.store.finish(message_id, "unknown", detail)

    def receive(self, frame, uid, pid):
        from claude_transport import validate_peer, canonical_endpoint

        if uid != os.getuid():
            raise ValueError("Foreign OS user")
        address = frame.get("from", "")
        if not isinstance(address, str) or not address.startswith("uds:"):
            raise ValueError("Missing native sender address")
        source = canonical_endpoint(unquote(address[4:]))
        if frame.get("type") == "control":
            correlation = frame.get("orig_msg_id")
        else:
            correlation = parse_reply_id(frame.get("message", {}).get("content", ""))
        original = self.store.get(correlation)
        peer = self.store.peer(original["recipient"])
        if (
            peer["provider"] != "claude"
            or canonical_endpoint(peer["endpoint"]) != source
            or (pid is not None and peer.get("pid") != pid)
        ):
            raise ValueError("Sender is not enrolled with this process identity")
        validate_peer(peer)
        return self.receive_verified(frame, peer)

    def dispatch_one(self):
        message = self.store.claim()
        if message is None:
            return False
        peer = self.store.peer(message["recipient"])
        if peer["provider"] == "codex":
            self.store.finish(
                message["id"],
                "delivered",
                "Durable Codex inbox available; not yet read by the model. Use wait or native heartbeat.",
            )
            return True
        if peer["provider"] != "claude":
            self.store.finish(message["id"], "failed", "Unsupported peer provider")
            return True
        from claude_transport import send, TransportError

        if not self.reply_address:
            self.store.defer(message["id"], "Broker reply listener is not ready", delay=5)
            return False
        try:
            send(peer, message, self.reply_address)
        except TransportError as error:
            if error.uncertain:
                self.mark_unknown_if_sending(
                    message["id"], "Write outcome unknown; reconcile before retry"
                )
            else:
                self.store.defer(message["id"], str(error)[:2000], delay=30)
        else:
            # Successful sendall is not a receipt. A callback may already have
            # changed the status while the sending thread returned.
            self.mark_unknown_if_sending(
                message["id"], "Written to native socket; awaiting receipt or reply"
            )
        return True


class InboxServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, broker):
        self.broker = broker
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, InboxHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class InboxHandler(socketserver.StreamRequestHandler):
    server: InboxServer
    timeout = 3

    def handle(self):
        try:
            uid, pid = connection_identity(self.request, allow_disconnected=True)
            for _ in range(8):
                line = self.rfile.readline(MAX_FRAME + 1)
                if not line:
                    return
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise ValueError("Frame exceeds limit or is incomplete")
                try:
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise ValueError("Frame must be an object")
                    if frame.get("type") == "auth":
                        continue  # Never persist or log optional native token frames.
                    self.server.broker.receive(frame, uid, pid)
                except (ValueError, KeyError, TypeError) as error:
                    print(
                        json.dumps({"event": "frame_rejected", "error": str(error)[:300]}),
                        flush=True,
                    )
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(json.dumps({"event": "inbox_rejected", "error": str(error)[:300]}), flush=True)


def serve(state_dir, socket_path="/tmp/cc-socks/codex-orchestrator.sock"):
    os.umask(0o077)
    broker = Broker(state_dir)
    lock_path = broker.store.state_dir / "daemon.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Unsafe daemon lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent = private_socket_directory(Path(socket_path).parent)
        actual = parent / Path(socket_path).name
        if os.path.lexists(actual):
            info = actual.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Refusing to replace non-owned or non-socket path")
            with socket.socket(socket.AF_UNIX) as check:
                check.settimeout(0.2)
                try:
                    check.connect(str(actual))
                except (ConnectionRefusedError, FileNotFoundError):
                    actual.unlink()
                else:
                    raise ValueError("Reply socket is already live")
        server = InboxServer(str(actual), broker)
        broker.reply_address = "uds:" + socket_path
        broker.store.recover()
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()

        def stop(_signal, _frame):
            broker.stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(
            json.dumps(
                {"event": "ready", "pid": os.getpid(), "reply_address": broker.reply_address}
            ),
            flush=True,
        )
        try:
            while not broker.stopping.is_set():
                if not broker.dispatch_one():
                    broker.stopping.wait(1)
        finally:
            server.shutdown()
            server.server_close()
            actual.unlink(missing_ok=True)
    finally:
        os.close(fd)
