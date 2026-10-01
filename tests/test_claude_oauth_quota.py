"""DG-AI-USAGE-OVERVIEW-v2 pins: Claude subscription quota via the Claude Code
OAuth usage endpoint.

- the token is read from ``~/.claude/.credentials.json`` into memory only and
  never appears in the projection, the route response, or logs;
- exactly one destination (``api.anthropic.com``), one bounded GET, no retry;
- failures degrade to ``unavailable`` with a classified reason;
- the flag gates the adapter; off keeps ``requires_credentialed_api``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ai_usage import claude_oauth_quota as oq
from app.ai_usage import clear_cache as clear_projection_cache
from app.ai_usage.projection import build_ai_provider_quota_projection

TOKEN = "sk-ant-oat01-SECRET-TOKEN-VALUE"
NOW = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_caches():
    oq.clear_cache()
    clear_projection_cache()
    yield
    oq.clear_cache()
    clear_projection_cache()


def _home_with_token(tmp_path: Path, token: str = TOKEN) -> Path:
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "r-SECRET", "expiresAt": 1}}))
    return tmp_path


def _usage_body(now: datetime = NOW) -> bytes:
    return json.dumps(
        {
            "five_hour": {"utilization": 42.5, "resets_at": (now + timedelta(hours=2)).isoformat()},
            "seven_day": {"utilization": 12.0, "resets_at": (now + timedelta(days=3)).isoformat().replace("+00:00", "Z")},
            "seven_day_opus": {"utilization": 0, "resets_at": None},
            "extra_usage": {"enabled": False},
            "limits": [{"group": "weekly", "percent": 3}],
        }
    ).encode()


def test_read_token_is_bounded_and_never_raises(tmp_path):
    assert oq.read_oauth_access_token(tmp_path) is None
    home = _home_with_token(tmp_path)
    assert oq.read_oauth_access_token(home) == TOKEN
    creds = home / ".claude" / ".credentials.json"
    creds.write_text("{not json")
    assert oq.read_oauth_access_token(home) is None
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}))
    assert oq.read_oauth_access_token(home) is None
    creds.write_bytes(b"{" + b" " * (oq.MAX_CREDENTIALS_BYTES + 1) + b"}")
    assert oq.read_oauth_access_token(home) is None


def test_fetch_uses_single_pinned_destination_and_bounded_request(tmp_path):
    home = _home_with_token(tmp_path)
    calls: list[tuple[str, dict[str, str], float]] = []

    def http_get(url, headers, timeout):
        calls.append((url, dict(headers), timeout))
        return 200, _usage_body()

    quota = oq.fetch_claude_oauth_quota(home, now=NOW, http_get=http_get, use_cache=False)
    assert calls[0][0] == oq.USAGE_URL == "https://api.anthropic.com/api/oauth/usage"
    assert calls[0][1]["Authorization"] == f"Bearer {TOKEN}"
    assert calls[0][1]["anthropic-beta"] == "oauth-2025-04-20"
    assert calls[0][2] == oq.REQUEST_TIMEOUT_SECONDS <= 10
    assert quota["availability"] == "available" and quota["source"] == oq.SOURCE
    assert quota["observed_at"] == NOW.isoformat()
    assert [w["id"] for w in quota["windows"]] == ["five_hour", "seven_day", "seven_day_opus"]
    five = quota["windows"][0]
    assert five == {
        "id": "five_hour", "label": "5 小時", "window_minutes": 300, "used_percent": 42.5,
        "resets_at": (NOW + timedelta(hours=2)).isoformat(), "state": "current",
    }
    assert quota["windows"][1]["resets_at"] == (NOW + timedelta(days=3)).isoformat()
    assert quota["windows"][2]["resets_at"] is None and quota["windows"][2]["used_percent"] == 0.0
    assert TOKEN not in json.dumps(quota) and "SECRET" not in json.dumps(quota)


@pytest.mark.parametrize(
    "status, body, reason",
    [
        (401, b"{}", oq.REASON_TOKEN_REJECTED),
        (403, b"{}", oq.REASON_TOKEN_REJECTED),
        (429, b"{}", oq.REASON_RATE_LIMITED),
        (500, b"{}", oq.REASON_UNREACHABLE),
        (200, b"not json", oq.REASON_RESPONSE_INVALID),
        (200, b"[]", oq.REASON_RESPONSE_INVALID),
        (200, b'{"extra_usage": {"enabled": true}}', oq.REASON_RESPONSE_INVALID),
    ],
)
def test_failures_are_classified_without_secrets(tmp_path, status, body, reason):
    home = _home_with_token(tmp_path)
    quota = oq.fetch_claude_oauth_quota(home, now=NOW, http_get=lambda *a: (status, body), use_cache=False)
    assert quota["availability"] == "unavailable" and quota["reason"] == reason
    assert quota["windows"] == [] and TOKEN not in json.dumps(quota)


def test_transport_error_and_missing_credentials(tmp_path):
    def boom(url, headers, timeout):
        raise OSError(f"dns failed while sending {headers['Authorization']}")

    home = _home_with_token(tmp_path)
    quota = oq.fetch_claude_oauth_quota(home, now=NOW, http_get=boom, use_cache=False)
    assert quota["reason"] == oq.REASON_UNREACHABLE and TOKEN not in json.dumps(quota)
    calls = 0

    def never(url, headers, timeout):
        nonlocal calls
        calls += 1
        return 200, _usage_body()

    quota = oq.fetch_claude_oauth_quota(tmp_path / "empty", now=NOW, http_get=never, use_cache=False)
    assert quota["reason"] == oq.REASON_CREDENTIALS_MISSING and calls == 0


def test_expired_window_state_and_cache(tmp_path):
    home = _home_with_token(tmp_path)
    past = json.dumps({"five_hour": {"utilization": 99, "resets_at": (NOW - timedelta(minutes=1)).isoformat()}}).encode()
    calls = 0

    def http_get(url, headers, timeout):
        nonlocal calls
        calls += 1
        return 200, past

    first = oq.fetch_claude_oauth_quota(home, now=NOW, http_get=http_get)
    second = oq.fetch_claude_oauth_quota(home, now=NOW, http_get=http_get)
    assert first["windows"][0]["state"] == "expired"
    assert second == first and calls == 1
    second["windows"][0]["used_percent"] = 1.0  # cached copies are independent
    assert oq.fetch_claude_oauth_quota(home, now=NOW, http_get=http_get)["windows"][0]["used_percent"] == 99.0


def test_projection_uses_adapter_and_keeps_categories_separate(tmp_path):
    home = _home_with_token(tmp_path)
    adapter = oq.ClaudeOAuthQuotaAdapter(home, http_get=lambda *a: (200, _usage_body()))
    projection = build_ai_provider_quota_projection(home, now=NOW, claude_quota=adapter, use_cache=False)
    claude = projection["providers"]["claude_code"]
    assert claude["account_quota"]["availability"] == "available"
    assert claude["account_quota"]["source"] == oq.SOURCE
    assert claude["local_usage"]["availability"] == "unavailable"  # no session files in tmp home
    assert claude["estimated_cost"]["availability"] == "unavailable"
    assert TOKEN not in json.dumps(projection)


def test_route_flag_gates_adapter_and_never_leaks_token(api_client, tmp_path, monkeypatch):
    client, main_module = api_client
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.ai_usage_v1_enabled = True
    config.ai_usage_home_dir = str(_home_with_token(tmp_path))
    calls = 0

    def http_get(url, headers, timeout):
        nonlocal calls
        calls += 1
        return 200, _usage_body()

    monkeypatch.setattr(oq, "_default_http_get", http_get)

    config.ai_usage_claude_oauth_quota_enabled = False
    off = client.get("/api/v2/ai-providers/quota")
    assert off.status_code == 200, off.text
    assert off.json()["providers"]["claude_code"]["account_quota"]["reason"] == "requires_credentialed_api"
    assert calls == 0

    clear_projection_cache()
    config.ai_usage_claude_oauth_quota_enabled = True
    on = client.get("/api/v2/ai-providers/quota")
    assert on.status_code == 200, on.text
    quota = on.json()["providers"]["claude_code"]["account_quota"]
    assert quota["availability"] == "available" and quota["windows"][0]["id"] == "five_hour"
    assert calls == 1
    assert TOKEN not in on.text and "SECRET" not in on.text
    assert on.headers["Cache-Control"] == "no-store"
