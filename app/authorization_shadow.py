"""Fail-open authorization shadow observation for existing interfaces.

This module is deliberately observational.  It performs read-only resource
resolution, calls the pure policy evaluator, and prepares audit evidence.  It
never raises to a request/tool caller and never returns an allow/deny value
that can be used for enforcement.

Evidence is prepared separately from emission so HTTP responses, the audit
collection endpoints, and the ``events`` agent tool can materialize their
existing result before a shadow record is appended.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional

from app.audit import append_audit, audit_actor_from_request_context
from app.authorization import (
    Action,
    ResourceKind,
    ResourceReference,
    ResourceResolution,
    ResourceScope,
    evaluate_authorization,
    resolve_approval_resource,
    resolve_coding_run_resource,
    resolve_dataset_resource,
    resolve_job_resource,
    resolve_project_resource,
)
from app.authorization_catalog import (
    LOCAL_TOOL_AUTHORIZATION,
    LOCAL_TOOL_RESOURCES,
    ROUTE_AUTHORIZATION,
)
from app.db import Approval, Database, VALID_APPROVAL_KINDS
from app.identity import RequestContext

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


SHADOW_MODE = "shadow"

SUPPORTED_RESOURCE_KINDS = frozenset(
    {
        "identity_self",
        "platform",
        "audit",
        "dynamic_agent",
        "agent_catalog",
        "project",
        "project_collection",
        "job",
        "job_request",
        "job_collection",
        "coding_run",
        "coding_run_collection",
        "dataset",
        "dataset_collection",
        "approval",
        "approval_collection",
    }
)

_GLOBAL_ONLY_ACTIONS = frozenset(
    {
        Action.PLATFORM_VIEW,
        Action.PLATFORM_MANAGE,
        Action.AUDIT_VIEW,
        Action.IDENTITY_SELF_VIEW,
        Action.IDENTITY_MANAGE,
    }
)

# Roadmap high-risk classes that already exist as approval kinds.  This set is
# policy evidence only: it must never change approval execution or status.
HIGH_RISK_APPROVAL_KINDS = frozenset(
    VALID_APPROVAL_KINDS.difference({"enqueue", "stop"})
)


@dataclass(frozen=True)
class ShadowTarget:
    """One safely labelled, resolved policy target."""

    resource: str
    scope: ResourceScope
    project_id: Optional[str] = None
    requester_actor_id: Optional[str] = None
    high_risk: bool = False


@dataclass(frozen=True)
class ShadowResolutionIssue:
    """A safe resolution failure without raw request data."""

    resource: str
    reason: str
    project_id: Optional[str] = None


@dataclass(frozen=True)
class ShadowEvidence:
    """A deferred append-only audit event."""

    audit_action: str
    params: dict[str, Any]
    result: str
    request_context: Optional[RequestContext]


async def collect_http_shadow_evidence(
    connection: "HTTPConnection",
    *,
    db: Database,
    config: object,
    context: Optional[RequestContext] = None,
    values: Mapping[str, Any] | None = None,
) -> tuple[ShadowEvidence, ...]:
    """Resolve one cataloged HTTP route and prepare observational evidence.

    Route names come from Starlette's matched route object, never from the raw
    URL.  Path/query/body values are used only as read-only resolver inputs and
    are never included wholesale in audit evidence.  ``values`` lets a caller
    pass already-validated endpoint inputs; otherwise a JSON object body is
    read through Starlette's request cache and merged with path/query values.

    The caller may defer :func:`emit_shadow_evidence` until after its normal
    response is materialized.  That is important for audit/event collection
    interfaces, whose existing response must not include their own shadow
    observation.
    """

    mode = getattr(config, "authorization_mode", "off")
    if mode != SHADOW_MODE:
        # The off-mode fast path intentionally precedes route lookup, body
        # access, database resolution, evaluator calls, and audit work.
        return ()

    request_context = context or _connection_context(connection)
    method, route = _canonical_http_interface(connection)
    canonical_interface = f"{method or '<unknown>'} {route}"
    spec = ROUTE_AUTHORIZATION.get((method, route))
    if spec is None:
        return (
            _error_evidence(
                request_context,
                action="unmapped",
                resource="unmapped",
                interface_kind="route",
                interface_name=canonical_interface,
                stage="catalog",
                reason="route_not_cataloged",
            ),
        )

    try:
        resolver_values = (
            dict(values)
            if values is not None
            else await _extract_http_values(connection)
        )
    except Exception as exc:  # noqa: BLE001 - body observation must fail open
        return (
            _error_evidence(
                request_context,
                action=spec.action.value,
                resource=spec.resource_kind,
                interface_kind="route",
                interface_name=canonical_interface,
                stage="resolution",
                exception_type=type(exc).__name__,
            ),
        )

    return collect_shadow_evidence(
        mode=mode,
        db=db,
        context=request_context,
        action=spec.action,
        resource_kind=spec.resource_kind,
        values=resolver_values,
        interface_kind="route",
        interface_name=canonical_interface,
    )


async def observe_http_authorization(
    connection: "HTTPConnection",
    *,
    db: Database,
    config: object,
    audit_path: str,
    context: Optional[RequestContext] = None,
    values: Mapping[str, Any] | None = None,
) -> tuple[ShadowEvidence, ...]:
    """Collect and immediately append HTTP shadow evidence, always fail open."""

    evidence = await collect_http_shadow_evidence(
        connection,
        db=db,
        config=config,
        context=context,
        values=values,
    )
    emit_shadow_evidence(evidence, audit_path=audit_path)
    return evidence


def collect_local_tool_shadow_evidence(
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    db: Database,
    config: object,
    context: Optional[RequestContext] = None,
) -> tuple[ShadowEvidence, ...]:
    """Prepare evidence for one cataloged in-process agent tool invocation."""

    mode = getattr(config, "authorization_mode", "off")
    if mode != SHADOW_MODE:
        return ()

    action = LOCAL_TOOL_AUTHORIZATION.get(tool_name)
    resource_kind = LOCAL_TOOL_RESOURCES.get(tool_name)
    request_context = context or RequestContext()
    if action is None or resource_kind is None:
        return (
            _error_evidence(
                request_context,
                action="unmapped",
                resource="unmapped",
                interface_kind="tool",
                interface_name=(
                    tool_name if tool_name in LOCAL_TOOL_AUTHORIZATION else "<unmapped>"
                ),
                stage="catalog",
                reason="tool_not_cataloged",
            ),
        )

    return collect_shadow_evidence(
        mode=mode,
        db=db,
        context=request_context,
        action=action,
        resource_kind=resource_kind,
        values=dict(args or {}),
        interface_kind="tool",
        interface_name=tool_name,
    )


def observe_local_tool_authorization(
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    db: Database,
    config: object,
    audit_path: str,
    context: Optional[RequestContext] = None,
) -> tuple[ShadowEvidence, ...]:
    """Collect and immediately append local-tool shadow evidence fail open."""

    evidence = collect_local_tool_shadow_evidence(
        tool_name,
        args,
        db=db,
        config=config,
        context=context,
    )
    emit_shadow_evidence(evidence, audit_path=audit_path)
    return evidence


def collect_shadow_evidence(
    *,
    mode: str,
    db: Database,
    context: Optional[RequestContext],
    action: Action | str,
    resource_kind: str,
    values: Mapping[str, Any] | None,
    interface_kind: str,
    interface_name: str,
) -> tuple[ShadowEvidence, ...]:
    """Prepare would-deny/error evidence and otherwise return an empty tuple.

    Every exception is converted to safe error evidence.  Callers always
    continue to the existing endpoint/tool behavior regardless of the return
    value.  Invalid modes are treated as off here; configuration validation is
    responsible for rejecting them at startup.
    """

    if mode != SHADOW_MODE:
        return ()

    request_context = context or RequestContext()
    try:
        normalized_action = Action(action)
    except Exception as exc:  # noqa: BLE001 - shadow must always fail open
        return (
            _error_evidence(
                request_context,
                action=_safe_action(action),
                resource=resource_kind,
                interface_kind=interface_kind,
                interface_name=interface_name,
                stage="catalog",
                exception_type=type(exc).__name__,
            ),
        )

    if normalized_action in _GLOBAL_ONLY_ACTIONS:
        # Global actions do not need ownership evidence.  In particular, do
        # not make a cosmetic DB lookup whose failure could turn a perfectly
        # resolvable platform/audit/identity observation into a resolver error.
        if resource_kind in SUPPORTED_RESOURCE_KINDS:
            targets: tuple[ShadowTarget, ...] = (
                ShadowTarget(resource_kind, ResourceScope.GLOBAL),
            )
            issues: tuple[ShadowResolutionIssue, ...] = ()
        else:
            targets = ()
            issues = (
                ShadowResolutionIssue(
                    resource="unmapped",
                    reason="unknown_resource_kind",
                ),
            )
    else:
        try:
            targets, issues = resolve_shadow_targets(
                db, resource_kind=resource_kind, values=values or {}
            )
        except Exception as exc:  # noqa: BLE001 - resolver errors are observational
            return (
                _error_evidence(
                    request_context,
                    action=normalized_action.value,
                    resource=resource_kind,
                    interface_kind=interface_kind,
                    interface_name=interface_name,
                    stage="resolution",
                    exception_type=type(exc).__name__,
                ),
            )

    evidence: list[ShadowEvidence] = [
        _error_evidence(
            request_context,
            action=normalized_action.value,
            resource=issue.resource,
            project=issue.project_id,
            interface_kind=interface_kind,
            interface_name=interface_name,
            stage="resolution",
            reason=issue.reason,
        )
        for issue in issues
    ]

    # A resource may have several project bindings (datasets).  Access through
    # any binding is sufficient; emit denials only when every binding denies.
    grouped: dict[str, list[ShadowTarget]] = {}
    for target in targets:
        grouped.setdefault(target.resource, []).append(target)

    for resource, resource_targets in grouped.items():
        decisions = []
        try:
            for target in resource_targets:
                decisions.append(
                    evaluate_authorization(
                        request_context,
                        normalized_action,
                        project_id=target.project_id,
                        resource_scope=target.scope,
                        requester_actor_id=target.requester_actor_id,
                        high_risk=target.high_risk,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - evaluator must fail open
            evidence.append(
                _error_evidence(
                    request_context,
                    action=normalized_action.value,
                    resource=resource,
                    project=resource_targets[0].project_id,
                    interface_kind=interface_kind,
                    interface_name=interface_name,
                    stage="evaluation",
                    exception_type=type(exc).__name__,
                )
            )
            continue

        if any(decision.would_allow for decision in decisions):
            continue
        for target, decision in zip(resource_targets, decisions):
            evidence.append(
                ShadowEvidence(
                    audit_action="authorization_shadow_denied",
                    params=_evidence_params(
                        request_context,
                        action=normalized_action.value,
                        resource=target.resource,
                        project=target.project_id,
                        interface_kind=interface_kind,
                        interface_name=interface_name,
                        reason=decision.reason.value,
                    ),
                    result="would_deny",
                    request_context=request_context,
                )
            )

    return tuple(evidence)


def emit_shadow_evidence(
    evidence: tuple[ShadowEvidence, ...] | list[ShadowEvidence],
    *,
    audit_path: str,
) -> None:
    """Best-effort append deferred evidence without changing caller behavior."""

    for item in evidence:
        try:
            append_audit(
                item.audit_action,
                item.params,
                result=item.result,
                path=audit_path,
                actor=audit_actor_from_request_context(item.request_context),
            )
        except Exception as exc:  # noqa: BLE001 - even audit adapters may be mocked
            try:
                append_audit(
                    "authorization_shadow_error",
                    {
                        "action": item.params.get("action"),
                        "resource": item.params.get("resource"),
                        "project": item.params.get("project"),
                        "route": item.params.get("route"),
                        "tool": item.params.get("tool"),
                        "reason": "audit_append_failed",
                        "stage": "audit",
                        "exception_type": type(exc).__name__,
                        "principal_kind": item.params.get("principal_kind"),
                        "shadow_mode": True,
                    },
                    result="error",
                    path=audit_path,
                    actor=audit_actor_from_request_context(item.request_context),
                )
            except Exception:  # noqa: BLE001 - shadow evidence must never escape
                pass


def resolve_shadow_targets(
    db: Database,
    *,
    resource_kind: str,
    values: Mapping[str, Any],
) -> tuple[tuple[ShadowTarget, ...], tuple[ShadowResolutionIssue, ...]]:
    """Read current rows and resolve one route/tool resource classification."""

    if resource_kind in {
        "identity_self",
        "platform",
        "audit",
        "dynamic_agent",
        "agent_catalog",
    }:
        return ((ShadowTarget(resource_kind, ResourceScope.GLOBAL),), ())

    if resource_kind == "project":
        identifier = _first(values, "name", "project", "project_name")
        project = db.get_project(identifier) if isinstance(identifier, str) else None
        return _from_resolution(resolve_project_resource(identifier, project))

    if resource_kind == "project_collection":
        return _combine(
            resolve_project_resource(project.name, project)
            for project in db.list_projects()
        )

    if resource_kind == "job":
        job_id = _positive_int(values.get("job_id"))
        return _job_targets(db, job_id)

    if resource_kind == "job_request":
        project_ref = values.get("project")
        if project_ref is None:
            return ((ShadowTarget("job_request:projectless", ResourceScope.GLOBAL),), ())
        project = db.get_project(project_ref) if isinstance(project_ref, str) else None
        return _from_resolution(resolve_project_resource(project_ref, project))

    if resource_kind == "job_collection":
        jobs = db.list_jobs(
            status=_optional_string(values.get("status")),
            project=_optional_string(values.get("project")),
        )
        limit = _bounded_int(values.get("limit"), default=len(jobs), low=1, high=20)
        if "limit" in values:
            jobs = jobs[-limit:]
        resolutions = []
        for job in jobs:
            project = (
                db.get_project(job.project)
                if isinstance(job.project, str) and job.project
                else None
            )
            resolutions.append(resolve_job_resource(job.id, job, project))
        return _combine(resolutions)

    if resource_kind == "coding_run":
        run_id = _positive_int(values.get("coding_run_id"))
        run = db.get_coding_run(run_id) if run_id is not None else None
        project = (
            db.get_project(run.project)
            if run is not None and isinstance(run.project, str) and run.project
            else None
        )
        return _from_resolution(resolve_coding_run_resource(run_id, run, project))

    if resource_kind == "coding_run_collection":
        limit = _bounded_int(values.get("limit"), default=50, low=1, high=50)
        runs = db.list_coding_runs(
            status=_optional_string(values.get("status")),
            project=_optional_string(values.get("project")),
            limit=limit,
        )
        resolutions = []
        for run in runs:
            project = db.get_project(run.project) if isinstance(run.project, str) else None
            resolutions.append(resolve_coding_run_resource(run.id, run, project))
        return _combine(resolutions)

    if resource_kind == "dataset":
        name = values.get("name")
        version = values.get("version")
        dataset = (
            db.get_dataset(name, version)
            if isinstance(name, str) and isinstance(version, str)
            else None
        )
        return _from_resolution(
            resolve_dataset_resource(name, version, dataset, db.list_projects())
        )

    if resource_kind == "dataset_collection":
        projects = db.list_projects()
        return _combine(
            resolve_dataset_resource(
                dataset.name,
                dataset.version,
                dataset,
                projects,
            )
            for dataset in db.list_datasets()
        )

    if resource_kind == "approval":
        approval_id = _positive_int(values.get("approval_id"))
        return _approval_targets(db, approval_id)

    if resource_kind == "approval_collection":
        approvals = db.list_approvals(
            status=_optional_string(values.get("status")),
            kind=_optional_string(values.get("kind")),
        )
        return _combine_approval_targets(db, approvals)

    return (
        (),
        (
            ShadowResolutionIssue(
                resource=resource_kind,
                reason="unknown_resource_kind",
            ),
        ),
    )


def _job_targets(
    db: Database, job_id: Optional[int]
) -> tuple[tuple[ShadowTarget, ...], tuple[ShadowResolutionIssue, ...]]:
    job = db.get_job(job_id) if job_id is not None else None
    project = (
        db.get_project(job.project)
        if job is not None and isinstance(job.project, str) and job.project
        else None
    )
    return _from_resolution(resolve_job_resource(job_id, job, project))


def _approval_targets(
    db: Database, approval_id: Optional[int]
) -> tuple[tuple[ShadowTarget, ...], tuple[ShadowResolutionIssue, ...]]:
    approval = db.get_approval(approval_id) if approval_id is not None else None
    project = None
    job = None
    job_project = None
    if approval is not None and isinstance(approval.payload, dict):
        project_key = (
            "project_id"
            if approval.kind
            in {"project_membership_upsert", "project_membership_remove"}
            else "project"
        )
        project_ref = approval.payload.get(project_key)
        if isinstance(project_ref, str):
            project = db.get_project(project_ref)
        job_ref = _positive_int(approval.payload.get("job_id"))
        if job_ref is not None:
            job = db.get_job(job_ref)
            if job is not None and isinstance(job.project, str) and job.project:
                job_project = db.get_project(job.project)
    resolution = resolve_approval_resource(
        approval_id,
        approval,
        project=project,
        job=job,
        job_project=job_project,
    )
    targets, issues = _from_resolution(resolution)
    if approval is None:
        return targets, issues
    return (
        tuple(
            ShadowTarget(
                resource=target.resource,
                scope=target.scope,
                project_id=target.project_id,
                requester_actor_id=approval.requester_actor_id,
                high_risk=approval.kind in HIGH_RISK_APPROVAL_KINDS,
            )
            for target in targets
        ),
        issues,
    )


def _combine_approval_targets(
    db: Database, approvals: list[Approval]
) -> tuple[tuple[ShadowTarget, ...], tuple[ShadowResolutionIssue, ...]]:
    targets: list[ShadowTarget] = []
    issues: list[ShadowResolutionIssue] = []
    for approval in approvals:
        approval_targets, approval_issues = _approval_targets(db, approval.id)
        targets.extend(approval_targets)
        issues.extend(approval_issues)
    return tuple(targets), tuple(issues)


def _combine(
    resolutions,
) -> tuple[tuple[ShadowTarget, ...], tuple[ShadowResolutionIssue, ...]]:
    targets: list[ShadowTarget] = []
    issues: list[ShadowResolutionIssue] = []
    for resolution in resolutions:
        current_targets, current_issues = _from_resolution(resolution)
        targets.extend(current_targets)
        issues.extend(current_issues)
    return tuple(targets), tuple(issues)


def _from_resolution(
    resolution: ResourceResolution,
) -> tuple[tuple[ShadowTarget, ...], tuple[ShadowResolutionIssue, ...]]:
    resource = _resource_label(resolution.resource)
    if resolution.unresolved:
        return (
            (),
            (
                ShadowResolutionIssue(
                    resource=resource,
                    reason=resolution.reason.value,
                ),
            ),
        )
    if resolution.scope is ResourceScope.GLOBAL:
        return ((ShadowTarget(resource, ResourceScope.GLOBAL),), ())
    return (
        tuple(
            ShadowTarget(
                resource=resource,
                scope=ResourceScope.PROJECT,
                project_id=project_id,
            )
            for project_id in resolution.project_ids
        ),
        (),
    )


def _resource_label(reference: ResourceReference) -> str:
    identifier = reference.resource_id
    if reference.kind is ResourceKind.DATASET and isinstance(identifier, tuple):
        return f"dataset:{identifier[0]}@{identifier[1]}"
    return f"{reference.kind.value}:{identifier}"


def _evidence_params(
    context: RequestContext,
    *,
    action: str,
    resource: str,
    project: Optional[str],
    interface_kind: str,
    interface_name: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "resource": resource,
        "project": project,
        "route": interface_name if interface_kind == "route" else None,
        "tool": interface_name if interface_kind == "tool" else None,
        "reason": reason,
        "principal_kind": (
            context.actor_type.value if context.actor_type is not None else "anonymous"
        ),
        "shadow_mode": True,
    }


def _error_evidence(
    context: RequestContext,
    *,
    action: str,
    resource: str,
    interface_kind: str,
    interface_name: str,
    stage: str,
    project: Optional[str] = None,
    reason: str = "shadow_observation_error",
    exception_type: Optional[str] = None,
) -> ShadowEvidence:
    params = _evidence_params(
        context,
        action=action,
        resource=resource,
        project=project,
        interface_kind=interface_kind,
        interface_name=interface_name,
        reason=reason,
    )
    params["stage"] = stage
    if exception_type is not None:
        params["exception_type"] = exception_type
    return ShadowEvidence(
        audit_action="authorization_shadow_error",
        params=params,
        result="error",
        request_context=context,
    )


def _safe_action(action: object) -> str:
    return action.value if isinstance(action, Action) else type(action).__name__


def _connection_context(connection: "HTTPConnection") -> RequestContext:
    state = getattr(connection, "state", None)
    context = getattr(state, "request_context", None)
    return context if isinstance(context, RequestContext) else RequestContext()


def _canonical_http_interface(connection: "HTTPConnection") -> tuple[str, str]:
    scope = getattr(connection, "scope", {})
    method = str(scope.get("method") or "").upper()
    matched_route = scope.get("route")
    route = getattr(matched_route, "path", None)
    if not isinstance(route, str) or not route:
        route = "<unmapped>"
    return method, route


async def _extract_http_values(connection: "HTTPConnection") -> dict[str, Any]:
    values: dict[str, Any] = {}

    path_params = getattr(connection, "path_params", None)
    if isinstance(path_params, Mapping):
        values.update(path_params)

    query_params = getattr(connection, "query_params", None)
    if query_params is not None:
        try:
            values.update(dict(query_params))
        except (TypeError, ValueError):
            # A malformed query adapter is observationally irrelevant; body
            # and path values may still resolve the target safely.
            pass

    method = str(getattr(connection, "scope", {}).get("method") or "").upper()
    json_reader = getattr(connection, "json", None)
    if method not in {"GET", "HEAD"} and callable(json_reader):
        body = await json_reader()
        if isinstance(body, Mapping):
            values.update(body)
    return values


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _optional_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None
