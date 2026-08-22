"""Product v2 deployment of one promoted version onto one existing instance.

The request surface is intentionally identifier-only.  A preview resolves all
transport and checkout evidence server-side; the immutable approval contract
never exposes or accepts a path, credential, host, or shell fragment.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, field_validator

from app.authorization import Action, AuthorizationReason, ResourceScope, evaluate_enforced_authorization
from app.audit import audit_actor_from_request_context
from app.db import Approval, Database
from app.execution_contract import canonical_json, canonical_json_sha256, utf8_sha256
from app.execution_plan_v2_store import _server_revision_contract
from app.hub import build_deploy_push_command, hub_repo_path, local_deploy_bundle_path
from app.identity import RequestContext
from app.localrun import local_run
from app.server_publication import credential_reference, normalize_target
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import IdempotencyRequestContext, IdempotencyResource, idempotency_context_dependency
from dispatch_center.api.schemas import ProjectRoleDecisionRequest
from dispatch_center.api.v2 import API_V2_PREFIX, api_v2_feature_gate, product_rbac_v2_feature_gate, run_experience_v2_feature_gate
from dispatch_center.infrastructure.db.sqlite import SQLiteUnitOfWork


PROJECT_INSTANCE_UPDATE_APPROVAL_KIND = "project_instance_update_v2"
PROJECT_INSTANCE_UPDATE_CONTRACT_VERSION = "project-instance-update-v1"
INSTANCE_UPDATE_PREVIEW_ROUTE = "/api/v2/projects/{project_id}/instance-update-previews"
INSTANCE_UPDATE_REQUEST_ROUTE = "/api/v2/projects/{project_id}/instance-update-requests"
_OPAQUE = frozenset({AuthorizationReason.DENIED_CROSS_PROJECT, AuthorizationReason.DENIED_PROJECT_MEMBERSHIP_MISSING})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _uuid(value: str, name: str) -> str:
    try:
        result = str(uuid.UUID(value))
    except (TypeError, ValueError):
        raise ValueError(f"{name}_invalid") from None
    if result != value:
        raise ValueError(f"{name}_invalid")
    return result


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name}_invalid")
    return value


def _instance_id(value: str) -> str:
    # Inventory predates Product v2 and uses stable 16-hex opaque IDs; newer
    # schema rows may use canonical UUIDs.  Both are DB-resolved, never paths.
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value):
        return value
    return _uuid(value, "instance_id")


class InstanceUpdatePreviewRequest(_StrictModel):
    project_version_id: str
    instance_id: str

    @field_validator("project_version_id")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _uuid(value, info.field_name)

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        return _instance_id(value)


class InstanceUpdateSubmitRequest(InstanceUpdatePreviewRequest):
    expected_preview_digest: str

    @field_validator("expected_preview_digest")
    @classmethod
    def _preview_digest(cls, value: str) -> str:
        return _digest(value, "expected_preview_digest")


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate), Depends(product_rbac_v2_feature_gate),
        Depends(run_experience_v2_feature_gate),
    ],
)


def _database(request: Request) -> Database:
    database = getattr(request.app.state, "dispatch_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Product v2 database state is unavailable")
    return database


def _context(request: Request) -> RequestContext:
    context = getattr(request.state, "request_context", None)
    if not isinstance(context, RequestContext) or context.actor is None:
        raise APIError(code="authentication_required", message="Authentication is required", status_code=401)
    return context


def _not_found() -> APIError:
    return APIError(code="not_found", message="Resource not found", status_code=404)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _record_decision_idempotency(database: Database, identity: Any, approval_id: int) -> bool:
    """Store/replay a terminal decision receipt without reopening its effect."""

    def receipt(_cursor: Any) -> IdempotencyResource:
        return IdempotencyResource("approval", str(approval_id))
    with SQLiteUnitOfWork(database) as unit:
        return unit.run_idempotent(identity, receipt).replayed


def _validate_runtime_target_binding(
    database: Database, runtime: Any, *, server_name: str, revision_id: str, target_identity_sha256: str
) -> None:
    """Verify the approved revision and current SSH identity address the same target."""

    with database.cursor() as cursor:
        revision = cursor.execute(
            """
            SELECT revision.*, creator.kind AS creator_kind, creator.status AS creator_status,
                   creator.payload AS creator_payload, creator.payload_sha256 AS creator_payload_sha256,
                   creator.payload_contract_version AS creator_contract
            FROM server_config_revisions AS revision JOIN approvals AS creator
              ON creator.id = revision.created_by_approval_id
            WHERE revision.id = ? AND revision.server_name = ?
              AND revision.assignment_eligibility = 'approved'
              AND revision.publication_state = 'active'
              AND revision.attempt_backend_preflight = 'eligible'
              AND revision.target_identity_sha256 = ?
            """, (revision_id, server_name, target_identity_sha256),
        ).fetchone()
        if revision is None:
            raise ValueError("instance_update_target_unavailable")
        try:
            normalized_target, _ = _server_revision_contract(cursor, revision)
        except ValueError as exc:
            raise ValueError("instance_update_target_unavailable") from exc
    cfg = runtime.server_configs.get(server_name)
    if cfg is None or not getattr(cfg, "enabled", False):
        raise ValueError("instance_update_target_unavailable")
    raw = {
        "execution_backend": getattr(cfg, "execution_backend", "ssh"), "host": cfg.host,
        "port": cfg.port, "user": cfg.user, "key": cfg.key,
        "project_roots": list(cfg.project_roots), "dataset_roots": list(cfg.dataset_roots),
    }
    if (
        normalized_target != normalize_target(raw)
        or canonical_json_sha256({"normalized_target": normalize_target(raw), "credential_ref": credential_reference(raw)})
        != target_identity_sha256
    ):
        raise ValueError("instance_update_target_unavailable")


def _require(request: Request, action: Action, project_id: str, *, requester_actor_id: str | None = None) -> RequestContext:
    context = _context(request)
    decision = evaluate_enforced_authorization(
        context, action, project_id=project_id, resource_scope=ResourceScope.PROJECT,
        requester_actor_id=requester_actor_id, high_risk=True,
    )
    if decision.allowed:
        return context
    if decision.reason in _OPAQUE:
        raise _not_found()
    raise APIError(code="forbidden", message="The requested action is not permitted", status_code=403, details={"reason": decision.reason.value})


def _error(exc: ValueError) -> APIError:
    reason = str(exc)
    if reason.endswith("_invalid") or "not_found" in reason:
        return _not_found()
    safe = {
        "instance_update_preview_changed", "instance_update_active_work", "instance_update_instance_unavailable",
        "instance_update_remote_unavailable", "instance_update_remote_dirty", "instance_update_remote_mismatch",
        "instance_update_promotion_invalid", "instance_update_target_unavailable", "instance_update_hub_ref_unavailable",
        "instance_update_recovery_hold", "instance_update_approval_conflict",
    }
    return APIError(code="instance_update_conflict", message="The instance update is not valid for the current state", status_code=409, details={"reason": reason if reason in safe else "not_ready"})


async def _probe(ssh_run: Any, server_name: str, path: str) -> dict[str, Any]:
    # Fixed, read-only command; path originates only from the stored instance.
    command = (
        f"commit=$(git -C {shlex.quote(path)} rev-parse HEAD) && "
        f"branch=$(git -C {shlex.quote(path)} branch --show-current) && "
        f"dirty=$(git -C {shlex.quote(path)} status --porcelain | wc -c) && "
        "printf 'DC_COMMIT:%s\\nDC_BRANCH:%s\\nDC_DIRTY:%s\\n' \"$commit\" \"$branch\" \"$dirty\""
    )
    result = await ssh_run(server_name, command, 20)
    if getattr(result, "exit_status", 1) != 0:
        raise ValueError("instance_update_remote_unavailable")
    fields = {}
    for line in (getattr(result, "stdout", "") or "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value
    commit, branch, dirty = fields.get("DC_COMMIT"), fields.get("DC_BRANCH"), fields.get("DC_DIRTY")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit) or branch is None or not isinstance(dirty, str) or not dirty.strip().isdigit():
        raise ValueError("instance_update_remote_unavailable")
    # An empty DC_BRANCH is the explicit detached-checkout representation.
    return {"commit": commit, "branch": branch, "dirty": int(dirty.strip()) != 0}


async def resolve_instance_update_preview(
    database: Database, *, project_id: str, body: InstanceUpdatePreviewRequest, runtime: Any
) -> dict[str, Any]:
    """Resolve fresh live evidence and return only reviewer-safe fields."""

    with database.cursor() as cursor:
        version = cursor.execute(
            """
            SELECT version.*, approval.payload AS promotion_payload, approval.status AS promotion_status,
                   approval.kind AS promotion_kind, approval.payload_contract_version AS promotion_contract,
                   approval.payload_sha256 AS promotion_payload_sha256
            FROM project_versions AS version JOIN approvals AS approval ON approval.id = version.promotion_approval_id
            WHERE version.id = ? AND version.project_id = ? AND version.promotion_state = 'promoted'
            """, (body.project_version_id, project_id),
        ).fetchone()
        instance = cursor.execute(
            "SELECT * FROM project_instances WHERE id = ? AND project_id = ?", (body.instance_id, project_id)
        ).fetchone()
        if version is None or instance is None:
            raise ValueError("instance_update_instance_unavailable")
        target = cursor.execute(
            """
            SELECT revision.*, creator.kind AS creator_kind, creator.status AS creator_status,
                   creator.payload AS creator_payload, creator.payload_sha256 AS creator_payload_sha256,
                   creator.payload_contract_version AS creator_contract
            FROM server_config_revisions AS revision
            JOIN approvals AS creator ON creator.id = revision.created_by_approval_id
            WHERE revision.server_name = ? AND revision.assignment_eligibility = 'approved'
              AND revision.publication_state = 'active' AND revision.attempt_backend_preflight = 'eligible'
              AND creator.kind IN ('server_add','server_update') AND creator.status = 'approved'
              AND creator.payload_contract_version = 'server-config-v1'
            ORDER BY revision.revision DESC, revision.id ASC
            """, (instance["server"],),
        ).fetchall()
        active = cursor.execute(
            "SELECT 1 FROM jobs WHERE project = ? AND (server = ? OR pin_server = ? OR target_server = ?) "
            "AND status IN ('queued','running','blocked') LIMIT 1",
            (instance["project_name"], instance["server"], instance["server"], instance["server"]),
        ).fetchone()
        if len(target) != 1:
            raise ValueError("instance_update_target_unavailable")
        try:
            normalized_target, _tags = _server_revision_contract(cursor, target[0])
        except ValueError as exc:
            raise ValueError("instance_update_target_unavailable") from exc
    cfg = runtime.server_configs.get(str(instance["server"]))
    if cfg is None or not getattr(cfg, "enabled", False):
        raise ValueError("instance_update_target_unavailable")
    runtime_raw = {
        "execution_backend": getattr(cfg, "execution_backend", "ssh"), "host": cfg.host,
        "port": cfg.port, "user": cfg.user, "key": cfg.key,
        "project_roots": list(cfg.project_roots), "dataset_roots": list(cfg.dataset_roots),
    }
    runtime_identity = canonical_json_sha256({
        "normalized_target": normalize_target(runtime_raw),
        "credential_ref": credential_reference(runtime_raw),
    })
    if normalized_target != normalize_target(runtime_raw) or runtime_identity != target[0]["target_identity_sha256"]:
        raise ValueError("instance_update_target_unavailable")
    if active is not None:
        raise ValueError("instance_update_active_work")
    try:
        promotion_payload = json.loads(version["promotion_payload"])
    except (TypeError, json.JSONDecodeError):
        promotion_payload = None
    if (
        version["promotion_kind"] != "engineering_task_promote"
        or version["promotion_status"] != "approved"
        or version["promotion_contract"] != "code-promotion-v1"
        or not isinstance(version["bundle_sha256"], str)
        or not isinstance(version["promotion_payload_sha256"], str)
        or utf8_sha256(str(version["promotion_payload"])) != version["promotion_payload_sha256"]
        or not isinstance(promotion_payload, dict)
        or canonical_json(promotion_payload) != version["promotion_payload"]
        or promotion_payload.get("project_name") != instance["project_name"]
        or promotion_payload.get("git_commit") != version["git_commit"]
        or promotion_payload.get("bundle_sha256") != version["bundle_sha256"]
    ):
        raise ValueError("instance_update_promotion_invalid")
    hub_ref = version["git_ref"]
    if hub_ref != f"refs/heads/codex-promoted/{body.project_version_id}":
        raise ValueError("instance_update_hub_ref_unavailable")
    repo = hub_repo_path(str(instance["project_name"]), runtime.config.local_home_dir)
    local = await local_run(f"git --git-dir={shlex.quote(repo)} rev-parse --verify {shlex.quote(hub_ref)}", 15)
    if getattr(local, "exit_status", 1) != 0 or (getattr(local, "stdout", "") or "").strip() != version["git_commit"]:
        raise ValueError("instance_update_hub_ref_unavailable")
    live = await _probe(runtime.ssh_run, str(instance["server"]), str(instance["path"]))
    if live["dirty"]:
        raise ValueError("instance_update_remote_dirty")
    if live["commit"] != instance["git_commit"] or live["branch"] != (instance["git_branch"] or ""):
        raise ValueError("instance_update_remote_mismatch")
    before = {
        "project_id": project_id, "instance_id": body.instance_id, "server_name": instance["server"],
        "path_sha256": utf8_sha256(str(instance["path"])), "git_commit": live["commit"],
        "git_branch": live["branch"], "dirty": False,
    }
    checkout_before_digest = utf8_sha256(canonical_json(before))
    payload = {
        "project_id": project_id, "project_version_id": body.project_version_id, "instance_id": body.instance_id,
        "server_config_revision_id": target[0]["id"], "target_identity_sha256": target[0]["target_identity_sha256"], "server_name": instance["server"],
        "git_commit": version["git_commit"], "hub_ref": hub_ref,
        "promotion_approval_id": int(version["promotion_approval_id"]), "promotion_bundle_sha256": version["bundle_sha256"],
        "expected_before_commit": live["commit"], "expected_before_branch": live["branch"] or None,
        "path_sha256": before["path_sha256"], "checkout_before_digest": checkout_before_digest,
    }
    payload["preview_digest"] = utf8_sha256(canonical_json(payload))
    return {"payload": payload, "preview_digest": payload["preview_digest"], "target": {"server_name": instance["server"], "server_config_revision_id": target[0]["id"], "target_identity_sha256": target[0]["target_identity_sha256"]}, "desired": {"project_version_id": body.project_version_id, "git_commit": version["git_commit"]}, "before": {"git_commit": live["commit"], "git_branch": live["branch"] or None, "checkout_digest": checkout_before_digest}}


async def _resulting_state(runtime: Any, *, project_name: str, git_commit: str) -> str:
    """Compare the observed promoted checkout to the live hub HEAD."""

    repo = hub_repo_path(project_name, runtime.config.local_home_dir)
    main = await local_run(
        f"git --git-dir={shlex.quote(repo)} rev-parse --verify HEAD", 15
    )
    if getattr(main, "exit_status", 1) != 0:
        raise ValueError("instance_update_hub_ref_unavailable")
    return "available" if (getattr(main, "stdout", "") or "").strip() == git_commit else "diverged"


@router.post("/projects/{project_id}/instance-update-previews")
async def preview_instance_update(project_id: str, body: InstanceUpdatePreviewRequest, request: Request, response: Response) -> dict[str, Any]:
    try:
        project_id = _uuid(project_id, "project_id")
    except ValueError:
        raise _not_found() from None
    _require(request, Action.PROJECT_OPERATE, project_id)
    runtime = getattr(request.app.state, "dispatch_runtime", None)
    if runtime is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    try:
        result = await resolve_instance_update_preview(_database(request), project_id=project_id, body=body, runtime=runtime)
    except ValueError as exc:
        raise _error(exc) from exc
    _no_store(response)
    return {"contract_version": PROJECT_INSTANCE_UPDATE_CONTRACT_VERSION, "ready": True, **result}


@router.post("/projects/{project_id}/instance-update-requests", status_code=status.HTTP_202_ACCEPTED)
async def request_instance_update(project_id: str, body: InstanceUpdateSubmitRequest, request: Request, response: Response, idempotency: IdempotencyRequestContext = Depends(idempotency_context_dependency)) -> dict[str, Any]:
    try:
        project_id = _uuid(project_id, "project_id")
    except ValueError:
        raise _not_found() from None
    context = _require(request, Action.PROJECT_OPERATE, project_id)
    database = _database(request)
    identity = idempotency.bind(body=body.model_dump(mode="json"), path={"project_id": project_id}, query={})
    # Lookup is deliberately before the live preview: a lost response must not
    # force a caller to reproduce old remote evidence just to get its approval.
    with database.cursor() as cursor:
        prior = cursor.execute(
            """SELECT request_sha256, result_resource_type, result_resource_id, expires_at
               FROM api_idempotency_keys WHERE actor_id = ? AND route_key = ? AND key_sha256 = ?""",
            (identity.actor_id, identity.route_key, identity.key_sha256),
        ).fetchone()
        if prior is not None and str(prior["expires_at"]) <= database._sqlite_now(cursor):
            prior = None
    if prior is not None:
        if prior["request_sha256"] != identity.request_sha256:
            raise APIError(code="idempotency_key_reused", message="Idempotency-Key was already used for another request", status_code=409)
        if prior["result_resource_type"] != "approval":
            raise RuntimeError("Instance update idempotency resource type is invalid")
        approval = database.get_verified_product_approval(int(prior["result_resource_id"]))
        if approval is None or approval.kind != PROJECT_INSTANCE_UPDATE_APPROVAL_KIND:
            raise RuntimeError("Instance update approval disappeared after commit")
        _no_store(response)
        return {"approval_id": approval.id, "status": approval.status, "preview_digest": approval.payload["preview_digest"], "replayed": True}
    runtime = getattr(request.app.state, "dispatch_runtime", None)
    if runtime is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    preview_body = InstanceUpdatePreviewRequest(project_version_id=body.project_version_id, instance_id=body.instance_id)
    try:
        resolved = await resolve_instance_update_preview(_database(request), project_id=project_id, body=preview_body, runtime=runtime)
    except ValueError as exc:
        raise _error(exc) from exc
    if resolved["preview_digest"] != body.expected_preview_digest:
        raise _error(ValueError("instance_update_preview_changed"))
    def create(_cursor: Any) -> IdempotencyResource:
        payload_json = canonical_json(resolved["payload"])
        created_at = database._sqlite_now(_cursor)
        _cursor.execute(
            """INSERT INTO approvals (kind, payload, status, created_at, requester_actor_id,
               payload_sha256, payload_contract_version, payload_immutable_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (PROJECT_INSTANCE_UPDATE_APPROVAL_KIND, payload_json, created_at,
             cast(str, context.actor_id), utf8_sha256(payload_json),
             PROJECT_INSTANCE_UPDATE_CONTRACT_VERSION, created_at),
        )
        approval_id = int(_cursor.lastrowid)
        database._append_approval_created_audit(
            _cursor, approval_id=approval_id, kind=PROJECT_INSTANCE_UPDATE_APPROVAL_KIND,
            requester_actor_id=cast(str, context.actor_id),
        )
        return IdempotencyResource("approval", str(approval_id))
    with SQLiteUnitOfWork(database) as unit:
        outcome = unit.run_idempotent(identity, create)
    approval = database.get_verified_product_approval(int(outcome.resource.resource_id))
    if approval is None or approval.kind != PROJECT_INSTANCE_UPDATE_APPROVAL_KIND:
        raise RuntimeError("Instance update approval contract failed after commit")
    _no_store(response)
    return {"approval_id": approval.id, "status": approval.status, "preview_digest": resolved["preview_digest"], "replayed": outcome.replayed}


async def handle_project_instance_update_decision(*, approval: Approval, body: ProjectRoleDecisionRequest, request: Request, response: Response, idempotency: IdempotencyRequestContext) -> dict[str, Any]:
    database = _database(request)
    try:
        verified = database.get_verified_product_approval(approval.id)
    except (TypeError, ValueError):
        raise _not_found() from None
    if verified is None or verified.kind != PROJECT_INSTANCE_UPDATE_APPROVAL_KIND or not isinstance(verified.payload, dict):
        raise _not_found()
    approval = verified
    payload = verified.payload
    project_id = payload.get("project_id")
    if not isinstance(project_id, str):
        raise _not_found()
    context = _require(request, Action.APPROVAL_DECIDE, project_id, requester_actor_id=approval.requester_actor_id)
    identity = idempotency.bind(body=body.model_dump(mode="json"), path={"approval_id": approval.id}, query={})
    if approval.status != "pending":
        expected_status = "approved" if body.decision == "approve" else "rejected"
        if approval.status != expected_status or approval.decision_actor_id != context.actor_id:
            raise APIError(code="approval_decision_conflict", message="The approval is no longer pending", status_code=409)
        replayed = _record_decision_idempotency(database, identity, approval.id)
        _no_store(response)
        return {"approval_id": approval.id, "status": approval.status, "replayed": replayed}
    if body.decision != "approve":
        try:
            database.finalize_project_instance_update_decision(
                approval_id=approval.id, payload=payload, outcome="rejected", note=body.note,
                decision_actor_id=cast(str, context.actor_id),
                decision_actor_kind=(context.actor.actor_type.value if context.actor is not None else None),
                resulting_state="diverged",
                audit_actor=audit_actor_from_request_context(context),
            )
        except ValueError as exc:
            raise _error(exc) from exc
        replayed = _record_decision_idempotency(database, identity, approval.id)
        _no_store(response)
        return {"approval_id": approval.id, "status": "rejected", "replayed": replayed}
    runtime = getattr(request.app.state, "dispatch_runtime", None)
    if runtime is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    instances = {instance.id: instance for instance in database.list_all_project_instances()}
    instance = instances.get(payload["instance_id"])
    if instance is None:
        raise _not_found()
    if (
        instance.project_id != payload["project_id"]
        or instance.server != payload["server_name"]
        or utf8_sha256(instance.path) != payload["path_sha256"]
    ):
        # The path is never client supplied, but it is still binding evidence
        # for the durable intent.  Do not probe or apply after inventory drift.
        raise _error(ValueError("instance_update_recovery_hold"))
    # A previously-claimed intent is a response-loss recovery path.  It must
    # not be rejected by the old-before snapshot check or replayed blindly.
    if approval.materialization_started_at is not None:
        intent = database.get_project_instance_update_intent(approval.id)
        if intent is None or intent.get("payload_sha256") != approval.payload_sha256:
            raise _error(ValueError("instance_update_recovery_hold"))
        try:
            _validate_runtime_target_binding(
                database, runtime, server_name=payload["server_name"],
                revision_id=payload["server_config_revision_id"],
                target_identity_sha256=payload["target_identity_sha256"],
            )
        except ValueError as exc:
            raise _error(ValueError("instance_update_recovery_hold")) from exc
        try:
            live = await _probe(runtime.ssh_run, payload["server_name"], instance.path)
            if live["commit"] != payload["git_commit"] or live["branch"] != "" or live["dirty"]:
                raise ValueError("instance_update_recovery_hold")
            state = await _resulting_state(runtime, project_name=instance.project_name, git_commit=payload["git_commit"])
            database.finalize_project_instance_update_decision(
                approval_id=approval.id, payload=payload, outcome="applied", note=body.note,
                decision_actor_id=cast(str, context.actor_id),
                decision_actor_kind=(context.actor.actor_type.value if context.actor is not None else None),
                resulting_state=state,
                audit_actor=audit_actor_from_request_context(context),
            )
        except Exception as exc:
            try:
                database.record_project_instance_update_unknown(
                    approval_id=approval.id, payload=payload,
                    audit_actor=audit_actor_from_request_context(context),
                )
            except ValueError:
                pass
            raise _error(ValueError("instance_update_recovery_hold")) from exc
        replayed = _record_decision_idempotency(database, identity, approval.id)
        _no_store(response)
        return {"approval_id": approval.id, "status": "approved", "replayed": replayed}
    # Revalidate all current evidence before crossing the durable boundary.
    try:
        fresh = await resolve_instance_update_preview(database, project_id=project_id, body=InstanceUpdatePreviewRequest(project_version_id=payload["project_version_id"], instance_id=payload["instance_id"]), runtime=runtime)
    except ValueError as exc:
        raise _error(exc) from exc
    if fresh["preview_digest"] != payload.get("preview_digest"):
        raise _error(ValueError("instance_update_preview_changed"))
    repo = hub_repo_path(instance.project_name, runtime.config.local_home_dir)
    pinned = await local_run(
        f"git --git-dir={shlex.quote(repo)} rev-parse --verify {shlex.quote(payload['hub_ref'])}", 15
    )
    if getattr(pinned, "exit_status", 1) != 0 or (getattr(pinned, "stdout", "") or "").strip() != payload["git_commit"]:
        raise _error(ValueError("instance_update_hub_ref_unavailable"))
    # Intent is committed before even the local bundle creation.  From this
    # point any failure is unknown/reconcile-only; no automatic replay occurs.
    try:
        database.begin_project_instance_update_intent(
            approval_id=approval.id, payload=payload,
            decision_actor_id=cast(str, context.actor_id),
            audit_actor=audit_actor_from_request_context(context),
        )
    except ValueError as exc:
        raise _error(exc) from exc
    try:
        bundle = local_deploy_bundle_path(approval.id, runtime.config.local_home_dir)
        made = await local_run(f"git --git-dir={shlex.quote(repo)} bundle create {shlex.quote(bundle)} {shlex.quote(payload['hub_ref'])}", 60)
        if getattr(made, "exit_status", 1) != 0:
            raise ValueError("bundle_create_failed")
        verified_bundle = await local_run(f"git bundle verify {shlex.quote(bundle)}", 30)
        if getattr(verified_bundle, "exit_status", 1) != 0:
            raise ValueError("bundle_verify_failed")
        pushed = await local_run(build_deploy_push_command(approval.id, "pinned", runtime.server_configs[payload["server_name"]], runtime.config.local_home_dir), 60)
        if getattr(pushed, "exit_status", 1) != 0:
            raise ValueError("bundle_push_failed")
        remote = await runtime.ssh_run(
            payload["server_name"],
            f"git -C {shlex.quote(instance.path)} fetch --no-tags \"$HOME/deploy_bundles/{approval.id}.bundle\" {shlex.quote(payload['hub_ref'])} && git -C {shlex.quote(instance.path)} checkout --detach {shlex.quote(payload['git_commit'])}",
            90,
        )
        if getattr(remote, "exit_status", 1) != 0:
            raise ValueError("remote_checkout_failed")
        after = await _probe(runtime.ssh_run, payload["server_name"], instance.path)
        if after["commit"] != payload["git_commit"] or after["branch"] != "" or after["dirty"]:
            raise ValueError("instance_update_remote_mismatch")
        state = await _resulting_state(
            runtime, project_name=instance.project_name, git_commit=payload["git_commit"]
        )
    except Exception as exc:
        database.record_project_instance_update_unknown(
            approval_id=approval.id, payload=payload,
            audit_actor=audit_actor_from_request_context(context),
        )
        raise _error(ValueError("instance_update_recovery_hold")) from exc
    database.finalize_project_instance_update_decision(approval_id=approval.id, payload=payload, outcome="applied", note=body.note, decision_actor_id=cast(str, context.actor_id), decision_actor_kind=(context.actor.actor_type.value if context.actor is not None else None), resulting_state=state, audit_actor=audit_actor_from_request_context(context))
    replayed = _record_decision_idempotency(database, identity, approval.id)
    _no_store(response)
    return {"approval_id": approval.id, "status": "approved", "replayed": replayed}


__all__ = ["INSTANCE_UPDATE_PREVIEW_ROUTE", "INSTANCE_UPDATE_REQUEST_ROUTE", "PROJECT_INSTANCE_UPDATE_APPROVAL_KIND", "PROJECT_INSTANCE_UPDATE_CONTRACT_VERSION", "handle_project_instance_update_decision", "router"]
