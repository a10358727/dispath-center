"""結果回收：任務完成後，從工作機把 `results/{id}/` 拉回 Server A 本地
`results/{id}/`（PLAN.md E 節）。

方向跟 sync 任務（`app/datasets.py`）相反（sync 是從 Server A 推到工作機，
這裡是從工作機拉回 Server A），但基礎設施相同：整個 rsync 指令用
`app.localrun.local_run()` 在 Server A 本地執行——rsync 內部用
`-e "ssh -i ..."` 連到工作機，不需要先 SSH 登入工作機才能發起 rsync（跟
`app/datasets.py` 的 sync 任務同樣道理，見該檔案模組 docstring）。

工作機上沒有 `results/{id}/`（任務本來就不產出任何結果）是正常情況：
rsync 會因為來源目錄不存在而失敗，但**不影響任務狀態**——呼叫端
（`app/jobfinish.py`）只記稽核 `result_pull_failed`，任務仍然是 done。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Optional

from app.config import ServerConfig
from app.datasets import build_ssh_opts


@dataclass
class PullResult:
    ok: bool
    path: Optional[str]
    error: Optional[str]


def local_result_dir(job_id: int, local_home_dir: str) -> str:
    """結果在 Server A 本地的目的地目錄（相對 `local_home_dir` 解析，路徑
    慣例同 `agent_jobs/{id}/...`，見 `app/jobqueue.py`）。這裡直接組完整
    路徑字串，方便寫進 email 內容與稽核紀錄。"""
    base = (local_home_dir or ".").rstrip("/") or "."
    return f"{base}/results/{job_id}/"


def build_result_pull_command(job_id: int, server: ServerConfig, local_home_dir: str) -> str:
    """組出「從工作機拉 `results/{id}/` 回本地」的 rsync 指令：先在本地
    `mkdir -p` 目的地目錄，成功後才 rsync 拉過來。"""
    dest = local_result_dir(job_id, local_home_dir)
    ssh_opts = build_ssh_opts(server.key_path, server.port)
    remote_src = f"{server.user}@{server.host}:results/{job_id}/"
    return (
        f"mkdir -p {shlex.quote(dest)} && "
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(remote_src)} {shlex.quote(dest)}"
    )


def build_bundle_push_command(
    run_job_id: int, coding_run_id: int, target: ServerConfig, local_home_dir: str
) -> str:
    """組出「把某次 Codex coding run 的 `changes.bundle` 從 Server A 本地推到
    下游目標機」的 rsync 指令（PLAN.md N.7，Codex Worker v2 的下游 bundle
    流）：方向跟 `build_result_pull_command()` 相反（那個是拉，這個是推），
    慣例模仿 `app.datasets.build_sync_script()`——先在目標機 `mkdir -p`
    目的地目錄，成功後才 rsync 推過去。

    - `run_job_id`：產出這份 bundle 的原始 Codex coding job id（**不是**這次
      要建立的下游任務 id）——本地來源固定是既有 E 節結果回收已經拉回的
      `results/{run_job_id}/changes.bundle`（`app.approvals.
      build_coding_task_script()` 的任務腳本在 Runner 上把它複製進
      `$HOME/results/$JOB_ID/`，既有 rsync 回收自動拉回 Server A）。
    - `coding_run_id`：對外定址用的 `coding_runs.id`——目的地固定是目標機
      `coding_bundles/{coding_run_id}/`（相對 SSH user home，路徑慣例同
      `app.jobqueue.AGENT_JOBS_DIR`），下游任務 `cmd.sh` 的前置段
      （`app.approvals.build_bundle_checkout_preamble()`）固定從這個路徑
      讀 bundle，兩邊路徑組裝邏輯必須一致。
    - `target.port`：port 統一由 `build_ssh_opts()` 處理（非 22 時附加
      `-p {port}`），不在這裡另外組裝，避免多處各自處理埠號的邏輯落差
      （2026-07-10：pro6000 32221 埠的修復把入口收斂到 `build_ssh_opts()`）。
    """
    dest_dir = f"coding_bundles/{coding_run_id}"
    src = f"{local_result_dir(run_job_id, local_home_dir)}changes.bundle"
    ssh_opts = build_ssh_opts(target.key_path, target.port)
    remote = f"{target.user}@{target.host}"
    remote_mkdir = f"{ssh_opts} {shlex.quote(remote)} {shlex.quote(f'mkdir -p {dest_dir}')}"
    rsync_cmd = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(src)} "
        f"{shlex.quote(f'{remote}:{dest_dir}/')}"
    )
    return f"{remote_mkdir} && {rsync_cmd}"


async def pull_job_results(
    local_run,
    job_id: int,
    server: ServerConfig,
    local_home_dir: str,
    timeout: float,
) -> PullResult:
    """在 Server A 本地跑 rsync，把 `job_id` 的 results 拉回來。

    `local_run` 是 `app.localrun.local_run` 或測試注入的假件，介面形狀：
    `async local_run(command, timeout) -> CommandResult`（`.exit_status`/
    `.stdout`/`.stderr`）。逾時、本地 subprocess 起不來等例外，或 rsync
    exit_status 非 0（例如來源目錄不存在），都回傳 `ok=False`，不往上炸
    例外——呼叫端只記稽核、不影響任務狀態。
    """
    command = build_result_pull_command(job_id, server, local_home_dir)
    try:
        result = await local_run(command, timeout)
    except Exception as exc:  # noqa: BLE001 - 本地 rsync 執行失敗（逾時等）
        return PullResult(ok=False, path=None, error=str(exc))

    if result.exit_status != 0:
        stderr_summary = (result.stderr or result.stdout or "").strip()
        # 只保留尾巴一段，避免稽核紀錄裡塞進整包很長的 rsync 錯誤輸出。
        stderr_summary = stderr_summary[-500:] or f"rsync exit_status={result.exit_status}"
        return PullResult(ok=False, path=None, error=stderr_summary)

    return PullResult(ok=True, path=local_result_dir(job_id, local_home_dir), error=None)
