from __future__ import annotations

import sqlite3
import uuid
from typing import Any

import pytest

from app.execution_contract import canonical_json, utf8_sha256
from app.product_run_store import (
    _approval_scope_project_id,
    _project_state,
    _scope_from_cursor,
)


def test_pinned_execution_plan_approval_provides_fail_closed_project_scope():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_sha256 TEXT,
            payload_contract_version TEXT,
            payload_immutable_at TEXT
        )
        """
    )
    plan_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    plan_digest = "a" * 64
    raw_payload = canonical_json(
        {
            "contract_version": "execution-plan-v2-approval-v1",
            "execution_plan_id": plan_id,
            "plan_digest": plan_digest,
            "project_id": project_id,
        }
    )
    connection.execute(
        """
        INSERT INTO approvals (
            id, kind, payload, payload_sha256,
            payload_contract_version, payload_immutable_at
        ) VALUES (1, 'execution_plan_v2', ?, ?,
                  'execution-plan-v2-approval-v1', '2026-08-11T00:00:00Z')
        """,
        (raw_payload, utf8_sha256(raw_payload)),
    )
    plan: Any = {
        "id": plan_id,
        "plan_digest": plan_digest,
        "request_approval_id": 1,
    }
    cursor = connection.cursor()
    assert _approval_scope_project_id(cursor, plan) == project_id

    connection.execute(
        "UPDATE approvals SET payload_sha256 = ? WHERE id = 1",
        ("0" * 64,),
    )
    assert _approval_scope_project_id(cursor, plan) is None
    connection.close()


def test_product_scope_prefers_pinned_approval_then_companion_then_legacy_name():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_sha256 TEXT,
            payload_contract_version TEXT,
            payload_immutable_at TEXT
        );
        CREATE TABLE execution_plans (
            id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            request_approval_id INTEGER
        );
        CREATE TABLE execution_plan_v2_specs (
            execution_plan_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL
        );
        """
    )
    canonical_project_id = str(uuid.uuid4())
    companion_project_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    plan_digest = "b" * 64
    connection.executemany(
        "INSERT INTO projects (id, name) VALUES (?, ?)",
        (
            (canonical_project_id, "canonical"),
            (companion_project_id, "companion"),
        ),
    )
    raw_payload = canonical_json(
        {
            "contract_version": "execution-plan-v2-approval-v1",
            "execution_plan_id": plan_id,
            "plan_digest": plan_digest,
            "project_id": canonical_project_id,
        }
    )
    connection.execute(
        """
        INSERT INTO approvals VALUES (
            1, 'execution_plan_v2', ?, ?,
            'execution-plan-v2-approval-v1', '2026-08-11T00:00:00Z'
        )
        """,
        (raw_payload, utf8_sha256(raw_payload)),
    )
    connection.execute(
        "INSERT INTO execution_plans VALUES (?, 'mutable-name', 'execution-plan-v2', ?, 1)",
        (plan_id, plan_digest),
    )
    connection.execute(
        "INSERT INTO execution_plan_v2_specs VALUES (?, ?)",
        (plan_id, companion_project_id),
    )
    cursor = connection.cursor()
    assert _scope_from_cursor(cursor, plan_id)["project_id"] == canonical_project_id

    connection.execute(
        "DELETE FROM execution_plan_v2_specs WHERE execution_plan_id = ?",
        (plan_id,),
    )
    assert _scope_from_cursor(cursor, plan_id)["project_id"] == canonical_project_id

    connection.execute(
        "UPDATE approvals SET payload_sha256 = ? WHERE id = 1",
        ("0" * 64,),
    )
    connection.execute(
        "INSERT INTO execution_plan_v2_specs VALUES (?, ?)",
        (plan_id, companion_project_id),
    )
    assert _scope_from_cursor(cursor, plan_id)["project_id"] == companion_project_id

    legacy_plan_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO execution_plans VALUES (?, 'canonical', 'execution-plan-v1', ?, NULL)",
        (legacy_plan_id, "c" * 64),
    )
    assert _scope_from_cursor(cursor, legacy_plan_id)["project_id"] == canonical_project_id
    connection.close()


@pytest.mark.parametrize(
    ("case", "expected_state", "expected_reason"),
    [
        ("awaiting", "awaiting_approval", None),
        ("rejected", "rejected", None),
        ("queued", "queued", None),
        ("blocked", "blocked", None),
        ("cancelled", "cancelled", None),
        ("preparing", "preparing", None),
        ("running", "running", None),
        ("succeeded", "succeeded", "result_collection_pending"),
        ("failed", "failed", "result_collection_pending"),
        ("terminal_mismatch", "needs_attention", "terminal_evidence_mismatch"),
        ("collection_failed", "needs_attention", "result_collection_failed"),
        ("unknown_liveness", "needs_attention", "attempt_liveness_unknown"),
        ("recovery_hold", "needs_attention", "attempt_recovery_hold"),
        ("stalled", "needs_attention", "job_stalled_suspect"),
    ],
)
def test_product_run_closed_state_projection_matrix(
    db,
    case: str,
    expected_state: str,
    expected_reason: str | None,
):
    approval = {"status": "approved"}
    job: dict[str, Any] | None = {
        "id": 1,
        "status": "running",
        "exit_code": None,
        "finished_at": None,
        "stalled_suspect": 0,
    }
    attempts: list[dict[str, Any]] = [
        {
            "id": "attempt-1",
            "job_id": 1,
            "state": "running",
            "liveness": "known",
            "terminal_at": None,
            "exit_code": None,
            "recovery_hold_reason": None,
        }
    ]
    operations: list[dict[str, Any]] = []
    if case == "awaiting":
        approval = {"status": "pending"}
        job = None
        attempts = []
    elif case == "rejected":
        approval = {"status": "rejected"}
        job = None
        attempts = []
    elif case in {"queued", "blocked", "cancelled"}:
        assert job is not None
        job["status"] = case
        attempts = []
    elif case == "preparing":
        attempts[0]["state"] = "dispatching"
    elif case in {"succeeded", "failed", "terminal_mismatch", "collection_failed"}:
        assert job is not None
        failed = case == "failed"
        timestamp = "2026-08-10T00:00:00.000000Z"
        job.update(
            status="failed" if failed else "done",
            exit_code=7 if failed else 0,
            finished_at=timestamp,
        )
        attempts[0].update(
            state="failed" if failed else "done",
            terminal_at=(
                "2026-08-10T00:00:01.000000Z" if case == "terminal_mismatch" else timestamp
            ),
            exit_code=7 if failed else 0,
        )
        if case == "collection_failed":
            operations = [
                {
                    "attempt_id": "attempt-1",
                    "operation": "collect",
                    "state": "failed",
                }
            ]
    elif case == "unknown_liveness":
        attempts[0]["liveness"] = "unknown"
    elif case == "recovery_hold":
        attempts[0]["recovery_hold_reason"] = "manual_review"
    elif case == "stalled":
        assert job is not None
        job["stalled_suspect"] = 1

    with db.cursor() as cursor:
        state, reasons, _current, _collection = _project_state(
            cursor,
            contract_kind="legacy_execution_plan",
            approval=approval,
            job=job,
            attempts=attempts,
            operations=operations,
            completion_operations=[],
            stop_approvals=[],
        )
    assert state == expected_state
    if expected_reason is not None:
        assert expected_reason in reasons
