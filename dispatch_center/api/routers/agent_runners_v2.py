"""Runner-agent identities (DG-AGENT-RUNTIME-V3 R3, INV-AGENT-1).

`GET /api/v2/agent-runners` lists enrolled/revoked runner agents with their
last-seen time, Agent Card and whether the gateway currently holds a live
outbound connection from them. The two `*-requests` routes only create
pending approval cards (`agent_runner_enroll` / `agent_runner_revoke`); the
credential is generated at approval time and returned exactly once by the
approval decision, never by these routes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.agent_runner_install import (
    DEFAULT_WORKSPACE_ROOT,
    is_managed_install,
    set_claude_token,
    validate_claude_token,
)
from app.audit import append_audit, audit_actor_from_request_context
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
AGENT_RUNNER_CLAUDE_TOKEN_ROUTE = "/api/v2/agent-runners/{runner_id}/claude-token"

router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[Depends(api_v2_feature_gate), Depends(product_rbac_v2_feature_gate)],
)


class AgentRunnerInstallSpec(BaseModel):
    """DG-AGENT-RUNNER-INSTALL-v1: platform-managed install at approval time."""

    workspace_root: str = Field(default=DEFAULT_WORKSPACE_ROOT, min_length=2, max_length=200)


class AgentRunnerEnrollRequest(BaseModel):
    server: str = Field(min_length=1, max_length=64)
    label: Optional[str] = Field(default=None, max_length=64)
    install: Optional[AgentRunnerInstallSpec] = None


class AgentRunnerClaudeTokenRequest(BaseModel):
    #: Bounded manually in the handler (no pydantic length constraint) so a
    #: validation error can never echo the submitted secret back.
    token: str


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


def _managed_runner_ids(app_state: Any, runners: list[Any]) -> set[str]:
    """Runners whose enrol card carried a platform-managed install spec."""

    managed: set[str] = set()
    for runner in runners:
        if runner.approval_id is None:
            continue
        approval = app_state.db.get_approval(runner.approval_id)
        if approval is not None and is_managed_install(approval.payload):
            managed.add(runner.id)
    return managed


def runner_to_dict(runner: Any, *, connected: bool, managed: bool = False) -> dict[str, Any]:
    return {
        "id": runner.id,
        "server": runner.server_name,
        "label": runner.label,
        "managed": managed,
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
    managed = _managed_runner_ids(app_state, runners)
    return {
        "enabled": bool(getattr(app_state.config, "agent_runtime_v3_enabled", False)),
        "agent_runners": [
            runner_to_dict(runner, connected=runner.id in connected, managed=runner.id in managed)
            for runner in runners
        ],
    }


@router.post("/agent-runners/enroll-requests", status_code=202)
async def request_agent_runner_enroll(body: AgentRunnerEnrollRequest, request: Request) -> dict[str, Any]:
    app_state = _runtime(request)
    try:
        payload: dict[str, Any] = {"server": body.server}
        if body.install is not None:
            payload["install"] = {"workspace_root": body.install.workspace_root}
        approval = request_agent_runner_enroll_approval(
            app_state.db,
            payload,
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


@router.post("/agent-runners/{runner_id}/claude-token")
async def set_agent_runner_claude_token(
    runner_id: str, body: AgentRunnerClaudeTokenRequest, request: Request, response: Response
) -> dict[str, Any]:
    """DG-AGENT-RUNNER-INSTALL-v1 K-3: write the Claude Code OAuth token
    (`claude setup-token`) into the managed runner's `claude.env` over the
    trusted SSH channel and restart the tmux session. The token lives only in
    this request: never stored, logged, audited, or echoed."""

    from dispatch_center.api.routers.infrastructure_v2 import _require_human_platform_manage

    context = _require_human_platform_manage(request)
    app_state = _runtime(request)
    runner = app_state.db.get_agent_runner(runner_id)
    if runner is None or not runner.is_active:
        raise APIError(code="not_found", message="runner agent 不存在或已撤銷", status_code=404)
    approval = app_state.db.get_approval(runner.approval_id) if runner.approval_id is not None else None
    if approval is None or not is_managed_install(approval.payload):
        raise APIError(code="runner_not_managed", message="此 runner 不是由平台安裝，請在機器上手動設定 claude.env", status_code=409)
    token = body.token
    if not token or len(token) > 512:
        raise APIError(code="invalid_claude_token", message="Claude token 必須提供且不超過 512 字元", status_code=400)
    try:
        validate_claude_token(token)
    except ValueError as exc:
        raise APIError(code="invalid_claude_token", message=str(exc), status_code=400) from None
    if app_state.db.get_active_ssh_host_identity(runner.server_name) is None:
        raise APIError(code="ssh_host_identity_untrusted", message="主機身分尚未信任", status_code=409)
    outcome = await set_claude_token(
        server_name=runner.server_name,
        ssh_run=app_state.ssh_run,
        ssh_write_file=app_state.ssh_write_file,
        claude_token=token,
    )
    del token
    append_audit(
        "agent_runner_claude_token",
        {"runner_id": runner.id, "server": runner.server_name, "ok": outcome.ok, "step": outcome.step},
        result="ok" if outcome.ok else "failed",
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(context),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"ok": outcome.ok, "step": outcome.step, "detail": outcome.detail}


__all__ = [
    "AGENT_RUNNERS_ROUTE",
    "AGENT_RUNNER_CLAUDE_TOKEN_ROUTE",
    "AGENT_RUNNER_ENROLL_ROUTE",
    "AGENT_RUNNER_REVOKE_ROUTE",
    "router",
    "runner_to_dict",
]
