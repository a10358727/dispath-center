"""Table-driven tests for the pure Goal 1 authorization policy."""

from itertools import product

import pytest

from app.authorization import (
    Action,
    AuthorizationReason,
    ResourceKind,
    ResourceReference,
    ResourceResolution,
    ResourceResolutionReason,
    ResourceScope,
    evaluate_authorization,
    resolve_approval_resource,
    resolve_coding_run_resource,
    resolve_dataset_resource,
    resolve_job_resource,
    resolve_project_resource,
)
from app.db import VALID_APPROVAL_KINDS, Approval, CodingRun, Dataset, Job, Project
from app.identity import Actor, ActorType, ProjectMembership, ProjectRole, RequestContext


PROJECT_ID = "project-a"
PROJECT_UUID_A = "11111111-1111-4111-8111-111111111111"
PROJECT_UUID_B = "22222222-2222-4222-8222-222222222222"
SERVICE_ACTOR_UUID = "33333333-3333-4333-8333-333333333333"
MEMBER_ACTOR_UUID = "44444444-4444-4444-8444-444444444444"
SERVICE_TOKEN_UUID = "55555555-5555-4555-8555-555555555555"


def _context(
    *,
    actor_id="actor-1",
    actor_type=ActorType.HUMAN,
    role=None,
    project_id=PROJECT_ID,
    platform_admin=False,
    service_scopes=(),
    disabled_at=None,
):
    actor = Actor(
        id=actor_id,
        actor_type=actor_type,
        display_name=actor_id,
        platform_admin=platform_admin,
        disabled_at=disabled_at,
    )
    memberships = ()
    if role is not None:
        memberships = (
            ProjectMembership(
                project_id=project_id,
                actor_id=actor_id,
                role=role,
            ),
        )
    return RequestContext(
        actor=actor,
        authentication_method="service_token"
        if actor_type is ActorType.SERVICE
        else "session",
        service_scopes=service_scopes,
        project_memberships=memberships,
    )


def test_action_catalog_has_exact_goal_1_values():
    assert {action.value for action in Action} == {
        "project.view",
        "project.operate",
        "project.admin",
        "project.membership.manage",
        "approval.view",
        "approval.decide",
        "platform.view",
        "platform.manage",
        "audit.view",
        "identity.self.view",
        "identity.manage",
    }


PROJECT_ACTION_MATRIX = {
    ProjectRole.VIEWER: {Action.PROJECT_VIEW},
    ProjectRole.OPERATOR: {Action.PROJECT_VIEW, Action.PROJECT_OPERATE},
    ProjectRole.ADMIN: {
        Action.PROJECT_VIEW,
        Action.PROJECT_OPERATE,
        Action.PROJECT_ADMIN,
        Action.PROJECT_MEMBERSHIP_MANAGE,
        Action.APPROVAL_VIEW,
        Action.APPROVAL_DECIDE,
    },
}


@pytest.mark.parametrize(
    ("role", "action"),
    list(
        product(
            ProjectRole,
            (
                Action.PROJECT_VIEW,
                Action.PROJECT_OPERATE,
                Action.PROJECT_ADMIN,
                Action.PROJECT_MEMBERSHIP_MANAGE,
                Action.APPROVAL_VIEW,
                Action.APPROVAL_DECIDE,
            ),
        ),
    ),
)
def test_project_role_matrix(role, action):
    decision = evaluate_authorization(
        _context(role=role),
        action,
        project_id=PROJECT_ID,
    )

    expected = action in PROJECT_ACTION_MATRIX[role]
    assert decision.allowed is expected
    assert decision.would_allow is expected
    assert decision.would_deny is not expected
    assert decision.scope is ResourceScope.PROJECT
    assert decision.project_id == PROJECT_ID
    assert decision.project_role is role
    assert decision.reason is (
        AuthorizationReason.ALLOWED_PROJECT_ROLE
        if expected
        else AuthorizationReason.DENIED_PROJECT_ROLE_INSUFFICIENT
    )


@pytest.mark.parametrize("action", list(Action))
def test_platform_admin_allows_every_valid_action(action):
    project_id = PROJECT_ID if action.value.startswith("project.") else None
    decision = evaluate_authorization(
        _context(platform_admin=True),
        action,
        project_id=project_id,
    )

    assert decision.allowed is True
    assert decision.reason is AuthorizationReason.ALLOWED_PLATFORM_ADMIN


@pytest.mark.parametrize("action", list(Action))
def test_anonymous_actor_is_denied_for_every_action(action):
    decision = evaluate_authorization(
        RequestContext(),
        action,
        project_id=PROJECT_ID,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_ANONYMOUS
    assert decision.actor_id is None


@pytest.mark.parametrize(
    ("actor_type", "platform_admin", "role", "service_scopes", "action", "project_id"),
    [
        (ActorType.HUMAN, True, None, (), Action.PLATFORM_MANAGE, None),
        (ActorType.HUMAN, False, ProjectRole.ADMIN, (), Action.PROJECT_VIEW, PROJECT_ID),
        (
            ActorType.SERVICE,
            True,
            None,
            {Action.PLATFORM_VIEW.value},
            Action.PLATFORM_VIEW,
            None,
        ),
    ],
)
def test_disabled_actor_is_denied_before_admin_role_or_service_scope_allows(
    actor_type,
    platform_admin,
    role,
    service_scopes,
    action,
    project_id,
):
    decision = evaluate_authorization(
        _context(
            actor_type=actor_type,
            platform_admin=platform_admin,
            role=role,
            service_scopes=service_scopes,
            disabled_at="2026-07-12T00:00:00+00:00",
        ),
        action,
        project_id=project_id,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_ACTOR_DISABLED


@pytest.mark.parametrize(
    "action",
    [
        Action.PLATFORM_VIEW,
        Action.PLATFORM_MANAGE,
        Action.AUDIT_VIEW,
        Action.IDENTITY_MANAGE,
        Action.APPROVAL_VIEW,
        Action.APPROVAL_DECIDE,
    ],
)
def test_non_platform_admin_cannot_use_global_actions(action):
    decision = evaluate_authorization(_context(role=ProjectRole.ADMIN), action)

    assert decision.would_deny is True
    assert decision.scope is ResourceScope.GLOBAL
    assert decision.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED


def test_any_authenticated_actor_can_view_its_own_identity():
    decision = evaluate_authorization(
        _context(role=ProjectRole.VIEWER),
        Action.IDENTITY_SELF_VIEW,
    )

    assert decision.allowed is True
    assert decision.scope is ResourceScope.GLOBAL
    assert decision.reason is AuthorizationReason.ALLOWED_AUTHENTICATED_ACTOR


def test_project_action_requires_a_project_identifier():
    decision = evaluate_authorization(
        _context(role=ProjectRole.ADMIN),
        Action.PROJECT_VIEW,
    )

    assert decision.would_deny is True
    assert decision.scope is ResourceScope.PROJECT
    assert decision.reason is AuthorizationReason.DENIED_PROJECT_REQUIRED


def test_explicit_global_scope_evaluates_project_action_as_global_resource():
    platform_admin = evaluate_authorization(
        _context(platform_admin=True),
        Action.PROJECT_VIEW,
        resource_scope=ResourceScope.GLOBAL,
    )
    project_admin = evaluate_authorization(
        _context(role=ProjectRole.ADMIN),
        Action.PROJECT_VIEW,
        resource_scope="global",
    )

    assert platform_admin.allowed is True
    assert platform_admin.scope is ResourceScope.GLOBAL
    assert platform_admin.project_id is None
    assert platform_admin.reason is AuthorizationReason.ALLOWED_PLATFORM_ADMIN
    assert project_admin.would_deny is True
    assert project_admin.scope is ResourceScope.GLOBAL
    assert project_admin.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED


@pytest.mark.parametrize(
    ("service_scopes", "allowed", "reason"),
    [
        ((), False, AuthorizationReason.DENIED_SERVICE_SCOPE_MISSING),
        (
            (Action.PROJECT_VIEW.value,),
            True,
            AuthorizationReason.ALLOWED_PLATFORM_ADMIN,
        ),
    ],
)
def test_global_project_action_still_requires_exact_service_scope(
    service_scopes,
    allowed,
    reason,
):
    decision = evaluate_authorization(
        _context(
            actor_type=ActorType.SERVICE,
            platform_admin=True,
            service_scopes=service_scopes,
        ),
        Action.PROJECT_VIEW,
        resource_scope=ResourceScope.GLOBAL,
    )

    assert decision.allowed is allowed
    assert decision.scope is ResourceScope.GLOBAL
    assert decision.reason is reason


def test_exact_service_scope_does_not_replace_global_platform_admin_requirement():
    decision = evaluate_authorization(
        _context(
            actor_type=ActorType.SERVICE,
            role=ProjectRole.ADMIN,
            service_scopes=(Action.PROJECT_VIEW.value,),
        ),
        Action.PROJECT_VIEW,
        resource_scope=ResourceScope.GLOBAL,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED


def test_explicit_project_scope_retains_project_identifier_requirement():
    decision = evaluate_authorization(
        _context(role=ProjectRole.ADMIN),
        Action.PROJECT_VIEW,
        resource_scope=ResourceScope.PROJECT,
    )

    assert decision.would_deny is True
    assert decision.scope is ResourceScope.PROJECT
    assert decision.reason is AuthorizationReason.DENIED_PROJECT_REQUIRED


def test_global_only_action_ignores_supplied_project_scope_and_identifier():
    decision = evaluate_authorization(
        _context(role=ProjectRole.ADMIN),
        Action.PLATFORM_VIEW,
        project_id=PROJECT_ID,
        resource_scope=ResourceScope.PROJECT,
    )

    assert decision.would_deny is True
    assert decision.scope is ResourceScope.GLOBAL
    assert decision.project_id is None
    assert decision.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED


def test_approval_action_follows_explicit_scope_and_project_identifier():
    context = _context(role=ProjectRole.ADMIN)

    global_decision = evaluate_authorization(
        context,
        Action.APPROVAL_VIEW,
        project_id=PROJECT_ID,
        resource_scope=ResourceScope.GLOBAL,
    )
    missing_project_decision = evaluate_authorization(
        context,
        Action.APPROVAL_VIEW,
        resource_scope=ResourceScope.PROJECT,
    )
    project_decision = evaluate_authorization(
        context,
        Action.APPROVAL_VIEW,
        project_id=PROJECT_ID,
        resource_scope=ResourceScope.PROJECT,
    )

    assert global_decision.would_deny is True
    assert global_decision.scope is ResourceScope.GLOBAL
    assert global_decision.project_id is None
    assert global_decision.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED
    assert missing_project_decision.would_deny is True
    assert missing_project_decision.reason is AuthorizationReason.DENIED_PROJECT_REQUIRED
    assert project_decision.allowed is True
    assert project_decision.scope is ResourceScope.PROJECT
    assert project_decision.project_id == PROJECT_ID


@pytest.mark.parametrize(
    ("action", "project_id"),
    [
        (Action.PROJECT_VIEW, PROJECT_ID),
        (Action.APPROVAL_VIEW, PROJECT_ID),
        (Action.APPROVAL_VIEW, None),
        (Action.PLATFORM_VIEW, None),
    ],
)
def test_omitted_resource_scope_preserves_default_inference(action, project_id):
    context = _context(role=ProjectRole.ADMIN)

    implicit = evaluate_authorization(context, action, project_id=project_id)
    explicit_none = evaluate_authorization(
        context,
        action,
        project_id=project_id,
        resource_scope=None,
    )

    assert implicit == explicit_none


def test_invalid_explicit_resource_scope_is_rejected():
    with pytest.raises(ValueError):
        evaluate_authorization(
            _context(platform_admin=True),
            Action.PROJECT_VIEW,
            resource_scope="tenant",
        )


def test_actor_with_no_membership_is_denied_project_access():
    decision = evaluate_authorization(
        _context(),
        Action.PROJECT_VIEW,
        project_id=PROJECT_ID,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING


def test_cross_project_membership_does_not_authorize_target_project():
    decision = evaluate_authorization(
        _context(role=ProjectRole.ADMIN, project_id="project-b"),
        Action.APPROVAL_DECIDE,
        project_id=PROJECT_ID,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_CROSS_PROJECT
    assert decision.project_id == PROJECT_ID


def test_membership_for_a_different_actor_is_not_accepted():
    context = _context()
    foreign_membership = ProjectMembership(
        project_id=PROJECT_ID,
        actor_id="someone-else",
        role=ProjectRole.ADMIN,
    )
    context = RequestContext(
        actor=context.actor,
        authentication_method=context.authentication_method,
        project_memberships=(foreign_membership,),
    )

    decision = evaluate_authorization(
        context,
        Action.PROJECT_VIEW,
        project_id=PROJECT_ID,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING


def test_high_risk_approval_cannot_be_decided_by_its_requester():
    context = _context(role=ProjectRole.ADMIN)
    decision = evaluate_authorization(
        context,
        Action.APPROVAL_DECIDE,
        project_id=PROJECT_ID,
        requester_actor_id=context.actor_id,
        high_risk=True,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_HIGH_RISK_SELF_DECISION


@pytest.mark.parametrize(
    ("requester_actor_id", "high_risk"),
    [("different-actor", True), ("actor-1", False)],
)
def test_non_self_or_non_high_risk_approval_decision_uses_normal_role_policy(
    requester_actor_id,
    high_risk,
):
    decision = evaluate_authorization(
        _context(role=ProjectRole.ADMIN),
        Action.APPROVAL_DECIDE,
        project_id=PROJECT_ID,
        requester_actor_id=requester_actor_id,
        high_risk=high_risk,
    )

    assert decision.allowed is True
    assert decision.reason is AuthorizationReason.ALLOWED_PROJECT_ROLE


def test_high_risk_self_decision_rule_also_applies_to_platform_admin():
    context = _context(platform_admin=True)
    decision = evaluate_authorization(
        context,
        Action.APPROVAL_DECIDE,
        requester_actor_id=context.actor_id,
        high_risk=True,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_HIGH_RISK_SELF_DECISION


def test_service_actor_needs_both_project_role_and_exact_token_scope():
    context = _context(
        actor_type=ActorType.SERVICE,
        role=ProjectRole.OPERATOR,
        service_scopes={Action.PROJECT_VIEW.value},
    )

    view_decision = evaluate_authorization(
        context,
        Action.PROJECT_VIEW,
        project_id=PROJECT_ID,
    )
    operate_decision = evaluate_authorization(
        context,
        Action.PROJECT_OPERATE,
        project_id=PROJECT_ID,
    )

    assert view_decision.allowed is True
    assert view_decision.reason is AuthorizationReason.ALLOWED_PROJECT_ROLE
    assert operate_decision.would_deny is True
    assert operate_decision.reason is AuthorizationReason.DENIED_SERVICE_SCOPE_MISSING


@pytest.mark.parametrize(
    "action",
    [Action.APPROVAL_DECIDE, Action.IDENTITY_MANAGE],
)
def test_service_actor_is_always_denied_prohibited_actions(action):
    context = _context(
        actor_type=ActorType.SERVICE,
        role=ProjectRole.ADMIN,
        platform_admin=True,
        service_scopes={action.value},
    )
    project_id = PROJECT_ID if action is Action.APPROVAL_DECIDE else None

    decision = evaluate_authorization(context, action, project_id=project_id)

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_SERVICE_ACTION


def test_service_platform_admin_still_requires_token_scope():
    decision = evaluate_authorization(
        _context(actor_type=ActorType.SERVICE, platform_admin=True),
        Action.PLATFORM_VIEW,
    )

    assert decision.would_deny is True
    assert decision.reason is AuthorizationReason.DENIED_SERVICE_SCOPE_MISSING


def test_action_string_is_normalized_to_closed_enum():
    decision = evaluate_authorization(
        _context(role=ProjectRole.VIEWER),
        "project.view",
        project_id=PROJECT_ID,
    )

    assert decision.allowed is True
    assert decision.action is Action.PROJECT_VIEW

    with pytest.raises(ValueError):
        evaluate_authorization(_context(), "project.unknown", project_id=PROJECT_ID)


def _project(
    name="alpha",
    project_id=PROJECT_UUID_A,
    *,
    dataset_name=None,
    dataset_version=None,
):
    return Project(
        name=name,
        repo_or_path=f"/projects/{name}",
        id=project_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
    )


def _job(job_id=1, project="alpha"):
    return Job(
        id=job_id,
        type="adhoc",
        project=project,
        command="echo ok",
        require_tag=None,
        pin_server=None,
    )


def _coding_run(coding_run_id=1, project="alpha"):
    return CodingRun(
        id=coding_run_id,
        approval_id=10,
        project=project,
        runner_server="runner",
        instruction="change code",
    )


def _dataset(name="images", version="v1"):
    return Dataset(
        name=name,
        version=version,
        size_bytes=10,
        source_path="/datasets/images/v1",
        manifest={},
    )


def _approval(kind, payload, approval_id=1):
    return Approval(id=approval_id, kind=kind, payload=payload)


def test_resource_resolution_model_keeps_unresolved_distinct_from_global():
    reference = ResourceReference(ResourceKind.JOB, 1)
    unresolved = ResourceResolution(
        resource=reference,
        scope=None,
        reason=ResourceResolutionReason.RESOURCE_NOT_FOUND,
    )

    assert unresolved.unresolved is True
    assert unresolved.resolved is False
    assert unresolved.scope is None

    with pytest.raises(ValueError, match="requires at least one"):
        ResourceResolution(
            resource=reference,
            scope=ResourceScope.PROJECT,
            reason=ResourceResolutionReason.RESOLVED_PROJECT,
        )
    with pytest.raises(ValueError, match="only project scope"):
        ResourceResolution(
            resource=reference,
            scope=ResourceScope.GLOBAL,
            reason=ResourceResolutionReason.RESOLVED_PROJECTLESS_JOB,
            project_ids=(PROJECT_UUID_A,),
        )


@pytest.mark.parametrize("requested_id", ["alpha", PROJECT_UUID_A])
def test_project_resource_resolves_name_or_uuid_to_canonical_uuid(requested_id):
    resolution = resolve_project_resource(requested_id, _project())

    assert resolution.resolved is True
    assert resolution.scope is ResourceScope.PROJECT
    assert resolution.project_ids == (PROJECT_UUID_A,)
    assert resolution.resource == ResourceReference(ResourceKind.PROJECT, PROJECT_UUID_A)
    assert resolution.reason is ResourceResolutionReason.RESOLVED_PROJECT


@pytest.mark.parametrize(
    ("requested_id", "project", "reason"),
    [
        ("alpha", None, ResourceResolutionReason.RESOURCE_NOT_FOUND),
        ("", None, ResourceResolutionReason.INVALID_RESOURCE_ID),
        ("alpha", _project(name="different"), ResourceResolutionReason.MALFORMED_REFERENCE),
        ("alpha", _project(project_id=None), ResourceResolutionReason.PROJECT_ID_MISSING),
        ("alpha", _project(project_id="not-a-uuid"), ResourceResolutionReason.PROJECT_ID_MISSING),
    ],
)
def test_project_resource_never_fabricates_missing_or_malformed_scope(
    requested_id,
    project,
    reason,
):
    resolution = resolve_project_resource(requested_id, project)

    assert resolution.unresolved is True
    assert resolution.scope is None
    assert resolution.project_ids == ()
    assert resolution.reason is reason


def test_job_resource_distinguishes_project_scope_from_explicit_projectless_scope():
    project_resolution = resolve_job_resource(1, _job(), _project())
    global_resolution = resolve_job_resource(2, _job(2, project=None))

    assert project_resolution.scope is ResourceScope.PROJECT
    assert project_resolution.project_ids == (PROJECT_UUID_A,)
    assert project_resolution.reason is ResourceResolutionReason.RESOLVED_PROJECT
    assert global_resolution.scope is ResourceScope.GLOBAL
    assert global_resolution.project_ids == ()
    assert global_resolution.reason is ResourceResolutionReason.RESOLVED_PROJECTLESS_JOB


@pytest.mark.parametrize(
    ("job_id", "job", "project", "reason"),
    [
        (1, None, None, ResourceResolutionReason.RESOURCE_NOT_FOUND),
        (0, None, None, ResourceResolutionReason.INVALID_RESOURCE_ID),
        (1, _job(2), _project(), ResourceResolutionReason.MALFORMED_REFERENCE),
        (1, _job(project=""), None, ResourceResolutionReason.MALFORMED_REFERENCE),
        (1, _job(), None, ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED),
        (
            1,
            _job(),
            _project(name="deleted-replacement"),
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        ),
        (1, _job(), _project(project_id=None), ResourceResolutionReason.PROJECT_ID_MISSING),
    ],
)
def test_job_resource_keeps_missing_or_stale_references_unresolved(
    job_id,
    job,
    project,
    reason,
):
    resolution = resolve_job_resource(job_id, job, project)

    assert resolution.unresolved is True
    assert resolution.reason is reason


def test_coding_run_resolves_only_through_its_existing_project():
    resolution = resolve_coding_run_resource(1, _coding_run(), _project())

    assert resolution.scope is ResourceScope.PROJECT
    assert resolution.project_ids == (PROJECT_UUID_A,)
    assert resolution.resource == ResourceReference(ResourceKind.CODING_RUN, 1)


@pytest.mark.parametrize(
    ("run_id", "run", "project", "reason"),
    [
        (1, None, None, ResourceResolutionReason.RESOURCE_NOT_FOUND),
        (-1, None, None, ResourceResolutionReason.INVALID_RESOURCE_ID),
        (1, _coding_run(2), _project(), ResourceResolutionReason.MALFORMED_REFERENCE),
        (1, _coding_run(project=""), None, ResourceResolutionReason.MALFORMED_REFERENCE),
        (
            1,
            _coding_run(),
            None,
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        ),
    ],
)
def test_coding_run_missing_or_malformed_project_evidence_is_unresolved(
    run_id,
    run,
    project,
    reason,
):
    resolution = resolve_coding_run_resource(run_id, run, project)

    assert resolution.unresolved is True
    assert resolution.reason is reason


def test_dataset_resolution_retains_every_exact_bound_project_uuid():
    projects = [
        _project(
            "zeta",
            PROJECT_UUID_B,
            dataset_name="images",
            dataset_version="v1",
        ),
        _project(
            "alpha",
            PROJECT_UUID_A,
            dataset_name="images",
            dataset_version="v1",
        ),
        _project(
            "wrong-version",
            "33333333-3333-4333-8333-333333333333",
            dataset_name="images",
            dataset_version="v2",
        ),
        _project(
            "wrong-name",
            "44444444-4444-4444-8444-444444444444",
            dataset_name="other",
            dataset_version="v1",
        ),
    ]

    resolution = resolve_dataset_resource("images", "v1", _dataset(), projects)

    assert resolution.scope is ResourceScope.PROJECT
    assert resolution.project_ids == (PROJECT_UUID_A, PROJECT_UUID_B)
    assert resolution.resource == ResourceReference(
        ResourceKind.DATASET,
        ("images", "v1"),
    )
    assert resolution.reason is ResourceResolutionReason.RESOLVED_DATASET_BINDINGS


def test_existing_dataset_with_complete_empty_bindings_is_proven_orphan_global():
    resolution = resolve_dataset_resource("images", "v1", _dataset(), [])

    assert resolution.scope is ResourceScope.GLOBAL
    assert resolution.reason is ResourceResolutionReason.RESOLVED_ORPHAN_DATASET


def test_projectless_job_and_orphan_dataset_are_evaluated_as_global_resources():
    projectless_job = resolve_job_resource(1, _job(project=None))
    orphan_dataset = resolve_dataset_resource("images", "v1", _dataset(), [])
    context = _context(role=ProjectRole.ADMIN)

    job_decision = evaluate_authorization(
        context,
        Action.PROJECT_OPERATE,
        resource_scope=projectless_job.scope,
    )
    dataset_decision = evaluate_authorization(
        context,
        Action.PROJECT_VIEW,
        resource_scope=orphan_dataset.scope,
    )

    assert projectless_job.scope is ResourceScope.GLOBAL
    assert orphan_dataset.scope is ResourceScope.GLOBAL
    assert job_decision.scope is ResourceScope.GLOBAL
    assert dataset_decision.scope is ResourceScope.GLOBAL
    assert job_decision.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED
    assert dataset_decision.reason is AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED


@pytest.mark.parametrize(
    ("name", "version", "dataset", "projects", "reason"),
    [
        ("images", "v1", None, [], ResourceResolutionReason.RESOURCE_NOT_FOUND),
        ("", "v1", None, [], ResourceResolutionReason.INVALID_RESOURCE_ID),
        (
            "images",
            "v1",
            _dataset(version="v2"),
            [],
            ResourceResolutionReason.MALFORMED_REFERENCE,
        ),
        (
            "images",
            "v1",
            _dataset(),
            None,
            ResourceResolutionReason.PROJECT_CATALOG_UNAVAILABLE,
        ),
        (
            "images",
            "v1",
            _dataset(),
            [_project(dataset_name="images", dataset_version="v1", project_id=None)],
            ResourceResolutionReason.PROJECT_ID_MISSING,
        ),
    ],
)
def test_dataset_missing_or_incomplete_evidence_never_becomes_global(
    name,
    version,
    dataset,
    projects,
    reason,
):
    resolution = resolve_dataset_resource(name, version, dataset, projects)

    assert resolution.unresolved is True
    assert resolution.scope is None
    assert resolution.reason is reason


GLOBAL_APPROVAL_PAYLOADS = {
    "inventory_scan": {"server": "gpu-a", "project_roots": ["/work"]},
    "import_project": {"candidate_id": "candidate-1"},
    "ignore_project_candidate": {"candidate_id": "candidate-1"},
    "ignore_nested_candidates": {
        "candidate_ids": ["candidate-1"],
        "items": [{"id": "candidate-1", "path": "/work/nested", "name_guess": None}],
    },
    "server_add": {"name": "gpu-a"},
    "server_update": {"name": "gpu-a", "updates": {"enabled": False}},
    "server_disable": {"name": "gpu-a"},
    "server_delete": {"name": "gpu-a"},
    "service_account_create": {
        "actor_id": SERVICE_ACTOR_UUID,
        "name": "automation",
        "description": "CI",
    },
    "service_token_issue": {
        "service_account_actor_id": SERVICE_ACTOR_UUID,
        "label": "rotation-2",
        "scopes": [],
        "expires_at": "2030-01-01T00:00:00Z",
    },
    "service_token_revoke": {"token_id": SERVICE_TOKEN_UUID},
}


@pytest.mark.parametrize(("kind", "payload"), GLOBAL_APPROVAL_PAYLOADS.items())
def test_platform_approval_kinds_resolve_global_from_valid_payload_evidence(kind, payload):
    resolution = resolve_approval_resource(1, _approval(kind, payload))

    assert resolution.scope is ResourceScope.GLOBAL
    assert resolution.reason is ResourceResolutionReason.RESOLVED_PLATFORM_APPROVAL
    assert resolution.resource == ResourceReference(ResourceKind.APPROVAL, 1)


@pytest.mark.parametrize(
    "kind",
    ["apply_patch", "coding_task", "git_init", "project_deploy"],
)
def test_project_approval_kinds_resolve_exact_persisted_project(kind):
    resolution = resolve_approval_resource(
        1,
        _approval(kind, {"project": "alpha"}),
        project=_project(),
    )

    assert resolution.scope is ResourceScope.PROJECT
    assert resolution.project_ids == (PROJECT_UUID_A,)
    assert resolution.reason is ResourceResolutionReason.RESOLVED_APPROVAL_TARGET


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (
            "project_membership_upsert",
            {
                "project_id": PROJECT_UUID_A,
                "actor_id": MEMBER_ACTOR_UUID,
                "role": "operator",
            },
        ),
        (
            "project_membership_remove",
            {
                "project_id": PROJECT_UUID_A,
                "actor_id": MEMBER_ACTOR_UUID,
            },
        ),
    ],
)
def test_membership_approval_kinds_resolve_exact_persisted_project(kind, payload):
    resolution = resolve_approval_resource(
        1,
        _approval(kind, payload),
        project=_project(),
    )

    assert resolution.scope is ResourceScope.PROJECT
    assert resolution.project_ids == (PROJECT_UUID_A,)
    assert resolution.reason is ResourceResolutionReason.RESOLVED_APPROVAL_TARGET


def test_goal_1_identity_approval_kinds_are_registered():
    assert {
        "service_account_create",
        "service_token_issue",
        "service_token_revoke",
        "project_membership_upsert",
        "project_membership_remove",
    } <= VALID_APPROVAL_KINDS


def test_enqueue_approval_requires_explicit_projectless_or_resolved_project_evidence():
    projectless = resolve_approval_resource(1, _approval("enqueue", {"project": None}))
    project = resolve_approval_resource(
        2,
        _approval("enqueue", {"project": "alpha"}, approval_id=2),
        project=_project(),
    )
    missing_key = resolve_approval_resource(3, _approval("enqueue", {}, approval_id=3))

    assert projectless.scope is ResourceScope.GLOBAL
    assert project.scope is ResourceScope.PROJECT
    assert project.project_ids == (PROJECT_UUID_A,)
    assert missing_key.unresolved is True
    assert missing_key.reason is ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD


def test_stop_approval_inherits_existing_job_project_or_projectless_scope():
    project = resolve_approval_resource(
        1,
        _approval("stop", {"job_id": 10}),
        job=_job(10),
        job_project=_project(),
    )
    projectless = resolve_approval_resource(
        2,
        _approval("stop", {"job_id": 11}, approval_id=2),
        job=_job(11, project=None),
    )

    assert project.scope is ResourceScope.PROJECT
    assert project.project_ids == (PROJECT_UUID_A,)
    assert projectless.scope is ResourceScope.GLOBAL
    assert projectless.project_ids == ()


@pytest.mark.parametrize(
    ("approval", "kwargs", "reason"),
    [
        (None, {}, ResourceResolutionReason.RESOURCE_NOT_FOUND),
        (
            _approval("unknown_kind", {"project": "alpha"}),
            {"project": _project()},
            ResourceResolutionReason.UNKNOWN_APPROVAL_KIND,
        ),
        (
            _approval("apply_patch", {}),
            {"project": _project()},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval("apply_patch", {"project": "deleted"}),
            {"project": None},
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        ),
        (
            _approval("stop", {"job_id": 10}),
            {"job": None},
            ResourceResolutionReason.REFERENCED_JOB_UNRESOLVED,
        ),
        (
            _approval("stop", {"job_id": "10"}),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval("server_update", {"name": "gpu-a"}),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "service_account_create",
                {
                    "actor_id": SERVICE_ACTOR_UUID,
                    "description": "missing name",
                },
            ),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "service_token_issue",
                {
                    "service_account_actor_id": SERVICE_ACTOR_UUID,
                    "scopes": ["project.view"],
                    "expires_at": "2030-01-01T00:00:00Z",
                },
            ),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "service_token_issue",
                {
                    "service_account_actor_id": SERVICE_ACTOR_UUID,
                    "label": None,
                    "scopes": ["unknown.action"],
                    "expires_at": "2030-01-01T00:00:00Z",
                },
            ),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "service_token_revoke",
                {"token_id": ""},
            ),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "service_token_revoke",
                {"token_id": SERVICE_TOKEN_UUID, "raw_token": "must-not-exist"},
            ),
            {},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "project_membership_upsert",
                {
                    "project_id": PROJECT_UUID_A,
                    "actor_id": MEMBER_ACTOR_UUID,
                    "role": "owner",
                },
            ),
            {"project": _project()},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "project_membership_remove",
                {"project_id": PROJECT_UUID_A},
            ),
            {"project": _project()},
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        ),
        (
            _approval(
                "project_membership_remove",
                {
                    "project_id": PROJECT_UUID_B,
                    "actor_id": MEMBER_ACTOR_UUID,
                },
            ),
            {"project": _project()},
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        ),
    ],
)
def test_approval_unknown_malformed_or_stale_evidence_is_unresolved(
    approval,
    kwargs,
    reason,
):
    resolution = resolve_approval_resource(1, approval, **kwargs)

    assert resolution.unresolved is True
    assert resolution.scope is None
    assert resolution.reason is reason


def test_approval_row_identifier_must_match_requested_resource():
    resolution = resolve_approval_resource(
        1,
        _approval("server_disable", {"name": "gpu-a"}, approval_id=2),
    )

    assert resolution.unresolved is True
    assert resolution.reason is ResourceResolutionReason.MALFORMED_REFERENCE
