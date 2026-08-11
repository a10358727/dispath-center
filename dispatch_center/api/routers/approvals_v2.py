"""Always-authorized Product v2 approval review surfaces."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from app.authorization import (
    Action,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.authorization_shadow import HIGH_RISK_APPROVAL_KINDS
from app.db import Approval, Database
from app.execution_plan_v2_store import get_verified_execution_plan_v2_approval
from app.identity import ActorType, ProjectRoleV2, RequestContext
from app.product_run_store import (
    get_product_stop_approval_scope,
    get_product_stop_request_result,
)
from dispatch_center.api.errors import APIError
from dispatch_center.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_CURSOR_BYTES,
    MAX_PAGE_LIMIT,
    build_cursor_page,
    decode_cursor,
    pagination_query_sha256,
)
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)


APPROVAL_LIST_ROUTE = "/api/v2/approvals"
APPROVAL_DETAIL_ROUTE = "/api/v2/approvals/{approval_id}"
APPROVAL_LIST_SORT = "approval_id:desc"
ProductApprovalKind = Literal[
    "project_role_change",
    "project_bootstrap_v2",
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
    "stop",
]
ApprovalStatus = Literal["pending", "approved", "rejected"]
_DATASET_SHARING_APPROVAL_KINDS = frozenset(
    {
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
        "dataset_publish_v2",
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


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _not_found() -> APIError:
    return APIError(
        code="not_found",
        message="Resource not found",
        status_code=404,
    )


def _approval_target(
    approval: Approval,
    database: Database,
) -> tuple[ResourceScope, str | None]:
    if approval.kind == "project_bootstrap_v2":
        return ResourceScope.GLOBAL, None
    if approval.kind == "stop":
        scope = get_product_stop_approval_scope(database, approval.id)
        if scope is None:
            raise _not_found()
        return ResourceScope.PROJECT, scope["project_id"]
    if approval.kind not in {
        "project_role_change",
        "environment_change_v2",
        "run_template_change_v2",
        "project_defaults_change_v2",
        "dataset_asset_adoption_v2",
        "dataset_alias_change_v2",
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
        "execution_plan_v2",
    } or not isinstance(
        approval.payload,
        dict,
    ):
        raise _not_found()
    project_id = approval.payload.get("project_id")
    if not isinstance(project_id, str):
        raise _not_found()
    return ResourceScope.PROJECT, project_id


def _authorization(
    context: RequestContext,
    approval: Approval,
    action: Action,
    *,
    database: Database,
) -> tuple[bool, str]:
    scope, project_id = _approval_target(approval, database)
    target_alias = False
    if approval.kind == "dataset_alias_change_v2" and isinstance(
        approval.payload, dict
    ):
        asset_id = approval.payload.get("asset_id")
        owner = (
            database.get_dataset_asset_owner_project_id(asset_id)
            if isinstance(asset_id, str)
            else None
        )
        target_alias = owner is not None and owner != project_id
    if approval.kind in _DATASET_SHARING_APPROVAL_KINDS or target_alias:
        roles = {
            binding.role
            for binding in context.project_role_bindings
            if binding.active
            and binding.actor_id == context.actor_id
            and binding.project_id == project_id
        }
        if (
            context.actor_type is not ActorType.HUMAN
            or not roles & {ProjectRoleV2.OWNER, ProjectRoleV2.REVIEWER}
        ):
            return False, "denied_project_role_insufficient"
        if (
            action is Action.APPROVAL_DECIDE
            and approval.requester_actor_id == context.actor_id
        ):
            return False, "denied_high_risk_self_decision"
        return True, "allowed_project_role"
    decision = evaluate_enforced_authorization(
        context,
        action,
        project_id=project_id,
        resource_scope=scope,
        requester_actor_id=approval.requester_actor_id,
        high_risk=approval.kind in HIGH_RISK_APPROVAL_KINDS,
    )
    return decision.allowed, decision.reason.value


def _kind_visible(request: Request, approval: Approval) -> bool:
    config = request.app.state.dispatch_config
    if approval.kind == "project_bootstrap_v2":
        return bool(config.project_bootstrap_v2_enabled)
    if approval.kind == "environment_change_v2":
        return bool(config.project_environments_v1_enabled)
    if approval.kind in {
        "run_template_change_v2",
        "project_defaults_change_v2",
    }:
        return bool(config.run_template_v2_enabled)
    if approval.kind == "dataset_alias_change_v2" and isinstance(
        approval.payload, dict
    ):
        asset_id = approval.payload.get("asset_id")
        project_id = approval.payload.get("project_id")
        owner = (
            _database(request).get_dataset_asset_owner_project_id(asset_id)
            if isinstance(asset_id, str)
            else None
        )
        if owner is None or not isinstance(project_id, str):
            return False
        return bool(
            config.dataset_assets_v2_enabled
            and (
                owner == project_id
                or config.dataset_sharing_v2_enabled
            )
        )
    if approval.kind == "dataset_asset_adoption_v2":
        return bool(config.dataset_assets_v2_enabled)
    if approval.kind == "dataset_publish_v2":
        return bool(
            config.dataset_assets_v2_enabled
            and config.dataset_publish_v2_enabled
        )
    if approval.kind == "execution_plan_v2":
        return bool(config.run_experience_v2_enabled)
    if approval.kind == "stop":
        return bool(config.run_experience_v2_enabled)
    if approval.kind in _DATASET_SHARING_APPROVAL_KINDS:
        return bool(
            config.dataset_assets_v2_enabled
            and config.dataset_sharing_v2_enabled
        )
    return True


def _safe_summary(
    approval: Approval,
    *,
    context: RequestContext,
    database: Database,
) -> dict[str, Any]:
    _, project_id = _approval_target(approval, database)
    can_decide, decision_reason = _authorization(
        context,
        approval,
        Action.APPROVAL_DECIDE,
        database=database,
    )
    return {
        "id": approval.id,
        "kind": approval.kind,
        "status": approval.status,
        "project_id": project_id,
        "created_at": approval.created_at,
        "decided_at": approval.decided_at,
        "requester_is_self": approval.requester_actor_id == context.actor_id,
        "can_decide": can_decide,
        "decision_reason": decision_reason,
        "payload_digest": approval.payload_sha256,
        "payload_contract_version": approval.payload_contract_version,
    }


@router.get("/approvals")
def list_product_approvals(
    request: Request,
    response: Response,
    status: ApprovalStatus | None = Query(default=None),
    kind: ProductApprovalKind | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    """Filter by caller authorization before applying the bounded page."""

    context = _request_context(request)
    database = _database(request)
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=APPROVAL_LIST_ROUTE,
        sort_contract=APPROVAL_LIST_SORT,
        filters={"kind": kind, "status": status},
    )
    before_id: int | None = None
    if cursor is not None:
        decoded = decode_cursor(
            cursor,
            expected_query_sha256=query_sha256,
            key_types=(int,),
        )
        before_id = cast(int, decoded[0])
        if before_id < 1:
            raise APIError(
                code="invalid_cursor",
                message="The pagination cursor is invalid",
                status_code=400,
            )

    visible: list[Approval] = []
    for approval in database.list_verified_product_approvals(
        status=status,
        kind=kind,
    ):
        if before_id is not None and approval.id >= before_id:
            continue
        if not _kind_visible(request, approval):
            continue
        can_view, _ = _authorization(
            context,
            approval,
            Action.APPROVAL_VIEW,
            database=database,
        )
        if can_view:
            visible.append(approval)
    page = build_cursor_page(
        visible[: limit + 1],
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda approval: (approval.id,),
    )
    _no_store(response)
    return {
        "items": [
            _safe_summary(approval, context=context, database=database)
            for approval in page.items
        ],
        "next_cursor": page.next_cursor,
    }


@router.get("/approvals/{approval_id}")
def get_product_approval_detail(
    approval_id: int,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    if approval_id < 1:
        raise _not_found()
    context = _request_context(request)
    database = _database(request)
    try:
        candidate = database.get_approval(approval_id)
    except (TypeError, ValueError):
        raise _not_found() from None
    if (
        candidate is None
        or candidate.kind
        not in {
            "project_role_change",
            "project_bootstrap_v2",
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
            "stop",
        }
        or not _kind_visible(request, candidate)
    ):
        raise _not_found()
    can_view, _ = _authorization(
        context,
        candidate,
        Action.APPROVAL_VIEW,
        database=database,
    )
    if not can_view:
        raise _not_found()
    try:
        approval = database.get_verified_product_approval(approval_id)
    except (TypeError, ValueError):
        raise APIError(
            code="approval_contract_invalid",
            message="The immutable approval contract could not be verified",
            status_code=409,
        ) from None
    if approval is None:
        raise _not_found()
    can_decide, decision_reason = _authorization(
        context,
        approval,
        Action.APPROVAL_DECIDE,
        database=database,
    )
    review: dict[str, Any] | None = None
    if approval.kind == "execution_plan_v2":
        verified = get_verified_execution_plan_v2_approval(database, approval_id)
        if verified is None:
            raise APIError(
                code="approval_contract_invalid",
                message="The immutable approval contract could not be verified",
                status_code=409,
            )
        approval_payload = verified["approval_payload"]
        spec = verified["spec"]
        review = {
            "execution_plan_id": approval_payload.execution_plan_id,
            "plan_digest": approval_payload.plan_digest,
            "contract": spec.model_dump(mode="json"),
        }
    elif approval.kind == "stop":
        stop = get_product_stop_request_result(database, approval_id)
        if stop is None:
            raise APIError(
                code="approval_contract_invalid",
                message="The immutable approval contract could not be verified",
                status_code=409,
            )
        review = {
            "plan_id": stop["plan_id"],
            "job_id": stop["job_id"],
            "attempt_id": stop["attempt_id"],
            "effect": "request_stop",
            "job_terminal_status_unchanged": True,
        }
    _no_store(response)
    detail = {
        "id": approval.id,
        "kind": approval.kind,
        "status": approval.status,
        "created_at": approval.created_at,
        "decided_at": approval.decided_at,
        "requester_actor_id": approval.requester_actor_id,
        "requester_is_self": approval.requester_actor_id == context.actor_id,
        "can_decide": can_decide,
        "decision_reason": decision_reason,
        "payload": approval.payload,
        "payload_digest": approval.payload_sha256,
        "payload_contract_version": approval.payload_contract_version,
        "payload_verified": True,
    }
    if review is not None:
        detail["review"] = review
    return detail


__all__ = [
    "APPROVAL_DETAIL_ROUTE",
    "APPROVAL_LIST_ROUTE",
    "APPROVAL_LIST_SORT",
    "router",
]
