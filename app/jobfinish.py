"""任務結束 hook：狀態更新之後依序「拉結果 → 寄信 → 寫稽核」（PLAN.md E
節固定順序）。

狀態更新本身由 `app.jobqueue.apply_reconcile_outcome()` 在呼叫這裡之前就
已經落地完成（見該函式對 `on_job_finished` 的說明），所以這個模組只負責
剩下三步。這裡刻意寫成一個獨立、不管背景排程細節的協調函式：不呼叫
`asyncio.create_task()`、不做例外吞噬與 task 追蹤（那些是
`app.main.AppState.schedule_job_finished_hook()` 的責任），方便直接用假的
`local_run`/`send_mail` 注入單元測試「順序」與「拉結果失敗不影響後續」這
兩件事，不需要真的起背景 task。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.audit import SYSTEM_AUDIT_ACTOR, append_audit
from app.config import AppConfig, ServerConfig
from app.db import Database, Job
from app.llm import summarize_mail_body as default_summarize_mail_body
from app.mailer import build_job_mail
from app.mailer import send_mail as default_send_mail
from app.results import local_result_dir, pull_job_results

logger = logging.getLogger(__name__)


def _backfill_coding_run(job: Job, *, db: Database, config: AppConfig, audit_path: str) -> None:
    """階段 13（PLAN.md N.6，Codex Worker v2）：`job.type == "coding"` 的
    job 結束後，解析本地已回收的 `results/{job_id}/result.json`（見
    `app/approvals.py` 的 `build_coding_task_script()` 的 `write_result()`
    ——結構化結果的唯一來源）回填對應的 `coding_runs` 一列。

    - 找不到對應的 `coding_runs`（`db.get_coding_run_by_job_id()` 回傳
      `None`）：v1 遺留的 coding job（approve() 沒有建過 coding_run）——
      直接跳過，不當成錯誤，呼叫端仍照常往下寄信。
    - `result.json` 不存在或不是合法 JSON（例如 rsync 拉結果失敗、
      任務腳本在寫檔前就整個中止）：`status="failed"`，
      `error_message` 附上 `job.exit_code` 供人到 job log 追查——這正是
      「拉結果條件放寬到 coding 的 failed 任務」的理由（見
      `handle_job_finished()` docstring）：這種情況下 job 本身也是
      `failed`，但我們仍然嘗試過拉一次，只是沒拉到東西。
    - 合法 JSON：直接把 `result.json` 的欄位映射進
      `coding_runs`（`status`/`base_commit`/`result_branch`/
      `result_commit`/`test_command`/`test_exit_code`/`error_message`/
      `codex_version`），`bundle_path` 只在本地確實有
      `results/{job_id}/changes.bundle` 檔案時才填（no_changes／
      secret_violation／codex 失敗這幾種終態不會有這個檔案）。
      `started_at`/`finished_at` 直接抄 `job.started_at`/`job.finished_at`
      （scheduler dispatch 時已經記過，不需要另外記 coding_dispatch）。
    - 不論以上哪一種結果都寫稽核 `coding_finished`
      （`job_id`/`coding_run_id`/`status`/`result_commit`）。
    """
    coding_run = db.get_coding_run_by_job_id(job.id)
    if coding_run is None:
        return

    result_dir = Path(local_result_dir(job.id, config.local_home_dir))
    result_json_path = result_dir / "result.json"
    bundle_path_candidate = result_dir / "changes.bundle"

    fields: dict[str, Any] = {
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    try:
        data = json.loads(result_json_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("result.json 內容不是 JSON object")
    except (OSError, ValueError) as exc:
        fields["status"] = "failed"
        fields["error_message"] = (
            f"result.json 缺失或不可解析；job exit={job.exit_code}，見 job log"
        )
        logger.warning(
            "coding_run #%s（job #%s）回填失敗，result.json 缺失或不可解析：%s",
            coding_run.id,
            job.id,
            exc,
        )
    else:
        fields["status"] = data.get("status") or "failed"
        fields["base_commit"] = data.get("base_commit")
        fields["result_branch"] = data.get("result_branch")
        fields["result_commit"] = data.get("result_commit")
        fields["test_command"] = data.get("test_command")
        fields["test_exit_code"] = data.get("test_exit_code")
        fields["error_message"] = data.get("error_message")
        fields["codex_version"] = data.get("codex_version")
        if bundle_path_candidate.exists():
            fields["bundle_path"] = str(bundle_path_candidate)

    db.update_coding_run(coding_run.id, **fields)
    append_audit(
        "coding_finished",
        {
            "job_id": job.id,
            "coding_run_id": coding_run.id,
            "status": fields.get("status"),
            "result_commit": fields.get("result_commit"),
        },
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )


async def handle_job_finished(
    job: Job,
    *,
    server_cfg: Optional[ServerConfig],
    local_run,
    config: AppConfig,
    audit_path: str,
    db: Optional[Database] = None,
    send_mail=default_send_mail,
    summarize_mail=default_summarize_mail_body,
) -> None:
    """任務結束背景 hook 的核心邏輯。

    - 拉結果條件：`job.status == "done"`，**或**（階段 13，PLAN.md N.6）
      `job.type == "coding"` 且 `job.status == "failed"`——coding 任務
      failed 時 `result.json`／`final_message.txt` 往往是唯一的診斷線索
      （codex 執行失敗、secret 檔案違規等任務腳本自己判定的失敗都會先把
      這些檔案複製進 `results/{job_id}/` 才 `exit 1`，見
      `app/approvals.py` 的 `build_coding_task_script()`），不能因為任務
      本身標成 failed 就整個放棄拉結果。其餘 failed/cancelled 任務不拉
      結果，一樣寄信。
    - 拉結果失敗（工作機沒有 `results/{id}/`、rsync 逾時等）**不影響任務
      狀態**——這裡完全不碰 `job.status`，只記稽核 `result_pull_failed`
      後繼續往下寄信；拉成功記稽核 `result_pulled`。
    - 階段 13：拉結果這一步之後（不論拉成功與否），若 `job.type ==
      "coding"` 且呼叫端有傳入 `db`——呼叫 `_backfill_coding_run()` 回填
      對應的 `coding_runs`（見該函式 docstring）。`db` 是選填參數（預設
      `None`）：`app.main.AppState.schedule_job_finished_hook()` 目前還
      沒有接這條線（留給後續批次），沒傳 `db` 時這段整個跳過，不影響
      既有寄信行為，也不會因為缺參數而報錯。
    - 寄信內容的結果路徑：拉成功給本地路徑，否則「無」（`None`，交給
      `build_job_mail()` 轉成「無」字樣）。
    - 階段 5：寄信前若 LLM 可用，把信件內容餵給 `app.llm.summarize_mail_body()`
      產生 ≤3 行摘要，加在信件開頭。`summarize_mail_body()` 本身在 LLM
      不可用時就直接回傳 `None`，這裡再包一層 `try/except` 是防禦性作法
      （就算呼叫端注入的假 `summarize_mail` 意外丟例外，也不能讓寄信這件
      事跟著失敗）——**絕不因為摘要失敗而不寄信**。
    - 最後寫一筆總結稽核 `job_notified`（含是否寄信成功、結果路徑），對應
      PLAN.md「更新狀態→拉結果→寄信→寫稽核」的最後一步。
    """
    result_path: Optional[str] = None
    should_pull = job.status == "done" or (job.type == "coding" and job.status == "failed")
    if should_pull and server_cfg is not None:
        pull = await pull_job_results(
            local_run, job.id, server_cfg, config.local_home_dir, config.result_pull_timeout_sec
        )
        if pull.ok:
            result_path = pull.path
            append_audit(
                "result_pulled",
                {"job_id": job.id, "path": pull.path},
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
        else:
            append_audit(
                "result_pull_failed",
                {"job_id": job.id, "error": pull.error},
                result="failed",
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )

    if db is not None and job.type == "coding":
        _backfill_coding_run(job, db=db, config=config, audit_path=audit_path)

    subject, body = build_job_mail(job, result_path)

    try:
        summary = await summarize_mail(body, config)
    except Exception as exc:  # noqa: BLE001 - 摘要失敗絕不能拖累寄信
        logger.warning("寄信摘要生成失敗，照常寄原信: %s", exc)
        summary = None
    if summary:
        body = f"【AI 摘要】\n{summary}\n\n{body}"

    mailed = await send_mail(config, subject, body)

    append_audit(
        "job_notified",
        {"job_id": job.id, "mailed": mailed, "result_path": result_path},
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )
