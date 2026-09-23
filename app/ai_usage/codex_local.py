"""Codex CLI local usage parser (DG-AI-USAGE-OVERVIEW-v1 U-2).

Reads only ``<home>/.codex/sessions/**/*.jsonl`` (never ``auth.json``,
``history.jsonl`` or the sqlite stores) and makes no network calls.

On-disk shapes (verified 2026-09-23, structure only):

* every line: ``{"timestamp": iso, "ordinal": int, "type": str, "payload": {...}}``;
* ``event_msg`` / ``payload.type == "token_count"``: ``payload.info`` carries
  ``total_token_usage`` (cumulative per session), ``last_token_usage`` (last
  model call) and ``model_context_window``; ``payload.rate_limits`` carries
  ``plan_type``, ``limit_id`` and the ``primary`` / ``secondary`` windows
  (``used_percent``, ``window_minutes``, ``resets_at`` unix seconds — older
  builds used ``resets_in_seconds``). Windows may be ``null`` on some events;
* ``token_usage_record``: per-response ``payload.usage`` (newer builds);
* ``turn_context``: ``payload.model``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.ai_usage.common import (
    AVAILABLE,
    MAX_FILES_PER_PROVIDER,
    MAX_FILES_SCANNED_FOR_TODAY,
    QUOTA_SNAPSHOT_FILES,
    STALE,
    STALE_AFTER_SECONDS,
    JsonlFile,
    ScanStats,
    account_quota_unavailable,
    context_usage_unavailable,
    dict_or_none,
    humanize_window_minutes,
    iso,
    iter_lines_reverse,
    list_jsonl_files,
    local_usage_unavailable,
    parse_json_line,
    parse_timestamp,
    safe_int,
    safe_model_name,
    safe_percent,
)

SOURCE = "codex_session_jsonl"
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in USAGE_FIELDS}


def _usage_from(value: Any) -> Optional[dict[str, int]]:
    payload = dict_or_none(value)
    if payload is None:
        return None
    usage = {field: safe_int(payload.get(field)) or 0 for field in USAGE_FIELDS}
    if all(count == 0 for count in usage.values()):
        return None
    return {field: max(0, count) for field, count in usage.items()}


def _add(total: dict[str, int], part: dict[str, int]) -> None:
    for field in USAGE_FIELDS:
        total[field] += part.get(field, 0)


def _resets_at(window: dict[str, Any], event_ts: Optional[datetime]) -> Optional[datetime]:
    absolute = safe_int(window.get("resets_at"))
    if absolute is not None and absolute > 0:
        try:
            return datetime.fromtimestamp(absolute, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    relative = safe_int(window.get("resets_in_seconds"))
    if relative is not None and event_ts is not None:
        return event_ts + timedelta(seconds=relative)
    return None


def _windows(rate_limits: dict[str, Any], event_ts: Optional[datetime]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for window_id in ("primary", "secondary"):
        window = dict_or_none(rate_limits.get(window_id))
        if window is None:
            continue
        minutes = safe_int(window.get("window_minutes"))
        windows.append(
            {
                "id": window_id,
                "label": humanize_window_minutes(minutes) or window_id,
                "window_minutes": minutes if minutes and minutes > 0 else None,
                "used_percent": safe_percent(window.get("used_percent")),
                "resets_at": _resets_at(window, event_ts),
                "state": "current",
            }
        )
    return windows


class _FileScan:
    """Per-file reverse scan state."""

    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.today = _zero_usage()
        self.has_records = False
        self.events = 0
        self.newest_event: Optional[datetime] = None
        self.latest_total_today: Optional[dict[str, int]] = None
        self.latest_total_before: Optional[dict[str, int]] = None


def scan_codex(home: Path, *, now: datetime, today_start: datetime) -> dict[str, Any]:
    stats = ScanStats()
    codex_dir = home / ".codex"
    if not codex_dir.is_dir():
        return {
            "account_quota": account_quota_unavailable("home_missing"),
            "local_usage": local_usage_unavailable("home_missing", source=SOURCE, stats=stats, now=now),
            "context_usage": context_usage_unavailable("home_missing"),
        }
    files = list_jsonl_files(codex_dir / "sessions", limit=MAX_FILES_PER_PROVIDER, stats=stats)
    if not files:
        return {
            "account_quota": account_quota_unavailable("no_session_files"),
            "local_usage": local_usage_unavailable("no_session_files", source=SOURCE, stats=stats, now=now),
            "context_usage": context_usage_unavailable("no_session_files"),
        }

    quota: Optional[dict[str, Any]] = None
    context: Optional[dict[str, Any]] = None
    context_scan: Optional[_FileScan] = None
    totals = _zero_usage()
    sessions_today = 0
    events_today = 0
    newest_event: Optional[datetime] = None
    models_today: dict[str, int] = {}
    today_files = 0
    today_start_ts = today_start.timestamp()

    for index, file in enumerate(files):
        want_today = file.mtime >= today_start_ts and today_files < MAX_FILES_SCANNED_FOR_TODAY
        want_quota = quota is None and index < QUOTA_SNAPSHOT_FILES
        want_context = context is None and index < QUOTA_SNAPSHOT_FILES
        if not (want_today or want_quota or want_context):
            stats.files_skipped += 1
            continue
        if want_today:
            today_files += 1
        scan = _FileScan()
        today_done = not want_today
        for line in iter_lines_reverse(file, stats=stats):
            record = parse_json_line(line)
            if record is None:
                stats.lines_skipped += 1
                continue
            kind = record.get("type")
            payload = dict_or_none(record.get("payload")) or {}
            ts = parse_timestamp(record.get("timestamp"))
            if kind == "turn_context":
                if scan.model is None:
                    scan.model = safe_model_name(payload.get("model"))
            elif kind == "token_usage_record" and not today_done:
                usage = _usage_from(payload.get("usage"))
                if usage is not None and ts is not None and ts >= today_start:
                    scan.has_records = True
                    _add(scan.today, usage)
                    scan.events += 1
                    if scan.newest_event is None or ts > scan.newest_event:
                        scan.newest_event = ts
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = dict_or_none(payload.get("info"))
                rate_limits = dict_or_none(payload.get("rate_limits"))
                if want_context and context is None and info is not None:
                    last = dict_or_none(info.get("last_token_usage"))
                    used = safe_int(last.get("total_tokens")) if last is not None else None
                    window = safe_int(info.get("model_context_window"))
                    if used is not None and used >= 0:
                        context = {
                            "used_tokens": used,
                            "context_window": window if window and window > 0 else None,
                            "observed_at": ts,
                        }
                        context_scan = scan
                if (
                    want_quota
                    and quota is None
                    and rate_limits is not None
                    and (dict_or_none(rate_limits.get("primary")) or dict_or_none(rate_limits.get("secondary")))
                ):
                    quota = {
                        "plan_type": safe_model_name(rate_limits.get("plan_type")),
                        "limit_id": safe_model_name(rate_limits.get("limit_id")),
                        "observed_at": ts,
                        "windows": _windows(rate_limits, ts),
                    }
                if not today_done and info is not None and ts is not None:
                    total = _usage_from(info.get("total_token_usage"))
                    if total is not None:
                        if ts >= today_start:
                            if scan.latest_total_today is None:
                                scan.latest_total_today = total
                        elif scan.latest_total_before is None:
                            scan.latest_total_before = total
            if not today_done and ts is not None and ts < today_start:
                if scan.has_records or scan.latest_total_before is not None or scan.latest_total_today is None:
                    today_done = True
            quota_done = quota is not None or not want_quota
            context_done = context is not None or not want_context
            model_done = scan.model is not None or (context_scan is not scan and not want_today)
            if today_done and quota_done and context_done and model_done:
                break
        if want_today:
            file_total: Optional[dict[str, int]] = None
            file_events = 0
            if scan.has_records:
                file_total = scan.today
                file_events = scan.events
            elif scan.latest_total_today is not None:
                before = scan.latest_total_before or _zero_usage()
                delta = {
                    field: max(0, scan.latest_total_today[field] - before.get(field, 0))
                    for field in USAGE_FIELDS
                }
                if delta["total_tokens"] > 0 or delta["input_tokens"] > 0 or delta["output_tokens"] > 0:
                    file_total = delta
                    file_events = 1
            if file_total is not None and any(file_total.values()):
                _add(totals, file_total)
                sessions_today += 1
                events_today += file_events
                model_key = scan.model or "unknown"
                models_today[model_key] = models_today.get(model_key, 0) + file_total["total_tokens"]
                if scan.newest_event is not None and (newest_event is None or scan.newest_event > newest_event):
                    newest_event = scan.newest_event

    account_quota: dict[str, Any]
    if quota is None:
        account_quota = account_quota_unavailable("no_rate_limit_events")
    else:
        observed_at: Optional[datetime] = quota["observed_at"]
        stale = observed_at is None or (now - observed_at).total_seconds() > STALE_AFTER_SECONDS
        windows = []
        for window in quota["windows"]:
            resets_at: Optional[datetime] = window["resets_at"]
            if resets_at is not None and resets_at <= now:
                state = "expired"
            elif stale:
                state = "stale"
            else:
                state = "current"
            windows.append({**window, "resets_at": iso(resets_at), "state": state})
        account_quota = {
            "availability": STALE if stale else AVAILABLE,
            "reason": "stale_snapshot" if stale else None,
            "source": SOURCE,
            "plan_type": quota["plan_type"],
            "limit_id": quota["limit_id"],
            "observed_at": iso(observed_at),
            "windows": windows,
        }

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

    context_usage: dict[str, Any]
    if context is None:
        context_usage = context_usage_unavailable("no_context_events")
    else:
        used = context["used_tokens"]
        window = context["context_window"]
        model = context_scan.model if context_scan is not None else None
        if window:
            context_usage = {
                "availability": AVAILABLE,
                "reason": None,
                "model": model,
                "used_tokens": used,
                "context_window": window,
                "used_percent": round(min(100.0, used / window * 100.0), 1),
                "observed_at": iso(context["observed_at"]),
            }
        else:
            context_usage = {
                "availability": "partial",
                "reason": "context_window_unknown",
                "model": model,
                "used_tokens": used,
                "context_window": None,
                "used_percent": None,
                "observed_at": iso(context["observed_at"]),
            }

    return {
        "account_quota": account_quota,
        "local_usage": local_usage,
        "context_usage": context_usage,
    }


__all__ = ["SOURCE", "USAGE_FIELDS", "scan_codex", "JsonlFile"]
