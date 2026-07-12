"""Email 通知（PLAN.md E 節）。

設定經 `app.config.AppConfig` 讀 `.env`：`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/
`SMTP_PASS`、`MAIL_FROM`、`MAIL_TO`。必要項（host/port/from/to）任一沒設
時 `send_mail()` 記 log 後直接跳過（回傳 `False`），系統照常運作——寄信是
錦上添花的通知，不該讓沒填 SMTP 設定變成系統掛掉的理由。`SMTP_USER`/
`SMTP_PASS` 允許留空（部分內網 relay 不需要認證）。

`smtplib` 是阻塞 API，`send_mail()` 用 `asyncio.to_thread()` 包起來避免堵住
event loop（跟 `app/datasets.py` 的 `build_manifest()` 掃描同樣的處理方式）。
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Optional

from app.config import AppConfig
from app.db import Job

logger = logging.getLogger(__name__)

_STATUS_LABEL = {
    "done": "成功",
    "failed": "失敗",
    "cancelled": "已取消",
    "blocked": "被擋住",
    "queued": "排隊中",
    "running": "執行中",
}


def _elapsed_str(started_at: Optional[str], finished_at: Optional[str]) -> str:
    """耗時字串（finished_at - started_at）。任一時間缺漏或格式無法解析都
    回傳「未知」，不讓組信這種純字串操作因為時間格式問題而拋例外。"""
    if not started_at or not finished_at:
        return "未知"
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except ValueError:
        return "未知"
    secs = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}時{minutes}分{seconds}秒"
    return f"{minutes}分{seconds}秒"


def build_job_mail(job: Job, result_path: Optional[str]) -> tuple[str, str]:
    """組出任務結束通知信的 (subject, body)。純字串組裝，不碰網路，方便
    單元測試。內容依原規格 5.6：任務名（用 id，任務目前沒有獨立的「名稱」
    欄位，用 command 前段輔助辨識）、專案、機器、成敗、exit code、耗時、
    結果路徑、log 尾 40 行。

    `result_path`：拉結果成功時是本地路徑字串；拉結果失敗或本來就不拉
    （failed/cancelled 任務）時傳 `None`，信裡顯示「無」。
    """
    status_label = _STATUS_LABEL.get(job.status, job.status)
    command_hint = job.command[:40] + ("…" if len(job.command) > 40 else "")
    subject = f"[調度中心] 任務 #{job.id} {status_label}（{job.project or command_hint}）"

    log_lines = (job.log_tail or "").splitlines()
    log_tail_40 = "\n".join(log_lines[-40:]) if log_lines else "（無 log）"

    body_lines = [
        f"任務 ID：{job.id}",
        f"專案：{job.project or '（無）'}",
        f"指令：{job.command}",
        f"機器：{job.server or '（無）'}",
        f"結果：{status_label}",
        f"Exit code：{job.exit_code if job.exit_code is not None else '（無）'}",
        f"耗時：{_elapsed_str(job.started_at, job.finished_at)}",
        f"結果路徑：{result_path or '無'}",
        "",
        "--- log 尾 40 行 ---",
        log_tail_40,
    ]
    return subject, "\n".join(body_lines)


def build_stall_mail(job: Job, stall_minutes: int) -> tuple[str, str]:
    """卡死偵測提醒信（PLAN.md E）：只提醒，不代表任務失敗，`status` 完全
    不受影響（見 `app/stall.py` 說明）。"""
    subject = f"[調度中心] 任務 #{job.id} 疑似卡死（log 超過 {stall_minutes} 分鐘無變化）"
    body_lines = [
        f"任務 ID：{job.id}",
        f"專案：{job.project or '（無）'}",
        f"指令：{job.command}",
        f"機器：{job.server or '（無）'}",
        "",
        f"job.log 已經超過 {stall_minutes} 分鐘沒有增長，可能卡住了（也可能只是跑得比較慢）。",
        "系統不會自動終止這個任務，這封信只是提醒；請自行到介面查看 log 內容，",
        "視情況決定是否手動停止。",
    ]
    return subject, "\n".join(body_lines)


def _mail_config_ready(config: AppConfig) -> bool:
    """SMTP_USER/SMTP_PASS 不列入必要項（允許不需要認證的內網 relay）。"""
    return bool(config.smtp_host and config.smtp_port and config.mail_from and config.mail_to)


def _send_mail_sync(config: AppConfig, subject: str, body: str) -> None:
    """實際跑 smtplib 的阻塞邏輯，`send_mail()` 用 `asyncio.to_thread()` 呼叫。"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = config.mail_from
    msg["To"] = config.mail_to

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except smtplib.SMTPNotSupportedError:
            pass  # 伺服器不支援 STARTTLS（例如純內網 relay），退回明文
        if config.smtp_user and config.smtp_pass:
            server.login(config.smtp_user, config.smtp_pass)
        server.sendmail(config.mail_from, [config.mail_to], msg.as_string())


async def send_mail(config: AppConfig, subject: str, body: str) -> bool:
    """寄信；任一必要設定沒填就跳過（記 log），回傳是否真的寄出去。

    寄信失敗（連線失敗、認證失敗、逾時等）一律捕捉例外、記 log、回傳
    `False`，**絕不**讓例外往上炸——寄信只是錦上添花的通知，不能拖累任務
    結束流程或排程迴圈。
    """
    if not _mail_config_ready(config):
        logger.info("SMTP 設定不完整，跳過寄信（subject=%r）", subject)
        return False
    try:
        await asyncio.to_thread(_send_mail_sync, config, subject, body)
    except Exception as exc:  # noqa: BLE001 - 寄信失敗不能讓呼叫端跟著炸
        logger.warning("寄信失敗（subject=%r）：%s", subject, exc)
        return False
    return True
