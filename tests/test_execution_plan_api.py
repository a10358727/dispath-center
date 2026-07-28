"""WP-3B end-to-end: the plan endpoints, through the real app.

This is where RB-DATASET-001's D-5 ruling actually takes effect. A pure
function that *could* reject a legacy dataset proves nothing; the rejection has
to happen on the request path a user reaches.
"""

from __future__ import annotations

import pytest

from app.audit import read_audit


def test_preview_creates_nothing(api_client):
    """A genuine read: the user can ask what would happen without committing."""
    client, main_module = api_client

    before = _counts(main_module)
    response = client.post(
        "/projects/demo/execution-plans/preview",
        json={"command": "python train.py"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert "target_missing" in body["reason_codes"]
    assert body["plan_digest"] is None
    assert _counts(main_module) == before


def _counts(main_module) -> tuple[int, int]:
    with main_module.app_state.db.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM execution_plans")
        plans = cursor.fetchone()["n"]
        cursor.execute("SELECT COUNT(*) AS n FROM approvals")
        approvals = cursor.fetchone()["n"]
    return plans, approvals


def test_preview_reports_every_missing_prerequisite_at_once(api_client):
    client, _ = api_client

    body = client.post(
        "/projects/demo/execution-plans/preview",
        json={"command": "python train.py"},
    ).json()

    assert {
        "project_version_missing",
        "run_profile_missing",
        "dataset_snapshot_not_published",
        "target_missing",
    } <= set(body["reason_codes"])
    assert set(body["missing"]) >= {"project_version", "run_profile", "target"}


def test_run_request_is_rejected_when_the_plan_is_not_ready(api_client):
    """Rejected at request time, never downgraded into a run that claims a
    reproducibility it does not have."""
    client, main_module = api_client
    before = _counts(main_module)

    response = client.post(
        "/projects/demo/runs/request",
        json={"command": "python train.py"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "plan_not_ready"
    assert "target_missing" in detail["reason_codes"]
    # Nothing persisted: no orphan plan, no approval nobody can act on.
    assert _counts(main_module) == before


def test_a_dangerous_command_never_reaches_a_plan(api_client):
    client, main_module = api_client
    before = _counts(main_module)

    body = client.post(
        "/projects/demo/execution-plans/preview",
        json={"command": "rm -rf /"},
    ).json()

    assert "command_dangerous" in body["reason_codes"]
    assert body["plan_digest"] is None
    assert _counts(main_module) == before


def test_run_view_404s_for_an_unknown_plan(api_client):
    client, _ = api_client
    assert client.get("/runs/does-not-exist").status_code == 404


def test_plan_endpoints_are_never_auth_exempt():
    """INV-APPROVAL-5: the middleware protects by default, so a new endpoint is
    only reachable unauthenticated if someone adds it to the exempt set."""
    from app.authorization_catalog import (
        PUBLIC_ROUTE_INTERFACES,
        ROUTE_AUTHORIZATION,
    )

    for interface in (
        ("POST", "/projects/{name}/execution-plans/preview"),
        ("POST", "/projects/{name}/runs/request"),
        ("GET", "/runs/{plan_id}"),
    ):
        assert interface not in PUBLIC_ROUTE_INTERFACES
        assert interface in ROUTE_AUTHORIZATION


def test_the_run_request_is_a_material_action_and_preview_is_not():
    """Preview is a POST only because it takes a body; it creates nothing, so
    it must not carry an operate action."""
    from app.authorization import Action
    from app.authorization_catalog import ROUTE_AUTHORIZATION

    preview = ROUTE_AUTHORIZATION[("POST", "/projects/{name}/execution-plans/preview")]
    request = ROUTE_AUTHORIZATION[("POST", "/projects/{name}/runs/request")]
    assert preview.action is Action.PROJECT_VIEW
    assert request.action is Action.PROJECT_OPERATE


# ---------------------------------------------------------------------------
# Approving a plan creates the Job (Phase 3's last segment)
# ---------------------------------------------------------------------------


def _ready_plan(main_module, tmp_path):
    """Build the four pinned inputs a reproducible plan needs."""
    import hashlib

    from tests.test_execution_attempt_foundation import _foundation_records

    database = main_module.app_state.db
    records = _foundation_records(database)

    # A promoted ProjectVersion (WP-3C).
    bundle_dir = tmp_path / "hub_bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "task-1.bundle").write_bytes(b"bundle")
    promote_id = database.insert_pinned_approval(
        kind="engineering_task_promote",
        contract_version="code-promotion-v1",
        payload={
            "engineering_task_id": "task-1",
            "project_name": "demo",
            "base_project_version_id": None,
            "git_commit": "a" * 40,
            "bundle_sha256": hashlib.sha256(b"bundle").hexdigest(),
        },
    )
    version = database.promote_project_version(
        approval_id=promote_id,
        project_name="demo",
        git_commit="a" * 40,
        bundle_sha256=hashlib.sha256(b"bundle").hexdigest(),
    )

    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO run_profiles (id, project_id, project_name, name,"
            " revision, status, created_at)"
            " VALUES ('rp-1', 'p1', 'demo', 'default', 1, 'active', 'now')"
        )

    return records, version


def test_approving_a_plan_creates_a_queued_job_pinned_to_the_plan(api_client, tmp_path):
    """Phase 3's last segment: an approved plan becomes a real Job, pinned to
    the target the plan chose — the scheduler decides when, never where."""
    import asyncio

    from app.approvals import approve

    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    records, version = _ready_plan(main_module, tmp_path)

    response = client.post(
        "/projects/demo/runs/request",
        json={
            "command": "python train.py",
            "project_version_id": version["id"],
            "run_profile_id": "rp-1",
            "dataset_none": True,
            "server_config_revision_id": records["revision"]["id"],
        },
    )
    assert response.status_code == 200, response.json()
    approval_id = response.json()["approval_id"]
    plan_id = response.json()["plan"]["id"]

    result = asyncio.run(
        approve(
            main_module.app_state.db,
            approval_id,
            app_state=main_module.app_state,
            audit_path=str(tmp_path / "audit.jsonl"),
        )
    )

    job_id = result["job_id"]
    job = main_module.app_state.db.get_job(job_id)
    assert job.status == "queued"
    assert job.command == "python train.py"
    # Pinned to the plan's own target: the scheduler may not re-plan where.
    assert job.pin_server == "compute-a"
    assert main_module.app_state.db.get_execution_plan(plan_id)["job_id"] == job_id


def test_approving_the_same_plan_twice_does_not_create_two_jobs(api_client, tmp_path):
    """A reviewed plan must not become two runs."""
    import asyncio

    from app.approvals import approve

    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    records, version = _ready_plan(main_module, tmp_path)

    body = client.post(
        "/projects/demo/runs/request",
        json={
            "command": "python train.py",
            "project_version_id": version["id"],
            "run_profile_id": "rp-1",
            "dataset_none": True,
            "server_config_revision_id": records["revision"]["id"],
        },
    ).json()

    database = main_module.app_state.db
    first = asyncio.run(
        approve(database, body["approval_id"], app_state=main_module.app_state,
                audit_path=str(tmp_path / "audit.jsonl"))
    )
    second = database.materialize_plan_job(
        plan_id=body["plan"]["id"], approval_id=body["approval_id"]
    )

    assert second["created"] is False
    assert second["job_id"] == first["job_id"]
    # Count only plan-derived jobs: the fixture creates one of its own.
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE execution_approval_id = ?",
            (body["approval_id"],),
        )
        assert cursor.fetchone()["n"] == 1


def test_a_rejected_plan_creates_no_job(api_client, tmp_path):
    """If a bound revision moved while the request sat pending, nothing runs."""
    import asyncio

    from app.approvals import approve

    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    records, version = _ready_plan(main_module, tmp_path)
    database = main_module.app_state.db

    body = client.post(
        "/projects/demo/runs/request",
        json={
            "command": "python train.py",
            "project_version_id": version["id"],
            "run_profile_id": "rp-1",
            "dataset_none": True,
            "server_config_revision_id": records["revision"]["id"],
        },
    ).json()

    # The run profile is archived after the request was made.
    with database.cursor() as cursor:
        cursor.execute("UPDATE run_profiles SET status = 'archived' WHERE id = 'rp-1'")

    result = asyncio.run(
        approve(database, body["approval_id"], app_state=main_module.app_state,
                audit_path=str(tmp_path / "audit.jsonl"))
    )

    assert result["approval"].status == "rejected"
    assert "job_id" not in result
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE execution_approval_id = ?",
            (body["approval_id"],),
        )
        assert cursor.fetchone()["n"] == 0
        cursor.execute(
            "SELECT job_id FROM execution_plans WHERE id = ?", (body["plan"]["id"],)
        )
        assert cursor.fetchone()["job_id"] is None
