"""Product v2 multi-role read, request, and decision routes."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.authorization import (
    Action,
    AuthorizationReason,
    evaluate_enforced_authorization,
)
from app.db import Approval, Database
from app.identity import RequestContext
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import (
    IdempotencyRequestContext,
    IdempotencyResource,
    idempotency_context_dependency,
)
from dispatch_center.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_CURSOR_BYTES,
    MAX_PAGE_LIMIT,
    build_cursor_page,
    decode_cursor,
    pagination_query_sha256,
)
from dispatch_center.api.schemas import (
    ProjectRoleChangeRequest,
    ProjectRoleDecisionRequest,
)
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork
from dispatch_center.api.routers.project_bootstrap_v2 import (
    handle_project_bootstrap_decision,
)
from dispatch_center.api.routers.project_environments_v1 import (
    handle_environment_change_decision,
)
from dispatch_center.api.routers.run_templates_v2 import (
    handle_run_template_product_change_decision,
)
from dispatch_center.api.routers.dataset_assets_v2 import (
    DATASET_APPROVAL_KINDS,
    handle_dataset_approval_decision,
)
from dispatch_center.api.routers.runs_v2 import (
    handle_execution_plan_v2_decision,
    handle_product_stop_decision,
)
from dispatch_center.api.routers.compatibility_approvals_v2 import (
    handle_compatibility_enqueue_decision,
)
from dispatch_center.api.routers.project_instance_update_v2 import (
    PROJECT_INSTANCE_UPDATE_APPROVAL_KIND,
    handle_project_instance_update_decision,
)


ROLE_LIST_ROUTE = "/api/v2/projects/{project_id}/roles"
ROLE_REQUEST_ROUTE = "/api/v2/projects/{project_id}/role-change-requests"
ROLE_DECISION_ROUTE = "/api/v2/approvals/{approval_id}/decisions"
ROLE_LIST_SORT = "actor_id:asc,role:asc,binding_id:asc"
_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)
router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
    ],
)


def _database(request: Request) -> Database:
    database = getattr(request.app.state, "dispatch_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Product v2 database state is unavailable")
    return database


def _request_context(request: Request) -> RequestContext:
    context = getattr(request.state, "request_context", None)
    if not isinstance(context, RequestContext) or context.actor is None:
        raise APIError(
            code="authentication_required",
            message="Authentication is required",
            status_code=401,
        )
    return context


def _canonical_uuid(value: str, *, field_name: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        ) from None
    if normalized != value:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    return normalized


def _require_project_action(
    request: Request,
    action: Action,
    *,
    project_id: str,
    requester_actor_id: str | None = None,
    high_risk: bool = False,
    hide_unknown_project_membership: bool = False,
) -> RequestContext:
    context = _request_context(request)
    decision = evaluate_enforced_authorization(
        context,
        action,
        project_id=project_id,
        requester_actor_id=requester_actor_id,
        high_risk=high_risk,
    )
    if not decision.allowed:
        if hide_unknown_project_membership and decision.reason in _OPAQUE_PROJECT_DENIALS:
            raise APIError(
                code="not_found",
                message="Resource not found",
                status_code=404,
            )
        raise APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
            details={"reason": decision.reason.value},
        )
    return context


def _role_change_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason == "roles_digest_conflict":
        return APIError(
            code="roles_digest_conflict",
            message="Project roles changed; refresh and retry",
            status_code=409,
        )
    if "not found" in reason or "does not exist" in reason:
        return APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    return APIError(
        code="role_change_conflict",
        message="The role change is not valid for the current project state",
        status_code=409,
        details={"reason": reason},
    )


def _load_role_change_approval(database: Database, approval_id: int) -> tuple[Approval, str]:
    approval = database.get_approval(approval_id)
    if approval is None or approval.kind != "project_role_change":
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    try:
        payload = approval.payload
        if not isinstance(payload, dict):
            raise ValueError
        project_id = _canonical_uuid(
            payload["project_id"],
            field_name="project_id",
        )
    except (KeyError, TypeError, ValueError):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        ) from None
    return approval, project_id


@router.get("/projects/{project_id}/roles")
def list_project_roles(
    project_id: str,
    request: Request,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id, field_name="project_id")
    context = _require_project_action(
        request,
        Action.PROJECT_ROLE_VIEW,
        project_id=project_id,
    )
    database = _database(request)
    try:
        snapshot = database.get_project_role_snapshot(project_id)
    except ValueError as exc:
        raise _role_change_error(exc) from exc
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=ROLE_LIST_ROUTE,
        sort_contract=ROLE_LIST_SORT,
        filters={},
    )
    after: tuple[str, str, str] | None = None
    if cursor is not None:
        decoded = decode_cursor(
            cursor,
            expected_query_sha256=query_sha256,
            key_types=(str, str, str),
        )
        after = cast(tuple[str, str, str], decoded)
    rows = database.list_project_role_bindings_page(
        project_id=project_id,
        after=after,
        limit_plus_one=limit + 1,
    )
    page = build_cursor_page(
        rows,
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda binding: (
            binding.actor_id,
            binding.role.value,
            binding.id,
        ),
    )
    actors = snapshot["actors"]
    items = []
    for binding in page.items:
        actor = actors.get(binding.actor_id)
        items.append(
            {
                "actor_disabled_at": (actor.disabled_at if actor is not None else None),
                "actor_id": binding.actor_id,
                "actor_type": (actor.actor_type.value if actor is not None else None),
                "binding_id": binding.id,
                "grant_approval_id": binding.grant_approval_id,
                "grant_provenance": binding.grant_provenance.value,
                "granted_at": binding.granted_at,
                "role": binding.role.value,
            }
        )
    readiness = snapshot["readiness"]
    return {
        "project_id": project_id,
        "rbac_state": readiness.state.value,
        "readiness_reasons": [reason.value for reason in readiness.reasons],
        "roles_digest": snapshot["roles_digest"],
        "items": items,
        "next_cursor": page.next_cursor,
    }


@router.post(
    "/projects/{project_id}/role-change-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_project_role_change(
    project_id: str,
    body: ProjectRoleChangeRequest,
    request: Request,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id, field_name="project_id")
    context = _require_project_action(
        request,
        Action.PROJECT_ROLE_MANAGE,
        project_id=project_id,
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"project_id": project_id},
        query={},
    )
    database = _database(request)

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_project_role_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                target_actor_id=body.actor_id,
                add_roles=list(body.add_roles),
                remove_roles=list(body.remove_roles),
                expected_roles_digest=body.expected_roles_digest,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _role_change_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval_id = int(outcome.resource.resource_id)
    approval = database.get_approval(approval_id)
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("role change approval disappeared after commit")
    return {
        "approval_id": approval_id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.post(
    "/approvals/{approval_id}/decisions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def decide_project_role_change(
    approval_id: int,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    if approval_id < 1:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    database = _database(request)
    candidate = database.get_approval(approval_id)
    if candidate is None:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    if candidate.kind == "enqueue":
        return await handle_compatibility_enqueue_decision(
            approval=candidate,
            body=body,
            request=request,
            idempotency=idempotency,
        )
    if candidate.kind == "project_bootstrap_v2":
        return handle_project_bootstrap_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    if candidate.kind == "environment_change_v2":
        return handle_environment_change_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    if candidate.kind in {
        "run_template_change_v2",
        "project_defaults_change_v2",
    }:
        return handle_run_template_product_change_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    if candidate.kind in DATASET_APPROVAL_KINDS:
        return handle_dataset_approval_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    if candidate.kind == "execution_plan_v2":
        return handle_execution_plan_v2_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    if candidate.kind == PROJECT_INSTANCE_UPDATE_APPROVAL_KIND:
        return await handle_project_instance_update_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    if (
        candidate.kind == "stop"
        and isinstance(candidate.payload, dict)
        and candidate.payload.get("source") == "product_v2"
    ):
        return handle_product_stop_decision(
            approval=candidate,
            body=body,
            request=request,
            response=response,
            idempotency=idempotency,
        )
    approval, project_id = _load_role_change_approval(database, approval_id)
    context = _require_project_action(
        request,
        Action.APPROVAL_DECIDE,
        project_id=project_id,
        requester_actor_id=approval.requester_actor_id,
        high_risk=True,
        hide_unknown_project_membership=True,
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"approval_id": approval_id},
        query={},
    )

    def decide(_cursor: Any) -> IdempotencyResource:
        try:
            if body.decision == "approve":
                database.apply_project_role_change_decision(
                    approval_id=approval_id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
            else:
                database.reject_project_role_change_decision(
                    approval_id=approval_id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _role_change_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    decided = database.get_approval(approval_id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("role change approval disappeared after decision")
    return {
        "approval_id": approval_id,
        "replayed": outcome.replayed,
        "status": decided.status,
    }


__all__ = [
    "ROLE_DECISION_ROUTE",
    "ROLE_LIST_ROUTE",
    "ROLE_LIST_SORT",
    "ROLE_REQUEST_ROUTE",
    "router",
]
