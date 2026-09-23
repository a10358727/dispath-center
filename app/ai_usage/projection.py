"""``ai-provider-quota-v1`` projection (DG-AI-USAGE-OVERVIEW-v1 U-1/U-4).

Assembles the read-only Overview projection from the two local parsers and the
separate Claude quota adapter. The four categories — ``account_quota``,
``local_usage``, ``context_usage`` and ``estimated_cost`` — each carry their own
``availability`` + ``reason`` and are never relabelled as one another;
``estimated_cost`` is always unavailable in this packet (no pricing source).

A short in-process TTL cache keeps Overview polling from rescanning session
files every few seconds. Any parser failure degrades that provider to
``scan_error`` without raising and without logging file contents or paths.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.ai_usage.claude_local import scan_claude_code
from app.ai_usage.claude_quota import ClaudeQuotaAdapter, UnavailableClaudeQuotaAdapter
from app.ai_usage.codex_local import scan_codex
from app.ai_usage.common import (
    CACHE_TTL_SECONDS,
    SCHEMA,
    ScanStats,
    account_quota_unavailable,
    context_usage_unavailable,
    estimated_cost_unavailable,
    local_usage_unavailable,
)

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {"claude_code": "Claude Code", "codex": "Codex"}

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _guarded(
    provider: str,
    scan: Callable[..., dict[str, Any]],
    home: Path,
    *,
    now: datetime,
    today_start: datetime,
    source: str,
) -> dict[str, Any]:
    try:
        return scan(home, now=now, today_start=today_start)
    except Exception as exc:  # noqa: BLE001 - never fail the projection; never log paths
        logger.warning("ai_usage: %s local scan failed (%s)", provider, type(exc).__name__)
        return {
            "account_quota": account_quota_unavailable("scan_error"),
            "local_usage": local_usage_unavailable(
                "scan_error", source=source, stats=ScanStats(), now=now
            ),
            "context_usage": context_usage_unavailable("scan_error"),
        }


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def build_ai_provider_quota_projection(
    home: Path,
    *,
    now: Optional[datetime] = None,
    claude_quota: Optional[ClaudeQuotaAdapter] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Build the projection for ``home`` (the service user's home directory).

    ``now`` must be timezone-aware; "today" is the local calendar day of
    ``now``'s timezone (tests pass a fixed UTC ``now``).
    """

    key = str(home)
    if use_cache:
        with _cache_lock:
            entry = _cache.get(key)
            if entry is not None and time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
                return copy.deepcopy(entry[1])

    current = now or datetime.now(timezone.utc).astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)

    codex = _guarded(
        "codex", scan_codex, home, now=current, today_start=today_start, source="codex_session_jsonl"
    )
    claude = _guarded(
        "claude_code",
        scan_claude_code,
        home,
        now=current,
        today_start=today_start,
        source="claude_code_project_jsonl",
    )
    adapter = claude_quota or UnavailableClaudeQuotaAdapter()
    try:
        claude_account_quota = adapter.fetch()
    except Exception as exc:  # noqa: BLE001 - adapter must never break the projection
        logger.warning("ai_usage: claude quota adapter failed (%s)", type(exc).__name__)
        claude_account_quota = account_quota_unavailable("scan_error")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": current.isoformat(),
        "today": {
            "date": today_start.date().isoformat(),
            "timezone": current.tzname() or current.strftime("%z") or "UTC",
            "starts_at": today_start.isoformat(),
        },
        "providers": {
            "claude_code": {
                "provider": "claude_code",
                "label": PROVIDER_LABELS["claude_code"],
                "account_quota": claude_account_quota,
                "local_usage": claude["local_usage"],
                "context_usage": claude["context_usage"],
                "estimated_cost": estimated_cost_unavailable(),
            },
            "codex": {
                "provider": "codex",
                "label": PROVIDER_LABELS["codex"],
                "account_quota": codex["account_quota"],
                "local_usage": codex["local_usage"],
                "context_usage": codex["context_usage"],
                "estimated_cost": estimated_cost_unavailable(),
            },
        },
    }
    if use_cache:
        with _cache_lock:
            _cache[key] = (time.monotonic(), copy.deepcopy(result))
    return result


__all__ = ["build_ai_provider_quota_projection", "clear_cache", "PROVIDER_LABELS"]
