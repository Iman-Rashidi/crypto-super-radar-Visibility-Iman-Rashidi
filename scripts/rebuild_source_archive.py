#!/usr/bin/env python3
"""Rebuild the bundled source archive by overlaying tracked package files.

Run from the repository root after pulling the feature branch:
    python scripts/rebuild_source_archive.py
The original bundle is preserved until the replacement archive is complete.
"""
from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dist" / "crypto-super-radar-source.tar.xz"
PACKAGE_DIR = ROOT / "crypto_super_radar"
TEST_DIR = ROOT / "tests"


def main() -> int:
    if not ARCHIVE.exists():
        raise SystemExit(f"Source archive not found: {ARCHIVE}")
    tracked = [p for base in (PACKAGE_DIR, TEST_DIR, ROOT / "docs") if base.exists()
               for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    tracked.extend(
        ROOT / name for name in ("README.md", "pyproject.toml", "requirements.txt", ".env.example", "render.yaml")
        if (ROOT / name).is_file()
    )
    # Infer the archive's top-level directory to preserve its documented layout.
    with tarfile.open(ARCHIVE, "r:xz") as src:
        members = src.getmembers()
        roots = [m.name.split("/", 1)[0] for m in members if m.name]
        prefix = roots[0] + "/" if roots and roots[0] == "crypto-super-radar" else ""
        with tempfile.NamedTemporaryFile(dir=ARCHIVE.parent, suffix=".tmp", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            with tarfile.open(temp_path, "w:xz") as dst:
                overlay_names = {
                    prefix + p.relative_to(ROOT).as_posix() for p in tracked
                }
                for member in members:
                    if member.name in overlay_names:
                        continue
                    if member.isfile():
                        stream = src.extractfile(member)
                        if stream is not None:
                            dst.addfile(member, stream)
                    else:
                        dst.addfile(member)
                for path in tracked:
                    arcname = prefix + path.relative_to(ROOT).as_posix()
                    info = dst.gettarinfo(str(path), arcname=arcname)
                    with path.open("rb") as stream:
                        dst.addfile(info, stream)
            os.replace(temp_path, ARCHIVE)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    print(f"Updated {ARCHIVE} with {len(tracked)} tracked package/test files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
