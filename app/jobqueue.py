"""任務佇列：CRUD、狀態機、依賴檢查、哨兵協議 reconcile。

命名避免撞到標準函式庫的 `queue` 模組，因此叫 jobqueue。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from app.audit import append_audit, now_iso
from app.db import Database, Job
from app.security import is_dangerous

#: SFTP 不做 tilde 展開（`sftp.open("~/...")` 會去找一個字面上叫 `~` 的
#: 目錄而不是 home），所以這裡一律用「相對於 home 的相對路徑」，不帶
#: `~/` 前綴。非互動 SSH session（`conn.run(...)`）的 cwd 預設就是 home，
#: 所以 shell 指令（mkdir/tmux/cat/tail）用同樣的相對路徑一樣正確，
#: 兩邊（SFTP 與 shell）路徑語意才會一致。
AGENT_JOBS_DIR = "agent_jobs"

# 階段 1 額外允許的終止態，不在原規格 5.3 的 queued/running/done/failed/blocked
# 之列，但 PLAN.md 的最小 API 明確要求「POST /jobs/{id}/cancel：僅限
# queued」，需要一個表示「使用者主動取消」的終止狀態，用 "cancelled"。
CANCELLED = "cancelled"


class DangerousCommandError(ValueError):
    """指令命中危險黑名單，拒絕入列。"""


# --------------------------------------------------------------------------
# 依賴檢查（純函式）
# --------------------------------------------------------------------------


def deps_all_done(dep_statuses: list[str]) -> bool:
    """所有依賴都是 done 才算「依賴已完成」。沒有依賴視為已完成。"""
    return all(s == "done" for s in dep_statuses)


def deps_any_blocked(dep_statuses: list[str]) -> bool:
    """任一依賴是 failed/blocked/missing（被刪除）就視為本任務要被擋住。"""
    return any(s in ("failed", "blocked", "missing") for s in dep_statuses)


def dependency_statuses(db: Database, depends_on: list[int]) -> list[str]:
    statuses = []
    for dep_id in depends_on:
        dep = db.get_job(dep_id)
        statuses.append(dep.status if dep is not None else "missing")
    return statuses


# --------------------------------------------------------------------------
# 入列 / 查詢 / 取消
# --------------------------------------------------------------------------


def enqueue_job(
    db: Database,
    *,
    command: str,
    type: str = "adhoc",
    project: Optional[str] = None,
    require_tag: Optional[str] = None,
    pin_server: Optional[str] = None,
    depends_on: Optional[list[int]] = None,
    gpus_needed: Optional[int] = None,
    priority: str = "normal",
    audit_path: str = "audit.jsonl",
    source_coding_run_id: Optional[int] = None,
) -> Job:
    """危險指令直接拒絕（寫稽核＋丟例外），安全指令才入列（寫稽核）。

    `source_coding_run_id`（階段 13，PLAN.md N.6）：選填，這個 job 是「用
    某次 Codex coding run 的 result（changes.bundle）當起點」的後續
    train／驗證 job 時才帶，直接透傳給 `db.insert_job()`。
    """
    dangerous, reason = is_dangerous(command)
    if dangerous:
        append_audit(
            "reject",
            {"command": command, "reason": reason},
            result="rejected",
            path=audit_path,
        )
        raise DangerousCommandError(reason)

    job_id = db.insert_job(
        command=command,
        type=type,
        project=project,
        require_tag=require_tag,
        pin_server=pin_server,
        depends_on=depends_on,
        gpus_needed=gpus_needed,
        priority=priority,
        source_coding_run_id=source_coding_run_id,
    )
    job = db.get_job(job_id)
    append_audit(
        "enqueue",
        {
            "job_id": job_id,
            "command": command,
            "project": project,
            "priority": priority,
            "pin_server": pin_server,
            "require_tag": require_tag,
            "depends_on": depends_on or [],
        },
        path=audit_path,
    )
    return job


def cancel_job(db: Database, job_id: int, audit_path: str = "audit.jsonl") -> bool:
    """只能取消還在 queued 的任務，回傳是否成功。"""
    job = db.get_job(job_id)
    if job is None or job.status != "queued":
        return False
    db.update_job(job_id, status=CANCELLED, finished_at=now_iso())
    append_audit("cancel", {"job_id": job_id}, path=audit_path)
    return True


def refresh_blocked_jobs(db: Database, audit_path: str = "audit.jsonl") -> list[int]:
    """掃描 queued 任務，依賴中有 failed/blocked/missing 的標記為 blocked。"""
    blocked_ids: list[int] = []
    for job in db.list_jobs(status="queued"):
        if not job.depends_on:
            continue
        statuses = dependency_statuses(db, job.depends_on)
        if deps_any_blocked(statuses):
            db.update_job(job.id, status="blocked")
            append_audit(
                "blocked",
                {"job_id": job.id, "depends_on": job.depends_on, "dep_statuses": statuses},
                path=audit_path,
            )
            blocked_ids.append(job.id)
    return blocked_ids


def list_dispatchable_jobs(db: Database) -> list[Job]:
    """回傳目前「依賴已完成」的 queued 任務（尚未看 pin_server/require_tag/優先權）。"""
    candidates: list[Job] = []
    for job in db.list_jobs(status="queued"):
        if job.depends_on:
            statuses = dependency_statuses(db, job.depends_on)
            if not deps_all_done(statuses):
                continue
        candidates.append(job)
    return candidates


# --------------------------------------------------------------------------
# 哨兵檔案協議：reconcile
# --------------------------------------------------------------------------


@dataclass
class ReconcileOutcome:
    status: str  # "running" | "done" | "failed" | "requeued" | "unreachable"
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None


def build_dispatch_paths(job_id: int) -> dict[str, str]:
    job_dir = f"{AGENT_JOBS_DIR}/{job_id}"
    return {
        "dir": job_dir,
        "cmd_sh": f"{job_dir}/cmd.sh",
        "run_sh": f"{job_dir}/run.sh",
        "log": f"{job_dir}/job.log",
        "exit_code": f"{job_dir}/exit_code",
    }


def build_run_sh_content(job_id: int) -> str:
    paths = build_dispatch_paths(job_id)
    return (
        f"bash {paths['cmd_sh']} > {paths['log']} 2>&1; "
        f"echo $? > {paths['exit_code']}\n"
    )


def build_mkdir_command(job_id: int) -> str:
    paths = build_dispatch_paths(job_id)
    return f"mkdir -p {paths['dir']}"


def build_launch_command(job_id: int) -> str:
    paths = build_dispatch_paths(job_id)
    return f"tmux new-session -d -s job_{job_id} 'bash {paths['run_sh']}'"


def build_check_exit_code_command(job_id: int) -> str:
    paths = build_dispatch_paths(job_id)
    return f"cat {paths['exit_code']} 2>/dev/null"


def build_log_tail_command(job_id: int, lines: int = 40) -> str:
    paths = build_dispatch_paths(job_id)
    return f"tail -n {lines} {paths['log']} 2>/dev/null"


def build_tmux_check_command(job_id: int) -> str:
    return f"tmux has-session -t job_{job_id} 2>/dev/null && echo EXISTS || echo GONE"


def build_log_size_command(job_id: int) -> str:
    """階段 4 卡死偵測：查 `job.log` 目前大小（bytes）。檔案不存在
    （任務剛派發、還沒開始寫 log）時 `stat` 出錯，`2>/dev/null` 讓 stdout
    維持空字串，呼叫端（`app.stall.parse_log_size`）會解析成 None（本輪
    跳過，不做任何判定）。"""
    paths = build_dispatch_paths(job_id)
    return f"stat -c %s {paths['log']} 2>/dev/null"


async def reconcile_job(ssh_run, server_name: str, job_id: int) -> ReconcileOutcome:
    """依哨兵檔案協議判定單一 running 任務目前狀態。

    `ssh_run` 是 async callable：`await ssh_run(server_name, command, timeout)
    -> CommandResult`（需有 `.stdout` 屬性）。SSH 連不上時 ssh_run 應該丟例外，
    這裡一律轉成 `unreachable`，呼叫端不對該任務做任何狀態變更（本輪跳過）。

    三個分支（PLAN.md A.1）：
    1. exit_code 檔案存在 → 依內容判 done/failed，抓 log 尾 40 行。
    2. exit_code 不存在，tmux session 還在 → running（不變）。
    3. exit_code 不存在，tmux session 不在 → 任務被中斷 → requeued。

    競態處理：分支 1 判定「不存在」與分支 3 查 tmux 之間有時間差——如果
    任務剛好在這個空檔完成（寫入 exit_code、tmux session 退出），會被
    誤判成「中斷」而 requeue，一個成功的任務就白白重跑一次。因此 tmux
    回報 GONE 之後，**再查一次 exit_code** 才能下 requeued 的結論。
    """
    outcome = await _check_exit_code(ssh_run, server_name, job_id)
    if outcome is not None:
        return outcome

    try:
        tmux_res = await ssh_run(server_name, build_tmux_check_command(job_id), 15)
    except Exception:  # noqa: BLE001
        return ReconcileOutcome(status="unreachable")

    if "EXISTS" in (tmux_res.stdout or ""):
        return ReconcileOutcome(status="running")

    # tmux 說 session 不在了，但有可能任務剛好在這個瞬間完成，
    # 再查一次 exit_code，確認真的沒有才判定為中斷。
    outcome = await _check_exit_code(ssh_run, server_name, job_id)
    if outcome is not None:
        return outcome
    return ReconcileOutcome(status="requeued")


async def _check_exit_code(
    ssh_run, server_name: str, job_id: int
) -> Optional[ReconcileOutcome]:
    """查一次 exit_code 檔案；有結果回傳 done/failed 的 ReconcileOutcome，
    檔案還不存在回傳 None（呼叫端決定接下來怎麼做），SSH 連不上回傳
    unreachable。
    """
    try:
        exit_res = await ssh_run(server_name, build_check_exit_code_command(job_id), 15)
    except Exception:  # noqa: BLE001 - 任何 SSH 層例外都視為連不上
        return ReconcileOutcome(status="unreachable")

    output = (exit_res.stdout or "").strip()
    if not output:
        return None

    try:
        code = int(output.splitlines()[0].strip())
    except ValueError:
        code = None
    try:
        log_res = await ssh_run(server_name, build_log_tail_command(job_id), 15)
        log_tail = log_res.stdout or ""
    except Exception:  # noqa: BLE001
        log_tail = None
    if code == 0:
        return ReconcileOutcome(status="done", exit_code=code, log_tail=log_tail)
    return ReconcileOutcome(status="failed", exit_code=code, log_tail=log_tail)


def apply_reconcile_outcome(
    db: Database,
    job: Job,
    outcome: ReconcileOutcome,
    audit_path: str = "audit.jsonl",
    on_job_finished: Optional[Callable[[Job], None]] = None,
) -> None:
    """把 reconcile_job() 的結果落地到 DB，並寫稽核。

    `on_job_finished`（階段 4，PLAN.md E）：狀態落地為 done/failed 之後呼叫
    的**同步**回呼，接收落地後的 `Job`（已經是 done/failed）。這裡刻意設計
    成同步回呼而不是 `await` 一個 coroutine：呼叫端（`app.main.AppState`）
    真正想做的事（拉結果、寄信）可能跑數分鐘，不能卡住排程輪，所以回呼本身
    只負責把工作丟進背景 `asyncio.create_task()`（見
    `AppState.schedule_job_finished_hook()`），`apply_reconcile_outcome()`
    這個函式本身維持同步、不需要在一個執行中的 event loop 才能呼叫（既有
    測試有幾條直接同步呼叫這個函式，不透過 `asyncio.run()`）。requeued 分支
    不觸發（任務還沒真的結束）；`_local` sync 任務由呼叫端
    （`app.scheduler.scheduler_tick`）刻意不傳這個參數，一律不觸發（sync
    任務不寄信，見模組層級說明）。
    """
    if outcome.status in ("unreachable", "running"):
        return
    if outcome.status == "done":
        db.update_job(
            job.id,
            status="done",
            finished_at=now_iso(),
            exit_code=outcome.exit_code,
            log_tail=outcome.log_tail,
        )
        append_audit(
            "done",
            {"job_id": job.id, "exit_code": outcome.exit_code},
            path=audit_path,
        )
        if on_job_finished is not None:
            on_job_finished(db.get_job(job.id))
    elif outcome.status == "failed":
        db.update_job(
            job.id,
            status="failed",
            finished_at=now_iso(),
            exit_code=outcome.exit_code,
            log_tail=outcome.log_tail,
        )
        append_audit(
            "failed",
            {"job_id": job.id, "exit_code": outcome.exit_code},
            path=audit_path,
        )
        if on_job_finished is not None:
            on_job_finished(db.get_job(job.id))
    elif outcome.status == "requeued":
        db.update_job(
            job.id,
            status="queued",
            server=None,
            started_at=None,
        )
        append_audit(
            "requeue",
            {"job_id": job.id, "reason": "tmux session gone, no exit_code (interrupted)"},
            path=audit_path,
        )
