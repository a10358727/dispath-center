"""FastAPI 入口：lifespan 啟動 monitor 與 scheduler 迴圈；綁 127.0.0.1。

階段 1 範圍內的最小 API（PLAN.md B）：
    GET  /servers
    GET  /jobs, GET /jobs/{id}
    POST /jobs                （階段 2 起改走核准流，見下）
    POST /jobs/{id}/cancel     （僅限 queued）

階段 2 新增（PLAN.md C）：核准流＋網頁前端＋共享 token 認證。
**行為變更**：POST /jobs 不再直接入列，改為建立 kind=enqueue 的
approval（與 POST /dispatch 同義，保留兩個路徑），核准後才真正入列。
    GET  /approvals?status=&kind=
    POST /approve/{id}
    POST /reject/{id}
    POST /dispatch             （= POST /jobs）
    POST /jobs/{id}/stop       （建立 kind=stop 的 approval）
    GET  /jobs/{id}/log        （running 即時 SSH 抓尾；否則回存好的 log_tail）
    GET  /events               （audit.jsonl 尾 100 行，新到舊）
    GET  /                     （靜態頁 static/index.html）

階段 5 新增（PLAN.md F）：LLM 選配層（沒有 ANTHROPIC_API_KEY 時，以下入口
全部降級為規則式/不可用，前四階段任何行為不受影響，見 app/llm.py）。
    WS   /ws                   （聊天；AUTH_TOKEN 有設時，連線後第一則訊息
                                 必須是 {"type":"auth","token":"..."}）
    POST /jobs/{id}/diagnose   （failed 任務的失敗診斷，只建議不執行）
    GET  /audit                （= GET /events，別名，對齊規格 §7 API 列表）

階段 7 新增（PLAN.md H）：本地 vLLM Agent Layer。vLLM 未設定
（VLLM_BASE_URL/VLLM_MODEL）時以下入口明確降級，前六階段任何行為不受影響。
    POST /agent/chat           （單次 agent 對話，vLLM 未設定 → 503）
    GET  /agent/tools          （工具白名單清單，由 app/agent_tools.py 產生）
    POST /agent/cmd            （enum 分派到唯讀工具，不經 LLM，非法值 400）

階段 8 第一批新增（PLAN.md I 節）：Project Inventory（唯讀掃描 → 候選專案
→ 人工核准後匯入成正式專案）。
    POST /inventory/scan                             （建 inventory_scan approval；
                                                        見下方第二批升級說明）
    GET  /inventory/candidates?server=&status=&q=
    GET  /inventory/candidates/{id}
    POST /inventory/candidates/{id}/import-request   （建 import_project approval）
    POST /inventory/candidates/{id}/ignore-request   （建 ignore_project_candidate approval）
    GET  /projects/{name}/instances

階段 8 第二批新增（PLAN.md I.12「第二批追加項」＋ I.7 Server config）：
`ServerConfig` 有了 `project_roots` 之後，`POST /inventory/scan` 升級為
`project_roots` 選填（沒帶時從該機器設定自動代入）、`server="all"` 真的
支援批次掃描（對每一台 enabled 機器各自建立一筆 approval，回傳
`{"approvals": [...]}`；單一 server 仍回傳單一 approval dict）。另新增
Web Server Management（PLAN.md I.4/I.7）：
    GET  /server-config                              （key 只顯示路徑，不回傳內容）
    GET  /server-config/{name}
    POST /server-config/test-ssh                      （唯讀直接執行，寫稽核 server_test_ssh）
    POST /server-config/add-request                   （建 server_add approval）
    POST /server-config/update-request                 （建 server_update approval）
    POST /server-config/disable-request                （建 server_disable approval）
    POST /server-config/delete-request                 （建 server_delete approval；
                                                         第一版核准後只設 enabled=false）
    POST /server-config/reload                         （呼叫 reload_server_config_if_supported()）

階段 11 新增（PLAN.md L 節）：`GET /jobs` 補 `project` 選填篩選（可與
`status` 並用）；新增專案執行近況聚合端點（唯讀直接執行，不走核准，先例
是 `GET /jobs/{id}/log` 的即時 SSH tail）：
    GET  /projects/{name}/activity   （專案基本資料＋各機 project_instances／
                                       GPU／磁碟現況＋最近 10 筆 jobs 摘要與
                                       最新一筆 log_tail＋對每個在線 instance
                                       的唯讀 SSH 探測，見 app/activity.py；
                                       專案不存在 404；沒有登記機器時
                                       `activity` 欄位回明確訊息，不是 404）

階段 12 新增（PLAN.md M 節）：AI 改碼層次一——讀檔＋diff 核准卡（使用者
明確要求越過原規格「只建議不改碼」的紅線，受控方式＝人工逐一核准 diff、
改動只在新 git branch 上、永不 push，見 README「AI 改碼層次一」小節）：
    GET  /projects/{name}/files?server=&subdir=      （唯讀直接執行，不走
                                                        核准；列檔案，秘密
                                                        檔已過濾）
    GET  /projects/{name}/file?server=&path=          （唯讀直接執行，不走
                                                        核准；讀單一檔案前
                                                        64KB；路徑穿越/秘密
                                                        檔名一律 400）
    POST /projects/{name}/apply-patch-request         （建 kind=apply_patch
                                                        approval，真正的
                                                        git apply/commit
                                                        發生在核准當下）

階段 13 新增（PLAN.md N 節，2026-07-10 使用者裁定改版為 Codex Worker
v2：Central Codex Runner）：AI 改碼層次二——核准的是自然語言 instruction，
不是 diff；核准後建一筆 coding_runs 記錄＋派一個 type="coding" 的任務跑
`codex exec`（永遠在唯一一台 `.env` `CODEX_RUNNER_SERVER` 指定的機器上），
見 README「Codex Worker」小節：
    POST /projects/{name}/coding-task-request         （建 kind=coding_task
                                                        approval，真正派工
                                                        發生在核准當下，見
                                                        `app/approvals.py`
                                                        的 `approve()` 的
                                                        coding_task 分支）
    GET  /codex-runner/status                          （唯讀；Runner 未設定
                                                        只回
                                                        {"configured": false}）
    GET  /coding-runs?status=&project=&limit=          （唯讀列表；不回傳
                                                        worktree/bundle 的
                                                        絕對路徑）
    GET  /coding-runs/{id}                              （唯讀詳情，另附
                                                        final_message／
                                                        diff_patch）
    POST /coding-runs/{id}/cleanup                      （PLAN.md N.9 鐵律
                                                        11；只能清理已終態
                                                        且無 job 引用的
                                                        task 目錄，見
                                                        `app.approvals.
                                                        cleanup_coding_run()`）

PLAN.md N.7（下游 bundle 流）：`POST /dispatch`／`POST /jobs` 的
`JobCreateRequest` 加選填 `source_coding_run_id`，核准當下（kind=enqueue
分支）建一個 bundle 推送任務並在主任務 command 前面加確定性的 checkout
前置段，見 `app.approvals.build_bundle_checkout_preamble()`／
`app.results.build_bundle_push_command()`。

階段 15 Phase A 新增（PLAN.md P.1 節，專案統一管理——看得到）：
    GET  /projects/matrix                              （唯讀，純 DB、不即時
                                                        SSH：專案 × 機器
                                                        矩陣＋各機 pending
                                                        候選數，見
                                                        `get_projects_matrix()`）
    POST /inventory/candidates/ignore-nested-request    （建 kind=
                                                        ignore_nested_candidates
                                                        approval——Fable
                                                        裁定第 2 點：批次
                                                        清理巢狀候選一樣走
                                                        核准流，不是繞過
                                                        核准直接執行，只是
                                                        一次核准全部生效，
                                                        見
                                                        `app.approvals.
                                                        request_ignore_nested_candidates_approval()`；
                                                        找不到可清理的巢狀
                                                        候選 → 400，不建
                                                        approval）

階段 15 Phase B 新增（PLAN.md P.2 節，整理：git 化＋中央 hub）：
    POST /projects/{name}/git-init-request              （建 kind=git_init
                                                        approval，真正的
                                                        .gitignore／
                                                        git init／commit
                                                        發生在核准當下，見
                                                        `app.approvals.
                                                        approve()` 的
                                                        git_init 分支；
                                                        專案不存在 404，
                                                        instance 不存在／
                                                        已是 git repo／
                                                        extra_ignores 不合法
                                                        400）
    POST /projects/{name}/hub-sync                      （直接執行＋稽核
                                                        hub_sync，不出核准
                                                        卡，見
                                                        `app.hub.
                                                        sync_project_to_hub()`；
                                                        instance 不存在 404，
                                                        不是 git repo 400）
    DELETE /projects/{name}                             （直接執行＋稽核
                                                        project_deleted，
                                                        只刪 DB 列、絕不動
                                                        機器上的檔案；
                                                        queued/running job
                                                        引用 → 409）
`GET /projects/matrix` 每筆 project 加 `hub: {exists, head, last_sync}`
（PLAN.md P.2.3，見 `app.hub.get_project_hub_info()`）。

階段 16 新增（PLAN.md Q.3 節，資料集倉庫 v2：打包快照＋資料卡）：
**`POST /datasets` 行為變更（刻意的 breaking change）**——`description`／
`method` 改為強制必填（使用者定的規則：「資料集必須明確記錄製作方式與
數量」），缺其一 -> 400；新增選填 `derived_from`（`{name, version}`，
指向的版本必須已存在，否則 400）與 `counts`（自訂數量 dict，值限
str/int/float）：
    GET  /datasets/{name}/{version}/card    （唯讀；`card` 可能是
                                              `null`——階段 16 之前建立
                                              的版本沒有卡，查詢不報錯、
                                              不回空，`note` 欄位明確
                                              標示「未登記資料卡」，
                                              `auto_facts`／`rendered`
                                              照常提供；資料集不存在
                                              404）
    PATCH /datasets/{name}/{version}/card   （補登／更新資料卡，
                                              description/method 同樣
                                              強制必填；保留原
                                              created_at，稽核
                                              dataset_card_updated；
                                              資料集不存在 404）
見 `app/datasets.py` 的 `build_dataset_card()`/`render_dataset_card()`/
`validate_card_fields()`。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

import httpx

from app import approvals as approvals_module
from app import autoapprove
from app.activity import (
    ProjectInstanceResolutionError,
    list_instance_files,
    probe_instance,
    read_instance_file,
    resolve_project_instance,
    validate_rel_path,
)
from app.agent_runtime import run_agent
from app.agent_tools import AgentContext, dispatch_tool, list_tool_specs
from app.approvals import (
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    CandidateNotFoundError,
    CandidateNotPendingError,
    CodingRunNotCleanableError,
    CodingRunNotFoundError,
    ForbiddenScanRootError,
    InvalidApplyPatchRequestError,
    InvalidCodingTaskRequestError,
    InvalidGitInitRequestError,
    InvalidServerConfigError,
    IdentityAdministrationDisabledError,
    IdentityTargetNotFoundError,
    JobNotFoundError,
    JobNotRunningError,
    ManualCandidateDuplicateError,
    ManualCandidatePathInvalidError,
    ManualCandidateServerInvalidError,
    NoNestedCandidatesError,
    ProjectNotFoundError,
    ServerNotFoundError,
    ServerRenameNotSupportedError,
)
from app.audit import (
    SYSTEM_AUDIT_ACTOR,
    append_audit,
    audit_actor_from_request_context,
    tail_audit,
)
from app.authentication import ensure_legacy_admin_actor, resolve_request_context
from app.authorization_catalog import ROUTE_AUTHORIZATION
from app.authorization_shadow import collect_shadow_evidence, emit_shadow_evidence
from app.chat import handle_chat_text
from app.config import AppConfig, ServerConfig, apply_codex_config_rules, load_app_config
from app.datasets import (
    LOCAL_SERVER,
    NO_CARD_NOTE,
    InvalidDatasetCardError,
    InvalidNameError,
    build_dataset_auto_facts,
    build_dataset_card,
    build_manifest,
    parse_dataset_ls_output,
    reconcile_server_dataset_cache,
    render_dataset_card,
    validate_card_fields,
    validate_name_component,
)
from app.db import (
    Approval,
    CodingRun,
    Database,
    Dataset,
    ExperimentRecord,
    Job,
    Project,
    ProjectCandidate,
    ProjectInstance,
    ProjectVersion,
    VALID_RECORD_KINDS,
)
from app.hub import (
    HubSyncError,
    InvalidProjectDeployRequestError,
    get_project_hub_info,
    request_project_deploy_approval,
    sync_project_to_hub,
)
from app.inventory import find_link_suggestions
from app.jobfinish import handle_job_finished
from app.jobqueue import (
    DangerousCommandError,
    build_log_tail_command,
    cancel_job,
)
from app.identity import (
    Actor,
    IssuedServiceToken,
    ProjectMembership,
    RequestContext,
    ServiceAccount,
    ServiceAccountToken,
)
from app.project_instances import reconcile_all_instances
from app.records import build_timeline
from app.llm import LLMError, build_client, diagnose_job_failure, is_llm_available
from app.llm import summarize_mail_body
from app.llm_local import (
    LLMLocalError,
    diagnose_job_failure_local,
    is_vllm_available,
    summarize_mail_body_local,
)
from app.localrun import local_run, local_write_file
from app.mailer import build_stall_mail, send_mail
from app.monitor import ServerState, probe_server
from app.results import local_result_dir
from app.scheduler import scheduler_tick
from app.server_config import (
    load_servers_config,
    reload_server_config_if_supported,
    server_config_to_safe_dict,
    test_ssh_connection,
    validate_server_config,
)
from app.sshpool import SSHPool

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: 階段 13（PLAN.md N.6）：`GET /codex-runner/status` 用的唯讀 SSH 探測指令
#: ——固定輸出兩行：第一行是 `codex --version` 的輸出（沒裝就是
#: `NO_CODEX`），第二行是 `AUTH_OK`/`AUTH_NO`。**不落地／不回傳
#: `codex login status` 的原始輸出**（可能含帳號 email），只轉成這兩個
#: 固定字樣（PLAN.md N.9 鐵律 7）。
_CODEX_PROBE_COMMAND = (
    "command -v codex >/dev/null 2>&1 "
    "&& codex --version 2>/dev/null | head -1 || echo NO_CODEX; "
    "codex login status >/dev/null 2>&1 && echo AUTH_OK || echo AUTH_NO"
)
_CODEX_PROBE_CACHE_TTL_SEC = 30.0
_CODEX_PROBE_DEFAULT = {"codex_installed": False, "codex_version": None, "authenticated": False}


def _parse_codex_probe_output(output: str) -> dict:
    """解析 `_CODEX_PROBE_COMMAND` 的 stdout：第一行非空且不是 `NO_CODEX`
    -> `codex_installed=True`、`codex_version` 記那一行原文；第二行是
    `AUTH_OK` -> `authenticated=True`。任何格式不如預期（空輸出、只有一
    行等）一律降級成未安裝／未登入，不丟例外——呼叫端（SSH 探測失敗）已經
    有自己的 try/except，這裡只負責「輸出格式不符預期」這一種情況。"""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    version_line = lines[0] if lines else ""
    auth_line = lines[1] if len(lines) > 1 else ""
    codex_installed = bool(version_line) and version_line != "NO_CODEX"
    return {
        "codex_installed": codex_installed,
        "codex_version": version_line if codex_installed else None,
        "authenticated": auth_line == "AUTH_OK",
    }


class AppState:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database(config.db_path)
        # Bootstrap before any SSH/client/background side effect.  A reserved
        # identity collision raises and stops startup without rewriting the
        # conflicting row; disabling compatibility leaves any existing actor
        # intact and skips this write entirely.
        if config.auth_token and config.legacy_shared_token_enabled:
            try:
                ensure_legacy_admin_actor(self.db)
            except Exception:
                self.db.close()
                raise
        self.ssh_pool = SSHPool(config)
        self.server_states: dict[str, ServerState] = {
            s.name: ServerState(name=s.name, online=False) for s in config.servers
        }
        self.server_configs = {s.name: s for s in config.servers}
        self._tasks: list[asyncio.Task] = []
        #: 階段 4：任務結束 hook（拉結果／寄信）與卡死提醒信都是「背景執行、
        #: 不 await 阻塞排程輪」的 task（拉大結果的 rsync 可能跑數分鐘）。
        #: 收進這個 set 追蹤，done 時自動移除、記例外 log；
        #: `stop_background_tasks()` 關服務時一併 cancel，不留下孤兒 task。
        self._background_tasks: set[asyncio.Task] = set()
        #: 階段 5：LLM client，啟動時建立一次、重複使用（見 app/llm.py）。
        #: 沒有裝 anthropic 套件或沒設定 ANTHROPIC_API_KEY 時維持 None，
        #: 聊天／診斷／信件摘要會各自降級（規則式／不可用／不加摘要），
        #: 不影響系統其他部分運作（鐵律第 1 條）。建立 client 本身只是物件
        #: 建構，不會發起網路連線，但仍防禦性包一層 try/except。
        self.llm_client = None
        if is_llm_available(config):
            try:
                self.llm_client = build_client(config)
            except Exception as exc:  # noqa: BLE001
                logger.warning("建立 LLM client 失敗，LLM 功能降級為不可用: %s", exc)
                self.llm_client = None

        #: 階段 7：本地 vLLM Agent Layer（app/llm_local.py／app/agent_runtime.py）。
        #: 只有 `is_vllm_available()` 為 True 時才建立共用的 `httpx.AsyncClient`
        #: （啟動時建一次、重複使用，shutdown 時 aclose），並用一個
        #: `asyncio.Semaphore` 限制同時跑幾個 agent 對話（`agent_max_concurrency`，
        #: 預設 2）——JSON tool loop 一次請求可能連續呼叫本地模型好幾輪，不限制
        #: 併發很容易把單張 GPU 的 vLLM 服務打爆。沒設定 VLLM_BASE_URL/VLLM_MODEL
        #: 時這兩者維持 None，`/agent/*` 與 WS `/ws` 的 agent 路徑都會明確降級
        #: （見對應 endpoint），不影響其他任何功能（鐵律第 1 條精神的延伸）。
        self.vllm_client: Optional[httpx.AsyncClient] = None
        if is_vllm_available(config):
            self.vllm_client = httpx.AsyncClient()
        self.agent_semaphore = asyncio.Semaphore(max(1, config.agent_max_concurrency))

        #: 階段 13（PLAN.md N.6）：`GET /codex-runner/status` 的 codex 安裝/
        #: 版本/登入探測結果快取（30 秒，見 `_probe_codex_runner()`）——避免
        #: 每次輪詢這個端點都重新 SSH 到 Runner。`time.monotonic()` 計時
        #: （不受系統時間調整影響），值是 `None` 表示還沒探測過。
        self._codex_probe_cache: Optional[dict] = None
        self._codex_probe_cache_at: Optional[float] = None

    async def ssh_run(self, server_name: str, command: str, timeout: float):
        """階段 3：`server_name == "_local"` 時路由到 `app.localrun`（sync 任務
        在 Server A 本地執行），其餘照舊走 SSH。呼叫端（scheduler／jobqueue）
        完全不用區分，介面形狀一致。"""
        if server_name == LOCAL_SERVER:
            return await local_run(command, timeout, cwd=self.config.local_home_dir)
        server_cfg = self.server_configs[server_name]
        return await self.ssh_pool.run(server_cfg, command, timeout)

    async def ssh_write_file(self, server_name: str, path: str, content: str):
        if server_name == LOCAL_SERVER:
            return await local_write_file(path, content, cwd=self.config.local_home_dir)
        server_cfg = self.server_configs[server_name]
        return await self.ssh_pool.write_file(server_cfg, path, content)

    async def _probe_one(self, server) -> ServerState:
        try:
            return await probe_server(self.ssh_run, server.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("監控 %s 失敗: %s", server.name, exc)
            return ServerState(name=server.name, online=False, error=str(exc))

    async def monitor_loop(self):
        while True:
            # 平行探測所有機器，不用逐台等待；sshpool 本來就有每機序列化的
            # lock 與全域併發上限（ssh_max_concurrency），平行呼叫是安全的。
            # 機器數一多，串行探測很容易一輪就超過 monitor_interval_sec。
            results = await asyncio.gather(
                *(self._probe_one(server) for server in self.config.servers)
            )
            for server, state in zip(self.config.servers, results):
                self.server_states[server.name] = state
            await asyncio.sleep(self.config.monitor_interval_sec)

    async def scheduler_loop(self):
        while True:
            try:
                await scheduler_tick(
                    self.db,
                    self.server_states,
                    self.server_configs,
                    self.ssh_run,
                    self.ssh_write_file,
                    audit_path=self.config.audit_path,
                    on_job_finished=self.schedule_job_finished_hook,
                    on_stall_detected=self.schedule_stall_notification,
                    stall_minutes=self.config.stall_minutes,
                    codex_runner_server=self.config.codex_runner_server,
                    codex_runner_reserve=self.config.codex_runner_reserve,
                    codex_max_concurrency=self.config.codex_max_concurrency,
                )
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_tick 發生例外，本輪略過")
            await asyncio.sleep(self.config.scheduler_interval_sec)

    def _spawn_tracked_task(self, coro) -> None:
        """把一個 coroutine 丟進背景執行、不 await（呼叫端可能正在排程輪
        裡，不能被卡住），收進 `self._background_tasks` 追蹤：done 時自動
        移除，若是因為例外結束（非 cancel）記 log，不讓背景任務的例外無聲
        消失。`stop_background_tasks()` 關服務時會把這裡還沒跑完的 task
        一併 cancel。"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("背景通知任務發生例外: %s", exc, exc_info=exc)

        task.add_done_callback(_on_done)

    async def _summarize_mail(self, body: str, config: AppConfig) -> Optional[str]:
        """`handle_job_finished()` 的 `summarize_mail` 注入點：重用啟動時建立
        好的 `self.llm_client`（見 `__init__`），不用每封信都重新建一次
        client。`app.llm.summarize_mail_body()` 本身在 LLM 不可用時就直接
        回傳 None，不會發起任何網路呼叫。

        階段 7 後備：anthropic 優先，沒有的話（`is_llm_available()` 為
        False）才試本地 vLLM（`summarize_mail_body_local()`，同樣任何失敗
        都回 None）；兩者都沒有 → None，照常寄原信不加摘要。"""
        if is_llm_available(config):
            return await summarize_mail_body(body, config, client=self.llm_client)
        return await summarize_mail_body_local(body, config, client=self.vllm_client)

    def schedule_job_finished_hook(self, job: Job) -> None:
        """任務結束（done/failed）背景 hook：拉結果 → 寄信（含階段 5 的 LLM
        摘要，可用才加）→ 寫稽核（PLAN.md E／F）。用 `asyncio.create_task()`
        背景執行、不 await——拉大結果的 rsync 可能跑數分鐘，不能卡住排程輪。

        階段 13（PLAN.md N.6，批次 3a 補線）：傳入 `db=self.db`，讓
        `handle_job_finished()` 對 `job.type == "coding"` 的任務真的呼叫
        `_backfill_coding_run()` 回填 `coding_runs`（批次 2 已經支援
        `db=` 這個選填參數，只是這裡一直沒接線）。
        """
        server_cfg = self.server_configs.get(job.server) if job.server else None
        self._spawn_tracked_task(
            handle_job_finished(
                job,
                server_cfg=server_cfg,
                local_run=local_run,
                config=self.config,
                audit_path=self.config.audit_path,
                db=self.db,
                summarize_mail=self._summarize_mail,
            )
        )

    async def _probe_codex_runner(self, runner: str, online: bool) -> dict:
        """PLAN.md N.6：`GET /codex-runner/status` 用的唯讀 SSH 探測——是否
        裝了 `codex`、版本字串、是否已登入（`codex login status`）。結果
        cache 30 秒（`self._codex_probe_cache`/`_at`），避免每次輪詢這個
        端點都重新 SSH。**離線時完全跳過探測**（不嘗試連線一台已知離線的
        機器），回上一次的快取（沒有快取就回全 False 的預設值）。

        **絕不把 `codex login status` 的原始輸出、帳號 email、token 落地或
        回傳**（PLAN.md N.6／N.9 鐵律 7）——探測指令本身只用 shell
        `&&`/`||` 把結果轉成固定的 `NO_CODEX`/`AUTH_OK`/`AUTH_NO` 字樣，
        `_parse_codex_probe_output()` 只留兩個布林 + 版本字串這三個欄位，
        SSH 回傳的完整 stdout 不會被存起來或往上傳遞。
        """
        now = time.monotonic()
        if not online:
            return self._codex_probe_cache or dict(_CODEX_PROBE_DEFAULT)
        if (
            self._codex_probe_cache is not None
            and self._codex_probe_cache_at is not None
            and now - self._codex_probe_cache_at < _CODEX_PROBE_CACHE_TTL_SEC
        ):
            return self._codex_probe_cache
        try:
            result = await self.ssh_run(runner, _CODEX_PROBE_COMMAND, 15)
            parsed = _parse_codex_probe_output(result.stdout or "")
        except Exception as exc:  # noqa: BLE001 - SSH 連不上等，降級回預設值
            logger.warning("探測 Codex Runner %s 狀態失敗: %s", runner, exc)
            parsed = dict(_CODEX_PROBE_DEFAULT)
        self._codex_probe_cache = parsed
        self._codex_probe_cache_at = now
        return parsed

    async def get_codex_runner_status(self) -> dict:
        """`GET /codex-runner/status`（PLAN.md N.6）的核心邏輯：未設定
        `CODEX_RUNNER_SERVER` 只回 `{"configured": False}`；有設定時
        `online` 直接讀 `self.server_states`（monitor 迴圈已經在維護，不用
        另外 SSH），`busy`/`running_job_id` 統計目前 `status="running"` 且
        `type="coding"` 的 job（PLAN.md N.8：coding job 只可能 running 在
        Runner 上，不用另外篩 `server` 欄位，跟 `app.scheduler.
        scheduler_tick()` 的 `running_coding_count` 統計方式一致）。
        """
        runner = self.config.codex_runner_server
        if runner is None:
            return {"configured": False}
        state = self.server_states.get(runner)
        online = bool(state and state.online)
        probe = await self._probe_codex_runner(runner, online)
        running_coding = [j for j in self.db.list_jobs(status="running") if j.type == "coding"]
        return {
            "configured": True,
            "server": runner,
            "online": online,
            "codex_installed": probe["codex_installed"],
            "codex_version": probe["codex_version"],
            "authenticated": probe["authenticated"],
            "auth_mode": self.config.codex_auth_mode,
            "busy": bool(running_coding),
            "running_job_id": running_coding[0].id if running_coding else None,
            "max_concurrency": self.config.codex_max_concurrency,
        }

    async def _send_stall_mail(self, job: Job) -> None:
        subject, body = build_stall_mail(job, self.config.stall_minutes)
        mailed = await send_mail(self.config, subject, body)
        append_audit(
            "stall_notified",
            {"job_id": job.id, "mailed": mailed},
            path=self.config.audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )

    def schedule_stall_notification(self, job: Job) -> None:
        """卡死偵測提醒信，同任務結束 hook 的背景 task 追蹤機制（見
        `_spawn_tracked_task()`），去重（只寄一次）邏輯在
        `app.scheduler._check_stalled_jobs()`（`stall_notified` 旗標）。"""
        self._spawn_tracked_task(self._send_stall_mail(job))

    async def dataset_cache_reconcile_loop(self):
        """每小時 SSH 各機 `ls -d datasets/*/*/` 校正 dataset_cache（PLAN.md D）。
        離線機跳過；單一機器探測失敗不影響其他機器（記 log，本輪略過）。"""
        while True:
            for server in self.config.servers:
                state = self.server_states.get(server.name)
                if state is None or not state.online:
                    continue
                try:
                    result = await self.ssh_run(
                        server.name, "ls -d datasets/*/*/ 2>/dev/null", 20
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("校正 %s 的 dataset_cache 失敗: %s", server.name, exc)
                    continue
                found = parse_dataset_ls_output(result.stdout or "")
                added, removed = reconcile_server_dataset_cache(self.db, server.name, found)
                if added or removed:
                    append_audit(
                        "cache_reconcile",
                        {
                            "server": server.name,
                            "added": [f"{n}@{v}" for n, v in sorted(added)],
                            "removed": [f"{n}@{v}" for n, v in sorted(removed)],
                        },
                        path=self.config.audit_path,
                        actor=SYSTEM_AUDIT_ACTOR,
                    )
            await asyncio.sleep(self.config.dataset_reconcile_interval_sec)

    async def project_instance_reconcile_loop(self):
        """PLAN.md 2026-07-11 版 §14 切片 2:定期唯讀探測全部
        project_instances、收斂 state（app/project_instances.py）。獨立
        背景迴圈,不佔排程輪（INV-STATE-6）;list/detail API 讀的是這裡
        落地的快照,不即時 SSH（INV-PROJECT-3 草案）。單輪失敗記 log 後
        照常等下一輪,不讓迴圈死掉。"""

        async def hub_head_for(project_name: str) -> Optional[str]:
            # reconcile_all_instances() 內已對同一輪的同專案快取,這裡
            # 每次都問 get_project_hub_info()(純 Server A 本地 git 查詢,
            # 不對工作機 SSH)。
            info = await get_project_hub_info(
                project_name, self.config.local_home_dir, local_run=local_run
            )
            return info.get("head") if info.get("exists") else None

        def is_server_online(server: str) -> bool:
            state = self.server_states.get(server)
            return state is not None and state.online

        while True:
            try:
                changes = await reconcile_all_instances(
                    self.db, self.ssh_run, is_server_online, hub_head_for
                )
                if changes:
                    append_audit(
                        "instance_reconcile",
                        {"changes": changes},
                        path=self.config.audit_path,
                        actor=SYSTEM_AUDIT_ACTOR,
                    )
            except Exception as exc:  # noqa: BLE001 - 單輪失敗不讓迴圈死掉
                logger.warning("instance reconcile 一輪失敗: %s", exc)
            await asyncio.sleep(self.config.project_reconcile_interval_sec)

    def start_background_tasks(self):
        self._tasks = [
            asyncio.create_task(self.monitor_loop()),
            asyncio.create_task(self.scheduler_loop()),
            asyncio.create_task(self.dataset_cache_reconcile_loop()),
            asyncio.create_task(self.project_instance_reconcile_loop()),
        ]

    async def stop_background_tasks(self):
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 階段 4：任務結束 hook／卡死提醒信的背景 task 也要一併收掉，不留
        # 孤兒 task（例如服務關閉時剛好有一個 rsync 拉結果還在跑）。
        pending_hooks = list(self._background_tasks)
        for task in pending_hooks:
            task.cancel()
        for task in pending_hooks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 關服務時不需要再處理背景例外
                pass
        await self.ssh_pool.close_all()
        if self.vllm_client is not None:
            await self.vllm_client.aclose()
        self.db.close()


app_state: Optional[AppState] = None


async def _authorization_shadow_dependency(connection: HTTPConnection) -> None:
    """Prepare observational policy evidence for one matched HTTP route.

    FastAPI resolves this dependency after routing and after the authentication
    middleware attached ``RequestContext``.  No decision is returned to the
    endpoint.  The middleware appends deferred evidence only after the existing
    response has been materialized.
    """

    # FastAPI application dependencies are also attached to WebSocket routes.
    # WebSockets have their explicit post-authentication hook below.
    if connection.scope.get("type") != "http" or not isinstance(connection, Request):
        return
    request = connection
    state = app_state
    if state is None or state.config.authorization_mode != "shadow":
        return
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if not isinstance(route_path, str):
        return
    spec = ROUTE_AUTHORIZATION.get((request.method, route_path))
    if spec is None:
        # Public and framework-owned interfaces are deliberately not evaluated.
        return

    values: dict[str, Any] = dict(request.path_params)
    for key, value in request.query_params.items():
        values.setdefault(key, value)
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - endpoint retains normal validation
            body = None
        if isinstance(body, dict):
            for key, value in body.items():
                values.setdefault(key, value)

    request.state.authorization_shadow_evidence = collect_shadow_evidence(
        mode=state.config.authorization_mode,
        db=state.db,
        context=request.state.request_context,
        action=spec.action,
        resource_kind=spec.resource_kind,
        values=values,
        interface_kind="route",
        interface_name=f"{request.method} {route_path}",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_state
    config = load_app_config()
    #: 階段 13（PLAN.md N.1）：CODEX_RUNNER_SERVER 有設定時，值必須是
    #: servers.yaml 既有、enabled 的 server，且 CODEX_AUTH_MODE 合法——
    #: 不符合直接讓 ValueError 炸出、啟動失敗（設了就要設對）。未設定
    #: （None）＝ Codex 功能停用，`apply_codex_config_rules()` 不做事、
    #: 服務照常啟動。concurrency 超標的 warning 照 servers.yaml／
    #: auto_approve.yaml 解析失敗即降級的既有 log 慣例逐條印出。
    for warning in apply_codex_config_rules(
        config, {s.name: s.enabled for s in config.servers}
    ):
        logger.warning(warning)
    app_state = AppState(config)
    app_state.start_background_tasks()
    try:
        yield
    finally:
        await app_state.stop_background_tasks()


app = FastAPI(
    title="AI 訓練調度中心",
    lifespan=lifespan,
    dependencies=[Depends(_authorization_shadow_dependency)],
)

#: 不需要 AUTH_TOKEN header 也能存取的路徑（靜態頁與其資源）。
_AUTH_EXEMPT_PATHS = {"/"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Resolve a session, opted-in service bearer, or compatible shared token.

    AUTH_TOKEN configured keeps the exact historical protected-route/401
    behavior; unset remains anonymous/open for local development.  This slice
    attaches identity only and does not evaluate authorization policy.
    """
    context: Optional[RequestContext] = None
    token = app_state.config.auth_token if app_state is not None else None
    path = request.url.path
    exempt = path in _AUTH_EXEMPT_PATHS or path.startswith("/static/")
    if app_state is not None:
        config = app_state.config
        context = resolve_request_context(
            app_state.db,
            session_token=request.cookies.get(config.session_cookie_name),
            authorization=request.headers.get("Authorization"),
            legacy_token=request.headers.get("X-Auth-Token"),
            configured_legacy_token=token,
            legacy_shared_token_enabled=config.legacy_shared_token_enabled,
            service_token_auth_enabled=config.service_token_auth_enabled,
        )
    if context is None:
        # AUTH_TOKEN-unset development mode remains open and anonymous.  When
        # it is configured, a valid session/service/compatible legacy
        # credential is required for every non-exempt path, preserving the
        # exact historical 401 response.
        context = RequestContext()
        if token and not exempt:
            return JSONResponse(
                status_code=401, content={"detail": "缺少或錯誤的 X-Auth-Token"}
            )
    request.state.request_context = context
    try:
        return await call_next(request)
    finally:
        # Deferred emission keeps `/events`, `/audit`, and every other response
        # identical to mode `off`; shadow evidence is append-only afterwards.
        if app_state is not None:
            emit_shadow_evidence(
                getattr(request.state, "authorization_shadow_evidence", ()),
                audit_path=app_state.config.audit_path,
            )


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    return FileResponse(str(index_path))


@app.get("/auth/me")
async def auth_me(request: Request):
    """Return only log-safe metadata for the caller's resolved principal."""

    context: RequestContext = request.state.request_context
    actor = context.actor
    return {
        "authenticated": actor is not None,
        "authentication_method": context.authentication_method,
        "actor": (
            {
                "id": actor.id,
                "type": actor.actor_type.value,
                "display_name": actor.display_name,
                "platform_admin": actor.platform_admin,
            }
            if actor is not None
            else None
        ),
        "project_memberships": [
            {"project_id": membership.project_id, "role": membership.role.value}
            for membership in context.project_memberships
        ],
        "service_scopes": sorted(context.service_scopes),
    }


#: 階段 10（PLAN.md K.1）：請求來源枚舉。未標記／不合法值一律當 "api"
#: 處理（不是 400——source 只是一個提示欄位，不是需要嚴格驗證的輸入；行為
#: 同現狀＝一律出核准卡，向下相容既有呼叫端）。信任說明：`source` 可以被
#: 持有 `AUTH_TOKEN` 的呼叫端自由填寫，這不是漏洞——token 持有者本來就有
#: 完整核准權，冒充 `source` 得不到超出 token 已有的權限（見 README）。
_VALID_SOURCES = {"web", "chatgpt", "vllm", "api"}


def _normalize_source(source: Optional[str]) -> str:
    return source if source in _VALID_SOURCES else "api"


class JobCreateRequest(BaseModel):
    command: str
    type: str = "adhoc"
    project: Optional[str] = None
    require_tag: Optional[str] = None
    pin_server: Optional[str] = None
    priority: str = "normal"
    depends_on: list[int] = Field(default_factory=list)
    gpus_needed: Optional[int] = None
    #: 階段 10（PLAN.md K.1）：web/chatgpt/vllm/api，預設 "api"（未標記時
    #: 行為同現狀）。
    source: str = "api"
    #: 階段 13（PLAN.md N.7）：選填，這個任務要用某次 Codex coding run 的
    #: `changes.bundle` 當起點（下游 train／驗證任務）。驗證見
    #: `app.approvals.request_enqueue_approval()`。
    source_coding_run_id: Optional[int] = None


class StopJobRequest(BaseModel):
    """`POST /jobs/{id}/stop` 的選填 body（階段 10 前這個端點完全不吃
    body）——留空（不帶 body）時 `source` 預設 "api"，行為同現狀。"""

    source: str = "api"

    model_config = {"extra": "ignore"}


class RejectRequest(BaseModel):
    note: Optional[str] = None

    model_config = {"extra": "ignore"}


class ProjectCreateRequest(BaseModel):
    name: str
    repo_or_path: str
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    default_command: Optional[str] = None
    require_tag: Optional[str] = None
    setup_cmd: Optional[str] = None


class ProjectPatchRequest(BaseModel):
    """`PATCH /projects/{name}`（專案詳情頁計畫第 3 節）：只更新有明確帶的
    欄位（`exclude_unset`，見端點實作），空 body（沒有任何欄位）400。
    `summary` 沿用既有欄位（階段 8 第一批就有），這裡一起開放編輯，不需要
    另一支端點。"""

    goal: Optional[str] = None
    optimization_notes: Optional[str] = None
    progress: Optional[str] = None
    summary: Optional[str] = None

    model_config = {"extra": "ignore"}


class ExperimentRecordCreateRequest(BaseModel):
    """`POST /projects/{name}/records`（專案詳情頁計畫第 3 節）：手動建立
    一筆實驗紀錄。`author` 選填，省略時預設 `"user"`（網頁前端固定不帶這
    個欄位，行為等同既有設計）；有帶時必須是 `"user"` 或以 `"agent"` 開頭
    （同 `app.db._validate_record_author()` 的值域，見端點實作），這是給
    `app/mcp_bridge.py` 的 `add_experiment_record` 代理工具用的
    （帶 `"agent:chatgpt"`，PLAN.md 專案詳情頁計畫第 4 節）——本端點跟
    `app/mcp_bridge.py` 一樣是 AUTH_TOKEN 保護的可信呼叫端，`app.
    agent_tools` 的本地 vLLM 版 `add_experiment_record` 工具則完全不經過
    這支 HTTP 端點（直接呼叫 `db.insert_experiment_record()`），兩條路徑
    最終都落在同一張表、同一個值域驗證上。`job_id`／`coding_run_id`
    選填，用來把這筆紀錄掛在某次派工或 Codex 執行底下（弱關聯，同
    `app.db.insert_experiment_record()` 的既有設計——沒有 FK，不驗證所指
    id 是否存在／屬於這個專案，跟全案「弱關聯」慣例一致）。"""

    content: str
    kind: str = "note"
    title: Optional[str] = None
    author: Optional[str] = None
    job_id: Optional[int] = None
    coding_run_id: Optional[int] = None


class ExperimentRecordPatchRequest(BaseModel):
    """`PATCH /projects/{name}/records/{id}`：只更新有明確帶的欄位
    （`exclude_unset`），空 body 400。同 `insert`，不開放改 `author`。"""

    title: Optional[str] = None
    content: Optional[str] = None
    kind: Optional[str] = None

    model_config = {"extra": "ignore"}


class DatasetDerivedFromRequest(BaseModel):
    """階段 16（PLAN.md Q.3）：資料卡的 `derived_from`——`{name, version}`
    指向另一個已註冊的資料集版本，形成 lineage 鏈。"""

    name: str
    version: str


class DatasetCreateRequest(BaseModel):
    """階段 16（PLAN.md Q.3）起，`description`／`method` **強制必填**——
    刻意的 breaking change（使用者定的規則：「資料集必須明確記錄製作方式
    與數量」）。這裡刻意宣告成 `Optional[str] = None` 而不是必填的 `str`：
    缺這兩欄時要走 `create_dataset()` 裡自訂的 400（引用使用者規則的訊息
    文字），不要走 FastAPI 預設的 422 驗證錯誤格式。"""

    name: str
    version: str
    source_path: str
    description: Optional[str] = None
    method: Optional[str] = None
    derived_from: Optional[DatasetDerivedFromRequest] = None
    counts: Optional[dict[str, Any]] = None


class DatasetCardUpdateRequest(BaseModel):
    """`PATCH /datasets/{name}/{version}/card`——補登／更新資料卡，同樣
    強制 `description`／`method`（理由同 `DatasetCreateRequest`）。"""

    description: Optional[str] = None
    method: Optional[str] = None
    derived_from: Optional[DatasetDerivedFromRequest] = None
    counts: Optional[dict[str, Any]] = None


class InventoryScanRequest(BaseModel):
    """階段 8 第二批：`project_roots` 選填——省略（`None`）時從
    `server_configs[server].project_roots` 自動代入；明確帶入（含空字串以外
    的空列表 `[]`）視為「這次只掃這幾個」的覆蓋，空列表視為不合法請求（見
    app/approvals.py 的 request_inventory_scan_approval() docstring）。"""

    server: str
    project_roots: Optional[list[str]] = None


class ManualCandidateRequest(BaseModel):
    """P.1.5（2026-07-10 追加，Fable 定案）：`POST /inventory/candidates/manual`
    的 body。`name` 選填，省略時 fallback 用 `os.path.basename(path)`（見
    `app.approvals.add_manual_candidate()`）。"""

    server: str
    path: str
    name: Optional[str] = None


class ImportProjectCandidateRequest(BaseModel):
    """全部欄位選填：省略的部分核准時會 fallback 用 candidate 本身的猜測值
    （見 request_import_project_approval()）。"""

    name: Optional[str] = None
    default_command: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_mode: Optional[str] = None
    require_tag: Optional[str] = None
    setup_cmd: Optional[str] = None
    summary: Optional[str] = None
    #: PLAN.md 2026-07-11 版 §14 切片 3:有值 → 連結到既有 Project(名稱或
    #: UUID 皆可),不新建。與其餘欄位互斥的驗證由
    #: `request_import_project_approval()` 負責(找不到目標即 400)。
    link_to_project: Optional[str] = None

    model_config = {"extra": "ignore"}


class ApplyPatchRequest(BaseModel):
    """階段 12（PLAN.md M.2）：`POST /projects/{name}/apply-patch-request`
    的 body——`server` 必填（apply_patch 只能對準一台機器，不像讀檔端點
    的 `server` 選填自動選擇）。"""

    server: str
    diff: str
    description: Optional[str] = None


class CodingTaskRequest(BaseModel):
    """階段 13（PLAN.md N.2，Codex Worker v2）：
    `POST /projects/{name}/coding-task-request` 的 body。

    v2 不再讓呼叫端指定 Codex 執行機器——`server` 從必填改成選填，只為了
    相容 v1 舊客戶端（等於 `CODEX_RUNNER_SERVER` 才接受，見
    `app.approvals.request_coding_task_approval()` 的 `legacy_server`
    參數）；`instruction` 是自然語言需求全文；`base_branch`／
    `validation_target` 選填（後者只代表後續驗證想在哪台機器跑，不代表
    Codex 執行位置，見 PLAN.md N.2 第 2 點）。"""

    instruction: str
    base_branch: Optional[str] = None
    validation_target: Optional[str] = None
    server: Optional[str] = None


class GitInitRequest(BaseModel):
    """階段 15 Phase B（PLAN.md P.2.1）：`POST /projects/{name}/git-init-request`
    的 body——`server` 必填（git_init 只能對準一台機器，比照
    `ApplyPatchRequest`）；`extra_ignores` 選填，每項限單行 pattern，字元集
    驗證見 `app.approvals.request_git_init_approval()`。"""

    server: str
    extra_ignores: Optional[list[str]] = None


class HubSyncRequest(BaseModel):
    """階段 15 Phase B（PLAN.md P.2.2）：`POST /projects/{name}/hub-sync`
    的 body。"""

    server: str


class ProjectDeployRequest(BaseModel):
    """階段 15 Phase C（PLAN.md P.3）：`POST /projects/{name}/deploy-request`
    的 body——`target_server` 必填；`dest_path`／`ref` 選填，沒給時由
    `app.hub.request_project_deploy_approval()` 算出預設值（見該函式
    docstring）。"""

    target_server: str
    dest_path: Optional[str] = None
    ref: Optional[str] = None


class ServerConfigPayload(BaseModel):
    """一組完整的 server 設定（新增/測試 SSH 用）。故意用寬鬆的 dict-like
    模型（`extra="allow"`），實際驗證交給 `app.server_config.
    validate_server_config()`（單一驗證來源，不在 Pydantic 層重複規則）。
    """

    name: str
    host: str
    user: str
    key: str
    gpu: bool = False
    idle_gpu_util: float = 15.0
    idle_load: float = 2.0
    tags: list[str] = Field(default_factory=list)
    port: int = 22
    project_roots: list[str] = Field(default_factory=list)
    dataset_roots: list[str] = Field(default_factory=list)
    project_embedded_dataset_names: Optional[list[str]] = None
    project_exclude_names: Optional[list[str]] = None
    enabled: bool = True
    note: Optional[str] = None

    model_config = {"extra": "allow"}


class ServerUpdateRequest(BaseModel):
    name: str
    updates: dict = Field(default_factory=dict)

    model_config = {"extra": "ignore"}


class ServerNameRequest(BaseModel):
    name: str

    model_config = {"extra": "ignore"}


class ServiceAccountCreateRequest(BaseModel):
    """Non-secret input for an approval-gated service account creation."""

    name: str
    description: Optional[str] = None

    model_config = {"extra": "ignore"}


class ServiceTokenIssueRequest(BaseModel):
    """Non-secret token policy stored in the immutable approval payload."""

    label: Optional[str] = None
    scopes: list[str]
    expires_at: str

    model_config = {"extra": "ignore"}


class ProjectMembershipRequest(BaseModel):
    """Requested durable role for one actor in the route's project."""

    actor_id: str
    role: str

    model_config = {"extra": "ignore"}


def _job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.type,
        "project": job.project,
        "command": job.command,
        "require_tag": job.require_tag,
        "pin_server": job.pin_server,
        "depends_on": job.depends_on,
        "gpus_needed": job.gpus_needed,
        "status": job.status,
        "server": job.server,
        "priority": job.priority,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
        "log_tail": job.log_tail,
        "target_server": job.target_server,
        "dataset_name": job.dataset_name,
        "dataset_version": job.dataset_version,
        #: 階段 4：卡死偵測旗標（不影響 status，見 app/stall.py）。
        "stalled_suspect": bool(job.stalled_suspect),
        #: 階段 13（PLAN.md N.6/N.7）：這個 job 是否是「用某次 Codex
        #: coding run 的 changes.bundle 當起點」的下游任務，一般任務一律
        #: `None`。「job manifest」在現制＝jobs 欄位＋稽核（見 N.13），
        #: 這裡是那份 manifest 對外可見的一部分。
        "source_coding_run_id": job.source_coding_run_id,
    }


def _server_state_to_dict(state: ServerState, db: Optional[Database] = None) -> dict:
    d = asdict(state)
    d["gpu_util_max"] = state.gpu_util_max
    d["gpu_count"] = state.gpu_count
    #: 階段 3：已快取資料集小標籤（"name@version"），總覽卡片顯示用。
    if db is not None:
        d["cached_datasets"] = [
            f"{c.dataset}@{c.version}" for c in db.list_dataset_cache(server=state.name)
        ]
    else:
        d["cached_datasets"] = []
    return d


def _project_to_dict(project: Project) -> dict:
    return {
        "name": project.name,
        #: PLAN.md 2026-07-11 版 §14 切片 1：UUID 正式身分。呼叫這個 helper
        #: 的端點（/projects、/{name}/activity、/{name}/detail…）自動帶到;
        #: name-based 路徑參數過渡期照舊有效（get_project() 雙讀 adapter
        #: 也接受 UUID）。
        "id": project.id,
        "repo_or_path": project.repo_or_path,
        "dataset_name": project.dataset_name,
        "dataset_version": project.dataset_version,
        "default_command": project.default_command,
        "require_tag": project.require_tag,
        "setup_cmd": project.setup_cmd,
        "created_at": project.created_at,
        #: 階段 8 第一批：inventory 匯入專案可能帶的摘要與資料集模式。
        "summary": project.summary,
        "dataset_mode": project.dataset_mode,
        #: 專案詳情頁計畫第 3 節：目標／優化方法／目前進度，自由文字
        #: Markdown，`None`＝尚未填寫。`GET /projects/matrix` 走
        #: `hub_info`／自己組 dict（不呼叫這個 helper），這三欄不會自動帶到
        #: 那支端點；`GET /projects/{name}/activity` 呼叫這個 helper，加了
        #: 這三欄之後自動跟著帶到（見計畫第 3 節「`_project_to_dict` 加三欄
        #: 後 `/activity` 自動帶到」）。
        "goal": project.goal,
        "optimization_notes": project.optimization_notes,
        "progress": project.progress,
    }


def _candidate_to_dict(c: ProjectCandidate) -> dict:
    return {
        "id": c.id,
        "server": c.server,
        "path": c.path,
        "name_guess": c.name_guess,
        "kind": c.kind,
        "git_remote": c.git_remote,
        "git_branch": c.git_branch,
        "git_commit": c.git_commit,
        "markers": c.markers,
        "readme_excerpt": c.readme_excerpt,
        "command_guess": c.command_guess,
        "embedded_data_paths": c.embedded_data_paths,
        "embedded_data_summary": c.embedded_data_summary,
        "estimated_data_bytes": c.estimated_data_bytes,
        "excluded_paths": c.excluded_paths,
        "confidence": c.confidence,
        "status": c.status,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _instance_to_dict(i: ProjectInstance) -> dict:
    return {
        "id": i.id,
        "project_name": i.project_name,
        #: 切片 1:所屬 Project 的 UUID 雙寫;孤兒列（project 已刪）是 None。
        "project_id": i.project_id,
        "server": i.server,
        "path": i.path,
        "git_remote": i.git_remote,
        "git_branch": i.git_branch,
        "git_commit": i.git_commit,
        "dirty": i.dirty,
        "embedded_data_paths": i.embedded_data_paths,
        "last_seen": i.last_seen,
        #: 切片 1 schema readiness:一律 'unknown',切片 2 reconcile 才有
        #: available/missing/dirty/diverged 判定。
        "state": i.state,
    }


def _project_version_to_dict(v: ProjectVersion) -> dict:
    return {
        "id": v.id,
        "project_id": v.project_id,
        "project_name": v.project_name,
        "git_commit": v.git_commit,
        "git_ref": v.git_ref,
        "source_instance_id": v.source_instance_id,
        "created_at": v.created_at,
        "metadata": v.metadata,
    }


def _job_activity_summary(job: Job) -> dict:
    """階段 11（PLAN.md L.2）：`GET /projects/{name}/activity` 的「最近 10 筆
    jobs（狀態/exit_code/耗時）」摘要——耗時用 `started_at`/`finished_at`算，
    任一時間缺漏或格式無法解析都回傳 `None`，不讓這個端點因為時間格式問題
    而整個炸掉（比照 `app/mailer.py` 的 `_elapsed_str()` 容錯風格）。"""
    duration_seconds: Optional[int] = None
    if job.started_at:
        try:
            start = datetime.fromisoformat(job.started_at)
            end = datetime.fromisoformat(job.finished_at) if job.finished_at else datetime.now(
                timezone.utc
            )
            duration_seconds = max(0, int((end - start).total_seconds()))
        except ValueError:
            duration_seconds = None
    return {
        "id": job.id,
        "status": job.status,
        "exit_code": job.exit_code,
        "duration_seconds": duration_seconds,
        "server": job.server,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _activity_server_state_summary(state: Optional[ServerState]) -> dict:
    """階段 11（PLAN.md L.2）：「各機當下 GPU/磁碟」摘要，只取
    online/gpu_util_max/disk_avail_bytes 三個欄位（不是完整
    `_server_state_to_dict()`，這個端點只需要精簡摘要）。`state` 為 `None`
    （機器不在 `server_states` 裡，理論上不會發生，防禦性處理）時視為離線。
    """
    if state is None:
        return {"online": False, "gpu_util_max": None, "disk_avail_bytes": None}
    return {
        "online": state.online,
        "gpu_util_max": state.gpu_util_max,
        "disk_avail_bytes": state.disk_avail_bytes,
    }


def _dataset_to_dict(dataset: Dataset, full: bool = False) -> dict:
    d = {
        "name": dataset.name,
        "version": dataset.version,
        "size_bytes": dataset.size_bytes,
        "source_path": dataset.source_path,
        "created_at": dataset.created_at,
        "file_count": dataset.manifest.get("file_count"),
        #: 階段 16（PLAN.md Q.3）：資料卡摘要（`None` = 尚未登記，見
        #: GET/PATCH `/datasets/{name}/{version}/card`）。這裡回傳整份
        #: card（欄位不多，不像 manifest 那樣可能很大），列表頁與詳情頁
        #: 都能直接讀 description 首行做摘要，不需要額外打一次 card 端點。
        "card": dataset.card,
        #: 階段 16（PLAN.md Q.1/Q.2）：本批只回傳這個欄位供前端顯示，不
        #: 影響任何同步邏輯。
        "sync_mode": dataset.sync_mode,
    }
    if full:
        d["manifest"] = dataset.manifest
    return d


def _resolve_derived_from(
    db: Database, derived_from: Optional[DatasetDerivedFromRequest]
) -> Optional[dict]:
    """`derived_from` 選填；有給時該資料集版本必須已存在，否則 400（見
    POST /datasets 與 PATCH .../card 共用這個檢查）。"""
    if derived_from is None:
        return None
    if db.get_dataset(derived_from.name, derived_from.version) is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"derived_from 指向的資料集 {derived_from.name}@{derived_from.version} "
                "尚未註冊，請先註冊該版本或移除 derived_from"
            ),
        )
    return {"name": derived_from.name, "version": derived_from.version}


def _record_to_dict(record: ExperimentRecord) -> dict:
    """`experiment_records` 一列的 API 序列化——專案詳情頁計畫第 3 節的
    `POST`/`PATCH /projects/{name}/records` 共用。"""
    return {
        "id": record.id,
        "project": record.project,
        "kind": record.kind,
        "title": record.title,
        "content": record.content,
        "author": record.author,
        "job_id": record.job_id,
        "coding_run_id": record.coding_run_id,
        "extra": record.extra,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _approval_to_dict(approval: Approval) -> dict:
    return approvals_module.approval_to_dict(approval)


def _require_identity_admin_enabled() -> None:
    """Hide every Slice 6 administration interface behind one rollback switch."""

    if app_state is None or not app_state.config.identity_admin_enabled:
        raise HTTPException(
            status_code=404, detail="identity administration is disabled"
        )


def _safe_actor_to_dict(actor: Optional[Actor]) -> Optional[dict]:
    """Serialize only actor metadata suitable for identity administration lists."""

    if actor is None:
        return None
    return {
        "id": actor.id,
        "type": actor.actor_type.value,
        "display_name": actor.display_name,
        "platform_admin": actor.platform_admin,
        "disabled_at": actor.disabled_at,
        "created_at": actor.created_at,
        "updated_at": actor.updated_at,
    }


def _service_token_to_dict(token: ServiceAccountToken) -> dict:
    """Serialize persisted token metadata without reading or exposing its hash."""

    return {
        "id": token.id,
        "service_account_actor_id": token.service_account_actor_id,
        "label": token.label,
        "scopes": list(token.scopes),
        "created_by_actor_id": token.created_by_actor_id,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
    }


def _service_account_to_dict(account: ServiceAccount, db: Database) -> dict:
    """Return a service account and revocable token metadata, never credentials."""

    return {
        "actor_id": account.actor_id,
        "name": account.name,
        "description": account.description,
        "created_by_actor_id": account.created_by_actor_id,
        "created_at": account.created_at,
        "actor": _safe_actor_to_dict(db.get_actor(account.actor_id)),
        "tokens": [
            _service_token_to_dict(token)
            for token in db.list_service_account_tokens(account.actor_id)
        ],
    }


def _project_membership_to_dict(
    membership: ProjectMembership,
    db: Database,
    *,
    project_name: Optional[str] = None,
) -> dict:
    """Return canonical project/actor role metadata with no identity secrets."""

    project = db.get_project(membership.project_id)
    return {
        "project": project_name or (project.name if project is not None else None),
        "project_id": membership.project_id,
        "actor_id": membership.actor_id,
        "role": membership.role.value,
        "created_by_actor_id": membership.created_by_actor_id,
        "created_at": membership.created_at,
        "updated_at": membership.updated_at,
        "actor": _safe_actor_to_dict(db.get_actor(membership.actor_id)),
    }


@app.get("/servers")
async def get_servers():
    return [
        _server_state_to_dict(s, app_state.db) for s in app_state.server_states.values()
    ]


@app.get(
    "/identity/service-accounts",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def list_service_accounts_endpoint():
    """List service principals and revocable token metadata without secrets."""

    return [
        _service_account_to_dict(account, app_state.db)
        for account in app_state.db.list_service_accounts()
    ]


@app.post(
    "/identity/service-accounts/request",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def request_service_account_endpoint(
    req: ServiceAccountCreateRequest, request: Request
):
    try:
        approval = approvals_module.request_service_account_create_approval(
            app_state.db,
            req.name,
            req.description,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post(
    "/identity/service-accounts/{actor_id}/tokens/request",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def request_service_token_endpoint(
    actor_id: str, req: ServiceTokenIssueRequest, request: Request
):
    try:
        approval = approvals_module.request_service_token_issue_approval(
            app_state.db,
            actor_id,
            label=req.label,
            scopes=req.scopes,
            expires_at=req.expires_at,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except IdentityTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post(
    "/identity/service-tokens/{token_id}/revoke-request",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def request_service_token_revoke_endpoint(token_id: str, request: Request):
    try:
        approval = approvals_module.request_service_token_revoke_approval(
            app_state.db,
            token_id,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except IdentityTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.get(
    "/projects/{name}/memberships",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def list_project_memberships_endpoint(name: str):
    project = app_state.db.get_project(name)
    if project is None or project.id is None:
        raise HTTPException(status_code=404, detail=f"project {name} not found")
    return [
        _project_membership_to_dict(
            membership, app_state.db, project_name=project.name
        )
        for membership in app_state.db.list_project_memberships(project_id=project.id)
    ]


@app.post(
    "/projects/{name}/memberships/request",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def request_project_membership_endpoint(
    name: str, req: ProjectMembershipRequest, request: Request
):
    try:
        approval = approvals_module.request_project_membership_upsert_approval(
            app_state.db,
            name,
            req.actor_id,
            req.role,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except IdentityTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post(
    "/projects/{name}/memberships/{actor_id}/remove-request",
    dependencies=[Depends(_require_identity_admin_enabled)],
)
async def request_project_membership_remove_endpoint(
    name: str, actor_id: str, request: Request
):
    try:
        approval = approvals_module.request_project_membership_remove_approval(
            app_state.db,
            name,
            actor_id,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except IdentityTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/projects")
async def create_project(req: ProjectCreateRequest, request: Request):
    """建立專案（階段 3）。`name`／`dataset_name`／`dataset_version` 會拿去
    拼 shell 指令（git clone 目錄名、rsync 目的地路徑），字元集限
    `[A-Za-z0-9._-]`。

    `dataset_name`／`dataset_version` 必須同時提供或同時省略（Fable 覆核
    修正 3）：只給一個的話，`make_has_dataset()` 會拿 `version=None` 去查
    `is_dataset_cached()`，結果恆為 False，配上 `pick_job()` 的資格過濾
    （修正 1），這個專案的訓練任務在自動模式下會永遠選不到機器，而使用者
    完全看不出原因（沒有清楚的錯誤訊息），所以在建立當下就擋掉。
    """
    try:
        validate_name_component(req.name, field="name")
        if req.dataset_name:
            validate_name_component(req.dataset_name, field="dataset_name")
        if req.dataset_version:
            validate_name_component(req.dataset_version, field="dataset_version")
    except InvalidNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if bool(req.dataset_name) != bool(req.dataset_version):
        raise HTTPException(
            status_code=400,
            detail="dataset_name 與 dataset_version 必須同時提供或同時省略",
        )

    try:
        app_state.db.insert_project(
            name=req.name,
            repo_or_path=req.repo_or_path,
            dataset_name=req.dataset_name,
            dataset_version=req.dataset_version,
            default_command=req.default_command,
            require_tag=req.require_tag,
            setup_cmd=req.setup_cmd,
        )
    except Exception as exc:  # noqa: BLE001 - sqlite3.IntegrityError：名稱重複
        raise HTTPException(status_code=400, detail=f"專案 {req.name} 已存在或建立失敗：{exc}") from exc

    append_audit(
        "project_created",
        {"name": req.name, "dataset_name": req.dataset_name, "dataset_version": req.dataset_version},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return _project_to_dict(app_state.db.get_project(req.name))


@app.get("/projects")
async def list_projects():
    return [_project_to_dict(p) for p in app_state.db.list_projects()]


@app.get("/projects/matrix")
async def get_projects_matrix():
    """階段 15 Phase A（PLAN.md P.1.3）：專案 × 機器矩陣。**唯讀、純 DB**，
    不對任何機器發起即時 SSH——`instances` 裡的 git 資訊是上一次
    inventory_scan／import_project 落地時寫進 `project_instances` 的快照，
    不是這次請求當下重新探測的即時狀態（要看即時狀態用既有
    `GET /projects/{name}/activity`）。

    `servers` 取自 `app_state.server_configs`（含 disabled 的機器，比照
    `GET /server-config` 的既有慣例），維持 servers.yaml 的設定順序；
    `projects` 是 `projects` 表全數，每筆帶 `instances`（依 server 分組的
    `project_instances`，只有實際登記過的機器才有 key）；
    `pending_candidates` 對每台 server 各自算一次 `status='pending'` 的
    候選數（含 0），供前端顯示「還有多少候選沒處理」並連到候選分頁。

    階段 15 Phase B（PLAN.md P.2.3）：每筆 project 另外加一個 `hub` 欄
    `{exists, head, last_sync}`（`app.hub.get_project_hub_info()`）——純
    Server A 本地檔案系統/git 查詢，不對任何工作機發起 SSH，不違反本端點
    「唯讀、不即時 SSH」的既有原則；不暴露絕對路徑。
    """
    servers = list(app_state.server_configs.keys())

    projects: list[dict] = []
    for project in app_state.db.list_projects():
        instances = {
            inst.server: {
                "path": inst.path,
                "git_remote": inst.git_remote,
                "git_branch": inst.git_branch,
                "git_commit": inst.git_commit,
                "dirty": inst.dirty,
                #: PLAN.md 2026-07-11 版 §14 切片 2/5:available/missing/
                #: dirty/diverged/unknown,由背景 reconcile 迴圈落地
                #: （app/project_instances.py）,矩陣本身不即時 SSH。
                "state": inst.state,
            }
            for inst in app_state.db.list_project_instances(project.name)
        }
        hub_info = await get_project_hub_info(
            project.name, app_state.config.local_home_dir, local_run=local_run
        )
        projects.append(
            {
                "name": project.name,
                "repo_or_path": project.repo_or_path,
                "instances": instances,
                "hub": hub_info,
            }
        )

    pending_candidates = {
        server_name: len(
            app_state.db.list_project_candidates(server=server_name, status="pending")
        )
        for server_name in servers
    }

    return {
        "servers": servers,
        "projects": projects,
        "pending_candidates": pending_candidates,
    }


@app.get("/projects/{name}/instances")
async def get_project_instances(name: str):
    """階段 8 第一批（PLAN.md I.7）：某個已註冊專案在各機器上確認過存在的
    實例列表（`project_instances`，由 import_project 核准時寫入）。"""
    return [_instance_to_dict(i) for i in app_state.db.list_project_instances(name)]


@app.get("/projects/{name}/versions")
async def get_project_versions(name: str):
    """PLAN.md 2026-07-11 版 §14 切片 4:某專案的 canonical version 歷史
    （新到舊）——由 hub_sync／project_deploy 核准時登記
    （`db.get_or_create_project_version()`）,純 DB 查詢,不 SSH。專案本身
    不存在時回空清單（同 `/instances` 既有慣例,不是 404——版本歷史本來
    就可能是空的）。"""
    return [_project_version_to_dict(v) for v in app_state.db.list_project_versions(name)]


@app.get("/projects/{name}/activity")
async def get_project_activity(name: str, request: Request):
    """階段 11（PLAN.md L 節）：某個已註冊專案的完整執行近況——`project` 基本
    資料＋`project_instances`（server/path/git）＋各機當下 GPU/磁碟（現成
    `server_states`）＋該專案最近 10 筆 jobs（狀態/exit_code/耗時）＋最新一
    筆 job 的 `log_tail`＋對每個在線 instance 的唯讀 SSH 探測（近期變動檔案
    ＋至多 3 個 log 檔尾段，見 `app/activity.py`）。

    - 專案不存在 → 404。
    - 專案存在但沒有已登記的 `project_instances`（沒 import 過任何機器，或
      import 時沒有掃到任何實例）→ **不是** 404（專案本身確實存在），
      `activity` 欄位改回一段明確訊息，說明沒有已登記的機器/路徑可以探測。
    - 離線的 instance 直接跳過探測（`{"skipped": "offline"}`），不嘗試 SSH。
    - 唯讀直接執行、不走核准流程——先例是 `GET /jobs/{id}/log` 的即時 SSH
      tail（PLAN.md L.2：「唯讀直接執行不走核准」）。
    - 每次呼叫都寫稽核 `project_activity`（`project`、實際被探測
      〔在線且真的發了 SSH〕的 `probed` 機器列表）。
    """
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    instances = app_state.db.list_project_instances(name)

    jobs = app_state.db.list_jobs(project=name)
    recent_jobs = [_job_activity_summary(j) for j in jobs[-10:]]
    latest_job_log_tail = jobs[-1].log_tail if jobs else None

    server_names = sorted({i.server for i in instances})
    server_states_summary = {
        s: _activity_server_state_summary(app_state.server_states.get(s)) for s in server_names
    }

    activity: Any
    probed_servers: list[str] = []
    if not instances:
        activity = (
            f"專案 {name} 沒有已登記的機器/路徑（project_instances 為空），"
            "無法探測執行近況——請先透過 inventory scan／匯入流程登記至少一台機器。"
        )
    else:
        activity = []
        for inst in instances:
            state = app_state.server_states.get(inst.server)
            if state is None or not state.online:
                activity.append(
                    {
                        "instance_id": inst.id,
                        "server": inst.server,
                        "path": inst.path,
                        "skipped": "offline",
                    }
                )
                continue
            server_cfg = app_state.server_configs.get(inst.server)
            exclude_names = server_cfg.project_exclude_names if server_cfg else None
            probe_result = await probe_instance(
                app_state.ssh_run, inst.server, inst.path, exclude_names
            )
            activity.append(
                {
                    "instance_id": inst.id,
                    "server": inst.server,
                    "path": inst.path,
                    **probe_result,
                }
            )
            probed_servers.append(inst.server)

    append_audit(
        "project_activity",
        {"project": name, "probed": probed_servers},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )

    return {
        "project": _project_to_dict(project),
        "instances": [_instance_to_dict(i) for i in instances],
        "server_states": server_states_summary,
        "recent_jobs": recent_jobs,
        "latest_job_log_tail": latest_job_log_tail,
        "activity": activity,
    }


# ---------------------------------------------------------------------------
# 專案詳情頁（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
# 方法／進度管理」計畫）：六支端點——GET detail（純 DB＋本地查詢，不 SSH，
# 刻意跟 `/activity` 分開，見 `get_project_detail()` docstring）、
# PATCH 目標／方法／進度／摘要、GET timeline（`app/records.py` 動態合併）、
# POST/PATCH/DELETE 手動實驗紀錄。全部端點自動被既有 auth_middleware 涵蓋。
# ---------------------------------------------------------------------------


@app.get("/projects/{name}/detail")
async def get_project_detail(name: str):
    """專案詳情頁的頂部聚合資料：`{project, instances, server_states, hub}`。

    **純 DB＋本地查詢、不對任何機器發起 SSH**——刻意不用既有
    `GET /projects/{name}/activity` 當詳情頁的載入路徑：那支端點每次呼叫
    都會對每個在線 instance 發起即時 SSH 探測＋寫稽核 `project_activity`
    （PLAN.md L 節既有設計），詳情頁一開就打一次太重；詳情頁另外提供
    「探測執行近況」按鈕，使用者要看即時狀態時手動呼叫既有 `/activity`。

    `server_states` 沿用 `_activity_server_state_summary()`（同
    `/activity` 的精簡摘要：online/gpu_util_max/disk_avail_bytes）；`hub`
    沿用 `GET /projects/matrix` 的 `get_project_hub_info()`（純 Server A
    本地檔案系統/git 查詢，不對工作機發起 SSH）。專案不存在 -> 404。

    PLAN.md 2026-07-11 版 §14 切片 5:`instances` 序列化（`_instance_to_
    dict()`）已含切片 1/2 的 `project_id`/`state`（available/missing/
    dirty/diverged/unknown,背景 reconcile 落地,這裡不重新探測）；另外加
    `versions`——`app.db.list_project_versions()` 的 canonical version
    歷史（新到舊,切片 4）。兩者合起來就是 §14 結尾的第一個里程碑：一頁看到
    所有機器的 instance 現況與中央版本差異。
    """
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    instances = app_state.db.list_project_instances(name)
    server_names = sorted({i.server for i in instances})
    server_states_summary = {
        s: _activity_server_state_summary(app_state.server_states.get(s)) for s in server_names
    }
    hub_info = await get_project_hub_info(
        name, app_state.config.local_home_dir, local_run=local_run
    )
    versions = app_state.db.list_project_versions(name)

    return {
        "project": _project_to_dict(project),
        "instances": [_instance_to_dict(i) for i in instances],
        "server_states": server_states_summary,
        "hub": hub_info,
        "versions": [_project_version_to_dict(v) for v in versions],
    }


@app.patch("/projects/{name}")
async def patch_project_endpoint(name: str, req: ProjectPatchRequest, request: Request):
    """更新專案的目標／優化方法／目前進度／摘要（自由文字 Markdown，
    專案詳情頁計畫第 3 節）。`exclude_unset` 只更新請求 body 裡明確帶到的
    欄位——省略的欄位維持原值不動（跟局部編輯每個文件卡的前端互動對齊：
    使用者一次只編輯一張卡，不該連帶動到其他兩張卡的內容）；沒有帶任何
    欄位（空 body）-> 400，不做沒有意義的 no-op 更新。稽核只記欄位名，不
    記全文（避免大段 Markdown 灌爆 audit.jsonl）。專案不存在 -> 404。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="body 至少要帶一個欄位")

    app_state.db.update_project(name, **fields)
    append_audit(
        "project_updated",
        {"name": name, "fields": sorted(fields.keys())},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return _project_to_dict(app_state.db.get_project(name))


@app.get("/projects/{name}/timeline")
async def get_project_timeline_endpoint(
    name: str,
    q: Optional[str] = None,
    limit: int = 20,
    before_ts: Optional[str] = None,
    kinds: Optional[str] = None,
):
    """實驗紀錄時間軸——`experiment_records`／`jobs`／`coding_runs` 三源
    動態合併（見 `app.records.build_timeline()`）。前端搜尋、載入更多、
    agent 查找全部走這支。`kinds` 是逗號分隔字串（querystring 慣例，比照
    既有其他端點沒有用重複參數表示列表）；`limit` 夾在 `[1, 50]`
    （`build_timeline()` 內部防禦性再夾一次）。專案不存在 -> 404。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    return build_timeline(
        app_state.db, name, q=q, limit=limit, before_ts=before_ts, kinds=kinds_list
    )


@app.post("/projects/{name}/records")
async def create_experiment_record_endpoint(
    name: str, req: ExperimentRecordCreateRequest, request: Request
):
    """建立一筆手動實驗紀錄。**直接執行、不走核准**——先例是資料集資料卡
    的 `PATCH /datasets/{name}/{version}/card`（main.py 既有端點）：純 DB
    文字寫入，不觸發任何機器動作、可逆（能刪能改），不需要人工核准這一層
    保護。`author` 選填，省略時是 `"user"`（網頁呼叫的既有行為）；本地
    vLLM 走 `app/agent_tools.py` 的 `add_experiment_record` 工具，那個工具
    完全不經過這支 HTTP 端點（直接寫 DB，強制 `author="agent"`）；
    `app/mcp_bridge.py`（ChatGPT）的同名代理工具會呼叫這支端點並帶
    `author="agent:chatgpt"`（見 `ExperimentRecordCreateRequest` docstring）
    ——`author` 不合法（非 `"user"` 也不是 `"agent"` 開頭）／`kind` 不合法
    -> 400；專案不存在 -> 404。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")
    if req.kind not in VALID_RECORD_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind 必須是 {sorted(VALID_RECORD_KINDS)} 其中之一",
        )
    author = req.author or "user"
    if author != "user" and not author.startswith("agent"):
        raise HTTPException(status_code=400, detail="author 必須是 user 或以 agent 開頭")

    record_id = app_state.db.insert_experiment_record(
        project=name,
        content=req.content,
        kind=req.kind,
        title=req.title,
        author=author,
        job_id=req.job_id,
        coding_run_id=req.coding_run_id,
    )
    append_audit(
        "experiment_record_created",
        {
            "project": name,
            "record_id": record_id,
            "kind": req.kind,
            "author": author,
            "job_id": req.job_id,
            "coding_run_id": req.coding_run_id,
        },
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return _record_to_dict(app_state.db.get_experiment_record(record_id))


@app.patch("/projects/{name}/records/{record_id}")
async def patch_experiment_record_endpoint(
    name: str, record_id: int, req: ExperimentRecordPatchRequest, request: Request
):
    """更新一筆手動實驗紀錄的 title/content/kind。`exclude_unset` 只更新
    有帶的欄位，空 body -> 400。紀錄不存在或不屬於這個專案 -> 404
    （避免用別的專案底下的 record id 改到這裡，即使 id 猜對了）。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    record = app_state.db.get_experiment_record(record_id)
    if record is None or record.project != name:
        raise HTTPException(status_code=404, detail=f"紀錄 {record_id} 不存在")

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="body 至少要帶一個欄位")
    if "kind" in fields and fields["kind"] not in VALID_RECORD_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind 必須是 {sorted(VALID_RECORD_KINDS)} 其中之一",
        )

    app_state.db.update_experiment_record(record_id, **fields)
    append_audit(
        "experiment_record_updated",
        {"project": name, "record_id": record_id, "fields": sorted(fields.keys())},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return _record_to_dict(app_state.db.get_experiment_record(record_id))


@app.delete("/projects/{name}/records/{record_id}")
async def delete_experiment_record_endpoint(
    name: str, record_id: int, request: Request
):
    """刪除一筆手動實驗紀錄。紀錄不存在或不屬於這個專案 -> 404。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    record = app_state.db.get_experiment_record(record_id)
    if record is None or record.project != name:
        raise HTTPException(status_code=404, detail=f"紀錄 {record_id} 不存在")

    app_state.db.delete_experiment_record(record_id)
    append_audit(
        "experiment_record_deleted",
        {"project": name, "record_id": record_id},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"ok": True, "id": record_id}


# ---------------------------------------------------------------------------
# 階段 12（PLAN.md M 節）：AI 改碼層次一——讀檔（唯讀直接執行，不走核准，
# 先例同 `GET /jobs/{id}/log`／`GET /projects/{name}/activity`）＋
# apply-patch-request（只建立 kind=apply_patch 的核准請求，真正的 SSH／
# git 操作發生在 `POST /approve/{id}`，見 `app/approvals.py` 的 `approve()`）。
# ---------------------------------------------------------------------------


@app.get("/projects/{name}/files")
async def list_project_files_endpoint(
    name: str,
    request: Request,
    server: Optional[str] = None,
    subdir: Optional[str] = None,
):
    """列出某個已註冊專案在指定機器（省略且剛好一個 instance 時自動選；
    多個 instance 存在時必須指定 `server`，否則 400）上的檔案（相對路徑，
    秘密檔已過濾、至多 200 筆）。`subdir` 選填，是相對 instance 路徑的子
    目錄——含 `..`/絕對路徑一律 400，不嘗試解析。唯讀直接執行，不走核准
    流程；每次呼叫寫稽核 `project_file_list`。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    try:
        instance = resolve_project_instance(app_state.db, name, server)
    except ProjectInstanceResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target_path = instance.path
    if subdir:
        err = validate_rel_path(subdir)
        if err is not None:
            raise HTTPException(status_code=400, detail=err)
        target_path = f"{instance.path.rstrip('/')}/{subdir}"

    server_cfg = app_state.server_configs.get(instance.server)
    exclude_names = server_cfg.project_exclude_names if server_cfg else None
    result = await list_instance_files(
        app_state.ssh_run, instance.server, target_path, exclude_names
    )

    append_audit(
        "project_file_list",
        {"project": name, "server": instance.server, "subdir": subdir},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"project": name, "server": instance.server, "path": target_path, **result}


@app.get("/projects/{name}/file")
async def read_project_file_endpoint(
    name: str, path: str, request: Request, server: Optional[str] = None
):
    """讀取某個已註冊專案在指定機器上的單一檔案內容（前 64KB）。`path` 是
    相對 instance 路徑的相對路徑——含 `..`/絕對路徑/秘密檔名一律 400，不
    嘗試 SSH。唯讀直接執行，不走核准流程；每次呼叫寫稽核
    `project_file_read`。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    try:
        instance = resolve_project_instance(app_state.db, name, server)
    except ProjectInstanceResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    err = validate_rel_path(path)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)

    result = await read_instance_file(app_state.ssh_run, instance.server, instance.path, path)

    append_audit(
        "project_file_read",
        {"project": name, "server": instance.server, "path": path},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"project": name, "server": instance.server, "path": path, **result}


@app.post("/projects/{name}/apply-patch-request")
async def apply_patch_request_endpoint(
    name: str, req: ApplyPatchRequest, request: Request
):
    """建立 kind=apply_patch 的核准請求，不真的套用任何改動（真正的
    `git apply`／commit 發生在 `POST /approve/{id}`，見 `app/approvals.py`
    的 `approve()` 的 apply_patch 分支）。diff 格式/大小/目標路徑不合法，
    或 project/server 沒有對應的已登記 instance → 400，不建立 approval。"""
    try:
        approval = approvals_module.request_apply_patch_approval(
            app_state.db,
            name,
            req.server,
            req.diff,
            description=req.description,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidApplyPatchRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/projects/{name}/coding-task-request")
async def coding_task_request_endpoint(
    name: str, req: CodingTaskRequest, request: Request
):
    """建立 kind=coding_task 的核准請求，不真的派工（真正建立 coding_run／
    寫 instruction.txt／enqueue 一個 type="coding" 任務發生在
    `POST /approve/{id}`，見 `app/approvals.py` 的 `approve()` 的
    coding_task 分支）。階段 13（PLAN.md N.2，Codex Worker v2）：Codex 執行
    機器永遠是 `.env` 的 `CODEX_RUNNER_SERVER`，`req.server`（若有帶）只是
    v1 舊客戶端相容參數，轉成 `legacy_server` 傳給
    `request_coding_task_approval()`。Codex 功能未設定／instruction
    空白/過長／base_branch 格式不合法／validation_target 不是已啟用的
    server／project 不存在／`server` 與 Runner 不符／N.3 三段式判定找不到
    可用的 repo 來源 → 400，不建立 approval。"""
    try:
        approval = approvals_module.request_coding_task_approval(
            app_state.db,
            name,
            req.instruction,
            base_branch=req.base_branch,
            validation_target=req.validation_target,
            config=app_state.config,
            server_enabled={s.name: s.enabled for s in app_state.server_configs.values()},
            legacy_server=req.server,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidCodingTaskRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


# ---------------------------------------------------------------------------
# 階段 15 Phase B（PLAN.md P.2 節）：git 化＋中央 hub＋刪除專案登記。
# ---------------------------------------------------------------------------


@app.post("/projects/{name}/git-init-request")
async def git_init_request_endpoint(name: str, req: GitInitRequest, request: Request):
    """建立 kind=git_init 的核准請求，不真的動任何檔案（真正的
    `.gitignore`／`git init`／`git add -A`／size guard／commit 發生在
    `POST /approve/{id}`，見 `app/approvals.py` 的 `approve()` 的 git_init
    分支）。專案不存在 → 404；instance 不存在（該 server 沒登記這個專案）
    ／該 instance 已經是 git repo／`extra_ignores` 含不合法字元 → 400，
    不建立 approval。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")
    try:
        approval = await approvals_module.request_git_init_approval(
            app_state.db,
            name,
            req.server,
            req.extra_ignores,
            ssh_run=app_state.ssh_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidGitInitRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/projects/{name}/hub-sync")
async def hub_sync_endpoint(name: str, req: HubSyncRequest, request: Request):
    """階段 15 Phase B（PLAN.md P.2.2）：直接執行的 web 動作＋稽核，**不出
    核准卡**（見 `app.hub.sync_project_to_hub()` 模組/函式 docstring 的風險
    說明）。instance 不存在 → 404；該 instance 不是 git repo → 400。"""
    try:
        result = await sync_project_to_hub(
            app_state.db,
            name,
            req.server,
            ssh_run=app_state.ssh_run,
            local_run=local_run,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ProjectInstanceResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HubSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/projects/{name}/deploy-request")
async def project_deploy_request_endpoint(
    name: str, req: ProjectDeployRequest, request: Request
):
    """階段 15 Phase C（PLAN.md P.3）：建立 kind=project_deploy 的核准請求，
    不真的動任何檔案（真正的 bundle 建立／rsync 推送／目標機 clone 發生在
    `POST /approve/{id}`，見 `app.approvals.approve()` 的 project_deploy
    分支）。**來源固定是 Server A 中央 hub**——尚未對該專案執行過
    `hub-sync` → 400。專案不存在 → 404；target 不存在/disabled、target
    已有 instance、ref/dest_path 不合法等 → 400，不建立 approval。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")
    try:
        approval = await request_project_deploy_approval(
            app_state.db,
            name,
            req.target_server,
            req.dest_path,
            req.ref,
            config=app_state.config,
            server_configs=app_state.server_configs,
            ssh_run=app_state.ssh_run,
            local_run=local_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidProjectDeployRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.delete("/projects/{name}")
async def delete_project_endpoint(name: str, request: Request):
    """階段 15 Phase B（PLAN.md P.2.4）：直接執行＋稽核 `project_deleted`。
    **只刪除 DB 列**（`projects`＋該專案所有 `project_instances`），絕不動
    任何機器上的檔案——不對任何機器發起 SSH（`app.db.Database.
    delete_project()` 是純 DB 操作）；`project_candidates` 的 `imported`
    狀態不回溯。專案不存在 → 404；有 queued/running 的 job 引用這個專案
    → 409，不刪除。"""
    project = app_state.db.get_project(name)
    if project is None:
        raise HTTPException(status_code=404, detail=f"專案 {name} 不存在")

    blocking_job_ids = sorted(
        j.id
        for j in app_state.db.list_jobs(status="queued") + app_state.db.list_jobs(status="running")
        if j.project == name
    )
    if blocking_job_ids:
        raise HTTPException(
            status_code=409,
            detail=f"專案 {name} 仍有 queued/running 任務引用（{blocking_job_ids}），無法刪除",
        )

    instance_count = len(app_state.db.list_project_instances(name))
    app_state.db.delete_project(name)
    append_audit(
        "project_deleted",
        {"name": name, "instance_count": instance_count},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# 階段 13（PLAN.md N.6）：三個唯讀端點（GET /codex-runner/status、
# GET /coding-runs、GET /coding-runs/{id}）＋一個 cleanup 端點（POST
# /coding-runs/{id}/cleanup，PLAN.md N.9 鐵律 11）。全部端點自動被既有
# auth_middleware 涵蓋。
# ---------------------------------------------------------------------------

#: `GET /coding-runs`／`GET /coding-runs/{id}` 序列化白名單（PLAN.md N.6：
#: 「API／MCP 回應不暴露 worktree 任意絕對路徑」）——刻意排除
#: `worktree_path`/`bundle_path`，只回一個 `has_bundle` 布林；有需要看實際
#: 內容一律走 `final_message`/`diff_patch`（只有 `GET /coding-runs/{id}`
#: 附，讀本地已回收的檔案，見下方 `_read_local_coding_result_file()`）。
def _coding_run_to_dict(run: CodingRun) -> dict:
    return {
        "id": run.id,
        "approval_id": run.approval_id,
        "job_id": run.job_id,
        "project": run.project,
        "runner_server": run.runner_server,
        "instruction": run.instruction,
        "base_branch": run.base_branch,
        "base_commit": run.base_commit,
        "result_branch": run.result_branch,
        "result_commit": run.result_commit,
        "has_bundle": bool(run.bundle_path),
        "validation_target": run.validation_target,
        "codex_version": run.codex_version,
        "status": run.status,
        "test_command": run.test_command,
        "test_exit_code": run.test_exit_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_message": run.error_message,
    }


#: `final_message`/`diff_patch` 截斷上限（PLAN.md N.6：「≤64KB 截斷」）。
_CODING_RUN_FILE_MAX_CHARS = 65536


def _read_local_coding_result_file(job_id: Optional[int], filename: str) -> Optional[str]:
    """讀本地已由既有 E 節結果回收拉回的 `results/{job_id}/{filename}`
    （`final_message.txt`／`diff.patch`）。`job_id` 是 None（coding_run 還
    沒回填 job_id，理論上不會發生但防禦性處理）、檔案不存在、或任何讀取
    錯誤都回傳 `None`，不丟例外——這是輔助顯示用的附加內容，缺失不代表
    coding_run 本身有問題。超過 `_CODING_RUN_FILE_MAX_CHARS` 截斷並附註記。
    """
    if job_id is None:
        return None
    path = Path(local_result_dir(job_id, app_state.config.local_home_dir)) / filename
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > _CODING_RUN_FILE_MAX_CHARS:
        return text[:_CODING_RUN_FILE_MAX_CHARS] + "\n…（已截斷，超過 64KB）"
    return text


@app.get("/codex-runner/status")
async def codex_runner_status_endpoint():
    """`GET /codex-runner/status`（PLAN.md N.6）：`CODEX_RUNNER_SERVER`
    未設定只回 `{"configured": false}`；有設定時回完整 shape（見
    `AppState.get_codex_runner_status()`）。**絕不回傳 `codex login
    status` 的原始輸出、帳號 email、token**（PLAN.md N.9 鐵律 7）。"""
    return await app_state.get_codex_runner_status()


@app.get("/coding-runs")
async def list_coding_runs_endpoint(
    status: Optional[str] = None, project: Optional[str] = None, limit: int = 50
):
    runs = app_state.db.list_coding_runs(status=status, project=project, limit=limit)
    return [_coding_run_to_dict(r) for r in runs]


@app.get("/coding-runs/{coding_run_id}")
async def get_coding_run_endpoint(coding_run_id: int):
    run = app_state.db.get_coding_run(coding_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"coding_run {coding_run_id} 不存在")
    data = _coding_run_to_dict(run)
    data["final_message"] = _read_local_coding_result_file(run.job_id, "final_message.txt")
    data["diff_patch"] = _read_local_coding_result_file(run.job_id, "diff.patch")
    return data


@app.post("/coding-runs/{coding_run_id}/cleanup")
async def cleanup_coding_run_endpoint(coding_run_id: int, request: Request):
    """`POST /coding-runs/{id}/cleanup`（PLAN.md N.9 鐵律 11，web 觸發＋
    稽核；**不給 MCP**，見 `app/mcp_bridge.py` 沒有對應工具）。真正的驗證
    與 SSH 動作在 `app.approvals.cleanup_coding_run()`（見該函式
    docstring）。"""
    try:
        result = await approvals_module.cleanup_coding_run(
            app_state.db,
            coding_run_id,
            ssh_run=app_state.ssh_run,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CodingRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CodingRunNotCleanableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# 階段 8 第一批：Project Inventory（POST /inventory/scan、GET/POST
# /inventory/candidates/*）。全部端點自動被既有 auth_middleware 涵蓋。
# ---------------------------------------------------------------------------


@app.post("/inventory/scan")
async def inventory_scan_endpoint(req: InventoryScanRequest, request: Request):
    """建立 kind=inventory_scan 的核准請求，不真的掃描（真正的 SSH 發生在
    `POST /approve/{id}`）。階段 8 第二批：`project_roots` 省略時從該機器
    `servers.yaml` 設定自動代入；`server="all"` 對每一台已啟用的機器各自
    建立一筆獨立 approval，回傳 `{"approvals": [...]}`（單一 server 仍回傳
    單一 approval dict）。`project_roots` 命中禁止掃描的路徑 → 400，不建立
    approval。"""
    try:
        result = approvals_module.request_inventory_scan_approval(
            app_state.db,
            req.server,
            req.project_roots,
            server_configs=app_state.server_configs,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ForbiddenScanRootError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(result, list):
        return {"approvals": [_approval_to_dict(a) for a in result]}
    return _approval_to_dict(result)


@app.get("/inventory/candidates")
async def list_inventory_candidates(
    server: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None
):
    return [
        _candidate_to_dict(c)
        for c in app_state.db.list_project_candidates(server=server, status=status, q=q)
    ]


def _known_projects_for_link_suggestions() -> list[tuple[str, Optional[str], list]]:
    """切片 3:`find_link_suggestions()` 要比對的既有專案清單——每個專案帶
    `repo_or_path`（可能就是 git remote）與各 instance 的 `git_remote`,
    純 DB 讀取,不對任何機器 SSH。"""
    known: list[tuple[str, Optional[str], list]] = []
    for project in app_state.db.list_projects():
        remotes = [project.repo_or_path] + [
            i.git_remote for i in app_state.db.list_project_instances(project.name)
        ]
        known.append((project.name, project.id, remotes))
    return known


@app.get("/inventory/candidates/{candidate_id}")
async def get_inventory_candidate(candidate_id: str):
    """切片 3(PLAN.md §14):回傳附帶 `link_suggestions`——依 normalized
    git remote 比對出的「可能是同一專案」提示（`app.inventory.
    find_link_suggestions()`,只提示不自動合併,INV-PROJECT-4 草案）,
    供前端在匯入前提示使用者改走「連結既有 Project」。"""
    candidate = app_state.db.get_project_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    suggestions = find_link_suggestions(
        candidate.git_remote, _known_projects_for_link_suggestions()
    )
    return {**_candidate_to_dict(candidate), "link_suggestions": suggestions}


@app.post("/inventory/candidates/{candidate_id}/import-request")
async def import_candidate_request(
    candidate_id: str, req: ImportProjectCandidateRequest, request: Request
):
    """建立 kind=import_project 的核准請求，不真的匯入（真正寫
    projects/project_instances 發生在 `POST /approve/{id}`）。"""
    overrides = req.model_dump(exclude_none=True)
    try:
        approval = approvals_module.request_import_project_approval(
            app_state.db,
            candidate_id,
            overrides,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateNotPendingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/inventory/candidates/{candidate_id}/ignore-request")
async def ignore_candidate_request(candidate_id: str, request: Request):
    """建立 kind=ignore_project_candidate 的核准請求，不真的改狀態。"""
    try:
        approval = approvals_module.request_ignore_project_candidate_approval(
            app_state.db,
            candidate_id,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateNotPendingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/inventory/candidates/manual")
async def add_manual_candidate(req: ManualCandidateRequest, request: Request):
    """P.1.5（PLAN.md，2026-07-10 追加，Fable 定案）：手動新增候選——背景是
    像 Controlnet 這種「workspace 型」專案（頂層無任何 marker、掃描器認不出
    來）。**不走核准流**，直接建立 `status=pending` 候選（等同掃描產出的
    策展輸入）；專案/instance 的真正建立仍走既有的 `import_project` 核准流。
    詳細驗證順序見 `app.approvals.add_manual_candidate()` docstring。"""
    try:
        candidate = await approvals_module.add_manual_candidate(
            app_state.db,
            req.server,
            req.path,
            req.name,
            server_configs=app_state.server_configs,
            ssh_run=app_state.ssh_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ManualCandidateDuplicateError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "id": exc.existing.id,
                "status": exc.existing.status,
            },
        ) from exc
    except (ManualCandidateServerInvalidError, ManualCandidatePathInvalidError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InvalidNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _candidate_to_dict(candidate)


@app.post("/inventory/candidates/ignore-nested-request")
async def ignore_nested_candidates_request(request: Request):
    """階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）：建立
    kind=ignore_nested_candidates 的核准請求，不真的改狀態——批次把「路徑
    位於其他非 ignored 候選之下」的所有 pending 候選收進**一張**核准卡，
    核准當下才一次全部標 ignored（見
    `app.approvals.request_ignore_nested_candidates_approval()`／
    `approve()` 的對應分支）。找不到任何可清理的巢狀候選 → 400，不建立
    空的 approval。"""
    try:
        approval = approvals_module.request_ignore_nested_candidates_approval(
            app_state.db,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except NoNestedCandidatesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


# ---------------------------------------------------------------------------
# 階段 8 第二批：Web Server Management（PLAN.md I.4/I.7）。全部端點自動被
# 既有 auth_middleware 涵蓋。**鐵律**：這裡沒有任何一個端點會直接寫
# servers.yaml——add/update/disable/delete 一律只建立 approval，真正落地
# （atomic write）發生在 `POST /approve/{id}`（見 app/approvals.py 的
# `approve()`）。`GET /server-config*` 與 `POST /server-config/test-ssh`
# 都不讀取、不回傳私鑰檔案內容。
# ---------------------------------------------------------------------------


@app.get("/server-config")
async def list_server_config_endpoint():
    return [server_config_to_safe_dict(cfg) for cfg in app_state.server_configs.values()]


@app.get("/server-config/{name}")
async def get_server_config_endpoint(name: str):
    cfg = app_state.server_configs.get(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"server {name} 不存在")
    return server_config_to_safe_dict(cfg)


@app.post("/server-config/test-ssh")
async def test_server_ssh_endpoint(req: ServerConfigPayload, request: Request):
    """唯讀直接執行（不建 approval，見使用者規格）：body 是一組完整的
    server 設定，用來測試「還沒加入 servers.yaml 的機器」，不是查現有
    name。只跑固定六類唯讀指令（見 `app.server_config.test_ssh_connection()`
    docstring），**不寫遠端檔案、不建 agent_jobs、不 kill tmux、不改
    servers.yaml**。

    **Bug 修正**（原本會用 `payload.name` 查 `app_state.server_configs`，
    完全無視 payload 帶的 host/port/user/key，導致測試「還沒加入
    servers.yaml 的機器」直接 KeyError、測試「已改但還沒核准的設定」測到
    的是舊值）：這裡改用一個綁定 `server_cfg`（payload 組出來的那個）的
    closure 直接呼叫 `app_state.ssh_pool.run()`，`_direct_run` 的
    `_name` 參數完全不使用（`test_ssh_connection()` 介面不變，仍然把
    name 傳回來，這裡就是刻意忽略它）。

    連線測試之前先跑 `validate_server_config()`：有 errors（例如
    host=0.0.0.0）→ 不做任何 SSH 呼叫，直接回 `ok=False` 給前端顯示在表單
    旁邊（HTTP 200，不是 400——這是唯讀測試，不是要不要建立/落地的判斷）；
    只有 warnings（例如 key 檔案權限過寬）→ 照常測試，warnings 併入回傳。
    """
    payload = req.model_dump()
    server_cfg = ServerConfig(
        name=payload["name"],
        host=payload["host"],
        user=payload["user"],
        key=payload["key"],
        gpu=payload["gpu"],
        idle_gpu_util=payload["idle_gpu_util"],
        idle_load=payload["idle_load"],
        tags=list(payload["tags"]),
        port=payload["port"],
        project_roots=list(payload["project_roots"]),
        dataset_roots=list(payload["dataset_roots"]),
    )

    _ok, errors, warnings = validate_server_config(payload, app_state.config)
    if errors:
        # 驗證沒過：不發生任何 SSH 呼叫，也不寫稽核（沒有實際測試發生）。
        return {"ok": False, "results": {}, "warnings": warnings, "errors": errors}

    async def _direct_run(_name: str, command: str, timeout: float):
        return await app_state.ssh_pool.run(server_cfg, command, timeout)

    result = await test_ssh_connection(server_cfg, _direct_run)
    result["warnings"] = list(result.get("warnings") or []) + warnings
    append_audit(
        "server_test_ssh",
        {"name": server_cfg.name, "host": server_cfg.host, "ok": result["ok"]},
        result="ok" if result["ok"] else "failed",
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return result


@app.post("/server-config/add-request")
async def server_add_request_endpoint(req: ServerConfigPayload, request: Request):
    """建立 kind=server_add 的核准請求，不真的寫 servers.yaml（真正的
    atomic write 發生在 `POST /approve/{id}`）。不合法的設定（見
    `app.server_config.validate_server_config()`）直接 400，不建立
    approval。"""
    payload = req.model_dump(exclude_none=True)
    try:
        approval = approvals_module.request_server_add_approval(
            app_state.db,
            payload,
            app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidServerConfigError as exc:
        raise HTTPException(status_code=400, detail="；".join(exc.errors)) from exc
    return _approval_to_dict(approval)


@app.post("/server-config/update-request")
async def server_update_request_endpoint(req: ServerUpdateRequest, request: Request):
    """建立 kind=server_update 的核准請求。`updates` 內含 `name` 且與現有
    `name` 不同 → 400（不支援 rename）。"""
    current_servers = load_servers_config(app_state.config.servers_yaml_path).get("servers") or []
    try:
        approval = approvals_module.request_server_update_approval(
            app_state.db,
            req.name,
            req.updates,
            app_state.config,
            current_servers,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ServerRenameNotSupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ServerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidServerConfigError as exc:
        raise HTTPException(status_code=400, detail="；".join(exc.errors)) from exc
    return _approval_to_dict(approval)


@app.post("/server-config/disable-request")
async def server_disable_request_endpoint(req: ServerNameRequest, request: Request):
    """建立 kind=server_disable 的核准請求。建立請求當下只檢查 server 是否
    存在，**不擋 running job**——那是核准當下的責任（見
    `app.approvals.approve()` 的 server_disable 分支）。"""
    current_names = list(app_state.server_configs.keys())
    try:
        approval = approvals_module.request_server_disable_approval(
            app_state.db,
            req.name,
            current_names,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ServerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/server-config/delete-request")
async def server_delete_request_endpoint(req: ServerNameRequest, request: Request):
    """建立 kind=server_delete 的核准請求。第一版核准後只做
    `enabled=false`（不做真刪除），見
    `app.approvals.approve()` 的 server_delete 分支。"""
    current_names = list(app_state.server_configs.keys())
    try:
        approval = approvals_module.request_server_delete_approval(
            app_state.db,
            req.name,
            current_names,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ServerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/server-config/reload")
async def server_config_reload_endpoint():
    """重新讀取 servers.yaml，替換 in-memory 的
    `app_state.server_configs`/`server_states`（不需要重啟服務）。

    階段 13（PLAN.md N.1，批次 1 遺留）：重載成功後重跑一次
    `apply_codex_config_rules()`——`CODEX_RUNNER_SERVER` 指向的機器有可能
    在這次 reload 被改成 `enabled=false`（或整個從 servers.yaml 消失，理論
    上不會發生但仍防禦性處理），需要重新驗證。**取捨（回應 main session
    的取捨提示）**：`lifespan()` 啟動時的硬失敗語意（設了 Runner 就要設
    對，錯了直接讓服務起不來）只適用於「啟動那一刻」；服務已經在運行中
    時，同一個 `ValueError` 改成**只 log error、不中斷這個請求**——reload
    本身（讀 servers.yaml、換掉 in-memory dict）已經成功，不能因為
    Runner 設定現在不合法就讓整個 `POST /server-config/reload` 回傳失敗、
    甚至讓服務跟著炸掉；不合法的後果是 Codex 功能繼續用著已經過期的驗證
    結果（下一次 `GET /codex-runner/status`／coding_task 請求仍然會用當下
    的 `config`/`server_enabled` 各自驗證一次，不依賴這裡的重跑結果），
    只是操作者需要靠 log 才能發現「reload 之後 Runner 設定壞了」，不像
    啟動失敗那樣立刻可見——這是「啟動時硬失敗、運行中軟警告」的刻意取捨
    （不對稱處理，服務中途不能因為 reload 就被一個設定問題撂倒）。
    warnings（例如 chatgpt 模式 concurrency 降級）照常逐條 log，跟
    `lifespan()` 的既有慣例一致。
    """
    result = reload_server_config_if_supported(app_state)
    try:
        for warning in apply_codex_config_rules(
            app_state.config,
            {s.name: s.enabled for s in app_state.config.servers},
        ):
            logger.warning(warning)
    except ValueError as exc:  # noqa: BLE001 - 運行中軟警告，不中斷這個請求
        logger.error("reload 後重新驗證 CODEX_RUNNER_SERVER 設定失敗: %s", exc)
    return result


@app.post("/datasets")
async def create_dataset(req: DatasetCreateRequest, request: Request):
    """註冊資料集（階段 3；階段 16 起 `description`／`method` 強制必填，
    PLAN.md Q.3——**刻意的 breaking change**，使用者定的規則：「資料集
    必須明確記錄製作方式與數量」，登記時沒寫就 400，可事後 PATCH
    `/datasets/{name}/{version}/card` 補登）：從 `source_path`（Server A
    上的本機路徑）掃描產生 manifest（檔案清單＋各檔大小，代替全量 hash，
    見 README 取捨說明）。同一個 (name, version) 只能註冊一次——版本是
    一級概念，要更新內容請註冊新版本。`derived_from` 選填，有給時該資料
    集版本必須已存在，否則 400。`counts` 選填，值限 str/int/float。掃描
    是阻塞的檔案系統操作，用 `asyncio.to_thread()` 避免卡住 event loop。
    """
    try:
        validate_name_component(req.name, field="name")
        validate_name_component(req.version, field="version")
    except InvalidNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        validate_card_fields(req.description, req.method)
    except InvalidDatasetCardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    derived_from_dict = _resolve_derived_from(app_state.db, req.derived_from)

    try:
        card = build_dataset_card(req.description, req.method, derived_from_dict, req.counts)
    except InvalidDatasetCardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        manifest = await asyncio.to_thread(build_manifest, req.source_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        app_state.db.insert_dataset(
            name=req.name,
            version=req.version,
            size_bytes=manifest["total_size"],
            source_path=req.source_path,
            manifest=manifest,
            card=card,
        )
    except Exception as exc:  # noqa: BLE001 - sqlite3.IntegrityError：(name, version) 重複
        raise HTTPException(
            status_code=400,
            detail=f"資料集 {req.name}@{req.version} 已註冊過或建立失敗：{exc}",
        ) from exc

    append_audit(
        "dataset_registered",
        {
            "name": req.name,
            "version": req.version,
            "size_bytes": manifest["total_size"],
            "file_count": manifest["file_count"],
        },
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    append_audit(
        "dataset_card_updated",
        {"name": req.name, "version": req.version},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return _dataset_to_dict(app_state.db.get_dataset(req.name, req.version), full=True)


@app.get("/datasets")
async def list_datasets():
    return [_dataset_to_dict(d) for d in app_state.db.list_datasets()]


@app.get("/datasets/{name}/{version}/card")
async def get_dataset_card_endpoint(name: str, version: str):
    """階段 16（PLAN.md Q.3）：資料卡查詢——回結構化 `card`（`None` 表示
    這個版本沒有登記卡，階段 16 之前建立的版本一律如此）＋自動事實
    `auto_facts`（檔案數/總大小/登記時間/目前快取在哪幾台機器，不靠資料
    卡也答得出來）＋人可讀 `rendered`（Markdown 全文，`card is None` 時
    描述/製作方式段落固定寫 `note` 那句話——agent 必須照實轉述「沒有
    紀錄」，不得腦補）。資料集不存在 -> 404。
    """
    dataset = app_state.db.get_dataset(name, version)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"資料集 {name}@{version} 不存在")

    auto_facts = build_dataset_auto_facts(app_state.db, dataset)
    rendered = render_dataset_card(name, version, dataset.card, auto_facts)
    note = None if dataset.card is not None else NO_CARD_NOTE
    return {
        "name": name,
        "version": version,
        "card": dataset.card,
        "auto_facts": auto_facts,
        "rendered": rendered,
        "note": note,
    }


@app.patch("/datasets/{name}/{version}/card")
async def update_dataset_card_endpoint(
    name: str, version: str, req: DatasetCardUpdateRequest, request: Request
):
    """階段 16（PLAN.md Q.3）：補登／更新資料卡——`description`／`method`
    一樣強制必填（補登同樣是「明確記錄」的規則，不因為是事後補登就放寬）。
    保留原 `created_at`（這是補充紀錄，不是重新建立這個版本），
    `updated_at` 更新為現在。稽核 `dataset_card_updated`。資料集不存在
    -> 404。
    """
    dataset = app_state.db.get_dataset(name, version)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"資料集 {name}@{version} 不存在")

    try:
        validate_card_fields(req.description, req.method)
    except InvalidDatasetCardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    derived_from_dict = _resolve_derived_from(app_state.db, req.derived_from)

    try:
        new_card = build_dataset_card(req.description, req.method, derived_from_dict, req.counts)
    except InvalidDatasetCardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if dataset.card and dataset.card.get("created_at"):
        new_card["created_at"] = dataset.card["created_at"]

    app_state.db.update_dataset_card(name, version, new_card)
    append_audit(
        "dataset_card_updated",
        {"name": name, "version": version},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )

    updated = app_state.db.get_dataset(name, version)
    auto_facts = build_dataset_auto_facts(app_state.db, updated)
    rendered = render_dataset_card(name, version, updated.card, auto_facts)
    return {
        "name": name,
        "version": version,
        "card": updated.card,
        "auto_facts": auto_facts,
        "rendered": rendered,
        "note": None,
    }


@app.get("/jobs")
async def get_jobs(status: Optional[str] = None, project: Optional[str] = None):
    """階段 11（PLAN.md L.3）：新增選填 `project` 篩選，可與 `status` 並用。"""
    return [_job_to_dict(j) for j in app_state.db.list_jobs(status=status, project=project)]


@app.get("/jobs/{job_id}")
async def get_job(job_id: int):
    job = app_state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_dict(job)


async def _finalize_approval(
    approval: Approval, source: str, request_context: RequestContext
) -> dict:
    """approval 建立成功後的「要不要立刻生效」判斷（階段 10，PLAN.md K
    節）：POST /dispatch、POST /jobs、POST /jobs/{id}/stop 共用。

    - `source == "web"` 且 `config.web_direct_execute` 開啟（預設開）：
      K.2「網頁一步生效」——在**同一個請求內**直接呼叫既有 `approve()`，
      `approved_by="web-direct"`，note 記「網頁直接執行（提案者＝批准
      者）」。這不是繞過核准流：資料模型、危險指令攔截、稽核全部沿用
      `approve()` 本身，只是把「同一個人的第二次點擊」自動化。
    - 其餘情況（source 非 web，或 `web_direct_execute=false`）：K.3「自動
      核准規則」——諮詢 `app.autoapprove` 規則檔（有 mtime 快取，改規則
      檔不用重啟服務），命中就 `approve()`（`approved_by="auto-rule-N"`）；
      沒有規則檔／沒命中 -> 回傳既有 pending 的 approval dict（行為完全
      同現狀，這也是既有測試不用改的原因：預設 source="api"、沒有
      auto_approve.yaml 時這條路徑跟階段 10 之前一模一樣）。

    回傳形狀刻意不對稱（PLAN.md K.2）：pending 時回傳單純的 approval dict
    （既有形狀）；一步生效／自動核准時回傳
    `{"approval":..., "job":..., "auto_approved": true}`，前端／
    ChatGPT/vLLM 呼叫端據此判斷要顯示「已執行」還是「待核准」。
    """
    try:
        if source == "web" and app_state.config.web_direct_execute:
            result = await approvals_module.approve(
                app_state.db,
                approval.id,
                ssh_run=app_state.ssh_run,
                audit_path=app_state.config.audit_path,
                server_configs=app_state.server_configs,
                app_state=app_state,
                approved_by="web-direct",
                note="網頁直接執行（提案者＝批准者）",
                request_context=request_context,
            )
        else:
            rules = autoapprove.get_rules(app_state.config.auto_approve_rules_path)
            result = await approvals_module.maybe_auto_approve(
                app_state.db,
                approval,
                source=source,
                rules=rules,
                ssh_run=app_state.ssh_run,
                server_configs=app_state.server_configs,
                app_state=app_state,
                audit_path=app_state.config.audit_path,
                request_context=request_context,
            )
    except (
        ApprovalNotFoundError,
        ApprovalNotPendingError,
        JobNotFoundError,
        ServerNotFoundError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result is None:
        return _approval_to_dict(approval)

    response: dict = {
        "approval": _approval_to_dict(result["approval"]),
        "auto_approved": True,
    }
    if result.get("job") is not None:
        response["job"] = _job_to_dict(result["job"])
    return response


async def _create_enqueue_approval(
    req: JobCreateRequest, request_context: RequestContext
):
    """POST /jobs 與 POST /dispatch 共用：階段 2 起一律先建 approval。

    危險指令在建立核准請求「當下」就直接拒絕（400 + 稽核 reject），不
    建立 approval、不給核准機會（鐵律第 2 條）。

    階段 10（PLAN.md K 節）：建立成功後交給 `_finalize_approval()` 判斷
    要不要一步生效／被自動核准規則命中，見該函式 docstring。
    """
    source = _normalize_source(req.source)
    try:
        approval = approvals_module.request_enqueue_approval(
            app_state.db,
            command=req.command,
            type=req.type,
            project=req.project,
            require_tag=req.require_tag,
            pin_server=req.pin_server,
            depends_on=req.depends_on,
            gpus_needed=req.gpus_needed,
            priority=req.priority,
            source=source,
            source_coding_run_id=req.source_coding_run_id,
            local_home_dir=app_state.config.local_home_dir,
            audit_path=app_state.config.audit_path,
            request_context=request_context,
        )
    except DangerousCommandError as exc:
        raise HTTPException(status_code=400, detail=f"指令被拒絕：{exc}") from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 例如：train 任務指定的專案有資料集要求，但該資料集尚未 POST /datasets 註冊
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _finalize_approval(approval, source, request_context)


@app.post("/jobs")
async def create_job(req: JobCreateRequest, request: Request):
    """**行為變更（階段 2 起）**：不再直接入列，改為建立待核准請求。
    與 POST /dispatch 完全同義，保留兩個路徑。"""
    return await _create_enqueue_approval(req, request.state.request_context)


@app.post("/dispatch")
async def dispatch(req: JobCreateRequest, request: Request):
    return await _create_enqueue_approval(req, request.state.request_context)


@app.get("/approvals")
async def list_approvals(status: Optional[str] = None, kind: Optional[str] = None):
    return [
        _approval_to_dict(a) for a in app_state.db.list_approvals(status=status, kind=kind)
    ]


@app.post("/approve/{approval_id}")
async def approve_endpoint(approval_id: int, request: Request):
    try:
        result = await approvals_module.approve(
            app_state.db,
            approval_id,
            ssh_run=app_state.ssh_run,
            audit_path=app_state.config.audit_path,
            server_configs=app_state.server_configs,
            app_state=app_state,
            local_run=local_run,
            request_context=request.state.request_context,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="approval 不存在") from exc
    except ApprovalNotPendingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IdentityAdministrationDisabledError as exc:
        raise HTTPException(
            status_code=404, detail="identity administration is disabled"
        ) from exc
    except IdentityTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateNotFoundError as exc:
        # candidate 在核准請求建立之後、核准之前被刪除/清空（理論上不會
        # 發生，這裡沒有刪除 candidate 的操作，但防禦性處理）。
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ServerNotFoundError as exc:
        # server 在核准請求建立之後、核准之前從 servers.yaml 消失（理論上
        # 不會發生，防禦性處理）。
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 例如：sync 計畫指向一台 servers.yaml 裡不存在的機器設定；
        # import_project 核准時專案名稱已存在；server_add 核准時機器名稱
        # 已存在。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response: dict = {"approval": _approval_to_dict(result["approval"])}
    if "job" in result and result["job"] is not None:
        response["job"] = _job_to_dict(result["job"])
    if "project" in result and result["project"] is not None:
        response["project"] = _project_to_dict(result["project"])
    if "candidates_found" in result:
        response["candidates_found"] = result["candidates_found"]
    if "reload" in result:
        response["reload"] = result["reload"]
    #: 階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）：
    #: ignore_nested_candidates 分支回傳的批次處理結果，原樣帶出。
    if "ignored_count" in result:
        response["ignored_count"] = result["ignored_count"]
        response["ignored_ids"] = result["ignored_ids"]
        response["skipped_ids"] = result["skipped_ids"]
    #: 階段 13（PLAN.md N.6，批次 3a 補線）：coding_task 分支回傳的
    #: `coding_run_id` 原樣帶出，讓核准回應能直接對得上 `GET
    #: /coding-runs/{id}`，不用另外查一次 `GET /jobs/{id}` 再反查。
    if "coding_run_id" in result:
        response["coding_run_id"] = result["coding_run_id"]
    service_account = result.get("service_account")
    if isinstance(service_account, ServiceAccount):
        response["service_account"] = _service_account_to_dict(
            service_account, app_state.db
        )
    service_token = result.get("service_token")
    if isinstance(service_token, ServiceAccountToken):
        token_response = _service_token_to_dict(service_token)
        issued = result.get("issued_service_token")
        if (
            result["approval"].kind == "service_token_issue"
            and isinstance(issued, IssuedServiceToken)
            and issued.id == service_token.id
        ):
            # This value exists only in the in-memory result of the first
            # successful approval.  It is never persisted in approval/audit
            # rows and repeated decisions fail before this response is built.
            token_response["raw_token"] = issued.raw_token
        response["service_token"] = token_response
    membership = result.get("membership")
    if isinstance(membership, ProjectMembership):
        response["membership"] = _project_membership_to_dict(
            membership, app_state.db
        )
    if isinstance(result.get("membership_removed"), bool):
        response["membership_removed"] = result["membership_removed"]
    return response


@app.post("/reject/{approval_id}")
async def reject_endpoint(
    approval_id: int, request: Request, req: Optional[RejectRequest] = None
):
    note = req.note if req is not None else None
    try:
        approval = approvals_module.reject(
            app_state.db,
            approval_id,
            note=note,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="approval 不存在") from exc
    except ApprovalNotPendingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _approval_to_dict(approval)


@app.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: int, request: Request):
    ok = cancel_job(
        app_state.db,
        job_id,
        audit_path=app_state.config.audit_path,
        audit_actor=audit_actor_from_request_context(request.state.request_context),
    )
    if not ok:
        raise HTTPException(
            status_code=400, detail="任務不存在或不是 queued 狀態，無法取消"
        )
    return {"ok": True}


@app.post("/jobs/{job_id}/stop")
async def stop_job_endpoint(
    job_id: int, request: Request, req: Optional[StopJobRequest] = None
):
    """建立 kind=stop 的 approval，僅限 running 狀態的任務；核准後才真的
    SSH kill-session（見 POST /approve/{id}）。

    階段 10（PLAN.md K 節）：`source="web"` 且開啟 `web_direct_execute`
    時同一請求內直接核准；其餘情況交給自動核准規則諮詢，見
    `_finalize_approval()`。"""
    source = _normalize_source(req.source if req is not None else None)
    try:
        approval = approvals_module.request_stop_approval(
            app_state.db,
            job_id,
            source=source,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobNotRunningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _finalize_approval(
        approval, source, request.state.request_context
    )


@app.get("/jobs/{job_id}/log")
async def get_job_log(job_id: int, lines: int = 40):
    """running 任務即時 SSH 抓尾 N 行；其他狀態回傳存好的 `log_tail`。

    SSH 抓不到時（連不上、逾時等）退回存好的 `log_tail`，不讓這個端點
    因為暫時連不上而整個炸掉。
    """
    job = app_state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    log_tail = job.log_tail
    live = False
    if job.status == "running" and job.server:
        try:
            result = await app_state.ssh_run(
                job.server, build_log_tail_command(job_id, lines=lines), 15
            )
            log_tail = result.stdout
            live = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("即時抓取任務 %s log 失敗，退回存好的 log_tail: %s", job_id, exc)

    return {"job_id": job_id, "status": job.status, "live": live, "log_tail": log_tail}


@app.get("/events")
async def get_events(n: int = 100):
    """audit.jsonl 尾 n 行，新到舊。"""
    return tail_audit(app_state.config.audit_path, n=n)


@app.get("/audit")
async def get_audit(n: int = 100):
    """`GET /events` 的別名：實作指令 §7 的最小 API 集合列的是 `/audit`，
    行為完全相同（同一份 `audit.jsonl`），保留 `/events` 是因為階段 2～4
    已經有既有測試/前端在用這個名字，兩個路徑並存不衝突。"""
    return tail_audit(app_state.config.audit_path, n=n)


# ---------------------------------------------------------------------------
# 階段 5：失敗診斷（POST /jobs/{id}/diagnose）
# ---------------------------------------------------------------------------


@app.post("/jobs/{job_id}/diagnose")
async def diagnose_job_endpoint(job_id: int, request: Request):
    """失敗任務的診斷：只顯示說明與修改建議（diff），**絕不執行、不改碼、
    不重跑**（鐵律＋實作指令 5.8）。

    - 任務不存在 → 404
    - 任務不是 `failed` 狀態 → 400
    - 沒有設定 `ANTHROPIC_API_KEY`（`is_llm_available()` 為 False）→ 改用本地
      vLLM（`is_vllm_available()`）；兩者都不可用才回 503
    - 專案結構（若任務有掛專案且目標機在線）用 best-effort SSH 抓取，抓不到
      就略過，不影響診斷本身
    - LLM 呼叫失敗（`app.llm.diagnose_job_failure()`／
      `app.llm_local.diagnose_job_failure_local()` 內部都已重試至多 2 次
      仍失敗）→ 502
    """
    job = app_state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "failed":
        raise HTTPException(status_code=400, detail="只有 failed 狀態的任務可以診斷")

    use_anthropic = is_llm_available(app_state.config)
    use_vllm = not use_anthropic and is_vllm_available(app_state.config)
    if not use_anthropic and not use_vllm:
        raise HTTPException(
            status_code=503,
            detail="未設定 ANTHROPIC_API_KEY，也未設定 VLLM_BASE_URL/VLLM_MODEL，診斷不可用",
        )

    project_structure: Optional[str] = None
    if job.project:
        server_state = app_state.server_states.get(job.server) if job.server else None
        if server_state is not None and server_state.online:
            try:
                result = await app_state.ssh_run(
                    job.server,
                    f"find {shlex.quote('projects/' + job.project)} -maxdepth 2 "
                    "-not -path '*/.git*' | head -100",
                    20,
                )
                project_structure = result.stdout
            except Exception as exc:  # noqa: BLE001 - best effort，抓不到就略過
                logger.warning("診斷任務 %s 時抓取專案結構失敗，略過: %s", job_id, exc)

    try:
        if use_anthropic:
            diagnosis = await diagnose_job_failure(
                command=job.command,
                exit_code=job.exit_code,
                log_tail=job.log_tail,
                project_structure=project_structure,
                config=app_state.config,
                client=app_state.llm_client,
            )
        else:
            diagnosis = await diagnose_job_failure_local(
                command=job.command,
                exit_code=job.exit_code,
                log_tail=job.log_tail,
                project_structure=project_structure,
                config=app_state.config,
                client=app_state.vllm_client,
            )
    except (LLMError, LLMLocalError) as exc:
        append_audit(
            "diagnose",
            {"job_id": job_id, "success": False, "error": str(exc)},
            result="failed",
            path=app_state.config.audit_path,
            actor=audit_actor_from_request_context(request.state.request_context),
        )
        raise HTTPException(status_code=502, detail=f"診斷失敗：{exc}") from exc

    append_audit(
        "diagnose",
        {"job_id": job_id, "success": True, "diagnosis": diagnosis},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    return {"job_id": job_id, "diagnosis": diagnosis}


# ---------------------------------------------------------------------------
# 階段 7：Local vLLM Agent Layer（POST /agent/chat、GET /agent/tools、
# POST /agent/cmd）。三者都在 `_AUTH_EXEMPT_PATHS` 之外，AUTH_TOKEN 有設定
# 時自動要求 X-Auth-Token（沿用既有 auth_middleware，不用另外加程式碼）。
# ---------------------------------------------------------------------------


class AgentChatRequest(BaseModel):
    text: str


class AgentCmdRequest(BaseModel):
    cmd: str


#: POST /agent/cmd 的白名單（enum 分派）：直接呼叫對應的唯讀工具 handler，
#: **不經過 LLM、不經過 tool loop**，也不接受自由文字、不做黑名單解析——
#: 這是給前端/腳本快速查狀態用的固定端點，不是聊天介面。
_AGENT_CMD_TOOL_MAP = {
    "status": "status",
    "servers": "servers",
    "jobs": "jobs",
    "approvals": "approvals",
    "events": "events",
    "gpu": "gpu",
    "vllm": "vllm_health",
}


@app.post("/agent/chat")
async def agent_chat_endpoint(req: AgentChatRequest, request: Request):
    """本地 vLLM Agent 對話（單次請求、不做跨請求記憶）。vLLM 未設定
    （`is_vllm_available()` 為 False）→ 503。併發用 `app_state.agent_semaphore`
    限制（`AGENT_MAX_CONCURRENCY`，預設 2），避免同時打爆單張 GPU 上的
    vLLM 服務。"""
    if not is_vllm_available(app_state.config):
        raise HTTPException(
            status_code=503, detail="未設定 VLLM_BASE_URL/VLLM_MODEL，本地模型不可用"
        )
    async with app_state.agent_semaphore:
        messages = await run_agent(
            req.text,
            db=app_state.db,
            server_states=app_state.server_states,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            http_client=app_state.vllm_client,
            server_configs=app_state.server_configs,
            ssh_run=app_state.ssh_run,
            ssh_run_direct=app_state.ssh_pool.run,
            request_context=request.state.request_context,
        )
    return {"messages": messages}


@app.get("/agent/tools")
async def agent_tools_endpoint():
    """工具清單（name/description/args），由 `app.agent_tools.TOOLS` 表產生，
    不手寫第二份。跟 vLLM 有沒有設定無關——單純是白名單清單本身。"""
    return list_tool_specs()


@app.post("/agent/cmd")
async def agent_cmd_endpoint(req: AgentCmdRequest, request: Request):
    """固定 enum 分派到對應的唯讀工具，不經過 LLM。合法值：
    status/servers/jobs/approvals/events/gpu/vllm；其他一律 400。"""
    tool_name = _AGENT_CMD_TOOL_MAP.get(req.cmd)
    if tool_name is None:
        raise HTTPException(
            status_code=400,
            detail=f"不合法的 cmd，合法值：{', '.join(sorted(_AGENT_CMD_TOOL_MAP))}",
        )
    ctx = AgentContext(
        db=app_state.db,
        server_states=app_state.server_states,
        config=app_state.config,
        audit_path=app_state.config.audit_path,
        http_client=app_state.vllm_client,
        server_configs=app_state.server_configs,
        ssh_run=app_state.ssh_run,
        ssh_run_direct=app_state.ssh_pool.run,
        request_context=request.state.request_context,
    )
    result = await dispatch_tool(tool_name, {}, ctx)
    return {"cmd": req.cmd, "result": result}


# ---------------------------------------------------------------------------
# 階段 5：聊天 WebSocket（WS /ws）
# ---------------------------------------------------------------------------


async def _ws_authenticate(
    websocket: WebSocket,
    config: AppConfig,
) -> Optional[RequestContext]:
    """Resolve WS identity while preserving the existing message protocol.

    A valid session cookie or service Authorization header returns immediately
    without consuming a message.
    Otherwise AUTH_TOKEN-configured deployments still require the first
    ``{"type":"auth","token":"..."}`` envelope and close with 1008 on
    failure.  The token may be the compatible shared token or, when enabled,
    a service token.  No credential is placed in a query string or log.
    """
    preauthenticated_context = resolve_request_context(
        app_state.db,
        session_token=websocket.cookies.get(config.session_cookie_name),
        authorization=websocket.headers.get("Authorization"),
        legacy_token=None,
        configured_legacy_token=config.auth_token,
        legacy_shared_token_enabled=config.legacy_shared_token_enabled,
        service_token_auth_enabled=config.service_token_auth_enabled,
    )
    if preauthenticated_context is not None:
        return preauthenticated_context

    if not config.auth_token:
        return RequestContext()
    try:
        first = await websocket.receive_json()
    except Exception:  # noqa: BLE001 - 不是合法 JSON、連線中斷等都視為認證失敗
        await websocket.close(code=1008)
        return None
    if not isinstance(first, dict) or first.get("type") != "auth":
        await websocket.close(code=1008)
        return None
    supplied_token = first.get("token")
    authorization = None
    if isinstance(supplied_token, str):
        authorization = (
            supplied_token
            if supplied_token.lower().startswith("bearer ")
            else f"Bearer {supplied_token}"
        )
    context = resolve_request_context(
        app_state.db,
        authorization=authorization,
        legacy_token=supplied_token,
        configured_legacy_token=config.auth_token,
        legacy_shared_token_enabled=config.legacy_shared_token_enabled,
        service_token_auth_enabled=config.service_token_auth_enabled,
    )
    if context is None:
        await websocket.close(code=1008)
    return context


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """聊天 WebSocket（實作指令 5.7；階段 7 起可能改走本地 vLLM agent
    runtime，見下）。現有 `auth_middleware` 只攔 HTTP，WebSocket 不會經過
    它，因此認證邏輯在這裡另外處理（見 `_ws_authenticate()`）。訊息流：
    client 送 `{"type":"chat","text":"..."}`，server 回一或多則 JSON
    （`{"type":"reply"|"system"|"tool_note","text":...}` 或
    `{"type":"approval_card","approval":{...}}`）。

    **階段 7 切換**：`is_vllm_available()` 為 True 時，改呼叫
    `app.agent_runtime.run_agent()`（JSON tool loop，單一大腦，見該模組
    docstring）；vLLM 不可用時完全沿用既有 `app.chat.handle_chat_text()`
    路徑（anthropic 或規則式）——既有 289 條測試都沒有設定
    `VLLM_BASE_URL`，天然走舊路徑，行為不受影響。

    **對話記憶（範圍＝這條 WebSocket 連線）**：走 vLLM agent 路徑時，這裡
    維護一個 `history` 列表，每輪呼叫 `run_agent(..., history=history)`
    後把「這輪使用者訊息」與「這輪組出來的 assistant 內容」append 進去
    （`tool_note`/`system` 訊息不進 history；若有 `approval_card`，在
    assistant 內容尾端補一行摘要讓模型知道自己剛做了什麼）——重新整理頁面
    等於開一條新連線，`history` 從空列表重新開始，不做持久化／跨連線記憶。
    走規則式路徑（vLLM 不可用）時不維護 history（規則式本來就無記憶，行為
    不變）。`POST /agent/chat` 是另一個獨立入口，維持既有無狀態行為。"""
    await websocket.accept()
    request_context = await _ws_authenticate(websocket, app_state.config)
    if request_context is None:
        return

    shadow_evidence = collect_shadow_evidence(
        mode=app_state.config.authorization_mode,
        db=app_state.db,
        context=request_context,
        action=ROUTE_AUTHORIZATION[("WEBSOCKET", "/ws")].action,
        resource_kind=ROUTE_AUTHORIZATION[("WEBSOCKET", "/ws")].resource_kind,
        values={},
        interface_kind="route",
        interface_name="WEBSOCKET /ws",
    )

    #: 這條連線範圍的對話歷史（見上方 docstring）；只有 vLLM agent 路徑會
    #: 讀寫它，`run_agent()` 內部會再用 `trim_history()` 砍過一次。
    history: list[dict] = []

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:  # noqa: BLE001 - 不是合法 JSON，提醒後繼續等下一則
                await websocket.send_json(
                    {"type": "reply", "text": "訊息格式錯誤，請傳送合法的 JSON。"}
                )
                continue

            if not isinstance(data, dict) or data.get("type") != "chat":
                continue
            text = str(data.get("text") or "")

            use_vllm = is_vllm_available(app_state.config)
            agent_error = False
            try:
                if use_vllm:
                    async with app_state.agent_semaphore:
                        messages = await run_agent(
                            text,
                            db=app_state.db,
                            server_states=app_state.server_states,
                            config=app_state.config,
                            audit_path=app_state.config.audit_path,
                            http_client=app_state.vllm_client,
                            server_configs=app_state.server_configs,
                            ssh_run=app_state.ssh_run,
                            ssh_run_direct=app_state.ssh_pool.run,
                            history=history,
                            request_context=request_context,
                        )
                else:
                    messages = await handle_chat_text(
                        text,
                        db=app_state.db,
                        server_states=app_state.server_states,
                        config=app_state.config,
                        audit_path=app_state.config.audit_path,
                        llm_client=app_state.llm_client,
                        server_configs=app_state.server_configs,
                        request_context=request_context,
                    )
            except Exception as exc:  # noqa: BLE001 - 聊天處理絕不能讓連線整個炸掉
                logger.exception("聊天處理發生例外")
                messages = [{"type": "reply", "text": f"處理訊息時發生錯誤：{exc}"}]
                agent_error = True

            if use_vllm and not agent_error:
                history.append({"role": "user", "content": text})
                reply_text = "\n".join(
                    m["text"] for m in messages if m.get("type") == "reply" and m.get("text")
                )
                assistant_parts = [reply_text] if reply_text else []
                for m in messages:
                    if m.get("type") == "approval_card":
                        approval = m.get("approval") or {}
                        assistant_parts.append(
                            f"（已建立核准請求 #{approval.get('id')}："
                            f"{approval.get('kind')}）"
                        )
                history.append({"role": "assistant", "content": "\n".join(assistant_parts)})

            for msg in messages:
                await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    finally:
        # Keep every frame and the in-connection `events` view unchanged; append
        # the connection-level observation only once the socket finishes.
        emit_shadow_evidence(
            shadow_evidence,
            audit_path=app_state.config.audit_path,
        )


def run() -> None:
    """`python -m app.main` 的進入點：先讀設定拿到 host/port，再啟動 uvicorn。

    鐵律第 4 條：服務只綁私網。host 一律來自設定（.env 的 API_HOST，預設
    127.0.0.1），不接受從命令列傳入 0.0.0.0。
    """
    import uvicorn

    bootstrap_config = load_app_config()
    uvicorn.run(
        "app.main:app",
        host=bootstrap_config.api_host,
        port=bootstrap_config.api_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
