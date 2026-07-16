"""任務佇列：CRUD、狀態機、依賴檢查、哨兵協議 reconcile。

命名避免撞到標準函式庫的 `queue` 模組，因此叫 jobqueue。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from app.audit import AuditActor, SYSTEM_AUDIT_ACTOR, append_audit, now_iso
from app.config import ServerConfig
from app.datasets import LOCAL_SERVER
from app.db import Database, Job
from app.security import is_dangerous

logger = logging.getLogger(__name__)

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


class EngineeringTaskJobCancellationError(ValueError):
    """工程任務擁有的內部 Job 必須由 task-safe action 管理。"""


_ENGINEERING_JOB_DISPLAY = {
    "staging": "Stage approved immutable ProjectVersion bundle",
    "coding": "Codex agent turn in isolated worktree",
    "validation": "Run approved Engineering Task validation",
}

_ENGINEERING_COMMAND_ROLE_BY_JOB_ROLE = {
    "staging": "staging",
    "coding": "agent_turn",
    "validation": "validation",
}


def engineering_job_command_audit_fields(
    command: str, role: Optional[str]
) -> dict[str, str]:
    """Project an owner Job command onto log-safe correlation fields.

    The execution backend still receives the approved immutable command.  The
    append-only audit trail only records a fixed semantic label and SHA-256,
    because the command itself contains private Server A paths and may contain
    future opaque references.
    """

    return {
        "command_display": _ENGINEERING_JOB_DISPLAY.get(
            role, "Run approved Engineering Task job"
        ),
        "command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "command_digest_algorithm": "sha256",
    }


def engineering_job_failure_category(exc: BaseException) -> str:
    """Classify an owner Job failure without serializing exception text."""

    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection_error"
    return "remote_operation_failed"


def _engineering_command_authorizing_approval_matches(
    db: Database, task, command
) -> bool:
    """Verify the approval that authorized this attempt's command journal row.

    Attempt 1 is authorized by the task's own original ``coding_task``
    approval (``task.approval_id``, immutable for the task's lifetime).
    Attempt N>1 (a retry) is authorized by a separate, later
    ``engineering_task_retry`` approval whose id the immutable task row does
    not know about — it must be looked up and independently re-verified:
    approved, the correct kind, and its payload names this exact task and
    attempt number.  Without that payload check, a stale or wrong-attempt
    retry approval id recorded in the journal could authenticate a different
    attempt's command.
    """

    if command.attempt_number == 1:
        return command.approval_id == task.approval_id
    approval = db.get_approval(command.approval_id)
    if (
        approval is None
        or approval.status != "approved"
        or approval.kind != "engineering_task_retry"
    ):
        return False
    payload = approval.payload if isinstance(approval.payload, dict) else {}
    return (
        payload.get("engineering_task_id") == task.id
        and payload.get("attempt_number") == command.attempt_number
    )


def engineering_job_command_contract_matches(db: Database, job: Job) -> bool:
    """Verify that an owner Job still matches its approved command journal.

    Legacy Jobs have no Engineering Task owner and retain their existing
    behavior.  Owner Jobs fail closed unless the durable journal ties the same
    task, attempt, Job, semantic role and approval to the SHA-256 of the exact
    command that would be delivered to the executor.
    """

    if job.engineering_task_id is None:
        return True
    task = db.get_engineering_task(job.engineering_task_id)
    command = db.get_engineering_task_command_by_job_id(job.id)
    expected_command_role = _ENGINEERING_COMMAND_ROLE_BY_JOB_ROLE.get(
        job.engineering_task_role or ""
    )
    if (
        task is None
        or command is None
        or expected_command_role is None
        or job.engineering_attempt_number is None
    ):
        return False
    expected_digest = hashlib.sha256(job.command.encode("utf-8")).hexdigest()
    return (
        command.engineering_task_id == task.id == job.engineering_task_id
        and command.attempt_number == job.engineering_attempt_number
        and command.job_id == job.id
        and command.command_role == expected_command_role
        and _engineering_command_authorizing_approval_matches(db, task, command)
        and command.policy_disposition == "task_approved"
        and command.command_digest == expected_digest
        and db.engineering_task_job_is_approved(job)
    )


def record_engineering_job_execution_contract_mismatch(
    db: Database, job: Job
) -> None:
    """Record one fixed, log-safe refusal without exposing command content."""

    if job.engineering_task_id is None:
        return
    try:
        db.append_engineering_task_event(
            task_id=job.engineering_task_id,
            attempt_number=job.engineering_attempt_number,
            event_key=f"job:{job.id}:execution-contract-mismatch",
            event_type="execution_contract_mismatch",
            phase="execution",
            state="unknown",
            source_kind="system",
            source_id=str(job.id),
            summary=(
                "Approved Engineering Task execution contract mismatch; "
                "executor not contacted"
            ),
            details={"job_id": job.id},
        )
    except Exception:  # noqa: BLE001 - visibility journal cannot reopen execution
        # The execution decision is already fail-closed.  A corrupt/unavailable
        # presentation journal must not crash scheduler or stop handling, and
        # exception text may contain private DB paths or values.
        logger.warning(
            "Engineering Task Job #%s contract mismatch event could not be recorded; "
            "execution remains blocked",
            job.id,
        )


def engineering_coding_job_runner_contract_matches(
    db: Database, job: Job, server_cfg: Optional[ServerConfig]
) -> bool:
    """Fail closed before an owner Coding Job contacts a mutable server name.

    Staging Jobs execute their already-approved immutable transfer command on
    ``_local`` and are outside this lookup.  Coding Jobs, reconciliation and
    stop operations resolve a server name through the current configuration;
    they may do so only while it is the same enabled ``name/host/user/port``
    identity captured by the task approval.

    This accepts any attempt number rather than pinning attempt 1: the
    ``engineering_job_command_contract_matches()`` digest check plus the
    ``expected_job.id == job.id`` lookup below, both keyed by this exact
    ``(task_id, role, attempt_number)``, already fully authenticate that
    ``job`` is the unique approved owner row for its own attempt.
    """

    if job.engineering_task_id is None:
        return True
    if (
        job.engineering_task_role != "coding"
        or job.type != "coding"
        or not engineering_job_command_contract_matches(db, job)
    ):
        return False
    task = db.get_engineering_task(job.engineering_task_id)
    if task is None or server_cfg is None or not server_cfg.enabled:
        return False
    expected_job = db.get_engineering_task_job(
        task.id, "coding", job.engineering_attempt_number or 0
    )
    if (
        expected_job is None
        or expected_job.id != job.id
        or task.runner_server != server_cfg.name
        or job.pin_server != server_cfg.name
        or (job.server is not None and job.server != server_cfg.name)
        or not db.engineering_task_job_is_approved(job)
    ):
        return False
    approved_runner = task.execution_contract.get("runner")
    current_runner = {
        "name": server_cfg.name,
        "host": server_cfg.host,
        "user": server_cfg.user,
        "port": server_cfg.port,
    }
    return isinstance(approved_runner, dict) and approved_runner == current_runner


def engineering_staging_job_contract_matches(db: Database, job: Job) -> bool:
    """Validate a task-owned local staging Job before an approved stop.

    Staging runs on Server A's existing ``_local`` executor.  It does not
    resolve a mutable remote server name, but it must still be the unique
    approved owner row created atomically for this task and attempt (any
    attempt number: see ``engineering_coding_job_runner_contract_matches``
    for why the command-journal digest plus per-attempt job lookup already
    make an explicit attempt-1 pin redundant).
    """

    if (
        job.engineering_task_id is None
        or job.engineering_task_role != "staging"
        or job.type != "sync"
        or job.pin_server != LOCAL_SERVER
        or job.server != LOCAL_SERVER
        or not db.engineering_task_job_is_approved(job)
        or not engineering_job_command_contract_matches(db, job)
    ):
        return False
    expected_job = db.get_engineering_task_job(
        job.engineering_task_id, "staging", job.engineering_attempt_number
    )
    return expected_job is not None and expected_job.id == job.id


def engineering_staging_job_runner_contract_matches(
    db: Database, job: Job, server_cfg: Optional[ServerConfig]
) -> bool:
    """Fail closed before local staging contacts the approved Runner.

    See ``engineering_coding_job_runner_contract_matches`` for why this
    accepts any attempt number rather than pinning attempt 1.
    """

    if (
        job.engineering_task_id is None
        or job.engineering_task_role != "staging"
        or job.type != "sync"
        or job.pin_server != LOCAL_SERVER
        or job.server not in {None, LOCAL_SERVER}
        or not db.engineering_task_job_is_approved(job)
        or not engineering_job_command_contract_matches(db, job)
    ):
        return False
    task = db.get_engineering_task(job.engineering_task_id)
    if task is None or server_cfg is None or not server_cfg.enabled:
        return False
    expected_job = db.get_engineering_task_job(
        task.id, "staging", job.engineering_attempt_number
    )
    approved_runner = task.execution_contract.get("runner")
    current_runner = {
        "name": server_cfg.name,
        "host": server_cfg.host,
        "user": server_cfg.user,
        "port": server_cfg.port,
    }
    return (
        expected_job is not None
        and expected_job.id == job.id
        and task.runner_server == server_cfg.name
        and isinstance(approved_runner, dict)
        and approved_runner == current_runner
    )


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
    audit_actor: Optional[AuditActor] = None,
    engineering_task_id: Optional[str] = None,
    engineering_task_role: Optional[str] = None,
    engineering_attempt_number: Optional[int] = None,
) -> Job:
    """危險指令直接拒絕（寫稽核＋丟例外），安全指令才入列（寫稽核）。

    `source_coding_run_id`（階段 13，PLAN.md N.6）：選填，這個 job 是「用
    某次 Codex coding run 的 result（changes.bundle）當起點」的後續
    train／驗證 job 時才帶，直接透傳給 `db.insert_job()`。
    """
    dangerous, reason = is_dangerous(command)
    if dangerous:
        if engineering_task_id is not None:
            reject_params = {
                **engineering_job_command_audit_fields(
                    command, engineering_task_role
                ),
                "reason": "dangerous_command",
                "engineering_task_id": engineering_task_id,
                "engineering_task_role": engineering_task_role,
                "engineering_attempt_number": engineering_attempt_number,
            }
        else:
            reject_params = {"command": command, "reason": reason}
        append_audit(
            "reject",
            reject_params,
            result="rejected",
            path=audit_path,
            actor=audit_actor,
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
        engineering_task_id=engineering_task_id,
        engineering_task_role=engineering_task_role,
        engineering_attempt_number=engineering_attempt_number,
    )
    job = db.get_job(job_id)
    audit_params = {
        "job_id": job_id,
        "project": project,
        "priority": priority,
        "pin_server": pin_server,
        "require_tag": require_tag,
        "depends_on": depends_on or [],
    }
    if engineering_task_id is not None:
        audit_params.update(
            {
                **engineering_job_command_audit_fields(
                    command, engineering_task_role
                ),
                "engineering_task_id": engineering_task_id,
                "engineering_task_role": engineering_task_role,
                "engineering_attempt_number": engineering_attempt_number,
            }
        )
    else:
        audit_params["command"] = command
    append_audit(
        "enqueue",
        audit_params,
        path=audit_path,
        actor=audit_actor,
    )
    return job


def cancel_job(
    db: Database,
    job_id: int,
    audit_path: str = "audit.jsonl",
    audit_actor: Optional[AuditActor] = None,
) -> bool:
    """取消 legacy queued Job；owner Job 必須走 task-safe action。"""
    job = db.get_job(job_id)
    if job is None or job.status != "queued":
        return False
    if job.engineering_task_id is not None:
        raise EngineeringTaskJobCancellationError(
            "AI 工程任務的內部 Job 不能透過通用取消；"
            "請使用工程任務的安全取消流程"
        )
    db.update_job(job_id, status=CANCELLED, finished_at=now_iso())
    if job.engineering_validation_request_id is not None:
        db.refresh_engineering_validation_request_status(
            job.engineering_validation_request_id
        )
    append_audit("cancel", {"job_id": job_id}, path=audit_path, actor=audit_actor)
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
            if job.engineering_task_id is not None:
                db.refresh_engineering_task_status_from_jobs(
                    job.engineering_task_id
                )
            if job.engineering_validation_request_id is not None:
                db.refresh_engineering_validation_request_status(
                    job.engineering_validation_request_id
                )
            append_audit(
                "blocked",
                {"job_id": job.id, "depends_on": job.depends_on, "dep_statuses": statuses},
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
            blocked_ids.append(job.id)
    return blocked_ids


def list_dispatchable_jobs(db: Database) -> list[Job]:
    """回傳目前「依賴已完成」的 queued 任務（尚未看 pin_server/require_tag/優先權）。"""
    candidates: list[Job] = []
    for job in db.list_jobs(status="queued"):
        # Defense in depth for immutable Engineering Tasks: their run and Jobs
        # are finalized in the same transaction as approval.  If a database
        # imported from an interrupted pre-transaction build contains owner
        # rows under a pending/rejected approval, never dispatch them.
        if not db.engineering_task_job_is_approved(job):
            continue
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
    # A line limit alone is not a byte limit: one attacker-controlled final
    # line could otherwise make AsyncSSH buffer an arbitrarily large value
    # before local redaction/truncation runs.  Keep the local 64 KiB sanitizer
    # as defense in depth and cap bytes on the remote side as well.
    return f"tail -n {lines} {paths['log']} 2>/dev/null | tail -c 65536"


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
    persisted_log_tail = safe_persisted_engineering_log_tail(job, outcome.log_tail)
    if outcome.status == "done":
        db.update_job(
            job.id,
            status="done",
            finished_at=now_iso(),
            exit_code=outcome.exit_code,
            log_tail=persisted_log_tail,
        )
        append_audit(
            "done",
            {"job_id": job.id, "exit_code": outcome.exit_code},
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
        if on_job_finished is not None:
            on_job_finished(db.get_job(job.id))
    elif outcome.status == "failed":
        db.update_job(
            job.id,
            status="failed",
            finished_at=now_iso(),
            exit_code=outcome.exit_code,
            log_tail=persisted_log_tail,
        )
        append_audit(
            "failed",
            {"job_id": job.id, "exit_code": outcome.exit_code},
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
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
            actor=SYSTEM_AUDIT_ACTOR,
        )
    if job.engineering_task_id is not None:
        db.refresh_engineering_task_status_from_jobs(job.engineering_task_id)
    if job.engineering_validation_request_id is not None:
        db.refresh_engineering_validation_request_status(
            job.engineering_validation_request_id
        )


def safe_persisted_engineering_log_tail(
    job: Job, raw_log_tail: Optional[str]
) -> Optional[str]:
    """Sanitize protected task logs before they enter SQLite or backups.

    Ordinary non-coding legacy Jobs retain their historical raw-log persistence
    contract.  Every coding Job (including the Slice-1 compatibility wizard),
    Engineering Task Job, and linked Worker-validation Job contains untrusted
    agent/code output: common credentials and private paths are redacted,
    control obfuscation is normalized, and a private-key marker withholds the
    entire tail.  API/UI projection remains a second defense, not the first one.
    """

    if (
        job.type != "coding"
        and job.engineering_task_id is None
        and job.engineering_validation_request_id is None
    ):
        return raw_log_tail
    if raw_log_tail is None:
        return None

    # Local import keeps the ordinary queue/state module independent at import
    # time while reusing the single reviewed Engineering Task sanitizer.
    from app.engineering_tasks import redact_engineering_text

    preview = redact_engineering_text(raw_log_tail, max_chars=65_536)
    if preview.get("withheld"):
        return None
    content = preview.get("content")
    return content if isinstance(content, str) else None
