"""排程器：每 10 秒一輪。純函式 pick_job() + 派發/完成偵測的迴圈邏輯。

排程順序（PLAN.md B，階段 3 修訂資料引力語意）：
    pin_server 相符 → require_tag 相符 → 依賴全 done（由呼叫端先過濾）→
    **has_dataset 資格過濾**（見下）→ priority（normal > low）→
    同級 FIFO（created_at 早的優先）

資料引力（階段 3，Fable 覆核修正 1）：`has_dataset` 是**硬性資格過濾**，
不是排序偏好。`has_dataset(job) == False` 的任務直接不列入候選——如果
只是排後面（原本的 sort key gravity），自動模式下一個需要資料集 D 的
訓練任務，在「D 沒快取在任何機器」時仍然會被派到某台沒資料的機器，訓練
跑起來才因為讀不到 `datasets/D/v` 靜默失敗，白白佔用機器一輪。改成資格
過濾之後，需要資料集的訓練任務只會被派到「已快取該資料集」的機器；
資料集哪都沒有時任務留在 queued，不會被誤派，等使用者改用「指定機器」
派工（核准卡片會附帶 sync 計畫）或者 sync 完成、`dataset_cache` 登記後，
下一輪就自然變得可派（見 `app/approvals.py` 的警告機制與
`app/datasets.finalize_sync_job()`）。`has_dataset=None`（呼叫端不傳，
或任務沒有資料集要求）視為「沒有這個限制」，維持原本 pin_server/
require_tag/priority/FIFO 的排序。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from app.audit import SYSTEM_AUDIT_ACTOR, append_audit, now_iso
from app.datasets import (
    LOCAL_SERVER,
    LOCAL_SYNC_CONCURRENCY,
    check_disk_space,
    finalize_sync_job,
    make_has_dataset,
)
from app.db import Database, Job
from app.engineering_validation import (
    engineering_validation_job_audit_fields,
    engineering_validation_job_contract_failure,
    record_engineering_validation_contract_refusal,
)
from app.jobqueue import (
    ReconcileOutcome,
    apply_reconcile_outcome,
    build_launch_command,
    build_log_size_command,
    build_mkdir_command,
    engineering_coding_job_runner_contract_matches,
    engineering_job_command_contract_matches,
    engineering_job_command_audit_fields,
    engineering_job_failure_category,
    engineering_staging_job_runner_contract_matches,
    list_dispatchable_jobs,
    reconcile_job,
    record_engineering_job_execution_contract_mismatch,
    refresh_blocked_jobs,
)
from app.monitor import ServerState, is_idle
from app.stall import parse_log_size, update_stall_state

logger = logging.getLogger(__name__)

#: 卡死偵測預設門檻（分鐘），實際值由 `app.config.AppConfig.stall_minutes`
#: 提供（.env 的 STALL_MINUTES 可調），這裡只是 `scheduler_tick()` 參數的
#: 預設值，讓沒有傳這個參數的既有呼叫端／測試行為不變。
DEFAULT_STALL_MINUTES = 30

_PRIORITY_RANK = {"normal": 0, "low": 1}


def _record_engineering_runner_contract_mismatch(db: Database, job: Job) -> None:
    if job.engineering_task_id is None:
        return
    try:
        db.append_engineering_task_event(
            task_id=job.engineering_task_id,
            attempt_number=job.engineering_attempt_number,
            event_key=f"job:{job.id}:runner-contract-mismatch",
            event_type="runner_contract_mismatch",
            phase="execution",
            state="disconnected",
            source_kind="reconciler",
            source_id=str(job.id),
            summary="Coding Runner 設定已與核准的 execution contract 不同；未連線",
            details={"job_id": job.id},
        )
    except Exception:  # noqa: BLE001 - visibility journal cannot reopen execution
        logger.warning(
            "Engineering Task Job #%s Runner mismatch event could not be recorded; "
            "execution remains blocked",
            job.id,
        )


def _engineering_command_contract_allows_execution(db: Database, job: Job) -> bool:
    if engineering_job_command_contract_matches(db, job):
        return True
    record_engineering_job_execution_contract_mismatch(db, job)
    return False


def _validation_contract_allows_execution(
    db: Database,
    job: Job,
    server_configs: dict,
    local_home_dir: Optional[str],
) -> bool:
    if job.engineering_validation_request_id is None:
        return True
    failure = engineering_validation_job_contract_failure(
        db,
        job,
        server_configs,
        local_home_dir=local_home_dir,
    )
    if failure is None:
        return True
    record_engineering_validation_contract_refusal(db, job, failure)
    return False


def _refresh_job_owner_status(db: Database, job: Job) -> None:
    if job.engineering_task_id is not None:
        db.refresh_engineering_task_status_from_jobs(job.engineering_task_id)
    if job.engineering_validation_request_id is not None:
        db.refresh_engineering_validation_request_status(
            job.engineering_validation_request_id
        )


def _dispatch_audit_params(job: Job, server_name: str) -> dict:
    params = {"job_id": job.id, "server": server_name}
    if job.engineering_validation_request_id is not None:
        params.update(engineering_validation_job_audit_fields(job))
        return params
    if job.engineering_task_id is None:
        params["command"] = job.command
        return params
    params.update(
        {
            **engineering_job_command_audit_fields(
                job.command, job.engineering_task_role
            ),
            "engineering_task_id": job.engineering_task_id,
            "engineering_task_role": job.engineering_task_role,
            "engineering_attempt_number": job.engineering_attempt_number,
        }
    )
    return params


def _dispatch_failure_audit_params(
    job: Job, server_name: str, exc: BaseException
) -> dict:
    if (
        job.engineering_task_id is None
        and job.engineering_validation_request_id is None
    ):
        return {"job_id": job.id, "server": server_name, "error": str(exc)}
    return {
        **_dispatch_audit_params(job, server_name),
        "failure_category": engineering_job_failure_category(exc),
    }


def pick_job(
    server_name: str,
    server_tags: list[str],
    candidates: list[Job],
    has_dataset: Optional[Callable[[Job], bool]] = None,
    *,
    codex_runner_server: Optional[str] = None,
    codex_runner_reserve: bool = True,
    codex_max_concurrency: int = 1,
    running_coding_count: int = 0,
) -> Optional[Job]:
    """純函式：從候選任務（已假設依賴已完成）中，替某台空閒機器挑一個任務。

    - pin_server：非 None 時必須等於 server_name 才符合（`type=="coding"`
      的任務**額外**先過 Runner 身分檢查，見下方 Runner 規則第 1 條——
      pin_server 本身的比對規則不變，只是多了一道更早短路的關卡）。
    - require_tag：非 None 時必須在 server_tags 內才符合。
    - has_dataset：**資格過濾**，不是排序偏好（見模組 docstring）。非
      None 且 `has_dataset(job)` 回傳 False 時，這個任務直接不列入
      `eligible`（不會被這台機器挑走，即使沒有其他候選）。None（呼叫端
      不傳）或任務本身沒有資料集要求時視為沒有這個限制。
    - priority：normal 優先於 low。
    - 同級 FIFO：created_at 早的優先，created_at 相同再比 id（穩定排序）。

    階段 13（PLAN.md N.8，Codex Runner）：以下四個 keyword-only 參數預設值
    向下相容既有呼叫端（不傳＝行為完全不變）。

    1. `type=="coding"` 的任務只有 `server_name == codex_runner_server` 才
       可能合格；`codex_runner_server is None`（Codex 功能停用）時 coding
       任務永遠不合格，保持 queued。**這條檢查優先於 pin_server 檢查**：
       在比對 pin_server 之前就先短路——`server_name != codex_runner_server`
       時直接不合格，**pin_server 比對本身仍然照舊套用**（非 None 時必須
       等於 server_name）。合起來的效果：v1 遺留、pin 到別台（非 Runner）
       機器的 coding job 永遠不會被派到任何機器——原本 pin 的那台機器被
       規則 1 擋下（它不是 Runner），Runner 則被 pin_server 比對擋下
       （pin 值跟 Runner 名字對不上）；只有「未設定 pin_server」或
       「pin_server 剛好就是 Runner」的 coding job 才可能在 Runner 上合格
       ——v2 的 approve() 一律把 coding job pin 到 Runner（N.8 規則 1），
       這是唯一預期會發生的組合，其餘視為 v1 遺留、刻意留在 queued，
       批次 2 才處理遷移相容。
    2. coding 任務另需 `running_coding_count < codex_max_concurrency` 才
       合格（Runner 上同時執行的 coding job 數上限；ChatGPT 登入模式在
       `app.config.apply_codex_config_rules()` 已經強制 `codex_max_concurrency
       == 1`）。
    3. `server_name == codex_runner_server` 且 `codex_runner_reserve=True`
       時：非 coding 的一般任務必須 `pin_server == server_name`（使用者
       明確 pin 到 Runner）才合格，否則這台機器這輪不派給它。
    4. `server_name == codex_runner_server` 且 `codex_runner_reserve=False`
       時：只要 `running_coding_count > 0` 或候選裡還有 `type=="coding"`
       的任務（不論合不合格），這輪就不派一般任務給 Runner（coding
       優先，讓它下一輪有機會被挑到）。
    - 排序偏好：合格候選裡 `type=="coding"` 的任務排最前（同級 FIFO／
      priority 排序維持不變）——只有在 Runner 上才會有合格的 coding
      候選，其他機器這個排序鍵不影響任何行為。
    """
    has_coding_candidate = any(j.type == "coding" for j in candidates)

    def eligible_check(j: Job) -> bool:
        if j.type == "coding":
            if codex_runner_server is None or server_name != codex_runner_server:
                return False
            if j.pin_server is not None and j.pin_server != server_name:
                return False
            if running_coding_count >= codex_max_concurrency:
                return False
            return True

        if (j.pin_server is not None and j.pin_server != server_name):
            return False
        if j.require_tag is not None and j.require_tag not in server_tags:
            return False
        if has_dataset is not None and not has_dataset(j):
            return False

        if server_name == codex_runner_server:
            if codex_runner_reserve:
                if j.pin_server != server_name:
                    return False
            else:
                if running_coding_count > 0 or has_coding_candidate:
                    return False
        return True

    eligible = [j for j in candidates if eligible_check(j)]
    if not eligible:
        return None

    def sort_key(j: Job):
        is_coding_rank = 0 if j.type == "coding" else 1
        return (is_coding_rank, _PRIORITY_RANK.get(j.priority, 0), j.created_at, j.id)

    eligible.sort(key=sort_key)
    return eligible[0]


async def dispatch_job(ssh_run, ssh_write_file, server_name: str, job: Job) -> None:
    """依哨兵檔案協議把任務派上工作機：mkdir、寫 cmd.sh/run.sh、tmux 起 session。"""
    from app.jobqueue import build_dispatch_paths, build_run_sh_content

    paths = build_dispatch_paths(job.id)
    await ssh_run(server_name, build_mkdir_command(job.id), 15)
    await ssh_write_file(server_name, paths["cmd_sh"], job.command)
    await ssh_write_file(server_name, paths["run_sh"], build_run_sh_content(job.id))
    await ssh_run(server_name, build_launch_command(job.id), 15)


async def scheduler_tick(
    db: Database,
    server_states: dict[str, ServerState],
    server_configs: dict,
    ssh_run,
    ssh_write_file,
    audit_path: str = "audit.jsonl",
    on_job_finished: Optional[Callable[[Job], None]] = None,
    on_stall_detected: Optional[Callable[[Job], None]] = None,
    stall_minutes: int = DEFAULT_STALL_MINUTES,
    codex_runner_server: Optional[str] = None,
    codex_runner_reserve: bool = True,
    codex_max_concurrency: int = 1,
    local_home_dir: Optional[str] = None,
) -> None:
    """跑一輪排程：先 reconcile 所有 running 任務，再處理 blocked，再派工。

    - `server_states`：server name -> monitor.ServerState（monitor loop 維護）。
    - `server_configs`：server name -> app.config.ServerConfig。
    - `ssh_run` / `ssh_write_file`：sshpool 提供的 async callable，呼叫端
      （`app.main.AppState`）已經把 `server_name == "_local"` 的呼叫路由到
      `app.localrun`，這裡完全不用區分本地／SSH，只要照常呼叫即可。
    - `on_job_finished`（階段 4）：任務在真實伺服器上 reconcile 成
      done/failed 時的同步回呼（見 `app.jobqueue.apply_reconcile_outcome`
      的說明）。**刻意只在步驟 1（真實伺服器）傳給
      `apply_reconcile_outcome()`，步驟 1b（`_local` sync 任務）完全不傳**
      ——sync 任務完成不寄信、不拉結果（結果回收與寄信是給訓練/一般任務
      的；sync 任務的完成情況已經有 `sync_verified`/`sync_verify_failed`
      稽核）。
    - `on_stall_detected`／`stall_minutes`（階段 4，卡死偵測）：見步驟 1c
      與 `app.stall` 模組。
    - `codex_runner_server`／`codex_runner_reserve`／`codex_max_concurrency`
      （階段 13，PLAN.md N.8）：直接透傳給每一次 `pick_job()` 呼叫；預設值
      向下相容既有呼叫端（不傳＝行為不變，coding 任務永遠不合格）。
      `running_coding_count`（Runner 上目前 running 的 coding job 數）由
      這裡自己從 `db.list_jobs(status="running")` 統計，不開放外部傳入
      ——呼叫端只需要提供 `.env` 讀出的三個設定值。

    階段 3：`_local`（sync 任務的執行目標）視為永遠在線、可並行
    （`LOCAL_SYNC_CONCURRENCY` 個同時），跟一般伺服器分開處理（見步驟 1b、4）。
    """
    # 1) reconcile 所有跑在真實伺服器上的 running 任務（離線機跳過）
    for job in db.list_jobs(status="running"):
        if not job.server or job.server == LOCAL_SERVER:
            continue
        if not _validation_contract_allows_execution(
            db, job, server_configs, local_home_dir
        ):
            continue
        if job.engineering_task_id is not None and not (
            _engineering_command_contract_allows_execution(db, job)
        ):
            continue
        if job.engineering_task_id is not None and not (
            engineering_coding_job_runner_contract_matches(
                db, job, server_configs.get(job.server)
            )
        ):
            _record_engineering_runner_contract_mismatch(db, job)
            continue
        state = server_states.get(job.server)
        if state is None or not state.online:
            # 目標機離線：保持 running 不動，等回報上線後下一輪再判
            continue
        outcome: ReconcileOutcome = await reconcile_job(ssh_run, job.server, job.id)
        apply_reconcile_outcome(
            db, job, outcome, audit_path=audit_path, on_job_finished=on_job_finished
        )

    # 1b) reconcile 跑在 `_local` 的 running 任務（sync 任務）。`_local` 永遠
    # 視為在線，不用查 server_states。sync 任務的 cmd.sh 只做「mkdir + rsync」，
    # rsync exit code 0 不代表資料真的完整——還要驗證 manifest（檔案數／總
    # 大小）才能登記進 dataset_cache，所以 done 分支交給
    # `finalize_sync_job()` 接手，不能直接套用 `apply_reconcile_outcome()`。
    # 注意：這裡刻意不傳 `on_job_finished`（sync 任務不觸發任務結束 hook）。
    for job in db.list_jobs(status="running"):
        if job.server != LOCAL_SERVER:
            continue
        if not _validation_contract_allows_execution(
            db, job, server_configs, local_home_dir
        ):
            continue
        if job.engineering_task_id is not None and not (
            _engineering_command_contract_allows_execution(db, job)
        ):
            continue
        outcome = await reconcile_job(ssh_run, LOCAL_SERVER, job.id)
        if job.type == "sync" and outcome.status == "done":
            await finalize_sync_job(
                db, job, outcome.exit_code, outcome.log_tail, ssh_run, audit_path=audit_path
            )
            _refresh_job_owner_status(db, job)
        else:
            apply_reconcile_outcome(db, job, outcome, audit_path=audit_path)

    # 1c) 卡死偵測（階段 4）：對 reconcile 之後「仍然是 running」的真實機器
    # 任務，查一次 job.log 大小。只是旗標，不改 job.status（見 app/stall.py）。
    await _check_stalled_jobs(
        db,
        server_states,
        server_configs,
        ssh_run,
        audit_path,
        stall_minutes,
        on_stall_detected,
        local_home_dir,
    )

    # 2) 依賴中有 failed/blocked 的 queued 任務標記為 blocked
    refresh_blocked_jobs(db, audit_path=audit_path)

    # 3) 對每台空閒機器挑任務並派發（資料引力：優先挑已快取所需資料集的任務）
    running_jobs = db.list_jobs(status="running")
    running_servers = {j.server for j in running_jobs if j.server}
    #: 階段 13（PLAN.md N.8 規則 2）：Runner 上目前 running 的 coding job
    #: 數——不分機器統計都一樣，因為 coding 只可能 running 在 Runner 上
    #: （這條規則從階段 13 才開始生效，本輪之前派發的 running coding job
    #: 一定已經在 Runner，不會有別台機器的 coding job 混進來計數）。
    running_coding_count = sum(1 for j in running_jobs if j.type == "coding")
    candidates = list_dispatchable_jobs(db)

    for server_name, state in server_states.items():
        server_cfg = server_configs.get(server_name)
        if server_cfg is None:
            continue
        if not server_cfg.enabled:
            # enabled=false：monitor 仍探測狀態（總覽頁看得到），但排程器
            # 不派工給這台機器（PLAN.md I.8）。
            continue
        idle = is_idle(
            online=state.online,
            has_running_job=server_name in running_servers,
            is_gpu_server=server_cfg.gpu,
            gpu_util_max=state.gpu_util_max,
            idle_gpu_util=server_cfg.idle_gpu_util,
            load1=state.load1,
            idle_load=server_cfg.idle_load,
        )
        if not idle:
            continue

        # A fail-closed owner Job must not starve unrelated valid work forever.
        # Each rejected candidate is removed from this tick's finite snapshot,
        # then normal priority/FIFO selection is repeated for the same server.
        # No DB state is changed and every loop iteration strictly shrinks the
        # candidate list, so this cannot become a busy loop.
        while True:
            job = pick_job(
                server_name,
                server_cfg.tags,
                candidates,
                has_dataset=make_has_dataset(db, server_name),
                codex_runner_server=codex_runner_server,
                codex_runner_reserve=codex_runner_reserve,
                codex_max_concurrency=codex_max_concurrency,
                running_coding_count=running_coding_count,
            )
            if job is None:
                break
            if not _validation_contract_allows_execution(
                db, job, server_configs, local_home_dir
            ):
                candidates = [
                    candidate for candidate in candidates if candidate.id != job.id
                ]
                continue
            if job.engineering_task_id is not None and not (
                _engineering_command_contract_allows_execution(db, job)
            ):
                candidates = [
                    candidate for candidate in candidates if candidate.id != job.id
                ]
                continue
            if job.engineering_task_id is not None and not (
                engineering_coding_job_runner_contract_matches(db, job, server_cfg)
            ):
                _record_engineering_runner_contract_mismatch(db, job)
                candidates = [
                    candidate for candidate in candidates if candidate.id != job.id
                ]
                continue
            break
        if job is None:
            continue

        # 先標 running 再派發（不是反過來）：如果順序倒過來（先 SSH 派發、
        # 成功後才更新 DB），服務在 tmux 起了 session 之後、DB 更新之前
        # 崩潰的話，這個任務重啟後仍是 queued，會被排程器再派一次
        # ——可能派去另一台機，同一個訓練就跑了兩份。
        #
        # 反過來（先標 running）的話，同樣的崩潰窗口留下的是「running 但
        # 其實 SSH 派發沒完成」的任務，下一輪 reconcile 會用哨兵協議正確
        # 處理：exit_code 沒有、tmux session 也沒有 → requeued，不會雙重
        # 派發。
        db.update_job(job.id, status="running", server=server_name, started_at=now_iso())
        _refresh_job_owner_status(db, job)
        try:
            await dispatch_job(ssh_run, ssh_write_file, server_name, job)
        except Exception as exc:  # noqa: BLE001
            if (
                job.engineering_task_id is None
                and job.engineering_validation_request_id is None
            ):
                logger.warning("派發任務 %s 到 %s 失敗: %s", job.id, server_name, exc)
            else:
                logger.warning(
                    "派發 Engineering Task Job %s 到 %s 失敗（%s）",
                    job.id,
                    server_name,
                    engineering_job_failure_category(exc),
                )
            # revert：派發失敗（例如 SSH 一開始就連不上），這個任務其實
            # 根本沒有真的上工作機跑，退回 queued 讓下一輪重新挑機。
            db.update_job(job.id, status="queued", server=None, started_at=None)
            _refresh_job_owner_status(db, job)
            append_audit(
                "dispatch_failed",
                _dispatch_failure_audit_params(job, server_name, exc),
                result="failed",
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
            continue

        append_audit(
            "dispatch",
            _dispatch_audit_params(job, server_name),
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
        # 這台機這輪已經派了一個任務，從候選與可再派名單移除
        candidates = [c for c in candidates if c.id != job.id]
        running_servers.add(server_name)

    # 4) 派發 sync 任務到 `_local`（永遠在線，上限 LOCAL_SYNC_CONCURRENCY 個並行）
    await _dispatch_local_sync_jobs(
        db,
        candidates,
        server_configs,
        ssh_run,
        ssh_write_file,
        audit_path,
        local_home_dir,
    )


async def _dispatch_local_sync_jobs(
    db: Database,
    candidates: list[Job],
    server_configs: dict,
    ssh_run,
    ssh_write_file,
    audit_path: str,
    local_home_dir: Optional[str],
) -> None:
    """把 pin 給 `_local` 的 sync 任務派發出去，上限
    `LOCAL_SYNC_CONCURRENCY` 個同時執行。派發前先對 `job.target_server` 做
    df 剩餘空間檢查（原規格 5.5）：空間不足直接判該任務 failed（不真的跑
    rsync），依賴這個 sync 任務的訓練任務會在下一輪 `refresh_blocked_jobs()`
    被標記 blocked。
    """
    local_running = sum(1 for j in db.list_jobs(status="running") if j.server == LOCAL_SERVER)
    local_candidates = [
        c for c in candidates if c.pin_server == LOCAL_SERVER and c.type == "sync"
    ]
    local_candidates.sort(
        key=lambda j: (_PRIORITY_RANK.get(j.priority, 0), j.created_at, j.id)
    )

    for job in local_candidates:
        if local_running >= LOCAL_SYNC_CONCURRENCY:
            break

        if not _validation_contract_allows_execution(
            db, job, server_configs, local_home_dir
        ):
            continue

        if job.engineering_task_id is not None:
            if not _engineering_command_contract_allows_execution(db, job):
                continue
            task = db.get_engineering_task(job.engineering_task_id)
            runner_cfg = (
                server_configs.get(task.runner_server) if task is not None else None
            )
            if not engineering_staging_job_runner_contract_matches(
                db, job, runner_cfg
            ):
                _record_engineering_runner_contract_mismatch(db, job)
                continue

        if job.dataset_name and job.target_server:
            dataset = db.get_dataset(job.dataset_name, job.dataset_version)
            if dataset is not None:
                ok, reason, _avail = await check_disk_space(
                    ssh_run, job.target_server, dataset.size_bytes
                )
                if not ok:
                    db.update_job(
                        job.id, status="failed", finished_at=now_iso(), log_tail=reason
                    )
                    _refresh_job_owner_status(db, job)
                    append_audit(
                        "failed",
                        {"job_id": job.id, "reason": reason},
                        result="failed",
                        path=audit_path,
                        actor=SYSTEM_AUDIT_ACTOR,
                    )
                    continue

        db.update_job(job.id, status="running", server=LOCAL_SERVER, started_at=now_iso())
        _refresh_job_owner_status(db, job)
        try:
            await dispatch_job(ssh_run, ssh_write_file, LOCAL_SERVER, job)
        except Exception as exc:  # noqa: BLE001
            if (
                job.engineering_task_id is None
                and job.engineering_validation_request_id is None
            ):
                logger.warning("派發 sync 任務 %s 到 %s 失敗: %s", job.id, LOCAL_SERVER, exc)
            else:
                logger.warning(
                    "派發 Engineering Task staging Job %s 失敗（%s）",
                    job.id,
                    engineering_job_failure_category(exc),
                )
            db.update_job(job.id, status="queued", server=None, started_at=None)
            _refresh_job_owner_status(db, job)
            append_audit(
                "dispatch_failed",
                _dispatch_failure_audit_params(job, LOCAL_SERVER, exc),
                result="failed",
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
            continue

        append_audit(
            "dispatch",
            _dispatch_audit_params(job, LOCAL_SERVER),
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
        local_running += 1


async def _check_stalled_jobs(
    db: Database,
    server_states: dict[str, ServerState],
    server_configs: dict,
    ssh_run,
    audit_path: str,
    stall_minutes: int,
    on_stall_detected: Optional[Callable[[Job], None]],
    local_home_dir: Optional[str],
) -> None:
    """卡死偵測（PLAN.md E）：對「reconcile 之後仍然是 running」的真實機器
    任務，查一次 `job.log` 目前大小，跟上一輪記錄比對（`app.stall`）。

    - 只是旗標（`stalled_suspect`），不改 `job.status`——卡住有可能只是
      訓練跑得比較慢，系統不該自作主張判定失敗。
    - `stall_notified` 用來讓提醒信只寄一次（去重）：進入卡住狀態時如果
      還沒通知過，才呼叫 `on_stall_detected()`（同任務結束 hook 的背景
      task 追蹤機制，見 `app.main.AppState`）並標記已通知；`log_size` 之後
      又增長、`stalled_suspect` 清掉時，`stall_notified` 刻意不清，避免
      同一次卡住反覆寄信。
    - SSH 連不上、`stat` 讀不到檔案大小（`current_size is None`，例如任務
      剛派發還沒開始寫 log）都直接跳過這個任務本輪的判定，完全不更新任何
      欄位（不清也不設 `stalled_suspect`），等下一輪有資訊了再判斷。
    - `_local`／離線機不檢查（sync 任務沒有這個 hook，離線機的 running 任務
      本來就跳過 reconcile，見步驟 1 的說明）。
    """
    now = datetime.now(timezone.utc)
    for job in db.list_jobs(status="running"):
        if not job.server or job.server == LOCAL_SERVER:
            continue
        if not _validation_contract_allows_execution(
            db, job, server_configs, local_home_dir
        ):
            continue
        if job.engineering_task_id is not None and not (
            _engineering_command_contract_allows_execution(db, job)
        ):
            continue
        if job.engineering_task_id is not None and not (
            engineering_coding_job_runner_contract_matches(
                db, job, server_configs.get(job.server)
            )
        ):
            _record_engineering_runner_contract_mismatch(db, job)
            continue
        state = server_states.get(job.server)
        if state is None or not state.online:
            continue

        try:
            result = await ssh_run(job.server, build_log_size_command(job.id), 15)
        except Exception:  # noqa: BLE001 - SSH 連不上，本輪跳過，不做任何判定
            continue

        current_size = parse_log_size(result.stdout or "")
        if current_size is None:
            continue

        new_size, new_changed_at, is_stalled = update_stall_state(
            job.log_size, job.log_size_changed_at, current_size, now, stall_minutes
        )

        updates: dict = {"log_size": new_size, "log_size_changed_at": new_changed_at}
        was_suspect = bool(job.stalled_suspect)
        if is_stalled:
            updates["stalled_suspect"] = 1
            if not was_suspect:
                append_audit(
                    "stall_suspect",
                    {"job_id": job.id, "server": job.server},
                    result="warning",
                    path=audit_path,
                    actor=SYSTEM_AUDIT_ACTOR,
                )
            if not job.stall_notified:
                updates["stall_notified"] = 1
                if on_stall_detected is not None:
                    on_stall_detected(db.get_job(job.id))
        elif was_suspect:
            updates["stalled_suspect"] = 0

        db.update_job(job.id, **updates)
