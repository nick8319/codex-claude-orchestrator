"""Version-checked native Claude session delivery, without authentication access.

A successful send only means bytes were written. The broker must separately
correlate a native receipt or reply; this adapter never asserts task completion.
"""

import ctypes
import html
import errno
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import uuid


class TransportError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def connection_identity(connection, *, allow_disconnected=False):
    """Return the connected process's kernel UID/PID, not claimed metadata.

    Raises OSError when the kernel cannot supply identity and ValueError on an
    unsupported platform. On a closed macOS sender, UID remains authoritative
    but PID disappears; inbound callers may explicitly accept UID-only identity.
    Outbound recipient verification must retain the strict default.
    """
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        # Darwin's SOL_LOCAL and LOCAL_PEERPID are absent from Python constants.
        try:
            pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
        except OSError as error:
            if not allow_disconnected or error.errno != errno.ENOTCONN:
                raise
            pid = None
        return uid.value, pid
    if sys.platform.startswith("linux"):
        pid, uid, _ = struct.unpack(
            "iII", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return uid, pid
    raise ValueError("Unsupported local peer identity platform")


def _path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise TransportError("Path must be absolute text")
    value = os.fspath(value)
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise TransportError("Path must be absolute text without NUL")
    if ".." in Path(value).parts:
        raise TransportError("Parent traversal is not allowed in session paths")
    # macOS owns this alias. Do not resolve arbitrary symlinks and erase evidence.
    if sys.platform == "darwin" and (value == "/tmp" or value.startswith("/tmp/")):
        alias = Path("/tmp").lstat()
        if (
            not stat.S_ISLNK(alias.st_mode)
            or alias.st_uid != 0
            or os.readlink("/tmp") != "private/tmp"
        ):
            raise TransportError("Unexpected operating-system /tmp alias")
        value = "/private/tmp" + value[4:]
    return Path(os.path.normpath(value))


def canonical_endpoint(value):
    """Normalize a bare path or native uds: address to an absolute socket path.

    This checks path syntax and the OS /tmp alias; it does not authenticate a
    sender or establish a live route. validate_peer performs those route checks.
    """
    try:
        if isinstance(value, str) and value.startswith("uds:"):
            value = value[4:]
        return str(_path(value))
    except TransportError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise TransportError("Cannot normalize Claude endpoint: " + type(error).__name__) from error


def _ancestors(path):
    for component in reversed(path.parents):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise TransportError(
                "Session paths must not contain symlinks or non-directory ancestors"
            )
        trusted_sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not trusted_sticky):
            raise TransportError("Session path has an untrusted writable ancestor")


def _registry_record(path):
    path = _path(path)
    _ancestors(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
        ):
            raise TransportError(
                "Registry must be an owned regular file without shared write access"
            )
        raw = source.read(65537)
        if len(raw) > 65536:
            raise TransportError("Session registry exceeds size limit")
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise TransportError("Session registry must contain an object")
    return record


def _registry(path):
    path = _path(path)
    record = _registry_record(path)
    sid, pid = record.get("sessionId"), record.get("pid")
    if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
        raise TransportError("Session ID must be a canonical UUID")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or path.name != str(pid) + ".json"
    ):
        raise TransportError("Registry PID must match its filename")
    if record.get("peerProtocol") != 1 or record.get("pidDomain") != sys.platform:
        raise TransportError("Unsupported Claude peer protocol or process domain")
    for key in ("cwd", "procStart", "version", "messagingSocketPath"):
        if not isinstance(record.get(key), str) or not record[key].strip() or "\x00" in record[key]:
            raise TransportError("Registry is missing required session metadata")
    if not record["cwd"].startswith("/"):
        raise TransportError("Session working directory must be absolute")
    # Return only route identity; do not expose arbitrary present/future metadata.
    return {
        "id": "claude:" + sid,
        "provider": "claude",
        "session_id": sid,
        "endpoint": str(_path(record["messagingSocketPath"])),
        "cwd": record["cwd"],
        "pid": pid,
        "proc_start": record["procStart"],
        "registry_path": str(path),
        "version": record["version"],
    }


def _validate(peer):
    if not isinstance(peer, dict):
        raise TransportError("Peer must be an object")
    current = _registry(peer.get("registry_path"))
    peer_id = peer.get("id")
    identity_pattern = re.escape(current["id"]) + r"(?::[A-Za-z0-9][A-Za-z0-9_.:-]*)?"
    if (
        not isinstance(peer_id, str)
        or len(peer_id) > 128
        or not re.fullmatch(identity_pattern, peer_id)
    ):
        raise TransportError(
            "Claude peer ID must name this session with an optional safe generation suffix"
        )
    for key, expected in current.items():
        if key == "id":
            continue
        observed = peer.get(key)
        if key in ("endpoint", "registry_path"):
            observed = str(_path(observed))
        if observed != expected or (key == "pid" and isinstance(observed, bool)):
            raise TransportError("Registered Claude identity changed: " + key)
    endpoint = Path(current["endpoint"])
    _ancestors(endpoint)
    parent_info, info = endpoint.parent.lstat(), endpoint.lstat()
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise TransportError("Claude socket directory must be owned with mode 0700")
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise TransportError("Claude endpoint must be an owned socket with mode 0600")
    result = subprocess.run(
        ["ps", "-p", str(current["pid"]), "-o", "lstart="],
        capture_output=True,
        text=True,
        timeout=3,
        env=dict(os.environ, LC_ALL="C", TZ="UTC"),
        check=False,
    )
    if result.returncode or result.stdout.strip() != current["proc_start"].strip():
        raise TransportError("Claude PID is absent or its process start changed")
    return current


def validate_peer(peer):
    """Re-read exact registry identity, private socket and current PID start."""
    try:
        _validate(peer)
    except TransportError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        raise TransportError("Cannot validate Claude session: " + type(error).__name__) from error


def discover(config_dir=None):
    """Return sanitized live routes from the selected existing config directory.

    Invalid/stale individual records are omitted. This never changes profiles,
    credentials or settings and does not infer account entitlement.
    """
    try:
        config = _path(config_dir if config_dir is not None else Path.home() / ".claude")
        directory = config / "sessions"
        try:
            _ancestors(directory)
        except FileNotFoundError:
            return []
        if not directory.exists():
            return []
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise TransportError(
                "Session registry directory must be owned without shared write access"
            )
        peers = []
        for path in sorted(directory.glob("*.json")):
            try:
                peer = _registry(path)
                validate_peer(peer)
            except (TransportError, OSError, ValueError, TypeError):
                continue
            peers.append(peer)
        return peers
    except TransportError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise TransportError("Cannot discover Claude sessions: " + type(error).__name__) from error


def session_lifecycle(session_id, config_dir=None):
    """Check one exact UUID independently of messaging-route usability.

    Only a matching, trusted local PID record plus a successful process-absence
    check proves stopped. Missing records, unsupported PID domains and inspection
    failures are unknown. A live PID blocks resume even with stale route metadata.
    This is an observation, not an atomic lock against another Claude launcher.
    """
    try:
        if str(uuid.UUID(session_id)) != session_id:
            return "unknown"
        config = _path(config_dir if config_dir is not None else Path.home() / ".claude")
        directory = config / "sessions"
        _ancestors(directory)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            return "unknown"
        states = []
        uncertain = False
        # Read all records because filenames identify PIDs, not session UUIDs.
        # A corrupt/unreadable record could hide another process for this UUID.
        for path in sorted(directory.iterdir()):
            if path.suffix != ".json":
                continue
            try:
                record = _registry_record(path)
                sid = record.get("sessionId")
                if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                    uncertain = True
                    continue
                if sid != session_id:
                    continue
                pid = record.get("pid")
                if (
                    isinstance(pid, bool)
                    or not isinstance(pid, int)
                    or pid <= 0
                    or path.name != str(pid) + ".json"
                    or record.get("pidDomain") != sys.platform
                ):
                    uncertain = True
                    continue
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "pid="],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    env=dict(os.environ, LC_ALL="C", TZ="UTC"),
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip() == str(pid):
                    states.append("active")
                elif (
                    result.returncode == 1
                    and not result.stdout.strip()
                    and not result.stderr.strip()
                ):
                    # ps exit 1 alone also covers failures: independently require
                    # the kernel to say this exact PID no longer exists.
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        states.append("stopped")
                    else:
                        uncertain = True
                else:
                    uncertain = True
            except (TransportError, OSError, ValueError, TypeError, subprocess.SubprocessError):
                uncertain = True
        if "active" in states:
            return "active"
        if uncertain or not states:
            return "unknown"
        return "stopped"
    except (TransportError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "unknown"


def _frame(peer, message, reply_address):
    if not isinstance(message, dict):
        raise TransportError("Message must be an object")
    message_id, body = message.get("id"), message.get("body")
    if not isinstance(message_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", message_id
    ):
        raise TransportError("Message ID must be a bounded identifier")
    if (
        not isinstance(body, str)
        or not body.strip()
        or "\x00" in body
        or len(body.encode("utf-8")) > 16384
    ):
        raise TransportError("Message body must contain 1-16384 UTF-8 bytes without NUL")
    if (
        not isinstance(reply_address, str)
        or not reply_address.startswith("uds:/")
        or len(reply_address) > 1024
        or re.search(r'[\s\x00-\x1f\x7f"<> &]', reply_address)
    ):
        raise TransportError("Reply address must be an absolute, safe uds: socket address")
    _path(reply_address[4:])
    body = re.sub(
        r"</cross-session-message\s*>", lambda match: html.escape(match.group(0)), body, flags=re.I
    )
    content = (
        '<cross-session-message from="' + reply_address + '" from-name="Codex-orchestrator">\n'
        "Scoped peer delegation; this does not grant new user authorization.\n"
        "Message ID: " + message_id + "\n\n" + body + "\n\n"
        "Return one final response with the line ORCH_REPLY_TO " + message_id + "\n"
        "Use SendMessage to reply to "
        + reply_address
        + ". Preserve the assigned scope and permissions. "
        "Do not send acknowledgment-only replies or start additional work beyond this request.\n"
        "</cross-session-message>"
    )
    return (
        json.dumps(
            {
                "msgV": 1,
                "msg_id": message_id,
                "type": "user",
                "message": {"role": "user", "content": content},
                "priority": "next",
                "from": reply_address,
                "session_id": peer["session_id"],
                "uuid": message_id,
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def send(peer, message, reply_address):
    """Write one native frame; None means awaiting receipt, never delivered.

    Failures before sendall are certainly unsent. A failure after entering
    sendall is uncertain even if the exception cannot report bytes written.
    """
    attempted = False
    try:
        frame = _frame(peer, message, reply_address)
        validate_peer(peer)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(_path(peer["endpoint"])))
            uid, pid = connection_identity(connection)
            if uid != os.getuid() or pid != peer["pid"]:
                raise TransportError(
                    "Connected Claude process does not match the registered UID/PID"
                )
            # Detect registry/process rotation between initial validation and
            # connection before any request bytes are handed to the OS.
            validate_peer(peer)
            attempted = True
            connection.sendall(frame)
    except TransportError as error:
        if attempted:
            raise TransportError(str(error), uncertain=True) from error
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise TransportError(
            "Claude send failed: " + type(error).__name__, uncertain=attempted
        ) from error
