"""Phase 5 evidence evaluator fails closed and never manufactures a canary."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.db import Database
from scripts.node_canary_report import (
    CONTRACT_VERSION,
    REQUIRED_DRILLS,
    collect,
    evaluate,
)


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "node_canary_report.py"
SINCE = "2026-08-01T00:00:00Z"
THROUGH = "2026-08-08T00:00:00Z"


def _evidence() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "environment": "non-production",
        "candidate_commit": "a" * 40,
        "require_tag": "node-canary",
        "since": SINCE,
        "through": THROUGH,
        "nodes": [
            {
                "node_id": "node-a",
                "server": "compute-a",
                "agent_version": "test-v2",
                "credential_id": "credential-a",
                "ssh_fallback_verified": True,
            },
            {
                "node_id": "node-b",
                "server": "compute-b",
                "agent_version": "test-v2",
                "credential_id": "credential-b",
                "ssh_fallback_verified": True,
            },
        ],
        "drills": {
            name: {
                "passed": True,
                "observed_at": "2026-08-04T00:00:00Z",
                "evidence_ref": f"evidence/{name}.json",
            }
            for name in REQUIRED_DRILLS
        },
    }


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "node-canary.db"
    database = Database(str(path))
    database.close()
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = OFF")
    for node_id, server in (("node-a", "compute-a"), ("node-b", "compute-b")):
        connection.execute(
            """
            INSERT INTO nodes
                (id, server_name, secret_hash, status, agent_version, created_at,
                 primary_credential_id, last_activated_at)
            VALUES (?, ?, 'digest', 'enrolled', 'test-v2', ?, ?, ?)
            """,
            (
                node_id,
                server,
                SINCE,
                f"credential-{node_id[-1]}",
                "2026-08-03T00:00:00Z",
            ),
        )
    for index in range(100):
        node_id = "node-a" if index % 2 == 0 else "node-b"
        server = "compute-a" if node_id == "node-a" else "compute-b"
        cursor = connection.execute(
            """
            INSERT INTO jobs
                (command, require_tag, pin_server, status, created_at,
                 finished_at, exit_code)
            VALUES ('true', 'node-canary', ?, 'done', ?, ?, 0)
            """,
            (server, "2026-08-02T00:00:00Z", "2026-08-02T00:01:00Z"),
        )
        job_id = int(cursor.lastrowid)
        execution_id = f"execution-{index}"
        connection.execute(
            """
            INSERT INTO execution_attempts
                (id, job_id, attempt_number, backend, server_name,
                 server_config_revision_id, target_identity_sha256,
                 execution_approval_id, approved_payload_sha256,
                 execution_contract_version, state, liveness, fencing_token,
                 scheduler_fencing_epoch, created_at, terminal_at, exit_code)
            VALUES (?, ?, 1, 'node', ?, 'revision', 'target', 1, 'payload',
                    'enqueue-execution-v1', 'done', 'known', ?, 1, ?, ?, 0)
            """,
            (
                execution_id,
                job_id,
                server,
                f"fence-{index}",
                "2026-08-02T00:00:00Z",
                "2026-08-02T00:01:00Z",
            ),
        )
        for completion_operation in (
            "dependency_refresh",
            "result_collection",
            "notification",
            "owner_projection",
        ):
            connection.execute(
                """
                INSERT INTO execution_completion_operations
                    (id, attempt_id, job_id, operation, idempotency_key,
                     payload_json, payload_sha256, state, attempt_count,
                     output_json, output_sha256, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '{}', ?, 'delivered', 1,
                        '{}', ?, ?, ?)
                """,
                (
                    f"completion-{index}-{completion_operation}",
                    execution_id,
                    job_id,
                    completion_operation,
                    f"completion-key-{index}-{completion_operation}",
                    "p" * 64,
                    "o" * 64,
                    "2026-08-02T00:01:00Z",
                    "2026-08-02T00:01:01Z",
                ),
            )
        connection.execute(
            """
            INSERT INTO node_attempts
                (id, job_id, node_id, status, command_sha256,
                 lease_expires_at, created_at, acked_at, terminal_at,
                 exit_code, execution_attempt_id)
            VALUES (?, ?, ?, 'done', ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                f"node-attempt-{index}",
                job_id,
                node_id,
                "d" * 64,
                "2026-08-02T00:02:00Z",
                "2026-08-02T00:00:00Z",
                "2026-08-02T00:00:10Z",
                "2026-08-02T00:01:00Z",
                execution_id,
            ),
        )
    connection.execute(
        """
        INSERT INTO execution_attempt_events
            (attempt_id, event_type, from_state, to_state, from_liveness,
             to_liveness, reason_code, evidence_json, event_sha256, created_at)
        VALUES ('execution-0', 'security_credential_revoked', 'running',
                'running', 'known', 'unknown',
                'security_credential_revoked', '{}', 'event-digest',
                '2026-08-04T00:00:00Z')
        """
    )
    connection.commit()
    connection.close()
    return path


def _metrics(path: Path) -> dict:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return collect(
            connection,
            since=SINCE,
            through=THROUGH,
            require_tag="node-canary",
        )
    finally:
        connection.close()


def test_clean_two_node_seven_day_evidence_passes(tmp_path):
    metrics = _metrics(_database(tmp_path))
    results = evaluate(metrics, _evidence())

    assert metrics["acknowledged_workloads"] == 100
    assert metrics["node_ids"] == ["node-a", "node-b"]
    assert all(passed for _, passed, _ in results), [
        item for item in results if not item[1]
    ]


def test_duplicate_or_cross_node_launch_fails(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        INSERT INTO node_attempts
            (id, job_id, node_id, status, command_sha256,
             lease_expires_at, created_at, acked_at, terminal_at, exit_code)
        VALUES ('duplicate', 1, 'node-b', 'done', ?, ?, ?, ?, ?, 0)
        """,
        (
            "d" * 64,
            "2026-08-02T00:02:00Z",
            "2026-08-02T00:00:00Z",
            "2026-08-02T00:00:10Z",
            "2026-08-02T00:01:00Z",
        ),
    )
    connection.commit()
    connection.close()

    results = {name: passed for name, passed, _ in evaluate(_metrics(path), _evidence())}

    assert results["zero duplicate workload launches"] is False
    assert results["zero cross-Node launches for one Job"] is False


def test_acknowledged_work_without_done_or_failed_terminal_fails(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(str(path))
    connection.execute(
        """
        UPDATE node_attempts
        SET status = 'expired'
        WHERE id = 'node-attempt-0'
        """
    )
    connection.commit()
    connection.close()

    metrics = _metrics(path)
    results = {
        name: passed for name, passed, _ in evaluate(metrics, _evidence())
    }

    assert metrics["acknowledged_workloads"] == 100
    assert metrics["terminal_attempts"] == 99
    assert results["every acknowledged workload has a terminal"] is False


def test_missing_drill_and_short_window_fail_without_becoming_unusable(tmp_path):
    metrics = _metrics(_database(tmp_path))
    metrics["window_seconds"] = 6 * 24 * 60 * 60
    evidence = _evidence()
    del evidence["drills"]["network_interruption"]

    results = {name: passed for name, passed, _ in evaluate(metrics, evidence)}

    assert results["window is at least seven consecutive days"] is False
    assert results["drill passed: network_interruption"] is False


def test_missing_or_failed_terminal_completion_fails_canary(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(str(path))
    connection.execute(
        """
        UPDATE execution_completion_operations
        SET state = 'failed'
        WHERE id = 'completion-0-result_collection'
        """
    )
    connection.commit()
    connection.close()

    metrics = _metrics(path)
    results = {
        name: passed for name, passed, _ in evaluate(metrics, _evidence())
    }

    assert metrics["completion_operation_failures"] == 1
    assert results["all terminal completion operations were delivered"] is False


def test_declared_node_runtime_identity_must_match_database(tmp_path):
    metrics = _metrics(_database(tmp_path))
    evidence = _evidence()
    evidence["nodes"][0]["agent_version"] = "different-version"

    results = {name: passed for name, passed, _ in evaluate(metrics, evidence)}

    assert (
        results[
            "two independent Node identities and SSH fallbacks are declared"
        ]
        is False
    )


def test_cli_rejects_malformed_evidence_as_unusable(tmp_path):
    path = _database(tmp_path)
    evidence_path = tmp_path / "bad.json"
    evidence_path.write_text("{bad", encoding="utf-8")

    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(path),
            "--since",
            SINCE,
            "--through",
            THROUGH,
            "--require-tag",
            "node-canary",
            "--evidence",
            str(evidence_path),
        ],
        capture_output=True,
        text=True,
    )

    assert process.returncode == 2
    assert "UNUSABLE" in process.stderr


def test_cli_is_read_only_and_json_exit_reflects_verdict(tmp_path):
    path = _database(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    evidence = _evidence()
    evidence["drills"]["agent_restart"]["passed"] = False
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    before = path.read_bytes()

    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(path),
            "--since",
            SINCE,
            "--through",
            THROUGH,
            "--require-tag",
            "node-canary",
            "--evidence",
            str(evidence_path),
            "--json",
        ],
        capture_output=True,
        text=True,
    )

    assert process.returncode == 1
    assert json.loads(process.stdout)["passed"] is False
    assert path.read_bytes() == before
