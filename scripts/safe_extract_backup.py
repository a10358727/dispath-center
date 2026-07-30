#!/usr/bin/env python3
"""Validate or safely extract one Dispatch Center backup archive.

Only directories and regular files are accepted. Absolute/traversal paths,
links, devices, FIFOs and duplicate member paths are rejected before any file
is written. ``deploy/backup.sh`` uses validation mode after creating an
archive; ``deploy/restore.sh`` extracts only into a fresh staging directory.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from pathlib import Path


class UnsafeBackupArchive(ValueError):
    """The archive cannot be restored without unsafe filesystem semantics."""


def _validated_members(archive: Path) -> list[tarfile.TarInfo]:
    members: list[tarfile.TarInfo] = []
    seen: set[tuple[str, ...]] = set()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            member_path = Path(member.name)
            parts = member_path.parts
            if (
                not member.name
                or member_path.is_absolute()
                or ".." in parts
                or "." in parts
            ):
                raise UnsafeBackupArchive(
                    f"unsafe archive path in {archive.name}"
                )
            if parts in seen:
                raise UnsafeBackupArchive(
                    f"duplicate archive path in {archive.name}"
                )
            seen.add(parts)
            if not member.isdir() and not member.isfile():
                raise UnsafeBackupArchive(
                    f"links/devices are not accepted in {archive.name}"
                )
            members.append(member)
    return members


def validate_archive(archive: Path) -> int:
    """Return the number of safe regular files without extracting anything."""

    return sum(1 for member in _validated_members(archive) if member.isfile())


def extract_archive(archive: Path, destination: Path) -> int:
    """Extract a prevalidated archive beneath an existing staging directory."""

    members = _validated_members(archive)
    root = destination.resolve(strict=True)
    extracted = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in members:
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise UnsafeBackupArchive(
                    f"archive path escapes restore root in {archive.name}"
                )
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise UnsafeBackupArchive(
                    f"unreadable archive member in {archive.name}"
                )
            # "xb" makes duplicate/colliding paths fail instead of silently
            # replacing a file already materialized in the staging tree.
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            extracted += 1
    return extracted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("archive")
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args(argv)
    archive = Path(args.archive).resolve(strict=True)
    try:
        if args.validate_only:
            if args.destination is not None:
                parser.error("destination is not accepted with --validate-only")
            count = validate_archive(archive)
        else:
            if args.destination is None:
                parser.error("destination is required when extracting")
            destination = Path(args.destination).resolve(strict=True)
            count = extract_archive(archive, destination)
    except (OSError, tarfile.TarError, UnsafeBackupArchive) as exc:
        print(
            f"UNUSABLE: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"validated_regular_files={count}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
