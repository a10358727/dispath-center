"""Claude Code local usage parser (DG-AI-USAGE-OVERVIEW-v1 U-3).

Reads only ``<home>/.claude/projects/**/*.jsonl`` and, from each line, only
``type == "assistant"`` records' ``message.usage`` / ``message.model`` /
``message.id`` / ``requestId`` / ``timestamp``. Prompt text, tool results,
``.credentials.json``, ``history.jsonl``, ``settings.json`` and
``stats-cache.json`` are never opened or returned. Account quota is not derived
here at all — see :mod:`app.ai_usage.claude_quota` for the separate adapter seam.

On-disk shape (verified 2026-09-23, structure only): assistant lines carry
``message.usage.{input_tokens, cache_creation_input_tokens,
cache_read_input_tokens, output_tokens}``; the same message is written on
several lines (one per content block) with identical usage, so entries are
deduplicated by ``(message.id, requestId)``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.ai_usage.common import (
    AVAILABLE,
    MAX_FILES_PER_PROVIDER,
    MAX_FILES_SCANNED_FOR_TODAY,
    PARTIAL,
    QUOTA_SNAPSHOT_FILES,
    ScanStats,
    context_usage_unavailable,
    dict_or_none,
    iso,
    iter_lines_reverse,
    list_jsonl_files,
    local_usage_unavailable,
    parse_json_line,
    parse_timestamp,
    safe_int,
    safe_model_name,
)

SOURCE = "claude_code_project_jsonl"
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _usage_from(value: Any) -> Optional[dict[str, int]]:
    usage = dict_or_none(value)
    if usage is None:
        return None
    counts = {field: max(0, safe_int(usage.get(field)) or 0) for field in USAGE_FIELDS}
    counts["total_tokens"] = sum(counts.values())
    return counts


def scan_claude_code(home: Path, *, now: datetime, today_start: datetime) -> dict[str, Any]:
    """Return ``local_usage`` and ``context_usage`` for Claude Code."""

    stats = ScanStats()
    claude_dir = home / ".claude"
    if not claude_dir.is_dir():
        return {
            "local_usage": local_usage_unavailable("home_missing", source=SOURCE, stats=stats, now=now),
            "context_usage": context_usage_unavailable("home_missing"),
        }
    files = list_jsonl_files(claude_dir / "projects", limit=MAX_FILES_PER_PROVIDER, stats=stats)
    if not files:
        return {
            "local_usage": local_usage_unavailable("no_session_files", source=SOURCE, stats=stats, now=now),
            "context_usage": context_usage_unavailable("no_session_files"),
        }

    totals = {field: 0 for field in (*USAGE_FIELDS, "total_tokens")}
    sessions_today = 0
    events_today = 0
    newest_event: Optional[datetime] = None
    models_today: dict[str, int] = {}
    context: Optional[dict[str, Any]] = None
    today_files = 0
    today_start_ts = today_start.timestamp()

    for index, file in enumerate(files):
        want_today = file.mtime >= today_start_ts and today_files < MAX_FILES_SCANNED_FOR_TODAY
        want_context = context is None and index < QUOTA_SNAPSHOT_FILES
        if not (want_today or want_context):
            stats.files_skipped += 1
            continue
        if want_today:
            today_files += 1
        seen: set[tuple[Any, Any]] = set()
        file_total = 0
        file_events = 0
        line_no = 0
        for line in iter_lines_reverse(file, stats=stats):
            line_no += 1
            record = parse_json_line(line)
            if record is None:
                stats.lines_skipped += 1
                continue
            ts = parse_timestamp(record.get("timestamp"))
            if record.get("type") != "assistant":
                if want_today and ts is not None and ts < today_start and context is not None:
                    break
                continue
            message = dict_or_none(record.get("message"))
            usage = _usage_from(message.get("usage")) if message is not None else None
            if message is None or usage is None:
                continue
            model = safe_model_name(message.get("model"))
            if want_context and context is None:
                context = {
                    "used_tokens": usage["input_tokens"]
                    + usage["cache_read_input_tokens"]
                    + usage["cache_creation_input_tokens"],
                    "model": model,
                    "observed_at": ts,
                }
            if not want_today:
                if context is not None:
                    break
                continue
            if ts is None:
                continue
            if ts < today_start:
                if context is not None:
                    break
                continue
            message_id = message.get("id")
            request_id = record.get("requestId")
            key: tuple[Any, Any]
            if isinstance(message_id, str) or isinstance(request_id, str):
                key = (message_id if isinstance(message_id, str) else None,
                       request_id if isinstance(request_id, str) else None)
            else:
                key = ("line", line_no)
            if key in seen:
                continue
            seen.add(key)
            for field in USAGE_FIELDS:
                totals[field] += usage[field]
            totals["total_tokens"] += usage["total_tokens"]
            file_total += usage["total_tokens"]
            file_events += 1
            model_key = model or "unknown"
            models_today[model_key] = models_today.get(model_key, 0) + usage["total_tokens"]
            if newest_event is None or ts > newest_event:
                newest_event = ts
        if want_today and file_events:
            sessions_today += 1
            events_today += file_events

    local_usage = {
        "availability": AVAILABLE,
        "reason": None,
        "source": SOURCE,
        "today": dict(totals),
        "sessions_today": sessions_today,
        "events_today": events_today,
        "newest_event_at": iso(newest_event),
        "models_today": models_today,
        "scan": stats.as_dict(now),
    }
    if context is None:
        context_usage = context_usage_unavailable("no_context_events")
    else:
        context_usage = {
            "availability": PARTIAL,
            "reason": "context_window_unknown",
            "model": context["model"],
            "used_tokens": context["used_tokens"],
            "context_window": None,
            "used_percent": None,
            "observed_at": iso(context["observed_at"]),
        }
    return {"local_usage": local_usage, "context_usage": context_usage}


__all__ = ["SOURCE", "USAGE_FIELDS", "scan_claude_code"]
