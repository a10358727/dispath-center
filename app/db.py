"""SQLite 層：schema 建立與存取。

選擇：使用標準函式庫 `sqlite3`（同步）+ `threading.Lock` 序列化寫入，
不用 aiosqlite。原因：階段 1 佇列操作量很小（幾十筆任務等級），用同步
sqlite3 搭配單一鎖最簡單可靠；非同步呼叫端（FastAPI route、scheduler
迴圈）用 `asyncio.to_thread()` 呼叫這裡的同步函式即可，不需要在整個
專案中維護兩套 DB API。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from app.execution_attempt_schema import (
    EXECUTION_ATTEMPT_POST_MIGRATION_SCHEMA,
    EXECUTION_ATTEMPT_SCHEMA,
)
from app.execution_contract import (
    canonical_json,
    canonical_json_sha256,
    utf8_sha256,
    validate_execution_contract,
)
from app.identity import (
    Actor,
    ActorSession,
    ActorType,
    OIDCIdentity,
    OIDCLoginFlow,
    ProjectMembership,
    ProjectRole,
    ServiceAccount,
    ServiceAccountToken,
)

VALID_STATUSES = {"queued", "running", "done", "failed", "blocked", "cancelled"}
VALID_PRIORITIES = {"normal", "low"}
EXECUTION_ATTEMPT_ACTIVE_STATES = frozenset({"leased", "dispatching", "running"})
EXECUTION_ATTEMPT_TERMINAL_STATES = frozenset(
    {"done", "failed", "expired", "abandoned_before_launch"}
)
EXECUTION_ATTEMPT_TRANSITIONS = {
    "leased": frozenset({"dispatching", "expired"}),
    "dispatching": frozenset(
        {"running", "done", "failed", "abandoned_before_launch"}
    ),
    "running": frozenset({"done", "failed"}),
}
EXECUTION_OPERATION_TRANSITIONS = {
    "pending": frozenset({"processing", "failed"}),
    "processing": frozenset(
        {"pending", "delivered", "uncertain", "failed"}
    ),
    "uncertain": frozenset({"delivered", "failed"}),
}
EXECUTION_REASON_CODES = frozenset(
    {
        "approval_missing",
        "approval_not_approved",
        "approval_kind_mismatch",
        "contract_digest_mismatch",
        "target_revision_missing",
        "target_identity_mismatch",
        "leader_lease_lost",
        "claim_conflict",
        "contract_validated",
        "remote_state_observed",
        "pre_effect_definite_failure",
        "effect_outcome_unknown",
        "remote_unreachable",
        "terminal_evidence_valid",
        "security_credential_revoked",
        "manual_recovery_hold",
    }
)
# WP-2B pinned these exact read-only remote commands; an inspect outbox
# operation carries no mutation approval, so only these kinds are accepted.
EXECUTION_INSPECT_COMMAND_KINDS = frozenset(
    {"attempt_inspect", "attempt_arbitrate"}
)
LEGACY_STOP_UNRESOLVED_STATES = frozenset(
    {"requested", "delivery_uncertain", "delivered"}
)
PINNED_EXECUTION_CONTRACTS = {
    "enqueue": "enqueue-execution-v1",
    "auto_placement": "auto-placement-execution-v1",
    "dataset_prewarm": "dataset-prewarm-execution-v1",
}
PINNED_SERVER_CONFIG_KINDS = frozenset(
    {"server_add", "server_update", "server_disable", "server_delete"}
)
#: 階段 3 新增 "setup"：機器首次跑某專案時的 git clone/pull + setup_cmd 依賴任務。
#: 階段 13 新增 "coding"：AI 改碼層次二（Codex Worker），見 app/approvals.py
#: 的 request_coding_task_approval()／approve() 的 coding_task 分支。
VALID_TYPES = {"train", "sync", "adhoc", "setup", "coding"}

#: 階段 2：approvals 表（PLAN.md C 節）。核准對象不只「排任務」——停止任務
#: （本階段）、同步計畫（階段 3）、聊天 enqueue 卡片（階段 5）都走同一套
#: 核准，因此用獨立表而不是在 jobs 上加 pending 狀態。
#: 階段 8（第一批）：新增 import_project/ignore_project_candidate/
#: inventory_scan 三個 kind（PLAN.md I 節）。
#: 階段 8（第二批）：新增 server_add/server_update/server_disable/
#: server_delete 四個 kind（Web Server Management，見 app/server_config.py／
#: app/approvals.py）。
#: 階段 12（PLAN.md M 節）：新增 apply_patch——AI 改碼層次一（讀檔＋diff
#: 核准卡）。**這個 kind 永遠不進 `app.approvals.maybe_auto_approve()` 的
#: 自動核准白名單**（該函式只認 "enqueue"/"stop"，見其 docstring）——人必須
#: 逐個看過 diff 才能核准，不允許使用者自己的規則檔繞過。
#: 階段 13（PLAN.md N 節）：新增 coding_task——AI 改碼層次二（Codex
#: Worker）。理由同 apply_patch：**這個 kind 也永遠不進自動核准白名單**，
#: instruction 是自然語言、agent 有真實行動力（跑指令/裝套件/連網），人必須
#: 逐個核准。
#: 階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）：新增
#: ignore_nested_candidates——一次核准把「路徑位於其他非 ignored 候選之下」
#: 的 pending 候選全部標 ignored 的批次卡（維持「候選整理走核准」的既有
#: 一致性，不是繞過核准的直接執行）。同樣不進自動核准白名單（`maybe_
#: auto_approve()` 只認 "enqueue"/"stop"，天然排除，見該函式 docstring）。
#: 階段 15 Phase B（PLAN.md P.2.1 節）：新增 git_init——把一個尚未受 git
#: 管理的 project_instance 就地初始化成 git repo（寫 .gitignore／
#: `git init`／`git add -A`／size guard／commit）。理由同 apply_patch／
#: coding_task：**這個 kind 也永遠不會被 `maybe_auto_approve()` 自動核准**
#: （白名單只認 "enqueue"/"stop"，天然排除）——即使 size guard 通過，
#: 「把哪個目錄變成 git repo」仍然是人必須看過 .gitignore 內容才能決定的
#: 動作。
#: 階段 15 Phase C（PLAN.md P.3 節）：新增 project_deploy——把中央 hub
#: 的某個 ref 部署成另一台機器上一個全新的 project_instance（bundle 建立
#: →rsync 推送→目標機 clone）。理由同 git_init：**這個 kind 也永遠不會被
#: `maybe_auto_approve()` 自動核准**（白名單只認 "enqueue"/"stop"，天然
#: 排除）——把哪個目錄部署到哪台機器仍然是人必須核准的動作。
#: Goal 1 / Slice 6 身分與成員資格管理 kinds 也必須保持核准門槛；
#: 這些 kind 永遠不加入只允許 enqueue/stop 的自動核准白名單。
#: Goal 2 Slice 3（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：新增
#: dispatch_policy_create/_update/_archive——完全比照 Run Profile v1 的
#: immutable-revision 模式。這個切片的政策物件**沒有任何運行時效果**
#: （scheduler 完全不讀 dispatch_policies），但一樣走核准制，**永遠不加入
#: 只允許 enqueue/stop 的自動核准白名單**——之後若要讓政策範圍內自動放置
#: （Slice 4/5）成立，需要獨立、明確裁定後才存在的機制，不是擴大這裡的
#: 白名單。
VALID_APPROVAL_KINDS = {
    "enqueue",
    "stop",
    "import_project",
    "ignore_project_candidate",
    "inventory_scan",
    "server_add",
    "server_update",
    "server_disable",
    "server_delete",
    "apply_patch",
    "coding_task",
    "engineering_task_retry",
    "engineering_task_discard",
    "ignore_nested_candidates",
    "git_init",
    "project_deploy",
    "service_account_create",
    "service_token_issue",
    "service_token_revoke",
    "project_membership_upsert",
    "project_membership_remove",
    "run_profile_create",
    "run_profile_update",
    "run_profile_archive",
    "dispatch_policy_create",
    "dispatch_policy_update",
    "dispatch_policy_archive",
    #: Goal 2 Slice 4(docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md,DG-1 核准見
    #: docs/DECISIONS.md 2026-07-18):policy-driven placement proposal——
    #: background loop 找到符合政策的閒置伺服器時建立這個 kind 的 pending
    #: approval,人核准了才真的建 job。**這個 kind 也永遠不會被
    #: `maybe_auto_approve()` 自動核准**(Slice 5 才會有獨立、明確裁定後的
    #: 機制;這裡不擴大只認 "enqueue"/"stop" 的白名單)。
    "auto_placement",
    #: Goal 3 Phase B（docs/GOAL_3_FUTURE_WORK_PLAN.md；DG-B 核准見
    #: docs/DECISIONS.md 2026-07-19）：對「還不在 servers.yaml 的空機器」
    #: 執行審閱過的固定 bootstrap 腳本（SHA-256 pin 在 payload）＋ read-only
    #: capability check。同樣永遠不在 `maybe_auto_approve()` 白名單。
    "server_bootstrap",
    #: Goal 3 Phase B4（docs/DG_B4_DATASET_PREWARM_DRAFT.md；DG-B4 核准見
    #: docs/DECISIONS.md 2026-07-25）：對「剛開通、dataset_cache 還空的機器」
    #: 提案預先同步一份常用資料集。核准後走既有 `build_sync_script()` /
    #: `type="sync"` job 路徑（不另造通道）。同樣**永遠不在**
    #: `maybe_auto_approve()` 白名單——一律要人工點一次。
    "dataset_prewarm",
    #: Goal 3 Phase C / C2（INV-NODE-1，DG-C 核准 2026-07-19）：Node Agent
    #: 身分的登錄與撤銷。核發憑證是狀態變更，因此一樣走核准流程；raw token
    #: 只在核准回應裡出現一次，永不落庫、不進稽核。兩者都**永遠不在**
    #: `maybe_auto_approve()` 白名單。
    "node_enroll",
    "node_revoke",
    #: Goal 3 C3（roadmap Phase 3 的 "rotation"）：替既有 node 換發憑證，
    #: 保留同一個身分與 attempt 歸屬。同樣不在自動核准白名單。
    "node_rotate",
    #: Goal 3 D-3（2026-07-16 D3 裁定預告：「隨 D1 adapter 一起定義」）：
    #: app-server 在 turn 中途要求執行單一指令時的人工核准。payload 綁定
    #: `CodingAgentCommandApprovalHandle` 的不可變欄位（command_digest、
    #: working_directory、thread/turn/item、parent_approval_id）。
    #:
    #: **這個 kind 只記錄人的決定，不自己送回 app-server**——送回需要當下
    #: 那個 in-memory session（見 `app/codex_app_server.py`
    #: `respond_to_command_approval()`），不是可持久化的東西。
    #: 同樣**永遠不在** `maybe_auto_approve()` 白名單。
    "engineering_command",
}
VALID_APPROVAL_STATUSES = {"pending", "approved", "rejected"}

#: D5 Run Profile v1（docs/DECISIONS.md, docs/AI_ENGINEERING_DECISION_GATE.md）：
#: `run_profiles` 一列的狀態值域。"approved" 表示這個 revision 目前可選用；
#: "archived" 是同一個 (project_id, name) 底下較新的一列，表示該 profile
#: 已下架——不刪除、不改寫舊列，歷史 revision 永遠可查（同 ProjectVersion
#: 不可變精神）。這三個 kind 永遠不加入只允許 enqueue/stop 的自動核准白名單。
VALID_RUN_PROFILE_STATUSES = {"approved", "archived"}

#: Goal 2 Slice 3（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：`dispatch_policies`
#: 一列的狀態值域，同 `VALID_RUN_PROFILE_STATUSES` 的不可變 revision 精神
#: ——"approved" 是目前可選用的政策，"archived" 是同一個 (project_id, name)
#: 底下較新的一列表示下架，不刪除、不改寫舊列。
VALID_DISPATCH_POLICY_STATUSES = {"approved", "archived"}

#: 階段 8：project_candidates.status 值域（PLAN.md I.1）。
VALID_CANDIDATE_STATUSES = {"pending", "imported", "ignored"}
#: 階段 8：projects.dataset_mode 值域，Python 層驗證（PLAN.md I.1）。
#: "embedded" 表示資料就放在專案目錄底下（掃描到的 embedded dataset），
#: 不透過 datasets/dataset_cache 表管理、不自動同步——見
#: app/approvals.py 的 import_project 核准分支說明。
VALID_DATASET_MODES = {"none", "registered", "embedded"}

#: 專案詳情頁（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
#: 方法／進度管理」計畫第 1 節）：experiment_records.kind 值域，Python 層
#: 驗證，比照 `VALID_DATASET_MODES`。note＝一般筆記、observation＝觀察、
#: conclusion＝結論、decision＝決策——純粹給人／agent 分類用，不影響任何
#: 排程或核准邏輯。
VALID_RECORD_KINDS = {"note", "observation", "conclusion", "decision"}

#: PLAN.md 2026-07-11 版 §14 切片 1（Project identity migration）：
#: project_instances.state 值域（§6.1）。本批只做 schema readiness——新增
#: 欄位、預設 'unknown'、Python 層驗證；真正的 available/missing/dirty/
#: diverged 判定與轉移由切片 2（instance reconciliation）接手,在那之前
#: 既有列一律維持 'unknown'（誠實表示「還沒有 reconcile 過」）。
VALID_INSTANCE_STATES = {"available", "missing", "dirty", "diverged", "unknown"}
VALID_CODING_BASE_BINDINGS = {"legacy_unpinned", "project_version_pinned"}
VALID_ENGINEERING_EVENT_SOURCES = {
    "task",
    "approval",
    "job",
    "coding_run",
    "collector",
    "reconciler",
    "system",
}
VALID_ENGINEERING_COMMAND_ROLES = {
    "staging",
    "agent_turn",
    "validation",
    "worker_validation",
}
VALID_ENGINEERING_EXECUTION_LOCATIONS = {"server_a", "coding_runner", "worker"}
VALID_ENGINEERING_POLICY_DISPOSITIONS = {
    "task_approved",
    "separate_approval_required",
    "prohibited",
    "legacy_unknown",
}
VALID_ENGINEERING_STATUS_SOURCES = {"job", "coding_run", "recorded"}
VALID_ENGINEERING_ARTIFACT_AVAILABILITY = {
    "pending",
    "available",
    "missing",
    "cleaned",
    "withheld",
    "rejected",
}
VALID_ENGINEERING_ARTIFACT_VERIFICATION = {
    "not_required",
    "pending",
    "verified",
    "rejected",
    "unknown",
}
VALID_ENGINEERING_ARTIFACT_REDACTION = {
    "not_applicable",
    "pending",
    "redacted",
    "withheld",
}


#: Mirrors ``app.approvals.CODING_RUN_TERMINAL_STATUSES``.  Duplicated (rather
#: than imported) because ``app.db`` is a lower layer than ``app.approvals``;
#: keep both sets in sync when either changes.
_CODING_RUN_TERMINAL_STATUSES = {
    "done",
    "failed",
    "no_changes",
    "secret_violation",
    "path_policy_violation",
}

_ENGINEERING_STATUS_PHASES = {
    "pending_approval": "approval",
    "rejected": "approval",
    "queued": "queue",
    "staging": "staging",
    "staging_failed": "staging",
    "running": "execution",
    "finalizing": "finalization",
    "done": "complete",
    "no_changes": "complete",
    "failed": "complete",
    "secret_violation": "complete",
    "path_policy_violation": "complete",
    "blocked": "complete",
    "cancelled": "complete",
    "interrupted": "complete",
    "disconnected": "connectivity",
    "discarded": "complete",
    "unknown": "unknown",
}

_ENGINEERING_STATUS_SUMMARIES = {
    "pending_approval": "等待人工核准",
    "rejected": "核准請求已拒絕",
    "queued": "執行計畫已排入佇列",
    "staging": "正在準備不可變基準與指令",
    "staging_failed": "不可變基準準備失敗",
    "running": "程式代理正在隔離工作區執行",
    "finalizing": "正在驗證並收集執行結果",
    "done": "AI Engineering Task 已完成",
    "no_changes": "任務完成，沒有產生程式變更",
    "failed": "AI Engineering Task 執行失敗",
    "secret_violation": "AI Engineering Task 因安全檢查違規而拒絕",
    "path_policy_violation": "AI Engineering Task 因路徑政策違規而拒絕",
    "blocked": "AI Engineering Task 因相依條件而受阻",
    "cancelled": "AI Engineering Task 已取消",
    "interrupted": "AI Engineering Task 執行中斷",
    "disconnected": "Coding Runner 目前無法連線",
    "discarded": "AI Engineering Task 已作廢",
    "unknown": "AI Engineering Task 狀態尚未確認",
}

_ENGINEERING_VALIDATION_STATUS_SUMMARIES = {
    "requested": "Worker validation 已提出，等待 enqueue 核准",
    "queued": "Worker validation 已排入 ordinary Job 佇列",
    "running": "Worker validation ordinary Job 執行中",
    "done": "Worker validation ordinary Job 已完成",
    "failed": "Worker validation ordinary Job 執行失敗",
    "blocked": "Worker validation ordinary Job 因相依條件受阻",
    "cancelled": "Worker validation ordinary Job 已取消",
    "rejected": "Worker validation enqueue 核准請求已拒絕",
    "unknown": "Worker validation 狀態證據不完整",
}

_ENGINEERING_VALIDATION_TERMINAL_RESULT_STATUSES = {
    "done",
    "failed",
    "cancelled",
}


def _safe_engineering_validation_exit_code(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 255 else None


def _safe_engineering_validation_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return value


def _engineering_phase_for_status(status: str) -> str:
    """Return a presentation phase without collapsing unknown connectivity into failure."""

    return _ENGINEERING_STATUS_PHASES.get(status, "unknown")


def _engineering_status_summary(status: str) -> str:
    return _ENGINEERING_STATUS_SUMMARIES.get(status, f"任務狀態更新為 {status}")


def _is_full_hex_digest(value: str) -> bool:
    return len(value) in {40, 64} and all(char in "0123456789abcdefABCDEF" for char in value)


def _validate_record_author(author: str) -> None:
    """`experiment_records.author` 值域：`"user"`（網頁使用者手動填）或以
    `"agent"` 開頭的字串（`"agent"`＝本地 vLLM，`"agent:chatgpt"`＝MCP
    Bridge，見 `app/agent_tools.py`／`app/mcp_bridge.py`）——用前綴比對而
    不是固定集合，因為 agent 來源未來可能再細分，不想每加一種來源就要改
    這裡的值域常數。"""
    if author != "user" and not author.startswith("agent"):
        raise ValueError(f"invalid experiment record author: {author}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'adhoc',
    project TEXT,
    command TEXT NOT NULL,
    require_tag TEXT,
    pin_server TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    gpus_needed INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    server TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    log_tail TEXT,
    target_server TEXT,
    dataset_name TEXT,
    dataset_version TEXT,
    log_size INTEGER,
    log_size_changed_at TEXT,
    stalled_suspect INTEGER NOT NULL DEFAULT 0,
    stall_notified INTEGER NOT NULL DEFAULT 0,
    source_coding_run_id INTEGER,
    engineering_task_id TEXT,
    engineering_task_role TEXT,
    engineering_attempt_number INTEGER,
    engineering_validation_request_id TEXT,
    auto_placement_approval_id INTEGER,
    execution_approval_id INTEGER,
    approved_payload_sha256 TEXT,
    execution_contract_version TEXT,
    execution_contract_role TEXT,
    approved_command_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    note TEXT,
    requester_actor_id TEXT,
    decision_actor_id TEXT,
    decision_mechanism TEXT,
    payload_sha256 TEXT,
    payload_contract_version TEXT,
    payload_immutable_at TEXT,
    materialization_started_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- Goal 1 / Slice 1: durable actor identity and authorization-shadow
-- persistence.  These tables are additive and intentionally avoid foreign-key
-- cascades so that existing deletion and legacy-row behavior does not change.
CREATE TABLE IF NOT EXISTS actors (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    platform_admin INTEGER NOT NULL DEFAULT 0,
    disabled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actors_actor_type ON actors(actor_type);
CREATE INDEX IF NOT EXISTS idx_actors_platform_admin ON actors(platform_admin);

CREATE TABLE IF NOT EXISTS oidc_identities (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT,
    created_at TEXT NOT NULL,
    last_authenticated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oidc_identities_issuer_subject
    ON oidc_identities(issuer, subject);
CREATE INDEX IF NOT EXISTS idx_oidc_identities_actor_id ON oidc_identities(actor_id);

CREATE TABLE IF NOT EXISTS oidc_login_flows (
    state_hash TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL,
    pkce_verifier TEXT,
    return_to TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS actor_sessions (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    oidc_identity_id TEXT,
    secret_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_actor_sessions_secret_hash
    ON actor_sessions(secret_hash);
CREATE INDEX IF NOT EXISTS idx_actor_sessions_actor_id ON actor_sessions(actor_id);
CREATE INDEX IF NOT EXISTS idx_actor_sessions_expires_at ON actor_sessions(expires_at);

CREATE TABLE IF NOT EXISTS service_accounts (
    actor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_by_actor_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_service_accounts_name ON service_accounts(name);

CREATE TABLE IF NOT EXISTS service_account_tokens (
    id TEXT PRIMARY KEY,
    service_account_actor_id TEXT NOT NULL,
    label TEXT,
    secret_hash TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    created_by_actor_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_service_account_tokens_secret_hash
    ON service_account_tokens(secret_hash);
CREATE INDEX IF NOT EXISTS idx_service_account_tokens_account
    ON service_account_tokens(service_account_actor_id);
CREATE INDEX IF NOT EXISTS idx_service_account_tokens_expires_at
    ON service_account_tokens(expires_at);

CREATE TABLE IF NOT EXISTS project_memberships (
    project_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_by_actor_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, actor_id)
);
CREATE INDEX IF NOT EXISTS idx_project_memberships_actor_id
    ON project_memberships(actor_id);

-- 階段 3：專案／資料集註冊表、快取地圖（PLAN.md D 節）

-- PLAN.md 2026-07-11 版 §14 切片 1：`id` 是 Project 的正式跨系統身分
-- （UUID，INV-PROJECT-1 草案）；`name` 過渡期仍是 PRIMARY KEY 與既有外鍵
-- 的實際鍵（legacy name adapter 雙讀,見 get_project()）,等雙軌驗證完成
-- 才移除 name 引用（PLAN.md §12）。唯一索引在 _init_schema() 內建立
-- （不能寫在這裡：舊 DB 要先 ALTER TABLE 補上 id 欄,executescript 先跑
-- 會因欄位不存在而失敗）。
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    id TEXT,
    repo_or_path TEXT NOT NULL,
    dataset_name TEXT,
    dataset_version TEXT,
    default_command TEXT,
    require_tag TEXT,
    setup_cmd TEXT,
    created_at TEXT NOT NULL,
    goal TEXT,
    optimization_notes TEXT,
    progress TEXT
);

-- 資料集用「檔案清單＋各檔大小」的 manifest 代替全量 hash（大檔算 checksum
-- 太貴）；manifest 存成 JSON：{"file_count": N, "total_size": N,
-- "files": [{"path": rel_path, "size": bytes}, ...]}。取捨在 README 說明。
-- 階段 16（PLAN.md Q 節）：新增 `card`（JSON，nullable，資料卡——見
-- app/datasets.py 的 build_dataset_card()/render_dataset_card()，形狀
-- {description, method, derived_from, counts_custom, created_at,
-- updated_at}；階段 16 之前建立的舊版本一律是 NULL，查詢不報錯，見
-- GET /datasets/{name}/{version}/card 的 note 欄位）與 `sync_mode`
-- （'packed'｜'rolling'，Q.1/Q.2；本批只存取，不使用——rolling 模式的
-- 實際行為由後續批次接手）。
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    card TEXT,
    sync_mode TEXT NOT NULL DEFAULT 'packed',
    PRIMARY KEY (name, version)
);

-- 快取地圖：記錄每台機器已有哪些「資料集＠版本」。來源兩個：sync 任務成功
-- 後登記；每小時對各機 `ls datasets/*/*/` 校正一次。
CREATE TABLE IF NOT EXISTS dataset_cache (
    server TEXT NOT NULL,
    dataset TEXT NOT NULL,
    version TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (server, dataset, version)
);

-- 階段 8（第一批）：Project Inventory（PLAN.md I.1）。掃描是唯讀的；
-- candidate 的 id 是 server+path 的穩定 hash（見 make_candidate_id()），
-- 重複掃描是 upsert，不是每次新增一列。
CREATE TABLE IF NOT EXISTS project_candidates (
    id TEXT PRIMARY KEY,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    name_guess TEXT,
    kind TEXT NOT NULL DEFAULT 'project',
    git_remote TEXT,
    git_branch TEXT,
    git_commit TEXT,
    markers TEXT NOT NULL DEFAULT '[]',
    readme_excerpt TEXT,
    command_guess TEXT,
    embedded_data_paths TEXT NOT NULL DEFAULT '[]',
    embedded_data_summary TEXT,
    estimated_data_bytes INTEGER,
    excluded_paths TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_candidates_server_path
    ON project_candidates(server, path);
CREATE INDEX IF NOT EXISTS idx_project_candidates_status ON project_candidates(status);

-- id 是 project_name+server+path 的穩定 hash（見 make_instance_id()）：
-- 同一個專案在同一台機器同一個路徑只會有一筆，重覆匯入/掃描是 upsert。
-- 切片 1 補 `project_id`（projects.id 的 UUID,過渡期與 project_name 並存
-- 雙寫）與 `state`（值域見 VALID_INSTANCE_STATES；判定邏輯屬切片 2,本批
-- 一律 'unknown'）。
CREATE TABLE IF NOT EXISTS project_instances (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_id TEXT,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    git_remote TEXT,
    git_branch TEXT,
    git_commit TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    embedded_data_paths TEXT NOT NULL DEFAULT '[]',
    last_seen TEXT,
    state TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_project_instances_project_name
    ON project_instances(project_name);

-- PLAN.md 2026-07-11 版 §14 切片 4(canonical version service):一個
-- ProjectVersion＝某專案在某個時間點的不可變 hub commit(INV-PROJECT-2
-- 草案)。全新表,不需要 ALTER TABLE 遷移(同 coding_runs/
-- experiment_records 的既有慣例)。`(project_name, git_commit)` 唯一
-- ——同一個 commit 只會有一筆,重複遇到（hub_sync 重跑／deploy 用到已
-- 存在的 commit）回既有列,不新增、不覆寫(commit 的身分不可變)。
-- `source_instance_id` 記錄「這個 commit 是從哪個 project_instance 同步
-- 上來的」,project_deploy 是反方向(hub → 新 instance),沒有來源
-- instance,一律 NULL。
CREATE TABLE IF NOT EXISTS project_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    project_name TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    git_ref TEXT,
    source_instance_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_versions_project_commit
    ON project_versions(project_name, git_commit);
CREATE INDEX IF NOT EXISTS idx_project_versions_project_id
    ON project_versions(project_id);

-- D5 Run Profile v1（docs/DECISIONS.md）：additive schema，approval-gated
-- create/update/archive。每一列是一個不可變 revision——update/archive 永遠
-- INSERT 新的一列（revision = 目前該 (project_id, name) 最大 revision + 1），
-- 從不 UPDATE 既有列的 command/setup_cmd/require_tag/status。「目前狀態」＝
-- 該 (project_id, name) revision 最大的那一列（見 get_run_profile_head()）。
-- 既有 Project.default_command/setup_cmd/require_tag 保持相容欄位，這張表
-- 不會回填一筆假造的已核准 profile 去代表它們（docs/DECISIONS.md D5）。
CREATE TABLE IF NOT EXISTS run_profiles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    command TEXT,
    setup_cmd TEXT,
    require_tag TEXT,
    supersedes_id TEXT,
    approval_id INTEGER,
    created_by_actor_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_profiles_project_name_revision
    ON run_profiles(project_id, name, revision);
CREATE INDEX IF NOT EXISTS idx_run_profiles_project_name
    ON run_profiles(project_id, name);

-- Goal 2 Slice 3（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：Dispatch Policy
-- v1，完全比照 run_profiles 的不可變 revision 模式——update/archive 永遠
-- INSERT 新的一列，從不 UPDATE 既有列。**這個切片的政策物件沒有任何運行時
-- 效果**：scheduler 完全不讀這張表（Slice 4 才會新增唯讀評估步驟）。
-- `allowed_servers` 是 JSON 陣列（精確伺服器名清單）；`run_profile_id`
-- 是可選的、指到 run_profiles 某一列 id 的精確 revision 參照（不是
-- (project, name) 這種會漂移的頭部參照）。
CREATE TABLE IF NOT EXISTS dispatch_policies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    allowed_servers TEXT NOT NULL,
    require_tag TEXT,
    run_profile_id TEXT,
    dataset_required INTEGER NOT NULL DEFAULT 0,
    max_concurrent_placements INTEGER NOT NULL DEFAULT 1,
    valid_until TEXT,
    approval_id INTEGER,
    created_by_actor_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_policies_project_name_revision
    ON dispatch_policies(project_id, name, revision);
CREATE INDEX IF NOT EXISTS idx_dispatch_policies_project_name
    ON dispatch_policies(project_id, name);

-- AI Engineering Task backend v1: immutable parent request.  Historical
-- coding_runs are intentionally not backfilled into this table because their
-- approved base revision was not pinned.
CREATE TABLE IF NOT EXISTS engineering_tasks (
    id TEXT PRIMARY KEY,
    approval_id INTEGER NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    project_version_id TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    agent_provider_id TEXT NOT NULL,
    provider_capabilities TEXT NOT NULL DEFAULT '{}',
    execution_contract TEXT NOT NULL DEFAULT '{}',
    contract_version TEXT NOT NULL,
    structured_request TEXT NOT NULL,
    instruction TEXT NOT NULL,
    detected_metadata TEXT NOT NULL DEFAULT '{}',
    runner_server TEXT NOT NULL,
    validation_target TEXT,
    coding_run_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engineering_tasks_project_id
    ON engineering_tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_engineering_tasks_project_version_id
    ON engineering_tasks(project_version_id);
CREATE INDEX IF NOT EXISTS idx_engineering_tasks_created_at
    ON engineering_tasks(created_at);

-- Slice 3 visibility journal.  These rows contain only structured/redacted
-- metadata; raw command output, diff text and credential-bearing artifacts
-- remain outside SQLite and are redacted at the API boundary.
CREATE TABLE IF NOT EXISTS engineering_task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engineering_task_id TEXT NOT NULL,
    attempt_number INTEGER,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT,
    state TEXT,
    summary TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    source_kind TEXT NOT NULL,
    source_id TEXT,
    actor_id TEXT,
    occurred_at TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(engineering_task_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_engineering_task_events_task
    ON engineering_task_events(engineering_task_id, id);
CREATE INDEX IF NOT EXISTS idx_engineering_task_events_attempt
    ON engineering_task_events(engineering_task_id, attempt_number, id);

CREATE TABLE IF NOT EXISTS engineering_task_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engineering_task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    command_key TEXT NOT NULL,
    job_id INTEGER,
    coding_run_id INTEGER,
    command_role TEXT NOT NULL,
    display_command TEXT NOT NULL,
    command_digest TEXT,
    execution_location TEXT NOT NULL,
    target_ref TEXT,
    working_directory_label TEXT NOT NULL,
    policy_family TEXT NOT NULL,
    policy_disposition TEXT NOT NULL,
    approval_id INTEGER,
    status_source TEXT NOT NULL,
    recorded_status TEXT,
    recorded_started_at TEXT,
    recorded_finished_at TEXT,
    recorded_exit_code INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(engineering_task_id, attempt_number, command_key)
);
CREATE INDEX IF NOT EXISTS idx_engineering_task_commands_task
    ON engineering_task_commands(engineering_task_id, attempt_number, sequence, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_engineering_task_commands_job
    ON engineering_task_commands(job_id) WHERE job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS engineering_task_artifacts (
    id TEXT PRIMARY KEY,
    artifact_key TEXT NOT NULL,
    engineering_task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    coding_run_id INTEGER,
    source_job_id INTEGER,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    storage_kind TEXT NOT NULL,
    storage_key TEXT,
    content_type TEXT,
    source_sha256 TEXT,
    source_size_bytes INTEGER,
    verification_status TEXT NOT NULL,
    redaction_status TEXT NOT NULL,
    availability TEXT NOT NULL,
    created_at TEXT NOT NULL,
    collected_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(engineering_task_id, attempt_number, artifact_key)
);
CREATE INDEX IF NOT EXISTS idx_engineering_task_artifacts_task
    ON engineering_task_artifacts(engineering_task_id, attempt_number, kind, id);

-- Slice 5a: a worker validation is an ordinary enqueue approval/Job linked
-- back to an immutable Engineering Task result.  The opaque request_snapshot
-- is never updated; lifecycle columns are projections of the approval/Job.
CREATE TABLE IF NOT EXISTS engineering_validation_requests (
    id TEXT PRIMARY KEY,
    engineering_task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    coding_run_id INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    project_version_id TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    result_commit TEXT NOT NULL,
    target_server TEXT NOT NULL,
    request_snapshot TEXT NOT NULL,
    request_snapshot_sha256 TEXT NOT NULL,
    bundle_push_command_sha256 TEXT,
    downstream_command_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    bundle_push_job_id INTEGER,
    downstream_job_id INTEGER,
    result_status TEXT,
    result_exit_code INTEGER,
    result_finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engineering_validation_requests_task
    ON engineering_validation_requests(engineering_task_id, attempt_number, created_at);
CREATE INDEX IF NOT EXISTS idx_engineering_validation_requests_run
    ON engineering_validation_requests(coding_run_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_engineering_validation_requests_job
    ON engineering_validation_requests(downstream_job_id)
    WHERE downstream_job_id IS NOT NULL;

-- 階段 13（PLAN.md N.6）：Codex Worker v2——coding_runs 記錄每一次
-- `codex exec` 任務的結果（不再只塞 jobs.log_tail）。回填時機見
-- app/approvals.py 的 coding_task 分支與 on_job_finished hook（N.6）：
-- approve() 建 run（status=queued）→ scheduler dispatch 記 started_at →
-- job 完成時解析回收的 result.json 回填 status/result_commit/test_*/
-- finished_at/error_message。
CREATE TABLE IF NOT EXISTS coding_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id INTEGER NOT NULL,
    job_id INTEGER,
    project TEXT NOT NULL,
    runner_server TEXT NOT NULL,
    instruction TEXT NOT NULL,
    base_branch TEXT,
    base_commit TEXT,
    result_branch TEXT,
    result_commit TEXT,
    worktree_path TEXT,
    bundle_path TEXT,
    validation_target TEXT,
    codex_version TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    test_command TEXT,
    test_exit_code INTEGER,
    engineering_task_id TEXT,
    project_version_id TEXT,
    base_binding TEXT NOT NULL DEFAULT 'legacy_unpinned',
    attempt_number INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_coding_runs_status ON coding_runs(status);

-- 專案詳情頁（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
-- 方法／進度管理」計畫第 1 節）：手動實驗紀錄——時間軸採「查詢時動態
-- 合併」設計（見 app/records.py 的 build_timeline()），這張表只存人／agent
-- 手動補的筆記，jobs／coding_runs 的自動紀錄不寫進這張表、合併時才拼在
-- 一起。`job_id`／`coding_run_id` 是弱關聯（全案慣例，無 FK），純粹讓一筆
-- 紀錄能標註「針對哪一次執行/哪一次改碼寫的」，不做參照完整性檢查、也不
-- 因為被引用的 job/coding_run 被刪就跟著清掉。刪除專案時這張表的紀錄
-- 不會被清掉（見 `Database.delete_project()` docstring）。
CREATE TABLE IF NOT EXISTS experiment_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',      -- note/observation/conclusion/decision
    title TEXT,
    content TEXT NOT NULL,                  -- Markdown
    author TEXT NOT NULL DEFAULT 'user',    -- 'user' | 'agent' | 'agent:chatgpt'
    job_id INTEGER,                         -- 弱關聯，無 FK（全案慣例）
    coding_run_id INTEGER,
    extra TEXT NOT NULL DEFAULT '{}',       -- JSON 擴充位（未來存指標數值）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiment_records_project ON experiment_records(project, id);

-- Goal 2 Slice 1（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：monitor 每次探測
-- 落地成可查詢歷史，供之後的閒置摘要／自動放置提案引用。純粹是額外的觀測
-- 快照表，不參與排程決策（is_idle()/pick_job() 完全不讀這張表）；寫入是
-- best-effort，失敗不影響 in-memory server_states（見 AppState.monitor_loop()
-- docstring）。新表用 CREATE TABLE IF NOT EXISTS，舊 DB 開啟時自動補上，
-- 不需要 ALTER TABLE 欄位遷移。
CREATE TABLE IF NOT EXISTS server_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    online INTEGER NOT NULL,
    probe_ok INTEGER NOT NULL,
    gpu_count INTEGER,
    gpu_util_max REAL,
    gpu_mem_used_mb REAL,
    gpu_mem_total_mb REAL,
    load1 REAL,
    mem_total_bytes INTEGER,
    mem_available_bytes INTEGER,
    disk_avail_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_server_observations_server_time
    ON server_observations(server_name, observed_at);

-- Goal 3 Phase B（DG-B，docs/DECISIONS.md 2026-07-19）：server_bootstrap
-- approval 執行後的結果報告。目標機器此時通常**還不在 servers.yaml**，因此
-- 用 (host, username, port) 識別而不是 server_name。`server_add` 請求會查
-- 這張表：同目標最新一筆報告失敗 → 拒絕（沒有報告則放行，維持既有機器的
-- 相容性）。additive、CREATE TABLE IF NOT EXISTS，不動既有表。
CREATE TABLE IF NOT EXISTS server_bootstrap_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    username TEXT NOT NULL,
    port INTEGER NOT NULL,
    components TEXT NOT NULL,
    script_version TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    passed INTEGER NOT NULL,
    report TEXT NOT NULL,
    approval_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_server_bootstrap_reports_target
    ON server_bootstrap_reports(host, username, port, created_at);

-- Goal 3 Phase C / C2（INV-NODE-1，DG-C 核准 2026-07-19）：Node Agent 註冊表。
-- 每個 node 有**自己**一組可個別撤銷的 credential（不是人類 actor、也不是
-- service token）；只存 SHA-256 digest，raw token 只在核發當下回傳一次。
-- `server_name` 指向 servers.yaml 既有的一台機器——node 是那台機器的另一條
-- 執行通道，不是新機器。撤銷＝寫 revoked_at，不刪列（保留稽核軌跡）。
-- additive、CREATE TABLE IF NOT EXISTS，不動既有表；沒有任何 node 時整組
-- 行為與 C2 之前逐位元相同。
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    server_name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_version TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    approval_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_nodes_server ON nodes(server_name, status);

-- Goal 3 C2（INV-NODE-2/3/5）：agent 執行嘗試（attempt）的持久紀錄。
-- 一個 attempt 由 control plane 建立並 lease 給**恰好一個** node；agent 必須
-- 先 acknowledge（寫 acked_at）才能產生任何副作用。lease 未過期且未達終態
-- 之前，這個 job 不得再被派給任何通道（含 SSH）——這是 INV-NODE-2 的持久
-- 依據，重啟後靠這張表收斂（INV-NODE-5），不靠記憶體狀態。
-- `command_sha256` 綁定核准當下的指令位元組（INV-NODE-3）。
CREATE TABLE IF NOT EXISTS node_attempts (
    id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    command_sha256 TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked_at TEXT,
    last_heartbeat_at TEXT,
    terminal_at TEXT,
    exit_code INTEGER,
    log_tail TEXT,
    --: Goal 3 C3（roadmap Phase 3 的 stop-request 協議）：已核准的停止請求
    --: 落地時間。control plane **不能**直接殺工作機上的行程（沒有入站通道），
    --: 只能記下請求；agent 下次輪詢/心跳時取回並自行停止，再回報終態。
    --: 非 NULL 不代表已經停了——狀態仍以 agent 回報的終態為準（INV-NODE-4）。
    stop_requested_at TEXT,
    --: agent 確認收到停止請求的時間（用來區分「請求還沒送達」與「已送達
    --: 但還沒停完」，避免操作者誤以為系統沒反應）。
    stop_acked_at TEXT,
    execution_attempt_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_attempts_job ON node_attempts(job_id, status);
CREATE INDEX IF NOT EXISTS idx_node_attempts_node ON node_attempts(node_id, status);

-- Goal 3 C3（roadmap Phase 3 的 artifact-metadata 協議）：agent 回報「工作機
-- 上有哪些產出檔案」的**中繼資料**——路徑、大小、SHA-256。
--
-- **刻意只存中繼資料，不傳檔案內容**：這樣就不需要決定上傳儲存位置、大小
-- 上限、清理策略等政策（那些屬於 C4／另外的設計決策）。`availability` 的
-- 語意因此是「agent 說工作機上有這個檔案」，**不是**「Server A 已經有這份
-- 檔案」——沒有檔案被搬動過，不得把這張表當成結果已回收的證據。
--
-- 路徑一律是**相對於該 attempt 結果目錄**的相對路徑，寫入前經過
-- `validate_artifact_path()` 嚴格檢查（無 `..`、無絕對路徑、無 NUL）。
CREATE TABLE IF NOT EXISTS node_attempt_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    UNIQUE(attempt_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_node_attempt_artifacts_attempt
    ON node_attempt_artifacts(attempt_id);
"""


def make_candidate_id(server: str, path: str) -> str:
    """`project_candidates.id`：`server+path` 的穩定 hash（不是隨機
    UUID）——重複掃描同一台機器同一個路徑會 upsert 成同一列，不會每次
    掃描都新增一列垃圾資料。"""
    return hashlib.sha1(f"{server}:{path}".encode("utf-8")).hexdigest()[:16]


def make_instance_id(project_name: str, server: str, path: str) -> str:
    """`project_instances.id`：`project_name+server+path` 的穩定 hash，理由
    同 `make_candidate_id()`。"""
    return hashlib.sha1(f"{project_name}:{server}:{path}".encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: int
    type: str
    project: Optional[str]
    command: str
    require_tag: Optional[str]
    pin_server: Optional[str]
    depends_on: list[int] = field(default_factory=list)
    gpus_needed: Optional[int] = None
    status: str = "queued"
    server: Optional[str] = None
    priority: str = "normal"
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None
    #: 階段 3：sync 任務專用欄位。sync 任務本身 `server` 一律是 `_local`
    #: （在 Server A 本地跑 rsync），`target_server` 記錄實際要推去的工作機；
    #: `dataset_name`/`dataset_version` 供任務完成後驗證 manifest、登記
    #: dataset_cache 用。train/adhoc/setup 任務這三欄一律是 None。
    target_server: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    #: 階段 4：卡死偵測（PLAN.md E）。`log_size`/`log_size_changed_at` 是
    #: `app.stall.update_stall_state()` 的持久化狀態；`stalled_suspect` 是
    #: 只給人看的旗標（不影響 `status`）；`stall_notified` 用來讓提醒信只寄
    #: 一次（去重，log 又增長也不清這個旗標，避免同一次卡住反覆寄信）。
    log_size: Optional[int] = None
    log_size_changed_at: Optional[str] = None
    stalled_suspect: int = 0
    stall_notified: int = 0
    #: 階段 13（PLAN.md N.6）：這個 job 若是「用某次 Codex coding run 的
    #: result（changes.bundle）當起點」的後續 train／驗證 job，記對應
    #: coding_runs.id；一般任務一律 None。
    source_coding_run_id: Optional[int] = None
    #: Engineering Task backend 的 idempotent internal ownership。舊 jobs
    #: 全部保持 NULL；只有 immutable task 的 staging/coding jobs 會填。
    engineering_task_id: Optional[str] = None
    engineering_task_role: Optional[str] = None
    engineering_attempt_number: Optional[int] = None
    engineering_validation_request_id: Optional[str] = None
    #: Goal 2 Slice 4：這個 job 若是由某個 `auto_placement` 核准建立，記那筆
    #: approval 的 id;一般任務（含手動 pin_server enqueue)一律 None。
    auto_placement_approval_id: Optional[int] = None
    #: DG-EXEC-ATTEMPT-v1：五欄全為 None 表示 honest legacy Job；全都有值
    #: 才是可由 generic attempt foundation 驗證的 pinned execution contract。
    execution_approval_id: Optional[int] = None
    approved_payload_sha256: Optional[str] = None
    execution_contract_version: Optional[str] = None
    execution_contract_role: Optional[str] = None
    approved_command_sha256: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Job":
        return Job(
            id=row["id"],
            type=row["type"],
            project=row["project"],
            command=row["command"],
            require_tag=row["require_tag"],
            pin_server=row["pin_server"],
            depends_on=json.loads(row["depends_on"] or "[]"),
            gpus_needed=row["gpus_needed"],
            status=row["status"],
            server=row["server"],
            priority=row["priority"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            exit_code=row["exit_code"],
            log_tail=row["log_tail"],
            target_server=row["target_server"],
            dataset_name=row["dataset_name"],
            dataset_version=row["dataset_version"],
            log_size=row["log_size"],
            log_size_changed_at=row["log_size_changed_at"],
            stalled_suspect=row["stalled_suspect"] if row["stalled_suspect"] is not None else 0,
            stall_notified=row["stall_notified"] if row["stall_notified"] is not None else 0,
            source_coding_run_id=row["source_coding_run_id"],
            engineering_task_id=row["engineering_task_id"],
            engineering_task_role=row["engineering_task_role"],
            engineering_attempt_number=row["engineering_attempt_number"],
            engineering_validation_request_id=row[
                "engineering_validation_request_id"
            ],
            auto_placement_approval_id=row["auto_placement_approval_id"],
            execution_approval_id=row["execution_approval_id"],
            approved_payload_sha256=row["approved_payload_sha256"],
            execution_contract_version=row["execution_contract_version"],
            execution_contract_role=row["execution_contract_role"],
            approved_command_sha256=row["approved_command_sha256"],
        )


@dataclass
class Approval:
    id: int
    kind: str
    payload: dict
    status: str = "pending"
    created_at: str = ""
    decided_at: Optional[str] = None
    note: Optional[str] = None
    requester_actor_id: Optional[str] = None
    decision_actor_id: Optional[str] = None
    decision_mechanism: Optional[str] = None
    payload_sha256: Optional[str] = None
    payload_contract_version: Optional[str] = None
    payload_immutable_at: Optional[str] = None
    materialization_started_at: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Approval":
        return Approval(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"] or "{}"),
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            note=row["note"],
            requester_actor_id=row["requester_actor_id"],
            decision_actor_id=row["decision_actor_id"],
            decision_mechanism=row["decision_mechanism"],
            payload_sha256=row["payload_sha256"],
            payload_contract_version=row["payload_contract_version"],
            payload_immutable_at=row["payload_immutable_at"],
            materialization_started_at=row["materialization_started_at"],
        )


@dataclass
class Project:
    name: str
    repo_or_path: str
    #: PLAN.md 2026-07-11 版 §14 切片 1：正式跨系統身分（UUID）。理論上
    #: migration backfill 後不會是 None,型別仍留 Optional 防禦「backfill
    #: 前就被讀出」的邊界。
    id: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    default_command: Optional[str] = None
    require_tag: Optional[str] = None
    setup_cmd: Optional[str] = None
    created_at: str = ""
    #: 階段 8（第一批）：inventory 匯入的專案可以帶一段摘要（多半來自 README
    #: 前段）；`dataset_mode` 區分「沒有資料集要求」／「用 datasets/
    #: dataset_cache 表管理的資料集」／「資料就內嵌在專案目錄底下，不自動
    #: 同步」（見 app/approvals.py 的 import_project 分支說明）。
    summary: Optional[str] = None
    dataset_mode: str = "none"
    #: 專案詳情頁計畫第 1 節：目標／優化方法／目前進度，自由文字 Markdown，
    #: `None` 代表尚未填寫（前端顯示「尚未填寫，點擊編輯」，不是錯誤）。
    goal: Optional[str] = None
    optimization_notes: Optional[str] = None
    progress: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Project":
        return Project(
            name=row["name"],
            repo_or_path=row["repo_or_path"],
            id=row["id"],
            dataset_name=row["dataset_name"],
            dataset_version=row["dataset_version"],
            default_command=row["default_command"],
            require_tag=row["require_tag"],
            setup_cmd=row["setup_cmd"],
            created_at=row["created_at"],
            summary=row["summary"],
            dataset_mode=row["dataset_mode"] if row["dataset_mode"] is not None else "none",
            goal=row["goal"],
            optimization_notes=row["optimization_notes"],
            progress=row["progress"],
        )


@dataclass
class ProjectCandidate:
    """`project_candidates` 一列：inventory 掃描找到的候選專案（PLAN.md
    I.1）。`id` 是 `make_candidate_id(server, path)` 的穩定 hash。"""

    id: str
    server: str
    path: str
    name_guess: Optional[str] = None
    kind: str = "project"
    git_remote: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    markers: list[str] = field(default_factory=list)
    readme_excerpt: Optional[str] = None
    command_guess: Optional[str] = None
    embedded_data_paths: list[str] = field(default_factory=list)
    embedded_data_summary: Optional[str] = None
    estimated_data_bytes: Optional[int] = None
    excluded_paths: list[str] = field(default_factory=list)
    confidence: Optional[float] = None
    status: str = "pending"
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ProjectCandidate":
        return ProjectCandidate(
            id=row["id"],
            server=row["server"],
            path=row["path"],
            name_guess=row["name_guess"],
            kind=row["kind"],
            git_remote=row["git_remote"],
            git_branch=row["git_branch"],
            git_commit=row["git_commit"],
            markers=json.loads(row["markers"] or "[]"),
            readme_excerpt=row["readme_excerpt"],
            command_guess=row["command_guess"],
            embedded_data_paths=json.loads(row["embedded_data_paths"] or "[]"),
            embedded_data_summary=row["embedded_data_summary"],
            estimated_data_bytes=row["estimated_data_bytes"],
            excluded_paths=json.loads(row["excluded_paths"] or "[]"),
            confidence=row["confidence"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class ProjectInstance:
    """`project_instances` 一列：某個已匯入專案在某台機器上實際確認過存在
    的一份副本。`id` 是 `make_instance_id(project_name, server, path)` 的
    穩定 hash。"""

    id: str
    project_name: str
    server: str
    path: str
    git_remote: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    dirty: bool = False
    embedded_data_paths: list[str] = field(default_factory=list)
    last_seen: str = ""
    #: 切片 1：所屬 Project 的 UUID（projects.id）雙寫;project 已被刪除的
    #: 孤兒列可能是 None。
    project_id: Optional[str] = None
    #: 切片 1 只做 schema readiness:一律 'unknown',真正的判定與轉移由
    #: 切片 2（instance reconciliation）接手。值域見 VALID_INSTANCE_STATES。
    state: str = "unknown"

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ProjectInstance":
        return ProjectInstance(
            id=row["id"],
            project_name=row["project_name"],
            server=row["server"],
            path=row["path"],
            git_remote=row["git_remote"],
            git_branch=row["git_branch"],
            git_commit=row["git_commit"],
            dirty=bool(row["dirty"]),
            embedded_data_paths=json.loads(row["embedded_data_paths"] or "[]"),
            last_seen=row["last_seen"],
            project_id=row["project_id"],
            state=row["state"] or "unknown",
        )


@dataclass
class ProjectVersion:
    """`project_versions` 一列:PLAN.md 2026-07-11 版 §14 切片 4（canonical
    version service）。某專案在某個時間點的不可變 hub commit——同一個
    `(project_name, git_commit)` 只會有一筆(見 `get_or_create_project_
    version()`),身分一經建立永不改變(同 INV-DATA-1 草案「DatasetSnapshot
    不可變」的同一精神)。"""

    id: str
    project_name: str
    git_commit: str
    project_id: Optional[str] = None
    git_ref: Optional[str] = None
    source_instance_id: Optional[str] = None
    created_at: str = ""
    metadata: Optional[dict] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ProjectVersion":
        return ProjectVersion(
            id=row["id"],
            project_name=row["project_name"],
            git_commit=row["git_commit"],
            project_id=row["project_id"],
            git_ref=row["git_ref"],
            source_instance_id=row["source_instance_id"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        )


@dataclass
class RunProfile:
    """`run_profiles` 一列（D5 Run Profile v1，docs/DECISIONS.md）。某個
    (project_id, name) 底下不可變的一個 revision——身分一經建立永不改變，
    update/archive 一律新增一列（同 `ProjectVersion` 不可變精神）。「目前
    狀態」是該 (project_id, name) revision 最大的那一列：`status="approved"`
    表示可選用，`"archived"` 表示已下架但歷史仍可查。"""

    id: str
    project_id: str
    project_name: str
    name: str
    revision: int
    status: str
    command: Optional[str] = None
    setup_cmd: Optional[str] = None
    require_tag: Optional[str] = None
    supersedes_id: Optional[str] = None
    approval_id: Optional[int] = None
    created_by_actor_id: Optional[str] = None
    created_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "RunProfile":
        return RunProfile(
            id=row["id"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            name=row["name"],
            revision=row["revision"],
            status=row["status"],
            command=row["command"],
            setup_cmd=row["setup_cmd"],
            require_tag=row["require_tag"],
            supersedes_id=row["supersedes_id"],
            approval_id=row["approval_id"],
            created_by_actor_id=row["created_by_actor_id"],
            created_at=row["created_at"],
        )


@dataclass
class DispatchPolicy:
    """`dispatch_policies` 一列（Goal 2 Slice 3）。同 `RunProfile` 的不可變
    revision 精神——身分一經建立永不改變，update/archive 一律新增一列。
    「目前狀態」是該 (project_id, name) revision 最大的那一列。**本切片這個
    物件沒有任何運行時效果**，scheduler 完全不讀它。

    跟 `RunProfile` 的差異：這張表刻意不存 `supersedes_id`——哪一列取代
    哪一列完全由 (project_id, name, revision) 的排序決定，呼叫端要找「上一個
    revision」用 `list_dispatch_policy_revisions()` 依 revision 排序取得。"""

    id: str
    project_id: str
    project_name: str
    name: str
    revision: int
    status: str
    allowed_servers: list[str] = field(default_factory=list)
    require_tag: Optional[str] = None
    run_profile_id: Optional[str] = None
    dataset_required: bool = False
    max_concurrent_placements: int = 1
    valid_until: Optional[str] = None
    approval_id: Optional[int] = None
    created_by_actor_id: Optional[str] = None
    created_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "DispatchPolicy":
        return DispatchPolicy(
            id=row["id"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            name=row["name"],
            revision=row["revision"],
            status=row["status"],
            allowed_servers=json.loads(row["allowed_servers"]),
            require_tag=row["require_tag"],
            run_profile_id=row["run_profile_id"],
            dataset_required=bool(row["dataset_required"]),
            max_concurrent_placements=row["max_concurrent_placements"],
            valid_until=row["valid_until"],
            approval_id=row["approval_id"],
            created_by_actor_id=row["created_by_actor_id"],
            created_at=row["created_at"],
        )


@dataclass
class ServerObservation:
    """`server_observations` 一列（Goal 2 Slice 1，
    docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：monitor 每次探測的快照，純粹是
    歷史證據，不參與排程決策。`gpu_mem_used_mb`/`gpu_mem_total_mb` 是該次探測
    所有 GPU 的加總（沒讀到任何 GPU 時是 None，不是 0，避免跟「有 GPU 但用量
    是 0」混淆）。"""

    id: int
    server_name: str
    observed_at: str
    online: bool
    probe_ok: bool
    gpu_count: Optional[int] = None
    gpu_util_max: Optional[float] = None
    gpu_mem_used_mb: Optional[float] = None
    gpu_mem_total_mb: Optional[float] = None
    load1: Optional[float] = None
    mem_total_bytes: Optional[int] = None
    mem_available_bytes: Optional[int] = None
    disk_avail_bytes: Optional[int] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ServerObservation":
        return ServerObservation(
            id=row["id"],
            server_name=row["server_name"],
            observed_at=row["observed_at"],
            online=bool(row["online"]),
            probe_ok=bool(row["probe_ok"]),
            gpu_count=row["gpu_count"],
            gpu_util_max=row["gpu_util_max"],
            gpu_mem_used_mb=row["gpu_mem_used_mb"],
            gpu_mem_total_mb=row["gpu_mem_total_mb"],
            load1=row["load1"],
            mem_total_bytes=row["mem_total_bytes"],
            mem_available_bytes=row["mem_available_bytes"],
            disk_avail_bytes=row["disk_avail_bytes"],
        )


@dataclass
class ServerBootstrapReport:
    """`server_bootstrap_reports` 一列（Goal 3 Phase B）：一次 bootstrap
    執行的完整結果。`components`/`report` 存 JSON 文字（讀取時解析），
    `passed` 是 fail-closed 的總結論——解析不到報告或任何缺項都是 False。"""

    id: int
    host: str
    username: str
    port: int
    components: list[str]
    script_version: str
    script_sha256: str
    passed: bool
    report: dict
    approval_id: Optional[int]
    created_at: str

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ServerBootstrapReport":
        try:
            components = json.loads(row["components"])
        except (json.JSONDecodeError, TypeError):
            components = []
        try:
            report = json.loads(row["report"])
        except (json.JSONDecodeError, TypeError):
            report = {}
        return ServerBootstrapReport(
            id=row["id"],
            host=row["host"],
            username=row["username"],
            port=row["port"],
            components=components if isinstance(components, list) else [],
            script_version=row["script_version"],
            script_sha256=row["script_sha256"],
            passed=bool(row["passed"]),
            report=report if isinstance(report, dict) else {},
            approval_id=row["approval_id"],
            created_at=row["created_at"],
        )


@dataclass
class Node:
    """`nodes` 一列（Goal 3 C2，INV-NODE-1）：一台工作機上的 Node Agent 身分。

    `secret_hash` 是憑證的 SHA-256；raw token 只在核發當下回傳一次，永不
    落庫。`status` 為 `enrolled`｜`revoked`——撤銷只寫 `revoked_at` 並改
    status，不刪列（保留稽核軌跡，也讓「這個 node 曾經存在」可查）。
    """

    id: str
    server_name: str
    secret_hash: str
    status: str
    agent_version: Optional[str]
    last_heartbeat_at: Optional[str]
    created_at: str
    revoked_at: Optional[str] = None
    approval_id: Optional[int] = None

    @property
    def is_active(self) -> bool:
        return self.status == "enrolled" and self.revoked_at is None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Node":
        return Node(
            id=row["id"],
            server_name=row["server_name"],
            secret_hash=row["secret_hash"],
            status=row["status"],
            agent_version=row["agent_version"],
            last_heartbeat_at=row["last_heartbeat_at"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
            approval_id=row["approval_id"],
        )


@dataclass
class NodeAttemptRow:
    """`node_attempts` 一列（Goal 3 C2，INV-NODE-2/3/5）。

    這是 attempt 的**持久真相**（INV-STATE-1）——lease 歸屬、是否 ack 過、
    終態，重啟後全靠這張表收斂，不靠任何 in-memory 狀態。
    """

    id: str
    job_id: int
    node_id: str
    status: str
    command_sha256: str
    lease_expires_at: str
    created_at: str
    acked_at: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    terminal_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None
    #: Goal 3 C3 stop-request：非 NULL＝已核准的停止請求已記下，等 agent
    #: 取回。**不代表已經停了**——終態仍以 agent 回報為準（INV-NODE-4）。
    stop_requested_at: Optional[str] = None
    stop_acked_at: Optional[str] = None
    #: DG-EXEC-ATTEMPT-v1 optional generic ownership link.  Legacy protocol
    #: attempts honestly remain NULL.
    execution_attempt_id: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "NodeAttemptRow":
        return NodeAttemptRow(
            id=row["id"],
            job_id=row["job_id"],
            node_id=row["node_id"],
            status=row["status"],
            command_sha256=row["command_sha256"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            acked_at=row["acked_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            terminal_at=row["terminal_at"],
            exit_code=row["exit_code"],
            log_tail=row["log_tail"],
            stop_requested_at=row["stop_requested_at"],
            stop_acked_at=row["stop_acked_at"],
            execution_attempt_id=row["execution_attempt_id"],
        )


@dataclass
class EngineeringTask:
    """Immutable AI Engineering Task request plus minimal lifecycle linkage."""

    id: str
    approval_id: int
    project_id: str
    project_name: str
    project_version_id: str
    base_commit: str
    agent_provider_id: str
    provider_capabilities: dict
    execution_contract: dict
    contract_version: str
    structured_request: dict
    instruction: str
    detected_metadata: dict
    runner_server: str
    validation_target: Optional[str] = None
    coding_run_id: Optional[int] = None
    status: str = "pending_approval"
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "EngineeringTask":
        return EngineeringTask(
            id=row["id"],
            approval_id=row["approval_id"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            project_version_id=row["project_version_id"],
            base_commit=row["base_commit"],
            agent_provider_id=row["agent_provider_id"],
            provider_capabilities=json.loads(row["provider_capabilities"] or "{}"),
            execution_contract=json.loads(row["execution_contract"] or "{}"),
            contract_version=row["contract_version"],
            structured_request=json.loads(row["structured_request"] or "{}"),
            instruction=row["instruction"],
            detected_metadata=json.loads(row["detected_metadata"] or "{}"),
            runner_server=row["runner_server"],
            validation_target=row["validation_target"],
            coding_run_id=row["coding_run_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class EngineeringTaskEvent:
    id: int
    event_key: str
    engineering_task_id: str
    event_type: str
    summary: str
    attempt_number: Optional[int] = None
    phase: Optional[str] = None
    state: Optional[str] = None
    details: dict = field(default_factory=dict)
    source_kind: str = "system"
    source_id: Optional[str] = None
    actor_id: Optional[str] = None
    occurred_at: Optional[str] = None
    recorded_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "EngineeringTaskEvent":
        return EngineeringTaskEvent(
            id=row["id"],
            event_key=row["event_key"],
            engineering_task_id=row["engineering_task_id"],
            event_type=row["event_type"],
            attempt_number=row["attempt_number"],
            phase=row["phase"],
            state=row["state"],
            summary=row["summary"],
            details=json.loads(row["details"] or "{}"),
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            actor_id=row["actor_id"],
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
        )


@dataclass
class EngineeringTaskCommand:
    id: int
    engineering_task_id: str
    attempt_number: int
    sequence: int
    command_key: str
    command_role: str
    display_command: str
    execution_location: str
    working_directory_label: str
    policy_family: str
    policy_disposition: str
    status_source: str
    job_id: Optional[int] = None
    coding_run_id: Optional[int] = None
    command_digest: Optional[str] = None
    target_ref: Optional[str] = None
    approval_id: Optional[int] = None
    recorded_status: Optional[str] = None
    recorded_started_at: Optional[str] = None
    recorded_finished_at: Optional[str] = None
    recorded_exit_code: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "EngineeringTaskCommand":
        return EngineeringTaskCommand(
            id=row["id"],
            engineering_task_id=row["engineering_task_id"],
            attempt_number=row["attempt_number"],
            sequence=row["sequence"],
            command_key=row["command_key"],
            job_id=row["job_id"],
            coding_run_id=row["coding_run_id"],
            command_role=row["command_role"],
            display_command=row["display_command"],
            command_digest=row["command_digest"],
            execution_location=row["execution_location"],
            target_ref=row["target_ref"],
            working_directory_label=row["working_directory_label"],
            policy_family=row["policy_family"],
            policy_disposition=row["policy_disposition"],
            approval_id=row["approval_id"],
            status_source=row["status_source"],
            recorded_status=row["recorded_status"],
            recorded_started_at=row["recorded_started_at"],
            recorded_finished_at=row["recorded_finished_at"],
            recorded_exit_code=row["recorded_exit_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class EngineeringTaskArtifact:
    id: str
    engineering_task_id: str
    attempt_number: int
    artifact_key: str
    kind: str
    label: str
    storage_kind: str
    availability: str
    verification_status: str
    redaction_status: str
    coding_run_id: Optional[int] = None
    source_job_id: Optional[int] = None
    storage_key: Optional[str] = None
    content_type: Optional[str] = None
    source_sha256: Optional[str] = None
    source_size_bytes: Optional[int] = None
    created_at: str = ""
    collected_at: Optional[str] = None
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "EngineeringTaskArtifact":
        return EngineeringTaskArtifact(
            id=row["id"],
            engineering_task_id=row["engineering_task_id"],
            attempt_number=row["attempt_number"],
            coding_run_id=row["coding_run_id"],
            source_job_id=row["source_job_id"],
            artifact_key=row["artifact_key"],
            kind=row["kind"],
            label=row["label"],
            storage_kind=row["storage_kind"],
            storage_key=row["storage_key"],
            content_type=row["content_type"],
            source_sha256=row["source_sha256"],
            source_size_bytes=row["source_size_bytes"],
            availability=row["availability"],
            verification_status=row["verification_status"],
            redaction_status=row["redaction_status"],
            created_at=row["created_at"],
            collected_at=row["collected_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class EngineeringValidationRequest:
    """Immutable proposal plus ordinary enqueue/Job result linkage."""

    id: str
    engineering_task_id: str
    attempt_number: int
    coding_run_id: int
    approval_id: int
    project_id: str
    project_name: str
    project_version_id: str
    base_commit: str
    result_commit: str
    target_server: str
    request_snapshot: dict
    request_snapshot_sha256: str
    bundle_push_command_sha256: Optional[str] = None
    downstream_command_sha256: Optional[str] = None
    status: str = "pending_approval"
    bundle_push_job_id: Optional[int] = None
    downstream_job_id: Optional[int] = None
    result_status: Optional[str] = None
    result_exit_code: Optional[int] = None
    result_finished_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "EngineeringValidationRequest":
        return EngineeringValidationRequest(
            id=row["id"],
            engineering_task_id=row["engineering_task_id"],
            attempt_number=row["attempt_number"],
            coding_run_id=row["coding_run_id"],
            approval_id=row["approval_id"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            project_version_id=row["project_version_id"],
            base_commit=row["base_commit"],
            result_commit=row["result_commit"],
            target_server=row["target_server"],
            request_snapshot=json.loads(row["request_snapshot"] or "{}"),
            request_snapshot_sha256=row["request_snapshot_sha256"],
            bundle_push_command_sha256=row["bundle_push_command_sha256"],
            downstream_command_sha256=row["downstream_command_sha256"],
            status=row["status"],
            bundle_push_job_id=row["bundle_push_job_id"],
            downstream_job_id=row["downstream_job_id"],
            result_status=row["result_status"],
            result_exit_code=row["result_exit_code"],
            result_finished_at=row["result_finished_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class CodingRun:
    """`coding_runs` 一列：一次 Codex Worker（`codex exec`）任務的完整記錄
    （PLAN.md N.6）。`approval_id`／`project`／`runner_server`／`instruction`
    是建立時必填；其餘欄位隨任務進行陸續回填（見
    `app/approvals.py` 的 coding_task 分支與 on_job_finished hook）。"""

    id: int
    approval_id: int
    project: str
    runner_server: str
    instruction: str
    job_id: Optional[int] = None
    base_branch: Optional[str] = None
    base_commit: Optional[str] = None
    result_branch: Optional[str] = None
    result_commit: Optional[str] = None
    worktree_path: Optional[str] = None
    bundle_path: Optional[str] = None
    validation_target: Optional[str] = None
    codex_version: Optional[str] = None
    status: str = "queued"
    test_command: Optional[str] = None
    test_exit_code: Optional[int] = None
    engineering_task_id: Optional[str] = None
    project_version_id: Optional[str] = None
    base_binding: str = "legacy_unpinned"
    attempt_number: Optional[int] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "CodingRun":
        return CodingRun(
            id=row["id"],
            approval_id=row["approval_id"],
            job_id=row["job_id"],
            project=row["project"],
            runner_server=row["runner_server"],
            instruction=row["instruction"],
            base_branch=row["base_branch"],
            base_commit=row["base_commit"],
            result_branch=row["result_branch"],
            result_commit=row["result_commit"],
            worktree_path=row["worktree_path"],
            bundle_path=row["bundle_path"],
            validation_target=row["validation_target"],
            codex_version=row["codex_version"],
            status=row["status"],
            test_command=row["test_command"],
            test_exit_code=row["test_exit_code"],
            engineering_task_id=row["engineering_task_id"],
            project_version_id=row["project_version_id"],
            base_binding=row["base_binding"] or "legacy_unpinned",
            attempt_number=row["attempt_number"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_message=row["error_message"],
        )


@dataclass
class ExperimentRecord:
    """`experiment_records` 一列：專案詳情頁的手動實驗紀錄（PLAN.md「專案
    詳情頁：可點入、調派、實驗紀錄時間軸、目標／方法／進度管理」計畫第 1
    節）。人與 agent 都能補筆記；`job_id`／`coding_run_id` 是弱關聯（無
    FK，比照全案慣例），純粹讓紀錄能標註「這是針對哪一次執行/哪一次改碼
    寫的觀察」，不做參照完整性檢查。時間軸實際呈現是跟 jobs／coding_runs
    查詢時動態合併，見 `app/records.py` 的 `build_timeline()`。"""

    id: int
    project: str
    content: str
    kind: str = "note"
    title: Optional[str] = None
    author: str = "user"
    job_id: Optional[int] = None
    coding_run_id: Optional[int] = None
    extra: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ExperimentRecord":
        return ExperimentRecord(
            id=row["id"],
            project=row["project"],
            kind=row["kind"],
            title=row["title"],
            content=row["content"],
            author=row["author"],
            job_id=row["job_id"],
            coding_run_id=row["coding_run_id"],
            extra=json.loads(row["extra"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Dataset:
    name: str
    version: str
    size_bytes: int
    source_path: str
    manifest: dict
    created_at: str = ""
    #: 階段 16（PLAN.md Q.3）：資料卡，`None` 表示這個版本是階段 16 之前
    #: 建立的（或建立時沒帶——理論上不會發生，因為 POST /datasets 強制
    #: 要求 description/method，但 `insert_dataset()` 的 `card` 參數仍是
    #: 選填，保留給既有呼叫端/測試用預設值的彈性）。
    card: Optional[dict] = None
    #: 階段 16（PLAN.md Q.1/Q.2）：'packed'（預設，Q.1 打包快照）｜
    #: 'rolling'（Q.2 滾動模式）。**本批（Q.3 資料卡）只存取這個欄位、
    #: 不使用其邏輯**——rolling 模式的實際同步行為由後續批次接手實作。
    sync_mode: str = "packed"

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Dataset":
        card_raw = row["card"]
        return Dataset(
            name=row["name"],
            version=row["version"],
            size_bytes=row["size_bytes"],
            source_path=row["source_path"],
            manifest=json.loads(row["manifest"] or "{}"),
            created_at=row["created_at"],
            card=json.loads(card_raw) if card_raw else None,
            sync_mode=row["sync_mode"] or "packed",
        )


@dataclass
class DatasetCacheEntry:
    server: str
    dataset: str
    version: str
    registered_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "DatasetCacheEntry":
        return DatasetCacheEntry(
            server=row["server"],
            dataset=row["dataset"],
            version=row["version"],
            registered_at=row["registered_at"],
        )


class Database:
    """薄封裝：一個 sqlite3 連線 + 一把鎖。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    #: 階段 4 新增欄位：`CREATE TABLE IF NOT EXISTS` 只對「全新資料庫」有效，
    #: 對「已經存在、舊 schema 的 jobs 表」不會補上新欄位，因此用
    #: `ALTER TABLE ... ADD COLUMN` 遷移既有 DB（PLAN.md E：「既有 DB 用
    #: ALTER TABLE 遷移」）。新建的 DB 這裡會是no-op（SCHEMA 已經包含這些欄位）。
    _JOB_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("log_size", "INTEGER"),
        ("log_size_changed_at", "TEXT"),
        ("stalled_suspect", "INTEGER NOT NULL DEFAULT 0"),
        ("stall_notified", "INTEGER NOT NULL DEFAULT 0"),
        #: 階段 13（PLAN.md N.6）：後續 train／驗證 job 可選填
        #: source_coding_run_id，指向產出它要用的 changes.bundle 的
        #: coding_runs.id。
        ("source_coding_run_id", "INTEGER"),
        ("engineering_task_id", "TEXT"),
        ("engineering_task_role", "TEXT"),
        ("engineering_attempt_number", "INTEGER"),
        ("engineering_validation_request_id", "TEXT"),
        #: Goal 2 Slice 4（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：政策驅動
        #: 的自動放置提案核准後建立的 job 一律填這欄（指向核准它的
        #: `auto_placement` approval），供 `count_active_auto_placement_jobs()`
        #: 統計某個政策目前有幾個非終態放置在跑，藉此套用
        #: `max_concurrent_placements` 上限。舊 job 全部保持 NULL。
        ("auto_placement_approval_id", "INTEGER"),
        #: DG-EXEC-ATTEMPT-v1: nullable means honest legacy ownership.  New
        #: pinned Jobs set all five fields at INSERT; no migration fabricates
        #: historical approval or command evidence.
        ("execution_approval_id", "INTEGER"),
        ("approved_payload_sha256", "TEXT"),
        ("execution_contract_version", "TEXT"),
        ("execution_contract_role", "TEXT"),
        ("approved_command_sha256", "TEXT"),
    )

    #: 階段 8（第一批）：同上一段說明的遷移模式，補 projects 表兩欄
    #: （PLAN.md I.1）。
    _PROJECT_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("summary", "TEXT"),
        ("dataset_mode", "TEXT NOT NULL DEFAULT 'none'"),
        #: 專案詳情頁計畫第 1 節：目標／優化方法／目前進度，三欄都是
        #: nullable 的自由文字 Markdown，既有專案一律從 NULL（「尚未填寫」）
        #: 開始，不強制回填。
        ("goal", "TEXT"),
        ("optimization_notes", "TEXT"),
        ("progress", "TEXT"),
        #: PLAN.md 2026-07-11 版 §14 切片 1：UUID 身分欄。既有列的實際
        #: UUID 值由 _init_schema() 的 backfill 逐列產生（ALTER TABLE 的
        #: DEFAULT 只能是常數,產生不了每列不同的 UUID）。
        ("id", "TEXT"),
    )

    #: 階段 16（PLAN.md Q 節）：同上一段說明的遷移模式，補 datasets 表兩欄
    #: （資料卡＋同步模式）。
    _DATASET_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("card", "TEXT"),
        ("sync_mode", "TEXT NOT NULL DEFAULT 'packed'"),
    )

    #: 切片 1：project_instances 補 project_id（UUID 雙寫）與 state
    #: （'unknown' 起步,切片 2 才有真正的判定,見 VALID_INSTANCE_STATES）。
    _INSTANCE_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("project_id", "TEXT"),
        ("state", "TEXT NOT NULL DEFAULT 'unknown'"),
    )

    #: Goal 1 / Slice 1: actor attribution is nullable so historical approvals
    #: remain honest and older application versions can continue inserting rows.
    _APPROVAL_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("requester_actor_id", "TEXT"),
        ("decision_actor_id", "TEXT"),
        ("decision_mechanism", "TEXT"),
        #: DG-EXEC-ATTEMPT-v1: existing approvals remain NULL and therefore
        #: legacy.  Only a new INSERT may establish an immutable payload pin.
        ("payload_sha256", "TEXT"),
        ("payload_contract_version", "TEXT"),
        ("payload_immutable_at", "TEXT"),
        ("materialization_started_at", "TEXT"),
    )

    #: Goal 3 C3：stop-request 協議欄位。`node_attempts` 本身是 C2 才建的新
    #: 表，這兩欄對全新 DB 由 SCHEMA 直接建出；這裡的遷移是給「已經跑過 C2
    #: 版本、本機已存在舊結構」的 DB 用的（additive，不改既有欄位）。
    _NODE_ATTEMPT_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("stop_requested_at", "TEXT"),
        ("stop_acked_at", "TEXT"),
        #: Optional link to the new generic attempt identity.  Existing
        #: protocol rows remain unlinked and are never reinterpreted.
        ("execution_attempt_id", "TEXT"),
    )

    #: AI Engineering Task backend v1：舊 coding_runs 明示為 legacy_unpinned；
    #: 不從執行後觀察到的 base_commit 猜測 ProjectVersion。
    _CODING_RUN_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("engineering_task_id", "TEXT"),
        ("project_version_id", "TEXT"),
        ("base_binding", "TEXT NOT NULL DEFAULT 'legacy_unpinned'"),
        ("attempt_number", "INTEGER"),
    )

    _ENGINEERING_TASK_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("execution_contract", "TEXT NOT NULL DEFAULT '{}'"),
    )

    _ENGINEERING_VALIDATION_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("bundle_push_command_sha256", "TEXT"),
        ("downstream_command_sha256", "TEXT"),
    )

    # WP-2B (DG-AMBIGUOUS-LAUNCH-v1 §7). Additive only. `ALTER TABLE ADD
    # COLUMN` cannot carry a CHECK, so the matching value domains are enforced
    # by triggers in EXECUTION_ATTEMPT_POST_MIGRATION_SCHEMA for both fresh and
    # migrated databases. Legacy rows stay NULL and are never backfilled.
    _EXECUTION_ATTEMPT_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("remote_claim_state", "TEXT"),
        ("launch_receipt_sha256", "TEXT"),
        ("remote_boot_id", "TEXT"),
        ("launcher_contract_version", "TEXT"),
        ("prelaunch_verdict", "TEXT"),
    )

    _EXECUTION_OPERATION_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("transmission_state", "TEXT"),
    )

    _SERVER_CONFIG_REVISION_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("attempt_backend_preflight", "TEXT"),
    )

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            foreign_keys = self._conn.execute("PRAGMA foreign_keys").fetchone()
            if foreign_keys is None or foreign_keys[0] != 1:
                raise RuntimeError("SQLite foreign-key enforcement is required")
            self._conn.executescript(SCHEMA)
            self._conn.executescript(EXECUTION_ATTEMPT_SCHEMA)
            self._conn.commit()
            cur = self._conn.execute("PRAGMA table_info(jobs)")
            existing_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._JOB_COLUMN_MIGRATIONS:
                if col_name not in existing_cols:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
            cur = self._conn.execute("PRAGMA table_info(projects)")
            existing_project_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._PROJECT_COLUMN_MIGRATIONS:
                if col_name not in existing_project_cols:
                    self._conn.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_type}")
            cur = self._conn.execute("PRAGMA table_info(datasets)")
            existing_dataset_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._DATASET_COLUMN_MIGRATIONS:
                if col_name not in existing_dataset_cols:
                    self._conn.execute(f"ALTER TABLE datasets ADD COLUMN {col_name} {col_type}")
            cur = self._conn.execute("PRAGMA table_info(project_instances)")
            existing_instance_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._INSTANCE_COLUMN_MIGRATIONS:
                if col_name not in existing_instance_cols:
                    self._conn.execute(
                        f"ALTER TABLE project_instances ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(approvals)")
            existing_approval_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._APPROVAL_COLUMN_MIGRATIONS:
                if col_name not in existing_approval_cols:
                    self._conn.execute(
                        f"ALTER TABLE approvals ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(node_attempts)")
            existing_node_attempt_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._NODE_ATTEMPT_COLUMN_MIGRATIONS:
                if col_name not in existing_node_attempt_cols:
                    self._conn.execute(
                        f"ALTER TABLE node_attempts ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(coding_runs)")
            existing_coding_run_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._CODING_RUN_COLUMN_MIGRATIONS:
                if col_name not in existing_coding_run_cols:
                    self._conn.execute(
                        f"ALTER TABLE coding_runs ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(engineering_tasks)")
            existing_engineering_task_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._ENGINEERING_TASK_COLUMN_MIGRATIONS:
                if col_name not in existing_engineering_task_cols:
                    self._conn.execute(
                        f"ALTER TABLE engineering_tasks ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute(
                "PRAGMA table_info(engineering_validation_requests)"
            )
            existing_validation_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._ENGINEERING_VALIDATION_COLUMN_MIGRATIONS:
                if col_name not in existing_validation_cols:
                    self._conn.execute(
                        "ALTER TABLE engineering_validation_requests "
                        f"ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(execution_attempts)")
            existing_attempt_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._EXECUTION_ATTEMPT_COLUMN_MIGRATIONS:
                if col_name not in existing_attempt_cols:
                    self._conn.execute(
                        f"ALTER TABLE execution_attempts ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(execution_operations)")
            existing_operation_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._EXECUTION_OPERATION_COLUMN_MIGRATIONS:
                if col_name not in existing_operation_cols:
                    self._conn.execute(
                        f"ALTER TABLE execution_operations ADD COLUMN {col_name} {col_type}"
                    )
            cur = self._conn.execute("PRAGMA table_info(server_config_revisions)")
            existing_revision_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._SERVER_CONFIG_REVISION_COLUMN_MIGRATIONS:
                if col_name not in existing_revision_cols:
                    self._conn.execute(
                        "ALTER TABLE server_config_revisions "
                        f"ADD COLUMN {col_name} {col_type}"
                    )
            # 切片 1 backfill：舊列補 UUID（逐列產生,只補 NULL——既有 id 一經
            # 產生永不改變）與 instance 的 project_id 雙寫;新 DB 這裡是 no-op。
            cur = self._conn.execute("SELECT name FROM projects WHERE id IS NULL")
            for (project_name,) in cur.fetchall():
                self._conn.execute(
                    "UPDATE projects SET id = ? WHERE name = ? AND id IS NULL",
                    (str(uuid.uuid4()), project_name),
                )
            self._conn.execute(
                """
                UPDATE project_instances SET project_id = (
                    SELECT id FROM projects
                    WHERE projects.name = project_instances.project_name)
                WHERE project_id IS NULL
                """
            )
            # 唯一索引不能寫進 SCHEMA：舊 DB 要先 ALTER 補欄位,executescript
            # 先跑會因欄位不存在而失敗,所以放在遷移與 backfill 之後。
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_id ON projects(id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_instances_project_id"
                " ON project_instances(project_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_requester_actor_id"
                " ON approvals(requester_actor_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_decision_actor_id"
                " ON approvals(decision_actor_id)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_engineering_owner"
                " ON jobs(engineering_task_id, engineering_task_role, engineering_attempt_number)"
                " WHERE engineering_task_id IS NOT NULL"
                " AND engineering_task_role IS NOT NULL"
                " AND engineering_attempt_number IS NOT NULL"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_coding_runs_engineering_attempt"
                " ON coding_runs(engineering_task_id, attempt_number)"
                " WHERE engineering_task_id IS NOT NULL AND attempt_number IS NOT NULL"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coding_runs_project_version_id"
                " ON coding_runs(project_version_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_engineering_validation_request"
                " ON jobs(engineering_validation_request_id)"
            )
            # These indexes/triggers reference columns added above, so they
            # must run after representative legacy schemas have been ALTERed.
            self._conn.executescript(EXECUTION_ATTEMPT_POST_MIGRATION_SCHEMA)
            self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    @contextmanager
    def _immediate_cursor(self) -> Iterator[sqlite3.Cursor]:
        """Serialize a DG-EXEC material transaction across DB connections."""

        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        # ``check_same_thread=False`` permits the AppState background executor
        # to use this connection.  Closing without the same lock as ``cursor()``
        # can race a live sqlite3 call and crash the interpreter instead of
        # raising a normal ProgrammingError.  AppState drains its tracked calls
        # first; this lock is the final connection-level safety boundary.
        with self._lock:
            self._conn.close()

    # ---- DG-EXEC-ATTEMPT-v1 internal foundation -----------------------

    @staticmethod
    def _sqlite_now(cur: sqlite3.Cursor) -> str:
        cur.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
        return str(cur.fetchone()[0])

    @staticmethod
    def _sqlite_after(cur: sqlite3.Cursor, seconds: int) -> str:
        cur.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"+{seconds} seconds",),
        )
        return str(cur.fetchone()[0])

    @staticmethod
    def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    @staticmethod
    def _require_reason_code(reason_code: str) -> None:
        if reason_code not in EXECUTION_REASON_CODES:
            raise ValueError(f"invalid execution reason code: {reason_code}")

    @staticmethod
    def _append_execution_event(
        cur: sqlite3.Cursor,
        *,
        attempt_id: str,
        event_type: str,
        reason_code: str,
        evidence: dict[str, Any],
        operation_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        from_liveness: Optional[str] = None,
        to_liveness: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        Database._require_reason_code(reason_code)
        event_created_at = created_at or now_iso()
        evidence_json = canonical_json(evidence)
        digest_input = {
            "attempt_id": attempt_id,
            "operation_id": operation_id,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "from_liveness": from_liveness,
            "to_liveness": to_liveness,
            "reason_code": reason_code,
            "evidence": evidence,
            "created_at": event_created_at,
        }
        cur.execute(
            """
            INSERT INTO execution_attempt_events
                (attempt_id, operation_id, event_type, from_state, to_state,
                 from_liveness, to_liveness, reason_code, evidence_json,
                 event_sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                operation_id,
                event_type,
                from_state,
                to_state,
                from_liveness,
                to_liveness,
                reason_code,
                evidence_json,
                canonical_json_sha256(digest_input),
                event_created_at,
            ),
        )

    @staticmethod
    def _require_live_scheduler_lease(
        cur: sqlite3.Cursor,
        *,
        lease_name: str,
        owner_id: str,
        fencing_epoch: int,
    ) -> None:
        current_time = Database._sqlite_now(cur)
        cur.execute(
            """
            SELECT 1 FROM scheduler_leases
            WHERE name = ? AND owner_id = ? AND fencing_epoch = ?
              AND lease_expires_at > ?
            """,
            (lease_name, owner_id, fencing_epoch, current_time),
        )
        if cur.fetchone() is None:
            raise ValueError("leader_lease_lost")

    def insert_pinned_approval(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        contract_version: str,
        requester_actor_id: Optional[str] = None,
    ) -> int:
        """Insert a new immutable contract; legacy approval creation is unchanged."""

        expected_version: Optional[str]
        if kind in PINNED_EXECUTION_CONTRACTS:
            expected_version = PINNED_EXECUTION_CONTRACTS[kind]
        elif kind in PINNED_SERVER_CONFIG_KINDS:
            expected_version = "server-config-v1"
        elif kind == "stop":
            expected_version = "stop-intent-v1"
        else:
            expected_version = None
        if expected_version is None or contract_version != expected_version:
            raise ValueError("unsupported pinned approval contract")
        if kind not in VALID_APPROVAL_KINDS:
            raise ValueError(f"invalid approval kind: {kind}")
        if kind in PINNED_EXECUTION_CONTRACTS:
            validate_execution_contract(payload)
        elif kind == "stop":
            if (
                set(payload) != {"job_id", "source"}
                or isinstance(payload.get("job_id"), bool)
                or not isinstance(payload.get("job_id"), int)
                or payload["job_id"] < 1
                or not isinstance(payload.get("source"), str)
                or not payload["source"].strip()
            ):
                raise ValueError("invalid stop intent contract")

        payload_json = canonical_json(payload)
        payload_sha256 = utf8_sha256(payload_json)
        immutable_at = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approvals
                    (kind, payload, status, created_at, requester_actor_id,
                     payload_sha256, payload_contract_version,
                     payload_immutable_at)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    payload_json,
                    immutable_at,
                    requester_actor_id,
                    payload_sha256,
                    contract_version,
                    immutable_at,
                ),
            )
            return int(cur.lastrowid)

    def mark_approval_materialization_started(
        self,
        *,
        approval_id: int,
        expected_payload_sha256: str,
        decision_actor_id: str,
    ) -> bool:
        """One-way CAS used before a pinned approval materializes resources."""

        marker = now_iso()
        with self._immediate_cursor() as cur:
            cur.execute(
                """
                UPDATE approvals
                SET materialization_started_at = ?, decision_actor_id = ?
                WHERE id = ? AND status = 'pending'
                  AND payload_sha256 = ?
                  AND materialization_started_at IS NULL
                """,
                (
                    marker,
                    decision_actor_id,
                    approval_id,
                    expected_payload_sha256,
                ),
            )
            return cur.rowcount == 1

    def materialize_pinned_execution_jobs(
        self,
        *,
        approval_id: int,
        expected_payload_sha256: str,
        decision_actor_id: str,
        decision_mechanism: str = "manual",
    ) -> dict[str, int]:
        """Publish one complete pinned Job graph or roll the transaction back."""

        if not decision_actor_id:
            raise ValueError("decision actor is required")
        if not decision_mechanism:
            raise ValueError("decision mechanism is required")
        with self._immediate_cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            approval = cur.fetchone()
            if approval is None:
                raise ValueError("approval_missing")
            if approval["status"] != "pending":
                raise ValueError("approval_not_approved")
            expected_version = PINNED_EXECUTION_CONTRACTS.get(approval["kind"])
            if expected_version is None:
                raise ValueError("approval_kind_mismatch")
            if (
                approval["payload_sha256"] != expected_payload_sha256
                or approval["payload_contract_version"] != expected_version
                or approval["materialization_started_at"] is not None
            ):
                raise ValueError("contract_digest_mismatch")
            try:
                contract = json.loads(approval["payload"])
                validate_execution_contract(contract)
            except (json.JSONDecodeError, TypeError, ValueError):
                raise ValueError("contract_digest_mismatch") from None
            if canonical_json(contract) != approval["payload"]:
                raise ValueError("contract_digest_mismatch")

            specs = list(contract["job_specs"])
            specs_by_role = {spec["role"]: spec for spec in specs}
            ordered_roles: list[str] = []
            pending_roles = [spec["role"] for spec in specs]
            while pending_roles:
                ready = [
                    role
                    for role in pending_roles
                    if all(
                        dependency in ordered_roles
                        for dependency in specs_by_role[role].get(
                            "depends_on_roles", []
                        )
                    )
                ]
                if not ready:
                    raise ValueError("execution contract dependency cycle")
                for role in ready:
                    ordered_roles.append(role)
                    pending_roles.remove(role)

            timestamp = now_iso()
            cur.execute(
                """
                UPDATE approvals
                SET materialization_started_at = ?, decision_actor_id = ?,
                    decision_mechanism = ?
                WHERE id = ? AND status = 'pending'
                  AND payload_sha256 = ?
                  AND materialization_started_at IS NULL
                """,
                (
                    timestamp,
                    decision_actor_id,
                    decision_mechanism,
                    approval_id,
                    expected_payload_sha256,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")

            job_ids: dict[str, int] = {}
            for role in ordered_roles:
                spec = specs_by_role[role]
                command = base64.b64decode(
                    spec["command_utf8_b64"], validate=True
                ).decode("utf-8")
                dependency_ids = [
                    job_ids[dependency]
                    for dependency in spec.get("depends_on_roles", [])
                ]
                project = spec.get("project", contract.get("project"))
                require_tag = spec.get(
                    "require_tag", contract.get("require_tag")
                )
                pin_server = spec.get("pin_server", contract.get("pin_server"))
                gpus_needed = spec.get(
                    "gpus_needed", contract.get("gpus_needed")
                )
                priority = spec.get("priority", contract.get("priority", "normal"))
                if project is not None and not isinstance(project, str):
                    raise ValueError("execution contract project is invalid")
                if require_tag is not None and not isinstance(require_tag, str):
                    raise ValueError("execution contract require_tag is invalid")
                if pin_server is not None and not isinstance(pin_server, str):
                    raise ValueError("execution contract pin_server is invalid")
                if (
                    gpus_needed is not None
                    and (
                        isinstance(gpus_needed, bool)
                        or not isinstance(gpus_needed, int)
                        or gpus_needed < 0
                    )
                ):
                    raise ValueError("execution contract gpus_needed is invalid")
                if priority not in VALID_PRIORITIES:
                    raise ValueError("execution contract priority is invalid")
                cur.execute(
                    """
                    INSERT INTO jobs
                        (type, project, command, require_tag, pin_server,
                         depends_on, gpus_needed, status, priority, created_at,
                         auto_placement_approval_id, execution_approval_id,
                         approved_payload_sha256, execution_contract_version,
                         execution_contract_role, approved_command_sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec["type"],
                        project,
                        command,
                        require_tag,
                        pin_server,
                        canonical_json(dependency_ids),
                        gpus_needed,
                        priority,
                        timestamp,
                        approval_id
                        if approval["kind"] == "auto_placement"
                        else None,
                        approval_id,
                        approval["payload_sha256"],
                        approval["payload_contract_version"],
                        role,
                        spec["command_sha256"],
                    ),
                )
                job_ids[role] = int(cur.lastrowid)

            cur.execute(
                """
                UPDATE approvals
                SET status = 'approved', decided_at = ?
                WHERE id = ? AND status = 'pending'
                  AND materialization_started_at = ?
                """,
                (timestamp, approval_id, timestamp),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            return job_ids

    def create_legacy_job_stop_intent(
        self,
        *,
        approval_id: int,
        job_id: int,
        intent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist one pinned legacy stop intent before any remote effect."""

        intent_id = intent_id or str(uuid.uuid4())
        with self._immediate_cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            approval = cur.fetchone()
            if approval is None:
                raise ValueError("approval_missing")
            if approval["status"] != "pending":
                raise ValueError("approval_not_approved")
            if (
                approval["kind"] != "stop"
                or approval["payload_contract_version"] != "stop-intent-v1"
                or approval["payload_sha256"] is None
                or approval["payload_immutable_at"] is None
            ):
                raise ValueError(
                    "legacy stop approval is unpinned; reject and re-create it"
                )
            try:
                payload = json.loads(approval["payload"])
            except (json.JSONDecodeError, TypeError):
                raise ValueError("contract_digest_mismatch") from None
            if (
                canonical_json(payload) != approval["payload"]
                or utf8_sha256(approval["payload"])
                != approval["payload_sha256"]
                or set(payload) != {"job_id", "source"}
                or payload.get("job_id") != job_id
                or isinstance(payload.get("job_id"), bool)
                or not isinstance(payload.get("source"), str)
                or not payload["source"].strip()
            ):
                raise ValueError("contract_digest_mismatch")

            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            job = cur.fetchone()
            if job is None:
                raise ValueError("job_missing")
            if job["status"] != "running":
                raise ValueError("claim_conflict")
            if not job["server"]:
                raise ValueError("legacy running job has no execution server")
            cur.execute(
                "SELECT 1 FROM execution_attempts WHERE job_id = ? LIMIT 1",
                (job_id,),
            )
            if cur.fetchone() is not None:
                raise ValueError(
                    "generic execution attempt requires a generic stop operation"
                )

            cur.execute(
                """
                SELECT * FROM legacy_job_stop_intents
                WHERE stop_approval_id = ? AND job_id = ?
                ORDER BY requested_at, id
                LIMIT 1
                """,
                (approval_id, job_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if (
                    existing["approved_payload_sha256"]
                    != approval["payload_sha256"]
                    or existing["server_name"] != job["server"]
                ):
                    raise ValueError("contract_digest_mismatch")
                return dict(existing)

            requested_at = now_iso()
            try:
                cur.execute(
                    """
                    INSERT INTO legacy_job_stop_intents
                        (id, job_id, stop_approval_id,
                         approved_payload_sha256, backend, server_name,
                         state, requested_at)
                    VALUES (?, ?, ?, ?, 'ssh', ?, 'requested', ?)
                    """,
                    (
                        intent_id,
                        job_id,
                        approval_id,
                        approval["payload_sha256"],
                        job["server"],
                        requested_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("claim_conflict") from exc
            cur.execute(
                "SELECT * FROM legacy_job_stop_intents WHERE id = ?",
                (intent_id,),
            )
            return dict(cur.fetchone())

    def begin_legacy_job_stop_delivery(self, intent_id: str) -> bool:
        """Cross the material-effect boundary once; retries never resend kill."""

        with self._immediate_cursor() as cur:
            timestamp = now_iso()
            cur.execute(
                """
                UPDATE legacy_job_stop_intents
                SET state = 'delivery_uncertain', delivery_started_at = ?
                WHERE id = ? AND state = 'requested'
                  AND delivery_started_at IS NULL
                """,
                (timestamp, intent_id),
            )
            return cur.rowcount == 1

    def mark_legacy_job_stop_delivered(self, intent_id: str) -> dict[str, Any]:
        with self._immediate_cursor() as cur:
            timestamp = now_iso()
            cur.execute(
                """
                UPDATE legacy_job_stop_intents
                SET state = 'delivered', delivered_at = ?,
                    last_error_category = NULL,
                    sanitized_error_detail = NULL
                WHERE id = ? AND state = 'delivery_uncertain'
                """,
                (timestamp, intent_id),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            cur.execute(
                "SELECT * FROM legacy_job_stop_intents WHERE id = ?",
                (intent_id,),
            )
            return dict(cur.fetchone())

    def record_legacy_job_stop_uncertain(
        self,
        intent_id: str,
        *,
        error_category: str,
        sanitized_error_detail: Optional[str] = None,
    ) -> dict[str, Any]:
        if not error_category:
            raise ValueError("stop delivery error category is required")
        with self._immediate_cursor() as cur:
            cur.execute(
                """
                UPDATE legacy_job_stop_intents
                SET last_error_category = ?, sanitized_error_detail = ?
                WHERE id = ? AND state = 'delivery_uncertain'
                """,
                (error_category, sanitized_error_detail, intent_id),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            cur.execute(
                "SELECT * FROM legacy_job_stop_intents WHERE id = ?",
                (intent_id,),
            )
            return dict(cur.fetchone())

    def record_legacy_job_stop_pre_delivery_failure(
        self,
        intent_id: str,
        *,
        error_category: str,
        sanitized_error_detail: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record a definite pre-effect refusal without claiming delivery."""

        if not error_category:
            raise ValueError("stop delivery error category is required")
        with self._immediate_cursor() as cur:
            cur.execute(
                """
                UPDATE legacy_job_stop_intents
                SET last_error_category = ?, sanitized_error_detail = ?
                WHERE id = ? AND state = 'requested'
                  AND delivery_started_at IS NULL
                """,
                (error_category, sanitized_error_detail, intent_id),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            cur.execute(
                "SELECT * FROM legacy_job_stop_intents WHERE id = ?",
                (intent_id,),
            )
            return dict(cur.fetchone())

    def get_legacy_job_stop_intent(
        self,
        *,
        job_id: Optional[int] = None,
        approval_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        if job_id is None and approval_id is None:
            raise ValueError("job_id or approval_id is required")
        clauses = []
        params: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if approval_id is not None:
            clauses.append("stop_approval_id = ?")
            params.append(approval_id)
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM legacy_job_stop_intents WHERE "
                + " AND ".join(clauses)
                + " ORDER BY requested_at DESC, id DESC LIMIT 1",
                params,
            )
            return self._row_dict(cur.fetchone())

    def has_unresolved_legacy_job_stop_intent(self, job_id: int) -> bool:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM legacy_job_stop_intents
                WHERE job_id = ?
                  AND state IN ('requested', 'delivery_uncertain', 'delivered')
                LIMIT 1
                """,
                (job_id,),
            )
            return cur.fetchone() is not None

    def prepare_server_config_mutation(
        self,
        *,
        approval_id: int,
        operation: str,
        server_name: str,
        yaml_before_sha256: str,
        yaml_after_sha256: str,
        decision_actor_id: str,
        normalized_target: Optional[dict[str, Any]] = None,
        credential_ref: Optional[dict[str, Any]] = None,
        prior_revision_id: Optional[str] = None,
        mutation_id: Optional[str] = None,
        revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Atomically pin approval materialization, journal intent and revision."""

        expected_kind = {
            "add": "server_add",
            "update": "server_update",
            "disable": "server_disable",
            "delete": "server_delete",
        }.get(operation)
        if expected_kind is None:
            raise ValueError("invalid server config mutation operation")
        if operation in {"add", "update"}:
            if normalized_target is None or credential_ref is None:
                raise ValueError("add/update requires an exact prepared target")
        elif normalized_target is not None or credential_ref is not None:
            raise ValueError("disable/delete must not fabricate a target revision")
        if (
            len(yaml_before_sha256) != 64
            or len(yaml_after_sha256) != 64
            or not _is_full_hex_digest(yaml_before_sha256)
            or not _is_full_hex_digest(yaml_after_sha256)
        ):
            raise ValueError("server config YAML digests must be SHA-256")

        mutation_id = mutation_id or str(uuid.uuid4())
        revision_id = revision_id or (
            str(uuid.uuid4()) if operation in {"add", "update"} else None
        )
        created_at = now_iso()
        with self._immediate_cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            approval = cur.fetchone()
            if approval is None:
                raise ValueError("approval_missing")
            if approval["kind"] != expected_kind:
                raise ValueError("approval_kind_mismatch")
            if approval["status"] != "pending":
                raise ValueError("approval_not_approved")
            try:
                payload = json.loads(approval["payload"])
                payload_is_canonical = canonical_json(payload) == approval["payload"]
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
                payload_is_canonical = False
            if (
                approval["payload_sha256"] is None
                or approval["payload_contract_version"] != "server-config-v1"
                or not payload_is_canonical
            ):
                raise ValueError("contract_digest_mismatch")
            if (
                payload.get("operation") != operation
                or payload.get("server_name") != server_name
                or payload.get("yaml_before_sha256") != yaml_before_sha256
                or payload.get("yaml_after_sha256") != yaml_after_sha256
                or payload.get("normalized_target") != normalized_target
                or payload.get("credential_ref") != credential_ref
            ):
                raise ValueError("contract_digest_mismatch")

            cur.execute(
                """
                SELECT * FROM server_config_revisions
                WHERE server_name = ? AND publication_state = 'active'
                """,
                (server_name,),
            )
            current_revision = cur.fetchone()
            if operation == "add":
                if current_revision is not None or prior_revision_id is not None:
                    raise ValueError("target_identity_mismatch")
            else:
                if (
                    current_revision is None
                    or current_revision["id"] != prior_revision_id
                ):
                    raise ValueError("target_revision_missing")

            cur.execute(
                """
                UPDATE approvals
                SET materialization_started_at = ?, decision_actor_id = ?
                WHERE id = ? AND status = 'pending'
                  AND materialization_started_at IS NULL
                """,
                (created_at, decision_actor_id, approval_id),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")

            if revision_id is not None:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(revision), 0) + 1
                    FROM server_config_revisions
                    WHERE server_name = ?
                    """,
                    (server_name,),
                )
                revision = int(cur.fetchone()[0])
                normalized_target_json = canonical_json(normalized_target)
                credential_ref_json = canonical_json(credential_ref)
                target_identity_sha256 = canonical_json_sha256(
                    {
                        "normalized_target": normalized_target,
                        "credential_ref": credential_ref,
                    }
                )
                cur.execute(
                    """
                    INSERT INTO server_config_revisions
                        (id, server_name, revision, normalized_target_json,
                         credential_ref_json, target_identity_sha256,
                         assignment_eligibility, publication_state,
                         created_by_approval_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'approved', 'prepared', ?, ?)
                    """,
                    (
                        revision_id,
                        server_name,
                        revision,
                        normalized_target_json,
                        credential_ref_json,
                        target_identity_sha256,
                        approval_id,
                        created_at,
                    ),
                )
            cur.execute(
                """
                INSERT INTO server_config_mutations
                    (id, approval_id, operation, server_name,
                     prior_revision_id, prepared_revision_id,
                     approved_payload_sha256, yaml_before_sha256,
                     yaml_after_sha256, state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?)
                """,
                (
                    mutation_id,
                    approval_id,
                    operation,
                    server_name,
                    prior_revision_id,
                    revision_id,
                    approval["payload_sha256"],
                    yaml_before_sha256,
                    yaml_after_sha256,
                    created_at,
                ),
            )
            cur.execute(
                "SELECT * FROM server_config_mutations WHERE id = ?",
                (mutation_id,),
            )
            mutation = dict(cur.fetchone())
            if revision_id is not None:
                cur.execute(
                    "SELECT * FROM server_config_revisions WHERE id = ?",
                    (revision_id,),
                )
                mutation["prepared_revision"] = dict(cur.fetchone())
            else:
                mutation["prepared_revision"] = None
            return mutation

    def transition_server_config_mutation(
        self,
        *,
        mutation_id: str,
        expected_state: str,
        new_state: str,
        observed_yaml_sha256: str,
        last_error_category: Optional[str] = None,
        sanitized_error_detail: Optional[str] = None,
    ) -> dict[str, Any]:
        """CAS publication evidence; compensation accepts only exact digests."""

        allowed = {
            "intent": {"yaml_applied", "rolled_back", "recovery_hold"},
            "yaml_applied": {"rolled_back", "recovery_hold"},
            "recovery_hold": {"yaml_applied", "rolled_back"},
        }
        if new_state not in allowed.get(expected_state, set()):
            raise ValueError("invalid server config mutation transition")
        with self._immediate_cursor() as cur:
            cur.execute(
                "SELECT * FROM server_config_mutations WHERE id = ?",
                (mutation_id,),
            )
            mutation = cur.fetchone()
            if mutation is None or mutation["state"] != expected_state:
                raise ValueError("claim_conflict")
            if (
                new_state == "yaml_applied"
                and observed_yaml_sha256 != mutation["yaml_after_sha256"]
            ):
                raise ValueError("target_identity_mismatch")
            if (
                new_state == "rolled_back"
                and observed_yaml_sha256 != mutation["yaml_before_sha256"]
            ):
                raise ValueError("target_identity_mismatch")
            if new_state == "recovery_hold" and observed_yaml_sha256 in {
                mutation["yaml_before_sha256"],
                mutation["yaml_after_sha256"],
            }:
                raise ValueError("recovery_hold requires a third or unreadable digest")

            timestamp = now_iso()
            cur.execute(
                """
                UPDATE server_config_mutations
                SET state = ?,
                    yaml_applied_at =
                        CASE WHEN ? = 'yaml_applied' THEN ? ELSE yaml_applied_at END,
                    last_error_category = ?,
                    sanitized_error_detail = ?
                WHERE id = ? AND state = ?
                """,
                (
                    new_state,
                    new_state,
                    timestamp,
                    last_error_category,
                    sanitized_error_detail,
                    mutation_id,
                    expected_state,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            if new_state == "rolled_back":
                if mutation["prepared_revision_id"] is not None:
                    cur.execute(
                        """
                        UPDATE server_config_revisions
                        SET publication_state = 'retired', retired_at = ?
                        WHERE id = ? AND publication_state = 'prepared'
                        """,
                        (timestamp, mutation["prepared_revision_id"]),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("claim_conflict")
                cur.execute(
                    """
                    UPDATE approvals
                    SET status = 'rejected', decided_at = ?,
                        note = 'server config materialization rolled back'
                    WHERE id = ? AND status = 'pending'
                    """,
                    (timestamp, mutation["approval_id"]),
                )
                if cur.rowcount != 1:
                    raise ValueError("claim_conflict")
            cur.execute(
                "SELECT * FROM server_config_mutations WHERE id = ?",
                (mutation_id,),
            )
            return dict(cur.fetchone())

    def activate_server_config_mutation(
        self,
        *,
        mutation_id: str,
        observed_yaml_sha256: str,
    ) -> dict[str, Any]:
        """Finalize revision(s), journal and approval in one transaction."""

        with self._immediate_cursor() as cur:
            cur.execute(
                """
                SELECT mutation.*, approval.status AS approval_status,
                       approval.payload_sha256 AS current_payload_sha256,
                       approval.payload_contract_version,
                       approval.materialization_started_at
                FROM server_config_mutations AS mutation
                JOIN approvals AS approval ON approval.id = mutation.approval_id
                WHERE mutation.id = ?
                """,
                (mutation_id,),
            )
            mutation = cur.fetchone()
            if mutation is None or mutation["state"] != "yaml_applied":
                raise ValueError("claim_conflict")
            if observed_yaml_sha256 != mutation["yaml_after_sha256"]:
                raise ValueError("target_identity_mismatch")
            if (
                mutation["approval_status"] != "pending"
                or mutation["current_payload_sha256"]
                != mutation["approved_payload_sha256"]
                or mutation["payload_contract_version"] != "server-config-v1"
                or mutation["materialization_started_at"] is None
            ):
                raise ValueError("contract_digest_mismatch")

            timestamp = now_iso()
            if mutation["prior_revision_id"] is not None:
                cur.execute(
                    """
                    UPDATE server_config_revisions
                    SET publication_state = 'retired', retired_at = ?
                    WHERE id = ? AND publication_state = 'active'
                    """,
                    (timestamp, mutation["prior_revision_id"]),
                )
                if cur.rowcount != 1:
                    raise ValueError("claim_conflict")
            if mutation["prepared_revision_id"] is not None:
                cur.execute(
                    """
                    UPDATE server_config_revisions
                    SET publication_state = 'active', activated_at = ?
                    WHERE id = ? AND publication_state = 'prepared'
                    """,
                    (timestamp, mutation["prepared_revision_id"]),
                )
                if cur.rowcount != 1:
                    raise ValueError("claim_conflict")
            cur.execute(
                """
                UPDATE server_config_mutations
                SET state = 'activated', activated_at = ?
                WHERE id = ? AND state = 'yaml_applied'
                """,
                (timestamp, mutation_id),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            cur.execute(
                """
                UPDATE approvals
                SET status = 'approved', decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (timestamp, mutation["approval_id"]),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            cur.execute(
                "SELECT * FROM server_config_mutations WHERE id = ?",
                (mutation_id,),
            )
            return dict(cur.fetchone())

    def acquire_scheduler_lease(
        self,
        *,
        owner_id: str,
        lease_seconds: int,
        name: str = "execution-attempt-v1",
    ) -> Optional[dict[str, Any]]:
        """Acquire/renew with SQLite time; a post-expiry owner gets a new epoch."""

        if not owner_id:
            raise ValueError("scheduler owner_id must not be blank")
        if isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("scheduler lease_seconds must be positive")
        with self._immediate_cursor() as cur:
            current_time = self._sqlite_now(cur)
            lease_expires_at = self._sqlite_after(cur, lease_seconds)
            cur.execute("SELECT * FROM scheduler_leases WHERE name = ?", (name,))
            current = cur.fetchone()
            if current is None:
                epoch = 1
                cur.execute(
                    """
                    INSERT INTO scheduler_leases
                        (name, owner_id, fencing_epoch, lease_expires_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, owner_id, epoch, lease_expires_at, current_time),
                )
            elif (
                current["owner_id"] == owner_id
                and current["lease_expires_at"] > current_time
            ):
                epoch = int(current["fencing_epoch"])
                cur.execute(
                    """
                    UPDATE scheduler_leases
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE name = ? AND owner_id = ? AND fencing_epoch = ?
                    """,
                    (
                        lease_expires_at,
                        current_time,
                        name,
                        owner_id,
                        epoch,
                    ),
                )
            elif current["lease_expires_at"] <= current_time:
                epoch = int(current["fencing_epoch"]) + 1
                cur.execute(
                    """
                    UPDATE scheduler_leases
                    SET owner_id = ?, fencing_epoch = ?, lease_expires_at = ?,
                        updated_at = ?
                    WHERE name = ? AND fencing_epoch = ?
                      AND lease_expires_at <= ?
                    """,
                    (
                        owner_id,
                        epoch,
                        lease_expires_at,
                        current_time,
                        name,
                        current["fencing_epoch"],
                        current_time,
                    ),
                )
                if cur.rowcount != 1:
                    return None
            else:
                return None

            cur.execute("SELECT * FROM scheduler_leases WHERE name = ?", (name,))
            return dict(cur.fetchone())

    def get_execution_control_plane_telemetry(
        self,
        *,
        lease_name: str = "execution-attempt-v1",
    ) -> dict[str, Any]:
        """Return a read-only, evidence-backed WP-2A telemetry snapshot.

        Ages and lease liveness use the same SQLite clock as fencing.  Empty
        evidence is represented by ``None``/an empty mapping; this method does
        not synthesize a reconciler success or infer remote state from
        reachability.
        """

        with self.cursor() as cur:
            observed_at = self._sqlite_now(cur)

            cur.execute(
                "SELECT * FROM scheduler_leases WHERE name = ?",
                (lease_name,),
            )
            lease_row = cur.fetchone()
            lease = None
            if lease_row is not None:
                lease = {
                    "name": lease_row["name"],
                    "owner_id": lease_row["owner_id"],
                    "fencing_epoch": int(lease_row["fencing_epoch"]),
                    "lease_expires_at": lease_row["lease_expires_at"],
                    "updated_at": lease_row["updated_at"],
                    "is_live": lease_row["lease_expires_at"] > observed_at,
                }

            cur.execute(
                """
                SELECT backend, state, COUNT(*) AS count
                FROM execution_attempts
                GROUP BY backend, state
                ORDER BY backend, state
                """
            )
            attempts_by_backend_state: dict[str, dict[str, int]] = {}
            attempts_by_state: dict[str, int] = {}
            attempts_by_backend: dict[str, int] = {}
            for row in cur.fetchall():
                count = int(row["count"])
                backend = str(row["backend"])
                state = str(row["state"])
                attempts_by_backend_state.setdefault(backend, {})[state] = count
                attempts_by_state[state] = attempts_by_state.get(state, 0) + count
                attempts_by_backend[backend] = (
                    attempts_by_backend.get(backend, 0) + count
                )

            cur.execute(
                """
                SELECT
                    CASE WHEN COUNT(*) = 0 THEN NULL
                    ELSE CAST(
                        MAX(
                            0,
                            (julianday(?) - julianday(MIN(created_at))) * 86400
                        ) AS INTEGER
                    )
                    END AS age_seconds
                FROM execution_attempts
                WHERE liveness = 'unknown'
                  AND state IN ('leased', 'dispatching', 'running')
                """,
                (observed_at,),
            )
            oldest_unknown_age_seconds = cur.fetchone()["age_seconds"]

            cur.execute(
                """
                SELECT operation, state, COUNT(*) AS count
                FROM execution_operations
                GROUP BY operation, state
                ORDER BY operation, state
                """
            )
            operations_by_type_state: dict[str, dict[str, int]] = {}
            operations_by_state: dict[str, int] = {}
            for row in cur.fetchall():
                count = int(row["count"])
                operation = str(row["operation"])
                state = str(row["state"])
                operations_by_type_state.setdefault(operation, {})[state] = count
                operations_by_state[state] = (
                    operations_by_state.get(state, 0) + count
                )

            cur.execute(
                """
                SELECT
                    CASE WHEN COUNT(*) = 0 THEN NULL
                    ELSE CAST(
                        MAX(
                            0,
                            (julianday(?) - julianday(MIN(updated_at))) * 86400
                        ) AS INTEGER
                    )
                    END AS age_seconds
                FROM execution_operations
                WHERE state = 'uncertain'
                """,
                (observed_at,),
            )
            oldest_uncertain_age_seconds = cur.fetchone()["age_seconds"]

            cur.execute(
                """
                SELECT
                    COUNT(*) AS depth,
                    CASE WHEN COUNT(*) = 0 THEN NULL
                    ELSE CAST(
                        MAX(
                            0,
                            (julianday(?) - julianday(MIN(created_at))) * 86400
                        ) AS INTEGER
                    )
                    END AS oldest_age_seconds
                FROM jobs
                WHERE status = 'queued'
                """,
                (observed_at,),
            )
            queue_row = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM execution_attempt_events
                WHERE reason_code = 'claim_conflict'
                """
            )
            recorded_duplicate_prevention_count = int(cur.fetchone()["count"])

            unresolved_states = ("pending", "processing", "uncertain")
            outbox_backlog = sum(
                operations_by_state.get(state, 0) for state in unresolved_states
            )
            collection_by_state = dict(
                operations_by_type_state.get("collect", {})
            )

            return {
                "observed_at": observed_at,
                "lease": lease,
                "attempts": {
                    "total": sum(attempts_by_state.values()),
                    "by_state": attempts_by_state,
                    "by_backend": attempts_by_backend,
                    "by_backend_state": attempts_by_backend_state,
                    "oldest_active_unknown_age_seconds": (
                        int(oldest_unknown_age_seconds)
                        if oldest_unknown_age_seconds is not None
                        else None
                    ),
                },
                "outbox": {
                    "backlog": outbox_backlog,
                    "uncertain": operations_by_state.get("uncertain", 0),
                    "oldest_uncertain_age_seconds": (
                        int(oldest_uncertain_age_seconds)
                        if oldest_uncertain_age_seconds is not None
                        else None
                    ),
                    "by_state": operations_by_state,
                    "by_operation_state": operations_by_type_state,
                },
                "queue": {
                    "depth": int(queue_row["depth"]),
                    "oldest_age_seconds": (
                        int(queue_row["oldest_age_seconds"])
                        if queue_row["oldest_age_seconds"] is not None
                        else None
                    ),
                },
                "collection": {
                    "by_state": collection_by_state,
                    "backlog": sum(
                        collection_by_state.get(state, 0)
                        for state in unresolved_states
                    ),
                },
                "duplicate_prevention": {
                    "recorded_count": recorded_duplicate_prevention_count,
                    "source": (
                        "execution_attempt_events.reason_code=claim_conflict; "
                        "recorded lower bound"
                    ),
                },
            }

    def has_active_execution_ownership(self) -> bool:
        """True means startup must keep reconciliation/outbox ownership alive."""

        with self.cursor() as cur:
            cur.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM execution_attempts
                        WHERE state IN ('leased', 'dispatching', 'running')
                    )
                    OR EXISTS(
                        SELECT 1 FROM execution_operations
                        WHERE state IN ('pending', 'processing', 'uncertain')
                    )
                """
            )
            return bool(cur.fetchone()[0])

    def get_server_execution_blockers(
        self,
        server_name: str,
    ) -> dict[str, list[Any]]:
        """Return durable owners that make delete/repoint/rotation unsafe."""

        with self.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM execution_attempts
                WHERE server_name = ?
                  AND state IN ('leased', 'dispatching', 'running')
                ORDER BY id
                """,
                (server_name,),
            )
            generic_attempt_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                """
                SELECT operation.id
                FROM execution_operations AS operation
                JOIN execution_attempts AS attempt
                  ON attempt.id = operation.attempt_id
                WHERE attempt.server_name = ?
                  AND operation.state IN ('pending', 'processing', 'uncertain')
                ORDER BY operation.id
                """,
                (server_name,),
            )
            operation_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                """
                SELECT attempt.id
                FROM node_attempts AS attempt
                JOIN nodes AS node ON node.id = attempt.node_id
                WHERE node.server_name = ?
                  AND attempt.execution_attempt_id IS NULL
                  AND (
                      (
                          attempt.status = 'leased'
                          AND julianday(attempt.lease_expires_at)
                              > julianday('now')
                      )
                      OR attempt.status IN ('acked', 'running')
                  )
                ORDER BY attempt.id
                """,
                (server_name,),
            )
            legacy_node_attempt_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id FROM nodes
                WHERE server_name = ? AND status = 'enrolled'
                  AND revoked_at IS NULL
                ORDER BY id
                """,
                (server_name,),
            )
            enrolled_node_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id FROM jobs
                WHERE server = ? AND status = 'running'
                ORDER BY id
                """,
                (server_name,),
            )
            running_job_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id FROM legacy_job_stop_intents
                WHERE server_name = ?
                  AND state IN ('requested', 'delivery_uncertain', 'delivered')
                ORDER BY id
                """,
                (server_name,),
            )
            legacy_stop_intent_ids = [row["id"] for row in cur.fetchall()]
        return {
            "generic_attempt_ids": generic_attempt_ids,
            "operation_ids": operation_ids,
            "legacy_node_attempt_ids": legacy_node_attempt_ids,
            "enrolled_node_ids": enrolled_node_ids,
            "running_job_ids": running_job_ids,
            "legacy_stop_intent_ids": legacy_stop_intent_ids,
        }

    def server_has_execution_blockers(self, server_name: str) -> bool:
        return any(self.get_server_execution_blockers(server_name).values())

    def get_unresolved_server_config_mutation(self) -> Optional[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM server_config_mutations
                WHERE state IN ('intent', 'yaml_applied', 'recovery_hold')
                ORDER BY created_at, id
                LIMIT 1
                """
            )
            return self._row_dict(cur.fetchone())

    def record_execution_shadow_tick(
        self,
        *,
        tick_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Write measurements only; never claim or mutate Job/attempt truth."""

        tick_id = tick_id or str(uuid.uuid4())
        created_at = now_iso()
        observations: list[dict[str, Any]] = []
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM jobs
                ORDER BY id
                """
            )
            jobs = cur.fetchall()
            for job in jobs:
                cur.execute(
                    """
                    SELECT legacy_status_after
                    FROM execution_shadow_observations
                    WHERE job_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (job["id"],),
                )
                latest = cur.fetchone()
                status_before = (
                    latest["legacy_status_after"]
                    if latest is not None
                    and latest["legacy_status_after"] is not None
                    else job["status"]
                )
                if latest is not None and status_before == job["status"]:
                    continue

                proposed_backend: Optional[str] = None
                proposed_server_name = job["server"] or job["pin_server"]
                proposed_revision_id: Optional[str] = None
                reason_code = "contract_validated"
                eligible = False

                cur.execute(
                    "SELECT * FROM approvals WHERE id = ?",
                    (job["execution_approval_id"],),
                )
                approval = cur.fetchone()
                if approval is None:
                    reason_code = "approval_missing"
                else:
                    try:
                        self._validate_pinned_job_contract(
                            job=job,
                            approval=approval,
                        )
                    except ValueError as exc:
                        reason_code = (
                            str(exc)
                            if str(exc) in EXECUTION_REASON_CODES
                            else "contract_digest_mismatch"
                        )
                    else:
                        if proposed_server_name is None:
                            reason_code = "target_revision_missing"
                        else:
                            cur.execute(
                                """
                                SELECT revision.*
                                FROM server_config_revisions AS revision
                                JOIN approvals AS creator
                                  ON creator.id = revision.created_by_approval_id
                                WHERE revision.server_name = ?
                                  AND revision.publication_state = 'active'
                                  AND revision.assignment_eligibility = 'approved'
                                  AND creator.status = 'approved'
                                """,
                                (proposed_server_name,),
                            )
                            revision = cur.fetchone()
                            cur.execute(
                                """
                                SELECT 1 FROM server_config_mutations
                                WHERE server_name = ?
                                  AND state IN (
                                      'intent', 'yaml_applied', 'recovery_hold'
                                  )
                                """,
                                (proposed_server_name,),
                            )
                            unresolved = cur.fetchone() is not None
                            if revision is None or unresolved:
                                reason_code = "target_revision_missing"
                            else:
                                try:
                                    normalized_target = json.loads(
                                        revision["normalized_target_json"]
                                    )
                                except (json.JSONDecodeError, TypeError):
                                    reason_code = "target_identity_mismatch"
                                else:
                                    proposed_backend = normalized_target.get(
                                        "backend"
                                    )
                                    if proposed_backend not in {"ssh", "node"}:
                                        reason_code = "target_identity_mismatch"
                                    else:
                                        proposed_revision_id = revision["id"]
                                        eligible = True

                transitioned = status_before != job["status"]
                parity_result = (
                    "eligible_transition"
                    if eligible and transitioned
                    else "eligible_stable"
                    if eligible
                    else "ineligible_transition"
                    if transitioned
                    else "ineligible_stable"
                )
                cur.execute(
                    """
                    INSERT INTO execution_shadow_observations
                        (tick_id, job_id, proposed_backend,
                         proposed_server_name, proposed_revision_id,
                         approved_payload_sha256, legacy_status_before,
                         legacy_status_after, parity_result, reason_code,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tick_id,
                        job["id"],
                        proposed_backend,
                        proposed_server_name,
                        proposed_revision_id,
                        job["approved_payload_sha256"],
                        status_before,
                        job["status"],
                        parity_result,
                        reason_code,
                        created_at,
                    ),
                )
                cur.execute(
                    """
                    SELECT * FROM execution_shadow_observations
                    WHERE id = last_insert_rowid()
                    """
                )
                observations.append(dict(cur.fetchone()))
        return observations

    @staticmethod
    def _validate_pinned_job_contract(
        *,
        job: sqlite3.Row,
        approval: sqlite3.Row,
    ) -> None:
        if approval["status"] != "approved":
            raise ValueError("approval_not_approved")
        expected_version = PINNED_EXECUTION_CONTRACTS.get(approval["kind"])
        if expected_version is None:
            raise ValueError("approval_kind_mismatch")
        if (
            approval["payload_sha256"] is None
            or approval["payload_contract_version"] != expected_version
            or job["execution_approval_id"] != approval["id"]
            or job["approved_payload_sha256"] != approval["payload_sha256"]
            or job["execution_contract_version"]
            != approval["payload_contract_version"]
        ):
            raise ValueError("contract_digest_mismatch")
        if (
            job["execution_contract_role"] is None
            or job["approved_command_sha256"] is None
            or utf8_sha256(job["command"]) != job["approved_command_sha256"]
        ):
            raise ValueError("contract_digest_mismatch")

        try:
            contract = json.loads(approval["payload"])
            job_specs = contract["job_specs"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ValueError("contract_digest_mismatch") from None
        try:
            validate_execution_contract(contract)
        except ValueError:
            raise ValueError("contract_digest_mismatch") from None
        if canonical_json(contract) != approval["payload"]:
            raise ValueError("contract_digest_mismatch")
        matching_specs = [
            spec
            for spec in job_specs
            if isinstance(spec, dict)
            and spec.get("role") == job["execution_contract_role"]
        ]
        if len(matching_specs) != 1:
            raise ValueError("contract_digest_mismatch")
        spec = matching_specs[0]
        if (
            spec.get("command_sha256") != job["approved_command_sha256"]
            or spec.get("type") != job["type"]
        ):
            raise ValueError("contract_digest_mismatch")

    def create_execution_attempt(
        self,
        *,
        job_id: int,
        backend: str,
        server_config_revision_id: str,
        leader_owner_id: str,
        scheduler_fencing_epoch: int,
        lease_expires_at: Optional[str] = None,
        node_id: Optional[str] = None,
        node_attempt_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        fencing_token: Optional[str] = None,
        lease_name: str = "execution-attempt-v1",
    ) -> dict[str, Any]:
        """Atomically validate ownership and create one generic attempt."""

        if backend not in {"ssh", "node"}:
            raise ValueError(f"invalid execution backend: {backend}")
        if backend == "node" and (
            lease_expires_at is None
            or node_id is None
            or node_attempt_id is None
        ):
            raise ValueError(
                "node execution attempt requires lease, node and protocol row ids"
            )
        if backend == "ssh" and (node_id is not None or node_attempt_id is not None):
            raise ValueError("ssh execution attempt cannot own a Node protocol row")
        initial_state = "dispatching" if backend == "ssh" else "leased"
        attempt_id = attempt_id or str(uuid.uuid4())
        fencing_token = fencing_token or str(uuid.uuid4())
        created_at = now_iso()

        with self._immediate_cursor() as cur:
            self._require_live_scheduler_lease(
                cur,
                lease_name=lease_name,
                owner_id=leader_owner_id,
                fencing_epoch=scheduler_fencing_epoch,
            )
            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            job = cur.fetchone()
            if job is None:
                raise ValueError(f"job {job_id} not found")
            if job["status"] != "queued":
                raise ValueError("job is not queued")
            if job["execution_approval_id"] is None:
                raise ValueError("approval_missing")
            cur.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (job["execution_approval_id"],),
            )
            approval = cur.fetchone()
            if approval is None:
                raise ValueError("approval_missing")
            self._validate_pinned_job_contract(job=job, approval=approval)

            cur.execute(
                """
                SELECT revision.*, approval.status AS creator_approval_status,
                       approval.payload_contract_version AS creator_contract_version
                FROM server_config_revisions AS revision
                LEFT JOIN approvals AS approval
                  ON approval.id = revision.created_by_approval_id
                WHERE revision.id = ?
                """,
                (server_config_revision_id,),
            )
            revision = cur.fetchone()
            if revision is None:
                raise ValueError("target_revision_missing")
            if (
                revision["publication_state"] != "active"
                or revision["assignment_eligibility"] != "approved"
                or revision["creator_approval_status"] != "approved"
                or revision["creator_contract_version"] != "server-config-v1"
            ):
                raise ValueError("target_revision_missing")
            try:
                normalized_target = json.loads(revision["normalized_target_json"])
            except (json.JSONDecodeError, TypeError):
                raise ValueError("target_identity_mismatch") from None
            if normalized_target.get("backend") != backend:
                raise ValueError("target_identity_mismatch")
            if job["pin_server"] is not None and job["pin_server"] != revision["server_name"]:
                raise ValueError("target_identity_mismatch")
            cur.execute(
                """
                SELECT 1 FROM server_config_mutations
                WHERE server_name = ?
                  AND state IN ('intent', 'yaml_applied', 'recovery_hold')
                """,
                (revision["server_name"],),
            )
            if cur.fetchone() is not None:
                raise ValueError("target_revision_missing")
            if backend == "node":
                cur.execute(
                    """
                    SELECT 1 FROM nodes
                    WHERE id = ? AND server_name = ?
                      AND status = 'enrolled' AND revoked_at IS NULL
                    """,
                    (node_id, revision["server_name"]),
                )
                if cur.fetchone() is None:
                    raise ValueError("target_identity_mismatch")

            current_time = self._sqlite_now(cur)
            cur.execute(
                """
                SELECT 1 FROM node_attempts
                WHERE job_id = ? AND execution_attempt_id IS NULL
                  AND (
                      (status = 'leased' AND lease_expires_at > ?)
                      OR status IN ('acked', 'running')
                  )
                LIMIT 1
                """,
                (job_id, current_time),
            )
            if cur.fetchone() is not None:
                raise ValueError("claim_conflict")

            cur.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM execution_attempts
                WHERE job_id = ?
                """,
                (job_id,),
            )
            attempt_number = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO execution_attempts
                    (id, job_id, attempt_number, backend, server_name,
                     server_config_revision_id, target_identity_sha256,
                     execution_approval_id, approved_payload_sha256,
                     execution_contract_version, state, liveness,
                     fencing_token, scheduler_fencing_epoch, created_at,
                     lease_expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'known', ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    attempt_number,
                    backend,
                    revision["server_name"],
                    server_config_revision_id,
                    revision["target_identity_sha256"],
                    approval["id"],
                    approval["payload_sha256"],
                    approval["payload_contract_version"],
                    initial_state,
                    fencing_token,
                    scheduler_fencing_epoch,
                    created_at,
                    lease_expires_at,
                ),
            )
            if backend == "node":
                cur.execute(
                    """
                    INSERT INTO node_attempts
                        (id, job_id, node_id, status, command_sha256,
                         lease_expires_at, created_at, execution_attempt_id)
                    VALUES (?, ?, ?, 'leased', ?, ?, ?, ?)
                    """,
                    (
                        node_attempt_id,
                        job_id,
                        node_id,
                        job["approved_command_sha256"],
                        lease_expires_at,
                        created_at,
                        attempt_id,
                    ),
                )
            if backend == "ssh":
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', server = ?, started_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (revision["server_name"], created_at, job_id),
                )
                if cur.rowcount != 1:
                    raise ValueError("claim_conflict")
            self._append_execution_event(
                cur,
                attempt_id=attempt_id,
                event_type="attempt_created",
                from_state=None,
                to_state=initial_state,
                from_liveness=None,
                to_liveness="known",
                reason_code="contract_validated",
                evidence={
                    "approval_id": approval["id"],
                    "payload_sha256": approval["payload_sha256"],
                    "server_config_revision_id": server_config_revision_id,
                    "target_identity_sha256": revision["target_identity_sha256"],
                    "scheduler_fencing_epoch": scheduler_fencing_epoch,
                },
                created_at=created_at,
            )
            cur.execute("SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,))
            return dict(cur.fetchone())

    def transition_execution_attempt(
        self,
        *,
        attempt_id: str,
        expected_state: str,
        expected_liveness: str,
        leader_owner_id: str,
        scheduler_fencing_epoch: int,
        reason_code: str,
        evidence: dict[str, Any],
        new_state: Optional[str] = None,
        new_liveness: Optional[str] = None,
        exit_code: Optional[int] = None,
        recovery_hold_reason: Optional[str] = None,
        lease_name: str = "execution-attempt-v1",
    ) -> dict[str, Any]:
        """CAS state/liveness and append evidence in the same transaction."""

        self._require_reason_code(reason_code)
        target_state = new_state or expected_state
        target_liveness = new_liveness or expected_liveness
        if target_liveness not in {"known", "unknown"}:
            raise ValueError("invalid attempt liveness")
        if new_state is not None and target_state not in EXECUTION_ATTEMPT_TRANSITIONS.get(
            expected_state, frozenset()
        ):
            raise ValueError("invalid execution attempt transition")
        if new_state is None and new_liveness is None and recovery_hold_reason is None:
            raise ValueError("attempt transition has no changes")
        if recovery_hold_reason is not None and recovery_hold_reason not in {
            "security_credential_revoked",
            "manual_recovery_review",
        }:
            raise ValueError("invalid recovery hold reason")
        if recovery_hold_reason is not None and target_liveness != "unknown":
            raise ValueError("recovery hold requires unknown liveness")
        if reason_code == "security_credential_revoked" and (
            target_liveness != "unknown"
            or recovery_hold_reason != "security_credential_revoked"
        ):
            raise ValueError("credential revocation must set its recovery hold")
        if (
            new_state is not None
            and target_state in EXECUTION_ATTEMPT_ACTIVE_STATES
            and reason_code != "remote_state_observed"
        ):
            raise ValueError("active attempt transition requires matching remote evidence")
        if (
            expected_liveness == "unknown"
            and target_liveness == "known"
            and reason_code
            not in {
                "remote_state_observed",
                # WP-2C: controller arbitration resolves an unknown attempt by
                # *winning the remote claim*, which is positive remote evidence
                # and is the only way this reason code can be reached for an
                # already-unknown attempt.  Requiring `remote_state_observed`
                # here would contradict the abandon guard below, which demands
                # exactly `pre_effect_definite_failure` for that same
                # transition.
                "pre_effect_definite_failure",
            }
        ):
            raise ValueError("known liveness requires matching remote evidence")
        if (
            expected_liveness == "known"
            and target_liveness == "unknown"
            and reason_code
            not in {
                "remote_unreachable",
                # WP-2C: a lost launch response is an uncertainty in its own
                # right.  The host may be perfectly reachable; what is unknown
                # is whether the effect took hold, which is exactly the state
                # DG-AMBIGUOUS-LAUNCH-v1 requires to be representable.
                "effect_outcome_unknown",
                "security_credential_revoked",
                "manual_recovery_hold",
            }
        ):
            raise ValueError("unknown liveness requires an uncertainty reason")

        terminal_at: Optional[str] = None
        if target_state in {"done", "failed"}:
            if reason_code != "terminal_evidence_valid":
                raise ValueError("terminal workload state requires terminal evidence")
            if target_state == "done" and exit_code != 0:
                raise ValueError("done attempt requires exit_code=0")
            if target_state == "failed" and (exit_code is None or exit_code == 0):
                raise ValueError("failed attempt requires non-zero exit_code")
            terminal_at = now_iso()
        elif target_state in {"expired", "abandoned_before_launch"}:
            if reason_code != "pre_effect_definite_failure":
                raise ValueError("pre-launch terminal state requires positive evidence")
            if exit_code is not None:
                raise ValueError("pre-launch terminal state cannot have exit_code")
            terminal_at = now_iso()
        elif exit_code is not None:
            raise ValueError("non-terminal attempt cannot have exit_code")

        with self._immediate_cursor() as cur:
            self._require_live_scheduler_lease(
                cur,
                lease_name=lease_name,
                owner_id=leader_owner_id,
                fencing_epoch=scheduler_fencing_epoch,
            )
            cur.execute("SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,))
            attempt = cur.fetchone()
            if attempt is None:
                raise ValueError("execution attempt not found")
            if (
                attempt["state"] != expected_state
                or attempt["liveness"] != expected_liveness
            ):
                raise ValueError("claim_conflict")
            if target_state in {"expired", "abandoned_before_launch"}:
                # WP-1A refused to abandon once *any* operation had started an
                # effect.  WP-2C narrows that to what can actually start a
                # workload: only a `launch` effect makes non-launch unprovable.
                # A `prepare` that began and failed (SSH refused before the
                # launcher was ever invoked) is precisely the definite
                # pre-launch failure INV-STATE-2 permits to requeue.
                #
                # A launch whose transmission was proven not to happen — either
                # transport evidence or a controller-won remote claim, recorded
                # as `transmission_state='not_transmitted'` — is equally safe.
                # Everything else still fails closed.
                cur.execute(
                    """
                    SELECT 1 FROM execution_operations
                    WHERE attempt_id = ?
                      AND operation = 'launch'
                      AND effect_started_at IS NOT NULL
                      AND (transmission_state IS NULL
                           OR transmission_state <> 'not_transmitted')
                    LIMIT 1
                    """,
                    (attempt_id,),
                )
                if attempt["acknowledged_at"] is not None or cur.fetchone() is not None:
                    raise ValueError("effect_outcome_unknown")

            cur.execute(
                """
                UPDATE execution_attempts
                SET state = ?, liveness = ?, recovery_hold_reason = ?,
                    terminal_at = COALESCE(?, terminal_at),
                    exit_code = CASE WHEN ? IS NULL THEN exit_code ELSE ? END,
                    last_observed_at = ?
                WHERE id = ? AND state = ? AND liveness = ?
                """,
                (
                    target_state,
                    target_liveness,
                    recovery_hold_reason,
                    terminal_at,
                    exit_code,
                    exit_code,
                    now_iso(),
                    attempt_id,
                    expected_state,
                    expected_liveness,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")

            if target_state in {"done", "failed"}:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = ?, finished_at = ?, exit_code = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (target_state, terminal_at, exit_code, attempt["job_id"]),
                )
                if cur.rowcount != 1:
                    raise ValueError("claim_conflict")
            elif target_state in {"expired", "abandoned_before_launch"}:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued', server = NULL, started_at = NULL
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (attempt["job_id"],),
                )
                if cur.rowcount != 1:
                    raise ValueError("claim_conflict")

            self._append_execution_event(
                cur,
                attempt_id=attempt_id,
                event_type="attempt_transition",
                from_state=expected_state,
                to_state=target_state,
                from_liveness=expected_liveness,
                to_liveness=target_liveness,
                reason_code=reason_code,
                evidence=evidence,
            )
            cur.execute("SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,))
            return dict(cur.fetchone())

    def record_launch_not_transmitted(self, *, attempt_id: str) -> bool:
        """Persist controller-arbitration evidence on the launch operation.

        Only reachable after the controller won the remote claim, which proves
        the launcher can never start the workload. This is the evidence the
        abandon guard requires before an attempt may be requeued.
        """

        with self._immediate_cursor() as cur:
            cur.execute(
                """
                UPDATE execution_operations
                SET transmission_state = 'not_transmitted', updated_at = ?
                WHERE attempt_id = ? AND operation = 'launch'
                """,
                (now_iso(), attempt_id),
            )
            return cur.rowcount >= 1

    def get_execution_attempt(self, attempt_id: str) -> Optional[dict[str, Any]]:
        """Read one attempt row. Read-only: no lease and no ownership needed."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,)
            )
            return self._row_dict(cur.fetchone())

    def requeue_job_after_abandoned_attempt(self, *, attempt_id: str) -> bool:
        """WP-2C: return a Job to `queued` after a *proven* non-launch.

        The guard is the whole point of `INV-STATE-2`'s revised text: this is
        reachable only from `abandoned_before_launch`, which in turn is
        reachable only from a definite pre-launch failure or a controller-won
        remote claim. An ambiguous attempt can never take this path, so no
        second dispatch of a possibly-running workload can originate here.
        """

        with self._immediate_cursor() as cur:
            cur.execute(
                "SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,)
            )
            attempt = cur.fetchone()
            if attempt is None:
                raise ValueError("execution attempt not found")
            if attempt["state"] != "abandoned_before_launch":
                raise ValueError(
                    "requeue requires a proven abandoned_before_launch attempt"
                )
            if attempt["liveness"] != "known":
                raise ValueError("requeue requires known liveness")
            cur.execute(
                """
                UPDATE jobs SET status = 'queued', server = NULL, started_at = NULL
                WHERE id = ? AND status = 'running'
                """,
                (attempt["job_id"],),
            )
            requeued = cur.rowcount == 1
            self._append_execution_event(
                cur,
                attempt_id=attempt_id,
                event_type="job_requeued_after_abandon",
                reason_code="pre_effect_definite_failure",
                evidence={"job_id": attempt["job_id"], "applied": requeued},
            )
            return requeued

    def apply_attempt_resolution_to_job(
        self,
        *,
        attempt_id: str,
        job_status: str,
        exit_code: Optional[int] = None,
    ) -> bool:
        """WP-2C: project a resolved attempt onto the canonical Job.

        `done`/`failed` require the attempt to already hold that terminal state
        from exit-code sentinel evidence, and `queued` requires the attempt to
        have failed with proof the host rebooted. The projection can never
        invent a terminal status the attempt itself does not carry.
        """

        if job_status not in {"done", "failed", "queued"}:
            raise ValueError(f"invalid job projection: {job_status}")

        with self._immediate_cursor() as cur:
            cur.execute(
                "SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,)
            )
            attempt = cur.fetchone()
            if attempt is None:
                raise ValueError("execution attempt not found")
            if job_status in {"done", "failed"} and attempt["state"] != job_status:
                raise ValueError("job projection must match attempt terminal state")
            if job_status == "queued" and attempt["state"] != "failed":
                raise ValueError("requeue projection requires a failed attempt")

            if job_status == "queued":
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued', server = NULL, started_at = NULL
                    WHERE id = ? AND status = 'running'
                    """,
                    (attempt["job_id"],),
                )
            else:
                cur.execute(
                    """
                    UPDATE jobs SET status = ?, finished_at = ?, exit_code = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        job_status,
                        self._sqlite_now(cur),
                        exit_code,
                        attempt["job_id"],
                    ),
                )
            applied = cur.rowcount == 1
            self._append_execution_event(
                cur,
                attempt_id=attempt_id,
                event_type="job_projection",
                reason_code=(
                    "terminal_evidence_valid"
                    if job_status in {"done", "failed"}
                    else "remote_state_observed"
                ),
                evidence={
                    "job_id": attempt["job_id"],
                    "job_status": job_status,
                    "applied": applied,
                },
            )
            return applied

    @staticmethod
    def _validate_execution_operation_authorization(
        cur: sqlite3.Cursor,
        *,
        attempt: sqlite3.Row,
        operation: str,
        authorization_approval_id: Optional[int],
        authorized_contract_sha256: Optional[str],
    ) -> tuple[str, Optional[sqlite3.Row]]:
        authorization_class = {
            "prepare": "execution",
            "launch": "execution",
            "inspect": "inspect",
            "stop": "stop",
            "collect": "execution",
            "cleanup": "cleanup",
        }.get(operation)
        if authorization_class is None:
            raise ValueError(f"invalid execution operation: {operation}")
        if operation == "inspect":
            if (
                authorization_approval_id is not None
                or authorized_contract_sha256 is not None
            ):
                raise ValueError("inspect must not carry mutation approval")
            return authorization_class, None
        if (
            authorization_approval_id is None
            or authorized_contract_sha256 is None
        ):
            raise ValueError("material operation requires approval authorization")

        cur.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (authorization_approval_id,),
        )
        approval = cur.fetchone()
        if approval is None:
            raise ValueError("approval_missing")
        if approval["status"] != "approved":
            raise ValueError("approval_not_approved")
        if approval["payload_sha256"] != authorized_contract_sha256:
            raise ValueError("contract_digest_mismatch")

        if authorization_class == "execution":
            if (
                approval["id"] != attempt["execution_approval_id"]
                or approval["payload_sha256"]
                != attempt["approved_payload_sha256"]
                or approval["payload_contract_version"]
                != attempt["execution_contract_version"]
                or PINNED_EXECUTION_CONTRACTS.get(approval["kind"])
                != approval["payload_contract_version"]
            ):
                raise ValueError("approval_kind_mismatch")
            try:
                contract = json.loads(approval["payload"])
            except (json.JSONDecodeError, TypeError):
                raise ValueError("contract_digest_mismatch") from None
            authorized_operations = contract.get("authorized_operations", [])
            if operation not in authorized_operations:
                raise ValueError("approval_kind_mismatch")
        elif authorization_class == "stop":
            if (
                approval["kind"] != "stop"
                or approval["payload_contract_version"] != "stop-intent-v1"
            ):
                raise ValueError("approval_kind_mismatch")
            try:
                payload = json.loads(approval["payload"])
            except (json.JSONDecodeError, TypeError):
                raise ValueError("contract_digest_mismatch") from None
            if (
                payload.get("attempt_id") != attempt["id"]
                or payload.get("job_id") != attempt["job_id"]
            ):
                raise ValueError("contract_digest_mismatch")
        else:
            if (
                approval["kind"] != "attempt_cleanup"
                or approval["payload_contract_version"] != "attempt-cleanup-v1"
            ):
                raise ValueError("approval_kind_mismatch")
        return authorization_class, approval

    def insert_execution_operation(
        self,
        *,
        attempt_id: str,
        operation: str,
        payload: dict[str, Any],
        leader_owner_id: str,
        scheduler_fencing_epoch: int,
        authorization_approval_id: Optional[int] = None,
        authorized_contract_sha256: Optional[str] = None,
        operation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        lease_name: str = "execution-attempt-v1",
    ) -> dict[str, Any]:
        """Persist an authorized outbox intent before any remote interaction."""

        if operation == "inspect":
            # WP-1A failed this closed because no read-only command allowlist
            # was pinned yet.  WP-2B pinned it, so an inspect operation is now
            # accepted only when its payload names one of those exact builders.
            # Anything else still fails closed: an inspect operation carries no
            # mutation approval, so an unrecognized payload must never reach a
            # worker.
            command_kind = payload.get("command_kind") if isinstance(payload, dict) else None
            if command_kind not in EXECUTION_INSPECT_COMMAND_KINDS:
                raise ValueError("inspect command is not in the pinned allowlist")
        operation_id = operation_id or str(uuid.uuid4())
        idempotency_key = idempotency_key or str(uuid.uuid4())
        payload_json = canonical_json(payload)
        payload_sha256 = utf8_sha256(payload_json)
        created_at = now_iso()
        with self._immediate_cursor() as cur:
            self._require_live_scheduler_lease(
                cur,
                lease_name=lease_name,
                owner_id=leader_owner_id,
                fencing_epoch=scheduler_fencing_epoch,
            )
            cur.execute("SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,))
            attempt = cur.fetchone()
            if attempt is None:
                raise ValueError("execution attempt not found")
            authorization_class, _ = self._validate_execution_operation_authorization(
                cur,
                attempt=attempt,
                operation=operation,
                authorization_approval_id=authorization_approval_id,
                authorized_contract_sha256=authorized_contract_sha256,
            )
            cur.execute(
                """
                INSERT INTO execution_operations
                    (id, attempt_id, operation, idempotency_key,
                     authorization_approval_id, authorization_class,
                     authorized_contract_sha256, payload_json, payload_sha256,
                     state, attempt_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    operation_id,
                    attempt_id,
                    operation,
                    idempotency_key,
                    authorization_approval_id,
                    authorization_class,
                    authorized_contract_sha256,
                    payload_json,
                    payload_sha256,
                    created_at,
                    created_at,
                ),
            )
            self._append_execution_event(
                cur,
                attempt_id=attempt_id,
                operation_id=operation_id,
                event_type="operation_created",
                reason_code="contract_validated",
                evidence={
                    "operation": operation,
                    "authorization_class": authorization_class,
                    "payload_sha256": payload_sha256,
                },
                created_at=created_at,
            )
            cur.execute(
                "SELECT * FROM execution_operations WHERE id = ?",
                (operation_id,),
            )
            return dict(cur.fetchone())

    def claim_execution_operation(
        self,
        *,
        operation_id: str,
        claim_owner: str,
        claim_seconds: int,
        leader_owner_id: str,
        scheduler_fencing_epoch: int,
        lease_name: str = "execution-attempt-v1",
    ) -> Optional[dict[str, Any]]:
        """Claim pending work, or recover a definitely pre-effect expired claim."""

        if not claim_owner:
            raise ValueError("operation claim_owner must not be blank")
        if isinstance(claim_seconds, bool) or claim_seconds < 1:
            raise ValueError("operation claim_seconds must be positive")
        with self._immediate_cursor() as cur:
            self._require_live_scheduler_lease(
                cur,
                lease_name=lease_name,
                owner_id=leader_owner_id,
                fencing_epoch=scheduler_fencing_epoch,
            )
            current_time = self._sqlite_now(cur)
            claim_expires_at = self._sqlite_after(cur, claim_seconds)
            cur.execute(
                """
                SELECT operation.*
                FROM execution_operations AS operation
                JOIN execution_attempts AS attempt
                  ON attempt.id = operation.attempt_id
                WHERE operation.id = ?
                """,
                (operation_id,),
            )
            operation_row = cur.fetchone()
            if operation_row is None:
                raise ValueError("execution operation not found")
            cur.execute(
                "SELECT * FROM execution_attempts WHERE id = ?",
                (operation_row["attempt_id"],),
            )
            attempt = cur.fetchone()
            self._validate_execution_operation_authorization(
                cur,
                attempt=attempt,
                operation=operation_row["operation"],
                authorization_approval_id=operation_row[
                    "authorization_approval_id"
                ],
                authorized_contract_sha256=operation_row[
                    "authorized_contract_sha256"
                ],
            )

            if operation_row["state"] == "processing":
                if (
                    operation_row["claim_expires_at"] is None
                    or operation_row["claim_expires_at"] > current_time
                ):
                    return None
                if operation_row["effect_started_at"] is not None:
                    updated_at = now_iso()
                    cur.execute(
                        """
                        UPDATE execution_operations
                        SET state = 'uncertain', updated_at = ?,
                            last_error_category = 'effect_outcome_unknown'
                        WHERE id = ? AND state = 'processing'
                          AND effect_started_at IS NOT NULL
                          AND claim_expires_at <= ?
                        """,
                        (updated_at, operation_id, current_time),
                    )
                    if cur.rowcount == 1:
                        self._append_execution_event(
                            cur,
                            attempt_id=operation_row["attempt_id"],
                            operation_id=operation_id,
                            event_type="operation_transition",
                            from_state="processing",
                            to_state="uncertain",
                            reason_code="effect_outcome_unknown",
                            evidence={"claim_expired_at": operation_row["claim_expires_at"]},
                            created_at=updated_at,
                        )
                    return None
            elif operation_row["state"] != "pending":
                return None
            if (
                operation_row["retry_at"] is not None
                and operation_row["retry_at"] > current_time
            ):
                return None

            expected_state = operation_row["state"]
            updated_at = now_iso()
            cur.execute(
                """
                UPDATE execution_operations
                SET state = 'processing', claim_owner = ?,
                    claim_fencing_epoch = ?, claim_expires_at = ?,
                    attempt_count = attempt_count + 1, updated_at = ?,
                    last_error_category = NULL,
                    sanitized_error_detail = NULL
                WHERE id = ? AND state = ?
                  AND (
                    state = 'pending'
                    OR (
                        state = 'processing'
                        AND effect_started_at IS NULL
                        AND claim_expires_at <= ?
                    )
                  )
                """,
                (
                    claim_owner,
                    scheduler_fencing_epoch,
                    claim_expires_at,
                    updated_at,
                    operation_id,
                    expected_state,
                    current_time,
                ),
            )
            if cur.rowcount != 1:
                return None
            cur.execute(
                "SELECT * FROM execution_operations WHERE id = ?",
                (operation_id,),
            )
            return dict(cur.fetchone())

    def mark_execution_operation_effect_started(
        self,
        *,
        operation_id: str,
        claim_owner: str,
        leader_owner_id: str,
        scheduler_fencing_epoch: int,
        lease_name: str = "execution-attempt-v1",
    ) -> bool:
        """Persist the crash boundary immediately before a material call."""

        with self._immediate_cursor() as cur:
            self._require_live_scheduler_lease(
                cur,
                lease_name=lease_name,
                owner_id=leader_owner_id,
                fencing_epoch=scheduler_fencing_epoch,
            )
            current_time = self._sqlite_now(cur)
            cur.execute(
                """
                UPDATE execution_operations
                SET effect_started_at = ?, updated_at = ?
                WHERE id = ? AND state = 'processing'
                  AND claim_owner = ? AND claim_fencing_epoch = ?
                  AND claim_expires_at > ?
                  AND effect_started_at IS NULL
                """,
                (
                    now_iso(),
                    now_iso(),
                    operation_id,
                    claim_owner,
                    scheduler_fencing_epoch,
                    current_time,
                ),
            )
            return cur.rowcount == 1

    def transition_execution_operation(
        self,
        *,
        operation_id: str,
        expected_state: str,
        new_state: str,
        reason_code: str,
        evidence: dict[str, Any],
        leader_owner_id: str,
        scheduler_fencing_epoch: int,
        claim_owner: Optional[str] = None,
        retry_at: Optional[str] = None,
        last_error_category: Optional[str] = None,
        sanitized_error_detail: Optional[str] = None,
        transmission_state: Optional[str] = None,
        lease_name: str = "execution-attempt-v1",
    ) -> dict[str, Any]:
        """CAS an outbox result; uncertain never transitions back to pending."""

        self._require_reason_code(reason_code)
        if new_state not in EXECUTION_OPERATION_TRANSITIONS.get(
            expected_state, frozenset()
        ):
            raise ValueError("invalid execution operation transition")
        if new_state == "pending" and retry_at is None:
            raise ValueError("pending retry requires retry_at")
        with self._immediate_cursor() as cur:
            self._require_live_scheduler_lease(
                cur,
                lease_name=lease_name,
                owner_id=leader_owner_id,
                fencing_epoch=scheduler_fencing_epoch,
            )
            cur.execute(
                """
                SELECT operation.*
                FROM execution_operations AS operation
                JOIN execution_attempts AS attempt
                  ON attempt.id = operation.attempt_id
                WHERE operation.id = ?
                """,
                (operation_id,),
            )
            operation_row = cur.fetchone()
            if operation_row is None:
                raise ValueError("execution operation not found")
            if (
                operation_row["state"] != expected_state
            ):
                raise ValueError("claim_conflict")
            if (
                expected_state == "processing"
                and claim_owner is not None
                and operation_row["claim_owner"] != claim_owner
            ):
                raise ValueError("claim_conflict")
            if new_state == "pending" and operation_row["effect_started_at"] is not None:
                raise ValueError("effect_outcome_unknown")
            if (
                new_state == "uncertain"
                and operation_row["effect_started_at"] is None
            ):
                raise ValueError("pre-effect failure cannot be uncertain")
            if (
                new_state == "delivered"
                and operation_row["operation"] != "inspect"
                and operation_row["effect_started_at"] is None
            ):
                raise ValueError("material delivery requires effect boundary")

            updated_at = now_iso()
            cur.execute(
                """
                UPDATE execution_operations
                SET state = ?, retry_at = ?, updated_at = ?,
                    last_error_category = ?, sanitized_error_detail = ?,
                    transmission_state = COALESCE(?, transmission_state),
                    claim_owner = CASE WHEN ? = 'processing' THEN claim_owner ELSE NULL END,
                    claim_fencing_epoch =
                        CASE WHEN ? = 'processing' THEN claim_fencing_epoch ELSE NULL END,
                    claim_expires_at =
                        CASE WHEN ? = 'processing' THEN claim_expires_at ELSE NULL END
                WHERE id = ? AND state = ?
                """,
                (
                    new_state,
                    retry_at,
                    updated_at,
                    last_error_category,
                    sanitized_error_detail,
                    transmission_state,
                    new_state,
                    new_state,
                    new_state,
                    operation_id,
                    expected_state,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("claim_conflict")
            self._append_execution_event(
                cur,
                attempt_id=operation_row["attempt_id"],
                operation_id=operation_id,
                event_type="operation_transition",
                from_state=expected_state,
                to_state=new_state,
                reason_code=reason_code,
                evidence=evidence,
                created_at=updated_at,
            )
            cur.execute(
                "SELECT * FROM execution_operations WHERE id = ?",
                (operation_id,),
            )
            return dict(cur.fetchone())

    # ---- actor identity CRUD (Goal 1 / Slice 1) --------------------------

    def insert_actor(
        self,
        *,
        actor_type: ActorType | str,
        display_name: str,
        actor_id: Optional[str] = None,
        email: Optional[str] = None,
        platform_admin: bool = False,
    ) -> Actor:
        actor_type = ActorType(actor_type)
        if not display_name or not display_name.strip():
            raise ValueError("actor display_name must not be blank")
        actor_id = actor_id or str(uuid.uuid4())
        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO actors
                    (id, actor_type, display_name, email, platform_admin,
                     disabled_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    actor_id,
                    actor_type.value,
                    display_name.strip(),
                    email,
                    int(platform_admin),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_actor(actor_id)

    def get_actor(self, actor_id: str) -> Optional[Actor]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM actors WHERE id = ?", (actor_id,))
            row = cur.fetchone()
        return self._actor_from_row(row) if row else None

    def list_actors(self, actor_type: ActorType | str | None = None) -> list[Actor]:
        query = "SELECT * FROM actors"
        params: list[Any] = []
        if actor_type is not None:
            query += " WHERE actor_type = ?"
            params.append(ActorType(actor_type).value)
        query += " ORDER BY created_at, id"
        with self.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [self._actor_from_row(row) for row in rows]

    def update_actor(self, actor_id: str, **fields: Any) -> None:
        allowed = {"display_name", "email", "platform_admin", "disabled_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported actor fields: {sorted(unknown)}")
        if not fields:
            return
        if "display_name" in fields and (not fields["display_name"] or not fields["display_name"].strip()):
            raise ValueError("actor display_name must not be blank")
        if "platform_admin" in fields:
            fields["platform_admin"] = int(bool(fields["platform_admin"]))
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{name} = ?" for name in fields)
        with self.cursor() as cur:
            cur.execute(
                f"UPDATE actors SET {cols} WHERE id = ?",
                [*fields.values(), actor_id],
            )
            if cur.rowcount != 1:
                raise ValueError(f"actor {actor_id} not found")

    @staticmethod
    def _actor_from_row(row: sqlite3.Row) -> Actor:
        return Actor(
            id=row["id"],
            actor_type=row["actor_type"],
            display_name=row["display_name"],
            email=row["email"],
            platform_admin=bool(row["platform_admin"]),
            disabled_at=row["disabled_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def insert_oidc_identity(
        self,
        *,
        actor_id: str,
        issuer: str,
        subject: str,
        identity_id: Optional[str] = None,
        email: Optional[str] = None,
    ) -> OIDCIdentity:
        if self.get_actor(actor_id) is None:
            raise ValueError(f"actor {actor_id} not found")
        if not issuer or not subject:
            raise ValueError("OIDC issuer and subject must not be blank")
        identity_id = identity_id or str(uuid.uuid4())
        created_at = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oidc_identities
                    (id, actor_id, issuer, subject, email, created_at,
                     last_authenticated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (identity_id, actor_id, issuer, subject, email, created_at),
            )
        return self.get_oidc_identity(identity_id)

    def get_oidc_identity(self, identity_id: str) -> Optional[OIDCIdentity]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM oidc_identities WHERE id = ?", (identity_id,))
            row = cur.fetchone()
        return self._oidc_identity_from_row(row) if row else None

    def get_oidc_identity_by_subject(
        self, issuer: str, subject: str
    ) -> Optional[OIDCIdentity]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM oidc_identities WHERE issuer = ? AND subject = ?",
                (issuer, subject),
            )
            row = cur.fetchone()
        return self._oidc_identity_from_row(row) if row else None

    def touch_oidc_identity(self, identity_id: str, email: Optional[str] = None) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE oidc_identities
                SET email = ?, last_authenticated_at = ?
                WHERE id = ?
                """,
                (email, now_iso(), identity_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"OIDC identity {identity_id} not found")

    def bind_oidc_identity(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        platform_admin_on_create: bool = False,
        authenticated_at: Optional[str] = None,
    ) -> tuple[Actor, OIDCIdentity, bool]:
        """Atomically load or create the human bound to ``(issuer, subject)``.

        OIDC identity is *only* looked up by the provider's exact issuer and
        subject pair.  Email and display name are descriptive metadata: they
        may be refreshed on an existing binding, but they are never queried to
        find, merge, or select an actor.  ``platform_admin_on_create`` applies
        only while creating a brand-new binding, so changing bootstrap config
        later cannot silently elevate an existing actor during login.

        The database lock and unique issuer/subject index make concurrent first
        logins converge on one actor and one identity without a first-login-wins
        administrator rule.  A broken or non-human existing binding is rejected
        rather than repaired or rebound.
        """

        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError("OIDC issuer must not be blank")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("OIDC subject must not be blank")
        # Issuer and subject are opaque identity keys.  Keep their exact values
        # (including case and any non-empty surrounding whitespace) so login
        # can never collapse two provider identities through normalization.
        normalized_name = (
            display_name.strip()
            if isinstance(display_name, str) and display_name.strip()
            else None
        )
        normalized_email = (
            email.strip() if isinstance(email, str) and email.strip() else None
        )
        timestamp = authenticated_at or now_iso()

        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM oidc_identities WHERE issuer = ? AND subject = ?",
                (issuer, subject),
            )
            identity_row = cur.fetchone()
            created = identity_row is None

            if identity_row is None:
                actor_id = str(uuid.uuid4())
                identity_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO actors
                        (id, actor_type, display_name, email, platform_admin,
                         disabled_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        actor_id,
                        ActorType.HUMAN.value,
                        normalized_name or "OIDC user",
                        normalized_email,
                        int(bool(platform_admin_on_create)),
                        timestamp,
                        timestamp,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO oidc_identities
                        (id, actor_id, issuer, subject, email, created_at,
                         last_authenticated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity_id,
                        actor_id,
                        issuer,
                        subject,
                        normalized_email,
                        timestamp,
                        timestamp,
                    ),
                )
                cur.execute("SELECT * FROM actors WHERE id = ?", (actor_id,))
                actor_row = cur.fetchone()
                cur.execute(
                    "SELECT * FROM oidc_identities WHERE id = ?", (identity_id,)
                )
                identity_row = cur.fetchone()
            else:
                cur.execute(
                    "SELECT * FROM actors WHERE id = ?", (identity_row["actor_id"],)
                )
                actor_row = cur.fetchone()
                if actor_row is None:
                    raise ValueError("OIDC identity references a missing actor")
                if actor_row["actor_type"] != ActorType.HUMAN.value:
                    raise ValueError("OIDC identity must reference a human actor")
                if actor_row["disabled_at"] is not None:
                    raise PermissionError("OIDC actor is disabled")

                cur.execute(
                    """
                    UPDATE oidc_identities
                    SET email = COALESCE(?, email), last_authenticated_at = ?
                    WHERE id = ?
                    """,
                    (normalized_email, timestamp, identity_row["id"]),
                )
                cur.execute(
                    """
                    UPDATE actors
                    SET display_name = COALESCE(?, display_name),
                        email = COALESCE(?, email), updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_name,
                        normalized_email,
                        timestamp,
                        identity_row["actor_id"],
                    ),
                )
                cur.execute(
                    "SELECT * FROM actors WHERE id = ?", (identity_row["actor_id"],)
                )
                actor_row = cur.fetchone()
                cur.execute(
                    "SELECT * FROM oidc_identities WHERE id = ?", (identity_row["id"],)
                )
                identity_row = cur.fetchone()

        # Both rows were selected inside the same serialized transaction.  The
        # small domain projections below contain no credential material.
        actor = self._actor_from_row(actor_row)
        identity = self._oidc_identity_from_row(identity_row)
        return actor, identity, created

    @staticmethod
    def _oidc_identity_from_row(row: sqlite3.Row) -> OIDCIdentity:
        return OIDCIdentity(
            id=row["id"],
            actor_id=row["actor_id"],
            issuer=row["issuer"],
            subject=row["subject"],
            email=row["email"],
            created_at=row["created_at"],
            last_authenticated_at=row["last_authenticated_at"],
        )

    def insert_oidc_login_flow(
        self,
        *,
        state_hash: str,
        nonce_hash: str,
        pkce_verifier: str,
        expires_at: str,
        return_to: Optional[str] = None,
    ) -> OIDCLoginFlow:
        if not state_hash or not nonce_hash or not pkce_verifier:
            raise ValueError("OIDC flow secrets must not be blank")
        created_at = now_iso()
        with self.cursor() as cur:
            # Opportunistic bounded-retention cleanup on every public login.
            # Active unconsumed flows remain available across restarts; expired
            # rows (which may still contain a verifier) and already-consumed
            # tombstones no longer accumulate indefinitely.
            cur.execute(
                "DELETE FROM oidc_login_flows "
                "WHERE expires_at <= ? OR consumed_at IS NOT NULL",
                (created_at,),
            )
            cur.execute(
                """
                INSERT INTO oidc_login_flows
                    (state_hash, nonce_hash, pkce_verifier, return_to,
                     created_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (state_hash, nonce_hash, pkce_verifier, return_to, created_at, expires_at),
            )
        return self.get_oidc_login_flow(state_hash)

    def get_oidc_login_flow(self, state_hash: str) -> Optional[OIDCLoginFlow]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM oidc_login_flows WHERE state_hash = ?", (state_hash,))
            row = cur.fetchone()
        return self._oidc_login_flow_from_row(row) if row else None

    def consume_oidc_login_flow(
        self, state_hash: str, *, now: Optional[str] = None
    ) -> Optional[OIDCLoginFlow]:
        consumed_at = now or now_iso()
        with self.cursor() as cur:
            # A callback or replay is also a cleanup opportunity.  In
            # particular, delete expired rows before lookup so their obsolete
            # raw PKCE verifier is not retained after expiry is observed.
            cur.execute(
                "DELETE FROM oidc_login_flows "
                "WHERE expires_at <= ? OR consumed_at IS NOT NULL",
                (consumed_at,),
            )
            cur.execute(
                """
                SELECT * FROM oidc_login_flows
                WHERE state_hash = ? AND consumed_at IS NULL
                  AND expires_at > ? AND pkce_verifier IS NOT NULL
                """,
                (state_hash, consumed_at),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                UPDATE oidc_login_flows
                SET consumed_at = ?, pkce_verifier = NULL
                WHERE state_hash = ? AND consumed_at IS NULL
                """,
                (consumed_at, state_hash),
            )
            if cur.rowcount != 1:
                return None
        flow = self._oidc_login_flow_from_row(row)
        flow.consumed_at = consumed_at
        return flow

    @staticmethod
    def _oidc_login_flow_from_row(row: sqlite3.Row) -> OIDCLoginFlow:
        return OIDCLoginFlow(
            state_hash=row["state_hash"],
            nonce_hash=row["nonce_hash"],
            pkce_verifier=row["pkce_verifier"],
            return_to=row["return_to"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
        )

    def insert_actor_session(
        self,
        *,
        actor_id: str,
        secret_hash: str,
        expires_at: str,
        session_id: Optional[str] = None,
        oidc_identity_id: Optional[str] = None,
    ) -> ActorSession:
        if self.get_actor(actor_id) is None:
            raise ValueError(f"actor {actor_id} not found")
        session_id = session_id or str(uuid.uuid4())
        created_at = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO actor_sessions
                    (id, actor_id, oidc_identity_id, secret_hash, created_at,
                     expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (session_id, actor_id, oidc_identity_id, secret_hash, created_at, expires_at),
            )
        return self.get_actor_session(session_id)

    def get_actor_session(self, session_id: str) -> Optional[ActorSession]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM actor_sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return ActorSession(
            id=row["id"], actor_id=row["actor_id"], oidc_identity_id=row["oidc_identity_id"],
            secret_hash=row["secret_hash"], created_at=row["created_at"],
            expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        )

    def revoke_actor_session(self, session_id: str, *, revoked_at: Optional[str] = None) -> bool:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE actor_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (revoked_at or now_iso(), session_id),
            )
            return cur.rowcount == 1

    def insert_service_account(
        self,
        *,
        name: str,
        actor_id: str,
        description: Optional[str] = None,
        created_by_actor_id: Optional[str] = None,
    ) -> ServiceAccount:
        actor = self.get_actor(actor_id)
        if actor is None or actor.actor_type is not ActorType.SERVICE:
            raise ValueError("service account requires an existing service actor")
        if not name or not name.strip():
            raise ValueError("service account name must not be blank")
        created_at = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO service_accounts
                    (actor_id, name, description, created_by_actor_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (actor_id, name.strip(), description, created_by_actor_id, created_at),
            )
        return self.get_service_account(actor_id)

    def get_service_account(self, actor_id: str) -> Optional[ServiceAccount]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM service_accounts WHERE actor_id = ?", (actor_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return ServiceAccount(
            actor_id=row["actor_id"], name=row["name"], description=row["description"],
            created_by_actor_id=row["created_by_actor_id"], created_at=row["created_at"],
        )

    def list_service_accounts(self) -> list[ServiceAccount]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM service_accounts ORDER BY created_at, actor_id")
            rows = cur.fetchall()
        return [
            ServiceAccount(
                actor_id=row["actor_id"], name=row["name"], description=row["description"],
                created_by_actor_id=row["created_by_actor_id"], created_at=row["created_at"],
            )
            for row in rows
        ]

    def insert_service_account_token(
        self,
        *,
        token_id: str,
        service_account_actor_id: str,
        secret_hash: str,
        scopes: list[str],
        expires_at: str,
        label: Optional[str] = None,
        created_by_actor_id: Optional[str] = None,
    ) -> ServiceAccountToken:
        if self.get_service_account(service_account_actor_id) is None:
            raise ValueError(f"service account {service_account_actor_id} not found")
        if not isinstance(scopes, list) or any(
            not isinstance(scope, str) or not scope for scope in scopes
        ):
            raise ValueError("service token scopes must be a list of non-empty strings")
        created_at = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO service_account_tokens
                    (id, service_account_actor_id, label, secret_hash, scopes,
                     created_by_actor_id, created_at, expires_at, last_used_at,
                     revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    token_id, service_account_actor_id, label, secret_hash,
                    json.dumps(scopes), created_by_actor_id, created_at, expires_at,
                ),
            )
        return self.get_service_account_token(token_id)

    def get_service_account_token(self, token_id: str) -> Optional[ServiceAccountToken]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM service_account_tokens WHERE id = ?", (token_id,))
            row = cur.fetchone()
        return self._service_account_token_from_row(row) if row else None

    def list_service_account_tokens(self, actor_id: str) -> list[ServiceAccountToken]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM service_account_tokens WHERE service_account_actor_id = ?"
                " ORDER BY created_at, id",
                (actor_id,),
            )
            rows = cur.fetchall()
        return [self._service_account_token_from_row(row) for row in rows]

    def revoke_service_account_token(
        self, token_id: str, *, revoked_at: Optional[str] = None
    ) -> bool:
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE service_account_tokens SET revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (revoked_at or now_iso(), token_id),
            )
            return cur.rowcount == 1

    def touch_service_account_token(self, token_id: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE service_account_tokens SET last_used_at = ? WHERE id = ?",
                (now_iso(), token_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"service token {token_id} not found")

    @staticmethod
    def _service_account_token_from_row(row: sqlite3.Row) -> ServiceAccountToken:
        scopes = json.loads(row["scopes"] or "[]")
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise ValueError("stored service token scopes are invalid")
        return ServiceAccountToken(
            id=row["id"], service_account_actor_id=row["service_account_actor_id"],
            label=row["label"], secret_hash=row["secret_hash"], scopes=scopes,
            created_by_actor_id=row["created_by_actor_id"], created_at=row["created_at"],
            expires_at=row["expires_at"], last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
        )

    def upsert_project_membership(
        self,
        *,
        project: str,
        actor_id: str,
        role: ProjectRole | str,
        created_by_actor_id: Optional[str] = None,
    ) -> ProjectMembership:
        project_row = self.get_project(project)
        if project_row is None or project_row.id is None:
            raise ValueError(f"project {project} not found")
        if self.get_actor(actor_id) is None:
            raise ValueError(f"actor {actor_id} not found")
        role = ProjectRole(role)
        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_memberships
                    (project_id, actor_id, role, created_by_actor_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, actor_id) DO UPDATE SET
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (
                    project_row.id, actor_id, role.value, created_by_actor_id,
                    timestamp, timestamp,
                ),
            )
        return self.get_project_membership(project_row.id, actor_id)

    def get_project_membership(
        self, project_id: str, actor_id: str
    ) -> Optional[ProjectMembership]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_memberships WHERE project_id = ? AND actor_id = ?",
                (project_id, actor_id),
            )
            row = cur.fetchone()
        return self._project_membership_from_row(row) if row else None

    def list_project_memberships(
        self, *, project_id: Optional[str] = None, actor_id: Optional[str] = None
    ) -> list[ProjectMembership]:
        query = "SELECT * FROM project_memberships WHERE 1=1"
        params: list[str] = []
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        if actor_id is not None:
            query += " AND actor_id = ?"
            params.append(actor_id)
        query += " ORDER BY created_at, project_id, actor_id"
        with self.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [self._project_membership_from_row(row) for row in rows]

    def delete_project_membership(self, project_id: str, actor_id: str) -> bool:
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM project_memberships WHERE project_id = ? AND actor_id = ?",
                (project_id, actor_id),
            )
            return cur.rowcount == 1

    @staticmethod
    def _project_membership_from_row(row: sqlite3.Row) -> ProjectMembership:
        return ProjectMembership(
            project_id=row["project_id"], actor_id=row["actor_id"], role=row["role"],
            created_by_actor_id=row["created_by_actor_id"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---- jobs CRUD -----------------------------------------------------

    def insert_job(
        self,
        command: str,
        type: str = "adhoc",
        project: Optional[str] = None,
        require_tag: Optional[str] = None,
        pin_server: Optional[str] = None,
        depends_on: Optional[list[int]] = None,
        gpus_needed: Optional[int] = None,
        priority: str = "normal",
        status: str = "queued",
        source_coding_run_id: Optional[int] = None,
        engineering_task_id: Optional[str] = None,
        engineering_task_role: Optional[str] = None,
        engineering_attempt_number: Optional[int] = None,
        auto_placement_approval_id: Optional[int] = None,
    ) -> int:
        if type not in VALID_TYPES:
            raise ValueError(f"invalid job type: {type}")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"invalid priority: {priority}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        owner_values = (
            engineering_task_id,
            engineering_task_role,
            engineering_attempt_number,
        )
        if any(value is not None for value in owner_values) and not all(
            value is not None for value in owner_values
        ):
            raise ValueError("engineering task job ownership must be all-or-none")
        if engineering_task_role is not None and engineering_task_role not in {
            "staging",
            "coding",
        }:
            raise ValueError("invalid engineering task job role")
        if engineering_attempt_number is not None and engineering_attempt_number < 1:
            raise ValueError("engineering attempt number must be positive")
        depends_on = depends_on or []
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs
                    (type, project, command, require_tag, pin_server,
                     depends_on, gpus_needed, status, priority, created_at,
                     source_coding_run_id, engineering_task_id,
                     engineering_task_role, engineering_attempt_number,
                     auto_placement_approval_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    type,
                    project,
                    command,
                    require_tag,
                    pin_server,
                    json.dumps(depends_on),
                    gpus_needed,
                    status,
                    priority,
                    now_iso(),
                    source_coding_run_id,
                    engineering_task_id,
                    engineering_task_role,
                    engineering_attempt_number,
                    auto_placement_approval_id,
                ),
            )
            return cur.lastrowid

    def get_engineering_task_job(
        self, task_id: str, role: str, attempt_number: int
    ) -> Optional[Job]:
        """依 immutable owner key 反查 staging/coding job，供 approve retry。"""

        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM jobs
                WHERE engineering_task_id = ? AND engineering_task_role = ?
                  AND engineering_attempt_number = ?
                LIMIT 1
                """,
                (task_id, role, attempt_number),
            )
            row = cur.fetchone()
            return Job.from_row(row) if row else None

    def get_job(self, job_id: int) -> Optional[Job]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            return Job.from_row(row) if row else None

    def list_jobs(
        self, status: Optional[str] = None, project: Optional[str] = None
    ) -> list[Job]:
        """階段 11（PLAN.md L.3）：新增選填 `project` 篩選（`WHERE
        project = ?`），可與 `status` 並用；兩者都省略時行為與既有呼叫完全
        相同（向下相容，比照 `list_approvals()` 的 `WHERE 1=1` 累加寫法）。
        """
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id ASC"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [Job.from_row(r) for r in cur.fetchall()]

    def count_active_auto_placement_jobs(self, policy_id: str) -> int:
        """Goal 2 Slice 4:數某個 dispatch policy 目前有幾個非終態
        (queued/running/blocked)的 job 是由它的 `auto_placement` 核准建立
        ——`request_auto_placement_approval()` 用這個數字套用
        `max_concurrent_placements` 上限。

        `policy_id` 存在 `approvals.payload` 這個 JSON blob 裡,不是額外的
        FK 欄位;這裡在 Python 端解碼比對,刻意不依賴 SQLite 的 JSON1
        extension(維持全案「同步 sqlite3 標準函式庫、不加額外依賴」的既有
        慣例)。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, payload FROM approvals WHERE kind = 'auto_placement'"
            )
            approval_ids = [
                row["id"]
                for row in cur.fetchall()
                if json.loads(row["payload"] or "{}").get("policy_id") == policy_id
            ]
            if not approval_ids:
                return 0
            placeholders = ",".join("?" for _ in approval_ids)
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM jobs"
                f" WHERE auto_placement_approval_id IN ({placeholders})"
                " AND status IN ('queued', 'running', 'blocked')",
                approval_ids,
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "depends_on" in fields and not isinstance(fields["depends_on"], str):
            fields["depends_on"] = json.dumps(fields["depends_on"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        previous_row: Optional[sqlite3.Row] = None
        current_row: Optional[sqlite3.Row] = None
        # A running -> queued write competes with stop-intent creation.  Taking
        # the same IMMEDIATE transaction boundary prevents a reconciler from
        # requeueing between the stop guard and the UPDATE.
        cursor_context = (
            self._immediate_cursor()
            if fields.get("status") == "queued"
            else self.cursor()
        )
        with cursor_context as cur:
            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            previous_row = cur.fetchone()
            if (
                previous_row is not None
                and previous_row["status"] == "running"
                and fields.get("status") == "queued"
            ):
                cur.execute(
                    """
                    SELECT 1 FROM legacy_job_stop_intents
                    WHERE job_id = ?
                      AND state IN (
                          'requested', 'delivery_uncertain', 'delivered'
                      )
                    LIMIT 1
                    """,
                    (job_id,),
                )
                if cur.fetchone() is not None:
                    return
            cur.execute(f"UPDATE jobs SET {cols} WHERE id = ?", values)
            if (
                previous_row is not None
                and previous_row["status"] == "running"
                and fields.get("status") in {"done", "failed"}
            ):
                cur.execute(
                    """
                    UPDATE legacy_job_stop_intents
                    SET state = 'terminal_observed',
                        terminal_observed_at = ?
                    WHERE job_id = ?
                      AND state IN (
                          'requested', 'delivery_uncertain', 'delivered'
                      )
                    """,
                    (fields.get("finished_at") or now_iso(), job_id),
                )
            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            current_row = cur.fetchone()

        # Command observations and events are presentation-only.  The canonical
        # Job update above commits first; a corrupt/conflicting visibility row
        # must never roll execution state back or cause remote work to repeat.
        if (
            previous_row is None
            or current_row is None
            or previous_row["engineering_task_id"] is None
        ):
            return
        try:
            command = self.get_engineering_task_command_by_job_id(job_id)
            if command is not None:
                self.update_engineering_task_command_observation(
                    command.id,
                    recorded_status=current_row["status"],
                    recorded_started_at=current_row["started_at"],
                    recorded_finished_at=current_row["finished_at"],
                    recorded_exit_code=current_row["exit_code"],
                )
        except Exception:  # noqa: BLE001 - canonical Job state already committed
            pass

        previous_status = previous_row["status"]
        current_status = current_row["status"]
        if current_status == previous_status:
            return
        task_id = previous_row["engineering_task_id"]
        attempt_number = current_row["engineering_attempt_number"]
        role = current_row["engineering_task_role"] or "job"
        if previous_status == "running" and current_status == "queued":
            event_type = "execution_interrupted"
            summary = "執行中斷；工作已安全地回到佇列等待重試"
            phase = "queue"
            token = previous_row["started_at"] or "unknown-start"
        elif current_status == "running":
            event_type = "job_started"
            summary = "工作已開始執行"
            phase = "staging" if role == "staging" else "execution"
            token = current_row["started_at"] or f"{previous_status}-no-time"
        elif current_status in {"done", "failed", "blocked", "cancelled"}:
            event_type = "job_finished"
            summary = {
                "done": "工作已完成，等待結果收集",
                "failed": "工作已回報執行失敗",
                "blocked": "工作因相依條件而受阻",
                "cancelled": "工作已取消",
            }[current_status]
            phase = "staging" if role == "staging" else "finalization"
            token = current_row["finished_at"] or f"{previous_status}-no-time"
        else:
            event_type = "job_status_changed"
            summary = f"工作狀態更新為 {current_status}"
            phase = "staging" if role == "staging" else "execution"
            token = f"{previous_status}-to-{current_status}"
        try:
            self.append_engineering_task_event(
                task_id=task_id,
                attempt_number=attempt_number,
                event_key=f"job:{job_id}:{event_type}:{token}",
                event_type=event_type,
                phase=phase,
                state=current_status,
                summary=summary,
                details={
                    "job_id": job_id,
                    "job_role": role,
                    "previous_status": previous_status,
                },
                source_kind="job",
                source_id=str(job_id),
                occurred_at=(
                    current_row["started_at"]
                    if current_status == "running"
                    else current_row["finished_at"]
                ),
            )
        except Exception:  # noqa: BLE001 - canonical Job state already committed
            pass

    def delete_job(self, job_id: int) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # ---- approvals CRUD -------------------------------------------------

    def insert_approval(
        self,
        kind: str,
        payload: dict,
        requester_actor_id: Optional[str] = None,
    ) -> int:
        if kind not in VALID_APPROVAL_KINDS:
            raise ValueError(f"invalid approval kind: {kind}")
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approvals
                    (kind, payload, status, created_at, requester_actor_id)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (kind, json.dumps(payload), now_iso(), requester_actor_id),
            )
            return cur.lastrowid

    def get_approval(self, approval_id: int) -> Optional[Approval]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            row = cur.fetchone()
            return Approval.from_row(row) if row else None

    def list_approvals(
        self, status: Optional[str] = None, kind: Optional[str] = None
    ) -> list[Approval]:
        query = "SELECT * FROM approvals WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY id ASC"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [Approval.from_row(r) for r in cur.fetchall()]

    def update_approval(self, approval_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "payload" in fields and not isinstance(fields["payload"], str):
            fields["payload"] = json.dumps(fields["payload"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [approval_id]
        with self.cursor() as cur:
            cur.execute(f"UPDATE approvals SET {cols} WHERE id = ?", values)

    # ---- projects CRUD（階段 3）-----------------------------------------

    def insert_project(
        self,
        name: str,
        repo_or_path: str,
        dataset_name: Optional[str] = None,
        dataset_version: Optional[str] = None,
        default_command: Optional[str] = None,
        require_tag: Optional[str] = None,
        setup_cmd: Optional[str] = None,
        summary: Optional[str] = None,
        dataset_mode: str = "none",
    ) -> str:
        """名稱重複會丟 sqlite3.IntegrityError（PRIMARY KEY），呼叫端轉 400。
        切片 1 起同時產生並回傳 UUID 身分（既有呼叫端都不接回傳值,相容）。"""
        if dataset_mode not in VALID_DATASET_MODES:
            raise ValueError(f"invalid dataset_mode: {dataset_mode}")
        project_id = str(uuid.uuid4())
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects
                    (name, id, repo_or_path, dataset_name, dataset_version,
                     default_command, require_tag, setup_cmd, created_at,
                     summary, dataset_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    project_id,
                    repo_or_path,
                    dataset_name,
                    dataset_version,
                    default_command,
                    require_tag,
                    setup_cmd,
                    now_iso(),
                    summary,
                    dataset_mode,
                ),
            )
        return project_id

    def get_project(self, name: str) -> Optional[Project]:
        """切片 1 legacy name adapter：`name` 同時接受專案名稱或 UUID
        （`projects.id`）,名稱精確命中優先——所有既有 name-based 呼叫端
        行為不變,新呼叫端可直接拿 UUID 查,不必先反查名稱。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM projects WHERE name = ? OR id = ?"
                " ORDER BY (name = ?) DESC LIMIT 1",
                (name, name, name),
            )
            row = cur.fetchone()
            return Project.from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM projects ORDER BY name ASC")
            return [Project.from_row(r) for r in cur.fetchall()]

    def update_project(self, name: str, **fields: Any) -> None:
        """階段 8（第一批）新增：目前只有 import_project 核准分支用得到（設
        `summary`/`dataset_mode`），比照 `update_job`/`update_approval` 的
        寫法。"""
        if not fields:
            return
        if "dataset_mode" in fields and fields["dataset_mode"] not in VALID_DATASET_MODES:
            raise ValueError(f"invalid dataset_mode: {fields['dataset_mode']}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [name]
        with self.cursor() as cur:
            cur.execute(f"UPDATE projects SET {cols} WHERE name = ?", values)

    # ---- project_candidates / project_instances CRUD（階段 8，第一批）------

    def upsert_project_candidate(
        self,
        *,
        server: str,
        path: str,
        name_guess: Optional[str] = None,
        kind: str = "project",
        git_remote: Optional[str] = None,
        git_branch: Optional[str] = None,
        git_commit: Optional[str] = None,
        markers: Optional[list[str]] = None,
        readme_excerpt: Optional[str] = None,
        command_guess: Optional[str] = None,
        embedded_data_paths: Optional[list[str]] = None,
        embedded_data_summary: Optional[str] = None,
        estimated_data_bytes: Optional[int] = None,
        excluded_paths: Optional[list[str]] = None,
        confidence: Optional[float] = None,
    ) -> str:
        """用 `server+path` 的穩定 id（`make_candidate_id()`）upsert 一筆候選
        專案。**重新掃描不會覆蓋既有的 `status`**（pending/imported/
        ignored）——使用者已經做過的匯入/忽略決定不該被下一次掃描默默蓋掉；
        全新的候選（這個 id 第一次出現）才會以 `status='pending'` 建立。
        """
        cid = make_candidate_id(server, path)
        markers = markers or []
        embedded_data_paths = embedded_data_paths or []
        excluded_paths = excluded_paths or []
        now = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT id FROM project_candidates WHERE id = ?", (cid,))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    """
                    UPDATE project_candidates SET
                        name_guess = ?, kind = ?, git_remote = ?, git_branch = ?,
                        git_commit = ?, markers = ?, readme_excerpt = ?, command_guess = ?,
                        embedded_data_paths = ?, embedded_data_summary = ?,
                        estimated_data_bytes = ?, excluded_paths = ?, confidence = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name_guess,
                        kind,
                        git_remote,
                        git_branch,
                        git_commit,
                        json.dumps(markers),
                        readme_excerpt,
                        command_guess,
                        json.dumps(embedded_data_paths),
                        embedded_data_summary,
                        estimated_data_bytes,
                        json.dumps(excluded_paths),
                        confidence,
                        now,
                        cid,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO project_candidates
                        (id, server, path, name_guess, kind, git_remote, git_branch,
                         git_commit, markers, readme_excerpt, command_guess,
                         embedded_data_paths, embedded_data_summary, estimated_data_bytes,
                         excluded_paths, confidence, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        cid,
                        server,
                        path,
                        name_guess,
                        kind,
                        git_remote,
                        git_branch,
                        git_commit,
                        json.dumps(markers),
                        readme_excerpt,
                        command_guess,
                        json.dumps(embedded_data_paths),
                        embedded_data_summary,
                        estimated_data_bytes,
                        json.dumps(excluded_paths),
                        confidence,
                        now,
                        now,
                    ),
                )
        return cid

    def get_project_candidate(self, candidate_id: str) -> Optional[ProjectCandidate]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM project_candidates WHERE id = ?", (candidate_id,))
            row = cur.fetchone()
            return ProjectCandidate.from_row(row) if row else None

    def list_project_candidates(
        self,
        server: Optional[str] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[ProjectCandidate]:
        """`q` 對 `name_guess`/`path`/`readme_excerpt` 做簡單 LIKE 搜尋。"""
        query = "SELECT * FROM project_candidates WHERE 1=1"
        params: list[Any] = []
        if server:
            query += " AND server = ?"
            params.append(server)
        if status:
            query += " AND status = ?"
            params.append(status)
        if q:
            query += " AND (name_guess LIKE ? OR path LIKE ? OR readme_excerpt LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like, like])
        query += " ORDER BY updated_at DESC"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [ProjectCandidate.from_row(r) for r in cur.fetchall()]

    def update_project_candidate_status(self, candidate_id: str, status: str) -> None:
        if status not in VALID_CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status: {status}")
        with self.cursor() as cur:
            cur.execute(
                "UPDATE project_candidates SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), candidate_id),
            )

    def insert_project_instance(
        self,
        *,
        project_name: str,
        server: str,
        path: str,
        git_remote: Optional[str] = None,
        git_branch: Optional[str] = None,
        git_commit: Optional[str] = None,
        dirty: bool = False,
        embedded_data_paths: Optional[list[str]] = None,
    ) -> str:
        """用 `project_name+server+path` 的穩定 id（`make_instance_id()`）
        upsert：已存在同一個 id 就更新 git 資訊與 `last_seen`，不新增一列。
        """
        iid = make_instance_id(project_name, server, path)
        embedded_data_paths = embedded_data_paths or []
        now = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT id FROM project_instances WHERE id = ?", (iid,))
            exists = cur.fetchone() is not None
            if exists:
                # 切片 1：project_id 一併補寫（子查詢查不到＝維持 NULL,
                # 孤兒列不腦補）;state 不動——那是切片 2 reconcile 的所有權。
                cur.execute(
                    """
                    UPDATE project_instances SET
                        git_remote = ?, git_branch = ?, git_commit = ?, dirty = ?,
                        embedded_data_paths = ?, last_seen = ?,
                        project_id = (SELECT id FROM projects WHERE name = ?)
                    WHERE id = ?
                    """,
                    (
                        git_remote,
                        git_branch,
                        git_commit,
                        int(bool(dirty)),
                        json.dumps(embedded_data_paths),
                        now,
                        project_name,
                        iid,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO project_instances
                        (id, project_name, project_id, server, path, git_remote,
                         git_branch, git_commit, dirty, embedded_data_paths,
                         last_seen)
                    VALUES (?, ?, (SELECT id FROM projects WHERE name = ?),
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        iid,
                        project_name,
                        project_name,
                        server,
                        path,
                        git_remote,
                        git_branch,
                        git_commit,
                        int(bool(dirty)),
                        json.dumps(embedded_data_paths),
                        now,
                    ),
                )
        return iid

    def list_project_instances(self, project_name: str) -> list[ProjectInstance]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_instances WHERE project_name = ? ORDER BY server ASC",
                (project_name,),
            )
            return [ProjectInstance.from_row(r) for r in cur.fetchall()]

    def list_all_project_instances(self) -> list[ProjectInstance]:
        """切片 2（instance reconciliation）:背景 reconcile 迴圈一輪要掃的
        全部 instance,固定排序讓每輪順序可重現。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_instances ORDER BY project_name ASC, server ASC"
            )
            return [ProjectInstance.from_row(r) for r in cur.fetchall()]

    def update_instance_reconcile(
        self,
        instance_id: str,
        *,
        state: str,
        git_branch: Optional[str] = None,
        git_commit: Optional[str] = None,
        dirty: Optional[bool] = None,
        touch_last_seen: bool = False,
    ) -> None:
        """切片 2:reconcile 一輪對單一 instance 的落地。`state` 必在
        `VALID_INSTANCE_STATES`;`touch_last_seen=True`（真的觀察到 instance
        存在）才更新 git 快照三欄與 last_seen——unknown/missing 只動 state,
        git 欄位保留「最後一次確實觀察到」的事實(app/project_instances.py
        的呼叫端說明)。`instance_id` 不存在是 no-op（instance 可能在掃描與
        落地之間被專案刪除流程收走）。"""
        if state not in VALID_INSTANCE_STATES:
            raise ValueError(f"invalid instance state: {state}")
        with self.cursor() as cur:
            if touch_last_seen:
                cur.execute(
                    """
                    UPDATE project_instances SET
                        state = ?, git_branch = ?, git_commit = ?, dirty = ?,
                        last_seen = ?
                    WHERE id = ?
                    """,
                    (
                        state,
                        git_branch,
                        git_commit,
                        int(bool(dirty)),
                        now_iso(),
                        instance_id,
                    ),
                )
            else:
                cur.execute(
                    "UPDATE project_instances SET state = ? WHERE id = ?",
                    (state, instance_id),
                )

    def update_instance_git_state(
        self, instance_id: str, *, git_branch: Optional[str], git_commit: Optional[str]
    ) -> None:
        """階段 15 Phase B（PLAN.md P.2.1）：`kind=git_init` 核准成功後，把
        剛剛就地初始化出來的 branch/commit 寫回對應的 `project_instances`
        一列。刻意只更新這兩欄（不像 `insert_project_instance()` upsert 整列
        ——那個會把 `dirty`/`embedded_data_paths` 覆蓋成呼叫端傳入的預設值，
        這裡只想動 git_branch/git_commit，不動其他既有欄位）。`instance_id`
        不存在時是 no-op（呼叫端應該已經先用 `resolve_project_instance()`
        確認過存在）。"""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE project_instances SET git_branch = ?, git_commit = ? WHERE id = ?",
                (git_branch, git_commit, instance_id),
            )

    def delete_project(self, name: str) -> None:
        """階段 15 Phase B（PLAN.md P.2.4）：刪除 `projects` 一列＋該專案所有
        `project_instances` 列。**只動這兩張表，不碰任何其他資料**——
        `project_candidates` 的 `imported` 狀態不回溯（維持 imported，不會
        變回 pending，避免同一個候選被重複匯入）；`jobs` 歷史紀錄也不刪
        （保留執行歷史）。呼叫端（`app/main.py` 的 `DELETE /projects/{name}`）
        負責在呼叫這裡之前檢查有沒有 queued/running job 引用這個專案
        （409），這裡本身不做這個檢查，也絕不對任何機器發起 SSH 或動任何
        機器上的檔案（docstring 明確：這是純 DB 操作）。"""
        with self.cursor() as cur:
            cur.execute("DELETE FROM project_instances WHERE project_name = ?", (name,))
            cur.execute("DELETE FROM projects WHERE name = ?", (name,))
        # project_versions 刻意不刪:歷史 commit 紀錄比照 jobs 的既有慣例
        # 保留(上面 delete_project() docstring 的「jobs 歷史紀錄也不刪」
        # 同一精神)——之後 project_id 會是孤兒(專案已不存在),不腦補清除。

    # ---- project_versions CRUD（PLAN.md 2026-07-11 版 §14 切片 4）--------

    def get_or_create_project_version(
        self,
        project_name: str,
        git_commit: str,
        *,
        git_ref: Optional[str] = None,
        source_instance_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> ProjectVersion:
        """`(project_name, git_commit)` 已存在就原樣回傳既有列——commit 的
        身分不可變,重複呼叫(hub_sync 重跑同一個 commit、project_deploy 用
        到已經被同步過的 commit)**不**更新 `git_ref`/`source_instance_id`/
        `metadata`,不製造第二筆。找不到才新建,`project_id` 由當下查
        `projects.id` 決定(找不到對應 Project 時是 None,不腦補)。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_versions WHERE project_name = ? AND git_commit = ?",
                (project_name, git_commit),
            )
            row = cur.fetchone()
            if row is not None:
                return ProjectVersion.from_row(row)

            cur.execute("SELECT id FROM projects WHERE name = ?", (project_name,))
            proj_row = cur.fetchone()
            project_id = proj_row["id"] if proj_row is not None else None

            version_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO project_versions
                    (id, project_id, project_name, git_commit, git_ref,
                     source_instance_id, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    project_name,
                    git_commit,
                    git_ref,
                    source_instance_id,
                    now_iso(),
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            cur.execute("SELECT * FROM project_versions WHERE id = ?", (version_id,))
            return ProjectVersion.from_row(cur.fetchone())

    def list_project_versions(self, project_name: str) -> list[ProjectVersion]:
        """新到舊排序,供專案頁版本歷史使用。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_versions WHERE project_name = ?"
                " ORDER BY created_at DESC",
                (project_name,),
            )
            return [ProjectVersion.from_row(r) for r in cur.fetchall()]

    def get_project_version(self, version_id: str) -> Optional[ProjectVersion]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM project_versions WHERE id = ?", (version_id,))
            row = cur.fetchone()
            return ProjectVersion.from_row(row) if row else None

    # ---- run_profiles CRUD（D5 Run Profile v1，docs/DECISIONS.md）------

    def get_run_profile_head(
        self, project_id: str, name: str
    ) -> Optional[RunProfile]:
        """目前狀態＝該 (project_id, name) revision 最大的那一列。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM run_profiles WHERE project_id = ? AND name = ?"
                " ORDER BY revision DESC LIMIT 1",
                (project_id, name),
            )
            row = cur.fetchone()
            return RunProfile.from_row(row) if row else None

    def get_run_profile_by_id(self, profile_id: str) -> Optional[RunProfile]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM run_profiles WHERE id = ?", (profile_id,))
            row = cur.fetchone()
            return RunProfile.from_row(row) if row else None

    def list_run_profile_heads(self, project_id: str) -> list[RunProfile]:
        """一個專案底下每個 name 目前狀態（最新 revision）各一列，供列表用。"""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT rp.* FROM run_profiles rp
                INNER JOIN (
                    SELECT name, MAX(revision) AS max_revision
                    FROM run_profiles
                    WHERE project_id = ?
                    GROUP BY name
                ) heads
                ON rp.name = heads.name AND rp.revision = heads.max_revision
                WHERE rp.project_id = ?
                ORDER BY rp.name
                """,
                (project_id, project_id),
            )
            return [RunProfile.from_row(row) for row in cur.fetchall()]

    def list_run_profile_revisions(
        self, project_id: str, name: str
    ) -> list[RunProfile]:
        """一個 (project_id, name) 的完整不可變歷史，新到舊排序。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM run_profiles WHERE project_id = ? AND name = ?"
                " ORDER BY revision DESC",
                (project_id, name),
            )
            return [RunProfile.from_row(row) for row in cur.fetchall()]

    def insert_run_profile_revision(
        self,
        *,
        project_id: str,
        project_name: str,
        name: str,
        status: str,
        command: Optional[str],
        setup_cmd: Optional[str],
        require_tag: Optional[str],
        supersedes_id: Optional[str],
        approval_id: Optional[int],
        created_by_actor_id: Optional[str],
    ) -> RunProfile:
        """新增一個不可變 revision——永不 UPDATE 既有列。

        `revision` 是目前該 (project_id, name) 最大 revision + 1（第一次是
        1）；唯一索引 `idx_run_profiles_project_name_revision` 是最後一道
        防線，並發下如果算出來的下一個 revision 已被搶先寫入會丟
        `sqlite3.IntegrityError`（呼叫端／approve() 應視為核准當下狀態已
        過期，比照既有 revalidate-then-reject 模式處理，不重試腦補）。
        """
        if status not in VALID_RUN_PROFILE_STATUSES:
            raise ValueError(f"invalid run profile status: {status!r}")
        with self.cursor() as cur:
            cur.execute(
                "SELECT MAX(revision) AS max_revision FROM run_profiles"
                " WHERE project_id = ? AND name = ?",
                (project_id, name),
            )
            row = cur.fetchone()
            next_revision = (
                1 if row is None or row["max_revision"] is None
                else row["max_revision"] + 1
            )
            profile_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO run_profiles
                    (id, project_id, project_name, name, revision, status,
                     command, setup_cmd, require_tag, supersedes_id,
                     approval_id, created_by_actor_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    project_id,
                    project_name,
                    name,
                    next_revision,
                    status,
                    command,
                    setup_cmd,
                    require_tag,
                    supersedes_id,
                    approval_id,
                    created_by_actor_id,
                    now_iso(),
                ),
            )
            cur.execute("SELECT * FROM run_profiles WHERE id = ?", (profile_id,))
            return RunProfile.from_row(cur.fetchone())

    # ---- dispatch_policies CRUD（Goal 2 Slice 3，同 run_profiles 模式）----

    def get_dispatch_policy_head(
        self, project_id: str, name: str
    ) -> Optional[DispatchPolicy]:
        """目前狀態＝該 (project_id, name) revision 最大的那一列。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM dispatch_policies WHERE project_id = ? AND name = ?"
                " ORDER BY revision DESC LIMIT 1",
                (project_id, name),
            )
            row = cur.fetchone()
            return DispatchPolicy.from_row(row) if row else None

    def get_dispatch_policy_by_id(self, policy_id: str) -> Optional[DispatchPolicy]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM dispatch_policies WHERE id = ?", (policy_id,))
            row = cur.fetchone()
            return DispatchPolicy.from_row(row) if row else None

    def list_dispatch_policy_heads(self, project_id: str) -> list[DispatchPolicy]:
        """一個專案底下每個 name 目前狀態（最新 revision）各一列，供列表用。"""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT dp.* FROM dispatch_policies dp
                INNER JOIN (
                    SELECT name, MAX(revision) AS max_revision
                    FROM dispatch_policies
                    WHERE project_id = ?
                    GROUP BY name
                ) heads
                ON dp.name = heads.name AND dp.revision = heads.max_revision
                WHERE dp.project_id = ?
                ORDER BY dp.name
                """,
                (project_id, project_id),
            )
            return [DispatchPolicy.from_row(row) for row in cur.fetchall()]

    def list_dispatch_policy_revisions(
        self, project_id: str, name: str
    ) -> list[DispatchPolicy]:
        """一個 (project_id, name) 的完整不可變歷史，新到舊排序。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM dispatch_policies WHERE project_id = ? AND name = ?"
                " ORDER BY revision DESC",
                (project_id, name),
            )
            return [DispatchPolicy.from_row(row) for row in cur.fetchall()]

    def insert_dispatch_policy_revision(
        self,
        *,
        project_id: str,
        project_name: str,
        name: str,
        status: str,
        allowed_servers: list[str],
        require_tag: Optional[str],
        run_profile_id: Optional[str],
        dataset_required: bool,
        max_concurrent_placements: int,
        valid_until: Optional[str],
        approval_id: Optional[int],
        created_by_actor_id: Optional[str],
    ) -> DispatchPolicy:
        """新增一個不可變 revision——永不 UPDATE 既有列。

        `revision` 是目前該 (project_id, name) 最大 revision + 1（第一次是
        1）；唯一索引 `idx_dispatch_policies_project_name_revision` 是最後一
        道防線，並發下如果算出來的下一個 revision 已被搶先寫入會丟
        `sqlite3.IntegrityError`（呼叫端／approve() 應視為核准當下狀態已
        過期，比照既有 revalidate-then-reject 模式處理，不重試腦補）。
        """
        if status not in VALID_DISPATCH_POLICY_STATUSES:
            raise ValueError(f"invalid dispatch policy status: {status!r}")
        with self.cursor() as cur:
            cur.execute(
                "SELECT MAX(revision) AS max_revision FROM dispatch_policies"
                " WHERE project_id = ? AND name = ?",
                (project_id, name),
            )
            row = cur.fetchone()
            next_revision = (
                1 if row is None or row["max_revision"] is None
                else row["max_revision"] + 1
            )
            policy_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO dispatch_policies
                    (id, project_id, project_name, name, revision, status,
                     allowed_servers, require_tag, run_profile_id,
                     dataset_required, max_concurrent_placements, valid_until,
                     approval_id, created_by_actor_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    project_id,
                    project_name,
                    name,
                    next_revision,
                    status,
                    json.dumps(list(allowed_servers)),
                    require_tag,
                    run_profile_id,
                    1 if dataset_required else 0,
                    max_concurrent_placements,
                    valid_until,
                    approval_id,
                    created_by_actor_id,
                    now_iso(),
                ),
            )
            cur.execute("SELECT * FROM dispatch_policies WHERE id = ?", (policy_id,))
            return DispatchPolicy.from_row(cur.fetchone())

    # ---- server_observations（Goal 2 Slice 1，容量觀測持久化）-----------

    def insert_server_observation(
        self,
        *,
        server_name: str,
        online: bool,
        probe_ok: bool,
        gpu_count: Optional[int] = None,
        gpu_util_max: Optional[float] = None,
        gpu_mem_used_mb: Optional[float] = None,
        gpu_mem_total_mb: Optional[float] = None,
        load1: Optional[float] = None,
        mem_total_bytes: Optional[int] = None,
        mem_available_bytes: Optional[int] = None,
        disk_avail_bytes: Optional[int] = None,
    ) -> ServerObservation:
        """插入一筆探測快照。`observed_at` 一律用伺服器現在時間（`now_iso()`），
        不接受呼叫端傳入，避免時鐘漂移造成排序錯亂。"""
        observed_at = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO server_observations
                    (server_name, observed_at, online, probe_ok,
                     gpu_count, gpu_util_max, gpu_mem_used_mb, gpu_mem_total_mb,
                     load1, mem_total_bytes, mem_available_bytes, disk_avail_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server_name,
                    observed_at,
                    1 if online else 0,
                    1 if probe_ok else 0,
                    gpu_count,
                    gpu_util_max,
                    gpu_mem_used_mb,
                    gpu_mem_total_mb,
                    load1,
                    mem_total_bytes,
                    mem_available_bytes,
                    disk_avail_bytes,
                ),
            )
            observation_id = int(cur.lastrowid)
            cur.execute(
                "SELECT * FROM server_observations WHERE id = ?", (observation_id,)
            )
            return ServerObservation.from_row(cur.fetchone())

    def list_server_observations(
        self, server_name: str, *, since_iso: str, limit: int
    ) -> list[ServerObservation]:
        """某台伺服器 `since_iso` 之後（含）的觀測列，最新在前。`limit` 由
        呼叫端（API 層）先夾限範圍，這裡不重複做邊界檢查。"""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM server_observations
                WHERE server_name = ? AND observed_at >= ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (server_name, since_iso, limit),
            )
            return [ServerObservation.from_row(row) for row in cur.fetchall()]

    def prune_server_observations(self, *, before_iso: str) -> int:
        """刪除 `observed_at` 早於 `before_iso` 的觀測列，回傳刪除筆數。由
        monitor 迴圈機會性呼叫（best-effort 保留策略），不是排程強制的
        cleanup job。"""
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM server_observations WHERE observed_at < ?",
                (before_iso,),
            )
            return cur.rowcount if cur.rowcount is not None else 0

    def count_active_coding_jobs_by_server(self) -> dict[str, int]:
        """Goal 3 Phase D-1：每台 Runner 目前 active（queued＋running）的
        coding job 數——`pick_codex_runner()` 的決定性負載證據。queued 的
        job 用 `pin_server`（v2 coding job 建立時一律 pin 到選定 Runner），
        running 的用實際 `server`；兩者都沒有的列不計入任何機器。"""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(server, pin_server) AS runner, COUNT(*) AS n
                FROM jobs
                WHERE type = 'coding' AND status IN ('queued', 'running')
                      AND COALESCE(server, pin_server) IS NOT NULL
                GROUP BY runner
                """
            )
            return {row["runner"]: row["n"] for row in cur.fetchall()}

    # ---- nodes / node_attempts（Goal 3 C2，INV-NODE-*）------------------

    def insert_node(
        self,
        *,
        node_id: str,
        server_name: str,
        secret_hash: str,
        approval_id: Optional[int] = None,
    ) -> Node:
        """登錄一個 node（INV-NODE-1）。呼叫端負責產生憑證並只把 digest
        交進來——raw token 永不進這一層。"""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nodes
                    (id, server_name, secret_hash, status, agent_version,
                     last_heartbeat_at, created_at, revoked_at, approval_id)
                VALUES (?, ?, ?, 'enrolled', NULL, NULL, ?, NULL, ?)
                """,
                (node_id, server_name, secret_hash, now_iso(), approval_id),
            )
            cur.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            return Node.from_row(cur.fetchone())

    def get_node(self, node_id: str) -> Optional[Node]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            row = cur.fetchone()
            return Node.from_row(row) if row else None

    def list_nodes(self, *, server_name: Optional[str] = None) -> list[Node]:
        with self.cursor() as cur:
            if server_name is None:
                cur.execute("SELECT * FROM nodes ORDER BY created_at ASC, id ASC")
            else:
                cur.execute(
                    "SELECT * FROM nodes WHERE server_name = ? ORDER BY created_at ASC",
                    (server_name,),
                )
            return [Node.from_row(row) for row in cur.fetchall()]

    def revoke_node(self, node_id: str) -> Optional[Node]:
        """撤銷單一 node 的憑證（INV-NODE-1：個別撤銷，不影響其他 node）。
        不刪列——保留稽核軌跡。已撤銷的再撤銷是 no-op（冪等）。"""
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE nodes SET status = 'revoked', revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now_iso(), node_id),
            )
            cur.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            row = cur.fetchone()
            return Node.from_row(row) if row else None

    def update_node_secret(self, node_id: str, secret_hash: str) -> None:
        """換發憑證 digest（Goal 3 C3 rotation）。node id 與所有 attempt 歸屬
        完全不變——只有 secret 換掉，舊憑證立即失效。已撤銷的不換。"""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE nodes SET secret_hash = ? WHERE id = ? AND revoked_at IS NULL",
                (secret_hash, node_id),
            )

    def touch_node_heartbeat(
        self, node_id: str, *, agent_version: Optional[str] = None
    ) -> None:
        """更新 node 的最後心跳時間（INV-NODE-4：這只是觀測，不推斷任務
        狀態）。已撤銷的 node 不更新。"""
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE nodes SET last_heartbeat_at = ?,
                       agent_version = COALESCE(?, agent_version)
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now_iso(), agent_version, node_id),
            )

    def insert_node_attempt(
        self,
        *,
        attempt_id: str,
        job_id: int,
        node_id: str,
        command_sha256: str,
        lease_expires_at: str,
    ) -> NodeAttemptRow:
        """建立一筆 `leased` attempt。這一步必須在任何遠端副作用**之前**
        完成（DB-before-side-effect，INV-STATE-*／INV-NODE-2）。"""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO node_attempts
                    (id, job_id, node_id, status, command_sha256,
                     lease_expires_at, created_at)
                VALUES (?, ?, ?, 'leased', ?, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    node_id,
                    command_sha256,
                    lease_expires_at,
                    now_iso(),
                ),
            )
            cur.execute("SELECT * FROM node_attempts WHERE id = ?", (attempt_id,))
            return NodeAttemptRow.from_row(cur.fetchone())

    def get_node_attempt(self, attempt_id: str) -> Optional[NodeAttemptRow]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM node_attempts WHERE id = ?", (attempt_id,))
            row = cur.fetchone()
            return NodeAttemptRow.from_row(row) if row else None

    def list_node_attempts(
        self,
        *,
        job_id: Optional[int] = None,
        node_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[NodeAttemptRow]:
        clauses, params = [], []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.cursor() as cur:
            cur.execute(
                f"SELECT * FROM node_attempts {where} ORDER BY created_at ASC, id ASC",
                tuple(params),
            )
            return [NodeAttemptRow.from_row(row) for row in cur.fetchall()]

    def ack_node_attempt(self, attempt_id: str, node_id: str) -> bool:
        """原子性 acknowledge（INV-NODE-2）。

        `WHERE ... AND acked_at IS NULL AND node_id = ?` 讓「第一個 ack 才
        寫入」由 SQLite 保證——兩個並行請求只有一個會影響到列，另一個
        rowcount=0 並被呼叫端當成重複 ack（冪等回成功，不重複執行）。
        回傳 True 表示這一次確實是「第一次」ack。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE node_attempts
                SET status = 'acked', acked_at = ?, last_heartbeat_at = ?
                WHERE id = ? AND node_id = ? AND acked_at IS NULL
                      AND status = 'leased'
                """,
                (now_iso(), now_iso(), attempt_id, node_id),
            )
            return cur.rowcount == 1

    def upsert_node_attempt_artifact(
        self, *, attempt_id: str, relative_path: str, size_bytes: int, sha256: str
    ) -> None:
        """記錄/更新一筆 artifact 中繼資料（Goal 3 C3）。

        `UNIQUE(attempt_id, relative_path)` + upsert 讓 agent 的重送是冪等的
        （同一個檔案再報一次只更新大小/digest，不長出重複列）。**不搬動任何
        檔案內容**。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO node_attempt_artifacts
                    (attempt_id, relative_path, size_bytes, sha256, reported_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id, relative_path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    reported_at = excluded.reported_at
                """,
                (attempt_id, relative_path, size_bytes, sha256, now_iso()),
            )

    def list_node_attempt_artifacts(self, attempt_id: str) -> list[dict]:
        """某個 attempt 已回報的 artifact 中繼資料（路徑排序，確定性）。"""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT relative_path, size_bytes, sha256, reported_at
                FROM node_attempt_artifacts WHERE attempt_id = ?
                ORDER BY relative_path ASC
                """,
                (attempt_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def request_node_attempt_stop(self, attempt_id: str) -> bool:
        """記下一個已核准的停止請求（Goal 3 C3，roadmap Phase 3 stop-request）。

        只對**尚未終態**的 attempt 生效；已經停過的重複請求是 no-op（冪等）。
        回傳 True 表示這一次確實寫入了新的請求。

        這裡刻意**不改** attempt status——control plane 沒有入站通道可以真的
        殺掉遠端行程,只能留下請求等 agent 取回。狀態仍以 agent 回報的終態
        為準（INV-NODE-4：不得因為「我請求停止了」就推斷它停了）。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE node_attempts SET stop_requested_at = ?
                WHERE id = ? AND stop_requested_at IS NULL AND terminal_at IS NULL
                """,
                (now_iso(), attempt_id),
            )
            return cur.rowcount == 1

    def ack_node_attempt_stop(self, attempt_id: str, node_id: str) -> bool:
        """agent 確認收到停止請求。只有該 attempt 的擁有者能 ack。"""
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE node_attempts SET stop_acked_at = ?
                WHERE id = ? AND node_id = ? AND stop_requested_at IS NOT NULL
                      AND stop_acked_at IS NULL
                """,
                (now_iso(), attempt_id, node_id),
            )
            return cur.rowcount == 1

    def update_node_attempt(
        self,
        attempt_id: str,
        *,
        status: Optional[str] = None,
        last_heartbeat_at: Optional[str] = None,
        terminal_at: Optional[str] = None,
        exit_code: Optional[int] = None,
        log_tail: Optional[str] = None,
    ) -> Optional[NodeAttemptRow]:
        sets, params = [], []
        for column, value in (
            ("status", status),
            ("last_heartbeat_at", last_heartbeat_at),
            ("terminal_at", terminal_at),
            ("exit_code", exit_code),
            ("log_tail", log_tail),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if not sets:
            return self.get_node_attempt(attempt_id)
        params.append(attempt_id)
        with self.cursor() as cur:
            cur.execute(
                f"UPDATE node_attempts SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            cur.execute("SELECT * FROM node_attempts WHERE id = ?", (attempt_id,))
            row = cur.fetchone()
            return NodeAttemptRow.from_row(row) if row else None

    # ---- server_bootstrap_reports（Goal 3 Phase B）----------------------

    def insert_server_bootstrap_report(
        self,
        *,
        host: str,
        username: str,
        port: int,
        components: list[str],
        script_version: str,
        script_sha256: str,
        passed: bool,
        report: dict,
        approval_id: Optional[int] = None,
    ) -> ServerBootstrapReport:
        """插入一筆 bootstrap 結果報告。`created_at` 一律用伺服器現在時間
        （同 `insert_server_observation()` 慣例）。"""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO server_bootstrap_reports
                    (host, username, port, components, script_version,
                     script_sha256, passed, report, approval_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    host,
                    username,
                    port,
                    json.dumps(components, ensure_ascii=False),
                    script_version,
                    script_sha256,
                    1 if passed else 0,
                    json.dumps(report, ensure_ascii=False),
                    approval_id,
                    now_iso(),
                ),
            )
            report_id = int(cur.lastrowid)
            cur.execute(
                "SELECT * FROM server_bootstrap_reports WHERE id = ?", (report_id,)
            )
            return ServerBootstrapReport.from_row(cur.fetchone())

    def list_server_bootstrap_reports(
        self,
        *,
        host: Optional[str] = None,
        limit: int = 50,
    ) -> list[ServerBootstrapReport]:
        """報告列表（最新在前），可選 host 過濾。`limit` 由 API 層夾限。"""
        with self.cursor() as cur:
            if host is None:
                cur.execute(
                    """
                    SELECT * FROM server_bootstrap_reports
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM server_bootstrap_reports
                    WHERE host = ?
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (host, limit),
                )
            return [ServerBootstrapReport.from_row(row) for row in cur.fetchall()]

    def latest_server_bootstrap_report(
        self, *, host: str, username: str, port: int
    ) -> Optional[ServerBootstrapReport]:
        """同一目標 (host, username, port) 的最新報告；沒有 → None。
        `server_add` 閘只看最新一筆——舊的失敗報告被之後成功的一筆覆蓋。"""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM server_bootstrap_reports
                WHERE host = ? AND username = ? AND port = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (host, username, port),
            )
            row = cur.fetchone()
            return ServerBootstrapReport.from_row(row) if row else None

    # ---- engineering_tasks CRUD（AI Engineering Task backend v1）-------

    _ENGINEERING_TASK_UPDATE_FIELDS = {"coding_run_id", "status", "updated_at"}

    @classmethod
    def _insert_engineering_task_event_cur(
        cls,
        cur: sqlite3.Cursor,
        *,
        task_id: str,
        event_type: str,
        summary: str,
        attempt_number: Optional[int] = None,
        phase: Optional[str] = None,
        state: Optional[str] = None,
        details: Optional[dict] = None,
        source_kind: str,
        source_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        event_key: str,
        occurred_at: Optional[str] = None,
        recorded_at: Optional[str] = None,
    ) -> bool:
        safe_details = cls._validate_engineering_event_details(details)
        encoded_details = json.dumps(
            safe_details, sort_keys=True, separators=(",", ":")
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO engineering_task_events
                (event_key, engineering_task_id, attempt_number, event_type, phase, state,
                 summary, details, source_kind, source_id, actor_id,
                 occurred_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                task_id,
                attempt_number,
                event_type,
                phase,
                state,
                summary,
                encoded_details,
                source_kind,
                source_id,
                actor_id,
                occurred_at,
                recorded_at or now_iso(),
            ),
        )
        inserted = cur.rowcount == 1
        if not inserted:
            cur.execute(
                """
                SELECT attempt_number, event_type, phase, state, summary, details,
                       source_kind, source_id, actor_id, occurred_at
                FROM engineering_task_events
                WHERE engineering_task_id = ? AND event_key = ?
                """,
                (task_id, event_key),
            )
            existing = cur.fetchone()
            expected = (
                attempt_number,
                event_type,
                phase,
                state,
                summary,
                encoded_details,
                source_kind,
                source_id,
                actor_id,
                occurred_at,
            )
            actual = tuple(existing) if existing is not None else None
            if actual != expected:
                raise ValueError("engineering task event idempotency conflict")
        return inserted

    def insert_engineering_task_request(
        self,
        *,
        project_id: str,
        project_name: str,
        project_version_id: str,
        base_commit: str,
        agent_provider_id: str,
        provider_capabilities: dict,
        execution_contract: dict,
        contract_version: str,
        structured_request: dict,
        instruction: str,
        detected_metadata: dict,
        runner_server: str,
        validation_target: Optional[str],
        approval_payload: dict,
        requester_actor_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> tuple[str, int]:
        """以單一 transaction 建立 immutable task 與 pending coding_task approval。"""

        task_id = task_id or str(uuid.uuid4())
        try:
            task_id = str(uuid.UUID(task_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid engineering task id") from exc
        if approval_payload.get("engineering_task_id") != task_id:
            raise ValueError("approval payload engineering_task_id mismatch")
        now = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approvals
                    (kind, payload, status, created_at, requester_actor_id)
                VALUES ('coding_task', ?, 'pending', ?, ?)
                """,
                (json.dumps(approval_payload), now, requester_actor_id),
            )
            approval_id = int(cur.lastrowid)
            cur.execute(
                """
                INSERT INTO engineering_tasks
                    (id, approval_id, project_id, project_name,
                     project_version_id, base_commit, agent_provider_id,
                     provider_capabilities, execution_contract, contract_version, structured_request,
                     instruction, detected_metadata, runner_server,
                     validation_target, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending_approval', ?, ?)
                """,
                (
                    task_id,
                    approval_id,
                    project_id,
                    project_name,
                    project_version_id,
                    base_commit,
                    agent_provider_id,
                    json.dumps(provider_capabilities),
                    json.dumps(execution_contract),
                    contract_version,
                    json.dumps(structured_request),
                    instruction,
                    json.dumps(detected_metadata),
                    runner_server,
                    validation_target,
                    now,
                    now,
                ),
            )
            self._insert_engineering_task_event_cur(
                cur,
                task_id=task_id,
                event_type="task_created",
                phase="approval",
                state="pending_approval",
                summary="AI Engineering Task 已建立，等待人工核准",
                details={
                    "approval_id": approval_id,
                    "project_version_id": project_version_id,
                    "base_commit": base_commit,
                    "agent_provider_id": agent_provider_id,
                },
                source_kind="task",
                source_id=str(approval_id),
                actor_id=requester_actor_id,
                event_key=f"task-created:{task_id}",
                occurred_at=now,
                recorded_at=now,
            )
        return task_id, approval_id

    def get_engineering_task(self, task_id: str) -> Optional[EngineeringTask]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM engineering_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return EngineeringTask.from_row(row) if row else None

    def get_engineering_task_by_approval_id(
        self, approval_id: int
    ) -> Optional[EngineeringTask]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM engineering_tasks WHERE approval_id = ?", (approval_id,)
            )
            row = cur.fetchone()
            return EngineeringTask.from_row(row) if row else None

    def list_engineering_tasks(
        self,
        *,
        project: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[EngineeringTask]:
        query = "SELECT * FROM engineering_tasks WHERE 1=1"
        params: list[Any] = []
        if project:
            query += " AND project_name = ?"
            params.append(project)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(query, params)
            return [EngineeringTask.from_row(row) for row in cur.fetchall()]

    @staticmethod
    def _validate_engineering_visibility_text(value: str, *, field_name: str) -> None:
        if not value or len(value) > 512 or any(char in value for char in ("\n", "\r", "\0")):
            raise ValueError(f"invalid engineering visibility {field_name}")
        lowered = value.lower()
        if any(
            marker in lowered
            for marker in ("-----begin ", "authorization:", "bearer ", "/home/", "../")
        ):
            raise ValueError(f"unsafe engineering visibility {field_name}")

    @staticmethod
    def _validate_engineering_event_details(details: Optional[dict]) -> dict:
        if details is None:
            return {}
        if not isinstance(details, dict):
            raise ValueError("engineering event details must be an object")

        forbidden_keys = (
            "token",
            "secret",
            "password",
            "passwd",
            "cookie",
            "command",
            "credential",
            "authorization",
            "private_key",
            "api_key",
            "access_key",
            "session_key",
            "ssh_key",
            "log",
            "diff",
            "path",
        )
        unsafe_values = (
            "-----begin ",
            "authorization:",
            "authorization=",
            "bearer ",
            "access_token=",
            "access_token:",
            "api_key=",
            "api_key:",
            "api-key=",
            "api-key:",
            "password=",
            "password:",
            "passwd=",
            "passwd:",
            "secret=",
            "secret:",
            "cookie=",
            "cookie:",
            "/home/",
            "/root/",
            "/tmp/",
            "github_pat_",
            "ghp_",
            "gho_",
            "ghu_",
            "ghs_",
            "ghr_",
            "sk-",
            "akia",
            "c:\\users\\",
            "c:/users/",
        )

        def normalize(value: Any, *, depth: int) -> Any:
            if depth > 8:
                raise ValueError("engineering event details are too deeply nested")
            if isinstance(value, dict):
                normalized_object: dict[str, Any] = {}
                for key, nested_value in value.items():
                    if not isinstance(key, str) or not key or len(key) > 128:
                        raise ValueError("engineering event details contain an invalid field")
                    lowered_key = key.casefold().replace("-", "_")
                    if any(marker in lowered_key for marker in forbidden_keys):
                        raise ValueError(
                            "engineering event details contain a forbidden field"
                        )
                    if any(char in key for char in ("\n", "\r", "\0")):
                        raise ValueError("engineering event details contain an invalid field")
                    normalized_object[key] = normalize(nested_value, depth=depth + 1)
                return normalized_object
            if isinstance(value, (list, tuple)):
                return [normalize(item, depth=depth + 1) for item in value]
            if value is None or isinstance(value, (str, bool, int)):
                if isinstance(value, str):
                    if any(char in value for char in ("\0",)):
                        raise ValueError(
                            "engineering event details contain unsafe content"
                        )
                    lowered_value = value.casefold()
                    if any(marker in lowered_value for marker in unsafe_values):
                        raise ValueError(
                            "engineering event details contain unsafe content"
                        )
                return value
            if isinstance(value, float) and math.isfinite(value):
                return value
            raise ValueError("engineering event details contain an unsupported value")

        normalized = normalize(details, depth=0)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 4096:
            raise ValueError("engineering event details are too large")
        return normalized

    def append_engineering_task_event(
        self,
        *,
        task_id: str,
        event_key: str,
        event_type: str,
        source_kind: str,
        summary: str,
        attempt_number: Optional[int] = None,
        phase: Optional[str] = None,
        state: Optional[str] = None,
        source_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        details: Optional[dict] = None,
        occurred_at: Optional[str] = None,
        recorded_at: Optional[str] = None,
    ) -> EngineeringTaskEvent:
        """Append an idempotent, bounded visibility fact.

        Reusing a key with different immutable content fails closed.  This is a
        query journal only; it never drives Approval, Job or CodingRun state.
        """

        for value, name in (
            (event_key, "event_key"),
            (event_type, "event_type"),
            (source_kind, "source_kind"),
            (summary, "summary"),
        ):
            self._validate_engineering_visibility_text(value, field_name=name)
        if source_kind not in VALID_ENGINEERING_EVENT_SOURCES:
            raise ValueError("invalid engineering event source")
        safe_details = self._validate_engineering_event_details(details)
        with self.cursor() as cur:
            cur.execute("SELECT 1 FROM engineering_tasks WHERE id = ?", (task_id,))
            if cur.fetchone() is None:
                raise ValueError("engineering task does not exist")
            self._insert_engineering_task_event_cur(
                cur,
                task_id=task_id,
                event_key=event_key,
                event_type=event_type,
                source_kind=source_kind,
                summary=summary,
                attempt_number=attempt_number,
                phase=phase,
                state=state,
                source_id=source_id,
                actor_id=actor_id,
                details=safe_details,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
            )
            cur.execute(
                """
                SELECT * FROM engineering_task_events
                WHERE engineering_task_id = ? AND event_key = ?
                """,
                (task_id, event_key),
            )
            return EngineeringTaskEvent.from_row(cur.fetchone())

    def list_engineering_task_events(
        self,
        task_id: str,
        *,
        after_id: int = 0,
        attempt_number: Optional[int] = None,
        limit: int = 100,
    ) -> list[EngineeringTaskEvent]:
        query = (
            "SELECT * FROM engineering_task_events "
            "WHERE engineering_task_id = ? AND id > ?"
        )
        params: list[Any] = [task_id, max(after_id, 0)]
        if attempt_number is not None:
            query += " AND attempt_number = ?"
            params.append(attempt_number)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with self.cursor() as cur:
            cur.execute(query, params)
            return [EngineeringTaskEvent.from_row(row) for row in cur.fetchall()]

    def get_engineering_task_presentation_flags(self, task_id: str) -> dict[str, bool]:
        """Return full-journal safety flags used by task presentation.

        Timeline pages are intentionally bounded, so presentation must not infer
        the absence of a safety-significant event from the first page.  Keep the
        query and returned key set fixed: this is not a general event search API.
        """

        event_types = ("execution_interrupted", "runner_contract_mismatch")
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT event_type
                FROM engineering_task_events
                WHERE engineering_task_id = ?
                  AND event_type IN (?, ?)
                """,
                (task_id, *event_types),
            )
            observed = {str(row["event_type"]) for row in cur.fetchall()}
        return {event_type: event_type in observed for event_type in event_types}

    def register_engineering_task_command(
        self,
        *,
        task_id: str,
        attempt_number: int,
        sequence: int,
        command_key: str,
        command_role: str,
        display_command: str,
        execution_location: str,
        working_directory_label: str,
        policy_family: str,
        policy_disposition: str,
        status_source: str,
        job_id: Optional[int] = None,
        coding_run_id: Optional[int] = None,
        command_digest: Optional[str] = None,
        target_ref: Optional[str] = None,
        approval_id: Optional[int] = None,
        recorded_status: Optional[str] = None,
        recorded_started_at: Optional[str] = None,
        recorded_finished_at: Optional[str] = None,
        recorded_exit_code: Optional[int] = None,
    ) -> EngineeringTaskCommand:
        if attempt_number < 1 or sequence < 1:
            raise ValueError("engineering command attempt/sequence must be positive")
        for value, name in (
            (command_key, "command_key"),
            (command_role, "command_role"),
            (display_command, "display_command"),
            (execution_location, "execution_location"),
            (working_directory_label, "working_directory_label"),
            (policy_family, "policy_family"),
            (policy_disposition, "policy_disposition"),
            (status_source, "status_source"),
        ):
            self._validate_engineering_visibility_text(value, field_name=name)
        if command_role not in VALID_ENGINEERING_COMMAND_ROLES:
            raise ValueError("invalid engineering command role")
        if execution_location not in VALID_ENGINEERING_EXECUTION_LOCATIONS:
            raise ValueError("invalid engineering execution location")
        if policy_disposition not in VALID_ENGINEERING_POLICY_DISPOSITIONS:
            raise ValueError("invalid engineering command policy disposition")
        if status_source not in VALID_ENGINEERING_STATUS_SOURCES:
            raise ValueError("invalid engineering command status source")
        if command_digest is not None and (
            len(command_digest) != 64 or not _is_full_hex_digest(command_digest)
        ):
            raise ValueError("invalid engineering command digest")
        now = now_iso()
        spec = (
            task_id,
            attempt_number,
            sequence,
            command_key,
            job_id,
            coding_run_id,
            command_role,
            display_command,
            command_digest,
            execution_location,
            target_ref,
            working_directory_label,
            policy_family,
            policy_disposition,
            approval_id,
            status_source,
        )
        with self.cursor() as cur:
            cur.execute("SELECT 1 FROM engineering_tasks WHERE id = ?", (task_id,))
            if cur.fetchone() is None:
                raise ValueError("engineering task does not exist")
            if job_id is not None:
                cur.execute(
                    """
                    SELECT 1 FROM jobs
                    WHERE id = ? AND engineering_task_id = ?
                      AND engineering_attempt_number = ?
                    """,
                    (job_id, task_id, attempt_number),
                )
                if cur.fetchone() is None:
                    raise ValueError("engineering command job ownership mismatch")
            if coding_run_id is not None:
                cur.execute(
                    """
                    SELECT 1 FROM coding_runs
                    WHERE id = ? AND engineering_task_id = ? AND attempt_number = ?
                    """,
                    (coding_run_id, task_id, attempt_number),
                )
                if cur.fetchone() is None:
                    raise ValueError("engineering command run ownership mismatch")
            cur.execute(
                """
                INSERT OR IGNORE INTO engineering_task_commands
                    (engineering_task_id, attempt_number, sequence, command_key,
                     job_id, coding_run_id, command_role, display_command,
                     command_digest, execution_location, target_ref,
                     working_directory_label, policy_family, policy_disposition,
                     approval_id, status_source, recorded_status,
                     recorded_started_at, recorded_finished_at,
                     recorded_exit_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                spec
                + (
                    recorded_status,
                    recorded_started_at,
                    recorded_finished_at,
                    recorded_exit_code,
                    now,
                    now,
                ),
            )
            cur.execute(
                """
                SELECT * FROM engineering_task_commands
                WHERE engineering_task_id = ? AND attempt_number = ? AND command_key = ?
                """,
                (task_id, attempt_number, command_key),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("engineering command registration failed")
            actual_spec = tuple(
                row[column]
                for column in (
                    "engineering_task_id", "attempt_number", "sequence", "command_key",
                    "job_id", "coding_run_id", "command_role", "display_command",
                    "command_digest", "execution_location", "target_ref",
                    "working_directory_label", "policy_family", "policy_disposition",
                    "approval_id", "status_source",
                )
            )
            if actual_spec != spec:
                raise ValueError("engineering command idempotency conflict")
            return EngineeringTaskCommand.from_row(row)

    def update_engineering_task_command_observation(
        self,
        command_id: int,
        *,
        recorded_status: Optional[str],
        recorded_started_at: Optional[str],
        recorded_finished_at: Optional[str],
        recorded_exit_code: Optional[int],
    ) -> Optional[EngineeringTaskCommand]:
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE engineering_task_commands
                SET recorded_status = ?, recorded_started_at = ?,
                    recorded_finished_at = ?, recorded_exit_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    recorded_status,
                    recorded_started_at,
                    recorded_finished_at,
                    recorded_exit_code,
                    now_iso(),
                    command_id,
                ),
            )
            cur.execute("SELECT * FROM engineering_task_commands WHERE id = ?", (command_id,))
            row = cur.fetchone()
            return EngineeringTaskCommand.from_row(row) if row else None

    def list_engineering_task_commands(
        self,
        task_id: str,
        *,
        attempt_number: Optional[int] = None,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[EngineeringTaskCommand]:
        query = (
            "SELECT * FROM engineering_task_commands "
            "WHERE engineering_task_id = ? AND id > ?"
        )
        params: list[Any] = [task_id, max(after_id, 0)]
        if attempt_number is not None:
            query += " AND attempt_number = ?"
            params.append(attempt_number)
        query += " ORDER BY attempt_number, sequence, id LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with self.cursor() as cur:
            cur.execute(query, params)
            return [EngineeringTaskCommand.from_row(row) for row in cur.fetchall()]

    def get_engineering_task_command(
        self, task_id: str, command_id: int
    ) -> Optional[EngineeringTaskCommand]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM engineering_task_commands
                WHERE engineering_task_id = ? AND id = ?
                """,
                (task_id, command_id),
            )
            row = cur.fetchone()
            return EngineeringTaskCommand.from_row(row) if row else None

    def get_engineering_task_command_by_job_id(
        self, job_id: int
    ) -> Optional[EngineeringTaskCommand]:
        """Return the immutable command-journal row owned by one Job.

        ``idx_engineering_task_commands_job`` makes this relation one-to-one
        whenever ``job_id`` is present.  Execution contract gates use this
        narrow lookup instead of trusting the mutable ``jobs.command`` column
        on its own.
        """

        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM engineering_task_commands WHERE job_id = ?",
                (job_id,),
            )
            row = cur.fetchone()
            return EngineeringTaskCommand.from_row(row) if row else None

    def register_engineering_task_artifact(
        self,
        *,
        task_id: str,
        attempt_number: int,
        artifact_key: str,
        kind: str,
        label: str,
        storage_kind: str,
        coding_run_id: Optional[int] = None,
        source_job_id: Optional[int] = None,
        storage_key: Optional[str] = None,
        content_type: Optional[str] = None,
        verification_status: str = "pending",
        redaction_status: str = "pending",
        availability: str = "pending",
    ) -> EngineeringTaskArtifact:
        if attempt_number < 1:
            raise ValueError("engineering artifact attempt must be positive")
        for value, name in (
            (artifact_key, "artifact_key"),
            (kind, "kind"),
            (label, "label"),
            (storage_kind, "storage_kind"),
            (verification_status, "verification_status"),
            (redaction_status, "redaction_status"),
            (availability, "availability"),
        ):
            self._validate_engineering_visibility_text(value, field_name=name)
        if availability not in VALID_ENGINEERING_ARTIFACT_AVAILABILITY:
            raise ValueError("invalid engineering artifact availability")
        if verification_status not in VALID_ENGINEERING_ARTIFACT_VERIFICATION:
            raise ValueError("invalid engineering artifact verification status")
        if redaction_status not in VALID_ENGINEERING_ARTIFACT_REDACTION:
            raise ValueError("invalid engineering artifact redaction status")
        if storage_key is not None:
            self._validate_engineering_visibility_text(storage_key, field_name="storage_key")
            if "/" in storage_key or "\\" in storage_key:
                raise ValueError("artifact storage_key must be opaque")
        artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{task_id}:{attempt_number}:{artifact_key}"))
        now = now_iso()
        identity = (
            artifact_id,
            artifact_key,
            task_id,
            attempt_number,
            coding_run_id,
            source_job_id,
            kind,
            label,
            storage_kind,
            storage_key,
            content_type,
        )
        with self.cursor() as cur:
            cur.execute("SELECT 1 FROM engineering_tasks WHERE id = ?", (task_id,))
            if cur.fetchone() is None:
                raise ValueError("engineering task does not exist")
            if source_job_id is not None:
                cur.execute(
                    """
                    SELECT 1 FROM jobs
                    WHERE id = ? AND engineering_task_id = ?
                      AND engineering_attempt_number = ?
                    """,
                    (source_job_id, task_id, attempt_number),
                )
                if cur.fetchone() is None:
                    raise ValueError("engineering artifact job ownership mismatch")
            if coding_run_id is not None:
                cur.execute(
                    """
                    SELECT 1 FROM coding_runs
                    WHERE id = ? AND engineering_task_id = ? AND attempt_number = ?
                    """,
                    (coding_run_id, task_id, attempt_number),
                )
                if cur.fetchone() is None:
                    raise ValueError("engineering artifact run ownership mismatch")
            cur.execute(
                """
                INSERT OR IGNORE INTO engineering_task_artifacts
                    (id, artifact_key, engineering_task_id, attempt_number,
                     coding_run_id, source_job_id, kind, label, storage_kind,
                     storage_key, content_type, verification_status,
                     redaction_status, availability, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                identity
                + (
                    verification_status,
                    redaction_status,
                    availability,
                    now,
                    now,
                ),
            )
            cur.execute("SELECT * FROM engineering_task_artifacts WHERE id = ?", (artifact_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError("engineering artifact registration failed")
            actual = tuple(
                row[column]
                for column in (
                    "id", "artifact_key", "engineering_task_id", "attempt_number",
                    "coding_run_id", "source_job_id", "kind", "label", "storage_kind",
                    "storage_key", "content_type",
                )
            )
            if actual != identity:
                raise ValueError("engineering artifact idempotency conflict")
            return EngineeringTaskArtifact.from_row(row)

    def record_engineering_task_artifact_collection(
        self,
        artifact_id: str,
        *,
        source_sha256: Optional[str],
        source_size_bytes: Optional[int],
        verification_status: str,
        redaction_status: str,
        availability: str,
        collected_at: Optional[str] = None,
    ) -> Optional[EngineeringTaskArtifact]:
        if source_sha256 is not None and (
            len(source_sha256) != 64 or not _is_full_hex_digest(source_sha256)
        ):
            raise ValueError("invalid engineering artifact digest")
        if source_size_bytes is not None and source_size_bytes < 0:
            raise ValueError("invalid engineering artifact size")
        now = now_iso()
        if availability not in VALID_ENGINEERING_ARTIFACT_AVAILABILITY:
            raise ValueError("invalid engineering artifact availability")
        if verification_status not in VALID_ENGINEERING_ARTIFACT_VERIFICATION:
            raise ValueError("invalid engineering artifact verification status")
        if redaction_status not in VALID_ENGINEERING_ARTIFACT_REDACTION:
            raise ValueError("invalid engineering artifact redaction status")
        with self.cursor() as cur:
            cur.execute("SELECT * FROM engineering_task_artifacts WHERE id = ?", (artifact_id,))
            existing = cur.fetchone()
            if existing is None:
                return None
            if existing["source_sha256"] not in (None, source_sha256):
                raise ValueError("engineering artifact digest is immutable")
            if existing["source_size_bytes"] not in (None, source_size_bytes):
                raise ValueError("engineering artifact size is immutable")
            forward = {
                "verification_status": {
                    "pending": VALID_ENGINEERING_ARTIFACT_VERIFICATION,
                    "unknown": {"unknown", "pending", "verified", "rejected"},
                    "not_required": {"not_required"},
                    "verified": {"verified"},
                    "rejected": {"rejected"},
                },
                "redaction_status": {
                    "pending": VALID_ENGINEERING_ARTIFACT_REDACTION,
                    "not_applicable": {"not_applicable"},
                    "redacted": {"redacted"},
                    "withheld": {"withheld"},
                },
                "availability": {
                    "pending": VALID_ENGINEERING_ARTIFACT_AVAILABILITY,
                    "missing": VALID_ENGINEERING_ARTIFACT_AVAILABILITY,
                    "available": {"available", "cleaned"},
                    "withheld": {"withheld", "cleaned"},
                    "rejected": {"rejected", "cleaned"},
                    "cleaned": {"cleaned"},
                },
            }
            for field_name, requested in (
                ("verification_status", verification_status),
                ("redaction_status", redaction_status),
                ("availability", availability),
            ):
                if requested not in forward[field_name][existing[field_name]]:
                    raise ValueError(f"engineering artifact {field_name} cannot regress")
            cur.execute(
                """
                UPDATE engineering_task_artifacts
                SET source_sha256 = COALESCE(source_sha256, ?),
                    source_size_bytes = COALESCE(source_size_bytes, ?),
                    verification_status = ?, redaction_status = ?,
                    availability = ?, collected_at = COALESCE(collected_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    source_sha256,
                    source_size_bytes,
                    verification_status,
                    redaction_status,
                    availability,
                    collected_at or now,
                    now,
                    artifact_id,
                ),
            )
            cur.execute("SELECT * FROM engineering_task_artifacts WHERE id = ?", (artifact_id,))
            return EngineeringTaskArtifact.from_row(cur.fetchone())

    def mark_engineering_task_artifacts_cleaned(
        self, *, task_id: str, attempt_number: Optional[int] = None
    ) -> int:
        query = (
            "UPDATE engineering_task_artifacts SET availability = 'cleaned', updated_at = ? "
            "WHERE engineering_task_id = ? AND availability != 'cleaned'"
        )
        params: list[Any] = [now_iso(), task_id]
        if attempt_number is not None:
            query += " AND attempt_number = ?"
            params.append(attempt_number)
        with self.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount

    def list_engineering_task_artifacts(
        self, task_id: str, *, attempt_number: Optional[int] = None
    ) -> list[EngineeringTaskArtifact]:
        query = "SELECT * FROM engineering_task_artifacts WHERE engineering_task_id = ?"
        params: list[Any] = [task_id]
        if attempt_number is not None:
            query += " AND attempt_number = ?"
            params.append(attempt_number)
        query += " ORDER BY attempt_number, kind, id"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [EngineeringTaskArtifact.from_row(row) for row in cur.fetchall()]

    def get_engineering_task_artifact(
        self, task_id: str, artifact_id: str
    ) -> Optional[EngineeringTaskArtifact]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM engineering_task_artifacts
                WHERE engineering_task_id = ? AND id = ?
                """,
                (task_id, artifact_id),
            )
            row = cur.fetchone()
            return EngineeringTaskArtifact.from_row(row) if row else None

    def insert_engineering_validation_request(
        self,
        *,
        validation_request_id: str,
        task_id: str,
        attempt_number: int,
        coding_run_id: int,
        project_id: str,
        project_name: str,
        project_version_id: str,
        base_commit: str,
        result_commit: str,
        target_server: str,
        request_snapshot: dict,
        request_snapshot_sha256: str,
        approval_payload: dict,
        requester_actor_id: Optional[str] = None,
    ) -> tuple[str, int]:
        """Atomically persist an immutable validation proposal and approval."""

        try:
            validation_request_id = str(uuid.UUID(validation_request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid engineering validation request id") from exc
        encoded_snapshot = json.dumps(
            request_snapshot, sort_keys=True, separators=(",", ":")
        )
        expected_digest = hashlib.sha256(encoded_snapshot.encode("utf-8")).hexdigest()
        if request_snapshot_sha256 != expected_digest:
            raise ValueError("engineering validation snapshot digest mismatch")
        if approval_payload.get("validation_request_id") != validation_request_id:
            raise ValueError("approval payload validation_request_id mismatch")
        if approval_payload.get("request_snapshot_sha256") != expected_digest:
            raise ValueError("approval payload validation snapshot mismatch")
        task_snapshot = request_snapshot.get("task")
        bundle_snapshot = request_snapshot.get("bundle")
        parent_approval_snapshot = request_snapshot.get("parent_approval")
        execution_snapshot = request_snapshot.get("execution")
        if not all(
            isinstance(value, dict)
            for value in (
                task_snapshot,
                bundle_snapshot,
                parent_approval_snapshot,
                execution_snapshot,
            )
        ):
            raise ValueError("engineering validation snapshot is incomplete")
        for digest_key in (
            "bundle_push_command_sha256",
            "downstream_command_sha256",
        ):
            value = execution_snapshot.get(digest_key)
            if not isinstance(value, str) or len(value) != 64 or not _is_full_hex_digest(value):
                raise ValueError("engineering validation command digest is invalid")
        now = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT * FROM engineering_tasks WHERE id = ?", (task_id,))
            task = cur.fetchone()
            if (
                task is None
                or task["project_id"] != project_id
                or task["project_name"] != project_name
                or task["project_version_id"] != project_version_id
                or task["base_commit"].lower() != base_commit.lower()
                or task["coding_run_id"] != coding_run_id
                or task_snapshot.get("id") != task_id
                or task_snapshot.get("project_id") != project_id
                or task_snapshot.get("project_name") != project_name
                or task_snapshot.get("project_version_id") != project_version_id
                or task_snapshot.get("base_commit") != base_commit
                or task_snapshot.get("coding_run_id") != coding_run_id
                or task_snapshot.get("attempt_number") != attempt_number
                or task_snapshot.get("result_commit") != result_commit
            ):
                raise ValueError("engineering validation task contract changed")
            cur.execute("SELECT * FROM approvals WHERE id = ?", (task["approval_id"],))
            parent_approval = cur.fetchone()
            if parent_approval is None:
                raise ValueError("engineering validation parent approval is missing")
            parent_payload = json.loads(parent_approval["payload"] or "{}")
            parent_payload_digest = hashlib.sha256(
                json.dumps(
                    parent_payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            if (
                parent_approval["kind"] != "coding_task"
                or parent_approval["status"] != "approved"
                or parent_approval_snapshot.get("id") != parent_approval["id"]
                or parent_approval_snapshot.get("payload_sha256")
                != parent_payload_digest
            ):
                raise ValueError("engineering validation parent approval changed")
            cur.execute("SELECT * FROM coding_runs WHERE id = ?", (coding_run_id,))
            run = cur.fetchone()
            if (
                run is None
                or run["engineering_task_id"] != task_id
                or run["attempt_number"] != attempt_number
                or run["project_version_id"] != project_version_id
                or run["base_binding"] != "project_version_pinned"
                or (run["base_commit"] or "").lower() != base_commit.lower()
                or run["status"] != "done"
                or run["result_commit"] != result_commit
            ):
                raise ValueError("engineering validation coding run is not eligible")
            cur.execute(
                """
                SELECT * FROM engineering_task_artifacts
                WHERE engineering_task_id = ? AND attempt_number = ?
                  AND coding_run_id = ? AND kind = 'bundle'
                  AND verification_status = 'verified'
                  AND availability = 'available'
                LIMIT 1
                """,
                (task_id, attempt_number, coding_run_id),
            )
            bundle_artifact = cur.fetchone()
            if (
                bundle_artifact is None
                or bundle_snapshot.get("storage_key") != "changes.bundle"
                or bundle_artifact["storage_key"] != bundle_snapshot.get("storage_key")
                or bundle_artifact["source_sha256"] != bundle_snapshot.get("sha256")
                or bundle_artifact["source_size_bytes"]
                != bundle_snapshot.get("size_bytes")
            ):
                raise ValueError("engineering validation requires a verified bundle")
            cur.execute(
                """
                INSERT INTO approvals
                    (kind, payload, status, created_at, requester_actor_id)
                VALUES ('enqueue', ?, 'pending', ?, ?)
                """,
                (json.dumps(approval_payload), now, requester_actor_id),
            )
            approval_id = int(cur.lastrowid)
            cur.execute(
                """
                INSERT INTO engineering_validation_requests
                    (id, engineering_task_id, attempt_number, coding_run_id,
                     approval_id, project_id, project_name, project_version_id,
                     base_commit, result_commit, target_server, request_snapshot,
                     request_snapshot_sha256, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending_approval', ?, ?)
                """,
                (
                    validation_request_id,
                    task_id,
                    attempt_number,
                    coding_run_id,
                    approval_id,
                    project_id,
                    project_name,
                    project_version_id,
                    base_commit,
                    result_commit,
                    target_server,
                    encoded_snapshot,
                    expected_digest,
                    now,
                    now,
                ),
            )
            self._insert_engineering_task_event_cur(
                cur,
                task_id=task_id,
                attempt_number=attempt_number,
                event_key=f"worker-validation:{validation_request_id}:requested",
                event_type="worker_validation_requested",
                phase="validation",
                state="requested",
                summary="Worker validation 已提出，等待 enqueue 核准",
                details={
                    "approval_id": approval_id,
                    "coding_run_id": coding_run_id,
                    "target_server": target_server,
                    "validation_request_id": validation_request_id,
                },
                source_kind="approval",
                source_id=str(approval_id),
                actor_id=requester_actor_id,
                occurred_at=now,
                recorded_at=now,
            )
        return validation_request_id, approval_id

    def get_engineering_validation_request(
        self, validation_request_id: str
    ) -> Optional[EngineeringValidationRequest]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM engineering_validation_requests WHERE id = ?",
                (validation_request_id,),
            )
            row = cur.fetchone()
            return EngineeringValidationRequest.from_row(row) if row else None

    def get_engineering_validation_request_by_approval_id(
        self, approval_id: int
    ) -> Optional[EngineeringValidationRequest]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM engineering_validation_requests WHERE approval_id = ?",
                (approval_id,),
            )
            row = cur.fetchone()
            return EngineeringValidationRequest.from_row(row) if row else None

    def get_engineering_validation_request_by_job_id(
        self, job_id: int
    ) -> Optional[EngineeringValidationRequest]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT validation.*
                FROM jobs AS job
                JOIN engineering_validation_requests AS validation
                  ON validation.id = job.engineering_validation_request_id
                WHERE job.id = ?
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return EngineeringValidationRequest.from_row(row) if row else None

    def list_engineering_validation_requests(
        self, task_id: str
    ) -> list[EngineeringValidationRequest]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM engineering_validation_requests
                WHERE engineering_task_id = ?
                ORDER BY created_at, id
                """,
                (task_id,),
            )
            return [
                EngineeringValidationRequest.from_row(row) for row in cur.fetchall()
            ]

    def list_engineering_task_approvals(self, task_id: str) -> list[Approval]:
        """Return the parent and linked Worker-validation approvals in order.

        ``UNION`` deliberately deduplicates approval ids before the canonical
        ``created_at, id`` ordering is applied.  Callers still have to use a
        safe projection: :class:`Approval` contains the immutable payload and
        must not be serialized wholesale by task-detail APIs.
        """

        with self.cursor() as cur:
            cur.execute(
                """
                SELECT approval.*
                FROM approvals AS approval
                WHERE approval.id IN (
                    SELECT approval_id
                    FROM engineering_tasks
                    WHERE id = ?
                    UNION
                    SELECT approval_id
                    FROM engineering_validation_requests
                    WHERE engineering_task_id = ?
                )
                ORDER BY approval.created_at, approval.id
                """,
                (task_id, task_id),
            )
            return [Approval.from_row(row) for row in cur.fetchall()]

    @staticmethod
    def _next_engineering_validation_transition_number_cur(
        cur: sqlite3.Cursor,
        *,
        task_id: str,
        transition_prefix: str,
    ) -> int:
        """Return a collision-free, deterministic transition ordinal.

        Event rows are append-only, but a restored or manually inspected
        database can contain gaps or malformed keys.  Counting matching rows
        is therefore not an ordinal allocator: one existing ``4:running`` row
        plus two unrelated/malformed rows would make ``COUNT(*) + 1`` choose
        the already-used ordinal 4.  Parse bounded positive decimal ordinals
        and choose the first unused value instead.

        The exact-prefix ``substr`` predicate deliberately avoids SQL ``LIKE``
        wildcard semantics.  Oversized numeric fragments are treated as
        malformed, so corrupt input cannot force unbounded integer parsing.
        """

        cur.execute(
            """
            SELECT event_key
            FROM engineering_task_events
            WHERE engineering_task_id = ?
              AND substr(event_key, 1, ?) = ?
            """,
            (task_id, len(transition_prefix), transition_prefix),
        )
        used_ordinals: set[int] = set()
        for event in cur.fetchall():
            event_key = event["event_key"]
            if not isinstance(event_key, str):
                continue
            ordinal_text, separator, _state = event_key[
                len(transition_prefix) :
            ].partition(":")
            if (
                not separator
                or not ordinal_text
                or len(ordinal_text) > 128
                or not ordinal_text.isascii()
                or not ordinal_text.isdecimal()
            ):
                continue
            ordinal = int(ordinal_text)
            if ordinal > 0:
                used_ordinals.add(ordinal)
        candidate = 1
        while candidate in used_ordinals:
            candidate += 1
        return candidate

    def reject_engineering_validation_approval(
        self,
        *,
        validation_request_id: str,
        approval_id: int,
        note: Optional[str],
        decision_actor_id: Optional[str],
        decision_mechanism: str,
    ) -> None:
        """Atomically reject a pending Worker-validation proposal.

        Rejection is a decision on the linked ordinary ``enqueue`` approval,
        but the validation projection and its safe journal fact must become
        durable in the same transaction.  No read-side refresh is required,
        and a proposal that has acquired any Job linkage fails closed.
        """

        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT validation.engineering_task_id,
                       validation.attempt_number,
                       validation.status AS validation_status,
                       validation.bundle_push_job_id,
                       validation.downstream_job_id,
                       approval.kind AS approval_kind,
                       approval.status AS approval_status
                FROM engineering_validation_requests AS validation
                JOIN approvals AS approval
                  ON approval.id = validation.approval_id
                WHERE validation.id = ? AND approval.id = ?
                """,
                (validation_request_id, approval_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("engineering validation approval linkage is missing")
            if (
                row["approval_kind"] != "enqueue"
                or row["approval_status"] != "pending"
                or row["validation_status"] != "pending_approval"
                or row["bundle_push_job_id"] is not None
                or row["downstream_job_id"] is not None
            ):
                raise ValueError("engineering validation approval is not rejectable")
            cur.execute(
                """
                SELECT 1
                FROM jobs
                WHERE engineering_validation_request_id = ?
                LIMIT 1
                """,
                (validation_request_id,),
            )
            if cur.fetchone() is not None:
                raise ValueError("engineering validation already has linked Jobs")

            cur.execute(
                """
                UPDATE approvals
                SET status = 'rejected', decided_at = ?, note = ?,
                    decision_actor_id = ?, decision_mechanism = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    timestamp,
                    note,
                    decision_actor_id,
                    decision_mechanism,
                    approval_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    "engineering validation approval changed during rejection"
                )
            cur.execute(
                """
                UPDATE engineering_validation_requests
                SET status = 'rejected', result_status = NULL,
                    result_exit_code = NULL, result_finished_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'pending_approval'
                  AND bundle_push_job_id IS NULL
                  AND downstream_job_id IS NULL
                """,
                (timestamp, validation_request_id),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    "engineering validation state changed during rejection"
                )
            self._insert_engineering_task_event_cur(
                cur,
                task_id=row["engineering_task_id"],
                attempt_number=row["attempt_number"],
                event_key=f"worker-validation:{validation_request_id}:rejected",
                event_type="worker_validation_rejected",
                phase="validation",
                state="rejected",
                summary=_ENGINEERING_VALIDATION_STATUS_SUMMARIES["rejected"],
                details={
                    "approval_id": approval_id,
                    "previous_state": "requested",
                    "validation_request_id": validation_request_id,
                },
                source_kind="approval",
                source_id=str(approval_id),
                actor_id=decision_actor_id,
                occurred_at=timestamp,
                recorded_at=timestamp,
            )

    def finalize_engineering_validation_request(
        self,
        *,
        validation_request_id: str,
        approval_id: int,
        request_snapshot_sha256: str,
        bundle_push_command: str,
        downstream_command: str,
        job_type: str,
        project: str,
        target_server: str,
        require_tag: Optional[str],
        gpus_needed: Optional[int],
        priority: str,
        source_coding_run_id: int,
        approval_note: Optional[str],
        decision_actor_id: Optional[str],
        decision_mechanism: str,
    ) -> tuple[int, int]:
        """Approve and publish the push/main ordinary Jobs in one transaction."""

        if job_type not in VALID_TYPES or priority not in VALID_PRIORITIES:
            raise ValueError("invalid engineering validation Job contract")
        push_digest = hashlib.sha256(bundle_push_command.encode("utf-8")).hexdigest()
        downstream_digest = hashlib.sha256(
            downstream_command.encode("utf-8")
        ).hexdigest()
        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            approval = cur.fetchone()
            cur.execute(
                "SELECT * FROM engineering_validation_requests WHERE id = ?",
                (validation_request_id,),
            )
            validation = cur.fetchone()
            if (
                approval is None
                or approval["kind"] != "enqueue"
                or approval["status"] != "pending"
                or validation is None
                or validation["approval_id"] != approval_id
                or validation["status"] != "pending_approval"
                or validation["request_snapshot_sha256"]
                != request_snapshot_sha256
                or validation["project_name"] != project
                or validation["target_server"] != target_server
                or validation["coding_run_id"] != source_coding_run_id
                or validation["bundle_push_job_id"] is not None
                or validation["downstream_job_id"] is not None
            ):
                raise ValueError("engineering validation request is not finalizable")
            approval_payload = json.loads(approval["payload"] or "{}")
            if (
                approval_payload.get("validation_request_id")
                != validation_request_id
                or approval_payload.get("request_snapshot_sha256")
                != request_snapshot_sha256
            ):
                raise ValueError("engineering validation approval payload changed")
            snapshot = json.loads(validation["request_snapshot"] or "{}")
            execution_snapshot = snapshot.get("execution")
            parent_approval_snapshot = snapshot.get("parent_approval")
            if not isinstance(execution_snapshot, dict) or not isinstance(
                parent_approval_snapshot, dict
            ):
                raise ValueError("engineering validation snapshot changed")
            if (
                execution_snapshot.get("bundle_push_command_sha256") != push_digest
                or execution_snapshot.get("downstream_command_sha256")
                != downstream_digest
            ):
                raise ValueError("engineering validation generated command changed")
            cur.execute(
                "SELECT * FROM engineering_tasks WHERE id = ?",
                (validation["engineering_task_id"],),
            )
            task = cur.fetchone()
            cur.execute(
                "SELECT * FROM coding_runs WHERE id = ?",
                (source_coding_run_id,),
            )
            run = cur.fetchone()
            cur.execute(
                "SELECT * FROM project_versions WHERE id = ?",
                (validation["project_version_id"],),
            )
            version = cur.fetchone()
            parent_approval = None
            if task is not None:
                cur.execute(
                    "SELECT * FROM approvals WHERE id = ?", (task["approval_id"],)
                )
                parent_approval = cur.fetchone()
            if (
                task is None
                or run is None
                or version is None
                or parent_approval is None
                or parent_approval["kind"] != "coding_task"
                or parent_approval["status"] != "approved"
                or parent_approval["id"] != parent_approval_snapshot.get("id")
                or hashlib.sha256(
                    json.dumps(
                        json.loads(parent_approval["payload"] or "{}"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                != parent_approval_snapshot.get("payload_sha256")
                or task["coding_run_id"] != source_coding_run_id
                or task["project_id"] != validation["project_id"]
                or task["project_name"] != validation["project_name"]
                or task["project_version_id"] != validation["project_version_id"]
                or task["base_commit"] != validation["base_commit"]
                or run["engineering_task_id"] != validation["engineering_task_id"]
                or run["attempt_number"] != validation["attempt_number"]
                or run["project_version_id"] != validation["project_version_id"]
                or run["base_binding"] != "project_version_pinned"
                or run["base_commit"] != validation["base_commit"]
                or run["status"] != "done"
                or run["result_commit"] != validation["result_commit"]
                or version["project_id"] != validation["project_id"]
                or version["project_name"] != validation["project_name"]
                or version["git_commit"] != validation["base_commit"]
            ):
                raise ValueError("engineering validation parent contract changed")

            cur.execute(
                """
                INSERT INTO jobs
                    (type, project, command, pin_server, depends_on, status,
                     priority, created_at, engineering_validation_request_id)
                VALUES ('sync', ?, ?, '_local', '[]', 'queued', 'normal', ?, ?)
                """,
                (project, bundle_push_command, timestamp, validation_request_id),
            )
            push_job_id = int(cur.lastrowid)
            cur.execute(
                """
                INSERT INTO jobs
                    (type, project, command, require_tag, pin_server,
                     depends_on, gpus_needed, status, priority, created_at,
                     source_coding_run_id, engineering_validation_request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_type,
                    project,
                    downstream_command,
                    require_tag,
                    target_server,
                    json.dumps([push_job_id]),
                    gpus_needed,
                    priority,
                    timestamp,
                    source_coding_run_id,
                    validation_request_id,
                ),
            )
            downstream_job_id = int(cur.lastrowid)
            cur.execute(
                """
                UPDATE engineering_validation_requests
                SET status = 'queued', bundle_push_job_id = ?,
                    downstream_job_id = ?, result_status = 'queued',
                    bundle_push_command_sha256 = ?,
                    downstream_command_sha256 = ?, updated_at = ?
                WHERE id = ? AND status = 'pending_approval'
                """,
                (
                    push_job_id,
                    downstream_job_id,
                    push_digest,
                    downstream_digest,
                    timestamp,
                    validation_request_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("engineering validation linkage changed")
            final_note = approval_note or (
                f"已建立 worker validation Job #{downstream_job_id}"
                f"（bundle push Job #{push_job_id}）"
            )
            cur.execute(
                """
                UPDATE approvals
                SET status = 'approved', decided_at = ?, note = ?,
                    decision_actor_id = ?, decision_mechanism = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    timestamp,
                    final_note,
                    decision_actor_id,
                    decision_mechanism,
                    approval_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("engineering validation approval changed")
            self._insert_engineering_task_event_cur(
                cur,
                task_id=validation["engineering_task_id"],
                attempt_number=validation["attempt_number"],
                event_key=f"worker-validation:{validation_request_id}:queued",
                event_type="worker_validation_queued",
                phase="validation",
                state="queued",
                summary="Worker validation 已核准並排入 ordinary Job 佇列",
                details={
                    "approval_id": approval_id,
                    "bundle_push_job_id": push_job_id,
                    "downstream_job_id": downstream_job_id,
                    "validation_request_id": validation_request_id,
                },
                source_kind="job",
                source_id=str(downstream_job_id),
                actor_id=decision_actor_id,
                occurred_at=timestamp,
                recorded_at=timestamp,
            )
        return push_job_id, downstream_job_id

    def refresh_engineering_validation_request_status(
        self, validation_request_id: str
    ) -> Optional[EngineeringValidationRequest]:
        """Project approval/Job state without guessing unknown remote outcomes."""

        validation_statuses = VALID_STATUSES | {
            "pending_approval",
            "rejected",
            "unknown",
        }
        # The process normally owns one Database object, but tests, CLI tools,
        # and rolling restarts can briefly use multiple SQLite connections.
        # Compare-and-set keeps two observers of the same transition from both
        # journaling it. A bounded retry is enough because this is a derived,
        # presentation-only projection; the next read can safely reconcile
        # again if the row is changing continuously.
        for _attempt in range(4):
            with self.cursor() as cur:
                cur.execute(
                    "SELECT * FROM engineering_validation_requests WHERE id = ?",
                    (validation_request_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute(
                    "SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)
                )
                approval = cur.fetchone()
                job = None
                if row["downstream_job_id"] is not None:
                    cur.execute(
                        "SELECT * FROM jobs WHERE id = ?",
                        (row["downstream_job_id"],),
                    )
                    job = cur.fetchone()

                raw_job_status = job["status"] if job is not None else None
                safe_job_status = (
                    raw_job_status if raw_job_status in VALID_STATUSES else "unknown"
                )
                if approval is None:
                    projected = "unknown"
                elif approval["status"] == "pending":
                    projected = "pending_approval"
                elif approval["status"] == "rejected":
                    projected = "rejected"
                elif approval["status"] != "approved" or job is None:
                    projected = "unknown"
                else:
                    projected = safe_job_status

                result_status = safe_job_status if job is not None else None
                if (
                    job is not None
                    and safe_job_status
                    in _ENGINEERING_VALIDATION_TERMINAL_RESULT_STATUSES
                ):
                    result_exit_code = _safe_engineering_validation_exit_code(
                        job["exit_code"]
                    )
                    result_finished_at = _safe_engineering_validation_timestamp(
                        job["finished_at"]
                    )
                else:
                    result_exit_code = None
                    result_finished_at = None
                stored_status = (
                    row["status"] if row["status"] in validation_statuses else "unknown"
                )
                status_changed = stored_status != projected
                projection_changed = (
                    row["status"] != projected
                    or row["result_status"] != result_status
                    or row["result_exit_code"] != result_exit_code
                    or row["result_finished_at"] != result_finished_at
                )
                if not projection_changed:
                    return EngineeringValidationRequest.from_row(row)

                timestamp = now_iso()
                cur.execute(
                    """
                    UPDATE engineering_validation_requests
                    SET status = ?, result_status = ?, result_exit_code = ?,
                        result_finished_at = ?, updated_at = ?
                    WHERE id = ?
                      AND status IS ?
                      AND result_status IS ?
                      AND result_exit_code IS ?
                      AND result_finished_at IS ?
                    """,
                    (
                        projected,
                        result_status,
                        result_exit_code,
                        result_finished_at,
                        timestamp,
                        validation_request_id,
                        row["status"],
                        row["result_status"],
                        row["result_exit_code"],
                        row["result_finished_at"],
                    ),
                )
                if cur.rowcount != 1:
                    continue

                if status_changed:
                    journal_state = (
                        "requested" if projected == "pending_approval" else projected
                    )
                    previous_state = (
                        "requested" if stored_status == "pending_approval" else stored_status
                    )
                    # A Job may legitimately requeue and later run again.  A
                    # deterministic unused per-request ordinal makes every
                    # occurrence unique while the CAS above ensures that two
                    # SQLite connections cannot journal the same observation.
                    transition_prefix = (
                        f"worker-validation:{validation_request_id}:transition:"
                    )
                    transition_number = (
                        self._next_engineering_validation_transition_number_cur(
                            cur,
                            task_id=row["engineering_task_id"],
                            transition_prefix=transition_prefix,
                        )
                    )
                    source_kind = "job" if job is not None else "approval"
                    source_id = (
                        str(row["downstream_job_id"])
                        if job is not None
                        else str(row["approval_id"])
                    )
                    actor_id = (
                        approval["decision_actor_id"]
                        if approval is not None and projected == "rejected"
                        else None
                    )
                    occurred_at = timestamp
                    if projected == "rejected" and approval is not None:
                        occurred_at = (
                            _safe_engineering_validation_timestamp(
                                approval["decided_at"]
                            )
                            or timestamp
                        )
                    elif job is not None and projected == "running":
                        occurred_at = (
                            _safe_engineering_validation_timestamp(job["started_at"])
                            or timestamp
                        )
                    elif job is not None and projected in {
                        "done",
                        "failed",
                        "cancelled",
                    }:
                        occurred_at = result_finished_at or timestamp
                    self._insert_engineering_task_event_cur(
                        cur,
                        task_id=row["engineering_task_id"],
                        attempt_number=row["attempt_number"],
                        event_key=(
                            f"{transition_prefix}{transition_number}:{journal_state}"
                        ),
                        event_type=f"worker_validation_{journal_state}",
                        phase="validation",
                        state=journal_state,
                        summary=_ENGINEERING_VALIDATION_STATUS_SUMMARIES.get(
                            journal_state,
                            "Worker validation 狀態已更新",
                        ),
                        details={
                            "approval_id": row["approval_id"],
                            "downstream_job_id": row["downstream_job_id"],
                            "previous_state": previous_state,
                            "validation_request_id": validation_request_id,
                        },
                        source_kind=source_kind,
                        source_id=source_id,
                        actor_id=actor_id,
                        occurred_at=occurred_at,
                        recorded_at=timestamp,
                    )
                cur.execute(
                    "SELECT * FROM engineering_validation_requests WHERE id = ?",
                    (validation_request_id,),
                )
                return EngineeringValidationRequest.from_row(cur.fetchone())

        # A continuously changing projection is not an execution failure.  Do
        # not guess or raise from this read path; return the latest durable row
        # and let a later refresh reconcile it.
        return self.get_engineering_validation_request(validation_request_id)

    def list_engineering_task_jobs(
        self, task_id: str, *, attempt_number: Optional[int] = None
    ) -> list[Job]:
        query = "SELECT * FROM jobs WHERE engineering_task_id = ?"
        params: list[Any] = [task_id]
        if attempt_number is not None:
            query += " AND engineering_attempt_number = ?"
            params.append(attempt_number)
        query += " ORDER BY engineering_attempt_number, id"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [Job.from_row(row) for row in cur.fetchall()]

    def list_engineering_task_attempt_runs(self, task_id: str) -> list[CodingRun]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM coding_runs
                WHERE engineering_task_id = ?
                ORDER BY attempt_number, id
                """,
                (task_id,),
            )
            return [CodingRun.from_row(row) for row in cur.fetchall()]

    def update_engineering_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        invalid = set(fields) - self._ENGINEERING_TASK_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"invalid engineering_task field(s): {sorted(invalid)}")
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self.cursor() as cur:
            previous_status = None
            if "status" in fields:
                cur.execute(
                    "SELECT status FROM engineering_tasks WHERE id = ?", (task_id,)
                )
                row = cur.fetchone()
                previous_status = row["status"] if row is not None else None
            cur.execute(f"UPDATE engineering_tasks SET {cols} WHERE id = ?", values)
            new_status = fields.get("status")
            if (
                cur.rowcount == 1
                and new_status is not None
                and new_status != previous_status
            ):
                self._insert_engineering_task_event_cur(
                    cur,
                    task_id=task_id,
                    event_type="status_changed",
                    phase=_engineering_phase_for_status(new_status),
                    state=new_status,
                    summary=_engineering_status_summary(new_status),
                    details={"previous_status": previous_status},
                    source_kind="system",
                    event_key=(
                        f"task-status:{previous_status or 'none'}:{new_status}:"
                        f"{fields['updated_at']}"
                    ),
                    occurred_at=fields["updated_at"],
                )

    def reject_engineering_task_approval(
        self,
        *,
        task_id: str,
        approval_id: int,
        note: Optional[str],
        decision_actor_id: Optional[str],
        decision_mechanism: str,
    ) -> None:
        """Atomically reject the approval, parent cache and safe journal fact."""

        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT task.status AS task_status, approval.status AS approval_status
                FROM engineering_tasks AS task
                JOIN approvals AS approval ON approval.id = task.approval_id
                WHERE task.id = ? AND approval.id = ?
                """,
                (task_id, approval_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("engineering task approval linkage is missing")
            if row["approval_status"] != "pending" or row["task_status"] not in {
                "pending_approval",
                "planning",
            }:
                raise ValueError("engineering task approval is not rejectable")
            cur.execute(
                """
                UPDATE approvals
                SET status = 'rejected', decided_at = ?, note = ?,
                    decision_actor_id = ?, decision_mechanism = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    timestamp,
                    note,
                    decision_actor_id,
                    decision_mechanism,
                    approval_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("engineering task approval changed during rejection")
            cur.execute(
                """
                UPDATE engineering_tasks
                SET status = 'rejected', updated_at = ?
                WHERE id = ?
                """,
                (timestamp, task_id),
            )
            self._insert_engineering_task_event_cur(
                cur,
                task_id=task_id,
                event_key=f"approval:{approval_id}:rejected",
                event_type="approval_rejected",
                phase="approval",
                state="rejected",
                summary="AI Engineering Task 核准請求已拒絕",
                details={"approval_id": approval_id},
                source_kind="approval",
                source_id=str(approval_id),
                actor_id=decision_actor_id,
                occurred_at=timestamp,
                recorded_at=timestamp,
            )

    def _insert_engineering_task_attempt_rows(
        self,
        cur: sqlite3.Cursor,
        *,
        task_id: str,
        attempt_number: int,
        approval_id: int,
        project: str,
        runner_server: str,
        instruction: str,
        base_commit: str,
        project_version_id: str,
        validation_target: Optional[str],
        worktree_path: str,
        staging_command: str,
        coding_command: str,
        decision_actor_id: Optional[str],
        timestamp: str,
    ) -> tuple[int, int, int]:
        """Insert one attempt's coding_run/jobs/command-journal rows.

        Shared by ``finalize_engineering_task_approval_plan`` (attempt 1, the
        original coding_task approval) and
        ``finalize_engineering_task_retry_plan`` (attempt N>1).  Callers own
        every precondition check; this helper only performs the atomic insert
        shape and must stay an exact transformation of the original attempt-1
        SQL, parameterized by ``attempt_number``.
        """

        cur.execute(
            """
            INSERT INTO coding_runs
                (approval_id, job_id, project, runner_server, instruction,
                 base_branch, base_commit, worktree_path, validation_target,
                 status, engineering_task_id, project_version_id,
                 base_binding, attempt_number, created_at)
            VALUES (?, NULL, ?, ?, ?, NULL, ?, ?, ?, 'queued', ?, ?,
                    'project_version_pinned', ?, ?)
            """,
            (
                approval_id,
                project,
                runner_server,
                instruction,
                base_commit,
                worktree_path,
                validation_target,
                task_id,
                project_version_id,
                attempt_number,
                timestamp,
            ),
        )
        run_id = int(cur.lastrowid)

        cur.execute(
            """
            INSERT INTO jobs
                (type, project, command, pin_server, depends_on, status,
                 priority, created_at, engineering_task_id,
                 engineering_task_role, engineering_attempt_number)
            VALUES ('sync', ?, ?, '_local', '[]', 'queued', 'normal', ?,
                    ?, 'staging', ?)
            """,
            (project, staging_command, timestamp, task_id, attempt_number),
        )
        staging_job_id = int(cur.lastrowid)

        cur.execute(
            """
            INSERT INTO jobs
                (type, project, command, pin_server, depends_on, status,
                 priority, created_at, engineering_task_id,
                 engineering_task_role, engineering_attempt_number)
            VALUES ('coding', ?, ?, ?, ?, 'queued', 'normal', ?,
                    ?, 'coding', ?)
            """,
            (
                project,
                coding_command,
                runner_server,
                json.dumps([staging_job_id]),
                timestamp,
                task_id,
                attempt_number,
            ),
        )
        coding_job_id = int(cur.lastrowid)
        command_rows = (
            (
                task_id,
                attempt_number,
                1,
                "staging-job",
                staging_job_id,
                run_id,
                "staging",
                f"Stage approved ProjectVersion {base_commit[:12]} bundle",
                hashlib.sha256(staging_command.encode("utf-8")).hexdigest(),
                "server_a",
                "Server A",
                "Server A local staging area",
                "immutable_base_staging",
                "task_approved",
                approval_id,
                "job",
                "queued",
                timestamp,
                timestamp,
            ),
            (
                task_id,
                attempt_number,
                2,
                "codex-agent-turn",
                coding_job_id,
                run_id,
                "agent_turn",
                "Codex agent turn in isolated worktree",
                hashlib.sha256(coding_command.encode("utf-8")).hexdigest(),
                "coding_runner",
                runner_server,
                "Isolated task worktree",
                "approved_agent_execution",
                "task_approved",
                approval_id,
                "job",
                "queued",
                timestamp,
                timestamp,
            ),
        )
        cur.executemany(
            """
            INSERT INTO engineering_task_commands
                (engineering_task_id, attempt_number, sequence, command_key,
                 job_id, coding_run_id, command_role, display_command,
                 command_digest, execution_location, target_ref,
                 working_directory_label, policy_family, policy_disposition,
                 approval_id, status_source, recorded_status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            command_rows,
        )

        cur.execute(
            "UPDATE coding_runs SET job_id = ? WHERE id = ?",
            (coding_job_id, run_id),
        )
        cur.execute(
            """
            UPDATE engineering_tasks
            SET coding_run_id = ?, status = 'queued', updated_at = ?
            WHERE id = ?
            """,
            (run_id, timestamp, task_id),
        )

        self._insert_engineering_task_event_cur(
            cur,
            task_id=task_id,
            attempt_number=attempt_number,
            event_key=f"approval:{approval_id}:approved",
            event_type="approval_approved",
            phase="approval",
            state="queued",
            summary="AI Engineering Task 已核准",
            details={"approval_id": approval_id},
            source_kind="approval",
            source_id=str(approval_id),
            actor_id=decision_actor_id,
            occurred_at=timestamp,
            recorded_at=timestamp,
        )
        self._insert_engineering_task_event_cur(
            cur,
            task_id=task_id,
            attempt_number=attempt_number,
            event_key=f"attempt:{attempt_number}:queued:{coding_job_id}",
            event_type="attempt_queued",
            phase="queue",
            state="queued",
            summary="不可變執行計畫已排入佇列",
            details={
                "coding_run_id": run_id,
                "staging_job_id": staging_job_id,
                "coding_job_id": coding_job_id,
            },
            source_kind="coding_run",
            source_id=str(run_id),
            occurred_at=timestamp,
            recorded_at=timestamp,
        )
        return run_id, staging_job_id, coding_job_id

    def finalize_engineering_task_approval_plan(
        self,
        *,
        task_id: str,
        approval_id: int,
        project: str,
        runner_server: str,
        instruction: str,
        base_commit: str,
        project_version_id: str,
        validation_target: Optional[str],
        worktree_path: str,
        staging_command: str,
        coding_command: str,
        approval_note: Optional[str],
        decision_actor_id: Optional[str],
        decision_mechanism: str,
    ) -> tuple[int, int, int]:
        """Atomically approve and publish a complete immutable execution plan.

        No scheduler-visible owner Job may exist while the approval is pending.
        A process crash therefore leaves either the original pending request and
        no execution rows, or an approved task with its run, dependency edge and
        both Jobs fully linked.  SQLite rolls every intermediate insert back.
        """

        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            approval_row = cur.fetchone()
            if (
                approval_row is None
                or approval_row["kind"] != "coding_task"
                or approval_row["status"] != "pending"
            ):
                raise ValueError("engineering task approval is not pending")

            cur.execute("SELECT * FROM engineering_tasks WHERE id = ?", (task_id,))
            task_row = cur.fetchone()
            if (
                task_row is None
                or task_row["approval_id"] != approval_id
                or task_row["status"] != "pending_approval"
                or task_row["project_name"] != project
                or task_row["runner_server"] != runner_server
                or task_row["project_version_id"] != project_version_id
                or task_row["base_commit"].lower() != base_commit.lower()
                or task_row["instruction"] != instruction
            ):
                raise ValueError("engineering task contract is not finalizable")

            # Partial owner rows can only come from an older/broken planner.
            # Do not adopt them: they may already have been observed by a
            # scheduler and cannot be proven equivalent to this atomic plan.
            cur.execute(
                "SELECT 1 FROM coding_runs WHERE engineering_task_id = ? LIMIT 1",
                (task_id,),
            )
            if cur.fetchone() is not None:
                raise ValueError("engineering task already has a partial coding run")
            cur.execute(
                "SELECT 1 FROM jobs WHERE engineering_task_id = ? LIMIT 1",
                (task_id,),
            )
            if cur.fetchone() is not None:
                raise ValueError("engineering task already has partial jobs")

            run_id, staging_job_id, coding_job_id = (
                self._insert_engineering_task_attempt_rows(
                    cur,
                    task_id=task_id,
                    attempt_number=1,
                    approval_id=approval_id,
                    project=project,
                    runner_server=runner_server,
                    instruction=instruction,
                    base_commit=base_commit,
                    project_version_id=project_version_id,
                    validation_target=validation_target,
                    worktree_path=worktree_path,
                    staging_command=staging_command,
                    coding_command=coding_command,
                    decision_actor_id=decision_actor_id,
                    timestamp=timestamp,
                )
            )
            final_note = approval_note or (
                f"已建立 immutable coding 任務 #{coding_job_id}（Runner "
                f"{runner_server}，coding_run #{run_id}，staging Job "
                f"#{staging_job_id}）"
            )

            cur.execute(
                """
                UPDATE approvals
                SET status = 'approved', decided_at = ?, note = ?,
                    decision_actor_id = ?, decision_mechanism = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    timestamp,
                    final_note,
                    decision_actor_id,
                    decision_mechanism,
                    approval_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("engineering task approval changed during finalization")

        return run_id, staging_job_id, coding_job_id

    def finalize_engineering_task_retry_plan(
        self,
        *,
        task_id: str,
        approval_id: int,
        attempt_number: int,
        project: str,
        runner_server: str,
        instruction: str,
        base_commit: str,
        project_version_id: str,
        validation_target: Optional[str],
        worktree_path: str,
        staging_command: str,
        coding_command: str,
        approval_note: Optional[str],
        decision_actor_id: Optional[str],
        decision_mechanism: str,
    ) -> tuple[int, int, int]:
        """Atomically approve an ``engineering_task_retry`` and publish attempt N.

        Mirrors ``finalize_engineering_task_approval_plan`` (attempt 1's atomic
        plan builder) but scopes the "no partial owner rows" guard to this
        exact ``(task_id, attempt_number)`` pair instead of the whole task, so
        a prior terminal attempt does not block a later one.  The caller
        already re-validated the retry contract against current DB/config
        state; this method re-checks only what a concurrent writer could have
        changed since that revalidation.
        """

        if attempt_number < 2:
            raise ValueError("engineering task retry must target attempt_number >= 2")
        timestamp = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            approval_row = cur.fetchone()
            if (
                approval_row is None
                or approval_row["kind"] != "engineering_task_retry"
                or approval_row["status"] != "pending"
            ):
                raise ValueError("engineering task retry approval is not pending")

            cur.execute("SELECT * FROM engineering_tasks WHERE id = ?", (task_id,))
            task_row = cur.fetchone()
            if (
                task_row is None
                or task_row["project_name"] != project
                or task_row["runner_server"] != runner_server
                or task_row["project_version_id"] != project_version_id
                or task_row["base_commit"].lower() != base_commit.lower()
                or task_row["instruction"] != instruction
                or task_row["status"] not in _CODING_RUN_TERMINAL_STATUSES
            ):
                raise ValueError("engineering task retry contract is not finalizable")

            # Scoped by (task_id, attempt_number): unlike the attempt-1 planner,
            # earlier terminal attempts are expected to already have rows.
            cur.execute(
                """
                SELECT 1 FROM coding_runs
                WHERE engineering_task_id = ? AND attempt_number = ? LIMIT 1
                """,
                (task_id, attempt_number),
            )
            if cur.fetchone() is not None:
                raise ValueError("engineering task already has this attempt's coding run")
            cur.execute(
                """
                SELECT 1 FROM jobs
                WHERE engineering_task_id = ? AND engineering_attempt_number = ? LIMIT 1
                """,
                (task_id, attempt_number),
            )
            if cur.fetchone() is not None:
                raise ValueError("engineering task already has this attempt's jobs")

            run_id, staging_job_id, coding_job_id = (
                self._insert_engineering_task_attempt_rows(
                    cur,
                    task_id=task_id,
                    attempt_number=attempt_number,
                    approval_id=approval_id,
                    project=project,
                    runner_server=runner_server,
                    instruction=instruction,
                    base_commit=base_commit,
                    project_version_id=project_version_id,
                    validation_target=validation_target,
                    worktree_path=worktree_path,
                    staging_command=staging_command,
                    coding_command=coding_command,
                    decision_actor_id=decision_actor_id,
                    timestamp=timestamp,
                )
            )
            final_note = approval_note or (
                f"已核准 attempt #{attempt_number} retry：coding 任務 "
                f"#{coding_job_id}（Runner {runner_server}，coding_run #{run_id}，"
                f"staging Job #{staging_job_id}）"
            )
            cur.execute(
                """
                UPDATE approvals
                SET status = 'approved', decided_at = ?, note = ?,
                    decision_actor_id = ?, decision_mechanism = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    timestamp,
                    final_note,
                    decision_actor_id,
                    decision_mechanism,
                    approval_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    "engineering task retry approval changed during finalization"
                )

        return run_id, staging_job_id, coding_job_id

    def engineering_task_job_is_approved(self, job: Job) -> bool:
        """Owner Jobs are dispatchable only after their parent approval commits."""

        if job.engineering_task_id is None:
            return True
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM engineering_tasks AS task
                JOIN approvals AS approval ON approval.id = task.approval_id
                WHERE task.id = ? AND approval.status = 'approved'
                """,
                (job.engineering_task_id,),
            )
            return cur.fetchone() is not None

    def refresh_engineering_task_status_from_jobs(self, task_id: str) -> Optional[str]:
        """Derive the parent status from its durable staging/coding owner Jobs."""

        task = self.get_engineering_task(task_id)
        if task is None or task.status == "rejected":
            return task.status if task is not None else None
        staging = self.get_engineering_task_job(task_id, "staging", 1)
        coding = self.get_engineering_task_job(task_id, "coding", 1)
        if staging is None or coding is None:
            return task.status

        if staging.status not in VALID_STATUSES or coding.status not in VALID_STATUSES:
            status = "unknown"
        elif staging.status == "running":
            status = "staging"
        elif staging.status in {"failed", "blocked"}:
            status = "staging_failed" if staging.status == "failed" else "blocked"
        elif staging.status == "cancelled":
            status = "cancelled"
        elif staging.status != "done":
            status = "queued"
        elif coding.status == "running":
            status = "running"
        elif coding.status == "done":
            status = "finalizing"
        elif coding.status in {"failed", "blocked", "cancelled"}:
            status = coding.status
        else:
            status = "queued"

        if task.status != status:
            self.update_engineering_task(task_id, status=status)
        return status

    # ---- datasets CRUD（階段 3）------------------------------------------

    def insert_dataset(
        self,
        name: str,
        version: str,
        size_bytes: int,
        source_path: str,
        manifest: dict,
        card: Optional[dict] = None,
        sync_mode: str = "packed",
    ) -> None:
        """同一個 (name, version) 重複註冊會丟 sqlite3.IntegrityError，呼叫端轉
        400——版本是一級概念（原規格 5.2），要更新內容請註冊新版本，不是覆蓋。

        階段 16（PLAN.md Q 節）：`card` 選填（`None` = 沒有資料卡，見
        `app/datasets.py` 的 `build_dataset_card()`）；`sync_mode` 選填，
        預設 `'packed'`（Q.1），本批只存取欄位、不使用其邏輯（見 Dataset
        dataclass 的欄位註解）。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO datasets
                    (name, version, size_bytes, source_path, manifest, created_at, card, sync_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    version,
                    size_bytes,
                    source_path,
                    json.dumps(manifest),
                    now_iso(),
                    json.dumps(card) if card is not None else None,
                    sync_mode,
                ),
            )

    def get_dataset(self, name: str, version: str) -> Optional[Dataset]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM datasets WHERE name = ? AND version = ?", (name, version)
            )
            row = cur.fetchone()
            return Dataset.from_row(row) if row else None

    def list_datasets(self) -> list[Dataset]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM datasets ORDER BY name ASC, version ASC")
            return [Dataset.from_row(r) for r in cur.fetchall()]

    def update_dataset_card(self, name: str, version: str, card: dict) -> None:
        """階段 16（PLAN.md Q.3）：整份 card dict 覆蓋寫入（不是逐欄
        UPDATE）——呼叫端（`app/main.py` 的 PATCH
        `/datasets/{name}/{version}/card`）負責組出完整的新 card（含保留
        原 `created_at`、更新 `updated_at`），這裡只管寫入序列化後的
        JSON。呼叫端應先 `get_dataset()` 確認資料集存在（同既有 CRUD
        慣例，這裡不檢查、不報錯，資料集不存在時是 no-op）。"""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE datasets SET card = ? WHERE name = ? AND version = ?",
                (json.dumps(card), name, version),
            )

    # ---- dataset_cache CRUD（階段 3：快取地圖）----------------------------

    def upsert_dataset_cache(self, server: str, dataset: str, version: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dataset_cache (server, dataset, version, registered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(server, dataset, version)
                DO UPDATE SET registered_at = excluded.registered_at
                """,
                (server, dataset, version, now_iso()),
            )

    def delete_dataset_cache(self, server: str, dataset: str, version: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM dataset_cache WHERE server = ? AND dataset = ? AND version = ?",
                (server, dataset, version),
            )

    def is_dataset_cached(self, server: str, dataset: str, version: str) -> bool:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM dataset_cache
                WHERE server = ? AND dataset = ? AND version = ? LIMIT 1
                """,
                (server, dataset, version),
            )
            return cur.fetchone() is not None

    def is_dataset_cached_anywhere(self, dataset: str, version: str) -> bool:
        """某個資料集＠版本是否已經同步到**任何一台**機器（不指定 server）。
        「自動」派工模式下用來判斷：這個訓練任務需要的資料集哪台機器都
        沒有，會提醒使用者（見 `app/approvals.py` 的 warning 機制），
        因為 `has_dataset` 資格過濾之後這種任務會一直卡在 queued。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dataset_cache WHERE dataset = ? AND version = ? LIMIT 1",
                (dataset, version),
            )
            return cur.fetchone() is not None

    def list_dataset_cache(self, server: Optional[str] = None) -> list[DatasetCacheEntry]:
        with self.cursor() as cur:
            if server:
                cur.execute(
                    "SELECT * FROM dataset_cache WHERE server = ? ORDER BY dataset ASC, version ASC",
                    (server,),
                )
            else:
                cur.execute("SELECT * FROM dataset_cache ORDER BY server ASC, dataset ASC")
            return [DatasetCacheEntry.from_row(r) for r in cur.fetchall()]

    # ---- 輔助查詢：setup 任務是否已在某機完成過（階段 3）--------------------

    def has_completed_setup(self, project: str, server: str) -> bool:
        """某機是否已經成功跑過某專案的 setup 任務（type='setup' 且 status='done'）。

        用既有 `jobs` 表歷史紀錄判斷「機器是否已經跑過這個專案」，取代對工作機
        做即時 SSH 目錄探測——這樣建立核准請求時不需要 SSH（見
        app/datasets.py 的設計說明），且不需要為此另外新增一張表。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM jobs
                WHERE type = 'setup' AND project = ? AND server = ? AND status = 'done'
                LIMIT 1
                """,
                (project, server),
            )
            return cur.fetchone() is not None

    # ---- coding_runs CRUD（階段 13，PLAN.md N.6：Codex Worker v2）---------

    #: `update_coding_run()` 允許更新的欄位白名單，比照 `update_job`／
    #: `update_project` 的既有 whitelist 慣例（白名單外一律 raise
    #: ValueError）。刻意排除 `approval_id`/`project`/`runner_server`/
    #: `instruction`/`created_at`——這些是建立時就定案、不該被之後的
    #: dispatch/回填流程改動的欄位。
    _CODING_RUN_UPDATE_FIELDS = {
        "job_id",
        "base_branch",
        "base_commit",
        "result_branch",
        "result_commit",
        "worktree_path",
        "bundle_path",
        "validation_target",
        "codex_version",
        "status",
        "test_command",
        "test_exit_code",
        "started_at",
        "finished_at",
        "error_message",
    }

    def insert_coding_run(
        self,
        *,
        approval_id: int,
        project: str,
        runner_server: str,
        instruction: str,
        job_id: Optional[int] = None,
        base_branch: Optional[str] = None,
        base_commit: Optional[str] = None,
        result_branch: Optional[str] = None,
        result_commit: Optional[str] = None,
        worktree_path: Optional[str] = None,
        bundle_path: Optional[str] = None,
        validation_target: Optional[str] = None,
        codex_version: Optional[str] = None,
        status: str = "queued",
        test_command: Optional[str] = None,
        test_exit_code: Optional[int] = None,
        engineering_task_id: Optional[str] = None,
        project_version_id: Optional[str] = None,
        base_binding: str = "legacy_unpinned",
        attempt_number: Optional[int] = None,
    ) -> int:
        if base_binding not in VALID_CODING_BASE_BINDINGS:
            raise ValueError(f"invalid coding base binding: {base_binding}")
        if base_binding == "project_version_pinned":
            if not all(
                (
                    engineering_task_id,
                    project_version_id,
                    base_commit,
                    attempt_number is not None and attempt_number > 0,
                )
            ):
                raise ValueError("pinned coding run requires task/version/base/attempt")
        elif any(
            value is not None
            for value in (engineering_task_id, project_version_id, attempt_number)
        ):
            raise ValueError("legacy coding run cannot claim engineering task binding")
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO coding_runs
                    (approval_id, job_id, project, runner_server, instruction,
                     base_branch, base_commit, result_branch, result_commit,
                     worktree_path, bundle_path, validation_target, codex_version,
                     status, test_command, test_exit_code, engineering_task_id,
                     project_version_id, base_binding, attempt_number, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    job_id,
                    project,
                    runner_server,
                    instruction,
                    base_branch,
                    base_commit,
                    result_branch,
                    result_commit,
                    worktree_path,
                    bundle_path,
                    validation_target,
                    codex_version,
                    status,
                    test_command,
                    test_exit_code,
                    engineering_task_id,
                    project_version_id,
                    base_binding,
                    attempt_number,
                    now_iso(),
                ),
            )
            return cur.lastrowid

    def get_coding_run(self, coding_run_id: int) -> Optional[CodingRun]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM coding_runs WHERE id = ?", (coding_run_id,))
            row = cur.fetchone()
            return CodingRun.from_row(row) if row else None

    def get_coding_run_by_job_id(self, job_id: int) -> Optional[CodingRun]:
        """依 `jobs.id` 找對應的 `coding_runs` 一列（PLAN.md N.6 回填）：
        `approve()` 建立 coding_run 時已經把 `job_id` 寫回去（見
        `update_coding_run(run_id, job_id=job.id)`），這裡只是反查。v1
        遺留的 coding job（沒有對應 coding_run）回傳 `None`，呼叫端
        （`app.jobfinish`）應該跳過回填、不當成錯誤。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM coding_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
            row = cur.fetchone()
            return CodingRun.from_row(row) if row else None

    def get_coding_run_by_engineering_attempt(
        self, task_id: str, attempt_number: int
    ) -> Optional[CodingRun]:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM coding_runs
                WHERE engineering_task_id = ? AND attempt_number = ?
                LIMIT 1
                """,
                (task_id, attempt_number),
            )
            row = cur.fetchone()
            return CodingRun.from_row(row) if row else None

    def list_coding_runs(
        self,
        status: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 50,
    ) -> list[CodingRun]:
        """新到舊（`id DESC`），比照其他「最近 N 筆」查詢的既有慣例。"""
        query = "SELECT * FROM coding_runs WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(query, params)
            return [CodingRun.from_row(r) for r in cur.fetchall()]

    def update_coding_run(self, coding_run_id: int, **fields: Any) -> None:
        if not fields:
            return
        invalid = set(fields) - self._CODING_RUN_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"invalid coding_run field(s): {sorted(invalid)}")
        existing = self.get_coding_run(coding_run_id)
        if "base_commit" in fields:
            if (
                existing is not None
                and existing.base_binding == "project_version_pinned"
            ):
                if fields["base_commit"] != existing.base_commit:
                    raise ValueError("pinned coding_run base_commit is immutable")
                fields.pop("base_commit")
                if not fields:
                    return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [coding_run_id]
        result_event: Optional[dict[str, Any]] = None
        with self.cursor() as cur:
            cur.execute(f"UPDATE coding_runs SET {cols} WHERE id = ?", values)
            result_status = fields.get("status")
            if (
                existing is not None
                and existing.engineering_task_id is not None
                and result_status is not None
            ):
                timestamp = fields.get("finished_at") or now_iso()
                cur.execute(
                    """
                    UPDATE engineering_tasks
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (result_status, timestamp, existing.engineering_task_id),
                )
                summary = {
                    "done": "結果已驗證並完成收集",
                    "no_changes": "結果已驗證；沒有產生程式變更",
                    "failed": "結果收集完成，執行結果為失敗",
                    "secret_violation": "結果因安全檢查違規而拒絕",
                    "path_policy_violation": "結果因路徑政策違規而拒絕",
                }.get(result_status, "Coding Runner 結果已完成收集")
                result_event = {
                    "task_id": existing.engineering_task_id,
                    "attempt_number": existing.attempt_number,
                    "event_key": (
                        f"coding-run:{coding_run_id}:result:{result_status}:"
                        f"{timestamp}"
                    ),
                    "event_type": "result_collected",
                    "phase": "complete",
                    "state": result_status,
                    "summary": summary,
                    "details": {
                        "coding_run_id": coding_run_id,
                        "job_id": existing.job_id,
                        "result_status": result_status,
                    },
                    "source_kind": "coding_run",
                    "source_id": str(coding_run_id),
                    "occurred_at": fields.get("finished_at"),
                }

        # Visibility is explicitly presentation-only.  Commit canonical
        # CodingRun + parent state first, then best-effort the idempotent event
        # in its own transaction.  A corrupt/conflicting journal row must never
        # roll the terminal result back and trigger repeated result pulls.
        if result_event is not None:
            try:
                self.append_engineering_task_event(**result_event)
            except Exception:  # noqa: BLE001 - canonical state already committed
                pass

    # ---- experiment_records CRUD（專案詳情頁：實驗紀錄時間軸）--------------

    #: `update_experiment_record()` 允許更新的欄位白名單，比照
    #: `_CODING_RUN_UPDATE_FIELDS` 的既有慣例——刻意排除 `project`/`author`/
    #: `job_id`/`coding_run_id`/`created_at`：這些是建立時就定案的欄位，
    #: `author` 尤其不該事後被改成別的身分（見 `POST/PATCH
    #: /projects/{name}/records` 的稽核設計）。
    _EXPERIMENT_RECORD_UPDATE_FIELDS = {"title", "content", "kind"}

    def insert_experiment_record(
        self,
        project: str,
        content: str,
        kind: str = "note",
        title: Optional[str] = None,
        author: str = "user",
        job_id: Optional[int] = None,
        coding_run_id: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> int:
        if kind not in VALID_RECORD_KINDS:
            raise ValueError(f"invalid experiment record kind: {kind}")
        _validate_record_author(author)
        now = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO experiment_records
                    (project, kind, title, content, author, job_id, coding_run_id,
                     extra, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    kind,
                    title,
                    content,
                    author,
                    job_id,
                    coding_run_id,
                    json.dumps(extra or {}),
                    now,
                    now,
                ),
            )
            return cur.lastrowid

    def get_experiment_record(self, record_id: int) -> Optional[ExperimentRecord]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM experiment_records WHERE id = ?", (record_id,))
            row = cur.fetchone()
            return ExperimentRecord.from_row(row) if row else None

    def list_experiment_records(
        self,
        project: str,
        q: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 20,
        before_ts: Optional[str] = None,
    ) -> list[ExperimentRecord]:
        """新到舊（`created_at DESC`，同一微秒內以 `id DESC` 當 tie-break）。
        `q` 對 `title`/`content` 做簡單 LIKE 搜尋；`before_ts` 是游標，
        `created_at < before_ts`——`app/records.py` 的 `build_timeline()`
        對這個游標選字串比較（而不是額外開一個 id 游標）的取捨有完整說明：
        `now_iso()` 含微秒，字典序比較足夠安全，碰撞機率可忽略。"""
        query = "SELECT * FROM experiment_records WHERE project = ?"
        params: list[Any] = [project]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        if q:
            query += " AND (title LIKE ? OR content LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])
        if before_ts:
            query += " AND created_at < ?"
            params.append(before_ts)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(query, params)
            return [ExperimentRecord.from_row(r) for r in cur.fetchall()]

    def update_experiment_record(self, record_id: int, **fields: Any) -> None:
        """白名單 `title`/`content`/`kind`（比照 `update_coding_run()`），
        自動更新 `updated_at`（比照 `update_dataset_card()` 一帶「補登/更新
        要更新時間戳」的既有慣例）。`kind` 有帶時一樣要通過
        `VALID_RECORD_KINDS` 驗證。"""
        if not fields:
            return
        invalid = set(fields) - self._EXPERIMENT_RECORD_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"invalid experiment record field(s): {sorted(invalid)}")
        if "kind" in fields and fields["kind"] not in VALID_RECORD_KINDS:
            raise ValueError(f"invalid experiment record kind: {fields['kind']}")
        fields = dict(fields)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [record_id]
        with self.cursor() as cur:
            cur.execute(f"UPDATE experiment_records SET {cols} WHERE id = ?", values)

    def delete_experiment_record(self, record_id: int) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM experiment_records WHERE id = ?", (record_id,))
