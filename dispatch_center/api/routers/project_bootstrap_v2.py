"""Product v2 Project bootstrap preview, request, and Workspace routes."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Request, Response, status

from app.authorization import Action, ResourceScope, evaluate_enforced_authorization
from app.db import Approval, Database
from app.identity import ActorType, RequestContext
from app.project_bootstrap import (
    ProjectBootstrapPreviewRequest,
    ProjectBootstrapRequest,
    bootstrap_payload_digest,
    build_bootstrap_payload,
    dangerous_setup_reason,
)
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
    product_rbac_v2_feature_gate,
    project_bootstrap_v2_feature_gate,
)
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


BOOTSTRAP_PREVIEW_ROUTE = "/api/v2/projects/bootstrap-previews"
BOOTSTRAP_REQUEST_ROUTE = "/api/v2/projects/bootstrap-requests"
PROJECT_WORKSPACE_ROUTE = "/api/v2/projects/{project_id}/workspace"

_CONFLICT_FINDINGS = frozenset(
    {
        "project_uuid_conflict",
        "project_name_conflict",
        "environment_uuid_conflict",
        "environment_revision_uuid_conflict",
        "run_profile_uuid_conflict",
        "defaults_revision_uuid_conflict",
        "role_binding_uuid_conflict",
    }
)
_READINESS_FINDINGS = frozenset(
    {
        "role_actor_missing",
        "role_actor_disabled",
        "legacy_actor_assignment_forbidden",
        "service_privileged_assignment_forbidden",
        "enabled_human_owner_required",
        "two_approval_capable_humans_required",
    }
)

router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
        Depends(project_bootstrap_v2_feature_gate),
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


def _require_bootstrap_admin(request: Request) -> RequestContext:
    context = _request_context(request)
    actor = context.actor
    decision = evaluate_enforced_authorization(
        context,
        Action.PLATFORM_MANAGE,
        resource_scope=ResourceScope.GLOBAL,
    )
    if (
        not decision.allowed
        or actor is None
        or actor.actor_type is not ActorType.HUMAN
        or actor.disabled_at is not None
        or not actor.platform_admin
    ):
        raise APIError(
            code="forbidden",
            message="Project bootstrap requires an enabled human platform admin",
            status_code=403,
        )
    return context


def _canonical_project_id(value: str) -> str:
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


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _reject_dangerous_setup(command: str) -> None:
    reason = dangerous_setup_reason(command)
    if reason is not None:
        raise APIError(
            code="dangerous_setup_command",
            message="The Environment setup command is not permitted",
            status_code=400,
            details={"reason": reason},
        )


def _bootstrap_error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason == "high_risk_self_decision":
        return APIError(
            code="high_risk_self_decision",
            message="The requester cannot decide this approval",
            status_code=403,
        )
    if reason.startswith("capability_unavailable:"):
        return APIError(
            code="capability_unavailable",
            message="A requested bootstrap capability is not available",
            status_code=409,
            details={"capability": reason.partition(":")[2]},
        )
    if reason.startswith("bootstrap_conflict:"):
        return APIError(
            code="bootstrap_conflict",
            message="Bootstrap resources conflict with current state",
            status_code=409,
            details={"reasons": reason.partition(":")[2].split(",")},
        )
    if reason.startswith("bootstrap_invalid:"):
        return APIError(
            code="bootstrap_invalid",
            message="Bootstrap is not valid for the current actor state",
            status_code=409,
            details={"reasons": reason.partition(":")[2].split(",")},
        )
    if "platform admin" in reason:
        return APIError(
            code="forbidden",
            message="Project bootstrap requires an enabled human platform admin",
            status_code=403,
        )
    if "payload" in reason or "contract" in reason or "canonical" in reason:
        return APIError(
            code="bootstrap_contract_conflict",
            message="The immutable bootstrap contract is invalid",
            status_code=409,
        )
    return APIError(
        code="bootstrap_state_conflict",
        message="The bootstrap approval is no longer valid for current state",
        status_code=409,
        details={"reason": reason},
    )


def _preview_response(
    payload: Any,
    findings: tuple[str, ...],
) -> dict[str, Any]:
    conflicts = sorted(set(findings) & _CONFLICT_FINDINGS)
    readiness_reasons = sorted(set(findings) & _READINESS_FINDINGS)
    dataset_blocked = "dataset_capability_unavailable" in findings
    return {
        "payload": None if dataset_blocked else payload.model_dump(mode="json"),
        "payload_digest": (
            None if dataset_blocked else bootstrap_payload_digest(payload)
        ),
        "blocking": bool(findings),
        "conflicts": conflicts,
        "readiness": {
            "ready": not readiness_reasons,
            "reasons": readiness_reasons,
        },
        "capabilities": {
            "dataset_grants_aliases": {
                "implemented": False,
                "enabled": False,
                "state": "unavailable" if dataset_blocked else "not_requested",
                "reason": "pending_pr_07_pr_08",
            }
        },
        "findings": list(findings),
    }


@router.post("/projects/bootstrap-previews")
def preview_project_bootstrap(
    body: ProjectBootstrapPreviewRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Pure read: construct and inspect immutable bootstrap bytes."""

    _require_bootstrap_admin(request)
    _reject_dangerous_setup(body.environment.setup_command)
    dataset_requested = bool(body.dataset_grants or body.dataset_aliases)
    canonical_input = (
        body.model_copy(update={"dataset_grants": [], "dataset_aliases": []})
        if dataset_requested
        else body
    )
    payload = build_bootstrap_payload(canonical_input)
    findings = set(_database(request).project_bootstrap_findings(payload))
    if dataset_requested:
        findings.add("dataset_capability_unavailable")
    _no_store(response)
    return _preview_response(payload, tuple(sorted(findings)))


@router.post(
    "/projects/bootstrap-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_project_bootstrap(
    body: ProjectBootstrapRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency),
) -> dict[str, Any]:
    context = _require_bootstrap_admin(request)
    if body.payload.dataset_grants or body.payload.dataset_aliases:
        raise APIError(
            code="capability_unavailable",
            message="A requested bootstrap capability is not available",
            status_code=409,
            details={"capability": "dataset_governance"},
        )
    _reject_dangerous_setup(body.payload.environment.setup_command)
    actual_digest = bootstrap_payload_digest(body.payload)
    if actual_digest != body.expected_payload_digest:
        raise APIError(
            code="bootstrap_payload_digest_mismatch",
            message="Bootstrap payload digest changed; preview again",
            status_code=409,
        )
    identity = idempotency.bind(
        body={
            "payload": body.payload.model_dump(mode="json"),
            "expected_payload_digest": body.expected_payload_digest,
        },
        path={},
        query={},
    )
    database = _database(request)

    def create(cursor: Any) -> IdempotencyResource:
        try:
            approval_id = database.create_project_bootstrap_approval_in_transaction(
                cursor,
                payload=body.payload,
                requester_actor_id=cast(str, context.actor_id),
            )
        except ValueError as exc:
            raise _bootstrap_error(exc) from exc
        return IdempotencyResource("approval", str(approval_id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, create)
    approval = database.get_approval(int(outcome.resource.resource_id))
    if approval is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("bootstrap approval disappeared after commit")
    _no_store(response)
    return {
        "approval_id": approval.id,
        "payload_digest": approval.payload_sha256,
        "replayed": outcome.replayed,
        "status": approval.status,
    }


@router.get("/projects/{project_id}/workspace")
def get_project_workspace(
    project_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    project_id = _canonical_project_id(project_id)
    context = _request_context(request)
    decision = evaluate_enforced_authorization(
        context,
        Action.PROJECT_VIEW,
        project_id=project_id,
    )
    if not decision.allowed:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    workspace = _database(request).get_project_workspace_v2(project_id)
    if workspace is None:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    workspace["capability"] = {
        "implemented": True,
        "enabled": True,
        "state": "available",
    }
    run_template_enabled = bool(
        request.app.state.dispatch_config.run_template_v2_enabled
    )
    workspace["run_template_capability"] = {
        "implemented": True,
        "enabled": run_template_enabled,
        "state": "available" if run_template_enabled else "disabled",
    }
    if not run_template_enabled:
        workspace["run_template"] = None
        workspace["defaults"] = None
    _no_store(response)
    return workspace


def handle_project_bootstrap_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    response: Response,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    config = request.app.state.dispatch_config
    if not bool(getattr(config, "project_bootstrap_v2_enabled", False)):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    context = _require_bootstrap_admin(request)
    decision = evaluate_enforced_authorization(
        context,
        Action.APPROVAL_DECIDE,
        resource_scope=ResourceScope.GLOBAL,
        requester_actor_id=approval.requester_actor_id,
        high_risk=True,
    )
    if not decision.allowed:
        raise APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
            details={"reason": decision.reason.value},
        )
    if not isinstance(approval.payload_sha256, str):
        raise APIError(
            code="bootstrap_contract_conflict",
            message="The immutable bootstrap contract is invalid",
            status_code=409,
        )
    identity = idempotency.bind(
        body={
            **body.model_dump(mode="json"),
            "approval_payload_digest": approval.payload_sha256,
        },
        path={"approval_id": approval.id},
        query={},
    )
    database = _database(request)

    def decide(_cursor: Any) -> IdempotencyResource:
        try:
            if body.decision == "approve":
                database.apply_project_bootstrap_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="project_bootstrap_v2",
                    note=body.note,
                )
            else:
                database.reject_project_bootstrap_decision(
                    approval_id=approval.id,
                    decision_actor_id=cast(str, context.actor_id),
                    decision_mechanism="project_bootstrap_v2",
                    note=body.note,
                )
        except ValueError as exc:
            raise _bootstrap_error(exc) from exc
        return IdempotencyResource("approval", str(approval.id))

    with SQLiteUnitOfWork(database) as unit_of_work:
        outcome = unit_of_work.run_idempotent(identity, decide)
    decided = database.get_approval(approval.id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("bootstrap approval disappeared after decision")
    project = approval.payload.get("project")
    project_id = project.get("id") if isinstance(project, dict) else None
    _no_store(response)
    return {
        "approval_id": approval.id,
        "project_id": project_id if decided.status == "approved" else None,
        "replayed": outcome.replayed,
        "status": decided.status,
    }


__all__ = [
    "BOOTSTRAP_PREVIEW_ROUTE",
    "BOOTSTRAP_REQUEST_ROUTE",
    "PROJECT_WORKSPACE_ROUTE",
    "handle_project_bootstrap_decision",
    "router",
]
