import asyncio

import pytest

from app.config import AppConfig
from app.db import Job
from app.mailer import build_job_mail, build_stall_mail, send_mail


def make_config(**overrides) -> AppConfig:
    base = dict(
        servers=[],
        smtp_host=None,
        smtp_port=None,
        smtp_user=None,
        smtp_pass=None,
        mail_from=None,
        mail_to=None,
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
        finished_at="2026-01-01T00:05:30+00:00",
        exit_code=0,
        log_tail="\n".join(f"line {i}" for i in range(1, 61)),
    )
    base.update(overrides)
    return Job(**base)


# ---------------------------------------------------------------------------
# build_job_mail：純字串組裝
# ---------------------------------------------------------------------------


def test_build_job_mail_success_contains_all_required_fields():
    job = make_job()
    subject, body = build_job_mail(job, result_path="/home/train/results/1/")

    assert "#1" in subject
    assert "成功" in subject
    assert "demo" in body
    assert "python train.py" in body
    assert "server-a" in body
    assert "成功" in body
    assert "Exit code：0" in body
    assert "5分30秒" in body
    assert "/home/train/results/1/" in body
    assert "line 60" in body  # log 尾應該保留最後幾行
    assert "line 1\n" not in body  # 只留尾 40 行，前面的行不該出現


def test_build_job_mail_log_tail_truncated_to_40_lines():
    job = make_job(log_tail="\n".join(f"line {i}" for i in range(1, 101)))
    _subject, body = build_job_mail(job, result_path=None)
    assert "line 100" in body
    assert "line 61" in body
    assert "line 60" not in body  # 只留最後 40 行（61~100）


def test_build_job_mail_failed_shows_failure_and_no_result_path():
    job = make_job(status="failed", exit_code=1, log_tail="Traceback...\n")
    subject, body = build_job_mail(job, result_path=None)
    assert "失敗" in subject
    assert "失敗" in body
    assert "Exit code：1" in body
    assert "結果路徑：無" in body


def test_build_job_mail_missing_started_or_finished_elapsed_unknown():
    job = make_job(started_at=None)
    _subject, body = build_job_mail(job, result_path=None)
    assert "耗時：未知" in body


def test_build_stall_mail_mentions_stall_minutes_and_no_auto_kill():
    job = make_job(status="running")
    subject, body = build_stall_mail(job, stall_minutes=30)
    assert "疑似卡死" in subject
    assert "30" in body
    assert "不會自動終止" in body


# ---------------------------------------------------------------------------
# send_mail：設定不完整就跳過；寄信失敗不炸例外
# ---------------------------------------------------------------------------


def test_send_mail_skips_when_config_incomplete():
    config = make_config()  # 什麼都沒設
    ok = asyncio.run(send_mail(config, "subject", "body"))
    assert ok is False


def test_send_mail_skips_when_only_partial_config():
    config = make_config(smtp_host="smtp.example.com", smtp_port=587)  # 缺 from/to
    ok = asyncio.run(send_mail(config, "subject", "body"))
    assert ok is False


class _FakeSMTP:
    """假的 smtplib.SMTP context manager，不真的連網路。"""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, to_addrs, msg))


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


def test_send_mail_success_path_with_fake_smtp(monkeypatch):
    monkeypatch.setattr("app.mailer.smtplib.SMTP", _FakeSMTP)
    config = make_config(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="u",
        smtp_pass="p",
        mail_from="from@example.com",
        mail_to="to@example.com",
    )
    ok = asyncio.run(send_mail(config, "subject-x", "body-y"))
    assert ok is True
    assert len(_FakeSMTP.instances) == 1
    sent = _FakeSMTP.instances[0].sent
    assert len(sent) == 1
    assert sent[0][0] == "from@example.com"
    assert sent[0][1] == ["to@example.com"]
    assert "subject-x" in sent[0][2]


def test_send_mail_failure_does_not_raise(monkeypatch):
    class _RaisingSMTP:
        def __init__(self, *a, **kw):
            raise ConnectionRefusedError("simulated connection failure")

    monkeypatch.setattr("app.mailer.smtplib.SMTP", _RaisingSMTP)
    config = make_config(
        smtp_host="smtp.example.com",
        smtp_port=587,
        mail_from="from@example.com",
        mail_to="to@example.com",
    )
    ok = asyncio.run(send_mail(config, "subject", "body"))
    assert ok is False
