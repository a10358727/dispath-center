from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Literal

import pytest

from app import authorization_enforce
from app.approvals import (
    approve,
    request_dispatch_policy_create_approval,
    request_dispatch_policy_update_approval,
)
from app.audit import now_iso
from app.config import ServerConfig
from app.execution_dispatch import AttemptLaunchContext
from app.execution_contract import canonical_json, utf8_sha256
from app.identity import (
    ActorType,
    ProjectRoleV2,
    RequestContext,
    generate_service_token,
)
from app.monitor import ServerState
from app.scheduler import scheduler_tick
from app.server_attempt_preflight import (
    ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
)
from app.server_publication import (
    SERVER_CONFIG_CONTRACT_VERSION,
    build_server_config_contract,
    publish_approved_server_mutation,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork
from dispatch_center.api.routers import runs_v2
from tests.test_dataset_assets_v2 import (
    ADMIN_ID as DATASET_ADMIN_ID,
    _adopt as _adopt_dataset,
    _published_snapshot,
)
from tests.test_dataset_sharing_v2 import (
    SOURCE_REVIEWER,
    TARGET_OWNER,
    TARGET_REVIEWER,
    _adopt as _adopt_shared_dataset,
    _publish as _publish_shared_snapshot,
    _request_accept,
    _request_offer,
    _seed_projects as _seed_sharing_projects,
)
from tests.test_project_environments_v1 import _publish_server_evidence
from tests.test_run_templates_v2 import (
    OPERATOR_ID,
    OWNER_ID,
    REVIEWER_ID,
    SERVICE_ID,
    _compiler_template,
    _create_environment,
    _insert_binding,
    _request_defaults,
    _request_template,
    _seed_project,
    _session_for,
    _update_environment,
)


def _enable_execution_plan_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_environments_v1_enabled = True
    config.run_template_v2_enabled = True
    config.dataset_assets_v2_enabled = True
    config.run_experience_v2_enabled = True
    config.authorization_mode = "enforce"


def _counts(database) -> dict[str, int]:
    with database.cursor() as cursor:
        return {
            table: int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "api_idempotency_keys",
                "approvals",
                "audit_events",
                "execution_plan_v2_specs",
                "execution_plans",
                "jobs",
            )
        }


def _run_mutation_counts(database) -> dict[str, int]:
    counts = _counts(database)
    counts.pop("audit_events")
    return counts


def _seed_execution_context(main_module) -> dict:
    database = main_module.app_state.db
    project_id = _seed_project(database)
    with database.cursor() as cursor:
        project_name = str(
            cursor.execute(
                "SELECT name FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()["name"]
        )
    environment = _create_environment(database, project_id)
    template_approval_id = _request_template(
        database,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    template = database.apply_run_template_change_decision(
        approval_id=template_approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )

    git_commit = "a" * 40
    promotion_id = database.insert_pinned_approval(
        kind="engineering_task_promote",
        contract_version="code-promotion-v1",
        payload={
            "engineering_task_id": str(uuid.uuid4()),
            "project_name": project_name,
            "base_project_version_id": "legacy-base",
            "git_commit": git_commit,
            "bundle_sha256": "b" * 64,
        },
    )
    prepared = database.prepare_project_version_promotion(
        approval_id=promotion_id,
        project_name=project_name,
        git_commit=git_commit,
        bundle_sha256="b" * 64,
    )
    version = database.finalize_project_version_promotion(
        approval_id=promotion_id,
        version_id=prepared["version"]["id"],
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )

    _publish_server_evidence(
        database,
        server_name="pilot-117",
        tags=["gpu"],
        verified_yaml_bytes=True,
    )
    revision = database.get_active_server_config_revision("pilot-117")
    assert revision is not None
    revision = database.record_server_attempt_backend_preflight(
        server_name="pilot-117",
        revision_id=revision["id"],
        status="eligible",
        contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
        filesystem_type="ext2/ext3/ext4",
    )
    database.insert_server_observation(
        server_name="pilot-117",
        online=True,
        probe_ok=True,
        gpu_count=2,
        gpu_mem_used_mb=1024,
        gpu_mem_total_mb=24576,
        mem_total_bytes=64 * 1024 * 1024 * 1024,
        mem_available_bytes=32 * 1024 * 1024 * 1024,
        disk_avail_bytes=512 * 1024 * 1024 * 1024,
    )
    instance_id = database.insert_project_instance(
        project_name=project_name,
        server="pilot-117",
        path="/srv/projects/product-v2",
        git_branch="main",
        git_commit=git_commit,
        dirty=False,
    )
    database.update_instance_reconcile(
        instance_id,
        state="available",
        git_branch="main",
        git_commit=git_commit,
        dirty=False,
        touch_last_seen=True,
    )
    return {
        "project_id": project_id,
        "project_name": project_name,
        "environment": environment,
        "template": template,
        "version": version,
        "revision": revision,
        "instance_id": instance_id,
    }


def _preview_request(seed: dict) -> dict:
    return {
        "project_version_id": seed["version"]["id"],
        "template_selection": {
            "kind": "run_profile_revision",
            "run_profile_id": seed["template"]["run_profile_id"],
        },
        "parameter_overrides": {
            "enabled": True,
            "epochs": 12,
            "label": "model",
            "mode": "safe",
            "ratio": "0.25",
        },
        "dataset_selection": {"kind": "none"},
        "target_selection": {
            "kind": "server_config_revision",
            "server_config_revision_id": seed["revision"]["id"],
        },
    }


class _RecordingSSH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self.writes: list[tuple[str, str, str]] = []

    async def run(self, server_name: str, command: str, timeout: float):
        self.calls.append((server_name, command, timeout))
        return type("SSHResult", (), {"stdout": "", "exit_status": 0})()

    async def write_file(self, server_name: str, path: str, content: str) -> None:
        self.writes.append((server_name, path, content))


def _scheduler_server_config() -> ServerConfig:
    return ServerConfig(
        name="pilot-117",
        host="192.0.2.117",
        user="worker",
        key="/dispatch-test/nonexistent-pr05-key",
        gpu=False,
        tags=["gpu"],
    )


def _approved_execution_plan_v2_job(client, main_module) -> tuple[dict, dict]:
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    ).json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "scheduler-v2-submit"},
        json={**request_body, "expected_plan_digest": preview["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    _session_for(client, main_module, REVIEWER_ID)
    approved = client.post(
        f"/api/v2/approvals/{submitted.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "scheduler-v2-approve"},
        json={"decision": "approve", "note": "scheduler contract test"},
    )
    assert approved.status_code == 202, approved.json()
    assert approved.json()["status"] == "approved"
    return seed, approved.json()


def _publish_changed_revision(database) -> dict:
    before_server = {
        "name": "pilot-117",
        "enabled": True,
        "execution_backend": "ssh",
        "host": "192.0.2.117",
        "port": 22,
        "user": "worker",
        "key": "/dispatch-test/nonexistent-pr05-key",
        "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"],
        "tags": ["gpu"],
    }
    after_server = {**before_server, "port": 2222}
    yaml_before = {"servers": [before_server]}
    yaml_after = {"servers": [after_server]}
    contract = build_server_config_contract(
        operation="update",
        server_name="pilot-117",
        yaml_before=yaml_before,
        yaml_after=yaml_after,
        server_payload=after_server,
    )
    approval_id = database.insert_pinned_approval(
        kind="server_update",
        contract_version=SERVER_CONFIG_CONTRACT_VERSION,
        payload=contract,
    )
    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="update",
        server_name="pilot-117",
        server_payload=after_server,
        yaml_before=yaml_before,
        yaml_after=yaml_after,
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    assert outcome.state == "activated"
    revision = database.get_active_server_config_revision("pilot-117")
    assert revision is not None
    return revision


def _insert_approved_legacy_attempt_job(
    database,
    *,
    project_name: str,
    server_name: str,
) -> int:
    command = "sleep 60"
    command_digest = utf8_sha256(command)
    approval_id = database.insert_pinned_approval(
        kind="enqueue",
        contract_version="enqueue-execution-v1",
        payload={
            "authorized_operations": ["prepare", "launch", "collect"],
            "job_specs": [
                {
                    "role": "main",
                    "type": "adhoc",
                    "command_utf8_b64": base64.b64encode(
                        command.encode("utf-8")
                    ).decode("ascii"),
                    "command_sha256": command_digest,
                }
            ],
        },
    )
    approval = database.get_approval(approval_id)
    assert approval is not None
    database.update_approval(approval_id, status="approved")
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (
                type, project, command, require_tag, pin_server, depends_on,
                gpus_needed, status, priority, created_at,
                execution_approval_id, approved_payload_sha256,
                execution_contract_version, execution_contract_role,
                approved_command_sha256
            ) VALUES (
                'adhoc', ?, ?, NULL, ?, '[]', NULL, 'queued', 'normal', ?,
                ?, ?, 'enqueue-execution-v1', 'main', ?
            )
            """,
            (
                project_name,
                command,
                server_name,
                now_iso(),
                approval_id,
                approval.payload_sha256,
                command_digest,
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _create_dataset_alias(
    database,
    *,
    project_id: str,
    asset_id: str,
    snapshot_id: str,
    operation: Literal["create", "move"],
    expected_revision: int,
    expected_head_revision_id: str | None = None,
    expected_head_revision_digest: str | None = None,
) -> dict:
    with SQLiteUnitOfWork(database) as unit_of_work:
        approval_id = unit_of_work.run(
            lambda cursor: database.create_dataset_alias_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                asset_id=asset_id,
                operation=operation,
                alias_name="latest",
                snapshot_id=snapshot_id,
                expected_revision=expected_revision,
                expected_head_revision_id=expected_head_revision_id,
                expected_head_revision_digest=expected_head_revision_digest,
                requester_actor_id=OPERATOR_ID,
            )
        )
    return database.apply_dataset_alias_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )


def _owned_dataset_with_movable_alias(database, seed: dict) -> dict:
    if database.get_actor(DATASET_ADMIN_ID) is None:
        database.insert_actor(
            actor_id=DATASET_ADMIN_ID,
            actor_type=ActorType.HUMAN,
            display_name="Dataset platform administrator",
            platform_admin=True,
        )
    _insert_binding(
        database,
        project_id=seed["project_id"],
        actor_id=OPERATOR_ID,
        role=ProjectRoleV2.DATASET_MANAGER,
    )
    first_snapshot = "execution-plan-v2-alias-first"
    second_snapshot = "execution-plan-v2-alias-second"
    _published_snapshot(database, first_snapshot)
    second_approval_id = _published_snapshot(database, second_snapshot)
    asset = _adopt_dataset(
        database,
        project_id=seed["project_id"],
        snapshot_id=first_snapshot,
        decider_id=REVIEWER_ID,
    )
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dataset_asset_snapshots (
                asset_id, owning_project_id, snapshot_id, link_kind,
                link_approval_id, linked_by_actor_id, linked_at
            ) VALUES (?, ?, ?, 'publish', ?, ?, ?)
            """,
            (
                asset["asset_id"],
                seed["project_id"],
                second_snapshot,
                second_approval_id,
                OPERATOR_ID,
                "2026-08-10T00:00:00.000Z",
            ),
        )
    alias = _create_dataset_alias(
        database,
        project_id=seed["project_id"],
        asset_id=asset["asset_id"],
        snapshot_id=first_snapshot,
        operation="create",
        expected_revision=0,
    )
    return {
        "asset": asset,
        "alias": alias,
        "first_snapshot": first_snapshot,
        "second_snapshot": second_snapshot,
    }


def _approve_dispatch_policy(
    main_module,
    *,
    seed: dict,
    audit_path: str,
    update: bool = False,
) -> dict:
    database = main_module.app_state.db
    main_module.app_state.config.dispatch_policy_v1_enabled = True
    context = RequestContext(
        actor=database.get_actor(OPERATOR_ID),
        authentication_method="session",
    )
    request = (
        request_dispatch_policy_update_approval(
            database,
            seed["project_name"],
            "execution-plan-policy",
            allowed_servers=["pilot-117"],
            require_tag="gpu",
            max_concurrent_placements=2,
            config=main_module.app_state.config,
            audit_path=audit_path,
            request_context=context,
        )
        if update
        else request_dispatch_policy_create_approval(
            database,
            seed["project_name"],
            "execution-plan-policy",
            allowed_servers=["pilot-117"],
            require_tag="gpu",
            max_concurrent_placements=2,
            config=main_module.app_state.config,
            audit_path=audit_path,
            request_context=context,
        )
    )
    result = asyncio.run(
        approve(
            database,
            request.id,
            app_state=main_module.app_state,
            audit_path=audit_path,
            request_context=context,
        )
    )
    policy = result["dispatch_policy"]
    return {
        "id": policy.id,
        "revision": policy.revision,
        "status": policy.status,
    }


def _clone_pending_execution_plan(
    database,
    *,
    source_execution_plan_id: str,
    project_id: str,
    created_at: str,
) -> str:
    execution_plan_id = str(uuid.uuid4())
    with database.cursor() as cursor:
        source_plan = cursor.execute(
            "SELECT * FROM execution_plans WHERE id = ?",
            (source_execution_plan_id,),
        ).fetchone()
        source_spec = cursor.execute(
            "SELECT * FROM execution_plan_v2_specs WHERE execution_plan_id = ?",
            (source_execution_plan_id,),
        ).fetchone()
        assert source_plan is not None
        assert source_spec is not None
        payload = {
            "contract_version": "execution-plan-v2-approval-v1",
            "execution_plan_id": execution_plan_id,
            "project_id": project_id,
            "plan_digest": source_spec["plan_digest"],
        }
        raw_payload = canonical_json(payload)
        cursor.execute(
            """
            INSERT INTO approvals (
                kind, payload, status, created_at, requester_actor_id,
                payload_sha256, payload_contract_version, payload_immutable_at
            ) VALUES (
                'execution_plan_v2', ?, 'pending', ?, ?, ?,
                'execution-plan-v2-approval-v1', ?
            )
            """,
            (
                raw_payload,
                created_at,
                OPERATOR_ID,
                utf8_sha256(raw_payload),
                created_at,
            ),
        )
        approval_id = int(cursor.lastrowid)

        plan_values = dict(source_plan)
        plan_values.update(
            {
                "id": execution_plan_id,
                "job_id": None,
                "request_approval_id": approval_id,
                "created_at": created_at,
            }
        )
        plan_columns = list(plan_values)
        cursor.execute(
            "INSERT INTO execution_plans ({columns}) VALUES ({values})".format(
                columns=", ".join(plan_columns),
                values=", ".join("?" for _ in plan_columns),
            ),
            tuple(plan_values[column] for column in plan_columns),
        )

        spec_values = dict(source_spec)
        spec_values.update(
            {
                "execution_plan_id": execution_plan_id,
                "created_approval_id": approval_id,
                "created_at": created_at,
            }
        )
        spec_columns = list(spec_values)
        cursor.execute(
            "INSERT INTO execution_plan_v2_specs ({columns}) VALUES ({values})".format(
                columns=", ".join(spec_columns),
                values=", ".join("?" for _ in spec_columns),
            ),
            tuple(spec_values[column] for column in spec_columns),
        )
    return execution_plan_id


def test_preview_is_read_only_and_does_not_expose_execution_secrets(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    before = _counts(main_module.app_state.db)

    response = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=_preview_request(seed),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["ready"] is True
    assert body["plan_digest"] == body["plan"]["plan_digest"]
    assert body["plan"]["target"]["server_config_revision_id"] == seed["revision"]["id"]
    serialized = response.text
    for forbidden in (
        "/srv/projects/product-v2",
        "/dispatch-test/nonexistent-pr05-key",
        "192.0.2.117",
        "python -m venv .venv",
        "set -euo pipefail",
    ):
        assert forbidden not in serialized
    assert _counts(main_module.app_state.db) == before


def test_digest_mismatch_persists_nothing(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    before = _counts(main_module.app_state.db)
    body = {
        **_preview_request(seed),
        "expected_plan_digest": "0" * 64,
    }

    response = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-mismatch"},
        json=body,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plan_digest_mismatch"
    assert _counts(main_module.app_state.db) == before


def test_routes_are_hidden_until_the_complete_feature_package_is_enabled(api_client):
    client, main_module = api_client
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    before = _counts(main_module.app_state.db)

    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=_preview_request(seed),
    )
    submit = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-feature-disabled"},
        json={**_preview_request(seed), "expected_plan_digest": "0" * 64},
    )

    assert preview.status_code == submit.status_code == 404
    assert _counts(main_module.app_state.db) == before


def test_run_routes_keep_both_opaque_project_denial_layers() -> None:
    assert {
        ("POST", "/api/v2/projects/{project_id}/run-previews"),
        ("POST", "/api/v2/projects/{project_id}/run-requests"),
    }.issubset(authorization_enforce._OPAQUE_PROJECT_READ_INTERFACES)
    assert runs_v2._OPAQUE_PROJECT_DENIALS == {
        runs_v2.AuthorizationReason.DENIED_CROSS_PROJECT,
        runs_v2.AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }


def test_foreign_operator_gets_opaque_denial_and_safe_validation(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    database = main_module.app_state.db
    foreign_actor_id = str(uuid.uuid4())
    foreign_project_id = database.insert_project(
        f"foreign-run-project-{uuid.uuid4()}",
        "/srv/projects/foreign-run-project",
    )
    database.insert_actor(
        actor_id=foreign_actor_id,
        actor_type=ActorType.HUMAN,
        display_name="Foreign operator",
    )
    _insert_binding(
        database,
        project_id=foreign_project_id,
        actor_id=foreign_actor_id,
        role=ProjectRoleV2.OPERATOR,
    )
    _session_for(client, main_module, foreign_actor_id)
    before = _run_mutation_counts(database)

    denied = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=_preview_request(seed),
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"
    assert "reason" not in denied.text
    denied_submit = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "foreign-run-submit"},
        json={
            **_preview_request(seed),
            "expected_plan_digest": "0" * 64,
        },
    )
    assert denied_submit.status_code == 404
    assert denied_submit.json()["error"]["code"] == "not_found"
    assert "reason" not in denied_submit.text
    assert _run_mutation_counts(database) == before

    for actor_id in (OWNER_ID, REVIEWER_ID):
        _session_for(client, main_module, actor_id)
        same_project_preview = client.post(
            f"/api/v2/projects/{seed['project_id']}/run-previews",
            json=_preview_request(seed),
        )
        same_project_submit = client.post(
            f"/api/v2/projects/{seed['project_id']}/run-requests",
            headers={"Idempotency-Key": f"insufficient-run-submit-{actor_id}"},
            json={
                **_preview_request(seed),
                "expected_plan_digest": "0" * 64,
            },
        )
        assert same_project_preview.status_code == 403
        assert same_project_submit.status_code == 403
        assert (
            same_project_preview.json()["error"]["details"]["reason"]
            == "denied_project_role_insufficient"
        )
        assert (
            same_project_submit.json()["error"]["details"]["reason"]
            == "denied_project_role_insufficient"
        )
        assert _run_mutation_counts(database) == before

    _session_for(client, main_module, OPERATOR_ID)
    sentinel = "SECRET_EXECUTION_PLAN_SENTINEL"
    invalid = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json={**_preview_request(seed), "project_version_id": sentinel},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_execution_plan_request"
    assert sentinel not in invalid.text
    assert _run_mutation_counts(database) == before


def test_scoped_service_operator_can_submit_but_cannot_decide(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    database = main_module.app_state.db
    database.insert_service_account(
        actor_id=SERVICE_ID,
        name=f"execution-plan-service-{uuid.uuid4()}",
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
    authorization = {"Authorization": f"Bearer {issued.raw_token}"}
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        headers=authorization,
        json=_preview_request(seed),
    )
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={
            **authorization,
            "Idempotency-Key": "service-run-submit",
        },
        json={
            **_preview_request(seed),
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    decision = client.post(
        f"/api/v2/approvals/{submitted.json()['approval_id']}/decisions",
        headers={
            **authorization,
            "Idempotency-Key": "service-run-decision",
        },
        json={"decision": "approve", "note": "not allowed"},
    )
    assert decision.status_code == 403
    assert database.get_approval(submitted.json()["approval_id"]).status == "pending"


def test_submit_and_approve_are_idempotent_and_pin_one_job(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=_preview_request(seed),
    ).json()
    submit_body = {
        **_preview_request(seed),
        "expected_plan_digest": preview["plan_digest"],
    }

    first = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-submit"},
        json=submit_body,
    )
    second = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-submit"},
        json=submit_body,
    )
    assert first.status_code == 202, first.json()
    assert second.status_code == 202, second.json()
    assert second.json()["execution_plan_id"] == first.json()["execution_plan_id"]
    assert second.json()["replayed"] is True

    _session_for(client, main_module, REVIEWER_ID)
    approval_id = first.json()["approval_id"]
    decision_url = f"/api/v2/approvals/{approval_id}/decisions"
    approved = client.post(
        decision_url,
        headers={"Idempotency-Key": "run-approve"},
        json={"decision": "approve", "note": "reviewed"},
    )
    replayed = client.post(
        decision_url,
        headers={"Idempotency-Key": "run-approve"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert approved.status_code == 202, approved.json()
    assert approved.json()["status"] == "approved"
    assert replayed.status_code == 202, replayed.json()
    assert replayed.json()["job_id"] == approved.json()["job_id"]
    assert replayed.json()["replayed"] is True
    _session_for(client, main_module, OPERATOR_ID)
    submit_after_approval = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-submit"},
        json=submit_body,
    )
    assert submit_after_approval.status_code == 202, submit_after_approval.json()
    assert submit_after_approval.json()["execution_plan_id"] == first.json()[
        "execution_plan_id"
    ]
    assert submit_after_approval.json()["approval_id"] == approval_id
    assert submit_after_approval.json()["job_id"] == approved.json()["job_id"]
    assert submit_after_approval.json()["status"] == "approved"
    assert submit_after_approval.json()["replayed"] is True
    with main_module.app_state.db.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM execution_plan_v2_specs"
        ).fetchone()[0] == 1
        assert cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()[0] == 1
        job = cursor.execute(
            "SELECT * FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()
        assert job["pin_server"] == "pilot-117"
        assert job["execution_contract_version"] == "execution-plan-v2"

    database = main_module.app_state.db
    approval = database.get_approval(approval_id)
    assert approval is not None
    assert approval.payload_contract_version == "execution-plan-v2-approval-v1"
    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )
    attempt = database.create_execution_attempt(
        job_id=approved.json()["job_id"],
        backend="ssh",
        server_config_revision_id=seed["revision"]["id"],
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        initial_operation={
            "operation": "prepare",
            "operation_id": "v2-prepare",
            "idempotency_key": "v2-prepare-key",
            "payload": {"job_id": approved.json()["job_id"]},
        },
    )
    assert attempt["execution_contract_version"] == "execution-plan-v2"
    for operation in ("launch", "collect"):
        created = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation=operation,
            payload={"job_id": approved.json()["job_id"]},
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
            authorization_approval_id=approval_id,
            authorized_contract_sha256=approval.payload_sha256,
        )
        assert created["authorization_class"] == "execution"
    with pytest.raises(ValueError, match="approval_kind_mismatch"):
        database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="cleanup",
            payload={"job_id": approved.json()["job_id"]},
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
            authorization_approval_id=approval_id,
            authorized_contract_sha256=approval.payload_sha256,
        )


def test_approval_detail_returns_verified_immutable_review_without_secrets(
    api_client,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview_response = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview_response.status_code == 200, preview_response.json()
    preview = preview_response.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-review-contract-submit"},
        json={**request_body, "expected_plan_digest": preview["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()

    database = main_module.app_state.db
    approval_id = submitted.json()["approval_id"]
    execution_plan_id = submitted.json()["execution_plan_id"]
    with database.cursor() as cursor:
        sensitive = cursor.execute(
            """
            SELECT plan.command, spec.canonical_argv_json,
                   spec.submit_observation_json,
                   revision.normalized_target_json,
                   revision.credential_ref_json,
                   environment.setup_command,
                   instance.path
            FROM execution_plans AS plan
            JOIN execution_plan_v2_specs AS spec
              ON spec.execution_plan_id = plan.id
            JOIN server_config_revisions AS revision
              ON revision.id = spec.server_config_revision_id
            JOIN environment_revisions AS environment
              ON environment.id = spec.environment_revision_id
            JOIN project_instances AS instance
              ON instance.id = spec.project_instance_id
            WHERE plan.id = ?
            """,
            (execution_plan_id,),
        ).fetchone()
    assert sensitive is not None

    _session_for(client, main_module, REVIEWER_ID)
    before = _counts(database)
    pending = client.get(f"/api/v2/approvals/{approval_id}")

    assert pending.status_code == 200, pending.json()
    assert pending.headers["Cache-Control"] == "no-store"
    assert pending.headers["Pragma"] == "no-cache"
    body = pending.json()
    assert body["status"] == "pending"
    assert set(body["payload"]) == {
        "contract_version",
        "execution_plan_id",
        "project_id",
        "plan_digest",
    }
    assert body["payload"] == {
        "contract_version": "execution-plan-v2-approval-v1",
        "execution_plan_id": execution_plan_id,
        "project_id": seed["project_id"],
        "plan_digest": preview["plan_digest"],
    }
    assert body["payload_contract_version"] == "execution-plan-v2-approval-v1"
    assert body["payload_verified"] is True
    assert body["review"] == {
        "execution_plan_id": execution_plan_id,
        "plan_digest": preview["plan_digest"],
        "contract": preview["plan"],
    }
    assert body["review"]["plan_digest"] == body["payload"]["plan_digest"]
    assert body["review"]["contract"]["plan_digest"] == preview["plan_digest"]
    contract = body["review"]["contract"]
    assert contract["parameter_values"] == request_body["parameter_overrides"]
    assert contract["dataset_none"] is True
    assert contract["dataset_bindings"] == []
    assert contract["target"]["server_config_revision_id"] == seed["revision"]["id"]
    assert contract["resource_requirements"]

    serialized = pending.text
    for forbidden in (
        sensitive["path"],
        sensitive["setup_command"],
        sensitive["command"],
        sensitive["canonical_argv_json"],
        sensitive["submit_observation_json"],
        sensitive["normalized_target_json"],
        sensitive["credential_ref_json"],
        "192.0.2.117",
        '\"user\":\"worker\"',
        "/dispatch-test/nonexistent-pr05-key",
    ):
        assert forbidden
        assert forbidden not in serialized
    assert _counts(database) == before

    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-review-contract-approve"},
        json={"decision": "approve", "note": "reviewed from full contract"},
    )
    assert approved.status_code == 202, approved.json()
    after_decision = _counts(database)
    approved_detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert approved_detail.status_code == 200, approved_detail.json()
    assert approved_detail.json()["status"] == "approved"
    assert approved_detail.json()["review"] == body["review"]
    assert _counts(database) == after_decision


def test_cancelled_execution_plan_v2_keeps_detail_and_exact_replays_valid(
    api_client,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    request_body = _preview_request(seed)
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    ).json()
    submit_body = {
        **request_body,
        "expected_plan_digest": preview["plan_digest"],
    }
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "cancelled-v2-submit"},
        json=submit_body,
    )
    assert submitted.status_code == 202, submitted.json()
    approval_id = submitted.json()["approval_id"]

    _session_for(client, main_module, REVIEWER_ID)
    decision_body = {"decision": "approve", "note": "approve then cancel"}
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "cancelled-v2-approve"},
        json=decision_body,
    )
    assert approved.status_code == 202, approved.json()
    job_id = approved.json()["job_id"]

    _session_for(client, main_module, OPERATOR_ID)
    cancelled = client.post(f"/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.json()
    assert main_module.app_state.db.get_job(job_id).status == "cancelled"

    replayed_submit = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "cancelled-v2-submit"},
        json=submit_body,
    )
    assert replayed_submit.status_code == 202, replayed_submit.json()
    assert replayed_submit.json()["replayed"] is True
    assert replayed_submit.json()["status"] == "approved"
    assert replayed_submit.json()["job_id"] == job_id

    _session_for(client, main_module, REVIEWER_ID)
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["status"] == "approved"
    assert detail.json()["review"]["contract"] == preview["plan"]
    replayed_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "cancelled-v2-approve"},
        json=decision_body,
    )
    assert replayed_decision.status_code == 202, replayed_decision.json()
    assert replayed_decision.json()["replayed"] is True
    assert replayed_decision.json()["status"] == "approved"
    assert replayed_decision.json()["job_id"] == job_id


def test_pruned_source_observation_keeps_historical_approval_review_valid(
    api_client,
):
    client, main_module = api_client
    _seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    approval_id = approved["approval_id"]
    before_prune = client.get(f"/api/v2/approvals/{approval_id}")
    assert before_prune.status_code == 200, before_prune.json()

    with database.cursor() as cursor:
        observation = cursor.execute(
            """
            SELECT spec.submit_observation_id
            FROM execution_plan_v2_specs AS spec
            WHERE spec.created_approval_id = ?
            """,
            (approval_id,),
        ).fetchone()
        assert observation is not None
        observation_id = int(observation["submit_observation_id"])
        assert cursor.execute(
            "SELECT 1 FROM server_observations WHERE id = ?",
            (observation_id,),
        ).fetchone() is not None
        cursor.execute(
            "DELETE FROM server_observations WHERE id = ?",
            (observation_id,),
        )

    before_read = _counts(database)
    after_prune = client.get(f"/api/v2/approvals/{approval_id}")
    assert after_prune.status_code == 200, after_prune.json()
    assert after_prune.json()["review"] == before_prune.json()["review"]
    assert _counts(database) == before_read


@pytest.mark.parametrize("context_kind", ("absent", "disabled", "does_not_own"))
def test_scheduler_never_falls_back_to_legacy_for_execution_plan_v2(
    api_client,
    tmp_path,
    context_kind,
):
    client, main_module = api_client
    seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    lease = database.acquire_scheduler_lease(
        owner_id=f"v2-scheduler-{context_kind}",
        lease_seconds=120,
    )
    if context_kind == "absent":
        context = None
    else:
        context = AttemptLaunchContext(
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
            enabled=context_kind != "disabled",
            revision_ids=(
                {"pilot-117": seed["revision"]["id"]}
                if context_kind == "disabled"
                else {}
            ),
        )
    remote = _RecordingSSH()

    asyncio.run(
        scheduler_tick(
            database,
            {"pilot-117": ServerState(name="pilot-117", online=True, load1=0.1)},
            {"pilot-117": _scheduler_server_config()},
            remote.run,
            remote.write_file,
            audit_path=str(tmp_path / f"scheduler-{context_kind}.jsonl"),
            attempt_launch=context,
        )
    )

    job = database.get_job(approved["job_id"])
    assert job is not None
    assert job.status == "queued"
    assert job.server is None
    assert database.get_latest_execution_attempt_for_job(job.id) is None
    assert remote.calls == []
    assert remote.writes == []


def test_scheduler_rejects_current_revision_drift_without_legacy_fallback(
    api_client,
    tmp_path,
):
    client, main_module = api_client
    _seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    changed_revision = _publish_changed_revision(database)
    lease = database.acquire_scheduler_lease(
        owner_id="v2-scheduler-revision-drift",
        lease_seconds=120,
    )
    context = AttemptLaunchContext(
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        enabled=True,
        revision_ids={"pilot-117": changed_revision["id"]},
    )
    remote = _RecordingSSH()

    asyncio.run(
        scheduler_tick(
            database,
            {"pilot-117": ServerState(name="pilot-117", online=True, load1=0.1)},
            {"pilot-117": _scheduler_server_config()},
            remote.run,
            remote.write_file,
            audit_path=str(tmp_path / "scheduler-revision-drift.jsonl"),
            attempt_launch=context,
        )
    )

    job = database.get_job(approved["job_id"])
    assert job is not None
    assert job.status == "queued"
    assert job.server is None
    assert database.get_latest_execution_attempt_for_job(job.id) is None
    assert remote.calls == []
    assert remote.writes == []


def test_scheduler_revision_drift_does_not_starve_later_eligible_job(
    api_client,
    tmp_path,
):
    client, main_module = api_client
    seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    legacy_job_id = _insert_approved_legacy_attempt_job(
        database,
        project_name=seed["project_name"],
        server_name="pilot-117",
    )
    changed_revision = _publish_changed_revision(database)
    changed_revision = database.record_server_attempt_backend_preflight(
        server_name="pilot-117",
        revision_id=changed_revision["id"],
        status="eligible",
        contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
        filesystem_type="ext2/ext3/ext4",
    )
    lease = database.acquire_scheduler_lease(
        owner_id="v2-scheduler-revision-drift-fifo",
        lease_seconds=120,
    )
    context = AttemptLaunchContext(
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        enabled=True,
        revision_ids={"pilot-117": changed_revision["id"]},
    )
    remote = _RecordingSSH()

    asyncio.run(
        scheduler_tick(
            database,
            {"pilot-117": ServerState(name="pilot-117", online=True, load1=0.1)},
            {"pilot-117": _scheduler_server_config()},
            remote.run,
            remote.write_file,
            audit_path=str(tmp_path / "scheduler-revision-drift-fifo.jsonl"),
            attempt_launch=context,
        )
    )

    v2_job = database.get_job(approved["job_id"])
    dispatched_legacy = database.get_job(legacy_job_id)
    assert v2_job is not None
    assert v2_job.status == "queued"
    assert v2_job.server is None
    assert database.get_latest_execution_attempt_for_job(v2_job.id) is None
    assert dispatched_legacy is not None
    assert dispatched_legacy.status == "running"
    assert dispatched_legacy.server == "pilot-117"
    assert database.get_latest_execution_attempt_for_job(legacy_job_id) is not None
    assert remote.calls
    assert remote.writes


def test_scheduler_dispatches_execution_plan_v2_only_through_exact_attempt(
    api_client,
    tmp_path,
):
    client, main_module = api_client
    seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    lease = database.acquire_scheduler_lease(
        owner_id="v2-scheduler-exact-revision",
        lease_seconds=120,
    )
    context = AttemptLaunchContext(
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        enabled=True,
        revision_ids={"pilot-117": seed["revision"]["id"]},
    )
    remote = _RecordingSSH()
    audit_path = tmp_path / "scheduler-exact-revision.jsonl"

    asyncio.run(
        scheduler_tick(
            database,
            {"pilot-117": ServerState(name="pilot-117", online=True, load1=0.1)},
            {"pilot-117": _scheduler_server_config()},
            remote.run,
            remote.write_file,
            audit_path=str(audit_path),
            attempt_launch=context,
        )
    )

    job = database.get_job(approved["job_id"])
    assert job is not None
    assert job.status == "running"
    assert job.server == "pilot-117"
    attempt = database.get_latest_execution_attempt_for_job(job.id)
    assert attempt is not None
    assert attempt["server_config_revision_id"] == seed["revision"]["id"]
    assert attempt["execution_contract_version"] == "execution-plan-v2"
    assert remote.calls
    assert remote.writes
    audit_records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    dispatch = [record for record in audit_records if record["action"] == "dispatch"]
    assert len(dispatch) == 1
    assert dispatch[0]["params"] == {
        "approved_command_sha256": job.approved_command_sha256,
        "attempt_id": attempt["id"],
        "execution_approval_id": job.execution_approval_id,
        "execution_contract_version": "execution-plan-v2",
        "job_id": job.id,
        "reason_code": "remote_state_observed",
        "server": "pilot-117",
    }
    assert "command" not in dispatch[0]["params"]


@pytest.mark.parametrize(
    "tamper_kind",
    (
        "missing_companion",
        "malformed_companion",
        "split_json",
        "split_digest",
        "observation",
        "plan_mismatch",
    ),
)
def test_approval_detail_fails_closed_after_authorization_and_stays_opaque(
    api_client,
    tamper_kind,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    ).json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": f"run-review-invalid-{tamper_kind}"},
        json={**request_body, "expected_plan_digest": preview["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    approval_id = submitted.json()["approval_id"]
    execution_plan_id = submitted.json()["execution_plan_id"]

    database = main_module.app_state.db
    with database.cursor() as cursor:
        if tamper_kind == "missing_companion":
            cursor.execute("DROP TRIGGER trg_execution_plan_v2_specs_immutable_delete")
            cursor.execute(
                "DELETE FROM execution_plan_v2_specs WHERE execution_plan_id = ?",
                (execution_plan_id,),
            )
        elif tamper_kind in {
            "malformed_companion",
            "split_json",
            "split_digest",
            "observation",
        }:
            cursor.execute("DROP TRIGGER trg_execution_plan_v2_specs_immutable_update")
            if tamper_kind == "malformed_companion":
                cursor.execute(
                    """
                    UPDATE execution_plan_v2_specs
                    SET canonical_spec_json = '{}'
                    WHERE execution_plan_id = ?
                    """,
                    (execution_plan_id,),
                )
            elif tamper_kind == "split_json":
                cursor.execute(
                    """
                    UPDATE execution_plan_v2_specs
                    SET parameter_values_json = '{}'
                    WHERE execution_plan_id = ?
                    """,
                    (execution_plan_id,),
                )
            elif tamper_kind == "split_digest":
                cursor.execute(
                    """
                    UPDATE execution_plan_v2_specs
                    SET dataset_bindings_digest = ?
                    WHERE execution_plan_id = ?
                    """,
                    ("0" * 64, execution_plan_id),
                )
            else:
                row = cursor.execute(
                    """
                    SELECT submit_observation_json
                    FROM execution_plan_v2_specs
                    WHERE execution_plan_id = ?
                    """,
                    (execution_plan_id,),
                ).fetchone()
                observation = json.loads(row["submit_observation_json"])
                observation["observed_at"] = "2099-01-01T00:00:00.000Z"
                raw_observation = canonical_json(observation)
                cursor.execute(
                    """
                    UPDATE execution_plan_v2_specs
                    SET submit_observation_json = ?
                    WHERE execution_plan_id = ?
                    """,
                    (raw_observation, execution_plan_id),
                )
        else:
            cursor.execute("DROP TRIGGER execution_plans_are_immutable")
            cursor.execute(
                "UPDATE execution_plans SET command = command || 'tampered' WHERE id = ?",
                (execution_plan_id,),
            )
    outsider_id = str(uuid.uuid4())
    database.insert_actor(
        actor_id=outsider_id,
        actor_type=ActorType.HUMAN,
        display_name="Opaque ExecutionPlan outsider",
    )

    _session_for(client, main_module, REVIEWER_ID)
    before = _counts(database)
    invalid = client.get(f"/api/v2/approvals/{approval_id}")
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "approval_contract_invalid"
    assert "payload" not in invalid.text
    assert "review" not in invalid.text
    assert _counts(database) == before

    _session_for(client, main_module, outsider_id)
    before_hidden = _counts(database)
    hidden = client.get(f"/api/v2/approvals/{approval_id}")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"
    assert "approval_contract_invalid" not in hidden.text
    assert _counts(database) == before_hidden


def test_approved_job_pointer_tamper_fails_detail_and_decision_replay_closed(
    api_client,
):
    client, main_module = api_client
    seed, approved = _approved_execution_plan_v2_job(client, main_module)
    database = main_module.app_state.db
    approval_id = approved["approval_id"]
    replacement_job_id = _insert_approved_legacy_attempt_job(
        database,
        project_name=seed["project_name"],
        server_name="pilot-117",
    )
    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER execution_plans_are_immutable")
        cursor.execute(
            """
            UPDATE execution_plans
            SET job_id = ?
            WHERE request_approval_id = ?
            """,
            (replacement_job_id, approval_id),
        )

    before = _counts(database)
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 409
    assert detail.json()["error"]["code"] == "approval_contract_invalid"
    assert "review" not in detail.text
    replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "scheduler-v2-approve"},
        json={"decision": "approve", "note": "scheduler contract test"},
    )
    assert replay.status_code == 409
    assert "job_id" not in replay.text
    assert _counts(database) == before


def test_approve_time_environment_drift_rejects_without_job(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=_preview_request(seed),
    ).json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-stale-submit"},
        json={
            **_preview_request(seed),
            "expected_plan_digest": preview["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    _update_environment(
        main_module.app_state.db,
        project_id=seed["project_id"],
        environment_id=seed["environment"]["environment_id"],
        head_revision_id=seed["environment"]["environment_revision_id"],
        expected_revision=1,
    )
    _session_for(client, main_module, REVIEWER_ID)
    approval_id = submitted.json()["approval_id"]
    pending_detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert pending_detail.status_code == 200, pending_detail.json()
    assert pending_detail.json()["status"] == "pending"
    assert pending_detail.json()["review"]["contract"] == preview["plan"]
    response = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-stale-approve"},
        json={"decision": "approve", "note": "reviewed"},
    )

    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "rejected"
    assert response.json()["reason_code"] == "execution_plan_stale"
    rejected_detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert rejected_detail.status_code == 200, rejected_detail.json()
    assert rejected_detail.json()["status"] == "rejected"
    assert rejected_detail.json()["review"] == pending_detail.json()["review"]
    assert response.json()["job_id"] is None
    with main_module.app_state.db.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()[0] == 0


def test_approve_time_target_revision_drift_rejects_without_job(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-approve-target-drift-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    changed_revision = _publish_changed_revision(main_module.app_state.db)
    assert changed_revision["id"] != seed["revision"]["id"]
    _session_for(client, main_module, REVIEWER_ID)
    approval_id = submitted.json()["approval_id"]
    response = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-approve-target-drift-decide"},
        json={"decision": "approve", "note": "reviewed"},
    )

    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "rejected"
    assert response.json()["reason_code"] == "execution_plan_stale"
    assert response.json()["job_id"] is None
    with main_module.app_state.db.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()[0] == 0


def test_approve_time_dispatch_policy_head_drift_rejects_without_job(
    api_client,
    tmp_path,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    policy = _approve_dispatch_policy(
        main_module,
        seed=seed,
        audit_path=str(tmp_path / "dispatch-policy-audit.jsonl"),
    )
    assert policy["revision"] == 1
    request_body = {
        **_preview_request(seed),
        "target_selection": {
            "kind": "dispatch_policy_revision",
            "dispatch_policy_id": policy["id"],
        },
    }
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    assert preview.json()["plan"]["target"]["dispatch_policy_id"] == policy["id"]
    assert (
        preview.json()["plan"]["target"]["server_config_revision_id"]
        == seed["revision"]["id"]
    )
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-policy-drift-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    successor = _approve_dispatch_policy(
        main_module,
        seed=seed,
        audit_path=str(tmp_path / "dispatch-policy-audit.jsonl"),
        update=True,
    )
    assert successor["revision"] == 2
    assert successor["id"] != policy["id"]

    _session_for(client, main_module, REVIEWER_ID)
    approval_id = submitted.json()["approval_id"]
    rejected = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-policy-drift-approve"},
        json={"decision": "approve", "note": "policy revalidation"},
    )
    assert rejected.status_code == 202, rejected.json()
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["reason_code"] == "execution_plan_stale"
    assert rejected.json()["job_id"] is None
    with main_module.app_state.db.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()[0] == 0


def test_submit_audit_failure_rolls_back_every_row_and_retry_succeeds(
    api_client,
    monkeypatch,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    submit_body = {
        **request_body,
        "expected_plan_digest": preview.json()["plan_digest"],
    }
    database = main_module.app_state.db
    before = _counts(database)
    original_append = database.append_durable_audit_event_in_transaction

    def fail_materialization(cursor, **kwargs):
        if kwargs.get("action") == "execution_plan_v2_materialized":
            raise RuntimeError("injected ExecutionPlan v2 submit audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_materialization,
    )
    with pytest.raises(RuntimeError, match="submit audit failure"):
        client.post(
            f"/api/v2/projects/{seed['project_id']}/run-requests",
            headers={"Idempotency-Key": "run-submit-audit-rollback"},
            json=submit_body,
        )
    assert _counts(database) == before

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        original_append,
    )
    retried = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-submit-audit-rollback"},
        json=submit_body,
    )
    assert retried.status_code == 202, retried.json()
    assert retried.json()["replayed"] is False


def test_approval_audit_failure_rolls_back_job_and_retry_succeeds(
    api_client,
    monkeypatch,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-approval-audit-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    database = main_module.app_state.db
    approval_id = submitted.json()["approval_id"]
    _session_for(client, main_module, REVIEWER_ID)
    before = _counts(database)
    original_append = database.append_durable_audit_event_in_transaction

    def fail_job_materialization(cursor, **kwargs):
        if kwargs.get("action") == "execution_plan_v2_job_materialized":
            raise RuntimeError("injected ExecutionPlan v2 approval audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_job_materialization,
    )
    with pytest.raises(RuntimeError, match="approval audit failure"):
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            headers={"Idempotency-Key": "run-approval-audit-decide"},
            json={"decision": "approve", "note": "reviewed"},
        )
    assert _counts(database) == before
    assert database.get_approval(approval_id).status == "pending"
    with database.cursor() as cursor:
        plan = cursor.execute(
            "SELECT job_id FROM execution_plans WHERE request_approval_id = ?",
            (approval_id,),
        ).fetchone()
        assert plan is not None
        assert plan["job_id"] is None

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        original_append,
    )
    retried = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-approval-audit-decide"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert retried.status_code == 202, retried.json()
    assert retried.json()["status"] == "approved"
    assert retried.json()["replayed"] is False
    assert retried.json()["job_id"] is not None


def test_stale_rejection_audit_failure_rolls_back_and_retry_succeeds(
    api_client,
    monkeypatch,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    ).json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-stale-audit-submit"},
        json={**request_body, "expected_plan_digest": preview["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    _update_environment(
        main_module.app_state.db,
        project_id=seed["project_id"],
        environment_id=seed["environment"]["environment_id"],
        head_revision_id=seed["environment"]["environment_revision_id"],
        expected_revision=1,
    )

    database = main_module.app_state.db
    approval_id = submitted.json()["approval_id"]
    _session_for(client, main_module, REVIEWER_ID)
    before = _counts(database)
    original_append = database.append_durable_audit_event_in_transaction

    def fail_stale_rejection(cursor, **kwargs):
        if kwargs.get("action") == "execution_plan_v2_rejected_stale":
            raise RuntimeError("injected ExecutionPlan v2 stale audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_stale_rejection,
    )
    with pytest.raises(RuntimeError, match="stale audit failure"):
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            headers={"Idempotency-Key": "run-stale-audit-decide"},
            json={"decision": "approve", "note": "detect stale contract"},
        )
    assert _counts(database) == before
    assert database.get_approval(approval_id).status == "pending"
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()[0] == 0

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        original_append,
    )
    retried = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-stale-audit-decide"},
        json={"decision": "approve", "note": "detect stale contract"},
    )
    assert retried.status_code == 202, retried.json()
    assert retried.json()["status"] == "rejected"
    assert retried.json()["reason_code"] == "execution_plan_stale"
    assert retried.json()["job_id"] is None
    assert retried.json()["replayed"] is False


def test_alias_head_move_keeps_submitted_snapshot_and_usage_pinned(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    dataset = _owned_dataset_with_movable_alias(main_module.app_state.db, seed)
    request_body = {
        **_preview_request(seed),
        "dataset_selection": {
            "kind": "bindings",
            "bindings": [
                {
                    "name": "training",
                    "asset_id": dataset["asset"]["asset_id"],
                    "selection": {"kind": "alias", "alias_name": "latest"},
                }
            ],
        },
    }
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    preview_binding = preview.json()["plan"]["dataset_bindings"][0]
    assert preview_binding["snapshot_id"] == dataset["first_snapshot"]
    assert preview_binding["selection"]["kind"] == "alias"
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-alias-pinned-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    prior_alias = dataset["alias"]
    moved = _create_dataset_alias(
        main_module.app_state.db,
        project_id=seed["project_id"],
        asset_id=dataset["asset"]["asset_id"],
        snapshot_id=dataset["second_snapshot"],
        operation="move",
        expected_revision=prior_alias["revision"],
        expected_head_revision_id=prior_alias["alias_revision_id"],
        expected_head_revision_digest=prior_alias["revision_digest"],
    )
    assert moved["snapshot_id"] == dataset["second_snapshot"]

    _session_for(client, main_module, REVIEWER_ID)
    approval_id = submitted.json()["approval_id"]
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-alias-pinned-approve"},
        json={"decision": "approve", "note": "historical alias verified"},
    )
    assert approved.status_code == 202, approved.json()
    assert approved.json()["status"] == "approved"

    usage = main_module.app_state.db.get_dataset_asset_usage(
        dataset["asset"]["asset_id"],
        project_id=seed["project_id"],
    )
    assert usage is not None
    assert usage["project_defaults"] == {
        "state": "available",
        "items": [],
        "truncated": False,
    }
    assert usage["execution_plans"]["truncated"] is False
    assert usage["execution_plans"]["scope_truncated"] is False
    assert usage["execution_plans"]["items"] == [
        {
            "execution_plan_id": submitted.json()["execution_plan_id"],
            "plan_digest": submitted.json()["plan_digest"],
            "binding_name": "training",
            "snapshot_id": dataset["first_snapshot"],
            "selection_kind": "alias",
            "approval_id": approval_id,
            "approval_status": "approved",
            "job_id": approved.json()["job_id"],
            "created_at": usage["execution_plans"]["items"][0]["created_at"],
        }
    ]
    assert dataset["second_snapshot"] not in repr(
        usage["execution_plans"]["items"]
    )


def test_dataset_execution_plan_usage_is_bounded_and_deterministic(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    dataset = _owned_dataset_with_movable_alias(main_module.app_state.db, seed)
    request_body = {
        **_preview_request(seed),
        "dataset_selection": {
            "kind": "bindings",
            "bindings": [
                {
                    "name": "training",
                    "asset_id": dataset["asset"]["asset_id"],
                    "selection": {
                        "kind": "snapshot",
                        "snapshot_id": dataset["first_snapshot"],
                    },
                }
            ],
        },
    }
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-usage-bound-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    database = main_module.app_state.db
    clone_ids = []
    for index in range(100):
        clone_ids.append(
            _clone_pending_execution_plan(
                database,
                source_execution_plan_id=submitted.json()["execution_plan_id"],
                project_id=seed["project_id"],
                created_at=(
                    f"2098-01-01T00:{index // 60:02d}:{index % 60:02d}.000Z"
                ),
            )
        )

    first = database.get_dataset_asset_usage(
        dataset["asset"]["asset_id"],
        project_id=seed["project_id"],
    )
    second = database.get_dataset_asset_usage(
        dataset["asset"]["asset_id"],
        project_id=seed["project_id"],
    )
    assert first == second
    assert first is not None
    projection = first["execution_plans"]
    assert projection["state"] == "available"
    assert projection["truncated"] is True
    assert projection["scope_truncated"] is False
    assert len(projection["items"]) == 100
    assert [item["execution_plan_id"] for item in projection["items"]] == list(
        reversed(clone_ids)
    )
    assert submitted.json()["execution_plan_id"] not in {
        item["execution_plan_id"] for item in projection["items"]
    }
    assert {item["snapshot_id"] for item in projection["items"]} == {
        dataset["first_snapshot"]
    }
    assert {item["selection_kind"] for item in projection["items"]} == {
        "snapshot"
    }


def test_project_defaults_reference_does_not_invent_dataset_default_usage(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    dataset = _owned_dataset_with_movable_alias(main_module.app_state.db, seed)
    defaults_approval_id = _request_defaults(
        main_module.app_state.db,
        project_id=seed["project_id"],
        template_result=seed["template"],
        environment_revision_id=seed["environment"]["environment_revision_id"],
    )
    defaults = main_module.app_state.db.apply_project_defaults_change_decision(
        approval_id=defaults_approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    request_body = {
        **_preview_request(seed),
        "template_selection": {
            "kind": "project_defaults",
            "project_defaults_revision_id": defaults["defaults_revision_id"],
        },
        "dataset_selection": {
            "kind": "bindings",
            "bindings": [
                {
                    "name": "training",
                    "asset_id": dataset["asset"]["asset_id"],
                    "selection": {
                        "kind": "snapshot",
                        "snapshot_id": dataset["first_snapshot"],
                    },
                }
            ],
        },
    }
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    assert (
        preview.json()["plan"]["project_defaults"][
            "project_defaults_revision_id"
        ]
        == defaults["defaults_revision_id"]
    )
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-defaults-dataset-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    usage = main_module.app_state.db.get_dataset_asset_usage(
        dataset["asset"]["asset_id"],
        project_id=seed["project_id"],
    )
    assert usage is not None
    assert usage["project_defaults"] == {
        "state": "available",
        "items": [],
        "truncated": False,
    }
    assert [
        item["execution_plan_id"]
        for item in usage["execution_plans"]["items"]
    ] == [submitted.json()["execution_plan_id"]]


def test_revoked_shared_grant_is_rejected_at_approve_time(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    main_module.app_state.config.dataset_sharing_v2_enabled = True
    seed = _seed_execution_context(main_module)
    database = main_module.app_state.db
    source_project_id, _unused_target = _seed_sharing_projects(database)
    _insert_binding(
        database,
        project_id=seed["project_id"],
        actor_id=TARGET_OWNER,
        role=ProjectRoleV2.OWNER,
    )
    _insert_binding(
        database,
        project_id=seed["project_id"],
        actor_id=TARGET_REVIEWER,
        role=ProjectRoleV2.REVIEWER,
    )
    snapshot_id = "execution-plan-v2-shared-grant"
    _publish_shared_snapshot(database, snapshot_id)
    asset = _adopt_shared_dataset(database, source_project_id, snapshot_id)
    offer_approval_id = _request_offer(
        database,
        asset_id=asset["asset_id"],
        source_project_id=source_project_id,
        target_project_id=seed["project_id"],
        snapshot_ids=[snapshot_id],
        asset_digest=asset["asset_digest"],
    )
    offer = database.apply_dataset_share_offer_decision(
        approval_id=offer_approval_id,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    accept_approval_id = _request_accept(database, offer)
    accepted = database.apply_dataset_share_accept_decision(
        approval_id=accept_approval_id,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    grant = database.get_dataset_grant_read_model(accepted["grant_ids"][0])
    assert grant is not None

    request_body = {
        **_preview_request(seed),
        "dataset_selection": {
            "kind": "bindings",
            "bindings": [
                {
                    "name": "shared_training",
                    "asset_id": asset["asset_id"],
                    "selection": {"kind": "snapshot", "snapshot_id": snapshot_id},
                }
            ],
        },
    }
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-shared-grant-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()

    with SQLiteUnitOfWork(database) as unit_of_work:
        revoke_approval_id = unit_of_work.run(
            lambda cursor: database.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation="unlink",
                project_id=seed["project_id"],
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=TARGET_OWNER,
            )
        )
    database.apply_dataset_grant_revoke_decision(
        approval_id=revoke_approval_id,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    assert database.get_dataset_asset_usage(
        asset["asset_id"],
        project_id=seed["project_id"],
        sharing_enabled=True,
    ) is None
    source_usage = database.get_dataset_asset_usage(
        asset["asset_id"],
        project_id=source_project_id,
        sharing_enabled=True,
    )
    assert source_usage is not None
    assert source_usage["execution_plans"]["items"] == []

    _session_for(client, main_module, REVIEWER_ID)
    approval_id = submitted.json()["approval_id"]
    rejected = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-shared-grant-approve"},
        json={"decision": "approve", "note": "grant revalidation"},
    )
    assert rejected.status_code == 202, rejected.json()
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["reason_code"] == "execution_plan_stale"
    assert rejected.json()["job_id"] is None
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?",
            (approval_id,),
        ).fetchone()[0] == 0


def test_attempt_refuses_a_new_revision_on_the_same_server(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    request_body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json=request_body,
    )
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "run-target-drift-submit"},
        json={
            **request_body,
            "expected_plan_digest": preview.json()["plan_digest"],
        },
    )
    assert submitted.status_code == 202, submitted.json()
    _session_for(client, main_module, REVIEWER_ID)
    approval_id = submitted.json()["approval_id"]
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "run-target-drift-approve"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert approved.status_code == 202, approved.json()

    database = main_module.app_state.db
    changed_revision = _publish_changed_revision(database)
    assert changed_revision["server_name"] == "pilot-117"
    assert changed_revision["id"] != seed["revision"]["id"]
    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )
    with pytest.raises(ValueError, match="contract_digest_mismatch"):
        database.create_execution_attempt(
            job_id=approved.json()["job_id"],
            backend="ssh",
            server_config_revision_id=changed_revision["id"],
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
            initial_operation={
                "operation": "prepare",
                "payload": {"job_id": approved.json()["job_id"]},
            },
        )
    assert database.get_latest_execution_attempt_for_job(
        approved.json()["job_id"]
    ) is None
    assert database.get_job(approved.json()["job_id"]).status == "queued"
