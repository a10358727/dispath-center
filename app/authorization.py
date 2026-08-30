"""Pure authorization policy for Goal 1 shadow and enforcement evaluation.

This module describes what an authenticated actor *would* be allowed to do.
It deliberately has no HTTP, database, audit, or side-effect dependencies and
does not enforce its decisions.  Runtime integrations added by later slices
may observe :class:`AuthorizationDecision` values.  The compatibility default
remains observational; the separate ``app.authorization_enforce`` adapter is
the only integration allowed to turn a reviewed decision into a fail-closed
response.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

from app.identity import ActorType, ProjectRole, ProjectRoleV2, RequestContext
from app.dataset_assets import (
    parse_dataset_adoption_payload,
    parse_dataset_alias_change_payload,
)
from app.dataset_sharing import (
    DatasetGrantRevokePayload,
    DatasetShareAcceptPayload,
    DatasetShareOfferPayload,
)
from app.dataset_publish import parse_dataset_publish_payload
from app.execution_plan_v2 import parse_execution_plan_v2_approval_payload
from app.experiment_v2 import parse_experiment_v2_approval_payload
from app.execution_contract import canonical_json, utf8_sha256
from app.project_roles import ROLE_CHANGE_CONTRACT_VERSION, normalize_role_change
from app.project_bootstrap import parse_bootstrap_payload
from app.project_environments import parse_environment_change_payload
from app.run_templates import (
    parse_project_defaults_change_payload,
    parse_run_template_change_payload,
)

if TYPE_CHECKING:
    from app.db import AgentSession, Approval, CodingRun, Dataset, EngineeringTask, Job, Project


class Action(str, Enum):
    """Closed catalog of actions understood by the Goal 1 policy."""

    PROJECT_VIEW = "project.view"
    PROJECT_OPERATE = "project.operate"
    PROJECT_ADMIN = "project.admin"
    PROJECT_MEMBERSHIP_MANAGE = "project.membership.manage"
    PROJECT_ROLE_VIEW = "project.roles.view"
    PROJECT_ROLE_MANAGE = "project.roles.manage"
    DATASET_MANAGE = "dataset.manage"
    DATASET_SHARE = "dataset.share"
    DATASET_WITHDRAW = "dataset.withdraw"
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
    ENGINEERING_TASK = "engineering_task"
    AGENT_SESSION = "agent_session"
    DATASET = "dataset"
    APPROVAL = "approval"
    EXECUTION_PLAN = "execution_plan"

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
    REFERENCED_ENGINEERING_TASK_UNRESOLVED = "referenced_engineering_task_unresolved"

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
    DENIED_LEGACY_SHARED_TOKEN = "denied_legacy_shared_token"

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
    project_roles_v2: tuple[ProjectRoleV2, ...] = ()

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
        Action.PROJECT_ROLE_VIEW,
        Action.PROJECT_ROLE_MANAGE,
        Action.DATASET_MANAGE,
        Action.DATASET_SHARE,
        Action.DATASET_WITHDRAW,
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
    Action.PROJECT_ROLE_VIEW: ProjectRole.ADMIN,
    Action.PROJECT_ROLE_MANAGE: ProjectRole.ADMIN,
    Action.DATASET_MANAGE: ProjectRole.ADMIN,
    Action.DATASET_SHARE: ProjectRole.ADMIN,
    Action.DATASET_WITHDRAW: ProjectRole.ADMIN,
    Action.APPROVAL_VIEW: ProjectRole.ADMIN,
    Action.APPROVAL_DECIDE: ProjectRole.ADMIN,
}

_PROJECT_ROLE_LEVEL = {
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.ADMIN: 3,
}

_PROJECT_ROLE_V2_ACTIONS = {
    ProjectRoleV2.OWNER: frozenset(
        {
            Action.PROJECT_VIEW,
            Action.PROJECT_ADMIN,
            Action.PROJECT_MEMBERSHIP_MANAGE,
            Action.PROJECT_ROLE_VIEW,
            Action.PROJECT_ROLE_MANAGE,
            Action.APPROVAL_VIEW,
            Action.APPROVAL_DECIDE,
            Action.DATASET_WITHDRAW,
        }
    ),
    ProjectRoleV2.OPERATOR: frozenset(
        {Action.PROJECT_VIEW, Action.PROJECT_OPERATE}
    ),
    ProjectRoleV2.REVIEWER: frozenset(
        {
            Action.PROJECT_VIEW,
            Action.PROJECT_ROLE_VIEW,
            Action.APPROVAL_VIEW,
            Action.APPROVAL_DECIDE,
        }
    ),
    ProjectRoleV2.DATASET_MANAGER: frozenset(
        {
            Action.PROJECT_VIEW,
            Action.DATASET_MANAGE,
            Action.DATASET_SHARE,
            Action.DATASET_WITHDRAW,
        }
    ),
    ProjectRoleV2.VIEWER: frozenset({Action.PROJECT_VIEW}),
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
        "project_role_change",
        "environment_change_v2",
        "run_template_change_v2",
        "project_defaults_change_v2",
        "dataset_asset_adoption_v2",
        "dataset_alias_change_v2",
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
        "dataset_publish_v2",
        "execution_plan_v2",
        "experiment_create_v2",
        "project_instance_update_v2",
    }
)

_PROJECT_APPROVAL_KINDS = frozenset(
    {
        "apply_patch",
        "coding_task",
        "engineering_task_promote",
        "git_init",
        "project_deploy",
        # DG-UI-UNIFICATION v1 U1 fix: these kinds were missing from any
        # classification set, so `resolve_approval_resource()` fell through to
        # UNKNOWN_APPROVAL_KIND -> unresolved -> the enforce middleware's
        # fail-closed opaque handling, hiding them from every actor (including
        # the owning project's own roles) instead of only widening exposure.
        # Each payload carries a direct "project" reference at creation time
        # (see the matching `request_*_approval()` in app/approvals.py) — same
        # shape as `enqueue`/`apply_patch` above, so the default project_key
        # applies.
        "engineering_task_retry",
        "engineering_task_discard",
        "auto_placement",
        "agent_session_open",
        # These carry "project_name" instead of "project" — see
        # _PROJECT_NAME_KEY_APPROVAL_KINDS below.
        "run_profile_create",
        "run_profile_update",
        "run_profile_archive",
        "dispatch_policy_create",
        "dispatch_policy_update",
        "dispatch_policy_archive",
        "agent_session_checkpoint",
        "plan_run",
    }
) | _PROJECT_ID_APPROVAL_KINDS

#: Kinds in `_PROJECT_APPROVAL_KINDS` whose payload identifies the project
#: through a "project_name" key rather than the default "project" key. Mirrors
#: each kind's `request_*_approval()` payload shape in app/approvals.py.
_PROJECT_NAME_KEY_APPROVAL_KINDS = frozenset(
    {
        "engineering_task_promote",
        "run_profile_create",
        "run_profile_update",
        "run_profile_archive",
        "dispatch_policy_create",
        "dispatch_policy_update",
        "dispatch_policy_archive",
        "agent_session_checkpoint",
        "plan_run",
    }
)

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
        "node_enroll",
        "node_revoke",
        "node_rotate",
        "node_retire",
        "agent_runner_enroll",
        "agent_runner_revoke",
        "service_account_create",
        "service_token_issue",
        "service_token_revoke",
        "project_bootstrap_v2",
        # DG-UI-UNIFICATION v1 U1 fix: system-proposed kinds with no
        # user-facing HTTP request route to mirror. `server_bootstrap`'s
        # payload carries no project reference at all (host/username/key/...);
        # `dataset_prewarm`/`dataset_snapshot_build` reference a dataset, not
        # a project, and their nearest precedent route
        # (`POST /datasets/{name}/{version}/snapshot-request`) is classified
        # PLATFORM_MANAGE/"dataset" in app/authorization_catalog.py — same
        # platform-wide scope as server_add et al. above.
        "server_bootstrap",
        "dataset_prewarm",
        "dataset_snapshot_build",
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


def resolve_agent_session_resource(
    session_id: str,
    session: Optional["AgentSession"],
    project: Optional["Project"] = None,
) -> ResourceResolution:
    """Resolve an AgentSession (V1 or Studio/v3) through its persisted project id.

    DG-AGENT-RUNTIME-V3: every `/api/v2/studio/sessions/{session_id}/...` and
    `/api/v2/agent-sessions/{session_id}/...` interface is classified with the
    `agent_session` resource kind; in `enforce` mode the route is unusable
    (403 `unknown_resource_kind`) unless this resolver maps the session to its
    project, so the project-scoped action can be evaluated."""

    reference = _resource_reference(ResourceKind.AGENT_SESSION, session_id)
    if not _is_nonempty_string(session_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if session is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if getattr(session, "id", None) != session_id:
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)
    requested_project_id = getattr(session, "project_id", None)
    if project is None or requested_project_id != getattr(project, "id", None):
        return _unresolved(
            reference, ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED
        )
    return _resolved(
        reference,
        ResourceScope.PROJECT,
        ResourceResolutionReason.RESOLVED_PROJECT,
        (requested_project_id,),
    )


def resolve_engineering_task_resource(
    task_id: str,
    task: Optional["EngineeringTask"],
    project: Optional["Project"] = None,
) -> ResourceResolution:
    """Resolve an immutable engineering task through its persisted project id."""

    reference = _resource_reference(ResourceKind.ENGINEERING_TASK, task_id)
    if not _is_nonempty_string(task_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if task is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if getattr(task, "id", None) != task_id:
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)
    requested_project_id = getattr(task, "project_id", None)
    if project is None or requested_project_id != getattr(project, "id", None):
        return _unresolved(
            reference, ResourceResolutionReason.REFERENCED_PROJECT_UNRESOLVED
        )
    return _resolved(
        reference,
        ResourceScope.PROJECT,
        ResourceResolutionReason.RESOLVED_PROJECT,
        (requested_project_id,),
    )


def resolve_execution_plan_resource(
    plan_id: str,
    execution_plan: Mapping[str, object] | None,
    project: Optional["Project"] = None,
) -> ResourceResolution:
    """Resolve the canonical Product Run identity through exact Project name."""

    reference = _resource_reference(ResourceKind.EXECUTION_PLAN, plan_id)
    if not _is_nonempty_string(plan_id):
        return _unresolved(reference, ResourceResolutionReason.INVALID_RESOURCE_ID)
    if execution_plan is None:
        return _unresolved(reference, ResourceResolutionReason.RESOURCE_NOT_FOUND)
    if execution_plan.get("id") != plan_id:
        return _unresolved(reference, ResourceResolutionReason.MALFORMED_REFERENCE)
    return _resolve_parent_project(
        reference,
        execution_plan.get("project_name", _MISSING),
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
    engineering_task: Optional["EngineeringTask"] = None,
    engineering_task_project: Optional["Project"] = None,
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

    if kind == "engineering_command":
        # DG-UI-UNIFICATION v1 U1 fix: unlike its sibling engineering-task
        # kinds, this payload carries only `engineering_task_id` (no direct
        # "project"/"project_name" field — see
        # `_ENGINEERING_COMMAND_HANDLE_FIELDS` in app/approvals.py), so scope
        # is resolved through the referenced task, reusing
        # `resolve_engineering_task_resource()` the same way `stop` reuses
        # `resolve_job_resource()` above.
        task_id = payload.get("engineering_task_id")
        if not _is_nonempty_string(task_id):
            return _unresolved(
                reference,
                ResourceResolutionReason.MALFORMED_APPROVAL_PAYLOAD,
            )
        task_resolution = resolve_engineering_task_resource(
            task_id, engineering_task, engineering_task_project
        )
        if task_resolution.unresolved:
            return _unresolved(
                reference,
                ResourceResolutionReason.REFERENCED_ENGINEERING_TASK_UNRESOLVED,
            )
        return _resolved(
            reference,
            task_resolution.scope,
            ResourceResolutionReason.RESOLVED_APPROVAL_TARGET,
            task_resolution.project_ids,
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
        project_key = (
            "project_name"
            if kind in _PROJECT_NAME_KEY_APPROVAL_KINDS
            else "project"
        )
        return _resolve_approval_project(
            reference, payload.get(project_key), project
        )

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
    if kind == "project_bootstrap_v2":
        try:
            parse_bootstrap_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
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
    if kind in {"server_add", "server_update", "server_disable", "server_delete"}:
        # Historical rows carry name/updates. New public requests carry an
        # immutable server-config-v1 contract whose exact YAML bytes and
        # digests are verified by the materializer.
        if _is_nonempty_string(payload.get("name")):
            return kind != "server_update" or isinstance(
                payload.get("updates"), dict
            )
        expected_operation = {
            "server_add": "add",
            "server_update": "update",
            "server_disable": "disable",
            "server_delete": "delete",
        }[kind]
        return (
            _is_nonempty_string(payload.get("server_name"))
            and payload.get("operation") == expected_operation
            and isinstance(payload.get("yaml_before_sha256"), str)
            and len(payload["yaml_before_sha256"]) == 64
            and isinstance(payload.get("yaml_after_sha256"), str)
            and len(payload["yaml_after_sha256"]) == 64
            and _is_nonempty_string(payload.get("yaml_after_utf8_b64"))
        )
    if kind == "node_enroll":
        return set(payload) == {"server"} and _is_nonempty_string(
            payload.get("server")
        )
    if kind == "agent_runner_enroll":
        return set(payload) == {"server"} and _is_nonempty_string(payload.get("server"))
    if kind == "agent_runner_revoke":
        return (
            set(payload) == {"runner_id", "server"}
            and _is_nonempty_string(payload.get("runner_id"))
            and _is_nonempty_string(payload.get("server"))
        )
    if kind == "node_revoke":
        return (
            set(payload) == {"node_id", "server"}
            and _is_nonempty_string(payload.get("node_id"))
            and _is_nonempty_string(payload.get("server"))
        )
    if kind == "node_rotate":
        return (
            set(payload)
            == {
                "node_id",
                "server",
                "overlap_sec",
                "rotation_mode",
                "pending_ttl_sec",
                "replace_pending_credential_id",
            }
            and _is_nonempty_string(payload.get("node_id"))
            and _is_nonempty_string(payload.get("server"))
            and payload.get("rotation_mode")
            in {"staged_activation", "legacy_overlap"}
            and (
                payload.get("overlap_sec") is None
                or (
                    isinstance(payload.get("overlap_sec"), int)
                    and not isinstance(payload.get("overlap_sec"), bool)
                    and 1 <= payload["overlap_sec"] <= 86400
                )
            )
            and isinstance(payload.get("pending_ttl_sec"), int)
            and not isinstance(payload.get("pending_ttl_sec"), bool)
            and 60 <= payload["pending_ttl_sec"] <= 604800
            and (
                payload.get("replace_pending_credential_id") is None
                or _is_nonempty_string(
                    payload.get("replace_pending_credential_id")
                )
            )
        )
    if kind == "node_retire":
        return (
            set(payload) == {"node_id", "server", "action"}
            and _is_nonempty_string(payload.get("node_id"))
            and _is_nonempty_string(payload.get("server"))
            and payload.get("action")
            in {
                "start_drain",
                "resume_assignment",
                "complete_retirement",
            }
        )
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
    if kind == "server_bootstrap":
        return (
            set(payload)
            == {
                "host",
                "username",
                "port",
                "key",
                "components",
                "gpu",
                "script_version",
                "script_sha256",
            }
            and _is_nonempty_string(payload.get("host"))
            and _is_nonempty_string(payload.get("username"))
            and isinstance(payload.get("port"), int)
            and not isinstance(payload.get("port"), bool)
            and _is_nonempty_string(payload.get("key"))
            and isinstance(payload.get("components"), list)
            and isinstance(payload.get("gpu"), bool)
            and _is_nonempty_string(payload.get("script_version"))
            and isinstance(payload.get("script_sha256"), str)
            and len(payload["script_sha256"]) == 64
        )
    if kind == "dataset_prewarm":
        return (
            set(payload)
            == {"server", "dataset", "version", "size_bytes", "cached_on_count"}
            and _is_nonempty_string(payload.get("server"))
            and _is_nonempty_string(payload.get("dataset"))
            and _is_nonempty_string(payload.get("version"))
            and isinstance(payload.get("size_bytes"), int)
            and not isinstance(payload.get("size_bytes"), bool)
            and isinstance(payload.get("cached_on_count"), int)
            and not isinstance(payload.get("cached_on_count"), bool)
        )
    if kind == "dataset_snapshot_build":
        digest = payload.get("source_candidate_digest")
        return (
            set(payload)
            == {
                "dataset_name",
                "dataset_version",
                "source_path",
                "source_candidate_digest",
                "store_revision",
                "shard_policy",
                "max_bytes",
            }
            and _is_nonempty_string(payload.get("dataset_name"))
            and _is_nonempty_string(payload.get("dataset_version"))
            and _is_nonempty_string(payload.get("source_path"))
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)
            and _is_nonempty_string(payload.get("store_revision"))
            and isinstance(payload.get("shard_policy"), dict)
            and isinstance(payload.get("max_bytes"), int)
            and not isinstance(payload.get("max_bytes"), bool)
        )
    return False


def _valid_membership_approval_payload(kind: str, payload: dict) -> bool:
    sharing_payload_type = {
        "dataset_share_offer_v2": DatasetShareOfferPayload,
        "dataset_share_accept_v2": DatasetShareAcceptPayload,
        "dataset_grant_revoke_v2": DatasetGrantRevokePayload,
    }.get(kind)
    if sharing_payload_type is not None:
        try:
            sharing_payload_type.model_validate(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "dataset_asset_adoption_v2":
        try:
            parse_dataset_adoption_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "dataset_alias_change_v2":
        try:
            parse_dataset_alias_change_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "dataset_publish_v2":
        try:
            parse_dataset_publish_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "execution_plan_v2":
        try:
            parse_execution_plan_v2_approval_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "experiment_create_v2":
        try:
            parse_experiment_v2_approval_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "project_instance_update_v2":
        required = {
            "project_id", "project_version_id", "instance_id", "server_config_revision_id",
            "target_identity_sha256", "server_name", "git_commit", "hub_ref",
            "promotion_approval_id", "promotion_bundle_sha256", "expected_before_commit",
            "expected_before_branch", "path_sha256", "checkout_before_digest", "preview_digest",
        }
        if set(payload) != required or not all(
            _is_canonical_uuid_string(payload.get(key))
            for key in ("project_id", "project_version_id", "server_config_revision_id")
        ):
            return False
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not (
            re.fullmatch(r"[0-9a-f]{16}", instance_id)
            or _is_canonical_uuid_string(instance_id)
        ):
            return False
        preview_input = {key: value for key, value in payload.items() if key != "preview_digest"}
        return (
            isinstance(payload.get("hub_ref"), str)
            and payload["hub_ref"] == f"refs/heads/codex-promoted/{payload['project_version_id']}"
            and isinstance(payload.get("promotion_approval_id"), int)
            and not isinstance(payload.get("promotion_approval_id"), bool)
            and payload["promotion_approval_id"] > 0
            and isinstance(payload.get("server_name"), str)
            and bool(payload["server_name"])
            and (
                payload.get("expected_before_branch") is None
                or isinstance(payload.get("expected_before_branch"), str)
            )
            and all(
                isinstance(payload.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", payload[key])
                for key in ("target_identity_sha256", "promotion_bundle_sha256", "path_sha256", "checkout_before_digest", "preview_digest")
            )
            and all(
                isinstance(payload.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", payload[key])
                for key in ("git_commit", "expected_before_commit")
            )
            and payload["preview_digest"] == utf8_sha256(canonical_json(preview_input))
        )
    if kind == "environment_change_v2":
        try:
            parse_environment_change_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "run_template_change_v2":
        try:
            parse_run_template_change_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "project_defaults_change_v2":
        try:
            parse_project_defaults_change_payload(payload)
        except (TypeError, ValueError):
            return False
        return True
    if kind == "project_role_change":
        if set(payload) != {
            "add_roles",
            "contract_version",
            "expected_roles_digest",
            "project_id",
            "remove_roles",
            "target_actor_id",
        }:
            return False
        if (
            payload.get("contract_version") != ROLE_CHANGE_CONTRACT_VERSION
            or not _is_canonical_uuid_string(payload.get("project_id"))
            or not _is_canonical_uuid_string(payload.get("target_actor_id"))
        ):
            return False
        try:
            normalize_role_change(
                target_actor_id=payload["target_actor_id"],
                add_roles=payload["add_roles"],
                remove_roles=payload["remove_roles"],
                expected_roles_digest=payload["expected_roles_digest"],
            )
        except (TypeError, ValueError):
            return False
        return True
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
    enforce_legacy_shared_token: bool = False,
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
        and not context.allow_high_risk_self_approval
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

    if enforce_legacy_shared_token and context.actor_type is ActorType.LEGACY and normalized_action in {
        Action.PLATFORM_VIEW,
        Action.PLATFORM_MANAGE,
        Action.AUDIT_VIEW,
        Action.IDENTITY_MANAGE,
        Action.APPROVAL_VIEW,
        Action.APPROVAL_DECIDE,
    }:
        return _decision(
            False,
            normalized_action,
            scope,
            AuthorizationReason.DENIED_LEGACY_SHARED_TOKEN,
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

    if context.project_roles_v2_enabled:
        roles_v2 = _roles_v2_for(context, scoped_project_id)
        if not roles_v2:
            own_bindings = tuple(
                binding
                for binding in context.project_role_bindings
                if binding.active and binding.actor_id == actor_id
            )
            reason = (
                AuthorizationReason.DENIED_CROSS_PROJECT
                if own_bindings
                else AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING
            )
            return _decision(
                False,
                normalized_action,
                scope,
                reason,
                actor_id=actor_id,
                project_id=scoped_project_id,
                project_roles_v2=roles_v2,
            )
        if not any(
            normalized_action in _PROJECT_ROLE_V2_ACTIONS[role]
            for role in roles_v2
        ):
            return _decision(
                False,
                normalized_action,
                scope,
                AuthorizationReason.DENIED_PROJECT_ROLE_INSUFFICIENT,
                actor_id=actor_id,
                project_id=scoped_project_id,
                project_roles_v2=roles_v2,
            )
        return _decision(
            True,
            normalized_action,
            scope,
            AuthorizationReason.ALLOWED_PROJECT_ROLE,
            actor_id=actor_id,
            project_id=scoped_project_id,
            project_roles_v2=roles_v2,
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


def evaluate_enforced_authorization(
    context: RequestContext,
    action: Action | str,
    *,
    project_id: Optional[str] = None,
    resource_scope: ResourceScope | str | None = None,
    requester_actor_id: Optional[str] = None,
    high_risk: bool = False,
) -> AuthorizationDecision:
    """Evaluate the policy with legacy shared-token admin disabled.

    The compatibility actor remains platform-admin in ``off`` and ``shadow``
    modes so historical behavior and evidence stay unchanged.  Enforcement is
    the explicit boundary where the shared bearer credential loses implicit
    global administration; operators must use a human session or scoped
    service token instead.
    """

    return evaluate_authorization(
        context,
        action,
        project_id=project_id,
        resource_scope=resource_scope,
        requester_actor_id=requester_actor_id,
        high_risk=high_risk,
        enforce_legacy_shared_token=True,
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


def _roles_v2_for(
    context: RequestContext,
    project_id: Optional[str],
) -> tuple[ProjectRoleV2, ...]:
    roles: set[ProjectRoleV2] = set()
    for binding in context.project_role_bindings:
        if (
            not binding.active
            or binding.actor_id != context.actor_id
            or binding.project_id != project_id
        ):
            continue
        if (
            binding.role in {ProjectRoleV2.OWNER, ProjectRoleV2.REVIEWER}
            and context.actor_type is not ActorType.HUMAN
        ):
            continue
        roles.add(binding.role)
    return tuple(sorted(roles, key=lambda role: role.value))


def _decision(
    allowed: bool,
    action: Action,
    scope: ResourceScope,
    reason: AuthorizationReason,
    *,
    actor_id: Optional[str],
    project_id: Optional[str] = None,
    project_role: Optional[ProjectRole] = None,
    project_roles_v2: tuple[ProjectRoleV2, ...] = (),
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=allowed,
        action=action,
        scope=scope,
        reason=reason,
        actor_id=actor_id,
        project_id=project_id,
        project_role=project_role,
        project_roles_v2=project_roles_v2,
    )
