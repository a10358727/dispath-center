"""Product v2 Dataset asset, adoption, alias, lineage, and usage routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable
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
from app.dataset_assets import DatasetAdoptionRequest, DatasetAliasChangeRequest
from app.dataset_publish import (
    DATASET_PUBLISH_APPROVAL_KIND,
    DatasetPublishPreviewRequest,
    DatasetPublishRequest,
    build_dataset_publish_preview,
    execute_dataset_publish_build,
    parse_dataset_publish_payload,
    revalidate_dataset_publish_source,
)
from app.dataset_sharing import (
    DatasetGrantRevokeRequest,
    DatasetShareAcceptRequest,
    DatasetShareOfferRequest,
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
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    dataset_assets_v2_feature_gate,
    dataset_publish_v2_feature_gate,
    dataset_sharing_v2_feature_gate,
    product_rbac_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


DATASET_LIST_ROUTE = "/api/v2/projects/{project_id}/datasets"
DATASET_ADOPTION_REQUEST_ROUTE = (
    "/api/v2/projects/{project_id}/dataset-adoption-requests"
)
DATASET_ASSET_DETAIL_ROUTE = "/api/v2/dataset-assets/{asset_id}"
DATASET_ASSET_LINEAGE_ROUTE = "/api/v2/dataset-assets/{asset_id}/lineage"
DATASET_ASSET_USAGE_ROUTE = "/api/v2/dataset-assets/{asset_id}/usage"
DATASET_ASSET_STORAGE_ROUTE = "/api/v2/dataset-assets/{asset_id}/storage"
DATASET_ALIAS_REQUEST_ROUTE = (
    "/api/v2/dataset-assets/{asset_id}/alias-change-requests"
)
DATASET_SHARE_OFFER_REQUEST_ROUTE = (
    "/api/v2/dataset-assets/{asset_id}/share-offer-requests"
)
DATASET_SHARE_ACCEPT_REQUEST_ROUTE = (
    "/api/v2/dataset-share-offers/{offer_id}/accept-requests"
)
DATASET_GRANT_REVOKE_REQUEST_ROUTE = (
    "/api/v2/dataset-grants/{grant_id}/revoke-requests"
)
DATASET_PUBLISH_PREVIEW_ROUTE = (
    "/api/v2/projects/{project_id}/dataset-publish-previews"
)
DATASET_PUBLISH_REQUEST_ROUTE = (
    "/api/v2/projects/{project_id}/dataset-publish-requests"
)
DATASET_LIST_SORT = "name:asc,asset_id:asc"
DATASET_APPROVAL_KINDS = frozenset(
    {
        "dataset_asset_adoption_v2",
        "dataset_alias_change_v2",
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
        DATASET_PUBLISH_APPROVAL_KIND,
    }
)
DATASET_SHARING_APPROVAL_KINDS = frozenset(
    {
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
    }
)
_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)
_SAFE_LOCATION_SEGMENTS = frozenset(
    {
        "alias_name",
        "asset",
        "body",
        "collection_method",
        "counts",
        "data_card",
        "description",
        "expected_preview_digest",
        "expires_at",
        "expected_asset_digest",
        "expected_grant_digest",
        "expected_head_revision_digest",
        "expected_head_revision_id",
        "expected_revision",
        "license",
        "offer_digest",
        "offer_id",
        "max_depth",
        "max_edges",
        "name",
        "operation",
        "output_declaration_name",
        "path",
        "processing_method",
        "project_id",
        "source_project_id",
        "source",
        "snapshot_id",
        "snapshot_ids",
        "snapshot_set_digest",
        "target_project_id",
    }
)


class DatasetSafeValidationRoute(APIRoute):
    """Discard raw invalid Dataset values from route validation responses."""

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
                        if isinstance(segment, str)
                        and segment in _SAFE_LOCATION_SEGMENTS
                        else "<field>"
                        for segment in error.get("loc", ())[:6]
                    ]
                    errors.append(
                        {
                            "type": "invalid_value",
                            "location": location or ["body"],
                            "message": "A Dataset request value is invalid",
                        }
                    )
                raise APIError(
                    code="invalid_dataset_request",
                    message="The Dataset request is invalid",
                    status_code=422,
                    details={"errors": errors},
                ) from None

        return safe_route_handler


router = APIRouter(
    prefix=API_V2_PREFIX,
    route_class=DatasetSafeValidationRoute,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
        Depends(dataset_assets_v2_feature_gate),
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
    has_project_binding = any(
        binding.active and binding.project_id == project_id
        for binding in context.project_role_bindings
    )
    if decision.reason in _OPAQUE_PROJECT_DENIALS or (
        decision.reason is AuthorizationReason.DENIED_PROJECT_ROLE_INSUFFICIENT
        and not has_project_binding
    ):
        raise _not_found()
    raise APIError(
        code="forbidden",
        message="The requested action is not permitted",
        status_code=403,
        details={"reason": decision.reason.value},
    )


def _require_platform_manage(request: Request) -> RequestContext:
    context = _request_context(request)
    decision = evaluate_enforced_authorization(
        context,
        Action.PLATFORM_MANAGE,
        resource_scope=ResourceScope.GLOBAL,
    )
    if not decision.allowed:
        raise APIError(
            code="forbidden",
            message="Platform administration is required",
            status_code=403,
            details={"reason": decision.reason.value},
        )
    return context


def _require_project_exists(database: Database, project_id: str) -> None:
    if database.get_project(project_id) is None:
        raise _not_found()


def _asset_owner(database: Database, asset_id: str) -> str:
    owner = database.get_dataset_asset_owner_project_id(asset_id)
    if owner is None:
        raise _not_found()
    return owner


def _dataset_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason in {"dataset project not found"}:
        return _not_found()
    if reason == "high_risk_self_decision":
        return APIError(
            code="high_risk_self_decision",
            message="The requester cannot decide this approval",
            status_code=403,
        )
    if "authorized" in reason or "must be human" in reason or "platform admin" in reason:
        return APIError(
            code="forbidden",
            message="The requested Dataset action is not permitted",
            status_code=403,
        )
    if "not pending" in reason:
        return APIError(
            code="approval_not_pending",
            message="The approval is no longer pending",
            status_code=409,
        )
    if "contract" in reason or "payload" in reason or "digest" in reason:
        return APIError(
            code="dataset_contract_invalid",
            message="The immutable Dataset contract could not be verified",
            status_code=409,
        )
    return APIError(
        code="dataset_state_conflict",
        message="The Dataset resource is stale or unavailable",
        status_code=409,
        details={"reason": reason},
    )


def _dataset_publish_error(exc: ValueError) -> APIError:
    """Map publish failures without reflecting a local path or source fact."""

    reason = str(exc)
    if reason == "dataset project not found":
        return _not_found()
    if reason == "high_risk_self_decision":
        return APIError(
            code="high_risk_self_decision",
            message="The requester cannot decide this approval",
            status_code=403,
        )
    if "authorized" in reason or "must be human" in reason:
        return APIError(
            code="forbidden",
            message="The requested Dataset action is not permitted",
            status_code=403,
        )
    if "not pending" in reason:
        return APIError(
            code="approval_not_pending",
            message="The approval is no longer pending",
            status_code=409,
        )
    return APIError(
        code="dataset_publish_conflict",
        message="The Dataset publish source or contract could not be verified",
        status_code=409,
    )


@router.get("/projects/{project_id}/datasets")
def list_project_datasets(
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
        route_template=DATASET_LIST_ROUTE,
        sort_contract=DATASET_LIST_SORT,
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
        rows = database.list_project_dataset_assets_page(
            project_id=project_id,
            after=after,
            limit_plus_one=limit + 1,
            sharing_enabled=bool(
                request.app.state.dispatch_config.dataset_sharing_v2_enabled
            ),
        )
    except ValueError as exc:
        raise _dataset_error(exc) from exc
    page = build_cursor_page(
        rows,
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda item: (item["name"], item["asset_id"]),
    )
    _no_store(response)
    return {
        "project_id": project_id,
        "items": list(page.items),
        "next_cursor": page.next_cursor,
    }


@router.post(
    "/projects/{project_id}/dataset-publish-previews",
    dependencies=[Depends(dataset_publish_v2_feature_gate)],
)
def preview_dataset_publish(
    project_id: str,
    body: DatasetPublishPreviewRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    _require_project_action(
        request,
        Action.DATASET_MANAGE,
        project_id=project_id,
    )
    database = _database(request)
    _require_project_exists(database, project_id)
    try:
        preview, _source_path, _candidate = build_dataset_publish_preview(
            body,
            project_id=project_id,
            database=database,
            config=request.app.state.dispatch_config,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise _dataset_publish_error(exc) from exc
    _no_store(response)
    return preview.model_dump(mode="json")


@router.post(
    "/projects/{project_id}/dataset-publish-requests",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(dataset_publish_v2_feature_gate)],
)
def request_dataset_publish(
    project_id: str,
    body: DatasetPublishRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_project_action(
        request,
        Action.DATASET_MANAGE,
        project_id=project_id,
    )
    database = _database(request)
    _require_project_exists(database, project_id)
    preview_request = DatasetPublishPreviewRequest.model_validate(
        body.model_dump(mode="json", exclude={"expected_preview_digest"})
    )
    try:
        preview, _source_path, _candidate = build_dataset_publish_preview(
            preview_request,
            project_id=project_id,
            database=database,
            config=request.app.state.dispatch_config,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise _dataset_publish_error(exc) from exc
    if preview.preview_digest != body.expected_preview_digest:
        # This check deliberately precedes both idempotency binding and the
        # transaction: stale preview requests must create zero durable rows.
        raise APIError(
            code="dataset_publish_preview_mismatch",
            message="The Dataset publish preview is stale",
            status_code=409,
        )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"project_id": project_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_dataset_publish_approval_in_transaction(
                cursor,
                project_id=project_id,
                preview=preview,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _dataset_publish_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset publish approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "preview_digest": preview.preview_digest,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.post(
    "/projects/{project_id}/dataset-adoption-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_dataset_adoption(
    project_id: str,
    body: DatasetAdoptionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_platform_manage(request)
    database = _database(request)
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"project_id": project_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_dataset_asset_adoption_approval_in_transaction(
                cursor,
                project_id=project_id,
                snapshot_id=body.snapshot_id,
                asset=body.asset,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _dataset_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset adoption approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


def _load_asset_for_read(
    *,
    asset_id: str,
    request: Request,
    project_id: str | None,
) -> tuple[Database, str, str, bool]:
    asset_id = _canonical_uuid(asset_id)
    database = _database(request)
    scope_project_id = (
        _asset_owner(database, asset_id)
        if project_id is None
        else _canonical_uuid(project_id)
    )
    _require_project_action(
        request,
        Action.PROJECT_VIEW,
        project_id=scope_project_id,
    )
    sharing_enabled = bool(
        request.app.state.dispatch_config.dataset_sharing_v2_enabled
    )
    return database, asset_id, scope_project_id, sharing_enabled


@router.get("/dataset-assets/{asset_id}")
def get_dataset_asset(
    asset_id: str,
    request: Request,
    response: Response,
    project_id: str | None = Query(default=None),
) -> dict[str, Any]:
    database, asset_id, scope_project_id, sharing_enabled = _load_asset_for_read(
        asset_id=asset_id,
        request=request,
        project_id=project_id,
    )
    try:
        model = database.get_dataset_asset_read_model(
            asset_id,
            project_id=scope_project_id,
            sharing_enabled=sharing_enabled,
        )
    except ValueError as exc:
        raise _dataset_error(exc) from exc
    if model is None:
        raise _not_found()
    _no_store(response)
    return model


@router.get("/dataset-assets/{asset_id}/lineage")
def get_dataset_asset_lineage(
    asset_id: str,
    request: Request,
    response: Response,
    project_id: str | None = Query(default=None),
    max_depth: int = Query(10, ge=1, le=20),
    max_edges: int = Query(200, ge=1, le=200),
) -> dict[str, Any]:
    database, asset_id, scope_project_id, sharing_enabled = _load_asset_for_read(
        asset_id=asset_id,
        request=request,
        project_id=project_id,
    )
    model = database.get_dataset_asset_lineage(
        asset_id,
        project_id=scope_project_id,
        sharing_enabled=sharing_enabled,
        max_depth=max_depth,
        max_edges=max_edges,
    )
    if model is None:
        raise _not_found()
    _no_store(response)
    return model


@router.get("/dataset-assets/{asset_id}/usage")
def get_dataset_asset_usage(
    asset_id: str,
    request: Request,
    response: Response,
    project_id: str | None = Query(default=None),
) -> dict[str, Any]:
    database, asset_id, scope_project_id, sharing_enabled = _load_asset_for_read(
        asset_id=asset_id,
        request=request,
        project_id=project_id,
    )
    model = database.get_dataset_asset_usage(
        asset_id,
        project_id=scope_project_id,
        sharing_enabled=sharing_enabled,
    )
    if model is None:
        raise _not_found()
    _no_store(response)
    return model


@router.get("/dataset-assets/{asset_id}/storage")
def get_dataset_asset_storage(
    asset_id: str,
    request: Request,
    response: Response,
    project_id: str | None = Query(default=None),
) -> dict[str, Any]:
    database, asset_id, scope_project_id, sharing_enabled = _load_asset_for_read(
        asset_id=asset_id,
        request=request,
        project_id=project_id,
    )
    model = database.get_dataset_asset_storage(
        asset_id,
        project_id=scope_project_id,
        sharing_enabled=sharing_enabled,
    )
    if model is None:
        raise _not_found()
    _no_store(response)
    return model


@router.post(
    "/dataset-assets/{asset_id}/alias-change-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_dataset_alias_change(
    asset_id: str,
    body: DatasetAliasChangeRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    asset_id = _canonical_uuid(asset_id)
    database = _database(request)
    owner = _asset_owner(database, asset_id)
    sharing_enabled = bool(
        request.app.state.dispatch_config.dataset_sharing_v2_enabled
    )
    if body.project_id != owner and not sharing_enabled:
        raise _not_found()
    eligibility = database.get_dataset_snapshot_project_eligibility(
        project_id=body.project_id,
        asset_id=asset_id,
        snapshot_id=body.snapshot_id,
        sharing_enabled=sharing_enabled,
    )
    if eligibility.get("state") not in {"owned_published", "active_grant"}:
        raise _not_found()
    context = _require_project_action(
        request,
        Action.DATASET_MANAGE,
        project_id=body.project_id,
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"asset_id": asset_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_dataset_alias_change_approval_in_transaction(
                cursor,
                project_id=body.project_id,
                asset_id=asset_id,
                operation=body.operation,
                alias_name=body.alias_name,
                snapshot_id=body.snapshot_id,
                expected_revision=body.expected_revision,
                expected_head_revision_id=body.expected_head_revision_id,
                expected_head_revision_digest=body.expected_head_revision_digest,
                requester_actor_id=cast(str, context.actor_id),
                sharing_enabled=sharing_enabled,
            )
        except ValueError as exc:
            raise _dataset_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset alias approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.post(
    "/dataset-assets/{asset_id}/share-offer-requests",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(dataset_sharing_v2_feature_gate)],
)
def request_dataset_share_offer(
    asset_id: str,
    body: DatasetShareOfferRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    asset_id = _canonical_uuid(asset_id)
    database = _database(request)
    owner = _asset_owner(database, asset_id)
    if body.source_project_id != owner:
        raise _not_found()
    context = _require_project_action(
        request,
        Action.DATASET_SHARE,
        project_id=body.source_project_id,
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"asset_id": asset_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_dataset_share_offer_approval_in_transaction(
                cursor,
                asset_id=asset_id,
                request=body,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _dataset_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset share offer approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.post(
    "/dataset-share-offers/{offer_id}/accept-requests",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(dataset_sharing_v2_feature_gate)],
)
def request_dataset_share_accept(
    offer_id: str,
    body: DatasetShareAcceptRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    offer_id = _canonical_uuid(offer_id)
    if body.offer_id != offer_id:
        raise _not_found()
    database = _database(request)
    offer = database.get_dataset_share_offer_read_model(offer_id)
    if offer is None or body.target_project_id != offer["target_project_id"]:
        raise _not_found()
    context = _require_project_action(
        request,
        Action.PROJECT_ADMIN,
        project_id=cast(str, offer["target_project_id"]),
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"offer_id": offer_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_dataset_share_accept_approval_in_transaction(
                cursor,
                offer_id=offer_id,
                request=body,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _dataset_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset share accept approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.post(
    "/dataset-grants/{grant_id}/revoke-requests",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(dataset_sharing_v2_feature_gate)],
)
def request_dataset_grant_revoke(
    grant_id: str,
    body: DatasetGrantRevokeRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    grant_id = _canonical_uuid(grant_id)
    database = _database(request)
    grant = database.get_dataset_grant_read_model(grant_id)
    if grant is None:
        raise _not_found()
    actual_project_id = cast(
        str,
        grant[
            "source_project_id" if body.operation == "revoke" else "target_project_id"
        ],
    )
    if body.project_id != actual_project_id:
        raise _not_found()
    action = Action.DATASET_SHARE if body.operation == "revoke" else Action.PROJECT_ADMIN
    context = _require_project_action(
        request,
        action,
        project_id=actual_project_id,
    )
    identity = idempotency.bind(
        body=body.model_dump(mode="json"),
        path={"grant_id": grant_id},
        query={},
    )

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_dataset_grant_revoke_approval_in_transaction(
                cursor,
                grant_id=grant_id,
                operation=body.operation,
                project_id=body.project_id,
                expected_grant_digest=body.expected_grant_digest,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _dataset_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset grant withdrawal approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


def _load_dataset_approval(
    database: Database,
    approval_id: int,
) -> tuple[Approval, str]:
    approval = database.get_approval(approval_id)
    if (
        approval is None
        or approval.kind not in DATASET_APPROVAL_KINDS
        or not isinstance(approval.payload, dict)
    ):
        raise _not_found()
    project_id = approval.payload.get("project_id")
    if not isinstance(project_id, str):
        raise _not_found()
    return approval, _canonical_uuid(project_id)


def _handle_dataset_publish_decision(
    *,
    verified: Approval,
    project_id: str,
    context: RequestContext,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    identity: Any,
) -> dict[str, Any]:
    database = _database(request)
    if not bool(
        getattr(request.app.state.dispatch_config, "dataset_publish_v2_enabled", False)
    ):
        raise _not_found()
    if not isinstance(verified.payload, dict):
        raise _not_found()
    try:
        payload = parse_dataset_publish_payload(verified.payload)
    except ValueError as exc:
        raise _dataset_publish_error(exc) from exc
    if payload.project_id != project_id:
        raise _not_found()
    materialized: dict[str, Any] = {}

    if body.decision == "approve":
        try:
            _source_path, observed = revalidate_dataset_publish_source(
                payload,
                database=database,
                config=request.app.state.dispatch_config,
            )
        except ValueError as exc:
            raise _dataset_publish_error(exc) from exc

        def decide(cursor: Any) -> IdempotencyResource:
            nonlocal materialized
            try:
                materialized = (
                    database.begin_dataset_publish_decision_in_transaction(
                        cursor,
                        approval_id=verified.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        observed_source_candidate_digest=(
                            observed.source_candidate_digest
                        ),
                        observed_manifest_digest=observed.manifest_digest,
                        note=body.note,
                    )
                )
            except ValueError as exc:
                raise _dataset_publish_error(exc) from exc
            return IdempotencyResource("approval", str(verified.id))

    else:

        def decide(cursor: Any) -> IdempotencyResource:
            try:
                database.reject_dataset_publish_decision_in_transaction(
                    cursor,
                    approval_id=verified.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
            except ValueError as exc:
                raise _dataset_publish_error(exc) from exc
            return IdempotencyResource("approval", str(verified.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    decided = database.get_approval(verified.id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset publish approval disappeared after decision")
    if body.decision == "approve":
        if outcome.replayed:
            try:
                replayed = database.get_dataset_publish_materialization(verified.id)
            except ValueError as exc:
                raise _dataset_publish_error(exc) from exc
            if replayed is None:
                raise _dataset_publish_error(
                    ValueError("dataset publish materialization is unavailable")
                )
            materialized = replayed
        if materialized.get("state") == "building":
            try:
                materialized = execute_dataset_publish_build(
                    database,
                    approval_id=verified.id,
                    config=request.app.state.dispatch_config,
                )
            except ValueError as exc:
                raise _dataset_publish_error(exc) from exc
    _no_store(response)
    return {
        "approval_id": verified.id,
        "replayed": outcome.replayed,
        "status": decided.status,
        **materialized,
    }


def handle_dataset_approval_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    config = request.app.state.dispatch_config
    if not bool(config.dataset_assets_v2_enabled):
        raise _not_found()
    database = _database(request)
    approval, project_id = _load_dataset_approval(database, approval.id)
    if (
        approval.kind in DATASET_SHARING_APPROVAL_KINDS
        and not bool(config.dataset_sharing_v2_enabled)
    ):
        raise _not_found()
    if approval.kind == "dataset_alias_change_v2":
        asset_id = approval.payload.get("asset_id")
        if not isinstance(asset_id, str):
            raise _not_found()
        owner = database.get_dataset_asset_owner_project_id(asset_id)
        if owner is None:
            raise _not_found()
        if project_id != owner and not bool(config.dataset_sharing_v2_enabled):
            raise _not_found()
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
        raise _dataset_error(ValueError("dataset approval contract is invalid")) from exc
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
    if verified.kind == DATASET_PUBLISH_APPROVAL_KIND:
        return _handle_dataset_publish_decision(
            verified=verified,
            project_id=project_id,
            context=context,
            body=body,
            request=request,
            response=response,
            identity=identity,
        )
    materialized: dict[str, Any] = {}

    def decide(_cursor: Any) -> IdempotencyResource:
        nonlocal materialized
        try:
            if verified.kind in DATASET_SHARING_APPROVAL_KINDS:
                if body.decision == "reject":
                    database.reject_dataset_sharing_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
                elif verified.kind == "dataset_share_offer_v2":
                    materialized = database.apply_dataset_share_offer_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
                elif verified.kind == "dataset_share_accept_v2":
                    materialized = database.apply_dataset_share_accept_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
                else:
                    materialized = database.apply_dataset_grant_revoke_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
            elif verified.kind == "dataset_asset_adoption_v2":
                if body.decision == "approve":
                    materialized = database.apply_dataset_asset_adoption_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
                else:
                    database.reject_dataset_asset_adoption_decision(
                        approval_id=approval.id,
                        decision_actor_id=cast(str, context.actor_id),
                        decision_mechanism="product_rbac_v2",
                        note=body.note,
                    )
            elif body.decision == "approve":
                materialized = database.apply_dataset_alias_change_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
            else:
                database.reject_dataset_alias_change_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="product_rbac_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _dataset_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    decided = database.get_approval(approval.id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("Dataset approval disappeared after decision")
    if outcome.replayed and body.decision == "approve":
        if verified.kind == "dataset_share_offer_v2":
            target_offer = verified.payload.get("target_offer", {})
            materialized = {
                **target_offer,
                "payload_digest": verified.payload_sha256,
            }
        elif verified.kind == "dataset_share_accept_v2":
            offer = verified.payload.get("offer", {})
            grants = verified.payload.get("grants", [])
            materialized = {
                "offer_id": offer.get("offer_id"),
                "asset_id": offer.get("asset_id"),
                "source_project_id": offer.get("source_project_id"),
                "target_project_id": offer.get("target_project_id"),
                "grant_ids": [
                    grant.get("grant_id")
                    for grant in grants
                    if isinstance(grant, dict)
                ],
                "snapshot_ids": offer.get("snapshot_ids"),
                "payload_digest": verified.payload_sha256,
            }
        elif verified.kind == "dataset_grant_revoke_v2":
            materialized = {
                "grant_id": verified.payload.get("grant_id"),
                "operation": verified.payload.get("operation"),
                "project_id": verified.payload.get("project_id"),
                "revoked_at": decided.decided_at,
                "payload_digest": verified.payload_sha256,
            }
        elif verified.kind == "dataset_asset_adoption_v2":
            target_asset = verified.payload.get("target_asset", {})
            materialized = {
                "asset_id": target_asset.get("asset_id"),
                "snapshot_id": verified.payload.get("snapshot_id"),
            }
        else:
            target_revision = verified.payload.get("target_revision", {})
            materialized = {
                "asset_id": verified.payload.get("asset_id"),
                "alias_revision_id": target_revision.get("revision_id"),
                "revision": target_revision.get("revision"),
                "snapshot_id": target_revision.get("snapshot_id"),
            }
    _no_store(response)
    return {
        "approval_id": approval.id,
        "replayed": outcome.replayed,
        "status": decided.status,
        **materialized,
    }


__all__ = [
    "DATASET_ADOPTION_REQUEST_ROUTE",
    "DATASET_ALIAS_REQUEST_ROUTE",
    "DATASET_APPROVAL_KINDS",
    "DATASET_GRANT_REVOKE_REQUEST_ROUTE",
    "DATASET_SHARE_ACCEPT_REQUEST_ROUTE",
    "DATASET_SHARE_OFFER_REQUEST_ROUTE",
    "DATASET_SHARING_APPROVAL_KINDS",
    "DATASET_ASSET_DETAIL_ROUTE",
    "DATASET_ASSET_LINEAGE_ROUTE",
    "DATASET_ASSET_STORAGE_ROUTE",
    "DATASET_ASSET_USAGE_ROUTE",
    "DATASET_LIST_ROUTE",
    "DATASET_PUBLISH_PREVIEW_ROUTE",
    "DATASET_PUBLISH_REQUEST_ROUTE",
    "handle_dataset_approval_decision",
    "router",
]
