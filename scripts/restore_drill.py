#!/usr/bin/env python3
"""Phase 6 §11.3: restore drill with recorded evidence.

`deploy/backup.sh` and `deploy/restore.sh` exist, but the capability ledger
records `backup_restore` as having no real drill evidence. A backup nobody has
restored is a hypothesis, not a recovery capability.

This performs a drill against a *copy*, never the live database, and reports
what a reviewer actually needs: row counts per table compared against the
source, schema completeness, and the measured restore duration that an RPO/RTO
decision has to be based on.

    python scripts/restore_drill.py --backup path/to/backup.db --source jobqueue.db

Read-only with respect to both inputs. The restored copy is written to a
temporary directory and removed unless `--keep` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

# Reuse the same verifier as ``dispatch db restore-verify`` when an operator
# supplies a signed checkpoint; the default drill needs no anchor arguments.
from app.audit_anchor import (  # noqa: E402
    file_sha256,
    verify_audit_checkpoint_against_database,
)

#: Tables whose absence means the restore is unusable, not merely incomplete.
CRITICAL_TABLES = (
    "jobs",
    "approvals",
    "datasets",
    "execution_attempts",
    "execution_plans",
)


def _connect_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        try:
            counts[table] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
        except sqlite3.Error:
            counts[table] = -1  # unreadable: recorded, never silently skipped
    return counts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_backup_directory(backup_dir: Path) -> dict[str, Any]:
    """Verify both checksum layers before any archive is extracted."""
    manifest_path = backup_dir / "MANIFEST"
    checksums_path = backup_dir / "CHECKSUMS.sha256"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise SystemExit("UNUSABLE: backup directory lacks MANIFEST/CHECKSUMS.sha256")
    manifest: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            raise SystemExit("UNUSABLE: malformed MANIFEST")
        key, value = line.split("=", 1)
        manifest[key] = value
    expected_inventory = manifest.get("checksums_sha256")
    if expected_inventory != _sha256(checksums_path):
        raise SystemExit("UNUSABLE: checksum inventory does not match MANIFEST")

    verified: list[str] = []
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise SystemExit("UNUSABLE: malformed checksum inventory")
        expected, raw_name = parts
        name = raw_name.lstrip("* ")
        if (
            not name
            or name != Path(name).name
            or "/" in name
            or "\\" in name
        ):
            raise SystemExit("UNUSABLE: unsafe checksum inventory path")
        artifact = backup_dir / name
        if not artifact.is_file() or _sha256(artifact) != expected:
            raise SystemExit(f"UNUSABLE: checksum mismatch: {name}")
        verified.append(name)
    if "jobqueue.db" not in verified:
        raise SystemExit("UNUSABLE: backup directory has no database snapshot")
    return {
        "manifest": manifest,
        "verified_artifacts": sorted(verified),
        "inventory_sha256": expected_inventory,
    }


def _extract_regular_archive(archive: Path, destination: Path) -> int:
    """Extract only directories/regular files and reject traversal or links."""
    extracted = 0
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            member_path = Path(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or not member.name
            ):
                raise SystemExit(f"UNUSABLE: unsafe archive path in {archive.name}")
            target = (destination / member_path).resolve()
            if target != root and root not in target.parents:
                raise SystemExit("UNUSABLE: archive path escapes restore root")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise SystemExit(
                    f"UNUSABLE: links/devices are not accepted in {archive.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise SystemExit(f"UNUSABLE: unreadable member in {archive.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted += 1
    return extracted


def _git_ref_evidence(restored_root: Path) -> dict[str, Any]:
    git_root = restored_root / "git"
    if not git_root.is_dir():
        return {"repositories": 0, "refs": 0, "sample": [], "errors": []}
    repositories = sorted(
        path for path in git_root.rglob("*") if path.is_dir() and (path / "HEAD").is_file()
    )
    refs: list[str] = []
    errors: list[str] = []
    for repository in repositories:
        process = subprocess.run(
            [
                "git",
                f"--git-dir={repository}",
                "for-each-ref",
                "--format=%(refname) %(objectname)",
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if process.returncode != 0:
            errors.append(str(repository.relative_to(restored_root)))
            continue
        prefix = str(repository.relative_to(restored_root))
        refs.extend(
            f"{prefix}:{line}"
            for line in process.stdout.splitlines()
            if line.strip()
        )
    return {
        "repositories": len(repositories),
        "refs": len(refs),
        "sample": refs[:10],
        "errors": errors,
    }


def _result_sample(restored_root: Path) -> list[dict[str, Any]]:
    results_root = restored_root / "results"
    if not results_root.is_dir():
        return []
    sample: list[dict[str, Any]] = []
    for path in sorted(item for item in results_root.rglob("*") if item.is_file())[:10]:
        sample.append(
            {
                "path": str(path.relative_to(restored_root)),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return sample


def _verify_audit_anchor_for_restore(
    restored_database: str,
    audit_anchor: str,
    signing_key_file: str | None,
    backup_manifest: str | None,
) -> dict[str, Any]:
    """Verify an optional signed checkpoint against the restored database.

    The checkpoint and key are deliberately supplied as paths rather than
    inferred from the backup. This keeps the distinction between a local
    drill and a production off-host anchor explicit, and avoids silently
    accepting a checkpoint with the wrong manifest binding.
    """

    result: dict[str, Any] = {"requested": True, "verified": False}
    try:
        if not signing_key_file:
            raise ValueError("--signing-key-file is required with --audit-anchor")
        checkpoint = json.loads(
            Path(audit_anchor).read_text(encoding="utf-8")
        )
        manifest_digest = file_sha256(backup_manifest) if backup_manifest else None
        verified = verify_audit_checkpoint_against_database(
            restored_database,
            checkpoint,
            signing_key=Path(signing_key_file).read_bytes(),
            backup_manifest_sha256=manifest_digest,
        )
        result.update(
            {
                "verified": True,
                "first_sequence": verified["first_sequence"],
                "last_sequence": verified["last_sequence"],
                "event_count": verified["event_count"],
                "schema_version": verified["schema_version"],
                "backup_manifest_sha256": manifest_digest,
            }
        )
    except (OSError, ValueError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def run_drill(
    backup_path: str,
    source_path: str | None,
    *,
    keep: bool,
    audit_anchor: str | None = None,
    signing_key_file: str | None = None,
    backup_manifest: str | None = None,
) -> dict[str, Any]:
    if not os.path.exists(backup_path):
        raise SystemExit(f"UNUSABLE: backup not found: {backup_path}")

    workdir = tempfile.mkdtemp(prefix="restore-drill-")
    restored = os.path.join(workdir, "restored.db")

    started = time.monotonic()
    shutil.copy2(backup_path, restored)
    # Opening and running an integrity check is the part that actually proves
    # the file is a usable database rather than a well-sized blob.  A badly
    # corrupted file raises rather than returning a verdict, and an operator
    # running a drill needs a FAIL, not a traceback.
    try:
        with sqlite3.connect(restored) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        integrity = f"unreadable: {type(exc).__name__}"
    duration = round(time.monotonic() - started, 3)

    report: dict[str, Any] = {
        "backup": backup_path,
        "restore_seconds": duration,
        "integrity_check": integrity,
        "integrity_ok": integrity == "ok",
    }

    try:
        restored_counts = _table_counts(_connect_readonly(restored))
    except sqlite3.DatabaseError:
        restored_counts = {}
    report["restored_tables"] = len(restored_counts)
    report["restored_rows"] = sum(v for v in restored_counts.values() if v >= 0)
    report["unreadable_tables"] = sorted(
        name for name, count in restored_counts.items() if count < 0
    )

    missing = [t for t in CRITICAL_TABLES if t not in restored_counts]
    report["missing_critical_tables"] = missing

    if source_path and os.path.exists(source_path):
        source_counts = _table_counts(_connect_readonly(source_path))
        drift = {
            table: {"source": source_counts.get(table), "restored": restored_counts.get(table)}
            for table in sorted(set(source_counts) | set(restored_counts))
            if source_counts.get(table) != restored_counts.get(table)
        }
        report["row_count_drift"] = drift
        # Drift is expected — the source keeps moving after the backup was
        # taken — so it is reported for the operator to judge, never asserted
        # away. What must not happen is the restore having *more* rows than
        # the source, which would mean the backup is not of this database.
        report["restored_ahead_of_source"] = sorted(
            table
            for table, values in drift.items()
            if (values["restored"] or 0) > (values["source"] or 0)
        )
    else:
        report["row_count_drift"] = None
        report["restored_ahead_of_source"] = []

    if audit_anchor:
        report["audit_anchor"] = _verify_audit_anchor_for_restore(
            restored,
            audit_anchor,
            signing_key_file,
            backup_manifest,
        )
    else:
        report["audit_anchor"] = {"requested": False, "verified": None}

    if keep:
        report["restored_copy"] = restored
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    report["pass"] = bool(
        report["integrity_ok"]
        and not missing
        and not report["unreadable_tables"]
        and not report["restored_ahead_of_source"]
        and (
            not report["audit_anchor"]["requested"]
            or report["audit_anchor"]["verified"]
        )
    )
    return report


def run_backup_directory_drill(
    backup_directory: str,
    source_path: str | None,
    *,
    keep: bool,
    audit_anchor: str | None = None,
    signing_key_file: str | None = None,
    backup_manifest: str | None = None,
) -> dict[str, Any]:
    """Restore the complete backup set into an isolated directory."""
    backup_dir = Path(backup_directory).resolve()
    if not backup_dir.is_dir():
        raise SystemExit(f"UNUSABLE: backup directory not found: {backup_dir}")
    verification = _verify_backup_directory(backup_dir)
    workdir = Path(tempfile.mkdtemp(prefix="full-restore-drill-"))
    restored_root = workdir / "state"
    restored_root.mkdir()
    started = time.monotonic()
    extracted_files = 0
    try:
        for name in verification["verified_artifacts"]:
            source = backup_dir / name
            if name.endswith(".tar.gz"):
                extracted_files += _extract_regular_archive(source, restored_root)
            else:
                shutil.copy2(source, restored_root / name)
                extracted_files += 1
        database_report = run_drill(
            str(restored_root / "jobqueue.db"),
            source_path,
            keep=False,
            audit_anchor=audit_anchor,
            signing_key_file=signing_key_file,
            backup_manifest=backup_manifest,
        )
        git_evidence = _git_ref_evidence(restored_root)
        result_sample = _result_sample(restored_root)
        duration = round(time.monotonic() - started, 3)
        database_report.update(
            {
                "backup_directory": str(backup_dir),
                "backup_directory_verified": True,
                "verified_artifacts": verification["verified_artifacts"],
                "inventory_sha256": verification["inventory_sha256"],
                "full_restore": True,
                "restore_seconds": duration,
                "restored_files": extracted_files,
                "git_ref_evidence": git_evidence,
                "result_sample": result_sample,
            }
        )
        database_report["pass"] = bool(
            database_report["pass"] and not git_evidence["errors"]
        )
        if keep:
            database_report["restored_copy"] = str(restored_root)
        return database_report
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--backup", help="database-only backup file")
    source_group.add_argument(
        "--backup-dir",
        help="complete directory produced by deploy/backup.sh (recommended)",
    )
    parser.add_argument(
        "--source",
        help="the live database, for row-count comparison (opened read-only)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the restored copy")
    parser.add_argument(
        "--audit-anchor",
        help="signed checkpoint JSON to verify against the restored database",
    )
    parser.add_argument(
        "--signing-key-file",
        help="key file used to verify --audit-anchor",
    )
    parser.add_argument(
        "--backup-manifest",
        help="manifest whose digest must match the checkpoint binding",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.backup_dir:
        report = run_backup_directory_drill(
            args.backup_dir,
            args.source,
            keep=args.keep,
            audit_anchor=args.audit_anchor,
            signing_key_file=args.signing_key_file,
            backup_manifest=args.backup_manifest,
        )
    else:
        report = run_drill(
            args.backup,
            args.source,
            keep=args.keep,
            audit_anchor=args.audit_anchor,
            signing_key_file=args.signing_key_file,
            backup_manifest=args.backup_manifest,
        )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["pass"] else 1

    print(
        "Restore drill: "
        + str(report.get("backup_directory") or report["backup"])
    )
    print(f"  full backup set  : {bool(report.get('full_restore', False))}")
    if report.get("full_restore"):
        print(
            "  checksum artifacts: "
            f"{len(report.get('verified_artifacts', []))}"
        )
    print(f"  integrity        : {report['integrity_check']}")
    print(f"  restore duration : {report['restore_seconds']}s")
    print(f"  tables / rows    : {report['restored_tables']} / {report['restored_rows']}")
    if report["missing_critical_tables"]:
        print(f"  MISSING TABLES   : {', '.join(report['missing_critical_tables'])}")
    if report["unreadable_tables"]:
        print(f"  UNREADABLE       : {', '.join(report['unreadable_tables'])}")
    if report["restored_ahead_of_source"]:
        print(
            "  RESTORE AHEAD OF SOURCE: "
            + ", ".join(report["restored_ahead_of_source"])
            + "  (this backup may not be of this database)"
        )
    if report["row_count_drift"]:
        print(f"  row-count drift  : {len(report['row_count_drift'])} tables differ")
        print("    (drift is expected — the source moves on after a backup)")
    if report.get("git_ref_evidence") is not None:
        git_evidence = report["git_ref_evidence"]
        print(
            "  Git repositories/refs: "
            f"{git_evidence['repositories']} / {git_evidence['refs']}"
        )
    if report.get("result_sample") is not None:
        print(f"  result samples   : {len(report['result_sample'])}")
    anchor = report.get("audit_anchor")
    if anchor and anchor.get("requested"):
        print(
            "  audit anchor     : "
            + ("verified" if anchor.get("verified") else "FAILED")
        )
        if anchor.get("error"):
            print(f"    {anchor['error']}")
    print(f"\nRESULT: {'PASS' if report['pass'] else 'FAIL'}")
    print(
        "\nRecord this output in docs/evidence/ and the capability ledger row. RPO/RTO"
        " targets are not established until DG-OPS-SLO rules on them."
    )
    return 0 if report["pass"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
