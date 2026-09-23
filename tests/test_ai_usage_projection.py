"""DG-AI-USAGE-OVERVIEW-v1 pins: bounded, read-only local AI usage projection.

Fixtures live in ``tests/fixtures/ai_usage`` (deterministic JSONL plus decoy
credential/history files carrying ``FIXTURE-SECRET-MARKER-DO-NOT-LEAK``).
``make_home`` copies them into a temporary home and pins mtimes so "today" is
2026-09-23 (UTC) for every test.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ai_usage import build_ai_provider_quota_projection, clear_cache
from app.ai_usage import common
from app.ai_usage.claude_quota import UnavailableClaudeQuotaAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "ai_usage"
NOW = datetime(2026, 9, 23, 1, 30, tzinfo=timezone.utc)
TODAY_START = datetime(2026, 9, 23, tzinfo=timezone.utc)
SECRET = "FIXTURE-SECRET-MARKER-DO-NOT-LEAK"
MARKERS = (SECRET, "FIXTURE-PROMPT-MARKER", "FIXTURE-TRANSCRIPT-MARKER", "FIXTURE-PATH-MARKER")
DECOYS = (
    ".codex/auth.json",
    ".codex/history.jsonl",
    ".codex/logs_2.sqlite",
    ".codex/session_index.jsonl",
    ".claude/.credentials.json",
    ".claude/history.jsonl",
    ".claude/stats-cache.json",
    ".claude/settings.json",
)


def _touch(path: Path, when: datetime) -> None:
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def make_home(tmp_path: Path, *, codex: bool = True, claude: bool = True) -> Path:
    """Copy the fixture tree into ``tmp_path/home`` with deterministic mtimes."""

    home = tmp_path / "home"
    home.mkdir()
    if codex:
        shutil.copytree(FIXTURES / "codex", home / ".codex")
    if claude:
        shutil.copytree(FIXTURES / "claude", home / ".claude")
    for path in home.rglob("*.jsonl"):
        text = path.name
        if "2026-09-23T01" in text or text == "session-fixture.jsonl":
            _touch(path, NOW - timedelta(minutes=5))
        elif "2026-09-23T03" in text:
            _touch(path, NOW - timedelta(minutes=4))
        else:
            _touch(path, TODAY_START - timedelta(hours=14))
    return home


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def opened(monkeypatch):
    """Spy on the package's single file-open entry point."""

    paths: list[Path] = []
    real = common.open_binary

    def spy(path: Path):
        paths.append(Path(path))
        return real(path)

    monkeypatch.setattr(common, "open_binary", spy)
    return paths


def _build(home: Path, *, now: datetime = NOW, use_cache: bool = False) -> dict:
    return build_ai_provider_quota_projection(home, now=now, use_cache=use_cache)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def test_codex_rate_limit_snapshot_and_window_states(tmp_path):
    projection = _build(make_home(tmp_path))
    quota = projection["providers"]["codex"]["account_quota"]

    assert quota["availability"] == "available"
    assert quota["reason"] is None
    assert quota["source"] == "codex_session_jsonl"
    assert quota["plan_type"] == "plus"
    assert quota["limit_id"] == "codex"
    assert quota["observed_at"] == "2026-09-23T01:10:00+00:00"
    primary, secondary = quota["windows"]
    assert primary == {
        "id": "primary",
        "label": "5h",
        "window_minutes": 300,
        "used_percent": 95.0,
        "resets_at": "2026-09-23T02:56:00+00:00",
        "state": "current",
    }
    assert secondary["label"] == "weekly"
    assert secondary["used_percent"] == 100.0
    assert secondary["state"] == "expired"


def test_codex_newest_event_without_windows_falls_back_to_older_snapshot(tmp_path):
    """The newest token_count (01:25) carries null windows and a different
    limit_id; the snapshot must come from the 01:10 event instead of being
    reported as missing."""

    projection = _build(make_home(tmp_path))
    quota = projection["providers"]["codex"]["account_quota"]
    assert quota["limit_id"] == "codex"
    assert quota["observed_at"] == "2026-09-23T01:10:00+00:00"


def test_codex_stale_snapshot_and_stale_window_state(tmp_path):
    home = make_home(tmp_path)
    later = datetime(2026, 9, 23, 2, 30, tzinfo=timezone.utc)
    quota = _build(home, now=later)["providers"]["codex"]["account_quota"]
    assert quota["availability"] == "stale"
    assert quota["reason"] == "stale_snapshot"
    states = {window["id"]: window["state"] for window in quota["windows"]}
    assert states == {"primary": "stale", "secondary": "expired"}

    much_later = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    quota = _build(home, now=much_later)["providers"]["codex"]["account_quota"]
    assert quota["availability"] == "stale"
    assert {window["state"] for window in quota["windows"]} == {"expired"}


def test_codex_today_tokens_from_usage_records_and_cumulative_fallback(tmp_path):
    usage = _build(make_home(tmp_path))["providers"]["codex"]["local_usage"]

    assert usage["availability"] == "available"
    assert usage["source"] == "codex_session_jsonl"
    # file A: two token_usage_record lines today (yesterday's excluded);
    # file C: no records -> cumulative delta 1750 - 1100.
    assert usage["today"] == {
        "input_tokens": 1200 + 600,
        "cached_input_tokens": 300,
        "output_tokens": 130 + 50,
        "reasoning_output_tokens": 50,
        "total_tokens": 1330 + 650,
    }
    assert usage["sessions_today"] == 2
    assert usage["events_today"] == 3
    assert usage["newest_event_at"] == "2026-09-23T01:20:00+00:00"
    assert usage["models_today"] == {"gpt-5-codex": 1330, "unknown": 650}
    assert usage["scan"]["files_considered"] == 3
    assert usage["scan"]["truncated"] is False


def test_codex_context_usage_from_newest_token_count(tmp_path):
    context = _build(make_home(tmp_path))["providers"]["codex"]["context_usage"]
    assert context == {
        "availability": "available",
        "reason": None,
        "model": "gpt-5-codex",
        "used_tokens": 155000,
        "context_window": 256000,
        "used_percent": 60.5,
        "observed_at": "2026-09-23T01:25:00+00:00",
    }


def test_codex_malformed_partial_and_oversized_lines_are_skipped(tmp_path):
    home = make_home(tmp_path)
    target = next((home / ".codex" / "sessions").rglob("*aaaaaaaa-fixture.jsonl"))
    huge = json.dumps(
        {
            "timestamp": "2026-09-23T01:26:00.000Z",
            "type": "token_usage_record",
            "payload": {"usage": {"total_tokens": 10**9}, "filler": "x" * (common.MAX_LINE_BYTES + 10)},
        }
    )
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(huge + "\n")
    _touch(target, NOW - timedelta(minutes=5))

    usage = _build(home)["providers"]["codex"]["local_usage"]
    assert usage["today"]["total_tokens"] == 1980  # oversized record ignored
    # malformed JSON line + payload-less token_count + oversized line
    assert usage["scan"]["lines_skipped"] >= 2


def test_missing_home_and_missing_session_dirs(tmp_path):
    missing = tmp_path / "nowhere"
    projection = _build(missing)
    for provider in ("codex", "claude_code"):
        block = projection["providers"][provider]
        assert block["local_usage"]["availability"] == "unavailable"
        assert block["local_usage"]["reason"] == "home_missing"
        assert block["local_usage"]["today"] is None
        assert block["context_usage"]["reason"] == "home_missing"
    assert projection["providers"]["codex"]["account_quota"]["reason"] == "home_missing"
    # Claude account quota is unavailable by design regardless of files.
    assert projection["providers"]["claude_code"]["account_quota"]["reason"] == "requires_credentialed_api"

    empty = tmp_path / "empty-home"
    (empty / ".codex").mkdir(parents=True)
    (empty / ".claude" / "projects").mkdir(parents=True)
    projection = _build(empty)
    assert projection["providers"]["codex"]["local_usage"]["reason"] == "no_session_files"
    assert projection["providers"]["codex"]["account_quota"]["reason"] == "no_session_files"
    assert projection["providers"]["claude_code"]["local_usage"]["reason"] == "no_session_files"


def test_no_secret_prompt_or_path_leaks_and_decoys_never_opened(tmp_path, opened):
    home = make_home(tmp_path)
    projection = _build(home)
    serialized = json.dumps(projection)
    for marker in MARKERS:
        assert marker not in serialized
    assert str(home) not in serialized
    opened_names = {path.resolve() for path in opened}
    for decoy in DECOYS:
        assert (home / decoy).resolve() not in opened_names, decoy
    assert opened_names, "fixture session files were read"
    assert all(path.suffix == ".jsonl" for path in opened_names)
    assert all(
        (home / ".codex" / "sessions") in path.parents or (home / ".claude" / "projects") in path.parents
        for path in opened_names
    )


def test_symlink_escaping_the_session_tree_is_skipped(tmp_path, opened):
    home = make_home(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-23T01:29:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"total_tokens": 1}, "model_context_window": 2},
                    "rate_limits": {"primary": {"used_percent": 1.0, "window_minutes": 300}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    link = home / ".codex" / "sessions" / "2026" / "09" / "23" / "link-fixture.jsonl"
    link.symlink_to(outside)
    _touch(outside, NOW)

    quota = _build(home)["providers"]["codex"]["account_quota"]
    assert quota["windows"][0]["used_percent"] == 95.0
    assert outside.resolve() not in {path.resolve() for path in opened}


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


def test_claude_today_usage_dedupes_and_context_is_partial(tmp_path):
    block = _build(make_home(tmp_path))["providers"]["claude_code"]
    usage = block["local_usage"]
    assert usage["availability"] == "available"
    assert usage["source"] == "claude_code_project_jsonl"
    assert usage["today"] == {
        "input_tokens": 30,
        "output_tokens": 1300,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 50000,
        "total_tokens": 52330,
    }
    assert usage["sessions_today"] == 1
    assert usage["events_today"] == 2
    assert usage["newest_event_at"] == "2026-09-23T01:00:00+00:00"
    assert usage["models_today"] == {"claude-sonnet-5": 52330}
    assert block["context_usage"] == {
        "availability": "partial",
        "reason": "context_window_unknown",
        "model": "claude-sonnet-5",
        "used_tokens": 20 + 30000,
        "context_window": None,
        "used_percent": None,
        "observed_at": "2026-09-23T01:00:00+00:00",
    }
    assert block["account_quota"] == {
        "availability": "unavailable",
        "reason": "requires_credentialed_api",
        "source": None,
        "plan_type": None,
        "limit_id": None,
        "observed_at": None,
        "windows": [],
    }


def test_claude_quota_adapter_seam_is_separate_and_optional(tmp_path):
    class FakeAdapter:
        def fetch(self):
            return {
                "availability": "available",
                "reason": None,
                "source": "fake_adapter",
                "plan_type": "max",
                "limit_id": None,
                "observed_at": NOW.isoformat(),
                "windows": [],
            }

    home = make_home(tmp_path)
    default = build_ai_provider_quota_projection(home, now=NOW, use_cache=False)
    assert default["providers"]["claude_code"]["account_quota"]["reason"] == UnavailableClaudeQuotaAdapter.reason
    injected = build_ai_provider_quota_projection(
        home, now=NOW, claude_quota=FakeAdapter(), use_cache=False
    )
    assert injected["providers"]["claude_code"]["account_quota"]["source"] == "fake_adapter"
    # local usage is identical regardless of the adapter
    assert injected["providers"]["claude_code"]["local_usage"] == default["providers"]["claude_code"]["local_usage"]


# ---------------------------------------------------------------------------
# Projection shape, categories, cache
# ---------------------------------------------------------------------------


def test_four_categories_are_distinct_and_cost_is_always_unavailable(tmp_path):
    projection = _build(make_home(tmp_path))
    assert projection["schema"] == "ai-provider-quota-v1"
    assert projection["today"] == {
        "date": "2026-09-23",
        "timezone": "UTC",
        "starts_at": "2026-09-23T00:00:00+00:00",
    }
    for provider, label in (("claude_code", "Claude Code"), ("codex", "Codex")):
        block = projection["providers"][provider]
        assert block["provider"] == provider
        assert block["label"] == label
        assert set(block) == {
            "provider",
            "label",
            "account_quota",
            "local_usage",
            "context_usage",
            "estimated_cost",
        }
        for category in ("account_quota", "local_usage", "context_usage", "estimated_cost"):
            assert "availability" in block[category]
            assert "reason" in block[category]
        assert block["estimated_cost"] == {
            "availability": "unavailable",
            "reason": "no_pricing_source",
            "currency": None,
            "today_usd": None,
        }


def test_cache_serves_repeat_calls_without_rescanning(tmp_path, opened):
    home = make_home(tmp_path)
    first = build_ai_provider_quota_projection(home, now=NOW, use_cache=True)
    reads = len(opened)
    assert reads > 0
    second = build_ai_provider_quota_projection(home, now=NOW, use_cache=True)
    assert second == first
    assert len(opened) == reads
    build_ai_provider_quota_projection(home, now=NOW, use_cache=False)
    assert len(opened) > reads


def test_scan_errors_degrade_to_scan_error_without_raising(tmp_path, monkeypatch):
    home = make_home(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("app.ai_usage.projection.scan_codex", boom)
    projection = _build(home)
    codex = projection["providers"]["codex"]
    assert codex["account_quota"]["reason"] == "scan_error"
    assert codex["local_usage"]["reason"] == "scan_error"
    assert codex["context_usage"]["reason"] == "scan_error"
    assert projection["providers"]["claude_code"]["local_usage"]["availability"] == "available"
