import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_files import release_files


class ReleaseFilesTests(unittest.TestCase):
    def test_allowlist_excludes_private_and_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (
                "SKILL.md",
                "scripts/store.py",
                "scripts/build_release.py",
                "references/models.json",
                "tests/test_store.py",
                ".github/workflows/ci.yml",
                "INSTALLATION.json",
                ".DS_Store",
                ".git/config",
                "private.txt",
                "scripts/rogue.py",
                "state/state.sqlite3",
                "scripts/__pycache__/store.pyc",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            self.assertEqual(
                [str(p.relative_to(root)) for p in release_files(root)],
                [
                    ".github/workflows/ci.yml",
                    "SKILL.md",
                    "references/models.json",
                    "scripts/build_release.py",
                    "scripts/store.py",
                    "tests/test_store.py",
                ],
            )

    def test_selected_symlinks_hardlinks_and_symlink_parent_rejected(self):
        for kind in ("symlink", "hardlink", "parent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "release"
                root.mkdir()
                other = Path(tmp) / "other"
                other.mkdir()
                target = other / "store.py"
                target.write_text("fixture")
                if kind == "parent":
                    (root / "scripts").symlink_to(other, target_is_directory=True)
                else:
                    (root / "scripts").mkdir()
                    path = root / "scripts/store.py"
                    if kind == "symlink":
                        path.symlink_to(target)
                    else:
                        os.link(target, path)
                with self.assertRaises(ValueError):
                    release_files(root)
