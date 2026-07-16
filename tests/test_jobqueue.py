import pytest

from app.audit import read_audit
from app.jobqueue import (
    CANCELLED,
    DangerousCommandError,
    build_check_exit_code_command,
    build_dispatch_paths,
    build_launch_command,
    build_log_tail_command,
    build_mkdir_command,
    build_run_sh_content,
    cancel_job,
    deps_all_done,
    deps_any_blocked,
    enqueue_job,
    list_dispatchable_jobs,
    refresh_blocked_jobs,
)


# ---------------------------------------------------------------------------
# 依賴判定純函式
# ---------------------------------------------------------------------------


def test_deps_all_done_empty_is_true():
    assert deps_all_done([]) is True


def test_deps_all_done_true_only_when_all_done():
    assert deps_all_done(["done", "done"]) is True
    assert deps_all_done(["done", "running"]) is False


def test_deps_any_blocked():
    assert deps_any_blocked(["done", "queued"]) is False
    assert deps_any_blocked(["failed"]) is True
    assert deps_any_blocked(["blocked"]) is True
    assert deps_any_blocked(["missing"]) is True


# ---------------------------------------------------------------------------
# enqueue：安全指令入列、危險指令拒絕並寫稽核
# ---------------------------------------------------------------------------


def test_enqueue_safe_command_succeeds(db, audit_path):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    assert job.status == "queued"
    assert job.id is not None

    records = read_audit(audit_path)
    assert any(r["action"] == "enqueue" and r["params"]["job_id"] == job.id for r in records)


def test_enqueue_dangerous_command_rejected(db, audit_path):
    with pytest.raises(DangerousCommandError):
        enqueue_job(db, command="rm -rf /", audit_path=audit_path)

    # 沒有任何任務被建立
    assert db.list_jobs() == []

    records = read_audit(audit_path)
    assert any(r["action"] == "reject" and r["result"] == "rejected" for r in records)


# ---------------------------------------------------------------------------
# 狀態機：queued -> running -> done/failed
# ---------------------------------------------------------------------------


def test_status_transition_queued_to_running_to_done(db, audit_path):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    assert job.status == "queued"

    db.update_job(job.id, status="running", server="server-a")
    running_job = db.get_job(job.id)
    assert running_job.status == "running"
    assert running_job.server == "server-a"

    db.update_job(job.id, status="done", exit_code=0)
    done_job = db.get_job(job.id)
    assert done_job.status == "done"
    assert done_job.exit_code == 0


def test_status_transition_queued_to_running_to_failed(db, audit_path):
    job = enqueue_job(db, command="false", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    db.update_job(job.id, status="failed", exit_code=1)
    failed_job = db.get_job(job.id)
    assert failed_job.status == "failed"
    assert failed_job.exit_code == 1


# ---------------------------------------------------------------------------
# 依賴：未完成不可派、failed 依賴標 blocked
# ---------------------------------------------------------------------------


def test_dependency_not_done_job_not_dispatchable(db, audit_path):
    base = enqueue_job(db, command="sleep 10", audit_path=audit_path)
    dependent = enqueue_job(
        db, command="sleep 20", depends_on=[base.id], audit_path=audit_path
    )

    dispatchable_ids = [j.id for j in list_dispatchable_jobs(db)]
    assert base.id in dispatchable_ids
    assert dependent.id not in dispatchable_ids

    # 依賴完成後才可派
    db.update_job(base.id, status="done", exit_code=0)
    dispatchable_ids = [j.id for j in list_dispatchable_jobs(db)]
    assert dependent.id in dispatchable_ids


def test_dependency_failed_marks_dependent_blocked(db, audit_path):
    base = enqueue_job(db, command="sleep 10", audit_path=audit_path)
    dependent = enqueue_job(
        db, command="sleep 20", depends_on=[base.id], audit_path=audit_path
    )

    db.update_job(base.id, status="failed", exit_code=1)
    blocked_ids = refresh_blocked_jobs(db, audit_path=audit_path)

    assert dependent.id in blocked_ids
    assert db.get_job(dependent.id).status == "blocked"

    records = read_audit(audit_path)
    assert any(r["action"] == "blocked" and r["params"]["job_id"] == dependent.id for r in records)


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_queued_job_succeeds(db, audit_path):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    ok = cancel_job(db, job.id, audit_path=audit_path)
    assert ok is True
    assert db.get_job(job.id).status == CANCELLED


def test_cancel_running_job_fails(db, audit_path):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    ok = cancel_job(db, job.id, audit_path=audit_path)
    assert ok is False
    assert db.get_job(job.id).status == "running"


def test_cancel_nonexistent_job_fails(db, audit_path):
    ok = cancel_job(db, 9999, audit_path=audit_path)
    assert ok is False


# ---------------------------------------------------------------------------
# 派發路徑：不得帶 ~/ 前綴（SFTP 不做 tilde 展開，見 app/jobqueue.py 說明）
# ---------------------------------------------------------------------------


def test_dispatch_paths_have_no_tilde_prefix():
    paths = build_dispatch_paths(123)
    for key, value in paths.items():
        assert not value.startswith("~"), (
            f"{key} 路徑不可用 ~ 開頭（SFTP 不會展開 tilde）: {value!r}"
        )

    # 各個組指令的函式也不該混進 ~，確保 shell 端與 SFTP 端路徑語意一致
    for cmd in (
        build_mkdir_command(123),
        build_launch_command(123),
        build_check_exit_code_command(123),
        build_log_tail_command(123),
        build_run_sh_content(123),
    ):
        assert "~" not in cmd, f"指令內容不該出現 ~: {cmd!r}"


def test_log_tail_command_caps_remote_bytes_before_ssh_collection():
    command = build_log_tail_command(123, lines=40)

    assert "tail -n 40" in command
    assert "| tail -c 65536" in command
