from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.dataset_assets import DatasetAssetInput, DatasetDataCardV2
from app.dataset_publish import (
    DATASET_PUBLISH_APPROVAL_KIND,
    DatasetPublishPreviewRequest,
    RunOutputPublishSource,
    build_dataset_publish_preview,
    execute_dataset_publish_build,
    revalidate_dataset_publish_source,
    resume_dataset_publish,
)
from app.db import Database
from app.execution_contract import canonical_json, utf8_sha256
from app.identity import ActorType, ProjectRoleV2, generate_session_token
from app.project_bootstrap import RunTemplateSpecInput
from app.run_templates import build_run_template_revision
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


OWNER_ID = "22000000-0000-0000-0000-000000000091"
REVIEWER_ID = "22000000-0000-0000-0000-000000000092"
MANAGER_ID = "22000000-0000-0000-0000-000000000093"
OUTSIDER_ID = "22000000-0000-0000-0000-000000000094"
ADMIN_ID = "22000000-0000-0000-0000-000000000095"


def _binding(
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
                "2026-08-09T00:00:00.000Z",
            ),
        )


def _seed_project(database: Database, *, name: str = "publish-project") -> str:
    project_id = database.insert_project(
        f"{name}-{uuid.uuid4()}",
        f"/srv/projects/{name}",
    )
    for actor_id, display_name in (
        (OWNER_ID, "Publish owner"),
        (REVIEWER_ID, "Publish reviewer"),
        (MANAGER_ID, "Publish manager"),
        (OUTSIDER_ID, "Publish outsider"),
        (ADMIN_ID, "Publish platform admin"),
    ):
        if database.get_actor(actor_id) is None:
            database.insert_actor(
                actor_id=actor_id,
                actor_type=ActorType.HUMAN,
                display_name=display_name,
                platform_admin=actor_id == ADMIN_ID,
            )
    for actor_id, role in (
        (OWNER_ID, ProjectRoleV2.OWNER),
        (REVIEWER_ID, ProjectRoleV2.REVIEWER),
        (MANAGER_ID, ProjectRoleV2.DATASET_MANAGER),
    ):
        _binding(
            database,
            project_id=project_id,
            actor_id=actor_id,
            role=role,
        )
    return project_id


def _asset(name: str = "published-corpus") -> DatasetAssetInput:
    return DatasetAssetInput(
        name=name,
        description="Immutable Project-owned training records",
        data_card=DatasetDataCardV2(
            collection_method="Collected from approved local records",
            processing_method="Normalized deterministically",
            license="internal-approved",
            sensitive_data="No direct identifiers",
            counts=[{"name": "records", "value": 2}],
        ),
    )


def _local_source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "publish-root"
    source = root / "candidate"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("alpha\n", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"beta\x00")
    return root, source


def _config(tmp_path: Path, root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        dataset_publish_local_roots=(str(root),),
        dataset_snapshot_store_root=str(tmp_path / "snapshot-store"),
        dataset_snapshot_max_bytes=1024 * 1024,
        dataset_snapshot_shard_policy={
            "max_shard_bytes": 1024,
            "max_shard_files": 2,
        },
        local_home_dir=str(tmp_path),
    )


def _preview_request(source: Path, *, name: str = "published-corpus") -> DatasetPublishPreviewRequest:
    return DatasetPublishPreviewRequest.model_validate(
        {
            "source": {"kind": "local_path", "path": str(source)},
            "asset": _asset(name).model_dump(mode="json"),
            "initial_alias": "latest",
        }
    )


def _request_approval(
    database: Database,
    *,
    project_id: str,
    preview,
) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_dataset_publish_approval_in_transaction(
                cursor,
                project_id=project_id,
                preview=preview,
                requester_actor_id=MANAGER_ID,
            )
        )


def _begin_publish(
    database: Database,
    *,
    approval_id: int,
    preview,
    decider_id: str = OWNER_ID,
) -> dict:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.begin_dataset_publish_decision_in_transaction(
                cursor,
                approval_id=approval_id,
                decision_actor_id=decider_id,
                decision_mechanism="session",
                observed_source_candidate_digest=preview.source_candidate_digest,
                observed_manifest_digest=preview.manifest_digest,
            )
        )


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


def _enable_publish(main_module, *, root: Path, store: Path) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.dataset_assets_v2_enabled = True
    config.dataset_snapshot_v1_enabled = True
    config.dataset_snapshot_publish_enabled = True
    config.dataset_publish_v2_enabled = True
    config.dataset_publish_local_roots = (str(root),)
    config.dataset_snapshot_store_root = str(store)
    config.dataset_snapshot_max_bytes = 1024 * 1024
    config.authorization_mode = "enforce"


def test_local_preview_is_deterministic_read_only_and_path_redacted(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    config = _config(tmp_path, root)
    before = {
        table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "approvals",
            "api_idempotency_keys",
            "dataset_snapshots",
            "dataset_assets",
            "audit_events",
        )
    }
    first, first_path, first_candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    second, second_path, second_candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    assert first == second
    assert first_candidate == second_candidate
    assert first_path == second_path == str(source)
    assert first.source.kind == "local_path"
    assert first.source.relative_path == "candidate"
    serialized = canonical_json(first.model_dump(mode="json"))
    assert str(source) not in serialized
    assert str(root) not in serialized
    assert not (tmp_path / "snapshot-store").exists()
    assert before == {
        table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }


def test_local_publish_uses_one_approval_and_atomic_asset_alias_finalization(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    config = _config(tmp_path, root)
    preview, _source_path, _candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    approval_id = _request_approval(db, project_id=project_id, preview=preview)
    approval = db.get_verified_product_approval(approval_id)
    assert approval is not None
    assert approval.kind == DATASET_PUBLISH_APPROVAL_KIND
    assert str(source) not in canonical_json(approval.payload)
    assert db._conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0

    with pytest.raises(ValueError, match="high_risk_self_decision"):
        _begin_publish(
            db,
            approval_id=approval_id,
            preview=preview,
            decider_id=MANAGER_ID,
        )
    building = _begin_publish(db, approval_id=approval_id, preview=preview)
    assert building["state"] == "building"
    result = execute_dataset_publish_build(
        db,
        approval_id=approval_id,
        config=config,
    )
    assert result["state"] == "published"
    assert result["asset_id"] == approval.payload["target_asset"]["asset_id"]
    assert result["alias_revision_id"] == approval.payload["target_alias"][
        "revision_id"
    ]
    snapshot = db.get_dataset_snapshot(result["snapshot_id"])
    assert snapshot is not None
    assert snapshot.state == "published"
    assert snapshot.manifest_digest == preview.manifest_digest
    assert sum(
        row["file_count"]
        for row in db.get_dataset_snapshot_shards(result["snapshot_id"])
    ) == preview.file_count
    model = db.get_dataset_asset_read_model(result["asset_id"])
    assert model is not None
    assert model["contract"]["data_card"] == preview.asset.data_card.model_dump(
        mode="json"
    )
    assert model["active_aliases"][0]["alias_name"] == "latest"
    assert db._conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    actions = {
        event["action"] for event in db.list_durable_audit_events(limit=100)
    }
    assert {
        "approval_created",
        "approval_decided",
        "project_snapshot_published",
        "dataset_asset_published",
        "dataset_alias_revision_created",
    } <= actions
    assert str(source) not in repr(db.list_durable_audit_events(limit=100))


def test_source_drift_keeps_approval_pending_and_creates_no_building_row(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    config = _config(tmp_path, root)
    preview, _source_path, _candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    approval_id = _request_approval(db, project_id=project_id, preview=preview)
    (source / "a.txt").write_text("changed\n", encoding="utf-8")
    approval = db.get_verified_product_approval(approval_id)
    assert approval is not None
    with pytest.raises(ValueError, match="source drifted"):
        revalidate_dataset_publish_source(
            approval.payload,
            database=db,
            config=config,
        )
    assert db.get_approval(approval_id).status == "pending"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_snapshots WHERE state = 'building'"
    ).fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 0


def test_publish_name_reservation_has_one_winner_and_rejection_materializes_nothing(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    config = _config(tmp_path, root)
    preview, _source_path, _candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    first_id = _request_approval(db, project_id=project_id, preview=preview)
    second_id = _request_approval(db, project_id=project_id, preview=preview)

    first = _begin_publish(db, approval_id=first_id, preview=preview)
    assert first["state"] == "building"
    with pytest.raises(ValueError, match="name is already reserved"):
        _begin_publish(db, approval_id=second_id, preview=preview)
    assert db.get_approval(second_id).status == "pending"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_snapshots WHERE state = 'building'"
    ).fetchone()[0] == 1

    with SQLiteUnitOfWork(db) as unit_of_work:
        unit_of_work.run(
            lambda cursor: db.reject_dataset_publish_decision_in_transaction(
                cursor,
                approval_id=second_id,
                decision_actor_id=REVIEWER_ID,
                decision_mechanism="session",
                note="duplicate publish request",
            )
        )
    assert db.get_approval(second_id).status == "rejected"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_snapshots WHERE build_approval_id = ?",
        (second_id,),
    ).fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 0


def test_local_publish_rejects_escape_symlink_special_file_and_store_overlap(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    outside = tmp_path / "publish-root-prefix-collision"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    config = _config(tmp_path, root)
    with pytest.raises(ValueError, match="outside the allowlist"):
        build_dataset_publish_preview(
            _preview_request(outside),
            project_id=project_id,
            database=db,
            config=config,
        )

    link = source / "escape-link"
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(ValueError, match="symlink"):
        build_dataset_publish_preview(
            _preview_request(source),
            project_id=project_id,
            database=db,
            config=config,
        )
    link.unlink()

    fifo = source / "special"
    os.mkfifo(fifo)
    try:
        with pytest.raises(ValueError, match="unreproducible entry type"):
            build_dataset_publish_preview(
                _preview_request(source),
                project_id=project_id,
                database=db,
                config=config,
            )
    finally:
        fifo.unlink()

    overlap = _config(tmp_path, root)
    overlap.dataset_snapshot_store_root = str(source / "store")
    with pytest.raises(ValueError, match="overlap"):
        build_dataset_publish_preview(
            _preview_request(source),
            project_id=project_id,
            database=db,
            config=overlap,
        )

    root_link = tmp_path / "publish-root-link"
    root_link.symlink_to(root, target_is_directory=True)
    linked_root_config = _config(tmp_path, root_link)
    with pytest.raises(ValueError, match="roots are unavailable"):
        build_dataset_publish_preview(
            _preview_request(source),
            project_id=project_id,
            database=db,
            config=linked_root_config,
        )

    with pytest.raises(ValueError, match="canonical absolute"):
        DatasetPublishPreviewRequest.model_validate(
            {
                **_preview_request(source).model_dump(mode="json"),
                "source": {
                    "kind": "local_path",
                    "path": f"{root}/candidate/../candidate",
                },
            }
        )


def test_finalization_failure_rolls_back_and_same_approval_resumes(
    db: Database,
    tmp_path: Path,
    monkeypatch,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    config = _config(tmp_path, root)
    preview, _source_path, _candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    approval_id = _request_approval(db, project_id=project_id, preview=preview)
    _begin_publish(db, approval_id=approval_id, preview=preview)
    original_append = db.append_durable_audit_event_in_transaction

    def fail_after_rows(cursor, **kwargs):
        if kwargs.get("action") == "dataset_asset_published":
            raise RuntimeError("injected finalization interruption")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_after_rows)
    with pytest.raises(RuntimeError, match="injected finalization"):
        execute_dataset_publish_build(db, approval_id=approval_id, config=config)
    snapshot_id = db.get_verified_product_approval(approval_id).payload["snapshot_id"]
    assert db.get_dataset_snapshot(snapshot_id).state == "building"
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_snapshot_shards WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()[0] == 0
    assert any((tmp_path / "snapshot-store" / "blobs").rglob("*.tar"))

    monkeypatch.setattr(
        db,
        "append_durable_audit_event_in_transaction",
        original_append,
    )
    completed = resume_dataset_publish(db, snapshot_id=snapshot_id, config=config)
    assert completed["state"] == "published"
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_alias_revisions").fetchone()[0] == 1
    replay = execute_dataset_publish_build(db, approval_id=approval_id, config=config)
    assert replay == completed
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 1


def test_artifact_store_initialization_interruption_keeps_exact_build_resumable(
    db: Database,
    tmp_path: Path,
    monkeypatch,
):
    project_id = _seed_project(db)
    root, source = _local_source(tmp_path)
    config = _config(tmp_path, root)
    preview, _source_path, _candidate = build_dataset_publish_preview(
        _preview_request(source),
        project_id=project_id,
        database=db,
        config=config,
    )
    approval_id = _request_approval(db, project_id=project_id, preview=preview)
    building = _begin_publish(db, approval_id=approval_id, preview=preview)

    def interrupted_store(_config):
        raise OSError("sensitive store path must not escape")

    with monkeypatch.context() as scoped:
        scoped.setattr("app.dataset_publish.dataset_publish_store", interrupted_store)
        interrupted = execute_dataset_publish_build(
            db,
            approval_id=approval_id,
            config=config,
        )
    assert interrupted == {
        **building,
        "resume_required": True,
        "build_error": "artifact_store_interrupted",
    }
    assert "sensitive" not in repr(interrupted)
    assert db.get_dataset_snapshot(building["snapshot_id"]).state == "building"
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 0

    completed = resume_dataset_publish(
        db,
        snapshot_id=building["snapshot_id"],
        config=config,
    )
    assert completed["state"] == "published"
    assert completed["asset_id"] == building["asset_id"]


def _api_body(source: Path) -> dict:
    return {
        "source": {"kind": "local_path", "path": str(source)},
        "asset": _asset().model_dump(mode="json"),
        "initial_alias": "latest",
    }


@pytest.mark.usefixtures("legacy_posture")
def test_publish_api_feature_gate_mismatch_zero_writes_and_end_to_end(
    api_client,
    tmp_path: Path,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    root, source = _local_source(tmp_path)
    body = _api_body(source)
    disabled = client.post(
        f"/api/v2/projects/{project_id}/dataset-publish-previews",
        json=body,
    )
    assert disabled.status_code == 404

    _enable_publish(
        main_module,
        root=root,
        store=tmp_path / "api-snapshot-store",
    )
    _session_for(client, main_module, MANAGER_ID)
    preview_response = client.post(
        f"/api/v2/projects/{project_id}/dataset-publish-previews",
        json=body,
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert str(source) not in preview_response.text
    counts = {
        table: database._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "approvals",
            "api_idempotency_keys",
            "dataset_snapshots",
            "audit_events",
        )
    }
    stale = client.post(
        f"/api/v2/projects/{project_id}/dataset-publish-requests",
        headers={"Idempotency-Key": "publish-stale"},
        json={**body, "expected_preview_digest": "0" * 64},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "dataset_publish_preview_mismatch"
    assert counts == {
        table: database._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in counts
    }

    requested = client.post(
        f"/api/v2/projects/{project_id}/dataset-publish-requests",
        headers={"Idempotency-Key": "publish-request"},
        json={**body, "expected_preview_digest": preview["preview_digest"]},
    )
    assert requested.status_code == 202
    approval_id = requested.json()["approval_id"]
    replayed_request = client.post(
        f"/api/v2/projects/{project_id}/dataset-publish-requests",
        headers={"Idempotency-Key": "publish-request"},
        json={**body, "expected_preview_digest": preview["preview_digest"]},
    )
    assert replayed_request.status_code == 202
    assert replayed_request.json()["approval_id"] == approval_id
    assert replayed_request.json()["replayed"] is True

    _session_for(client, main_module, OWNER_ID)
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 200
    assert detail.json()["kind"] == DATASET_PUBLISH_APPROVAL_KIND
    assert detail.json()["can_decide"] is True
    assert detail.json()["payload_verified"] is True
    assert str(source) not in detail.text

    decided = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "publish-decision"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert decided.status_code == 202
    assert decided.json()["status"] == "approved"
    assert decided.json()["state"] == "published"
    replayed_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "publish-decision"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert replayed_decision.status_code == 202
    assert replayed_decision.json()["replayed"] is True
    assert replayed_decision.json()["state"] == "published"
    assert database._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 1

    other_project = _seed_project(database, name="publish-other")
    _session_for(client, main_module, OUTSIDER_ID)
    foreign = client.post(
        f"/api/v2/projects/{other_project}/dataset-publish-previews",
        json=body,
    )
    assert foreign.status_code == 404


def _published_input_asset(database: Database, project_id: str) -> tuple[str, str]:
    snapshot_id = "run-input-snapshot"
    approval_id = database.insert_approval("dataset_snapshot_build", {})
    digest = hashlib.sha256(snapshot_id.encode()).hexdigest()
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dataset_snapshots (
                id, dataset_name, dataset_version, state,
                source_candidate_digest, manifest_digest, manifest_path,
                descriptor_path, store_revision, shard_policy_json,
                file_count, total_bytes, build_approval_id, created_at, published_at
            ) VALUES (?, 'run-input', 'v1', 'published', ?, ?, 'manifest',
                      'descriptor', 'local-artifact-store-v1', '{}',
                      1, 1, ?, ?, ?)
            """,
            (
                snapshot_id,
                digest,
                digest,
                approval_id,
                "2026-08-09T00:00:00.000Z",
                "2026-08-09T00:00:01.000Z",
            ),
        )
    target = _asset("run-input-asset")
    with SQLiteUnitOfWork(database) as unit_of_work:
        adoption_id = unit_of_work.run(
            lambda cursor: database.create_dataset_asset_adoption_approval_in_transaction(
                cursor,
                project_id=project_id,
                snapshot_id=snapshot_id,
                asset=target,
                requester_actor_id=ADMIN_ID,
            )
        )
    adopted = database.apply_dataset_asset_adoption_decision(
        approval_id=adoption_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    return adopted["asset_id"], snapshot_id


def _seed_run_output_evidence(
    database: Database,
    *,
    project_id: str,
    tmp_path: Path,
    output_pattern: str = "model.bin",
    completion_payload_overrides: dict | None = None,
) -> tuple[str, int, str]:
    project = database.get_project(project_id)
    assert project is not None
    input_asset_id, input_snapshot_id = _published_input_asset(database, project_id)
    environment_id = "23000000-0000-0000-0000-000000000091"
    environment_revision_id = "23000000-0000-0000-0000-000000000092"
    run_profile_id = "23000000-0000-0000-0000-000000000093"
    server_revision_id = "23000000-0000-0000-0000-000000000094"
    plan_id = "23000000-0000-0000-0000-000000000095"
    attempt_id = "23000000-0000-0000-0000-000000000096"
    completion_id = "23000000-0000-0000-0000-000000000097"
    materialization_approval = database.insert_pinned_approval(
        kind="enqueue",
        payload={
            "authorized_operations": ["prepare", "launch", "collect"],
            "job_specs": [
                {
                    "role": "main",
                    "type": "adhoc",
                    "command_utf8_b64": base64.b64encode(
                        b"python produce.py"
                    ).decode("ascii"),
                    "command_sha256": utf8_sha256("python produce.py"),
                }
            ],
        },
        contract_version="enqueue-execution-v1",
    )
    database.update_approval(materialization_approval, status="approved")
    approval_digest = database.get_approval(materialization_approval).payload_sha256
    assert isinstance(approval_digest, str)
    template = build_run_template_revision(
        RunTemplateSpecInput.model_validate(
            {
                "name": "produce-model",
                "argv_template": [{"kind": "literal", "value": "python"}],
                "parameter_schema": [],
                "resource_requirements": {
                    "required_tags": [],
                    "min_gpu_count": 0,
                    "min_gpu_memory_mb": 0,
                    "min_available_ram_mb": 0,
                    "min_available_disk_mb": 0,
                    "exclusive_worker": True,
                },
                "output_declarations": [
                    {
                        "name": "model",
                        "kind": "file",
                        "path_pattern": output_pattern,
                        "required": True,
                    }
                ],
            }
        ),
        run_profile_id=run_profile_id,
        environment_revision_id=environment_revision_id,
        revision=1,
    )
    with database.cursor() as cursor:
        timestamp = "2026-08-09T00:00:00.000Z"
        cursor.execute(
            """
            INSERT INTO project_environments (
                id, project_id, name, created_approval_id,
                created_by_actor_id, created_at
            ) VALUES (?, ?, 'run-host', ?, ?, ?)
            """,
            (
                environment_id,
                project_id,
                materialization_approval,
                OWNER_ID,
                timestamp,
            ),
        )
        cursor.execute(
            """
            INSERT INTO environment_revisions (
                id, environment_id, project_id, revision, status,
                contract_version, setup_command, required_server_tags_json,
                working_directory_policy, non_secret_env_json,
                secret_references_json, preflight_checks_json,
                revision_digest, supersedes_id, approval_id,
                created_by_actor_id, created_at
            ) VALUES (?, ?, ?, 1, 'approved', 'host-environment-v1', '', '[]',
                      'project_checkout', '[]', '[]', '[]', ?, NULL, ?, ?, ?)
            """,
            (
                environment_revision_id,
                environment_id,
                project_id,
                "1" * 64,
                materialization_approval,
                OWNER_ID,
                timestamp,
            ),
        )
        cursor.execute(
            """
            INSERT INTO run_profiles (
                id, project_id, project_name, name, revision, status,
                command, setup_cmd, require_tag, supersedes_id,
                approval_id, created_by_actor_id, created_at
            ) VALUES (?, ?, ?, ?, 1, 'approved', NULL, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                run_profile_id,
                project_id,
                project.name,
                template.name,
                materialization_approval,
                OWNER_ID,
                timestamp,
            ),
        )
        cursor.execute(
            """
            INSERT INTO run_profile_specs (
                run_profile_id, project_id, contract_version,
                environment_revision_id, argv_template_json,
                parameter_schema_json, resource_requirements_json,
                output_declarations_json, spec_digest, approval_id,
                created_by_actor_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_profile_id,
                project_id,
                template.contract_version,
                environment_revision_id,
                canonical_json(
                    [item.model_dump(mode="json") for item in template.argv_template]
                ),
                canonical_json([]),
                canonical_json(template.resource_requirements.model_dump(mode="json")),
                canonical_json(
                    [
                        item.model_dump(mode="json")
                        for item in template.output_declarations
                    ]
                ),
                template.spec_digest,
                materialization_approval,
                OWNER_ID,
                timestamp,
            ),
        )
        cursor.execute(
            """
            INSERT INTO server_config_revisions (
                id, server_name, revision, normalized_target_json,
                credential_ref_json, target_identity_sha256,
                assignment_eligibility, publication_state, created_at
            ) VALUES (?, 'node-runner', 1, '{}', '{}', ?,
                      'legacy_observed', 'active', ?)
            """,
            (server_revision_id, "2" * 64, timestamp),
        )
        cursor.execute(
            """
            INSERT INTO jobs (
                type, project, command, depends_on, status, priority,
                created_at, finished_at, exit_code, execution_approval_id,
                approved_payload_sha256, execution_contract_version,
                execution_contract_role, approved_command_sha256
            ) VALUES ('adhoc', ?, 'python produce.py', '[]', 'done', 'normal',
                      ?, ?, 0, ?, ?, 'enqueue-execution-v1', 'main', ?)
            """,
            (
                project.name,
                timestamp,
                timestamp,
                materialization_approval,
                approval_digest,
                utf8_sha256("python produce.py"),
            ),
        )
        job_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO execution_plans (
                id, project_name, contract_version, plan_digest,
                command, command_sha256, reproducible, run_profile_id,
                dataset_snapshot_id, dataset_none, server_config_revision_id,
                request_approval_id, job_id, created_at
                ) VALUES (?, ?, 'execution-plan-v1', ?, 'python produce.py', ?, 0,
                      ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                plan_id,
                project.name,
                "3" * 64,
                utf8_sha256("python produce.py"),
                run_profile_id,
                input_snapshot_id,
                server_revision_id,
                materialization_approval,
                job_id,
                timestamp,
            ),
        )
        cursor.execute(
            """
            INSERT INTO execution_attempts (
                id, job_id, attempt_number, backend, server_name,
                server_config_revision_id, target_identity_sha256,
                execution_approval_id, approved_payload_sha256,
                execution_contract_version, state, liveness, fencing_token,
                scheduler_fencing_epoch, created_at, terminal_at, exit_code
            ) VALUES (?, ?, 1, 'node', 'node-runner', ?, ?, ?, ?,
                      'enqueue-execution-v1', 'done', 'known', ?, 1, ?, ?, 0)
            """,
            (
                attempt_id,
                job_id,
                server_revision_id,
                "2" * 64,
                materialization_approval,
                approval_digest,
                f"{attempt_id}-fence",
                timestamp,
                timestamp,
            ),
        )
        completion_payload_body = {
            "job_id": job_id,
            "terminal_state": "done",
            "server_name": "node-runner",
            "server_config_revision_id": server_revision_id,
        }
        completion_payload_body.update(completion_payload_overrides or {})
        completion_payload = canonical_json(completion_payload_body)
        output = canonical_json(
            {
                "job_id": job_id,
                "required": True,
                "collected": True,
                "result_path_available": True,
            }
        )
        cursor.execute(
            """
            INSERT INTO execution_completion_operations (
                id, attempt_id, job_id, operation, idempotency_key,
                payload_json, payload_sha256, state, attempt_count,
                output_json, output_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, 'result_collection', ?, ?, ?, 'delivered', 1,
                      ?, ?, ?, ?)
            """,
            (
                completion_id,
                attempt_id,
                job_id,
                f"completion:{completion_id}",
                completion_payload,
                utf8_sha256(completion_payload),
                output,
                utf8_sha256(output),
                timestamp,
                timestamp,
            ),
        )
    result_dir = tmp_path / "results" / str(job_id)
    result_dir.mkdir(parents=True)
    (result_dir / "model.bin").write_bytes(b"model-weights")
    return plan_id, job_id, input_asset_id


def test_run_output_rejects_completion_payload_target_identity_mismatch(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    plan_id, _job_id, _input_asset_id = _seed_run_output_evidence(
        db,
        project_id=project_id,
        tmp_path=tmp_path,
        completion_payload_overrides={
            "server_config_revision_id": str(uuid.uuid4()),
        },
    )

    assert db.get_dataset_publish_run_output_evidence(
        project_id=project_id,
        plan_id=plan_id,
        output_declaration_name="model",
    ) == {"state": "result_collection_evidence_invalid"}


def test_run_output_requires_exact_typed_completion_evidence_and_creates_lineage(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    plan_id, job_id, input_asset_id = _seed_run_output_evidence(
        db,
        project_id=project_id,
        tmp_path=tmp_path,
    )
    config = SimpleNamespace(
        dataset_publish_local_roots=(),
        dataset_snapshot_store_root=str(tmp_path / "run-snapshot-store"),
        dataset_snapshot_max_bytes=1024 * 1024,
        dataset_snapshot_shard_policy={
            "max_shard_bytes": 1024,
            "max_shard_files": 2,
        },
        local_home_dir=str(tmp_path),
    )
    request = DatasetPublishPreviewRequest(
        source=RunOutputPublishSource(
            plan_id=plan_id,
            output_declaration_name="model",
        ),
        asset=_asset("run-output-asset"),
        initial_alias="candidate",
    )
    preview, source_path, _candidate = build_dataset_publish_preview(
        request,
        project_id=project_id,
        database=db,
        config=config,
    )
    assert source_path == str(tmp_path / "results" / str(job_id) / "model.bin")
    assert preview.source.kind == "run_output"
    assert preview.source.input_asset_id == input_asset_id
    approval_id = _request_approval(db, project_id=project_id, preview=preview)
    _begin_publish(db, approval_id=approval_id, preview=preview, decider_id=REVIEWER_ID)
    completed = execute_dataset_publish_build(
        db,
        approval_id=approval_id,
        config=config,
    )
    assert completed["state"] == "published"
    lineage = db._conn.execute("SELECT * FROM dataset_lineage_edges").fetchone()
    assert lineage is not None
    assert lineage["input_asset_id"] == input_asset_id
    assert lineage["output_asset_id"] == completed["asset_id"]
    assert lineage["producing_execution_plan_id"] == plan_id
    assert lineage["output_declaration_name"] == "model"

    with db.cursor() as cursor:
        evidence = json.loads(
            cursor.execute(
                """
                SELECT output_json FROM execution_completion_operations
                WHERE job_id = ? AND operation = 'result_collection'
                """,
                (job_id,),
            ).fetchone()[0]
        )
        evidence["collected"] = False
        output_json = canonical_json(evidence)
        cursor.execute(
            """
            UPDATE execution_completion_operations
            SET output_json = ?, output_sha256 = ?
            WHERE job_id = ? AND operation = 'result_collection'
            """,
            (output_json, utf8_sha256(output_json), job_id),
        )
    current = db.get_dataset_publish_run_output_evidence(
        project_id=project_id,
        plan_id=plan_id,
        output_declaration_name="model",
    )
    assert current == {"state": "result_collection_evidence_invalid"}


def test_run_output_rejects_result_path_escape_and_multiple_matches(
    db: Database,
    tmp_path: Path,
):
    project_id = _seed_project(db)
    plan_id, job_id, _input_asset_id = _seed_run_output_evidence(
        db,
        project_id=project_id,
        tmp_path=tmp_path,
        output_pattern="*.bin",
    )
    config = SimpleNamespace(
        dataset_publish_local_roots=(),
        dataset_snapshot_store_root=str(tmp_path / "safe-store"),
        dataset_snapshot_max_bytes=1024 * 1024,
        local_home_dir=str(tmp_path),
    )
    request = DatasetPublishPreviewRequest(
        source=RunOutputPublishSource(
            plan_id=plan_id,
            output_declaration_name="model",
        ),
        asset=_asset("escaped-output"),
    )
    result_root = tmp_path / "results" / str(job_id)
    (result_root / "model.bin").unlink()
    (result_root / "model.bin").symlink_to(tmp_path / "outside-model")
    (tmp_path / "outside-model").write_bytes(b"secret")
    with pytest.raises(ValueError, match="symlink"):
        build_dataset_publish_preview(
            request,
            project_id=project_id,
            database=db,
            config=config,
        )

    (result_root / "model.bin").unlink()
    (result_root / "model.bin").write_bytes(b"one")
    (result_root / "second.bin").write_bytes(b"two")
    with pytest.raises(ValueError, match="exactly one"):
        build_dataset_publish_preview(
            request,
            project_id=project_id,
            database=db,
            config=config,
        )
