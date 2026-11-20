#!/usr/bin/env python3
"""Global orchestration mailbox CLI; all output is JSON except argparse help."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
import uuid

# Direct invocation must not add unmanaged files to the installed skill.
sys.dont_write_bytecode = True

from store import Store  # noqa: E402 - set bytecode policy before local imports
from claude_transport import TransportError  # noqa: E402


def default_peer():
    thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    return "codex:" + thread if thread else None


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", default=str(Path.home() / ".local/state/codex-orchestrator"))
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("discover", help="Read live Claude session identities without sending")
    q.add_argument("--config-dir")
    q = sub.add_parser("enroll-root", help="Register this exact Codex task as a durable inbox")
    q.add_argument(
        "--thread", default=os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    )
    q.add_argument("--cwd", default=os.getcwd())
    q = sub.add_parser("enroll", help="Pin one discovered Claude process/session identity")
    q.add_argument("--session", required=True)
    q.add_argument("--config-dir")
    q.add_argument(
        "--peer-id", help="Explicit new generation ID after restarting an enrolled session"
    )
    sub.add_parser("peers")
    q = sub.add_parser("send", help="Persist a scoped message; daemon performs native delivery")
    q.add_argument("--from", dest="sender", default=default_peer())
    q.add_argument("--to", required=True)
    q.add_argument("--body-file", required=True)
    q.add_argument("--message-id")
    q.add_argument("--reply-to")
    for command in ("inbox", "wait"):
        q = sub.add_parser(command, help="Read unread inbox; reading does not ACK")
        q.add_argument("--peer", default=default_peer())
        if command == "wait":
            q.add_argument("--timeout", type=float, default=50)
        else:
            q.add_argument("--all", action="store_true")
    q = sub.add_parser("ack", help="Acknowledge specific messages after processing")
    q.add_argument("--peer", default=default_peer())
    q.add_argument("message_ids", nargs="+")
    q = sub.add_parser("cancel", help="Cancel only a message not yet sent")
    q.add_argument("message_id")
    q = sub.add_parser("status", help="Compact delivery state, without message contents")
    q.add_argument("--peer", help="Filter to messages sent or received by this peer")
    q = sub.add_parser("serve", help="Run the singleton broker (normally launchd-owned)")
    q.add_argument("--socket", default="/tmp/cc-socks/codex-orchestrator.sock")
    sub.add_parser("models", help="Read the dated model catalog")
    q = sub.add_parser("headless", help="Run Claude CLI with a durable local receipt")
    q.add_argument("--cwd", required=True)
    q.add_argument("--brief", required=True)
    q.add_argument("--model", default="claude-fable-5-1")
    q.add_argument("--effort", default="high")
    q.add_argument("--mode", choices=("read-only", "workspace-write"), default="read-only")
    q.add_argument(
        "--billing-policy",
        choices=("subscription-only", "native-metering"),
        default="subscription-only",
        help="native-metering requires explicit user authority; subscription-only blocks unless no-overage is verified",
    )
    q.add_argument("--resume")
    q.add_argument("--preflight", action="store_true")
    return p


def require_peer(store, peer):
    if not peer:
        raise ValueError("Specify --peer / --from or run inside an identified Codex task")
    return store.peer(peer)


def execute(args):
    state = Path(args.state_dir).expanduser()
    if args.command == "models":
        return json.loads(
            (Path(__file__).resolve().parents[1] / "references/models.json").read_text()
        )
    if args.command == "discover":
        from claude_transport import discover

        return discover(args.config_dir)
    if args.command == "headless":
        from headless import preflight, run

        fn = preflight if args.preflight else run
        return fn(
            args.cwd,
            args.brief,
            model=args.model,
            effort=args.effort,
            mode=args.mode,
            state_dir=state,
            resume=args.resume,
            billing_policy=args.billing_policy,
        )
    if args.command == "serve":
        from service import serve

        serve(state, args.socket)
        return None
    store = Store(state)
    if args.command == "enroll-root":
        if not args.thread:
            raise ValueError("An exact --thread UUID is required; recent-task guessing is disabled")
        thread = str(uuid.UUID(args.thread))
        cwd = str(Path(args.cwd).resolve(strict=True))
        return store.register(
            {
                "id": "codex:" + thread,
                "provider": "codex",
                "session_id": thread,
                "endpoint": "local-inbox",
                "cwd": cwd,
            }
        )
    if args.command == "enroll":
        from claude_transport import discover

        matches = [p for p in discover(args.config_dir) if p["session_id"] == args.session]
        if len(matches) != 1:
            raise ValueError("Expected exactly one live Claude session for the supplied UUID")
        peer = matches[0]
        if args.peer_id:
            peer["id"] = args.peer_id
        return store.register(peer)
    if args.command == "peers":
        return store.peers()
    if args.command == "send":
        require_peer(store, args.sender)
        return store.enqueue(
            args.sender,
            args.to,
            Path(args.body_file).read_text(),
            message_id=args.message_id,
            reply_to=args.reply_to,
            kind="reply" if args.reply_to else "message",
        )
    if args.command in ("inbox", "wait"):
        require_peer(store, args.peer)
        timeout = args.timeout if args.command == "wait" else 0
        if not 0 <= timeout <= 3600:
            raise ValueError("Wait timeout must be 0..3600 seconds")
        deadline = time.monotonic() + timeout
        while True:
            rows = store.messages(args.peer, unread=not getattr(args, "all", False))
            if rows or time.monotonic() >= deadline:
                return rows
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    if args.command == "ack":
        require_peer(store, args.peer)
        return [store.ack(mid, args.peer) for mid in args.message_ids]
    if args.command == "cancel":
        return store.cancel(args.message_id)
    if args.command == "status":
        rows = store.messages()
        if args.peer:
            rows = [m for m in rows if args.peer in (m["sender"], m["recipient"])]
        fields = (
            "id",
            "sender",
            "recipient",
            "status",
            "detail",
            "acked_at",
            "created_at",
            "next_attempt_at",
        )
        return {
            "counts": dict(Counter(m["status"] for m in rows)),
            "messages": [{k: m[k] for k in fields} for m in rows],
        }
    raise ValueError("Unknown command")


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        result = execute(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if (
            args.command == "headless"
            and not args.preflight
            and result.get("status") != "completed"
        ):
            return 1
        return 0
    except (ValueError, KeyError, OSError, TransportError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
