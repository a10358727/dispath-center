#!/usr/bin/env python3
"""Read-only audit export outbox gate.

The durable SQLite outbox is authoritative for export health. This command
does not open the application, run migrations, claim work, or touch the
JSONL sink. It is suitable for an operator check or an offline evidence
collector:

    python scripts/audit_export_status.py --db jobqueue.db --require-clear

Without ``--require-clear`` the command reports ``clear`` or ``attention``
and exits successfully. With it, any pending/failed/processing backlog or
dead-letter row returns a non-zero exit status. The threshold is deliberately
explicit here; the command does not invent a production SLO or readiness
policy.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# This file is also invoked directly from a checkout (not only as an imported
# test module), so make the repository package discoverable in that mode.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.audit import audit_export_alert_snapshot  # noqa: E402


class AuditExportStatusError(RuntimeError):
    """The requested database cannot provide durable outbox evidence."""


def read_audit_export_status(database_path: str | Path) -> dict[str, Any]:
    """Read durable outbox counts from a SQLite database without writing it."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'audit_export_operations'
            """
        ).fetchone()
        if table is None:
            raise AuditExportStatusError(
                "durable audit export outbox is missing or unreadable"
            )

        rows = connection.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM audit_export_operations
            GROUP BY state
            ORDER BY state
            """
        ).fetchall()
        by_state = {str(row["state"]): int(row["count"]) for row in rows}
        processing_age = connection.execute(
            """
            SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE CAST(
                MAX(0, (julianday('now') - julianday(MIN(updated_at))) * 86400)
            AS INTEGER) END AS age_seconds
            FROM audit_export_operations
            WHERE state = 'processing'
            """
        ).fetchone()["age_seconds"]
    except sqlite3.Error as exc:
        raise AuditExportStatusError(
            "durable audit export outbox is missing or unreadable"
        ) from exc
    finally:
        connection.close()

    retryable_states = ("pending", "failed", "processing")
    backlog = sum(by_state.get(state, 0) for state in retryable_states)
    dead_letter = by_state.get("dead_letter", 0)
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "database": str(path),
        "total": sum(by_state.values()),
        "by_state": by_state,
        "backlog": backlog,
        "dead_letter": dead_letter,
        "oldest_processing_age_seconds": (
            int(processing_age) if processing_age is not None else None
        ),
        **audit_export_alert_snapshot(
            backlog=backlog,
            dead_letter=dead_letter,
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite database to inspect")
    parser.add_argument(
        "--require-clear",
        action="store_true",
        help="exit 1 when backlog or dead-letter rows are present",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = read_audit_export_status(args.db)
    except (AuditExportStatusError, OSError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"audit export status failed: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Audit export status: {report['status']}")
        print(f"  database         : {report['database']}")
        print(f"  backlog          : {report['backlog']}")
        print(f"  dead-letter      : {report['dead_letter']}")
        print(
            "  oldest processing: "
            f"{report['oldest_processing_age_seconds']}s"
        )

    if args.require_clear and report["status"] != "clear":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
