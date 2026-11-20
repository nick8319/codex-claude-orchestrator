#!/usr/bin/env python3
"""Ownership-aware macOS installation; upgrade/uninstall preserve private state."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile

# Lifecycle commands may run directly from the installed tree.
sys.dont_write_bytecode = True
from check_release import validate as validate_release  # noqa: E402

LABEL = "local.codex-claude-orchestrator"
MANIFEST = "INSTALLATION.json"


def checked_path(path, *, private=False):
    """Reject replaceable ancestors, symlinks, hardlinks and foreign ownership."""
    path = Path(path)
    for item in reversed((path, *path.parents)):
        if not os.path.lexists(item):
            continue
        info = item.lstat()
        sticky = info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.getuid()):
            raise ValueError("Untrusted install path: " + str(item))
        if info.st_mode & 0o022 and not sticky:
            raise ValueError("Shared-writable install path: " + str(item))
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Install ancestor must be a directory: " + str(item))
        if item == path:
            if info.st_uid != os.getuid():
                raise ValueError("Install path must belong to this user: " + str(item))
            if not stat.S_ISDIR(info.st_mode) and (
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            ):
                raise ValueError("Install file must be regular and not linked: " + str(item))
            if private and stat.S_IMODE(info.st_mode) != (
                0o700 if stat.S_ISDIR(info.st_mode) else 0o600
            ):
                raise ValueError("State/log path must be private: " + str(item))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, body, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".orchestrator-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_manifest(target, manifest, status):
    manifest["status"] = status
    atomic_write(
        target / MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    )


def validate_record(record, target, launcher, plist):
    if not isinstance(record, dict):
        raise ValueError("Invalid ownership record")
    if (
        record.get("schema") != 1
        or record.get("label") != LABEL
        or record.get("target") != str(target)
        or record.get("launcher") != str(launcher)
        or record.get("plist") != str(plist)
        or not isinstance(record.get("files"), dict)
    ):
        raise ValueError("Installation has no matching ownership manifest")
    for name, value in record["files"].items():
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or str(relative) != name
            or name == MANIFEST
            or not isinstance(value, str)
            or len(value) != 64
        ):
            raise ValueError("Invalid owned file entry")


def remaining_ownership(journal, target, launcher, plist):
    """An uninstall journal permits missing files, never changed or new files."""
    checked_path(journal, private=True)
    data = json.loads(journal.read_text())
    if not isinstance(data, dict) or data.get("operation") != "uninstall":
        raise ValueError("Invalid uninstall journal")
    record = data.get("installation")
    if not isinstance(record, dict):
        raise ValueError("Invalid uninstall ownership record")
    validate_record(record, target, launcher, plist)
    expected = dict(record["files"])
    expected[MANIFEST] = data.get("manifest_sha256")
    allowed_directories = {str(parent) for name in expected for parent in Path(name).parents}
    for path in target.rglob("*") if target.exists() else []:
        checked_path(path)
        name = str(path.relative_to(target))
        if path.is_dir():
            if name not in allowed_directories:
                raise ValueError("Unowned directory during uninstall; preserved: " + name)
        elif name not in expected or digest(path) != expected[name]:
            raise ValueError("Changed or unowned file during uninstall; preserved: " + name)
    for path, key in ((launcher, "launcher_sha256"), (plist, "plist_sha256")):
        checked_path(path)
        if path.exists() and digest(path) != record.get(key):
            raise ValueError("Changed uninstall file preserved: " + str(path))
    return record


def finish_uninstall(journal, target, launcher, plist, runner):
    if loaded(runner, LABEL):
        result = launchctl(runner, "bootout", "gui/" + str(os.getuid()) + "/" + LABEL)
        if result.returncode != 0 or loaded(runner, LABEL):
            raise RuntimeError("Uninstall stop is uncertain; journal and remaining files retained")
    record = remaining_ownership(journal, target, launcher, plist)
    launcher.unlink(missing_ok=True)
    plist.unlink(missing_ok=True)
    for name in sorted(record["files"]):
        (target / name).unlink(missing_ok=True)
    (target / MANIFEST).unlink(missing_ok=True)
    if target.exists():
        directories = sorted(
            (p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
        )
        for directory in directories:
            directory.rmdir()
        target.rmdir()
    # Keep ownership proof until removal of every external path is durable.
    for parent in (target.parent, launcher.parent, plist.parent):
        sync_directory(parent)
    journal.unlink()
    sync_directory(journal.parent)


def ownership(target, launcher, plist):
    for path in (target, target / MANIFEST, launcher, plist):
        checked_path(path)
        if not path.exists():
            raise ValueError(
                "Managed installation is incomplete; preserve and inspect: " + str(path)
            )
    record = json.loads((target / MANIFEST).read_text())
    validate_record(record, target, launcher, plist)
    actual = {}
    for path in target.rglob("*"):
        checked_path(path)
        if path.is_file() and path != target / MANIFEST:
            actual[str(path.relative_to(target))] = digest(path)
    if (
        actual != record["files"]
        or digest(launcher) != record.get("launcher_sha256")
        or digest(plist) != record.get("plist_sha256")
    ):
        raise ValueError(
            "Managed files were changed or added; preserve and reconcile before replacing"
        )
    return record


def launchctl(runner, *arguments):
    return runner(
        ["launchctl", *arguments], capture_output=True, text=True, timeout=30, check=False
    )


def loaded(runner, label):
    result = launchctl(runner, "print", "gui/" + str(os.getuid()) + "/" + label)
    if result.returncode == 0:
        return True
    if result.returncode == 113:
        return False
    raise RuntimeError("Cannot determine launchd state for " + label + "; nothing replaced")


def stop_service(runner, target, manifest):
    try:
        if loaded(runner, LABEL):
            result = launchctl(runner, "bootout", "gui/" + str(os.getuid()) + "/" + LABEL)
            if result.returncode != 0 or loaded(runner, LABEL):
                raise RuntimeError("Service stop was not confirmed")
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        write_manifest(target, manifest, "stop-unknown")
        raise RuntimeError(
            "Service stop is uncertain; owned code retained. Inspect launchd before retrying."
        ) from error


def _install(source, home=None, *, mode="install", dry_run=False, runner=subprocess.run):
    """Plan or execute against the supplied home; tests inject a fixture runner."""
    if mode not in ("install", "upgrade", "uninstall"):
        raise ValueError("Unknown installation mode")
    source = Path(os.path.abspath(source))
    home = Path(os.path.abspath(Path.home() if home is None else home))
    target = home / ".codex/skills/orchestrator"
    state = home / ".local/state/codex-orchestrator"
    launcher = home / ".local/bin/orchestrator"
    plist = home / ("Library/LaunchAgents/" + LABEL + ".plist")
    for path in (home, target, state, launcher, plist):
        checked_path(path, private=path == state)
    old = None
    journal = state / "uninstall.json"
    pending_uninstall = os.path.lexists(journal)
    if pending_uninstall:
        if mode != "uninstall":
            raise ValueError(
                "Uninstall is incomplete; rerun --uninstall from the release source first"
            )
        old = remaining_ownership(journal, target, launcher, plist)
    elif mode == "install":
        if any(os.path.lexists(p) for p in (target, launcher, plist)):
            raise ValueError(
                "Existing installation preserved; use --upgrade only for a managed public installation"
            )
    else:
        old = ownership(target, launcher, plist)
    logs = [state / name for name in ("daemon.log", "daemon.err.log")]
    for path in logs:
        checked_path(path, private=True)
    files = [] if mode == "uninstall" else validate_release(source)
    if mode != "uninstall" and source / "scripts/orchestrator.py" not in files:
        raise ValueError("Release is missing scripts/orchestrator.py")
    plan = dict(
        mode=mode,
        target=str(target),
        state=str(state),
        launcher=str(launcher),
        plist=str(plist),
        label=LABEL,
        files=[str(p.relative_to(source)) for p in files],
        preserved="Private state and verified logs",
    )
    if dry_run:
        return dict(plan, dry_run=True, service_state="unverified; no launchctl calls made")
    if mode == "install" and loaded(runner, LABEL):
        raise ValueError("Unowned launchd service is active; preserved")
    if mode == "uninstall":
        if not pending_uninstall:
            stop_service(runner, target, old)
            ownership(target, launcher, plist)
            data = dict(
                operation="uninstall", installation=old, manifest_sha256=digest(target / MANIFEST)
            )
            atomic_write(journal, (json.dumps(data, sort_keys=True) + "\n").encode())
        finish_uninstall(journal, target, launcher, plist, runner)
        return dict(plan, uninstalled=True)
    for directory in (target.parent, launcher.parent, plist.parent, state):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        checked_path(directory, private=directory == state)
    for path in logs:
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        checked_path(path, private=True)
    stage = Path(tempfile.mkdtemp(prefix=".orchestrator-stage-", dir=target.parent))
    backup = None
    old_launcher = launcher.read_bytes() if old else None
    old_plist = plist.read_bytes() if old else None
    try:
        for file in files:
            destination = stage / file.relative_to(source)
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copyfile(file, destination)
            destination.chmod(0o600)
        interpreter = str(Path(sys.executable).resolve())
        script = str(target / "scripts/orchestrator.py")
        wrapper = (
            "#!"
            + interpreter
            + "\nimport os, sys\nos.execv("
            + repr(interpreter)
            + ", ["
            + repr(interpreter)
            + ', "-B", '
            + repr(script)
            + "] + sys.argv[1:])\n"
        ).encode()
        job = dict(
            Label=LABEL,
            ProgramArguments=[interpreter, "-B", script, "--state-dir", str(state), "serve"],
            RunAtLoad=True,
            KeepAlive=True,
            ThrottleInterval=10,
            ProcessType="Background",
            WorkingDirectory=str(state),
            StandardOutPath=str(logs[0]),
            StandardErrorPath=str(logs[1]),
            EnvironmentVariables={
                "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            Umask=63,
        )
        job_bytes = plistlib.dumps(job)
        manifest = dict(
            schema=1,
            label=LABEL,
            target=str(target),
            launcher=str(launcher),
            plist=str(plist),
            files={str(p.relative_to(stage)): digest(p) for p in stage.rglob("*") if p.is_file()},
            launcher_sha256=hashlib.sha256(wrapper).hexdigest(),
            plist_sha256=hashlib.sha256(job_bytes).hexdigest(),
        )
        write_manifest(stage, manifest, "prepared")
        if old:
            stop_service(runner, target, old)
            ownership(target, launcher, plist)
            backup = Path(tempfile.mkdtemp(prefix=".orchestrator-backup-", dir=target.parent))
            backup.rmdir()
            target.rename(backup)
        try:
            stage.rename(target)
            atomic_write(launcher, wrapper, 0o755)
            atomic_write(plist, job_bytes)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if backup:
                backup.rename(target)
                backup = None
                atomic_write(launcher, old_launcher, 0o755)
                atomic_write(plist, old_plist)
                write_manifest(target, old, "stopped-needs-start")
            else:
                launcher.unlink(missing_ok=True)
                plist.unlink(missing_ok=True)
            raise
        if backup:
            manifest["recovery_backup"] = str(backup)
        write_manifest(target, manifest, "bootstrap-pending")
        try:
            result = launchctl(runner, "bootstrap", "gui/" + str(os.getuid()), str(plist))
            if result.returncode != 0 or not loaded(runner, LABEL):
                raise RuntimeError("Service bootstrap was not confirmed")
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            write_manifest(target, manifest, "bootstrap-unknown")
            raise RuntimeError(
                "Bootstrap is uncertain; owned code retained. Inspect launchd, then use --upgrade or --uninstall."
            ) from error
        write_manifest(target, manifest, "installed")
        if backup and backup.exists():
            shutil.rmtree(backup)
            backup = None
            manifest.pop("recovery_backup", None)
            write_manifest(target, manifest, "installed")
        return dict(plan, installed=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        # Failure retains any old backup, including when rollback itself failed.


def install(source, home=None, *, mode="install", dry_run=False, runner=subprocess.run):
    """Serialize lifecycle mutations across processes; dry-run is read-only."""
    if dry_run:
        return _install(source, home, mode=mode, dry_run=True, runner=runner)
    _install(source, home, mode=mode, dry_run=True, runner=runner)
    home = Path(os.path.abspath(Path.home() if home is None else home))
    state = home / ".local/state/codex-orchestrator"
    checked_path(home)
    checked_path(state, private=True)
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = state / "install.lock"
    checked_path(lock, private=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        checked_path(lock, private=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "Another installer is active; no service operations performed"
            ) from error
        return _install(source, home, mode=mode, dry_run=False, runner=runner)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--upgrade", action="store_true")
    operations.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This service installer targets macOS launchd")
    mode = "upgrade" if args.upgrade else "uninstall" if args.uninstall else "install"
    try:
        print(
            json.dumps(
                install(Path(__file__).resolve().parents[1], mode=mode, dry_run=args.dry_run),
                indent=2,
            )
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, "Installation stopped: " + str(error) + "\n")


if __name__ == "__main__":
    main()
