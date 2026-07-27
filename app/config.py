"""設定讀取：servers.yaml 與 .env

- servers.yaml：機器清單（見 servers.yaml.example）
- .env：環境變數（見 .env.example）。這裡自己寫一個很小的 parser，避免
  多引入 python-dotenv 這個依賴；只在對應的環境變數尚未存在時才設定，
  讓「真的環境變數」優先於 .env 檔內容。
"""

from __future__ import annotations

import os
import re
from math import isfinite
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml

from app.inventory import DEFAULT_EMBEDDED_DATASET_NAMES, DEFAULT_EXCLUDE_NAMES


AUTHORIZATION_MODES = frozenset({"off", "shadow"})
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_MAX_OIDC_CLOCK_SKEW_LEEWAY_SEC = 300


def _validate_cookie_name(value: str, setting_name: str) -> None:
    """Reject blank or response-splitting cookie names at configuration time."""

    if not isinstance(value, str) or not _COOKIE_NAME_RE.fullmatch(value):
        raise ValueError(f"{setting_name} must be a non-empty HTTP cookie token")


def _validate_oidc_https_url(
    value: str,
    setting_name: str,
    *,
    callback: bool = False,
) -> None:
    """Validate security-sensitive OIDC endpoints without contacting a provider."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{setting_name} must be a non-empty HTTPS URL")
    if value != value.strip():
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL")
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` performs urllib's numeric/range validation.  The
        # application does not otherwise need the parsed port here.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL")
    if callback:
        if parsed.path != "/auth/callback" or parsed.query:
            raise ValueError(
                "OIDC_REDIRECT_URI must use the exact /auth/callback path "
                "without query or fragment"
            )
    elif parsed.query:
        raise ValueError("OIDC_ISSUER must not contain a query or fragment")


@dataclass
class ServerConfig:
    name: str
    host: str
    user: str
    key: str
    gpu: bool = False
    idle_gpu_util: float = 15.0
    idle_load: float = 2.0
    tags: list[str] = field(default_factory=list)
    #: 階段 8（第二批，PLAN.md I.3）：SSH port，沒設定時預設 22（同
    #: asyncssh 的預設值，向下相容既有 servers.yaml）。
    port: int = 22
    #: Project Inventory 掃描的根目錄（見 app/inventory.py）。沒設定時為空
    #: 列表——這台機器就不會被 `POST /inventory/scan` 的 `server="all"`
    #: 自動掃到（見 app/approvals.py 的 request_inventory_scan_approval()）。
    project_roots: list[str] = field(default_factory=list)
    #: 資料集根目錄（目前只用於 `test_ssh_connection()` 的唯讀探測；資料集
    #: 的實際同步/快取仍是階段 3 的 datasets/dataset_cache 表機制）。
    dataset_roots: list[str] = field(default_factory=list)
    #: 掃描時視為「內嵌資料集」的目錄名，沒設定時用 app.inventory 的預設值。
    project_embedded_dataset_names: list[str] = field(
        default_factory=lambda: list(DEFAULT_EMBEDDED_DATASET_NAMES)
    )
    #: 掃描時 `find -prune` 排除的目錄名，沒設定時用 app.inventory 的預設值。
    project_exclude_names: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_NAMES)
    )
    #: `enabled=false` 時 monitor 仍會探測狀態（總覽頁看得到），但
    #: `scheduler_tick()` 不會派工給這台機器（PLAN.md I.8）。
    enabled: bool = True
    #: 使用者備註（例如停用原因），純顯示用途，不影響任何行為判斷。
    note: Optional[str] = None
    #: Goal 3 C3（INV-NODE-6）：**這一台**機器的執行通道，`ssh`（預設）或
    #: `node`。刻意是 per-machine 欄位而不是全域開關——INV-NODE-6 明文禁止
    #: 「全域一刀切開關」，也要求任何一台都能在不影響其他機器的情況下退回
    #: SSH（把這個欄位改回 `ssh` 即可，不需要資料遷移）。
    #:
    #: 值為 `node` 時排程器**不**主動 SSH 派工給這台機器；工作改由該機器的
    #: Node Agent 出站輪詢領取。`NODE_AGENT_V1_ENABLED` 關閉時這個欄位一律
    #: 被視為 `ssh`（fail-closed，見 `resolve_execution_backend()`）。
    execution_backend: str = "ssh"

    @property
    def key_path(self) -> str:
        return os.path.expanduser(self.key)


@dataclass
class AppConfig:
    servers: list[ServerConfig]
    db_path: str = "jobqueue.db"
    audit_path: str = "audit.jsonl"
    #: 階段 8（第二批）：`app.server_config.reload_server_config_if_supported()`
    #: 重新讀取 servers.yaml 時用這個路徑，不是硬編碼字串——`load_app_config()`
    #: 會把實際載入時用的路徑存回這裡。
    servers_yaml_path: str = "servers.yaml"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    monitor_interval_sec: int = 20
    scheduler_interval_sec: int = 10
    ssh_connect_timeout: int = 10
    ssh_command_timeout: int = 30
    ssh_max_concurrency: int = 8
    default_idle_load: float = 2.0
    auth_token: Optional[str] = None
    #: Goal 1 / Slice 3: temporary compatibility transport.  While enabled,
    #: the existing shared token resolves to the explicitly labelled durable
    #: legacy-admin actor.  Disabling this switch does not delete that actor or
    #: change the configured AUTH_TOKEN value.
    legacy_shared_token_enabled: bool = True
    #: Service bearer tokens are schema-ready but opt-in during compatibility
    #: rollout.  Session authentication remains available independently.
    service_token_auth_enabled: bool = False
    #: Goal 1 / Slice 5: authorization is observational only.  ``off`` keeps
    #: the pre-Goal-1 behavior; ``shadow`` records would-deny evidence without
    #: changing responses or side effects.  Enforcement is intentionally not
    #: a supported configuration value in Goal 1.
    authorization_mode: str = "off"
    #: Server-side session cookie name.  Cookie security attributes are applied
    #: by the Slice 7 OIDC lifecycle that issues it.
    session_cookie_name: str = "dispatch_session"
    #: Goal 1 / Slice 7: human OIDC login is an explicit rollback switch.  When
    #: disabled, provider settings are not required and no provider networking is
    #: attempted; existing legacy/service authentication behavior is unchanged.
    oidc_enabled: bool = False
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    #: Never include the client secret in a dataclass repr or log message.
    oidc_client_secret: Optional[str] = field(default=None, repr=False)
    oidc_redirect_uri: Optional[str] = None
    oidc_scopes: tuple[str, ...] = ("openid", "profile", "email")
    #: Exact, case-sensitive OIDC ``sub`` values.  An empty set means no OIDC
    #: identity is bootstrapped as platform admin; email is never consulted.
    oidc_platform_admin_subjects: frozenset[str] = field(default_factory=frozenset)
    oidc_login_flow_ttl_sec: int = 600
    oidc_session_ttl_sec: int = 28800
    #: Short-lived correlation cookie for the OIDC handshake.  It must remain
    #: distinct from the authenticated session cookie.
    oidc_flow_cookie_name: str = "dispatch_oidc_flow"
    #: Provider discovery/token/JWKS requests use a finite timeout.  ID-token
    #: temporal claim validation permits only this small clock-skew window.
    oidc_provider_timeout_sec: float = 10.0
    oidc_clock_skew_leeway_sec: int = 60
    #: Goal 1 / Slice 6: identity-administration APIs are opt-in.  Turning this
    #: rollback switch off must not alter service-token authentication or
    #: delete/revoke durable identity rows, and never enables authorization
    #: enforcement.
    identity_admin_enabled: bool = False
    #: DG-EXEC-ATTEMPT-v1 rollout controls.  They are deliberately separate:
    #: stopping new claims must never abandon reconciliation/outbox ownership
    #: for already durable work.  WP-2A reads them only to acquire the shared
    #: scheduler lease; remote claim/reconcile/outbox workers remain unwired
    #: until their later gates.  Every default stays false.
    execution_attempt_shadow_enabled: bool = False
    execution_attempt_new_claims_enabled: bool = False
    execution_attempt_reconcile_existing: bool = False
    execution_outbox_worker_enabled: bool = False
    # WP-2C (DG-AMBIGUOUS-LAUNCH-v1 §9). Routes dispatch through durable
    # attempts instead of the legacy revert-on-any-exception path.
    execution_attempt_ssh_launch_enabled: bool = False
    #: Plan v2 Slice 2：immutable AI Engineering Task backend rollback switch。
    #: 關閉時 legacy Coding Task API/Runner 完全不變；additive schema 仍可讀。
    engineering_task_backend_v1: bool = False
    #: D2 finalization-sandbox interlock（docs/AI_ENGINEERING_DECISION_GATE.md
    #: §D2）：post-turn Git finalization 仍在 Codex sandbox 外以 Runner OS user
    #: 執行。啟用 backend 必須同時明確接受這個未沙箱化殘餘；D2 sandbox 落地後
    #: 這個第二鑰匙應改為 sandbox preflight 條件並退場。直接建構 AppConfig 也
    #: 會經過 __post_init__，此 interlock 同時涵蓋 env 與程式建構兩條路徑。
    engineering_task_backend_v1_accept_unsandboxed_finalization: bool = False
    #: D1 首切片（docs/DECISIONS.md：bounded implementation only）：只控制
    #: `codex-app-server-v1` adapter 在 GET /coding-agents 探測輸出中是否可見。
    #: 不是啟用閘門——這個 adapter 的五個 CodingAgentProvider 方法在這個切片
    #: 一律 fail closed（未接線進 turn 生命週期），與這個旗標無關；旗標關閉
    #: 時單純從發現端點隱藏，不影響任何執行路徑。
    controlled_coding_runner_v1: bool = False
    #: D5 Run Profile v1（docs/DECISIONS.md：approve proposed v1）：additive
    #: `run_profiles` schema/approval-gated create/update/archive 的 rollback
    #: 開關，預設關閉。關閉時既有 `Project.default_command`/`setup_cmd`/
    #: `require_tag` 與既有 enqueue 行為完全不變；這個旗標只控制新路由的
    #: 可見性,不回填任何假造的已核准 profile,也不改變排程/派工邏輯。
    run_profile_v1_enabled: bool = False
    #: Goal 2 Slice 3（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md，
    #: docs/DECISIONS.md 2026-07-18）：Dispatch Policy v1 的 rollback 開關,
    #: 預設關閉。這個切片的政策物件**沒有任何運行時效果**——關閉時只是隱藏
    #: 新路由（404）並讓 approve() fail-closed,不影響既有排程/派工邏輯,也
    #: 不回填任何假造的已核准政策(同 D5 Run Profile v1 的 rollback 慣例)。
    dispatch_policy_v1_enabled: bool = False
    #: Goal 2 Slice 4（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md,DG-1 核准見
    #: docs/DECISIONS.md 2026-07-18）：政策驅動放置提案 background loop 的
    #: rollback 開關,**故意跟 `dispatch_policy_v1_enabled` 分開**——政策
    #: 物件可以先存在（Slice 3）而不啟動提案迴圈,兩者獨立開關、獨立回滾。
    #: 預設關閉。這個 loop 只建立 **pending** `auto_placement` approval,
    #: 從不自動核准（Slice 5 才有獨立機制）。
    auto_placement_proposals_enabled: bool = False
    #: 提案迴圈的檢查頻率（秒）,同 monitor/scheduler 迴圈的既有 interval
    #: 慣例。
    auto_placement_interval_sec: int = 300
    #: 同一個 (policy_id, server_name) 組合建立過一次 `auto_placement`
    #: proposal 之後,至少要間隔這麼多秒才會再提一次——防洪,避免每輪迴圈
    #: 都對同一組合灌 pending approval。
    auto_placement_cooldown_sec: int = 3600
    #: Goal 2 Slice 5（INV-APPROVAL-4b，docs/DECISIONS.md 2026-07-18 DG-2）：
    #: 全域煞車，**預設為 True＝自動執行停用**（"kill switch on" 這個命名
    #: 的語意是「殺掉／關掉自動執行」，不是「殺掉整個功能」——關掉時
    #: Slice 4 的提案行為完全不變，只是沒有東西會自動核准 `auto_placement`
    #: pending approval）。操作者要明確把它設成非停用值才會啟用
    #: `maybe_auto_decide_placement()`（見 app/approvals.py）；這是獨立於
    #: `maybe_auto_approve()` 的 policy-scoped 機制,不擴大該函式的
    #: "enqueue"/"stop" 白名單。
    auto_placement_kill_switch: bool = True
    #: Goal 2 Slice 1（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：monitor 每次
    #: 探測是否額外寫一筆 `server_observations` 快照，best-effort、失敗不影響
    #: 現有排程。回滾＝設 false（表本身留著，不刪資料）。
    server_observations_enabled: bool = True
    #: 同上切片：`server_observations` 保留天數，monitor 迴圈機會性清理過期
    #: 列（不是強制 cleanup job）。
    server_observation_retention_days: int = 14
    #: Goal 3 Phase B（docs/GOAL_3_FUTURE_WORK_PLAN.md；DG-B 核准見
    #: docs/DECISIONS.md 2026-07-19）：空伺服器 bootstrap 的 rollback 開關，
    #: 預設關閉。關閉時新路由 404、`server_bootstrap` approve fail-closed、
    #: `server_add` 的 bootstrap-report 閘完全不啟動——行為與 Phase B 之前
    #: 逐位元相同。
    server_bootstrap_v1_enabled: bool = False
    #: Goal 3 Phase B4（docs/DG_B4_DATASET_PREWARM_DRAFT.md；DG-B4 核准見
    #: docs/DECISIONS.md 2026-07-25）：新機 dataset 預熱提案的 rollback 開關，
    #: 預設關閉。關閉時 `dataset_prewarm_loop()` 每輪直接 no-op，
    #: `dataset_prewarm` approve fail-closed——行為與 B4 之前逐位元相同。
    dataset_prewarm_v1_enabled: bool = False
    #: 預熱提案迴圈的檢查頻率（秒），比照 `auto_placement_interval_sec`。
    dataset_prewarm_interval_sec: int = 300
    #: 同一個 (server, dataset, version) 組合建立過一次 `dataset_prewarm`
    #: proposal 之後，至少要間隔這麼多秒才會再提一次——防洪，也避免使用者
    #: 拒絕之後下一輪立刻重提（比照 `auto_placement_cooldown_sec`）。
    dataset_prewarm_cooldown_sec: int = 3600
    #: DG-B4 第二把煞車，**預設為 True＝提案停用**。跟
    #: `auto_placement_kill_switch` 同款雙重保險：`dataset_prewarm_v1_enabled`
    #: 與這個旗標都要撥開（enabled=True 且 kill switch=False）才會真的建立
    #: 提案。注意語意與 auto_placement 不同——這裡沒有任何自動核准機制，
    #: 這把煞車管的是**提案**本身；`dataset_prewarm` 永遠不進
    #: `maybe_auto_approve()` 的 "enqueue"/"stop" 白名單。
    dataset_prewarm_kill_switch: bool = True
    #: Goal 3 Phase C / C2（INV-NODE-*，DG-C 核准 2026-07-19）：Node Agent
    #: 協議的 rollback 開關，預設關閉。關閉時 node 端點全部 404、
    #: `node_enroll`/`node_revoke` approve fail-closed——行為與 C2 之前逐位元
    #: 相同。**注意**：這個旗標只開放「協議與登錄」；把任務實際路由到 node
    #: 通道是 C3/C4 的範圍，本輪沒有任何 job 會走 node 執行。
    node_agent_v1_enabled: bool = False
    #: lease 存活秒數：agent 必須在這段時間內 acknowledge，否則 control
    #: plane 可以把 attempt 重新 lease 給別的 node（因為還沒有副作用）。
    node_agent_lease_ttl_sec: float = 120.0
    #: 心跳 TTL 與寬限期（秒）。超過 TTL＋grace 判 `unknown`——**不是**
    #: failed（INV-NODE-4）。
    node_agent_heartbeat_ttl_sec: float = 60.0
    node_agent_heartbeat_grace_sec: float = 60.0
    #: Goal 3 C3（roadmap Phase 3：「canary eligibility limited to designated
    #: non-production ordinary jobs」）：只有 `require_tag` 精確等於這個值的
    #: **普通**任務（train/adhoc）才可以被 Node Agent 領走。
    #:
    #: **預設空字串＝沒有任何 job 合格**。這是刻意的第三道煞車：光是開啟
    #: `NODE_AGENT_V1_ENABLED` 並把某台機器設成 `execution_backend: node`，
    #: 仍然不會有任何正式工作流到未驗證的通道上——操作者必須再逐個任務
    #: 明確標記，canary 才會真的開始。
    node_canary_require_tag: str = ""
    #: 階段 3：sync 任務在本地執行時，「本地版的 home 目錄」——
    #: `agent_jobs/{id}/...` 這類相對路徑會相對這個目錄解析（見
    #: `app/localrun.py`）。預設用目前工作目錄，跟其他相對路徑（`db_path`／
    #: `audit_path` 預設值）的行為一致。
    local_home_dir: str = "."
    #: 每小時 SSH 各機 `ls -d datasets/*/*/` 校正 dataset_cache 的頻率（秒）。
    dataset_reconcile_interval_sec: int = 3600
    #: PLAN.md 2026-07-11 版 §14 切片 2:唯讀探測全部 project_instances、
    #: 收斂 state(available/missing/dirty/diverged/unknown)的頻率(秒),
    #: 同 dataset_cache 校正的節奏等級。
    project_reconcile_interval_sec: int = 3600

    #: 階段 4：Email 通知（app/mailer.py）。任一必要項（host/port/from/to）
    #: 沒設定，`send_mail()` 記 log 後跳過，系統照常運作（原規格 5.6）。
    #: SMTP_USER/SMTP_PASS 允許留空（部分內網 relay 不需要認證）。
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    mail_from: Optional[str] = None
    mail_to: Optional[str] = None
    #: 任務結束後從工作機拉 results/{id}/ 回本地的 rsync 逾時（秒）。
    result_pull_timeout_sec: int = 600
    #: 卡死偵測：job.log 超過幾分鐘沒有增長就標 stalled_suspect（PLAN.md E）。
    stall_minutes: int = 30

    #: 階段 5：LLM（選配層，app/llm.py）。`anthropic_api_key` 沒設定或
    #: `anthropic` 套件沒裝，`app.llm.is_llm_available()` 就回傳 False，
    #: 整個系統（聊天、失敗診斷、信件摘要）降級為規則式/跳過，不影響前四
    #: 階段任何行為（鐵律第 1 條）。`llm_model` 預設 claude-sonnet-5，
    #: `.env` 的 LLM_MODEL 可覆蓋。
    anthropic_api_key: Optional[str] = None
    llm_model: str = "claude-sonnet-5"

    #: 階段 7：本地 vLLM Agent Layer（選配層，app/llm_local.py／
    #: app/agent_runtime.py）。`vllm_base_url`/`vllm_model` 兩者都有設定，
    #: `app.llm_local.is_vllm_available()` 才回傳 True——這是這個選配層
    #: 唯一的開關判斷式，沒設定時 WS `/ws`、`POST /agent/chat` 等入口一律
    #: 明確降級（見 app/main.py），不影響既有行為（PLAN.md H 節）。
    #: 注意 port：調度中心自己是 8000，vLLM 建議另外綁 8001，別撞。
    vllm_base_url: Optional[str] = None
    vllm_model: Optional[str] = None
    #: 選填：vLLM 服務用 `--api-key` 啟動時要帶的憑證，`app.llm_local` 送
    #: 每個 HTTP 請求時加 `Authorization: Bearer {key}` header。**不列入
    #: `is_vllm_available()` 的判斷條件**——沒有 `--api-key` 的 vLLM 部署
    #: 也是合法的，這一欄純粹是「有沒有東西可以送」的旗標。
    vllm_api_key: Optional[str] = None
    #: JSON tool loop（app/agent_runtime.py）每次請求最多呼叫幾次工具，
    #: 超過就終止並回覆「已達工具呼叫上限」。
    agent_max_tool_steps: int = 6
    #: `POST /agent/chat`／WS `/ws`（走 agent runtime 時）的併發上限，
    #: `app.main.AppState` 啟動時建立對應的 `asyncio.Semaphore`。
    agent_max_concurrency: int = 2
    #: 工具呼叫結果回餵給模型前統一截斷的字元數上限（job_log/events 等
    #: 工具本身也會先限行數，這裡是最後一道保險）。
    agent_tool_result_max_chars: int = 4000

    #: 階段 8（第二批，PLAN.md I.3）：Web Server Management 的安全設定。
    #: `allow_root_ssh` 為 False 時，`validate_server_config()` 拒絕
    #: `user="root"`（見 app/server_config.py）。
    allow_root_ssh: bool = False
    #: SSH 私鑰檔案必須落在這些目錄底下（`os.path.realpath` 正規化後比較，
    #: 防 `../` 繞過），沒設定時預設只允許 `~/.ssh`。
    ssh_key_allowed_dirs: list[str] = field(default_factory=lambda: ["~/.ssh"])

    #: 階段 10（PLAN.md K 節，核准流減摩擦）：`source="web"` 的
    #: enqueue／stop 是否在**同一個請求內**直接呼叫既有 `approve()`（提案
    #: 者＝批准者，見 `app/approvals.py` 的 `approve()` 與
    #: `app/main.py` 的 `_finalize_approval()`）。預設開啟；設 `false`
    #: 恢復現行兩步（建立 approval -> 另外呼叫 `POST /approve/{id}`）。
    web_direct_execute: bool = True
    #: 自動核准規則檔路徑（見 `app/autoapprove.py`）。檔案不存在＝沒有
    #: 規則＝一切照舊出核准卡（安全預設），不是啟動必要條件。
    auto_approve_rules_path: str = "auto_approve.yaml"

    #: 階段 13（PLAN.md N.1，Codex Worker v2）：六個 CODEX_* 設定鍵。
    #: **不在 servers.yaml 加任何 runner 欄位**——「誰是 Runner」只看
    #: `.env`，避免兩個 source of truth（servers.yaml 只描述機器）。
    #:
    #: `codex_runner_server`：選填。**2026-07-10 使用者修訂**：未設定
    #: （None）＝ Codex 功能整體停用，允許「不用 Codex 的部署」，服務照常
    #: 啟動——不是啟動失敗條件。有設定時才需要合法（見
    #: `apply_codex_config_rules()`）。空字串／全空白視同未設定。
    codex_runner_server: Optional[str] = None
    #: Goal 3 Phase D-1（docs/GOAL_3_FUTURE_WORK_PLAN.md）：Codex Runner
    #: pool。空 tuple＋`codex_runner_server` 有設定 → 正規化成單元素 pool
    #: （見 `apply_codex_config_rules()`）；兩者都設定時 `codex_runner_server`
    #: 必須是成員（它同時是所有單 Runner 舊呼叫面的 primary）。每個成員
    #: 沿用「必須是 servers.yaml 既有且 enabled 的 server」的啟動驗證。
    codex_runner_servers: tuple[str, ...] = ()
    #: Runner 上（相對 SSH user home）所有 Codex worktree、mirror、prompt、
    #: 輸出與 git bundle 的根目錄。
    codex_workspace_root: str = "~/codex_workspaces"
    #: 同時執行的 coding job 數上限。`codex_auth_mode == "chatgpt"` 時強制
    #: 降為 1（見 `apply_codex_config_rules()`）。
    codex_max_concurrency: int = 1
    #: true＝Runner 不接一般運算任務，只接 coding 或明確 pin 到 Runner 的
    #: 任務；false＝Runner 空閒可接一般任務，但 coding 優先（PLAN.md N.8）。
    codex_runner_reserve: bool = True
    #: 控制 workspace-write sandbox 是否允許網路（見 PLAN.md N.5）。
    codex_network_access: bool = False
    #: `"chatgpt"`｜`"api_key"`。非法值在 `apply_codex_config_rules()` 炸出
    #: （前提是 `codex_runner_server` 有設定）。
    codex_auth_mode: str = "chatgpt"

    def __post_init__(self) -> None:
        if self.authorization_mode not in AUTHORIZATION_MODES:
            raise ValueError(
                f"AUTHORIZATION_MODE={self.authorization_mode!r} is invalid; "
                "expected 'off' or 'shadow'"
            )

        _validate_cookie_name(self.session_cookie_name, "SESSION_COOKIE_NAME")
        _validate_cookie_name(self.oidc_flow_cookie_name, "OIDC_FLOW_COOKIE_NAME")
        if self.session_cookie_name == self.oidc_flow_cookie_name:
            raise ValueError(
                "SESSION_COOKIE_NAME and OIDC_FLOW_COOKIE_NAME must be distinct"
            )

        if isinstance(self.oidc_scopes, str):
            raise ValueError("OIDC_SCOPES must be a sequence of scope tokens")
        try:
            scopes = tuple(self.oidc_scopes)
        except TypeError as exc:
            raise ValueError("OIDC_SCOPES must be a sequence of scope tokens") from exc
        if any(
            not isinstance(scope, str)
            or not scope
            or scope.strip() != scope
            or any(character.isspace() for character in scope)
            for scope in scopes
        ):
            raise ValueError("OIDC_SCOPES contains an invalid scope token")
        self.oidc_scopes = tuple(dict.fromkeys(scopes))
        if "openid" not in self.oidc_scopes:
            raise ValueError("OIDC_SCOPES must contain openid")

        if isinstance(self.oidc_platform_admin_subjects, str):
            raise ValueError(
                "OIDC_PLATFORM_ADMIN_SUBJECTS must be a collection of exact subjects"
            )
        try:
            subjects = frozenset(self.oidc_platform_admin_subjects)
        except TypeError as exc:
            raise ValueError(
                "OIDC_PLATFORM_ADMIN_SUBJECTS must be a collection of exact subjects"
            ) from exc
        if any(
            not isinstance(subject, str) or not subject or subject.strip() != subject
            for subject in subjects
        ):
            raise ValueError("OIDC_PLATFORM_ADMIN_SUBJECTS contains an invalid subject")
        self.oidc_platform_admin_subjects = subjects

        for value, setting_name in (
            (self.oidc_login_flow_ttl_sec, "OIDC_LOGIN_FLOW_TTL_SEC"),
            (self.oidc_session_ttl_sec, "OIDC_SESSION_TTL_SEC"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{setting_name} must be greater than zero")
        if (
            isinstance(self.oidc_provider_timeout_sec, bool)
            or not isinstance(self.oidc_provider_timeout_sec, (int, float))
            or (
                isinstance(self.oidc_provider_timeout_sec, float)
                and not isfinite(self.oidc_provider_timeout_sec)
            )
            or self.oidc_provider_timeout_sec <= 0
        ):
            raise ValueError("OIDC_PROVIDER_TIMEOUT_SEC must be greater than zero")
        if (
            isinstance(self.oidc_clock_skew_leeway_sec, bool)
            or not isinstance(self.oidc_clock_skew_leeway_sec, int)
            or self.oidc_clock_skew_leeway_sec < 0
            or self.oidc_clock_skew_leeway_sec
            > _MAX_OIDC_CLOCK_SKEW_LEEWAY_SEC
        ):
            raise ValueError(
                "OIDC_CLOCK_SKEW_LEEWAY_SEC must be between zero and 300"
            )

        if self.oidc_enabled:
            required = {
                "OIDC_ISSUER": self.oidc_issuer,
                "OIDC_CLIENT_ID": self.oidc_client_id,
                "OIDC_CLIENT_SECRET": self.oidc_client_secret,
                "OIDC_REDIRECT_URI": self.oidc_redirect_uri,
            }
            missing = [
                name
                for name, value in required.items()
                if not isinstance(value, str) or not value.strip()
            ]
            if missing:
                raise ValueError(
                    "OIDC_ENABLED=true requires " + ", ".join(sorted(missing))
                )
            _validate_oidc_https_url(self.oidc_issuer, "OIDC_ISSUER")
            _validate_oidc_https_url(
                self.oidc_redirect_uri,
                "OIDC_REDIRECT_URI",
                callback=True,
            )

        if (
            self.engineering_task_backend_v1
            and not self.engineering_task_backend_v1_accept_unsandboxed_finalization
        ):
            raise ValueError(
                "ENGINEERING_TASK_BACKEND_V1=true requires "
                "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION=true "
                "until the D2 finalization sandbox is implemented "
                "(docs/AI_ENGINEERING_DECISION_GATE.md)"
            )
        if self.execution_attempt_new_claims_enabled and (
            not self.execution_attempt_reconcile_existing
            or not self.execution_outbox_worker_enabled
        ):
            raise ValueError(
                "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true requires "
                "EXECUTION_ATTEMPT_RECONCILE_EXISTING=true and "
                "EXECUTION_OUTBOX_WORKER_ENABLED=true"
            )
        if self.execution_attempt_ssh_launch_enabled and (
            not self.execution_attempt_new_claims_enabled
        ):
            # Fail at configuration time, before any background loop starts:
            # a launch path without new-claim ownership would dispatch work
            # nothing is responsible for reconciling.
            raise ValueError(
                "EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=true requires "
                "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true"
            )

    def get_server(self, name: str) -> Optional[ServerConfig]:
        for s in self.servers:
            if s.name == name:
                return s
        return None


def load_servers_yaml(path: str | Path) -> list[ServerConfig]:
    """讀取 servers.yaml，回傳 ServerConfig 列表。

    檔案不存在或內容為空時回傳空列表（讓系統可以先啟動、再補設定）。
    """
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw_servers = data.get("servers") or []
    servers: list[ServerConfig] = []
    for raw in raw_servers:
        servers.append(
            ServerConfig(
                name=raw["name"],
                host=raw["host"],
                user=raw["user"],
                key=raw["key"],
                gpu=bool(raw.get("gpu", False)),
                idle_gpu_util=float(raw.get("idle_gpu_util", 15.0)),
                idle_load=float(raw.get("idle_load", 2.0)),
                tags=list(raw.get("tags", [])),
                port=int(raw.get("port", 22)),
                project_roots=list(raw.get("project_roots", [])),
                dataset_roots=list(raw.get("dataset_roots", [])),
                project_embedded_dataset_names=list(
                    raw.get("project_embedded_dataset_names", DEFAULT_EMBEDDED_DATASET_NAMES)
                ),
                project_exclude_names=list(
                    raw.get("project_exclude_names", DEFAULT_EXCLUDE_NAMES)
                ),
                enabled=bool(raw.get("enabled", True)),
                note=raw.get("note"),
                execution_backend=str(raw.get("execution_backend", "ssh")),
            )
        )
    return servers


def load_dotenv(path: str | Path = ".env") -> None:
    """極簡 .env 載入：只解析 KEY=VALUE，忽略空行與 # 開頭註解。

    不會覆蓋已經存在的環境變數（shell/systemd 設的優先）。
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_app_config(
    servers_yaml_path: str | Path = "servers.yaml",
    dotenv_path: str | Path = ".env",
) -> AppConfig:
    load_dotenv(dotenv_path)
    #: `SERVERS_YAML_PATH`（同 DB_PATH/AUDIT_PATH 的既有慣例：環境變數優先於
    #: 呼叫端傳入的參數）：階段 8 第二批之後，這個路徑不只是讀取用，
    #: `app.server_config.write_servers_yaml_atomically()` 會在 server_add/
    #: server_update/server_disable/server_delete 核准時真的寫入這個檔案
    #: ——測試（`tests/conftest.py` 的 `api_client` fixture）必須用這個環境
    #: 變數把路徑導向 tmp_path，否則會不小心寫到專案根目錄的真實
    #: servers.yaml（見 tests/conftest.py 的說明）。
    servers_yaml_path = os.environ.get("SERVERS_YAML_PATH", str(servers_yaml_path))
    servers = load_servers_yaml(servers_yaml_path)
    return AppConfig(
        servers=servers,
        servers_yaml_path=str(servers_yaml_path),
        db_path=os.environ.get("DB_PATH", "jobqueue.db"),
        audit_path=os.environ.get("AUDIT_PATH", "audit.jsonl"),
        api_host=os.environ.get("API_HOST", "127.0.0.1"),
        api_port=int(os.environ.get("API_PORT", "8000")),
        monitor_interval_sec=int(os.environ.get("MONITOR_INTERVAL_SEC", "20")),
        scheduler_interval_sec=int(os.environ.get("SCHEDULER_INTERVAL_SEC", "10")),
        ssh_connect_timeout=int(os.environ.get("SSH_CONNECT_TIMEOUT", "10")),
        ssh_command_timeout=int(os.environ.get("SSH_COMMAND_TIMEOUT", "30")),
        ssh_max_concurrency=int(os.environ.get("SSH_MAX_CONCURRENCY", "8")),
        auth_token=os.environ.get("AUTH_TOKEN") or None,
        legacy_shared_token_enabled=os.environ.get(
            "LEGACY_SHARED_TOKEN_ENABLED", "true"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        service_token_auth_enabled=os.environ.get(
            "SERVICE_TOKEN_AUTH_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        authorization_mode=os.environ.get("AUTHORIZATION_MODE", "off"),
        session_cookie_name=(
            os.environ.get("SESSION_COOKIE_NAME", "dispatch_session").strip()
            or "dispatch_session"
        ),
        oidc_enabled=os.environ.get("OIDC_ENABLED", "false").strip().lower()
        in ("1", "true", "yes", "on"),
        oidc_issuer=os.environ.get("OIDC_ISSUER", "").strip() or None,
        oidc_client_id=os.environ.get("OIDC_CLIENT_ID", "").strip() or None,
        oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET") or None,
        oidc_redirect_uri=os.environ.get("OIDC_REDIRECT_URI", "").strip() or None,
        oidc_scopes=tuple(
            dict.fromkeys(
                os.environ.get("OIDC_SCOPES", "openid profile email").split()
            )
        ),
        oidc_platform_admin_subjects=frozenset(
            subject.strip()
            for subject in os.environ.get("OIDC_PLATFORM_ADMIN_SUBJECTS", "").split(",")
            if subject.strip()
        ),
        oidc_login_flow_ttl_sec=int(
            os.environ.get("OIDC_LOGIN_FLOW_TTL_SEC", "600")
        ),
        oidc_session_ttl_sec=int(os.environ.get("OIDC_SESSION_TTL_SEC", "28800")),
        oidc_flow_cookie_name=(
            os.environ.get("OIDC_FLOW_COOKIE_NAME", "dispatch_oidc_flow").strip()
            or "dispatch_oidc_flow"
        ),
        oidc_provider_timeout_sec=float(
            os.environ.get("OIDC_PROVIDER_TIMEOUT_SEC", "10")
        ),
        oidc_clock_skew_leeway_sec=int(
            os.environ.get("OIDC_CLOCK_SKEW_LEEWAY_SEC", "60")
        ),
        identity_admin_enabled=os.environ.get(
            "IDENTITY_ADMIN_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        execution_attempt_shadow_enabled=os.environ.get(
            "EXECUTION_ATTEMPT_SHADOW_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        execution_attempt_new_claims_enabled=os.environ.get(
            "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        execution_attempt_reconcile_existing=os.environ.get(
            "EXECUTION_ATTEMPT_RECONCILE_EXISTING", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        execution_outbox_worker_enabled=os.environ.get(
            "EXECUTION_OUTBOX_WORKER_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        execution_attempt_ssh_launch_enabled=os.environ.get(
            "EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        engineering_task_backend_v1=os.environ.get(
            "ENGINEERING_TASK_BACKEND_V1", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        engineering_task_backend_v1_accept_unsandboxed_finalization=os.environ.get(
            "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        controlled_coding_runner_v1=os.environ.get(
            "CONTROLLED_CODING_RUNNER_V1", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        run_profile_v1_enabled=os.environ.get(
            "RUN_PROFILE_V1_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        dispatch_policy_v1_enabled=os.environ.get(
            "DISPATCH_POLICY_V1_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        auto_placement_proposals_enabled=os.environ.get(
            "AUTO_PLACEMENT_PROPOSALS_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        auto_placement_interval_sec=int(
            os.environ.get("AUTO_PLACEMENT_INTERVAL_SEC", "300")
        ),
        auto_placement_cooldown_sec=int(
            os.environ.get("AUTO_PLACEMENT_COOLDOWN_SEC", "3600")
        ),
        auto_placement_kill_switch=os.environ.get(
            "AUTO_PLACEMENT_KILL_SWITCH", "true"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        server_observations_enabled=os.environ.get(
            "SERVER_OBSERVATIONS_ENABLED", "true"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        server_observation_retention_days=int(
            os.environ.get("SERVER_OBSERVATION_RETENTION_DAYS", "14")
        ),
        server_bootstrap_v1_enabled=os.environ.get(
            "SERVER_BOOTSTRAP_V1_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        dataset_prewarm_v1_enabled=os.environ.get(
            "DATASET_PREWARM_V1_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        dataset_prewarm_interval_sec=int(
            os.environ.get("DATASET_PREWARM_INTERVAL_SEC", "300")
        ),
        dataset_prewarm_cooldown_sec=int(
            os.environ.get("DATASET_PREWARM_COOLDOWN_SEC", "3600")
        ),
        dataset_prewarm_kill_switch=os.environ.get(
            "DATASET_PREWARM_KILL_SWITCH", "true"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        node_agent_v1_enabled=os.environ.get(
            "NODE_AGENT_V1_ENABLED", "false"
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        node_agent_lease_ttl_sec=float(
            os.environ.get("NODE_AGENT_LEASE_TTL_SEC", "120")
        ),
        node_agent_heartbeat_ttl_sec=float(
            os.environ.get("NODE_AGENT_HEARTBEAT_TTL_SEC", "60")
        ),
        node_agent_heartbeat_grace_sec=float(
            os.environ.get("NODE_AGENT_HEARTBEAT_GRACE_SEC", "60")
        ),
        node_canary_require_tag=os.environ.get("NODE_CANARY_REQUIRE_TAG", "").strip(),
        local_home_dir=os.environ.get("LOCAL_HOME_DIR", "."),
        dataset_reconcile_interval_sec=int(
            os.environ.get("DATASET_RECONCILE_INTERVAL_SEC", "3600")
        ),
        project_reconcile_interval_sec=int(
            os.environ.get("PROJECT_RECONCILE_INTERVAL_SEC", "3600")
        ),
        smtp_host=os.environ.get("SMTP_HOST") or None,
        smtp_port=int(os.environ["SMTP_PORT"]) if os.environ.get("SMTP_PORT") else None,
        smtp_user=os.environ.get("SMTP_USER") or None,
        smtp_pass=os.environ.get("SMTP_PASS") or None,
        mail_from=os.environ.get("MAIL_FROM") or None,
        mail_to=os.environ.get("MAIL_TO") or None,
        result_pull_timeout_sec=int(os.environ.get("RESULT_PULL_TIMEOUT_SEC", "600")),
        stall_minutes=int(os.environ.get("STALL_MINUTES", "30")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        llm_model=os.environ.get("LLM_MODEL", "claude-sonnet-5"),
        vllm_base_url=os.environ.get("VLLM_BASE_URL") or None,
        vllm_model=os.environ.get("VLLM_MODEL") or None,
        vllm_api_key=os.environ.get("VLLM_API_KEY") or None,
        agent_max_tool_steps=int(os.environ.get("AGENT_MAX_TOOL_STEPS", "6")),
        agent_max_concurrency=int(os.environ.get("AGENT_MAX_CONCURRENCY", "2")),
        agent_tool_result_max_chars=int(
            os.environ.get("AGENT_TOOL_RESULT_MAX_CHARS", "4000")
        ),
        allow_root_ssh=os.environ.get("ALLOW_ROOT_SSH", "").strip().lower()
        in ("1", "true", "yes", "on"),
        ssh_key_allowed_dirs=(
            [p.strip() for p in os.environ["SSH_KEY_ALLOWED_DIRS"].split(",") if p.strip()]
            if os.environ.get("SSH_KEY_ALLOWED_DIRS")
            else ["~/.ssh"]
        ),
        web_direct_execute=os.environ.get("WEB_DIRECT_EXECUTE", "true").strip().lower()
        in ("1", "true"),
        auto_approve_rules_path=os.environ.get(
            "AUTO_APPROVE_RULES_PATH", "auto_approve.yaml"
        ),
        codex_runner_server=os.environ.get("CODEX_RUNNER_SERVER", "").strip() or None,
        codex_runner_servers=tuple(
            name.strip()
            for name in os.environ.get("CODEX_RUNNER_SERVERS", "").split(",")
            if name.strip()
        ),
        codex_workspace_root=os.environ.get(
            "CODEX_WORKSPACE_ROOT", "~/codex_workspaces"
        ),
        codex_max_concurrency=int(os.environ.get("CODEX_MAX_CONCURRENCY", "1")),
        codex_runner_reserve=os.environ.get("CODEX_RUNNER_RESERVE", "true").strip().lower()
        in ("1", "true"),
        codex_network_access=os.environ.get("CODEX_NETWORK_ACCESS", "").strip().lower()
        in ("1", "true"),
        codex_auth_mode=os.environ.get("CODEX_AUTH_MODE", "chatgpt"),
    )


def apply_codex_config_rules(config: AppConfig, server_enabled: dict[str, bool]) -> list[str]:
    """驗證＋就地正規化六個 CODEX_* 設定（PLAN.md N.1）。

    - `config.codex_runner_server is None`：**未設定＝ Codex 功能整體停用**
      （2026-07-10 使用者修訂——允許「不用 Codex 的部署」，服務照常啟動）。
      這個分支完全不做事、回傳空 list；呼叫端（`app.main` 的啟動流程）不
      應該因為沒設定 Runner 就失敗。
    - `config.codex_runner_server` 有設定時，值必須是 `servers.yaml` 既有
      的 server name：`server_enabled` 是 `{server_name: enabled}`（呼叫端
      從 servers.yaml 載入結果組出來，`enabled` 對應 `ServerConfig.enabled`）。
      不存在／`enabled=False` 都 `raise ValueError`。`codex_auth_mode` 不是
      `"chatgpt"`／`"api_key"` 也 `raise ValueError`。
      取捨（PLAN.md N.13）：`config.py` 本身沿用既有的寬鬆載入慣例（其他
      設定解析失敗多半是降級，不擋啟動），這裡是唯一的例外——**設了
      Runner 就要設對**，啟動失敗（吵起來）比默默停用一個使用者以為已經
      開啟的功能好。
    - `codex_auth_mode == "chatgpt"` 且 `codex_max_concurrency > 1`：
      ChatGPT-managed 登入模式強制序列化，**就地把 `config.codex_max_concurrency`
      降為 1**，回傳的 list 加一條 warning 文字（呼叫端照 `app.main` 既有
      的「設定解析失敗即降級」log 慣例逐條印出，例如 servers.yaml／
      auto_approve.yaml 解析失敗時的作法）。`api_key` 模式本階段不提高
      （PLAN.md N.15），這裡不做任何事。

    回傳值：warning 訊息字串 list（可能為空）。硬性錯誤一律用例外，不塞進
    這個 list。
    """
    warnings: list[str] = []

    # Goal 3 Phase D-1：pool 正規化（去重保序）。三種相容組合：
    # 1. 只設 CODEX_RUNNER_SERVER → pool = (server,)（單 Runner 舊語意不變）。
    # 2. 只設 CODEX_RUNNER_SERVERS → primary = pool[0]（決定性），所有既有
    #    單 Runner 呼叫面沿用 primary。
    # 3. 兩者都設 → CODEX_RUNNER_SERVER 必須是 pool 成員，否則啟動失敗。
    deduped: list[str] = []
    for name in config.codex_runner_servers:
        if name not in deduped:
            deduped.append(name)
    config.codex_runner_servers = tuple(deduped)

    if config.codex_runner_server is None and not config.codex_runner_servers:
        return warnings
    if not config.codex_runner_servers:
        config.codex_runner_servers = (config.codex_runner_server,)
    elif config.codex_runner_server is None:
        config.codex_runner_server = config.codex_runner_servers[0]
    elif config.codex_runner_server not in config.codex_runner_servers:
        raise ValueError(
            f"CODEX_RUNNER_SERVER={config.codex_runner_server!r} 不在 "
            f"CODEX_RUNNER_SERVERS={list(config.codex_runner_servers)!r} 之中："
            "兩者同時設定時 primary 必須是 pool 成員"
        )

    for server_name in config.codex_runner_servers:
        if server_name not in server_enabled:
            raise ValueError(
                f"CODEX_RUNNER_SERVER(S)={server_name!r} 找不到對應的機器："
                "每個 Runner 必須是 servers.yaml 既有的 server"
            )
        if not server_enabled[server_name]:
            raise ValueError(
                f"CODEX_RUNNER_SERVER(S)={server_name!r} 對應的機器 enabled=false："
                "每個 Runner 必須是 servers.yaml 既有的 server"
            )
    if config.codex_auth_mode not in ("chatgpt", "api_key"):
        raise ValueError(
            f"CODEX_AUTH_MODE={config.codex_auth_mode!r} 不合法，"
            "必須是 'chatgpt' 或 'api_key'"
        )

    if config.codex_auth_mode == "chatgpt" and config.codex_max_concurrency > 1:
        original = config.codex_max_concurrency
        config.codex_max_concurrency = 1
        warnings.append(
            f"ChatGPT 登入模式強制單一序列化 coding job，"
            f"CODEX_MAX_CONCURRENCY 已由 {original} 降為 1"
        )

    return warnings
