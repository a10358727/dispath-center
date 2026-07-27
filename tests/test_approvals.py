import asyncio

import pytest

from app.approvals import (
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    JobNotFoundError,
    JobNotRunningError,
    approve,
    maybe_auto_approve,
    reject,
    request_enqueue_approval,
    request_stop_approval,
)
from app.audit import read_audit
from app.jobqueue import DangerousCommandError, enqueue_job


# ---------------------------------------------------------------------------
# request_enqueue_approval：安全指令建 approval；危險指令直接拒絕不建 approval
# ---------------------------------------------------------------------------


def test_request_enqueue_approval_creates_pending(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", audit_path=audit_path)
    assert approval.kind == "enqueue"
    assert approval.status == "pending"
    assert approval.payload["command"] == "sleep 60"

    # 還沒核准，jobs 表應該是空的
    assert db.list_jobs() == []

    records = read_audit(audit_path)
    assert any(r["action"] == "approval_requested" for r in records)


def test_request_enqueue_approval_dangerous_command_rejected_no_approval(db, audit_path):
    with pytest.raises(DangerousCommandError):
        request_enqueue_approval(db, command="rm -rf /tmp/x", audit_path=audit_path)

    # 不建立 approval，不給核准機會（鐵律第 2 條）
    assert db.list_approvals() == []
    assert db.list_jobs() == []

    records = read_audit(audit_path)
    assert any(r["action"] == "reject" and r["result"] == "rejected" for r in records)


# ---------------------------------------------------------------------------
# 階段 3（Fable 覆核修正 2）：自動模式下，資料集哪都沒快取要在 payload 給警告
# ---------------------------------------------------------------------------


def test_request_enqueue_approval_auto_mode_warns_when_dataset_cached_nowhere(db, audit_path):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    approval = request_enqueue_approval(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    assert approval.payload["warning"] is not None
    assert "defect@v1" in approval.payload["warning"]
    assert "自動模式" in approval.payload["warning"]


def test_request_enqueue_approval_auto_mode_no_warning_when_cached_somewhere(db, audit_path):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    db.upsert_dataset_cache("server-b", "defect", "v1")
    approval = request_enqueue_approval(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    assert approval.payload["warning"] is None


def test_request_enqueue_approval_auto_mode_no_warning_without_dataset_requirement(db, audit_path):
    db.insert_project("proj", "git@x")
    approval = request_enqueue_approval(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    assert approval.payload["warning"] is None


def test_request_enqueue_approval_explicit_pin_server_has_no_warning_field_set(db, audit_path):
    """指定機器時走 sync_plan 分支，不是 warning 分支——兩者互斥。"""
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    db.insert_dataset("defect", "v1", 100, "/data/defect/v1", {"file_count": 1, "total_size": 100})
    approval = request_enqueue_approval(
        db, command="python train.py", type="train", project="proj", pin_server="server-a",
        audit_path=audit_path,
    )
    assert approval.payload["warning"] is None
    assert approval.payload["sync_plan"] is not None


# ---------------------------------------------------------------------------
# approve：核准後才真正入列；拒絕不入列
# ---------------------------------------------------------------------------


def test_approve_enqueue_creates_job(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", project="p1", audit_path=audit_path)
    result = asyncio.run(approve(db, approval.id, audit_path=audit_path))

    assert result["approval"].status == "approved"
    assert result["job"].status == "queued"
    assert result["job"].command == "sleep 60"
    assert result["job"].project == "p1"

    jobs = db.list_jobs()
    assert len(jobs) == 1

    records = read_audit(audit_path)
    assert any(r["action"] == "approve" for r in records)


def test_reject_enqueue_does_not_create_job(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", audit_path=audit_path)
    rejected = reject(db, approval.id, note="不需要", audit_path=audit_path)

    assert rejected.status == "rejected"
    assert rejected.note == "不需要"
    assert db.list_jobs() == []

    records = read_audit(audit_path)
    assert any(r["action"] == "reject" and r["params"]["approval_id"] == approval.id for r in records)


def test_approve_already_decided_raises(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", audit_path=audit_path)
    reject(db, approval.id, audit_path=audit_path)

    with pytest.raises(ApprovalNotPendingError):
        asyncio.run(approve(db, approval.id, audit_path=audit_path))


def test_reject_already_decided_raises(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", audit_path=audit_path)
    asyncio.run(approve(db, approval.id, audit_path=audit_path))

    with pytest.raises(ApprovalNotPendingError):
        reject(db, approval.id, audit_path=audit_path)


def test_approve_nonexistent_raises_not_found(db, audit_path):
    with pytest.raises(ApprovalNotFoundError):
        asyncio.run(approve(db, 9999, audit_path=audit_path))


def test_reject_nonexistent_raises_not_found(db, audit_path):
    with pytest.raises(ApprovalNotFoundError):
        reject(db, 9999, audit_path=audit_path)


# ---------------------------------------------------------------------------
# stop 流程：只能對 running 任務請求；核准後保存 intent 並送 kill，
# Job 等待 terminal evidence。
# ---------------------------------------------------------------------------


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


class RecordingFakeSSH:
    """記錄呼叫過的指令，並依內容回傳預設好的輸出。"""

    def __init__(self, tail_text: str = "job stopped, partial output\n"):
        self.calls: list[str] = []
        self.tail_text = tail_text

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if "tmux kill-session" in command:
            return FakeCommandResult("")
        if "tail -n" in command:
            return FakeCommandResult(self.tail_text)
        return FakeCommandResult("")


def test_request_stop_approval_requires_running_job(db, audit_path):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    with pytest.raises(JobNotRunningError):
        request_stop_approval(db, job.id, audit_path=audit_path)


def test_request_stop_approval_nonexistent_job(db, audit_path):
    with pytest.raises(JobNotFoundError):
        request_stop_approval(db, 9999, audit_path=audit_path)


def test_stop_flow_kills_tmux_session_and_waits_for_terminal_evidence(
    db, audit_path
):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")

    approval = request_stop_approval(db, job.id, audit_path=audit_path)
    assert approval.kind == "stop"
    assert approval.payload == {"job_id": job.id, "source": "api"}
    assert approval.payload_contract_version == "stop-intent-v1"
    assert approval.payload_sha256 is not None
    assert approval.payload_immutable_at is not None

    ssh = RecordingFakeSSH(tail_text="training interrupted by user\n")
    result = asyncio.run(approve(db, approval.id, ssh_run=ssh, audit_path=audit_path))

    assert result["approval"].status == "approved"
    assert result["job"].status == "running"
    assert result["job"].finished_at is None
    assert result["job"].log_tail == "training interrupted by user\n"
    intent = db.get_legacy_job_stop_intent(approval_id=approval.id)
    assert intent["state"] == "delivered"
    assert intent["approved_payload_sha256"] == approval.payload_sha256
    assert intent["delivery_started_at"] is not None
    assert intent["delivered_at"] is not None

    assert any(f"job_{job.id}" in c and "tmux kill-session" in c for c in ssh.calls)

    records = read_audit(audit_path)
    assert any(r["action"] == "stop" and r["params"]["job_id"] == job.id for r in records)


def test_stop_flow_reject_leaves_job_running(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")

    approval = request_stop_approval(db, job.id, audit_path=audit_path)
    reject(db, approval.id, audit_path=audit_path)

    assert db.get_job(job.id).status == "running"


def test_stop_flow_success_records_kill_ok_true_in_audit(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, audit_path=audit_path)

    ssh = RecordingFakeSSH(tail_text="tail\n")
    asyncio.run(approve(db, approval.id, ssh_run=ssh, audit_path=audit_path))

    records = read_audit(audit_path)
    stop_records = [r for r in records if r["action"] == "stop" and r["params"]["job_id"] == job.id]
    assert len(stop_records) == 1
    assert stop_records[0]["params"]["kill_ok"] is True
    assert stop_records[0]["params"]["kill_error"] is None
    assert stop_records[0]["result"] == "ok"


# ---------------------------------------------------------------------------
# 修正 1：核准 stop 之前重查任務狀態——已經不是 running（例如自己跑完了）
# 就不能把它改寫成 cancelled，歷史紀錄要保留真實的完成狀態。
# ---------------------------------------------------------------------------


def test_approve_stop_does_not_overwrite_job_that_already_finished(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")

    approval = request_stop_approval(db, job.id, audit_path=audit_path)

    # 使用者按下「請求停止」之後、核准之前，任務自己跑完了
    # （reconcile 已經把它標成 done）。
    db.update_job(job.id, status="done", exit_code=0, log_tail="finished cleanly\n")

    ssh = RecordingFakeSSH()
    result = asyncio.run(approve(db, approval.id, ssh_run=ssh, audit_path=audit_path))

    # approval 本身仍標 approved（使用者的核准意圖確實生效），但 job 完全
    # 沒有被動過：狀態、exit_code、log_tail 都維持「已經完成」的樣子。
    assert result["approval"].status == "approved"
    assert "已結束" in result["approval"].note
    assert result["job"].status == "done"
    assert result["job"].exit_code == 0
    assert result["job"].log_tail == "finished cleanly\n"

    # 而且完全沒有嘗試對已完成的任務發 kill-session。
    assert not any("tmux kill-session" in c for c in ssh.calls)

    final_job = db.get_job(job.id)
    assert final_job.status == "done"
    assert final_job.exit_code == 0

    records = read_audit(audit_path)
    stop_records = [r for r in records if r["action"] == "stop" and r["params"]["job_id"] == job.id]
    assert len(stop_records) == 1
    assert stop_records[0]["result"] == "skipped"
    assert stop_records[0]["params"]["job_status"] == "done"


def test_approve_stop_does_not_overwrite_job_that_already_failed(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, audit_path=audit_path)

    db.update_job(job.id, status="failed", exit_code=1, log_tail="boom\n")

    result = asyncio.run(approve(db, approval.id, ssh_run=RecordingFakeSSH(), audit_path=audit_path))
    assert result["job"].status == "failed"
    assert result["job"].exit_code == 1


# ---------------------------------------------------------------------------
# 修正 2：kill-session 失敗不能被靜默吞掉，稽核與 approval note 都要留痕。
# ---------------------------------------------------------------------------


class KillFailsFakeSSH:
    """tmux kill-session 這一步刻意丟例外，模擬 SSH 當下連不上目標機。"""

    def __init__(self, tail_text: str = "tail after kill failure\n"):
        self.calls: list[str] = []
        self.tail_text = tail_text

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if "tmux kill-session" in command:
            raise ConnectionError("simulated ssh unreachable")
        if "tail -n" in command:
            return FakeCommandResult(self.tail_text)
        return FakeCommandResult("")


def test_stop_kill_failure_stays_running_with_uncertain_intent(
    db, audit_path
):
    """WP-1C correction for RB-STOP-001."""
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, audit_path=audit_path)

    ssh = KillFailsFakeSSH()
    result = asyncio.run(approve(db, approval.id, ssh_run=ssh, audit_path=audit_path))

    assert result["job"].status == "running"
    assert result["job"].finished_at is None
    assert result["approval"].status == "approved"
    assert "未確認送達" in result["approval"].note
    assert "simulated ssh unreachable" in result["approval"].note
    intent = db.get_legacy_job_stop_intent(approval_id=approval.id)
    assert intent["state"] == "delivery_uncertain"
    assert intent["last_error_category"] == "remote_unreachable"
    assert intent["sanitized_error_detail"] == "remote stop delivery failed"
    assert "simulated ssh unreachable" not in intent["sanitized_error_detail"]

    records = read_audit(audit_path)
    stop_records = [r for r in records if r["action"] == "stop" and r["params"]["job_id"] == job.id]
    assert len(stop_records) == 1
    assert stop_records[0]["params"]["kill_ok"] is False
    assert "simulated ssh unreachable" in stop_records[0]["params"]["kill_error"]
    assert stop_records[0]["result"] == "partial"


def test_unpinned_pending_stop_is_refused_before_remote_effect(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    legacy_approval_id = db.insert_approval(
        "stop",
        {"job_id": job.id, "source": "api"},
    )
    ssh = RecordingFakeSSH()

    with pytest.raises(ValueError, match="reject and re-create"):
        asyncio.run(
            approve(
                db,
                legacy_approval_id,
                ssh_run=ssh,
                audit_path=audit_path,
            )
        )

    assert ssh.calls == []
    assert db.get_approval(legacy_approval_id).status == "pending"
    assert db.get_legacy_job_stop_intent(approval_id=legacy_approval_id) is None
    assert db.get_job(job.id).status == "running"


def test_stop_delivery_crash_retry_never_replays_material_kill(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, audit_path=audit_path)
    intent = db.create_legacy_job_stop_intent(
        approval_id=approval.id,
        job_id=job.id,
    )
    assert db.begin_legacy_job_stop_delivery(intent["id"]) is True
    ssh = RecordingFakeSSH()

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path)
    )

    assert ssh.calls == []
    assert result["approval"].status == "approved"
    assert result["job"].status == "running"
    persisted = db.get_legacy_job_stop_intent(approval_id=approval.id)
    assert persisted["state"] == "delivery_uncertain"
    assert persisted["last_error_category"] == "effect_outcome_unknown"


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.1）：source 標記記進 payload
# ---------------------------------------------------------------------------


def test_request_enqueue_approval_defaults_source_to_api(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", audit_path=audit_path)
    assert approval.payload["source"] == "api"


def test_request_enqueue_approval_records_given_source(db, audit_path):
    approval = request_enqueue_approval(
        db, command="sleep 60", source="chatgpt", audit_path=audit_path
    )
    assert approval.payload["source"] == "chatgpt"


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.2）：approve() 的 approved_by／note 參數
# ---------------------------------------------------------------------------


def test_approve_default_approved_by_is_human(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", audit_path=audit_path)
    asyncio.run(approve(db, approval.id, audit_path=audit_path))
    records = read_audit(audit_path)
    approve_records = [r for r in records if r["action"] == "approve"]
    assert len(approve_records) == 1
    assert approve_records[0]["params"]["approved_by"] == "human"


def test_approve_web_direct_records_approved_by_and_note(db, audit_path):
    approval = request_enqueue_approval(db, command="sleep 60", source="web", audit_path=audit_path)
    result = asyncio.run(
        approve(
            db,
            approval.id,
            audit_path=audit_path,
            approved_by="web-direct",
            note="網頁直接執行（提案者＝批准者）",
        )
    )
    assert result["approval"].note == "網頁直接執行（提案者＝批准者）"
    records = read_audit(audit_path)
    approve_records = [r for r in records if r["action"] == "approve"]
    assert approve_records[-1]["params"]["approved_by"] == "web-direct"


def test_approve_stop_combines_context_note_with_kill_failure_note(db, audit_path):
    """`note` 參數（K.2/K.3 情境註記）不會蓋掉 approve() 自己對 stop kind
    決定要寫的細節註記（鐵律第 3 條：稽核要如實）——兩者合併。"""
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, source="web", audit_path=audit_path)

    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=KillFailsFakeSSH(),
            audit_path=audit_path,
            approved_by="web-direct",
            note="網頁直接執行（提案者＝批准者）",
        )
    )
    assert "網頁直接執行" in result["approval"].note
    assert "未確認送達" in result["approval"].note


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.3）：maybe_auto_approve()
# ---------------------------------------------------------------------------


def test_maybe_auto_approve_no_rules_returns_none_and_stays_pending(db, audit_path):
    approval = request_enqueue_approval(
        db, command="sleep 60", source="chatgpt", audit_path=audit_path
    )
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=[], audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


def test_maybe_auto_approve_matching_rule_approves_and_enqueues_job(db, audit_path):
    approval = request_enqueue_approval(
        db, command="ls -la", source="chatgpt", audit_path=audit_path
    )
    rules = [{"source": "chatgpt", "command_regex": "^ls "}]
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=rules, audit_path=audit_path)
    )
    assert result is not None
    assert result["approval"].status == "approved"
    assert result["job"].status == "queued"
    assert "自動核准：規則 #0" in result["approval"].note
    assert "source=chatgpt" in result["approval"].note

    jobs = db.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].command == "ls -la"

    records = read_audit(audit_path)
    approve_records = [r for r in records if r["action"] == "approve"]
    assert approve_records[-1]["params"]["approved_by"] == "auto-rule-0"


def test_maybe_auto_approve_no_matching_rule_returns_none(db, audit_path):
    approval = request_enqueue_approval(
        db, command="python train.py", source="chatgpt", audit_path=audit_path
    )
    rules = [{"source": "web"}]  # source 不符
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=rules, audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"
    assert db.list_jobs() == []


def test_maybe_auto_approve_stop_without_ssh_run_stays_pending(db, audit_path):
    """stop 的自動核准需要 ssh_run；拿不到就保持 pending，不報錯（PLAN.md
    K.3：呼叫端沒有可用 SSH 介面時寧可少自動化，也不要標 approved 卻沒有
    真的執行任何東西）。"""
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, source="chatgpt", audit_path=audit_path)

    rules = [{"source": "chatgpt", "kind": "stop"}]
    result = asyncio.run(
        maybe_auto_approve(
            db, approval, source="chatgpt", rules=rules, ssh_run=None, audit_path=audit_path
        )
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"
    assert db.get_job(job.id).status == "running"


def test_maybe_auto_approve_stop_with_ssh_run_matching_rule_executes(db, audit_path):
    job = enqueue_job(db, command="sleep 600", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, source="chatgpt", audit_path=audit_path)

    rules = [{"source": "chatgpt", "kind": "stop"}]
    ssh = RecordingFakeSSH(tail_text="auto-approved stop\n")
    result = asyncio.run(
        maybe_auto_approve(
            db, approval, source="chatgpt", rules=rules, ssh_run=ssh, audit_path=audit_path
        )
    )
    assert result is not None
    assert result["job"].status == "running"
    assert db.get_legacy_job_stop_intent(job_id=job.id)["state"] == "delivered"
    assert any("tmux kill-session" in c for c in ssh.calls)


def test_maybe_auto_approve_unsupported_kind_returns_none(db, audit_path):
    """目前只支援 kind=enqueue/stop；其他 kind（例如 server_disable）一律
    回 None，維持既有兩步流程。"""
    from app.approvals import request_server_disable_approval

    approval = request_server_disable_approval(
        db, "server-a", ["server-a"], audit_path=audit_path
    )
    rules = [{"kind": "any"}]
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="api", rules=rules, audit_path=audit_path)
    )
    assert result is None


# ---------------------------------------------------------------------------
# 危險指令：任何規則下都被擋——is_dangerous() 在建立核准請求當下就先發生，
# 規則救不回黑名單指令（連 approval 都不存在，規則引擎根本看不到）。
# ---------------------------------------------------------------------------


def test_dangerous_command_rejected_before_any_rule_could_apply(db, audit_path):
    """就算規則寫 `.*`（理論上會命中一切）也救不回：危險指令連 approval
    都建立不起來，maybe_auto_approve() 根本沒有東西可以核准。"""
    with pytest.raises(DangerousCommandError):
        request_enqueue_approval(
            db, command="rm -rf /tmp/x", source="chatgpt", audit_path=audit_path
        )
    assert db.list_approvals() == []
    assert db.list_jobs() == []
