from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.dataset_assets import (
    DatasetAliasCreateRequest,
    DatasetAssetInput,
    DatasetDataCardV2,
)
from app.dataset_sharing import DatasetShareAcceptRequest, DatasetShareOfferRequest
from app.db import Database
from app.identity import ActorType, ProjectRoleV2, generate_session_token
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


SOURCE_OWNER = "21000000-0000-0000-0000-000000000081"
SOURCE_REVIEWER = "21000000-0000-0000-0000-000000000082"
SOURCE_MANAGER = "21000000-0000-0000-0000-000000000083"
TARGET_OWNER = "21000000-0000-0000-0000-000000000084"
TARGET_REVIEWER = "21000000-0000-0000-0000-000000000085"
TARGET_MANAGER = "21000000-0000-0000-0000-000000000086"
PLATFORM_ADMIN = "21000000-0000-0000-0000-000000000087"
SOURCE_SERVICE = "21000000-0000-0000-0000-000000000088"
SOURCE_SERVICE_DECIDER = "21000000-0000-0000-0000-000000000089"
TARGET_B_OWNER = "21000000-0000-0000-0000-000000000090"
TARGET_B_REVIEWER = "21000000-0000-0000-0000-000000000091"
TARGET_B_MANAGER = "21000000-0000-0000-0000-000000000092"


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


def _seed_projects(database: Database) -> tuple[str, str]:
    source = database.insert_project(
        f"sharing-source-{uuid.uuid4()}",
        "/srv/projects/sharing-source",
    )
    target = database.insert_project(
        f"sharing-target-{uuid.uuid4()}",
        "/srv/projects/sharing-target",
    )
    for actor_id, name, admin in (
        (SOURCE_OWNER, "Source owner", False),
        (SOURCE_REVIEWER, "Source reviewer", False),
        (SOURCE_MANAGER, "Source manager", False),
        (TARGET_OWNER, "Target owner", False),
        (TARGET_REVIEWER, "Target reviewer", False),
        (TARGET_MANAGER, "Target manager", False),
        (PLATFORM_ADMIN, "Platform admin", True),
    ):
        database.insert_actor(
            actor_id=actor_id,
            actor_type=ActorType.HUMAN,
            display_name=name,
            platform_admin=admin,
        )
    for project_id, assignments in (
        (
            source,
            (
                (SOURCE_OWNER, ProjectRoleV2.OWNER),
                (SOURCE_REVIEWER, ProjectRoleV2.REVIEWER),
                (SOURCE_MANAGER, ProjectRoleV2.DATASET_MANAGER),
            ),
        ),
        (
            target,
            (
                (TARGET_OWNER, ProjectRoleV2.OWNER),
                (TARGET_REVIEWER, ProjectRoleV2.REVIEWER),
                (TARGET_MANAGER, ProjectRoleV2.DATASET_MANAGER),
            ),
        ),
    ):
        for actor_id, role in assignments:
            _binding(database, project_id=project_id, actor_id=actor_id, role=role)
    return source, target


def _seed_additional_target(database: Database) -> str:
    target = database.insert_project(
        f"sharing-target-b-{uuid.uuid4()}",
        "/srv/projects/sharing-target-b",
    )
    for actor_id, name in (
        (TARGET_B_OWNER, "Target B owner"),
        (TARGET_B_REVIEWER, "Target B reviewer"),
        (TARGET_B_MANAGER, "Target B manager"),
    ):
        database.insert_actor(
            actor_id=actor_id,
            actor_type=ActorType.HUMAN,
            display_name=name,
        )
    for actor_id, role in (
        (TARGET_B_OWNER, ProjectRoleV2.OWNER),
        (TARGET_B_REVIEWER, ProjectRoleV2.REVIEWER),
        (TARGET_B_MANAGER, ProjectRoleV2.DATASET_MANAGER),
    ):
        _binding(database, project_id=target, actor_id=actor_id, role=role)
    return target


def _publish(database: Database, snapshot_id: str) -> int:
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
            ) VALUES (?, 'sharing-source', 'v1', 'published', ?, ?,
                      '/private/manifest', '/private/descriptor', 'store-v1',
                      '{}', 1, 10, ?, ?, ?)
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
    return approval_id


def _asset_input() -> DatasetAssetInput:
    return DatasetAssetInput(
        name="shared-training-corpus",
        description="Approved exact snapshots",
        data_card=DatasetDataCardV2(
            collection_method="Approved collection",
            processing_method="Deterministic processing",
        ),
    )


def _adopt(database: Database, project_id: str, snapshot_id: str) -> dict:
    with SQLiteUnitOfWork(database) as unit_of_work:
        approval_id = unit_of_work.run(
            lambda cursor: database.create_dataset_asset_adoption_approval_in_transaction(
                cursor,
                project_id=project_id,
                snapshot_id=snapshot_id,
                asset=_asset_input(),
                requester_actor_id=PLATFORM_ADMIN,
            )
        )
    return database.apply_dataset_asset_adoption_decision(
        approval_id=approval_id,
        decision_actor_id=SOURCE_OWNER,
        decision_mechanism="session",
    )


def _attach_snapshot(
    database: Database,
    *,
    project_id: str,
    asset_id: str,
    snapshot_id: str,
    approval_id: int,
) -> None:
    with database.cursor() as cursor:
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
                snapshot_id,
                approval_id,
                SOURCE_OWNER,
                "2026-08-09T00:00:02.000Z",
            ),
        )


def _expiry(*, days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _request_offer(
    database: Database,
    *,
    asset_id: str,
    source_project_id: str,
    target_project_id: str,
    snapshot_ids: list[str],
    asset_digest: str,
) -> int:
    request = DatasetShareOfferRequest(
        source_project_id=source_project_id,
        target_project_id=target_project_id,
        snapshot_ids=snapshot_ids,
        expected_asset_digest=asset_digest,
        expires_at=_expiry(),
    )
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_dataset_share_offer_approval_in_transaction(
                cursor,
                asset_id=asset_id,
                request=request,
                requester_actor_id=SOURCE_MANAGER,
            )
        )


def _request_accept(
    database: Database,
    offer_result: dict,
    *,
    requester_actor_id: str = TARGET_OWNER,
) -> int:
    request = DatasetShareAcceptRequest(
        offer_id=offer_result["offer_id"],
        asset_id=offer_result["asset_id"],
        source_project_id=offer_result["source_project_id"],
        target_project_id=offer_result["target_project_id"],
        offer_digest=offer_result["offer_digest"],
        expires_at=offer_result["expires_at"],
        snapshot_ids=offer_result["snapshot_ids"],
        snapshot_set_digest=offer_result["snapshot_set_digest"],
    )
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_dataset_share_accept_approval_in_transaction(
                cursor,
                offer_id=offer_result["offer_id"],
                request=request,
                requester_actor_id=requester_actor_id,
            )
        )


def _request_alias(
    database: Database,
    *,
    project_id: str,
    asset_id: str,
    snapshot_id: str,
    actor_id: str,
    alias_name: str,
    sharing_enabled: bool,
) -> int:
    request = DatasetAliasCreateRequest(
        operation="create",
        project_id=project_id,
        alias_name=alias_name,
        snapshot_id=snapshot_id,
        expected_revision=0,
    )
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_dataset_alias_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                asset_id=asset_id,
                operation=request.operation,
                alias_name=request.alias_name,
                snapshot_id=request.snapshot_id,
                expected_revision=request.expected_revision,
                expected_head_revision_id=request.expected_head_revision_id,
                expected_head_revision_digest=request.expected_head_revision_digest,
                requester_actor_id=actor_id,
                sharing_enabled=sharing_enabled,
            )
        )


def _session_for(client, main_module, actor_id: str) -> None:
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00.000Z",
    )
    client.cookies.clear()
    client.cookies.set(
        main_module.app_state.config.session_cookie_name,
        issued.raw_token,
    )


def _enable_sharing(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.dataset_assets_v2_enabled = True
    config.dataset_sharing_v2_enabled = True
    config.authorization_mode = "enforce"


def _active_single_snapshot_grant(
    database: Database,
    *,
    snapshot_id: str,
) -> tuple[str, str, dict, dict, dict]:
    source, target = _seed_projects(database)
    _publish(database, snapshot_id)
    adopted = _adopt(database, source, snapshot_id)
    offer_approval = _request_offer(
        database,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[snapshot_id],
        asset_digest=adopted["asset_digest"],
    )
    offer = database.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    accept_approval = _request_accept(database, offer)
    accepted = database.apply_dataset_share_accept_decision(
        approval_id=accept_approval,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    grant = database.get_dataset_grant_read_model(accepted["grant_ids"][0])
    assert grant is not None
    return source, target, adopted, offer, grant


def test_two_sided_offer_accept_and_source_revoke_are_exact_and_atomic(db: Database):
    source, target = _seed_projects(db)
    first = "sharing-snapshot-a"
    second = "sharing-snapshot-b"
    _publish(db, first)
    second_approval = _publish(db, second)
    adopted = _adopt(db, source, first)
    asset_id = adopted["asset_id"]
    _attach_snapshot(
        db,
        project_id=source,
        asset_id=asset_id,
        snapshot_id=second,
        approval_id=second_approval,
    )

    offer_approval = _request_offer(
        db,
        asset_id=asset_id,
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[first, second],
        asset_digest=adopted["asset_digest"],
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_share_offers"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 0

    offer = db.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_share_offers"
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 0

    accept_approval = _request_accept(db, offer)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 0
    accepted = db.apply_dataset_share_accept_decision(
        approval_id=accept_approval,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    assert accepted["snapshot_ids"] == [first, second]
    assert len(accepted["grant_ids"]) == 2
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 2

    eligibility = db.get_dataset_snapshot_project_eligibility(
        project_id=target,
        asset_id=asset_id,
        snapshot_id=first,
        sharing_enabled=True,
    )
    assert eligibility["state"] == "active_grant"
    grant_id = eligibility["grant_id"]
    with SQLiteUnitOfWork(db) as unit_of_work:
        revoke_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant_id,
                operation="revoke",
                project_id=source,
                expected_grant_digest=eligibility["grant_digest"],
                requester_actor_id=SOURCE_MANAGER,
            )
        )
    withdrawn = db.apply_dataset_grant_revoke_decision(
        approval_id=revoke_approval,
        decision_actor_id=SOURCE_OWNER,
        decision_mechanism="session",
    )
    assert withdrawn["operation"] == "revoke"
    assert db.get_dataset_snapshot_project_eligibility(
        project_id=target,
        asset_id=asset_id,
        snapshot_id=first,
        sharing_enabled=True,
    ) == {"state": "unavailable", "reason": "no_active_exact_entitlement"}
    assert db.get_dataset_snapshot_project_eligibility(
        project_id=source,
        asset_id=asset_id,
        snapshot_id=first,
        sharing_enabled=False,
    )["state"] == "owned_published"


def test_offer_ttl_and_target_actor_contract_fail_closed(db: Database):
    source, target = _seed_projects(db)
    snapshot_id = "sharing-snapshot-ttl"
    _publish(db, snapshot_id)
    adopted = _adopt(db, source, snapshot_id)
    request = DatasetShareOfferRequest(
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[snapshot_id],
        expected_asset_digest=adopted["asset_digest"],
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=4)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    )
    with pytest.raises(ValueError, match="between 5 minutes and 30 days"):
        with SQLiteUnitOfWork(db) as unit_of_work:
            unit_of_work.run(
                lambda cursor: db.create_dataset_share_offer_approval_in_transaction(
                    cursor,
                    asset_id=adopted["asset_id"],
                    request=request,
                    requester_actor_id=SOURCE_MANAGER,
                )
            )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE kind = 'dataset_share_offer_v2'"
    ).fetchone()[0] == 0


def test_target_scope_reads_alias_and_unlink_fail_closed(db: Database):
    source, target = _seed_projects(db)
    shared_snapshot = "sharing-scoped-snapshot"
    future_snapshot = "sharing-future-snapshot"
    _publish(db, shared_snapshot)
    adopted = _adopt(db, source, shared_snapshot)
    asset_id = adopted["asset_id"]

    offer_approval = _request_offer(
        db,
        asset_id=asset_id,
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[shared_snapshot],
        asset_digest=adopted["asset_digest"],
    )
    offer = db.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    future_approval = _publish(db, future_snapshot)
    _attach_snapshot(
        db,
        project_id=source,
        asset_id=asset_id,
        snapshot_id=future_snapshot,
        approval_id=future_approval,
    )
    accept_approval = _request_accept(db, offer)
    accepted = db.apply_dataset_share_accept_decision(
        approval_id=accept_approval,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )

    source_alias_approval = _request_alias(
        db,
        project_id=source,
        asset_id=asset_id,
        snapshot_id=shared_snapshot,
        actor_id=SOURCE_MANAGER,
        alias_name="source-latest",
        sharing_enabled=False,
    )
    db.apply_dataset_alias_change_decision(
        approval_id=source_alias_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    target_alias_approval = _request_alias(
        db,
        project_id=target,
        asset_id=asset_id,
        snapshot_id=shared_snapshot,
        actor_id=TARGET_MANAGER,
        alias_name="target-latest",
        sharing_enabled=True,
    )
    target_alias = db.apply_dataset_alias_change_decision(
        approval_id=target_alias_approval,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )

    target_list = db.list_project_dataset_assets_page(
        project_id=target,
        after=None,
        limit_plus_one=11,
        sharing_enabled=True,
    )
    assert [(item["asset_id"], item["access_mode"]) for item in target_list] == [
        (asset_id, "shared")
    ]
    assert target_list[0]["snapshot_count"] == 1
    target_detail = db.get_dataset_asset_read_model(
        asset_id,
        project_id=target,
        sharing_enabled=True,
    )
    assert target_detail is not None
    assert [item["snapshot_id"] for item in target_detail["snapshots"]] == [
        shared_snapshot
    ]
    assert [item["alias_name"] for item in target_detail["active_aliases"]] == [
        "target-latest"
    ]
    assert future_snapshot not in repr(target_detail)
    assert "source-latest" not in repr(target_detail)
    storage = db.get_dataset_asset_storage(
        asset_id,
        project_id=target,
        sharing_enabled=True,
    )
    assert storage is not None
    assert [item["snapshot_id"] for item in storage["snapshots"]] == [
        shared_snapshot
    ]
    usage = db.get_dataset_asset_usage(
        asset_id,
        project_id=target,
        sharing_enabled=True,
    )
    assert usage is not None
    assert [item["grant_id"] for item in usage["active_grants"]["items"]] == (
        accepted["grant_ids"]
    )
    resolved = db.resolve_dataset_alias_for_new_run(
        project_id=target,
        asset_id=asset_id,
        alias_name="target-latest",
        sharing_enabled=True,
    )
    assert resolved is not None
    assert resolved["eligibility"]["state"] == "active_grant"

    grant = db.get_dataset_grant_read_model(accepted["grant_ids"][0])
    assert grant is not None
    with SQLiteUnitOfWork(db) as unit_of_work:
        unlink_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation="unlink",
                project_id=target,
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=TARGET_OWNER,
            )
        )
    db.apply_dataset_grant_revoke_decision(
        approval_id=unlink_approval,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    assert db.get_dataset_asset_read_model(
        asset_id,
        project_id=target,
        sharing_enabled=True,
    ) is None
    assert db.resolve_dataset_alias_for_new_run(
        project_id=target,
        asset_id=asset_id,
        alias_name="target-latest",
        sharing_enabled=True,
    ) is None
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_alias_revisions WHERE id = ?",
        (target_alias["alias_revision_id"],),
    ).fetchone()[0] == 1


def test_sharing_materialization_rolls_back_when_durable_audit_fails(
    db: Database,
    monkeypatch,
):
    source, target = _seed_projects(db)
    snapshot_id = "sharing-audit-rollback"
    _publish(db, snapshot_id)
    adopted = _adopt(db, source, snapshot_id)
    offer_approval = _request_offer(
        db,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[snapshot_id],
        asset_digest=adopted["asset_digest"],
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_action(cursor, **kwargs):
        if kwargs.get("action") in {
            "dataset_share_offer_created",
            "dataset_share_grants_created",
            "dataset_grant_withdrawn",
        }:
            raise RuntimeError(f"injected {kwargs['action']} audit failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_action)
    with pytest.raises(RuntimeError, match="dataset_share_offer_created"):
        db.apply_dataset_share_offer_decision(
            approval_id=offer_approval,
            decision_actor_id=SOURCE_REVIEWER,
            decision_mechanism="session",
        )
    assert db._conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (offer_approval,)
    ).fetchone()[0] == "pending"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_share_offers"
    ).fetchone()[0] == 0

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", original)
    offer = db.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    accept_approval = _request_accept(db, offer)
    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_action)
    with pytest.raises(RuntimeError, match="dataset_share_grants_created"):
        db.apply_dataset_share_accept_decision(
            approval_id=accept_approval,
            decision_actor_id=TARGET_REVIEWER,
            decision_mechanism="session",
        )
    assert db._conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (accept_approval,)
    ).fetchone()[0] == "pending"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 0

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", original)
    accepted = db.apply_dataset_share_accept_decision(
        approval_id=accept_approval,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    grant = db.get_dataset_grant_read_model(accepted["grant_ids"][0])
    assert grant is not None
    with SQLiteUnitOfWork(db) as unit_of_work:
        revoke_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation="revoke",
                project_id=source,
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=SOURCE_MANAGER,
            )
        )
    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_action)
    with pytest.raises(RuntimeError, match="dataset_grant_withdrawn"):
        db.apply_dataset_grant_revoke_decision(
            approval_id=revoke_approval,
            decision_actor_id=SOURCE_OWNER,
            decision_mechanism="session",
        )
    row = db._conn.execute(
        """
        SELECT revocation_approval_id, revoked_at
        FROM project_dataset_grants WHERE id = ?
        """,
        (grant["grant_id"],),
    ).fetchone()
    assert tuple(row) == (None, None)
    assert db._conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (revoke_approval,)
    ).fetchone()[0] == "pending"


def test_accept_revalidation_rejects_racing_grant_without_partial_grants(
    db: Database,
):
    source, target = _seed_projects(db)
    first = "sharing-atomic-first"
    second = "sharing-atomic-second"
    _publish(db, first)
    second_approval = _publish(db, second)
    adopted = _adopt(db, source, first)
    _attach_snapshot(
        db,
        project_id=source,
        asset_id=adopted["asset_id"],
        snapshot_id=second,
        approval_id=second_approval,
    )
    offer_approval = _request_offer(
        db,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[first, second],
        asset_digest=adopted["asset_digest"],
    )
    offer = db.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    accept_approval = _request_accept(db, offer)

    racing_offer_approval = _request_offer(
        db,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[second],
        asset_digest=adopted["asset_digest"],
    )
    racing_offer = db.apply_dataset_share_offer_decision(
        approval_id=racing_offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    db.apply_dataset_share_accept_decision(
        approval_id=_request_accept(db, racing_offer),
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    with pytest.raises(ValueError, match="active target grant"):
        db.apply_dataset_share_accept_decision(
            approval_id=accept_approval,
            decision_actor_id=TARGET_REVIEWER,
            decision_mechanism="session",
        )
    assert [
        row[0]
        for row in db._conn.execute(
            "SELECT snapshot_id FROM project_dataset_grants ORDER BY snapshot_id"
        ).fetchall()
    ] == [second]
    assert db._conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (accept_approval,)
    ).fetchone()[0] == "pending"


def test_accept_rejection_duplicate_conflicts_and_new_offer_regrant(db: Database):
    source, target = _seed_projects(db)
    snapshot_id = "sharing-regrant"
    _publish(db, snapshot_id)
    adopted = _adopt(db, source, snapshot_id)

    first_offer_id = _request_offer(
        db,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[snapshot_id],
        asset_digest=adopted["asset_digest"],
    )
    first_offer = db.apply_dataset_share_offer_decision(
        approval_id=first_offer_id,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    rejected_accept = _request_accept(db, first_offer)
    db.reject_dataset_sharing_decision(
        approval_id=rejected_accept,
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 0

    accepted = db.apply_dataset_share_accept_decision(
        approval_id=_request_accept(db, first_offer),
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    with pytest.raises(ValueError, match="already accepted"):
        _request_accept(db, first_offer)

    second_offer_id = _request_offer(
        db,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[snapshot_id],
        asset_digest=adopted["asset_digest"],
    )
    second_offer = db.apply_dataset_share_offer_decision(
        approval_id=second_offer_id,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    with pytest.raises(ValueError, match="active target grant"):
        _request_accept(db, second_offer)

    first_grant = db.get_dataset_grant_read_model(accepted["grant_ids"][0])
    assert first_grant is not None
    with SQLiteUnitOfWork(db) as unit_of_work:
        revoke_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=first_grant["grant_id"],
                operation="revoke",
                project_id=source,
                expected_grant_digest=first_grant["grant_digest"],
                requester_actor_id=SOURCE_MANAGER,
            )
        )
    db.apply_dataset_grant_revoke_decision(
        approval_id=revoke_approval,
        decision_actor_id=SOURCE_OWNER,
        decision_mechanism="session",
    )
    regranted = db.apply_dataset_share_accept_decision(
        approval_id=_request_accept(db, second_offer),
        decision_actor_id=TARGET_REVIEWER,
        decision_mechanism="session",
    )
    assert regranted["grant_ids"] != accepted["grant_ids"]
    assert db.get_dataset_snapshot_project_eligibility(
        project_id=target,
        asset_id=adopted["asset_id"],
        snapshot_id=snapshot_id,
        sharing_enabled=True,
    )["grant_id"] == regranted["grant_ids"][0]


def test_offer_accept_roles_digest_and_expiry_are_revalidated(
    db: Database,
    monkeypatch,
):
    source, target = _seed_projects(db)
    snapshot_id = "sharing-expiry-race"
    _publish(db, snapshot_id)
    adopted = _adopt(db, source, snapshot_id)
    for actor_id in (SOURCE_SERVICE, SOURCE_SERVICE_DECIDER):
        db.insert_actor(
            actor_id=actor_id,
            actor_type=ActorType.SERVICE,
            display_name="Source automation",
        )
        _binding(
            db,
            project_id=source,
            actor_id=actor_id,
            role=ProjectRoleV2.DATASET_MANAGER,
        )
    request = DatasetShareOfferRequest(
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[snapshot_id],
        expected_asset_digest=adopted["asset_digest"],
        expires_at=_expiry(),
    )
    with pytest.raises(ValueError, match="not authorized"):
        with SQLiteUnitOfWork(db) as unit_of_work:
            unit_of_work.run(
                lambda cursor: db.create_dataset_share_offer_approval_in_transaction(
                    cursor,
                    asset_id=adopted["asset_id"],
                    request=request,
                    requester_actor_id=PLATFORM_ADMIN,
                )
            )
    with SQLiteUnitOfWork(db) as unit_of_work:
        offer_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_share_offer_approval_in_transaction(
                cursor,
                asset_id=adopted["asset_id"],
                request=request,
                requester_actor_id=SOURCE_SERVICE,
            )
        )
    with pytest.raises(ValueError, match="high_risk_self_decision"):
        db.apply_dataset_share_offer_decision(
            approval_id=offer_approval,
            decision_actor_id=SOURCE_SERVICE,
            decision_mechanism="session",
        )
    with pytest.raises(ValueError, match="not authorized"):
        db.apply_dataset_share_offer_decision(
            approval_id=offer_approval,
            decision_actor_id=SOURCE_SERVICE_DECIDER,
            decision_mechanism="session",
        )
    offer = db.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )

    stale_request = DatasetShareAcceptRequest(
        offer_id=offer["offer_id"],
        asset_id=offer["asset_id"],
        source_project_id=source,
        target_project_id=target,
        offer_digest="0" * 64,
        expires_at=offer["expires_at"],
        snapshot_ids=offer["snapshot_ids"],
        snapshot_set_digest=offer["snapshot_set_digest"],
    )
    with pytest.raises(ValueError, match="envelope is stale"):
        with SQLiteUnitOfWork(db) as unit_of_work:
            unit_of_work.run(
                lambda cursor: db.create_dataset_share_accept_approval_in_transaction(
                    cursor,
                    offer_id=offer["offer_id"],
                    request=stale_request,
                    requester_actor_id=TARGET_OWNER,
                )
            )
    valid_request = DatasetShareAcceptRequest(
        offer_id=offer["offer_id"],
        asset_id=offer["asset_id"],
        source_project_id=source,
        target_project_id=target,
        offer_digest=offer["offer_digest"],
        expires_at=offer["expires_at"],
        snapshot_ids=offer["snapshot_ids"],
        snapshot_set_digest=offer["snapshot_set_digest"],
    )
    with pytest.raises(ValueError, match="not authorized"):
        with SQLiteUnitOfWork(db) as unit_of_work:
            unit_of_work.run(
                lambda cursor: db.create_dataset_share_accept_approval_in_transaction(
                    cursor,
                    offer_id=offer["offer_id"],
                    request=valid_request,
                    requester_actor_id=TARGET_MANAGER,
                )
            )
    accept_approval = _request_accept(db, offer)
    future = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(
        Database,
        "_sharing_now_from_cursor",
        classmethod(lambda cls, cursor: future),
    )
    with pytest.raises(ValueError, match="offer is expired"):
        db.apply_dataset_share_accept_decision(
            approval_id=accept_approval,
            decision_actor_id=TARGET_REVIEWER,
            decision_mechanism="session",
        )
    assert db._conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (accept_approval,)
    ).fetchone()[0] == "pending"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM project_dataset_grants"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("operation", "counterparty_actor", "requester", "decider"),
    (
        ("revoke", TARGET_REVIEWER, SOURCE_MANAGER, SOURCE_OWNER),
        ("unlink", SOURCE_REVIEWER, TARGET_OWNER, TARGET_REVIEWER),
    ),
)
def test_withdrawal_only_requires_requesting_side_readiness(
    db: Database,
    operation: str,
    counterparty_actor: str,
    requester: str,
    decider: str,
):
    source, target, _adopted, _offer, grant = _active_single_snapshot_grant(
        db,
        snapshot_id=f"sharing-counterparty-{operation}",
    )
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE actors SET disabled_at = ? WHERE id = ?",
            ("2026-08-09T01:00:00.000Z", counterparty_actor),
        )
    project_id = source if operation == "revoke" else target
    with SQLiteUnitOfWork(db) as unit_of_work:
        approval_id = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation=operation,
                project_id=project_id,
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=requester,
            )
        )
    result = db.apply_dataset_grant_revoke_decision(
        approval_id=approval_id,
        decision_actor_id=decider,
        decision_mechanism="session",
    )
    assert result["operation"] == operation


def test_pending_target_alias_fails_if_grant_is_withdrawn(db: Database):
    source, target, adopted, _offer, grant = _active_single_snapshot_grant(
        db,
        snapshot_id="sharing-pending-alias",
    )
    alias_approval = _request_alias(
        db,
        project_id=target,
        asset_id=adopted["asset_id"],
        snapshot_id=grant["snapshot_id"],
        actor_id=TARGET_MANAGER,
        alias_name="pending-target",
        sharing_enabled=True,
    )
    with SQLiteUnitOfWork(db) as unit_of_work:
        revoke_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation="revoke",
                project_id=source,
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=SOURCE_MANAGER,
            )
        )
    db.apply_dataset_grant_revoke_decision(
        approval_id=revoke_approval,
        decision_actor_id=SOURCE_OWNER,
        decision_mechanism="session",
    )
    with pytest.raises(ValueError, match="snapshot is unavailable"):
        db.apply_dataset_alias_change_decision(
            approval_id=alias_approval,
            decision_actor_id=TARGET_REVIEWER,
            decision_mechanism="session",
        )
    assert db._conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (alias_approval,)
    ).fetchone()[0] == "pending"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM dataset_alias_revisions WHERE project_id = ?",
        (target,),
    ).fetchone()[0] == 0


def test_grant_reads_fail_closed_on_malformed_provenance_and_ignore_offer_expiry(
    db: Database,
    monkeypatch,
):
    source, target, adopted, _offer, grant = _active_single_snapshot_grant(
        db,
        snapshot_id="sharing-provenance",
    )
    malformed_offer_approval = _request_offer(
        db,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target,
        snapshot_ids=[grant["snapshot_id"]],
        asset_digest=adopted["asset_digest"],
    )
    malformed_offer = db.apply_dataset_share_offer_decision(
        approval_id=malformed_offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    future = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(
        Database,
        "_sharing_now_from_cursor",
        classmethod(lambda cls, cursor: future),
    )
    assert db.get_dataset_snapshot_project_eligibility(
        project_id=target,
        asset_id=adopted["asset_id"],
        snapshot_id=grant["snapshot_id"],
        sharing_enabled=True,
    )["state"] == "active_grant"

    with SQLiteUnitOfWork(db) as unit_of_work:
        revoke_approval = unit_of_work.run(
            lambda cursor: db.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation="revoke",
                project_id=source,
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=SOURCE_MANAGER,
            )
        )
    db.apply_dataset_grant_revoke_decision(
        approval_id=revoke_approval,
        decision_actor_id=SOURCE_OWNER,
        decision_mechanism="session",
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_dataset_grants (
                id, offer_id, asset_id, source_project_id,
                target_project_id, snapshot_id, contract_version,
                accept_approval_id, accepted_by_actor_id, granted_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'dataset-grant-v2', ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                malformed_offer["offer_id"],
                grant["asset_id"],
                grant["source_project_id"],
                grant["target_project_id"],
                grant["snapshot_id"],
                grant["accept_approval_id"],
                grant["accepted_by_actor_id"],
                grant["granted_at"],
            ),
        )
    assert db.get_dataset_snapshot_project_eligibility(
        project_id=target,
        asset_id=adopted["asset_id"],
        snapshot_id=grant["snapshot_id"],
        sharing_enabled=True,
    ) == {"state": "unavailable", "reason": "no_active_exact_entitlement"}
    assert db.get_dataset_asset_read_model(
        adopted["asset_id"],
        project_id=target,
        sharing_enabled=True,
    ) is None


def test_foreign_mutation_probes_are_opaque_and_write_nothing(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    source, target_a = _seed_projects(database)
    target_b = _seed_additional_target(database)
    _binding(
        database,
        project_id=target_a,
        actor_id=TARGET_OWNER,
        role=ProjectRoleV2.DATASET_MANAGER,
    )
    snapshot_id = "sharing-foreign-probe"
    _publish(database, snapshot_id)
    adopted = _adopt(database, source, snapshot_id)
    offer_approval = _request_offer(
        database,
        asset_id=adopted["asset_id"],
        source_project_id=source,
        target_project_id=target_b,
        snapshot_ids=[snapshot_id],
        asset_digest=adopted["asset_digest"],
    )
    offer = database.apply_dataset_share_offer_decision(
        approval_id=offer_approval,
        decision_actor_id=SOURCE_REVIEWER,
        decision_mechanism="session",
    )
    accepted = database.apply_dataset_share_accept_decision(
        approval_id=_request_accept(
            database,
            offer,
            requester_actor_id=TARGET_B_OWNER,
        ),
        decision_actor_id=TARGET_B_REVIEWER,
        decision_mechanism="session",
    )
    grant = database.get_dataset_grant_read_model(accepted["grant_ids"][0])
    assert grant is not None
    _enable_sharing(main_module)
    _session_for(client, main_module, TARGET_OWNER)

    def durable_counts() -> tuple[int, int, int]:
        return tuple(
            database._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("approvals", "api_idempotency_keys", "audit_events")
        )

    def safe_error(response) -> tuple[int, dict]:
        error = dict(response.json()["error"])
        error.pop("request_id", None)
        return response.status_code, error

    before = durable_counts()
    missing_offer = str(uuid.uuid4())
    accept_body = {
        key: offer[key]
        for key in (
            "offer_id",
            "asset_id",
            "source_project_id",
            "target_project_id",
            "offer_digest",
            "expires_at",
            "snapshot_ids",
            "snapshot_set_digest",
        )
    }
    accept_body["target_project_id"] = target_a
    foreign_accept = client.post(
        f"/api/v2/dataset-share-offers/{offer['offer_id']}/accept-requests",
        headers={"Idempotency-Key": "foreign-offer-probe"},
        json=accept_body,
    )
    missing_accept = client.post(
        f"/api/v2/dataset-share-offers/{missing_offer}/accept-requests",
        headers={"Idempotency-Key": "missing-offer-probe"},
        json={**accept_body, "offer_id": missing_offer},
    )

    missing_grant = str(uuid.uuid4())
    revoke_body = {
        "operation": "unlink",
        "project_id": target_a,
        "expected_grant_digest": grant["grant_digest"],
    }
    foreign_grant = client.post(
        f"/api/v2/dataset-grants/{grant['grant_id']}/revoke-requests",
        headers={"Idempotency-Key": "foreign-grant-probe"},
        json=revoke_body,
    )
    missing_grant_response = client.post(
        f"/api/v2/dataset-grants/{missing_grant}/revoke-requests",
        headers={"Idempotency-Key": "missing-grant-probe"},
        json=revoke_body,
    )

    alias_body = {
        "operation": "create",
        "project_id": target_a,
        "alias_name": "foreign-probe",
        "snapshot_id": snapshot_id,
        "expected_revision": 0,
        "expected_head_revision_id": None,
        "expected_head_revision_digest": None,
    }
    foreign_alias = client.post(
        f"/api/v2/dataset-assets/{adopted['asset_id']}/alias-change-requests",
        headers={"Idempotency-Key": "foreign-alias-probe"},
        json=alias_body,
    )
    missing_alias = client.post(
        f"/api/v2/dataset-assets/{uuid.uuid4()}/alias-change-requests",
        headers={"Idempotency-Key": "missing-alias-probe"},
        json=alias_body,
    )
    for foreign, missing in (
        (foreign_accept, missing_accept),
        (foreign_grant, missing_grant_response),
        (foreign_alias, missing_alias),
    ):
        assert safe_error(foreign) == safe_error(missing) == (
            404,
            {
                "code": "not_found",
                "message": "Resource not found",
                "details": {},
            },
        )
    assert durable_counts() == before

    _session_for(client, main_module, TARGET_B_OWNER)
    for suffix, override in (
        ("digest", {"offer_digest": "0" * 64}),
        ("expiry", {"expires_at": "2026-08-09T00:00:00.000000Z"}),
    ):
        stale_offer = client.post(
            f"/api/v2/dataset-share-offers/{offer['offer_id']}/accept-requests",
            headers={"Idempotency-Key": f"rightful-stale-offer-{suffix}"},
            json={**accept_body, "target_project_id": target_b, **override},
        )
        assert stale_offer.status_code == 409
    stale_digest = client.post(
        f"/api/v2/dataset-grants/{grant['grant_id']}/revoke-requests",
        headers={"Idempotency-Key": "rightful-stale-grant-digest"},
        json={
            "operation": "unlink",
            "project_id": target_b,
            "expected_grant_digest": "0" * 64,
        },
    )
    assert stale_digest.status_code == 409

    with SQLiteUnitOfWork(database) as unit_of_work:
        revoke_approval = unit_of_work.run(
            lambda cursor: database.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant["grant_id"],
                operation="revoke",
                project_id=source,
                expected_grant_digest=grant["grant_digest"],
                requester_actor_id=SOURCE_MANAGER,
            )
        )
    database.apply_dataset_grant_revoke_decision(
        approval_id=revoke_approval,
        decision_actor_id=SOURCE_OWNER,
        decision_mechanism="session",
    )
    stale_state = client.post(
        f"/api/v2/dataset-grants/{grant['grant_id']}/revoke-requests",
        headers={"Idempotency-Key": "rightful-stale-grant-state"},
        json={
            "operation": "unlink",
            "project_id": target_b,
            "expected_grant_digest": grant["grant_digest"],
        },
    )
    assert stale_state.status_code == 409


def test_workspace_keeps_same_asset_separate_for_each_target_scope(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    source, target_a = _seed_projects(database)
    target_b = _seed_additional_target(database)
    for project_id in (target_a, target_b):
        _binding(
            database,
            project_id=project_id,
            actor_id=TARGET_OWNER,
            role=ProjectRoleV2.DATASET_MANAGER,
        )
    _binding(
        database,
        project_id=target_b,
        actor_id=TARGET_OWNER,
        role=ProjectRoleV2.OWNER,
    )
    first = "sharing-multi-scope-first"
    second = "sharing-multi-scope-second"
    _publish(database, first)
    second_approval = _publish(database, second)
    adopted = _adopt(database, source, first)
    _attach_snapshot(
        database,
        project_id=source,
        asset_id=adopted["asset_id"],
        snapshot_id=second,
        approval_id=second_approval,
    )

    for target, snapshots, requester, decider in (
        (target_a, [first], TARGET_OWNER, TARGET_REVIEWER),
        (target_b, [first, second], TARGET_OWNER, TARGET_B_REVIEWER),
    ):
        offer_approval = _request_offer(
            database,
            asset_id=adopted["asset_id"],
            source_project_id=source,
            target_project_id=target,
            snapshot_ids=snapshots,
            asset_digest=adopted["asset_digest"],
        )
        offer = database.apply_dataset_share_offer_decision(
            approval_id=offer_approval,
            decision_actor_id=SOURCE_REVIEWER,
            decision_mechanism="session",
        )
        database.apply_dataset_share_accept_decision(
            approval_id=_request_accept(
                database,
                offer,
                requester_actor_id=requester,
            ),
            decision_actor_id=decider,
            decision_mechanism="session",
        )

    alias_approval = _request_alias(
        database,
        project_id=target_b,
        asset_id=adopted["asset_id"],
        snapshot_id=second,
        actor_id=TARGET_OWNER,
        alias_name="target-b-latest",
        sharing_enabled=True,
    )
    database.apply_dataset_alias_change_decision(
        approval_id=alias_approval,
        decision_actor_id=TARGET_B_REVIEWER,
        decision_mechanism="session",
    )
    _enable_sharing(main_module)
    _session_for(client, main_module, TARGET_OWNER)
    response = client.get("/api/v2/workspace")
    assert response.status_code == 200
    projections = {
        item["scope_project_id"]: (
            item["asset_id"],
            item["access_mode"],
            item["snapshot_count"],
            item["active_alias_count"],
        )
        for item in response.json()["recent_dataset_assets"]["items"]
        if item["asset_id"] == adopted["asset_id"]
    }
    assert projections == {
        target_a: (adopted["asset_id"], "shared", 1, 0),
        target_b: (adopted["asset_id"], "shared", 2, 1),
    }


def test_dataset_sharing_http_is_idempotent_scoped_and_hidden_when_disabled(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    source, target = _seed_projects(database)
    snapshot_id = "sharing-http-snapshot"
    _publish(database, snapshot_id)
    adopted = _adopt(database, source, snapshot_id)
    asset_id = adopted["asset_id"]
    _enable_sharing(main_module)

    _session_for(client, main_module, SOURCE_MANAGER)
    offer_body = {
        "source_project_id": source,
        "target_project_id": target,
        "snapshot_ids": [snapshot_id],
        "expected_asset_digest": adopted["asset_digest"],
        "expires_at": _expiry(),
    }
    requested = client.post(
        f"/api/v2/dataset-assets/{asset_id}/share-offer-requests",
        headers={"Idempotency-Key": "sharing-http-offer"},
        json=offer_body,
    )
    replayed = client.post(
        f"/api/v2/dataset-assets/{asset_id}/share-offer-requests",
        headers={"Idempotency-Key": "sharing-http-offer"},
        json=offer_body,
    )
    assert requested.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json() == {**requested.json(), "replayed": True}

    _session_for(client, main_module, SOURCE_REVIEWER)
    offer_decision = client.post(
        f"/api/v2/approvals/{requested.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "sharing-http-offer-decision"},
        json={"decision": "approve"},
    )
    assert offer_decision.status_code == 202
    offer = offer_decision.json()

    _session_for(client, main_module, TARGET_OWNER)
    accept_body = {
        key: offer[key]
        for key in (
            "offer_id",
            "asset_id",
            "source_project_id",
            "target_project_id",
            "offer_digest",
            "expires_at",
            "snapshot_ids",
            "snapshot_set_digest",
        )
    }
    accept_request = client.post(
        f"/api/v2/dataset-share-offers/{offer['offer_id']}/accept-requests",
        headers={"Idempotency-Key": "sharing-http-accept"},
        json=accept_body,
    )
    assert accept_request.status_code == 202
    _session_for(client, main_module, TARGET_REVIEWER)
    accept_decision = client.post(
        f"/api/v2/approvals/{accept_request.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "sharing-http-accept-decision"},
        json={"decision": "approve"},
    )
    assert accept_decision.status_code == 202

    _session_for(client, main_module, TARGET_MANAGER)
    target_list = client.get(f"/api/v2/projects/{target}/datasets")
    target_detail = client.get(
        f"/api/v2/dataset-assets/{asset_id}",
        params={"project_id": target},
    )
    implicit_owner_scope = client.get(f"/api/v2/dataset-assets/{asset_id}")
    workspace = client.get("/api/v2/workspace")
    assert target_list.status_code == 200
    assert target_list.json()["items"][0]["access_mode"] == "shared"
    assert target_detail.status_code == 200
    assert target_detail.json()["scope_project_id"] == target
    assert implicit_owner_scope.status_code == 404
    assert workspace.status_code == 200
    assert workspace.json()["features"]["dataset_sharing_v2"]["value"] is True
    assert workspace.json()["capabilities"]["dataset_sharing"]["state"] == (
        "available"
    )
    assert [
        (item["asset_id"], item["access_mode"])
        for item in workspace.json()["recent_dataset_assets"]["items"]
    ] == [(asset_id, "shared")]

    target_alias = client.post(
        f"/api/v2/dataset-assets/{asset_id}/alias-change-requests",
        headers={"Idempotency-Key": "sharing-http-target-alias"},
        json={
            "operation": "create",
            "project_id": target,
            "alias_name": "target-http",
            "snapshot_id": snapshot_id,
            "expected_revision": 0,
            "expected_head_revision_id": None,
            "expected_head_revision_digest": None,
        },
    )
    assert target_alias.status_code == 202

    grant_id = accept_decision.json()["grant_ids"][0]
    grant = database.get_dataset_grant_read_model(grant_id)
    assert grant is not None
    _session_for(client, main_module, SOURCE_MANAGER)
    revoke_body = {
        "operation": "revoke",
        "project_id": source,
        "expected_grant_digest": grant["grant_digest"],
    }
    revoke_request = client.post(
        f"/api/v2/dataset-grants/{grant_id}/revoke-requests",
        headers={"Idempotency-Key": "sharing-http-revoke"},
        json=revoke_body,
    )
    revoke_request_replay = client.post(
        f"/api/v2/dataset-grants/{grant_id}/revoke-requests",
        headers={"Idempotency-Key": "sharing-http-revoke"},
        json=revoke_body,
    )
    assert revoke_request.status_code == 202
    assert revoke_request_replay.json() == {
        **revoke_request.json(),
        "replayed": True,
    }
    _session_for(client, main_module, SOURCE_OWNER)
    revoke_decision = client.post(
        f"/api/v2/approvals/{revoke_request.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "sharing-http-revoke-decision"},
        json={"decision": "approve"},
    )
    revoke_decision_replay = client.post(
        f"/api/v2/approvals/{revoke_request.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "sharing-http-revoke-decision"},
        json={"decision": "approve"},
    )
    assert revoke_decision.status_code == 202
    assert revoke_decision_replay.json() == {
        **revoke_decision.json(),
        "replayed": True,
    }

    _session_for(client, main_module, TARGET_MANAGER)
    main_module.app_state.config.dataset_sharing_v2_enabled = False
    hidden_offer = client.post(
        f"/api/v2/dataset-assets/{asset_id}/share-offer-requests",
        headers={"Idempotency-Key": "sharing-http-hidden"},
        json=offer_body,
    )
    hidden_alias = client.get(
        f"/api/v2/approvals/{target_alias.json()['approval_id']}"
    )
    hidden_target = client.get(
        f"/api/v2/dataset-assets/{asset_id}",
        params={"project_id": target},
    )
    hidden_workspace = client.get("/api/v2/workspace")
    assert hidden_offer.status_code == hidden_alias.status_code == 404
    assert hidden_target.status_code == 404
    assert hidden_workspace.status_code == 200
    assert "dataset_sharing_v2" not in hidden_workspace.json()["features"]
    assert "dataset_sharing" not in hidden_workspace.json()["capabilities"]
    assert hidden_workspace.json()["recent_dataset_assets"]["items"] == []

    _session_for(client, main_module, SOURCE_OWNER)
    owner_detail = client.get(f"/api/v2/dataset-assets/{asset_id}")
    owner_usage = client.get(f"/api/v2/dataset-assets/{asset_id}/usage")
    assert owner_detail.status_code == 200
    assert owner_usage.status_code == 200
    assert owner_usage.json()["active_grants"]["reason"] == (
        "dataset_sharing_v2_disabled"
    )
