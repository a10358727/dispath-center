"""Fail-closed authorization adapters for the HTTP compatibility surface.

The policy itself remains pure in :mod:`app.authorization`.  This module only
connects the route catalog and read-only resource resolvers to FastAPI request
state.  It is intentionally separate from the fail-open shadow observer so a
resolver or audit failure cannot silently turn enforcement back into shadow
mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, NoReturn, Optional, TypeVar

from fastapi import Request

from app.authorization import (
    Action,
    AuthorizationDecision,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.authorization_catalog import (
    NODE_ROUTE_INTERFACES,
    PUBLIC_ROUTE_INTERFACES,
    ROUTE_AUTHORIZATION,
)
from app.authorization_shadow import (
    SUPPORTED_RESOURCE_KINDS,
    _GLOBAL_ONLY_ACTIONS,
    resolve_shadow_targets,
)
from app.db import VALID_APPROVAL_KINDS, Database
from app.identity import RequestContext
from dispatch_center.api.errors import APIError


COLLECTION_RESOURCE_KINDS = frozenset(
    {
        "project_collection",
        "job_collection",
        "coding_run_collection",
        "engineering_task_collection",
        "dataset_collection",
        "approval_collection",
    }
)
_OPAQUE_PRODUCT_DECISION_INTERFACE = (
    "POST",
    "/api/v2/approvals/{approval_id}/decisions",
)
_OPAQUE_PRODUCT_APPROVAL_READ_INTERFACE = (
    "GET",
    "/api/v2/approvals/{approval_id}",
)
#: DG-UI-UNIFICATION v1 U1 (docs/DECISIONS.md 2026-08-25): the Product v2
#: approval read/decision interfaces now cover every `VALID_APPROVAL_KINDS`
#: member (compatibility-snapshot mode for the kinds outside the typed
#: contract subset), not just the original transaction-only set. This opaque
#: gate must not lag behind that surface or it would 404 a kind the router
#: already supports whenever `AUTHORIZATION_MODE=enforce`.
_PRODUCT_DECISION_KINDS = VALID_APPROVAL_KINDS
_OPAQUE_PROJECT_READ_INTERFACES = frozenset(
    {
        ("GET", "/api/v2/projects/{project_id}/workspace"),
        ("GET", "/api/v2/projects/{project_id}/environments"),
        ("GET", "/api/v2/projects/{project_id}/run-templates"),
        ("GET", "/api/v2/projects/{project_id}/defaults"),
        ("GET", "/api/v2/projects/{project_id}/datasets"),
        # DG-HARDWARE-EXECUTION v1 P2: the image registry read is project-scoped
        ("GET", "/api/v2/projects/{project_id}/hardware-images"),
        ("POST", "/api/v2/projects/{project_id}/hardware-action-previews"),
        ("POST", "/api/v2/projects/{project_id}/hardware-action-requests"),
        ("GET", "/api/v2/projects/{project_id}/hardware-receipts"),
        ("POST", "/api/v2/projects/{project_id}/dataset-publish-previews"),
        ("POST", "/api/v2/projects/{project_id}/dataset-publish-requests"),
        ("POST", "/api/v2/projects/{project_id}/run-previews"),
        ("POST", "/api/v2/projects/{project_id}/run-requests"),
        ("POST", "/api/v2/projects/{project_id}/instance-update-previews"),
        ("POST", "/api/v2/projects/{project_id}/instance-update-requests"),
        ("GET", "/api/v2/runs/compare"),
        ("GET", "/api/v2/runs/{plan_id}"),
        ("POST", "/api/v2/runs/{plan_id}/clone-previews"),
        ("POST", "/api/v2/runs/{plan_id}/stop-requests"),
        ("GET", "/api/v2/runs/{plan_id}/artifacts"),
        ("GET", "/api/v2/dataset-assets/{asset_id}"),
        ("GET", "/api/v2/dataset-assets/{asset_id}/lineage"),
        ("GET", "/api/v2/dataset-assets/{asset_id}/usage"),
        ("GET", "/api/v2/dataset-assets/{asset_id}/storage"),
        ("POST", "/api/v2/projects/{project_id}/experiment-previews"),
        ("POST", "/api/v2/projects/{project_id}/experiment-requests"),
        ("GET", "/api/v2/experiments"),
        ("GET", "/api/v2/experiments/{experiment_id}"),
    }
)
_OPAQUE_PROJECT_DENIAL_REASONS = frozenset(
    {
        "denied_cross_project",
        "denied_project_membership_missing",
    }
)


@dataclass(frozen=True)
class EnforcementTarget:
    """One already-resolved target used by list filtering or route checks."""

    scope: ResourceScope
    project_id: Optional[str] = None
    requester_actor_id: Optional[str] = None
    high_risk: bool = False


T = TypeVar("T")


def _decision(
    context: RequestContext,
    action: Action,
    target: EnforcementTarget,
) -> AuthorizationDecision:
    return evaluate_enforced_authorization(
        context,
        action,
        project_id=target.project_id,
        resource_scope=target.scope,
        requester_actor_id=target.requester_actor_id,
        high_risk=target.high_risk,
    )


def any_target_allowed(
    context: RequestContext,
    action: Action,
    targets: Iterable[EnforcementTarget],
) -> bool:
    """Return whether at least one resolved ownership binding permits access."""

    return any(_decision(context, action, target).allowed for target in targets)


def filter_project_scoped(
    items: Iterable[T],
    context: RequestContext,
    project_ids_for_item: Callable[[T], Iterable[str]],
    *,
    action: Action = Action.PROJECT_VIEW,
) -> list[T]:
    """Filter a collection by exact project UUID membership in enforce mode.

    Callers pass only durable, already-resolved project IDs.  An item with no
    project binding is not visible through a project-scoped action; global
    platform surfaces must use their explicit platform action instead.
    """

    if context.platform_admin:
        return list(items)
    visible: list[T] = []
    for item in items:
        project_ids = tuple(project_ids_for_item(item))
        if project_ids and any_target_allowed(
            context,
            action,
            (
                EnforcementTarget(ResourceScope.PROJECT, project_id=project_id)
                for project_id in project_ids
            ),
        ):
            visible.append(item)
    return visible


def filter_targets(
    items: Iterable[T],
    context: RequestContext,
    targets_for_item: Callable[[T], Iterable[EnforcementTarget]],
    *,
    action: Action,
) -> list[T]:
    """Filter arbitrary resolved targets, including global/orphan resources."""

    if context.platform_admin:
        return list(items)
    visible: list[T] = []
    for item in items:
        if any_target_allowed(context, action, targets_for_item(item)):
            visible.append(item)
    return visible


async def _request_values(request: Request) -> dict[str, Any]:
    values: dict[str, Any] = dict(request.path_params)
    for key, value in request.query_params.items():
        values.setdefault(key, value)
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - validation owns malformed bodies
            body = None
        if isinstance(body, dict):
            for key, value in body.items():
                values.setdefault(key, value)
    return values


def _interface(request: Request) -> tuple[str, str]:
    route_path = getattr(request.scope.get("route"), "path", None)
    return request.method, route_path if isinstance(route_path, str) else "<unknown>"


def _deny(
    context: RequestContext,
    *,
    code: str,
    message: str,
    status_code: int,
    action: str,
    resource: str,
    reason: str,
) -> NoReturn:
    raise APIError(
        code=code,
        message=message,
        status_code=status_code,
        details={"action": action, "resource": resource, "reason": reason},
    )


def _deny_opaque_not_found() -> NoReturn:
    """Hide whether a Product role approval exists outside the caller's scope."""

    raise APIError(
        code="not_found",
        message="Resource not found",
        status_code=404,
    )


async def enforce_http_authorization(
    request: Request,
    *,
    db: Database,
    context: Optional[RequestContext] = None,
) -> None:
    """Enforce one cataloged HTTP route or raise a client-safe API error."""

    method, route = _interface(request)
    interface = (method, route)
    request_context = context or getattr(request.state, "request_context", None)
    if not isinstance(request_context, RequestContext):
        request_context = RequestContext()

    if interface in PUBLIC_ROUTE_INTERFACES or interface in NODE_ROUTE_INTERFACES:
        # Public browser handshakes and node credentials are authenticated by
        # their dedicated middleware/protocol and are not actor-policy routes.
        return

    spec = ROUTE_AUTHORIZATION.get(interface)
    if spec is None:
        _deny(
            request_context,
            code="authorization_route_unmapped",
            message="This interface is not authorized for the current policy.",
            status_code=403,
            action="unmapped",
            resource="unmapped",
            reason="route_not_cataloged",
        )

    if request_context.actor is None:
        _deny(
            request_context,
            code="authorization_required",
            message="Authentication is required for this interface.",
            status_code=401,
            action=spec.action.value,
            resource=spec.resource_kind,
            reason="denied_anonymous",
        )

    # Collection handlers apply row-level filtering after loading the same
    # durable rows they historically returned.  The dependency only verifies
    # that an authenticated principal may use the collection interface; an
    # empty result is preferable to leaking a cross-project row or revealing
    # whether one exists.
    if spec.resource_kind in COLLECTION_RESOURCE_KINDS:
        return

    values = await _request_values(request)
    if interface in {
        _OPAQUE_PRODUCT_APPROVAL_READ_INTERFACE,
        _OPAQUE_PRODUCT_DECISION_INTERFACE,
    }:
        raw_approval_id = values.get("approval_id")
        try:
            approval_id = (
                int(raw_approval_id)
                if isinstance(raw_approval_id, (str, int))
                and not isinstance(raw_approval_id, bool)
                else None
            )
        except ValueError:
            approval_id = None
        approval = (
            db.get_approval(approval_id)
            if isinstance(approval_id, int)
            and not isinstance(approval_id, bool)
            and approval_id > 0
            else None
        )
        if approval is None or approval.kind not in _PRODUCT_DECISION_KINDS:
            _deny_opaque_not_found()
    targets: tuple[EnforcementTarget, ...]
    if spec.action in _GLOBAL_ONLY_ACTIONS:
        targets = (EnforcementTarget(ResourceScope.GLOBAL),)
        issues: tuple[Any, ...] = ()
    else:
        resolved, issues = resolve_shadow_targets(
            db,
            resource_kind=spec.resource_kind,
            values=values,
        )
        targets = tuple(
            EnforcementTarget(
                target.scope,
                project_id=target.project_id,
                requester_actor_id=target.requester_actor_id,
                high_risk=target.high_risk,
            )
            for target in resolved
        )

    if issues or not targets:
        if (
            interface
            in {
                _OPAQUE_PRODUCT_APPROVAL_READ_INTERFACE,
                _OPAQUE_PRODUCT_DECISION_INTERFACE,
            }
            or interface in _OPAQUE_PROJECT_READ_INTERFACES
        ):
            _deny_opaque_not_found()
        reason = (
            getattr(issues[0], "reason", "resource_unresolved")
            if issues
            else "resource_unresolved"
        )
        _deny(
            request_context,
            code="authorization_resource_unresolved",
            message="The authorization resource could not be resolved.",
            status_code=403,
            action=spec.action.value,
            resource=spec.resource_kind,
            reason=str(reason),
        )

    decisions = tuple(_decision(request_context, spec.action, target) for target in targets)
    if any(decision.allowed for decision in decisions):
        return

    decision = decisions[0]
    if (
        interface == _OPAQUE_PRODUCT_APPROVAL_READ_INTERFACE
        or (
            interface
            in {
                _OPAQUE_PRODUCT_DECISION_INTERFACE,
            }
            or interface in _OPAQUE_PROJECT_READ_INTERFACES
        )
        and decision.reason.value in _OPAQUE_PROJECT_DENIAL_REASONS
    ):
        _deny_opaque_not_found()
    status_code = 401 if decision.reason.value == "denied_anonymous" else 403
    _deny(
        request_context,
        code="authorization_required" if status_code == 401 else "authorization_denied",
        message=(
            "Authentication is required for this interface."
            if status_code == 401
            else "The current principal is not authorized for this resource."
        ),
        status_code=status_code,
        action=spec.action.value,
        resource=spec.resource_kind,
        reason=decision.reason.value,
    )


def ensure_supported_resource_kind(resource_kind: str) -> None:
    """Guard catalog additions from silently bypassing the resolver."""

    if resource_kind not in SUPPORTED_RESOURCE_KINDS:
        raise ValueError(f"unsupported authorization resource kind: {resource_kind}")


def enforce_local_tool_authorization(
    *,
    db: Database,
    context: Optional[RequestContext],
    action: Action | str,
    resource_kind: str,
    values: Mapping[str, Any] | None,
    interface_name: str,
) -> None:
    """Enforce a local agent tool before its handler can cause side effects."""

    request_context = context or RequestContext()
    normalized_action = Action(action)
    if request_context.actor is None:
        _deny(
            request_context,
            code="authorization_required",
            message="Authentication is required for this tool.",
            status_code=401,
            action=normalized_action.value,
            resource=resource_kind,
            reason="denied_anonymous",
        )

    # Tool collection handlers do not yet expose a row-filtering adapter.  A
    # non-admin principal therefore receives an empty-safe denial rather than
    # an unfiltered cross-project result.  Project-specific tools continue
    # through the exact resolver below.
    if resource_kind in COLLECTION_RESOURCE_KINDS and not request_context.platform_admin:
        _deny(
            request_context,
            code="authorization_denied",
            message="The current principal cannot list this tool resource.",
            status_code=403,
            action=normalized_action.value,
            resource=resource_kind,
            reason="collection_filter_required",
        )

    targets: tuple[EnforcementTarget, ...]
    if normalized_action in _GLOBAL_ONLY_ACTIONS:
        targets = (EnforcementTarget(ResourceScope.GLOBAL),)
        issues: tuple[Any, ...] = ()
    else:
        resolved, issues = resolve_shadow_targets(
            db,
            resource_kind=resource_kind,
            values=dict(values or {}),
        )
        targets = tuple(
            EnforcementTarget(
                target.scope,
                project_id=target.project_id,
                requester_actor_id=target.requester_actor_id,
                high_risk=target.high_risk,
            )
            for target in resolved
        )
    if issues or not targets:
        reason = (
            getattr(issues[0], "reason", "resource_unresolved")
            if issues
            else "resource_unresolved"
        )
        _deny(
            request_context,
            code="authorization_resource_unresolved",
            message="The authorization resource could not be resolved.",
            status_code=403,
            action=normalized_action.value,
            resource=resource_kind,
            reason=str(reason),
        )
    decisions = tuple(
        _decision(request_context, normalized_action, target) for target in targets
    )
    if any(decision.allowed for decision in decisions):
        return
    decision = decisions[0]
    status_code = 401 if decision.reason.value == "denied_anonymous" else 403
    _deny(
        request_context,
        code="authorization_required" if status_code == 401 else "authorization_denied",
        message=(
            "Authentication is required for this tool."
            if status_code == 401
            else f"The current principal is not authorized for tool {interface_name}."
        ),
        status_code=status_code,
        action=normalized_action.value,
        resource=resource_kind,
        reason=decision.reason.value,
    )
