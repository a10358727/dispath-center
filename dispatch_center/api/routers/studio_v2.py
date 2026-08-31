"""Studio session routes (DG-AGENT-RUNTIME-V3 / DG-STUDIO-UI v1).

Thin REST surface over `app.agent_gateway.AgentGateway`: open a session (an
`agent_session_open` approval card pinned to an enrolled runner agent), start
it on the runner, send messages, answer workspace permission prompts, read
the persisted event log, fetch a diff, interrupt or close. The live event
stream is the WebSocket in `app.main` (`/api/v2/studio/sessions/{id}/stream`).
Every route 404s while `AGENT_RUNTIME_V3_ENABLED` is off.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.agent_attachments import InvalidAttachmentError
from app.agent_session_options import InvalidSessionOptionsError
from app.agent_gateway import ensure_agent_gateway
from app.approvals import (
    InvalidAgentSessionRequestError,
    approval_to_dict,
    request_agent_session_checkpoint_approval,
    request_agent_session_open_approval,
)
from app.audit import audit_actor_from_request_context
from dispatch_center.api.errors import APIError
from dispatch_center.api.v2 import API_V2_PREFIX, api_v2_feature_gate, product_rbac_v2_feature_gate

STUDIO_OPEN_ROUTE = "/api/v2/studio/projects/{name}/sessions/open-requests"
STUDIO_SESSION_ROUTE = "/api/v2/studio/sessions/{session_id}"

router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[Depends(api_v2_feature_gate), Depends(product_rbac_v2_feature_gate)],
)


class StudioOpenRequest(BaseModel):
    base_version_id: str = Field(min_length=1, max_length=64)
    runner_id: str = Field(min_length=1, max_length=64)
    #: DG-STUDIO-UI v1 Phase 2: model / effort / thinking / permission_mode
    #: (closed vocabulary, validated by `app.agent_session_options`).
    options: Optional[dict[str, Any]] = None
    #: P2-4: branch off an existing session's SDK conversation.
    fork_from_session_id: Optional[str] = Field(default=None, max_length=64)


class StudioConfigureRequest(BaseModel):
    model: Optional[str] = Field(default=None, max_length=80)
    permission_mode: Optional[str] = Field(default=None, max_length=32)


class StudioAttachment(BaseModel):
    type: str = Field(pattern="^image$")
    media_type: str = Field(max_length=64)
    data_base64: str = Field(max_length=6 * 1024 * 1024)


class StudioMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=65536)
    attachments: Optional[list[StudioAttachment]] = Field(default=None, max_length=4)


class StudioPermissionDecision(BaseModel):
    decision: str = Field(pattern="^(allow|deny)$")
    allow_pattern: Optional[str] = Field(default=None, max_length=256)


def _runtime(request: Request) -> Any:
    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    if not bool(getattr(app_state.config, "agent_runtime_v3_enabled", False)):
        raise APIError(code="agent_runtime_disabled", message="Studio session 功能未啟用", status_code=404)
    return app_state


def _session_or_404(app_state: Any, session_id: str) -> Any:
    session = app_state.db.get_agent_session(session_id)
    if session is None:
        raise APIError(code="not_found", message="session 不存在", status_code=404)
    return session


def session_to_dict(app_state: Any, session: Any) -> dict[str, Any]:
    runtime = app_state.db.get_agent_session_runtime(session.id) or {}
    gateway = ensure_agent_gateway(app_state)
    return {
        "id": session.id,
        "project_id": session.project_id,
        "provider_id": session.provider_id,
        "status": session.status,
        "base_version_id": session.base_version_id,
        "workspace_branch": session.workspace_branch,
        "created_at": session.created_at,
        "last_used_at": session.last_used_at,
        "closed_at": session.closed_at,
        "runtime": {
            "runner_id": runtime.get("runner_id"),
            "runner_connected": bool(runtime.get("runner_id") and runtime["runner_id"] in gateway.connected_runner_ids()),
            "task_state": runtime.get("task_state"),
            "sdk_session_id": runtime.get("sdk_session_id"),
            "cost_usd": runtime.get("cost_usd"),
            "last_seq": runtime.get("last_seq", 0),
            "options": runtime.get("options") or {},
        },
        "pending_permissions": app_state.db.list_agent_permission_requests(session.id),
    }


@router.post("/studio/projects/{name}/sessions/open-requests", status_code=202)
async def open_session_request(name: str, body: StudioOpenRequest, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    try:
        approval = request_agent_session_open_approval(
            app_state.db,
            name,
            base_version_id=body.base_version_id,
            config=app_state.config,
            agent_provider_id="claude-agent-sdk",
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
            runner_id=body.runner_id,
            options=body.options,
            fork_from_session_id=body.fork_from_session_id,
        )
    except (InvalidAgentSessionRequestError, ValueError) as exc:
        raise APIError(code="invalid_session_request", message=str(exc), status_code=400) from exc
    return {"approval": approval_to_dict(approval)}


@router.get("/studio/cost-summary")
async def cost_summary(request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    return app_state.db.agent_session_cost_summary()


@router.get("/studio/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    return session_to_dict(app_state, _session_or_404(app_state, session_id))


@router.get("/studio/sessions/{session_id}/events")
async def list_events(session_id: str, request: Request, after_seq: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    events = app_state.db.list_agent_session_events(session_id, after_seq=after_seq, limit=limit)
    return {"session_id": session_id, "events": events, "next_after_seq": events[-1]["seq"] if events else after_seq}


@router.post("/studio/sessions/{session_id}/start")
async def start_session(session_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    gateway = ensure_agent_gateway(app_state)
    context = request.state.request_context
    try:
        params = await gateway.open_session(
            session_id,
            actor_id=getattr(context, "actor_id", None),
            audit_actor=audit_actor_from_request_context(context),
        )
    except LookupError as exc:
        raise APIError(code="not_found", message=str(exc), status_code=404) from exc
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    opened = {k: v for k, v in params.items() if k != "session_id"}
    if "mcp" in opened:
        # never echo the bearer to the browser
        opened["mcp"] = {k: v for k, v in opened["mcp"].items() if k != "token"}
    return {"session_id": session_id, "opened": opened}


@router.post("/studio/sessions/{session_id}/messages", status_code=202)
async def send_message(session_id: str, body: StudioMessageRequest, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    context = request.state.request_context
    try:
        await ensure_agent_gateway(app_state).send_message(
            session_id,
            body.text,
            actor_id=getattr(context, "actor_id", None),
            attachments=[item.model_dump() for item in body.attachments] if body.attachments else None,
        )
    except InvalidAttachmentError as exc:
        raise APIError(code="invalid_attachment", message=str(exc), status_code=400) from exc
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    return {"session_id": session_id, "accepted": True}


@router.post("/studio/sessions/{session_id}/configure")
async def configure_session(session_id: str, body: StudioConfigureRequest, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    if not changes:
        raise APIError(code="invalid_session_options", message="nothing to change", status_code=400)
    try:
        options = await ensure_agent_gateway(app_state).configure_session(
            session_id, changes, actor_id=getattr(request.state.request_context, "actor_id", None)
        )
    except InvalidSessionOptionsError as exc:
        raise APIError(code="invalid_session_options", message=str(exc), status_code=400) from exc
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    return {"session_id": session_id, "options": options}


@router.post("/studio/sessions/{session_id}/interrupt", status_code=202)
async def interrupt_session(session_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    try:
        await ensure_agent_gateway(app_state).interrupt(session_id)
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    return {"session_id": session_id, "accepted": True}


@router.post("/studio/sessions/{session_id}/close")
async def close_session(session_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    await ensure_agent_gateway(app_state).close_session(session_id, reason="closed from Studio")
    return session_to_dict(app_state, _session_or_404(app_state, session_id))


@router.get("/studio/sessions/{session_id}/files")
async def session_files(session_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    try:
        result = await ensure_agent_gateway(app_state).request_files(session_id)
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    return {"session_id": session_id, **result}


@router.post("/studio/sessions/{session_id}/checkpoint-requests", status_code=202)
async def request_checkpoint(session_id: str, request: Request) -> dict[str, Any]:
    """Phase 1b: a Studio session feeds the unchanged promotion chain — this
    creates the same `agent_session_checkpoint` card (bundle -> verify ->
    `engineering_task_promote`) against the runner-agent worktree."""

    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    try:
        approval = request_agent_session_checkpoint_approval(
            app_state.db,
            session_id,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except (InvalidAgentSessionRequestError, ValueError) as exc:
        raise APIError(code="invalid_checkpoint_request", message=str(exc), status_code=400) from exc
    return {"approval": approval_to_dict(approval)}


@router.get("/studio/sessions/{session_id}/diff")
async def session_diff(session_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    try:
        result = await ensure_agent_gateway(app_state).request_diff(session_id)
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    return {"session_id": session_id, **result}


@router.post("/studio/sessions/{session_id}/permissions/{request_id}/decision")
async def decide_permission(session_id: str, request_id: str, body: StudioPermissionDecision, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    _session_or_404(app_state, session_id)
    context = request.state.request_context
    try:
        changed = await ensure_agent_gateway(app_state).decide_permission(
            session_id,
            request_id,
            allow=body.decision == "allow",
            allow_pattern=body.allow_pattern,
            actor=audit_actor_from_request_context(context),
            actor_id=getattr(context, "actor_id", None),
        )
    except LookupError as exc:
        raise APIError(code="not_found", message=str(exc), status_code=404) from exc
    except ConnectionError as exc:
        raise APIError(code="runner_not_connected", message=str(exc), status_code=409) from exc
    if not changed:
        raise APIError(code="already_decided", message="這個權限提示已經決定過", status_code=409)
    return {"session_id": session_id, "request_id": request_id, "decision": body.decision}


__all__ = ["STUDIO_OPEN_ROUTE", "STUDIO_SESSION_ROUTE", "router", "session_to_dict"]
