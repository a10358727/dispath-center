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
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any

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


def run_drill(backup_path: str, source_path: str | None, *, keep: bool) -> dict[str, Any]:
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

    if keep:
        report["restored_copy"] = restored
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    report["pass"] = bool(
        report["integrity_ok"]
        and not missing
        and not report["unreadable_tables"]
        and not report["restored_ahead_of_source"]
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, help="backup file to restore")
    parser.add_argument(
        "--source",
        help="the live database, for row-count comparison (opened read-only)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the restored copy")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = run_drill(args.backup, args.source, keep=args.keep)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["pass"] else 1

    print(f"Restore drill: {report['backup']}")
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
    print(f"\nRESULT: {'PASS' if report['pass'] else 'FAIL'}")
    print(
        "\nRecord this output in docs/IMPLEMENTATION_PROGRESS.md. RPO/RTO"
        " targets are not established until DG-OPS-SLO rules on them."
    )
    return 0 if report["pass"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
