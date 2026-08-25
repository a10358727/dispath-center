"""HTTP-surface tests for Experiment v2 (DG-EXPERIMENT-V1, P3).

Covers the 7-gate feature flag, preview (no writes), request (pending
card), decision approve/reject via the v2 decisions fan-out, list/detail
read-only projections (including metrics_status), authorization, and an
end-to-end store->dispatch check that an HTTP-approved member Job passes
the pinned-contract dispatch validation added in this same packet.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import Database
from app.identity import ActorType
from tests.test_experiment_v2_store import (
    _guard,
    _matrix_2x2,
    _seed_experiment_context,
)
from tests.test_run_templates_v2 import (
    OPERATOR_ID,
    OWNER_ID,
    REVIEWER_ID,
    SERVICE_ID,
    _session_for,
)


def _enable_experiment_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_environments_v1_enabled = True
    config.run_template_v2_enabled = True
    config.dataset_assets_v2_enabled = True
    config.run_experience_v2_enabled = True
    config.experiment_v2_enabled = True
    config.authorization_mode = "enforce"


def _seed_experiment_http_context(main_module) -> dict:
    database = main_module.app_state.db
    return _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))


def _experiment_request_body(seed: dict, *, target_servers: list[str]) -> dict:
    return {
        "project_version_id": seed["version"]["id"],
        "template_selection": {
            "kind": "run_profile_revision",
            "run_profile_id": seed["template"]["run_profile_id"],
        },
        "dataset_selection": {"kind": "none"},
        "matrix": {
            "axes": [
                {"name": "epochs", "values": [10, 20]},
                {"name": "mode", "values": ["fast", "safe"]},
                {"name": "enabled", "values": [True]},
                {"name": "ratio", "values": ["0.25"]},
                {"name": "label", "values": ["model"]},
            ]
        },
        "guard": {
            "total_runs": 4,
            "target_servers": target_servers,
        },
    }


def _counts(database: Database) -> dict[str, int]:
    with database.cursor() as cursor:
        return {
            table: int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "approvals",
                "experiments",
                "experiment_plan_members",
                "experiment_plan_specs",
                "execution_plans",
                "jobs",
            )
        }


def test_flag_off_is_a_hidden_interface(api_client):
    client, main_module = api_client
    _enable_experiment_v2(main_module)
    main_module.app_state.config.experiment_v2_enabled = False
    seed = _seed_experiment_http_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    body = _experiment_request_body(seed, target_servers=["pilot-a", "pilot-b"])

    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-previews",
        json=body,
    )
    assert preview.status_code == 404

    requested = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-requests",
        headers={"Idempotency-Key": "experiment-flag-off"},
        json=body,
    )
    assert requested.status_code == 404

    listed = client.get(f"/api/v2/experiments?project_id={seed['project_id']}")
    assert listed.status_code == 404

    detail = client.get("/api/v2/experiments/1")
    assert detail.status_code == 404


def test_preview_resolves_expansion_without_any_write(api_client):
    client, main_module = api_client
    _enable_experiment_v2(main_module)
    seed = _seed_experiment_http_context(main_module)
    database = main_module.app_state.db
    _session_for(client, main_module, OPERATOR_ID)
    body = _experiment_request_body(seed, target_servers=["pilot-a", "pilot-b"])

    before = _counts(database)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-previews",
        json=body,
    )
    after = _counts(database)

    assert preview.status_code == 200, preview.json()
    payload = preview.json()
    assert payload["ready"] is True
    assert payload["run_count"] == 4
    assert len(payload["plan_digests"]) == 4
    assert len(set(payload["plan_digests"])) == 4
    assert len(payload["members"]) == 4
    assert after == before


def test_request_happy_path_creates_pending_card(api_client):
    client, main_module = api_client
    _enable_experiment_v2(main_module)
    seed = _seed_experiment_http_context(main_module)
    database = main_module.app_state.db
    _session_for(client, main_module, OPERATOR_ID)
    body = _experiment_request_body(seed, target_servers=["pilot-a", "pilot-b"])

    before = _counts(database)
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-requests",
        headers={"Idempotency-Key": "experiment-request-happy"},
        json=body,
    )
    after = _counts(database)

    assert submitted.status_code == 202, submitted.json()
    result = submitted.json()
    assert result["run_count"] == 4
    assert len(result["plan_ids"]) == 4
    assert result["status"] == "pending"
    assert result["replayed"] is False
    assert after["approvals"] - before["approvals"] == 1
    assert after["experiments"] - before["experiments"] == 1
    assert after["experiment_plan_members"] - before["experiment_plan_members"] == 4
    assert after["jobs"] == before["jobs"]

    # Replay: identical idempotency key + body returns the same result.
    replayed = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-requests",
        headers={"Idempotency-Key": "experiment-request-happy"},
        json=body,
    )
    assert replayed.status_code == 202, replayed.json()
    assert replayed.json()["experiment_id"] == result["experiment_id"]
    assert replayed.json()["replayed"] is True
    assert _counts(database) == after


def _requested_experiment(client, main_module) -> tuple[dict, dict]:
    _enable_experiment_v2(main_module)
    seed = _seed_experiment_http_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    body = _experiment_request_body(seed, target_servers=["pilot-a", "pilot-b"])
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-requests",
        headers={"Idempotency-Key": f"experiment-request-{uuid.uuid4()}"},
        json=body,
    )
    assert submitted.status_code == 202, submitted.json()
    return seed, submitted.json()


def test_decision_approve_materializes_n_jobs_via_http(api_client):
    client, main_module = api_client
    seed, requested = _requested_experiment(client, main_module)
    database = main_module.app_state.db
    approval_id = requested["approval_id"]

    _session_for(client, main_module, REVIEWER_ID)
    before = _counts(database)
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "experiment-approve"},
        json={"decision": "approve", "note": "reviewed"},
    )
    after = _counts(database)

    assert approved.status_code == 202, approved.json()
    decision = approved.json()
    assert decision["status"] == "approved"
    assert decision["created"] is True
    assert len(decision["job_ids"]) == 4
    assert len(set(decision["job_ids"])) == 4
    assert after["jobs"] - before["jobs"] == 4

    # Replay the exact same decision: idempotent, no new jobs.
    replayed = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "experiment-approve"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert replayed.status_code == 202, replayed.json()
    assert replayed.json()["replayed"] is True
    assert sorted(replayed.json()["job_ids"]) == sorted(decision["job_ids"])
    assert _counts(database) == after


def test_decision_reject_leaves_zero_jobs(api_client):
    client, main_module = api_client
    seed, requested = _requested_experiment(client, main_module)
    database = main_module.app_state.db
    approval_id = requested["approval_id"]

    _session_for(client, main_module, REVIEWER_ID)
    before = _counts(database)
    rejected = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "experiment-reject"},
        json={"decision": "reject", "note": "not now"},
    )
    after = _counts(database)

    assert rejected.status_code == 202, rejected.json()
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["job_ids"] == []
    assert after["jobs"] == before["jobs"]

    detail = client.get(f"/api/v2/experiments/{requested['experiment_id']}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["status"] == "rejected"


def test_get_list_and_detail_projections_include_metrics_status(api_client):
    client, main_module = api_client
    seed, requested = _requested_experiment(client, main_module)
    database = main_module.app_state.db
    approval_id = requested["approval_id"]

    _session_for(client, main_module, REVIEWER_ID)
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "experiment-approve-metrics"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert approved.status_code == 202, approved.json()
    job_ids = approved.json()["job_ids"]

    _session_for(client, main_module, OPERATOR_ID)
    listed = client.get(f"/api/v2/experiments?project_id={seed['project_id']}")
    assert listed.status_code == 200, listed.json()
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["experiment_id"] == requested["experiment_id"]
    assert items[0]["run_count"] == 4
    assert len(items[0]["members"]) == 4
    for member in items[0]["members"]:
        assert member["job_id"] in job_ids
        assert member["canonical_job_status"] == "queued"
        assert member["collection_state"] == "pending"
        # No metrics-v1 collection has run yet for a freshly queued job.
        assert member["metrics_status"] == "unknown"
        assert member["metrics_summary"] is None
        assert member["parameter_values"]["epochs"] in (10, 20)
        assert member["server_name"] in ("pilot-a", "pilot-b")

    detail = client.get(f"/api/v2/experiments/{requested['experiment_id']}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["members"] == items[0]["members"]

    # Simulate a metrics-v1 collection result for one member and confirm the
    # projection reflects it without touching any other member.
    database.replace_run_metrics(
        job_ids[0],
        entries=[],
        status="collected",
        reason=None,
        source_sha256="a" * 64,
    )
    detail_after = client.get(f"/api/v2/experiments/{requested['experiment_id']}")
    assert detail_after.status_code == 200, detail_after.json()
    by_job = {m["job_id"]: m for m in detail_after.json()["members"]}
    assert by_job[job_ids[0]]["metrics_status"] == "collected"
    assert by_job[job_ids[0]]["metrics_summary"]["status"] == "collected"
    for other_job_id in job_ids[1:]:
        assert by_job[other_job_id]["metrics_status"] == "unknown"


def test_authorization_unauthenticated_and_unauthorized_rejected(api_client):
    client, main_module = api_client
    _enable_experiment_v2(main_module)
    seed = _seed_experiment_http_context(main_module)
    database = main_module.app_state.db
    body = _experiment_request_body(seed, target_servers=["pilot-a", "pilot-b"])

    # No session at all -> authentication required.
    unauthenticated_preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-previews",
        json=body,
    )
    assert unauthenticated_preview.status_code == 401

    # An actor with no role binding on this project is denied opaquely.
    outsider_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=outsider_id,
        actor_type=ActorType.HUMAN,
        display_name="Experiment outsider",
    )
    _session_for(client, main_module, outsider_id)
    outsider_preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/experiment-previews",
        json=body,
    )
    assert outsider_preview.status_code in (403, 404)

    outsider_list = client.get(f"/api/v2/experiments?project_id={seed['project_id']}")
    assert outsider_list.status_code in (403, 404)


def test_http_approved_member_job_passes_pinned_contract_dispatch(api_client):
    """End-to-end: HTTP request -> HTTP approve -> dispatch-time pinned
    contract validation for one materialized member Job."""

    client, main_module = api_client
    seed, requested = _requested_experiment(client, main_module)
    database = main_module.app_state.db
    approval_id = requested["approval_id"]

    _session_for(client, main_module, REVIEWER_ID)
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "experiment-approve-dispatch"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert approved.status_code == 202, approved.json()
    job_id = approved.json()["job_ids"][0]

    with database.cursor() as cursor:
        job = cursor.execute(
            "SELECT pin_server FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    server_name = job["pin_server"]
    server_config_revision_id = seed["revisions"][server_name]["id"]

    lease = database.acquire_scheduler_lease(
        owner_id="experiment-http-dispatch",
        lease_seconds=120,
    )
    attempt = database.create_execution_attempt(
        job_id=job_id,
        backend="ssh",
        server_config_revision_id=server_config_revision_id,
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        initial_operation={
            "operation": "prepare",
            "operation_id": "experiment-http-prepare",
            "idempotency_key": "experiment-http-prepare-key",
            "payload": {"job_id": job_id},
        },
    )
    assert attempt["execution_contract_version"] == "experiment-v2-approval-v1"
