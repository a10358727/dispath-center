import asyncio
import json
from pathlib import Path

import pytest

from app.approvals import approve
from app.server_attempt_preflight import (
    ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
)
from app.server_publication import credential_reference
from app.wp2d_canary import (
    CONTRACT_VERSION,
    PURPOSE,
    Wp2dCanaryRequestError,
    request_wp2d_canary_approval,
)


CANDIDATE_COMMIT = "a" * 40
CANARY_COMMAND = "echo wp2d-start && sleep 1 && echo wp2d-done"


def _seed_active_revision(database, *, preflight=True):
    normalized_target = {
        "backend": "ssh",
        "host": "192.0.2.20",
        "port": 22,
        "user": "worker",
        "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"],
    }
    approval_id = database.insert_pinned_approval(
        kind="server_add",
        contract_version="server-config-v1",
        payload={
            "operation": "add",
            "server_name": "canary-a",
            "normalized_target": normalized_target,
            "credential_ref": credential_reference(
                {"key": "/dispatch-test/nonexistent-key"}
            ),
            "yaml_before_sha256": "b" * 64,
            "yaml_after_sha256": "c" * 64,
        },
    )
    mutation = database.prepare_server_config_mutation(
        approval_id=approval_id,
        operation="add",
        server_name="canary-a",
        normalized_target=normalized_target,
        credential_ref=credential_reference(
            {"key": "/dispatch-test/nonexistent-key"}
        ),
        yaml_before_sha256="b" * 64,
        yaml_after_sha256="c" * 64,
        decision_actor_id="human-reviewer",
    )
    database.transition_server_config_mutation(
        mutation_id=mutation["id"],
        expected_state="intent",
        new_state="yaml_applied",
        observed_yaml_sha256="c" * 64,
    )
    database.activate_server_config_mutation(
        mutation_id=mutation["id"],
        observed_yaml_sha256="c" * 64,
    )
    revision = mutation["prepared_revision"]
    if preflight:
        revision = database.record_server_attempt_backend_preflight(
            server_name="canary-a",
            revision_id=revision["id"],
            status="eligible",
            contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
            filesystem_type="ext2/ext3/ext4",
        )
    return revision


def _request(database, audit_path):
    return request_wp2d_canary_approval(
        database,
        server_name="canary-a",
        candidate_commit=CANDIDATE_COMMIT,
        command=CANARY_COMMAND,
        acknowledge_non_production=True,
        audit_path=audit_path,
    )


def test_request_creates_only_exact_pending_pinned_approval(db, audit_path):
    revision = _seed_active_revision(db)

    approval = _request(db, audit_path)

    assert approval.status == "pending"
    assert approval.payload_contract_version == CONTRACT_VERSION
    assert approval.payload_sha256
    assert approval.payload["purpose"] == PURPOSE
    assert approval.payload["candidate_commit"] == CANDIDATE_COMMIT
    assert approval.payload["server_config_revision_id"] == revision["id"]
    assert approval.payload["authorized_operations"] == [
        "prepare",
        "launch",
        "collect",
    ]
    assert approval.payload["job_specs"][0]["pin_server"] == "canary-a"
    assert db.list_jobs() == []

    audit_text = Path(audit_path).read_text(encoding="utf-8")
    assert CANARY_COMMAND not in audit_text
    event = json.loads(audit_text.strip())
    assert event["params"]["purpose"] == PURPOSE
    assert event["params"]["command_sha256"]


def test_request_requires_explicit_nonproduction_acknowledgement(db, audit_path):
    _seed_active_revision(db)
    with pytest.raises(Wp2dCanaryRequestError, match="acknowledge_non_production"):
        request_wp2d_canary_approval(
            db,
            server_name="canary-a",
            candidate_commit=CANDIDATE_COMMIT,
            command=CANARY_COMMAND,
            acknowledge_non_production=False,
            audit_path=audit_path,
        )
    assert db.list_approvals(kind="enqueue") == []


def test_request_rejects_target_without_revision_scoped_d5_evidence(db, audit_path):
    _seed_active_revision(db, preflight=False)
    with pytest.raises(Wp2dCanaryRequestError, match="not_attempt_eligible"):
        _request(db, audit_path)
    assert db.list_approvals(kind="enqueue") == []


def test_request_rejects_dangerous_command_without_approval(db, audit_path):
    _seed_active_revision(db)
    with pytest.raises(Wp2dCanaryRequestError, match="command rejected"):
        request_wp2d_canary_approval(
            db,
            server_name="canary-a",
            candidate_commit=CANDIDATE_COMMIT,
            command="rm -rf /tmp/canary",
            acknowledge_non_production=True,
            audit_path=audit_path,
        )
    assert db.list_approvals(kind="enqueue") == []


def test_manual_approval_atomically_materializes_attempt_eligible_job(db, audit_path):
    revision = _seed_active_revision(db)
    approval = _request(db, audit_path)

    result = asyncio.run(approve(db, approval.id, audit_path=audit_path))

    decided = result["approval"]
    job = result["job"]
    assert decided.status == "approved"
    assert decided.materialization_started_at
    assert decided.decision_mechanism == "manual"
    assert job.status == "queued"
    assert job.pin_server == "canary-a"
    assert job.execution_approval_id == approval.id
    assert job.approved_payload_sha256 == approval.payload_sha256
    assert job.execution_contract_version == CONTRACT_VERSION
    assert job.execution_contract_role == "main"
    assert job.approved_command_sha256 == approval.payload["job_specs"][0][
        "command_sha256"
    ]
    assert result["job_ids"] == {"main": job.id}
    assert revision["id"] == approval.payload["server_config_revision_id"]


def test_canary_pinned_approval_cannot_use_web_direct_or_auto_rule(db, audit_path):
    _seed_active_revision(db)
    approval = _request(db, audit_path)

    with pytest.raises(ValueError, match="manual human approval"):
        asyncio.run(
            approve(
                db,
                approval.id,
                audit_path=audit_path,
                approved_by="web-direct",
            )
        )

    assert db.get_approval(approval.id).status == "pending"
    assert db.list_jobs() == []


def test_approval_api_projects_verified_command_for_human_review(api_client):
    client, main_module = api_client
    _seed_active_revision(main_module.app_state.db)
    approval = request_wp2d_canary_approval(
        main_module.app_state.db,
        server_name="canary-a",
        candidate_commit=CANDIDATE_COMMIT,
        command=CANARY_COMMAND,
        acknowledge_non_production=True,
        audit_path=main_module.app_state.config.audit_path,
    )

    response = client.get("/approvals")

    assert response.status_code == 200
    item = next(row for row in response.json() if row["id"] == approval.id)
    assert item["review_payload"]["command"] == CANARY_COMMAND
    assert item["review_payload"]["command_sha256"] == approval.payload[
        "job_specs"
    ][0]["command_sha256"]
    assert item["review_payload"]["pin_server"] == "canary-a"
