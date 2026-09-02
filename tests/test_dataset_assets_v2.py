from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid

import pytest
from pydantic import ValidationError

from app.dataset_assets import (
    DatasetAliasCreateRequest,
    DatasetAliasMoveRequest,
    DatasetAssetInput,
    DatasetDataCardV2,
)
from app.db import Database
from app.identity import ActorType, ProjectRoleV2, generate_session_token
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


OWNER_ID = "20000000-0000-0000-0000-000000000071"
REVIEWER_ID = "20000000-0000-0000-0000-000000000072"
MANAGER_ID = "20000000-0000-0000-0000-000000000073"
ADMIN_ID = "20000000-0000-0000-0000-000000000074"
OUTSIDER_ID = "20000000-0000-0000-0000-000000000075"


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


def _seed_project(database: Database, *, name: str = "dataset-project") -> str:
    project_id = database.insert_project(
        f"{name}-{uuid.uuid4()}",
        f"/srv/projects/{name}",
    )
    for actor_id, display_name, platform_admin in (
        (OWNER_ID, "Dataset owner", False),
        (REVIEWER_ID, "Dataset reviewer", False),
        (MANAGER_ID, "Dataset manager", False),
        (ADMIN_ID, "Platform admin", True),
        (OUTSIDER_ID, "Outsider", False),
    ):
        if database.get_actor(actor_id) is None:
            database.insert_actor(
                actor_id=actor_id,
                actor_type=ActorType.HUMAN,
                display_name=display_name,
                platform_admin=platform_admin,
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
        actor_id=MANAGER_ID,
        role=ProjectRoleV2.DATASET_MANAGER,
    )
    return project_id


def _asset_input(name: str = "training-corpus") -> DatasetAssetInput:
    return DatasetAssetInput(
        name=name,
        description="Approved immutable training corpus",
        data_card=DatasetDataCardV2(
            collection_method="Curated from approved records",
            processing_method="Deduplicated and normalized",
            license="internal-approved",
            sensitive_data="No direct identifiers",
            counts=[{"name": "records", "value": 10}],
        ),
    )


def _oversized_data_card() -> dict:
    text = "x" * 2000
    return {
        "collection_method": text,
        "processing_method": text,
        "license": text,
        "use_restrictions": text,
        "sensitive_data": text,
        "known_issues": text,
        "recommended_use": text,
        "counts": [
            {"name": f"{index:02d}{'a' * 62}", "value": index}
            for index in range(64)
        ],
    }


def _published_snapshot(database: Database, snapshot_id: str) -> int:
    approval_id = database.insert_approval("dataset_snapshot_build", {})
    digest = hashlib.sha256(snapshot_id.encode()).hexdigest()
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dataset_snapshots (
                id, dataset_name, dataset_version, state,
                source_candidate_digest, manifest_digest, manifest_path,
                descriptor_path, store_revision, shard_policy_json,
                file_count, total_bytes, build_approval_id,
                created_at, published_at
            ) VALUES (?, ?, 'v1', 'published', ?, ?, ?, ?, 'store-v1',
                      '{}', 10, 1024, ?, ?, ?)
            """,
            (
                snapshot_id,
                "legacy-secret-registry-name",
                digest,
                digest,
                "/sensitive/manifest/path",
                "/sensitive/descriptor/path",
                approval_id,
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:01+00:00",
            ),
        )
    return approval_id


def _request_adoption(
    database: Database,
    *,
    project_id: str,
    snapshot_id: str,
    name: str = "training-corpus",
) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_dataset_asset_adoption_approval_in_transaction(
                cursor,
                project_id=project_id,
                snapshot_id=snapshot_id,
                asset=_asset_input(name),
                requester_actor_id=ADMIN_ID,
            )
        )


def _adopt(
    database: Database,
    *,
    project_id: str,
    snapshot_id: str,
    name: str = "training-corpus",
    decider_id: str = OWNER_ID,
) -> dict:
    approval_id = _request_adoption(
        database,
        project_id=project_id,
        snapshot_id=snapshot_id,
        name=name,
    )
    return database.apply_dataset_asset_adoption_decision(
        approval_id=approval_id,
        decision_actor_id=decider_id,
        decision_mechanism="session",
    )


def _request_alias(
    database: Database,
    *,
    asset_id: str,
    request: DatasetAliasCreateRequest | DatasetAliasMoveRequest,
) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_dataset_alias_change_approval_in_transaction(
                cursor,
                project_id=request.project_id,
                asset_id=asset_id,
                operation=request.operation,
                alias_name=request.alias_name,
                snapshot_id=request.snapshot_id,
                expected_revision=request.expected_revision,
                expected_head_revision_id=request.expected_head_revision_id,
                expected_head_revision_digest=request.expected_head_revision_digest,
                requester_actor_id=MANAGER_ID,
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


def _enable_dataset_assets(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.dataset_assets_v2_enabled = True
    config.authorization_mode = "enforce"


def _insert_execution_plan(
    database: Database,
    *,
    plan_id: str,
    snapshot_id: str,
) -> None:
    server_revision_id = "50000000-0000-0000-0000-000000000071"
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT OR IGNORE INTO server_config_revisions (
                id, server_name, revision, normalized_target_json,
                credential_ref_json, target_identity_sha256,
                assignment_eligibility, publication_state, created_at
            ) VALUES (?, 'worker-117', 1, '{}', '{}', ?,
                      'legacy_observed', 'active', ?)
            """,
            (
                server_revision_id,
                "1" * 64,
                "2026-08-01T00:00:00+00:00",
            ),
        )
        cursor.execute(
            """
            INSERT INTO execution_plans (
                id, project_name, contract_version, plan_digest,
                command, command_sha256, reproducible, dataset_snapshot_id,
                dataset_none, server_config_revision_id, created_at
            ) VALUES (?, 'dataset-project', 'execution-plan-v1', ?,
                      'python train.py', ?, 0, ?, 0, ?, ?)
            """,
            (
                plan_id,
                hashlib.sha256(plan_id.encode()).hexdigest(),
                hashlib.sha256(b"python train.py").hexdigest(),
                snapshot_id,
                server_revision_id,
                "2026-08-01T00:00:00+00:00",
            ),
        )


def test_dataset_contracts_are_strict_bounded_and_snapshot_ids_are_opaque_text():
    asset = _asset_input()
    assert asset.data_card.counts[0].value == 10
    with pytest.raises(ValidationError):
        DatasetAssetInput.model_validate(
            {
                **asset.model_dump(mode="json"),
                "unexpected": "secret",
            }
        )
    with pytest.raises(ValidationError, match="sorted and unique"):
        DatasetDataCardV2(
            collection_method="collection",
            processing_method="processing",
            counts=[
                {"name": "z", "value": 1},
                {"name": "a", "value": 2},
            ],
        )
    with pytest.raises(ValidationError, match="16384 canonical UTF-8 bytes"):
        DatasetDataCardV2.model_validate(_oversized_data_card())
    request = DatasetAliasCreateRequest(
        operation="create",
        project_id="10000000-0000-0000-0000-000000000071",
        alias_name="latest",
        snapshot_id="sha256:opaque-snapshot-identity",
        expected_revision=0,
    )
    assert request.snapshot_id == "sha256:opaque-snapshot-identity"


def test_adoption_is_digest_bound_two_person_and_atomic(db: Database):
    project_id = _seed_project(db)
    snapshot_id = "legacy-snapshot-alpha"
    _published_snapshot(db, snapshot_id)
    approval_id = _request_adoption(
        db,
        project_id=project_id,
        snapshot_id=snapshot_id,
    )

    approval = db.get_verified_product_approval(approval_id)
    assert approval is not None
    assert approval.kind == "dataset_asset_adoption_v2"
    assert approval.payload["snapshot_id"] == snapshot_id
    assert len(approval.payload_sha256) == 64
    with pytest.raises(ValueError, match="high_risk_self_decision"):
        db.apply_dataset_asset_adoption_decision(
            approval_id=approval_id,
            decision_actor_id=ADMIN_ID,
            decision_mechanism="session",
        )

    result = db.apply_dataset_asset_adoption_decision(
        approval_id=approval_id,
        decision_actor_id=OWNER_ID,
        decision_mechanism="session",
    )
    model = db.get_dataset_asset_read_model(result["asset_id"])
    assert model is not None
    assert model["contract"]["owning_project_id"] == project_id
    assert [item["snapshot_id"] for item in model["snapshots"]] == [snapshot_id]
    assert db.get_approval(approval_id).status == "approved"
    assert any(
        event["action"] == "dataset_asset_adopted"
        for event in db.list_durable_audit_events(limit=100)
    )


def test_adoption_revalidates_published_unlinked_state_without_partial_rows(db: Database):
    project_id = _seed_project(db)
    snapshot_id = "legacy-snapshot-stale"
    _published_snapshot(db, snapshot_id)
    approval_id = _request_adoption(
        db,
        project_id=project_id,
        snapshot_id=snapshot_id,
    )
    other_snapshot = "legacy-snapshot-linked-elsewhere"
    _published_snapshot(db, other_snapshot)
    other_asset_id = _adopt(
        db,
        project_id=project_id,
        snapshot_id=other_snapshot,
        name="other-asset",
    )["asset_id"]
    link_approval = db.insert_approval("dataset_snapshot_build", {})
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dataset_asset_snapshots (
                asset_id, owning_project_id, snapshot_id, link_kind,
                link_approval_id, linked_by_actor_id, linked_at
            ) VALUES (?, ?, ?, 'publish', ?, ?, ?)
            """,
            (
                other_asset_id,
                project_id,
                snapshot_id,
                link_approval,
                OWNER_ID,
                "2026-08-01T00:00:03+00:00",
            ),
        )
    with pytest.raises(ValueError, match="already linked"):
        db.apply_dataset_asset_adoption_decision(
            approval_id=approval_id,
            decision_actor_id=OWNER_ID,
            decision_mechanism="session",
        )
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_assets").fetchone()[0] == 1
    assert db.get_approval(approval_id).status == "pending"


def test_alias_moves_append_history_and_never_change_resolved_plan(db: Database):
    project_id = _seed_project(db)
    first_snapshot = "snapshot-alias-one"
    second_snapshot = "snapshot-alias-two"
    _published_snapshot(db, first_snapshot)
    second_approval = _published_snapshot(db, second_snapshot)
    adopted = _adopt(db, project_id=project_id, snapshot_id=first_snapshot)
    asset_id = adopted["asset_id"]
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dataset_asset_snapshots (
                asset_id, owning_project_id, snapshot_id, link_kind,
                link_approval_id, linked_by_actor_id, linked_at
            ) VALUES (?, ?, ?, 'publish', ?, ?, ?)
            """,
            (
                asset_id,
                project_id,
                second_snapshot,
                second_approval,
                OWNER_ID,
                "2026-08-01T00:00:02+00:00",
            ),
        )
    plan_id = "60000000-0000-0000-0000-000000000071"
    _insert_execution_plan(db, plan_id=plan_id, snapshot_id=first_snapshot)

    create_id = _request_alias(
        db,
        asset_id=asset_id,
        request=DatasetAliasCreateRequest(
            operation="create",
            project_id=project_id,
            alias_name="latest",
            snapshot_id=first_snapshot,
            expected_revision=0,
        ),
    )
    created = db.apply_dataset_alias_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    move_id = _request_alias(
        db,
        asset_id=asset_id,
        request=DatasetAliasMoveRequest(
            operation="move",
            project_id=project_id,
            alias_name="latest",
            snapshot_id=second_snapshot,
            expected_revision=1,
            expected_head_revision_id=created["alias_revision_id"],
            expected_head_revision_digest=created["revision_digest"],
        ),
    )
    competing_move_id = _request_alias(
        db,
        asset_id=asset_id,
        request=DatasetAliasMoveRequest(
            operation="move",
            project_id=project_id,
            alias_name="latest",
            snapshot_id=second_snapshot,
            expected_revision=1,
            expected_head_revision_id=created["alias_revision_id"],
            expected_head_revision_digest=created["revision_digest"],
        ),
    )
    moved = db.apply_dataset_alias_change_decision(
        approval_id=move_id,
        decision_actor_id=OWNER_ID,
        decision_mechanism="session",
    )
    with pytest.raises(ValueError, match="head is stale"):
        db.apply_dataset_alias_change_decision(
            approval_id=competing_move_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="session",
        )
    rows = db._conn.execute(
        "SELECT revision, snapshot_id FROM dataset_alias_revisions ORDER BY revision"
    ).fetchall()
    assert [(row["revision"], row["snapshot_id"]) for row in rows] == [
        (1, first_snapshot),
        (2, second_snapshot),
    ]
    assert moved["revision"] == 2
    assert db._conn.execute(
        "SELECT dataset_snapshot_id FROM execution_plans WHERE id = ?",
        (plan_id,),
    ).fetchone()[0] == first_snapshot
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE dataset_alias_revisions SET snapshot_id = ? WHERE revision = 1",
                (second_snapshot,),
            )


def test_same_project_alias_identity_and_cas_are_isolated_by_asset(db: Database):
    project_id = _seed_project(db)
    first_snapshot = "snapshot-alias-asset-one"
    second_snapshot = "snapshot-alias-asset-two"
    _published_snapshot(db, first_snapshot)
    _published_snapshot(db, second_snapshot)
    first_asset = _adopt(
        db,
        project_id=project_id,
        snapshot_id=first_snapshot,
        name="alias-asset-one",
    )["asset_id"]
    second_asset = _adopt(
        db,
        project_id=project_id,
        snapshot_id=second_snapshot,
        name="alias-asset-two",
        decider_id=REVIEWER_ID,
    )["asset_id"]

    heads = []
    for asset_id, snapshot_id, decider_id in (
        (first_asset, first_snapshot, REVIEWER_ID),
        (second_asset, second_snapshot, OWNER_ID),
    ):
        approval_id = _request_alias(
            db,
            asset_id=asset_id,
            request=DatasetAliasCreateRequest(
                operation="create",
                project_id=project_id,
                alias_name="latest",
                snapshot_id=snapshot_id,
                expected_revision=0,
            ),
        )
        heads.append(
            db.apply_dataset_alias_change_decision(
                approval_id=approval_id,
                decision_actor_id=decider_id,
                decision_mechanism="session",
            )
        )

    assert [head["revision"] for head in heads] == [1, 1]
    rows = db._conn.execute(
        """
        SELECT asset_id, alias_name, revision FROM dataset_alias_revisions
        ORDER BY asset_id
        """
    ).fetchall()
    assert {
        (row["asset_id"], row["alias_name"], row["revision"])
        for row in rows
    } == {
        (first_asset, "latest", 1),
        (second_asset, "latest", 1),
    }

    with pytest.raises(ValueError, match="head is stale"):
        _request_alias(
            db,
            asset_id=second_asset,
            request=DatasetAliasMoveRequest(
                operation="move",
                project_id=project_id,
                alias_name="latest",
                snapshot_id=second_snapshot,
                expected_revision=1,
                expected_head_revision_id=heads[0]["alias_revision_id"],
                expected_head_revision_digest=heads[0]["revision_digest"],
            ),
        )

    own_move_id = _request_alias(
        db,
        asset_id=second_asset,
        request=DatasetAliasMoveRequest(
            operation="move",
            project_id=project_id,
            alias_name="latest",
            snapshot_id=second_snapshot,
            expected_revision=1,
            expected_head_revision_id=heads[1]["alias_revision_id"],
            expected_head_revision_digest=heads[1]["revision_digest"],
        ),
    )
    own_move = db.apply_dataset_alias_change_decision(
        approval_id=own_move_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    assert own_move["revision"] == 2
    first_model = db.get_dataset_asset_read_model(first_asset)
    second_model = db.get_dataset_asset_read_model(second_asset)
    assert first_model is not None
    assert second_model is not None
    first_head = first_model["active_aliases"][0]
    second_head = second_model["active_aliases"][0]
    assert first_head["revision"] == 1
    assert second_head["revision"] == 2


def test_lineage_insert_rejects_cycles_in_the_serialized_transaction(db: Database):
    project_id = _seed_project(db)
    input_snapshot = "snapshot-lineage-input"
    output_snapshot = "snapshot-lineage-output"
    _published_snapshot(db, input_snapshot)
    _published_snapshot(db, output_snapshot)
    input_asset = _adopt(
        db,
        project_id=project_id,
        snapshot_id=input_snapshot,
        name="lineage-input",
    )["asset_id"]
    output_asset = _adopt(
        db,
        project_id=project_id,
        snapshot_id=output_snapshot,
        name="lineage-output",
    )["asset_id"]
    forward_plan = "60000000-0000-0000-0000-000000000072"
    reverse_plan = "60000000-0000-0000-0000-000000000073"
    _insert_execution_plan(db, plan_id=forward_plan, snapshot_id=input_snapshot)
    _insert_execution_plan(db, plan_id=reverse_plan, snapshot_id=output_snapshot)
    publish_approval = db.insert_approval("dataset_snapshot_build", {})
    with db.transaction() as cursor:
        db.insert_dataset_lineage_edge_in_transaction(
            cursor,
            edge_id="70000000-0000-0000-0000-000000000071",
            input_asset_id=input_asset,
            input_snapshot_id=input_snapshot,
            output_asset_id=output_asset,
            output_snapshot_id=output_snapshot,
            producing_execution_plan_id=forward_plan,
            output_declaration_name="model-output",
            publish_approval_id=publish_approval,
        )
    duplicate_plan = "60000000-0000-0000-0000-000000000076"
    _insert_execution_plan(db, plan_id=duplicate_plan, snapshot_id=input_snapshot)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        with db.transaction() as cursor:
            db.insert_dataset_lineage_edge_in_transaction(
                cursor,
                edge_id="70000000-0000-0000-0000-000000000075",
                input_asset_id=input_asset,
                input_snapshot_id=input_snapshot,
                output_asset_id=output_asset,
                output_snapshot_id=output_snapshot,
                producing_execution_plan_id=duplicate_plan,
                output_declaration_name="different-declaration",
                publish_approval_id=publish_approval,
            )
    with pytest.raises(ValueError, match="lineage cycle"):
        with db.transaction() as cursor:
            db.insert_dataset_lineage_edge_in_transaction(
                cursor,
                edge_id="70000000-0000-0000-0000-000000000072",
                input_asset_id=output_asset,
                input_snapshot_id=output_snapshot,
                output_asset_id=input_asset,
                output_snapshot_id=input_snapshot,
                producing_execution_plan_id=reverse_plan,
                output_declaration_name="cycle-output",
                publish_approval_id=publish_approval,
            )
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_lineage_edges").fetchone()[0] == 1
    graph = db.get_dataset_asset_lineage(input_asset)
    assert graph is not None
    assert len(graph["edges"]) == 1
    assert graph["truncated"] is False


def test_usage_and_storage_are_bounded_truthful_and_redacted(db: Database):
    project_id = _seed_project(db)
    snapshot_id = "snapshot-storage-redaction"
    _published_snapshot(db, snapshot_id)
    asset_id = _adopt(
        db,
        project_id=project_id,
        snapshot_id=snapshot_id,
    )["asset_id"]
    legacy_plan_id = "60000000-0000-0000-0000-000000000077"
    _insert_execution_plan(db, plan_id=legacy_plan_id, snapshot_id=snapshot_id)
    target_project_id = db.insert_project(
        f"dataset-grant-target-{uuid.uuid4()}",
        "/srv/projects/dataset-grant-target",
    )
    offer_id = "80000000-0000-0000-0000-000000000071"
    grant_id = "90000000-0000-0000-0000-000000000071"
    offer_approval_id = db.insert_approval("dataset_snapshot_build", {})
    accept_approval_id = db.insert_approval("dataset_snapshot_build", {})
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO dataset_share_offers (
                id, asset_id, source_project_id, target_project_id,
                contract_version, snapshot_ids_json, snapshot_set_digest,
                offer_digest, expires_at, created_approval_id,
                created_by_actor_id, created_at
            ) VALUES (?, ?, ?, ?, 'dataset-share-offer-v2', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                offer_id,
                asset_id,
                project_id,
                target_project_id,
                f'["{snapshot_id}"]',
                "2" * 64,
                "3" * 64,
                "2099-01-01T00:00:00+00:00",
                offer_approval_id,
                OWNER_ID,
                "2026-08-01T00:00:02+00:00",
            ),
        )
        cursor.execute(
            """
            INSERT INTO project_dataset_grants (
                id, offer_id, asset_id, source_project_id, target_project_id,
                snapshot_id, contract_version, accept_approval_id,
                accepted_by_actor_id, granted_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'dataset-grant-v2', ?, ?, ?)
            """,
            (
                grant_id,
                offer_id,
                asset_id,
                project_id,
                target_project_id,
                snapshot_id,
                accept_approval_id,
                OWNER_ID,
                "2026-08-01T00:00:03+00:00",
            ),
        )
    usage = db.get_dataset_asset_usage(asset_id)
    assert usage is not None
    assert usage["execution_plans"] == {
        "state": "available",
        "items": [],
        "truncated": False,
        "scope_truncated": False,
    }
    assert usage["project_defaults"] == {
        "state": "available",
        "items": [],
        "truncated": False,
    }
    assert usage["active_aliases"] == {
        "state": "available",
        "items": [],
        "truncated": False,
    }
    assert usage["active_grants"] == {
        "state": "unavailable",
        "reason": "dataset_sharing_v2_disabled",
        "items": [],
        "truncated": False,
    }
    assert legacy_plan_id not in repr(usage)
    assert grant_id not in repr(usage)
    assert target_project_id not in repr(usage)
    storage = db.get_dataset_asset_storage(asset_id)
    assert storage is not None
    rendered = repr(storage)
    assert storage["redaction"] == "paths_and_source_identity_omitted"
    assert "/sensitive/" not in rendered
    assert "legacy-secret-registry-name" not in rendered


def test_concurrent_reciprocal_lineage_edges_cannot_form_a_cycle(db: Database):
    project_id = _seed_project(db)
    first_snapshot = "snapshot-concurrent-first"
    second_snapshot = "snapshot-concurrent-second"
    _published_snapshot(db, first_snapshot)
    _published_snapshot(db, second_snapshot)
    first_asset = _adopt(
        db,
        project_id=project_id,
        snapshot_id=first_snapshot,
        name="concurrent-first",
    )["asset_id"]
    second_asset = _adopt(
        db,
        project_id=project_id,
        snapshot_id=second_snapshot,
        name="concurrent-second",
    )["asset_id"]
    first_plan = "60000000-0000-0000-0000-000000000074"
    second_plan = "60000000-0000-0000-0000-000000000075"
    _insert_execution_plan(db, plan_id=first_plan, snapshot_id=first_snapshot)
    _insert_execution_plan(db, plan_id=second_plan, snapshot_id=second_snapshot)
    approval_id = db.insert_approval("dataset_snapshot_build", {})
    path = db.path
    writers = [Database(path), Database(path)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def insert_edge(index: int) -> None:
        writer = writers[index]
        if index == 0:
            values = (
                "70000000-0000-0000-0000-000000000073",
                first_asset,
                first_snapshot,
                second_asset,
                second_snapshot,
                first_plan,
            )
        else:
            values = (
                "70000000-0000-0000-0000-000000000074",
                second_asset,
                second_snapshot,
                first_asset,
                first_snapshot,
                second_plan,
            )
        barrier.wait(timeout=5)
        try:
            with writer.transaction() as cursor:
                writer.insert_dataset_lineage_edge_in_transaction(
                    cursor,
                    edge_id=values[0],
                    input_asset_id=values[1],
                    input_snapshot_id=values[2],
                    output_asset_id=values[3],
                    output_snapshot_id=values[4],
                    producing_execution_plan_id=values[5],
                    output_declaration_name=f"concurrent-{index}",
                    publish_approval_id=approval_id,
                )
        except ValueError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("inserted")

    threads = [threading.Thread(target=insert_edge, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for writer in writers:
        writer.close()

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["dataset lineage cycle", "inserted"]
    assert db._conn.execute("SELECT COUNT(*) FROM dataset_lineage_edges").fetchone()[0] == 1


def test_unscoped_legacy_snapshots_are_never_enumerated_as_project_assets(db: Database):
    project_id = _seed_project(db)
    _published_snapshot(db, "legacy-unscoped-only")
    assert db.list_project_dataset_assets_page(
        project_id=project_id,
        after=None,
        limit_plus_one=51,
    ) == []


@pytest.mark.usefixtures("legacy_posture")
def test_dataset_routes_are_hidden_until_the_package_flag_is_enabled(api_client):
    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True
    response = client.get(
        "/api/v2/projects/10000000-0000-0000-0000-000000000071/datasets"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_dataset_validation_does_not_reflect_rejected_values(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_dataset_assets(main_module)
    _session_for(client, main_module, ADMIN_ID)
    secret = "DO-NOT-REFLECT-DATASET-SECRET"
    response = client.post(
        f"/api/v2/projects/{project_id}/dataset-adoption-requests",
        headers={"Idempotency-Key": "dataset-safe-validation"},
        json={
            "snapshot_id": "candidate",
            "asset": {
                "name": "training",
                "description": f"{secret}\u0000",
                "data_card": {
                    "collection_method": "collection",
                    "processing_method": "processing",
                },
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_dataset_request"
    assert secret not in response.text


def test_oversized_data_card_is_rejected_before_idempotency_or_approval_write(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _enable_dataset_assets(main_module)
    _session_for(client, main_module, ADMIN_ID)
    before_approvals = database._conn.execute(
        "SELECT COUNT(*) FROM approvals"
    ).fetchone()[0]
    before_idempotency = database._conn.execute(
        "SELECT COUNT(*) FROM api_idempotency_keys"
    ).fetchone()[0]

    response = client.post(
        f"/api/v2/projects/{project_id}/dataset-adoption-requests",
        headers={"Idempotency-Key": "oversized-data-card"},
        json={
            "snapshot_id": "oversized-data-card-candidate",
            "asset": {
                "name": "oversized-card",
                "description": "must fail before materialization",
                "data_card": _oversized_data_card(),
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_dataset_request"
    assert database._conn.execute(
        "SELECT COUNT(*) FROM approvals"
    ).fetchone()[0] == before_approvals
    assert database._conn.execute(
        "SELECT COUNT(*) FROM api_idempotency_keys"
    ).fetchone()[0] == before_idempotency


def test_dataset_http_adoption_alias_reads_and_idempotent_decisions(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    snapshot_id = "snapshot-http-adoption"
    _published_snapshot(database, snapshot_id)
    _enable_dataset_assets(main_module)

    _session_for(client, main_module, ADMIN_ID)
    request_body = {
        "snapshot_id": snapshot_id,
        "asset": _asset_input().model_dump(mode="json"),
    }
    requested = client.post(
        f"/api/v2/projects/{project_id}/dataset-adoption-requests",
        headers={"Idempotency-Key": "dataset-adoption-http"},
        json=request_body,
    )
    replayed_request = client.post(
        f"/api/v2/projects/{project_id}/dataset-adoption-requests",
        headers={"Idempotency-Key": "dataset-adoption-http"},
        json=request_body,
    )
    assert requested.status_code == 202
    assert replayed_request.status_code == 202
    assert replayed_request.json()["approval_id"] == requested.json()["approval_id"]
    assert replayed_request.json()["replayed"] is True

    approval_id = requested.json()["approval_id"]
    approval_list = client.get(
        "/api/v2/approvals",
        params={"kind": "dataset_asset_adoption_v2"},
    )
    approval_detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert approval_list.status_code == 200
    assert approval_list.json()["items"][0]["id"] == approval_id
    assert approval_detail.status_code == 200
    assert approval_detail.json()["payload_verified"] is True
    _session_for(client, main_module, OWNER_ID)
    decided = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "dataset-adoption-decision"},
        json={"decision": "approve", "note": "reviewed"},
    )
    replayed_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "dataset-adoption-decision"},
        json={"decision": "approve", "note": "reviewed"},
    )
    assert decided.status_code == 202
    assert replayed_decision.status_code == 202
    assert replayed_decision.json()["replayed"] is True
    asset_id = decided.json()["asset_id"]

    listed = client.get(f"/api/v2/projects/{project_id}/datasets")
    detail = client.get(f"/api/v2/dataset-assets/{asset_id}")
    storage = client.get(f"/api/v2/dataset-assets/{asset_id}/storage")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["asset_id"] == asset_id
    assert detail.status_code == 200
    assert storage.status_code == 200
    assert "/sensitive/" not in storage.text
    workspace = client.get("/api/v2/workspace")
    assert workspace.status_code == 200
    assert workspace.json()["recent_dataset_assets"]["state"] == "available"
    assert workspace.json()["recent_dataset_assets"]["items"][0]["asset_id"] == asset_id

    _session_for(client, main_module, MANAGER_ID)
    alias_request = client.post(
        f"/api/v2/dataset-assets/{asset_id}/alias-change-requests",
        headers={"Idempotency-Key": "dataset-alias-http"},
        json={
            "operation": "create",
            "project_id": project_id,
            "alias_name": "latest",
            "snapshot_id": snapshot_id,
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "expected_head_revision_digest": None,
        },
    )
    assert alias_request.status_code == 202
    _session_for(client, main_module, REVIEWER_ID)
    alias_decision = client.post(
        f"/api/v2/approvals/{alias_request.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "dataset-alias-decision"},
        json={"decision": "approve"},
    )
    assert alias_decision.status_code == 202
    assert alias_decision.json()["revision"] == 1


def test_dataset_asset_reads_are_opaque_cross_project(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = _seed_project(database)
    snapshot_id = "snapshot-cross-project"
    _published_snapshot(database, snapshot_id)
    asset_id = _adopt(
        database,
        project_id=project_id,
        snapshot_id=snapshot_id,
    )["asset_id"]
    _enable_dataset_assets(main_module)
    _session_for(client, main_module, OUTSIDER_ID)
    for suffix in ("", "/lineage", "/usage", "/storage"):
        response = client.get(f"/api/v2/dataset-assets/{asset_id}{suffix}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
