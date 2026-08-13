"""Product v2 caller identity, session, and My Workspace read models."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, cast

from fastapi import APIRouter, Depends, Query, Request

from app.authorization import (
    Action,
    ResourceScope,
    evaluate_enforced_authorization,
    resolve_approval_resource,
)
from app.authorization_shadow import HIGH_RISK_APPROVAL_KINDS
from app.db import Approval, Database
from app.identity import (
    ActorType,
    ActorSession,
    ProjectRole,
    ProjectRoleV2,
    RequestContext,
    parse_session_token,
    verify_secret,
)
from app.product_run_store import (
    get_product_stop_request_result,
    list_product_run_summaries,
)
from dispatch_center.api.errors import APIError
from dispatch_center.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_CURSOR_BYTES,
    MAX_PAGE_LIMIT,
    decode_cursor,
    encode_cursor,
    pagination_query_sha256,
)
from dispatch_center.api.v2 import API_V2_PREFIX, api_v2_feature_gate


ME_ROUTE = "/api/v2/me"
SESSIONS_ROUTE = "/api/v2/me/sessions"
WORKSPACE_ROUTE = "/api/v2/workspace"
SESSION_SORT = "created_at:desc,internal_session_id:desc,offset-v1"
WORKSPACE_PROJECT_LIMIT = 100
WORKSPACE_RECENT_LIMIT = 10

_ROLE_ORDER = {
    ProjectRoleV2.OWNER: 0,
    ProjectRoleV2.OPERATOR: 1,
    ProjectRoleV2.REVIEWER: 2,
    ProjectRoleV2.DATASET_MANAGER: 3,
    ProjectRoleV2.VIEWER: 4,
}
_LEGACY_ROLE_MAPPING = {
    ProjectRole.ADMIN: (
        ProjectRoleV2.OWNER,
        ProjectRoleV2.OPERATOR,
        ProjectRoleV2.REVIEWER,
        ProjectRoleV2.DATASET_MANAGER,
    ),
    ProjectRole.OPERATOR: (ProjectRoleV2.OPERATOR,),
    ProjectRole.VIEWER: (ProjectRoleV2.VIEWER,),
}
_FEATURE_KEYS = (
    "api_v2",
    "product_rbac_v2",
    "project_bootstrap_v2",
    "project_environments_v1",
    "run_template_v2",
    "run_experience_v2",
    "dataset_assets_v2",
    "dataset_sharing_v2",
    "dataset_publish_v2",
    "oidc",
    "legacy_shared_token",
    "service_token_auth",
    "authorization_mode",
)

router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[Depends(api_v2_feature_gate)],
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


def _effective_roles(context: RequestContext) -> dict[str, tuple[ProjectRoleV2, ...]]:
    roles: defaultdict[str, set[ProjectRoleV2]] = defaultdict(set)
    if context.project_roles_v2_enabled:
        for binding in context.project_role_bindings:
            if binding.active and binding.actor_id == context.actor_id:
                roles[binding.project_id].add(binding.role)
    else:
        for membership in context.project_memberships:
            if membership.actor_id != context.actor_id:
                continue
            roles[membership.project_id].update(
                _LEGACY_ROLE_MAPPING[membership.role]
            )
    return {
        project_id: tuple(sorted(project_roles, key=_ROLE_ORDER.__getitem__))
        for project_id, project_roles in sorted(roles.items())
    }


def _me_payload(context: RequestContext, config: Any) -> dict[str, Any]:
    actor = cast(Any, context.actor)
    roles = _effective_roles(context)
    return {
        "actor": {
            "id": actor.id,
            "type": actor.actor_type.value,
            "display_name": actor.display_name,
            "platform_admin": bool(actor.platform_admin),
        },
        "authentication": {
            "method": context.authentication_method,
            "oidc_enabled": bool(config.oidc_enabled),
        },
        "authorization": {
            "mode": config.authorization_mode,
            "role_model": (
                "product_rbac_v2"
                if context.project_roles_v2_enabled
                else "legacy_membership_adapter"
            ),
        },
        "project_roles": [
            {
                "project_id": project_id,
                "roles": [role.value for role in project_roles],
            }
            for project_id, project_roles in roles.items()
        ],
        "service_scopes": sorted(context.service_scopes),
    }


def _parse_timestamp(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _session_state(session: ActorSession, now: datetime) -> str:
    if session.revoked_at is not None:
        return "revoked"
    expires_at = _parse_timestamp(session.expires_at)
    if expires_at is None or expires_at <= now:
        return "expired"
    return "active"


def _session_payload(
    session: ActorSession,
    *,
    current_session_id: str | None,
    now: datetime,
) -> dict[str, Any]:
    state = _session_state(session, now)
    return {
        "created_at": session.created_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "state": state,
        "current": state == "active" and session.id == current_session_id,
        "authentication_source": (
            "oidc" if session.oidc_identity_id is not None else "session"
        ),
    }


def _verified_current_session_id(
    request: Request,
    database: Database,
    context: RequestContext,
) -> str | None:
    if context.authentication_method != "session" or context.actor_id is None:
        return None
    config = request.app.state.dispatch_config
    raw_session = request.cookies.get(config.session_cookie_name)
    if not isinstance(raw_session, str):
        return None
    try:
        session_id, _ = parse_session_token(raw_session)
        session = database.get_actor_session(session_id)
    except (TypeError, ValueError):
        return None
    if (
        session is None
        or session.actor_id != context.actor_id
        or session.revoked_at is not None
        or not verify_secret(raw_session, session.secret_hash)
    ):
        return None
    return session.id


def _approval_targets(
    database: Database,
    approval: Approval,
) -> tuple[tuple[ResourceScope, str | None], ...]:
    payload = approval.payload if isinstance(approval.payload, dict) else {}
    project_ref = next(
        (
            payload.get(key)
            for key in ("project", "project_name", "project_id")
            if payload.get(key) is not None
        ),
        None,
    )
    project = (
        database.get_project(project_ref) if isinstance(project_ref, str) else None
    )
    job_id = payload.get("job_id")
    job = (
        database.get_job(job_id)
        if isinstance(job_id, int) and not isinstance(job_id, bool)
        else None
    )
    job_project = (
        database.get_project(job.project)
        if job is not None and isinstance(job.project, str)
        else None
    )
    resolution = resolve_approval_resource(
        approval.id,
        approval,
        project=project,
        job=job,
        job_project=job_project,
    )
    if resolution.scope is None:
        return ()
    if resolution.scope is ResourceScope.GLOBAL:
        return ((ResourceScope.GLOBAL, None),)
    return tuple(
        (ResourceScope.PROJECT, project_id)
        for project_id in resolution.project_ids
    )


def _can_use_approval(
    database: Database,
    context: RequestContext,
    approval: Approval,
    targets: Iterable[tuple[ResourceScope, str | None]],
    action: Action,
) -> tuple[bool, str]:
    target_alias = False
    if approval.kind == "dataset_alias_change_v2" and isinstance(
        approval.payload, dict
    ):
        asset_id = approval.payload.get("asset_id")
        project_id = approval.payload.get("project_id")
        owner = (
            database.get_dataset_asset_owner_project_id(asset_id)
            if isinstance(asset_id, str)
            else None
        )
        target_alias = (
            owner is not None
            and isinstance(project_id, str)
            and owner != project_id
        )
    if target_alias or approval.kind in {
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
    }:
        project_ids = {
            project_id
            for scope, project_id in targets
            if scope is ResourceScope.PROJECT and project_id is not None
        }
        roles = {
            binding.role
            for binding in context.project_role_bindings
            if binding.active
            and binding.actor_id == context.actor_id
            and binding.project_id in project_ids
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
    decisions = tuple(
        evaluate_enforced_authorization(
            context,
            action,
            project_id=project_id,
            resource_scope=scope,
            requester_actor_id=approval.requester_actor_id,
            high_risk=approval.kind in HIGH_RISK_APPROVAL_KINDS,
        )
        for scope, project_id in targets
    )
    allowed = next((decision for decision in decisions if decision.allowed), None)
    if allowed is not None:
        return True, allowed.reason.value
    if decisions:
        return False, decisions[0].reason.value
    return False, "resource_unresolved"


def _feature_capability_payload(config: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    report = config.settings.feature_report()
    features = {
        key: {
            "value": report[key]["value"],
            "default": report[key]["default"],
            "rollout_state": report[key]["rollout_state"],
        }
        for key in _FEATURE_KEYS
        if (
            key
            not in {
                "dataset_sharing_v2",
                "dataset_publish_v2",
                "run_experience_v2",
            }
            or bool(getattr(config, f"{key}_enabled", False))
        )
    }
    capabilities = {
        "my_workspace": {
            "implemented": True,
            "enabled": bool(config.api_v2_enabled),
            "state": "available",
        },
        "product_rbac_v2": {
            "implemented": True,
            "enabled": bool(config.product_rbac_v2_enabled),
            "state": (
                "available" if config.product_rbac_v2_enabled else "disabled"
            ),
        },
        "project_bootstrap_v2": {
            "implemented": True,
            "enabled": bool(config.project_bootstrap_v2_enabled),
            "state": (
                "available"
                if config.project_bootstrap_v2_enabled
                else "disabled"
            ),
        },
        "project_environments_v1": {
            "implemented": True,
            "enabled": bool(config.project_environments_v1_enabled),
            "state": (
                "available"
                if config.project_environments_v1_enabled
                else "disabled"
            ),
        },
        "run_template_v2": {
            "implemented": True,
            "enabled": bool(config.run_template_v2_enabled),
            "state": (
                "available" if config.run_template_v2_enabled else "disabled"
            ),
        },
        "oidc_login": {
            "implemented": True,
            "enabled": bool(config.oidc_enabled),
            "state": "available" if config.oidc_enabled else "disabled",
        },
        "recent_runs": {
            "implemented": True,
            "enabled": True,
            "state": (
                "execution_plan_product_projection"
                if config.run_experience_v2_enabled
                else "legacy_job_adapter"
            ),
        },
        "dataset_assets": {
            "implemented": True,
            "enabled": bool(config.dataset_assets_v2_enabled),
            "state": (
                "available" if config.dataset_assets_v2_enabled else "disabled"
            ),
        },
        "run_experience_v2": {
            "implemented": True,
            "enabled": bool(config.run_experience_v2_enabled),
            "state": (
                "available" if config.run_experience_v2_enabled else "disabled"
            ),
        },
    }
    if config.dataset_sharing_v2_enabled:
        capabilities["dataset_sharing"] = {
            "implemented": True,
            "enabled": True,
            "state": "available",
        }
    if config.dataset_publish_v2_enabled:
        capabilities["dataset_publish"] = {
            "implemented": True,
            "enabled": True,
            "state": "available",
        }
    return features, capabilities


@router.get("/me")
def get_me(request: Request) -> dict[str, Any]:
    context = _request_context(request)
    return _me_payload(context, request.app.state.dispatch_config)


@router.get("/me/sessions")
def list_my_sessions(
    request: Request,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_BYTES),
) -> dict[str, Any]:
    context = _request_context(request)
    actor_id = cast(str, context.actor_id)
    query_sha256 = pagination_query_sha256(
        actor_id=actor_id,
        route_template=SESSIONS_ROUTE,
        sort_contract=SESSION_SORT,
        filters={},
    )
    offset = 0
    if cursor is not None:
        decoded = decode_cursor(
            cursor,
            expected_query_sha256=query_sha256,
            key_types=(int,),
        )
        offset = cast(int, decoded[0])
        if offset < 0:
            raise APIError(
                code="invalid_cursor",
                message="The pagination cursor is invalid",
                status_code=400,
            )

    database = _database(request)
    sessions = database.list_actor_sessions_page(
        actor_id=actor_id,
        offset=offset,
        limit_plus_one=limit + 1,
    )
    visible = sessions[:limit]
    next_cursor = (
        encode_cursor((offset + limit,), query_sha256=query_sha256)
        if len(sessions) > limit
        else None
    )
    current_session_id = _verified_current_session_id(request, database, context)
    now = datetime.now(timezone.utc)
    return {
        "items": [
            _session_payload(
                session,
                current_session_id=current_session_id,
                now=now,
            )
            for session in visible
        ],
        "next_cursor": next_cursor,
        "remote_revocation_supported": False,
    }


@router.get("/workspace")
def get_workspace(request: Request) -> dict[str, Any]:
    context = _request_context(request)
    config = request.app.state.dispatch_config
    database = _database(request)
    effective_roles = _effective_roles(context)

    authorized_projects = []
    for project in database.list_projects():
        if project.id is None:
            continue
        decision = evaluate_enforced_authorization(
            context,
            Action.PROJECT_VIEW,
            project_id=project.id,
        )
        if not decision.allowed:
            continue
        authorized_projects.append(project)
    authorized_projects.sort(
        key=lambda project: (project.name, cast(str, project.id))
    )
    project_ids_by_name = {
        project.name: cast(str, project.id) for project in authorized_projects
    }
    visible_projects = authorized_projects[:WORKSPACE_PROJECT_LIMIT]

    if config.run_experience_v2_enabled:
        recent_run_summaries = list_product_run_summaries(
            database,
            project_ids=sorted(project_ids_by_name.values()),
            limit=WORKSPACE_RECENT_LIMIT,
        )
    else:
        recent_runs = [
            job
            for job in database.list_jobs()
            if isinstance(job.project, str) and job.project in project_ids_by_name
        ]
        recent_runs.sort(key=lambda job: (job.created_at, job.id), reverse=True)
        recent_run_summaries = [
            {
                "id": job.id,
                "project_id": project_ids_by_name[cast(str, job.project)],
                "project_name": job.project,
                "type": job.type,
                "status": job.status,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "source": "legacy_job",
            }
            for job in recent_runs[:WORKSPACE_RECENT_LIMIT]
        ]

    pending_approval_summaries = []
    for approval in database.list_approvals(status="pending"):
        if (
            approval.kind == "environment_change_v2"
            and not config.project_environments_v1_enabled
        ):
            continue
        if (
            approval.kind
            in {"run_template_change_v2", "project_defaults_change_v2"}
            and not config.run_template_v2_enabled
        ):
            continue
        if (
            approval.kind
            in {"dataset_asset_adoption_v2", "dataset_alias_change_v2"}
            and not config.dataset_assets_v2_enabled
        ):
            continue
        if (
            approval.kind == "dataset_publish_v2"
            and not config.dataset_publish_v2_enabled
        ):
            continue
        if (
            approval.kind == "execution_plan_v2"
            and not config.run_experience_v2_enabled
        ):
            continue
        if approval.kind == "stop" and isinstance(approval.payload, dict):
            product_stop = approval.payload.get("source") == "product_v2"
            if product_stop and (
                not config.run_experience_v2_enabled
                or get_product_stop_request_result(database, approval.id) is None
            ):
                continue
        if (
            approval.kind == "dataset_alias_change_v2"
            and config.dataset_assets_v2_enabled
            and not config.dataset_sharing_v2_enabled
            and isinstance(approval.payload, dict)
        ):
            asset_id = approval.payload.get("asset_id")
            project_id = approval.payload.get("project_id")
            owner = (
                database.get_dataset_asset_owner_project_id(asset_id)
                if isinstance(asset_id, str)
                else None
            )
            if owner is None or owner != project_id:
                continue
        if (
            approval.kind
            in {
                "dataset_share_offer_v2",
                "dataset_share_accept_v2",
                "dataset_grant_revoke_v2",
            }
            and not config.dataset_sharing_v2_enabled
        ):
            continue
        targets = _approval_targets(database, approval)
        can_view, _ = _can_use_approval(
            database,
            context,
            approval,
            targets,
            Action.APPROVAL_VIEW,
        )
        if not can_view:
            continue
        can_decide, decide_reason = _can_use_approval(
            database,
            context,
            approval,
            targets,
            Action.APPROVAL_DECIDE,
        )
        project_ids = sorted(
            project_id
            for scope, project_id in targets
            if scope is ResourceScope.PROJECT and project_id is not None
        )
        pending_approval_summaries.append(
            {
                "id": approval.id,
                "kind": approval.kind,
                "project_id": project_ids[0] if len(project_ids) == 1 else None,
                "created_at": approval.created_at,
                "requester_is_self": approval.requester_actor_id == context.actor_id,
                "can_decide": can_decide,
                "decision_reason": decide_reason,
            }
        )
    pending_approval_summaries.sort(
        key=lambda approval: (approval["created_at"], approval["id"]),
        reverse=True,
    )
    features, capabilities = _feature_capability_payload(config)
    recent_dataset_assets: list[dict[str, Any]] = []
    if config.dataset_assets_v2_enabled:
        for project in authorized_projects:
            recent_dataset_assets.extend(
                database.list_project_dataset_assets_page(
                    project_id=cast(str, project.id),
                    after=None,
                    limit_plus_one=WORKSPACE_RECENT_LIMIT + 1,
                    sharing_enabled=bool(config.dataset_sharing_v2_enabled),
                )
            )
        recent_dataset_assets.sort(
            key=lambda asset: (
                asset["created_at"],
                asset["asset_id"],
                asset["scope_project_id"],
            ),
            reverse=True,
        )

    return {
        "me": _me_payload(context, config),
        "projects": [
            {
                "id": project.id,
                "name": project.name,
                "summary": project.summary,
                "created_at": project.created_at,
                "roles": [
                    role.value
                    for role in effective_roles.get(cast(str, project.id), ())
                ],
            }
            for project in visible_projects
        ],
        "recent_runs": recent_run_summaries,
        "pending_approvals": pending_approval_summaries[:WORKSPACE_RECENT_LIMIT],
        "recent_dataset_assets": {
            "items": recent_dataset_assets[:WORKSPACE_RECENT_LIMIT],
            "state": (
                "available" if config.dataset_assets_v2_enabled else "disabled"
            ),
        },
        "features": features,
        "capabilities": capabilities,
    }


__all__ = [
    "ME_ROUTE",
    "SESSIONS_ROUTE",
    "SESSION_SORT",
    "WORKSPACE_ROUTE",
    "router",
]
