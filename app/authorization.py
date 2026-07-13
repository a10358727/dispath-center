"""Pure authorization policy for Goal 1 shadow-mode evaluation.

This module describes what an authenticated actor *would* be allowed to do.
It deliberately has no HTTP, database, audit, or side-effect dependencies and
does not enforce its decisions.  Runtime integrations added by later slices
may observe :class:`AuthorizationDecision` values, but Goal 1 shadow mode must
not use them to change responses, collections, mutations, approvals, or remote
execution behavior.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Optional

from app.identity import ActorType, ProjectRole, RequestContext

if TYPE_CHECKING:
    from app.db import Approval, CodingRun, Dataset, Job, Project


class Action(str, Enum):
    """Closed catalog of actions understood by the Goal 1 policy."""

    PROJECT_VIEW = "project.view"
    PROJECT_OPERATE = "project.operate"
    PROJECT_ADMIN = "project.admin"
    PROJECT_MEMBERSHIP_MANAGE = "project.membership.manage"
    APPROVAL_VIEW = "approval.view"
    APPROVAL_DECIDE = "approval.decide"
    PLATFORM_VIEW = "platform.view"
    PLATFORM_MANAGE = "platform.manage"
    AUDIT_VIEW = "audit.view"
    IDENTITY_SELF_VIEW = "identity.self.view"
    IDENTITY_MANAGE = "identity.manage"

    def __str__(self) -> str:
        return self.value


class ResourceScope(str, Enum):
    """The ownership boundary against which an action is evaluated."""

    PROJECT = "project"
    GLOBAL = "global"

    def __str__(self) -> str:
        return self.value


class ResourceKind(str, Enum):
    """Persisted resource kinds that can carry project ownership evidence."""

    PROJECT = "project"
    JOB = "job"
    CODING_RUN = "coding_run"
    DATASET = "dataset"
    APPROVAL = "approval"

    def __str__(self) -> str:
        return self.value


ResourceIdentifier = str | int | tuple[str, str]


@dataclass(frozen=True)
class ResourceReference:
    """Stable kind/identifier pair for a persisted authorization target."""

    kind: ResourceKind
    resource_id: ResourceIdentifier


class ResourceResolutionReason(str, Enum):
    """Stable evidence codes for deterministic resource-scope resolution."""

    RESOLVED_PROJECT = "resolved_project"
    RESOLVED_PROJECTLESS_JOB = "resolved_projectless_job"
    RESOLVED_DATASET_BINDINGS = "resolved_dataset_bindings"
    RESOLVED_ORPHAN_DATASET = "resolved_orphan_dataset"
    RESOLVED_APPROVAL_TARGET = "resolved_approval_target"
    RESOLVED_PLATFORM_APPROVAL = "resolved_platform_approval"
    INVALID_RESOURCE_ID = "invalid_resource_id"
    RESOURCE_NOT_FOUND = "resource_not_found"
    MALFORMED_REFERENCE = "malformed_reference"
    PROJECT_ID_MISSING = "project_id_missing"
    REFERENCED_PROJECT_UNRESOLVED = "referenced_project_unresolved"
    PROJECT_CATALOG_UNAVAILABLE = "project_catalog_unavailable"
    MALFORMED_APPROVAL_PAYLOAD = "malformed_approval_payload"
    UNKNOWN_APPROVAL_KIND = "unknown_approval_kind"
    REFERENCED_JOB_UNRESOLVED = "referenced_job_unresolved"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ResourceResolution:
    """Resolved scope evidence, or an explicit unresolved result.

    ``scope=None`` is intentionally distinct from ``GLOBAL``.  Missing rows,
    stale weak references, malformed payloads, and unavailable project-catalog
    evidence must remain unresolved rather than silently acquiring global
    scope.  A project-scoped dataset may contain multiple project UUIDs; all
    exact bindings are retained in deterministic order.
    """

    resource: ResourceReference
    scope: Optional[ResourceScope]
    reason: ResourceResolutionReason
    project_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized_ids = tuple(sorted(set(self.project_ids)))
        object.__setattr__(self, "project_ids", normalized_ids)
        if self.scope is ResourceScope.PROJECT and not normalized_ids:
            raise ValueError("project scope requires at least one project id")
        if self.scope is not ResourceScope.PROJECT and normalized_ids:
            raise ValueError("only project scope may contain project ids")

    @property
    def resolved(self) -> bool:
        return self.scope is not None

    @property
    def unresolved(self) -> bool:
        return self.scope is None


class AuthorizationReason(str, Enum):
    """Stable reason codes suitable for later structured shadow evidence."""

    ALLOWED_PLATFORM_ADMIN = "allowed_platform_admin"
    ALLOWED_PROJECT_ROLE = "allowed_project_role"
    ALLOWED_AUTHENTICATED_ACTOR = "allowed_authenticated_actor"
    DENIED_ANONYMOUS = "denied_anonymous"
    DENIED_ACTOR_DISABLED = "denied_actor_disabled"
    DENIED_PROJECT_REQUIRED = "denied_project_required"
    DENIED_SERVICE_SCOPE_MISSING = "denied_service_scope_missing"
    DENIED_SERVICE_ACTION = "denied_service_action"
    DENIED_HIGH_RISK_SELF_DECISION = "denied_high_risk_self_decision"
    DENIED_PLATFORM_ADMIN_REQUIRED = "denied_platform_admin_required"
    DENIED_PROJECT_MEMBERSHIP_MISSING = "denied_project_membership_missing"
    DENIED_CROSS_PROJECT = "denied_cross_project"
    DENIED_PROJECT_ROLE_INSUFFICIENT = "denied_project_role_insufficient"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AuthorizationDecision:
    """An observational policy result; it is not an enforcement response."""

    allowed: bool
    action: Action
    scope: ResourceScope
    reason: AuthorizationReason
    actor_id: Optional[str] = None
    project_id: Optional[str] = None
    project_role: Optional[ProjectRole] = None

    @property
    def would_allow(self) -> bool:
        return self.allowed

    @property
    def would_deny(self) -> bool:
        return not self.allowed


_PROJECT_ONLY_ACTIONS = frozenset(
    {
        Action.PROJECT_VIEW,
        Action.PROJECT_OPERATE,
        Action.PROJECT_ADMIN,
        Action.PROJECT_MEMBERSHIP_MANAGE,
    }
)

_PROJECT_OR_GLOBAL_ACTIONS = frozenset(
    {
        Action.APPROVAL_VIEW,
        Action.APPROVAL_DECIDE,
    }
)

_PROJECT_ROLE_REQUIRED = {
    Action.PROJECT_VIEW: ProjectRole.VIEWER,
    Action.PROJECT_OPERATE: ProjectRole.OPERATOR,
    Action.PROJECT_ADMIN: ProjectRole.ADMIN,
    Action.PROJECT_MEMBERSHIP_MANAGE: ProjectRole.ADMIN,
    Action.APPROVAL_VIEW: ProjectRole.ADMIN,
    Action.APPROVAL_DECIDE: ProjectRole.ADMIN,
}

_PROJECT_ROLE_LEVEL = {
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.ADMIN: 3,
}

_SERVICE_PROHIBITED_ACTIONS = frozenset(
    {
        Action.APPROVAL_DECIDE,
        Action.IDENTITY_MANAGE,
    }
)


_PROJECT_ID_APPROVAL_KINDS = frozenset(
    {
        "project_membership_upsert",
        "project_membership_remove",
    }
)

_PROJECT_APPROVAL_KINDS = frozenset(
    {
        "apply_patch",
        "coding_task",
        "git_init",
        "project_deploy",
    }
) | _PROJECT_ID_APPROVAL_KINDS

_PLATFORM_APPROVAL_KINDS = frozenset(
    {
        "inventory_scan",
        "import_project",
        "ignore_project_candidate",
        "ignore_nested_candidates",
        "server_add",
        "server_update",
        "server_disable",
        "server_delete",
        "service_account_create",
        "service_token_issue",
        "service_token_revoke",
    }
)

_MISSING = object()


def resolve_project_resource(
    requested_id: str,
    project: Optional["Project"],
) -> ResourceResolution:
    """Resolve an already-loaded project to its canonical UUID scope."""

    reference = _resource_reference(ResourceKind.PROJECT, requested_id)
    if not _is_nonempty_string(requested_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if project is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if requested_id not in (getattr(project, "name", None), getattr(project, "id", None)):
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)
    project_id = _canonical_project_id(getattr(project, "id", None))
    if project_id is None:
        return _unresolved(reference, ResourceResolutionReason.PROJECT_ID_MISSING)
    return _resolved(
        ResourceReference(ResourceKind.PROJECT, project_id),
        ResourceScope.PROJECT,
        ResourceResolutionReason.RESOLVED_PROJECT,
        (project_id,),
    )


def resolve_job_resource(
    job_id: int,
    job: Optional["Job"],
    project: Optional["Project"] = None,
) -> ResourceResolution:
    """Resolve a job using its persisted project-name evidence.

    Only an existing job whose ``project`` column is exactly ``None`` is known
    to be projectless.  An empty/malformed project value or a deleted project
    remains unresolved.
    """

    reference = _resource_reference(ResourceKind.JOB, job_id)
    if not _is_positive_int(job_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if job is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if getattr(job, "id", None) != job_id:
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)

    project_name = getattr(job, "project", _MISSING)
    if project_name is None:
        return _resolved(
            reference,
            ResourceScope.GLOBAL,
            ResourceResolutionReason.RESOLVED_PROJECTLESS_JOB,
        )
    return _resolve_parent_project(reference, project_name, project)


def resolve_coding_run_resource(
    coding_run_id: int,
    coding_run: Optional["CodingRun"],
    project: Optional["Project"] = None,
) -> ResourceResolution:
    """Resolve a coding run through its required persisted project name."""

    reference = _resource_reference(ResourceKind.CODING_RUN, coding_run_id)
    if not _is_positive_int(coding_run_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if coding_run is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if getattr(coding_run, "id", None) != coding_run_id:
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)
    return _resolve_parent_project(
        reference,
        getattr(coding_run, "project", _MISSING),
        project,
    )


def resolve_dataset_resource(
    name: str,
    version: str,
    dataset: Optional["Dataset"],
    all_projects: Optional[Iterable["Project"]],
) -> ResourceResolution:
    """Resolve a dataset from exact name+version project bindings.

    ``all_projects`` must be the complete current project catalog.  Passing
    ``None`` means that orphan status cannot be proven and therefore yields an
    unresolved result.  An empty iterable is affirmative evidence that the
    existing dataset is orphaned and consequently platform-scoped.
    """

    reference = _resource_reference(ResourceKind.DATASET, (name, version))
    if not _is_nonempty_string(name) or not _is_nonempty_string(version):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if dataset is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if (
        getattr(dataset, "name", None) != name
        or getattr(dataset, "version", None) != version
    ):
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)
    if all_projects is None:
        return _unresolved(
            reference,
            ResourceResolutionReason.PROJECT_CATALOG_UNAVAILABLE,
        )

    project_ids: list[str] = []
    for project in all_projects:
        if (
            getattr(project, "dataset_name", _MISSING) != name
            or getattr(project, "dataset_version", _MISSING) != version
        ):
            continue
        project_id = _canonical_project_id(getattr(project, "id", None))
        if project_id is None:
            return _unresolved(reference, ResourceResolutionReason.PROJECT_ID_MISSING)
        project_ids.append(project_id)

    if not project_ids:
        return _resolved(
            reference,
            ResourceScope.GLOBAL,
            ResourceResolutionReason.RESOLVED_ORPHAN_DATASET,
        )
    return _resolved(
        reference,
        ResourceScope.PROJECT,
        ResourceResolutionReason.RESOLVED_DATASET_BINDINGS,
        tuple(project_ids),
    )


def resolve_approval_resource(
    approval_id: int,
    approval: Optional["Approval"],
    *,
    project: Optional["Project"] = None,
    job: Optional["Job"] = None,
    job_project: Optional["Project"] = None,
) -> ResourceResolution:
    """Resolve an approval from the payload shape produced by its kind.

    Callers supply only the referenced rows they loaded read-only.  The helper
    verifies their identifiers against the immutable approval payload before
    using them.  Unknown kinds, malformed payloads, and stale weak references
    remain unresolved.
    """

    reference = _resource_reference(ResourceKind.APPROVAL, approval_id)
    if not _is_positive_int(approval_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if approval is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if getattr(approval, "id", None) != approval_id:
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)

    kind = getattr(approval, "kind", None)
    payload = getattr(approval, "payload", None)
    if not _is_nonempty_string(kind):
        return _unresolved(reference, ResourceResolutionReason.UNKNOWN_APPROVAL_KIND)
    if not isinstance(payload, dict):
        return _unresolved(
            reference,
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        )

    if kind == "enqueue":
        if "project" not in payload:
            return _unresolved(
                reference,
                ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
            )
        if payload["project"] is None:
            return _resolved(
                reference,
                ResourceScope.GLOBAL,
                ResourceResolutionReason.RESOLVED_APPROVAL_TARGET,
            )
        return _resolve_approval_project(reference, payload["project"], project)

    if kind == "stop":
        job_id = payload.get("job_id")
        if not _is_positive_int(job_id):
            return _unresolved(
                reference,
                ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
            )
        job_resolution = resolve_job_resource(job_id, job, job_project)
        if job_resolution.unresolved:
            return _unresolved(
                reference,
                ResourceResolutionReason.REFERENCED_JOB_UNRESOLVED,
            )
        return _resolved(
            reference,
            job_resolution.scope,
            ResourceResolutionReason.RESOLVED_APPROVAL_TARGET,
            job_resolution.project_ids,
        )

    if kind in _PROJECT_APPROVAL_KINDS:
        if kind in _PROJECT_ID_APPROVAL_KINDS:
            if not _valid_membership_approval_payload(kind, payload):
                return _unresolved(
                    reference,
                    ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
                )
            return _resolve_approval_project(
                reference, payload.get("project_id"), project
            )
        return _resolve_approval_project(reference, payload.get("project"), project)

    if kind in _PLATFORM_APPROVAL_KINDS:
        if not _valid_platform_approval_payload(kind, payload):
            return _unresolved(
                reference,
                ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
            )
        return _resolved(
            reference,
            ResourceScope.GLOBAL,
            ResourceResolutionReason.RESOLVED_PLATFORM_APPROVAL,
        )

    return _unresolved(reference, ResourceResolutionReason.UNKNOWN_APPROVAL_KIND)


def _resource_reference(
    kind: ResourceKind,
    resource_id: object,
) -> ResourceReference:
    """Build a safe reference even when caller input is malformed."""

    if kind is ResourceKind.DATASET:
        if (
            isinstance(resource_id, tuple)
            and len(resource_id) == 2
            and all(isinstance(part, str) for part in resource_id)
        ):
            return ResourceReference(kind, resource_id)
        return ResourceReference(kind, ("<invalid>", "<invalid>"))
    if isinstance(resource_id, bool) or not isinstance(resource_id, (str, int)):
        return ResourceReference(kind, "<invalid>")
    return ResourceReference(kind, resource_id)


def _resolved(
    resource: ResourceReference,
    scope: ResourceScope,
    reason: ResourceResolutionReason,
    project_ids: tuple[str, ...] = (),
) -> ResourceResolution:
    return ResourceResolution(
        resource=resource,
        scope=scope,
        reason=reason,
        project_ids=project_ids,
    )


def _unresolved(
    resource: ResourceReference,
    reason: ResourceResolutionReason,
) -> ResourceResolution:
    return ResourceResolution(resource=resource, scope=None, reason=reason)


def _resolve_parent_project(
    resource: ResourceReference,
    project_reference: object,
    project: Optional["Project"],
) -> ResourceResolution:
    if not _is_nonempty_string(project_reference):
        return _unresolved(resource, ResourceResolutionReason.MALFORMED_REFERENCE)
    if project is None:
        return _unresolved(
            resource,
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        )
    if project_reference not in (
        getattr(project, "name", None),
        getattr(project, "id", None),
    ):
        return _unresolved(
            resource,
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        )
    project_id = _canonical_project_id(getattr(project, "id", None))
    if project_id is None:
        return _unresolved(resource, ResourceResolutionReason.PROJECT_ID_MISSING)
    return _resolved(
        resource,
        ResourceScope.PROJECT,
        ResourceResolutionReason.RESOLVED_PROJECT,
        (project_id,),
    )


def _resolve_approval_project(
    resource: ResourceReference,
    project_reference: object,
    project: Optional["Project"],
) -> ResourceResolution:
    if not _is_nonempty_string(project_reference):
        return _unresolved(
            resource,
            ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
        )
    if project is None or project_reference not in (
        getattr(project, "name", None),
        getattr(project, "id", None),
    ):
        return _unresolved(
            resource,
            ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED,
        )
    project_id = _canonical_project_id(getattr(project, "id", None))
    if project_id is None:
        return _unresolved(resource, ResourceResolutionReason.PROJECT_ID_MISSING)
    return _resolved(
        resource,
        ResourceScope.PROJECT,
        ResourceResolutionReason.RESOLVED_APPROVAL_TARGET,
        (project_id,),
    )


def _valid_platform_approval_payload(kind: str, payload: dict) -> bool:
    if kind == "inventory_scan":
        return _is_nonempty_string(payload.get("server")) and _is_string_list(
            payload.get("project_roots")
        )
    if kind in {"import_project", "ignore_project_candidate"}:
        return _is_nonempty_string(payload.get("candidate_id"))
    if kind == "ignore_nested_candidates":
        candidate_ids = payload.get("candidate_ids")
        items = payload.get("items")
        return (
            _is_string_list(candidate_ids)
            and isinstance(items, list)
            and len(items) == len(candidate_ids)
            and all(
                isinstance(item, dict)
                and item.get("id") == candidate_id
                and _is_nonempty_string(item.get("path"))
                for candidate_id, item in zip(candidate_ids, items)
            )
        )
    if kind == "server_add":
        return _is_nonempty_string(payload.get("name"))
    if kind == "server_update":
        return _is_nonempty_string(payload.get("name")) and isinstance(
            payload.get("updates"), dict
        )
    if kind in {"server_disable", "server_delete"}:
        return _is_nonempty_string(payload.get("name"))
    if kind == "service_account_create":
        return (
            set(payload) == {"actor_id", "name", "description"}
            and _is_canonical_uuid_string(payload.get("actor_id"))
            and _is_nonempty_string(payload.get("name"))
            and (
                payload["description"] is None
                or isinstance(payload["description"], str)
            )
        )
    if kind == "service_token_issue":
        return (
            set(payload)
            == {"service_account_actor_id", "label", "scopes", "expires_at"}
            and _is_canonical_uuid_string(payload.get("service_account_actor_id"))
            and _is_string_list_allow_empty(payload.get("scopes"))
            and all(scope in Action._value2member_map_ for scope in payload["scopes"])
            and _is_nonempty_string(payload.get("expires_at"))
            and (payload["label"] is None or isinstance(payload["label"], str))
        )
    if kind == "service_token_revoke":
        return set(payload) == {"token_id"} and _is_canonical_uuid_string(
            payload.get("token_id")
        )
    return False


def _valid_membership_approval_payload(kind: str, payload: dict) -> bool:
    expected_keys = {"project_id", "actor_id"}
    if kind == "project_membership_upsert":
        expected_keys.add("role")
    if not (
        set(payload) == expected_keys
        and _is_canonical_uuid_string(payload.get("project_id"))
        and _is_canonical_uuid_string(payload.get("actor_id"))
    ):
        return False
    if kind == "project_membership_upsert":
        try:
            ProjectRole(payload.get("role"))
        except (TypeError, ValueError):
            return False
    return kind in {"project_membership_upsert", "project_membership_remove"}


def _is_string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_is_nonempty_string(item) for item in value)
    )


def _is_string_list_allow_empty(value: object) -> bool:
    return isinstance(value, list) and all(
        _is_nonempty_string(item) for item in value
    )


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical_project_id(value: object) -> Optional[str]:
    if not _is_nonempty_string(value):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _is_canonical_uuid_string(value: object) -> bool:
    return _is_nonempty_string(value) and _canonical_project_id(value) == value


def evaluate_authorization(
    context: RequestContext,
    action: Action | str,
    *,
    project_id: Optional[str] = None,
    resource_scope: ResourceScope | str | None = None,
    requester_actor_id: Optional[str] = None,
    high_risk: bool = False,
) -> AuthorizationDecision:
    """Return the permission the supplied context would receive.

    ``requester_actor_id`` is the actor that created an approval.  It is used
    only for the high-risk approval self-decision rule.  With no explicit
    ``resource_scope``, ``project_id`` must be supplied for project-only
    actions and approval actions are project-scoped when it is present and
    global otherwise.  An explicit scope lets resolved projectless resources
    be evaluated globally; global-only actions remain global regardless of a
    supplied scope.

    Service-account authorization is the intersection of its ordinary role or
    platform-admin permission and the exact action strings in its token scope.
    Service accounts can never decide approvals or manage identity, even when
    a token claims those scopes.
    """

    normalized_action = Action(action)
    normalized_resource_scope = (
        ResourceScope(resource_scope) if resource_scope is not None else None
    )
    scope = _scope_for(normalized_action, project_id, normalized_resource_scope)
    scoped_project_id = project_id if scope is ResourceScope.PROJECT else None
    actor_id = context.actor_id

    if context.actor is None:
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_ANONYMOUS,
            actor_id=actor_id,
            project_id=scoped_project_id,
        )

    if context.actor.disabled_at is not None:
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_ACTOR_DISABLED,
            actor_id=actor_id,
            project_id=scoped_project_id,
        )

    if scope is ResourceScope.PROJECT and not project_id:
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_PROJECT_REQUIRED,
            actor_id=actor_id,
        )

    if context.actor_type is ActorType.SERVICE:
        if normalized_action in _SERVICE_PROHIBITED_ACTIONS:
            return _decision(
                False,
                normalized_action,
                scope,
                AuthorizationReason.DENIED_SERVICE_ACTION,
                actor_id=actor_id,
                project_id=scoped_project_id,
            )
        if normalized_action.value not in context.service_scopes:
            return _decision(
                False,
                normalized_action,
                scope,
                AuthorizationReason.DENIED_SERVICE_SCOPE_MISSING,
                actor_id=actor_id,
                project_id=scoped_project_id,
            )

    if (
        normalized_action is Action.APPROVAL_DECIDE
        and high_risk
        and requester_actor_id is not None
        and requester_actor_id == actor_id
    ):
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_HIGH_RISK_SELF_DECISION,
            actor_id=actor_id,
            project_id=scoped_project_id,
        )

    if context.platform_admin:
        return _decision(
            True,
            normalized_action,
            scope,
            AuthorizationReason.ALLOWED_PLATFORM_ADMIN,
            actor_id=actor_id,
            project_id=scoped_project_id,
        )

    if normalized_action is Action.IDENTITY_SELF_VIEW:
        return _decision(
            True,
            normalized_action,
            scope,
            AuthorizationReason.ALLOWED_AUTHENTICATED_ACTOR,
            actor_id=actor_id,
        )

    if scope is ResourceScope.GLOBAL:
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_PLATFORM_ADMIN_REQUIRED,
            actor_id=actor_id,
        )

    membership = _membership_for(context, scoped_project_id)
    if membership is None:
        own_memberships = tuple(
            candidate
            for candidate in context.project_memberships
            if candidate.actor_id == actor_id
        )
        reason = (
            AuthorizationReason.DENIED_CROSS_PROJECT
            if own_memberships
            else AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING
        )
        return _decision(
            False,
            normalized_action,
            scope,
            reason,
            actor_id=actor_id,
            project_id=scoped_project_id,
        )

    required_role = _PROJECT_ROLE_REQUIRED[normalized_action]
    if _PROJECT_ROLE_LEVEL[membership.role] < _PROJECT_ROLE_LEVEL[required_role]:
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_PROJECT_ROLE_INSUFFICIENT,
            actor_id=actor_id,
            project_id=scoped_project_id,
            project_role=membership.role,
        )

    return _decision(
        True,
        normalized_action,
        scope,
        AuthorizationReason.ALLOWED_PROJECT_ROLE,
        actor_id=actor_id,
        project_id=scoped_project_id,
        project_role=membership.role,
    )


def _scope_for(
    action: Action,
    project_id: Optional[str],
    resource_scope: Optional[ResourceScope],
) -> ResourceScope:
    if action in _PROJECT_ONLY_ACTIONS:
        return resource_scope or ResourceScope.PROJECT
    if action in _PROJECT_OR_GLOBAL_ACTIONS:
        if resource_scope is not None:
            return resource_scope
        if project_id:
            return ResourceScope.PROJECT
    return ResourceScope.GLOBAL


def _membership_for(context: RequestContext, project_id: Optional[str]):
    for membership in context.project_memberships:
        if (
            membership.actor_id == context.actor_id
            and membership.project_id == project_id
        ):
            return membership
    return None


def _decision(
    allowed: bool,
    action: Action,
    scope: ResourceScope,
    reason: AuthorizationReason,
    *,
    actor_id: Optional[str],
    project_id: Optional[str] = None,
    project_role: Optional[ProjectRole] = None,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=allowed,
        action=action,
        scope=scope,
        reason=reason,
        actor_id=actor_id,
        project_id=project_id,
        project_role=project_role,
    )
