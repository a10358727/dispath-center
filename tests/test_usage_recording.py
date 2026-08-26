"""Packet D3: `app.usage_recording` -- the shared, always-safe assistant
usage recorder every producer channel goes through. No real DB I/O beyond a
tmp-path `Database` fixture; the point of this suite is the *failure*
behavior (never raises, never affects the caller) and the closure shape."""

from __future__ import annotations

import logging

from app.db import Database
from app.usage_recording import make_usage_recorder, safe_record_assistant_usage


def test_safe_record_assistant_usage_inserts_a_row(tmp_path):
    db = Database(str(tmp_path / "usage.db"))
    safe_record_assistant_usage(
        db,
        channel="runner_claude",
        server="server-a",
        model="sonnet",
        input_tokens=10,
        output_tokens=20,
        duration_ms=1234,
    )
    summary = db.get_assistant_usage_summary(7)
    assert summary["totals"] == {"turns": 1, "input_tokens": 10, "output_tokens": 20}
    db.close()


def test_safe_record_assistant_usage_none_db_is_a_silent_noop():
    # Must not raise even without a database at all.
    safe_record_assistant_usage(None, channel="vllm", input_tokens=1)


def test_safe_record_assistant_usage_invalid_channel_is_logged_not_raised(
    tmp_path, caplog
):
    db = Database(str(tmp_path / "usage.db"))
    with caplog.at_level(logging.WARNING):
        safe_record_assistant_usage(db, channel="not-a-real-channel")
    assert "記錄 assistant usage 失敗" in caplog.text
    summary = db.get_assistant_usage_summary(7)
    assert summary["totals"]["turns"] == 0
    db.close()


def test_make_usage_recorder_binds_channel_server_model(tmp_path):
    db = Database(str(tmp_path / "usage.db"))
    record = make_usage_recorder(db, channel="api", model="claude-opus-4")
    record({"input_tokens": 5, "output_tokens": 7})

    summary = db.get_assistant_usage_summary(7)
    assert summary["totals"] == {"turns": 1, "input_tokens": 5, "output_tokens": 7}
    breakdown = summary["breakdown"][0]
    assert breakdown["channel"] == "api"
    assert breakdown["model"] == "claude-opus-4"
    db.close()


def test_make_usage_recorder_closure_never_raises_on_bad_usage_shape(tmp_path):
    db = Database(str(tmp_path / "usage.db"))
    record = make_usage_recorder(db, channel="vllm")
    # Falsy/None usage must not raise -- defensive callers may pass either.
    record(None)
    record({})
    summary = db.get_assistant_usage_summary(7)
    assert summary["totals"]["turns"] == 2
    db.close()
