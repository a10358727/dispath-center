import asyncio
import os

from app.audit import read_audit
from app.config import AppConfig, ServerConfig
from app.db import Database, Job
from app.jobfinish import _collect_run_metrics, _safe_reported_test_result, handle_job_finished
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


# ---------------------------------------------------------------------------
# DG-METRICS-CONTRACT v1 (docs/DG_METRICS_CONTRACT_DECISION.md, approved
# 2026-08-24): metrics-v1 collection wiring after a successful result pull.
# ---------------------------------------------------------------------------


def write_metrics_file(local_home_dir: str, job_id: int, content: bytes) -> None:
    result_dir = os.path.join(local_home_dir, "results", str(job_id))
    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "metrics.json"), "wb") as fh:
        fh.write(content)


def make_metrics_db_and_job(tmp_path, **job_overrides):
    db = Database(str(tmp_path / "metrics-test.db"))
    job_id = db.insert_job(command="python train.py")
    job = make_job(id=job_id, **job_overrides)
    return db, job


def test_metrics_v1_collects_after_successful_pull_when_flag_enabled(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    write_metrics_file(
        config.local_home_dir, job.id, b'{"loss": "0.5", "epoch": 3}'
    )

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=make_recording_send_mail([]),
        )
    )

    collection = db.get_run_metrics_collection(job.id)
    assert collection["status"] == "collected"
    assert collection["reason"] is None
    assert collection["source_sha256"] is not None
    metrics = {row["key"]: row for row in db.list_run_metrics(job.id)}
    assert metrics["loss"]["value_type"] == "decimal"
    assert metrics["loss"]["value_text"] == "0.5"
    assert metrics["epoch"]["value_type"] == "int"

    actions = [r["action"] for r in read_audit(audit_path)]
    assert "metrics_collected" in actions
    assert "metrics_collection_failed" not in actions


def test_metrics_v1_missing_file_records_missing_status_not_an_error(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    # Deliberately do not write results/{job_id}/metrics.json.

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=make_recording_send_mail([]),
        )
    )

    collection = db.get_run_metrics_collection(job.id)
    assert collection["status"] == "missing"
    assert collection["source_sha256"] is None
    assert db.list_run_metrics(job.id) == []
    # missing = unknown, not a failure -- job finalization proceeds normally
    # and this outcome is not audited as a failed action.
    actions = [r["action"] for r in read_audit(audit_path)]
    assert "result_pulled" in actions
    assert "job_notified" in actions
    assert "metrics_collection_failed" not in actions


def test_metrics_v1_invalid_content_records_invalid_status_and_audit(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": 1.5}')  # float rejected

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=make_recording_send_mail([]),
        )
    )

    collection = db.get_run_metrics_collection(job.id)
    assert collection["status"] == "invalid"
    assert collection["reason"] == "float_value_rejected"
    assert db.list_run_metrics(job.id) == []
    records = read_audit(audit_path)
    failed = [r for r in records if r["action"] == "metrics_collection_failed"]
    assert len(failed) == 1
    assert failed[0]["result"] == "failed"
    assert failed[0]["params"]["status"] == "invalid"


def test_metrics_v1_oversize_content_records_oversize_status(tmp_path):
    from app.metrics_v1 import MAX_METRICS_BYTES

    audit_path = str(tmp_path / "audit.jsonl")
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    write_metrics_file(
        config.local_home_dir,
        job.id,
        b'{"k":"' + b"x" * (MAX_METRICS_BYTES + 100) + b'"}',
    )

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=make_recording_send_mail([]),
        )
    )

    collection = db.get_run_metrics_collection(job.id)
    assert collection["status"] == "oversize"
    assert db.list_run_metrics(job.id) == []


def test_metrics_v1_flag_off_never_parses_or_writes(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=False)
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": 1}')

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=make_recording_send_mail([]),
        )
    )

    assert db.get_run_metrics_collection(job.id) is None
    actions = [r["action"] for r in read_audit(audit_path)]
    assert "metrics_collected" not in actions
    assert "metrics_collection_failed" not in actions


def test_metrics_v1_pull_failure_never_writes_metrics(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": 1}')

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([], exit_status=1, stderr="no such file"),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=make_recording_send_mail([]),
        )
    )

    # The pull itself failed (rsync exit != 0) -- metrics collection must
    # never run against a result directory that was never populated by this
    # attempt, even though the file happens to already exist on disk.
    assert db.get_run_metrics_collection(job.id) is None
    assert job.status == "done"  # untouched by the metrics path either way


def test_metrics_v1_no_db_param_is_a_silent_noop(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    job = make_job(status="done")
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": 1}')

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            send_mail=make_recording_send_mail([]),
            # db intentionally omitted (defaults to None)
        )
    )

    actions = [r["action"] for r in read_audit(audit_path)]
    assert "metrics_collected" not in actions
    assert "metrics_collection_failed" not in actions


def test_metrics_v1_recollection_with_unchanged_bytes_is_idempotent(tmp_path):
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": "0.5"}')
    audit_path = str(tmp_path / "audit.jsonl")

    _collect_run_metrics(job, db=db, config=config, audit_path=audit_path)
    first = db.get_run_metrics_collection(job.id)
    _collect_run_metrics(job, db=db, config=config, audit_path=audit_path)
    second = db.get_run_metrics_collection(job.id)

    assert first["source_sha256"] == second["source_sha256"]
    assert first["status"] == second["status"] == "collected"
    assert len(db.list_run_metrics(job.id)) == 1


def test_metrics_v1_recollection_after_file_changes_replaces_rows(tmp_path):
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    audit_path = str(tmp_path / "audit.jsonl")
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": "0.5"}')

    _collect_run_metrics(job, db=db, config=config, audit_path=audit_path)
    write_metrics_file(config.local_home_dir, job.id, b'{"accuracy": "0.9000"}')
    _collect_run_metrics(job, db=db, config=config, audit_path=audit_path)

    metrics = db.list_run_metrics(job.id)
    assert [row["key"] for row in metrics] == ["accuracy"]


def test_metrics_v1_db_write_failure_never_raises_or_touches_job_status(tmp_path, monkeypatch):
    db, job = make_metrics_db_and_job(tmp_path)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=True)
    audit_path = str(tmp_path / "audit.jsonl")
    write_metrics_file(config.local_home_dir, job.id, b'{"loss": "0.5"}')

    def boom(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(db, "replace_run_metrics", boom)

    # Must not raise despite the injected DB failure (INV-SSH-6: metrics
    # failures never propagate into job finalization).
    _collect_run_metrics(job, db=db, config=config, audit_path=audit_path)
    assert job.status == "done"
