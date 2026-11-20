"""Controlled foreground Claude Code launch using the current first-party Max login.

This is a native tool restriction, not an OS sandbox. Safe mode disables local
customizations/hooks; managed policy still applies. Max auth does not prove extra
usage is disabled: native Claude metering applies and no CLI no-overage guarantee
is available. This helper never enables credits, switches accounts, or retries.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from claude_transport import TransportError, session_lifecycle
from store import Store

TESTED_VERSIONS = {"2.1.261"}
CATALOG = Path(__file__).resolve().parents[1] / "references" / "models.json"
AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
BOUNDARY = (
    "You are executing the supplied brief within the specified working directory. "
    "Before work, read applicable AGENTS.md and CLAUDE.md instructions in the working "
    "directory and its ancestors, and narrower instructions before editing their files. "
    "Safe mode means these files and custom skills were not automatically loaded. "
    "Preserve concurrent changes. Follow the brief and authorized workspace boundaries; "
    "do not change accounts, credentials, global configuration, or enable paid usage. "
    "Do not retry quota errors. Report evidence and unresolved limits honestly. "
    "Any delegated agents share the same brief scope and resource envelope."
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _environment(env):
    result = dict(os.environ if env is None else env)
    forbidden = sorted(
        k
        for k, v in result.items()
        if v
        and (
            k.startswith("ANTHROPIC_")
            or k.startswith("CLAUDE_CODE_USE_")
            or k
            in {
                "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_API_KEY_HELPER",
                "CLAUDE_CODE_SUBAGENT_MODEL",
                "CLAUDE_CODE_EFFORT_LEVEL",
                "CLAUDE_CODE_CUSTOM_MODEL_OPTION",
                "CLAUDE_CODE_CUSTOM_MODEL_OPTION_NAME",
                "CLAUDE_CODE_CUSTOM_MODEL_OPTION_DESCRIPTION",
            }
        )
    )
    if forbidden:
        raise ValueError(
            "Conflicting authentication/provider/model environment variables: "
            + ", ".join(forbidden)
        )
    return result


def _checked_output(argv, cwd, env):
    # Only bounded read-only auth/version commands use a timeout; no model process
    # receives an automatic timeout, kill, retry, or replacement session.
    result = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=20, check=False
    )
    if result.returncode != 0:
        raise ValueError(
            "Claude preflight command failed (output omitted to protect auth identity)"
        )
    return result.stdout


def preflight(
    cwd,
    brief_path,
    model="claude-fable-5-1",
    effort="high",
    mode="read-only",
    state_dir=None,
    resume=None,
    *,
    executable="claude",
    env=None,
    billing_policy="subscription-only",
):
    """Validate exact model/session, current Max auth and return a sanitized plan.

    Calls only local --version and auth status --json. No prompt is sent.
    env, when supplied, is the complete environment passed unchanged to the CLI.
    """
    if billing_policy not in {"subscription-only", "native-metering"}:
        raise ValueError("billing_policy must be subscription-only or native-metering")
    run_env = _environment(env)
    work = Path(cwd).expanduser().resolve(strict=True)
    brief = Path(brief_path).expanduser().resolve(strict=True)
    if not work.is_dir() or not brief.is_file():
        raise ValueError("cwd must be a directory and brief_path a regular file")
    if brief.stat().st_size > 4 * 1024 * 1024 or not brief.read_text().strip():
        raise ValueError("Brief must contain 1 byte to 4 MiB of text")
    models = json.loads(CATALOG.read_text())["models"]
    choice = next((m for m in models if m["id"] == model and m["provider"] == "anthropic"), None)
    if choice is None or effort not in choice["claude_code_effort_values_documented"]:
        raise ValueError("Use an exact allowlisted Claude model and supported effort")
    if mode not in {"read-only", "workspace-write"}:
        raise ValueError("mode must be read-only or workspace-write")
    if resume is not None:
        try:
            if str(uuid.UUID(resume)) != resume.lower():
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("resume must be an explicit canonical session UUID") from None
    _refuse_live_resume(resume, run_env)
    exe = shutil.which(str(executable), path=run_env.get("PATH"))
    if exe is None:
        raise ValueError("Claude executable not found")
    exe = str(Path(exe).resolve())
    version_output = _checked_output([exe, "--version"], work, run_env)
    found = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_output)
    if found is None:
        raise ValueError("Could not verify Claude CLI version")
    version = tuple(map(int, found.groups()))
    # Dated local tested snapshot, not a guarantee about newer CLI releases.
    minimum = max((2, 1, 261), tuple(map(int, choice["claude_code_minimum_version"].split("."))))
    compatible = ".".join(found.groups()) in TESTED_VERSIONS and version >= minimum
    block_reasons = []
    if not compatible:
        block_reasons.append("Claude CLI version is untested for sensitive headless launch")
    if billing_policy == "subscription-only":
        block_reasons.append(
            "subscription-only requested but no-overage enforcement cannot be verified"
        )
    try:
        raw_auth = json.loads(
            _checked_output([exe, "--safe-mode", "auth", "status", "--json"], work, run_env)
        )
        auth = {k: raw_auth.get(k) for k in AUTH_FIELDS}
    except (ValueError, AttributeError):
        raise ValueError("Could not verify current first-party Max authentication") from None
    if auth != {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }:
        raise ValueError(
            "Current Claude authentication must be logged-in first-party claude.ai Max"
        )
    tools = "Read,Glob,Grep" if mode == "read-only" else "Read,Glob,Grep,Bash,Edit,Write,Agent"
    argv = [
        exe,
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--effort",
        effort,
        "--safe-mode",
        "--strict-mcp-config",
        "--no-chrome",
        "--permission-prompts",
        "none",
        "--permission-mode",
        "dontAsk" if mode == "read-only" else "auto",
        "--tools",
        tools,
        "--append-system-prompt",
        BOUNDARY
        + "\nRead the canonical model catalog at "
        + str(CATALOG.resolve())
        + ". For any child agents, use only exact model IDs allowed for Anthropic in this catalog; do not use aliases or substitute models. Give each child the applicable scope and catalog path.",
    ]
    if resume:
        argv += ["--resume", resume]
    return {
        "argv": argv,
        "cwd": str(work),
        "brief_path": str(brief),
        "state_dir": str(
            Path(state_dir or "~/.local/state/codex-orchestrator").expanduser().absolute()
        ),
        "requested_model": model,
        "requested_effort": effort,
        "mode": mode,
        "resume": resume,
        "authentication": auth,
        "runtime_version": ".".join(found.groups()),
        "billing_policy": billing_policy,
        "launch_allowed": not block_reasons,
        "block_reason": "; ".join(block_reasons) or None,
        "compatibility_status": "tested" if compatible else "untested",
        "tested_versions": sorted(TESTED_VERSIONS),
        "subscription_only_enforced": False,
        "billing_route": "native Claude Max metering; extra-usage setting unverified",
        "limitations": [
            "No local no-overage enforcement; native account metering applies.",
            "Builtin tool restrictions are not an OS sandbox.",
            "Safe mode disables customizations and hooks; managed policy still applies.",
        ],
        "checked_at": _now(),
    }


def _refuse_live_resume(resume, env):
    if resume is None:
        return
    config = env.get("CLAUDE_CONFIG_DIR") or str(
        Path(env.get("HOME", str(Path.home()))) / ".claude"
    )
    state = session_lifecycle(resume, config)
    if state == "active":
        raise ValueError("Cannot resume an already-live Claude session")
    if state != "stopped":
        raise ValueError("Cannot resume Claude session with unknown lifecycle state")


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
        raise ValueError("State directory must be an owned directory, not a symlink")
    if info.st_mode & 0o077:
        raise ValueError("State directory must be private (0700)")


def _write_receipt(path, data):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _observations(events_path):
    models, sessions, result, malformed = set(), set(), None, False
    with events_path.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                malformed = True
                continue
            # Child agents may use a different approved model; root proof uses only
            # root init/assistant events, never aggregate child modelUsage keys.
            if item.get("parent_tool_use_id") is not None:
                continue
            sid = item.get("session_id")
            if isinstance(sid, str):
                sessions.add(sid)
            observed = (
                item.get("model")
                if item.get("type") == "system" and item.get("subtype") == "init"
                else None
            )
            if item.get("type") == "assistant" and isinstance(item.get("message"), dict):
                observed = item["message"].get("model")
            if isinstance(observed, str):
                models.add(observed)
            if item.get("type") == "result":
                result = item
    return models, sessions, result, malformed


def run(
    cwd,
    brief_path,
    model="claude-fable-5-1",
    effort="high",
    mode="read-only",
    state_dir=None,
    resume=None,
    *,
    executable="claude",
    env=None,
    billing_policy="subscription-only",
):
    """Run once in the foreground and persist private stdout/stderr plus a receipt.

    Completion requires exit 0, an explicit successful result, consistent session,
    and observed requested root model. No automatic retry, hard timeout or kill.
    On interruption, return outcome_unknown with child PID; inspect before resuming.
    """
    plan = preflight(
        cwd,
        brief_path,
        model,
        effort,
        mode,
        state_dir,
        resume,
        executable=executable,
        env=env,
        billing_policy=billing_policy,
    )
    if not plan["launch_allowed"]:
        return dict(plan, status="blocked")
    root = Path(plan["state_dir"])
    Store(root)
    _private_directory(root)
    runs = root / "headless"
    _private_directory(runs)
    run_dir = runs / str(uuid.uuid4())
    run_dir.mkdir(mode=0o700)
    receipt = dict(
        plan,
        run_id=run_dir.name,
        status="starting",
        started_at=_now(),
        observed_models=[],
        observed_model=None,
        session_id=None,
        observed_effort=None,
        events_path=str(run_dir / "events.jsonl"),
        stderr_path=str(run_dir / "stderr.log"),
        receipt_path=str(run_dir / "receipt.json"),
    )
    brief = Path(plan["brief_path"]).read_text()
    receipt["brief_sha256"] = hashlib.sha256(brief.encode()).hexdigest()
    receipt_path = Path(receipt["receipt_path"])
    _write_receipt(receipt_path, receipt)
    out_fd = os.open(receipt["events_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    err_fd = os.open(receipt["stderr_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    proc = None
    with os.fdopen(out_fd, "w") as out, os.fdopen(err_fd, "w") as err:
        try:
            _refuse_live_resume(resume, _environment(env))
            proc = subprocess.Popen(
                plan["argv"],
                cwd=plan["cwd"],
                env=_environment(env),
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                text=True,
            )
            receipt.update(status="running", pid=proc.pid)
            _write_receipt(receipt_path, receipt)
            proc.communicate(brief)
            receipt["exit_code"] = proc.returncode
        except KeyboardInterrupt:
            receipt.update(
                status="outcome_unknown",
                detail="Caller interrupted; no kill or retry issued",
                pid=None if proc is None else proc.pid,
                finished_at=_now(),
            )
            _write_receipt(receipt_path, receipt)
            return receipt
        except (OSError, ValueError, TransportError) as error:
            receipt.update(
                status="failed" if proc is None else "outcome_unknown",
                detail="Process launch/I/O failed: " + type(error).__name__,
                finished_at=_now(),
            )
            _write_receipt(receipt_path, receipt)
            return receipt
    models, sessions, result, malformed = _observations(Path(receipt["events_path"]))
    receipt.update(
        observed_models=sorted(models),
        observed_model=next(iter(models)) if len(models) == 1 else None,
        session_id=next(iter(sessions)) if len(sessions) == 1 else None,
        result_subtype=None if result is None else result.get("subtype"),
        finished_at=_now(),
    )
    if proc.returncode != 0 or (
        result is not None
        and (result.get("is_error") is True or result.get("subtype") != "success")
    ):
        receipt["status"] = "failed"
    elif (
        malformed
        or result is None
        or result.get("is_error") is not False
        or len(sessions) != 1
        or not models
    ):
        receipt["status"] = "outcome_unknown"
    elif resume is not None and receipt["session_id"] != resume:
        receipt["status"] = "session_mismatch"
    elif models != {model}:
        receipt["status"] = "model_mismatch"
    else:
        receipt["status"] = "completed"
    _write_receipt(receipt_path, receipt)
    return receipt
