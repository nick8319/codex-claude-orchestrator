"""Distribution checks run against isolated fixture trees, without provider calls."""

import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.out = Path(self.temp.name) / "out"
        for name in ("build_release", "check_release"):
            path = ROOT / "scripts" / (name + ".py")
            self.assertTrue(path.exists(), name + " implementation missing")
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setattr(self, name, module)

    def fixture(self):
        from release_files import FILES

        for name in FILES:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("" if p.suffix == ".py" else "Fixture content\n")
        (self.root / "VERSION").write_text("0.1.0-beta.1\n")
        (self.root / "SKILL.md").write_text(
            "---\nname: orchestrator\ndescription: Fixture\n---\n\n# Skill\n"
        )
        (self.root / "agents/openai.yaml").write_text("interface:\n  display_name: Orchestrator\n")
        (self.root / ".github/workflows/ci.yml").write_text("name: checks\njobs: {}\n")
        (self.root / "references/models.json").write_text(json.dumps({"models": []}))
        (self.root / "references/transport.md").write_text("# Transport\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_smoke.py").write_text("pass\n")

    def test_archive_is_deterministic_and_excludes_local_state(self):
        self.fixture()
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_text("private fixture config")
        (self.root / "INSTALLATION.json").write_text("private fixture installation")
        (self.root / ".DS_Store").write_bytes(b"fixture")
        first = self.build_release.build(self.root, self.out)
        second = self.build_release.build(self.root, self.out / "second")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            names = archive.getnames()
            self.assertTrue(any(n.endswith("/SKILL.md") for n in names))
            self.assertFalse(
                any(".git/" in n or "INSTALLATION" in n or ".DS_Store" in n for n in names)
            )
            self.assertTrue(
                all(member.uid == 0 and member.mtime == 0 for member in archive.getmembers())
            )
        self.assertTrue(first.with_suffix(first.suffix + ".sha256").exists())

    def test_existing_release_is_not_overwritten(self):
        self.fixture()
        path = self.build_release.build(self.root, self.out)
        previous = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build_release.build(self.root, self.out)
        self.assertEqual(path.read_bytes(), previous)

    def test_missing_required_files_and_broken_links_are_rejected(self):
        self.fixture()
        (self.root / "LICENSE").unlink()
        with self.assertRaisesRegex(ValueError, "LICENSE"):
            self.check_release.validate(self.root)
        (self.root / "LICENSE").write_text("License\n")
        (self.root / "README.md").write_text("[Missing](references/missing.md)\n")
        with self.assertRaisesRegex(ValueError, "missing.md"):
            self.check_release.validate(self.root)

    def test_personal_paths_and_secret_literals_rejected_without_echoing_secret(self):
        self.fixture()
        text = "sk-" + "x" * 40
        (self.root / "README.md").write_text(text)
        with self.assertRaises(ValueError) as caught:
            self.check_release.validate(self.root)
        self.assertNotIn(text, str(caught.exception))
        (self.root / "README.md").write_text("Home: /Users/example-person/project")
        with self.assertRaisesRegex(ValueError, "personal path"):
            self.check_release.validate(self.root)

    def test_unsafe_version_and_source_symlinks_cannot_enter_archive(self):
        self.fixture()
        (self.root / "VERSION").write_text("../../escape")
        with self.assertRaises(ValueError):
            self.build_release.build(self.root, self.out)
        (self.root / "VERSION").write_text("0.1.0-beta.1")
        (self.root / "README.md").unlink()
        (self.root / "README.md").symlink_to(self.root / "LICENSE")
        with self.assertRaises(ValueError):
            self.build_release.build(self.root, self.out)


if __name__ == "__main__":
    unittest.main()
