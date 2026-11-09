#!/usr/bin/env python3
"""Restore package modules retained in the bundled source archive.

The public package directory is a partial overlay. The source archive is the
complete, pinned distribution. This script restores only missing Python files
and never overwrites tracked modules.
"""
from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dist" / "crypto-super-radar-source.tar.xz"
PACKAGE = ROOT / "crypto_super_radar"


def main() -> int:
    if not ARCHIVE.is_file():
        raise SystemExit(f"Missing source archive: {ARCHIVE}")
    restored = []
    with tarfile.open(ARCHIVE, "r:xz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts
            try:
                index = parts.index("crypto_super_radar")
            except ValueError:
                continue
            relative = PurePosixPath(*parts[index + 1 :])
            if not relative.parts or relative.suffix != ".py":
                continue
            destination = PACKAGE.joinpath(*relative.parts)
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            destination.write_bytes(source.read())
            restored.append(relative.as_posix())
    print(f"Restored {len(restored)} missing package modules: {', '.join(restored)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
