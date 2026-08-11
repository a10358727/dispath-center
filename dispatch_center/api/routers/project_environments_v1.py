"""Product v2 immutable Host Environment read and mutation routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Coroutine, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.authorization import (
    Action,
    AuthorizationReason,
    evaluate_enforced_authorization,
)
from app.db import Approval, Database
from app.identity import RequestContext
from app.project_environments import (
    EnvironmentArchiveRequest,
    EnvironmentChangeRequest,
    EnvironmentCreateRequest,
    EnvironmentUpdateRequest,
    evaluate_environment_readiness,
)
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
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
    project_environments_v1_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


ENVIRONMENT_LIST_ROUTE = "/api/v2/projects/{project_id}/environments"
ENVIRONMENT_REQUEST_ROUTE = (
    "/api/v2/projects/{project_id}/environment-change-requests"
)
ENVIRONMENT_LIST_SORT = "name:asc,environment_id:asc"
_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)

_MAX_SAFE_VALIDATION_ERRORS = 12
_MAX_SAFE_LOCATION_DEPTH = 6
_SAFE_LOCATION_SEGMENTS = frozenset(
    {
        "body",
        "cookie",
        "cursor",
        "environment",
        "environment_id",
        "expected_head_revision_id",
        "expected_revision",
        "header",
        "kind",
        "limit",
        "name",
        "non_secret_env",
        "operation",
        "path",
        "preflight_checks",
        "project_id",
        "query",
        "required_server_tags",
        "secret_references",
        "setup_command",
        "working_directory_policy",
    }
)
_SAFE_VALIDATION_CLASSIFICATION = {
    "extra_forbidden": (
        "unexpected_field",
        "The request contains an unsupported field",
    ),
    "json_invalid": ("invalid_json", "The request body is not valid JSON"),
    "missing": ("required_field", "A required field is missing"),
    "union_tag_invalid": (
        "invalid_discriminator",
        "The request operation is not supported",
    ),
    "union_tag_not_found": (
        "invalid_discriminator",
        "The request operation is not supported",
    ),
}


class EnvironmentValidationIssue(BaseModel):
    type: Literal[
        "invalid_discriminator",
        "invalid_json",
        "invalid_value",
        "required_field",
        "unexpected_field",
    ]
    location: list[str] = Field(max_length=_MAX_SAFE_LOCATION_DEPTH)
    message: Literal[
        "A required field is missing",
        "A request value is invalid",
        "The request body is not valid JSON",
        "The request contains an unsupported field",
        "The request operation is not supported",
    ]

    model_config = ConfigDict(extra="forbid")


class EnvironmentValidationDetails(BaseModel):
    errors: list[EnvironmentValidationIssue] = Field(
        max_length=_MAX_SAFE_VALIDATION_ERRORS
    )

    model_config = ConfigDict(extra="forbid")


class EnvironmentValidationError(BaseModel):
    code: Literal["invalid_environment_request"]
    message: Literal["The Environment request is invalid"]
    request_id: str
    details: EnvironmentValidationDetails

    model_config = ConfigDict(extra="forbid")


class EnvironmentValidationErrorEnvelope(BaseModel):
    error: EnvironmentValidationError

    model_config = ConfigDict(extra="forbid")


def _safe_validation_issue(error: Mapping[str, Any]) -> dict[str, Any]:
    raw_type = error.get("type")
    issue_type, message = _SAFE_VALIDATION_CLASSIFICATION.get(
        raw_type if isinstance(raw_type, str) else "",
        ("invalid_value", "A request value is invalid"),
    )
    raw_location = error.get("loc")
    location = [
        segment if isinstance(segment, str) and segment in _SAFE_LOCATION_SEGMENTS else "<field>"
        for segment in (
            list(raw_location)[:_MAX_SAFE_LOCATION_DEPTH]
            if isinstance(raw_location, (list, tuple))
            else []
        )
    ]
    if not location:
        location = ["body"]
    return {"type": issue_type, "location": location, "message": message}


class EnvironmentSafeValidationRoute(APIRoute):
    """Keep rejected Environment values out of validation responses and logs."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                safe_errors = [
                    _safe_validation_issue(error)
                    for error in exc.errors()[:_MAX_SAFE_VALIDATION_ERRORS]
                ]
                if not safe_errors:
                    safe_errors = [
                        {
                            "type": "invalid_value",
                            "location": ["body"],
                            "message": "A request value is invalid",
                        }
                    ]
                raise APIError(
                    code="invalid_environment_request",
                    message="The Environment request is invalid",
                    status_code=422,
                    details={"errors": safe_errors},
                ) from None

        return safe_route_handler


_SAFE_VALIDATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "model": EnvironmentValidationErrorEnvelope,
        "description": "Secret-safe Environment request validation error",
    }
}

router = APIRouter(
    prefix=API_V2_PREFIX,
    route_class=EnvironmentSafeValidationRoute,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
        Depends(project_environments_v1_feature_gate),
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


def _canonical_uuid(value: str) -> str:
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
        has_project_binding = any(
            binding.active and binding.project_id == project_id
            for binding in context.project_role_bindings
        )
        if decision.reason in _OPAQUE_PROJECT_DENIALS or (
            decision.reason is AuthorizationReason.DENIED_PROJECT_ROLE_INSUFFICIENT
            and not has_project_binding
        ):
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


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _environment_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason in {
        "dangerous_setup_command",
        "setup command must not contain credential material",
        "setup command must not reference a declared secret",
    }:
        return APIError(
            code="dangerous_setup_command",
            message="The Environment setup command is not permitted",
            status_code=400,
        )
    if reason in {"environment_not_found", "environment project not found"}:
        return APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    if reason == "high_risk_self_decision":
        return APIError(
            code="high_risk_self_decision",
            message="The requester cannot decide this approval",
            status_code=403,
        )
    return APIError(
        code="environment_change_conflict",
        message="The Environment change is not valid for the current state",
        status_code=409,
        details={"reason": reason},
    )


def _load_environment_approval(
    database: Database,
    approval_id: int,
) -> tuple[Approval, str]:
    approval = database.get_approval(approval_id)
    if (
        approval is None
        or approval.kind != "environment_change_v2"
        or not isinstance(approval.payload, dict)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    project_id = approval.payload.get("project_id")
    if not isinstance(project_id, str):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    return approval, _canonical_uuid(project_id)


@router.get(
    "/projects/{project_id}/environments",
    responses=_SAFE_VALIDATION_RESPONSES,
)
def list_project_environments(
    project_id: str,
    request: Request,
    response: Response,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_project_action(
        request,
        Action.PROJECT_VIEW,
        project_id=project_id,
    )
    database = _database(request)
    if database.get_project(project_id) is None:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=ENVIRONMENT_LIST_ROUTE,
        sort_contract=ENVIRONMENT_LIST_SORT,
        filters={},
    )
    after: tuple[str, str] | None = None
    if cursor is not None:
        after = cast(
            tuple[str, str],
            decode_cursor(
                cursor,
                expected_query_sha256=query_sha256,
                key_types=(str, str),
            ),
        )
    try:
        rows = database.list_project_environment_heads_page(
            project_id=project_id,
            after=after,
            limit_plus_one=limit + 1,
        )
        candidates = database.list_verified_active_host_candidates()
    except ValueError as exc:
        raise _environment_error(exc) from exc
    page = build_cursor_page(
        rows,
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda item: (item["name"], item["environment_id"]),
    )
    stale_after_seconds = max(
        60,
        3 * int(request.app.state.dispatch_config.monitor_interval_sec),
    )
    now = datetime.now(timezone.utc)
    items = []
    for row in page.items:
        revision = row["revision"]
        row_status = row["status"]
        if row_status not in {"approved", "archived"}:
            raise APIError(
                code="environment_contract_invalid",
                message="The Environment contract could not be verified",
                status_code=409,
            )
        readiness = evaluate_environment_readiness(
            revision,
            status=row_status,
            candidates=candidates,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
        items.append(
            {
                "environment_id": row["environment_id"],
                "name": row["name"],
                "status": row_status,
                "head_revision": revision.model_dump(mode="json"),
                "readiness": readiness,
                "approval_id": row["approval_id"],
                "created_at": row["created_at"],
            }
        )
    _no_store(response)
    return {"project_id": project_id, "items": items, "next_cursor": page.next_cursor}


@router.post(
    "/projects/{project_id}/environment-change-requests",
    status_code=status.HTTP_202_ACCEPTED,
    responses=_SAFE_VALIDATION_RESPONSES,
)
def request_environment_change(
    project_id: str,
    body: EnvironmentChangeRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_project_action(
        request,
        Action.PROJECT_OPERATE,
        project_id=project_id,
    )
    database = _database(request)
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"project_id": project_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        environment_id = (
            body.environment_id
            if isinstance(body, (EnvironmentUpdateRequest, EnvironmentArchiveRequest))
            else None
        )
        environment = (
            body.environment
            if isinstance(body, (EnvironmentCreateRequest, EnvironmentUpdateRequest))
            else None
        )
        try:
            approval_id = database.create_environment_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation=body.operation,
                expected_revision=body.expected_revision,
                expected_head_revision_id=body.expected_head_revision_id,
                environment_id=environment_id,
                environment=environment,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _environment_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval_id = int(outcome.resource.resource_id)
    approval = database.get_approval(approval_id)
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Environment approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval_id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


def handle_environment_change_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    config = request.app.state.dispatch_config
    if not bool(config.project_environments_v1_enabled):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    database = _database(request)
    approval, project_id = _load_environment_approval(database, approval.id)
    context = _require_project_action(
        request,
        Action.APPROVAL_DECIDE,
        project_id=project_id,
        requester_actor_id=approval.requester_actor_id,
        high_risk=True,
    )
    try:
        verified = database.get_verified_product_approval(approval.id)
    except (TypeError, ValueError) as exc:
        raise _environment_error(ValueError("environment approval contract invalid")) from exc
    if verified is None or not isinstance(verified.payload_sha256, str):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    identity = idempotency.bind(
        body={
            **body.model_dump(mode="json"),
            "approval_payload_digest": verified.payload_sha256,
        },
        path={"approval_id": approval.id},
        query={},
    )
    materialized: dict[str, Any] = {}

    def decide(_cursor: Any) -> IdempotencyResource:
        nonlocal materialized
        try:
            if body.decision == "approve":
                materialized = database.apply_environment_change_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
            else:
                database.reject_environment_change_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _environment_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    decided = database.get_approval(approval.id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Environment approval disappeared after decision")
    if outcome.replayed and body.decision == "approve":
        payload = verified.payload
        target = payload.get("target_revision", {})
        materialized = {
            "project_id": payload.get("project_id"),
            "environment_id": target.get("environment_id"),
            "environment_revision_id": target.get("revision_id"),
            "revision": target.get("revision"),
            "status": "archived" if payload.get("operation") == "archive" else "approved",
            "payload_digest": verified.payload_sha256,
        }
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": decided.status,
        **materialized,
    }


__all__ = [
    "ENVIRONMENT_LIST_ROUTE",
    "ENVIRONMENT_LIST_SORT",
    "ENVIRONMENT_REQUEST_ROUTE",
    "handle_environment_change_decision",
    "router",
]
