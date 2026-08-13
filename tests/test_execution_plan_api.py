"""WP-3B end-to-end: the plan endpoints, through the real app.

This is where RB-DATASET-001's D-5 ruling actually takes effect. A pure
function that *could* reject a legacy dataset proves nothing; the rejection has
to happen on the request path a user reaches.
"""

from __future__ import annotations

import sqlite3

import pytest



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
    from tests.test_code_promotion import (
        _approve as approve_promotion,
        _request as request_promotion,
        _seed_native_candidate,
    )
    from tests.test_execution_attempt_foundation import _foundation_records

    database = main_module.app_state.db
    records = _foundation_records(database)

    # A real native Engineering Task bundle promoted through WP-3C. This
    # fixture must not bypass Hub verification/publication just to seed a row.
    seed = _seed_native_candidate(database, tmp_path)
    promotion = request_promotion(database, tmp_path, seed)
    version = approve_promotion(
        database, tmp_path, promotion.id
    )["project_version"]

    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO run_profiles (id, project_id, project_name, name,"
            " revision, status, created_at)"
            " VALUES ('rp-1', ?, 'demo', 'default', 1, 'active', 'now')",
            (seed.project_id,),
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
    persisted = main_module.app_state.db.get_execution_plan(plan_id)
    assert persisted["request_approval_id"] == approval_id
    plan_events = [
        event
        for event in main_module.app_state.db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_plan_materialized"
        and event["resource_id"] == plan_id
    ]
    assert len(plan_events) == 1
    assert plan_events[0]["result"] == "pending_approval"
    assert plan_events[0]["approval_id"] == approval_id
    assert plan_events[0]["params"] == {
        "command_sha256": persisted["command_sha256"],
        "contract_version": "execution-plan-v1",
        "dataset_bound": True,
        "project_version_bound": True,
        "reproducible": True,
        "run_profile_bound": True,
        "server_revision_bound": True,
    }

    pending_view = client.get(f"/runs/{plan_id}")
    assert pending_view.status_code == 200
    assert pending_view.json()["approval"]["status"] == "pending"
    assert pending_view.json()["job"] is None
    assert pending_view.json()["attempts"] == []

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


def test_plan_and_request_approval_rollback_as_one_transaction(
    api_client, tmp_path
):
    """A failure after approval insert must not leave an orphan approval."""
    from app.execution_plan import PlanInputs, derive_plan_draft

    _, main_module = api_client
    records, version = _ready_plan(main_module, tmp_path)
    database = main_module.app_state.db
    inputs = PlanInputs(
        project_name="demo",
        command="python train.py",
        project_version_id=version["id"],
        run_profile_id="rp-1",
        dataset_none=True,
        server_config_revision_id=records["revision"]["id"],
    )
    draft = derive_plan_draft(
        inputs, database.resolve_execution_plan_inputs(inputs)
    )
    before = _counts(main_module)
    with database.cursor() as cursor:
        cursor.execute(
            """
            CREATE TRIGGER injected_plan_insert_failure
            BEFORE INSERT ON execution_plans
            BEGIN SELECT RAISE(ABORT, 'injected plan insert failure'); END
            """
        )
    try:
        with pytest.raises(
            sqlite3.IntegrityError, match="injected plan insert failure"
        ):
            database.insert_execution_plan_request(
                draft=draft,
                command=inputs.command,
            )
    finally:
        with database.cursor() as cursor:
            cursor.execute("DROP TRIGGER injected_plan_insert_failure")
    assert _counts(main_module) == before


def test_plan_and_request_rolls_back_when_plan_audit_append_fails(
    api_client, tmp_path, monkeypatch
):
    from app.execution_plan import PlanInputs, derive_plan_draft

    _, main_module = api_client
    records, version = _ready_plan(main_module, tmp_path)
    database = main_module.app_state.db
    inputs = PlanInputs(
        project_name="demo",
        command="python train.py",
        project_version_id=version["id"],
        run_profile_id="rp-1",
        dataset_none=True,
        server_config_revision_id=records["revision"]["id"],
    )
    draft = derive_plan_draft(
        inputs, database.resolve_execution_plan_inputs(inputs)
    )
    before = _counts(main_module)
    original_append = database.append_durable_audit_event_in_transaction

    def fail_plan_event(cursor, **kwargs):
        if kwargs.get("action") == "execution_plan_materialized":
            raise RuntimeError("injected execution plan audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_plan_event
    )
    with pytest.raises(RuntimeError, match="injected execution plan audit failure"):
        database.insert_execution_plan_request(
            draft=draft,
            command=inputs.command,
        )

    assert _counts(main_module) == before
    assert not any(
        event["action"] == "execution_plan_materialized"
        for event in database.list_durable_audit_events(limit=100)
    )


def test_run_view_returns_job_attempt_operation_event_and_honest_results(
    api_client, tmp_path
):
    import asyncio

    from app.approvals import approve

    client, main_module = api_client
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
    approved = asyncio.run(
        approve(
            database,
            body["approval_id"],
            app_state=main_module.app_state,
            audit_path=str(tmp_path / "audit.jsonl"),
        )
    )
    attempt = database.create_execution_attempt(
        job_id=approved["job_id"],
        backend="ssh",
        server_config_revision_id=records["revision"]["id"],
        leader_owner_id=records["lease"]["owner_id"],
        scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        initial_operation={
            "operation": "prepare",
            "operation_id": "prepare-lineage",
            "idempotency_key": "prepare-lineage-key",
            "payload": {"job_id": approved["job_id"]},
        },
    )

    response = client.get(f"/runs/{body['plan']['id']}")

    assert response.status_code == 200
    lineage = response.json()
    assert lineage["plan"]["request_approval_id"] == body["approval_id"]
    assert lineage["approval"]["status"] == "approved"
    assert lineage["approval"]["payload"]["plan_id"] == body["plan"]["id"]
    assert lineage["job"]["id"] == approved["job_id"]
    assert lineage["job"]["status"] == "running"
    assert [item["attempt"]["id"] for item in lineage["attempts"]] == [
        attempt["id"]
    ]
    assert lineage["attempts"][0]["operations"][0]["operation"] == "prepare"
    assert lineage["attempts"][0]["operations"][0]["payload"] == {
        "job_id": approved["job_id"]
    }
    assert {
        event["event_type"] for event in lineage["attempts"][0]["events"]
    } == {"attempt_created", "operation_created"}
    assert lineage["attempts"][0]["events"][0]["evidence"] is not None
    assert lineage["results"] == {
        "job_status": "running",
        "exit_code": None,
        "finished_at": None,
        "log_tail": None,
        "collection_operations": [],
        "node_artifacts": [],
        "node_artifact_evidence": "not_recorded",
    }


def test_plan_request_approval_linkage_cannot_be_rewritten(
    api_client, tmp_path
):
    client, main_module = api_client
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

    with pytest.raises(sqlite3.IntegrityError, match="linkage is immutable"):
        with main_module.app_state.db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE execution_plans
                SET request_approval_id = NULL
                WHERE id = ?
                """,
                (body["plan"]["id"],),
            )


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


def test_preexisting_plan_for_legacy_predecessor_rejects_after_adoption(
    api_client,
    tmp_path,
):
    """A pending v1 plan may not outlive explicit Product v2 adoption."""
    import asyncio

    from app.approvals import approve
    from app.identity import ActorType, ProjectRoleV2
    from tests.test_run_templates_v2 import (
        OPERATOR_ID,
        OWNER_ID,
        REVIEWER_ID,
        _compiler_template,
        _create_environment,
        _insert_binding,
        _request_template,
    )

    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    records, version = _ready_plan(main_module, tmp_path)
    database = main_module.app_state.db
    project = database.get_project("demo")
    legacy = database.insert_run_profile_revision(
        project_id=project.id,
        project_name=project.name,
        name="adopt-after-plan",
        status="approved",
        command="python train.py",
        setup_cmd=None,
        require_tag=None,
        supersedes_id=None,
        approval_id=None,
        created_by_actor_id=None,
    )
    body = client.post(
        "/projects/demo/runs/request",
        json={
            "command": "python train.py",
            "project_version_id": version["id"],
            "run_profile_id": legacy.id,
            "dataset_none": True,
            "server_config_revision_id": records["revision"]["id"],
        },
    ).json()

    for actor_id, display_name in (
        (OWNER_ID, "Plan owner"),
        (REVIEWER_ID, "Plan reviewer"),
        (OPERATOR_ID, "Plan operator"),
    ):
        database.insert_actor(
            actor_id=actor_id,
            actor_type=ActorType.HUMAN,
            display_name=display_name,
        )
    _insert_binding(
        database,
        project_id=project.id,
        actor_id=OWNER_ID,
        role=ProjectRoleV2.OWNER,
    )
    _insert_binding(
        database,
        project_id=project.id,
        actor_id=REVIEWER_ID,
        role=ProjectRoleV2.REVIEWER,
    )
    _insert_binding(
        database,
        project_id=project.id,
        actor_id=OPERATOR_ID,
        role=ProjectRoleV2.OPERATOR,
    )
    environment = _create_environment(database, project.id)
    adoption_id = _request_template(
        database,
        project_id=project.id,
        environment_revision_id=environment["environment_revision_id"],
        operation="adopt",
        expected_revision=1,
        expected_head_run_profile_id=legacy.id,
        expected_head_classification="legacy_raw_command",
        template=_compiler_template().model_copy(
            update={"name": legacy.name}
        ),
    )
    database.apply_run_template_change_decision(
        approval_id=adoption_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )

    result = asyncio.run(
        approve(
            database,
            body["approval_id"],
            app_state=main_module.app_state,
            audit_path=str(tmp_path / "audit.jsonl"),
        )
    )
    assert result["approval"].status == "rejected"
    assert "job_id" not in result
    assert database.get_execution_plan(body["plan"]["id"])["job_id"] is None
