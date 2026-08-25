"""Experiment v2 preview, request, decision, and read-only projections.

DG-EXPERIMENT-V1 P3 (`docs/DG_EXPERIMENT_V1_DECISION.md` EX-1..EX-7,
`docs/DECISIONS.md` 2026-08-25). Follows `runs_v2.py`'s ExecutionPlan v2
route conventions: the same idempotent request/decision shape, the same
opaque-project-denial handling, and the same "resolve, never mutate" preview
contract -- generalized from one plan to N members sharing one approval.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.authorization import (
    Action,
    AuthorizationReason,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.db import Approval, Database
from app.experiment_v2 import (
    EXPERIMENT_V2_APPROVAL_KIND,
    ExperimentMatrixError,
    ExperimentV2Request,
    ExperimentV2SubmitRequest,
)
from app.experiment_v2_store import (
    apply_experiment_v2_decision_in_transaction,
    create_experiment_v2_request_in_transaction,
    get_experiment_v2,
    get_experiment_v2_by_approval,
    get_experiment_v2_scope,
    list_experiments_v2_for_project,
    preview_experiment_v2_in_cursor,
    reject_experiment_v2_decision_in_transaction,
)
from app.identity import RequestContext
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import (
    IdempotencyRequestContext,
    IdempotencyResource,
    idempotency_context_dependency,
)
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    dataset_assets_v2_feature_gate,
    experiment_v2_feature_gate,
    product_rbac_v2_feature_gate,
    project_environments_v1_feature_gate,
    run_experience_v2_feature_gate,
    run_template_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


EXPERIMENT_PREVIEW_ROUTE = "/api/v2/projects/{project_id}/experiment-previews"
EXPERIMENT_REQUEST_ROUTE = "/api/v2/projects/{project_id}/experiment-requests"
EXPERIMENT_LIST_ROUTE = "/api/v2/experiments"
EXPERIMENT_DETAIL_ROUTE = "/api/v2/experiments/{experiment_id}"
_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)
#: Safe, closed-vocabulary reason codes -- anything else collapses to
#: "not_ready" so a 409 body never reflects raw exception text (mirrors
#: `runs_v2.py`'s `_SAFE_CONFLICT_REASONS`).
_SAFE_CONFLICT_REASONS = frozenset(
    {
        "experiment_v2_disabled",
        "experiment_guard_run_count_mismatch",
        "experiment_target_resolution_mismatch",
        "experiment_member_shared_selection_mismatch",
        "experiment_member_unavailable",
        "experiment_member_companion_unavailable",
        "experiment_member_contract_mismatch",
        "experiment_member_spec_invalid",
        "experiment_member_count_mismatch",
        "experiment_materialization_incomplete",
        "experiment_materialization_conflict",
        "experiment_project_scope_changed",
        "experiment_approval_unavailable",
        "experiment_approval_invalid",
        "experiment_companion_unavailable",
        "experiment_approval_not_pending",
        "experiment_decision_conflict",
        "experiment_insert_failed",
        "expected_plan_digests_mismatch",
        "target_server_unavailable",
        "target_worker_not_exclusive",
        "project_not_found",
    }
)


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
        Depends(project_environments_v1_feature_gate),
        Depends(run_template_v2_feature_gate),
        Depends(dataset_assets_v2_feature_gate),
        Depends(run_experience_v2_feature_gate),
        Depends(experiment_v2_feature_gate),
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


def _not_found() -> APIError:
    return APIError(
        code="not_found",
        message="Resource not found",
        status_code=404,
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _canonical_project_id(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, ValueError):
        raise _not_found() from None
    if normalized != value:
        raise _not_found()
    return normalized


def _sharing_enabled(request: Request) -> bool:
    return bool(request.app.state.dispatch_config.dataset_sharing_v2_enabled)


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


def _experiment_error(exc: ValueError) -> APIError:
    if isinstance(exc, ExperimentMatrixError):
        return APIError(
            code="experiment_matrix_invalid",
            message="The Experiment matrix is invalid",
            status_code=422,
        )
    reason = str(exc)
    if reason == "high_risk_self_decision":
        return APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
            details={"reason": reason},
        )
    return APIError(
        code="experiment_conflict",
        message="The Experiment is not valid for the current project state",
        status_code=409,
        details={"reason": reason if reason in _SAFE_CONFLICT_REASONS else "not_ready"},
    )


def _experiment_scope_or_not_found(database: Database, experiment_id: int) -> dict[str, Any]:
    scope = get_experiment_v2_scope(database, experiment_id)
    if scope is None:
        raise _not_found()
    return scope


@router.post("/projects/{project_id}/experiment-previews")
def preview_experiment(
    project_id: str,
    body: ExperimentV2Request,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Resolve the full N-run expansion and its plan digests -- no writes."""

    project_id = _canonical_project_id(project_id)
    context = _require_project_action(
        request,
        Action.PROJECT_OPERATE,
        project_id=project_id,
    )
    database = _database(request)
    try:
        with database.cursor() as cursor:
            preview = preview_experiment_v2_in_cursor(
                database,
                cursor,
                project_id=project_id,
                template_selection=body.template_selection,
                dataset_selection=body.dataset_selection,
                project_version_id=body.project_version_id,
                matrix=body.matrix,
                guard=body.guard,
                requester_actor_id=cast(str, context.actor_id),
                sharing_enabled=_sharing_enabled(request),
                experiment_v2_enabled=bool(
                    request.app.state.dispatch_config.experiment_v2_enabled
                ),
            )
    except ValueError as exc:
        raise _experiment_error(exc) from exc
    _no_store(response)
    return preview


@router.post(
    "/projects/{project_id}/experiment-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_experiment(
    project_id: str,
    body: ExperimentV2SubmitRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    project_id = _canonical_project_id(project_id)
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
    created: dict[str, Any] = {}

    def create(cursor: Any) -> IdempotencyResource:
        nonlocal created
        try:
            created = create_experiment_v2_request_in_transaction(
                database,
                cursor,
                project_id=project_id,
                template_selection=body.template_selection,
                dataset_selection=body.dataset_selection,
                project_version_id=body.project_version_id,
                matrix=body.matrix,
                guard=body.guard,
                requester_actor_id=cast(str, context.actor_id),
                sharing_enabled=_sharing_enabled(request),
                experiment_v2_enabled=bool(
                    request.app.state.dispatch_config.experiment_v2_enabled
                ),
                expected_plan_digests=body.expected_plan_digests,
            )
        except ValueError as exc:
            raise _experiment_error(exc) from exc
        return IdempotencyResource("experiment", str(created["experiment_id"]))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    if outcome.resource.resource_type != "experiment":
        raise RuntimeError("experiment request idempotency resource type is invalid")
    result = get_experiment_v2(database, int(outcome.resource.resource_id))
    if result is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Experiment disappeared after commit")
    _no_store(response)
    return {
        "experiment_id": result["experiment_id"],
        "approval_id": result["approval_id"],
        "status": result["status"],
        "run_count": result["run_count"],
        "plan_ids": [member["execution_plan_id"] for member in result["members"]],
        "plan_digests": result["payload"].plan_digests,
        "replayed": outcome.replayed,
    }


@router.get("/experiments")
def list_experiments(
    request: Request,
    response: Response,
    project_id: str = Query(min_length=36, max_length=36),
) -> dict[str, Any]:
    project_id = _canonical_project_id(project_id)
    _require_project_action(
        request,
        Action.PROJECT_VIEW,
        project_id=project_id,
    )
    database = _database(request)
    experiments = list_experiments_v2_for_project(database, project_id)
    _no_store(response)
    return {
        "items": [_experiment_projection(entry) for entry in experiments],
    }


@router.get("/experiments/{experiment_id}")
def get_experiment(
    experiment_id: int,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    if experiment_id < 1:
        raise _not_found()
    database = _database(request)
    scope = _experiment_scope_or_not_found(database, experiment_id)
    _require_project_action(
        request,
        Action.PROJECT_VIEW,
        project_id=scope["project_id"],
    )
    result = get_experiment_v2(database, experiment_id)
    if result is None:  # pragma: no cover - same resolved row
        raise _not_found()
    _no_store(response)
    return _experiment_projection(result)


def _experiment_projection(result: dict[str, Any]) -> dict[str, Any]:
    payload = result["payload"]
    return {
        "experiment_id": result["experiment_id"],
        "project_id": result["project_id"],
        "approval_id": result["approval_id"],
        "status": result["status"],
        "run_count": result["run_count"],
        "matrix": payload.matrix.model_dump(mode="json"),
        "guard": payload.guard.model_dump(mode="json"),
        "members": [
            {
                "execution_plan_id": member["execution_plan_id"],
                "job_id": member["job_id"],
                "plan_digest": member["plan_digest"],
                "server_name": member["server_name"],
                "parameter_values": member["parameter_values"],
                "canonical_job_status": member["canonical_job_status"],
                "collection_state": member["collection_state"],
                "metrics_status": member["metrics_status"],
                "metrics_summary": member["metrics_summary"],
            }
            for member in result["members"]
        ],
    }


def handle_experiment_v2_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    if not bool(request.app.state.dispatch_config.experiment_v2_enabled):
        raise _not_found()
    if approval.kind != EXPERIMENT_V2_APPROVAL_KIND or not isinstance(
        approval.payload, dict
    ):
        raise _not_found()
    raw_project_id = approval.payload.get("project_id")
    if not isinstance(raw_project_id, str):
        raise _not_found()
    project_id = _canonical_project_id(raw_project_id)
    context = _require_project_action(
        request,
        Action.APPROVAL_DECIDE,
        project_id=project_id,
        requester_actor_id=approval.requester_actor_id,
        high_risk=True,
    )
    database = _database(request)
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"approval_id": approval.id},
        query={},
    )
    materialized: dict[str, Any] = {}

    def decide(cursor: Any) -> IdempotencyResource:
        nonlocal materialized
        try:
            if body.decision == "approve":
                materialized = apply_experiment_v2_decision_in_transaction(
                    database,
                    cursor,
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    sharing_enabled=_sharing_enabled(request),
                    note=body.note,
                )
            else:
                materialized = reject_experiment_v2_decision_in_transaction(
                    database,
                    cursor,
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _experiment_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    if outcome.replayed:
        # A true replay never re-invokes `decide()`, so `materialized` above
        # is unpopulated -- rebuild the response from durable state, exactly
        # `handle_product_stop_decision`'s approach for its own replay path.
        replayed = get_experiment_v2_by_approval(database, approval.id)
        if replayed is None:
            raise RuntimeError("Experiment decision result is unavailable")
        materialized = {
            "approval_id": approval.id,
            "experiment_id": replayed["experiment_id"],
            "status": replayed["status"],
            "job_ids": [member["job_id"] for member in replayed["members"]],
            "created": False,
        }
    _no_store(response)
    return {**materialized, "replayed": outcome.replayed}


__all__ = [
    "EXPERIMENT_DETAIL_ROUTE",
    "EXPERIMENT_LIST_ROUTE",
    "EXPERIMENT_PREVIEW_ROUTE",
    "EXPERIMENT_REQUEST_ROUTE",
    "handle_experiment_v2_decision",
    "router",
]
