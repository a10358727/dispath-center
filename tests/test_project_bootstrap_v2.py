"""PR-04 Project bootstrap contract, transaction, API, and privacy tests."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.authentication import ensure_legacy_admin_actor
from app.authorization import Action
from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, Database
from app.execution_plan import PlanInputs, derive_plan_draft
from app.identity import ActorType, generate_service_token, generate_session_token
from app.project_bootstrap import (
    PROJECT_BOOTSTRAP_CONTRACT_VERSION,
    ProjectBootstrapPreviewRequest,
    bootstrap_payload_digest,
    build_bootstrap_payload,
    parse_bootstrap_payload,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


REQUESTER_ID = "20000000-0000-0000-0000-000000000041"
DECIDER_ID = "20000000-0000-0000-0000-000000000042"
OUTSIDER_ID = "20000000-0000-0000-0000-000000000043"
PROJECT_ID = "10000000-0000-0000-0000-000000000041"


def _preview_body(*, source: str = "/srv/projects/bootstrap-alpha") -> dict:
    return {
        "project_id": PROJECT_ID,
        "name": "bootstrap-alpha",
        "slug": "bootstrap-alpha",
        "source": {"kind": "local_path", "reference": source},
        "role_assignments": [
            {
                "actor_id": REQUESTER_ID,
                "roles": ["owner", "operator", "dataset_manager"],
            },
            {"actor_id": DECIDER_ID, "roles": ["reviewer"]},
        ],
        "environment": {
            "name": "host-117",
            "setup_command": "python -m venv .venv",
            "required_server_tags": ["gpu", "pilot-117"],
            "working_directory_policy": "project_checkout",
            "non_secret_env": ["CUDA_VISIBLE_DEVICES"],
            "secret_references": ["MODEL_TOKEN"],
            "preflight_checks": [
                {"kind": "executable_present", "name": "python"},
                {"kind": "server_tag_present", "name": "gpu"},
                {
                    "kind": "non_secret_env_present",
                    "name": "CUDA_VISIBLE_DEVICES",
                },
                {"kind": "secret_reference_present", "name": "MODEL_TOKEN"},
                {"kind": "project_relative_path_present", "path": "train.py"},
            ],
        },
        "run_template": {
            "name": "train-model",
            "argv_template": [
                {"kind": "literal", "value": "python"},
                {"kind": "literal", "value": "train.py"},
                {"kind": "literal", "value": "--epochs"},
                {"kind": "parameter", "name": "epochs"},
            ],
            "parameter_schema": [
                {
                    "name": "epochs",
                    "type": "integer",
                    "required": True,
                    "minimum": 1,
                    "maximum": 100,
                }
            ],
            "resource_requirements": {
                "required_tags": ["gpu", "pilot-117"],
                "min_gpu_count": 1,
                "min_gpu_memory_mb": 8192,
                "min_available_ram_mb": 16384,
                "min_available_disk_mb": 10240,
                "exclusive_worker": True,
            },
            "output_declarations": [
                {
                    "name": "metrics",
                    "kind": "file",
                    "path_pattern": "metrics.json",
                    "required": True,
                }
            ],
        },
        "defaults": {"parameter_values": {"epochs": 10}},
        "dataset_grants": [],
        "dataset_aliases": [],
    }


def _seed_admins(database: Database):
    requester = database.insert_actor(
        actor_id=REQUESTER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Bootstrap requester",
        platform_admin=True,
    )
    decider = database.insert_actor(
        actor_id=DECIDER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Bootstrap decider",
        platform_admin=True,
    )
    return requester, decider


def _session_for(client, main_module, actor_id: str) -> None:
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(
        main_module.app_state.config.session_cookie_name,
        issued.raw_token,
    )


def _enable_bootstrap(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_bootstrap_v2_enabled = True
    config.authorization_mode = "enforce"


def _build_payload(body: dict | None = None):
    return build_bootstrap_payload(
        ProjectBootstrapPreviewRequest.model_validate(body or _preview_body())
    )


def _create_approval(database: Database, payload) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_project_bootstrap_approval_in_transaction(
                cursor,
                payload=payload,
                requester_actor_id=REQUESTER_ID,
            )
        )


def _table_counts(database: Database) -> dict[str, int]:
    tables = (
        "projects",
        "project_role_bindings",
        "project_environments",
        "environment_revisions",
        "run_profiles",
        "run_profile_specs",
        "project_default_revisions",
    )
    with database.cursor() as cursor:
        return {
            table: int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_preview_contract_is_canonical_typed_and_credential_free():
    generated = iter(
        uuid.UUID(f"30000000-0000-0000-0000-{index:012d}")
        for index in range(1, 20)
    )
    preview = ProjectBootstrapPreviewRequest.model_validate(_preview_body())
    payload = build_bootstrap_payload(preview, uuid_factory=lambda: next(generated))
    serialized = payload.model_dump(mode="json")

    assert payload.contract_version == PROJECT_BOOTSTRAP_CONTRACT_VERSION
    assert payload.environment.revision == 1
    assert payload.run_template.environment_revision_id == (
        payload.environment.revision_id
    )
    assert payload.defaults.run_profile_id == payload.run_template.run_profile_id
    assert payload.defaults.run_profile_spec_digest == payload.run_template.spec_digest
    assert payload.dataset_grants == []
    assert payload.dataset_aliases == []
    assert parse_bootstrap_payload(serialized) == payload
    assert bootstrap_payload_digest(payload) == bootstrap_payload_digest(
        parse_bootstrap_payload(serialized)
    )
    assert "MODEL_TOKEN" in json.dumps(serialized)
    assert "secret_value" not in json.dumps(serialized)

    credentialed = _preview_body()
    credentialed["source"] = {
        "kind": "repo",
        "reference": "https://user:password@example.test/private.git",
    }
    with pytest.raises(ValidationError, match="credential-free"):
        ProjectBootstrapPreviewRequest.model_validate(credentialed)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda body: body["run_template"]["output_declarations"][0].update(
                {"path_pattern": "../escape"}
            ),
            "declared root",
        ),
        (
            lambda body: body["run_template"]["parameter_schema"][0].update(
                {"type": "object"}
            ),
            "Input should be",
        ),
        (
            lambda body: body["defaults"].update(
                {"parameter_values": {"undeclared": "value"}}
            ),
            "undeclared",
        ),
        (
            lambda body: body["environment"]["secret_references"].__setitem__(
                0, "MODEL_TOKEN=raw-secret"
            ),
            "uppercase environment name",
        ),
        (
            lambda body: body["environment"].update(
                {"setup_command": "界" * 1366}
            ),
            "4096-byte storage limit",
        ),
    ],
)
def test_bootstrap_contract_rejects_untyped_or_secret_bearing_inputs(mutator, message):
    body = _preview_body()
    mutator(body)
    with pytest.raises(ValidationError, match=message):
        ProjectBootstrapPreviewRequest.model_validate(body)


def test_bootstrap_materializes_exact_resources_and_safe_workspace(db, tmp_path):
    _seed_admins(db)
    source = tmp_path / "must-remain-absent"
    payload = _build_payload(_preview_body(source=str(source)))
    approval_id = _create_approval(db, payload)

    result = db.apply_project_bootstrap_decision(
        approval_id=approval_id,
        decision_actor_id=DECIDER_ID,
        decision_mechanism="test_session",
        note="independent review",
    )

    assert result == {
        "approval_id": approval_id,
        "project_id": payload.project.id,
        "environment_revision_id": payload.environment.revision_id,
        "run_profile_id": payload.run_template.run_profile_id,
        "defaults_revision_id": payload.defaults.revision_id,
        "payload_digest": bootstrap_payload_digest(payload),
    }
    assert db.get_approval(approval_id).status == "approved"
    assert _table_counts(db) == {
        "projects": 1,
        "project_role_bindings": 4,
        "project_environments": 1,
        "environment_revisions": 1,
        "run_profiles": 1,
        "run_profile_specs": 1,
        "project_default_revisions": 1,
    }
    assert db.run_profile_has_typed_spec(payload.run_template.run_profile_id)
    legacy_plan_inputs = PlanInputs(
        project_name=payload.project.name,
        command="python train.py",
        run_profile_id=payload.run_template.run_profile_id,
        dataset_none=True,
        require_reproducible=False,
    )
    legacy_draft = derive_plan_draft(
        legacy_plan_inputs,
        db.resolve_execution_plan_inputs(legacy_plan_inputs),
    )
    assert "run_profile_requires_v2_compiler" in legacy_draft.reason_codes
    assert legacy_draft.plan_digest is None
    workspace = db.get_project_workspace_v2(payload.project.id)
    assert workspace is not None
    assert workspace["rbac"]["state"] == "ready"
    assert workspace["run_template"]["classification"] == "typed_spec"
    assert workspace["defaults"]["parameter_names"] == ["epochs"]
    serialized = json.dumps(workspace, sort_keys=True)
    assert str(source) not in serialized
    assert "MODEL_TOKEN" not in serialized
    assert "python -m venv" not in serialized
    assert "metrics.json" not in serialized
    assert not source.exists()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE environment_revisions SET status = 'archived' WHERE id = ?",
                (payload.environment.revision_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM run_profile_specs WHERE run_profile_id = ?",
                (payload.run_template.run_profile_id,),
            )

    actions = [
        event["action"]
        for event in reversed(db.list_durable_audit_events(limit=100))
        if event.get("approval_id") == approval_id
    ]
    assert actions == [
        "approval_created",
        "project_created",
        "membership_granted",
        "membership_granted",
        "membership_granted",
        "membership_granted",
        "environment_revision_created",
        "run_profile_spec_created",
        "project_defaults_revision_created",
        "project_bootstrap_materialized",
        "approval_decided",
    ]


def test_v7_rejects_cross_project_specs_and_defaults_at_the_database_boundary(db):
    _seed_admins(db)
    payload_a = _build_payload()
    approval_a = _create_approval(db, payload_a)
    db.apply_project_bootstrap_decision(
        approval_id=approval_a,
        decision_actor_id=DECIDER_ID,
        decision_mechanism="test_session",
    )
    body_b = _preview_body(source="/srv/projects/bootstrap-beta")
    body_b["project_id"] = "10000000-0000-0000-0000-000000000042"
    body_b["name"] = "bootstrap-beta"
    body_b["slug"] = "bootstrap-beta"
    payload_b = _build_payload(body_b)
    approval_b = _create_approval(db, payload_b)
    db.apply_project_bootstrap_decision(
        approval_id=approval_b,
        decision_actor_id=DECIDER_ID,
        decision_mechanism="test_session",
    )

    raw_profile_id = "72000000-0000-0000-0000-000000000001"
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO run_profiles (
                id, project_id, project_name, name, revision, status,
                command, setup_cmd, require_tag, supersedes_id,
                approval_id, created_by_actor_id, created_at
            ) VALUES (?, ?, ?, 'exact-ref-test', 1, 'approved', NULL, NULL,
                      NULL, NULL, ?, ?, '2026-08-07T00:00:00Z')
            """,
            (
                raw_profile_id,
                payload_a.project.id,
                payload_a.project.name,
                approval_a,
                DECIDER_ID,
            ),
        )

    spec_values = (
        raw_profile_id,
        payload_a.project.id,
        payload_b.environment.revision_id,
        "c" * 64,
        approval_a,
        DECIDER_ID,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO run_profile_specs (
                    run_profile_id, project_id, contract_version,
                    environment_revision_id, argv_template_json,
                    parameter_schema_json, resource_requirements_json,
                    output_declarations_json, spec_digest, approval_id,
                    created_by_actor_id, created_at
                ) VALUES (?, ?, 'run-template-spec-v2', ?, '[]', '[]', '{}',
                          '[]', ?, ?, ?, '2026-08-07T00:00:00Z')
                """,
                spec_values,
            )

    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO run_profile_specs (
                run_profile_id, project_id, contract_version,
                environment_revision_id, argv_template_json,
                parameter_schema_json, resource_requirements_json,
                output_declarations_json, spec_digest, approval_id,
                created_by_actor_id, created_at
            ) VALUES (?, ?, 'run-template-spec-v2', ?, '[]', '[]', '{}',
                      '[]', ?, ?, ?, '2026-08-07T00:00:00Z')
            """,
            (
                raw_profile_id,
                payload_a.project.id,
                payload_a.environment.revision_id,
                "c" * 64,
                approval_a,
                DECIDER_ID,
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO project_default_revisions (
                    id, project_id, revision, contract_version,
                    environment_revision_id, run_profile_id,
                    run_profile_spec_digest, parameter_values_json,
                    revision_digest, supersedes_id, approval_id,
                    created_by_actor_id, created_at
                ) VALUES (?, ?, 2, 'project-defaults-v1', ?, ?, ?, '{}', ?, ?,
                          ?, ?, '2026-08-07T00:00:00Z')
                """,
                (
                    "72000000-0000-0000-0000-000000000002",
                    payload_a.project.id,
                    payload_a.environment.revision_id,
                    payload_b.run_template.run_profile_id,
                    payload_b.run_template.spec_digest,
                    "d" * 64,
                    payload_a.defaults.revision_id,
                    approval_a,
                    DECIDER_ID,
                ),
            )

    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_default_revisions (
                id, project_id, revision, contract_version,
                environment_revision_id, run_profile_id,
                run_profile_spec_digest, parameter_values_json,
                revision_digest, supersedes_id, approval_id,
                created_by_actor_id, created_at
            ) VALUES (?, ?, 2, 'project-defaults-v1', ?, ?, ?, '{}', ?, ?, ?, ?,
                      '2026-08-07T00:00:00Z')
            """,
            (
                "72000000-0000-0000-0000-000000000003",
                payload_a.project.id,
                payload_a.environment.revision_id,
                raw_profile_id,
                "c" * 64,
                "e" * 64,
                payload_a.defaults.revision_id,
                approval_a,
                DECIDER_ID,
            ),
        )
    assert db.get_project_workspace_v2(payload_a.project.id) is not None


@pytest.mark.parametrize(
    "failed_action",
    [
        "project_created",
        "membership_granted",
        "environment_revision_created",
        "run_profile_spec_created",
        "project_defaults_revision_created",
        "project_bootstrap_materialized",
        "approval_decided",
    ],
)
def test_every_materialization_step_and_audit_failure_rolls_back_atomically(
    db,
    monkeypatch,
    failed_action,
):
    _seed_admins(db)
    payload = _build_payload()
    approval_id = _create_approval(db, payload)
    event_count = db.count_durable_audit_events()
    original = db.append_durable_audit_event_in_transaction

    def fail_selected(cursor, **kwargs):
        if kwargs.get("action") == failed_action:
            raise RuntimeError(f"injected {failed_action} failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(
        db,
        "append_durable_audit_event_in_transaction",
        fail_selected,
    )
    with pytest.raises(RuntimeError, match=f"injected {failed_action} failure"):
        db.apply_project_bootstrap_decision(
            approval_id=approval_id,
            decision_actor_id=DECIDER_ID,
            decision_mechanism="test_session",
        )

    assert db.get_approval(approval_id).status == "pending"
    assert _table_counts(db) == {
        "projects": 0,
        "project_role_bindings": 0,
        "project_environments": 0,
        "environment_revisions": 0,
        "run_profiles": 0,
        "run_profile_specs": 0,
        "project_default_revisions": 0,
    }
    assert db.count_durable_audit_events() == event_count


def test_bootstrap_revalidates_conflicts_actor_state_and_self_decision(db):
    _seed_admins(db)
    payload = _build_payload()
    approval_id = _create_approval(db, payload)

    with pytest.raises(ValueError, match="high_risk_self_decision"):
        db.apply_project_bootstrap_decision(
            approval_id=approval_id,
            decision_actor_id=REQUESTER_ID,
            decision_mechanism="test_session",
        )
    db.insert_project(payload.project.name, "/srv/existing")
    with pytest.raises(ValueError, match="project_name_conflict"):
        db.apply_project_bootstrap_decision(
            approval_id=approval_id,
            decision_actor_id=DECIDER_ID,
            decision_mechanism="test_session",
        )
    assert db.get_approval(approval_id).status == "pending"
    assert _table_counts(db)["project_environments"] == 0

    other_payload = _build_payload(
        {**_preview_body(), "project_id": "10000000-0000-0000-0000-000000000099", "name": "other-project", "slug": "other-project"}
    )
    other_approval_id = _create_approval(db, other_payload)
    db.update_actor(REQUESTER_ID, disabled_at="2026-08-07T00:00:00+00:00")
    with pytest.raises(ValueError, match="requester must be an enabled human"):
        db.apply_project_bootstrap_decision(
            approval_id=other_approval_id,
            decision_actor_id=DECIDER_ID,
            decision_mechanism="test_session",
        )
    assert db.get_approval(other_approval_id).status == "pending"


def test_bootstrap_allows_self_decision_when_deployment_policy_enables_it(db):
    _seed_admins(db)
    payload = _build_payload()
    approval_id = _create_approval(db, payload)
    db.allow_high_risk_self_approval = True

    result = db.apply_project_bootstrap_decision(
        approval_id=approval_id,
        decision_actor_id=REQUESTER_ID,
        decision_mechanism="test_session",
    )

    assert result["approval_id"] == approval_id
    assert db.get_approval(approval_id).status == "approved"
    assert db.get_approval(approval_id).decision_actor_id == REQUESTER_ID


def test_bootstrap_reject_and_tamper_paths_preserve_materialization_boundary(db):
    _seed_admins(db)
    rejected_payload = _build_payload()
    rejected_id = _create_approval(db, rejected_payload)

    db.reject_project_bootstrap_decision(
        approval_id=rejected_id,
        decision_actor_id=DECIDER_ID,
        decision_mechanism="test_session",
        note="not approved",
    )

    assert db.get_approval(rejected_id).status == "rejected"
    assert all(count == 0 for count in _table_counts(db).values())
    rejected_actions = {
        event["action"]
        for event in db.list_durable_audit_events(limit=100)
        if event.get("approval_id") == rejected_id
    }
    assert rejected_actions == {"approval_created", "approval_decided"}

    tampered_body = _preview_body()
    tampered_body.update(
        {
            "project_id": "10000000-0000-0000-0000-000000000098",
            "name": "tamper-target",
            "slug": "tamper-target",
        }
    )
    tampered_id = _create_approval(db, _build_payload(tampered_body))
    with db.cursor() as cursor:
        cursor.execute("DROP TRIGGER approvals_execution_pin_update_guard")
        cursor.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            ('{"tampered":true}', tampered_id),
        )
    with pytest.raises(ValueError, match="payload digest mismatch"):
        db.apply_project_bootstrap_decision(
            approval_id=tampered_id,
            decision_actor_id=DECIDER_ID,
            decision_mechanism="test_session",
        )
    assert db.get_approval(tampered_id).status == "pending"
    assert all(count == 0 for count in _table_counts(db).values())


@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled", "bootstrap_enabled"),
    [
        (False, False, False),
        (True, False, True),
        (True, True, False),
    ],
)
def test_bootstrap_routes_are_opaque_until_all_flags_are_enabled(
    api_client,
    api_enabled,
    rbac_enabled,
    bootstrap_enabled,
):
    client, main_module = api_client
    config = main_module.app_state.config
    config.api_v2_enabled = api_enabled
    config.product_rbac_v2_enabled = rbac_enabled
    config.project_bootstrap_v2_enabled = bootstrap_enabled

    response = client.post("/api/v2/projects/bootstrap-previews", json=_preview_body())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_bootstrap_actor_boundary_rejects_non_admin_service_and_legacy(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    _enable_bootstrap(main_module)
    ordinary = database.insert_actor(
        actor_id=OUTSIDER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Ordinary human",
    )
    _session_for(client, main_module, ordinary.id)
    human_response = client.post(
        "/api/v2/projects/bootstrap-previews",
        json=_preview_body(),
    )
    assert human_response.status_code == 403

    service = database.insert_actor(
        actor_id="20000000-0000-0000-0000-000000000044",
        actor_type=ActorType.SERVICE,
        display_name="Automation",
        platform_admin=True,
    )
    database.insert_service_account(actor_id=service.id, name="bootstrap-service")
    token = generate_service_token()
    database.insert_service_account_token(
        token_id=token.id,
        service_account_actor_id=service.id,
        secret_hash=token.secret_hash,
        scopes=[Action.PLATFORM_MANAGE.value],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.service_token_auth_enabled = True
    client.cookies.clear()
    service_response = client.post(
        "/api/v2/projects/bootstrap-previews",
        json=_preview_body(),
        headers={"Authorization": f"Bearer {token.raw_token}"},
    )
    assert service_response.status_code == 403

    main_module.app_state.config.auth_token = "approved-rollback-token"
    ensure_legacy_admin_actor(database)
    client.cookies.clear()
    legacy_response = client.post(
        "/api/v2/projects/bootstrap-previews",
        json=_preview_body(),
        headers={"X-Auth-Token": "approved-rollback-token"},
    )
    assert legacy_response.status_code == 403
    assert database.list_approvals(kind="project_bootstrap_v2") == []


def test_bootstrap_decision_is_hidden_when_package_flag_is_rolled_back(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    approval_id = _create_approval(database, _build_payload())
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_bootstrap_v2_enabled = False
    config.authorization_mode = "enforce"
    _session_for(client, main_module, DECIDER_ID)

    response = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "hidden-bootstrap-decision"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert database.get_approval(approval_id).status == "pending"
    assert all(count == 0 for count in _table_counts(database).values())
    assert client.get(f"/api/v2/approvals/{approval_id}").status_code == 404


def test_product_approval_list_is_safe_and_detail_requires_authorized_review(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    payload = _build_payload()
    approval_id = _create_approval(database, payload)
    _enable_bootstrap(main_module)
    _session_for(client, main_module, DECIDER_ID)

    listed = client.get("/api/v2/approvals?status=pending&limit=1")

    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.headers["Pragma"] == "no-cache"
    assert listed.json()["next_cursor"] is None
    assert listed.json()["items"] == [
        {
            "id": approval_id,
            "kind": "project_bootstrap_v2",
            "status": "pending",
            "project_id": None,
            "created_at": listed.json()["items"][0]["created_at"],
            "decided_at": None,
            #: DG-UI-UNIFICATION v1 U6b: `note` was added to the list summary
            #: so the AgentSession checkpoint->promote bridge can read
            #: `task_id=<uuid>` off an approved `agent_session_checkpoint`
            #: approval without a second backend read surface (see
            #: `dispatch_center/api/routers/approvals_v2.py::_safe_summary`).
            "note": None,
            "requester_is_self": False,
            "can_decide": True,
            "decision_reason": "allowed_platform_admin",
            "payload_digest": bootstrap_payload_digest(payload),
            "payload_contract_version": PROJECT_BOOTSTRAP_CONTRACT_VERSION,
        }
    ]
    assert "payload" not in listed.json()["items"][0]

    detail = client.get(f"/api/v2/approvals/{approval_id}")

    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert detail.headers["Pragma"] == "no-cache"
    assert detail.json()["payload"] == payload.model_dump(mode="json")
    assert detail.json()["payload_digest"] == bootstrap_payload_digest(payload)
    assert detail.json()["payload_contract_version"] == (
        PROJECT_BOOTSTRAP_CONTRACT_VERSION
    )
    assert detail.json()["payload_verified"] is True
    assert detail.json()["can_decide"] is True
    assert "review" not in detail.json()

    outsider = database.insert_actor(
        actor_id=OUTSIDER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Opaque outsider",
    )
    _session_for(client, main_module, outsider.id)
    hidden = client.get(f"/api/v2/approvals/{approval_id}")
    hidden_list = client.get("/api/v2/approvals?status=pending")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"
    assert hidden.headers["Cache-Control"] == "no-store"
    assert hidden_list.status_code == 200
    assert hidden_list.json() == {"items": [], "next_cursor": None}


def test_product_approval_detail_fails_closed_on_payload_tamper(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    payload = _build_payload()
    approval_id = _create_approval(database, payload)
    _enable_bootstrap(main_module)
    _session_for(client, main_module, DECIDER_ID)
    tampered = payload.model_dump(mode="json")
    tampered["project"]["name"] = "tampered-bootstrap"
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER approvals_execution_pin_update_guard")
        cursor.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            (json.dumps(tampered, sort_keys=True), approval_id),
        )

    detail = client.get(f"/api/v2/approvals/{approval_id}")
    listed = client.get("/api/v2/approvals?status=pending")

    assert detail.status_code == 409
    assert detail.json()["error"]["code"] == "approval_contract_invalid"
    assert "payload" not in json.dumps(detail.json())
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE approvals SET payload = '{' WHERE id = ?",
            (approval_id,),
        )
    malformed = client.get(f"/api/v2/approvals/{approval_id}")
    assert malformed.status_code == 404
    assert malformed.json()["error"]["code"] == "not_found"


def test_bootstrap_http_preview_request_decision_replay_and_workspace_privacy(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    outsider = database.insert_actor(
        actor_id=OUTSIDER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Outsider",
    )
    _enable_bootstrap(main_module)
    _session_for(client, main_module, REQUESTER_ID)
    before = _table_counts(database)
    event_count = database.count_durable_audit_events()

    preview = client.post(
        "/api/v2/projects/bootstrap-previews",
        json=_preview_body(),
    )

    assert preview.status_code == 200
    assert preview.headers["Cache-Control"] == "no-store"
    preview_json = preview.json()
    assert preview_json["blocking"] is False
    assert preview_json["readiness"] == {"ready": True, "reasons": []}
    assert preview_json["payload_digest"] == bootstrap_payload_digest(
        parse_bootstrap_payload(preview_json["payload"])
    )
    assert _table_counts(database) == before
    assert database.count_durable_audit_events() == event_count
    assert database.list_approvals(kind="project_bootstrap_v2") == []

    request_body = {
        "payload": preview_json["payload"],
        "expected_payload_digest": preview_json["payload_digest"],
    }
    assert client.post(
        "/api/v2/projects/bootstrap-requests",
        json=request_body,
    ).json()["error"]["code"] == "idempotency_key_required"
    mismatched = client.post(
        "/api/v2/projects/bootstrap-requests",
        json={**request_body, "expected_payload_digest": "a" * 64},
        headers={"Idempotency-Key": "bootstrap-request-mismatch"},
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["error"]["code"] == (
        "bootstrap_payload_digest_mismatch"
    )

    requested = client.post(
        "/api/v2/projects/bootstrap-requests",
        json=request_body,
        headers={"Idempotency-Key": "bootstrap-request-1"},
    )
    assert requested.status_code == 202
    assert requested.json()["replayed"] is False
    approval_id = requested.json()["approval_id"]
    replayed = client.post(
        "/api/v2/projects/bootstrap-requests",
        json=request_body,
        headers={"Idempotency-Key": "bootstrap-request-1"},
    )
    assert replayed.status_code == 202
    assert replayed.json()["approval_id"] == approval_id
    assert replayed.json()["replayed"] is True
    assert _table_counts(database) == before

    own = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "bootstrap-own-decision"},
    )
    assert own.status_code == 403
    assert own.json()["error"]["code"] == "authorization_denied"
    assert own.json()["error"]["details"]["reason"] == (
        "denied_high_risk_self_decision"
    )
    assert database.get_approval(approval_id).status == "pending"

    _session_for(client, main_module, DECIDER_ID)
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "bootstrap-decision-1"},
    )
    assert approved.status_code == 202
    assert approved.json() == {
        "approval_id": approval_id,
        "project_id": PROJECT_ID,
        "replayed": False,
        "status": "approved",
    }
    decision_replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "bootstrap-decision-1"},
    )
    assert decision_replay.status_code == 202
    assert decision_replay.json()["replayed"] is True

    workspace = client.get(f"/api/v2/projects/{PROJECT_ID}/workspace")
    assert workspace.status_code == 200
    assert workspace.json()["project"] == {
        "id": PROJECT_ID,
        "name": "bootstrap-alpha",
        "created_at": workspace.json()["project"]["created_at"],
    }
    serialized = json.dumps(workspace.json(), sort_keys=True)
    assert "/srv/projects/bootstrap-alpha" not in serialized
    assert "MODEL_TOKEN" not in serialized

    _session_for(client, main_module, outsider.id)
    hidden = client.get(f"/api/v2/projects/{PROJECT_ID}/workspace")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"


def test_bootstrap_http_allows_requester_decision_when_policy_is_enabled(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    _enable_bootstrap(main_module)
    main_module.app_state.config.allow_high_risk_self_approval = True
    database.allow_high_risk_self_approval = True
    _session_for(client, main_module, REQUESTER_ID)

    preview = client.post(
        "/api/v2/projects/bootstrap-previews",
        json=_preview_body(),
    )
    request_body = {
        "payload": preview.json()["payload"],
        "expected_payload_digest": preview.json()["payload_digest"],
    }
    requested = client.post(
        "/api/v2/projects/bootstrap-requests",
        json=request_body,
        headers={"Idempotency-Key": "bootstrap-self-request"},
    )
    approval_id = requested.json()["approval_id"]

    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "trusted-team self approval"},
        headers={"Idempotency-Key": "bootstrap-self-decision"},
    )

    assert requested.status_code == 202
    assert approved.status_code == 202
    assert approved.json()["status"] == "approved"
    approval = database.get_approval(approval_id)
    assert approval.requester_actor_id == REQUESTER_ID
    assert approval.decision_actor_id == REQUESTER_ID


def test_dataset_bootstrap_is_explicitly_blocked_without_creating_approval(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    _enable_bootstrap(main_module)
    _session_for(client, main_module, REQUESTER_ID)
    body = _preview_body()
    body["dataset_grants"] = ["40000000-0000-0000-0000-000000000001"]

    preview = client.post("/api/v2/projects/bootstrap-previews", json=body)

    assert preview.status_code == 200
    assert preview.json()["blocking"] is True
    assert preview.json()["findings"] == ["dataset_capability_unavailable"]
    assert preview.json()["payload"] is None
    assert preview.json()["payload_digest"] is None
    blocked_payload = _build_payload(body)
    requested = client.post(
        "/api/v2/projects/bootstrap-requests",
        json={
            "payload": blocked_payload.model_dump(mode="json"),
            "expected_payload_digest": bootstrap_payload_digest(blocked_payload),
        },
        headers={"Idempotency-Key": "dataset-bootstrap-blocked"},
    )
    assert requested.status_code == 409
    assert requested.json()["error"]["code"] == "capability_unavailable"
    assert database.list_approvals(kind="project_bootstrap_v2") == []


def test_dangerous_setup_is_a_request_time_400_and_never_persisted(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    _seed_admins(database)
    _enable_bootstrap(main_module)
    _session_for(client, main_module, REQUESTER_ID)
    body = _preview_body()
    body["environment"]["setup_command"] = "rm -rf /"

    response = client.post("/api/v2/projects/bootstrap-previews", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "dangerous_setup_command"
    assert database.list_approvals(kind="project_bootstrap_v2") == []
    payload = _build_payload(body)
    with pytest.raises(ValueError, match="dangerous_setup_command"):
        _create_approval(database, payload)
    assert database.list_approvals(kind="project_bootstrap_v2") == []


def test_dataset_placeholders_do_not_publish_future_uuid_element_semantics(api_client):
    client, _ = api_client

    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    for schema_name in (
        "ProjectBootstrapPayload",
        "ProjectBootstrapPreviewRequest",
    ):
        properties = schemas[schema_name]["properties"]
        assert properties["dataset_grants"]["items"] == {}
        assert properties["dataset_aliases"]["items"] == {}


def test_legacy_decision_path_cannot_materialize_bootstrap(db):
    assert TRANSACTION_ONLY_APPROVAL_KINDS == {
        "environment_change_v2",
        "project_defaults_change_v2",
        "project_role_change",
        "project_bootstrap_v2",
        "run_template_change_v2",
        "dataset_asset_adoption_v2",
        "dataset_alias_change_v2",
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
        "dataset_publish_v2",
        "execution_plan_v2",
        "project_instance_update_v2",
        "experiment_create_v2",
    }


def test_workspace_frontend_files_are_real_files():
    root = Path(__file__).parents[1]
    assert (root / "static" / "workspace.html").is_file()
    assert (root / "static" / "workspace.js").is_file()
