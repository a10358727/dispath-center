"""Product v2 typed Run Template and immutable Project Defaults routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
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
from app.run_templates import (
    ProjectDefaultsChangeRequest,
    RunTemplateAdoptRequest,
    RunTemplateArchiveRequest,
    RunTemplateChangeRequest,
    RunTemplateCreateRequest,
    RunTemplateUpdateRequest,
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
    run_template_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


RUN_TEMPLATE_LIST_ROUTE = "/api/v2/projects/{project_id}/run-templates"
RUN_TEMPLATE_REQUEST_ROUTE = (
    "/api/v2/projects/{project_id}/run-template-change-requests"
)
PROJECT_DEFAULTS_LIST_ROUTE = "/api/v2/projects/{project_id}/defaults"
PROJECT_DEFAULTS_REQUEST_ROUTE = (
    "/api/v2/projects/{project_id}/default-change-requests"
)
RUN_TEMPLATE_LIST_SORT = "name:asc,run_profile_id:asc"
PROJECT_DEFAULTS_LIST_SORT = "revision:desc,revision_id:desc"
_PRODUCT_CHANGE_KINDS = frozenset(
    {"run_template_change_v2", "project_defaults_change_v2"}
)
_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)
_MAX_SAFE_VALIDATION_ERRORS = 12
_MAX_SAFE_LOCATION_DEPTH = 6
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
_SAFE_LOCATION_SEGMENTS = frozenset(
    {
        "argv_template",
        "body",
        "contract_version",
        "defaults",
        "environment_revision_id",
        "exclusive_worker",
        "expected_environment_head_revision_id",
        "expected_head_classification",
        "expected_head_revision_digest",
        "expected_head_revision_id",
        "expected_head_run_profile_id",
        "expected_head_spec_digest",
        "expected_revision",
        "expected_template_head_run_profile_id",
        "expected_template_spec_digest",
        "kind",
        "limit",
        "maximum",
        "max_length",
        "minimum",
        "min_length",
        "name",
        "operation",
        "output_declarations",
        "parameter_schema",
        "parameter_values",
        "path_pattern",
        "project_id",
        "required",
        "required_tags",
        "resource_requirements",
        "run_profile_id",
        "sensitive",
        "template",
        "type",
        "value",
    }
)


class ProductContractValidationIssue(BaseModel):
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


class ProductContractValidationDetails(BaseModel):
    errors: list[ProductContractValidationIssue] = Field(
        max_length=_MAX_SAFE_VALIDATION_ERRORS
    )

    model_config = ConfigDict(extra="forbid")


class ProductContractValidationError(BaseModel):
    code: Literal["invalid_run_template_request"]
    message: Literal["The Run Template or Defaults request is invalid"]
    request_id: str
    details: ProductContractValidationDetails

    model_config = ConfigDict(extra="forbid")


class ProductContractValidationEnvelope(BaseModel):
    error: ProductContractValidationError

    model_config = ConfigDict(extra="forbid")


def _safe_validation_issue(error: Mapping[str, Any]) -> dict[str, Any]:
    raw_type = error.get("type")
    issue_type, message = _SAFE_VALIDATION_CLASSIFICATION.get(
        raw_type if isinstance(raw_type, str) else "",
        ("invalid_value", "A request value is invalid"),
    )
    raw_location = error.get("loc")
    segments = (
        list(raw_location)[:_MAX_SAFE_LOCATION_DEPTH]
        if isinstance(raw_location, (list, tuple))
        else []
    )
    location = [
        segment
        if isinstance(segment, str) and segment in _SAFE_LOCATION_SEGMENTS
        else "<field>"
        for segment in segments
    ]
    return {
        "type": issue_type,
        "location": location or ["body"],
        "message": message,
    }


class ProductContractSafeValidationRoute(APIRoute):
    """Discard rejected contract values and raw validator messages."""

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
                ] or [
                    {
                        "type": "invalid_value",
                        "location": ["body"],
                        "message": "A request value is invalid",
                    }
                ]
                raise APIError(
                    code="invalid_run_template_request",
                    message="The Run Template or Defaults request is invalid",
                    status_code=422,
                    details={"errors": safe_errors},
                ) from None

        return safe_route_handler


_SAFE_VALIDATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ProductContractValidationEnvelope,
        "description": "Value-safe typed contract validation error",
    }
}

router = APIRouter(
    prefix=API_V2_PREFIX,
    route_class=ProductContractSafeValidationRoute,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
        Depends(project_environments_v1_feature_gate),
        Depends(run_template_v2_feature_gate),
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
    if decision.allowed:
        return context
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


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _contract_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason in {"environment project not found"}:
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
    if "contract is invalid" in reason or "payload" in reason:
        return APIError(
            code="run_template_contract_invalid",
            message="The immutable Product contract could not be verified",
            status_code=409,
        )
    return APIError(
        code="run_template_change_conflict",
        message="The Run Template or Defaults change is stale or invalid",
        status_code=409,
        details={"reason": reason},
    )


def _require_project_exists(database: Database, project_id: str) -> None:
    if database.get_project(project_id) is None:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


@router.get(
    "/projects/{project_id}/run-templates",
    responses=_SAFE_VALIDATION_RESPONSES,
)
def list_project_run_templates(
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
    _require_project_exists(database, project_id)
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=RUN_TEMPLATE_LIST_ROUTE,
        sort_contract=RUN_TEMPLATE_LIST_SORT,
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
        rows = database.list_project_run_template_heads_page(
            project_id=project_id,
            after=after,
            limit_plus_one=limit + 1,
        )
    except ValueError as exc:
        raise _contract_error(exc) from exc
    page = build_cursor_page(
        rows,
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda item: (item["name"], item["run_profile_id"]),
    )
    _no_store(response)
    return {
        "project_id": project_id,
        "items": [
            {
                **item,
                "head_spec": (
                    item["head_spec"].model_dump(mode="json")
                    if item["head_spec"] is not None
                    else None
                ),
            }
            for item in page.items
        ],
        "next_cursor": page.next_cursor,
    }


@router.post(
    "/projects/{project_id}/run-template-change-requests",
    status_code=status.HTTP_202_ACCEPTED,
    responses=_SAFE_VALIDATION_RESPONSES,
)
def request_run_template_change(
    project_id: str,
    body: RunTemplateChangeRequest,
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
        try:
            approval_id = (
                database.create_run_template_change_approval_in_transaction(
                    cursor,
                    project_id=project_id,
                    operation=body.operation,
                    expected_revision=body.expected_revision,
                    expected_head_run_profile_id=(
                        body.expected_head_run_profile_id
                    ),
                    expected_head_classification=(
                        body.expected_head_classification
                    ),
                    expected_head_spec_digest=body.expected_head_spec_digest,
                    expected_environment_head_revision_id=(
                        body.expected_environment_head_revision_id
                        if not isinstance(body, RunTemplateArchiveRequest)
                        else None
                    ),
                    template=(
                        body.template
                        if isinstance(
                            body,
                            (
                                RunTemplateCreateRequest,
                                RunTemplateAdoptRequest,
                                RunTemplateUpdateRequest,
                            ),
                        )
                        else None
                    ),
                    requester_actor_id=cast(str, context.actor_id),
                )
            )
        except ValueError as exc:
            raise _contract_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Run Template approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.get(
    "/projects/{project_id}/defaults",
    responses=_SAFE_VALIDATION_RESPONSES,
)
def list_project_defaults(
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
    _require_project_exists(database, project_id)
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=PROJECT_DEFAULTS_LIST_ROUTE,
        sort_contract=PROJECT_DEFAULTS_LIST_SORT,
        filters={},
    )
    after: tuple[int, str] | None = None
    if cursor is not None:
        after = cast(
            tuple[int, str],
            decode_cursor(
                cursor,
                expected_query_sha256=query_sha256,
                key_types=(int, str),
            ),
        )
    try:
        rows = database.list_project_defaults_history_page(
            project_id=project_id,
            after=after,
            limit_plus_one=limit + 1,
        )
    except ValueError as exc:
        raise _contract_error(exc) from exc
    page = build_cursor_page(
        rows,
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda item: (item["revision"], item["revision_id"]),
    )
    _no_store(response)
    return {
        "project_id": project_id,
        "items": [
            {
                **item,
                "contract": item["contract"].model_dump(mode="json"),
            }
            for item in page.items
        ],
        "next_cursor": page.next_cursor,
    }


@router.post(
    "/projects/{project_id}/default-change-requests",
    status_code=status.HTTP_202_ACCEPTED,
    responses=_SAFE_VALIDATION_RESPONSES,
)
def request_project_defaults_change(
    project_id: str,
    body: ProjectDefaultsChangeRequest,
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
        try:
            approval_id = (
                database.create_project_defaults_change_approval_in_transaction(
                    cursor,
                    project_id=project_id,
                    operation=body.operation,
                    expected_revision=body.expected_revision,
                    expected_head_revision_id=body.expected_head_revision_id,
                    expected_head_revision_digest=(
                        body.expected_head_revision_digest
                    ),
                    expected_template_head_run_profile_id=(
                        body.expected_template_head_run_profile_id
                    ),
                    expected_template_spec_digest=(
                        body.expected_template_spec_digest
                    ),
                    expected_environment_head_revision_id=(
                        body.expected_environment_head_revision_id
                    ),
                    defaults=body.defaults,
                    requester_actor_id=cast(str, context.actor_id),
                )
            )
        except ValueError as exc:
            raise _contract_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Project Defaults approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


def _load_product_change_approval(
    database: Database,
    approval_id: int,
) -> tuple[Approval, str]:
    approval = database.get_approval(approval_id)
    if (
        approval is None
        or approval.kind not in _PRODUCT_CHANGE_KINDS
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


def handle_run_template_product_change_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    config = request.app.state.dispatch_config
    if not bool(config.run_template_v2_enabled):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    database = _database(request)
    approval, project_id = _load_product_change_approval(
        database,
        approval.id,
    )
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
        raise _contract_error(
            ValueError("Product approval contract is invalid")
        ) from exc
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
            if verified.kind == "run_template_change_v2":
                if body.decision == "approve":
                    materialized = database.apply_run_template_change_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
                else:
                    database.reject_run_template_change_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
            elif body.decision == "approve":
                materialized = database.apply_project_defaults_change_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
            else:
                database.reject_project_defaults_change_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _contract_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    decided = database.get_approval(approval.id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Product approval disappeared after decision")
    if outcome.replayed and body.decision == "approve":
        target = verified.payload.get("target_revision", {})
        if verified.kind == "run_template_change_v2":
            materialized = {
                "project_id": verified.payload.get("project_id"),
                "run_profile_id": target.get("run_profile_id"),
                "revision": target.get("revision"),
                "status": (
                    "archived"
                    if verified.payload.get("operation") == "archive"
                    else "approved"
                ),
                "spec_digest": target.get("spec_digest"),
                "payload_digest": verified.payload_sha256,
            }
        else:
            materialized = {
                "project_id": verified.payload.get("project_id"),
                "defaults_revision_id": target.get("revision_id"),
                "revision": target.get("revision"),
                "revision_digest": target.get("revision_digest"),
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
    "PROJECT_DEFAULTS_LIST_ROUTE",
    "PROJECT_DEFAULTS_LIST_SORT",
    "PROJECT_DEFAULTS_REQUEST_ROUTE",
    "RUN_TEMPLATE_LIST_ROUTE",
    "RUN_TEMPLATE_LIST_SORT",
    "RUN_TEMPLATE_REQUEST_ROUTE",
    "handle_run_template_product_change_decision",
    "router",
]
