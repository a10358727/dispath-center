"""Hardware track surface (DG-HARDWARE-EXECUTION v1, P2/P3).

* `GET /api/v2/projects/{project_id}/hardware-images` — the images a project's
  ``build`` runs registered (H-3): digest, size, kind, the build plan and job
  that produced them, and whether a human marked them known-good. Metadata
  only — image bytes never leave Server A through this API.
* `POST …/hardware-action-previews` / `POST …/hardware-action-requests` —
  a physical action (``program`` / ``power`` / ``hil_test``, H-2): the same
  immutable ExecutionPlan v2 resolution as a run, plus the exact device and
  image pins, filed as a ``hardware_action_v2`` card (transaction-only,
  high-risk, never auto-approved, never in an experiment matrix).
* the ``hardware_action_v2`` decision handler: re-verifies the pins, pushes
  the verified image to the worker by SFTP (never fetched by the worker),
  then materializes the Job through the plan store.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.authorization import (
    Action,
    AuthorizationReason,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.db import Approval, Database
from app.execution_plan_v2_store import (
    apply_execution_plan_v2_decision_in_transaction,
    create_execution_plan_v2_request_in_transaction,
    get_execution_plan_v2_request_result,
    reject_execution_plan_v2_decision_in_transaction,
    resolve_execution_plan_v2,
)
from app.hardware_actions import (
    HARDWARE_ACTION_V2_APPROVAL_KIND,
    HardwareActionV2Request,
    HardwareActionV2SubmitRequest,
    parse_hardware_action_v2_approval_payload,
)
from app.hardware_images import image_store_path
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
from dispatch_center.api.routers.runs_v2 import _execution_plan_error
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
    run_experience_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork

HARDWARE_IMAGE_LIST_ROUTE = "/api/v2/projects/{project_id}/hardware-images"
HARDWARE_IMAGE_LIST_SORT = "registered_at:desc,id:desc"
HARDWARE_ACTION_PREVIEW_ROUTE = "/api/v2/projects/{project_id}/hardware-action-previews"
HARDWARE_ACTION_REQUEST_ROUTE = "/api/v2/projects/{project_id}/hardware-action-requests"

_OPAQUE_PROJECT_DENIALS = frozenset(
    {
        AuthorizationReason.DENIED_CROSS_PROJECT,
        AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING,
    }
)
_HASH_CHUNK = 1024 * 1024

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


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _request_context(request: Request) -> RequestContext:
    context = getattr(request.state, "request_context", None)
    if not isinstance(context, RequestContext) or context.actor is None:
        raise APIError(
            code="authentication_required",
            message="Authentication is required",
            status_code=401,
        )
    return context


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


def _sharing_enabled(request: Request) -> bool:
    return bool(request.app.state.dispatch_config.dataset_sharing_v2_enabled)


# ---------------------------------------------------------------------------
# P2: image registry (read-only)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/hardware-images")
def list_project_hardware_images(
    project_id: str,
    request: Request,
    response: Response,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_project_action(request, Action.PROJECT_VIEW, project_id=project_id)
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
    _no_store(response)
    return {"project_id": project_id, "items": list(page.items), "next_cursor": page.next_cursor}


# ---------------------------------------------------------------------------
# P3: physical actions
# ---------------------------------------------------------------------------


def _pins_view(resolved: dict[str, Any]) -> dict[str, Any]:
    pins = resolved.get("hardware") or {}
    return {
        "action_class": pins.get("action_class"),
        "server_name": pins.get("server_name"),
        "device_id": pins.get("device_id"),
        "device_kind": pins.get("device_kind"),
        "image_sha256": pins.get("image_sha256"),
        "power_sequence": pins.get("power_sequence"),
    }


@router.post(
    "/projects/{project_id}/hardware-action-previews",
    dependencies=[Depends(run_experience_v2_feature_gate)],
)
def preview_hardware_action(
    project_id: str,
    body: HardwareActionV2Request,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Resolve the exact action (plan + physical pins) without persisting state."""

    project_id = _canonical_uuid(project_id)
    context = _require_project_action(request, Action.PROJECT_OPERATE, project_id=project_id)
    try:
        resolved = resolve_execution_plan_v2(
            _database(request),
            project_id=project_id,
            request=body,
            requester_actor_id=cast(str, context.actor_id),
            sharing_enabled=_sharing_enabled(request),
            hardware=body,
        )
    except ValueError as exc:
        raise _execution_plan_error(exc) from exc
    spec = resolved["spec"]
    _no_store(response)
    return {
        "contract_version": spec.contract_version,
        "ready": True,
        "approval_kind": HARDWARE_ACTION_V2_APPROVAL_KIND,
        "plan": spec.model_dump(mode="json"),
        "plan_digest": spec.plan_digest,
        "hardware": _pins_view(resolved),
        "evidence": resolved["evidence"],
    }


@router.post(
    "/projects/{project_id}/hardware-action-requests",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(run_experience_v2_feature_gate)],
)
def request_hardware_action(
    project_id: str,
    body: HardwareActionV2SubmitRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    project_id = _canonical_uuid(project_id)
    context = _require_project_action(request, Action.PROJECT_OPERATE, project_id=project_id)
    database = _database(request)
    request_body = HardwareActionV2Request.model_validate(
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
                hardware=request_body,
            )
        except ValueError as exc:
            raise _execution_plan_error(exc) from exc
        return IdempotencyResource("execution_plan", str(created["execution_plan_id"]))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    if outcome.resource.resource_type != "execution_plan":
        raise RuntimeError("hardware action idempotency resource type is invalid")
    try:
        result = get_execution_plan_v2_request_result(database, outcome.resource.resource_id)
    except ValueError as exc:
        raise _execution_plan_error(exc) from exc
    if result is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("ExecutionPlan disappeared after commit")
    _no_store(response)
    return {**result, "approval_kind": HARDWARE_ACTION_V2_APPROVAL_KIND, "replayed": outcome.replayed}


def _local_image_digest(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


async def _push_verified_image(request: Request, *, server_name: str, sha256: str, remote_path: str) -> None:
    """H-3: Server A re-hashes its stored copy and pushes exactly those bytes.

    The worker never fetches an image itself; a missing or altered local copy
    or a failed push leaves the card pending (409), nothing is materialized.
    """

    config = request.app.state.dispatch_config
    local_path = str(image_store_path(config.local_home_dir, sha256))
    if _local_image_digest(local_path) != sha256:
        raise _execution_plan_error(ValueError("hardware_image_digest_mismatch"))
    runtime = getattr(request.app.state, "dispatch_runtime", None)
    server_cfg = None
    if runtime is not None:
        server_cfg = getattr(runtime, "server_configs", {}).get(server_name)
    if runtime is None or server_cfg is None or getattr(runtime, "ssh_pool", None) is None:
        raise _execution_plan_error(ValueError("hardware_image_push_failed"))
    try:
        await runtime.ssh_pool.put_file(server_cfg, local_path, remote_path)
    except Exception as exc:  # noqa: BLE001 - any transport failure keeps the card pending
        raise _execution_plan_error(ValueError("hardware_image_push_failed")) from exc


async def handle_hardware_action_v2_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    if not bool(request.app.state.dispatch_config.run_experience_v2_enabled):
        raise _not_found()
    if approval.kind != HARDWARE_ACTION_V2_APPROVAL_KIND or not isinstance(
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
    try:
        payload = parse_hardware_action_v2_approval_payload(dict(approval.payload))
    except (TypeError, ValueError) as exc:
        raise _execution_plan_error(ValueError("execution_plan_approval_invalid")) from exc
    if (
        body.decision == "approve"
        and approval.status == "pending"
        and payload.image_sha256 is not None
        and payload.image_remote_path is not None
    ):
        await _push_verified_image(
            request,
            server_name=payload.server_name,
            sha256=payload.image_sha256,
            remote_path=payload.image_remote_path,
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
        try:
            replayed = get_execution_plan_v2_request_result(database, payload.execution_plan_id)
        except ValueError as exc:
            raise _execution_plan_error(exc) from exc
        if replayed is None:
            raise RuntimeError("hardware action decision result is unavailable")
        materialized = {
            "approval_id": approval.id,
            "execution_plan_id": payload.execution_plan_id,
            "status": replayed["status"],
            "job_id": replayed["job_id"],
            "created": False,
        }
    _no_store(response)
    return {**materialized, "approval_kind": HARDWARE_ACTION_V2_APPROVAL_KIND, "replayed": outcome.replayed}


__all__ = [
    "HARDWARE_ACTION_PREVIEW_ROUTE",
    "HARDWARE_ACTION_REQUEST_ROUTE",
    "HARDWARE_IMAGE_LIST_ROUTE",
    "handle_hardware_action_v2_decision",
    "router",
]
