#!/usr/bin/env python3
"""Phase 5 Node canary evidence evaluator.

This command is read-only.  It combines durable SQLite facts with a strict,
non-secret drill manifest and evaluates the Phase 5 contract from
``docs/NEXT_IMPLEMENTATION_PLAN.md`` §10.  Local tests can prove this evaluator
fails closed; only a real two-Node, seven-day run can make it print PASS.

Example:

    python scripts/node_canary_report.py \
      --db jobqueue.db \
      --since 2026-08-01T00:00:00Z \
      --through 2026-08-08T00:00:00Z \
      --require-tag node-canary \
      --evidence evidence/node-canary-20260801.json
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


CONTRACT_VERSION = "node-canary-evidence-v1"
REQUIRED_DRILLS = (
    "single_node_24h",
    "control_plane_restart",
    "agent_restart",
    "network_interruption",
    "staged_rotation",
    "activation_response_loss",
    "second_node_rollout",
    "rollback_to_ssh",
    "emergency_security_revoke",
)
MIN_WINDOW_SECONDS = 7 * 24 * 60 * 60


class EvidenceError(ValueError):
    """The supplied evidence cannot support any canary verdict."""


def parse_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{field} must be an ISO-8601 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise EvidenceError(f"{field} is not a valid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise EvidenceError(f"{field} must be UTC")
    return parsed


def connect_read_only(path: str) -> sqlite3.Connection:
    database_path = Path(path).resolve()
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _scalar(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> int:
    row = connection.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _scope(require_tag: str) -> tuple[str, tuple[Any, ...]]:
    return (
        """
        julianday(node_attempt.created_at) >= julianday(?)
        AND julianday(node_attempt.created_at) <= julianday(?)
        AND job.require_tag = ?
        """,
        (require_tag,),
    )


def collect(
    connection: sqlite3.Connection,
    *,
    since: str,
    through: str,
    require_tag: str,
) -> dict[str, Any]:
    """Collect only facts durably represented by the current schema."""
    scope, tag_params = _scope(require_tag)
    params = (since, through, *tag_params)
    node_rows = connection.execute(
        f"""
        SELECT DISTINCT node_attempt.node_id
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
          AND node_attempt.acked_at IS NOT NULL
        ORDER BY node_attempt.node_id
        """,
        params,
    ).fetchall()
    node_ids = [str(row["node_id"]) for row in node_rows]
    node_details: list[dict[str, Any]] = []
    if node_ids:
        node_details = [
            {
                "node_id": str(row["id"]),
                "server": str(row["server_name"]),
                "agent_version": row["agent_version"],
                "credential_id": row["primary_credential_id"],
            }
            for row in connection.execute(
                """
                SELECT id, server_name, agent_version, primary_credential_id
                FROM nodes
                WHERE id IN ({})
                ORDER BY id
                """.format(",".join("?" for _ in node_ids)),
                tuple(node_ids),
            ).fetchall()
        ]

    total_attempts = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
        """,
        params,
    )
    acknowledged_workloads = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
          AND node_attempt.acked_at IS NOT NULL
        """,
        params,
    )
    terminal_attempts = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
          AND node_attempt.status IN ('done', 'failed')
          AND node_attempt.terminal_at IS NOT NULL
        """,
        params,
    )
    active_attempts = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
          AND node_attempt.terminal_at IS NULL
          AND (
                (node_attempt.status = 'leased'
                 AND julianday(node_attempt.lease_expires_at) > julianday(?))
                OR node_attempt.status IN ('acked', 'running')
              )
        """,
        (*params, through),
    )
    duplicate_workload_launches = _scalar(
        connection,
        f"""
        SELECT COUNT(*) FROM (
            SELECT node_attempt.job_id
            FROM node_attempts AS node_attempt
            JOIN jobs AS job ON job.id = node_attempt.job_id
            WHERE {scope}
              AND node_attempt.acked_at IS NOT NULL
            GROUP BY node_attempt.job_id
            HAVING COUNT(*) > 1
        )
        """,
        params,
    )
    cross_node_launches = _scalar(
        connection,
        f"""
        SELECT COUNT(*) FROM (
            SELECT node_attempt.job_id
            FROM node_attempts AS node_attempt
            JOIN jobs AS job ON job.id = node_attempt.job_id
            WHERE {scope}
              AND node_attempt.acked_at IS NOT NULL
            GROUP BY node_attempt.job_id
            HAVING COUNT(DISTINCT node_attempt.node_id) > 1
        )
        """,
        params,
    )
    false_disconnect_failures = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
          AND job.status = 'failed'
          AND (
                node_attempt.status NOT IN ('done', 'failed')
                OR node_attempt.terminal_at IS NULL
                OR node_attempt.exit_code IS NULL
              )
        """,
        params,
    )
    lost_or_conflicting_terminals = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        LEFT JOIN execution_attempts AS execution
          ON execution.id = node_attempt.execution_attempt_id
        WHERE {scope}
          AND node_attempt.status IN ('done', 'failed')
          AND (
                node_attempt.terminal_at IS NULL
                OR job.status <> node_attempt.status
                OR (
                    node_attempt.execution_attempt_id IS NOT NULL
                    AND (
                        execution.id IS NULL
                        OR execution.state <> node_attempt.status
                    )
                )
              )
        """,
        params,
    )
    cross_target_violations = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        JOIN nodes AS node ON node.id = node_attempt.node_id
        LEFT JOIN execution_attempts AS execution
          ON execution.id = node_attempt.execution_attempt_id
        WHERE {scope}
          AND (
                (job.pin_server IS NOT NULL
                 AND job.pin_server <> node.server_name)
                OR (
                    node_attempt.execution_attempt_id IS NOT NULL
                    AND (
                        execution.id IS NULL
                        OR execution.backend <> 'node'
                        OR execution.server_name <> node.server_name
                    )
                )
              )
        """,
        params,
    )
    collection_projection_failures = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        WHERE {scope}
          AND node_attempt.status = 'done'
          AND job.status = 'failed'
        """,
        params,
    )
    completion_operation_failures = _scalar(
        connection,
        f"""
        SELECT COUNT(*) FROM (
            SELECT node_attempt.id
            FROM node_attempts AS node_attempt
            JOIN jobs AS job ON job.id = node_attempt.job_id
            LEFT JOIN execution_completion_operations AS completion
              ON completion.attempt_id = node_attempt.execution_attempt_id
            WHERE {scope}
              AND node_attempt.acked_at IS NOT NULL
              AND node_attempt.status IN ('done', 'failed')
              AND node_attempt.terminal_at IS NOT NULL
            GROUP BY node_attempt.id
            HAVING COUNT(completion.id) <> 4
               OR SUM(
                   CASE WHEN completion.state = 'delivered' THEN 1 ELSE 0 END
               ) <> 4
               OR COUNT(DISTINCT completion.operation) <> 4
        )
        """,
        params,
    )
    unresolved_unknown = _scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        JOIN execution_attempts AS execution
          ON execution.id = node_attempt.execution_attempt_id
        WHERE {scope}
          AND execution.state IN ('leased', 'dispatching', 'running')
          AND execution.liveness = 'unknown'
        """,
        params,
    )
    security_revoke_events = _scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT event.id)
        FROM node_attempts AS node_attempt
        JOIN jobs AS job ON job.id = node_attempt.job_id
        JOIN execution_attempt_events AS event
          ON event.attempt_id = node_attempt.execution_attempt_id
        WHERE {scope}
          AND event.reason_code = 'security_credential_revoked'
        """,
        params,
    )
    rotated_nodes = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM nodes
        WHERE id IN ({})
          AND julianday(last_activated_at) >= julianday(?)
          AND julianday(last_activated_at) <= julianday(?)
        """.format(",".join("?" for _ in node_ids) or "NULL"),
        (*node_ids, since, through),
    )
    return {
        "since": since,
        "through": through,
        "require_tag": require_tag,
        "window_seconds": int(
            (parse_utc(through, field="through") - parse_utc(since, field="since"))
            .total_seconds()
        ),
        "node_ids": node_ids,
        "nodes": node_details,
        "distinct_nodes": len(node_ids),
        "node_attempts_total": total_attempts,
        "acknowledged_workloads": acknowledged_workloads,
        "terminal_attempts": terminal_attempts,
        "active_attempts_at_close": active_attempts,
        "duplicate_workload_launches": duplicate_workload_launches,
        "cross_node_launches": cross_node_launches,
        "false_disconnect_failures": false_disconnect_failures,
        "lost_or_conflicting_terminals": lost_or_conflicting_terminals,
        "cross_target_violations": cross_target_violations,
        "collection_projection_failures": collection_projection_failures,
        "completion_operation_failures": completion_operation_failures,
        "unresolved_active_unknown": unresolved_unknown,
        "security_revoke_events": security_revoke_events,
        "rotated_nodes": rotated_nodes,
    }


def load_evidence(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read drill evidence: {exc}") from None
    if not isinstance(value, dict):
        raise EvidenceError("drill evidence must be one JSON object")
    return value


def evaluate_evidence(
    evidence: dict[str, Any],
    *,
    metrics: dict[str, Any],
) -> list[tuple[str, bool, str]]:
    """Validate the externally witnessed drills without accepting free-form claims."""
    since = metrics["since"]
    through = metrics["through"]
    expected_top_level = {
        "contract_version",
        "environment",
        "candidate_commit",
        "require_tag",
        "since",
        "through",
        "nodes",
        "drills",
    }
    results: list[tuple[str, bool, str]] = []
    results.append(
        (
            "evidence object has the exact contract fields",
            set(evidence) == expected_top_level,
            str(sorted(evidence)),
        )
    )
    results.append(
        (
            "evidence contract is pinned",
            evidence.get("contract_version") == CONTRACT_VERSION,
            str(evidence.get("contract_version")),
        )
    )
    results.append(
        (
            "environment is explicitly non-production",
            evidence.get("environment") == "non-production",
            str(evidence.get("environment")),
        )
    )
    results.append(
        (
            "candidate commit has an exact immutable identity",
            isinstance(evidence.get("candidate_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", evidence["candidate_commit"])
            is not None,
            str(evidence.get("candidate_commit")),
        )
    )
    results.append(
        (
            "canary tag matches the database query",
            evidence.get("require_tag") == metrics["require_tag"],
            str(evidence.get("require_tag")),
        )
    )
    results.append(
        (
            "evidence window matches database query",
            evidence.get("since") == since and evidence.get("through") == through,
            f"{evidence.get('since')} → {evidence.get('through')}",
        )
    )

    declared_nodes = evidence.get("nodes")
    declared_ids: list[str] = []
    declared_details: list[dict[str, Any]] = []
    node_entries_valid = False
    if isinstance(declared_nodes, list) and len(declared_nodes) >= 2:
        node_entries_valid = True
        for item in declared_nodes:
            if not (
                isinstance(item, dict)
                and set(item)
                == {
                    "node_id",
                    "server",
                    "agent_version",
                    "credential_id",
                    "ssh_fallback_verified",
                }
                and all(
                    isinstance(item.get(field), str) and bool(item[field].strip())
                    for field in (
                        "node_id",
                        "server",
                        "agent_version",
                        "credential_id",
                    )
                )
                and item.get("ssh_fallback_verified") is True
            ):
                node_entries_valid = False
                break
            declared_ids.append(item["node_id"])
            declared_details.append(
                {
                    "node_id": item["node_id"],
                    "server": item["server"],
                    "agent_version": item["agent_version"],
                    "credential_id": item["credential_id"],
                }
            )
    results.append(
        (
            "two independent Node identities and SSH fallbacks are declared",
            node_entries_valid
            and len(set(declared_ids)) == len(declared_ids)
            and sorted(
                declared_details, key=lambda item: item["node_id"]
            )
            == metrics["nodes"],
            (
                f"declared={sorted(declared_details, key=lambda item: item['node_id'])}, "
                f"observed={metrics['nodes']}"
            ),
        )
    )

    drills = evidence.get("drills")
    for name in REQUIRED_DRILLS:
        item = drills.get(name) if isinstance(drills, dict) else None
        valid = (
            isinstance(item, dict)
            and set(item) == {"passed", "observed_at", "evidence_ref"}
            and item.get("passed") is True
            and isinstance(item.get("evidence_ref"), str)
            and bool(item["evidence_ref"].strip())
        )
        if valid and isinstance(item, dict):
            try:
                observed_at = parse_utc(
                    item.get("observed_at"), field=f"drills.{name}.observed_at"
                )
                valid = (
                    parse_utc(since, field="since")
                    <= observed_at
                    <= parse_utc(through, field="through")
                )
            except EvidenceError:
                valid = False
        results.append(
            (
                f"drill passed: {name}",
                valid,
                str(item.get("evidence_ref")) if isinstance(item, dict) else "missing",
            )
        )
    return results


def evaluate(
    metrics: dict[str, Any],
    evidence: dict[str, Any],
    *,
    min_jobs: int = 100,
    min_nodes: int = 2,
) -> list[tuple[str, bool, str]]:
    results = [
        (
            "window is at least seven consecutive days",
            metrics["window_seconds"] >= MIN_WINDOW_SECONDS,
            f"{metrics['window_seconds']} seconds",
        ),
        (
            f"at least {min_jobs} acknowledged workloads",
            metrics["acknowledged_workloads"] >= min_jobs,
            str(metrics["acknowledged_workloads"]),
        ),
        (
            f"at least {min_nodes} Nodes executed work",
            metrics["distinct_nodes"] >= min_nodes,
            f"{metrics['distinct_nodes']}: {metrics['node_ids']}",
        ),
        (
            "every acknowledged workload has a terminal",
            metrics["terminal_attempts"]
            == metrics["acknowledged_workloads"],
            (
                f"{metrics['terminal_attempts']} terminal / "
                f"{metrics['acknowledged_workloads']} acknowledged"
            ),
        ),
        (
            "zero duplicate workload launches",
            metrics["duplicate_workload_launches"] == 0,
            str(metrics["duplicate_workload_launches"]),
        ),
        (
            "zero cross-Node launches for one Job",
            metrics["cross_node_launches"] == 0,
            str(metrics["cross_node_launches"]),
        ),
        (
            "zero false disconnect failures",
            metrics["false_disconnect_failures"] == 0,
            str(metrics["false_disconnect_failures"]),
        ),
        (
            "zero lost or conflicting terminal projections",
            metrics["lost_or_conflicting_terminals"] == 0,
            str(metrics["lost_or_conflicting_terminals"]),
        ),
        (
            "zero cross-server/pin violations",
            metrics["cross_target_violations"] == 0,
            str(metrics["cross_target_violations"]),
        ),
        (
            "result collection never changed execution success to failure",
            metrics["collection_projection_failures"] == 0,
            str(metrics["collection_projection_failures"]),
        ),
        (
            "all terminal completion operations were delivered",
            metrics["completion_operation_failures"] == 0,
            str(metrics["completion_operation_failures"]),
        ),
        (
            "all active attempts converged at close",
            metrics["active_attempts_at_close"] == 0,
            str(metrics["active_attempts_at_close"]),
        ),
        (
            "zero unresolved active unknown attempts",
            metrics["unresolved_active_unknown"] == 0,
            str(metrics["unresolved_active_unknown"]),
        ),
        (
            "staged activation has durable evidence",
            metrics["rotated_nodes"] >= 1,
            f"{metrics['rotated_nodes']} rotated Nodes",
        ),
        (
            "security-revoke hold has durable evidence",
            metrics["security_revoke_events"] >= 1,
            f"{metrics['security_revoke_events']} events",
        ),
    ]
    results.extend(
        evaluate_evidence(
            evidence,
            metrics=metrics,
        )
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--through", required=True)
    parser.add_argument("--require-tag", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--min-jobs", type=int, default=100)
    parser.add_argument("--min-nodes", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        since = parse_utc(args.since, field="since")
        through = parse_utc(args.through, field="through")
        if through <= since:
            raise EvidenceError("through must be later than since")
        if args.min_jobs < 1 or args.min_nodes < 2:
            raise EvidenceError("minimums must be positive and min-nodes >= 2")
        evidence = load_evidence(args.evidence)
        connection = connect_read_only(args.db)
        try:
            metrics = collect(
                connection,
                since=args.since,
                through=args.through,
                require_tag=args.require_tag,
            )
        finally:
            connection.close()
        results = evaluate(
            metrics,
            evidence,
            min_jobs=args.min_jobs,
            min_nodes=args.min_nodes,
        )
    except (EvidenceError, sqlite3.Error) as exc:
        print(f"UNUSABLE: {exc}", file=sys.stderr)
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
    else:
        width = max(len(name) for name, _, _ in results)
        print(
            f"Phase 5 Node canary evidence: {args.since} → {args.through}\n"
        )
        for name, ok, detail in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
        print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
