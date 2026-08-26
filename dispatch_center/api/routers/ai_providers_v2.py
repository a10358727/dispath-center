"""AI 供應商狀態面板 + Anthropic API key 直接執行例外 (DG-ASSISTANT-CLAUDE-TURN
v1 C2, docs/DECISIONS.md 2026-08-26 使用者裁定) + packet D2/D3 model
selection and usage accounting.

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

`POST /api/v2/ai-providers/assistant-model`/`.../api-model` (packet D2) are
the same direct-execute shape, generalized via `app.anthropic_key.
set_env_var()`/`ENV_VALUE_VALIDATORS`: `ASSISTANT_CLAUDE_MODEL` (the
runner-hosted `claude -p` assistant turn's model, `app.assistant_turns`) and
`LLM_MODEL` (the per-project conversation Anthropic API model,
`app.llm.get_model()`) respectively. **Unlike the Anthropic key, a model
name is not a secret** -- it IS included in the audit record's params, never
masked. Empty string clears the override (back to the CLI/SDK's own
default).

`GET /api/v2/ai-providers/usage` (packet D3) is a thin read-only wrapper
around `Database.get_assistant_usage_summary()` -- `days` bounded to 1..90.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from app.anthropic_key import (
    ENV_VALUE_VALIDATORS,
    InvalidAnthropicApiKeyError,
    InvalidEnvValueError,
    clear_anthropic_api_key,
    set_anthropic_api_key,
    set_env_var,
)
from app.audit import append_audit, audit_actor_from_request_context
from dispatch_center.api.errors import APIError
from dispatch_center.api.schemas import AnthropicApiKeyRequest, AssistantModelRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)

logger = logging.getLogger(__name__)

AI_PROVIDERS_STATUS_ROUTE = "/api/v2/ai-providers/status"
AI_PROVIDERS_ANTHROPIC_KEY_ROUTE = "/api/v2/ai-providers/anthropic-key"
AI_PROVIDERS_ASSISTANT_MODEL_ROUTE = "/api/v2/ai-providers/assistant-model"
AI_PROVIDERS_API_MODEL_ROUTE = "/api/v2/ai-providers/api-model"
AI_PROVIDERS_USAGE_ROUTE = "/api/v2/ai-providers/usage"


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


def _set_model_env_var(
    *, env_name: str, request: Request, response: Response, req: AssistantModelRequest
) -> str:
    """Shared body for the two packet D2 model-selection endpoints below --
    validate + atomically rewrite `.env`'s `{env_name}=` line via
    `app.anthropic_key.set_env_var()` (raises `InvalidEnvValueError` on a
    malformed value, mapped to 400 with zero writes), audit the (non-secret)
    resulting value, and return it so the caller can update the matching
    in-memory `config.*` field."""

    assert env_name in ENV_VALUE_VALIDATORS  # defense in depth, not caller input
    app_state = _runtime(request)
    try:
        validated = set_env_var(app_state.config.env_file_path, env_name, req.model)
    except InvalidEnvValueError as exc:
        raise APIError(
            code="invalid_model_name", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return validated


@router.post("/ai-providers/assistant-model")
async def set_assistant_model(
    req: AssistantModelRequest, request: Request, response: Response
) -> dict[str, Any]:
    """`ASSISTANT_CLAUDE_MODEL` -- the runner-hosted `claude -p` assistant
    turn's model (INV-SSH-3: `app.assistant_turns` re-validates and quotes
    this value again before it ever reaches a Runner shell command; this
    endpoint's validation is a UI-facing convenience, not the only gate)."""

    app_state = _runtime(request)
    validated = _set_model_env_var(
        env_name="ASSISTANT_CLAUDE_MODEL", request=request, response=response, req=req
    )
    app_state.config.assistant_claude_model = validated
    append_audit(
        "assistant_claude_model_configured",
        {"model": validated},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"assistant_claude_model": validated}


@router.post("/ai-providers/api-model")
async def set_api_model(
    req: AssistantModelRequest, request: Request, response: Response
) -> dict[str, Any]:
    """`LLM_MODEL` -- the per-project conversation Anthropic API model
    (`app.llm.get_model()`, read fresh on every turn -- no client rebuild
    needed, unlike the API key)."""

    app_state = _runtime(request)
    validated = _set_model_env_var(
        env_name="LLM_MODEL", request=request, response=response, req=req
    )
    app_state.config.llm_model = validated
    append_audit(
        "llm_model_configured",
        {"model": validated},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"llm_model": validated}


@router.get("/ai-providers/usage")
async def get_ai_providers_usage(
    request: Request,
    response: Response,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """`assistant_usage`（migration 16）的唯讀彙總——`days`-bounded totals
    plus a per-day/per-channel/per-model breakdown. See
    `Database.get_assistant_usage_summary()`."""

    app_state = _runtime(request)
    _no_store(response)
    return app_state.db.get_assistant_usage_summary(days)


__all__ = [
    "AI_PROVIDERS_ANTHROPIC_KEY_ROUTE",
    "AI_PROVIDERS_API_MODEL_ROUTE",
    "AI_PROVIDERS_ASSISTANT_MODEL_ROUTE",
    "AI_PROVIDERS_STATUS_ROUTE",
    "AI_PROVIDERS_USAGE_ROUTE",
    "router",
]
