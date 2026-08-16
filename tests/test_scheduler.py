import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.approvals import approve, request_stop_approval
from app.audit import read_audit
from app.config import ServerConfig
from app.db import Job
from app.execution_dispatch import AttemptLaunchContext
from app.jobqueue import (
    ReconcileOutcome,
    apply_reconcile_outcome,
    enqueue_job,
    list_dispatchable_jobs,
    reconcile_job,
)
from app.monitor import ServerState
from app.scheduler import pick_job, scheduler_tick


def make_job(
    id,
    pin_server=None,
    require_tag=None,
    priority="normal",
    created_at="2026-01-01T00:00:00",
    depends_on=None,
    status="queued",
    type="adhoc",
):
    return Job(
        id=id,
        type=type,
        project=None,
        command="sleep 60",
        require_tag=require_tag,
        pin_server=pin_server,
        depends_on=depends_on or [],
        status=status,
        server=None,
        priority=priority,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# pick_job：FIFO / priority / pin_server / require_tag
# ---------------------------------------------------------------------------


def test_pick_job_fifo_order():
    jobs = [
        make_job(1, created_at="2026-01-01T00:00:03"),
        make_job(2, created_at="2026-01-01T00:00:01"),
        make_job(3, created_at="2026-01-01T00:00:02"),
    ]
    picked = pick_job("server-a", ["gpu"], jobs)
    assert picked.id == 2  # 最早建立的先派


def test_pick_job_priority_normal_before_low():
    jobs = [
        make_job(1, priority="low", created_at="2026-01-01T00:00:00"),
        make_job(2, priority="normal", created_at="2026-01-01T00:00:05"),
    ]
    picked = pick_job("server-a", ["gpu"], jobs)
    assert picked.id == 2  # 就算比較晚建立，normal 還是先於 low


def test_pick_job_pin_server_must_match():
    jobs = [
        make_job(1, pin_server="server-b"),
        make_job(2, pin_server=None),
    ]
    picked = pick_job("server-a", ["gpu"], jobs)
    assert picked.id == 2  # job 1 指定給別台機，不會被 server-a 挑走


def test_pick_job_pin_server_matches_this_server():
    # pin_server 是「資格篩選」，不是排序加權（PLAN.md B：pin_server 相符
    # 這一關過了之後，真正決定順序的是 priority／FIFO）。
    # job 1 指定給 server-a、job 2 沒有指定，兩者在 server-a 上都符合資格，
    # 這時比的是建立時間，job 2 較早建立所以先派。
    jobs = [
        make_job(1, pin_server="server-a", created_at="2026-01-01T00:00:05"),
        make_job(2, pin_server=None, created_at="2026-01-01T00:00:00"),
    ]
    picked = pick_job("server-a", ["gpu"], jobs)
    assert picked.id == 2

    # 只有 job 1 pin 給 server-a 時（把 job2 拿掉），job 1 當然會被選中。
    only_pinned = [make_job(1, pin_server="server-a", created_at="2026-01-01T00:00:05")]
    assert pick_job("server-a", ["gpu"], only_pinned).id == 1


def test_pick_job_require_tag_must_be_present_on_server():
    jobs = [
        make_job(1, require_tag="gpu"),
    ]
    assert pick_job("server-cpu-only", [], jobs) is None
    assert pick_job("server-gpu", ["gpu", "training"], jobs).id == 1


def test_pick_job_returns_none_when_no_candidates():
    assert pick_job("server-a", ["gpu"], []) is None


def test_pick_job_no_eligible_job_returns_none():
    jobs = [make_job(1, pin_server="server-b"), make_job(2, require_tag="cpu")]
    assert pick_job("server-a", ["gpu"], jobs) is None


# ---------------------------------------------------------------------------
# 階段 3（Fable 覆核修正 1）：has_dataset 是資格過濾，不是排序偏好
# ---------------------------------------------------------------------------


def test_pick_job_has_dataset_false_excludes_job_even_if_only_candidate():
    """has_dataset 回傳 False 時直接不列入候選——就算它是唯一的候選任務，
    也不會被這台機器挑走（跟舊版「排後面但還是會被挑到」的 gravity sort
    行為不同）。"""
    jobs = [make_job(1)]
    picked = pick_job("server-a", ["gpu"], jobs, has_dataset=lambda j: False)
    assert picked is None


def test_pick_job_has_dataset_filters_out_only_the_uncached_ones():
    jobs = [make_job(1, created_at="2026-01-01T00:00:00"), make_job(2)]

    def has_dataset(j):
        return j.id == 2  # 只有 job 2 有資料

    picked = pick_job("server-a", ["gpu"], jobs, has_dataset=has_dataset)
    assert picked.id == 2


def test_pick_job_has_dataset_none_means_no_restriction():
    jobs = [make_job(1)]
    assert pick_job("server-a", ["gpu"], jobs, has_dataset=None).id == 1


def test_pick_job_default_params_preserve_existing_behavior_with_coding_job_present():
    """不傳任何 codex_* 參數（預設值）時，既有行為完全不變：一般任務照舊
    被挑選，`type=="coding"` 的任務因為 `codex_runner_server` 預設 None 而
    永遠不合格（即使它是唯一候選也不會害排程回傳 None——一般任務照樣被
    挑走）。"""
    jobs = [make_job(1, type="coding"), make_job(2, created_at="2026-01-01T00:00:05")]
    picked = pick_job("server-a", ["gpu"], jobs)
    assert picked.id == 2


# ---------------------------------------------------------------------------
# 階段 13（PLAN.md N.8）：pick_job 的 Codex Runner 規則
# ---------------------------------------------------------------------------


def test_pick_job_coding_only_eligible_on_runner_server():
    jobs = [make_job(1, type="coding")]
    # 非 Runner 機器：coding 永不合格
    assert pick_job("server-a", [], jobs, codex_runner_server="server-c") is None
    # Runner 本身：合格
    picked = pick_job("server-c", [], jobs, codex_runner_server="server-c")
    assert picked is not None
    assert picked.id == 1


def test_pick_job_coding_never_eligible_when_runner_unset():
    """`codex_runner_server` 未設定（None，Codex 功能停用）：coding 任務
    在任何機器上都永不合格，保持 queued（不會誤把 None 當成某台機器名字
    比對成功）。"""
    jobs = [make_job(1, type="coding")]
    assert pick_job("server-a", [], jobs, codex_runner_server=None) is None
    assert pick_job("_", [], jobs, codex_runner_server=None) is None


def test_pick_job_coding_pinned_elsewhere_never_dispatched_anywhere():
    """v1 遺留、pin 到別台（非 Runner）機器的 coding job：那台機器不是
    Runner（規則 1 擋下），Runner 本身則因為 pin_server 對不上而擋下——
    兩邊都不合格，保持 queued（PLAN.md N.8 規則 1 的「刻意」行為）。"""
    jobs = [make_job(1, type="coding", pin_server="server-a")]
    assert pick_job("server-a", [], jobs, codex_runner_server="server-c") is None
    assert pick_job("server-c", [], jobs, codex_runner_server="server-c") is None


def test_pick_job_coding_pinned_to_runner_itself_is_eligible():
    """coding job 明確 pin 到 Runner（v2 的 approve() 一律這樣做，見
    N.8 規則 1）：在 Runner 上合格。"""
    jobs = [make_job(1, type="coding", pin_server="server-c")]
    picked = pick_job("server-c", [], jobs, codex_runner_server="server-c")
    assert picked.id == 1


def test_pick_job_coding_concurrency_limit_blocks_dispatch():
    jobs = [make_job(1, type="coding")]
    # running_coding_count 已達上限：不合格
    assert (
        pick_job(
            "server-c",
            [],
            jobs,
            codex_runner_server="server-c",
            codex_max_concurrency=1,
            running_coding_count=1,
        )
        is None
    )
    # 還沒到上限：合格
    picked = pick_job(
        "server-c",
        [],
        jobs,
        codex_runner_server="server-c",
        codex_max_concurrency=1,
        running_coding_count=0,
    )
    assert picked.id == 1


def test_pick_job_reserve_true_blocks_regular_job_on_runner_unless_pinned():
    jobs = [make_job(1, type="adhoc")]
    # reserve=True、一般任務沒有 pin 到 Runner：不派
    assert (
        pick_job(
            "server-c",
            [],
            jobs,
            codex_runner_server="server-c",
            codex_runner_reserve=True,
        )
        is None
    )

    # reserve=True，但一般任務明確 pin 到 Runner：例外允許
    pinned_jobs = [make_job(1, type="adhoc", pin_server="server-c")]
    picked = pick_job(
        "server-c",
        [],
        pinned_jobs,
        codex_runner_server="server-c",
        codex_runner_reserve=True,
    )
    assert picked.id == 1

    # 非 Runner 機器完全不受這個規則影響
    picked_other = pick_job(
        "server-a", [], jobs, codex_runner_server="server-c", codex_runner_reserve=True
    )
    assert picked_other.id == 1


def test_pick_job_reserve_false_coding_priority_blocks_regular_job():
    """reserve=False：Runner 有 coding job queued（就算它自己這輪不合格，
    例如已達併發上限）時，一般任務這輪也不派給 Runner。"""
    jobs = [make_job(1, type="coding"), make_job(2, type="adhoc")]
    picked = pick_job(
        "server-c",
        [],
        jobs,
        codex_runner_server="server-c",
        codex_runner_reserve=False,
        codex_max_concurrency=1,
        running_coding_count=1,  # coding 本身這轮已達上限、不合格
    )
    assert picked is None  # 一般任務也不該被派——coding 優先


def test_pick_job_reserve_false_no_coding_present_allows_regular_job():
    """reserve=False 且候選裡完全沒有 coding 任務時，一般任務可以正常派給
    Runner（空閒可接一般任務）。"""
    jobs = [make_job(1, type="adhoc")]
    picked = pick_job(
        "server-c",
        [],
        jobs,
        codex_runner_server="server-c",
        codex_runner_reserve=False,
    )
    assert picked.id == 1


def test_pick_job_coding_sorted_first_on_runner():
    """Runner 上合格候選裡，coding 任務排最前，即使一般任務的 priority 較
    高或建立時間較早（PLAN.md N.8：coding 優先）。"""
    jobs = [
        make_job(1, type="adhoc", pin_server="server-c", created_at="2026-01-01T00:00:00"),
        make_job(2, type="coding", created_at="2026-01-01T00:00:05"),
    ]
    picked = pick_job(
        "server-c",
        [],
        jobs,
        codex_runner_server="server-c",
        codex_runner_reserve=True,
    )
    assert picked.id == 2


# ---------------------------------------------------------------------------
# reconcile_job：哨兵檔案協議三分支 + SSH 不可達
# ---------------------------------------------------------------------------


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


class FakeSSH:
    """依 command 內容回傳預先設定好的輸出，模擬 sshpool.run()。"""

    def __init__(self, responses: dict[str, str], unreachable: bool = False):
        self.responses = responses
        self.unreachable = unreachable
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if self.unreachable:
            raise ConnectionError("simulated unreachable")
        for key, value in self.responses.items():
            if key in command:
                return FakeCommandResult(value)
        return FakeCommandResult("")


def test_reconcile_job_exit_code_present_success():
    ssh = FakeSSH(
        {
            "exit_code": "0\n",
            "tail -n 40": "training finished\n",
        }
    )
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "done"
    assert outcome.exit_code == 0
    assert "training finished" in outcome.log_tail


def test_reconcile_job_exit_code_present_failure():
    ssh = FakeSSH(
        {
            "exit_code": "1\n",
            "tail -n 40": "traceback...\n",
        }
    )
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "failed"
    assert outcome.exit_code == 1


def test_reconcile_job_tmux_session_still_running():
    ssh = FakeSSH(
        {
            "exit_code": "",  # exit_code 檔案不存在，cat 輸出空字串
            "tmux has-session": "EXISTS\n",
        }
    )
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "running"


def test_reconcile_job_interrupted_no_exit_code_no_tmux():
    ssh = FakeSSH(
        {
            "exit_code": "",
            "tmux has-session": "GONE\n",
        }
    )
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "requeued"


def test_reconcile_job_ssh_unreachable_is_skipped():
    ssh = FakeSSH({}, unreachable=True)
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "unreachable"


class SequencedFakeSSH:
    """依 command 內容比對，但每個 key 可以設定「依呼叫次序」不同的回應，
    用來模擬 reconcile_job 兩次查 exit_code 之間任務剛好完成的競態情境。
    """

    def __init__(self, sequences: dict[str, list[str]]):
        self.sequences = sequences
        self.call_counts: dict[str, int] = {}
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        for key, outputs in self.sequences.items():
            if key in command:
                idx = self.call_counts.get(key, 0)
                idx = min(idx, len(outputs) - 1)
                self.call_counts[key] = self.call_counts.get(key, 0) + 1
                return FakeCommandResult(outputs[idx])
        return FakeCommandResult("")


def test_reconcile_job_race_exit_code_appears_right_after_tmux_gone():
    """競態：第一次查 exit_code 時任務還沒結束（空字串）→ 查 tmux 時
    session 剛好已經退出（GONE）→ 但任務其實是「剛好在這個空檔完成」，
    不是被中斷；reconcile_job 必須在判定 requeued 前再查一次 exit_code，
    這次拿到結果就該回報 done，而不是把一個成功的任務誤判成中斷重跑。
    """
    ssh = SequencedFakeSSH(
        {
            "exit_code": ["", "0\n"],
            "tmux has-session": ["GONE\n"],
            "tail -n 40": ["training finished\n"],
        }
    )
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "done"
    assert outcome.exit_code == 0
    assert "training finished" in outcome.log_tail


def test_reconcile_job_race_still_requeues_when_truly_interrupted():
    """兩次查 exit_code 都拿不到結果、tmux 也不在了，才真的是中斷。"""
    ssh = SequencedFakeSSH(
        {
            "exit_code": ["", ""],
            "tmux has-session": ["GONE\n"],
        }
    )
    outcome = asyncio.run(reconcile_job(ssh, "server-a", 42))
    assert outcome.status == "requeued"


def test_apply_reconcile_outcome_done_updates_db(db, audit_path):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    running_job = db.get_job(job.id)

    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="done", exit_code=0, log_tail="ok"),
        audit_path=audit_path,
    )
    updated = db.get_job(job.id)
    assert updated.status == "done"
    assert updated.exit_code == 0
    assert updated.log_tail == "ok"
    events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_terminal_recorded"
        and event["resource_id"] == str(job.id)
    ]
    assert len(events) == 1
    assert events[0]["result"] == "done"
    assert events[0]["params"]["exit_code"] == 0
    assert "ok" not in events[0]["params"]


def test_apply_reconcile_outcome_requeued_resets_job(db, audit_path):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a", started_at="2026-01-01T00:00:00")
    running_job = db.get_job(job.id)

    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="requeued"), audit_path=audit_path
    )
    updated = db.get_job(job.id)
    assert updated.status == "queued"
    assert updated.server is None
    assert updated.started_at is None
    events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_requeued"
        and event["resource_id"] == str(job.id)
    ]
    assert len(events) == 1
    assert events[0]["result"] == "queued"


@pytest.mark.parametrize(
    ("outcome_status", "exit_code"), [("done", 0), ("failed", 7)]
)
def test_apply_reconcile_terminal_and_audit_roll_back_together(
    db, audit_path, monkeypatch, outcome_status, exit_code
):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    original_append = db.append_durable_audit_event_in_transaction

    def fail_on_terminal(cursor, **kwargs):
        if kwargs.get("action") == "execution_job_terminal_recorded":
            raise RuntimeError("audit append failed")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_on_terminal)
    with pytest.raises(RuntimeError, match="audit append failed"):
        apply_reconcile_outcome(
            db,
            db.get_job(job.id),
            ReconcileOutcome(status=outcome_status, exit_code=exit_code, log_tail="secret tail"),
            audit_path=audit_path,
        )

    assert db.get_job(job.id).status == "running"
    assert not any(
        event["action"] == "execution_job_terminal_recorded"
        for event in db.list_durable_audit_events(limit=100)
    )


def test_apply_reconcile_requeue_and_audit_roll_back_together(
    db, audit_path, monkeypatch
):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    original_append = db.append_durable_audit_event_in_transaction

    def fail_on_requeue(cursor, **kwargs):
        if kwargs.get("action") == "execution_job_requeued":
            raise RuntimeError("audit append failed")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_on_requeue)
    with pytest.raises(RuntimeError, match="audit append failed"):
        apply_reconcile_outcome(
            db,
            db.get_job(job.id),
            ReconcileOutcome(status="requeued"),
            audit_path=audit_path,
        )

    assert db.get_job(job.id).status == "running"


def test_approved_stop_intent_blocks_requeue_and_all_dispatch_channels(
    db, audit_path
):
    from app.node_registry import job_is_dispatchable

    class StopDelivered:
        async def __call__(self, _server, _command, _timeout):
            return type("Result", (), {"stdout": ""})()

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(
        job.id,
        status="running",
        server="server-a",
        started_at="2026-01-01T00:00:00",
    )
    approval = request_stop_approval(db, job.id, audit_path=audit_path)
    asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=StopDelivered(),
            audit_path=audit_path,
        )
    )

    apply_reconcile_outcome(
        db,
        db.get_job(job.id),
        ReconcileOutcome(status="requeued"),
        audit_path=audit_path,
    )

    assert db.get_job(job.id).status == "running"
    assert job_is_dispatchable(db, job.id) is False
    blocked = [
        record
        for record in read_audit(audit_path)
        if record["action"] == "requeue_blocked"
    ]
    assert len(blocked) == 1

    # Defense in depth: even an imported/inconsistent queued row is withheld.
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE jobs SET status = 'queued' WHERE id = ?",
            (job.id,),
        )
    assert list_dispatchable_jobs(db) == []


def test_terminal_sentinel_closes_stop_intent_and_finishes_once(db, audit_path):
    class StopDelivered:
        async def __call__(self, _server, _command, _timeout):
            return type("Result", (), {"stdout": ""})()

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    approval = request_stop_approval(db, job.id, audit_path=audit_path)
    asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=StopDelivered(),
            audit_path=audit_path,
        )
    )
    finished = []

    apply_reconcile_outcome(
        db,
        db.get_job(job.id),
        ReconcileOutcome(status="done", exit_code=0, log_tail="stopped\n"),
        audit_path=audit_path,
        on_job_finished=finished.append,
    )

    assert db.get_job(job.id).status == "done"
    intent = db.get_legacy_job_stop_intent(job_id=job.id)
    assert intent["state"] == "terminal_observed"
    assert intent["terminal_observed_at"] is not None
    assert len(finished) == 1
    assert finished[0].status == "done"


def test_apply_reconcile_outcome_unreachable_is_noop(db, audit_path):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    running_job = db.get_job(job.id)

    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="unreachable"), audit_path=audit_path
    )
    updated = db.get_job(job.id)
    assert updated.status == "running"
    assert updated.server == "server-a"


# ---------------------------------------------------------------------------
# 階段 4：apply_reconcile_outcome 的 on_job_finished 回呼（任務結束 hook）
# ---------------------------------------------------------------------------


def test_apply_reconcile_outcome_done_invokes_on_job_finished_with_final_job(db, audit_path):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    running_job = db.get_job(job.id)

    finished_jobs = []
    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="done", exit_code=0, log_tail="ok"),
        audit_path=audit_path, on_job_finished=finished_jobs.append,
    )
    assert len(finished_jobs) == 1
    assert finished_jobs[0].id == job.id
    assert finished_jobs[0].status == "done"  # 回呼拿到的是落地後的最終狀態


def test_apply_reconcile_outcome_failed_invokes_on_job_finished(db, audit_path):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    running_job = db.get_job(job.id)

    finished_jobs = []
    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="failed", exit_code=1, log_tail="boom"),
        audit_path=audit_path, on_job_finished=finished_jobs.append,
    )
    assert len(finished_jobs) == 1
    assert finished_jobs[0].status == "failed"


def test_apply_reconcile_outcome_requeued_and_unreachable_do_not_invoke_hook(db, audit_path):
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a", started_at="2026-01-01T00:00:00")
    running_job = db.get_job(job.id)

    finished_jobs = []
    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="requeued"),
        audit_path=audit_path, on_job_finished=finished_jobs.append,
    )
    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="unreachable"),
        audit_path=audit_path, on_job_finished=finished_jobs.append,
    )
    assert finished_jobs == []


def test_apply_reconcile_outcome_no_callback_still_works(db, audit_path):
    """既有呼叫端（不傳 on_job_finished）行為不變：不需要在一個執行中的
    event loop 裡呼叫也不會出錯（這個測試本身就是同步呼叫，沒有
    asyncio.run()）。"""
    from app.jobqueue import enqueue_job

    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    running_job = db.get_job(job.id)
    apply_reconcile_outcome(
        db, running_job, ReconcileOutcome(status="done", exit_code=0), audit_path=audit_path
    )
    assert db.get_job(job.id).status == "done"


# ---------------------------------------------------------------------------
# scheduler_tick：先標 running 再派發，崩潰窗口不會造成雙重派發
# ---------------------------------------------------------------------------


def _idle_cpu_server_config(name: str) -> ServerConfig:
    return ServerConfig(
        name=name,
        host="10.0.0.1",
        user="train",
        key="~/.ssh/id_rsa",
        gpu=False,
        idle_gpu_util=15.0,
        idle_load=2.0,
        tags=[],
    )


def test_scheduler_tick_marks_running_before_ssh_dispatch_call(db, audit_path):
    """在真正 SSH 派發（第一個 ssh_run 呼叫，也就是 mkdir）發生的當下，
    DB 應該已經是 running——這樣就算這時候服務崩潰，重啟後也不會把這個
    任務當成 queued 再派一次（見 scheduler.py 派發順序的說明）。
    """
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}

    statuses_at_first_ssh_call: list[str] = []

    async def recording_ssh_run(server_name, command, timeout):
        statuses_at_first_ssh_call.append(db.get_job(job.id).status)
        return FakeCommandResult("")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, recording_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    assert statuses_at_first_ssh_call[0] == "running"
    assert db.get_job(job.id).status == "running"
    assert db.get_job(job.id).server == "server-a"
    events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_dispatched"
        and event["resource_id"] == str(job.id)
    ]
    assert len(events) == 1
    assert events[0]["params"] == {
        "backend": "ssh",
        "dispatch_mode": "legacy",
        "job_id": job.id,
        "server": "server-a",
        "status": "running",
        "previous_status": "queued",
    }


def test_attempt_owned_server_uses_legacy_dispatch_for_unpinned_job(db, audit_path):
    """A target revision does not give the attempt path an execution grant.

    Compatibility Jobs without an immutable execution approval must keep using
    the legacy SSH dispatcher.  Otherwise attempt creation rejects them with
    ``approval_missing`` and leaves them queued forever.
    """
    job = enqueue_job(
        db,
        command="sleep 60",
        pin_server="server-a",
        audit_path=audit_path,
    )
    server_states = {
        "server-a": ServerState(name="server-a", online=True, load1=0.1)
    }
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    context = AttemptLaunchContext(
        leader_owner_id="attempt-owner",
        scheduler_fencing_epoch=1,
        enabled=True,
        revision_ids={"server-a": "published-revision"},
    )
    remote_calls: list[tuple[str, str]] = []

    async def recording_ssh_run(server_name, command, _timeout):
        remote_calls.append((server_name, command))
        return FakeCommandResult("")

    async def noop_write_file(_server_name, _path, _content):
        return None

    asyncio.run(
        scheduler_tick(
            db,
            server_states,
            server_configs,
            recording_ssh_run,
            noop_write_file,
            audit_path=audit_path,
            attempt_launch=context,
        )
    )

    dispatched = db.get_job(job.id)
    assert dispatched is not None
    assert dispatched.status == "running"
    assert dispatched.server == "server-a"
    assert dispatched.execution_approval_id is None
    assert db.get_latest_execution_attempt_for_job(job.id) is None
    assert remote_calls


def test_scheduler_tick_reverts_to_queued_when_dispatch_fails(db, audit_path):
    """派發過程中 SSH 失敗（例如一開始 mkdir 就連不上），任務要退回
    queued（而不是卡在 running 卻其實根本沒真的上工作機跑），下一輪才有
    機會重新挑機派發。
    """
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}

    async def failing_ssh_run(server_name, command, timeout):
        raise ConnectionError("simulated ssh failure")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, failing_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    updated = db.get_job(job.id)
    assert updated.status == "queued"
    assert updated.server is None
    assert updated.started_at is None
    events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_dispatch_requeued"
        and event["resource_id"] == str(job.id)
    ]
    assert len(events) == 1
    assert events[0]["params"]["reason_code"] == "legacy_dispatch_exception"


@pytest.mark.parametrize(
    "failing_action",
    ["execution_job_dispatched", "execution_job_dispatch_requeued"],
)
def test_scheduler_legacy_dispatch_state_and_durable_event_roll_back_together(
    db, audit_path, monkeypatch, failing_action
):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    original_append = db.append_durable_audit_event_in_transaction

    def fail_dispatch_event(cursor, **kwargs):
        if kwargs.get("action") == failing_action:
            raise RuntimeError("dispatch audit append failed")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_dispatch_event)

    if failing_action == "execution_job_dispatched":
        async def ssh_run(_server_name, _command, _timeout):
            raise AssertionError("remote dispatch must not start before durable state")
    else:
        async def ssh_run(_server_name, _command, _timeout):
            raise ConnectionError("simulated ssh failure")

    async def noop_write_file(_server_name, _path, _content):
        return None

    with pytest.raises(RuntimeError, match="dispatch audit append failed"):
        asyncio.run(
            scheduler_tick(
                db,
                server_states,
                server_configs,
                ssh_run,
                noop_write_file,
                audit_path=audit_path,
            )
        )

    restored = db.get_job(job.id)
    assert restored is not None
    if failing_action == "execution_job_dispatched":
        assert restored.status == "queued"
        assert restored.server is None
    else:
        # The durable requeue append failed, so the pre-effect running claim is
        # intentionally retained for reconcile rather than silently replayed.
        assert restored.status == "running"
        assert restored.server == "server-a"
    assert not any(
        event["action"] == failing_action
        and event["resource_id"] == str(job.id)
        for event in db.list_durable_audit_events(limit=100)
    )


# ---------------------------------------------------------------------------
# 階段 3：`_local` sync 任務——永遠在線、上限 LOCAL_SYNC_CONCURRENCY 個並行
# ---------------------------------------------------------------------------


def _make_sync_job(db, audit_path, target_server="server-a", dataset="defect", version="v1"):
    job = enqueue_job(
        db, command="rsync ...", type="sync", pin_server="_local", audit_path=audit_path
    )
    db.update_job(
        job.id, target_server=target_server, dataset_name=dataset, dataset_version=version
    )
    return db.get_job(job.id)


def test_scheduler_tick_dispatches_sync_job_to_local_with_no_real_servers(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 1024, "/data/defect/v1", {"file_count": 1, "total_size": 1024}
    )
    job = _make_sync_job(db, audit_path)

    ssh = FakeSSH({"df -Pk": "/dev/sda1 100000000 1000000 99000000 2% /\n"})

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(db, {}, {}, ssh, noop_write_file, audit_path=audit_path)
    )

    updated = db.get_job(job.id)
    assert updated.status == "running"
    assert updated.server == "_local"
    assert any("tmux new-session" in c for c in ssh.calls)
    events = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_dispatched"
        and event["resource_id"] == str(job.id)
    ]
    assert len(events) == 1
    assert events[0]["params"]["backend"] == "local_sync"


def test_scheduler_local_dispatch_claim_rolls_back_when_durable_append_fails(
    db, audit_path, monkeypatch
):
    db.insert_dataset(
        "defect", "v1", 1024, "/data/defect/v1", {"file_count": 1, "total_size": 1024}
    )
    job = _make_sync_job(db, audit_path)
    ssh = FakeSSH({"df -Pk": "/dev/sda1 100000000 1000000 99000000 2% /\n"})
    original_append = db.append_durable_audit_event_in_transaction

    def fail_dispatch_event(cursor, **kwargs):
        if kwargs.get("action") == "execution_job_dispatched":
            raise RuntimeError("dispatch audit append failed")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_dispatch_event)

    async def noop_write_file(_server_name, _path, _content):
        return None

    with pytest.raises(RuntimeError, match="dispatch audit append failed"):
        asyncio.run(
            scheduler_tick(
                db, {}, {}, ssh, noop_write_file, audit_path=audit_path
            )
        )

    restored = db.get_job(job.id)
    assert restored is not None
    assert restored.status == "queued"
    assert restored.server is None
    assert not any("tmux new-session" in command for command in ssh.calls)


def test_scheduler_tick_local_sync_concurrency_limit_is_two(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 1024, "/data/defect/v1", {"file_count": 1, "total_size": 1024}
    )
    j1 = _make_sync_job(db, audit_path, dataset="a")
    j2 = _make_sync_job(db, audit_path, dataset="b")
    j3 = _make_sync_job(db, audit_path, dataset="c")

    ssh = FakeSSH({"df -Pk": "/dev/sda1 100000000 1000000 99000000 2% /\n"})

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(scheduler_tick(db, {}, {}, ssh, noop_write_file, audit_path=audit_path))

    statuses = [db.get_job(j.id).status for j in (j1, j2, j3)]
    assert statuses.count("running") == 2
    assert statuses.count("queued") == 1


def test_scheduler_tick_sync_job_insufficient_space_marked_failed(db, audit_path):
    db.insert_dataset(
        "defect",
        "v1",
        1000 * 1024 * 1024 * 1024,  # 1000 GB，遠大於下面 df 回報的可用空間
        "/data/defect/v1",
        {"file_count": 1, "total_size": 1},
    )
    job = _make_sync_job(db, audit_path)

    ssh = FakeSSH({"df -Pk": "/dev/sda1 100000000 99000000 1000000 99% /\n"})

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(scheduler_tick(db, {}, {}, ssh, noop_write_file, audit_path=audit_path))

    updated = db.get_job(job.id)
    assert updated.status == "failed"
    assert "剩餘空間不足" in (updated.log_tail or "")
    # 空間不足時完全不會真的跑 rsync（不會起 tmux session）
    assert not any("tmux new-session" in c for c in ssh.calls)


def test_scheduler_tick_finalizes_sync_job_and_registers_cache_on_completion(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 300, "/data/defect/v1", {"file_count": 2, "total_size": 300}
    )
    job = enqueue_job(
        db, command="rsync ...", type="sync", pin_server="_local", audit_path=audit_path
    )
    db.update_job(
        job.id,
        status="running",
        server="_local",
        target_server="server-a",
        dataset_name="defect",
        dataset_version="v1",
    )

    ssh = FakeSSH(
        {
            "exit_code": "0\n",
            "tail -n 40": "rsync done\n",
            "find": "COUNT:2\nSIZE:300\n",
        }
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(scheduler_tick(db, {}, {}, ssh, noop_write_file, audit_path=audit_path))

    updated = db.get_job(job.id)
    assert updated.status == "done"
    assert db.is_dataset_cached("server-a", "defect", "v1") is True


# ---------------------------------------------------------------------------
# 階段 3：資料引力——pick_job 的 has_dataset 掛勾接上 dataset_cache
# ---------------------------------------------------------------------------


def test_scheduler_tick_data_gravity_prefers_cached_dataset_job(db, audit_path):
    # proj_has：server-a 已經快取它的資料集；proj_needs：server-a 沒有，需要
    # sync 才能跑。兩個專案都掛了資料集需求，這樣才能看出「有資料的優先」
    # 而不是「沒有資料集需求的中性任務」這種混淆情境。
    db.insert_project("proj_has", "git@x", dataset_name="defect", dataset_version="v1")
    db.insert_project("proj_needs", "git@x", dataset_name="other", dataset_version="v1")
    db.upsert_dataset_cache("server-a", "defect", "v1")

    # job_no_data 先建立（較早的 created_at，FIFO 上本來會優先），但 server-a
    # 沒有它需要的資料集（other@v1）；job_has_data 較晚建立，但 server-a 已經
    # 有它的資料（defect@v1），資料引力應該讓 job_has_data 優先被挑走。
    job_no_data = enqueue_job(
        db, command="python train_other.py", type="train", project="proj_needs",
        audit_path=audit_path,
    )
    job_has_data = enqueue_job(
        db, command="python train.py", type="train", project="proj_has", audit_path=audit_path
    )

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}

    async def noop_ssh_run(server_name, command, timeout):
        return FakeCommandResult("")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, noop_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    assert db.get_job(job_has_data.id).status == "running"
    assert db.get_job(job_no_data.id).status == "queued"


def test_scheduler_tick_job_needing_uncached_dataset_not_dispatched_anywhere(db, audit_path):
    """Fable 覆核修正 1 (a)：資料集哪台機器都沒有快取時，需要這個資料集的
    訓練任務不會被派到任何機器（就算它是唯一的候選任務、機器閒著）。"""
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    job = enqueue_job(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}

    async def noop_ssh_run(server_name, command, timeout):
        return FakeCommandResult("")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, noop_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    assert db.get_job(job.id).status == "queued"


def test_scheduler_tick_job_becomes_dispatchable_after_cache_registered(db, audit_path):
    """修正 1 (b)：同一個任務，資料集被登記進 dataset_cache 之後，下一輪
    就變得可派。"""
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    job = enqueue_job(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    db.upsert_dataset_cache("server-a", "defect", "v1")

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}

    async def noop_ssh_run(server_name, command, timeout):
        return FakeCommandResult("")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, noop_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    updated = db.get_job(job.id)
    assert updated.status == "running"
    assert updated.server == "server-a"


# ---------------------------------------------------------------------------
# 階段 4：scheduler_tick 接上 on_job_finished（真實伺服器任務結束才觸發，
# _local sync 任務不觸發）
# ---------------------------------------------------------------------------


def test_scheduler_tick_invokes_on_job_finished_for_real_server_job_done(db, audit_path):
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")

    finished_jobs = []
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH({"exit_code": "0\n", "tail -n 40": "done\n"})

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file,
            audit_path=audit_path, on_job_finished=finished_jobs.append,
        )
    )

    assert len(finished_jobs) == 1
    assert finished_jobs[0].id == job.id
    assert finished_jobs[0].status == "done"


def test_scheduler_tick_sync_job_done_does_not_invoke_on_job_finished(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 300, "/data/defect/v1", {"file_count": 2, "total_size": 300}
    )
    job = enqueue_job(
        db, command="rsync ...", type="sync", pin_server="_local", audit_path=audit_path
    )
    db.update_job(
        job.id,
        status="running",
        server="_local",
        target_server="server-a",
        dataset_name="defect",
        dataset_version="v1",
    )

    finished_jobs = []
    ssh = FakeSSH(
        {"exit_code": "0\n", "tail -n 40": "rsync done\n", "find": "COUNT:2\nSIZE:300\n"}
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, {}, {}, ssh, noop_write_file,
            audit_path=audit_path, on_job_finished=finished_jobs.append,
        )
    )

    assert finished_jobs == []
    assert db.get_job(job.id).status == "done"


def test_scheduler_tick_sync_job_failure_does_not_invoke_on_job_finished(db, audit_path):
    """sync 任務的 cmd.sh 本身失敗（exit_code 非 0）走 apply_reconcile_outcome
    的 failed 分支（不是 finalize_sync_job），一樣不能觸發任務結束 hook。"""
    job = enqueue_job(
        db, command="rsync ...", type="sync", pin_server="_local", audit_path=audit_path
    )
    db.update_job(
        job.id,
        status="running",
        server="_local",
        target_server="server-a",
        dataset_name="defect",
        dataset_version="v1",
    )

    finished_jobs = []
    ssh = FakeSSH({"exit_code": "1\n", "tail -n 40": "rsync failed\n"})

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, {}, {}, ssh, noop_write_file,
            audit_path=audit_path, on_job_finished=finished_jobs.append,
        )
    )

    assert finished_jobs == []
    assert db.get_job(job.id).status == "failed"


# ---------------------------------------------------------------------------
# 階段 4：卡死偵測（stall detection）在 scheduler_tick 裡的整合行為
# ---------------------------------------------------------------------------


def _make_running_job_for_stall(db, audit_path, **overrides):
    job = enqueue_job(db, command="sleep 6000", audit_path=audit_path)
    fields = dict(status="running", server="server-a")
    fields.update(overrides)
    db.update_job(job.id, **fields)
    return db.get_job(job.id)


def test_scheduler_tick_marks_stalled_suspect_after_threshold(db, audit_path):
    past = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    job = _make_running_job_for_stall(db, audit_path, log_size=100, log_size_changed_at=past)

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH(
        {"exit_code": "", "tmux has-session": "EXISTS\n", "stat -c %s": "100\n"}
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file,
            audit_path=audit_path, stall_minutes=30,
        )
    )

    updated = db.get_job(job.id)
    assert updated.status == "running"  # 只是旗標，不改狀態
    assert updated.stalled_suspect == 1
    stall_events = [
        event
        for event in db.list_durable_audit_events(limit=50)
        if event["action"] == "execution_job_stall_state_recorded"
    ]
    assert len(stall_events) == 1
    assert stall_events[0]["result"] == "stalled"
    assert stall_events[0]["params"] == {
        "job_id": job.id,
        "previous_status": "running",
        "reason_code": "stall_threshold_reached",
        "server": "server-a",
        "stalled": True,
        "status": "running",
    }


def test_scheduler_tick_not_stalled_when_under_threshold(db, audit_path):
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    job = _make_running_job_for_stall(db, audit_path, log_size=100, log_size_changed_at=recent)

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH(
        {"exit_code": "", "tmux has-session": "EXISTS\n", "stat -c %s": "100\n"}
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file,
            audit_path=audit_path, stall_minutes=30,
        )
    )

    assert db.get_job(job.id).stalled_suspect == 0


def test_scheduler_tick_clears_stalled_suspect_when_log_grows_again(db, audit_path):
    past = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    job = _make_running_job_for_stall(
        db, audit_path, log_size=100, log_size_changed_at=past,
        stalled_suspect=1, stall_notified=1,
    )

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH(
        {"exit_code": "", "tmux has-session": "EXISTS\n", "stat -c %s": "500\n"}  # 又長大了
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file,
            audit_path=audit_path, stall_minutes=30,
        )
    )

    updated = db.get_job(job.id)
    assert updated.stalled_suspect == 0
    assert updated.stall_notified == 1  # 去重旗標不清，避免同一次卡住反覆寄信
    stall_events = [
        event
        for event in db.list_durable_audit_events(limit=50)
        if event["action"] == "execution_job_stall_state_recorded"
    ]
    assert len(stall_events) == 1
    assert stall_events[0]["result"] == "cleared"
    assert stall_events[0]["params"]["stalled"] is False
    assert stall_events[0]["params"]["reason_code"] == "log_resumed"


def test_scheduler_tick_stall_notification_fires_once_and_dedupes(db, audit_path):
    past = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    job = _make_running_job_for_stall(db, audit_path, log_size=100, log_size_changed_at=past)

    notified_ids = []

    def on_stall_detected(j):
        notified_ids.append(j.id)

    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH(
        {"exit_code": "", "tmux has-session": "EXISTS\n", "stat -c %s": "100\n"}
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file,
            audit_path=audit_path, on_stall_detected=on_stall_detected, stall_minutes=30,
        )
    )
    assert notified_ids == [job.id]
    assert db.get_job(job.id).stall_notified == 1
    assert len(
        [
            event
            for event in db.list_durable_audit_events(limit=50)
            if event["action"] == "execution_job_stall_state_recorded"
        ]
    ) == 1

    # 第二輪仍然卡住，但已經通知過，不該再呼叫一次
    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file,
            audit_path=audit_path, on_stall_detected=on_stall_detected, stall_minutes=30,
        )
    )
    assert notified_ids == [job.id]


def test_scheduler_stall_transition_rolls_back_when_durable_audit_fails(
    db, audit_path, monkeypatch
):
    past = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    job = _make_running_job_for_stall(db, audit_path, log_size=100, log_size_changed_at=past)
    original_append = db.append_durable_audit_event_in_transaction

    def fail_stall_event(cursor, **kwargs):
        if kwargs.get("action") == "execution_job_stall_state_recorded":
            raise RuntimeError("injected stall audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(
        db, "append_durable_audit_event_in_transaction", fail_stall_event
    )
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH(
        {"exit_code": "", "tmux has-session": "EXISTS\n", "stat -c %s": "100\n"}
    )

    async def noop_write_file(server_name, path, content):
        return None

    with pytest.raises(RuntimeError, match="injected stall audit failure"):
        asyncio.run(
            scheduler_tick(
                db,
                server_states,
                server_configs,
                ssh,
                noop_write_file,
                audit_path=audit_path,
                stall_minutes=30,
            )
        )

    rolled_back = db.get_job(job.id)
    assert rolled_back.stalled_suspect == 0
    assert rolled_back.log_size == 100
    assert not any(
        event["action"] == "execution_job_stall_state_recorded"
        for event in db.list_durable_audit_events(limit=50)
    )
    assert not any(record["action"] == "stall_suspect" for record in read_audit(audit_path))


def test_scheduler_tick_stall_check_skipped_when_ssh_unreachable(db, audit_path):
    job = _make_running_job_for_stall(
        db, audit_path, log_size=100, log_size_changed_at="2020-01-01T00:00:00+00:00"
    )
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH({}, unreachable=True)

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file, audit_path=audit_path,
        )
    )

    updated = db.get_job(job.id)
    assert updated.log_size == 100  # 完全沒有被改動
    assert updated.stalled_suspect == 0


def test_scheduler_tick_stall_check_skipped_when_log_not_found_yet(db, audit_path):
    job = _make_running_job_for_stall(db, audit_path, log_size=None, log_size_changed_at=None)
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=5.0)}
    server_configs = {"server-a": _idle_cpu_server_config("server-a")}
    ssh = FakeSSH(
        {"exit_code": "", "tmux has-session": "EXISTS\n", "stat -c %s": ""}
    )

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, ssh, noop_write_file, audit_path=audit_path,
        )
    )

    updated = db.get_job(job.id)
    assert updated.log_size is None
    assert updated.stalled_suspect == 0


# ---------------------------------------------------------------------------
# 階段 8 第二批（PLAN.md I.8）：enabled=false 的機器不會被派工
# ---------------------------------------------------------------------------


def test_scheduler_tick_does_not_dispatch_to_disabled_server(db, audit_path):
    """`enabled=false` 的機器 monitor 仍可以探測狀態（`server_states` 照常
    存在、online），但 `scheduler_tick()` 不會派工給它——任務保持
    queued。"""
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    server_cfg = _idle_cpu_server_config("server-a")
    server_cfg.enabled = False
    server_configs = {"server-a": server_cfg}

    calls: list[str] = []

    async def recording_ssh_run(server_name, command, timeout):
        calls.append(command)
        return FakeCommandResult("")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, recording_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    assert db.get_job(job.id).status == "queued"
    assert db.get_job(job.id).server is None
    assert calls == []  # 完全沒有對這台機器發任何 SSH 指令


def test_scheduler_tick_dispatches_to_other_enabled_server_when_one_disabled(db, audit_path):
    """有兩台機器、其中一台 disabled 時，任務會派給另一台 enabled 的機器
    （確認 enabled=false 只是跳過那一台，不影響其他機器正常派工）。"""
    job = enqueue_job(db, command="sleep 60", audit_path=audit_path)
    disabled_cfg = _idle_cpu_server_config("server-disabled")
    disabled_cfg.enabled = False
    server_states = {
        "server-disabled": ServerState(name="server-disabled", online=True, load1=0.1),
        "server-ok": ServerState(name="server-ok", online=True, load1=0.1),
    }
    server_configs = {
        "server-disabled": disabled_cfg,
        "server-ok": _idle_cpu_server_config("server-ok"),
    }

    async def recording_ssh_run(server_name, command, timeout):
        return FakeCommandResult("")

    async def noop_write_file(server_name, path, content):
        return None

    asyncio.run(
        scheduler_tick(
            db, server_states, server_configs, recording_ssh_run, noop_write_file,
            audit_path=audit_path,
        )
    )

    updated = db.get_job(job.id)
    assert updated.status == "running"
    assert updated.server == "server-ok"
