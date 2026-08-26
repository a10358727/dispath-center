"""AI 供應商狀態面板 + Anthropic API key 直接執行例外 (DG-ASSISTANT-CLAUDE-TURN
v1 C2, docs/DECISIONS.md 2026-08-26 使用者裁定).

`GET /api/v2/ai-providers/status` is a thin read-only wrapper around
`AppState.get_ai_providers_status()` (mirrors `infrastructure_v2.py`'s
`get_codex_runner_status()` wrapper -- same "call the one running AppState
instance" duck-typed `_runtime()` convention, same reason: `app.main`
imports this router before its own class body exists, so importing back
from `app.main` would be circular).

`POST`/`DELETE /api/v2/ai-providers/anthropic-key` is the one documented
direct-execute exception in this packet: shape-validate (`app.anthropic_key.
validate_anthropic_api_key()`, never a real call to Anthropic), atomically
rewrite the deployment `.env` (`app.anthropic_key.set_/clear_
anthropic_api_key()`), update the in-memory `config.anthropic_api_key` and
rebuild `app_state.llm_client` so the change takes effect immediately, and
append a zero-parameter audit record (`anthropic_api_key_configured`/
`_cleared`, see `app.audit_adoption`'s `platform.ai_provider_key` entry).
**The key value never appears in the response body, the audit record, or a
log line** -- it is passed straight from the validated request body into
`app.anthropic_key`, which itself performs the only I/O.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from app.anthropic_key import (
    InvalidAnthropicApiKeyError,
    clear_anthropic_api_key,
    set_anthropic_api_key,
)
from app.audit import append_audit, audit_actor_from_request_context
from dispatch_center.api.errors import APIError
from dispatch_center.api.schemas import AnthropicApiKeyRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)

logger = logging.getLogger(__name__)

AI_PROVIDERS_STATUS_ROUTE = "/api/v2/ai-providers/status"
AI_PROVIDERS_ANTHROPIC_KEY_ROUTE = "/api/v2/ai-providers/anthropic-key"


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
    ],
)


def _runtime(request: Request) -> Any:
    """The single running `app.main.AppState` instance, duck-typed `Any` --
    matches `infrastructure_v2._runtime()`/`jobs_v2._runtime()`."""

    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    return app_state


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get("/ai-providers/status")
async def get_ai_providers_status(
    request: Request, response: Response
) -> dict[str, Any]:
    """`{"assistant_brain": {...}, "anthropic": {...}, "claude_runner": {...},
    "codex_runner": {...}}` -- see `AppState.get_ai_providers_status()`.
    Never returns `claude auth status`/`codex login status` raw output or
    any credential."""

    app_state = _runtime(request)
    _no_store(response)
    return await app_state.get_ai_providers_status()


@router.post("/ai-providers/anthropic-key")
async def set_ai_provider_anthropic_key(
    req: AnthropicApiKeyRequest, request: Request, response: Response
) -> dict[str, Any]:
    app_state = _runtime(request)
    try:
        set_anthropic_api_key(app_state.config.env_file_path, req.api_key)
    except InvalidAnthropicApiKeyError as exc:
        raise APIError(
            code="invalid_anthropic_api_key", message=str(exc), status_code=400
        ) from exc

    app_state.config.anthropic_api_key = req.api_key
    app_state.rebuild_llm_client()
    append_audit(
        "anthropic_api_key_configured",
        {},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    _no_store(response)
    return {"key_configured": True}


@router.delete("/ai-providers/anthropic-key")
async def clear_ai_provider_anthropic_key(
    request: Request, response: Response
) -> dict[str, Any]:
    app_state = _runtime(request)
    clear_anthropic_api_key(app_state.config.env_file_path)
    app_state.config.anthropic_api_key = None
    app_state.rebuild_llm_client()
    append_audit(
        "anthropic_api_key_cleared",
        {},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    _no_store(response)
    return {"key_configured": False}


__all__ = [
    "AI_PROVIDERS_ANTHROPIC_KEY_ROUTE",
    "AI_PROVIDERS_STATUS_ROUTE",
    "router",
]
