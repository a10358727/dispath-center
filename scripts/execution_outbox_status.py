#!/usr/bin/env python3
"""Read-only execution-attempt outbox evidence gate.

The execution-attempt and Node terminal-completion tables are the durable
source of truth for local outbox work.  This command only opens SQLite in
read-only mode: it does not run migrations, claim rows, contact a backend, or
change Job/attempt state.

Without ``--require-clear`` the command reports ``clear`` or ``attention`` and
exits successfully.  With it, unresolved operation rows (``pending``,
``processing`` or ``uncertain``) return a non-zero exit status.  Terminal
``failed`` rows are reported separately and are not silently counted as
pending work; this is a point-in-time evidence check, not a production SLO or
remote-state conclusion.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class ExecutionOutboxStatusError(RuntimeError):
    """The requested database cannot provide execution outbox evidence."""


_TABLES = (
    "execution_operations",
    "execution_completion_operations",
)


def _group_counts(
    connection: sqlite3.Connection,
    *,
    table: str,
    group_columns: Iterable[str],
) -> dict[str, Any]:
    columns = tuple(group_columns)
    if not columns:
        raise ValueError("group_columns must not be empty")
    selected = ", ".join(columns)
    rows = connection.execute(
        f"""
        SELECT {selected}, COUNT(*) AS count
        FROM {table}
        GROUP BY {selected}
        ORDER BY {selected}
        """
    ).fetchall()
    if len(columns) == 1:
        return {str(row[0]): int(row[1]) for row in rows}

    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        current = grouped.setdefault(str(row[0]), {})
        current[str(row[1])] = int(row[2])
    return grouped


def _oldest_age_seconds(
    connection: sqlite3.Connection,
    *,
    table: str,
    states: tuple[str, ...],
    timestamp_column: str,
) -> int | None:
    placeholders = ", ".join("?" for _ in states)
    row = connection.execute(
        f"""
        SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE CAST(
            MAX(0, (julianday('now') - julianday(MIN({timestamp_column}))) * 86400)
        AS INTEGER) END AS age_seconds
        FROM {table}
        WHERE state IN ({placeholders})
        """,
        states,
    ).fetchone()
    value = row[0] if row is not None else None
    return int(value) if value is not None else None


def read_execution_outbox_status(database_path: str | Path) -> dict[str, Any]:
    """Read both durable execution outboxes without writing the database."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        for table_name in _TABLES:
            table = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table_name,),
            ).fetchone()
            if table is None:
                raise ExecutionOutboxStatusError(
                    "durable execution outbox is missing or unreadable"
                )

        operations_by_state = _group_counts(
            connection,
            table="execution_operations",
            group_columns=("state",),
        )
        operations_by_operation_state = _group_counts(
            connection,
            table="execution_operations",
            group_columns=("operation", "state"),
        )
        completions_by_state = _group_counts(
            connection,
            table="execution_completion_operations",
            group_columns=("state",),
        )
        completions_by_operation_state = _group_counts(
            connection,
            table="execution_completion_operations",
            group_columns=("operation", "state"),
        )
        operation_processing_age = _oldest_age_seconds(
            connection,
            table="execution_operations",
            states=("processing",),
            timestamp_column="updated_at",
        )
        operation_uncertain_age = _oldest_age_seconds(
            connection,
            table="execution_operations",
            states=("uncertain",),
            timestamp_column="updated_at",
        )
        completion_processing_age = _oldest_age_seconds(
            connection,
            table="execution_completion_operations",
            states=("processing",),
            timestamp_column="updated_at",
        )
    except sqlite3.Error as exc:
        raise ExecutionOutboxStatusError(
            "durable execution outbox is missing or unreadable"
        ) from exc
    finally:
        connection.close()

    unresolved_operation_states = ("pending", "processing", "uncertain")
    unresolved_completion_states = ("pending", "processing")
    operation_backlog = sum(
        int(operations_by_state.get(state, 0))
        for state in unresolved_operation_states
    )
    completion_backlog = sum(
        int(completions_by_state.get(state, 0))
        for state in unresolved_completion_states
    )
    uncertain = int(operations_by_state.get("uncertain", 0))
    backlog = operation_backlog + completion_backlog
    reason_codes: list[str] = []
    if operation_backlog:
        reason_codes.append("execution_operations_unresolved")
    if uncertain:
        reason_codes.append("execution_operations_uncertain")
    if completion_backlog:
        reason_codes.append("execution_completion_operations_unresolved")

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "database": str(path),
        "status": "attention" if reason_codes else "clear",
        "reason_codes": reason_codes,
        "backlog": backlog,
        "uncertain": uncertain,
        "operations": {
            "total": sum(int(value) for value in operations_by_state.values()),
            "backlog": operation_backlog,
            "uncertain": uncertain,
            "failed": int(operations_by_state.get("failed", 0)),
            "by_state": operations_by_state,
            "by_operation_state": operations_by_operation_state,
            "oldest_processing_age_seconds": operation_processing_age,
            "oldest_uncertain_age_seconds": operation_uncertain_age,
        },
        "completion": {
            "total": sum(int(value) for value in completions_by_state.values()),
            "backlog": completion_backlog,
            "failed": int(completions_by_state.get("failed", 0)),
            "by_state": completions_by_state,
            "by_operation_state": completions_by_operation_state,
            "oldest_processing_age_seconds": completion_processing_age,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite database to inspect")
    parser.add_argument(
        "--require-clear",
        action="store_true",
        help="exit 1 when unresolved execution outbox rows are present",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = read_execution_outbox_status(args.db)
    except (ExecutionOutboxStatusError, OSError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"execution outbox status failed: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        operations = report["operations"]
        completion = report["completion"]
        print(f"Execution outbox status: {report['status']}")
        print(f"  database            : {report['database']}")
        print(f"  unresolved backlog  : {report['backlog']}")
        print(f"  uncertain operations: {report['uncertain']}")
        print(f"  operation failed    : {operations['failed']}")
        print(f"  completion failed   : {completion['failed']}")

    if args.require_clear and report["status"] != "clear":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
