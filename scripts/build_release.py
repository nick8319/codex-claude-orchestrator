#!/usr/bin/env python3
"""Build a deterministic source archive from the declared public file set."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile

from check_release import validate


def build(root: Path, output_dir: Path) -> Path:
    root = root.absolute()
    files = validate(root)
    version = (root / "VERSION").read_text().strip()
    prefix = "orchestrator-" + version
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / (prefix + ".tar.gz")
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if os.path.lexists(archive_path) or os.path.lexists(checksum_path):
        raise FileExistsError("Release output already exists; choose a new output directory")
    fd, temporary = tempfile.mkstemp(prefix=".orchestrator-release-", dir=output_dir)
    published = False
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for path in files:
                        info = archive.gettarinfo(
                            str(path), prefix + "/" + str(path.relative_to(root))
                        )
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = 0
                        info.mode = 0o644
                        with path.open("rb") as source:
                            archive.addfile(info, source)
            raw.flush()
            os.fsync(raw.fileno())
        checksum = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
        os.link(
            temporary, archive_path
        )  # Atomically publish without replacing any existing output.
        published = True
        with checksum_path.open("x") as output:
            output.write(checksum + "  " + archive_path.name + "\n")
    except BaseException:
        if published:
            archive_path.unlink()
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    path = build(Path(__file__).resolve().parents[1], args.output_dir)
    print(
        json.dumps(
            {
                "archive": str(path.resolve()),
                "checksum": str(path.with_suffix(path.suffix + ".sha256").resolve()),
            }
        )
    )


if __name__ == "__main__":
    main()
