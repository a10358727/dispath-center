"""Hardware track read surface (DG-HARDWARE-EXECUTION v1, P2).

`GET /api/v2/projects/{project_id}/hardware-images` lists the images a
project's ``build`` runs registered (H-3): digest, size, kind, the build plan
and job that produced them, and whether a human marked them known-good. It
is metadata only — image bytes never leave Server A through this API.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from app.authorization import (
    Action,
    AuthorizationReason,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.db import Database
from app.identity import RequestContext
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

HARDWARE_IMAGE_LIST_ROUTE = "/api/v2/projects/{project_id}/hardware-images"
HARDWARE_IMAGE_LIST_SORT = "registered_at:desc,id:desc"

_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)

router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[Depends(api_v2_feature_gate), Depends(product_rbac_v2_feature_gate)],
)


def _database(request: Request) -> Database:
    database = getattr(request.app.state, "dispatch_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Product v2 database state is unavailable")
    return database


def _not_found() -> APIError:
    return APIError(code="not_found", message="Resource not found", status_code=404)


def _canonical_uuid(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise _not_found() from None
    if normalized != value:
        raise _not_found()
    return normalized


def _require_project_view(request: Request, *, project_id: str) -> RequestContext:
    context = getattr(request.state, "request_context", None)
    if not isinstance(context, RequestContext) or context.actor is None:
        raise APIError(
            code="authentication_required",
            message="Authentication is required",
            status_code=401,
        )
    decision = evaluate_enforced_authorization(
        context,
        Action.PROJECT_VIEW,
        project_id=project_id,
        resource_scope=ResourceScope.PROJECT,
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


@router.get("/projects/{project_id}/hardware-images")
def list_project_hardware_images(
    project_id: str,
    request: Request,
    response: Response,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_project_view(request, project_id=project_id)
    database = _database(request)
    if database.get_project(project_id) is None:
        raise _not_found()
    query_sha256 = pagination_query_sha256(
        actor_id=cast(str, context.actor_id),
        route_template=HARDWARE_IMAGE_LIST_ROUTE,
        sort_contract=HARDWARE_IMAGE_LIST_SORT,
        filters={},
    )
    after: tuple[str, str] | None = None
    if cursor is not None:
        after = cast(
            tuple[str, str],
            decode_cursor(cursor, expected_query_sha256=query_sha256, key_types=(str, str)),
        )
    rows = database.list_hardware_images_page(
        project_id=project_id, after=after, limit_plus_one=limit + 1
    )
    page = build_cursor_page(
        rows,
        limit=limit,
        query_sha256=query_sha256,
        cursor_keys=lambda item: (item["registered_at"], item["id"]),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"project_id": project_id, "items": list(page.items), "next_cursor": page.next_cursor}
