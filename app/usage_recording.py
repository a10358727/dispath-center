"""Packet D3 (usage accounting): the one shared, always-safe entry point every
usage-producing channel (`app.assistant_turns`' runner-hosted `claude -p`
turn, `app.llm.agent_chat_completion`'s per-project conversation Anthropic
turn, `app.llm_local.chat_completion`'s vLLM turn) goes through to record a
row in `assistant_usage` (`app.db.Database.record_assistant_usage()`,
migration 16).

**Recording a turn's usage must never affect the turn's own outcome** (same
rule `app.metrics_v1` collection follows for its own writes): every function
here swallows and logs any exception instead of raising, including an
invalid `channel` value or a `db` that is `None`/mid-shutdown. Callers
therefore never need their own extra try/except around a call into this
module.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: A recorder closure's shape: one usage dict in, nothing out, never raises.
#: `usage` keys are best-effort/optional: `input_tokens`, `output_tokens`,
#: `duration_ms` -- any subset, any of them `None`, is fine (see each
#: producer's defensive parser).
UsageRecorder = Callable[[dict], None]


def safe_record_assistant_usage(
    db: Any,
    *,
    channel: str,
    server: Optional[str] = None,
    model: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Insert one `assistant_usage` row, logging (never raising) on any
    failure -- including `db is None` (e.g. a code path exercised without a
    database, such as a pure unit test of a producer module)."""

    if db is None:
        return
    try:
        db.record_assistant_usage(
            channel=channel,
            server=server,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001 - never let usage accounting fail a turn
        logger.warning(
            "記錄 assistant usage 失敗（channel=%s, server=%s）: %s",
            channel,
            server,
            exc,
        )


def make_usage_recorder(
    db: Any,
    *,
    channel: str,
    server: Optional[str] = None,
    model: Optional[str] = None,
) -> UsageRecorder:
    """Bind `db`/`channel`/`server`/`model` once at the call site (e.g.
    `functools.partial(agent_chat_completion, record_usage=make_usage_recorder(...))`)
    and return a closure that only needs the per-turn usage dict. The
    returned closure is itself exception-safe (delegates to
    `safe_record_assistant_usage()`), so producer modules never need to wrap
    a call to it in their own try/except."""

    def _record(usage: dict) -> None:
        usage = usage or {}
        safe_record_assistant_usage(
            db,
            channel=channel,
            server=server,
            model=model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            duration_ms=usage.get("duration_ms"),
        )

    return _record


__all__ = [
    "UsageRecorder",
    "make_usage_recorder",
    "safe_record_assistant_usage",
]
