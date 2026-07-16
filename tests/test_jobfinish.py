import asyncio

from app.audit import read_audit
from app.config import AppConfig, ServerConfig
from app.db import Job
from app.jobfinish import _safe_reported_test_result, handle_job_finished
from app.sshpool import CommandResult


def make_config(**overrides) -> AppConfig:
    base = dict(
        servers=[],
        local_home_dir="/srv/dispatch",
        result_pull_timeout_sec=600,
        smtp_host="smtp.example.com",
        smtp_port=587,
        mail_from="from@example.com",
        mail_to="to@example.com",
    )
    base.update(overrides)
    return AppConfig(**base)


def make_job(**overrides) -> Job:
    base = dict(
        id=1,
        type="train",
        project="demo",
        command="python train.py",
        require_tag=None,
        pin_server=None,
        status="done",
        server="server-a",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:05:00+00:00",
        exit_code=0,
        log_tail="training finished\n",
    )
    base.update(overrides)
    return Job(**base)


def make_server_cfg() -> ServerConfig:
    return ServerConfig(name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa")


class RecordingLocalRun:
    def __init__(self, order: list, exit_status: int = 0, stderr: str = ""):
        self.order = order
        self.exit_status = exit_status
        self.stderr = stderr

    async def __call__(self, command, timeout):
        self.order.append("pull")
        return CommandResult(exit_status=self.exit_status, stdout="", stderr=self.stderr)


def test_current_safe_wrapper_cannot_report_an_outer_test_result():
    job = make_job(
        type="coding",
        command="log '自動 repository validation 已安全跳過：受控 sandbox 尚未啟用'",
    )

    assert _safe_reported_test_result(
        job,
        {"test_command": "python3 -m pytest -q", "test_exit_code": 0},
    ) == (None, None)


def test_inflight_legacy_wrapper_keeps_allowlisted_test_result_compatibility():
    job = make_job(type="coding", command="python3 -m pytest -q")

    assert _safe_reported_test_result(
        job,
        {"test_command": "python3 -m pytest -q", "test_exit_code": 0},
    ) == ("python3 -m pytest -q", 0)


def make_recording_send_mail(order: list, mailed: bool = True):
    async def _send_mail(config, subject, body):
        order.append("mail")
        return mailed

    return _send_mail


# ---------------------------------------------------------------------------
# hook 順序：拉結果 → 寄信 → 寫稽核
# ---------------------------------------------------------------------------


def test_handle_job_finished_order_pull_then_mail(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    order: list[str] = []
    job = make_job(status="done")
    config = make_config()

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun(order),
            config=config,
            audit_path=audit_path,
            send_mail=make_recording_send_mail(order),
        )
    )

    assert order == ["pull", "mail"]
    records = read_audit(audit_path)
    actions = [r["action"] for r in records]
    assert actions == ["result_pulled", "job_notified"]
    assert records[-1]["params"]["mailed"] is True
    assert records[-1]["params"]["result_path"] == "/srv/dispatch/results/1/"


def test_handle_job_finished_pull_failure_does_not_block_mail_and_status_unaffected(tmp_path):
    """拉結果失敗（工作機沒有 results/{id}/）不影響任務狀態、照樣寄信。"""
    audit_path = str(tmp_path / "audit.jsonl")
    order: list[str] = []
    job = make_job(status="done")
    config = make_config()

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun(order, exit_status=23, stderr="no such file"),
            config=config,
            audit_path=audit_path,
            send_mail=make_recording_send_mail(order),
        )
    )

    # 拉結果失敗仍然照順序繼續往下寄信、寫稽核
    assert order == ["pull", "mail"]
    # handle_job_finished 完全不碰 job.status —— 它本來就是呼叫端傳進來的、
    # 已經落地的狀態
    assert job.status == "done"
    records = read_audit(audit_path)
    actions = [r["action"] for r in records]
    assert actions == ["result_pull_failed", "job_notified"]
    assert records[0]["result"] == "failed"
    assert records[-1]["params"]["result_path"] is None


def test_engineering_job_notification_and_audit_hide_executor_details(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    captured: dict = {}
    secret = "synthetic-notification-secret-123456789"
    private_path = "/home/runner/private/task-42"
    job = make_job(
        type="coding",
        command=f"cd {private_path} && AUTHORIZATION='Bearer {secret}' codex exec",
        log_tail=f"Authorization: Bearer {secret}\nworking at {private_path}\n",
        engineering_task_id="8da8c173-f0f5-4e0b-b67b-3aad07155182",
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([], exit_status=23, stderr=f"failed {secret}"),
            config=make_config(),
            audit_path=audit_path,
            send_mail=make_capturing_send_mail(captured),
        )
    )

    encoded_mail = captured["subject"] + captured["body"]
    assert "Run Codex agent in an isolated worktree" in encoded_mail
    assert secret not in encoded_mail
    assert private_path not in encoded_mail
    assert "結果路徑：無" in captured["body"]

    encoded_audit = str(read_audit(audit_path))
    assert secret not in encoded_audit
    assert private_path not in encoded_audit
    records = read_audit(audit_path)
    assert records[0]["params"]["error_category"] == "result_transport_failed"
    assert records[-1]["params"]["result_available"] is False


def test_handle_job_finished_failed_job_skips_pull_but_still_mails(tmp_path):
    """failed/cancelled 任務不拉結果，一樣寄信。"""
    audit_path = str(tmp_path / "audit.jsonl")
    order: list[str] = []
    job = make_job(status="failed", exit_code=1, log_tail="Traceback\n")
    config = make_config()

    pull_calls: list[str] = []

    async def local_run_should_not_be_called(command, timeout):
        pull_calls.append(command)
        return CommandResult(exit_status=0, stdout="", stderr="")

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=local_run_should_not_be_called,
            config=config,
            audit_path=audit_path,
            send_mail=make_recording_send_mail(order),
        )
    )

    assert pull_calls == []  # 完全沒呼叫 rsync 拉結果
    assert order == ["mail"]
    records = read_audit(audit_path)
    assert [r["action"] for r in records] == ["job_notified"]
    assert records[-1]["params"]["result_path"] is None


def test_handle_job_finished_no_server_cfg_skips_pull(tmp_path):
    """server_cfg 拿不到（例如伺服器設定被移除）：跳過拉結果，一樣寄信。"""
    audit_path = str(tmp_path / "audit.jsonl")
    order: list[str] = []
    job = make_job(status="done")
    config = make_config()

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=None,
            local_run=RecordingLocalRun(order),
            config=config,
            audit_path=audit_path,
            send_mail=make_recording_send_mail(order),
        )
    )

    assert order == ["mail"]


# ---------------------------------------------------------------------------
# 階段 5：寄信前的 LLM 摘要（可注入假 summarize_mail 測試兩個分支）
# ---------------------------------------------------------------------------


def make_capturing_send_mail(captured: dict):
    async def _send_mail(config, subject, body):
        captured["subject"] = subject
        captured["body"] = body
        return True

    return _send_mail


def test_handle_job_finished_prepends_llm_summary_when_available(tmp_path):
    """LLM 可用時，摘要應該被加在信件開頭。"""
    audit_path = str(tmp_path / "audit.jsonl")
    job = make_job(status="done")
    config = make_config()
    captured: dict = {}

    async def fake_summarize(body, cfg):
        assert cfg is config
        return "訓練成功，耗時 5 分鐘。"

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=None,
            local_run=None,
            config=config,
            audit_path=audit_path,
            send_mail=make_capturing_send_mail(captured),
            summarize_mail=fake_summarize,
        )
    )

    assert captured["body"].startswith("【AI 摘要】\n訓練成功，耗時 5 分鐘。\n\n")
    assert "任務 ID：1" in captured["body"]  # 原信內容還在


def test_handle_job_finished_sends_original_mail_when_summary_returns_none(tmp_path):
    """`summarize_mail` 回傳 None（LLM 不可用或摘要失敗）時，照常寄原信，
    不會因為沒有摘要而不寄信。"""
    audit_path = str(tmp_path / "audit.jsonl")
    job = make_job(status="done")
    config = make_config()
    captured: dict = {}

    async def fake_summarize(body, cfg):
        return None

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=None,
            local_run=None,
            config=config,
            audit_path=audit_path,
            send_mail=make_capturing_send_mail(captured),
            summarize_mail=fake_summarize,
        )
    )

    assert "【AI 摘要】" not in captured["body"]
    assert captured["body"].startswith("任務 ID：1")


def test_handle_job_finished_summarize_mail_exception_does_not_block_sending(tmp_path):
    """即使注入的 `summarize_mail` 意外丟例外，寄信這件事也絕不能被拖累
    （防禦性 try/except，見 handle_job_finished docstring）。"""
    audit_path = str(tmp_path / "audit.jsonl")
    job = make_job(status="done")
    config = make_config()
    captured: dict = {}

    async def fake_summarize_raises(body, cfg):
        raise RuntimeError("boom")

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=None,
            local_run=None,
            config=config,
            audit_path=audit_path,
            send_mail=make_capturing_send_mail(captured),
            summarize_mail=fake_summarize_raises,
        )
    )

    assert "body" in captured  # 郵件還是寄出去了
    assert "【AI 摘要】" not in captured["body"]

    records = read_audit(audit_path)
    assert [r["action"] for r in records] == ["job_notified"]
    assert records[-1]["params"]["mailed"] is True
