#!/usr/bin/env python3
"""WP-2D canary evidence collector.

Reads a control-plane SQLite database and prints the exact pass/fail criteria
from `docs/DG_AMBIGUOUS_LAUNCH_DECISION.md` §9. Read-only: it opens the
database in immutable mode and never writes, so it is safe to run against a
live canary.

The point is that the verdict is computed from persisted evidence, not from an
operator's impression of how the window went. Exit code 0 means every criterion
passed; 1 means at least one failed; 2 means the evidence itself is unusable.

    python scripts/canary_report.py --db jobqueue.db --since 2026-07-28T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def collect(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    attempts_total = _scalar(
        conn,
        "SELECT COUNT(*) FROM execution_attempts WHERE created_at >= ?",
        (since,),
    )
    jobs_with_multiple_active = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM (
            SELECT job_id FROM execution_attempts
            WHERE state IN ('leased', 'dispatching', 'running')
            GROUP BY job_id HAVING COUNT(*) > 1
        )
        """,
    )
    # A duplicate launch would show up as two launcher-claimed attempts for one
    # Job. The partial unique index should make this impossible; counting it
    # anyway is the point of a canary.
    duplicate_launches = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM (
            SELECT job_id FROM execution_attempts
            WHERE remote_claim_state = 'launcher_claimed'
            GROUP BY job_id HAVING COUNT(*) > 1
        )
        """,
    )
    unresolved_unknown = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_attempts
        WHERE liveness = 'unknown'
          AND state IN ('leased', 'dispatching', 'running')
        """,
    )
    terminal_attempts = _scalar(
        conn,
        "SELECT COUNT(*) FROM execution_attempts"
        " WHERE created_at >= ? AND state IN ('done', 'failed')",
        (since,),
    )
    non_terminal_attempts = _scalar(
        conn,
        "SELECT COUNT(*) FROM execution_attempts"
        " WHERE created_at >= ?"
        " AND state IN ('leased', 'dispatching', 'running')",
        (since,),
    )
    # A false failure is a Job marked failed while its attempt never observed a
    # terminal sentinel; that is exactly what the ambiguous-launch fix forbids.
    false_failures = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_attempts a
        JOIN jobs j ON j.id = a.job_id
        WHERE a.created_at >= ?
          AND j.status = 'failed'
          AND a.state = 'failed'
          AND a.exit_code IS NULL
        """,
        (since,),
    )
    lost_terminals = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_attempts a
        JOIN jobs j ON j.id = a.job_id
        WHERE a.created_at >= ?
          AND a.state IN ('done', 'failed')
          AND j.status = 'running'
        """,
        (since,),
    )
    collect_ops = _scalar(
        conn,
        "SELECT COUNT(*) FROM execution_operations WHERE operation = 'collect'"
        " AND created_at >= ?",
        (since,),
    )
    collect_delivered = _scalar(
        conn,
        "SELECT COUNT(*) FROM execution_operations WHERE operation = 'collect'"
        " AND state = 'delivered' AND created_at >= ?",
        (since,),
    )
    uncertain_ops = _scalar(
        conn,
        "SELECT COUNT(*) FROM execution_operations"
        " WHERE state = 'uncertain' AND created_at >= ?",
        (since,),
    )
    requeues = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_attempt_events
        WHERE event_type = 'job_requeued_after_abandon' AND created_at >= ?
        """,
        (since,),
    )
    return {
        "since": since,
        "attempts_total": attempts_total,
        "terminal_attempts": terminal_attempts,
        "non_terminal_attempts": non_terminal_attempts,
        "duplicate_launches": duplicate_launches,
        "jobs_with_multiple_active_attempts": jobs_with_multiple_active,
        "false_failures": false_failures,
        "lost_terminals": lost_terminals,
        "unresolved_unknown": unresolved_unknown,
        "collect_operations": collect_ops,
        "collect_delivered": collect_delivered,
        "uncertain_operations": uncertain_ops,
        "proven_requeues": requeues,
    }


def evaluate(metrics: dict[str, Any], min_jobs: int) -> list[tuple[str, bool, str]]:
    """The §9 exit criteria, each as an independently checkable line."""
    collect_ops = metrics["collect_operations"]
    collect_rate_ok = collect_ops == 0 or metrics["collect_delivered"] == collect_ops
    return [
        (
            f"at least {min_jobs} attempts in the window",
            metrics["attempts_total"] >= min_jobs,
            f"{metrics['attempts_total']} attempts",
        ),
        (
            "zero duplicate launches",
            metrics["duplicate_launches"] == 0,
            f"{metrics['duplicate_launches']} jobs with >1 launcher-claimed attempt",
        ),
        (
            "at most one active attempt per Job",
            metrics["jobs_with_multiple_active_attempts"] == 0,
            f"{metrics['jobs_with_multiple_active_attempts']} violations",
        ),
        (
            "zero false failures",
            metrics["false_failures"] == 0,
            f"{metrics['false_failures']} failed jobs without an exit code",
        ),
        (
            "zero lost terminals",
            metrics["lost_terminals"] == 0,
            f"{metrics['lost_terminals']} terminal attempts whose Job is still running",
        ),
        (
            "all attempts converged",
            metrics["non_terminal_attempts"] == 0,
            f"{metrics['non_terminal_attempts']} still non-terminal",
        ),
        (
            "result collection 100%",
            collect_rate_ok,
            f"{metrics['collect_delivered']}/{collect_ops} delivered",
        ),
        (
            "zero unresolved unknown attempts",
            metrics["unresolved_unknown"] == 0,
            f"{metrics['unresolved_unknown']} unresolved",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the control-plane SQLite file")
    parser.add_argument(
        "--since",
        required=True,
        help="ISO-8601 UTC timestamp marking the start of the canary window",
    )
    parser.add_argument("--min-jobs", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="print raw metrics as JSON")
    args = parser.parse_args()

    try:
        conn = _connect(args.db)
        metrics = collect(conn, args.since)
    except sqlite3.Error as exc:
        print(f"UNUSABLE: cannot read evidence: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
        return 0

    results = evaluate(metrics, args.min_jobs)
    width = max(len(name) for name, _, _ in results)
    print(f"WP-2D canary evidence since {args.since}\n")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    passed = all(ok for _, ok, _ in results)
    print(
        f"\n  proven requeues (definite pre-launch only): {metrics['proven_requeues']}"
    )
    print(f"  operations still uncertain: {metrics['uncertain_operations']}")
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    if not passed:
        print(
            "\nA failing line does not close RB-LAUNCH-001. Investigate the"
            " evidence before rerunning the window."
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
