"""Canonical, non-persisted Product Run projections and stop transactions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any, Literal

from app.execution_contract import canonical_json, canonical_json_sha256, utf8_sha256
from app.execution_launch import build_attempt_stop_command
from app.execution_plan_v2 import (
    EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION,
    EXECUTION_PLAN_V2_APPROVAL_KIND,
    EXECUTION_PLAN_V2_CONTRACT_VERSION,
    ExecutionPlanV2Spec,
    execution_plan_v2_approval_payload_digest,
    parse_execution_plan_v2_approval_payload,
)
from app.execution_plan_v2_store import verify_execution_plan_v2_in_cursor
from app.node_protocol import (
    validate_artifact_digest,
    validate_artifact_kind,
    validate_artifact_path,
    validate_artifact_size,
)
from app.project_bootstrap import OutputDeclaration
from app.product_runs import ProductRunState

if TYPE_CHECKING:
    from app.db import Database


PRODUCT_STOP_SOURCE = "product_v2"
PRODUCT_STOP_CONTRACT_VERSION = "stop-intent-v1"
MAX_PRODUCT_TIMELINE_ITEMS = 500
MAX_PRODUCT_ARTIFACT_SCAN = 1_001

_ACTIVE_ATTEMPT_STATES = frozenset({"leased", "dispatching", "running"})
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})
_VALID_JOB_STATUSES = frozenset({"queued", "running", "done", "failed", "blocked", "cancelled"})


def _canonical_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, ValueError):
        return None
    return normalized if normalized == value else None


def _plan_row(cursor: sqlite3.Cursor, plan_id: str) -> sqlite3.Row | None:
    return cursor.execute(
        "SELECT * FROM execution_plans WHERE id = ?",
        (plan_id,),
    ).fetchone()


def _approval_scope_project_id(
    cursor: sqlite3.Cursor,
    plan: sqlite3.Row,
) -> str | None:
    approval_id = plan["request_approval_id"]
    if approval_id is None:
        return None
    approval = cursor.execute(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if (
        approval is None
        or approval["kind"] != EXECUTION_PLAN_V2_APPROVAL_KIND
        or approval["payload_contract_version"] != EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION
        or approval["payload_immutable_at"] is None
        or not isinstance(approval["payload_sha256"], str)
        or not isinstance(approval["payload"], str)
        or utf8_sha256(approval["payload"]) != approval["payload_sha256"]
    ):
        return None
    try:
        payload = parse_execution_plan_v2_approval_payload(json.loads(approval["payload"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        canonical_json(payload.model_dump(mode="json")) != approval["payload"]
        or execution_plan_v2_approval_payload_digest(payload) != approval["payload_sha256"]
        or payload.execution_plan_id != plan["id"]
        or payload.plan_digest != plan["plan_digest"]
    ):
        return None
    return payload.project_id


def _scope_from_cursor(
    cursor: sqlite3.Cursor,
    plan_id: str,
) -> dict[str, Any] | None:
    plan = cursor.execute(
        """
        SELECT plan.*, companion.project_id AS companion_project_id,
               companion.execution_plan_id AS companion_marker
        FROM execution_plans AS plan
        LEFT JOIN execution_plan_v2_specs AS companion
          ON companion.execution_plan_id = plan.id
        WHERE plan.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if plan is None:
        return None
    marked_v2 = (
        plan["contract_version"] == EXECUTION_PLAN_V2_CONTRACT_VERSION
        or plan["companion_marker"] is not None
    )
    if marked_v2:
        project_id = (
            _approval_scope_project_id(
                cursor,
                plan,
            )
            or plan["companion_project_id"]
        )
        projects = cursor.execute(
            "SELECT id, name FROM projects WHERE id = ?",
            (project_id,),
        ).fetchall()
    else:
        projects = cursor.execute(
            "SELECT id, name FROM projects WHERE name = ? ORDER BY id LIMIT 2",
            (plan["project_name"],),
        ).fetchall()
    if len(projects) != 1 or _canonical_uuid(projects[0]["id"]) is None:
        return None
    project = projects[0]
    project_id = str(project["id"])
    return {
        "plan_id": str(plan["id"]),
        "project_id": project_id,
        "project_name": str(project["name"]),
        "marked_v2": marked_v2,
    }


def get_product_run_scope(database: "Database", plan_id: str) -> dict[str, Any] | None:
    """Resolve only the minimum scope needed before route-local authorization."""

    with database.cursor() as cursor:
        return _scope_from_cursor(cursor, plan_id)


def _verified_product_stop_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    if (
        row["kind"] != "stop"
        or row["payload_contract_version"] != PRODUCT_STOP_CONTRACT_VERSION
        or row["payload_immutable_at"] is None
        or not isinstance(row["payload_sha256"], str)
    ):
        return None
    raw_payload = row["payload"]
    if not isinstance(raw_payload, str) or utf8_sha256(raw_payload) != row["payload_sha256"]:
        return None
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"attempt_id", "job_id", "source"}
        or canonical_json(payload) != raw_payload
        or isinstance(payload.get("job_id"), bool)
        or not isinstance(payload.get("job_id"), int)
        or payload["job_id"] < 1
        or not isinstance(payload.get("attempt_id"), str)
        or not payload["attempt_id"]
        or payload.get("source") != PRODUCT_STOP_SOURCE
    ):
        return None
    return payload


def _stop_approvals_for_attempt(
    cursor: sqlite3.Cursor,
    *,
    job_id: int,
    attempt_id: str,
) -> list[dict[str, Any]]:
    rows = cursor.execute(
        """
        SELECT * FROM approvals
        WHERE kind = 'stop'
          AND CASE WHEN json_valid(payload)
                   THEN json_extract(payload, '$.job_id') END = ?
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    verified = []
    for row in rows:
        payload = _verified_product_stop_payload(row)
        if payload is None or payload["attempt_id"] != attempt_id:
            continue
        verified.append({"row": row, "payload": payload})
    return verified


def _stop_operation_for_approval(
    cursor: sqlite3.Cursor,
    *,
    approval: sqlite3.Row,
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    operations = cursor.execute(
        """
        SELECT * FROM execution_operations
        WHERE authorization_approval_id = ? AND operation = 'stop'
        ORDER BY created_at, id
        """,
        (approval["id"],),
    ).fetchall()
    if not operations:
        return None, False
    if len(operations) != 1:
        return None, True
    operation = operations[0]
    raw_payload = operation["payload_json"]
    try:
        operation_payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError):
        return None, True
    expected_payload = {
        "attempt_id": payload["attempt_id"],
        "command": build_attempt_stop_command(
            int(payload["job_id"]),
            str(payload["attempt_id"]),
        ),
        "job_id": int(payload["job_id"]),
    }
    invalid = (
        operation["attempt_id"] != payload["attempt_id"]
        or operation["authorization_class"] != "stop"
        or operation["authorized_contract_sha256"] != approval["payload_sha256"]
        or not isinstance(raw_payload, str)
        or canonical_json(expected_payload) != raw_payload
        or utf8_sha256(raw_payload) != operation["payload_sha256"]
        or operation_payload != expected_payload
    )
    return (None, True) if invalid else (dict(operation), False)


def _load_attempt_graph(
    cursor: sqlite3.Cursor,
    *,
    job_id: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if job_id is None:
        return [], [], []
    attempts: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    completion_operations: list[dict[str, Any]] = []
    for row in cursor.execute(
        """
        SELECT * FROM execution_attempts
        WHERE job_id = ?
        ORDER BY attempt_number, created_at, id
        """,
        (job_id,),
    ).fetchall():
        attempt = dict(row)
        attempts.append(attempt)
        operations.extend(
            dict(item)
            for item in cursor.execute(
                """
                SELECT id, attempt_id, operation, authorization_approval_id,
                       authorization_class, authorized_contract_sha256,
                       payload_sha256, state, created_at, updated_at,
                       transmission_state
                FROM execution_operations
                WHERE attempt_id = ?
                ORDER BY created_at, id
                """,
                (attempt["id"],),
            ).fetchall()
        )
        completion_operations.extend(
            dict(item)
            for item in cursor.execute(
                """
                SELECT id, attempt_id, job_id, operation, payload_sha256,
                       state, output_sha256, created_at, updated_at
                FROM execution_completion_operations
                WHERE attempt_id = ?
                ORDER BY created_at, id
                """,
                (attempt["id"],),
            ).fetchall()
        )
    return attempts, operations, completion_operations


def _collection_projection(
    *,
    operations: list[dict[str, Any]],
    completion_operations: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    states = [str(item["state"]) for item in operations if item["operation"] == "collect"] + [
        str(item["state"])
        for item in completion_operations
        if item["operation"] == "result_collection"
    ]
    if not states or any(state in {"pending", "processing"} for state in states):
        return "pending", ["result_collection_pending"]
    if any(state in {"failed", "uncertain"} for state in states):
        return "failed", ["result_collection_failed"]
    if all(state == "delivered" for state in states):
        return "delivered", []
    return "unknown", ["result_collection_unknown"]


def _matching_terminal_attempt(
    *,
    job: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not attempts:
        return None
    latest = attempts[-1]
    expected_attempt_state = "done" if job["status"] == "done" else "failed"
    expected_exit = 0 if job["status"] == "done" else job["exit_code"]
    if (
        latest["state"] != expected_attempt_state
        or latest["liveness"] != "known"
        or latest["terminal_at"] is None
        or latest["exit_code"] != expected_exit
        or job["finished_at"] is None
        or latest["terminal_at"] != job["finished_at"]
        or job["exit_code"] != latest["exit_code"]
    ):
        return None
    return latest


def _project_state(
    cursor: sqlite3.Cursor,
    *,
    contract_kind: str,
    approval: dict[str, Any] | None,
    job: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    completion_operations: list[dict[str, Any]],
    stop_approvals: list[dict[str, Any]],
) -> tuple[ProductRunState, list[str], dict[str, Any] | None, str]:
    attention: list[str] = []
    if approval is None:
        return "needs_attention", ["execution_approval_missing"], None, "unknown"
    approval_status = approval.get("status")
    if approval_status == "pending":
        return "awaiting_approval", [], None, "not_started"
    if approval_status == "rejected":
        return "rejected", [], None, "not_started"
    if approval_status != "approved" or job is None:
        return "needs_attention", ["approved_job_missing"], None, "unknown"
    job_status = job.get("status")
    if job_status not in _VALID_JOB_STATUSES:
        return "needs_attention", ["canonical_job_status_invalid"], None, "unknown"

    active = [item for item in attempts if item["state"] in _ACTIVE_ATTEMPT_STATES]
    current = active[-1] if len(active) == 1 else (attempts[-1] if attempts else None)
    if len(active) > 1:
        attention.append("multiple_active_attempts")
    if contract_kind == "execution_plan_v2" and current is not None:
        if (
            current["job_id"] != job["id"]
            or current["execution_approval_id"] != job["execution_approval_id"]
            or current["approved_payload_sha256"] != job["approved_payload_sha256"]
            or current["execution_contract_version"] != EXECUTION_PLAN_V2_CONTRACT_VERSION
        ):
            attention.append("attempt_contract_mismatch")

    collection_state, collection_attention = _collection_projection(
        operations=operations,
        completion_operations=completion_operations,
    )

    if job_status == "cancelled":
        return "cancelled", sorted(set(attention)), current, collection_state
    if job_status == "blocked":
        return "blocked", sorted(set(attention)), current, collection_state
    if job_status == "queued":
        if active:
            attention.append("queued_job_has_active_attempt")
            return "needs_attention", sorted(set(attention)), current, collection_state
        return "queued", sorted(set(attention)), current, collection_state
    if job_status in {"done", "failed"}:
        terminal = _matching_terminal_attempt(job=job, attempts=attempts)
        if terminal is None:
            attention.append("terminal_evidence_mismatch")
            return "needs_attention", sorted(set(attention)), current, collection_state
        attention.extend(collection_attention)
        if job_status == "failed":
            return "failed", sorted(set(attention)), terminal, collection_state
        if collection_state in {"failed", "unknown"}:
            return "needs_attention", sorted(set(attention)), terminal, collection_state
        return "succeeded", sorted(set(attention)), terminal, collection_state

    if job_status != "running" or current is None or len(active) != 1:
        attention.append(
            "legacy_attempt_evidence_unavailable"
            if contract_kind == "legacy_execution_plan"
            else "active_attempt_missing"
        )
        return "needs_attention", sorted(set(attention)), current, collection_state

    unresolved_stop = False
    for item in stop_approvals:
        stop_row = item["row"]
        if stop_row["status"] == "pending":
            unresolved_stop = True
        elif stop_row["status"] == "approved":
            operation, invalid = _stop_operation_for_approval(
                cursor,
                approval=stop_row,
                payload=item["payload"],
            )
            if invalid:
                attention.append("stop_materialization_conflict")
            elif operation is not None:
                unresolved_stop = True
                if operation["state"] in {"uncertain", "failed"}:
                    attention.append("stop_delivery_uncertain")
    if current.get("liveness") == "unknown":
        attention.append("attempt_liveness_unknown")
    if current.get("recovery_hold_reason") is not None:
        attention.append("attempt_recovery_hold")
    if int(job.get("stalled_suspect") or 0) != 0:
        attention.append("job_stalled_suspect")
    if any(
        item["operation"] != "stop" and item["state"] == "uncertain"
        for item in operations
        if item["attempt_id"] == current["id"]
    ):
        attention.append("execution_operation_uncertain")

    if unresolved_stop:
        return "stopping", sorted(set(attention)), current, collection_state
    if attention:
        return "needs_attention", sorted(set(attention)), current, collection_state
    if current["state"] in {"leased", "dispatching"}:
        return "preparing", [], current, collection_state
    if current["state"] == "running" and current["liveness"] == "known":
        return "running", [], current, collection_state
    return "needs_attention", ["attempt_state_unresolved"], current, collection_state


def _safe_timeline(
    cursor: sqlite3.Cursor,
    *,
    plan: dict[str, Any],
    approval: dict[str, Any] | None,
    job: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    completion_operations: list[dict[str, Any]],
    stop_approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {
            "kind": "plan_created",
            "id": str(plan["id"]),
            "timestamp": plan["created_at"],
        }
    ]
    if approval is not None:
        items.append(
            {
                "kind": "approval_requested",
                "id": str(approval["id"]),
                "timestamp": approval["created_at"],
                "state": "pending",
                "payload_digest": approval.get("payload_sha256"),
            }
        )
        if approval.get("decided_at") is not None:
            items.append(
                {
                    "kind": "approval_decided",
                    "id": str(approval["id"]),
                    "timestamp": approval["decided_at"],
                    "state": approval.get("status"),
                }
            )
    if job is not None:
        items.append(
            {
                "kind": "job_created",
                "id": str(job["id"]),
                "timestamp": job["created_at"],
                "state": "queued",
            }
        )
        if job.get("started_at") is not None:
            items.append(
                {
                    "kind": "job_started",
                    "id": str(job["id"]),
                    "timestamp": job["started_at"],
                    "state": "running",
                }
            )
        if job.get("finished_at") is not None:
            items.append(
                {
                    "kind": "job_terminal",
                    "id": str(job["id"]),
                    "timestamp": job["finished_at"],
                    "state": job.get("status"),
                }
            )
    for attempt in attempts:
        for event in cursor.execute(
            """
            SELECT id, attempt_id, operation_id, event_type, from_state,
                   to_state, from_liveness, to_liveness, reason_code,
                   event_sha256, created_at
            FROM execution_attempt_events
            WHERE attempt_id = ?
            ORDER BY id
            """,
            (attempt["id"],),
        ).fetchall():
            items.append(
                {
                    "kind": "attempt_event",
                    "id": str(event["id"]),
                    "attempt_id": str(event["attempt_id"]),
                    "operation_id": event["operation_id"],
                    "timestamp": event["created_at"],
                    "event_type": event["event_type"],
                    "from_state": event["from_state"],
                    "to_state": event["to_state"],
                    "from_liveness": event["from_liveness"],
                    "to_liveness": event["to_liveness"],
                    "reason_code": event["reason_code"],
                    "event_digest": event["event_sha256"],
                }
            )
    for operation in operations:
        items.append(
            {
                "kind": "execution_operation",
                "id": str(operation["id"]),
                "attempt_id": str(operation["attempt_id"]),
                "timestamp": operation["created_at"],
                "operation": operation["operation"],
                "state": operation["state"],
                "payload_digest": operation["payload_sha256"],
            }
        )
    for operation in completion_operations:
        items.append(
            {
                "kind": "completion_operation",
                "id": str(operation["id"]),
                "attempt_id": str(operation["attempt_id"]),
                "timestamp": operation["created_at"],
                "operation": operation["operation"],
                "state": operation["state"],
                "payload_digest": operation["payload_sha256"],
                "output_digest": operation["output_sha256"],
            }
        )
    for item in stop_approvals:
        stop = item["row"]
        items.append(
            {
                "kind": "stop_approval_requested",
                "id": str(stop["id"]),
                "attempt_id": item["payload"]["attempt_id"],
                "timestamp": stop["created_at"],
                "state": "pending",
                "payload_digest": stop["payload_sha256"],
            }
        )
        if stop["decided_at"] is not None:
            items.append(
                {
                    "kind": "stop_approval_decided",
                    "id": str(stop["id"]),
                    "attempt_id": item["payload"]["attempt_id"],
                    "timestamp": stop["decided_at"],
                    "state": stop["status"],
                }
            )
    items.sort(
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("kind") or ""),
            str(item.get("id") or ""),
        )
    )
    truncated = len(items) > MAX_PRODUCT_TIMELINE_ITEMS
    return {
        "items": items[-MAX_PRODUCT_TIMELINE_ITEMS:],
        "truncated": truncated,
    }


def _typed_dimension_summary(spec: ExecutionPlanV2Spec) -> dict[str, Any]:
    return {
        "project_version": {
            "id": spec.project_version.project_version_id,
            "git_commit": spec.project_version.git_commit,
            "bundle_sha256": spec.project_version.bundle_sha256,
        },
        "template": {
            "run_profile_id": spec.run_profile.run_profile_id,
            "spec_digest": spec.run_profile.spec_digest,
        },
        "environment": {
            "revision_id": spec.environment.environment_revision_id,
            "revision_digest": spec.environment.revision_digest,
        },
        "parameters": {
            "keys": sorted(spec.parameter_values),
            "digest": canonical_json_sha256(spec.parameter_values),
        },
        "datasets": [
            {
                "name": item.name,
                "asset_id": item.asset_id,
                "snapshot_id": item.snapshot_id,
            }
            for item in spec.dataset_bindings
        ],
        "resources": spec.resource_requirements.model_dump(mode="json"),
        "target": {
            "backend": spec.backend,
            "server_config_revision_id": spec.target.server_config_revision_id,
            "target_identity_sha256": spec.target.target_identity_sha256,
        },
        "output_declarations_digest": spec.output_declarations_digest,
    }


def _load_product_run_in_cursor(
    database: "Database",
    cursor: sqlite3.Cursor,
    plan_id: str,
) -> dict[str, Any] | None:
    scope = _scope_from_cursor(cursor, plan_id)
    if scope is None:
        return None
    plan_row = _plan_row(cursor, plan_id)
    if plan_row is None:  # pragma: no cover - same read transaction
        return None
    plan = dict(plan_row)
    spec: ExecutionPlanV2Spec | None = None
    approval: dict[str, Any] | None
    contract_kind = "legacy_execution_plan"
    if scope["marked_v2"]:
        verified = verify_execution_plan_v2_in_cursor(database, cursor, plan_id)
        if verified is None:
            raise ValueError("product_run_contract_invalid")
        plan = dict(verified["execution_plan"])
        approval = dict(verified["approval"])
        spec = verified["spec"]
        contract_kind = "execution_plan_v2"
    else:
        approval_row = (
            cursor.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (plan["request_approval_id"],),
            ).fetchone()
            if plan["request_approval_id"] is not None
            else None
        )
        approval = dict(approval_row) if approval_row is not None else None
    job_row = (
        cursor.execute("SELECT * FROM jobs WHERE id = ?", (plan["job_id"],)).fetchone()
        if plan["job_id"] is not None
        else None
    )
    job = dict(job_row) if job_row is not None else None
    job_id = int(job["id"]) if job is not None else None
    attempts, operations, completion_operations = _load_attempt_graph(
        cursor,
        job_id=job_id,
    )
    current_attempt_id = str(attempts[-1]["id"]) if attempts else ""
    stop_approvals = (
        _stop_approvals_for_attempt(
            cursor,
            job_id=job_id,
            attempt_id=current_attempt_id,
        )
        if job_id is not None and current_attempt_id
        else []
    )
    state, attention, current_attempt, collection_state = _project_state(
        cursor,
        contract_kind=contract_kind,
        approval=approval,
        job=job,
        attempts=attempts,
        operations=operations,
        completion_operations=completion_operations,
        stop_approvals=stop_approvals,
    )
    terminal = (
        {
            "canonical_job_status": job["status"],
            "exit_code": job["exit_code"],
            "finished_at": job["finished_at"],
            "collection_state": collection_state,
        }
        if job is not None and job["status"] in _TERMINAL_JOB_STATUSES
        else None
    )
    detail = {
        "plan_id": plan_id,
        "project_id": scope["project_id"],
        "project_name": scope["project_name"],
        "created_at": plan["created_at"],
        "contract": {
            "kind": contract_kind,
            "version": plan.get("contract_version"),
            "verified": contract_kind == "execution_plan_v2",
            "plan_digest": plan.get("plan_digest"),
        },
        "state": state,
        "canonical_job_status": job.get("status") if job is not None else None,
        "current_attempt": (
            {
                "id": current_attempt["id"],
                "state": current_attempt["state"],
                "liveness": current_attempt["liveness"],
            }
            if current_attempt is not None
            else None
        ),
        "attention_reasons": attention,
        "approval": (
            {
                "id": int(approval["id"]),
                "kind": str(approval["kind"]),
                "status": str(approval["status"]),
                "created_at": approval["created_at"],
                "decided_at": approval["decided_at"],
                "payload_digest": approval.get("payload_sha256"),
            }
            if approval is not None
            else None
        ),
        "job": (
            {
                "id": int(job["id"]),
                "status": str(job["status"]),
                "created_at": job["created_at"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "exit_code": job["exit_code"],
                "stalled_suspect": bool(job["stalled_suspect"]),
            }
            if job is not None
            else None
        ),
        "dimensions": (
            _typed_dimension_summary(spec)
            if spec is not None
            else {
                name: {"availability": "unknown"}
                for name in (
                    "project_version",
                    "template",
                    "environment",
                    "parameters",
                    "datasets",
                    "resources",
                    "target",
                )
            }
        ),
        "terminal_result": terminal,
        "timeline": _safe_timeline(
            cursor,
            plan=plan,
            approval=approval,
            job=job,
            attempts=attempts,
            operations=operations,
            completion_operations=completion_operations,
            stop_approvals=stop_approvals,
        ),
        "lineage": {
            "quality": "verified" if spec is not None else "legacy",
            "attempt_count": len(attempts),
            "job_materialized": job is not None,
        },
        "_spec": spec,
        "_terminal_comparison": terminal,
    }
    return detail


def get_product_run_detail(database: "Database", plan_id: str) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        detail = _load_product_run_in_cursor(database, cursor, plan_id)
    if detail is not None:
        detail.pop("_spec", None)
        detail.pop("_terminal_comparison", None)
    return detail


def get_product_run_comparison_evidence(
    database: "Database",
    plan_id: str,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        return _load_product_run_in_cursor(database, cursor, plan_id)


def _declared_outputs(
    cursor: sqlite3.Cursor,
    spec: ExecutionPlanV2Spec | None,
) -> dict[str, Any]:
    if spec is None:
        return {"availability": "unknown", "items": []}
    row = cursor.execute(
        """
        SELECT output_declarations_json, spec_digest
        FROM run_profile_specs
        WHERE run_profile_id = ?
        """,
        (spec.run_profile.run_profile_id,),
    ).fetchone()
    if row is None or row["spec_digest"] != spec.run_profile.spec_digest:
        return {"availability": "unknown", "items": []}
    raw = row["output_declarations_json"]
    if not isinstance(raw, str):
        return {"availability": "unknown", "items": []}
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("invalid output declaration collection")
        declarations = [OutputDeclaration.model_validate(value) for value in values]
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"availability": "unknown", "items": []}
    normalized = [item.model_dump(mode="json") for item in declarations]
    if (
        canonical_json(normalized) != raw
        or canonical_json_sha256(normalized) != spec.output_declarations_digest
    ):
        return {"availability": "unknown", "items": []}
    return {
        "availability": "known",
        "items": normalized,
    }


def _artifact_rows(
    cursor: sqlite3.Cursor,
    *,
    job_id: int | None,
    execution_attempt_ids: list[str],
    verified_v2: bool,
    after_id: int,
) -> list[sqlite3.Row]:
    if job_id is None:
        return []
    if verified_v2:
        if not execution_attempt_ids:
            return []
        placeholders = ",".join("?" for _ in execution_attempt_ids)
        return cursor.execute(
            f"""
            SELECT artifact.*
            FROM node_attempt_artifacts AS artifact
            JOIN node_attempts AS node ON node.id = artifact.attempt_id
            WHERE artifact.id > ? AND node.job_id = ?
              AND node.execution_attempt_id IN ({placeholders})
            ORDER BY artifact.id ASC
            LIMIT ?
            """,
            (
                after_id,
                job_id,
                *execution_attempt_ids,
                MAX_PRODUCT_ARTIFACT_SCAN,
            ),
        ).fetchall()
    return cursor.execute(
        """
        SELECT artifact.*
        FROM node_attempt_artifacts AS artifact
        JOIN node_attempts AS node ON node.id = artifact.attempt_id
        WHERE artifact.id > ? AND node.job_id = ?
        ORDER BY artifact.id ASC
        LIMIT ?
        """,
        (after_id, job_id, MAX_PRODUCT_ARTIFACT_SCAN),
    ).fetchall()


def _validated_artifact(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        relative_path = validate_artifact_path(row["relative_path"])
        kind = validate_artifact_kind(row["kind"])
        size_bytes = validate_artifact_size(row["size_bytes"])
        digest = validate_artifact_digest(row["sha256"])
    except (KeyError, TypeError, ValueError):
        return None
    reported_at = row["reported_at"]
    if (
        not isinstance(reported_at, str)
        or not 1 <= len(reported_at) <= 64
        or any(ord(character) < 0x20 for character in reported_at)
    ):
        return None
    return {
        "relative_path": relative_path,
        "kind": kind,
        "size_bytes": size_bytes,
        "sha256": digest,
        "reported_at": reported_at,
        "metadata_only": True,
    }


def _product_run_artifacts_in_cursor(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    plan_id: str,
    after_id: int,
    limit: int,
) -> dict[str, Any] | None:
    if after_id < 0 or not 1 <= limit <= 1_000:
        raise ValueError("product_artifact_page_invalid")
    detail = _load_product_run_in_cursor(database, cursor, plan_id)
    if detail is None:
        return None
    job = detail["job"]
    job_id = int(job["id"]) if job is not None else None
    attempts = _load_attempt_graph(cursor, job_id=job_id)[0]
    verified_v2 = detail["contract"]["kind"] == "execution_plan_v2"
    rows = _artifact_rows(
        cursor,
        job_id=job_id,
        execution_attempt_ids=[str(item["id"]) for item in attempts],
        verified_v2=verified_v2,
        after_id=after_id,
    )
    valid: list[tuple[int, dict[str, Any]]] = []
    invalid = False
    for row in rows:
        item = _validated_artifact(row)
        if item is None:
            invalid = True
            continue
        valid.append((int(row["id"]), item))
        if len(valid) > limit:
            break
    visible = valid[:limit]
    more_valid = len(valid) > limit
    scan_truncated = len(rows) == MAX_PRODUCT_ARTIFACT_SCAN
    has_more = more_valid or scan_truncated
    next_after_id = None
    if more_valid and visible:
        next_after_id = visible[-1][0]
    elif scan_truncated and rows:
        next_after_id = int(rows[-1]["id"])
    return {
        "plan_id": plan_id,
        "project_id": detail["project_id"],
        "availability": "known" if valid and not invalid else "unknown",
        "metadata_only": True,
        "complete": False,
        "items": [item for _, item in visible],
        "declared_outputs": _declared_outputs(cursor, detail["_spec"]),
        "truncated": has_more or invalid,
        "scope_truncated": invalid,
        "next_after_id": next_after_id,
    }


def get_product_run_artifacts(
    database: "Database",
    *,
    plan_id: str,
    after_id: int = 0,
    limit: int = 50,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        return _product_run_artifacts_in_cursor(
            database,
            cursor,
            plan_id=plan_id,
            after_id=after_id,
            limit=limit,
        )


def get_product_run_artifact_comparison_summary(
    database: "Database",
    plan_id: str,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        result = _product_run_artifacts_in_cursor(
            database,
            cursor,
            plan_id=plan_id,
            after_id=0,
            limit=1_000,
        )
    if result is None or result["availability"] != "known" or result["truncated"]:
        return None
    items = result["items"]
    return {
        "count": len(items),
        "metadata_digest": canonical_json_sha256(items),
        "metadata_only": True,
    }


def _product_stop_plan_row_for_job(
    cursor: sqlite3.Cursor,
    job_id: int,
) -> sqlite3.Row | None:
    rows = cursor.execute(
        """
        SELECT plan.*
        FROM execution_plans AS plan
        WHERE plan.job_id = ?
        ORDER BY plan.id
        LIMIT 2
        """,
        (job_id,),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def get_product_stop_approval_scope(
    database: "Database",
    approval_id: int,
) -> dict[str, Any] | None:
    """Resolve minimal Product stop scope before authorization.

    This intentionally does not claim that the contract verifies.  The route
    authorizes through the referenced plan first, then the mutation verifier
    can return an opaque 409 to an authorized caller without exposing the
    target project to anyone else.
    """

    with database.cursor() as cursor:
        row = cursor.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        if row is None or row["kind"] != "stop":
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("source") != PRODUCT_STOP_SOURCE
            or isinstance(payload.get("job_id"), bool)
            or not isinstance(payload.get("job_id"), int)
        ):
            return None
        plan = _product_stop_plan_row_for_job(cursor, int(payload["job_id"]))
        if plan is None:
            return None
        return _scope_from_cursor(cursor, str(plan["id"]))


def verify_product_stop_approval_in_cursor(
    database: "Database",
    cursor: sqlite3.Cursor,
    approval_id: int,
) -> dict[str, Any] | None:
    row = cursor.execute(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        return None
    payload = _verified_product_stop_payload(row)
    if payload is None:
        return None
    plan = _product_stop_plan_row_for_job(cursor, int(payload["job_id"]))
    if plan is None:
        return None
    scope = _scope_from_cursor(cursor, str(plan["id"]))
    if scope is None or not scope["marked_v2"]:
        return None
    verified_plan = verify_execution_plan_v2_in_cursor(
        database,
        cursor,
        str(plan["id"]),
    )
    if verified_plan is None or verified_plan["execution_plan"]["job_id"] != payload["job_id"]:
        return None
    attempt = cursor.execute(
        "SELECT * FROM execution_attempts WHERE id = ? AND job_id = ?",
        (payload["attempt_id"], payload["job_id"]),
    ).fetchone()
    if attempt is None:
        return None
    return {
        "approval": row,
        "payload": payload,
        "execution_plan": verified_plan,
        "plan_id": str(plan["id"]),
        "project_id": scope["project_id"],
        "attempt": attempt,
    }


def _active_attempts_for_job(
    cursor: sqlite3.Cursor,
    job_id: int,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in _ACTIVE_ATTEMPT_STATES)
    return cursor.execute(
        f"""
        SELECT * FROM execution_attempts
        WHERE job_id = ? AND state IN ({placeholders})
        ORDER BY attempt_number, created_at, id
        """,
        (job_id, *sorted(_ACTIVE_ATTEMPT_STATES)),
    ).fetchall()


def _product_stop_candidates_for_job(
    cursor: sqlite3.Cursor,
    job_id: int,
) -> list[sqlite3.Row]:
    return cursor.execute(
        """
        SELECT * FROM approvals
        WHERE kind = 'stop'
          AND CASE WHEN json_valid(payload)
                   THEN json_extract(payload, '$.job_id') END = ?
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()


def create_product_stop_request_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    plan_id: str,
    requester_actor_id: str,
) -> dict[str, Any]:
    verified = verify_execution_plan_v2_in_cursor(database, cursor, plan_id)
    if verified is None:
        raise ValueError("product_run_contract_invalid")
    plan = verified["execution_plan"]
    approval = verified["approval"]
    spec = verified["spec"]
    if approval["status"] != "approved" or plan["job_id"] is None:
        raise ValueError("product_stop_not_running")
    database._validate_environment_project_actor(
        cursor,
        project_id=spec.project_id,
        actor_id=requester_actor_id,
        purpose="requester",
        require_readiness=True,
    )
    job_id = int(plan["job_id"])
    job = cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    active = _active_attempts_for_job(cursor, job_id)
    if job is None or job["status"] != "running" or len(active) != 1:
        raise ValueError("product_stop_not_running")
    attempt = active[0]
    candidates = _product_stop_candidates_for_job(cursor, job_id)
    current: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for candidate in candidates:
        payload = _verified_product_stop_payload(candidate)
        if payload is None:
            raise ValueError("product_stop_materialization_conflict")
        if payload["attempt_id"] == attempt["id"]:
            current.append((candidate, payload))
        elif candidate["status"] in {"pending", "approved"}:
            raise ValueError("product_stop_materialization_conflict")
    if len(current) > 1:
        raise ValueError("product_stop_materialization_conflict")
    if current:
        existing, payload = current[0]
        if existing["status"] == "pending":
            return {
                "approval_id": int(existing["id"]),
                "created": False,
                "deduplicated": True,
            }
        if existing["status"] == "approved":
            operation, invalid = _stop_operation_for_approval(
                cursor,
                approval=existing,
                payload=payload,
            )
            if invalid or operation is None:
                raise ValueError("product_stop_materialization_conflict")
            return {
                "approval_id": int(existing["id"]),
                "created": False,
                "deduplicated": True,
            }
        raise ValueError("product_stop_already_rejected")

    payload = {
        "attempt_id": str(attempt["id"]),
        "job_id": job_id,
        "source": PRODUCT_STOP_SOURCE,
    }
    payload_json = canonical_json(payload)
    timestamp = database._sqlite_now(cursor)
    cursor.execute(
        """
        INSERT INTO approvals (
            kind, payload, status, created_at, requester_actor_id,
            payload_sha256, payload_contract_version, payload_immutable_at
        ) VALUES ('stop', ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            payload_json,
            timestamp,
            requester_actor_id,
            utf8_sha256(payload_json),
            PRODUCT_STOP_CONTRACT_VERSION,
            timestamp,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - successful SQLite INSERT
        raise RuntimeError("Product stop approval id is unavailable")
    approval_id = int(cursor.lastrowid)
    database._append_approval_created_audit(
        cursor,
        approval_id=approval_id,
        kind="stop",
        requester_actor_id=requester_actor_id,
    )
    return {
        "approval_id": approval_id,
        "created": True,
        "deduplicated": False,
    }


def get_product_stop_request_result(
    database: "Database",
    approval_id: int,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        verified = verify_product_stop_approval_in_cursor(
            database,
            cursor,
            approval_id,
        )
        if verified is None:
            return None
        approval = verified["approval"]
        return {
            "approval_id": approval_id,
            "plan_id": verified["plan_id"],
            "project_id": verified["project_id"],
            "job_id": verified["payload"]["job_id"],
            "attempt_id": verified["payload"]["attempt_id"],
            "status": str(approval["status"]),
        }


def _reject_product_stop_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval: sqlite3.Row,
    decision_actor_id: str,
    decision_mechanism: str,
    note: str | None,
    stale: bool,
) -> None:
    timestamp = database._sqlite_now(cursor)
    stored_note = "stop_target_stale" if stale else note
    cursor.execute(
        """
        UPDATE approvals
        SET status = 'rejected', decided_at = ?, note = ?,
            decision_actor_id = ?, decision_mechanism = ?
        WHERE id = ? AND status = 'pending'
        """,
        (
            timestamp,
            stored_note,
            decision_actor_id,
            decision_mechanism,
            approval["id"],
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("product_stop_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=int(approval["id"]),
        kind="stop",
        status="rejected",
        decision_actor_id=decision_actor_id,
        decision_mechanism=decision_mechanism,
    )
    if stale:
        database.append_durable_audit_event_in_transaction(
            cursor,
            action="product_stop_target_stale",
            params={"reason": "stop_target_stale"},
            result="rejected",
            actor_id=decision_actor_id,
            actor_kind="actor",
            authentication=decision_mechanism,
            resource_type="approval",
            resource_id=str(approval["id"]),
            approval_id=int(approval["id"]),
            event_id=f"approval:{approval['id']}:stop-target-stale",
        )


def apply_product_stop_decision_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval_id: int,
    decision: Literal["approve", "reject"],
    decision_actor_id: str,
    decision_mechanism: str,
    note: str | None = None,
) -> dict[str, Any]:
    verified = verify_product_stop_approval_in_cursor(
        database,
        cursor,
        approval_id,
    )
    if verified is None:
        raise ValueError("product_stop_contract_invalid")
    approval = verified["approval"]
    if approval["status"] != "pending":
        raise ValueError("product_stop_decision_conflict")
    database._validate_environment_project_actor(
        cursor,
        project_id=verified["project_id"],
        actor_id=decision_actor_id,
        purpose="decider",
        require_readiness=True,
    )
    if decision == "reject":
        _reject_product_stop_in_transaction(
            database,
            cursor,
            approval=approval,
            decision_actor_id=decision_actor_id,
            decision_mechanism=decision_mechanism,
            note=note,
            stale=False,
        )
        return {"approval_id": approval_id, "status": "rejected", "operation_id": None}

    payload = verified["payload"]
    job = cursor.execute(
        "SELECT * FROM jobs WHERE id = ?",
        (payload["job_id"],),
    ).fetchone()
    active = _active_attempts_for_job(cursor, int(payload["job_id"]))
    stale = (
        job is None
        or job["status"] != "running"
        or len(active) != 1
        or active[0]["id"] != payload["attempt_id"]
        or verified["attempt"]["state"] not in _ACTIVE_ATTEMPT_STATES
    )
    if stale:
        _reject_product_stop_in_transaction(
            database,
            cursor,
            approval=approval,
            decision_actor_id=decision_actor_id,
            decision_mechanism=decision_mechanism,
            note=note,
            stale=True,
        )
        return {"approval_id": approval_id, "status": "rejected", "operation_id": None}
    if verified["attempt"]["recovery_hold_reason"] is not None:
        raise ValueError("product_stop_recovery_hold")
    existing_operations = cursor.execute(
        """
        SELECT id FROM execution_operations
        WHERE authorization_approval_id = ? OR
              (attempt_id = ? AND operation = 'stop')
        ORDER BY id LIMIT 2
        """,
        (approval_id, payload["attempt_id"]),
    ).fetchall()
    if existing_operations:
        raise ValueError("product_stop_materialization_conflict")
    operation_payload = {
        "attempt_id": payload["attempt_id"],
        "command": build_attempt_stop_command(
            int(payload["job_id"]),
            str(payload["attempt_id"]),
        ),
        "job_id": int(payload["job_id"]),
    }
    materialized = database.approve_stop_and_insert_execution_operation(
        approval_id=approval_id,
        attempt_id=str(payload["attempt_id"]),
        payload=operation_payload,
        decision_actor_id=decision_actor_id,
        decision_mechanism=decision_mechanism,
        note=note,
    )
    operation = materialized["operation"]
    return {
        "approval_id": approval_id,
        "status": "approved",
        "operation_id": str(operation["id"]),
    }


def list_product_run_summaries(
    database: "Database",
    *,
    project_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not project_ids or limit < 1:
        return []
    placeholders = ",".join("?" for _ in project_ids)
    with database.cursor() as cursor:
        rows = cursor.execute(
            f"""
            SELECT plan.id
            FROM execution_plans AS plan
            LEFT JOIN execution_plan_v2_specs AS companion
              ON companion.execution_plan_id = plan.id
            LEFT JOIN approvals AS approval
              ON approval.id = plan.request_approval_id
            LEFT JOIN projects AS legacy_project
              ON legacy_project.name = plan.project_name
            WHERE (
                (
                    plan.contract_version = ?
                    OR companion.execution_plan_id IS NOT NULL
                )
                AND (
                    companion.project_id IN ({placeholders})
                    OR (
                        approval.kind = ?
                        AND approval.payload_contract_version = ?
                        AND approval.payload_immutable_at IS NOT NULL
                        AND json_valid(approval.payload)
                        AND json_extract(approval.payload, '$.project_id')
                            IN ({placeholders})
                    )
                )
            ) OR (
                plan.contract_version <> ?
                AND companion.execution_plan_id IS NULL
                AND legacy_project.id IN ({placeholders})
            )
            ORDER BY plan.created_at DESC, plan.id DESC
            LIMIT ?
            """,
            (
                EXECUTION_PLAN_V2_CONTRACT_VERSION,
                *project_ids,
                EXECUTION_PLAN_V2_APPROVAL_KIND,
                EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION,
                *project_ids,
                EXECUTION_PLAN_V2_CONTRACT_VERSION,
                *project_ids,
                limit,
            ),
        ).fetchall()
        summaries = []
        for row in rows:
            try:
                detail = _load_product_run_in_cursor(
                    database,
                    cursor,
                    str(row["id"]),
                )
            except ValueError:
                scope = _scope_from_cursor(cursor, str(row["id"]))
                if scope is None or scope["project_id"] not in project_ids:
                    continue
                summaries.append(
                    {
                        "id": str(row["id"]),
                        "plan_id": str(row["id"]),
                        "project_id": scope["project_id"],
                        "project_name": scope["project_name"],
                        "state": "needs_attention",
                        "canonical_job_status": None,
                        "contract_kind": "execution_plan_v2",
                        "created_at": None,
                        "started_at": None,
                        "finished_at": None,
                        "source": "execution_plan_product_projection",
                    }
                )
                continue
            if detail is None:
                continue
            if detail["project_id"] not in project_ids:
                continue
            job = detail["job"]
            summaries.append(
                {
                    "id": detail["plan_id"],
                    "plan_id": detail["plan_id"],
                    "project_id": detail["project_id"],
                    "project_name": detail["project_name"],
                    "state": detail["state"],
                    "canonical_job_status": detail["canonical_job_status"],
                    "contract_kind": detail["contract"]["kind"],
                    "created_at": detail["created_at"],
                    "started_at": job["started_at"] if job is not None else None,
                    "finished_at": job["finished_at"] if job is not None else None,
                    "source": "execution_plan_product_projection",
                }
            )
        return summaries
