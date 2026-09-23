"""Bounded, read-only helpers shared by the local AI usage parsers.

DG-AI-USAGE-OVERVIEW-v1 U-6: every file this package touches is opened through
:func:`open_binary` (tests spy on it to prove decoy credential files are never
opened), only regular ``*.jsonl`` files inside the provider's session tree are
considered (no symlinks, realpath containment), and scanning is bounded by file
count, bytes per file, line length and total bytes. Nothing here shells out,
touches the network, or returns file contents/paths.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

MAX_FILES_PER_PROVIDER = 200
MAX_FILES_SCANNED_FOR_TODAY = 50
QUOTA_SNAPSHOT_FILES = 10
MAX_BYTES_PER_FILE = 32 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
STALE_AFTER_SECONDS = 3600
CACHE_TTL_SECONDS = 30

_BLOCK_SIZE = 256 * 1024
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")

SCHEMA = "ai-provider-quota-v1"
UNAVAILABLE = "unavailable"
AVAILABLE = "available"
STALE = "stale"
PARTIAL = "partial"


def open_binary(path: Path) -> BinaryIO:
    """The single file-open used by this package (tests spy on it)."""

    return open(path, "rb")


@dataclass
class JsonlFile:
    path: Path
    mtime: float
    size: int


@dataclass
class ScanStats:
    files_considered: int = 0
    files_scanned: int = 0
    files_skipped: int = 0
    lines_skipped: int = 0
    bytes_read: int = 0
    truncated: bool = False

    def as_dict(self, scanned_at: datetime) -> dict[str, Any]:
        return {
            "files_considered": self.files_considered,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "lines_skipped": self.lines_skipped,
            "bytes_read": self.bytes_read,
            "truncated": self.truncated,
            "scanned_at": scanned_at.isoformat(),
        }


def is_within(base: Path, path: Path) -> bool:
    """True when ``path`` resolves inside ``base`` (symlink escapes rejected)."""

    try:
        base_real = os.path.realpath(base)
        path_real = os.path.realpath(path)
    except OSError:
        return False
    return path_real == base_real or path_real.startswith(base_real.rstrip(os.sep) + os.sep)


def list_jsonl_files(base: Path, *, limit: int, stats: ScanStats) -> list[JsonlFile]:
    """Regular ``*.jsonl`` files under ``base`` newest-first (mtime), bounded."""

    if not base.is_dir() or os.path.islink(base):
        return []
    found: list[JsonlFile] = []
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = Path(root) / name
            if os.path.islink(path) or not is_within(base, path):
                stats.files_skipped += 1
                continue
            try:
                info = os.stat(path)
            except OSError:
                stats.files_skipped += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                stats.files_skipped += 1
                continue
            found.append(JsonlFile(path=path, mtime=info.st_mtime, size=info.st_size))
    found.sort(key=lambda item: item.mtime, reverse=True)
    stats.files_considered = len(found)
    if len(found) > limit:
        stats.files_skipped += len(found) - limit
        stats.truncated = True
    return found[:limit]


def iter_lines_reverse(
    file: JsonlFile,
    *,
    stats: ScanStats,
    max_bytes: int = MAX_BYTES_PER_FILE,
    max_line_bytes: int = MAX_LINE_BYTES,
    total_budget: int = MAX_TOTAL_BYTES,
) -> Iterator[bytes]:
    """Yield complete lines newest-first by reading blocks from the end.

    Stops (and marks ``stats.truncated``) once ``max_bytes`` of this file or the
    remaining ``total_budget`` has been read. Lines longer than
    ``max_line_bytes`` are skipped and counted in ``stats.lines_skipped``.
    """

    remaining_total = total_budget - stats.bytes_read
    if remaining_total <= 0:
        stats.truncated = True
        return
    budget = min(max_bytes, remaining_total)
    stats.files_scanned += 1
    with open_binary(file.path) as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        read_total = 0
        tail = b""
        while position > 0 and read_total < budget:
            step = min(_BLOCK_SIZE, position, budget - read_total)
            position -= step
            handle.seek(position)
            chunk = handle.read(step)
            read_total += len(chunk)
            stats.bytes_read += len(chunk)
            parts = (chunk + tail).split(b"\n")
            tail = parts[0]
            for raw in reversed(parts[1:]):
                line = raw.rstrip(b"\r")
                if not line:
                    continue
                if len(line) > max_line_bytes:
                    stats.lines_skipped += 1
                    continue
                yield line
            if len(tail) > max_line_bytes:
                # A single line larger than the cap cannot be parsed; drop it
                # without holding it in memory. It is counted once below.
                tail = b""
                stats.lines_skipped += 1
        if position > 0:
            stats.truncated = True
            return
        line = tail.rstrip(b"\r")
        if line:
            if len(line) > max_line_bytes:
                stats.lines_skipped += 1
            else:
                yield line


def parse_json_line(line: bytes) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return int(value)
    return None


def safe_percent(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(min(100.0, max(0.0, float(value))), 1)


def safe_model_name(value: Any) -> Optional[str]:
    if isinstance(value, str) and _MODEL_NAME.match(value):
        return value
    return None


def humanize_window_minutes(minutes: Optional[int]) -> Optional[str]:
    if minutes is None or minutes <= 0:
        return None
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "weekly"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def dict_or_none(value: Any) -> Optional[dict[str, Any]]:
    return value if isinstance(value, dict) else None


def unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"availability": UNAVAILABLE, "reason": reason}
    result.update(extra)
    return result


def account_quota_unavailable(reason: str) -> dict[str, Any]:
    return unavailable(
        reason,
        source=None,
        plan_type=None,
        limit_id=None,
        observed_at=None,
        windows=[],
    )


def local_usage_unavailable(reason: str, *, source: Optional[str], stats: ScanStats, now: datetime) -> dict[str, Any]:
    return unavailable(
        reason,
        source=source,
        today=None,
        sessions_today=0,
        events_today=0,
        newest_event_at=None,
        models_today={},
        scan=stats.as_dict(now),
    )


def context_usage_unavailable(reason: str) -> dict[str, Any]:
    return unavailable(
        reason,
        model=None,
        used_tokens=None,
        context_window=None,
        used_percent=None,
        observed_at=None,
    )


def estimated_cost_unavailable() -> dict[str, Any]:
    return unavailable("no_pricing_source", currency=None, today_usd=None)
