"""Always-authorized Product v2 approval review surfaces."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from app.approval_presentation import describe_approval
from app.approvals import approval_to_dict
from app.authorization import (
    Action,
    ResourceScope,
    evaluate_enforced_authorization,
)
from app.authorization_shadow import HIGH_RISK_APPROVAL_KINDS, resolve_shadow_targets
from app.db import Approval, Database, TRANSACTION_ONLY_APPROVAL_KINDS, VALID_APPROVAL_KINDS
from app.execution_contract import canonical_json_sha256
from app.execution_plan_v2_store import get_verified_execution_plan_v2_approval
from app.experiment_v2_store import get_experiment_v2_by_approval
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
#: DG-UI-UNIFICATION v1 U1: the `kind` list filter accepts any
#: `VALID_APPROVAL_KINDS` member (not just the typed-contract subset) so no
#: kind is unreachable from the Product v2 Workspace list. Validated against
#: `app.db.VALID_APPROVAL_KINDS` at call time instead of a hand-maintained
#: `Literal` to avoid the two drifting apart.
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
    if approval.kind == "enqueue":
        if not isinstance(approval.payload, dict) or "project" not in approval.payload:
            raise _not_found()
        project_ref = approval.payload.get("project")
        if project_ref is None:
            return ResourceScope.GLOBAL, None
        if not isinstance(project_ref, str):
            raise _not_found()
        project = database.get_project(project_ref)
        if project is None or not isinstance(project.id, str):
            raise _not_found()
        return ResourceScope.PROJECT, project.id
    if approval.kind == "project_bootstrap_v2":
        return ResourceScope.GLOBAL, None
    if approval.kind == "stop":
        scope = get_product_stop_approval_scope(database, approval.id)
        if scope is None:
            raise _not_found()
        return ResourceScope.PROJECT, scope["project_id"]
    if approval.kind in {
        "project_role_change",
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
        "experiment_create_v2",
        "project_instance_update_v2",
    }:
        if not isinstance(approval.payload, dict):
            raise _not_found()
        project_id = approval.payload.get("project_id")
        if not isinstance(project_id, str):
            raise _not_found()
        return ResourceScope.PROJECT, project_id
    #: DG-UI-UNIFICATION v1 U1: every other `VALID_APPROVAL_KINDS` member
    #: (the legacy kinds above) resolves through the exact same
    #: `resolve_shadow_targets("approval", ...)` resolver legacy
    #: `/approve|/reject` already authorizes through in enforce mode
    #: (`app.authorization_enforce.enforce_http_authorization`), so v2 is
    #: never weaker than legacy here. `resolve_approval_resource()` in
    #: `app.authorization` now classifies every previously-unclassified
    #: legacy kind (`engineering_task_retry`/`engineering_task_discard`,
    #: `run_profile_*`, `dispatch_policy_*`, `auto_placement`,
    #: `agent_session_open`/`agent_session_checkpoint`, `engineering_command`,
    #: `plan_run` as project-scoped; `server_bootstrap`, `dataset_prewarm`,
    #: `dataset_snapshot_build` as platform-scoped, mirroring each kind's own
    #: request-route classification). An approval whose kind is still
    #: genuinely unknown to that resolver remains fail-closed to
    #: `ResourceScope.GLOBAL` (platform-admin-only) rather than silently
    #: widening access to an unclassified resource.
    return _legacy_approval_target(approval, database)


def _legacy_approval_target(
    approval: Approval,
    database: Database,
) -> tuple[ResourceScope, str | None]:
    targets, issues = resolve_shadow_targets(
        database,
        resource_kind="approval",
        values={"approval_id": approval.id},
    )
    if issues or not targets:
        return ResourceScope.GLOBAL, None
    target = targets[0]
    return target.scope, target.project_id


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
        if context.actor_type is ActorType.HUMAN and context.platform_admin:
            if (
                action is Action.APPROVAL_DECIDE
                and approval.requester_actor_id == context.actor_id
                and not context.allow_high_risk_self_approval
            ):
                return False, "denied_high_risk_self_decision"
            return True, "allowed_platform_admin"
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
            and not context.allow_high_risk_self_approval
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
    if approval.kind == "experiment_create_v2":
        return bool(config.experiment_v2_enabled)
    if approval.kind == "project_instance_update_v2":
        return bool(config.run_experience_v2_enabled)
    if approval.kind == "stop":
        return bool(config.run_experience_v2_enabled)
    if approval.kind in _DATASET_SHARING_APPROVAL_KINDS:
        return bool(
            config.dataset_assets_v2_enabled
            and config.dataset_sharing_v2_enabled
        )
    return True


_SERVER_CONFIG_KINDS = frozenset(
    {"server_add", "server_update", "server_disable", "server_delete"}
)


def _review_safe_payload(approval: Approval) -> dict[str, Any]:
    """The payload a list card may show.

    Server-config kinds substitute the derived ``review_payload`` so the raw
    ``yaml_after_utf8_b64`` bytes never enter the list; every other kind shows
    the same payload the legacy ``GET /approvals`` list already exposes.
    """

    if approval.kind in _SERVER_CONFIG_KINDS:
        review_payload = approval_to_dict(approval).get("review_payload")
        if isinstance(review_payload, dict):
            return review_payload
        name = (
            approval.payload.get("server_name")
            if isinstance(approval.payload, dict)
            else None
        )
        return {"server_name": name} if isinstance(name, str) else {}
    payload = approval.payload
    return payload if isinstance(payload, dict) else {}


def _safe_summary(
    approval: Approval,
    *,
    context: RequestContext,
    database: Database,
) -> dict[str, Any]:
    _, project_id = _approval_target(approval, database)
    review_safe_payload = _review_safe_payload(approval)
    presentation = describe_approval(approval.kind, review_safe_payload)
    can_decide, decision_reason = _authorization(
        context,
        approval,
        Action.APPROVAL_DECIDE,
        database=database,
    )
    return {
        "id": approval.id,
        "kind": approval.kind,
        #: 整頓 U2: human title + one-line summary only. The list stays
        #: payload-free by ruling (DG-PRODUCT-RBAC-V2: opaque list, authorized
        #: detail) -- `tests/test_project_roles_v2.py`,
        #: `tests/test_project_environments_v1.py` and
        #: `tests/test_dataset_sharing_v2.py` pin that; the Studio fetches the
        #: detail on demand for 完整內容.
        "title": presentation["title"],
        "summary": presentation["summary"],
        "status": approval.status,
        "project_id": project_id,
        "created_at": approval.created_at,
        "decided_at": approval.decided_at,
        #: DG-UI-UNIFICATION v1 U6b: the checkpoint->promote bridge parses
        #: `task_id=<uuid>` out of an approved `agent_session_checkpoint`
        #: approval's `note` (see `app.db.Database.
        #: apply_agent_session_checkpoint_decision()`) -- same descriptive
        #: text already exposed unauthenticated-adjacent by the legacy
        #: `GET /approvals?kind=...` list (`app.approvals.approval_to_dict()`)
        #: and by chat approval cards; never a secret (one-time secrets are
        #: returned once in the decision response, never persisted here).
        "note": approval.note,
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
    kind: str | None = Query(default=None),
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

    try:
        product_approvals = database.list_verified_product_approvals(
            status=status,
            kind=kind,
        )
    except ValueError as exc:
        raise APIError(
            code="invalid_query",
            message="The approval list filter is invalid",
            status_code=400,
        ) from exc

    visible: list[Approval] = []
    for approval in product_approvals:
        if before_id is not None and approval.id >= before_id:
            continue
        if not _kind_visible(request, approval):
            continue
        try:
            can_view, _ = _authorization(
                context,
                approval,
                Action.APPROVAL_VIEW,
                database=database,
            )
        except APIError:
            #: `_approval_target()` raises `_not_found()` for a malformed or
            #: unresolvable row (e.g. a legacy `enqueue` row missing
            #: `project`). A single unreviewable row must not 404 the whole
            #: list — it is simply not visible, exactly like a row a lower
            #: role cannot see.
            continue
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
        or candidate.kind not in VALID_APPROVAL_KINDS
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
    if candidate.kind == "enqueue":
        can_decide, decision_reason = _authorization(
            context,
            candidate,
            Action.APPROVAL_DECIDE,
            database=database,
        )
        payload_digest = canonical_json_sha256(candidate.payload)
        _no_store(response)
        return {
            "id": candidate.id,
            "kind": candidate.kind,
            **describe_approval(candidate.kind, candidate.payload),
            "status": candidate.status,
            "created_at": candidate.created_at,
            "decided_at": candidate.decided_at,
            "requester_actor_id": candidate.requester_actor_id,
            "requester_is_self": candidate.requester_actor_id == context.actor_id,
            "can_decide": can_decide,
            "decision_reason": decision_reason,
            "payload": candidate.payload,
            "payload_digest": payload_digest,
            "payload_contract_version": None,
            "payload_verified": False,
            "review_mode": "compatibility_snapshot",
            "review": {
                "effect": "enqueue_job",
                "snapshot_digest_rechecked_at_decision": True,
                "legacy_unpinned": True,
            },
        }
    if candidate.kind not in TRANSACTION_ONLY_APPROVAL_KINDS and candidate.kind != "stop":
        #: DG-UI-UNIFICATION v1 U1: every remaining legacy kind gets the same
        #: unpinned compatibility-snapshot shape `enqueue` already used above,
        #: generalized. `payload_verified` stays `False` (this is a
        #: best-effort review of a mutable row, not an immutable typed
        #: contract) and the digest is recomputed fresh from the current
        #: payload every read.
        can_decide, decision_reason = _authorization(
            context,
            candidate,
            Action.APPROVAL_DECIDE,
            database=database,
        )
        payload_digest = canonical_json_sha256(candidate.payload)
        _no_store(response)
        return {
            "id": candidate.id,
            "kind": candidate.kind,
            **describe_approval(candidate.kind, _review_safe_payload(candidate)),
            "status": candidate.status,
            "created_at": candidate.created_at,
            "decided_at": candidate.decided_at,
            "requester_actor_id": candidate.requester_actor_id,
            "requester_is_self": candidate.requester_actor_id == context.actor_id,
            "can_decide": can_decide,
            "decision_reason": decision_reason,
            "payload": candidate.payload,
            "payload_digest": payload_digest,
            "payload_contract_version": None,
            "payload_verified": False,
            "review_mode": "compatibility_snapshot",
            "review": {
                "effect": "legacy_approval_decision",
                "snapshot_digest_rechecked_at_decision": True,
                "legacy_unpinned": True,
            },
        }
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
    elif approval.kind == "experiment_create_v2":
        experiment = get_experiment_v2_by_approval(database, approval_id)
        if experiment is None:
            raise APIError(
                code="approval_contract_invalid",
                message="The immutable approval contract could not be verified",
                status_code=409,
            )
        review = {
            "experiment_id": experiment["experiment_id"],
            "run_count": experiment["run_count"],
            "plan_digests": experiment["payload"].plan_digests,
            "members": experiment["members"],
        }
    elif approval.kind == "project_instance_update_v2":
        # The verified payload is identifier/digest-only; retain that contract
        # instead of deriving or displaying the underlying checkout path.
        review = {
            "effect": "checkout_exact_promoted_version",
            "project_version_id": approval.payload["project_version_id"],
            "instance_id": approval.payload["instance_id"],
            "server_name": approval.payload["server_name"],
            "git_commit": approval.payload["git_commit"],
            "preview_digest": approval.payload["preview_digest"],
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
        **describe_approval(approval.kind, approval.payload),
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
