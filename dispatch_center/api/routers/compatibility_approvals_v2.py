"""Narrow Product v2 bridge for reviewed legacy compatibility approvals.

The browser remains on the Product Workspace and only calls Product v2 routes.
Legacy rows (`enqueue` and, since DG-UI-UNIFICATION v1 U1, every other
`VALID_APPROVAL_KINDS` member outside the typed-contract subset) are
deliberately presented as unpinned compatibility snapshots, never as
immutable Product contracts.  The decision request must echo the reviewed
snapshot digest, which is revalidated immediately before the existing
`approvals_module.approve()`/`reject()` engine is invoked — the exact same
engine legacy `POST /approve|/reject/{id}` uses, so semantics stay unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app import approvals as approvals_module
from app.approvals import ApprovalNotFoundError, ApprovalNotPendingError
from app.authorization import Action, ResourceScope, evaluate_enforced_authorization
from app.authorization_shadow import HIGH_RISK_APPROVAL_KINDS, resolve_shadow_targets
from app.db import Approval, Database, ONE_TIME_SECRET_APPROVAL_KINDS
from app.execution_contract import canonical_json_sha256
from app.identity import RequestContext
from app.localrun import local_run
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import (
    IdempotencyIdentity,
    IdempotencyOutcome,
    IdempotencyRequestContext,
    IdempotencyResource,
)
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


COMPATIBILITY_PAYLOAD_DIGEST_HEADER = "X-Approval-Payload-Digest"


class _CompatibilityIdempotencyMiss(Exception):
    """Internal rollback signal used to perform a side-effect-free lookup."""


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


def _require_enqueue_decision(
    request: Request,
    approval: Approval,
    database: Database,
) -> RequestContext:
    payload = approval.payload
    if not isinstance(payload, dict) or "project" not in payload:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )
    project_ref = payload.get("project")
    if project_ref is None:
        scope = ResourceScope.GLOBAL
        project_id = None
    else:
        project = (
            database.get_project(project_ref)
            if isinstance(project_ref, str)
            else None
        )
        if project is None or not isinstance(project.id, str):
            raise APIError(
                code="not_found",
                message="Resource not found",
                status_code=404,
            )
        scope = ResourceScope.PROJECT
        project_id = project.id
    context = _request_context(request)
    decision = evaluate_enforced_authorization(
        context,
        Action.APPROVAL_DECIDE,
        project_id=project_id,
        resource_scope=scope,
        requester_actor_id=approval.requester_actor_id,
        high_risk=False,
    )
    if not decision.allowed:
        raise APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
            details={"reason": decision.reason.value},
        )
    return context


def _reviewed_payload_digest(request: Request, approval: Approval) -> str:
    values = request.headers.getlist(COMPATIBILITY_PAYLOAD_DIGEST_HEADER)
    supplied = values[0] if len(values) == 1 else None
    if (
        not isinstance(supplied, str)
        or len(supplied) != 64
        or any(character not in "0123456789abcdef" for character in supplied)
    ):
        raise APIError(
            code="approval_payload_digest_required",
            message="A valid reviewed approval payload digest is required",
            status_code=400,
        )
    current = canonical_json_sha256(approval.payload)
    if supplied != current:
        raise APIError(
            code="approval_payload_changed",
            message="The approval payload changed; refresh and review it again",
            status_code=409,
        )
    return current


def _lookup_idempotency(
    database: Database,
    identity: IdempotencyIdentity,
) -> IdempotencyOutcome | None:
    def missing(_cursor: Any) -> IdempotencyResource:
        raise _CompatibilityIdempotencyMiss

    try:
        with SQLiteUnitOfWork(database) as unit_of_work:
            return unit_of_work.run_idempotent(identity, missing)
    except _CompatibilityIdempotencyMiss:
        return None


def _record_idempotency(
    database: Database,
    identity: IdempotencyIdentity,
    approval_id: int,
) -> IdempotencyOutcome:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run_idempotent(
            identity,
            lambda _cursor: IdempotencyResource("approval", str(approval_id)),
        )


def _decision_response(
    database: Database,
    approval_id: int,
    *,
    replayed: bool,
    job_id: int | None = None,
) -> dict[str, Any]:
    decided = database.get_approval(approval_id)
    if decided is None:  # pragma: no cover - committed resource identity
        raise RuntimeError("compatibility approval disappeared after decision")
    response: dict[str, Any] = {
        "approval_id": approval_id,
        "compatibility": True,
        "replayed": replayed,
        "status": decided.status,
    }
    if job_id is not None:
        response["job_id"] = job_id
    return response


async def handle_compatibility_enqueue_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    database = _database(request)
    context = _require_enqueue_decision(request, approval, database)
    payload_digest = _reviewed_payload_digest(request, approval)
    identity = idempotency.bind(
        body={
            **body.model_dump(mode="json"),
            "expected_payload_digest": payload_digest,
        },
        path={"approval_id": approval.id},
        query={},
    )
    existing = _lookup_idempotency(database, identity)
    if existing is not None:
        if existing.resource != IdempotencyResource("approval", str(approval.id)):
            raise RuntimeError("compatibility idempotency resource mismatch")
        return _decision_response(database, approval.id, replayed=True)

    expected_status = "approved" if body.decision == "approve" else "rejected"
    if approval.status != "pending":
        if (
            approval.status == expected_status
            and approval.decision_actor_id == context.actor_id
        ):
            _record_idempotency(database, identity, approval.id)
            return _decision_response(database, approval.id, replayed=True)
        raise APIError(
            code="approval_decision_conflict",
            message="The approval is no longer pending",
            status_code=409,
        )

    runtime = getattr(request.app.state, "dispatch_runtime", None)
    if runtime is None or getattr(runtime, "db", None) is not database:
        raise RuntimeError("Dispatch runtime state is unavailable")
    job_id: int | None = None
    try:
        if body.decision == "approve":
            result = await approvals_module.approve(
                database,
                approval.id,
                ssh_run=runtime.ssh_run,
                audit_path=runtime.config.audit_path,
                server_configs=runtime.server_configs,
                app_state=runtime,
                approved_by="human",
                note=body.note,
                local_run=local_run,
                request_context=context,
            )
            job = result.get("job")
            if job is not None and isinstance(getattr(job, "id", None), int):
                job_id = job.id
        else:
            approvals_module.reject(
                database,
                approval.id,
                note=body.note,
                audit_path=runtime.config.audit_path,
                request_context=context,
            )
    except ApprovalNotFoundError as exc:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        ) from exc
    except ApprovalNotPendingError as exc:
        raise APIError(
            code="approval_decision_conflict",
            message="The approval is no longer pending",
            status_code=409,
        ) from exc
    except ValueError as exc:
        # #155/#157 follow-up (DG-INFRA-DIRECT-ACTIONS v1, 2026-08-26): an
        # honest failure names *why* -- e.g. a stale server-config snapshot --
        # instead of the generic "could not be applied", which forced the
        # operator to dig through server logs to find the same reason that
        # was already sitting in `details.reason`. The `code` stays stable.
        raise APIError(
            code="compatibility_decision_failed",
            message=f"核准無法套用：{exc}",
            status_code=409,
            details={"reason": str(exc)},
        ) from exc

    outcome = _record_idempotency(database, identity, approval.id)
    return _decision_response(
        database,
        approval.id,
        replayed=outcome.replayed,
        job_id=job_id,
    )


def _require_legacy_decision(
    request: Request,
    approval: Approval,
    database: Database,
) -> RequestContext:
    """Resolve the same scope legacy `/approve|/reject` authorizes through.

    Reuses `resolve_shadow_targets("approval", ...)` directly — the exact
    resolver `app.authorization_enforce.enforce_http_authorization` already
    uses for legacy routes — instead of duplicating the project/platform kind
    classification table. `resolve_approval_resource()` in `app.authorization`
    classifies every `VALID_APPROVAL_KINDS` member (see
    `dispatch_center.api.routers.approvals_v2._approval_target` docstring
    note); any kind still genuinely unclassified there conservatively falls
    back to platform-admin-only (`ResourceScope.GLOBAL`) rather than silently
    widening access.
    """

    targets, issues = resolve_shadow_targets(
        database,
        resource_kind="approval",
        values={"approval_id": approval.id},
    )
    if issues or not targets:
        scope, project_id = ResourceScope.GLOBAL, None
    else:
        target = targets[0]
        scope, project_id = target.scope, target.project_id
    context = _request_context(request)
    decision = evaluate_enforced_authorization(
        context,
        Action.APPROVAL_DECIDE,
        project_id=project_id,
        resource_scope=scope,
        requester_actor_id=approval.requester_actor_id,
        high_risk=approval.kind in HIGH_RISK_APPROVAL_KINDS,
    )
    if not decision.allowed:
        raise APIError(
            code="forbidden",
            message="The requested action is not permitted",
            status_code=403,
            details={"reason": decision.reason.value},
        )
    return context


async def handle_compatibility_legacy_decision(
    *,
    approval: Approval,
    body: ProjectRoleDecisionRequest,
    request: Request,
    idempotency: IdempotencyRequestContext,
) -> dict[str, Any]:
    """Decide any legacy (compatibility-snapshot) approval kind other than
    `enqueue`, through the exact same `approvals_module.approve()`/`reject()`
    engine legacy `POST /approve|/reject/{id}` uses (DG-UI-UNIFICATION v1 U1).

    `ONE_TIME_SECRET_APPROVAL_KINDS` (service_token_issue/node_enroll/
    node_rotate) refuse `approve` here — their successful response carries a
    raw one-time secret this generic review surface has no safe channel to
    display — while `reject` stays allowed, mirroring the legacy UI's
    disabled-approve-button semantics.
    """

    database = _database(request)
    context = _require_legacy_decision(request, approval, database)
    if body.decision == "approve" and approval.kind in ONE_TIME_SECRET_APPROVAL_KINDS:
        raise APIError(
            code="one_time_secret_approval_requires_secure_client",
            message=(
                "原因：此類核准會發放一次性秘密，v2 介面尚無安全顯示通道；"
                "請改用能安全接收一次性 response 的管理 client 核准，或在此拒絕。"
            ),
            status_code=409,
        )
    payload_digest = _reviewed_payload_digest(request, approval)
    identity = idempotency.bind(
        body={
            **body.model_dump(mode="json"),
            "expected_payload_digest": payload_digest,
        },
        path={"approval_id": approval.id},
        query={},
    )
    existing = _lookup_idempotency(database, identity)
    if existing is not None:
        if existing.resource != IdempotencyResource("approval", str(approval.id)):
            raise RuntimeError("compatibility idempotency resource mismatch")
        return _decision_response(database, approval.id, replayed=True)

    expected_status = "approved" if body.decision == "approve" else "rejected"
    if approval.status != "pending":
        if (
            approval.status == expected_status
            and approval.decision_actor_id == context.actor_id
        ):
            _record_idempotency(database, identity, approval.id)
            return _decision_response(database, approval.id, replayed=True)
        raise APIError(
            code="approval_decision_conflict",
            message="The approval is no longer pending",
            status_code=409,
        )

    runtime = getattr(request.app.state, "dispatch_runtime", None)
    if runtime is None or getattr(runtime, "db", None) is not database:
        raise RuntimeError("Dispatch runtime state is unavailable")
    job_id: int | None = None
    try:
        if body.decision == "approve":
            result = await approvals_module.approve(
                database,
                approval.id,
                ssh_run=runtime.ssh_run,
                audit_path=runtime.config.audit_path,
                server_configs=runtime.server_configs,
                app_state=runtime,
                approved_by="human",
                note=body.note,
                local_run=local_run,
                request_context=context,
            )
            job = result.get("job")
            if job is not None and isinstance(getattr(job, "id", None), int):
                job_id = job.id
        else:
            approvals_module.reject(
                database,
                approval.id,
                note=body.note,
                audit_path=runtime.config.audit_path,
                request_context=context,
            )
    except ApprovalNotFoundError as exc:
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        ) from exc
    except ApprovalNotPendingError as exc:
        raise APIError(
            code="approval_decision_conflict",
            message="The approval is no longer pending",
            status_code=409,
        ) from exc
    except ValueError as exc:
        # See the matching branch in `handle_compatibility_enqueue_decision()`
        # above for why the reason is now inline in `message`, not just
        # `details.reason`.
        raise APIError(
            code="compatibility_decision_failed",
            message=f"核准無法套用：{exc}",
            status_code=409,
            details={"reason": str(exc)},
        ) from exc

    outcome = _record_idempotency(database, identity, approval.id)
    return _decision_response(
        database,
        approval.id,
        replayed=outcome.replayed,
        job_id=job_id,
    )


__all__ = [
    "COMPATIBILITY_PAYLOAD_DIGEST_HEADER",
    "handle_compatibility_enqueue_decision",
    "handle_compatibility_legacy_decision",
]
