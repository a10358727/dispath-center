"""Per-session Claude Agent SDK options chosen in Studio (DG-STUDIO-UI v1, Phase 2).

Pure validation shared by the approval request (where the options are pinned
into the `agent_session_open` payload), the gateway (which ships them to the
runner in `session/open` / `session/configure`) and the Studio routes.

INV-AGENT-2: the permission mode can never disable the workspace prompts —
`bypassPermissions`, `dontAsk` and `auto` are rejected here and again on the
runner. `plan` and `acceptEdits` only change how the SDK treats *file edits*
inside the confined workspace; Bash and platform tools stay gated.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

MODEL_ALIASES = ("opus", "sonnet", "haiku")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan")
FORBIDDEN_PERMISSION_MODES = ("bypassPermissions", "dontAsk", "auto")
THINKING_PRESETS = ("adaptive", "disabled")
THINKING_BUDGET_MIN = 1024
THINKING_BUDGET_MAX = 131072
OPTION_KEYS = ("model", "effort", "thinking", "permission_mode")


class InvalidSessionOptionsError(ValueError):
    """The requested session options are outside the closed vocabulary."""


def normalize_session_options(raw: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a closed, validated copy of the requested options.

    Only keys that were provided (non-null) survive; an empty dict means
    "runner defaults". Every failure is an `InvalidSessionOptionsError`."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise InvalidSessionOptionsError("options must be an object")
    unknown = sorted(set(raw) - set(OPTION_KEYS))
    if unknown:
        raise InvalidSessionOptionsError(f"unknown option(s): {', '.join(unknown)}")
    out: dict[str, Any] = {}
    model = raw.get("model")
    if model is not None:
        if not isinstance(model, str) or not _MODEL_RE.match(model.strip()):
            raise InvalidSessionOptionsError("model must be an alias (opus/sonnet/haiku) or a model id")
        out["model"] = model.strip()
    effort = raw.get("effort")
    if effort is not None:
        if effort not in EFFORT_LEVELS:
            raise InvalidSessionOptionsError(f"effort must be one of {', '.join(EFFORT_LEVELS)}")
        out["effort"] = effort
    thinking = raw.get("thinking")
    if thinking is not None:
        out["thinking"] = _normalize_thinking(thinking)
    mode = raw.get("permission_mode")
    if mode is not None:
        out["permission_mode"] = normalize_permission_mode(mode)
    return out


def normalize_permission_mode(mode: Any) -> str:
    if mode in FORBIDDEN_PERMISSION_MODES:
        raise InvalidSessionOptionsError(f"permission_mode {mode!r} is never allowed (INV-AGENT-2)")
    if mode not in PERMISSION_MODES:
        raise InvalidSessionOptionsError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    return str(mode)


def _normalize_thinking(value: Any) -> Any:
    if isinstance(value, str):
        if value not in THINKING_PRESETS:
            raise InvalidSessionOptionsError("thinking must be adaptive, disabled or {budget_tokens: N}")
        return value
    if isinstance(value, Mapping):
        budget = value.get("budget_tokens")
        if isinstance(budget, bool) or not isinstance(budget, int) or not THINKING_BUDGET_MIN <= budget <= THINKING_BUDGET_MAX:
            raise InvalidSessionOptionsError(
                f"thinking.budget_tokens must be an integer between {THINKING_BUDGET_MIN} and {THINKING_BUDGET_MAX}"
            )
        if set(value) - {"budget_tokens"}:
            raise InvalidSessionOptionsError("thinking accepts only budget_tokens")
        return {"budget_tokens": budget}
    raise InvalidSessionOptionsError("thinking must be adaptive, disabled or {budget_tokens: N}")


def merge_session_options(current: Optional[Mapping[str, Any]], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a validated live change (model / permission_mode) on top of the stored options."""

    merged = dict(current or {})
    for key, value in normalize_session_options(changes).items():
        merged[key] = value
    return merged


__all__ = [
    "EFFORT_LEVELS",
    "FORBIDDEN_PERMISSION_MODES",
    "InvalidSessionOptionsError",
    "MODEL_ALIASES",
    "OPTION_KEYS",
    "PERMISSION_MODES",
    "THINKING_PRESETS",
    "merge_session_options",
    "normalize_permission_mode",
    "normalize_session_options",
]
