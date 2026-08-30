"""Runner-agent identities (DG-AGENT-RUNTIME-V3 R3, INV-AGENT-1).

`GET /api/v2/agent-runners` lists enrolled/revoked runner agents with their
last-seen time, Agent Card and whether the gateway currently holds a live
outbound connection from them. The two `*-requests` routes only create
pending approval cards (`agent_runner_enroll` / `agent_runner_revoke`); the
credential is generated at approval time and returned exactly once by the
approval decision, never by these routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.approvals import (
    AgentRuntimeDisabledError,
    InvalidAgentRunnerRequestError,
    approval_to_dict,
    request_agent_runner_enroll_approval,
    request_agent_runner_revoke_approval,
)
from dispatch_center.api.errors import APIError
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)

AGENT_RUNNERS_ROUTE = "/api/v2/agent-runners"
AGENT_RUNNER_ENROLL_ROUTE = "/api/v2/agent-runners/enroll-requests"
AGENT_RUNNER_REVOKE_ROUTE = "/api/v2/agent-runners/{runner_id}/revoke-requests"

router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[Depends(api_v2_feature_gate), Depends(product_rbac_v2_feature_gate)],
)


class AgentRunnerEnrollRequest(BaseModel):
    server: str = Field(min_length=1, max_length=64)


def _runtime(request: Request) -> Any:
    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    return app_state


def _connected_runner_ids(app_state: Any) -> set[str]:
    gateway = getattr(app_state, "agent_gateway", None)
    if gateway is None:
        return set()
    try:
        return set(gateway.connected_runner_ids())
    except Exception:  # noqa: BLE001 - liveness is a best-effort projection
        return set()


def runner_to_dict(runner: Any, *, connected: bool) -> dict[str, Any]:
    return {
        "id": runner.id,
        "server": runner.server_name,
        "label": runner.label,
        "status": runner.status,
        "active": runner.is_active,
        "connected": connected,
        "protocol_version": runner.protocol_version,
        "agent_version": runner.agent_version,
        "agent_card": runner.agent_card,
        "last_seen_at": runner.last_seen_at,
        "created_at": runner.created_at,
        "revoked_at": runner.revoked_at,
        "approval_id": runner.approval_id,
    }


def _translate(exc: Exception) -> APIError:
    if isinstance(exc, AgentRuntimeDisabledError):
        return APIError(code="agent_runtime_disabled", message="runner agent 功能未啟用", status_code=404)
    return APIError(code="invalid_agent_runner_request", message=str(exc), status_code=400)


@router.get("/agent-runners")
async def list_agent_runners(request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    connected = _connected_runner_ids(app_state)
    runners = app_state.db.list_agent_runners()
    return {
        "enabled": bool(getattr(app_state.config, "agent_runtime_v3_enabled", False)),
        "agent_runners": [runner_to_dict(runner, connected=runner.id in connected) for runner in runners],
    }


@router.post("/agent-runners/enroll-requests", status_code=202)
async def request_agent_runner_enroll(body: AgentRunnerEnrollRequest, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    try:
        approval = request_agent_runner_enroll_approval(
            app_state.db,
            {"server": body.server},
            config=app_state.config,
            server_configs=app_state.server_configs,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except (AgentRuntimeDisabledError, InvalidAgentRunnerRequestError, ValueError) as exc:
        raise _translate(exc) from exc
    return {"approval": approval_to_dict(approval)}


@router.post("/agent-runners/{runner_id}/revoke-requests", status_code=202)
async def request_agent_runner_revoke(runner_id: str, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    try:
        approval = request_agent_runner_revoke_approval(
            app_state.db,
            {"runner_id": runner_id},
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except (AgentRuntimeDisabledError, InvalidAgentRunnerRequestError, ValueError) as exc:
        raise _translate(exc) from exc
    return {"approval": approval_to_dict(approval)}


__all__ = [
    "AGENT_RUNNERS_ROUTE",
    "AGENT_RUNNER_ENROLL_ROUTE",
    "AGENT_RUNNER_REVOKE_ROUTE",
    "router",
    "runner_to_dict",
]
