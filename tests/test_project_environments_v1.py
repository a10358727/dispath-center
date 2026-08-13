"""PR-05 immutable Host Environment contract, transaction, and API tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from app import approvals as approval_module
from app.authorization import Action
from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, Database
from app.identity import (
    ActorType,
    ProjectRoleV2,
    generate_service_token,
    generate_session_token,
)
from app.project_bootstrap import EnvironmentRevisionInput
from app.project_environments import (
    ENVIRONMENT_CHANGE_CONTRACT_VERSION,
    EnvironmentChangeRequest,
    HostObservationEvidence,
    VerifiedHostCandidate,
    build_environment_revision_contract,
    environment_change_payload_digest,
    evaluate_environment_readiness,
    parse_environment_change_payload,
)
from app.server_publication import (
    SERVER_CONFIG_CONTRACT_VERSION,
    build_server_config_contract,
    publish_approved_server_mutation,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


OWNER_ID = "20000000-0000-0000-0000-000000000051"
REVIEWER_ID = "20000000-0000-0000-0000-000000000052"
OPERATOR_ID = "20000000-0000-0000-0000-000000000053"
OUTSIDER_ID = "20000000-0000-0000-0000-000000000054"
SERVICE_ID = "20000000-0000-0000-0000-000000000055"
PLATFORM_ADMIN_ID = "20000000-0000-0000-0000-000000000056"


def _environment_body(
    *,
    name: str = "host-117",
    setup_command: str = "python -m venv .venv",
    tags: list[str] | None = None,
    runtime_checks: bool = False,
) -> dict:
    required_tags = tags if tags is not None else ["gpu", "pilot-117"]
    checks: list[dict[str, str]] = [
        {"kind": "server_tag_present", "name": tag}
        for tag in required_tags
    ]
    if runtime_checks:
        checks.extend(
            [
                {"kind": "executable_present", "name": "python"},
                {"kind": "non_secret_env_present", "name": "CUDA_VISIBLE_DEVICES"},
                {"kind": "secret_reference_present", "name": "MODEL_TOKEN"},
                {"kind": "project_relative_path_present", "path": "train.py"},
            ]
        )
    return {
        "name": name,
        "setup_command": setup_command,
        "required_server_tags": required_tags,
        "working_directory_policy": "project_checkout",
        "non_secret_env": ["CUDA_VISIBLE_DEVICES"] if runtime_checks else [],
        "secret_references": ["MODEL_TOKEN"] if runtime_checks else [],
        "preflight_checks": checks,
    }


def _insert_binding(
    database: Database,
    *,
    project_id: str,
    actor_id: str,
    role: ProjectRoleV2,
) -> None:
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_role_bindings (
                id, project_id, actor_id, role, grant_provenance, granted_at
            ) VALUES (?, ?, ?, ?, 'legacy_membership', ?)
            """,
            (
                str(uuid.uuid4()),
                project_id,
                actor_id,
                role.value,
                "2026-08-01T00:00:00+00:00",
            ),
        )


def _seed_project(database: Database) -> str:
    project_id = database.insert_project(
        f"environment-{uuid.uuid4()}",
        "/srv/projects/environment",
    )
    for actor_id, name, actor_type in (
        (OWNER_ID, "Owner", ActorType.HUMAN),
        (REVIEWER_ID, "Reviewer", ActorType.HUMAN),
        (OPERATOR_ID, "Operator", ActorType.HUMAN),
        (OUTSIDER_ID, "Outsider", ActorType.HUMAN),
        (SERVICE_ID, "Service operator", ActorType.SERVICE),
    ):
        if database.get_actor(actor_id) is None:
            database.insert_actor(
                actor_id=actor_id,
                actor_type=actor_type,
                display_name=name,
            )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=OWNER_ID,
        role=ProjectRoleV2.OWNER,
    )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=REVIEWER_ID,
        role=ProjectRoleV2.REVIEWER,
    )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=OPERATOR_ID,
        role=ProjectRoleV2.OPERATOR,
    )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=SERVICE_ID,
        role=ProjectRoleV2.OPERATOR,
    )
    return project_id


def _session_for(client, main_module, actor_id: str) -> None:
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.clear()
    client.cookies.set(
        main_module.app_state.config.session_cookie_name,
        issued.raw_token,
    )


def _enable_environments(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_environments_v1_enabled = True
    config.authorization_mode = "enforce"


def _service_token_headers(
    client,
    main_module,
    *,
    scopes: list[str],
) -> dict[str, str]:
    database = main_module.app_state.db
    if database.get_service_account(SERVICE_ID) is None:
        database.insert_service_account(
            actor_id=SERVICE_ID,
            name=f"environment-service-{uuid.uuid4()}",
        )
    token = generate_service_token()
    database.insert_service_account_token(
        token_id=token.id,
        service_account_actor_id=SERVICE_ID,
        secret_hash=token.secret_hash,
        scopes=scopes,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.service_token_auth_enabled = True
    client.cookies.clear()
    return {"Authorization": f"Bearer {token.raw_token}"}


def _publish_server_evidence(
    database: Database,
    *,
    server_name: str,
    tags: list[str],
    verified_yaml_bytes: bool,
) -> int:
    server = {
        "name": server_name,
        "enabled": True,
        "execution_backend": "ssh",
        "host": "192.0.2.117",
        "port": 22,
        "user": "worker",
        "key": "/dispatch-test/nonexistent-pr05-key",
        "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"],
        "tags": tags,
    }
    before: dict[str, list[dict[str, object]]] = {"servers": []}
    after = {"servers": [server]}
    contract = build_server_config_contract(
        operation="add",
        server_name=server_name,
        yaml_before=before,
        yaml_after=after,
        server_payload=server,
    )
    if not verified_yaml_bytes:
        contract.pop("yaml_after_utf8_b64")
    approval_id = database.insert_pinned_approval(
        kind="server_add",
        contract_version=SERVER_CONFIG_CONTRACT_VERSION,
        payload=contract,
    )
    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name=server_name,
        server_payload=server,
        yaml_before=before,
        yaml_after=after,
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    assert outcome.state == "activated"
    database.insert_server_observation(
        server_name=server_name,
        online=True,
        probe_ok=True,
    )
    return approval_id


def _mutation_table_counts(database: Database) -> dict[str, int]:
    with database.cursor() as cursor:
        return {
            table: int(
                cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "api_idempotency_keys",
                "approvals",
                "audit_events",
                "audit_export_operations",
                "environment_revisions",
                "project_environments",
            )
        }


def _assert_secret_safe_validation(response, sentinel: str) -> None:
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    serialized = json.dumps(
        {"body": response.json(), "headers": dict(response.headers)},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert sentinel not in serialized
    payload = response.json()
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "invalid_environment_request"
    assert payload["error"]["message"] == "The Environment request is invalid"
    assert set(payload["error"]["details"]) == {"errors"}
    errors = payload["error"]["details"]["errors"]
    assert 1 <= len(errors) <= 12
    for error in errors:
        assert set(error) == {"location", "message", "type"}
        assert 1 <= len(error["location"]) <= 6
        assert all(isinstance(segment, str) for segment in error["location"])
        assert error["type"] in {
            "invalid_discriminator",
            "invalid_json",
            "invalid_value",
            "required_field",
            "unexpected_field",
        }


def _create_approval(
    database: Database,
    *,
    project_id: str,
    operation: Literal["create", "update", "archive"] = "create",
    expected_revision: int = 0,
    expected_head_revision_id: str | None = None,
    environment_id: str | None = None,
    environment: EnvironmentRevisionInput | None = None,
    requester_actor_id: str = OPERATOR_ID,
) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_environment_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation=operation,
                expected_revision=expected_revision,
                expected_head_revision_id=expected_head_revision_id,
                environment_id=environment_id,
                environment=environment,
                requester_actor_id=requester_actor_id,
            )
        )


def test_environment_contract_is_canonical_closed_and_secret_value_free():
    environment = EnvironmentRevisionInput.model_validate(
        _environment_body(runtime_checks=True)
    )
    target = build_environment_revision_contract(
        environment,
        environment_id="30000000-0000-0000-0000-000000000051",
        revision_id="40000000-0000-0000-0000-000000000051",
        revision=1,
    )
    payload = parse_environment_change_payload(
        {
            "contract_version": ENVIRONMENT_CHANGE_CONTRACT_VERSION,
            "operation": "create",
            "project_id": "10000000-0000-0000-0000-000000000051",
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "target_revision": target.model_dump(mode="json"),
        }
    )

    assert len(target.revision_digest) == 64
    assert len(environment_change_payload_digest(payload)) == 64
    assert target.required_server_tags == ["gpu", "pilot-117"]
    assert [check.kind for check in target.preflight_checks] == sorted(
        check.kind for check in target.preflight_checks
    )
    adapter: TypeAdapter[EnvironmentChangeRequest] = TypeAdapter(
        EnvironmentChangeRequest
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "operation": "create",
                "expected_revision": 0,
                "expected_head_revision_id": None,
                "environment": {
                    **_environment_body(),
                    "secret_values": {"MODEL_TOKEN": "do-not-store"},
                },
            }
        )
    with pytest.raises(ValueError, match="declared secret"):
        build_environment_revision_contract(
            EnvironmentRevisionInput.model_validate(
                {
                    **_environment_body(
                        setup_command="echo $MODEL_TOKEN",
                        runtime_checks=True,
                    )
                }
            ),
            environment_id="30000000-0000-0000-0000-000000000052",
            revision_id="40000000-0000-0000-0000-000000000052",
            revision=1,
        )


def test_environment_create_update_archive_are_exact_and_terminal(tmp_path):
    database = Database(str(tmp_path / "environment.db"))
    project_id = _seed_project(database)
    create_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    assert "environment_change_v2" in TRANSACTION_ONLY_APPROVAL_KINDS
    created = database.apply_environment_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    heads = database.list_project_environment_heads_page(
        project_id=project_id,
        after=None,
        limit_plus_one=2,
    )
    assert len(heads) == 1
    assert heads[0]["status"] == "approved"
    assert heads[0]["revision"].revision == 1

    with pytest.raises(ValueError, match="environment_name_immutable"):
        _create_approval(
            database,
            project_id=project_id,
            operation="update",
            expected_revision=1,
            expected_head_revision_id=created["environment_revision_id"],
            environment_id=created["environment_id"],
            environment=EnvironmentRevisionInput.model_validate(
                _environment_body(name="renamed-host")
            ),
        )
    update_id = _create_approval(
        database,
        project_id=project_id,
        operation="update",
        expected_revision=1,
        expected_head_revision_id=created["environment_revision_id"],
        environment_id=created["environment_id"],
        environment=EnvironmentRevisionInput.model_validate(
            _environment_body(setup_command="python -m venv .venv-v2")
        ),
    )
    updated = database.apply_environment_change_decision(
        approval_id=update_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    assert updated["revision"] == 2

    archive_id = _create_approval(
        database,
        project_id=project_id,
        operation="archive",
        expected_revision=2,
        expected_head_revision_id=updated["environment_revision_id"],
        environment_id=updated["environment_id"],
    )
    archived = database.apply_environment_change_decision(
        approval_id=archive_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    assert archived["revision"] == 3
    assert archived["status"] == "archived"
    with pytest.raises(ValueError, match="environment_archived"):
        _create_approval(
            database,
            project_id=project_id,
            operation="update",
            expected_revision=3,
            expected_head_revision_id=archived["environment_revision_id"],
            environment_id=archived["environment_id"],
            environment=EnvironmentRevisionInput.model_validate(_environment_body()),
        )
    with database.cursor() as cursor:
        rows = cursor.execute(
            """
            SELECT revision, status, supersedes_id FROM environment_revisions
            WHERE environment_id = ? ORDER BY revision
            """,
            (archived["environment_id"],),
        ).fetchall()
    assert [(row["revision"], row["status"]) for row in rows] == [
        (1, "approved"),
        (2, "approved"),
        (3, "archived"),
    ]
    assert rows[1]["supersedes_id"] == created["environment_revision_id"]
    assert rows[2]["supersedes_id"] == updated["environment_revision_id"]
    revision_events = [
        event
        for event in database.list_durable_audit_events(limit=100)
        if event["action"] == "environment_revision_created"
    ]
    assert len(revision_events) == 3
    for event in revision_events:
        assert set(event["params"]) == {
            "logical_resource_id",
            "operation",
            "payload_digest",
            "predecessor_revision_id",
            "project_id",
            "revision",
            "revision_digest",
            "status",
        }
    serialized_events = json.dumps(revision_events, sort_keys=True)
    for forbidden in (
        "python -m venv",
        "gpu",
        "pilot-117",
        "MODEL_TOKEN",
        "server_name",
        "credential",
    ):
        assert forbidden not in serialized_events


def test_stale_head_and_approve_time_danger_policy_leave_request_pending(
    tmp_path,
    monkeypatch,
):
    database = Database(str(tmp_path / "environment-stale.db"))
    project_id = _seed_project(database)
    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    monkeypatch.setattr("app.db.dangerous_setup_reason", lambda _command: "policy drift")

    with pytest.raises(ValueError, match="dangerous_setup_command"):
        database.apply_environment_change_decision(
            approval_id=approval_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="test_session",
        )

    pending = database.get_approval(approval_id)
    assert pending is not None and pending.status == "pending"
    assert database.list_project_environment_heads_page(
        project_id=project_id,
        after=None,
        limit_plus_one=2,
    ) == []


def test_archive_remains_available_after_setup_policy_drift(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "environment-archive-drift.db"))
    project_id = _seed_project(database)
    create_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    created = database.apply_environment_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    monkeypatch.setattr("app.db.dangerous_setup_reason", lambda _command: "drift")

    def reject_historical_contract(_environment):
        raise ValueError("new secret policy")

    monkeypatch.setattr("app.db.validate_secret_free_setup", reject_historical_contract)
    archive_id = _create_approval(
        database,
        project_id=project_id,
        operation="archive",
        expected_revision=1,
        expected_head_revision_id=created["environment_revision_id"],
        environment_id=created["environment_id"],
    )
    archived = database.apply_environment_change_decision(
        approval_id=archive_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    assert archived["status"] == "archived"
    assert archived["revision"] == 2


def test_environment_materialization_rolls_back_when_durable_audit_fails(
    tmp_path,
    monkeypatch,
):
    database = Database(str(tmp_path / "environment-audit.db"))
    project_id = _seed_project(database)
    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    original = database.append_durable_audit_event_in_transaction

    def fail_environment(*args, **kwargs):
        if kwargs.get("action") == "environment_revision_created":
            raise RuntimeError("environment audit fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_environment,
    )
    with pytest.raises(RuntimeError, match="environment audit fault"):
        database.apply_environment_change_decision(
            approval_id=approval_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="test_session",
        )
    pending = database.get_approval(approval_id)
    assert pending is not None and pending.status == "pending"
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM project_environments"
        ).fetchone()[0] == 0
        assert cursor.execute(
            "SELECT COUNT(*) FROM environment_revisions"
        ).fetchone()[0] == 0


def test_stale_successor_stays_pending_and_can_be_rejected_after_policy_drift(
    tmp_path,
    monkeypatch,
):
    database = Database(str(tmp_path / "environment-race.db"))
    project_id = _seed_project(database)
    create_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    created = database.apply_environment_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    update_ids = [
        _create_approval(
            database,
            project_id=project_id,
            operation="update",
            expected_revision=1,
            expected_head_revision_id=created["environment_revision_id"],
            environment_id=created["environment_id"],
            environment=EnvironmentRevisionInput.model_validate(
                _environment_body(setup_command=f"python -m venv .venv-v{version}")
            ),
        )
        for version in (2, 3)
    ]
    database.apply_environment_change_decision(
        approval_id=update_ids[0],
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    with pytest.raises(ValueError, match="environment_revision_conflict"):
        database.apply_environment_change_decision(
            approval_id=update_ids[1],
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="test_session",
        )
    stale = database.get_approval(update_ids[1])
    assert stale is not None and stale.status == "pending"

    database.update_actor(OPERATOR_ID, disabled_at="2026-08-07T12:00:00+00:00")
    monkeypatch.setattr("app.db.dangerous_setup_reason", lambda _command: "drift")
    database.reject_environment_change_decision(
        approval_id=update_ids[1],
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
        note="stale request rejected",
    )
    rejected = database.get_approval(update_ids[1])
    assert rejected is not None and rejected.status == "rejected"


def test_approve_revalidates_requester_and_project_rbac_but_reject_does_not(
    tmp_path,
):
    database = Database(str(tmp_path / "environment-rbac.db"))
    project_id = _seed_project(database)
    requester_disabled_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(
            _environment_body(name="requester-disabled")
        ),
    )
    database.update_actor(OPERATOR_ID, disabled_at="2026-08-07T12:00:00+00:00")
    with pytest.raises(ValueError, match="requester is not enabled"):
        database.apply_environment_change_decision(
            approval_id=requester_disabled_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="test_session",
        )
    requester_disabled = database.get_approval(requester_disabled_id)
    assert requester_disabled is not None and requester_disabled.status == "pending"
    database.reject_environment_change_decision(
        approval_id=requester_disabled_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )

    database.update_actor(OPERATOR_ID, disabled_at=None)
    rbac_drift_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(
            _environment_body(name="rbac-drift")
        ),
    )
    database.update_actor(OWNER_ID, disabled_at="2026-08-07T12:00:00+00:00")
    with pytest.raises(ValueError, match="project_rbac_not_ready"):
        database.apply_environment_change_decision(
            approval_id=rbac_drift_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="test_session",
        )
    rbac_drift = database.get_approval(rbac_drift_id)
    assert rbac_drift is not None and rbac_drift.status == "pending"
    database.reject_environment_change_decision(
        approval_id=rbac_drift_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    rejected = database.get_approval(rbac_drift_id)
    assert rejected is not None and rejected.status == "rejected"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_legacy_generic_decision_paths_cannot_decide_environment(
    tmp_path,
    decision,
):
    database = Database(str(tmp_path / f"environment-legacy-{decision}.db"))
    project_id = _seed_project(database)
    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    with pytest.raises(ValueError, match="must use the Product v2 decision"):
        if decision == "approve":
            asyncio.run(approval_module.approve(database, approval_id))
        else:
            approval_module.reject(database, approval_id)
    approval = database.get_approval(approval_id)
    assert approval is not None and approval.status == "pending"
    assert database.list_project_environment_heads_page(
        project_id=project_id,
        after=None,
        limit_plus_one=2,
    ) == []


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, "unknown"),
        ("fresh", "ready"),
        ("stale", "unknown"),
        ("future", "unknown"),
        ("malformed", "unknown"),
        ("offline", "not_ready"),
    ],
)
def test_readiness_freshness_is_conservative(candidate, expected):
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    revision = build_environment_revision_contract(
        EnvironmentRevisionInput.model_validate(_environment_body(tags=["gpu"])),
        environment_id="30000000-0000-0000-0000-000000000061",
        revision_id="40000000-0000-0000-0000-000000000061",
        revision=1,
    )
    candidates = []
    if candidate is not None:
        observed_at = {
            "fresh": now - timedelta(seconds=5),
            "stale": now - timedelta(seconds=61),
            "future": now + timedelta(seconds=1),
            "malformed": None,
            "offline": now - timedelta(seconds=5),
        }[candidate]
        candidates = [
            VerifiedHostCandidate(
                tags=("gpu",),
                activated_at=(now - timedelta(hours=1)).isoformat(),
                observation=HostObservationEvidence(
                    observed_at=(
                        observed_at.isoformat()
                        if isinstance(observed_at, datetime)
                        else "not-a-timestamp"
                    ),
                    online=candidate != "offline",
                    probe_ok=candidate != "offline",
                ),
            )
        ]
    readiness = evaluate_environment_readiness(
        revision,
        status="approved",
        candidates=candidates,
        now=now,
        stale_after_seconds=60,
    )
    assert readiness["state"] == expected


def test_readiness_requires_colocated_tags_and_keeps_runtime_checks_unknown():
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    revision = build_environment_revision_contract(
        EnvironmentRevisionInput.model_validate(
            _environment_body(tags=["gpu", "pilot-117"])
        ),
        environment_id="30000000-0000-0000-0000-000000000062",
        revision_id="40000000-0000-0000-0000-000000000062",
        revision=1,
    )
    observation = HostObservationEvidence(
        observed_at=(now - timedelta(seconds=5)).isoformat(),
        online=True,
        probe_ok=True,
    )
    distributed = [
        VerifiedHostCandidate(
            tags=(tag,),
            activated_at=(now - timedelta(hours=1)).isoformat(),
            observation=observation,
        )
        for tag in ("gpu", "pilot-117")
    ]
    assert evaluate_environment_readiness(
        revision,
        status="approved",
        candidates=distributed,
        now=now,
        stale_after_seconds=60,
    )["state"] == "not_ready"

    runtime_revision = build_environment_revision_contract(
        EnvironmentRevisionInput.model_validate(
            _environment_body(tags=["gpu"], runtime_checks=True)
        ),
        environment_id="30000000-0000-0000-0000-000000000063",
        revision_id="40000000-0000-0000-0000-000000000063",
        revision=1,
    )
    assert evaluate_environment_readiness(
        runtime_revision,
        status="approved",
        candidates=[
            VerifiedHostCandidate(
                tags=("gpu",),
                activated_at=(now - timedelta(hours=1)).isoformat(),
                observation=observation,
            )
        ],
        now=now,
        stale_after_seconds=60,
    )["state"] == "unknown"
    assert evaluate_environment_readiness(
        revision,
        status="archived",
        candidates=[],
        now=now,
        stale_after_seconds=60,
    )["state"] == "not_ready"


def test_environment_validation_is_secret_safe_and_router_scoped(
    api_client,
    caplog,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    before = _mutation_table_counts(database)
    endpoint = f"/api/v2/projects/{project_id}/environment-change-requests"

    sentinels_and_bodies = [
        (
            "PR05_SECRET_VALUE_7c9b4f",
            {
                "operation": "create",
                "expected_revision": 0,
                "expected_head_revision_id": None,
                "environment": {
                    **_environment_body(),
                    "secret_values": {
                        "MODEL_TOKEN": "PR05_SECRET_VALUE_7c9b4f"
                    },
                },
            },
        ),
        (
            "PR05_SECRET_FIELD_7c9b50",
            {
                "operation": "create",
                "expected_revision": 0,
                "expected_head_revision_id": None,
                "environment": {
                    **_environment_body(),
                    "PR05_SECRET_FIELD_7c9b50": "rejected",
                },
            },
        ),
        (
            "PR05_SECRET_OPERATION_7c9b51",
            {
                "operation": "PR05_SECRET_OPERATION_7c9b51",
                "expected_revision": 0,
                "expected_head_revision_id": None,
                "environment": _environment_body(),
            },
        ),
        (
            "PR05_SECRET_NESTED_7c9b52",
            {
                "operation": "create",
                "expected_revision": 0,
                "expected_head_revision_id": None,
                "environment": _environment_body(
                    tags=["PR05_SECRET_NESTED_7c9b52 invalid"]
                ),
            },
        ),
    ]
    for index, (sentinel, body) in enumerate(sentinels_and_bodies):
        response = client.post(
            endpoint,
            json=body,
            headers={"Idempotency-Key": f"secret-safe-{index}"},
        )
        _assert_secret_safe_validation(response, sentinel)

    malformed_sentinel = "PR05_SECRET_JSON_7c9b53"
    malformed = client.post(
        endpoint,
        content=f'{{"operation":"{malformed_sentinel}"',
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "secret-safe-malformed",
        },
    )
    _assert_secret_safe_validation(malformed, malformed_sentinel)
    assert malformed.json()["error"]["details"]["errors"][0]["type"] == (
        "invalid_json"
    )

    query_sentinel = "PR05_SECRET_QUERY_7c9b54"
    invalid_query = client.get(
        f"/api/v2/projects/{project_id}/environments",
        params={"limit": query_sentinel},
    )
    _assert_secret_safe_validation(invalid_query, query_sentinel)

    path_sentinel = "PR05_SECRET_PATH_7c9b55"
    invalid_path = client.get(
        f"/api/v2/projects/{path_sentinel}/environments"
    )
    assert invalid_path.status_code == 404
    assert path_sentinel not in invalid_path.text
    assert path_sentinel not in json.dumps(dict(invalid_path.headers))

    assert _mutation_table_counts(database) == before
    with database.cursor() as cursor:
        database_dump = "\n".join(cursor.connection.iterdump())
    for sentinel, _body in sentinels_and_bodies:
        assert sentinel not in database_dump
    for sentinel in (malformed_sentinel, query_sentinel, path_sentinel):
        assert sentinel not in database_dump
    server_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "app" or record.name.startswith(("app.", "dispatch_center"))
    )
    for sentinel, _body in sentinels_and_bodies:
        assert sentinel not in server_logs
    for sentinel in (malformed_sentinel, query_sentinel, path_sentinel):
        assert sentinel not in server_logs

    operation = main_module.app.openapi()["paths"][endpoint.replace(project_id, "{project_id}")][
        "post"
    ]
    request_schema = operation["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert len(request_schema["oneOf"]) == 3
    assert request_schema["discriminator"]["propertyName"] == "operation"
    assert operation["responses"]["422"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/EnvironmentValidationErrorEnvelope"}

    unrelated_sentinel = "PR05_UNRELATED_VALIDATION_7c9b56"
    _session_for(client, main_module, REVIEWER_ID)
    unrelated = client.get(
        "/api/v2/approvals",
        params={"limit": unrelated_sentinel},
    )
    assert unrelated.status_code == 422
    assert "detail" in unrelated.json()
    assert unrelated.json()["detail"][0]["input"] == unrelated_sentinel
    assert "error" not in unrelated.json()


def test_environment_requester_and_decider_actor_matrix(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    route = f"/api/v2/projects/{project_id}/environment-change-requests"

    _session_for(client, main_module, OWNER_ID)
    owner_only = client.post(
        route,
        json={
            "operation": "create",
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "environment": _environment_body(name="owner-only"),
        },
        headers={"Idempotency-Key": "owner-is-not-operator"},
    )
    assert owner_only.status_code == 403
    assert owner_only.json()["error"]["details"]["reason"] == (
        "denied_project_role_insufficient"
    )

    service_headers = _service_token_headers(
        client,
        main_module,
        scopes=[Action.PROJECT_OPERATE.value],
    )
    service_request = client.post(
        route,
        json={
            "operation": "create",
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "environment": _environment_body(name="service-operator"),
        },
        headers={
            **service_headers,
            "Idempotency-Key": "service-operator-request",
        },
    )
    assert service_request.status_code == 202

    admin = database.insert_actor(
        actor_id=PLATFORM_ADMIN_ID,
        actor_type=ActorType.HUMAN,
        display_name="Platform administrator",
        platform_admin=True,
    )
    _session_for(client, main_module, admin.id)
    admin_request = client.post(
        route,
        json={
            "operation": "create",
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "environment": _environment_body(name="admin-override"),
        },
        headers={"Idempotency-Key": "platform-admin-request"},
    )
    assert admin_request.status_code == 202

    _session_for(client, main_module, OWNER_ID)
    owner_decision = client.post(
        f"/api/v2/approvals/{admin_request.json()['approval_id']}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "owner-can-decide"},
    )
    assert owner_decision.status_code == 202

    _session_for(client, main_module, PLATFORM_ADMIN_ID)
    admin_decision = client.post(
        f"/api/v2/approvals/{service_request.json()['approval_id']}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "admin-can-decide"},
    )
    assert admin_decision.status_code == 202

    _session_for(client, main_module, OPERATOR_ID)
    human_request = client.post(
        route,
        json={
            "operation": "create",
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "environment": _environment_body(name="human-operator"),
        },
        headers={"Idempotency-Key": "human-operator-request"},
    )
    assert human_request.status_code == 202
    human_approval_id = human_request.json()["approval_id"]

    _insert_binding(
        database,
        project_id=project_id,
        actor_id=SERVICE_ID,
        role=ProjectRoleV2.OWNER,
    )
    service_decision_headers = _service_token_headers(
        client,
        main_module,
        scopes=[Action.APPROVAL_DECIDE.value],
    )
    service_decision = client.post(
        f"/api/v2/approvals/{human_approval_id}/decisions",
        json={"decision": "approve"},
        headers={
            **service_decision_headers,
            "Idempotency-Key": "service-cannot-decide",
        },
    )
    assert service_decision.status_code == 403
    assert service_decision.json()["error"]["details"]["reason"] == (
        "denied_service_action"
    )
    approval = database.get_approval(human_approval_id)
    assert approval is not None and approval.status == "pending"


def test_environment_request_and_decision_idempotency_roll_back_with_audit(
    api_client,
    monkeypatch,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    route = f"/api/v2/projects/{project_id}/environment-change-requests"
    before_request = _mutation_table_counts(database)
    original_created_audit = database._append_approval_created_audit

    def fail_request_audit(*args, **kwargs):
        raise RuntimeError("injected environment request audit failure")

    monkeypatch.setattr(
        database,
        "_append_approval_created_audit",
        fail_request_audit,
    )
    with pytest.raises(RuntimeError, match="environment request audit failure"):
        client.post(
            route,
            json={
                "operation": "create",
                "expected_revision": 0,
                "expected_head_revision_id": None,
                "environment": _environment_body(name="request-rollback"),
            },
            headers={"Idempotency-Key": "request-audit-rollback"},
        )
    assert _mutation_table_counts(database) == before_request
    monkeypatch.setattr(
        database,
        "_append_approval_created_audit",
        original_created_audit,
    )

    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(
            _environment_body(name="decision-rollback")
        ),
    )
    _session_for(client, main_module, REVIEWER_ID)
    before_decision = _mutation_table_counts(database)
    original_append = database.append_durable_audit_event_in_transaction

    def fail_decision_audit(cursor, **kwargs):
        if kwargs.get("action") == "approval_decided":
            raise RuntimeError("injected environment decision audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_decision_audit,
    )
    with pytest.raises(RuntimeError, match="environment decision audit failure"):
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            json={"decision": "approve"},
            headers={"Idempotency-Key": "decision-audit-rollback"},
        )
    assert _mutation_table_counts(database) == before_decision
    approval = database.get_approval(approval_id)
    assert approval is not None and approval.status == "pending"
    assert database.list_project_environment_heads_page(
        project_id=project_id,
        after=None,
        limit_plus_one=2,
    ) == []


def test_only_verified_active_server_evidence_can_make_environment_ready(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    _publish_server_evidence(
        database,
        server_name="legacy-pilot-117",
        tags=["gpu", "pilot-117"],
        verified_yaml_bytes=False,
    )
    assert database.list_verified_active_host_candidates() == []

    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    database.apply_environment_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    _session_for(client, main_module, REVIEWER_ID)
    unknown = client.get(f"/api/v2/projects/{project_id}/environments")
    assert unknown.status_code == 200
    assert unknown.json()["items"][0]["readiness"]["state"] == "unknown"

    verified_approval_id = _publish_server_evidence(
        database,
        server_name="verified-pilot-117",
        tags=["gpu", "pilot-117"],
        verified_yaml_bytes=True,
    )
    candidates = database.list_verified_active_host_candidates()
    assert len(candidates) == 1
    assert candidates[0].tags == ("gpu", "pilot-117")
    ready = client.get(f"/api/v2/projects/{project_id}/environments")
    assert ready.status_code == 200
    assert ready.json()["items"][0]["readiness"]["state"] == "ready"
    for forbidden in (
        "legacy-pilot-117",
        "verified-pilot-117",
        "192.0.2.117",
        "/dispatch-test/nonexistent-pr05-key",
    ):
        assert forbidden not in ready.text

    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER approvals_execution_pin_update_guard")
        cursor.execute(
            "UPDATE approvals SET payload = payload || ' ' WHERE id = ?",
            (verified_approval_id,),
        )
    assert database.list_verified_active_host_candidates() == []
    after_tamper = client.get(f"/api/v2/projects/{project_id}/environments")
    assert after_tamper.json()["items"][0]["readiness"]["state"] == "unknown"


def test_environment_head_list_is_bounded_and_cursor_paginated(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    for name in ("beta", "alpha"):
        approval_id = _create_approval(
            database,
            project_id=project_id,
            environment=EnvironmentRevisionInput.model_validate(
                _environment_body(name=name, tags=[])
            ),
        )
        database.apply_environment_change_decision(
            approval_id=approval_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="test_session",
        )
    _session_for(client, main_module, REVIEWER_ID)

    first = client.get(
        f"/api/v2/projects/{project_id}/environments",
        params={"limit": 1},
    )
    assert first.status_code == 200
    assert [item["name"] for item in first.json()["items"]] == ["alpha"]
    assert isinstance(first.json()["next_cursor"], str)
    second = client.get(
        f"/api/v2/projects/{project_id}/environments",
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert [item["name"] for item in second.json()["items"]] == ["beta"]
    assert second.json()["next_cursor"] is None


def test_environment_http_create_review_decide_read_and_replay(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = {
        "operation": "create",
        "expected_revision": 0,
        "expected_head_revision_id": None,
        "environment": _environment_body(tags=[]),
    }
    response = client.post(
        f"/api/v2/projects/{project_id}/environment-change-requests",
        json=request_body,
        headers={"Idempotency-Key": "environment-create"},
    )
    replay = client.post(
        f"/api/v2/projects/{project_id}/environment-change-requests",
        json=request_body,
        headers={"Idempotency-Key": "environment-create"},
    )
    assert response.status_code == 202
    assert response.headers["Cache-Control"] == "no-store"
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    approval_id = response.json()["approval_id"]
    request_conflict = client.post(
        f"/api/v2/projects/{project_id}/environment-change-requests",
        json={
            **request_body,
            "environment": _environment_body(name="different-environment"),
        },
        headers={"Idempotency-Key": "environment-create"},
    )
    assert request_conflict.status_code == 409
    assert request_conflict.json()["error"]["code"] == "idempotency_key_reused"
    assert len(database.list_approvals(kind="environment_change_v2")) == 1
    self_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "environment-self-decision"},
    )
    assert self_decision.status_code == 403
    assert self_decision.json()["error"]["details"]["reason"] == (
        "denied_high_risk_self_decision"
    )

    _session_for(client, main_module, REVIEWER_ID)
    listed = client.get("/api/v2/approvals", params={"kind": "environment_change_v2"})
    assert listed.status_code == 200
    assert listed.json()["items"][0].get("payload") is None
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 200
    assert detail.json()["payload_verified"] is True
    assert detail.json()["payload_contract_version"] == (
        ENVIRONMENT_CHANGE_CONTRACT_VERSION
    )
    decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "environment-create-decision"},
    )
    decision_replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "environment-create-decision"},
    )
    assert decision.status_code == 202
    assert decision_replay.status_code == 202
    assert decision_replay.json()["replayed"] is True
    decision_conflict = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "reject", "note": "different request"},
        headers={"Idempotency-Key": "environment-create-decision"},
    )
    assert decision_conflict.status_code == 409
    assert decision_conflict.json()["error"]["code"] == "idempotency_key_reused"
    environments = client.get(f"/api/v2/projects/{project_id}/environments")
    assert environments.status_code == 200
    assert environments.headers["Cache-Control"] == "no-store"
    item = environments.json()["items"][0]
    assert item["head_revision"]["revision"] == 1
    assert item["readiness"]["state"] == "unknown"
    assert "server_name" not in environments.text
    assert "credential" not in environments.text.casefold()


def test_environment_tamper_is_hidden_from_list_and_refused_by_detail_and_decision(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER approvals_execution_pin_update_guard")
        cursor.execute(
            "UPDATE approvals SET payload = payload || ' ' WHERE id = ?",
            (approval_id,),
        )
    _session_for(client, main_module, REVIEWER_ID)

    listed = client.get("/api/v2/approvals", params={"kind": "environment_change_v2"})
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 409
    assert detail.json()["error"]["code"] == "approval_contract_invalid"
    decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "tampered-environment-decision"},
    )
    assert decision.status_code == 409
    assert decision.json()["error"]["code"] == "environment_change_conflict"
    approval = database.get_approval(approval_id)
    assert approval is not None and approval.status == "pending"
    assert database.list_project_environment_heads_page(
        project_id=project_id,
        after=None,
        limit_plus_one=2,
    ) == []


def test_environment_generated_uuid_collisions_create_no_approval(
    tmp_path,
    monkeypatch,
):
    database = Database(str(tmp_path / "environment-uuid-collision.db"))
    project_id = _seed_project(database)
    existing_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    existing = database.apply_environment_change_decision(
        approval_id=existing_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="test_session",
    )
    before_count = len(database.list_approvals(kind="environment_change_v2"))
    monkeypatch.setattr(
        "app.db.uuid.uuid4",
        lambda: uuid.UUID(existing["environment_id"]),
    )
    with pytest.raises(ValueError, match="environment_uuid_conflict"):
        _create_approval(
            database,
            project_id=project_id,
            environment=EnvironmentRevisionInput.model_validate(
                _environment_body(name="uuid-collision")
            ),
        )
    assert len(database.list_approvals(kind="environment_change_v2")) == before_count


def test_environment_http_dangerous_setup_and_cross_project_are_fail_closed(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_environments(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    dangerous = client.post(
        f"/api/v2/projects/{project_id}/environment-change-requests",
        json={
            "operation": "create",
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "environment": _environment_body(setup_command="rm -rf /tmp/work"),
        },
        headers={"Idempotency-Key": "dangerous-environment"},
    )
    assert dangerous.status_code == 400
    assert database.list_approvals(kind="environment_change_v2") == []
    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )

    _session_for(client, main_module, OUTSIDER_ID)
    hidden = client.get(f"/api/v2/projects/{project_id}/environments")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"
    assert client.get(f"/api/v2/approvals/{approval_id}").status_code == 404
    hidden_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "cross-project-environment"},
    )
    assert hidden_decision.status_code == 404
    approval = database.get_approval(approval_id)
    assert approval is not None and approval.status == "pending"


def test_environment_routes_and_pending_approval_hide_when_flag_is_off(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    approval_id = _create_approval(
        database,
        project_id=project_id,
        environment=EnvironmentRevisionInput.model_validate(_environment_body()),
    )
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_environments_v1_enabled = False
    config.authorization_mode = "enforce"
    _session_for(client, main_module, REVIEWER_ID)

    assert client.get(f"/api/v2/projects/{project_id}/environments").status_code == 404
    listed = client.get(
        "/api/v2/approvals",
        params={"kind": "environment_change_v2"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert client.get(f"/api/v2/approvals/{approval_id}").status_code == 404
    decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "hidden-environment-decision"},
    )
    assert decision.status_code == 404
    assert database.get_approval(approval_id).status == "pending"
    workspace = client.get("/api/v2/workspace")
    assert all(
        item["kind"] != "environment_change_v2"
        for item in workspace.json()["pending_approvals"]
    )
