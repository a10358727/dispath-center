from datetime import datetime, timedelta, timezone

from app.stall import parse_log_size, update_stall_state

# ---------------------------------------------------------------------------
# parse_log_size
# ---------------------------------------------------------------------------


def test_parse_log_size_valid_number():
    assert parse_log_size("12345\n") == 12345


def test_parse_log_size_empty_output_is_none():
    # stat 找不到檔案時（2>/dev/null 吞掉錯誤），stdout 是空字串。
    assert parse_log_size("") is None
    assert parse_log_size("   \n") is None


def test_parse_log_size_garbage_is_none():
    assert parse_log_size("stat: cannot stat\n") is None


# ---------------------------------------------------------------------------
# update_stall_state
# ---------------------------------------------------------------------------


def test_update_stall_state_growth_resets_and_not_stalled():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_size, new_changed_at, is_stalled = update_stall_state(
        prev_size=100, prev_changed_at="2026-01-01T11:00:00+00:00",
        current_size=200, now=now, stall_minutes=30,
    )
    assert new_size == 200
    assert new_changed_at == now.isoformat()
    assert is_stalled is False


def test_update_stall_state_first_time_seen_is_growth():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_size, new_changed_at, is_stalled = update_stall_state(
        prev_size=None, prev_changed_at=None, current_size=50, now=now, stall_minutes=30,
    )
    assert new_size == 50
    assert new_changed_at == now.isoformat()
    assert is_stalled is False


def test_update_stall_state_no_growth_under_threshold_not_stalled():
    changed_at = "2026-01-01T11:45:00+00:00"  # 15 分鐘前
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_size, new_changed_at, is_stalled = update_stall_state(
        prev_size=100, prev_changed_at=changed_at, current_size=100, now=now, stall_minutes=30,
    )
    assert new_size == 100
    assert new_changed_at == changed_at  # 沒有變化，changed_at 不變
    assert is_stalled is False


def test_update_stall_state_no_growth_over_threshold_is_stalled():
    changed_at = "2026-01-01T11:00:00+00:00"  # 60 分鐘前
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_size, new_changed_at, is_stalled = update_stall_state(
        prev_size=100, prev_changed_at=changed_at, current_size=100, now=now, stall_minutes=30,
    )
    assert new_size == 100
    assert new_changed_at == changed_at
    assert is_stalled is True


def test_update_stall_state_shrink_counts_as_no_growth():
    changed_at = "2026-01-01T11:00:00+00:00"  # 60 分鐘前
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_size, new_changed_at, is_stalled = update_stall_state(
        prev_size=100, prev_changed_at=changed_at, current_size=50, now=now, stall_minutes=30,
    )
    assert is_stalled is True


def test_update_stall_state_current_size_none_does_not_change_judgement():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_size, new_changed_at, is_stalled = update_stall_state(
        prev_size=100, prev_changed_at="2026-01-01T11:00:00+00:00",
        current_size=None, now=now, stall_minutes=30,
    )
    assert new_size == 100
    assert new_changed_at == "2026-01-01T11:00:00+00:00"
    assert is_stalled is False
