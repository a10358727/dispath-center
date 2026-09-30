"""Claude subscription quota via the Claude Code OAuth usage endpoint
(DG-AI-USAGE-OVERVIEW-v2).

Claude Code stores the subscription login's OAuth access token in
``~/.claude/.credentials.json``; the same endpoint the CLI's ``/usage`` command
uses (``https://api.anthropic.com/api/oauth/usage``) returns the session /
weekly rate-limit windows. This adapter implements
:class:`app.ai_usage.claude_quota.ClaudeQuotaAdapter` with these bounds:

- the token is read into memory for one request and never stored, logged,
  audited, or returned; the credentials file is read once per fetch with a
  size bound and never written;
- exactly one network destination (``api.anthropic.com``), one GET, a short
  timeout, a bounded response; no retries, no token refresh, no CLI calls;
- any failure degrades to ``account_quota`` ``unavailable`` with a classified
  reason -- the projection's other categories are untouched;
- results are cached per home directory for :data:`FETCH_TTL_SECONDS` so the
  Overview poll never turns into a request per page view.

The endpoint is undocumented; unknown response shapes yield
``oauth_response_invalid`` rather than guesses.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.ai_usage.common import (
    AVAILABLE,
    account_quota_unavailable,
    iso,
    safe_percent,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SOURCE = "claude_oauth_usage_api"
CREDENTIALS_RELATIVE_PATH = Path(".claude") / ".credentials.json"
MAX_CREDENTIALS_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
FETCH_TTL_SECONDS = 60.0

REASON_CREDENTIALS_MISSING = "oauth_credentials_missing"
REASON_TOKEN_REJECTED = "oauth_token_rejected"
REASON_RATE_LIMITED = "oauth_rate_limited"
REASON_UNREACHABLE = "oauth_api_unreachable"
REASON_RESPONSE_INVALID = "oauth_response_invalid"

#: Known window keys -> (label, minutes). Unknown keys are still surfaced
#: with their key as label so new quota types appear without a code change.
WINDOW_LABELS: dict[str, tuple[str, Optional[int]]] = {
    "five_hour": ("5 小時", 300),
    "seven_day": ("7 天", 10080),
    "seven_day_sonnet": ("7 天（Sonnet）", 10080),
    "seven_day_opus": ("7 天（Opus）", 10080),
    "seven_day_fable": ("7 天（Fable）", 10080),
    "seven_day_cowork": ("7 天（Cowork）", 10080),
}
_WINDOW_ORDER = ["five_hour", "seven_day"]

HttpGet = Callable[[str, dict[str, str], float], tuple[int, bytes]]

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def read_oauth_access_token(home: Path) -> Optional[str]:
    """Return the Claude Code OAuth access token or ``None``. Never raises,
    never logs, never returns anything but the token string."""

    path = home / CREDENTIALS_RELATIVE_PATH
    try:
        if not path.is_file() or path.stat().st_size > MAX_CREDENTIALS_BYTES:
            return None
        with path.open("rb") as handle:
            data = json.loads(handle.read(MAX_CREDENTIALS_BYTES).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _default_http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("GET", url, headers=headers) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("response too large")
            return response.status_code, body


def _parse_resets_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_usage_response(data: Any, *, now: datetime) -> dict[str, Any]:
    """Map the usage payload onto the ``AccountQuota`` shape. Every top-level
    object carrying ``utilization`` is one window; nothing else is read."""

    if not isinstance(data, dict):
        return account_quota_unavailable(REASON_RESPONSE_INVALID)
    windows: list[dict[str, Any]] = []
    keys = [k for k in _WINDOW_ORDER if k in data] + sorted(k for k in data if k not in _WINDOW_ORDER)
    for key in keys:
        value = data.get(key)
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        used = safe_percent(value.get("utilization"))
        resets_at = _parse_resets_at(value.get("resets_at"))
        label, minutes = WINDOW_LABELS.get(key, (key, None))
        windows.append(
            {
                "id": key,
                "label": label,
                "window_minutes": minutes,
                "used_percent": used,
                "resets_at": iso(resets_at),
                "state": "expired" if resets_at is not None and resets_at <= now else "current",
            }
        )
    if not windows:
        return account_quota_unavailable(REASON_RESPONSE_INVALID)
    return {
        "availability": AVAILABLE,
        "reason": None,
        "source": SOURCE,
        "plan_type": None,
        "limit_id": None,
        "observed_at": iso(now),
        "windows": windows,
    }


def fetch_claude_oauth_quota(
    home: Path,
    *,
    now: Optional[datetime] = None,
    http_get: Optional[HttpGet] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """One bounded fetch (cached per ``home``). Never raises."""

    key = str(home)
    if use_cache:
        with _cache_lock:
            entry = _cache.get(key)
            if entry is not None and time.monotonic() - entry[0] < FETCH_TTL_SECONDS:
                return json.loads(json.dumps(entry[1]))
    current = now or datetime.now(timezone.utc)
    result = _fetch_uncached(home, now=current, http_get=http_get or _default_http_get)
    if use_cache:
        with _cache_lock:
            _cache[key] = (time.monotonic(), result)
    return json.loads(json.dumps(result))


def _fetch_uncached(home: Path, *, now: datetime, http_get: HttpGet) -> dict[str, Any]:
    token = read_oauth_access_token(home)
    if token is None:
        return account_quota_unavailable(REASON_CREDENTIALS_MISSING)
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "dispatch-center-ai-usage/1",
    }
    try:
        status, body = http_get(USAGE_URL, headers, REQUEST_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - never surface transport detail (could echo headers)
        return account_quota_unavailable(REASON_UNREACHABLE)
    finally:
        del token, headers
    if status in (401, 403):
        return account_quota_unavailable(REASON_TOKEN_REJECTED)
    if status == 429:
        return account_quota_unavailable(REASON_RATE_LIMITED)
    if status != 200:
        return account_quota_unavailable(REASON_UNREACHABLE)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return account_quota_unavailable(REASON_RESPONSE_INVALID)
    return parse_usage_response(data, now=now)


class ClaudeOAuthQuotaAdapter:
    """``ClaudeQuotaAdapter`` bound to one home directory."""

    def __init__(self, home: Path, *, http_get: Optional[HttpGet] = None) -> None:
        self._home = home
        self._http_get = http_get

    def fetch(self) -> dict[str, Any]:
        return fetch_claude_oauth_quota(self._home, http_get=self._http_get)


__all__ = [
    "ClaudeOAuthQuotaAdapter",
    "FETCH_TTL_SECONDS",
    "MAX_CREDENTIALS_BYTES",
    "MAX_RESPONSE_BYTES",
    "REASON_CREDENTIALS_MISSING",
    "REASON_RATE_LIMITED",
    "REASON_RESPONSE_INVALID",
    "REASON_TOKEN_REJECTED",
    "REASON_UNREACHABLE",
    "REQUEST_TIMEOUT_SECONDS",
    "SOURCE",
    "USAGE_URL",
    "clear_cache",
    "fetch_claude_oauth_quota",
    "parse_usage_response",
    "read_oauth_access_token",
]
