from __future__ import annotations

import sqlite3
import threading
import uuid
from typing import Any

import pytest

from app.audit import now_iso
from app.db import Database
from app.execution_contract import canonical_json_sha256, utf8_sha256
from app.identity import ActorType, ProjectRoleV2, generate_service_token
from app.product_run_store import create_product_stop_request_in_transaction
from dispatch_center.api.idempotency import (
    IdempotencyIdentity,
    IdempotencyResource,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork
from tests.test_execution_plan_v2_api import (
    _approved_execution_plan_v2_job,
    _enable_execution_plan_v2,
    _preview_request,
    _seed_execution_context,
)
from tests.test_run_templates_v2 import (
    OPERATOR_ID,
    REVIEWER_ID,
    SERVICE_ID,
    _insert_binding,
    _session_for,
)


def _running_product_run(client: Any, main_module: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )
    attempt = database.create_execution_attempt(
        job_id=approved["job_id"],
        backend="ssh",
        server_config_revision_id=seed["revision"]["id"],
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        initial_operation={
            "operation": "prepare",
            "payload": {"job_id": approved["job_id"]},
        },
    )
    return approved, attempt


def _all_database_counts(database: Any) -> dict[str, int]:
    with database.cursor() as cursor:
        tables = [
            str(row["name"])
            for row in cursor.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        return {
            table: int(cursor.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }


def _scheduler_lease(main_module: Any) -> dict[str, Any]:
    return main_module.app_state.db.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )


def _transition_attempt_running(
    main_module: Any,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    lease = _scheduler_lease(main_module)
    return main_module.app_state.db.transition_execution_attempt(
        attempt_id=attempt["id"],
        expected_state="dispatching",
        expected_liveness="known",
        new_state="running",
        new_liveness="known",
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        reason_code="remote_state_observed",
        evidence={"receipt_sha256": "7" * 64},
    )


def _terminalize_attempt(
    main_module: Any,
    attempt: dict[str, Any],
    *,
    exit_code: int,
) -> dict[str, Any]:
    running = (
        attempt
        if attempt["state"] == "running"
        else _transition_attempt_running(main_module, attempt)
    )
    lease = _scheduler_lease(main_module)
    return main_module.app_state.db.transition_execution_attempt(
        attempt_id=running["id"],
        expected_state="running",
        expected_liveness="known",
        new_state="done" if exit_code == 0 else "failed",
        new_liveness="known",
        exit_code=exit_code,
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        reason_code="terminal_evidence_valid",
        evidence={"sentinel_sha256": "8" * 64, "exit_code": exit_code},
    )


def _attach_artifact_metadata(
    database: Any,
    *,
    job_id: int,
    execution_attempt_id: str | None,
    relative_path: str,
) -> str:
    node_id = str(uuid.uuid4())
    node_attempt_id = str(uuid.uuid4())
    database.insert_node(
        node_id=node_id,
        server_name="pilot-117",
        secret_hash="node-secret-digest",
    )
    job = database.get_job(job_id)
    assert job is not None
    timestamp = now_iso()
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO node_attempts (
                id, job_id, node_id, status, command_sha256,
                lease_expires_at, created_at, terminal_at, exit_code,
                execution_attempt_id
            ) VALUES (?, ?, ?, 'done', ?, ?, ?, ?, 0, ?)
            """,
            (
                node_attempt_id,
                job_id,
                node_id,
                job.approved_command_sha256 or utf8_sha256(job.command),
                "2099-01-01T00:00:00.000Z",
                timestamp,
                timestamp,
                execution_attempt_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO node_attempt_artifacts (
                attempt_id, relative_path, kind, size_bytes, sha256, reported_at
            ) VALUES (?, ?, 'file', 12, ?, ?)
            """,
            (node_attempt_id, relative_path, "a" * 64, timestamp),
        )
    return node_attempt_id


def test_product_run_detail_clone_compare_and_empty_artifacts_are_read_only(api_client):
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)

    before = _all_database_counts(database)
    detail = client.get(f"/api/v2/runs/{plan_id}")
    assert detail.status_code == 200, detail.json()
    body = detail.json()
    assert body["plan_id"] == plan_id
    assert body["state"] == "preparing"
    assert body["canonical_job_status"] == "running"
    assert body["current_attempt"] == {
        "id": attempt["id"],
        "state": "dispatching",
        "liveness": "known",
    }
    assert body["contract"]["verified"] is True
    assert "command" not in str(body).lower()

    clone = client.post(
        f"/api/v2/runs/{plan_id}/clone-previews",
        json={"parameter_overrides": {"epochs": 13}},
    )
    assert clone.status_code == 409, clone.json()
    assert clone.json()["error"]["details"]["reason"] == ("target_worker_not_exclusive")

    compared = client.get(
        "/api/v2/runs/compare",
        params={"left_plan_id": plan_id, "right_plan_id": plan_id},
    )
    assert compared.status_code == 200, compared.json()
    dimensions = compared.json()["dimensions"]
    assert dimensions["parameters"]["equal"] is True
    assert "parameter_values" not in str(dimensions)
    assert dimensions["terminal_result"]["availability"] == "unknown"

    artifacts = client.get(f"/api/v2/runs/{plan_id}/artifacts")
    assert artifacts.status_code == 200, artifacts.json()
    artifact_body = artifacts.json()
    assert artifact_body["availability"] == "unknown"
    assert artifact_body["metadata_only"] is True
    assert artifact_body["complete"] is False
    assert artifact_body["items"] == []
    assert artifact_body["declared_outputs"]["availability"] == "known"
    assert _all_database_counts(database) == before


def test_product_stop_semantic_dedup_approval_and_replay_never_terminalize_job(
    api_client,
):
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)

    first = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "product-stop-first"},
        json={},
    )
    second = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "product-stop-second"},
        json={},
    )
    replay = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "product-stop-first"},
        json={},
    )
    assert first.status_code == second.status_code == replay.status_code == 202
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert second.json()["approval_id"] == first.json()["approval_id"]
    assert replay.json()["replayed"] is True
    assert replay.json()["deduplicated"] is False
    approval_id = first.json()["approval_id"]
    with database.cursor() as cursor:
        assert (
            cursor.execute("SELECT COUNT(*) FROM approvals WHERE kind = 'stop'").fetchone()[0] == 1
        )
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE operation = 'stop'"
            ).fetchone()[0]
            == 0
        )

    _session_for(client, main_module, REVIEWER_ID)
    review = client.get(f"/api/v2/approvals/{approval_id}")
    assert review.status_code == 200, review.json()
    assert review.json()["review"]["plan_id"] == plan_id
    assert review.json()["review"]["attempt_id"] == attempt["id"]
    decided = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "product-stop-approve"},
        json={"decision": "approve", "note": "stop this attempt"},
    )
    decided_replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "product-stop-approve"},
        json={"decision": "approve", "note": "stop this attempt"},
    )
    assert decided.status_code == 202, decided.json()
    assert decided.json()["status"] == "approved"
    assert decided_replay.status_code == 202, decided_replay.json()
    assert decided_replay.json()["replayed"] is True
    assert database.get_job(approved["job_id"]).status == "running"
    with database.cursor() as cursor:
        operation = cursor.execute(
            "SELECT * FROM execution_operations WHERE operation = 'stop'"
        ).fetchone()
        assert operation is not None
        assert operation["attempt_id"] == attempt["id"]
        assert operation["authorization_approval_id"] == approval_id

    detail = client.get(f"/api/v2/runs/{plan_id}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["state"] == "stopping"
    assert detail.json()["canonical_job_status"] == "running"

    _session_for(client, main_module, OPERATOR_ID)
    approved_reuse = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "product-stop-approved-reuse"},
        json={},
    )
    assert approved_reuse.status_code == 202, approved_reuse.json()
    assert approved_reuse.json()["approval_id"] == approval_id
    assert approved_reuse.json()["deduplicated"] is True
    with database.cursor() as cursor:
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE operation = 'stop'"
            ).fetchone()[0]
            == 1
        )

    _terminalize_attempt(main_module, attempt, exit_code=0)
    terminal = client.get(f"/api/v2/runs/{plan_id}")
    assert terminal.status_code == 200, terminal.json()
    assert terminal.json()["state"] == "succeeded"
    assert terminal.json()["canonical_job_status"] == "done"


def test_product_stop_two_connections_serialize_to_one_approval(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    writers = [Database(database.path), Database(database.path)]
    barrier = threading.Barrier(2)
    outcomes: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def request(index: int) -> None:
        writer = writers[index]
        created: dict[str, Any] = {}
        identity = IdempotencyIdentity(
            actor_id=OPERATOR_ID,
            route_key="POST /api/v2/runs/{plan_id}/stop-requests",
            key_sha256=("a" if index == 0 else "b") * 64,
            request_sha256="c" * 64,
        )

        def create(cursor):
            created.update(
                create_product_stop_request_in_transaction(
                    writer,
                    cursor,
                    plan_id=plan_id,
                    requester_actor_id=OPERATOR_ID,
                )
            )
            return IdempotencyResource("approval", str(created["approval_id"]))

        try:
            barrier.wait(timeout=5)
            with SQLiteUnitOfWork(writer) as unit_of_work:
                outcome = unit_of_work.run_idempotent(identity, create)
            outcomes.append({**created, "resource_id": outcome.resource.resource_id})
        except BaseException as exc:  # pragma: no cover - assertion below
            failures.append(exc)

    threads = [threading.Thread(target=request, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for writer in writers:
        writer.close()

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(outcomes) == 2
    assert len({item["approval_id"] for item in outcomes}) == 1
    assert sorted(item["created"] for item in outcomes) == [False, True]
    assert sorted(item["deduplicated"] for item in outcomes) == [False, True]
    with database.cursor() as cursor:
        assert (
            cursor.execute("SELECT COUNT(*) FROM approvals WHERE kind = 'stop'").fetchone()[0] == 1
        )
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM api_idempotency_keys "
                "WHERE route_key = 'POST /api/v2/runs/{plan_id}/stop-requests'"
            ).fetchone()[0]
            == 2
        )
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE operation = 'stop'"
            ).fetchone()[0]
            == 0
        )


def test_product_stop_request_and_decisions_rollback_on_audit_faults(
    api_client,
    monkeypatch,
):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    before_request = _all_database_counts(database)
    original_created_audit = database._append_approval_created_audit

    def fail_created_audit(*_args, **_kwargs):
        raise RuntimeError("injected Product stop request audit failure")

    monkeypatch.setattr(database, "_append_approval_created_audit", fail_created_audit)
    with pytest.raises(RuntimeError, match="Product stop request audit failure"):
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "stop-request-audit-fault"},
            json={},
        )
    assert _all_database_counts(database) == before_request

    monkeypatch.setattr(
        database,
        "_append_approval_created_audit",
        original_created_audit,
    )
    with database.cursor() as cursor:
        cursor.execute(
            """
            CREATE TRIGGER fail_product_stop_idempotency_insert
            BEFORE INSERT ON api_idempotency_keys
            WHEN NEW.route_key = 'POST /api/v2/runs/{plan_id}/stop-requests'
            BEGIN
                SELECT RAISE(ABORT, 'injected Product stop idempotency failure');
            END
            """
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="Product stop idempotency failure",
    ):
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "stop-request-idempotency-fault"},
            json={},
        )
    assert _all_database_counts(database) == before_request
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER fail_product_stop_idempotency_insert")

    requested = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "stop-request-after-fault"},
        json={},
    )
    assert requested.status_code == 202, requested.json()
    approval_id = requested.json()["approval_id"]
    _session_for(client, main_module, REVIEWER_ID)
    before_decision = _all_database_counts(database)
    original_append = database.append_durable_audit_event_in_transaction

    def fail_stop_outbox_audit(cursor, **kwargs):
        if kwargs.get("action") == "execution_stop_requested":
            raise RuntimeError("injected Product stop outbox audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_stop_outbox_audit,
    )
    with pytest.raises(RuntimeError, match="Product stop outbox audit failure"):
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            headers={"Idempotency-Key": "stop-approve-audit-fault"},
            json={"decision": "approve", "note": "must roll back"},
        )
    assert database.get_approval(approval_id).status == "pending"
    assert _all_database_counts(database) == before_decision

    def fail_reject_audit(cursor, **kwargs):
        if kwargs.get("action") == "approval_decided":
            raise RuntimeError("injected Product stop reject audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_reject_audit,
    )
    with pytest.raises(RuntimeError, match="Product stop reject audit failure"):
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            headers={"Idempotency-Key": "stop-reject-audit-fault"},
            json={"decision": "reject", "note": "must also roll back"},
        )
    assert database.get_approval(approval_id).status == "pending"
    assert _all_database_counts(database) == before_decision


def test_product_stop_allows_operator_owner_self_decision(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    project_id = client.get(f"/api/v2/runs/{approved['execution_plan_id']}").json()["project_id"]
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=OPERATOR_ID,
        role=ProjectRoleV2.OWNER,
    )
    _session_for(client, main_module, OPERATOR_ID)
    requested = client.post(
        f"/api/v2/runs/{approved['execution_plan_id']}/stop-requests",
        headers={"Idempotency-Key": "product-stop-self-request"},
        json={},
    )
    assert requested.status_code == 202, requested.json()
    decided = client.post(
        f"/api/v2/approvals/{requested.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "product-stop-self-approve"},
        json={"decision": "approve", "note": "operator is also owner"},
    )
    assert decided.status_code == 202, decided.json()
    assert decided.json()["status"] == "approved"


def test_pending_product_run_is_awaiting_approval_and_workspace_uses_plan_identity(
    api_client,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    preview_request = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=preview_request,
    )
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "pending-product-run"},
        json={
            **preview_request,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()
    plan_id = submitted.json()["execution_plan_id"]
    detail = client.get(f"/api/v2/runs/{plan_id}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["state"] == "awaiting_approval"
    before_clone = _all_database_counts(main_module.app_state.db)
    clone = client.post(
        f"/api/v2/runs/{plan_id}/clone-previews",
        json={"parameter_overrides": {"epochs": 13}},
    )
    assert clone.status_code == 200, clone.json()
    clone_body = clone.json()
    assert clone_body["source_plan_id"] == plan_id
    assert clone_body["request"]["parameter_overrides"]["epochs"] == 13
    assert clone_body["plan_digest"] == clone_body["plan"]["plan_digest"]
    assert _all_database_counts(main_module.app_state.db) == before_clone
    workspace = client.get("/api/v2/workspace")
    assert workspace.status_code == 200, workspace.json()
    recent = workspace.json()["recent_runs"]
    assert recent[0]["id"] == plan_id
    assert recent[0]["plan_id"] == plan_id
    assert recent[0]["source"] == "execution_plan_product_projection"
    assert all(item["source"] != "legacy_job" for item in recent)


def test_compare_route_is_static_and_feature_off_is_opaque(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    assert (
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ).status_code
        == 200
    )
    requested = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "feature-off-product-stop"},
        json={},
    )
    assert requested.status_code == 202, requested.json()
    main_module.app_state.config.run_experience_v2_enabled = False
    assert client.get(f"/api/v2/runs/{plan_id}").status_code == 404
    assert (
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ).status_code
        == 404
    )
    assert client.get(f"/api/v2/runs/{plan_id}/artifacts").status_code == 404
    assert (
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "feature-off-product-stop-hidden"},
            json={},
        ).status_code
        == 404
    )
    approval_id = requested.json()["approval_id"]
    assert client.get(f"/api/v2/approvals/{approval_id}").status_code == 404
    assert (
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            headers={"Idempotency-Key": "feature-off-stop-decision"},
            json={"decision": "approve", "note": "hidden"},
        ).status_code
        == 404
    )
    workspace = client.get("/api/v2/workspace")
    assert workspace.status_code == 200, workspace.json()
    assert approval_id not in {item["id"] for item in workspace.json()["pending_approvals"]}


def test_parameter_comparison_uses_digest_not_values(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    response = client.get(
        "/api/v2/runs/compare",
        params={"left_plan_id": plan_id, "right_plan_id": plan_id},
    )
    parameters = response.json()["dimensions"]["parameters"]
    assert parameters["left"]["digest"] == canonical_json_sha256(
        {
            "enabled": True,
            "epochs": 12,
            "label": "model",
            "mode": "safe",
            "ratio": "0.25",
        }
    )
    assert "model" not in str(parameters["left"])


def test_compare_requires_bilateral_access_and_mixed_legacy_stays_unknown(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    left_plan_id = approved["execution_plan_id"]
    foreign_name = f"foreign-{uuid.uuid4()}"
    foreign_project_id = database.insert_project(
        foreign_name,
        "/private/foreign-project",
    )
    right_plan_id = str(uuid.uuid4())
    command = "python legacy-foreign.py"
    with database.cursor() as cursor:
        source = cursor.execute(
            "SELECT server_config_revision_id FROM execution_plans WHERE id = ?",
            (left_plan_id,),
        ).fetchone()
        assert source is not None
        cursor.execute(
            """
            INSERT INTO execution_plans (
                id, project_name, contract_version, plan_digest,
                command, command_sha256, reproducible, dataset_none,
                server_config_revision_id, created_at
            ) VALUES (?, ?, 'execution-plan-v1', ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                right_plan_id,
                foreign_name,
                "e" * 64,
                command,
                utf8_sha256(command),
                source["server_config_revision_id"],
                now_iso(),
            ),
        )
    _session_for(client, main_module, OPERATOR_ID)
    denied = client.get(
        "/api/v2/runs/compare",
        params={"left_plan_id": left_plan_id, "right_plan_id": right_plan_id},
    )
    assert denied.status_code == 404

    _insert_binding(
        database,
        project_id=foreign_project_id,
        actor_id=OPERATOR_ID,
        role=ProjectRoleV2.VIEWER,
    )
    before = _all_database_counts(database)
    compared = client.get(
        "/api/v2/runs/compare",
        params={"left_plan_id": left_plan_id, "right_plan_id": right_plan_id},
    )
    assert compared.status_code == 200, compared.json()
    assert compared.json()["unknown_dimensions"] == sorted(compared.json()["dimensions"])
    assert all(
        dimension["availability"] == "unknown"
        for dimension in compared.json()["dimensions"].values()
    )
    assert _all_database_counts(database) == before


def test_product_stop_rejection_is_terminal_for_new_requests_but_not_job(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    requested = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "product-stop-reject-request"},
        json={},
    )
    assert requested.status_code == 202, requested.json()
    _session_for(client, main_module, REVIEWER_ID)
    rejected = client.post(
        f"/api/v2/approvals/{requested.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "product-stop-reject-decision"},
        json={"decision": "reject", "note": "keep running"},
    )
    assert rejected.status_code == 202, rejected.json()
    assert rejected.json()["status"] == "rejected"
    assert database.get_job(approved["job_id"]).status == "running"
    _session_for(client, main_module, OPERATOR_ID)
    retried = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "product-stop-after-rejection"},
        json={},
    )
    assert retried.status_code == 409, retried.json()
    assert retried.json()["error"]["details"]["reason"] == ("product_stop_already_rejected")
    with database.cursor() as cursor:
        assert (
            cursor.execute("SELECT COUNT(*) FROM approvals WHERE kind = 'stop'").fetchone()[0] == 1
        )
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE operation = 'stop'"
            ).fetchone()[0]
            == 0
        )


def test_unknown_liveness_never_projects_failed_or_mutates_job(api_client):
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE execution_attempts SET liveness = 'unknown' WHERE id = ?",
            (attempt["id"],),
        )
    _session_for(client, main_module, OPERATOR_ID)
    response = client.get(f"/api/v2/runs/{approved['execution_plan_id']}")
    assert response.status_code == 200, response.json()
    assert response.json()["state"] == "needs_attention"
    assert "attempt_liveness_unknown" in response.json()["attention_reasons"]
    assert response.json()["canonical_job_status"] == "running"
    assert database.get_job(approved["job_id"]).status == "running"


def test_done_with_uncollected_results_is_succeeded_with_attention(api_client):
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    _terminalize_attempt(main_module, attempt, exit_code=0)
    _session_for(client, main_module, OPERATOR_ID)
    response = client.get(f"/api/v2/runs/{approved['execution_plan_id']}")
    assert response.status_code == 200, response.json()
    assert response.json()["state"] == "succeeded"
    assert response.json()["canonical_job_status"] == "done"
    assert "result_collection_pending" in response.json()["attention_reasons"]


def test_v2_tamper_is_authorized_conflict_but_foreign_opaque_not_found(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER approvals_execution_pin_update_guard")
        cursor.execute(
            "UPDATE approvals SET payload_sha256 = ? WHERE id = ?",
            ("0" * 64, approved["approval_id"]),
        )
    _session_for(client, main_module, OPERATOR_ID)
    authorized = client.get(f"/api/v2/runs/{approved['execution_plan_id']}")
    assert authorized.status_code == 409, authorized.json()
    assert authorized.json()["error"]["details"]["reason"] == ("product_run_contract_invalid")

    foreign_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=foreign_id,
        actor_type=ActorType.HUMAN,
        display_name="Foreign viewer",
    )
    _session_for(client, main_module, foreign_id)
    foreign = client.get(f"/api/v2/runs/{approved['execution_plan_id']}")
    assert foreign.status_code == 404


def test_v2_project_scope_uses_companion_id_when_plan_name_is_tampered(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    canonical_project_id = client.get(f"/api/v2/runs/{plan_id}").json()["project_id"]

    foreign_project_id = database.insert_project(
        "foreign-product-scope",
        "/private/foreign-product-scope",
    )
    foreign_actor_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=foreign_actor_id,
        actor_type=ActorType.HUMAN,
        display_name="Foreign Product owner",
    )
    _insert_binding(
        database,
        project_id=foreign_project_id,
        actor_id=foreign_actor_id,
        role=ProjectRoleV2.OWNER,
    )

    with database.cursor() as cursor:
        with pytest.raises(sqlite3.IntegrityError, match="execution plan is immutable"):
            cursor.execute(
                "UPDATE execution_plans SET project_name = ? WHERE id = ?",
                ("foreign-product-scope", plan_id),
            )
        cursor.execute("DROP TRIGGER execution_plans_are_immutable")
        cursor.execute(
            "UPDATE execution_plans SET project_name = ? WHERE id = ?",
            ("foreign-product-scope", plan_id),
        )

    canonical_workspace = client.get("/api/v2/workspace")
    assert canonical_workspace.status_code == 200
    canonical_summary = next(
        item for item in canonical_workspace.json()["recent_runs"] if item["plan_id"] == plan_id
    )
    assert canonical_summary["project_id"] == canonical_project_id
    assert canonical_summary["state"] == "needs_attention"

    canonical_responses = (
        client.get(f"/api/v2/runs/{plan_id}"),
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ),
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ),
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "tampered-project-name-stop"},
            json={},
        ),
        client.get(f"/api/v2/runs/{plan_id}/artifacts"),
    )
    assert [response.status_code for response in canonical_responses] == [409] * 5
    assert all(
        response.json()["error"]["details"]["reason"] == "product_run_contract_invalid"
        for response in canonical_responses
    )

    _session_for(client, main_module, foreign_actor_id)
    foreign_workspace = client.get("/api/v2/workspace")
    assert foreign_workspace.status_code == 200
    assert all(item["plan_id"] != plan_id for item in foreign_workspace.json()["recent_runs"])
    foreign_responses = (
        client.get(f"/api/v2/runs/{plan_id}"),
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ),
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ),
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "foreign-tampered-project-name-stop"},
            json={},
        ),
        client.get(f"/api/v2/runs/{plan_id}/artifacts"),
    )
    assert all(response.status_code == 404 for response in foreign_responses)


def test_v2_missing_companion_uses_pinned_approval_scope_and_stays_opaque(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    canonical_project_id = client.get(f"/api/v2/runs/{plan_id}").json()["project_id"]

    foreign_project_id = database.insert_project(
        "foreign-missing-companion",
        "/private/foreign-missing-companion",
    )
    foreign_actor_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=foreign_actor_id,
        actor_type=ActorType.HUMAN,
        display_name="Foreign missing-companion owner",
    )
    _insert_binding(
        database,
        project_id=foreign_project_id,
        actor_id=foreign_actor_id,
        role=ProjectRoleV2.OWNER,
    )
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER trg_execution_plan_v2_specs_immutable_delete")
        cursor.execute(
            "DELETE FROM execution_plan_v2_specs WHERE execution_plan_id = ?",
            (plan_id,),
        )

    canonical_workspace = client.get("/api/v2/workspace")
    assert canonical_workspace.status_code == 200
    canonical_summary = next(
        item for item in canonical_workspace.json()["recent_runs"] if item["plan_id"] == plan_id
    )
    assert canonical_summary["project_id"] == canonical_project_id
    assert canonical_summary["state"] == "needs_attention"

    canonical_responses = (
        client.get(f"/api/v2/runs/{plan_id}"),
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ),
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ),
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "missing-companion-stop"},
            json={},
        ),
        client.get(f"/api/v2/runs/{plan_id}/artifacts"),
    )
    assert [response.status_code for response in canonical_responses] == [409] * 5
    assert all(
        response.json()["error"]["details"]["reason"] == "product_run_contract_invalid"
        for response in canonical_responses
    )

    _session_for(client, main_module, foreign_actor_id)
    foreign_workspace = client.get("/api/v2/workspace")
    assert foreign_workspace.status_code == 200
    assert all(item["plan_id"] != plan_id for item in foreign_workspace.json()["recent_runs"])
    foreign_responses = (
        client.get(f"/api/v2/runs/{plan_id}"),
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ),
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ),
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "foreign-missing-companion-stop"},
            json={},
        ),
        client.get(f"/api/v2/runs/{plan_id}/artifacts"),
    )
    assert all(response.status_code == 404 for response in foreign_responses)


def test_v2_companion_project_tamper_cannot_reassign_authorization_scope(api_client):
    client, main_module = api_client
    approved, _attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    canonical_project_id = client.get(f"/api/v2/runs/{plan_id}").json()["project_id"]

    foreign_project_id = database.insert_project(
        "foreign-companion-project",
        "/private/foreign-companion-project",
    )
    foreign_actor_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=foreign_actor_id,
        actor_type=ActorType.HUMAN,
        display_name="Foreign companion-project owner",
    )
    _insert_binding(
        database,
        project_id=foreign_project_id,
        actor_id=foreign_actor_id,
        role=ProjectRoleV2.OWNER,
    )
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER trg_execution_plan_v2_specs_immutable_update")
        cursor.execute(
            "UPDATE execution_plan_v2_specs SET project_id = ? WHERE execution_plan_id = ?",
            (foreign_project_id, plan_id),
        )

    canonical_workspace = client.get("/api/v2/workspace")
    assert canonical_workspace.status_code == 200
    canonical_summary = next(
        item for item in canonical_workspace.json()["recent_runs"] if item["plan_id"] == plan_id
    )
    assert canonical_summary["project_id"] == canonical_project_id
    assert canonical_summary["state"] == "needs_attention"

    canonical_responses = (
        client.get(f"/api/v2/runs/{plan_id}"),
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ),
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ),
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "companion-project-tamper-stop"},
            json={},
        ),
        client.get(f"/api/v2/runs/{plan_id}/artifacts"),
    )
    assert [response.status_code for response in canonical_responses] == [409] * 5

    _session_for(client, main_module, foreign_actor_id)
    foreign_workspace = client.get("/api/v2/workspace")
    assert foreign_workspace.status_code == 200
    assert all(item["plan_id"] != plan_id for item in foreign_workspace.json()["recent_runs"])
    foreign_responses = (
        client.get(f"/api/v2/runs/{plan_id}"),
        client.post(
            f"/api/v2/runs/{plan_id}/clone-previews",
            json={"parameter_overrides": {}},
        ),
        client.get(
            "/api/v2/runs/compare",
            params={"left_plan_id": plan_id, "right_plan_id": plan_id},
        ),
        client.post(
            f"/api/v2/runs/{plan_id}/stop-requests",
            headers={"Idempotency-Key": "foreign-companion-project-tamper-stop"},
            json={},
        ),
        client.get(f"/api/v2/runs/{plan_id}/artifacts"),
    )
    assert all(response.status_code == 404 for response in foreign_responses)


def test_stop_stale_target_rejects_and_same_key_replays_after_terminal(api_client):
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    requested = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "stale-stop-request"},
        json={},
    )
    assert requested.status_code == 202, requested.json()
    _terminalize_attempt(main_module, attempt, exit_code=0)
    replay = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "stale-stop-request"},
        json={},
    )
    assert replay.status_code == 202, replay.json()
    assert replay.json()["replayed"] is True
    before_new_key = _all_database_counts(database)
    new_key = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "stale-stop-new-key"},
        json={},
    )
    assert new_key.status_code == 409, new_key.json()
    assert _all_database_counts(database) == before_new_key

    _session_for(client, main_module, REVIEWER_ID)
    decision = client.post(
        f"/api/v2/approvals/{requested.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "stale-stop-decision"},
        json={"decision": "approve", "note": "target may have finished"},
    )
    assert decision.status_code == 202, decision.json()
    assert decision.json()["status"] == "rejected"
    approval = database.get_approval(requested.json()["approval_id"])
    assert approval is not None
    assert approval.note == "stop_target_stale"
    assert database.get_job(approved["job_id"]).status == "done"
    with database.cursor() as cursor:
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE operation = 'stop'"
            ).fetchone()[0]
            == 0
        )


def test_stop_recovery_hold_stays_pending_and_service_cannot_decide(api_client):
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    plan_id = approved["execution_plan_id"]
    _session_for(client, main_module, OPERATOR_ID)
    requested = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "hold-stop-request"},
        json={},
    )
    assert requested.status_code == 202, requested.json()
    approval_id = requested.json()["approval_id"]
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE execution_attempts SET recovery_hold_reason = ? WHERE id = ?",
            ("manual_recovery_review", attempt["id"]),
        )

    database.insert_service_account(
        actor_id=SERVICE_ID,
        name=f"product-stop-service-{uuid.uuid4()}",
    )
    issued = generate_service_token()
    database.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=SERVICE_ID,
        secret_hash=issued.secret_hash,
        scopes=["project.operate"],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.service_token_auth_enabled = True
    client.cookies.clear()
    service_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={
            "Authorization": f"Bearer {issued.raw_token}",
            "Idempotency-Key": "service-stop-decision",
        },
        json={"decision": "approve", "note": "service cannot decide"},
    )
    assert service_decision.status_code == 403

    _session_for(client, main_module, REVIEWER_ID)
    held = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "hold-stop-decision"},
        json={"decision": "approve", "note": "held"},
    )
    assert held.status_code == 409, held.json()
    assert held.json()["error"]["details"]["reason"] == ("product_stop_recovery_hold")
    assert database.get_approval(approval_id).status == "pending"
    with database.cursor() as cursor:
        assert (
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE operation = 'stop'"
            ).fetchone()[0]
            == 0
        )


def test_artifact_metadata_is_validated_again_and_never_exposes_bad_path(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    database = main_module.app_state.db
    command = "python legacy-artifacts.py"
    job_id = database.insert_job(command, project=seed["project_name"])
    plan_id = str(uuid.uuid4())
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO execution_plans (
                id, project_name, contract_version, plan_digest,
                command, command_sha256, reproducible, dataset_none,
                server_config_revision_id, job_id, created_at
            ) VALUES (?, ?, 'execution-plan-v1', ?, ?, ?, 0, 1, ?, ?, ?)
            """,
            (
                plan_id,
                seed["project_name"],
                "d" * 64,
                command,
                utf8_sha256(command),
                seed["revision"]["id"],
                job_id,
                now_iso(),
            ),
        )
    node_attempt_id = _attach_artifact_metadata(
        database,
        job_id=job_id,
        execution_attempt_id=None,
        relative_path="results/model.bin",
    )
    _session_for(client, main_module, OPERATOR_ID)
    known = client.get(f"/api/v2/runs/{plan_id}/artifacts")
    assert known.status_code == 200, known.json()
    assert known.json()["availability"] == "known"
    assert known.json()["items"] == [
        {
            "relative_path": "results/model.bin",
            "kind": "file",
            "size_bytes": 12,
            "sha256": "a" * 64,
            "reported_at": known.json()["items"][0]["reported_at"],
            "metadata_only": True,
        }
    ]
    assert "server_name" not in str(known.json())
    assert "storage" not in str(known.json())

    with database.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO node_attempt_artifacts (
                attempt_id, relative_path, kind, size_bytes, sha256, reported_at
            ) VALUES (?, '../secret', 'file', 1, ?, ?)
            """,
            [
                (node_attempt_id, "b" * 64, now_iso()),
            ],
        )
        cursor.executemany(
            """
            INSERT INTO node_attempt_artifacts (
                attempt_id, relative_path, kind, size_bytes, sha256, reported_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            [
                (node_attempt_id, "/absolute-secret", "file", "c" * 64, now_iso()),
                (node_attempt_id, "bad-kind", "<script>", "d" * 64, now_iso()),
                (node_attempt_id, "bad-digest", "file", "z" * 64, now_iso()),
            ],
        )
    invalid = client.get(f"/api/v2/runs/{plan_id}/artifacts")
    assert invalid.status_code == 200, invalid.json()
    assert invalid.json()["availability"] == "unknown"
    assert invalid.json()["scope_truncated"] is True
    assert "../secret" not in str(invalid.json())
    assert "/absolute-secret" not in str(invalid.json())
    assert "<script>" not in str(invalid.json())
    assert "z" * 64 not in str(invalid.json())

    foreign_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=foreign_id,
        actor_type=ActorType.HUMAN,
        display_name="Foreign artifact viewer",
    )
    _session_for(client, main_module, foreign_id)
    assert client.get(f"/api/v2/runs/{plan_id}/artifacts").status_code == 404


def test_legacy_plan_is_limited_and_cannot_clone_or_stop(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    database = main_module.app_state.db
    plan_id = str(uuid.uuid4())
    command = "python legacy.py"
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO execution_plans (
                id, project_name, contract_version, plan_digest,
                command, command_sha256, reproducible, dataset_none,
                server_config_revision_id, created_at
            ) VALUES (?, ?, 'execution-plan-v1', ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                plan_id,
                seed["project_name"],
                "c" * 64,
                command,
                utf8_sha256(command),
                seed["revision"]["id"],
                now_iso(),
            ),
        )
    _session_for(client, main_module, OPERATOR_ID)
    detail = client.get(f"/api/v2/runs/{plan_id}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["contract"]["kind"] == "legacy_execution_plan"
    assert detail.json()["state"] == "needs_attention"
    assert detail.json()["dimensions"]["parameters"]["availability"] == "unknown"
    clone = client.post(
        f"/api/v2/runs/{plan_id}/clone-previews",
        json={"parameter_overrides": {}},
    )
    stop = client.post(
        f"/api/v2/runs/{plan_id}/stop-requests",
        headers={"Idempotency-Key": "legacy-stop"},
        json={},
    )
    assert clone.status_code == 409
    assert stop.status_code == 409


def test_legacy_plan_without_unique_canonical_project_uuid_is_opaque(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    database = main_module.app_state.db
    _session_for(client, main_module, OPERATOR_ID)
    missing_uuid_plan_id = str(uuid.uuid4())
    unmapped_plan_id = str(uuid.uuid4())
    command = "python legacy-unmapped.py"
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO projects (name, id, repo_or_path, created_at)
            VALUES ('legacy-null-uuid', NULL, '/private/legacy-null', ?)
            """,
            (now_iso(),),
        )
        for plan_id, project_name in (
            (missing_uuid_plan_id, "legacy-null-uuid"),
            (unmapped_plan_id, "legacy-no-project-row"),
        ):
            cursor.execute(
                """
                INSERT INTO execution_plans (
                    id, project_name, contract_version, plan_digest,
                    command, command_sha256, reproducible, dataset_none,
                    server_config_revision_id, created_at
                ) VALUES (?, ?, 'execution-plan-v1', ?, ?, ?, 0, 1, ?, ?)
                """,
                (
                    plan_id,
                    project_name,
                    "f" * 64,
                    command,
                    utf8_sha256(command),
                    seed["revision"]["id"],
                    now_iso(),
                ),
            )
    assert client.get(f"/api/v2/runs/{missing_uuid_plan_id}").status_code == 404
    assert client.get(f"/api/v2/runs/{unmapped_plan_id}").status_code == 404
