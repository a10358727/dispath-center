"""Goal 3 C2：Node Agent 協議純函式（INV-NODE-2/3/4/5）。

全部是純資料 + 時間戳的測試，不碰 HTTP/DB/SSH/真實機器（INV-TEST-2）。
每組測試對應一條不變量，roadmap Phase 3 列的強制情境（重複 poll、重複
ack、agent 重啟、control plane 重啟、啟動前/後斷線、stale lease、
malformed/unauthorized payload）都在這裡以 fake 時序覆蓋。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.node_protocol import (
    AttemptStatus,
    HeartbeatState,
    NodeAttempt,
    build_node_launcher_argv,
    can_dispatch_job,
    can_lease,
    command_digest,
    evaluate_ack,
    heartbeat_state,
    is_lease_expired,
    next_lease_expiry,
    plan_agent_restart,
    verify_command_digest,
)


T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
DIGEST = command_digest("python train.py")


def _attempt(**kw) -> NodeAttempt:
    defaults = dict(
        id="11111111-1111-4111-8111-111111111111",
        job_id=7,
        node_id="node-a",
        status=AttemptStatus.LEASED,
        command_sha256=DIGEST,
        lease_expires_at=T0 + timedelta(seconds=60),
    )
    defaults.update(kw)
    return NodeAttempt(**defaults)


# ---------------------------------------------------------------------------
# INV-NODE-2：lease 語意
# ---------------------------------------------------------------------------


def test_lease_expiry_is_time_based_before_ack():
    attempt = _attempt()
    assert is_lease_expired(attempt, T0) is False
    assert is_lease_expired(attempt, T0 + timedelta(seconds=59)) is False
    assert is_lease_expired(attempt, T0 + timedelta(seconds=60)) is True


def test_acked_attempt_never_counts_as_lease_expired():
    """ack 之後 lease 不再是判準——語意交給心跳與終態（INV-NODE-4/5）。"""
    attempt = _attempt(status=AttemptStatus.ACKED, acked_at=T0)
    assert is_lease_expired(attempt, T0 + timedelta(days=365)) is False


def test_first_poll_can_lease():
    assert can_lease(None, "node-a", T0) is True


def test_repeat_poll_from_same_node_is_idempotent():
    """重複 poll 不得產生第二個 attempt——同一個 node 拿回同一份工作。"""
    attempt = _attempt()
    assert can_lease(attempt, "node-a", T0 + timedelta(seconds=5)) is True


def test_second_node_cannot_steal_live_lease():
    attempt = _attempt()
    assert can_lease(attempt, "node-b", T0 + timedelta(seconds=5)) is False


def test_second_node_may_reclaim_expired_unacked_lease():
    """從未 ack ⇒ 沒有副作用產生過 ⇒ 可安全回收。"""
    attempt = _attempt()
    assert can_lease(attempt, "node-b", T0 + timedelta(seconds=120)) is True


@pytest.mark.parametrize("status", [AttemptStatus.ACKED, AttemptStatus.RUNNING])
def test_acked_attempt_is_never_releasable_to_anyone(status):
    """已 ack 過的 attempt 就算失聯很久也不得再 lease（unknown ≠ 可重派）。"""
    attempt = _attempt(status=status, acked_at=T0)
    later = T0 + timedelta(days=7)
    assert can_lease(attempt, "node-a", later) is False
    assert can_lease(attempt, "node-b", later) is False


@pytest.mark.parametrize(
    "status", [AttemptStatus.DONE, AttemptStatus.FAILED, AttemptStatus.EXPIRED]
)
def test_terminal_attempt_is_never_releasable(status):
    attempt = _attempt(status=status, terminal_at=T0)
    assert can_lease(attempt, "node-a", T0 + timedelta(seconds=1)) is False


# ---------------------------------------------------------------------------
# INV-NODE-2：不得重複派發（含 SSH 通道）
# ---------------------------------------------------------------------------


def test_job_with_no_attempts_is_dispatchable():
    assert can_dispatch_job([], T0) is True


def test_job_with_live_lease_is_not_dispatchable():
    """lease 還在有效期內——不得同時走 SSH 再派一次。"""
    assert can_dispatch_job([_attempt()], T0 + timedelta(seconds=10)) is False


def test_job_with_acked_attempt_is_not_dispatchable_even_when_silent():
    attempt = _attempt(status=AttemptStatus.RUNNING, acked_at=T0)
    assert can_dispatch_job([attempt], T0 + timedelta(days=1)) is False


def test_job_with_expired_unacked_lease_is_dispatchable_again():
    assert can_dispatch_job([_attempt()], T0 + timedelta(seconds=120)) is True


def test_job_with_only_terminal_attempts_is_dispatchable():
    attempts = [
        _attempt(status=AttemptStatus.FAILED, terminal_at=T0),
        _attempt(status=AttemptStatus.EXPIRED, terminal_at=T0),
    ]
    assert can_dispatch_job(attempts, T0 + timedelta(seconds=1)) is True


def test_one_live_attempt_blocks_despite_other_terminal_ones():
    attempts = [
        _attempt(status=AttemptStatus.DONE, terminal_at=T0),
        _attempt(status=AttemptStatus.ACKED, acked_at=T0),
    ]
    assert can_dispatch_job(attempts, T0 + timedelta(seconds=1)) is False


# ---------------------------------------------------------------------------
# INV-NODE-2/3：acknowledgement
# ---------------------------------------------------------------------------


def test_ack_accepted_for_live_lease_with_matching_digest():
    outcome = evaluate_ack(_attempt(), "node-a", DIGEST, T0 + timedelta(seconds=5))
    assert outcome.accepted is True
    assert outcome.duplicate is False


def test_duplicate_ack_is_idempotent_not_a_second_execution():
    attempt = _attempt(status=AttemptStatus.ACKED, acked_at=T0)
    outcome = evaluate_ack(attempt, "node-a", DIGEST, T0 + timedelta(seconds=5))
    assert outcome.accepted is True
    assert outcome.duplicate is True


def test_ack_rejected_for_unknown_attempt():
    outcome = evaluate_ack(None, "node-a", DIGEST, T0)
    assert outcome.accepted is False
    assert "not found" in outcome.reason


def test_ack_rejected_from_wrong_node():
    """unauthorized payload：別的 node 不能 ack 不屬於它的 attempt。"""
    outcome = evaluate_ack(_attempt(), "node-b", DIGEST, T0)
    assert outcome.accepted is False
    assert "another node" in outcome.reason


def test_ack_rejected_on_digest_mismatch():
    """INV-NODE-3：digest 不符一律 fail-closed，不執行。"""
    outcome = evaluate_ack(_attempt(), "node-a", command_digest("rm -rf /"), T0)
    assert outcome.accepted is False
    assert "digest mismatch" in outcome.reason


def test_ack_rejected_after_lease_expired():
    outcome = evaluate_ack(_attempt(), "node-a", DIGEST, T0 + timedelta(seconds=120))
    assert outcome.accepted is False
    assert "lease expired" in outcome.reason


@pytest.mark.parametrize(
    "status", [AttemptStatus.DONE, AttemptStatus.FAILED, AttemptStatus.EXPIRED]
)
def test_ack_rejected_for_terminal_attempt(status):
    outcome = evaluate_ack(_attempt(status=status, terminal_at=T0), "node-a", DIGEST, T0)
    assert outcome.accepted is False


@pytest.mark.parametrize("bad", [None, 123, "", "not-a-digest"])
def test_ack_rejects_malformed_digest(bad):
    outcome = evaluate_ack(_attempt(), "node-a", bad, T0)
    assert outcome.accepted is False


# ---------------------------------------------------------------------------
# INV-NODE-3：非插值落地
# ---------------------------------------------------------------------------


def test_launcher_returns_argv_list_not_shell_string():
    argv = build_node_launcher_argv("abc-123", "/home/w/.dc/attempts/abc-123")
    assert isinstance(argv, list)
    assert argv == ["/bin/bash", "/home/w/.dc/attempts/abc-123/cmd.sh"]


@pytest.mark.parametrize(
    "attempt_id",
    ["a; rm -rf /", "a$(whoami)", "a`id`", "../../etc/passwd", "a b", "", "a|b", "a&b"],
)
def test_launcher_rejects_unsafe_attempt_ids(attempt_id):
    """自由文字結構性地進不了啟動器。"""
    with pytest.raises(ValueError):
        build_node_launcher_argv(attempt_id, "/tmp/x")


def test_launcher_never_contains_user_command_text():
    """使用者指令原文只透過檔案落地，永不出現在 argv 裡。"""
    argv = build_node_launcher_argv("abc-123", "/tmp/x")
    assert not any("train.py" in part for part in argv)


def test_command_digest_round_trip():
    assert verify_command_digest("python train.py", DIGEST) is True
    assert verify_command_digest("python evil.py", DIGEST) is False


# ---------------------------------------------------------------------------
# INV-NODE-4：心跳過期＝unknown，不是 failed
# ---------------------------------------------------------------------------


def test_heartbeat_fresh_within_ttl():
    assert heartbeat_state(T0, T0 + timedelta(seconds=10), ttl_sec=30) is (
        HeartbeatState.FRESH
    )


def test_heartbeat_stale_then_unknown_with_grace():
    at = lambda s: heartbeat_state(  # noqa: E731
        T0, T0 + timedelta(seconds=s), ttl_sec=30, grace_sec=30
    )
    assert at(31) is HeartbeatState.STALE
    assert at(60) is HeartbeatState.STALE
    assert at(61) is HeartbeatState.UNKNOWN


def test_missing_heartbeat_is_unknown_not_fresh():
    assert heartbeat_state(None, T0, ttl_sec=30) is HeartbeatState.UNKNOWN


def test_heartbeat_state_has_no_failed_value():
    """INV-NODE-4 的結構性保證：值域裡根本沒有 failed。"""
    assert {s.value for s in HeartbeatState} == {"fresh", "stale", "unknown"}


def test_next_lease_expiry_is_pure():
    assert next_lease_expiry(T0, 90) == T0 + timedelta(seconds=90)


# ---------------------------------------------------------------------------
# INV-NODE-5：重啟不重複
# ---------------------------------------------------------------------------


def test_restart_resumes_monitoring_when_process_alive():
    plan = plan_agent_restart(
        _attempt(status=AttemptStatus.RUNNING, acked_at=T0),
        local_process_alive=True,
        now=T0 + timedelta(seconds=10),
    )
    assert plan.may_launch is False
    assert plan.resume_monitoring is True


def test_restart_never_relaunches_acked_attempt_without_live_process():
    """斷線發生在啟動之後：unknown，絕不重跑（這條就是零重複啟動的根）。"""
    plan = plan_agent_restart(
        _attempt(status=AttemptStatus.ACKED, acked_at=T0),
        local_process_alive=False,
        now=T0 + timedelta(seconds=10),
    )
    assert plan.may_launch is False
    assert "never relaunch" in plan.reason


def test_restart_may_launch_when_leased_but_never_acked():
    """斷線發生在啟動之前：沒有副作用，可以安全啟動。"""
    plan = plan_agent_restart(
        _attempt(), local_process_alive=False, now=T0 + timedelta(seconds=5)
    )
    assert plan.may_launch is True


def test_restart_drops_expired_unacked_attempt():
    plan = plan_agent_restart(
        _attempt(), local_process_alive=False, now=T0 + timedelta(seconds=120)
    )
    assert plan.may_launch is False
    assert plan.resume_monitoring is False


@pytest.mark.parametrize(
    "status", [AttemptStatus.DONE, AttemptStatus.FAILED, AttemptStatus.EXPIRED]
)
def test_restart_ignores_terminal_attempts(status):
    plan = plan_agent_restart(
        _attempt(status=status, terminal_at=T0), local_process_alive=False, now=T0
    )
    assert plan.may_launch is False
    assert plan.resume_monitoring is False
