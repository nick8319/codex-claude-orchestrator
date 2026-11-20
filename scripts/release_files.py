"""Single declared public file set shared by archive builder and installer."""

import os
from pathlib import Path
import stat

FILES = {
    "SKILL.md",
    "README.md",
    "LICENSE",
    "VERSION",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "requirements-dev.txt",
    "agents/openai.yaml",
    ".github/workflows/ci.yml",
    ".gitignore",
    "references/models.json",
}
FILES.update(
    "scripts/" + name
    for name in (
        "orchestrator.py",
        "install.py",
        "store.py",
        "service.py",
        "claude_transport.py",
        "headless.py",
        "release_files.py",
        "build_release.py",
        "check_release.py",
    )
)


def release_files(root: Path) -> list[Path]:
    """Return sorted absolute paths; reject aliases for any selected file/directory.

    Unlisted files are excluded, never copied by recursive directory copying.
    Required release-content checks belong to check_release, allowing small fixtures.
    """
    root = Path(os.path.abspath(root))
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Release root must be a real directory")
    names = set(FILES)
    for directory, pattern in (("references", "*.md"), ("tests", "test_*.py")):
        parent = root / directory
        if parent.is_symlink():
            raise ValueError("Release directory must not be a symlink: " + directory)
        if parent.is_dir():
            names.update(str(p.relative_to(root)) for p in parent.glob(pattern))
    selected = []
    for name in sorted(names):
        path = root / name
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("Release file ancestor is a symlink: " + str(parent))
        if not os.path.lexists(path):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Release files must be regular and not linked: " + name)
        selected.append(path)
    return selected
