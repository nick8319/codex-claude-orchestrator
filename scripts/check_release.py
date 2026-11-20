#!/usr/bin/env python3
"""Validate the declared release files; development YAML checks need PyYAML."""

import ast
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from release_files import FILES, release_files

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)?\Z")
SECRET = re.compile(
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
)


def validate(root: Path) -> list[Path]:
    root = root.absolute()
    files = release_files(root)
    names = {str(p.relative_to(root)) for p in files}
    missing = sorted(FILES - names)
    if missing:
        raise ValueError("Missing release files: " + ", ".join(missing))
    version = (root / "VERSION").read_text().strip()
    if not VERSION.fullmatch(version):
        raise ValueError("VERSION must be a plain release identifier")
    if not any(name.startswith("tests/test_") for name in names):
        raise ValueError("Release must include its tests")
    for path in files:
        name = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        if SECRET.search(text):
            raise ValueError("Possible secret literal in " + name + "; contents omitted")
        if path.suffix in (".md", ".json", ".yaml", ".yml") and re.search(
            r"/(?:Users|home)/[^\s/]+/", text
        ):
            raise ValueError("Possible personal path in " + name + "; contents omitted")
        if path.suffix == ".py":
            ast.parse(text, filename=name)
        elif path.suffix == ".json":
            json.loads(text)
        elif path.suffix == ".md":
            for link in re.findall(r"\]\(([^\s)]+)\)", text):
                url = urlsplit(link)
                if url.scheme or url.netloc or not url.path:
                    continue
                target = (path.parent / unquote(url.path)).resolve()
                if not target.is_relative_to(root.resolve()) or not target.exists():
                    raise ValueError("Broken or external local link in " + name + ": " + link)
    return files


def main() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    files = validate(root)
    for path in files:
        if path.suffix in (".yaml", ".yml"):
            yaml.safe_load(path.read_text())
    frontmatter = (root / "SKILL.md").read_text().split("---", 2)
    if len(frontmatter) != 3 or frontmatter[0].strip():
        raise ValueError("SKILL.md needs YAML frontmatter")
    metadata = yaml.safe_load(frontmatter[1])
    if metadata.get("name") != "orchestrator" or not metadata.get("description"):
        raise ValueError("Skill metadata is incomplete")
    print(
        json.dumps(
            {
                "status": "pass",
                "files": len(files),
                "version": (root / "VERSION").read_text().strip(),
            }
        )
    )


if __name__ == "__main__":
    main()
