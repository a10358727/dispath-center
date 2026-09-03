"""Product v2 ExecutionPlan preview, submit, and approval materialization."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any, Coroutine, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.authorization import (
    Action,
    AuthorizationReason,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.db import Approval, Database
from app.execution_plan_v2 import (
    EXECUTION_PLAN_V2_APPROVAL_KIND,
    ExecutionPlanV2Request,
    ExecutionPlanV2SubmitRequest,
)
from app.execution_plan_v2_store import (
    apply_execution_plan_v2_decision_in_transaction,
    create_execution_plan_v2_request_in_transaction,
    get_execution_plan_v2_request_result,
    reject_execution_plan_v2_decision_in_transaction,
    resolve_execution_plan_v2,
)
from app.identity import RequestContext
from app.product_run_store import (
    apply_product_stop_decision_in_transaction,
    create_product_stop_request_in_transaction,
    get_product_run_artifact_comparison_summary,
    get_product_run_artifacts,
    get_product_run_comparison_evidence,
    get_product_run_detail,
    get_product_run_scope,
    get_product_stop_approval_scope,
    get_product_stop_request_result,
)
from app.product_runs import (
    ProductRunClonePreviewRequest,
    ProductRunStopRequest,
    build_product_run_clone_request,
    compare_product_run_dimensions,
)
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import (
    IdempotencyRequestContext,
    IdempotencyResource,
    idempotency_context_dependency,
)
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_CURSOR_BYTES,
    MAX_PAGE_LIMIT,
    decode_cursor,
    encode_cursor,
    pagination_query_sha256,
)
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    dataset_assets_v2_feature_gate,
    product_rbac_v2_feature_gate,
    project_environments_v1_feature_gate,
    run_experience_v2_feature_gate,
    run_template_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


RUN_PREVIEW_ROUTE = "/api/v2/projects/{project_id}/run-previews"
RUN_REQUEST_ROUTE = "/api/v2/projects/{project_id}/run-requests"
PRODUCT_RUN_DETAIL_ROUTE = "/api/v2/runs/{plan_id}"
PRODUCT_RUN_CLONE_ROUTE = "/api/v2/runs/{plan_id}/clone-previews"
PRODUCT_RUN_COMPARE_ROUTE = "/api/v2/runs/compare"
PRODUCT_RUN_STOP_ROUTE = "/api/v2/runs/{plan_id}/stop-requests"
PRODUCT_RUN_ARTIFACTS_ROUTE = "/api/v2/runs/{plan_id}/artifacts"
PRODUCT_RUN_ARTIFACT_SORT = "artifact_id:asc"
_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)
_SAFE_LOCATION_SEGMENTS = frozenset(
    {
        "alias_name",
        "asset_id",
        "bindings",
        "body",
        "dataset_selection",
        "dispatch_policy_id",
        "expected_plan_digest",
        "kind",
        "name",
        "parameter_overrides",
        "project_defaults_revision_id",
        "project_id",
        "project_version_id",
        "run_profile_id",
        "selection",
        "server_config_revision_id",
        "snapshot_id",
        "target_selection",
        "template_selection",
    }
)
_SAFE_CONFLICT_REASONS = frozenset(
    {
        "compiled_argv_changed",
        "dataset_alias_revision_changed",
        "dataset_alias_revision_unavailable",
        "dataset_alias_unavailable",
        "dataset_entitlement_changed",
        "dataset_entitlement_unavailable",
        "dataset_snapshot_unavailable",
        "dispatch_policy_changed",
        "dispatch_policy_concurrency_exhausted",
        "dispatch_policy_dataset_required",
        "dispatch_policy_expired",
        "dispatch_policy_head_unavailable",
        "dispatch_policy_run_profile_mismatch",
        "environment_contract_changed",
        "environment_head_changed",
        "environment_head_unavailable",
        "environment_preflight_evidence_unsupported",
        "execution_plan_approval_not_pending",
        "execution_plan_decision_conflict",
        "execution_plan_request_result_invalid",
        "hardware_action_required",
        "hardware_compute_template",
        "hardware_device_kind_mismatch",
        "hardware_device_not_declared",
        "hardware_image_digest_mismatch",
        "hardware_image_not_applicable",
        "hardware_image_push_failed",
        "hardware_image_required",
        "hardware_image_unavailable",
        "hardware_power_sequence_not_applicable",
        "hardware_power_sequence_required",
        "target_device_absent",
        "target_device_observation_unknown",
        "high_risk_self_decision",
        "legacy_run_profile_not_supported",
        "project_defaults_head_changed",
        "project_defaults_head_stale",
        "project_defaults_head_unavailable",
        "project_rbac_not_ready",
        "project_version_changed",
        "project_version_provenance_invalid",
        "project_version_unavailable",
        "run_profile_contract_changed",
        "run_profile_head_changed",
        "run_profile_head_unavailable",
        "target_contract_changed",
        "target_gpu_count_insufficient",
        "target_gpu_memory_insufficient",
        "target_observation_not_ready",
        "target_observation_stale",
        "target_observation_unknown",
        "target_project_instance_unavailable",
        "target_ram_insufficient",
        "target_disk_insufficient",
        "target_required_tags_missing",
        "target_revision_unavailable",
        "target_worker_not_exclusive",
    }
)


class ExecutionPlanSafeValidationRoute(APIRoute):
    """Do not reflect rejected parameter or identifier values in 422 bodies."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                errors = []
                for error in exc.errors()[:12]:
                    location = [
                        segment
                        if isinstance(segment, str) and segment in _SAFE_LOCATION_SEGMENTS
                        else "<field>"
                        for segment in error.get("loc", ())[:6]
                    ]
                    errors.append(
                        {
                            "type": "invalid_value",
                            "location": location or ["body"],
                            "message": "An ExecutionPlan request value is invalid",
                        }
                    )
                raise APIError(
                    code="invalid_execution_plan_request",
                    message="The ExecutionPlan request is invalid",
                    status_code=422,
                    details={"errors": errors},
                ) from None

        return safe_route_handler


router = APIRouter(
    prefix=API_V2_PREFIX,
    route_class=ExecutionPlanSafeValidationRoute,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
        Depends(project_environments_v1_feature_gate),
        Depends(run_template_v2_feature_gate),
        Depends(dataset_assets_v2_feature_gate),
        Depends(run_experience_v2_feature_gate),
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
    except (AttributeError, ValueError):
        raise _not_found() from None
    if normalized != value:
        raise _not_found()
    return normalized


def _not_found() -> APIError:
    return APIError(
        code="not_found",
        message="Resource not found",
        status_code=404,
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


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
        resource_scope=ResourceScope.PROJECT,
        requester_actor_id=requester_actor_id,
        high_risk=high_risk,
    )
    if decision.allowed:
        return context
    if decision.reason in _OPAQUE_PROJECT_DENIALS:
        raise _not_found()
    raise APIError(
        code="forbidden",
        message="The requested action is not permitted",
        status_code=403,
        details={"reason": decision.reason.value},
    )


def _execution_plan_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason == "expected_plan_digest_mismatch":
        return APIError(
            code="plan_digest_mismatch",
            message="Execution plan verification failed",
            status_code=409,
        )
    if reason == "high_risk_self_decision":
        return APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
            details={"reason": reason},
        )
    return APIError(
        code="execution_plan_conflict",
        message="The ExecutionPlan is not valid for the current project state",
        status_code=409,
        details={"reason": reason if reason in _SAFE_CONFLICT_REASONS else "not_ready"},
    )


def _sharing_enabled(request: Request) -> bool:
    return bool(request.app.state.dispatch_config.dataset_sharing_v2_enabled)


def _product_run_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if any(marker in reason for marker in ("not authorized", "must be human", "is not enabled")):
        return APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
        )
    safe_reasons = {
        "product_artifact_page_invalid",
        "product_run_contract_invalid",
        "product_stop_already_rejected",
        "product_stop_contract_invalid",
        "product_stop_decision_conflict",
        "product_stop_materialization_conflict",
        "product_stop_not_running",
        "product_stop_recovery_hold",
    }
    return APIError(
        code="product_run_conflict",
        message="The Product Run is not valid for this operation",
        status_code=409,
        details={"reason": reason if reason in safe_reasons else "not_ready"},
    )


def _product_run_scope_or_not_found(
    database: Database,
    plan_id: str,
) -> dict[str, Any]:
    scope = get_product_run_scope(database, plan_id)
    if scope is None:
        raise _not_found()
    return scope


def _require_compare_access(
    request: Request,
    *,
    left_project_id: str,
    right_project_id: str,
) -> RequestContext:
    context = _request_context(request)
    decisions = tuple(
        evaluate_enforced_authorization(
            context,
            Action.PROJECT_VIEW,
            project_id=project_id,
            resource_scope=ResourceScope.PROJECT,
        )
        for project_id in (left_project_id, right_project_id)
    )
    if any(decision.reason in _OPAQUE_PROJECT_DENIALS for decision in decisions):
        raise _not_found()
    if not all(decision.allowed for decision in decisions):
        raise APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
        )
    return context


@router.post("/projects/{project_id}/run-previews")
def preview_run(
    project_id: str,
    body: ExecutionPlanV2Request,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Resolve exact run inputs and target evidence without persisting state."""

    project_id = _canonical_uuid(project_id)
    context = _require_project_action(
        request,
        Action.PROJECT_OPERATE,
        project_id=project_id,
    )
    try:
        resolved = resolve_execution_plan_v2(
            _database(request),
            project_id=project_id,
            request=body,
            requester_actor_id=cast(str, context.actor_id),
            sharing_enabled=_sharing_enabled(request),
        )
    except ValueError as exc:
        raise _execution_plan_error(exc) from exc
    spec = resolved["spec"]
    _no_store(response)
    return {
        "contract_version": spec.contract_version,
        "ready": True,
        "plan": spec.model_dump(mode="json"),
        "plan_digest": spec.plan_digest,
        "evidence": resolved["evidence"],
    }


@router.post(
    "/projects/{project_id}/run-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_run(
    project_id: str,
    body: ExecutionPlanV2SubmitRequest,
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
    request_body = ExecutionPlanV2Request.model_validate(
        body.model_dump(mode="json", exclude={"expected_plan_digest"})
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"project_id": project_id},
        query={},
    )
    created: dict[str, Any] = {}

    def create(cursor: Any) -> IdempotencyResource:
        nonlocal created
        try:
            created = create_execution_plan_v2_request_in_transaction(
                database,
                cursor,
                project_id=project_id,
                request=request_body,
                expected_plan_digest=body.expected_plan_digest,
                requester_actor_id=cast(str, context.actor_id),
                sharing_enabled=_sharing_enabled(request),
            )
        except ValueError as exc:
            raise _execution_plan_error(exc) from exc
        return IdempotencyResource(
            "execution_plan",
            str(created["execution_plan_id"]),
        )

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    if outcome.resource.resource_type != "execution_plan":
        raise RuntimeError("run request idempotency resource type is invalid")
    try:
        result = get_execution_plan_v2_request_result(
            database,
            outcome.resource.resource_id,
        )
    except ValueError as exc:
        raise _execution_plan_error(exc) from exc
    if result is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("ExecutionPlan disappeared after commit")
    _no_store(response)
    return {**result, "replayed": outcome.replayed}


# This static route must be registered before every dynamic ``/runs/{plan_id}``
# route so the literal ``compare`` segment can never be interpreted as an ID.
@router.get("/runs/compare")
def compare_product_runs(
    request: Request,
    response: Response,
    left_plan_id: str = Query(min_length=36, max_length=36),
    right_plan_id: str = Query(min_length=36, max_length=36),
) -> dict[str, Any]:
    left_plan_id = _canonical_uuid(left_plan_id)
    right_plan_id = _canonical_uuid(right_plan_id)
    database = _database(request)
    left_scope = _product_run_scope_or_not_found(database, left_plan_id)
    right_scope = _product_run_scope_or_not_found(database, right_plan_id)
    _require_compare_access(
        request,
        left_project_id=left_scope["project_id"],
        right_project_id=right_scope["project_id"],
    )
    try:
        left = get_product_run_comparison_evidence(database, left_plan_id)
        right = get_product_run_comparison_evidence(database, right_plan_id)
    except ValueError as exc:
        raise _product_run_error(exc) from exc
    if left is None or right is None:  # pragma: no cover - same resolved rows
        raise _not_found()
    dimensions = compare_product_run_dimensions(
        left_spec=left["_spec"],
        right_spec=right["_spec"],
        left_terminal=left["_terminal_comparison"],
        right_terminal=right["_terminal_comparison"],
        left_artifacts=get_product_run_artifact_comparison_summary(
            database,
            left_plan_id,
        ),
        right_artifacts=get_product_run_artifact_comparison_summary(
            database,
            right_plan_id,
        ),
    )
    _no_store(response)
    return {
        "left_plan_id": left_plan_id,
        "right_plan_id": right_plan_id,
        "dimensions": dimensions,
        "truncated": False,
        "unknown_dimensions": sorted(
            name for name, dimension in dimensions.items() if dimension["availability"] == "unknown"
        ),
    }


@router.get("/runs/{plan_id}")
def get_product_run(
    plan_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    plan_id = _canonical_uuid(plan_id)
    database = _database(request)
    scope = _product_run_scope_or_not_found(database, plan_id)
    _require_project_action(
        request,
        Action.PROJECT_VIEW,
        project_id=scope["project_id"],
    )
    try:
        detail = get_product_run_detail(database, plan_id)
    except ValueError as exc:
        raise _product_run_error(exc) from exc
    if detail is None:  # pragma: no cover - same resolved row
        raise _not_found()
    _no_store(response)
    return detail


@router.post("/runs/{plan_id}/clone-previews")
def preview_product_run_clone(
    plan_id: str,
    body: ProductRunClonePreviewRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    plan_id = _canonical_uuid(plan_id)
    database = _database(request)
    scope = _product_run_scope_or_not_found(database, plan_id)
    context = _require_project_action(
        request,
        Action.PROJECT_OPERATE,
        project_id=scope["project_id"],
    )
    try:
        source = get_product_run_comparison_evidence(database, plan_id)
        if source is None or source["_spec"] is None:
            raise ValueError("product_run_contract_invalid")
        clone_request = build_product_run_clone_request(source["_spec"], body)
        resolved = resolve_execution_plan_v2(
            database,
            project_id=scope["project_id"],
            request=clone_request,
            requester_actor_id=cast(str, context.actor_id),
            sharing_enabled=_sharing_enabled(request),
        )
    except ValueError as exc:
        if str(exc).startswith("product_run_"):
            raise _product_run_error(exc) from exc
        raise _execution_plan_error(exc) from exc
    spec = resolved["spec"]
    _no_store(response)
    return {
        "source_plan_id": plan_id,
        "contract_version": spec.contract_version,
        "ready": True,
        "request": clone_request.model_dump(mode="json"),
        "plan": spec.model_dump(mode="json"),
        "plan_digest": spec.plan_digest,
        "evidence": resolved["evidence"],
    }


@router.post(
    "/runs/{plan_id}/stop-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_product_run_stop(
    plan_id: str,
    body: ProductRunStopRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    plan_id = _canonical_uuid(plan_id)
    database = _database(request)
    scope = _product_run_scope_or_not_found(database, plan_id)
    context = _require_project_action(
        request,
        Action.PROJECT_OPERATE,
        project_id=scope["project_id"],
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"plan_id": plan_id},
        query={},
    )
    created: dict[str, Any] = {}

    def create(cursor: Any) -> IdempotencyResource:
        nonlocal created
        try:
            created = create_product_stop_request_in_transaction(
                database,
                cursor,
                plan_id=plan_id,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _product_run_error(exc) from exc
        return IdempotencyResource("approval", str(created["approval_id"]))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    if outcome.resource.resource_type != "approval":
        raise RuntimeError("Product stop idempotency resource type is invalid")
    result = get_product_stop_request_result(
        database,
        int(outcome.resource.resource_id),
    )
    if result is None:
        raise RuntimeError("Product stop approval disappeared after commit")
    _no_store(response)
    return {
        **result,
        "replayed": outcome.replayed,
        "deduplicated": (False if outcome.replayed else bool(created["deduplicated"])),
    }


@router.get("/runs/{plan_id}/artifacts")
def list_product_run_artifacts(
    plan_id: str,
    request: Request,
    response: Response,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    plan_id = _canonical_uuid(plan_id)
    database = _database(request)
    scope = _product_run_scope_or_not_found(database, plan_id)
    context = _require_project_action(
        request,
        Action.PROJECT_VIEW,
        project_id=scope["project_id"],
    )
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=PRODUCT_RUN_ARTIFACTS_ROUTE,
        sort_contract=PRODUCT_RUN_ARTIFACT_SORT,
        filters={"plan_id": plan_id},
    )
    after_id = 0
    if cursor is not None:
        after_id = cast(
            int,
            decode_cursor(
                cursor,
                expected_query_sha256=query_sha256,
                key_types=(int,),
            )[0],
        )
        if after_id < 1:
            raise APIError(
                code="invalid_cursor",
                message="The pagination cursor is invalid",
                status_code=400,
            )
    try:
        result = get_product_run_artifacts(
            database,
            plan_id=plan_id,
            after_id=after_id,
            limit=limit,
        )
    except ValueError as exc:
        raise _product_run_error(exc) from exc
    if result is None:  # pragma: no cover - same resolved row
        raise _not_found()
    next_after_id = result.pop("next_after_id")
    result["next_cursor"] = (
        encode_cursor((next_after_id,), query_sha256=query_sha256)
        if next_after_id is not None
        else None
    )
    _no_store(response)
    return result


def handle_product_stop_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    if not bool(request.app.state.dispatch_config.run_experience_v2_enabled):
        raise _not_found()
    database = _database(request)
    scope = get_product_stop_approval_scope(database, approval.id)
    if scope is None:
        raise _not_found()
    context = _require_project_action(
        request,
        Action.APPROVAL_DECIDE,
        project_id=scope["project_id"],
        requester_actor_id=approval.requester_actor_id,
        high_risk=False,
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"approval_id": approval.id},
        query={},
    )
    materialized: dict[str, Any] = {}

    def decide(cursor: Any) -> IdempotencyResource:
        nonlocal materialized
        try:
            materialized = apply_product_stop_decision_in_transaction(
                database,
                cursor,
                approval_id=approval.id,
                decision=body.decision,
                decision_actor_id=cast(str, context.actor_id),
                decision_mechanism="product_rbac_v2",
                note=body.note,
            )
        except ValueError as exc:
            raise _product_run_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    result = get_product_stop_request_result(database, approval.id)
    if result is None:
        raise RuntimeError("Product stop decision result is unavailable")
    if outcome.replayed:
        materialized = {
            "approval_id": approval.id,
            "status": result["status"],
            "operation_id": None,
        }
    _no_store(response)
    return {**materialized, "plan_id": result["plan_id"], "replayed": outcome.replayed}


def handle_execution_plan_v2_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    if not bool(request.app.state.dispatch_config.run_experience_v2_enabled):
        raise _not_found()
    if approval.kind != EXECUTION_PLAN_V2_APPROVAL_KIND or not isinstance(
        approval.payload, Mapping
    ):
        raise _not_found()
    raw_project_id = approval.payload.get("project_id")
    if not isinstance(raw_project_id, str):
        raise _not_found()
    project_id = _canonical_uuid(raw_project_id)
    context = _require_project_action(
        request,
        Action.APPROVAL_DECIDE,
        project_id=project_id,
        requester_actor_id=approval.requester_actor_id,
        high_risk=True,
    )
    database = _database(request)
    try:
        verified = database.get_verified_product_approval(approval.id)
    except (TypeError, ValueError) as exc:
        raise _execution_plan_error(ValueError("execution_plan_approval_invalid")) from exc
    if verified is None or not isinstance(verified.payload_sha256, str):
        raise _not_found()
    identity = idempotency.bind(
        body={
            **body.model_dump(mode="json"),
            "approval_payload_digest": verified.payload_sha256,
        },
        path={"approval_id": approval.id},
        query={},
    )
    materialized: dict[str, Any] = {}

    def decide(cursor: Any) -> IdempotencyResource:
        nonlocal materialized
        try:
            if body.decision == "approve":
                materialized = apply_execution_plan_v2_decision_in_transaction(
                    database,
                    cursor,
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    sharing_enabled=_sharing_enabled(request),
                    note=body.note,
                )
            else:
                materialized = reject_execution_plan_v2_decision_in_transaction(
                    database,
                    cursor,
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _execution_plan_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    if outcome.replayed:
        execution_plan_id = verified.payload.get("execution_plan_id")
        if not isinstance(execution_plan_id, str):
            raise RuntimeError("ExecutionPlan approval result is invalid")
        try:
            replayed = get_execution_plan_v2_request_result(
                database,
                execution_plan_id,
            )
        except ValueError as exc:
            raise _execution_plan_error(exc) from exc
        if replayed is None:
            raise RuntimeError("ExecutionPlan decision result is unavailable")
        materialized = {
            "approval_id": approval.id,
            "execution_plan_id": execution_plan_id,
            "status": replayed["status"],
            "job_id": replayed["job_id"],
            "created": False,
        }
    _no_store(response)
    return {**materialized, "replayed": outcome.replayed}


__all__ = [
    "PRODUCT_RUN_ARTIFACTS_ROUTE",
    "PRODUCT_RUN_CLONE_ROUTE",
    "PRODUCT_RUN_COMPARE_ROUTE",
    "PRODUCT_RUN_DETAIL_ROUTE",
    "PRODUCT_RUN_STOP_ROUTE",
    "RUN_PREVIEW_ROUTE",
    "RUN_REQUEST_ROUTE",
    "handle_execution_plan_v2_decision",
    "handle_product_stop_decision",
    "router",
]
