"""Product v2 multi-role domain, authorization, and transaction tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app import approvals as approval_module
from app.authorization import (
    Action,
    AuthorizationReason,
    evaluate_authorization,
    evaluate_enforced_authorization,
)
from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, Database
from app.identity import (
    Actor,
    ActorType,
    ProjectMembership,
    ProjectRole,
    ProjectRoleBinding,
    ProjectRoleGrantProvenance,
    ProjectRoleV2,
    RequestContext,
    generate_session_token,
)
from app.project_roles import (
    ROLE_CHANGE_CONTRACT_VERSION,
    ProjectRBACReadinessReason,
    ProjectRBACState,
    evaluate_project_rbac_readiness,
    normalize_role_change,
    project_roles_digest,
    resulting_bindings_for_role_change,
    validate_resulting_role_change,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


PROJECT_ID = "10000000-0000-0000-0000-000000000001"
OTHER_PROJECT_ID = "10000000-0000-0000-0000-000000000002"
OWNER_ID = "20000000-0000-0000-0000-000000000001"
REVIEWER_ID = "20000000-0000-0000-0000-000000000002"
SERVICE_ID = "20000000-0000-0000-0000-000000000003"
TARGET_ID = "20000000-0000-0000-0000-000000000004"
OUTSIDER_ID = "20000000-0000-0000-0000-000000000005"
GRANTED_AT = "2026-08-01T00:00:00+00:00"


def _actor(
    actor_id: str,
    *,
    actor_type: ActorType = ActorType.HUMAN,
    disabled_at: str | None = None,
    platform_admin: bool = False,
) -> Actor:
    return Actor(
        id=actor_id,
        actor_type=actor_type,
        display_name=actor_id,
        disabled_at=disabled_at,
        platform_admin=platform_admin,
    )


def _binding(
    actor_id: str,
    role: ProjectRoleV2,
    *,
    project_id: str = PROJECT_ID,
    binding_id: str | None = None,
) -> ProjectRoleBinding:
    return ProjectRoleBinding(
        id=binding_id or f"{actor_id}:{role.value}",
        project_id=project_id,
        actor_id=actor_id,
        role=role,
        grant_provenance=ProjectRoleGrantProvenance.LEGACY_MEMBERSHIP,
        granted_at=GRANTED_AT,
    )


def _insert_active_binding(
    database: Database,
    *,
    project_id: str,
    actor_id: str,
    role: ProjectRoleV2,
    binding_id: str | None = None,
    provenance: ProjectRoleGrantProvenance = (ProjectRoleGrantProvenance.PROJECT_BOOTSTRAP),
) -> str:
    binding_id = binding_id or f"{actor_id}:{role.value}"
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_role_bindings (
                id, project_id, actor_id, role, grant_provenance, granted_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                project_id,
                actor_id,
                role.value,
                provenance.value,
                GRANTED_AT,
            ),
        )
    return binding_id


def _seed_ready_project(
    database: Database,
) -> tuple[str, Actor, Actor, Actor]:
    project_id = database.insert_project("product-rbac", "/tmp/product-rbac")
    owner = database.insert_actor(
        actor_id=OWNER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Owner",
    )
    reviewer = database.insert_actor(
        actor_id=REVIEWER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Reviewer",
    )
    target = database.insert_actor(
        actor_id=TARGET_ID,
        actor_type=ActorType.HUMAN,
        display_name="Target",
    )
    _insert_active_binding(
        database,
        project_id=project_id,
        actor_id=owner.id,
        role=ProjectRoleV2.OWNER,
    )
    _insert_active_binding(
        database,
        project_id=project_id,
        actor_id=reviewer.id,
        role=ProjectRoleV2.REVIEWER,
    )
    return project_id, owner, reviewer, target


def _create_role_change(
    database: Database,
    *,
    project_id: str,
    target_actor_id: str,
    requester_actor_id: str,
    add_roles: list[str],
    remove_roles: list[str],
    expected_roles_digest: str | None = None,
) -> int:
    digest = (
        expected_roles_digest
        if expected_roles_digest is not None
        else database.get_project_role_snapshot(project_id)["roles_digest"]
    )
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_project_role_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                target_actor_id=target_actor_id,
                add_roles=add_roles,
                remove_roles=remove_roles,
                expected_roles_digest=digest,
                requester_actor_id=requester_actor_id,
            )
        )


def _session_for(main_module, client, actor_id: str) -> None:
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


@pytest.mark.parametrize(
    ("bindings", "expected_state", "expected_reasons"),
    [
        (
            (),
            ProjectRBACState.NEEDS_ROLE_REPAIR,
            {
                ProjectRBACReadinessReason.OWNER_MISSING,
                ProjectRBACReadinessReason.APPROVAL_PAIR_MISSING,
            },
        ),
        (
            (
                _binding(OWNER_ID, ProjectRoleV2.OWNER),
                _binding(OWNER_ID, ProjectRoleV2.REVIEWER),
            ),
            ProjectRBACState.NEEDS_ROLE_REPAIR,
            {ProjectRBACReadinessReason.APPROVAL_PAIR_MISSING},
        ),
        (
            (
                _binding(OWNER_ID, ProjectRoleV2.OWNER),
                _binding(REVIEWER_ID, ProjectRoleV2.REVIEWER),
            ),
            ProjectRBACState.READY,
            set(),
        ),
        (
            (
                _binding(OWNER_ID, ProjectRoleV2.OWNER),
                _binding(REVIEWER_ID, ProjectRoleV2.OWNER),
            ),
            ProjectRBACState.READY,
            set(),
        ),
    ],
)
def test_readiness_requires_an_owner_and_two_distinct_enabled_humans(
    bindings,
    expected_state,
    expected_reasons,
):
    readiness = evaluate_project_rbac_readiness(
        bindings,
        {
            OWNER_ID: _actor(OWNER_ID),
            REVIEWER_ID: _actor(REVIEWER_ID),
        },
    )

    assert readiness.state is expected_state
    assert set(readiness.reasons) == expected_reasons


def test_readiness_ignores_disabled_humans_and_flags_non_human_privilege():
    bindings = (
        _binding(OWNER_ID, ProjectRoleV2.OWNER),
        _binding(REVIEWER_ID, ProjectRoleV2.REVIEWER),
        _binding(SERVICE_ID, ProjectRoleV2.OWNER),
    )
    readiness = evaluate_project_rbac_readiness(
        bindings,
        {
            OWNER_ID: _actor(OWNER_ID, disabled_at="2026-08-01T00:00:00+00:00"),
            REVIEWER_ID: _actor(REVIEWER_ID),
            SERVICE_ID: _actor(SERVICE_ID, actor_type=ActorType.SERVICE),
        },
        orphan_legacy_membership=True,
    )

    assert readiness.state is ProjectRBACState.NEEDS_ROLE_REPAIR
    assert set(readiness.reasons) == {
        ProjectRBACReadinessReason.OWNER_MISSING,
        ProjectRBACReadinessReason.APPROVAL_PAIR_MISSING,
        ProjectRBACReadinessReason.ORPHAN_LEGACY_MEMBERSHIP,
        ProjectRBACReadinessReason.NON_HUMAN_PRIVILEGED_BINDING,
    }


def test_roles_digest_binds_binding_identity_and_actor_security_state():
    owner = _actor(OWNER_ID)
    binding = _binding(OWNER_ID, ProjectRoleV2.OWNER, binding_id="binding-one")
    baseline = project_roles_digest((binding,), {OWNER_ID: owner})

    assert baseline != project_roles_digest(
        (replace(binding, id="binding-two"),),
        {OWNER_ID: owner},
    )
    assert baseline != project_roles_digest(
        (binding,),
        {OWNER_ID: _actor(OWNER_ID, disabled_at="2026-08-01T00:00:00+00:00")},
    )


@pytest.mark.parametrize(
    ("add_roles", "remove_roles"),
    [
        (["reviewer", "owner"], []),
        (["owner", "owner"], []),
        (["owner"], ["owner"]),
        ([], []),
    ],
)
def test_role_change_requires_sorted_unique_disjoint_nonempty_sets(add_roles, remove_roles):
    with pytest.raises(ValueError):
        normalize_role_change(
            target_actor_id=OWNER_ID,
            add_roles=add_roles,
            remove_roles=remove_roles,
            expected_roles_digest="a" * 64,
        )


def test_ineligible_project_allows_only_monotonic_human_repair():
    current = (_binding(OWNER_ID, ProjectRoleV2.OWNER),)
    actors = {
        OWNER_ID: _actor(OWNER_ID),
        REVIEWER_ID: _actor(REVIEWER_ID),
    }
    projected = resulting_bindings_for_role_change(
        current,
        target_actor_id=REVIEWER_ID,
        add_roles=(ProjectRoleV2.REVIEWER,),
        remove_roles=(),
    )

    repaired = validate_resulting_role_change(
        current_bindings=current,
        projected_bindings=projected,
        actors=actors,
        target_actor=actors[REVIEWER_ID],
        add_roles=(ProjectRoleV2.REVIEWER,),
        remove_roles=(),
    )

    assert repaired.state is ProjectRBACState.READY
    with pytest.raises(ValueError, match="cannot remove"):
        validate_resulting_role_change(
            current_bindings=current,
            projected_bindings=(),
            actors=actors,
            target_actor=actors[OWNER_ID],
            add_roles=(),
            remove_roles=(ProjectRoleV2.OWNER,),
        )


V2_ACTION_MATRIX = {
    ProjectRoleV2.OWNER: {
        Action.PROJECT_VIEW,
        Action.PROJECT_ADMIN,
        Action.PROJECT_MEMBERSHIP_MANAGE,
        Action.PROJECT_ROLE_VIEW,
        Action.PROJECT_ROLE_MANAGE,
        Action.APPROVAL_VIEW,
        Action.APPROVAL_DECIDE,
        Action.DATASET_WITHDRAW,
    },
    ProjectRoleV2.OPERATOR: {Action.PROJECT_VIEW, Action.PROJECT_OPERATE},
    ProjectRoleV2.REVIEWER: {
        Action.PROJECT_VIEW,
        Action.PROJECT_ROLE_VIEW,
        Action.APPROVAL_VIEW,
        Action.APPROVAL_DECIDE,
    },
    ProjectRoleV2.DATASET_MANAGER: {
        Action.PROJECT_VIEW,
        Action.DATASET_MANAGE,
        Action.DATASET_SHARE,
        Action.DATASET_WITHDRAW,
    },
    ProjectRoleV2.VIEWER: {Action.PROJECT_VIEW},
}
PROJECT_V2_ACTIONS = tuple(
    sorted(
        set().union(*V2_ACTION_MATRIX.values()),
        key=lambda action: action.value,
    )
)


@pytest.mark.parametrize(
    ("role", "action"),
    [(role, action) for role in ProjectRoleV2 for action in PROJECT_V2_ACTIONS],
)
def test_product_role_action_matrix_has_no_hierarchy(role, action):
    context = RequestContext(
        actor=_actor(OWNER_ID),
        authentication_method="session",
        project_role_bindings=(_binding(OWNER_ID, role),),
        project_roles_v2_enabled=True,
    )

    decision = evaluate_authorization(context, action, project_id=PROJECT_ID)

    assert decision.allowed is (action in V2_ACTION_MATRIX[role])
    assert decision.project_roles_v2 == (role,)
    assert decision.project_role is None


def test_multi_role_permissions_are_a_union_and_cross_project_fails_closed():
    context = RequestContext(
        actor=_actor(OWNER_ID),
        authentication_method="session",
        project_role_bindings=(
            _binding(OWNER_ID, ProjectRoleV2.OPERATOR),
            _binding(OWNER_ID, ProjectRoleV2.DATASET_MANAGER),
        ),
        project_roles_v2_enabled=True,
    )

    assert evaluate_authorization(context, Action.PROJECT_OPERATE, project_id=PROJECT_ID).allowed
    assert evaluate_authorization(context, Action.DATASET_SHARE, project_id=PROJECT_ID).allowed
    cross_project = evaluate_authorization(
        context, Action.PROJECT_VIEW, project_id=OTHER_PROJECT_ID
    )
    assert not cross_project.allowed
    assert cross_project.reason is AuthorizationReason.DENIED_CROSS_PROJECT


def test_service_role_permission_intersects_scope_and_never_decides():
    context = RequestContext(
        actor=_actor(SERVICE_ID, actor_type=ActorType.SERVICE),
        authentication_method="service_token",
        service_scopes=frozenset({Action.DATASET_MANAGE.value, Action.APPROVAL_DECIDE.value}),
        project_role_bindings=(
            _binding(SERVICE_ID, ProjectRoleV2.DATASET_MANAGER),
            _binding(SERVICE_ID, ProjectRoleV2.OWNER),
        ),
        project_roles_v2_enabled=True,
    )

    assert evaluate_authorization(context, Action.DATASET_MANAGE, project_id=PROJECT_ID).allowed
    denied = evaluate_authorization(context, Action.APPROVAL_DECIDE, project_id=PROJECT_ID)
    assert not denied.allowed
    assert denied.reason is AuthorizationReason.DENIED_SERVICE_ACTION
    assert not evaluate_authorization(context, Action.PROJECT_ADMIN, project_id=PROJECT_ID).allowed


def test_flag_off_uses_only_legacy_membership():
    context = RequestContext(
        actor=_actor(OWNER_ID),
        authentication_method="session",
        project_memberships=(
            ProjectMembership(
                project_id=PROJECT_ID,
                actor_id=OWNER_ID,
                role=ProjectRole.VIEWER,
            ),
        ),
        project_role_bindings=(_binding(OWNER_ID, ProjectRoleV2.OWNER),),
        project_roles_v2_enabled=False,
    )

    assert not evaluate_authorization(context, Action.PROJECT_ADMIN, project_id=PROJECT_ID).allowed
    assert evaluate_authorization(context, Action.PROJECT_VIEW, project_id=PROJECT_ID).allowed


def test_platform_admin_still_cannot_decide_own_high_risk_request():
    context = RequestContext(
        actor=_actor(OWNER_ID, platform_admin=True),
        authentication_method="session",
        project_roles_v2_enabled=True,
    )

    decision = evaluate_enforced_authorization(
        context,
        Action.APPROVAL_DECIDE,
        project_id=PROJECT_ID,
        requester_actor_id=OWNER_ID,
        high_risk=True,
    )

    assert not decision.allowed
    assert decision.reason is AuthorizationReason.DENIED_HIGH_RISK_SELF_DECISION


def test_role_change_approval_has_exact_immutable_contract_and_applies_atomically(
    db,
):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    expected_digest = db.get_project_role_snapshot(project_id)["roles_digest"]

    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["operator", "viewer"],
        remove_roles=[],
        expected_roles_digest=expected_digest,
    )

    pending = db.get_approval(approval_id)
    assert pending is not None
    assert pending.kind == "project_role_change"
    assert pending.status == "pending"
    assert pending.requester_actor_id == owner.id
    assert pending.payload_contract_version == ROLE_CHANGE_CONTRACT_VERSION
    assert pending.payload_immutable_at is not None
    assert pending.payload_sha256 is not None
    assert pending.payload == {
        "add_roles": ["operator", "viewer"],
        "contract_version": ROLE_CHANGE_CONTRACT_VERSION,
        "expected_roles_digest": expected_digest,
        "project_id": project_id,
        "remove_roles": [],
        "target_actor_id": target.id,
    }

    result = db.apply_project_role_change_decision(
        approval_id=approval_id,
        decision_actor_id=reviewer.id,
        decision_mechanism="test_session",
        note="approved by an independent reviewer",
    )

    decided = db.get_approval(approval_id)
    assert decided is not None
    assert decided.status == "approved"
    assert decided.decision_actor_id == reviewer.id
    assert decided.decision_mechanism == "test_session"
    target_bindings = db.list_project_role_bindings(
        project_id=project_id,
        actor_id=target.id,
        active_only=True,
    )
    assert {binding.role for binding in target_bindings} == {
        ProjectRoleV2.OPERATOR,
        ProjectRoleV2.VIEWER,
    }
    assert all(
        binding.grant_provenance is ProjectRoleGrantProvenance.APPROVED_ROLE_CHANGE
        and binding.grant_approval_id == approval_id
        for binding in target_bindings
    )
    assert result["roles_digest"] == db.get_project_role_snapshot(project_id)["roles_digest"]
    assert result["rbac_state"] == "ready"

    relevant_events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event.get("approval_id") == approval_id
    ]
    assert [
        (event["action"], event["resource_type"], event["result"])
        for event in reversed(relevant_events)
    ] == [
        ("approval_created", "approval", "pending"),
        ("membership_granted", "project_role_binding", "created"),
        ("membership_granted", "project_role_binding", "created"),
        ("approval_decided", "approval", "approved"),
    ]


def test_role_change_reject_preserves_bindings_and_records_only_decision(db):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    before = db.get_project_role_snapshot(project_id)["roles_digest"]

    db.reject_project_role_change_decision(
        approval_id=approval_id,
        decision_actor_id=reviewer.id,
        decision_mechanism="test_session",
        note="not needed",
    )

    rejected = db.get_approval(approval_id)
    assert rejected is not None
    assert rejected.status == "rejected"
    assert (
        db.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
        == []
    )
    assert db.get_project_role_snapshot(project_id)["roles_digest"] == before
    relevant_events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event.get("approval_id") == approval_id
    ]
    assert {(event["action"], event["result"]) for event in relevant_events} == {
        ("approval_created", "pending"),
        ("approval_decided", "rejected"),
    }


def test_role_change_reject_refuses_tampered_immutable_payload(db):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    approval = db.get_approval(approval_id)
    assert approval is not None
    # Simulate on-disk corruption below the public DB API. The ordinary write
    # path is already protected by ``approvals_execution_pin_update_guard``.
    with db.cursor() as cursor:
        cursor.execute("DROP TRIGGER approvals_execution_pin_update_guard")
        cursor.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            ('{"tampered":true}', approval_id),
        )

    with pytest.raises(ValueError, match="approval contract is invalid"):
        db.reject_project_role_change_decision(
            approval_id=approval_id,
            decision_actor_id=reviewer.id,
            decision_mechanism="test_session",
        )

    rejected = db.get_approval(approval_id)
    assert rejected is not None
    assert rejected.status == "pending"
    assert (
        db.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
        == []
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_generic_decision_paths_refuse_transaction_only_role_changes(
    db,
    monkeypatch,
    decision,
):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    before_bindings = db.list_project_role_bindings(
        project_id=project_id,
        active_only=False,
    )
    before_events = db.list_durable_audit_events(limit=100)
    legacy_calls = []
    monkeypatch.setattr(
        approval_module.audit_module,
        "append_audit",
        lambda *args, **kwargs: legacy_calls.append((args, kwargs)),
    )

    assert TRANSACTION_ONLY_APPROVAL_KINDS == {
        "environment_change_v2",
        "project_defaults_change_v2",
        "project_bootstrap_v2",
        "project_role_change",
        "run_template_change_v2",
        "dataset_asset_adoption_v2",
        "dataset_alias_change_v2",
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
            "dataset_grant_revoke_v2",
            "dataset_publish_v2",
            "execution_plan_v2",
        }
    with pytest.raises(ValueError, match="must use the Product v2 decision"):
        if decision == "approve":
            asyncio.run(approval_module.approve(db, approval_id))
        else:
            approval_module.reject(db, approval_id)

    approval = db.get_approval(approval_id)
    assert approval is not None
    assert approval.status == "pending"
    assert (
        db.list_project_role_bindings(
            project_id=project_id,
            active_only=False,
        )
        == before_bindings
    )
    assert db.list_durable_audit_events(limit=100) == before_events
    assert legacy_calls == []


def test_product_role_decisions_never_use_legacy_audit_writer(db, monkeypatch):
    project_id, owner, reviewer, target = _seed_ready_project(db)

    def fail_legacy_audit(*_args, **_kwargs):
        raise AssertionError("Product v2 role decisions must not write legacy audit")

    monkeypatch.setattr(
        approval_module.audit_module,
        "append_audit",
        fail_legacy_audit,
    )
    approved_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    db.apply_project_role_change_decision(
        approval_id=approved_id,
        decision_actor_id=reviewer.id,
        decision_mechanism="test_session",
    )
    rejected_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["operator"],
        remove_roles=[],
    )
    db.reject_project_role_change_decision(
        approval_id=rejected_id,
        decision_actor_id=reviewer.id,
        decision_mechanism="test_session",
    )

    assert db.get_approval(approved_id).status == "approved"
    assert db.get_approval(rejected_id).status == "rejected"
    relevant_events = {
        approval_id: {
            event["action"]
            for event in db.list_durable_audit_events(limit=100)
            if event.get("approval_id") == approval_id
        }
        for approval_id in (approved_id, rejected_id)
    }
    assert relevant_events == {
        approved_id: {
            "approval_created",
            "approval_decided",
            "membership_granted",
        },
        rejected_id: {"approval_created", "approval_decided"},
    }


def test_role_change_revalidates_digest_requester_and_high_risk_separation(db):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    with pytest.raises(ValueError, match="roles_digest_conflict"):
        _create_role_change(
            db,
            project_id=project_id,
            target_actor_id=target.id,
            requester_actor_id=owner.id,
            add_roles=["viewer"],
            remove_roles=[],
            expected_roles_digest="a" * 64,
        )
    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )

    with pytest.raises(ValueError, match="high_risk_self_decision"):
        db.apply_project_role_change_decision(
            approval_id=approval_id,
            decision_actor_id=owner.id,
            decision_mechanism="test_session",
        )

    _insert_active_binding(
        db,
        project_id=project_id,
        actor_id=target.id,
        role=ProjectRoleV2.OPERATOR,
    )
    with pytest.raises(ValueError, match="roles_digest_conflict"):
        db.apply_project_role_change_decision(
            approval_id=approval_id,
            decision_actor_id=reviewer.id,
            decision_mechanism="test_session",
        )
    assert db.get_approval(approval_id).status == "pending"
    assert {
        binding.role
        for binding in db.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
    } == {ProjectRoleV2.OPERATOR}


def test_role_change_revalidates_requester_and_target_security_state(db):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    requester_approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    db.update_actor(owner.id, disabled_at="2026-08-07T00:00:00+00:00")

    with pytest.raises(ValueError, match="requester must be an enabled human"):
        db.apply_project_role_change_decision(
            approval_id=requester_approval_id,
            decision_actor_id=reviewer.id,
            decision_mechanism="test_session",
        )
    assert db.get_approval(requester_approval_id).status == "pending"

    db.update_actor(owner.id, disabled_at=None)
    target_approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    db.update_actor(target.id, disabled_at="2026-08-07T00:00:00+00:00")

    with pytest.raises(ValueError, match="target actor is disabled"):
        db.apply_project_role_change_decision(
            approval_id=target_approval_id,
            decision_actor_id=reviewer.id,
            decision_mechanism="test_session",
        )
    assert db.get_approval(target_approval_id).status == "pending"


def test_role_change_protects_last_owner_and_two_person_approval_pair(db):
    project_id, owner, reviewer, _target = _seed_ready_project(db)

    with pytest.raises(ValueError, match="violate project RBAC readiness"):
        _create_role_change(
            db,
            project_id=project_id,
            target_actor_id=owner.id,
            requester_actor_id=owner.id,
            add_roles=[],
            remove_roles=["owner"],
        )
    with pytest.raises(ValueError, match="violate project RBAC readiness"):
        _create_role_change(
            db,
            project_id=project_id,
            target_actor_id=reviewer.id,
            requester_actor_id=owner.id,
            add_roles=[],
            remove_roles=["reviewer"],
        )
    assert db.list_approvals(kind="project_role_change") == []


def test_role_change_audit_failure_rolls_back_bindings_and_decision(
    db,
    monkeypatch,
):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    event_count = db.count_durable_audit_events()
    original = db.append_durable_audit_event_in_transaction

    def fail_decision_audit(cursor, **kwargs):
        if kwargs.get("action") == "approval_decided":
            raise RuntimeError("injected role decision audit failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(
        db,
        "append_durable_audit_event_in_transaction",
        fail_decision_audit,
    )
    with pytest.raises(RuntimeError, match="injected role decision audit failure"):
        db.apply_project_role_change_decision(
            approval_id=approval_id,
            decision_actor_id=reviewer.id,
            decision_mechanism="test_session",
        )

    assert db.get_approval(approval_id).status == "pending"
    assert (
        db.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
        == []
    )
    assert db.count_durable_audit_events() == event_count


def test_v2_role_change_never_projects_back_to_legacy_membership(db):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["reviewer"],
        remove_roles=[],
    )

    db.apply_project_role_change_decision(
        approval_id=approval_id,
        decision_actor_id=reviewer.id,
        decision_mechanism="test_session",
    )

    assert (
        db.list_project_memberships(
            project_id=project_id,
            actor_id=target.id,
        )
        == []
    )


def test_direct_legacy_membership_helpers_never_invent_v2_provenance(db):
    project_id = db.insert_project("legacy-helper", "/tmp/legacy-helper")
    actor = db.insert_actor(
        actor_id=TARGET_ID,
        actor_type=ActorType.HUMAN,
        display_name="Legacy helper target",
    )
    before = db.list_project_role_bindings(
        project_id=project_id,
        actor_id=actor.id,
    )

    db.upsert_project_membership(
        project=project_id,
        actor_id=actor.id,
        role=ProjectRole.ADMIN,
    )
    after_upsert = db.list_project_role_bindings(
        project_id=project_id,
        actor_id=actor.id,
    )
    db.delete_project_membership(project_id, actor.id)
    after_delete = db.list_project_role_bindings(
        project_id=project_id,
        actor_id=actor.id,
    )

    assert before == after_upsert == after_delete == []


def test_approved_v1_membership_decisions_sync_only_legacy_provenance(db):
    project_id = db.insert_project("approved-legacy", "/tmp/approved-legacy")
    target = db.insert_actor(
        actor_id=TARGET_ID,
        actor_type=ActorType.HUMAN,
        display_name="Approved legacy target",
    )
    decider = db.insert_actor(
        actor_id=OWNER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Legacy decider",
    )
    preexisting_owner_binding_id = _insert_active_binding(
        db,
        project_id=project_id,
        actor_id=target.id,
        role=ProjectRoleV2.OWNER,
    )
    upsert_approval_id = db.insert_approval(
        "project_membership_upsert",
        {
            "project_id": project_id,
            "actor_id": target.id,
            "role": "admin",
        },
        requester_actor_id=decider.id,
    )

    db.apply_project_membership_decision(
        approval_id=upsert_approval_id,
        project_id=project_id,
        actor_id=target.id,
        role=ProjectRole.ADMIN,
        created_by_actor_id=decider.id,
        decision_actor_id=decider.id,
        decision_actor_kind="human",
        decision_mechanism="test_session",
    )

    upsert_decision = db.get_approval(upsert_approval_id)
    assert upsert_decision is not None
    active_bindings = db.list_project_role_bindings(
        project_id=project_id,
        actor_id=target.id,
        active_only=True,
    )
    assert {binding.role for binding in active_bindings} == {
        ProjectRoleV2.OWNER,
        ProjectRoleV2.OPERATOR,
        ProjectRoleV2.REVIEWER,
        ProjectRoleV2.DATASET_MANAGER,
    }
    assert (
        next(binding for binding in active_bindings if binding.role is ProjectRoleV2.OWNER).id
        == preexisting_owner_binding_id
    )
    legacy_bindings = [
        binding
        for binding in active_bindings
        if binding.grant_provenance is ProjectRoleGrantProvenance.LEGACY_MEMBERSHIP
    ]
    assert {binding.role for binding in legacy_bindings} == {
        ProjectRoleV2.OPERATOR,
        ProjectRoleV2.REVIEWER,
        ProjectRoleV2.DATASET_MANAGER,
    }
    assert all(
        binding.grant_approval_id == upsert_approval_id
        and binding.granted_at == upsert_decision.decided_at
        for binding in legacy_bindings
    )
    unrelated_viewer_binding_id = _insert_active_binding(
        db,
        project_id=project_id,
        actor_id=target.id,
        role=ProjectRoleV2.VIEWER,
    )
    remove_approval_id = db.insert_approval(
        "project_membership_remove",
        {"project_id": project_id, "actor_id": target.id},
        requester_actor_id=decider.id,
    )

    db.apply_project_membership_decision(
        approval_id=remove_approval_id,
        project_id=project_id,
        actor_id=target.id,
        remove=True,
        decision_actor_id=decider.id,
        decision_actor_kind="human",
        decision_mechanism="test_session",
    )

    remove_decision = db.get_approval(remove_approval_id)
    assert remove_decision is not None
    all_bindings = db.list_project_role_bindings(
        project_id=project_id,
        actor_id=target.id,
    )
    assert {binding.id for binding in all_bindings if binding.active} == {
        preexisting_owner_binding_id,
        unrelated_viewer_binding_id,
    }
    revoked_legacy = [
        binding
        for binding in all_bindings
        if binding.grant_provenance is ProjectRoleGrantProvenance.LEGACY_MEMBERSHIP
    ]
    assert len(revoked_legacy) == 3
    assert all(
        binding.revocation_approval_id == remove_approval_id
        and binding.revoked_at == remove_decision.decided_at
        for binding in revoked_legacy
    )


def test_public_v1_membership_approval_flow_uses_provenance_sync(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id = database.insert_project(
        "public-approved-legacy",
        "/tmp/public-approved-legacy",
    )
    target = database.insert_actor(
        actor_id=TARGET_ID,
        actor_type=ActorType.HUMAN,
        display_name="Public legacy target",
    )
    main_module.app_state.config.identity_admin_enabled = True

    requested = client.post(
        f"/projects/{project_id}/memberships/request",
        json={"actor_id": target.id, "role": "admin"},
    )
    assert requested.status_code == 200
    upsert_approval_id = requested.json()["id"]
    approved = client.post(f"/approve/{upsert_approval_id}")
    assert approved.status_code == 200
    upsert_decision = database.get_approval(upsert_approval_id)
    active = database.list_project_role_bindings(
        project_id=project_id,
        actor_id=target.id,
        active_only=True,
    )
    assert len(active) == 4
    assert all(
        binding.grant_provenance is ProjectRoleGrantProvenance.LEGACY_MEMBERSHIP
        and binding.grant_approval_id == upsert_approval_id
        and binding.granted_at == upsert_decision.decided_at
        for binding in active
    )

    removal = client.post(f"/projects/{project_id}/memberships/{target.id}/remove-request")
    assert removal.status_code == 200
    remove_approval_id = removal.json()["id"]
    removed = client.post(f"/approve/{remove_approval_id}")
    assert removed.status_code == 200
    remove_decision = database.get_approval(remove_approval_id)
    revoked = database.list_project_role_bindings(
        project_id=project_id,
        actor_id=target.id,
    )
    assert len(revoked) == 4
    assert all(
        binding.revocation_approval_id == remove_approval_id
        and binding.revoked_at == remove_decision.decided_at
        for binding in revoked
    )


def test_corrupt_non_human_privilege_remains_fail_closed_and_not_removable(db):
    project_id, owner, reviewer, target = _seed_ready_project(db)
    service = db.insert_actor(
        actor_id=SERVICE_ID,
        actor_type=ActorType.SERVICE,
        display_name="Corrupt privileged service",
    )
    corrupt_binding_id = _insert_active_binding(
        db,
        project_id=project_id,
        actor_id=service.id,
        role=ProjectRoleV2.OWNER,
    )
    assert db.get_project_role_snapshot(project_id)["readiness"].state is (
        ProjectRBACState.NEEDS_ROLE_REPAIR
    )

    repair_approval_id = _create_role_change(
        db,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["reviewer"],
        remove_roles=[],
    )
    db.apply_project_role_change_decision(
        approval_id=repair_approval_id,
        decision_actor_id=reviewer.id,
        decision_mechanism="test_session",
    )
    readiness = db.get_project_role_snapshot(project_id)["readiness"]
    assert readiness.state is ProjectRBACState.NEEDS_ROLE_REPAIR
    assert ProjectRBACReadinessReason.NON_HUMAN_PRIVILEGED_BINDING in (readiness.reasons)

    with pytest.raises(
        ValueError,
        match="projects needing role repair cannot remove roles",
    ):
        _create_role_change(
            db,
            project_id=project_id,
            target_actor_id=service.id,
            requester_actor_id=owner.id,
            add_roles=[],
            remove_roles=["owner"],
        )
    assert {
        binding.id
        for binding in db.list_project_role_bindings(
            project_id=project_id,
            actor_id=service.id,
            active_only=True,
        )
    } == {corrupt_binding_id}


@pytest.mark.parametrize("role", ["owner", "reviewer"])
def test_approved_role_change_cannot_grant_privileged_role_to_service(db, role):
    project_id, owner, _reviewer, _target = _seed_ready_project(db)
    service = db.insert_actor(
        actor_id=SERVICE_ID,
        actor_type=ActorType.SERVICE,
        display_name="Service",
    )

    with pytest.raises(ValueError, match="non-human actors cannot hold"):
        _create_role_change(
            db,
            project_id=project_id,
            target_actor_id=service.id,
            requester_actor_id=owner.id,
            add_roles=[role],
            remove_roles=[],
        )
    assert (
        db.list_project_role_bindings(
            project_id=project_id,
            actor_id=service.id,
        )
        == []
    )


@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled"),
    [
        (False, False),
        (True, False),
        (False, True),
    ],
)
def test_product_role_routes_are_opaque_until_both_flags_are_enabled(
    api_client,
    api_enabled,
    rbac_enabled,
):
    client, main_module = api_client
    project_id = main_module.app_state.db.insert_project(
        "hidden-product-rbac",
        "/tmp/hidden-product-rbac",
    )
    main_module.app_state.config.api_v2_enabled = api_enabled
    main_module.app_state.config.product_rbac_v2_enabled = rbac_enabled

    response = client.get(f"/api/v2/projects/{project_id}/roles")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_product_role_route_requires_authentication_when_enabled(api_client):
    client, main_module = api_client
    project_id = main_module.app_state.db.insert_project(
        "anonymous-product-rbac",
        "/tmp/anonymous-product-rbac",
    )
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True

    response = client.get(f"/api/v2/projects/{project_id}/roles")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_product_role_http_flow_is_paginated_idempotent_and_high_risk(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id, owner, reviewer, target = _seed_ready_project(database)
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True
    _session_for(main_module, client, owner.id)

    first_page = client.get(
        f"/api/v2/projects/{project_id}/roles",
        params={"limit": 1},
    )
    assert first_page.status_code == 200
    assert first_page.json()["rbac_state"] == "ready"
    assert len(first_page.json()["items"]) == 1
    assert first_page.json()["next_cursor"] is not None
    expected_digest = first_page.json()["roles_digest"]

    second_page = client.get(
        f"/api/v2/projects/{project_id}/roles",
        params={
            "limit": 1,
            "cursor": first_page.json()["next_cursor"],
        },
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["next_cursor"] is None
    assert {item["role"] for item in first_page.json()["items"] + second_page.json()["items"]} == {
        "owner",
        "reviewer",
    }

    body = {
        "actor_id": target.id,
        "add_roles": ["operator"],
        "remove_roles": [],
        "expected_roles_digest": expected_digest,
    }
    missing_key = client.post(
        f"/api/v2/projects/{project_id}/role-change-requests",
        json=body,
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "idempotency_key_required"

    created = client.post(
        f"/api/v2/projects/{project_id}/role-change-requests",
        json=body,
        headers={"Idempotency-Key": "role-request-1"},
    )
    assert created.status_code == 202
    assert created.json()["status"] == "pending"
    assert created.json()["replayed"] is False
    approval_id = created.json()["approval_id"]

    replayed = client.post(
        f"/api/v2/projects/{project_id}/role-change-requests",
        json=body,
        headers={"Idempotency-Key": "role-request-1"},
    )
    assert replayed.status_code == 202
    assert replayed.json() == {
        "approval_id": approval_id,
        "replayed": True,
        "status": "pending",
    }
    changed_payload = client.post(
        f"/api/v2/projects/{project_id}/role-change-requests",
        json={
            **body,
            "add_roles": ["viewer"],
        },
        headers={"Idempotency-Key": "role-request-1"},
    )
    assert changed_payload.status_code == 409
    assert changed_payload.json()["error"]["code"] == "idempotency_key_reused"

    approval_page = client.get(
        "/api/v2/approvals",
        params={"kind": "project_role_change", "status": "pending"},
    )
    assert approval_page.status_code == 200
    assert approval_page.headers["Cache-Control"] == "no-store"
    assert len(approval_page.json()["items"]) == 1
    assert "payload" not in approval_page.json()["items"][0]
    own_detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert own_detail.status_code == 200
    assert own_detail.json()["payload_verified"] is True
    assert own_detail.json()["payload"] == {
        "add_roles": ["operator"],
        "contract_version": ROLE_CHANGE_CONTRACT_VERSION,
        "expected_roles_digest": expected_digest,
        "project_id": project_id,
        "remove_roles": [],
        "target_actor_id": target.id,
    }
    assert own_detail.json()["can_decide"] is False

    own_decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "self approval is forbidden"},
        headers={"Idempotency-Key": "role-decision-owner"},
    )
    assert own_decision.status_code == 403
    assert own_decision.json()["error"]["details"]["reason"] == ("denied_high_risk_self_decision")
    assert database.get_approval(approval_id).status == "pending"

    _session_for(main_module, client, reviewer.id)
    cursor_is_actor_bound = client.get(
        f"/api/v2/projects/{project_id}/roles",
        params={
            "limit": 1,
            "cursor": first_page.json()["next_cursor"],
        },
    )
    assert cursor_is_actor_bound.status_code == 400
    assert cursor_is_actor_bound.json()["error"]["code"] == "invalid_cursor"
    reviewer_detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert reviewer_detail.status_code == 200
    assert reviewer_detail.json()["can_decide"] is True

    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "independent review"},
        headers={"Idempotency-Key": "role-decision-reviewer"},
    )
    assert approved.status_code == 202
    assert approved.json() == {
        "approval_id": approval_id,
        "replayed": False,
        "status": "approved",
    }
    decision_replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "independent review"},
        headers={"Idempotency-Key": "role-decision-reviewer"},
    )
    assert decision_replay.status_code == 202
    assert decision_replay.json()["replayed"] is True
    assert {
        binding.role
        for binding in database.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
    } == {ProjectRoleV2.OPERATOR}


@pytest.mark.parametrize("authorization_mode", ["off", "enforce"])
def test_role_decision_lookup_is_opaque_across_projects(
    api_client,
    authorization_mode,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id, owner, _reviewer, target = _seed_ready_project(database)
    other_project_id = database.insert_project(
        "other-product-rbac",
        "/tmp/other-product-rbac",
    )
    outsider = database.insert_actor(
        actor_id=OUTSIDER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Other project reviewer",
    )
    _insert_active_binding(
        database,
        project_id=other_project_id,
        actor_id=outsider.id,
        role=ProjectRoleV2.REVIEWER,
    )
    role_approval_id = _create_role_change(
        database,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    non_role_approval_id = database.insert_approval(
        "enqueue",
        {"command": "true"},
        requester_actor_id=owner.id,
    )
    malformed_role_approval_id = database.insert_approval(
        "project_role_change",
        {"project_id": "not-a-canonical-uuid"},
        requester_actor_id=owner.id,
    )
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True
    main_module.app_state.config.authorization_mode = authorization_mode
    _session_for(main_module, client, outsider.id)
    with database.cursor() as cursor:
        idempotency_before = cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[
            0
        ]

    cross_project_list = client.get(f"/api/v2/projects/{project_id}/roles")
    cross_project_request = client.post(
        f"/api/v2/projects/{project_id}/role-change-requests",
        json={
            "actor_id": target.id,
            "add_roles": ["viewer"],
            "remove_roles": [],
            "expected_roles_digest": database.get_project_role_snapshot(project_id)["roles_digest"],
        },
        headers={"Idempotency-Key": f"cross-project-request-{authorization_mode}"},
    )
    assert cross_project_list.status_code == 403
    assert cross_project_request.status_code == 403

    review_responses = [
        client.get(f"/api/v2/approvals/{approval_id}")
        for approval_id in (
            999_999,
            non_role_approval_id,
            malformed_role_approval_id,
            role_approval_id,
        )
    ]
    assert all(response.status_code == 404 for response in review_responses)
    assert client.get("/api/v2/approvals?status=pending").json() == {
        "items": [],
        "next_cursor": None,
    }

    responses = [
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            json={"decision": "reject"},
            headers={"Idempotency-Key": f"opaque-{authorization_mode}-{index}"},
        )
        for index, approval_id in enumerate(
            (
                999_999,
                non_role_approval_id,
                malformed_role_approval_id,
                role_approval_id,
            )
        )
    ]

    public_errors = []
    for response in responses:
        assert response.status_code == 404
        error = dict(response.json()["error"])
        assert error.pop("request_id") == response.headers["X-Request-ID"]
        public_errors.append(error)
        assert project_id not in response.text
        assert "project_role_change" not in response.text
        assert "denied_" not in response.text
    assert (
        public_errors
        == [
            {
                "code": "not_found",
                "message": "Resource not found",
                "details": {},
            }
        ]
        * 4
    )
    assert database.get_approval(role_approval_id).status == "pending"
    assert (
        database.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
        == []
    )
    with database.cursor() as cursor:
        assert (
            cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0]
            == idempotency_before
        )


def test_same_project_operator_role_denial_remains_actionable_403(api_client):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id, owner, _reviewer, target = _seed_ready_project(database)
    operator = database.insert_actor(
        actor_id=OUTSIDER_ID,
        actor_type=ActorType.HUMAN,
        display_name="Same project operator",
    )
    _insert_active_binding(
        database,
        project_id=project_id,
        actor_id=operator.id,
        role=ProjectRoleV2.OPERATOR,
    )
    approval_id = _create_role_change(
        database,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True
    _session_for(main_module, client, operator.id)

    response = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "reject"},
        headers={"Idempotency-Key": "operator-role-denial"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert response.json()["error"]["details"]["reason"] == ("denied_project_role_insufficient")
    with database.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0] == 0


def test_role_decision_http_audit_failure_rolls_back_idempotency_and_state(
    api_client,
    monkeypatch,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id, owner, reviewer, target = _seed_ready_project(database)
    approval_id = _create_role_change(
        database,
        project_id=project_id,
        target_actor_id=target.id,
        requester_actor_id=owner.id,
        add_roles=["viewer"],
        remove_roles=[],
    )
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True
    _session_for(main_module, client, reviewer.id)
    event_count = database.count_durable_audit_events()
    with database.cursor() as cursor:
        idempotency_count = cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[
            0
        ]
    original = database.append_durable_audit_event_in_transaction

    def fail_decision_audit(cursor, **kwargs):
        if kwargs.get("action") == "approval_decided":
            raise RuntimeError("injected Product role decision audit failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(
        database,
        "append_durable_audit_event_in_transaction",
        fail_decision_audit,
    )
    with pytest.raises(
        RuntimeError,
        match="injected Product role decision audit failure",
    ):
        client.post(
            f"/api/v2/approvals/{approval_id}/decisions",
            json={"decision": "approve"},
            headers={"Idempotency-Key": "audit-rollback-decision"},
        )

    assert database.get_approval(approval_id).status == "pending"
    assert (
        database.list_project_role_bindings(
            project_id=project_id,
            actor_id=target.id,
            active_only=True,
        )
        == []
    )
    assert database.count_durable_audit_events() == event_count
    with database.cursor() as cursor:
        assert (
            cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0]
            == idempotency_count
        )


def test_product_role_v1_compatibility_boundary_and_legacy_write_gate(
    api_client,
):
    client, main_module = api_client
    database = main_module.app_state.db
    project_id, owner, reviewer, target = _seed_ready_project(database)
    database.upsert_project_membership(
        project=project_id,
        actor_id=target.id,
        role=ProjectRole.VIEWER,
    )
    main_module.app_state.config.api_v2_enabled = True
    main_module.app_state.config.product_rbac_v2_enabled = True
    main_module.app_state.config.identity_admin_enabled = True
    main_module.app_state.config.authorization_mode = "enforce"
    _session_for(main_module, client, owner.id)

    legacy_list = client.get(f"/projects/{project_id}/memberships")
    assert legacy_list.status_code == 200
    assert len(legacy_list.json()) == 1
    legacy_item = legacy_list.json()[0]
    assert legacy_item["project"] == "product-rbac"
    assert legacy_item["project_id"] == project_id
    assert legacy_item["actor_id"] == target.id
    assert legacy_item["role"] == "viewer"
    assert legacy_item["actor"]["type"] == "human"
    assert legacy_item["actor"]["display_name"] == "Target"
    assert set(legacy_item) == {
        "project",
        "project_id",
        "actor_id",
        "role",
        "created_by_actor_id",
        "created_at",
        "updated_at",
        "actor",
    }
    assert "roles" not in legacy_list.text
    assert "reviewer" not in legacy_list.text

    blocked_upsert = client.post(
        f"/projects/{project_id}/memberships/request",
        json={"actor_id": target.id, "role": "admin"},
    )
    assert blocked_upsert.status_code == 409
    assert blocked_upsert.json()["error"]["code"] == ("legacy_membership_write_disabled")
    blocked_remove = client.post(f"/projects/{project_id}/memberships/{target.id}/remove-request")
    assert blocked_remove.status_code == 409
    assert blocked_remove.json()["error"]["code"] == ("legacy_membership_write_disabled")

    _session_for(main_module, client, reviewer.id)
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["actor"]["platform_admin"] is False
    assert me.json()["project_memberships"] == []
    reviewer_is_not_legacy_admin = client.get(f"/projects/{project_id}/memberships")
    assert reviewer_is_not_legacy_admin.status_code == 403
