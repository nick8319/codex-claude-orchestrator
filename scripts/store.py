"""Private, durable local messages. Delivery receipts never mean task completion.

Only the singleton broker may call recover(), after taking its process lock and
before starting delivery workers. Unknown outcomes require human reconciliation;
there is deliberately no automatic retry or identity-rebinding operation.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Optional
import uuid


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
IDENTITY = ("id", "provider", "session_id", "endpoint", "cwd", "pid", "proc_start", "registry_path")
FINAL_STATUSES = ("delivered", "held", "refused", "unknown", "failed")
TRANSITIONS = {
    "sending": FINAL_STATUSES,
    "unknown": ("held", "delivered", "refused"),
    "held": ("delivered", "refused"),
}
DATABASE_FILES = (
    "state.sqlite3",
    "state.sqlite3-wal",
    "state.sqlite3-shm",
    "state.sqlite3-journal",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("%s must be a 1-128 character identifier" % field)
    return value


def _text(value, field, limit, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("%s must be text without NUL" % field)
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise ValueError("%s must be valid UTF-8" % field) from error
    if length > limit or (not allow_empty and not value.strip()):
        raise ValueError("%s must contain 1-%d UTF-8 bytes" % (field, limit))
    return value


class Store:
    def __init__(self, state_dir: Path):
        # Do not resolve(): that would hide caller-supplied symlink traps.
        self.state_dir = Path(os.path.abspath(os.path.expanduser(str(state_dir))))
        self.path = self.state_dir / "state.sqlite3"
        self._check_path()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_files()
        try:
            descriptor = os.open(
                str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
            descriptor = os.open(str(self.state_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        with self._connection() as connection:
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if journal != "wal":
                raise ValueError("SQLite WAL mode is required")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported store schema version")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS peers (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL REFERENCES peers(id),
                    recipient TEXT NOT NULL REFERENCES peers(id),
                    body TEXT NOT NULL, reply_to TEXT REFERENCES messages(id),
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','sending','delivered','held','refused','unknown','failed')),
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    acked_at TEXT,
                    next_attempt_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS messages_delivery ON messages(status, created_at);
                CREATE INDEX IF NOT EXISTS messages_inbox ON messages(recipient, acked_at);
            """)
            connection.execute("BEGIN IMMEDIATE")
            if "next_attempt_at" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            }:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
                )
            connection.execute("PRAGMA user_version=2")
            connection.commit()

    def _check_path(self):
        for component in reversed((self.state_dir,) + tuple(self.state_dir.parents)):
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("State path must contain only real directories: %s" % component)
            # Root-owned sticky temp directories are safe ancestors; a foreign
            # owner or a non-sticky writable ancestor can replace our directory.
            writable = info.st_mode & 0o022
            trusted_sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.getuid()) or (writable and not trusted_sticky):
                raise ValueError("Untrusted state path ancestor: %s" % component)

    def _check_files(self):
        self._check_path()
        info = self.state_dir.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("State directory must be owned by this user with mode 0700")
        for name in DATABASE_FILES:
            path = self.state_dir / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError(
                    "State database files must be private, owned regular files: %s" % path
                )

    @contextmanager
    def _connection(self, write=False):
        self._check_files()
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA fullfsync=ON")
            connection.execute("PRAGMA checkpoint_fullfsync=ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _get(connection, message_id):
        row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown message: %s" % message_id)
        return dict(row)

    def register(self, peer: dict) -> dict:
        if not isinstance(peer, dict):
            raise ValueError("Peer must be an object")
        for key in ("id", "provider", "session_id"):
            _identifier(peer.get(key), key)
        for key in ("endpoint", "cwd"):
            _text(peer.get(key), key, 4096)
        try:
            payload = json.dumps(peer, sort_keys=True, ensure_ascii=False, allow_nan=False)
            _text(payload, "peer metadata", 32768)
        except (TypeError, UnicodeError) as error:
            raise ValueError("Peer metadata must be JSON serializable") from error
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT data FROM peers WHERE id=?", (peer["id"],)).fetchone()
            if row:
                previous = json.loads(row["data"])
                if any(previous.get(key) != peer.get(key) for key in IDENTITY):
                    raise ValueError("Peer identity is immutable; enroll a new peer ID")
            connection.execute(
                "INSERT INTO peers(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (peer["id"], payload),
            )
        return json.loads(payload)

    def peers(self) -> list:
        with self._connection() as connection:
            return [
                json.loads(row["data"])
                for row in connection.execute("SELECT data FROM peers ORDER BY id")
            ]

    def peer(self, peer_id) -> dict:
        _identifier(peer_id, "peer_id")
        with self._connection() as connection:
            row = connection.execute("SELECT data FROM peers WHERE id=?", (peer_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown peer: %s" % peer_id)
            return json.loads(row["data"])

    def enqueue(
        self, sender, recipient, body, *, message_id=None, reply_to=None, kind="message"
    ) -> dict:
        message_id = str(uuid.uuid4()) if message_id is None else message_id
        for value, field in (
            (sender, "sender"),
            (recipient, "recipient"),
            (message_id, "message_id"),
            (kind, "kind"),
        ):
            _identifier(value, field)
        _text(body, "body", 16384)
        if reply_to is not None:
            _identifier(reply_to, "reply_to")
        envelope = dict(
            id=message_id,
            sender=sender,
            recipient=recipient,
            body=body,
            reply_to=reply_to,
            kind=kind,
        )
        with self._connection(write=True) as connection:
            previous = connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if previous:
                if any(previous[key] != value for key, value in envelope.items()):
                    raise ValueError("Message ID already exists with different payload")
                return dict(previous)
            for peer_id in (sender, recipient):
                if not connection.execute("SELECT 1 FROM peers WHERE id=?", (peer_id,)).fetchone():
                    raise KeyError("Unknown peer: %s" % peer_id)
            if reply_to is not None:
                self._get(connection, reply_to)
            now = _now()
            connection.execute(
                """INSERT INTO messages
                (id,sender,recipient,body,reply_to,kind,status,created_at,updated_at)
                VALUES(:id,:sender,:recipient,:body,:reply_to,:kind,'queued',:now,:now)""",
                dict(envelope, now=now),
            )
            return self._get(connection, message_id)

    def receive_reply(self, original_id, sender, body, *, message_id, before_insert=None) -> dict:
        """Atomically accept a verified reply, delivery receipt and read ACK.

        Queued/refused/failed originals reject replies; sending/unknown/held
        replies establish delivery. A delivered original accepts further replies.
        before_insert may durably publish a body artifact after all validation;
        it must not re-enter the store. A crash can leave an unreferenced artifact,
        but never a committed reply pointing at an unpublished artifact.
        """
        for value, field in (
            (original_id, "original_id"),
            (sender, "sender"),
            (message_id, "message_id"),
        ):
            _identifier(value, field)
        _text(body, "body", 16384)
        with self._connection(write=True) as connection:
            original = self._get(connection, original_id)
            if original["recipient"] != sender:
                raise ValueError("Reply sender does not own this delivery")
            if original["status"] not in ("sending", "unknown", "held", "delivered"):
                raise ValueError("Cannot accept reply to an undelivered message")
            envelope = dict(
                id=message_id,
                sender=sender,
                recipient=original["sender"],
                body=body,
                reply_to=original_id,
                kind="reply",
            )
            previous = connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if previous:
                if any(previous[key] != value for key, value in envelope.items()):
                    raise ValueError("Message ID already exists with different payload")
            else:
                if before_insert is not None:
                    before_insert()
                now = _now()
                connection.execute(
                    """INSERT INTO messages
                    (id,sender,recipient,body,reply_to,kind,status,created_at,updated_at)
                    VALUES(:id,:sender,:recipient,:body,:reply_to,:kind,'queued',:now,:now)""",
                    dict(envelope, now=now),
                )
            now = _now()
            if original["status"] != "delivered":
                connection.execute(
                    """UPDATE messages SET status='delivered',
                    detail='Correlated reply received',updated_at=? WHERE id=?""",
                    (now, original_id),
                )
            if original["acked_at"] is None:
                connection.execute(
                    "UPDATE messages SET acked_at=?,updated_at=? WHERE id=?",
                    (now, now, original_id),
                )
            return self._get(connection, message_id)

    def messages(self, recipient=None, unread=False) -> list:
        clauses, parameters = [], []
        if recipient is not None:
            clauses.append("recipient=?")
            parameters.append(_identifier(recipient, "recipient"))
        if unread:
            clauses.append("acked_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM messages" + where + " ORDER BY created_at,id", parameters
                )
            ]

    def get(self, message_id) -> dict:
        _identifier(message_id, "message_id")
        with self._connection() as connection:
            return self._get(connection, message_id)

    def claim(self) -> Optional[dict]:
        """Keep per-recipient send order while other recipients remain independent."""
        with self._connection(write=True) as connection:
            row = connection.execute(
                """SELECT candidate.id FROM messages AS candidate
                WHERE candidate.status='queued' AND candidate.next_attempt_at<=?
                    AND NOT EXISTS (
                        SELECT 1 FROM messages AS earlier
                        WHERE earlier.recipient=candidate.recipient
                            AND earlier.status IN ('queued','sending')
                            AND (earlier.created_at<candidate.created_at OR
                                (earlier.created_at=candidate.created_at AND earlier.id<candidate.id))
                    )
                ORDER BY candidate.created_at,candidate.id LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE messages SET status='sending',updated_at=? WHERE id=?", (_now(), row["id"])
            )
            return self._get(connection, row["id"])

    def defer(self, message_id, detail="", delay=5) -> dict:
        """Back off only when the adapter proves no transport write occurred."""
        _identifier(message_id, "message_id")
        _text(detail, "detail", 16384, allow_empty=True)
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or not 0 < delay <= 3600
        ):
            raise ValueError(
                "Deferral delay must be a finite number above 0 and at most 3600 seconds"
            )
        with self._connection(write=True) as connection:
            message = self._get(connection, message_id)
            if message["status"] != "sending":
                raise ValueError("Only a definitely unsent sending message can be deferred")
            connection.execute(
                "UPDATE messages SET status='queued',detail=?,updated_at=?,next_attempt_at=? WHERE id=?",
                (detail, _now(), time.time() + delay, message_id),
            )
            return self._get(connection, message_id)

    def cancel(self, message_id) -> dict:
        """Withdraw a queued request atomically before any worker can claim it."""
        _identifier(message_id, "message_id")
        with self._connection(write=True) as connection:
            message = self._get(connection, message_id)
            if message["status"] != "queued":
                raise ValueError("Only queued messages can be cancelled before delivery")
            connection.execute(
                "UPDATE messages SET status='failed',detail='Cancelled before delivery',updated_at=? WHERE id=?",
                (_now(), message_id),
            )
            return self._get(connection, message_id)

    def finish(self, message_id, status, detail="") -> dict:
        """Record a transport result, or a late receipt verified by its adapter."""
        _identifier(message_id, "message_id")
        if status not in FINAL_STATUSES:
            raise ValueError("Invalid delivery status")
        _text(detail, "detail", 16384, allow_empty=True)
        with self._connection(write=True) as connection:
            message = self._get(connection, message_id)
            if status not in TRANSITIONS.get(message["status"], ()):
                raise ValueError(
                    "Invalid delivery transition: %s -> %s" % (message["status"], status)
                )
            connection.execute(
                "UPDATE messages SET status=?,detail=?,updated_at=? WHERE id=?",
                (status, detail, _now(), message_id),
            )
            return self._get(connection, message_id)

    def ack(self, message_id, recipient) -> dict:
        _identifier(message_id, "message_id")
        _identifier(recipient, "recipient")
        with self._connection(write=True) as connection:
            message = self._get(connection, message_id)
            if message["recipient"] != recipient:
                raise ValueError("Only the message recipient may acknowledge reading it")
            if message["status"] not in ("sending", "delivered", "unknown", "held"):
                raise ValueError("Cannot acknowledge an undelivered message")
            if message["acked_at"] is None:
                now = _now()
                connection.execute(
                    "UPDATE messages SET acked_at=?,updated_at=? WHERE id=?", (now, now, message_id)
                )
            return self._get(connection, message_id)

    def recover(self) -> int:
        """Call only once exclusive broker ownership is established, never on reads."""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """UPDATE messages SET status='unknown',
                detail='Broker stopped during delivery; reconcile before any new send',updated_at=?
                WHERE status='sending'""",
                (_now(),),
            )
            return cursor.rowcount
