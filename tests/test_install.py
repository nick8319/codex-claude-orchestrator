import fcntl
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from install import install, LABEL
from release_files import FILES


class Launchctl:
    def __init__(self):
        self.calls = []
        self.loaded = False
        self.fail = None

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        action = command[1]
        if action == "print":
            loaded = self.loaded and command[-1].endswith("/" + LABEL)
            return subprocess.CompletedProcess(command, 0 if loaded else 113, "", "")
        if action == self.fail:
            if action == "bootstrap":
                self.loaded = True  # failure may occur after submission
            raise subprocess.TimeoutExpired(command, 30)
        if action == "bootstrap":
            self.loaded = True
        elif action == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(command, 0, "", "")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "scripts").mkdir()
        (self.source / "scripts/orchestrator.py").write_text('print("fixture")\n')
        (self.source / "SKILL.md").write_text("Fixture")
        for name in (
            "store.py",
            "service.py",
            "claude_transport.py",
            "headless.py",
            "install.py",
            "release_files.py",
        ):
            (self.source / "scripts" / name).write_text("# fixture runtime module\n")
        (self.source / "references").mkdir()
        (self.source / "references/models.json").write_text("{}")
        for name in FILES | {"tests/test_fixture.py"}:
            path = self.source / name
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}" if path.suffix == ".json" else "")
        (self.source / "VERSION").write_text("0.1.0-beta.1")
        (self.source / "INSTALLATION.json").write_text("private source metadata")
        self.runner = Launchctl()
        self.target = self.home / ".codex/skills/orchestrator"
        self.state = self.home / ".local/state/codex-orchestrator"

    def run_install(self, **kwargs):
        return install(self.source, self.home, runner=self.runner, **kwargs)

    def test_partial_uninstall_resumes_with_durable_ownership_proof(self):
        self.run_install()
        real_unlink = Path.unlink

        def fail_plist(path, *args, **kwargs):
            if path.name == LABEL + ".plist":
                raise OSError("fixture plist removal failure")
            return real_unlink(path, *args, **kwargs)

        with patch("pathlib.Path.unlink", new=fail_plist):
            with self.assertRaises(OSError):
                self.run_install(mode="uninstall")
        self.assertTrue((self.state / "uninstall.json").exists())
        self.assertFalse((self.home / ".local/bin/orchestrator").exists())
        self.run_install(mode="uninstall")
        self.assertFalse(self.target.exists())
        self.assertFalse((self.state / "uninstall.json").exists())
        self.run_install()

    def test_uninstall_journal_survives_target_removal_and_resumes(self):
        self.run_install()
        real_unlink = Path.unlink

        def fail_journal(path, *args, **kwargs):
            if path == self.state / "uninstall.json":
                raise OSError("fixture interruption after target removal")
            return real_unlink(path, *args, **kwargs)

        with patch("pathlib.Path.unlink", new=fail_journal):
            with self.assertRaises(OSError):
                self.run_install(mode="uninstall")
        self.assertFalse(self.target.exists())
        self.assertTrue((self.state / "uninstall.json").exists())
        self.run_install(mode="uninstall")
        self.assertFalse((self.state / "uninstall.json").exists())

    def test_partial_uninstall_preserves_modified_remaining_files(self):
        self.run_install()
        real_unlink = Path.unlink

        def fail_plist(path, *args, **kwargs):
            if path.name == LABEL + ".plist":
                raise OSError("fixture interruption")
            return real_unlink(path, *args, **kwargs)

        with patch("pathlib.Path.unlink", new=fail_plist):
            with self.assertRaises(OSError):
                self.run_install(mode="uninstall")
        (self.target / "SKILL.md").write_text("user modification")
        with self.assertRaises(ValueError):
            self.run_install(mode="uninstall")
        self.assertEqual((self.target / "SKILL.md").read_text(), "user modification")

    def test_missing_runtime_module_rejects_upgrade_before_service_stop(self):
        self.run_install()
        (self.source / "scripts/store.py").unlink()
        self.runner.calls.clear()
        with self.assertRaises(ValueError):
            self.run_install(mode="upgrade")
        self.assertTrue(self.runner.loaded)
        self.assertTrue((self.target / "scripts/store.py").exists())
        self.assertFalse(any(c[1] == "bootout" for c in self.runner.calls))

    def test_lifecycle_preserves_state_and_existing_logs(self):
        self.run_install()
        self.assertTrue(self.runner.loaded)
        manifest = json.loads((self.target / "INSTALLATION.json").read_text())
        self.assertEqual(manifest["status"], "installed")
        self.assertNotIn("INSTALLATION.json", manifest["files"])
        (self.state / "daemon.log").write_text("preserve log")
        (self.state / "user-data").write_text("preserve state")
        (self.source / "SKILL.md").write_text("New fixture")
        self.run_install(mode="upgrade")
        self.assertEqual((self.target / "SKILL.md").read_text(), "New fixture")
        self.assertEqual((self.state / "daemon.log").read_text(), "preserve log")
        self.run_install(mode="uninstall")
        self.assertFalse(self.target.exists())
        self.assertFalse(self.runner.loaded)
        self.assertEqual((self.state / "user-data").read_text(), "preserve state")
        self.run_install()
        self.assertEqual((self.state / "daemon.log").read_text(), "preserve log")

    def test_dry_run_has_no_mutations_or_launchctl_calls(self):
        plan = self.run_install(dry_run=True)
        self.assertEqual(plan["mode"], "install")
        self.assertFalse(self.target.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.runner.calls, [])

    def test_unmanaged_installs_are_preserved(self):
        self.target.mkdir(parents=True)
        (self.target / "custom").write_text("unmanaged")
        with self.assertRaises(ValueError):
            self.run_install(mode="upgrade")
        self.assertEqual((self.target / "custom").read_text(), "unmanaged")

    def test_modified_and_extra_installed_files_block_upgrade_and_uninstall(self):
        self.run_install()
        (self.target / "SKILL.md").write_text("user customization")
        for mode in ("upgrade", "uninstall"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.run_install(mode=mode)
        self.assertTrue(self.runner.loaded)

    def test_bootstrap_uncertainty_keeps_owned_code_and_can_recover(self):
        self.runner.fail = "bootstrap"
        with self.assertRaises(RuntimeError):
            self.run_install()
        self.assertTrue((self.target / "scripts/orchestrator.py").exists())
        manifest = json.loads((self.target / "INSTALLATION.json").read_text())
        self.assertEqual(manifest["status"], "bootstrap-unknown")
        self.runner.fail = None
        self.run_install(mode="upgrade")
        self.assertTrue(self.runner.loaded)
        self.assertEqual(
            json.loads((self.target / "INSTALLATION.json").read_text())["status"], "installed"
        )

    def test_failed_bootout_never_removes_code(self):
        self.run_install()
        self.runner.fail = "bootout"
        with self.assertRaises(RuntimeError):
            self.run_install(mode="uninstall")
        self.assertTrue((self.target / "scripts/orchestrator.py").exists())

    def test_copy_failure_cleans_new_install_without_service_operations(self):
        with patch("install.shutil.copyfile", side_effect=OSError("disk fixture")):
            with self.assertRaises(OSError):
                self.run_install()
        self.assertFalse(self.target.exists())
        self.assertFalse(any(c[1] in ("bootstrap", "bootout") for c in self.runner.calls))

    def test_direct_installed_command_creates_no_unmanaged_bytecode(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        for name in ("install.py", "release_files.py", "check_release.py"):
            shutil.copyfile(scripts / name, self.source / "scripts" / name)
        self.run_install()
        environment = dict(os.environ, HOME=str(self.home))
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run(
            [sys.executable, str(self.target / "scripts/install.py"), "--upgrade", "--dry-run"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.target.rglob("__pycache__")), [])
        self.run_install(mode="uninstall")

    def test_upgrade_bootstrap_uncertainty_retains_old_recovery_copy(self):
        self.run_install()
        (self.source / "SKILL.md").write_text("New fixture")
        self.runner.fail = "bootstrap"
        with self.assertRaises(RuntimeError):
            self.run_install(mode="upgrade")
        manifest = json.loads((self.target / "INSTALLATION.json").read_text())
        self.assertEqual(manifest["status"], "bootstrap-unknown")
        self.assertEqual((Path(manifest["recovery_backup"]) / "SKILL.md").read_text(), "Fixture")
        self.assertEqual((self.target / "SKILL.md").read_text(), "New fixture")

    def test_concurrent_installer_is_rejected_before_launchctl(self):
        self.state.mkdir(parents=True, mode=0o700)
        path = self.state / "install.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):
                self.run_install()
            self.assertEqual(self.runner.calls, [])
        finally:
            os.close(fd)

    def test_upgrade_publish_failure_restores_previous_files(self):
        self.run_install()
        previous = (self.target / "SKILL.md").read_text()
        (self.source / "SKILL.md").write_text("New fixture")
        import install as module

        real_write = module.atomic_write
        failed = False

        def fail_once(path, body, mode=0o600):
            nonlocal failed
            if path.name == "orchestrator" and not failed:
                failed = True
                raise OSError("fixture wrapper failure")
            return real_write(path, body, mode)

        with patch("install.atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.run_install(mode="upgrade")
        self.assertEqual((self.target / "SKILL.md").read_text(), previous)
        self.assertEqual(
            json.loads((self.target / "INSTALLATION.json").read_text())["status"],
            "stopped-needs-start",
        )
        self.run_install(mode="upgrade")
        self.assertTrue(self.runner.loaded)

    def test_unexpected_extra_file_blocks_cleanup(self):
        self.run_install()
        path = self.target / "private-note"
        path.write_text("preserve")
        with self.assertRaises(ValueError):
            self.run_install(mode="uninstall")
        self.assertEqual(path.read_text(), "preserve")

    def test_hardlinked_log_is_rejected_before_service_operations(self):
        self.state.mkdir(parents=True, mode=0o700)
        external = self.base / "external-log"
        external.write_text("preserve")
        external.chmod(0o600)
        os.link(external, self.state / "daemon.log")
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertFalse(any(c[1] in ("bootstrap", "bootout") for c in self.runner.calls))

    def test_symlink_ancestor_and_log_are_rejected(self):
        external = self.base / "external"
        external.mkdir()
        (self.home / ".codex").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_install()
        (self.home / ".codex").unlink()
        self.state.mkdir(parents=True, mode=0o700)
        original = external / "log"
        original.write_text("do not change")
        (self.state / "daemon.log").symlink_to(original)
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(original.read_text(), "do not change")
