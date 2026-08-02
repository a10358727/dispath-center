#!/usr/bin/env python3
"""WP-2D canary evidence collector.

Reads a stable online-backup copy of the control-plane SQLite database and
prints the exact pass/fail criteria from
`docs/DG_WP2D_CANARY_V2_DECISION.md`. It opens the copy read-only and
never writes. Do not point it at a live WAL database: take the documented
online backup at the evidence cutoff first.

The point is that the verdict is computed from persisted evidence, not from an
operator's impression of how the window went. Exit code 0 means every criterion
passed; 1 means at least one failed; 2 means the evidence itself is unusable.

    python scripts/canary_report.py \
      --db backup/jobqueue.db \
      --since 2026-07-28T00:00:00Z \
      --through 2026-07-28T08:00:00Z \
      --server canary-a \
      --evidence evidence/wp2d.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "ssh-canary-evidence-v2"
MIN_WINDOW_SECONDS = 8 * 60 * 60
REQUIRED_DRILLS = (
    "forced_response_loss",
    "control_plane_restart",
    "rollback_to_legacy_ssh",
)


class EvidenceError(ValueError):
    """The input cannot support a canary verdict."""


def _connect(path: str) -> sqlite3.Connection:
    database_path = Path(path).resolve()
    conn = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _parse_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise EvidenceError(f"{field} is not a valid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
        parsed
    ):
        raise EvidenceError(f"{field} must be UTC")
    return parsed


def _scope() -> str:
    return (
        "julianday(created_at) >= julianday(?) "
        "AND julianday(created_at) <= julianday(?) "
        "AND backend = 'ssh' AND server_name = ?"
    )


def collect(
    conn: sqlite3.Connection,
    since: str,
    through: str,
    server_name: str,
) -> dict[str, Any]:
    scope = _scope()
    scope_params = (since, through, server_name)
    attempts_total = _scalar(
        conn,
        f"SELECT COUNT(*) FROM execution_attempts WHERE {scope}",
        scope_params,
    )
    jobs_with_multiple_active = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM (
            SELECT job_id FROM execution_attempts
            WHERE {scope}
              AND state IN ('leased', 'dispatching', 'running')
            GROUP BY job_id HAVING COUNT(*) > 1
        )
        """,
        scope_params,
    )
    # A duplicate launch would show up as two launcher-claimed attempts for one
    # Job. The partial unique index should make this impossible; counting it
    # anyway is the point of a canary.
    duplicate_launches = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM (
            SELECT job_id FROM execution_attempts
            WHERE {scope}
              AND remote_claim_state = 'launcher_claimed'
            GROUP BY job_id HAVING COUNT(*) > 1
        )
        """,
        scope_params,
    )
    unresolved_unknown = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM execution_attempts
        WHERE {scope}
          AND liveness = 'unknown'
          AND state IN ('leased', 'dispatching', 'running')
        """,
        scope_params,
    )
    terminal_attempts = _scalar(
        conn,
        f"SELECT COUNT(*) FROM execution_attempts"
        f" WHERE {scope} AND state IN ('done', 'failed')",
        scope_params,
    )
    non_terminal_attempts = _scalar(
        conn,
        f"SELECT COUNT(*) FROM execution_attempts"
        f" WHERE {scope}"
        " AND state IN ('leased', 'dispatching', 'running')",
        scope_params,
    )
    # A false failure is a Job marked failed while its attempt never observed a
    # terminal sentinel; that is exactly what the ambiguous-launch fix forbids.
    false_failures = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM execution_attempts a
        JOIN jobs j ON j.id = a.job_id
        WHERE julianday(a.created_at) >= julianday(?)
          AND julianday(a.created_at) <= julianday(?)
          AND a.backend = 'ssh' AND a.server_name = ?
          AND j.status = 'failed'
          AND a.state = 'failed'
          AND (a.exit_code IS NULL OR a.exit_code = 0)
        """,
        scope_params,
    )
    lost_terminals = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM execution_attempts a
        JOIN jobs j ON j.id = a.job_id
        WHERE julianday(a.created_at) >= julianday(?)
          AND julianday(a.created_at) <= julianday(?)
          AND a.backend = 'ssh' AND a.server_name = ?
          AND a.state IN ('done', 'failed')
          AND j.status <> a.state
        """,
        scope_params,
    )
    collect_ops = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_operations operation
        JOIN execution_attempts attempt ON attempt.id = operation.attempt_id
        WHERE operation.operation = 'collect'
          AND julianday(attempt.created_at) >= julianday(?)
          AND julianday(attempt.created_at) <= julianday(?)
          AND attempt.backend = 'ssh' AND attempt.server_name = ?
        """,
        scope_params,
    )
    collect_delivered = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_operations operation
        JOIN execution_attempts attempt ON attempt.id = operation.attempt_id
        WHERE operation.operation = 'collect'
          AND operation.state = 'delivered'
          AND julianday(attempt.created_at) >= julianday(?)
          AND julianday(attempt.created_at) <= julianday(?)
          AND attempt.backend = 'ssh' AND attempt.server_name = ?
        """,
        scope_params,
    )
    uncertain_ops = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_operations operation
        JOIN execution_attempts attempt ON attempt.id = operation.attempt_id
        WHERE operation.state = 'uncertain'
          AND julianday(attempt.created_at) >= julianday(?)
          AND julianday(attempt.created_at) <= julianday(?)
          AND attempt.backend = 'ssh' AND attempt.server_name = ?
        """,
        scope_params,
    )
    requeues = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM execution_attempt_events
        WHERE event_type = 'job_requeued_after_abandon'
          AND julianday(created_at) >= julianday(?)
          AND julianday(created_at) <= julianday(?)
          AND attempt_id IN (
              SELECT id FROM execution_attempts
              WHERE backend = 'ssh' AND server_name = ?
          )
        """,
        scope_params,
    )
    return {
        "since": since,
        "through": through,
        "server_name": server_name,
        "window_seconds": int(
            (
                _parse_utc(through, field="through")
                - _parse_utc(since, field="since")
            ).total_seconds()
        ),
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


def _load_evidence(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read drill evidence: {exc}") from None
    if not isinstance(value, dict):
        raise EvidenceError("drill evidence must be one JSON object")
    return value


def _evaluate_evidence(
    metrics: dict[str, Any],
    evidence: dict[str, Any],
) -> list[tuple[str, bool, str]]:
    expected_fields = {
        "contract_version",
        "environment",
        "candidate_commit",
        "server_name",
        "since",
        "through",
        "drills",
    }
    results = [
        (
            "evidence object has the exact contract fields",
            set(evidence) == expected_fields,
            str(sorted(evidence)),
        ),
        (
            "evidence contract is pinned",
            evidence.get("contract_version") == CONTRACT_VERSION,
            str(evidence.get("contract_version")),
        ),
        (
            "environment is explicitly non-production",
            evidence.get("environment") == "non-production",
            str(evidence.get("environment")),
        ),
        (
            "candidate commit has an exact immutable identity",
            isinstance(evidence.get("candidate_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", evidence["candidate_commit"])
            is not None,
            str(evidence.get("candidate_commit")),
        ),
        (
            "server and evidence window match the database query",
            evidence.get("server_name") == metrics["server_name"]
            and evidence.get("since") == metrics["since"]
            and evidence.get("through") == metrics["through"],
            (
                f"{evidence.get('server_name')}: "
                f"{evidence.get('since')} → {evidence.get('through')}"
            ),
        ),
    ]
    drills = evidence.get("drills")
    for drill_name in REQUIRED_DRILLS:
        drill = drills.get(drill_name) if isinstance(drills, dict) else None
        valid = (
            isinstance(drill, dict)
            and set(drill) == {"passed", "observed_at", "evidence_ref"}
            and drill.get("passed") is True
            and isinstance(drill.get("evidence_ref"), str)
            and bool(drill["evidence_ref"].strip())
        )
        if valid:
            try:
                observed_at = _parse_utc(
                    drill.get("observed_at"),
                    field=f"drills.{drill_name}.observed_at",
                )
                valid = (
                    _parse_utc(metrics["since"], field="since")
                    <= observed_at
                    <= _parse_utc(metrics["through"], field="through")
                )
            except EvidenceError:
                valid = False
        results.append(
            (
                f"drill passed: {drill_name}",
                valid,
                (
                    drill.get("evidence_ref")
                    if isinstance(drill, dict)
                    else "missing"
                ),
            )
        )
    return results


def evaluate(
    metrics: dict[str, Any],
    evidence: dict[str, Any],
    min_jobs: int,
) -> list[tuple[str, bool, str]]:
    """The §9 exit criteria, each as an independently checkable line."""
    collect_ops = metrics["collect_operations"]
    collect_rate_ok = (
        collect_ops == metrics["terminal_attempts"]
        and metrics["collect_delivered"] == collect_ops
    )
    results = [
        (
            "window is at least 8 hours",
            metrics["window_seconds"] >= MIN_WINDOW_SECONDS,
            f"{metrics['window_seconds']} seconds",
        ),
        (
            f"at least {min_jobs} terminal workloads in the window",
            metrics["terminal_attempts"] >= min_jobs,
            f"{metrics['terminal_attempts']} terminal workloads",
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
            (
                f"{metrics['collect_delivered']}/{collect_ops} delivered; "
                f"{metrics['terminal_attempts']} terminal attempts"
            ),
        ),
        (
            "zero unresolved unknown attempts",
            metrics["unresolved_unknown"] == 0,
            f"{metrics['unresolved_unknown']} unresolved",
        ),
        (
            "zero uncertain operations at close",
            metrics["uncertain_operations"] == 0,
            f"{metrics['uncertain_operations']} uncertain",
        ),
    ]
    results.extend(_evaluate_evidence(metrics, evidence))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the control-plane SQLite file")
    parser.add_argument(
        "--since",
        required=True,
        help="ISO-8601 UTC timestamp marking the start of the canary window",
    )
    parser.add_argument(
        "--through",
        required=True,
        help="ISO-8601 UTC timestamp marking the stable evidence cutoff",
    )
    parser.add_argument("--server", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--min-jobs", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="print raw metrics as JSON")
    args = parser.parse_args()

    try:
        since = _parse_utc(args.since, field="since")
        through = _parse_utc(args.through, field="through")
        if through <= since:
            raise EvidenceError("through must be later than since")
        if args.min_jobs < 1:
            raise EvidenceError("min-jobs must be positive")
        if not args.server.strip():
            raise EvidenceError("server must not be blank")
        conn = _connect(args.db)
        try:
            metrics = collect(
                conn,
                args.since,
                args.through,
                args.server,
            )
        finally:
            conn.close()
        evidence = _load_evidence(args.evidence)
        results = evaluate(metrics, evidence, args.min_jobs)
    except (EvidenceError, sqlite3.Error) as exc:
        print(f"UNUSABLE: cannot read evidence: {exc}", file=sys.stderr)
        return 2

    passed = all(ok for _, ok, _ in results)
    if args.json:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "metrics": metrics,
                    "criteria": [
                        {"name": name, "passed": ok, "detail": detail}
                        for name, ok, detail in results
                    ],
                    "passed": passed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if passed else 1

    width = max(len(name) for name, _, _ in results)
    print(
        f"WP-2D canary evidence: {args.server} "
        f"{args.since} → {args.through}\n"
    )
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
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
