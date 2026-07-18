"""核准流：`approvals` 表狀態機（PLAN.md C 節）。

核准模型：獨立 `approvals` 表，不是在 jobs 上加 pending 狀態。理由：核准
對象不只「排任務」——停止任務（本階段）、同步計畫（階段 3）、聊天
enqueue 卡片（階段 5）都要走同一套核准，用一張表統一。

- kind=enqueue：payload 為 job 建立欄位；核准 → 呼叫既有 `jobqueue.enqueue_job`
  入列（含自動掛依賴，階段 3 起 payload 可含 sync 計畫）。
- kind=stop：payload 為 `{job_id}`；核准 → SSH `tmux kill-session -t job_{id}`
  → 任務標 `cancelled`、抓 log 尾、寫稽核 `stop`。
- 階段 8（第一批，PLAN.md I 節）新增三個 kind：
  - kind=inventory_scan：payload 為 `{server, project_roots}`；核准 →
    呼叫 `app.inventory.scan_server()`（這裡才真的發生 SSH）→
    `app.inventory.prune_nested_candidates()` 剔除巢狀噪音（PLAN.md
    P.1.2，頂層專案勝出）→ upsert `project_candidates`、寫稽核
    `inventory_scan`（`candidates_found` 是剪枝後的數量）。
  - kind=import_project：payload 含 candidate_id 與匯入參數；核准 → 寫
    `projects`（含 summary/dataset_mode）→ 寫 `project_instances` →
    candidate.status 改 imported、寫稽核 `import_project`。
  - kind=ignore_project_candidate：payload 為 `{candidate_id}`；核准 →
    candidate.status 改 ignored、寫稽核 `ignore_project_candidate`。
- 階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）新增一個 kind：
  - kind=ignore_nested_candidates：payload 為 `{candidate_ids, items}`
    （`items` 供核准卡逐筆顯示 path/name_guess）；核准 → 逐一重查
    candidate_ids 當下狀態，仍是 pending 才改 ignored（已被單獨處理過的
    跳過，不報錯，見 `approve()` 對應分支說明）、寫稽核
    `candidates_ignore_nested`。批次清理巢狀噪音改走核准（維持「候選整理
    走核准」的一致性），一次核准全部生效，不用逐一點好幾十次
    `ignore_project_candidate`。
- 階段 8（第二批，PLAN.md I 節）新增四個 Web Server Management kind：
  - kind=server_add：payload 為一份完整 server 設定（已經過
    `validate_server_config()`／`normalize_server_config()`）；核准 →
    `backup_servers_yaml()` → append → `write_servers_yaml_atomically()` →
    `reload_server_config_if_supported()` → 寫稽核 `server_add`。
  - kind=server_update：payload 為 `{name, updates}`；核准 → 合併既有設定
    → 同上流程落地。
  - kind=server_disable：payload 為 `{name}`；**核准前**重新檢查一次該
    機器是否有 running job——有 → approval 標 `rejected`，不改
    servers.yaml；沒有 → 設 `enabled=false` 後同上流程落地。
  - kind=server_delete：同 server_disable 的檢查與落地（第一版以停用取代
    真刪除），note 註明原因，audit action 仍記 `server_delete`。
- 階段 12（PLAN.md M 節）：新增 kind=apply_patch——AI 改碼層次一（讀檔＋
  diff 核准卡）。使用者明確要求越過原規格「只建議不改碼」的紅線，受控
  方式＝每個 diff 人工核准、改動只在新 git branch 上、永不 push、diff 全文
  進稽核。payload 為 `{project, server, diff, description}`；核准 → 全部
  SSH：確認是 git repo → 記原 branch → diff 寫檔 → `git apply --check` →
  失敗則 rejected 不留改動，成功則 `checkout -b ai-patch-{id}` →
  `git apply --index` → commit → 寫稽核 `apply_patch`（見 `approve()`
  docstring 的完整六步驟）。**這個 kind 永遠不會被 `maybe_auto_approve()`
  自動核准**（該函式的 kind 白名單只有 "enqueue"/"stop"，`apply_patch` 不
  在其中——人必須逐個看過 diff 才能核准）。
- 階段 13（PLAN.md N 節，2026-07-10 使用者裁定改版為 **Codex Worker
  v2：Central Codex Runner**，v1 全面廢止）：kind=coding_task——AI 改碼
  層次二（Codex Worker）。跟 apply_patch 的差異：這裡不是人先寫好 diff
  再核准套用，而是核准一段**自然語言需求**（instruction），核准後才由
  `codex exec`（OpenAI Codex CLI，只在**唯一一台**由 `.env`
  `CODEX_RUNNER_SERVER` 指定的 Central Codex Runner 上安裝並登入，其他
  工作機不需要 Codex 帳號）在一個新 git worktree（獨立 branch
  `ai-task-{id}`）上實際執行、自行讀寫檔案/跑指令/視 `CODEX_NETWORK_ACCESS`
  決定能否連網。受控方式：instruction 需人工核准、永遠在獨立 worktree／
  branch、永不 push external origin、永不直接修改正式 project instance；
  執行時要跑的 `cmd.sh` 由程式**確定性組裝**
  （`build_coding_task_script()`，模型絕不直接寫 shell）——instruction
  本身**不進 shell 字串**，改用獨立 `instruction.txt` 檔＋stdin 餵給
  `codex exec`（`build_codex_instruction_file()` 組出全文，含 guardrail
  前言）。核准時建立一筆 `coding_runs` 記錄（`app.db.insert_coding_run()`
  ，PLAN.md N.6）追蹤這次任務的完整生命週期，`jobqueue.enqueue_job()`
  派一個 `type="coding"`、**pin 到 CODEX_RUNNER_SERVER**（不接受呼叫端指定
  其他機器）的任務（**不是像 apply_patch 那樣自己動手 SSH 執行實際改碼**
  ——coding 任務要跑很久，交給既有 tmux/哨兵/log tail/卡死偵測基礎設施自然
  適用；`approve()` 只 SSH 寫 `instruction.txt` 到 Runner 上的 task 目錄）。
  payload 為 `{project, instruction, base_branch, validation_target,
  runner_server, source_kind, source}`（詳見
  `request_coding_task_approval()`／`build_coding_task_script()` 的
  docstring）。**這個 kind 也永遠不會被 `maybe_auto_approve()` 自動核准**
  ——理由同 apply_patch，agent 在這裡的行動力比 apply_patch 更大（不只是
  套用一份人已經看過的 diff），人必須逐個核准 instruction。
- 階段 15 Phase B（PLAN.md P.2.1 節）：新增 kind=git_init——把一個尚未受
  git 管理的 project_instance 就地初始化成 git repo。payload 為
  `{project, server, extra_ignores, gitignore}`（`gitignore` 是
  `build_default_gitignore()` 組出的全文，核准卡片 `<pre>` 顯示）；核准 →
  全部 SSH：雙重防線再驗一次非 git repo → 寫 `.gitignore` → `git init` →
  `git add -A` → size guard（staged 內容超過 500MB 直接 `rm -rf .git` 並
  rejected，不留下半初始化的 repo）→ commit → 記 HEAD、更新
  `project_instances` 的 git_branch/git_commit → 稽核 `git_init`（見
  `approve()` 的 git_init 分支完整八步驟）。**這個 kind 也永遠不會被
  `maybe_auto_approve()` 自動核准**（白名單只認 "enqueue"/"stop"，天然
  排除）。
- 階段 15 Phase C（PLAN.md P.3 節）：新增 kind=project_deploy——把中央
  hub（`app.hub`）目前的某個 ref 部署成另一台機器上一個全新的
  `project_instance`。payload 為 `{project, target_server, dest_path, ref,
  hub_head}`（見 `app.hub.request_project_deploy_approval()` 完整驗證
  順序，來源固定是 Server A hub，尚未 `hub-sync` 過的專案不能部署）；
  核准 → inline 執行（比照 git_init，不像 coding_task 那樣派背景 job）
  五步：1) 本地 `git bundle create --git-dir={hub} ... {ref}` ＋
  `bundle verify`；2) `app.hub.build_deploy_push_command()` 組的 rsync
  把 bundle 推到目標機 `deploy_bundles/{approval_id}.bundle`；3) 目標機
  `mkdir -p` 目的地父目錄 → `git clone -b {ref} $HOME/deploy_bundles/
  {id}.bundle {dest}` → `git -C {dest} remote remove origin`；4) 記
  `HEAD` → `db.insert_project_instance()` 建立全新 instance；5) 稽核
  `project_deploy`。任何一步失敗 → rejected，note 含失敗步驟＋stderr
  摘要＋清理指引，**已建立的部分留給人工檢查、不自動 `rm`**（見
  `approve()` 的 project_deploy 分支）。**這個 kind 也永遠不會被
  `maybe_auto_approve()` 自動核准**（白名單只認 "enqueue"/"stop"，天然
  排除）。

**危險指令在「建立核准請求」當下就直接拒絕**（400 + 稽核 reject），不建立
approval、不給核准機會（鐵律第 2 條）。inventory_scan 的禁止路徑檢查同理：
建立請求時與核准後真正掃描前都要各自檢查一次（雙重防線，見
`app/inventory.py` 模組 docstring）；server_add/server_update 的不合法設定
（`validate_server_config()` 失敗）同樣在建立請求當下就直接拒絕；
apply_patch 的 diff/路徑驗證（`InvalidApplyPatchRequestError`）同理；
coding_task 的 instruction 驗證（`InvalidCodingTaskRequestError`：專案/
Runner 設定/repo 來源/instruction 長度/base_branch 格式/validation_target）
也同理——**但 v2 不再對 instruction 組出來的指令做 `is_dangerous()` 掃描**
（v1 才有這一步；v2 的 instruction 全文只會進 `instruction.txt`＋stdin，
從不進 shell 字串，`cmd.sh` 本身是不含 instruction 內文的固定模板，
`is_dangerous()` 對這種確定性模板沒有意義，見
`request_coding_task_approval()` docstring）。`enqueue_job()` 仍會對組好
的 `cmd.sh` 本身跑一次 `is_dangerous()`（既有 enqueue 鐵律，適用所有
kind），但那不是 coding_task 專屬的第二道防線。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app import autoapprove
from app import audit as audit_module
from app.activity import ProjectInstanceResolutionError, resolve_project_instance, validate_rel_path
from app.audit import SYSTEM_AUDIT_ACTOR, append_audit, audit_actor_from_request_context, now_iso
from app.authorization import Action
from app.config import AppConfig, ServerConfig
from app.coding_agents import (
    CODEX_AGENT_PROVIDER_ID,
    CodingAgentTurnRequest,
    get_coding_agent_provider,
    require_coding_agent_provider,
)
from app.datasets import (
    LOCAL_SERVER,
    build_dispatch_plan,
    build_setup_script,
    build_sync_script,
    dataset_remote_dir,
    validate_name_component,
)
from app.db import (
    VALID_DATASET_MODES,
    VALID_DISPATCH_POLICY_STATUSES,
    VALID_RUN_PROFILE_STATUSES,
    Approval,
    Database,
    ProjectCandidate,
    make_candidate_id,
)
from app.engineering_tasks import (
    InvalidEngineeringTaskRequestError,
    build_engineering_bundle_create_command,
    build_engineering_bundle_verify_command,
    build_engineering_staging_push_command,
    inspect_engineering_result_file,
    inspect_hub_project_version,
    local_engineering_instruction_relpath,
    local_engineering_path_policy_relpath,
    local_engineering_path_verifier_relpath,
    normalize_engineering_task_spec,
    reject_engineering_raw_credentials,
    remote_engineering_bundle_path,
    render_engineering_task_instruction,
)
from app.engineering_path_policy import (
    CONTRACT_VERSION as ENGINEERING_TASK_CONTRACT_V2,
    EngineeringPathPolicyError,
    build_engineering_path_policy,
    engineering_path_verifier_source,
    render_engineering_path_policy_file,
    validate_engineering_path_policy,
    validate_engineering_path_verifier_contract,
)
from app.engineering_validation import (
    engineering_validation_job_contract_failure,
    record_engineering_validation_contract_refusal,
)
from app.hub import build_deploy_push_command, hub_repo_path, local_deploy_bundle_path
from app.identity import (
    ActorType,
    ProjectRole,
    RequestContext,
    generate_service_token,
)
from app.inventory import is_forbidden_root, prune_nested_candidates, scan_server
from app.jobqueue import (
    CANCELLED,
    DangerousCommandError,
    build_log_tail_command,
    engineering_coding_job_runner_contract_matches,
    engineering_job_command_contract_matches,
    engineering_job_command_audit_fields,
    engineering_job_failure_category,
    engineering_staging_job_contract_matches,
    enqueue_job,
    record_engineering_job_execution_contract_mismatch,
    safe_persisted_engineering_log_tail,
)
from app.results import build_bundle_push_command, local_result_dir
from app.scheduler import pick_codex_runner
from app.security import is_dangerous
from app.provisioning import (
    BOOTSTRAP_SCRIPT_VERSION,
    bootstrap_script_sha256,
    run_server_bootstrap,
    validate_bootstrap_components,
)
from app.server_config import (
    backup_servers_yaml,
    load_servers_config,
    normalize_server_config,
    reload_server_config_if_supported,
    validate_server_config,
    write_servers_yaml_atomically,
)

logger = logging.getLogger(__name__)


class ApprovalNotFoundError(Exception):
    """指定的 approval id 不存在。"""


class ApprovalNotPendingError(Exception):
    """approval 已經被核准或拒絕過，不能重複決定。"""


class IdentityAdministrationDisabledError(Exception):
    """Identity administration is disabled by the rollback switch."""


class IdentityTargetNotFoundError(Exception):
    """A service account, token, actor, project, or membership is missing."""


class InvalidIdentityAdminRequestError(ValueError):
    """An identity lifecycle request is malformed or currently invalid."""


class RunProfileAdministrationDisabledError(Exception):
    """D5 Run Profile v1 is disabled by its rollback switch (`RUN_PROFILE_V1_ENABLED`)."""


class InvalidRunProfileRequestError(ValueError):
    """A Run Profile lifecycle request is malformed or currently invalid."""


class DispatchPolicyAdministrationDisabledError(Exception):
    """Goal 2 Slice 3 Dispatch Policy v1 is disabled by its rollback switch
    (`DISPATCH_POLICY_V1_ENABLED`)."""


class InvalidDispatchPolicyRequestError(ValueError):
    """A Dispatch Policy lifecycle request is malformed or currently invalid."""


class ServerBootstrapDisabledError(Exception):
    """Goal 3 Phase B server bootstrap is disabled by its rollback switch
    (`SERVER_BOOTSTRAP_V1_ENABLED`)."""


class InvalidServerBootstrapRequestError(ValueError):
    """A server bootstrap request is malformed or currently invalid."""


class JobNotFoundError(Exception):
    """stop 核准指向的 job 不存在。"""


class JobNotRunningError(Exception):
    """只有 running 狀態的任務可以請求停止。"""


class ProjectNotFoundError(ValueError):
    """指定的專案不存在。"""


class ForbiddenScanRootError(ValueError):
    """`project_roots` 裡有任何一項命中 `app.inventory.is_forbidden_root()`
    ——建立核准請求當下直接拒絕，不建立 approval（鐵律第 2 條的延伸）。"""


class CandidateNotFoundError(Exception):
    """指定的 project candidate id 不存在。"""


class CandidateNotPendingError(Exception):
    """candidate 已經被匯入或忽略過，不能重複處理。"""


class NoNestedCandidatesError(ValueError):
    """階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）：
    `request_ignore_nested_candidates_approval()` 找不到任何「pending 且
    路徑位於其他非 ignored 候選之下」的候選——沒有東西可以清理，建立核准
    請求當下直接拒絕，不建立空的 approval（呼叫端轉 400）。"""


class ManualCandidateServerInvalidError(ValueError):
    """P.1.5（PLAN.md，2026-07-10 追加，Fable 定案）：`add_manual_candidate()`
    的 `server` 不存在於目前的 `server_configs` 或未啟用（`enabled=False`）
    ——建立當下直接拒絕，不落地任何候選（呼叫端轉 400）。"""


class ManualCandidatePathInvalidError(ValueError):
    """P.1.5：`add_manual_candidate()` 的 `path` 不合法（非絕對路徑／含
    `..` 片段），或唯讀 SSH 確認（`test -d`）判定該路徑不存在／不是目錄
    （呼叫端轉 400）。"""


class ManualCandidateDuplicateError(Exception):
    """P.1.5：`add_manual_candidate()` 發現 `server`+`path` 已經有候選（任何
    status，含 imported/ignored）——`existing` 帶現有候選，供呼叫端組
    409 detail（附現有候選 id 與 status）。"""

    def __init__(self, existing: ProjectCandidate):
        self.existing = existing
        super().__init__(f"候選已存在：id={existing.id} status={existing.status}")


class InvalidServerConfigError(ValueError):
    """`validate_server_config()` 判定不合法（見 `app/server_config.py`）
    ——建立核准請求當下直接拒絕，不建立 approval（鐵律第 2 條的延伸）。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("；".join(errors) or "server 設定不合法")


class ServerNotFoundError(Exception):
    """指定的 server 名稱不存在於目前設定。"""


class ServerRenameNotSupportedError(ValueError):
    """`updates` 內含 `name` 且與現有名稱不同——不支援改名（會破壞
    jobs.server／jobs.pin_server 等既有欄位引用）。"""


class InvalidApplyPatchRequestError(ValueError):
    """`request_apply_patch_approval()` 的驗證失敗（instance 不存在、diff
    空/過大/格式不像 unified diff、diff 目標路徑不合法）——建立核准請求
    當下直接拒絕，不建立 approval（鐵律第 2 條的延伸，比照
    `InvalidServerConfigError` 的模式）。"""


class InvalidGitInitRequestError(ValueError):
    """`request_git_init_approval()` 的驗證失敗（PLAN.md P.2.1）：instance
    不存在、該 instance 已經是 git repo（SSH 唯讀確認 `test -d {path}/
    .git`）、或 `extra_ignores` 內有項目不符合單行 pattern 字元集
    `[A-Za-z0-9._*/-]+`——建立核准請求當下直接拒絕，不建立 approval（鐵律
    第 2 條的延伸，比照 `InvalidApplyPatchRequestError` 的模式）。"""


class InvalidCodingTaskRequestError(ValueError):
    """`request_coding_task_approval()` 的驗證失敗（PLAN.md N 節 v2）：
    Codex 功能未設定、legacy `server` 參數與 Runner 不符、project 不存在、
    instruction 空/過長、base_branch 格式不合法、validation_target 不是
    已啟用的 server、或 N.3 三段式判定找不到可用的 repo 來源——建立核准
    請求當下直接拒絕，不建立 approval（鐵律第 2 條的延伸，比照
    `InvalidApplyPatchRequestError` 的模式）。"""


class InvalidEngineeringValidationRequestError(ValueError):
    """Structured worker-validation proposal is unsafe or no longer eligible."""


def approval_to_dict(approval: Approval) -> dict:
    """Approval -> JSON-serializable dict。`app/main.py`（REST API）與
    `app/chat.py`（聊天 approval_card 訊息）共用同一份轉換邏輯，避免兩處
    各自維護一份容易漂移的欄位清單。"""
    return {
        "id": approval.id,
        "kind": approval.kind,
        "payload": approval.payload,
        "status": approval.status,
        "created_at": approval.created_at,
        "decided_at": approval.decided_at,
        "note": approval.note,
        "requester_actor_id": approval.requester_actor_id,
        "decision_actor_id": approval.decision_actor_id,
        "decision_mechanism": approval.decision_mechanism,
    }


def _actor_id(context: Optional[RequestContext]) -> Optional[str]:
    return context.actor_id if context is not None else None


def _decision_mechanism(approved_by: str) -> str:
    """Map `approved_by` to the durable `decision_mechanism` value.

    Goal 2 Slice 5 (INV-APPROVAL-4b) extends this closed set with a third
    pass-through shape: `policy-{policy_id}-r{revision}`, recorded by
    `maybe_auto_decide_placement()` when a policy-scoped auto-decision goes
    through. This is additive — the two pre-existing mechanisms
    (`"web-direct"`, `"auto-rule-{N}"`) and the `"manual"` fallback are
    unchanged.
    """
    if approved_by == "web-direct":
        return approved_by
    if isinstance(approved_by, str) and re.fullmatch(r"auto-rule-\d+", approved_by):
        return approved_by
    if isinstance(approved_by, str) and re.fullmatch(r"policy-.+-r\d+", approved_by):
        return approved_by
    return "manual"


_SERVICE_TOKEN_SCOPES = frozenset(action.value for action in Action)
_IDENTITY_APPROVAL_KINDS = frozenset(
    {
        "service_account_create",
        "service_token_issue",
        "service_token_revoke",
        "project_membership_upsert",
        "project_membership_remove",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_service_token_scopes(scopes: object) -> list[str]:
    """Validate the exact closed action catalog and return stable unique scopes."""

    if not isinstance(scopes, list):
        raise InvalidIdentityAdminRequestError(
            "service token scopes must be a list"
        )
    if any(not isinstance(scope, str) or scope not in _SERVICE_TOKEN_SCOPES for scope in scopes):
        raise InvalidIdentityAdminRequestError(
            "service token scopes contain an unknown action"
        )
    return sorted(set(scopes))


def _normalize_future_expiry(expires_at: object) -> str:
    """Return a UTC ISO timestamp only when it is timezone-aware and future."""

    if not isinstance(expires_at, str) or not expires_at:
        raise InvalidIdentityAdminRequestError(
            "expires_at must be a timezone-aware future timestamp"
        )
    normalized = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        expiry = datetime.fromisoformat(normalized)
    except ValueError:
        raise InvalidIdentityAdminRequestError(
            "expires_at must be a timezone-aware future timestamp"
        ) from None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise InvalidIdentityAdminRequestError(
            "expires_at must be a timezone-aware future timestamp"
        )
    expiry = expiry.astimezone(timezone.utc)
    if expiry <= _utc_now():
        raise InvalidIdentityAdminRequestError("expires_at must be in the future")
    return expiry.isoformat()


def _normalize_optional_identity_text(value: object, *, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidIdentityAdminRequestError(f"{field} must be a string or null")
    normalized = value.strip()
    return normalized or None


def _service_account_by_name(db: Database, name: str):
    return next(
        (account for account in db.list_service_accounts() if account.name == name),
        None,
    )


def _require_active_service_account(db: Database, actor_id: object):
    if not isinstance(actor_id, str) or not actor_id:
        raise IdentityTargetNotFoundError("service account not found")
    account = db.get_service_account(actor_id)
    actor = db.get_actor(actor_id)
    if account is None or actor is None or actor.actor_type is not ActorType.SERVICE:
        raise IdentityTargetNotFoundError(f"service account {actor_id} not found")
    if actor.disabled_at is not None:
        raise ValueError(f"service account {actor_id} is disabled")
    return account, actor


def _insert_identity_approval(
    db: Database,
    *,
    kind: str,
    payload: dict[str, Any],
    audit_path: str,
    request_context: Optional[RequestContext],
) -> Approval:
    approval_id = db.insert_approval(
        kind=kind,
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": kind, "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_service_account_create_approval(
    db: Database,
    name: str,
    description: Optional[str] = None,
    *,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending service-account request without creating an actor."""

    if not isinstance(name, str) or not name.strip():
        raise InvalidIdentityAdminRequestError(
            "service account name must not be blank"
        )
    normalized_name = name.strip()
    if _service_account_by_name(db, normalized_name) is not None:
        raise InvalidIdentityAdminRequestError(
            f"service account name {normalized_name} already exists"
        )
    normalized_description = _normalize_optional_identity_text(
        description,
        field="description",
    )
    return _insert_identity_approval(
        db,
        kind="service_account_create",
        payload={
            "actor_id": str(uuid.uuid4()),
            "name": normalized_name,
            "description": normalized_description,
        },
        audit_path=audit_path,
        request_context=request_context,
    )


def request_service_token_issue_approval(
    db: Database,
    actor_id: str,
    *,
    label: Optional[str],
    scopes: list[str],
    expires_at: str,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending token request; no token ID or secret exists yet."""

    _require_active_service_account(db, actor_id)
    normalized_label = _normalize_optional_identity_text(label, field="label")
    payload = {
        "service_account_actor_id": actor_id,
        "label": normalized_label,
        "scopes": _normalize_service_token_scopes(scopes),
        "expires_at": _normalize_future_expiry(expires_at),
    }
    return _insert_identity_approval(
        db,
        kind="service_token_issue",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


def request_service_token_revoke_approval(
    db: Database,
    token_id: str,
    *,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending revocation for an existing, unrevoked token."""

    token = db.get_service_account_token(token_id) if isinstance(token_id, str) else None
    if token is None:
        raise IdentityTargetNotFoundError(f"service token {token_id} not found")
    if token.revoked_at is not None:
        raise InvalidIdentityAdminRequestError(
            f"service token {token_id} is already revoked"
        )
    return _insert_identity_approval(
        db,
        kind="service_token_revoke",
        payload={"token_id": token_id},
        audit_path=audit_path,
        request_context=request_context,
    )


def request_project_membership_upsert_approval(
    db: Database,
    project: str,
    actor_id: str,
    role: str,
    *,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending idempotent role upsert after validating current rows."""

    project_row = db.get_project(project) if isinstance(project, str) else None
    if project_row is None or project_row.id is None:
        raise IdentityTargetNotFoundError(f"project {project} not found")
    actor = db.get_actor(actor_id) if isinstance(actor_id, str) else None
    if actor is None:
        raise IdentityTargetNotFoundError(f"actor {actor_id} not found")
    if actor.disabled_at is not None:
        raise ValueError(f"actor {actor_id} is disabled")
    try:
        normalized_role = ProjectRole(role).value
    except (TypeError, ValueError):
        raise InvalidIdentityAdminRequestError(
            f"invalid project membership role: {role}"
        ) from None
    return _insert_identity_approval(
        db,
        kind="project_membership_upsert",
        payload={
            "project_id": project_row.id,
            "actor_id": actor_id,
            "role": normalized_role,
        },
        audit_path=audit_path,
        request_context=request_context,
    )


def request_project_membership_remove_approval(
    db: Database,
    project: str,
    actor_id: str,
    *,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending membership removal without deleting anything yet."""

    project_row = db.get_project(project) if isinstance(project, str) else None
    if project_row is None or project_row.id is None:
        raise IdentityTargetNotFoundError(f"project {project} not found")
    if db.get_actor(actor_id) is None:
        raise IdentityTargetNotFoundError(f"actor {actor_id} not found")
    if db.get_project_membership(project_row.id, actor_id) is None:
        raise IdentityTargetNotFoundError(
            f"membership {project_row.name}/{actor_id} not found"
        )
    return _insert_identity_approval(
        db,
        kind="project_membership_remove",
        payload={"project_id": project_row.id, "actor_id": actor_id},
        audit_path=audit_path,
        request_context=request_context,
    )


_RUN_PROFILE_APPROVAL_KINDS = {
    "run_profile_create",
    "run_profile_update",
    "run_profile_archive",
}


def _require_run_profile_v1_enabled(config: Optional[AppConfig]) -> None:
    if not bool(getattr(config, "run_profile_v1_enabled", False)):
        raise RunProfileAdministrationDisabledError(
            "Run Profile administration is disabled"
        )


def _require_project_for_run_profile(db: Database, project: str):
    project_row = db.get_project(project) if isinstance(project, str) else None
    if project_row is None or project_row.id is None:
        raise IdentityTargetNotFoundError(f"project {project} not found")
    return project_row


def _normalize_run_profile_require_tag(value: object) -> Optional[str]:
    normalized = _normalize_optional_identity_text(value, field="require_tag")
    if normalized is not None and len(normalized) > 128:
        raise InvalidRunProfileRequestError("require_tag must be at most 128 characters")
    return normalized


def request_run_profile_create_approval(
    db: Database,
    project: str,
    name: str,
    *,
    command: Optional[str] = None,
    setup_cmd: Optional[str] = None,
    require_tag: Optional[str] = None,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending request for a brand-new (project, name) Run Profile.

    D5 v1 (docs/DECISIONS.md): the profile does not exist yet; approval
    creates its first immutable revision (revision 1, status="approved").
    """

    _require_run_profile_v1_enabled(config)
    project_row = _require_project_for_run_profile(db, project)
    normalized_name = validate_name_component(name, field="name")
    if db.get_run_profile_head(project_row.id, normalized_name) is not None:
        raise InvalidRunProfileRequestError(
            f"run profile {normalized_name!r} already exists for this project;"
            " use an update request"
        )
    payload = {
        "project_id": project_row.id,
        "project_name": project_row.name,
        "name": normalized_name,
        "command": _normalize_optional_identity_text(command, field="command"),
        "setup_cmd": _normalize_optional_identity_text(setup_cmd, field="setup_cmd"),
        "require_tag": _normalize_run_profile_require_tag(require_tag),
    }
    return _insert_identity_approval(
        db,
        kind="run_profile_create",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


def request_run_profile_update_approval(
    db: Database,
    project: str,
    name: str,
    *,
    command: Optional[str] = None,
    setup_cmd: Optional[str] = None,
    require_tag: Optional[str] = None,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending new-revision proposal for an existing active profile.

    The request pins `based_on_revision` to the current head so approval-time
    revalidation can reject a concurrently changed profile instead of silently
    superseding a revision the requester never saw (D5 v1).
    """

    _require_run_profile_v1_enabled(config)
    project_row = _require_project_for_run_profile(db, project)
    normalized_name = validate_name_component(name, field="name")
    head = db.get_run_profile_head(project_row.id, normalized_name)
    if head is None:
        raise IdentityTargetNotFoundError(
            f"run profile {normalized_name!r} not found for this project"
        )
    if head.status != "approved":
        raise InvalidRunProfileRequestError(
            f"run profile {normalized_name!r} is {head.status} and cannot be updated"
        )
    payload = {
        "project_id": project_row.id,
        "project_name": project_row.name,
        "name": normalized_name,
        "based_on_revision": head.revision,
        "command": _normalize_optional_identity_text(command, field="command"),
        "setup_cmd": _normalize_optional_identity_text(setup_cmd, field="setup_cmd"),
        "require_tag": _normalize_run_profile_require_tag(require_tag),
    }
    return _insert_identity_approval(
        db,
        kind="run_profile_update",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


def request_run_profile_archive_approval(
    db: Database,
    project: str,
    name: str,
    *,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending archive-tombstone revision for an active profile."""

    _require_run_profile_v1_enabled(config)
    project_row = _require_project_for_run_profile(db, project)
    normalized_name = validate_name_component(name, field="name")
    head = db.get_run_profile_head(project_row.id, normalized_name)
    if head is None:
        raise IdentityTargetNotFoundError(
            f"run profile {normalized_name!r} not found for this project"
        )
    if head.status != "approved":
        raise InvalidRunProfileRequestError(
            f"run profile {normalized_name!r} is already {head.status}"
        )
    payload = {
        "project_id": project_row.id,
        "project_name": project_row.name,
        "name": normalized_name,
        "based_on_revision": head.revision,
    }
    return _insert_identity_approval(
        db,
        kind="run_profile_archive",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


_DISPATCH_POLICY_APPROVAL_KINDS = {
    "dispatch_policy_create",
    "dispatch_policy_update",
    "dispatch_policy_archive",
}


def _require_dispatch_policy_v1_enabled(config: Optional[AppConfig]) -> None:
    if not bool(getattr(config, "dispatch_policy_v1_enabled", False)):
        raise DispatchPolicyAdministrationDisabledError(
            "Dispatch Policy administration is disabled"
        )


def _normalize_dispatch_policy_require_tag(value: object) -> Optional[str]:
    normalized = _normalize_optional_identity_text(value, field="require_tag")
    if normalized is not None and len(normalized) > 128:
        raise InvalidDispatchPolicyRequestError(
            "require_tag must be at most 128 characters"
        )
    return normalized


def _normalize_dispatch_policy_allowed_servers(value: object) -> list[str]:
    """`allowed_servers` must be a non-empty list of distinct, non-blank
    strings. Deliberately **not** validated against the live `servers.yaml`
    roster at request time — the server set changes over time and this
    slice's policy object has zero runtime effect anyway (scheduler never
    reads `dispatch_policies`; Slice 4's placement-proposal evaluation is
    where `allowed_servers` would actually be intersected with the current
    enabled server set). Request-time validation only guards data shape."""
    if not isinstance(value, list) or not value:
        raise InvalidDispatchPolicyRequestError(
            "allowed_servers must be a non-empty list of server names"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidDispatchPolicyRequestError(
                "allowed_servers entries must be non-blank strings"
            )
        server_name = item.strip()
        if server_name in seen:
            raise InvalidDispatchPolicyRequestError(
                f"allowed_servers contains a duplicate entry: {server_name!r}"
            )
        seen.add(server_name)
        normalized.append(server_name)
    return normalized


def _normalize_max_concurrent_placements(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDispatchPolicyRequestError(
            "max_concurrent_placements must be an integer"
        )
    if value < 1 or value > 10:
        raise InvalidDispatchPolicyRequestError(
            "max_concurrent_placements must be between 1 and 10"
        )
    return value


def _normalize_dispatch_policy_valid_until(value: object) -> Optional[str]:
    if value is None:
        return None
    try:
        return _normalize_future_expiry(value)
    except InvalidIdentityAdminRequestError as exc:
        raise InvalidDispatchPolicyRequestError(str(exc)) from exc


def _validate_dispatch_policy_run_profile_reference(
    db: Database, project_id: str, run_profile_id: object
) -> Optional[str]:
    """`run_profile_id`, when given, must be an exact `run_profiles.id`
    revision belonging to this project. The referenced (project, name)'s
    **current head** must be status "approved" — pinning an old revision id
    of a profile whose head has since been archived is rejected, matching D5
    Run Profile v1's "archived means no longer selectable" semantics. Note
    this deliberately checks the *head* status, not the pinned revision's own
    status: the point of pinning an exact revision id is reproducibility of
    content, not resurrecting an abandoned profile."""

    if run_profile_id is None:
        return None
    if not isinstance(run_profile_id, str) or not run_profile_id:
        raise InvalidDispatchPolicyRequestError(
            "run_profile_id must be a string or null"
        )
    profile = db.get_run_profile_by_id(run_profile_id)
    if profile is None or profile.project_id != project_id:
        raise InvalidDispatchPolicyRequestError(
            f"run_profile_id {run_profile_id!r} not found for this project"
        )
    head = db.get_run_profile_head(project_id, profile.name)
    if head is None or head.status != "approved":
        raise InvalidDispatchPolicyRequestError(
            f"run profile {profile.name!r} is not currently approved"
        )
    return run_profile_id


def request_dispatch_policy_create_approval(
    db: Database,
    project: str,
    name: str,
    *,
    allowed_servers: list[str],
    require_tag: Optional[str] = None,
    run_profile_id: Optional[str] = None,
    dataset_required: bool = False,
    max_concurrent_placements: int = 1,
    valid_until: Optional[str] = None,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending request for a brand-new (project, name) Dispatch Policy.

    Goal 2 Slice 3 (docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md): the policy does
    not exist yet; approval creates its first immutable revision (revision 1,
    status="approved"). **This slice's policy object has zero runtime
    effect**: the scheduler never reads `dispatch_policies` (that is Slice 4).
    """

    _require_dispatch_policy_v1_enabled(config)
    project_row = _require_project_for_run_profile(db, project)
    normalized_name = validate_name_component(name, field="name")
    if db.get_dispatch_policy_head(project_row.id, normalized_name) is not None:
        raise InvalidDispatchPolicyRequestError(
            f"dispatch policy {normalized_name!r} already exists for this project;"
            " use an update request"
        )
    payload = {
        "project_id": project_row.id,
        "project_name": project_row.name,
        "name": normalized_name,
        "allowed_servers": _normalize_dispatch_policy_allowed_servers(allowed_servers),
        "require_tag": _normalize_dispatch_policy_require_tag(require_tag),
        "run_profile_id": _validate_dispatch_policy_run_profile_reference(
            db, project_row.id, run_profile_id
        ),
        "dataset_required": bool(dataset_required),
        "max_concurrent_placements": _normalize_max_concurrent_placements(
            max_concurrent_placements
        ),
        "valid_until": _normalize_dispatch_policy_valid_until(valid_until),
    }
    return _insert_identity_approval(
        db,
        kind="dispatch_policy_create",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


def request_dispatch_policy_update_approval(
    db: Database,
    project: str,
    name: str,
    *,
    allowed_servers: list[str],
    require_tag: Optional[str] = None,
    run_profile_id: Optional[str] = None,
    dataset_required: bool = False,
    max_concurrent_placements: int = 1,
    valid_until: Optional[str] = None,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending new-revision proposal for an existing active policy.

    The request pins `based_on_revision` to the current head so approval-time
    revalidation can reject a concurrently changed policy instead of silently
    superseding a revision the requester never saw (same D5 v1 pattern).
    """

    _require_dispatch_policy_v1_enabled(config)
    project_row = _require_project_for_run_profile(db, project)
    normalized_name = validate_name_component(name, field="name")
    head = db.get_dispatch_policy_head(project_row.id, normalized_name)
    if head is None:
        raise IdentityTargetNotFoundError(
            f"dispatch policy {normalized_name!r} not found for this project"
        )
    if head.status != "approved":
        raise InvalidDispatchPolicyRequestError(
            f"dispatch policy {normalized_name!r} is {head.status} and cannot be updated"
        )
    payload = {
        "project_id": project_row.id,
        "project_name": project_row.name,
        "name": normalized_name,
        "based_on_revision": head.revision,
        "allowed_servers": _normalize_dispatch_policy_allowed_servers(allowed_servers),
        "require_tag": _normalize_dispatch_policy_require_tag(require_tag),
        "run_profile_id": _validate_dispatch_policy_run_profile_reference(
            db, project_row.id, run_profile_id
        ),
        "dataset_required": bool(dataset_required),
        "max_concurrent_placements": _normalize_max_concurrent_placements(
            max_concurrent_placements
        ),
        "valid_until": _normalize_dispatch_policy_valid_until(valid_until),
    }
    return _insert_identity_approval(
        db,
        kind="dispatch_policy_update",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


def request_dispatch_policy_archive_approval(
    db: Database,
    project: str,
    name: str,
    *,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create a pending archive-tombstone revision for an active policy."""

    _require_dispatch_policy_v1_enabled(config)
    project_row = _require_project_for_run_profile(db, project)
    normalized_name = validate_name_component(name, field="name")
    head = db.get_dispatch_policy_head(project_row.id, normalized_name)
    if head is None:
        raise IdentityTargetNotFoundError(
            f"dispatch policy {normalized_name!r} not found for this project"
        )
    if head.status != "approved":
        raise InvalidDispatchPolicyRequestError(
            f"dispatch policy {normalized_name!r} is already {head.status}"
        )
    payload = {
        "project_id": project_row.id,
        "project_name": project_row.name,
        "name": normalized_name,
        "based_on_revision": head.revision,
    }
    return _insert_identity_approval(
        db,
        kind="dispatch_policy_archive",
        payload=payload,
        audit_path=audit_path,
        request_context=request_context,
    )


def _resolve_auto_placement_command(
    db: Database, policy: "DispatchPolicy"
) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """Resolve (command, setup_cmd, require_tag) for a policy, or ``None``
    when nothing usable can be resolved (never raises — the caller silently
    skips proposing when this happens, logging at debug level only, since
    this runs unattended in a background loop, not a user-facing request)."""

    if policy.run_profile_id is not None:
        profile = db.get_run_profile_by_id(policy.run_profile_id)
        if profile is None:
            return None
        head = db.get_run_profile_head(profile.project_id, profile.name)
        if head is None or head.status != "approved":
            return None
        if not profile.command:
            return None
        return profile.command, profile.setup_cmd, profile.require_tag

    project = next(
        (item for item in db.list_projects() if item.id == policy.project_id), None
    )
    if project is None or not project.default_command:
        return None
    return project.default_command, project.setup_cmd, project.require_tag


@dataclass(frozen=True)
class _AutoPlacementValidation:
    """Result of read-only revalidation of an `auto_placement` payload,
    shared by both the manual `approve()` branch and the policy-scoped
    auto-decider (`maybe_auto_decide_placement()`, INV-APPROVAL-4b).
    `ok=False` never mutates anything; the caller decides what to do next
    (manual approve branch rejects, auto-decider leaves the approval
    pending)."""

    ok: bool
    reason: Optional[str] = None
    policy_row: Optional["DispatchPolicy"] = None
    head: Optional["DispatchPolicy"] = None
    command: Optional[str] = None
    setup_cmd: Optional[str] = None
    require_tag: Optional[str] = None
    server_cfg: Optional[Any] = None


_AUTO_PLACEMENT_PAYLOAD_KEYS = {
    "policy_id",
    "policy_revision",
    "project",
    "project_id",
    "server",
    "command",
    "command_sha256",
    "setup_cmd",
    "require_tag",
    "run_profile_id",
    "priority",
}


def _validate_auto_placement_payload(
    db: Database, payload: Any, server_configs: Optional[dict]
) -> _AutoPlacementValidation:
    """Re-derive and re-check every INV-APPROVAL-4b condition (2)-(6) for one
    `auto_placement` payload, read-only (no DB writes, no job creation).

    Conditions checked, in order (matches INV-APPROVAL-4b numbering):
    (2) the referenced dispatch policy's current head is still `approved`
        and its revision matches the payload (the proposal has not gone
        stale); (3) the proposed server is still in the policy's exact
        `allowed_servers` list *and* is currently configured + enabled
        — the `allowed_servers` membership check is new in this slice
        (Goal 2 Slice 5): Slice 4's approve branch only checked
        configured+enabled, relying on the fact that a normal `allowed_servers`
        change always bumps the policy revision (caught by the revision
        check above); this explicit re-check closes the gap for any
        out-of-band mutation and is required for the auto-decider to
        correctly leave an approval pending when only the allowed-servers
        set shrinks without a revision bump; (4) `valid_until` unexpired;
        (6) command re-derivation SHA match + `is_dangerous()` recheck;
        (5) active-placement count still under `max_concurrent_placements`.
    """

    if not isinstance(payload, dict) or set(payload) != _AUTO_PLACEMENT_PAYLOAD_KEYS:
        return _AutoPlacementValidation(False, "auto placement payload is malformed")

    policy_id = payload["policy_id"]
    server_name = payload["server"]
    policy_row = db.get_dispatch_policy_by_id(policy_id)
    if policy_row is None:
        return _AutoPlacementValidation(False, "dispatch policy no longer exists")
    head = db.get_dispatch_policy_head(policy_row.project_id, policy_row.name)
    if (
        head is None
        or head.status != "approved"
        or head.revision != payload["policy_revision"]
    ):
        return _AutoPlacementValidation(
            False, "dispatch policy changed since this proposal was created"
        )
    if server_name not in head.allowed_servers:
        return _AutoPlacementValidation(
            False,
            f"server {server_name!r} is no longer in the policy's allowed_servers",
        )
    if head.valid_until is not None:
        try:
            valid_until = datetime.fromisoformat(head.valid_until)
        except (ValueError, TypeError):
            return _AutoPlacementValidation(
                False, "dispatch policy valid_until is unreadable"
            )
        if valid_until <= datetime.now(timezone.utc):
            return _AutoPlacementValidation(False, "dispatch policy has expired")

    server_cfg = (server_configs or {}).get(server_name)
    if server_cfg is None or not server_cfg.enabled:
        return _AutoPlacementValidation(
            False, f"server {server_name!r} is no longer configured or enabled"
        )

    resolved = _resolve_auto_placement_command(db, head)
    if resolved is None:
        return _AutoPlacementValidation(
            False,
            "command can no longer be resolved (run profile archived or missing)",
        )
    command, setup_cmd, require_tag = resolved
    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    if command != payload["command"] or command_sha256 != payload["command_sha256"]:
        return _AutoPlacementValidation(
            False,
            "resolved command no longer matches the proposal"
            " (source command or run profile changed since request)",
        )

    dangerous, reason = is_dangerous(payload["command"])
    if dangerous:
        return _AutoPlacementValidation(
            False, f"command is now considered dangerous: {reason}"
        )

    if db.count_active_auto_placement_jobs(policy_id) >= head.max_concurrent_placements:
        return _AutoPlacementValidation(
            False, "dispatch policy is at its max_concurrent_placements limit"
        )

    return _AutoPlacementValidation(
        True,
        policy_row=policy_row,
        head=head,
        command=command,
        setup_cmd=setup_cmd,
        require_tag=require_tag,
        server_cfg=server_cfg,
    )


def request_auto_placement_approval(
    db: Database,
    *,
    policy: "DispatchPolicy",
    server_name: str,
    config: Optional[AppConfig] = None,
    audit_path: str = "audit.jsonl",
) -> Optional[Approval]:
    """Goal 2 Slice 4 (docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md, DG-1 approved):
    create a pending ``auto_placement`` approval proposing that ``policy`` be
    placed on ``server_name``. Called by the background proposal loop
    (`app.main.AppState.auto_placement_loop()`), never by an HTTP route —
    there is no user-facing "create placement proposal" endpoint this slice;
    humans see and decide these proposals through the existing approvals UI.

    Returns ``None`` (never raises) whenever the proposal should simply not
    be created this tick: missing/archived run profile, empty resolved
    command, a dangerous command, an existing pending duplicate, a proposal
    for the same (policy, server) within the cooldown window, or the policy
    already at its concurrent-placement cap. This mirrors the loop's
    best-effort spirit — a skipped tick is not an error, it is retried next
    tick, and none of these conditions should crash the background loop.
    """

    _require_dispatch_policy_v1_enabled(config)

    resolved = _resolve_auto_placement_command(db, policy)
    if resolved is None:
        logger.debug(
            "auto_placement: skip policy %s server %s (no resolvable command)",
            policy.id,
            server_name,
        )
        return None
    command, setup_cmd, require_tag = resolved

    dangerous, reason = is_dangerous(command)
    if dangerous:
        logger.debug(
            "auto_placement: skip policy %s server %s (dangerous command: %s)",
            policy.id,
            server_name,
            reason,
        )
        return None

    # Idempotency / rate limiting — see module docstring reference in
    # docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md §Slice 4: (a) an existing
    # pending proposal for this exact (policy, server) pair; (b) any
    # proposal for this pair created within the cooldown window, pending or
    # not (prevents re-proposing immediately after a human rejects); (c) the
    # policy's `max_concurrent_placements` cap, counted from non-terminal
    # jobs stamped with this policy's auto_placement approvals.
    existing = [
        approval
        for approval in db.list_approvals(kind="auto_placement")
        if approval.payload.get("policy_id") == policy.id
        and approval.payload.get("server") == server_name
    ]
    if any(approval.status == "pending" for approval in existing):
        return None

    cooldown_sec = float(getattr(config, "auto_placement_cooldown_sec", 3600) or 3600)
    now = datetime.fromisoformat(now_iso())
    for approval in existing:
        try:
            created_at = datetime.fromisoformat(approval.created_at)
        except (ValueError, TypeError):
            continue
        if (now - created_at).total_seconds() < cooldown_sec:
            return None

    if db.count_active_auto_placement_jobs(policy.id) >= policy.max_concurrent_placements:
        return None

    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    payload = {
        "policy_id": policy.id,
        "policy_revision": policy.revision,
        "project": policy.project_name,
        "project_id": policy.project_id,
        "server": server_name,
        "command": command,
        "command_sha256": command_sha256,
        "setup_cmd": setup_cmd,
        "require_tag": require_tag,
        "run_profile_id": policy.run_profile_id,
        "priority": "normal",
    }
    approval_id = db.insert_approval(kind="auto_placement", payload=payload)
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "auto_placement", "payload": payload},
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )
    return db.get_approval(approval_id)


class _DecisionAttributingDatabase:
    """Delegate Database operations while atomically enriching terminal writes.

    Existing approval branches retain their exact status/side-effect ordering.
    Only terminal updates for the approval being decided gain the three Goal 1
    attribution values in the same SQLite UPDATE as status/decided_at.
    """

    def __init__(
        self,
        db: Database,
        approval_id: int,
        *,
        actor_id: Optional[str],
        mechanism: str,
    ) -> None:
        self._db = db
        self._approval_id = approval_id
        self._actor_id = actor_id
        self._mechanism = mechanism

    def __getattr__(self, name: str):
        return getattr(self._db, name)

    def update_approval(self, approval_id: int, **fields: Any) -> None:
        fields = dict(fields)
        if (
            approval_id == self._approval_id
            and fields.get("status") in {"approved", "rejected"}
        ):
            fields["decision_actor_id"] = self._actor_id
            fields["decision_mechanism"] = self._mechanism
        self._db.update_approval(approval_id, **fields)


# --------------------------------------------------------------------------
# 建立核准請求
# --------------------------------------------------------------------------


_ENGINEERING_VALIDATION_CONTRACT_VERSION = "engineering-worker-validation-v1"
_ENGINEERING_VALIDATION_COMMAND_LIMIT = 4000


def _validation_snapshot_digest(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verified_validation_bundle_descriptor(
    db: Database,
    *,
    task_id: str,
    attempt_number: int,
    coding_run_id: int,
    run_job_id: Optional[int],
    local_home_dir: str,
) -> dict[str, Any]:
    if run_job_id is None:
        raise InvalidEngineeringValidationRequestError(
            "Coding Run 沒有 canonical Job，不能提出 worker validation"
        )
    inspected = inspect_engineering_result_file(
        result_dir=local_result_dir(run_job_id, local_home_dir),
        filename="changes.bundle",
    )
    if (
        not inspected.get("available")
        or not isinstance(inspected.get("sha256"), str)
        or not isinstance(inspected.get("size_bytes"), int)
    ):
        reason = inspected.get("reason") or "unverified"
        raise InvalidEngineeringValidationRequestError(
            f"本地 changes.bundle 無法安全驗證（{reason}）"
        )
    artifact = next(
        (
            item
            for item in db.list_engineering_task_artifacts(
                task_id, attempt_number=attempt_number
            )
            if item.kind == "bundle"
            and item.coding_run_id == coding_run_id
            and item.verification_status == "verified"
            and item.availability == "available"
        ),
        None,
    )
    if (
        artifact is None
        or artifact.source_sha256 != inspected["sha256"]
        or artifact.source_size_bytes != inspected["size_bytes"]
    ):
        raise InvalidEngineeringValidationRequestError(
            "changes.bundle 與已驗證 artifact descriptor 不一致"
        )
    return {
        "storage_key": "changes.bundle",
        "sha256": inspected["sha256"],
        "size_bytes": inspected["size_bytes"],
    }


def _engineering_validation_target_snapshot(server: Any) -> dict[str, Any]:
    return {
        "name": server.name,
        "host": server.host,
        "user": server.user,
        "port": server.port,
    }


def _engineering_validation_instance_snapshot(instance: Any) -> dict[str, Any]:
    return {
        "id": instance.id,
        "project_id": instance.project_id,
        "server": instance.server,
        "path": instance.path,
    }


def _engineering_task_parent_approval_matches(db: Database, task: Any) -> bool:
    approval = db.get_approval(task.approval_id)
    if approval is None or approval.kind != "coding_task" or approval.status != "approved":
        return False
    payload = approval.payload
    return (
        isinstance(payload, dict)
        and payload.get("engineering_task_id") == task.id
        and payload.get("project") == task.project_name
        and payload.get("project_id") == task.project_id
        and payload.get("project_version_id") == task.project_version_id
        and payload.get("base_commit") == task.base_commit
        and payload.get("agent_provider_id") == task.agent_provider_id
        and payload.get("contract_version") == task.contract_version
        and payload.get("execution_contract") == task.execution_contract
        and payload.get("structured_request") == task.structured_request
        and payload.get("instruction") == task.instruction
        and payload.get("runner_server") == task.runner_server
    )


def request_engineering_worker_validation_approval(
    db: Database,
    task_id: str,
    *,
    command: str,
    pin_server: str,
    server_configs: dict[str, Any],
    local_home_dir: str,
    gpus_needed: Optional[int] = None,
    priority: str = "normal",
    require_tag: Optional[str] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> tuple[Any, Approval]:
    """Create only a pending ordinary enqueue approval; no Job or SSH."""

    command = command.strip() if isinstance(command, str) else ""
    pin_server = pin_server.strip() if isinstance(pin_server, str) else ""
    require_tag = require_tag.strip() if isinstance(require_tag, str) else None
    if not command or len(command) > _ENGINEERING_VALIDATION_COMMAND_LIMIT or "\0" in command:
        raise InvalidEngineeringValidationRequestError(
            "validation command 必填且不可超過 4000 字元"
        )
    if (
        not pin_server
        or len(pin_server) > 128
        or any(char in pin_server for char in "\r\n\0")
        or pin_server == LOCAL_SERVER
    ):
        raise InvalidEngineeringValidationRequestError(
            "pin_server 必須是已啟用的非本地 worker"
        )
    if require_tag is not None and (
        not require_tag
        or len(require_tag) > 128
        or any(char in require_tag for char in "\r\n\0")
    ):
        raise InvalidEngineeringValidationRequestError("require_tag 格式不合法")
    if priority not in {"normal", "low"}:
        raise InvalidEngineeringValidationRequestError("priority 格式不合法")
    if gpus_needed is not None and (
        isinstance(gpus_needed, bool)
        or not isinstance(gpus_needed, int)
        or gpus_needed < 0
        or gpus_needed > 64
    ):
        raise InvalidEngineeringValidationRequestError(
            "gpus_needed 必須介於 0 與 64"
        )
    try:
        reject_engineering_raw_credentials(command)
    except InvalidEngineeringTaskRequestError as exc:
        raise InvalidEngineeringValidationRequestError(str(exc)) from exc
    dangerous, reason = is_dangerous(command)
    if dangerous:
        append_audit(
            "reject",
            {
                "reason": "dangerous_engineering_validation_command",
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            },
            result="rejected",
            path=audit_path,
            actor=audit_actor_from_request_context(request_context),
        )
        raise DangerousCommandError(reason)

    task = db.get_engineering_task(task_id)
    if task is None:
        raise InvalidEngineeringValidationRequestError(
            "只有 native AI Engineering Task 可提出 worker validation"
        )
    if task.coding_run_id is None:
        raise InvalidEngineeringValidationRequestError("Engineering Task 尚未建立 Coding Run")
    if not _engineering_task_parent_approval_matches(db, task):
        raise InvalidEngineeringValidationRequestError(
            "Engineering Task 原始核准已漂移或不再有效"
        )
    parent_approval = db.get_approval(task.approval_id)
    if parent_approval is None:
        raise InvalidEngineeringValidationRequestError(
            "Engineering Task 原始核准不存在"
        )
    run = db.get_coding_run(task.coding_run_id)
    if (
        run is None
        or run.engineering_task_id != task.id
        or run.attempt_number is None
        or run.base_binding != "project_version_pinned"
        or run.project_version_id != task.project_version_id
        or (run.base_commit or "").lower() != task.base_commit.lower()
        or run.status != "done"
        or not run.result_commit
        or not run.bundle_path
    ):
        raise InvalidEngineeringValidationRequestError(
            "需要已完成且具有 verified bundle 的 pinned Coding Run"
        )
    version = db.get_project_version(task.project_version_id)
    if (
        version is None
        or version.project_id != task.project_id
        or version.project_name != task.project_name
        or version.git_commit.lower() != task.base_commit.lower()
    ):
        raise InvalidEngineeringValidationRequestError("ProjectVersion contract 已漂移")
    target = server_configs.get(pin_server)
    if target is None or not target.enabled or target.name == LOCAL_SERVER:
        raise InvalidEngineeringValidationRequestError(
            "pin_server 必須是已啟用的非本地 worker"
        )
    instance = resolve_project_instance(db, task.project_name, pin_server)
    if instance.project_id != task.project_id:
        raise InvalidEngineeringValidationRequestError(
            "目標 project instance 不屬於此 Project identity"
        )
    bundle = _verified_validation_bundle_descriptor(
        db,
        task_id=task.id,
        attempt_number=run.attempt_number,
        coding_run_id=run.id,
        run_job_id=run.job_id,
        local_home_dir=local_home_dir,
    )
    push_command = build_bundle_push_command(
        run.job_id, run.id, target, local_home_dir
    )
    downstream_command = build_bundle_checkout_preamble(
        run.id, run.result_commit, instance.path
    ) + command
    for prepared in (push_command, downstream_command):
        dangerous, reason = is_dangerous(prepared)
        if dangerous:
            raise DangerousCommandError(reason)
    execution_digests = {
        "bundle_push_command_sha256": hashlib.sha256(
            push_command.encode("utf-8")
        ).hexdigest(),
        "downstream_command_sha256": hashlib.sha256(
            downstream_command.encode("utf-8")
        ).hexdigest(),
    }
    validation_request_id = str(uuid.uuid4())
    snapshot = {
        "contract_version": _ENGINEERING_VALIDATION_CONTRACT_VERSION,
        "parent_approval": {
            "id": parent_approval.id,
            "payload_sha256": _validation_snapshot_digest(parent_approval.payload),
        },
        "task": {
            "id": task.id,
            "attempt_number": run.attempt_number,
            "project_id": task.project_id,
            "project_name": task.project_name,
            "project_version_id": task.project_version_id,
            "base_commit": task.base_commit,
            "coding_run_id": run.id,
            "result_commit": run.result_commit,
        },
        "bundle": bundle,
        "target": _engineering_validation_target_snapshot(target),
        "project_instance": _engineering_validation_instance_snapshot(instance),
        "execution": execution_digests,
        "job": {
            "command": command,
            "type": "adhoc",
            "require_tag": require_tag,
            "gpus_needed": gpus_needed,
            "priority": priority,
        },
    }
    snapshot_digest = _validation_snapshot_digest(snapshot)
    payload = {
        "command": command,
        "type": "adhoc",
        "project": task.project_name,
        "require_tag": require_tag,
        "pin_server": pin_server,
        "depends_on": [],
        "gpus_needed": gpus_needed,
        "priority": priority,
        "sync_plan": None,
        "setup_plan": None,
        "warning": None,
        "source": "engineering_validation",
        "source_coding_run_id": run.id,
        "validation_request_id": validation_request_id,
        "validation_contract_version": _ENGINEERING_VALIDATION_CONTRACT_VERSION,
        "engineering_task_id": task.id,
        "attempt_number": run.attempt_number,
        "project_id": task.project_id,
        "project_version_id": task.project_version_id,
        "base_commit": task.base_commit,
        "result_commit": run.result_commit,
        "request_snapshot_sha256": snapshot_digest,
        "validation_execution_summary": {
            "contract_version": _ENGINEERING_VALIDATION_CONTRACT_VERSION,
            "parent_approval": snapshot["parent_approval"],
            "target_identity": snapshot["target"],
            "project_instance": snapshot["project_instance"],
            "bundle": snapshot["bundle"],
            "command_digests": snapshot["execution"],
        },
    }
    validation_request_id, approval_id = db.insert_engineering_validation_request(
        validation_request_id=validation_request_id,
        task_id=task.id,
        attempt_number=run.attempt_number,
        coding_run_id=run.id,
        project_id=task.project_id,
        project_name=task.project_name,
        project_version_id=task.project_version_id,
        base_commit=task.base_commit,
        result_commit=run.result_commit,
        target_server=pin_server,
        request_snapshot=snapshot,
        request_snapshot_sha256=snapshot_digest,
        approval_payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "enqueue",
            "validation_request_id": validation_request_id,
            "engineering_task_id": task.id,
            "coding_run_id": run.id,
            "target_server": pin_server,
            "request_snapshot_sha256": snapshot_digest,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return (
        db.get_engineering_validation_request(validation_request_id),
        db.get_approval(approval_id),
    )


def request_enqueue_approval(
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
    source: str = "api",
    source_coding_run_id: Optional[int] = None,
    local_home_dir: str = ".",
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=enqueue 的核准請求。

    危險指令在這裡（建立核准請求當下）就直接拒絕：寫稽核 `reject`、丟
    `DangerousCommandError`，**不建立 approval 列**，呼叫端（API）應該把
    這個例外轉成 400，不給核准機會。

    階段 10（PLAN.md K.1）：`source` 標記這筆請求從哪裡來（web/chatgpt/
    vllm/api，未標記時預設 "api"，行為同現狀）——寫進 payload 與
    `approval_requested` 稽核，供 `app.approvals.maybe_auto_approve()`／
    `app/main.py` 的 `_finalize_approval()` 判斷要不要一步生效／諮詢自動
    核准規則。信任說明：`source` 可以被持有 `AUTH_TOKEN` 的呼叫端自由填
    寫——這不是漏洞，token 持有者本來就有完整核准權，冒充 `source` 得不到
    超出 token 已有的權限（見 README）。

    階段 3：`type="train"` 且指定了 `project` 與具體 `pin_server`（不是
    「自動」）時，會用 `app.datasets.build_dispatch_plan()` 算出這次派工要不
    要附帶 sync（目標機沒有所需資料集）／setup（機器沒跑過這個專案）依賴
    任務，整包計畫（`sync_plan`/`setup_plan`）放進 payload，讓核准卡片能
    顯示完整計畫；核准的當下（`approve()`）才真的建立這些任務列並串好
    `depends_on`（鐵律第 2 條：sync 也要先核准才生效，不能排程器自己臨時
    生一個沒被核准過的任務）。

    Fable 覆核修正 2：`type="train"` 且專案有資料集要求，但**沒有**指定
    `pin_server`（自動模式）時，查一次該資料集有沒有快取在任何一台機器
    上——如果哪都沒有，`app.scheduler.pick_job()` 的資格過濾（修正 1）
    會讓這個任務永遠選不到機器、一直卡在 queued。這裡在 payload 加一個
    `warning` 欄位，讓核准卡片在核准的當下就能提醒使用者，而不是任務
    默默排隊排到天荒地老都沒人知道為什麼。

    階段 13（PLAN.md N.7，Codex Worker v2 下游 bundle 流）：`source_coding_run_id`
    有值時，代表這個任務要用某次 Codex coding run 的 `changes.bundle`
    當起點（下游 train／驗證任務）。這裡（建立請求當下，不過就直接
    `ValueError`、不建立 approval）依序驗證：
    1. `coding_run = db.get_coding_run(source_coding_run_id)` 存在。
    2. `coding_run.status == "done"` 且 `coding_run.result_commit` 非空
       （只有成功完成、真的有 result commit 的 run 才能被下游引用）。
    3. 本地 `results/{coding_run.job_id}/changes.bundle` 確實存在
       （`local_home_dir` 解析，同 `app.results.local_result_dir()` 的路徑
       慣例）——bundle 還沒被既有 E 節結果回收拉回本地就不能建這個請求。
    4. `pin_server` 有值（下游任務必須明確指定要跑在哪台機器，才能驗證
       目標機有沒有這個專案的 instance；不接受「自動」模式）。
    5. `resolve_project_instance(db, coding_run.project, pin_server)` 能解出
       唯一一筆 instance（目標機要有這個專案的既有 instance，`approve()`
       的 bundle checkout 前置段要用它取得 repo，見
       `app.approvals.build_bundle_checkout_preamble()`）——`ProjectInstanceResolutionError`
       是 `ValueError` 子類別，直接讓它往上傳。
    6. `project` 若省略：自動帶入 `coding_run.project`；若有填但跟
       `coding_run.project` 不一致：`ValueError`（不允許張冠李戴）。

    payload 存 `source_coding_run_id`（`None` 時完全不影響既有行為，向下
    相容既有呼叫端——`app/chat.py`／`app/agent_tools.py` 都沒有傳這個新
    參數）。真正建立推送任務／組 bundle checkout 前置段發生在核准當下
    （`approve()` 的 enqueue 分支，鐵律第 2 條：核准前不能有任何實際
    派工副作用）。
    """
    dangerous, reason = is_dangerous(command)
    if dangerous:
        append_audit(
            "reject",
            {"command": command, "reason": reason},
            result="rejected",
            path=audit_path,
            actor=audit_actor_from_request_context(request_context),
        )
        raise DangerousCommandError(reason)

    if source_coding_run_id is not None:
        coding_run = db.get_coding_run(source_coding_run_id)
        if coding_run is None:
            raise ValueError(f"coding_run {source_coding_run_id} 不存在")
        if coding_run.status != "done" or not coding_run.result_commit:
            raise ValueError(
                f"coding_run {source_coding_run_id} 尚未成功完成"
                f"（狀態 {coding_run.status!r}），不能作為下游任務的起點"
            )
        bundle_file = Path(local_result_dir(coding_run.job_id, local_home_dir)) / "changes.bundle"
        if not bundle_file.is_file():
            raise ValueError(
                f"coding_run {source_coding_run_id} 找不到本地 changes.bundle"
                f"（{bundle_file}），可能尚未完成結果回收"
            )
        if not pin_server:
            raise ValueError(
                "source_coding_run_id 需要同時指定 pin_server（下游任務要跑在哪台機器）"
            )
        resolve_project_instance(db, coding_run.project, pin_server)
        if project is None:
            project = coding_run.project
        elif project != coding_run.project:
            raise ValueError(
                f"project（{project}）與 coding_run {source_coding_run_id} 的 project"
                f"（{coding_run.project}）不一致"
            )

    payload = {
        "command": command,
        "type": type,
        "project": project,
        "require_tag": require_tag,
        "pin_server": pin_server,
        "depends_on": depends_on or [],
        "gpus_needed": gpus_needed,
        "priority": priority,
        "sync_plan": None,
        "setup_plan": None,
        "warning": None,
        "source": source,
        "source_coding_run_id": source_coding_run_id,
    }

    if type == "train" and project and pin_server:
        proj = db.get_project(project)
        if proj is None:
            raise ProjectNotFoundError(f"專案 {project} 不存在")
        plan = build_dispatch_plan(db, proj, pin_server)
        payload["sync_plan"] = plan.sync_plan
        payload["setup_plan"] = plan.setup_plan
    elif type == "train" and project and not pin_server:
        proj = db.get_project(project)
        if proj is None:
            raise ProjectNotFoundError(f"專案 {project} 不存在")
        if proj.dataset_name and proj.dataset_version:
            if not db.is_dataset_cached_anywhere(proj.dataset_name, proj.dataset_version):
                payload["warning"] = (
                    f"資料集 {proj.dataset_name}@{proj.dataset_version} 尚未同步到任何機器；"
                    "自動模式下此任務會一直排隊，不會被派發。建議改用「指定機器」派工，"
                    "核准卡片會附帶同步計畫。"
                )

    approval_id = db.insert_approval(
        kind="enqueue",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "enqueue", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_stop_approval(
    db: Database,
    job_id: int,
    *,
    source: str = "api",
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=stop 的核准請求，僅限 running 狀態的任務。

    `source`：同 `request_enqueue_approval()`（PLAN.md K.1），未標記時預設
    "api"，行為同現狀。
    """
    job = db.get_job(job_id)
    if job is None:
        raise JobNotFoundError(f"job {job_id} not found")
    if job.status != "running":
        raise JobNotRunningError("只有 running 狀態的任務可以請求停止")

    payload = {"job_id": job_id, "source": source}
    approval_id = db.insert_approval(
        kind="stop",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "stop", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


# --------------------------------------------------------------------------
# 階段 8（第一批）：Project Inventory 核准請求（PLAN.md I 節）
# --------------------------------------------------------------------------


def _create_single_inventory_scan_approval(
    db: Database,
    server: str,
    project_roots: list[str],
    audit_path: str,
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """單一機器的 inventory_scan approval 建立邏輯（`server="all"` 時對每台
    機器各自呼叫一次，維持「一個 approval 對應一次對單一機器的 SSH 掃描」
    的稽核顆粒度）。危險/禁止路徑在這裡（建立請求當下）就檢查一次，任何
    一項非法就直接拒絕（寫稽核 `reject`、丟 `ForbiddenScanRootError`），不
    建立 approval；核准後真正掃描前（`approve()`）還會再檢查一次（雙重
    防線）。"""
    for root in project_roots:
        if is_forbidden_root(root):
            append_audit(
                "reject",
                {
                    "kind": "inventory_scan",
                    "server": server,
                    "project_root": root,
                    "reason": "禁止掃描的路徑",
                },
                result="rejected",
                path=audit_path,
                actor=audit_actor_from_request_context(request_context),
            )
            raise ForbiddenScanRootError(f"禁止掃描的路徑：{root}")

    payload = {"server": server, "project_roots": list(project_roots)}
    approval_id = db.insert_approval(
        kind="inventory_scan",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "inventory_scan", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_inventory_scan_approval(
    db: Database,
    server: str,
    project_roots: Optional[list[str]] = None,
    *,
    server_configs: Optional[dict] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval | list[Approval]:
    """建立 kind=inventory_scan 的核准請求，不真的掃描。

    階段 8 第二批（PLAN.md I.12「第二批追加項」）：`ServerConfig` 有了
    `project_roots` 之後，這個函式升級為：

    - `project_roots` 為 `None`（呼叫端沒帶）時，從
      `server_configs[server].project_roots` 自動代入；仍然可以在呼叫時
      明確帶入 `project_roots` 做「這次只掃這幾個」的選填覆蓋（即使該值是
      空字串以外的非 None 值，也視為明確覆蓋，空列表會被拒絕）。
    - `server="all"`：對每一台 `server_configs` 裡 `enabled=True` 的機器
      各自呼叫 `_create_single_inventory_scan_approval()` 建立**一筆獨立**
      的 approval（用各自設定的 `project_roots`），回傳 `list[Approval]`
      （可能是空列表，例如沒有任何機器設定了 `project_roots`，這不是錯誤
      ——沒有東西可以掃）。某一台機器的 `project_roots` 命中禁止路徑
      （理論上不會發生，`server_add`/`server_update` 核准當下已經驗證過）
      時只跳過那一台（`ForbiddenScanRootError` 已經在
      `_create_single_inventory_scan_approval()` 內寫過稽核），不影響其他
      機器的請求繼續建立。
    - 單一 `server` 時仍回傳單一 `Approval`（呼叫端用
      `isinstance(result, list)` 判斷回傳形狀）。
    """
    if server == "all":
        server_configs = server_configs or {}
        approvals: list[Approval] = []
        for name in sorted(server_configs):
            cfg = server_configs[name]
            if not getattr(cfg, "enabled", True):
                continue
            roots = list(getattr(cfg, "project_roots", None) or [])
            if not roots:
                # 這台機器沒設定 project_roots，沒東西可掃，跳過不算錯誤。
                continue
            try:
                approvals.append(
                    _create_single_inventory_scan_approval(
                        db,
                        name,
                        roots,
                        audit_path=audit_path,
                        request_context=request_context,
                    )
                )
            except ForbiddenScanRootError:
                # 稽核已經在 _create_single_inventory_scan_approval() 內寫過
                # reject，這裡只是不讓一台機器的設定問題擋住其他機器。
                continue
        return approvals

    if project_roots is None:
        cfg = (server_configs or {}).get(server)
        if cfg is None:
            raise ValueError(
                f"未知的機器設定：{server}，且請求未帶入 project_roots"
            )
        project_roots = list(getattr(cfg, "project_roots", None) or [])
        if not project_roots:
            raise ValueError(
                f"機器 {server} 沒有設定 project_roots，且請求未帶入 project_roots"
            )
    elif not project_roots:
        raise ValueError("project_roots 不可為空，請至少指定一個掃描根目錄")

    return _create_single_inventory_scan_approval(
        db,
        server,
        project_roots,
        audit_path=audit_path,
        request_context=request_context,
    )


def request_import_project_approval(
    db: Database,
    candidate_id: str,
    overrides: Optional[dict] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=import_project 的核准請求。`overrides` 覆蓋 candidate 本身
    的猜測值（`name_guess`/`command_guess`/`readme_excerpt`）當預設；
    candidate 不存在或已經不是 pending 狀態 → 例外（呼叫端轉 400/404），
    不建立 approval。"""
    candidate = db.get_project_candidate(candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"candidate {candidate_id} 不存在")
    if candidate.status != "pending":
        raise CandidateNotPendingError(
            f"candidate {candidate_id} 已經是 {candidate.status}，不能重複匯入"
        )

    overrides = overrides or {}
    dataset_mode = overrides.get("dataset_mode") or (
        "embedded" if candidate.embedded_data_paths else "none"
    )
    if dataset_mode not in VALID_DATASET_MODES:
        raise ValueError(f"不合法的 dataset_mode: {dataset_mode}")

    #: PLAN.md 2026-07-11 版 §14 切片 3:`link_to_project` 有值＝把 candidate
    #: 連結到**既有** Project(新增一個 project_instance),不再新建 Project
    #: ——同一專案散在多台機器時不會被迫重複登記。值接受專案名稱或 UUID
    #: (get_project() 雙讀 adapter),建立請求當下驗證存在,payload 固定
    #: 存 canonical name＋UUID;核准時依 INV-APPROVAL-3 重驗。連結永遠由
    #: 使用者明示,系統只在 GET candidate 提供 remote 比對提示,不自動合併
    #: (INV-PROJECT-4 草案)。
    link_to = overrides.get("link_to_project")
    link_payload: dict = {}
    if link_to:
        target = db.get_project(link_to)
        if target is None:
            raise ValueError(f"連結目標專案 {link_to} 不存在")
        link_payload = {
            "link_to_project": target.name,
            "link_to_project_id": target.id,
        }

    payload = {
        "candidate_id": candidate_id,
        "name": overrides.get("name") or candidate.name_guess,
        "default_command": overrides.get("default_command") or candidate.command_guess,
        "dataset_name": overrides.get("dataset_name"),
        "dataset_version": overrides.get("dataset_version"),
        "dataset_mode": dataset_mode,
        "require_tag": overrides.get("require_tag"),
        "setup_cmd": overrides.get("setup_cmd"),
        "summary": overrides.get("summary") or candidate.readme_excerpt,
        **link_payload,
    }
    approval_id = db.insert_approval(
        kind="import_project",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "import_project", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_ignore_project_candidate_approval(
    db: Database,
    candidate_id: str,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=ignore_project_candidate 的核准請求，同樣要求 candidate
    存在且目前是 pending。"""
    candidate = db.get_project_candidate(candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"candidate {candidate_id} 不存在")
    if candidate.status != "pending":
        raise CandidateNotPendingError(
            f"candidate {candidate_id} 已經是 {candidate.status}，不能重複處理"
        )

    payload = {"candidate_id": candidate_id}
    approval_id = db.insert_approval(
        kind="ignore_project_candidate",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "ignore_project_candidate", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_ignore_nested_candidates_approval(
    db: Database,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=ignore_nested_candidates 的核准請求（PLAN.md P.1.2 節，
    Fable 裁定第 2 點）：一次核准把「路徑位於其他非 ignored 候選之下」的
    所有 pending 候選全部標 ignored——維持「候選整理走核准」的既有一致性
    （不是繞過核准直接執行），但收斂成一張批次卡，不用逐一點好幾十次。

    巢狀判定重用 `app.inventory.prune_nested_candidates()` 同一套字串比對
    規則（`path.startswith(other + "/")`，先各自 `rstrip("/")`，避免
    `/a/bc` 被誤判為 `/a/b` 的子目錄——見該函式 docstring）；**只在同一台
    server 內比較**（不同機器路徑字串剛好相同不代表誰在誰底下，`app.
    inventory.prune_nested_candidates()` 也一貫只在單一 server 的候選集合
    內比較，這裡跨 server 彙整多筆候選時延續同樣的假設）。「其他候選」指
    同一台機器上所有 status 不是 ignored 的候選（pending 或 imported 都
    算——已匯入的頂層專案底下如果還留著 pending 的巢狀噪音，一樣該清掉，
    不是只跟其他 pending 互相比較）。

    找不到任何符合條件的候選 -> `NoNestedCandidatesError`（呼叫端轉
    400），不建立空的 approval。payload 為
    `{"candidate_ids": [...], "items": [{"id", "path", "name_guess"}, ...]}`
    ——`items` 供核准卡逐筆顯示，`candidate_ids` 是 `approve()` 真正處理
    時要用的清單。
    """
    all_candidates = db.list_project_candidates()

    non_ignored_paths_by_server: dict[str, list[str]] = {}
    for c in all_candidates:
        if c.status == "ignored":
            continue
        non_ignored_paths_by_server.setdefault(c.server, []).append(c.path.rstrip("/"))

    def _is_nested_under_another(candidate: ProjectCandidate) -> bool:
        normalized = candidate.path.rstrip("/")
        for other in non_ignored_paths_by_server.get(candidate.server, []):
            if other == normalized:
                continue
            if normalized.startswith(other + "/"):
                return True
        return False

    nested_pending = [
        c for c in all_candidates if c.status == "pending" and _is_nested_under_another(c)
    ]

    if not nested_pending:
        raise NoNestedCandidatesError("沒有可清理的巢狀候選")

    payload = {
        "candidate_ids": [c.id for c in nested_pending],
        "items": [
            {"id": c.id, "path": c.path, "name_guess": c.name_guess} for c in nested_pending
        ],
    }
    approval_id = db.insert_approval(
        kind="ignore_nested_candidates",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "ignore_nested_candidates", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


async def add_manual_candidate(
    db: Database,
    server: str,
    path: str,
    name: Optional[str] = None,
    *,
    server_configs: Optional[dict] = None,
    ssh_run=None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> ProjectCandidate:
    """P.1.5（PLAN.md，2026-07-10 追加，Fable 定案）：手動新增候選專案。

    背景：使用者的 Controlnet（server-b）是「workspace 型」專案——頂層沒有
    任何 `app.inventory.MARKER_FILES`、只有子目錄，掃描器按規則永遠認不出
    來，但使用者裁定它就是一個專案。這個函式讓使用者直接補一筆候選，繞過
    掃描，等同「掃描產出」的策展輸入。

    **不走核准流**：這裡直接 upsert `project_candidates`（`status=pending`）
    ，不建立 approval——建立候選本身只是策展輸入（跟 `inventory_scan` 核准
    後 upsert 候選是同一種資料形狀，只是來源從 SSH find 換成使用者手動指定
    ），沒有繞過任何鐵律；**專案/instance 的真正建立仍然要走既有的
    `import_project` 核准流**（`POST /inventory/candidates/{id}/
    import-request` → `POST /approve/{id}`）——這裡建立的候選跟掃描產生的
    候選走同一套後續流程，沒有任何特權。

    驗證順序（不通過 → 對應例外，呼叫端轉 400，除了重複是 409）：
    1. `server` 存在於 `server_configs` 且 `enabled=True`
       （`ManualCandidateServerInvalidError`）。
    2. `path` 是絕對路徑（`/` 開頭）、不含 `..` 片段
       （`ManualCandidatePathInvalidError`）；結尾 `/` 正規化掉。
    3. 正規化後的 `path` 不得命中 `app.inventory.is_forbidden_root()`
       （`ManualCandidatePathInvalidError`）——跟 `inventory_scan` 的雙重
       防線慣例一致（見 `app/inventory.py` 模組 docstring／
       `_create_single_inventory_scan_approval()`）：使用者手動輸入的路徑
       一樣不該被拿去登記成候選，`/`、`/etc`、`/var`、`/root`、`/usr`、
       `/opt`、`/tmp`、精確符合的 `/home`、裸 `~` 都禁止（`/home/<user>/
       ...`、`~/...` 允許，規則細節見 `is_forbidden_root()` docstring）。
    4. `name` 有給時過既有 `validate_name_component()`（`InvalidNameError`，
       是 `ValueError` 子類，呼叫端一樣轉 400）；沒給時用
       `os.path.basename(path)` 當 `name_guess`，不額外驗證字元集——跟
       `app.inventory.scan_server()` 的既有慣例一致（掃描出來的 `name_guess`
       本來就不受這個限制）。
    5. **唯讀 SSH 確認目錄存在**：`test -d {path} && echo DIR_OK`——輸出
       沒有 `DIR_OK` → `ManualCandidatePathInvalidError`。
    6. `server`+正規化後 `path` 已經有候選（任何 status，含 imported/
       ignored）→ `ManualCandidateDuplicateError`（呼叫端轉 409，detail 附
       現有候選 id 與 status）。

    通過全部驗證後，`kind="project"`、`markers=["manual"]`（供前端/後續
    邏輯辨識這是手動輸入、不是掃描找到的），寫稽核 `candidate_manual_added`，
    回傳建立的候選（`upsert_project_candidate()` 對全新 id 一律建
    `status="pending"`）。
    """
    server_configs = server_configs or {}
    cfg = server_configs.get(server)
    if cfg is None or not getattr(cfg, "enabled", True):
        raise ManualCandidateServerInvalidError(f"未知或未啟用的機器：{server}")

    raw_path = (path or "").strip()
    if not raw_path.startswith("/"):
        raise ManualCandidatePathInvalidError("path 必須是絕對路徑（以 / 開頭）")
    if ".." in raw_path.split("/"):
        raise ManualCandidatePathInvalidError("path 不可包含 .. 片段")
    normalized_path = raw_path.rstrip("/") or "/"

    if is_forbidden_root(normalized_path):
        raise ManualCandidatePathInvalidError(f"禁止登記候選的路徑：{normalized_path}")

    if name:
        validate_name_component(name, field="name")
        name_guess = name
    else:
        name_guess = os.path.basename(normalized_path) or normalized_path

    if ssh_run is None:
        raise ValueError("add_manual_candidate 需要 ssh_run，呼叫端未提供")
    check_cmd = f"test -d {shlex.quote(normalized_path)} && echo DIR_OK"
    check_result = await ssh_run(server, check_cmd, 10)
    if "DIR_OK" not in (check_result.stdout or ""):
        raise ManualCandidatePathInvalidError("路徑不存在或不是目錄")

    existing_id = make_candidate_id(server, normalized_path)
    existing = db.get_project_candidate(existing_id)
    if existing is not None:
        raise ManualCandidateDuplicateError(existing)

    cid = db.upsert_project_candidate(
        server=server,
        path=normalized_path,
        name_guess=name_guess,
        kind="project",
        markers=["manual"],
    )
    append_audit(
        "candidate_manual_added",
        {
            "server": server,
            "path": normalized_path,
            "name_guess": name_guess,
            "candidate_id": cid,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_project_candidate(cid)


# --------------------------------------------------------------------------
# 階段 8（第二批）：Web Server Management 核准請求（PLAN.md I 節）
# --------------------------------------------------------------------------


def select_codex_runner(db: Database, config: AppConfig) -> Optional[str]:
    """Goal 3 Phase D-1：替**新的** coding 工作選一台 Runner。

    - pool 未設定（含只設 `CODEX_RUNNER_SERVER` 的舊配置在
      `apply_codex_config_rules()` 正規化前的測試情境）→ 回傳
      `config.codex_runner_server`（可能是 None＝功能停用），行為與
      Phase D-1 之前逐位元相同。
    - 單元素 pool → 該元素（同上，零行為差異）。
    - 多元素 pool → `pick_codex_runner()`：active coding job 最少者，
      平手取 pool 順序。已綁定 Runner 的既有工作（retry 等）**不經過**
      這裡——綁定不因 pool 變動而漂移。"""

    pool = tuple(getattr(config, "codex_runner_servers", ()) or ())
    if not pool:
        return config.codex_runner_server
    if len(pool) == 1:
        return pool[0]
    return pick_codex_runner(pool, db.count_active_coding_jobs_by_server())


def _require_server_bootstrap_v1_enabled(config: Optional[AppConfig]) -> None:
    if not bool(getattr(config, "server_bootstrap_v1_enabled", False)):
        raise ServerBootstrapDisabledError("server bootstrap is disabled")


_BOOTSTRAP_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_BOOTSTRAP_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: 比照 `onboard-worker.sh` 的安全限制：key 只允許目前使用者 `~/.ssh` 的
#: 直接子路徑（字串形式；私鑰內容永遠不出現在 payload/DB/稽核）。
_BOOTSTRAP_KEY_RE = re.compile(r"^~/\.ssh/[A-Za-z0-9._-]{1,128}$")


def request_server_bootstrap_approval(
    db: Database,
    payload: dict,
    config: AppConfig,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=server_bootstrap 的核准請求（Goal 3 Phase B B1；DG-B 核准
    見 docs/DECISIONS.md 2026-07-19），**不執行任何遠端動作**。

    payload 是 typed 欄位（host/username/port/key/components/gpu），加上
    請求當下的腳本版本與 SHA-256 pin——核准時腳本已改版會被拒絕，操作者
    核准的永遠是「請求當下審閱過的那一版」。同目標已有 pending 請求 →
    拒絕（防洪，比照既有 pending 去重慣例）。"""

    _require_server_bootstrap_v1_enabled(config)

    def _reject(reason: str) -> None:
        append_audit(
            "reject",
            {"kind": "server_bootstrap", "host": payload.get("host"), "reason": reason},
            result="rejected",
            path=audit_path,
            actor=audit_actor_from_request_context(request_context),
        )
        raise InvalidServerBootstrapRequestError(reason)

    if not isinstance(payload, dict):
        _reject("payload 必須是物件")
    host = payload.get("host")
    username = payload.get("username") or payload.get("user")
    key = payload.get("key")
    gpu = bool(payload.get("gpu", False))
    try:
        port = int(payload.get("port", 22))
    except (TypeError, ValueError):
        _reject("port 必須是整數")
    if not isinstance(host, str) or not _BOOTSTRAP_HOST_RE.match(host):
        _reject("host 格式不正確（限英數、點、連字號）")
    if not isinstance(username, str) or not _BOOTSTRAP_USER_RE.match(username):
        _reject("username 格式不正確（非 root 的 Linux 帳號名）")
    if username == "root":
        _reject("bootstrap 不允許 root 帳號（DG-B：非 root、使用者層）")
    if not (1 <= port <= 65535):
        _reject("port 必須介於 1–65535")
    if not isinstance(key, str) or not _BOOTSTRAP_KEY_RE.match(key):
        _reject("key 必須是 ~/.ssh/ 底下的私鑰路徑字串（不是私鑰內容）")
    try:
        components = validate_bootstrap_components(payload.get("components"))
    except ValueError as exc:
        _reject(str(exc))

    for approval in db.list_approvals(status="pending", kind="server_bootstrap"):
        existing = approval.payload or {}
        if (
            existing.get("host") == host
            and existing.get("username") == username
            and existing.get("port") == port
        ):
            _reject(f"同目標已有 pending 的 bootstrap 請求 #{approval.id}")

    normalized = {
        "host": host,
        "username": username,
        "port": port,
        "key": key,
        "components": components,
        "gpu": gpu,
        "script_version": BOOTSTRAP_SCRIPT_VERSION,
        "script_sha256": bootstrap_script_sha256(),
    }
    approval_id = db.insert_approval(
        kind="server_bootstrap",
        payload=normalized,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "server_bootstrap", "payload": normalized},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_server_add_approval(
    db: Database,
    payload: dict,
    config: AppConfig,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=server_add 的核准請求。**先呼叫 `validate_server_config()`**
    ——不合法直接寫稽核 `reject`、丟 `InvalidServerConfigError`，不建立
    approval（鐵律第 2 條的延伸：不合法的設定跟危險指令一樣，在建立請求
    當下就直接拒絕，不給核准機會）。合法的話用
    `normalize_server_config()` 補齊預設值後存入 payload。
    """
    ok, errors, warnings = validate_server_config(payload, config)
    if not ok:
        append_audit(
            "reject",
            {"kind": "server_add", "name": payload.get("name"), "errors": errors},
            result="rejected",
            path=audit_path,
            actor=audit_actor_from_request_context(request_context),
        )
        raise InvalidServerConfigError(errors)

    # Goal 3 Phase B B2（DG-B）：bootstrap 報告閘——**只在功能開啟且同目標
    # 存在報告時**才介入：最新一筆失敗 → 拒絕（報告內容說明缺什麼）；通過
    # 或根本沒有報告（既有機器、功能關閉）→ 完全不影響，維持相容。
    if bool(getattr(config, "server_bootstrap_v1_enabled", False)):
        latest = db.latest_server_bootstrap_report(
            host=str(payload.get("host") or ""),
            username=str(payload.get("user") or ""),
            port=int(payload.get("port", 22) or 22),
        )
        if latest is not None and not latest.passed:
            reason = (
                f"目標機最新 bootstrap 報告（#{latest.id}）未通過："
                f"{'；'.join(latest.report.get('errors') or []) or '缺項未記錄'}"
                "。請先重跑 server_bootstrap 直到通過，或由操作者以 root 補齊系統工具。"
            )
            append_audit(
                "reject",
                {"kind": "server_add", "name": payload.get("name"), "errors": [reason]},
                result="rejected",
                path=audit_path,
                actor=audit_actor_from_request_context(request_context),
            )
            raise InvalidServerConfigError([reason])

    normalized = normalize_server_config(payload)
    approval_id = db.insert_approval(
        kind="server_add",
        payload=normalized,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "server_add",
            "payload": normalized,
            "warnings": warnings,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_server_update_approval(
    db: Database,
    name: str,
    updates: dict,
    config: AppConfig,
    current_servers: list[dict],
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=server_update 的核准請求。`updates` 內若含 `name` 且與現有
    `name` 不同 → `ServerRenameNotSupportedError`（不支援 rename，避免破壞
    `jobs.server`／`jobs.pin_server` 等既有欄位引用），呼叫端應轉 400。找到
    現有設定（`current_servers`：呼叫端傳入的現有 servers.yaml 原始結構
    列表）合併 `updates` 後跑 `validate_server_config()`，不合法同樣直接
    拒絕、不建立 approval。payload 只存 `{name, updates}`（不存整份合併結果）
    ——核准當下（`approve()`）會再讀一次當下最新的 servers.yaml 重新合併，
    避免用到請求建立當下就過時的快照。
    """
    new_name = updates.get("name")
    if new_name is not None and new_name != name:
        raise ServerRenameNotSupportedError(
            "不支援改名 server（會破壞 jobs.server/jobs.pin_server 等既有欄位引用）"
        )

    existing = next((s for s in current_servers if s.get("name") == name), None)
    if existing is None:
        raise ServerNotFoundError(f"server {name} 不存在")

    merged = dict(existing)
    merged.update(updates)
    merged["name"] = name

    ok, errors, warnings = validate_server_config(merged, config)
    if not ok:
        append_audit(
            "reject",
            {"kind": "server_update", "name": name, "errors": errors},
            result="rejected",
            path=audit_path,
            actor=audit_actor_from_request_context(request_context),
        )
        raise InvalidServerConfigError(errors)

    payload = {"name": name, "updates": dict(updates)}
    approval_id = db.insert_approval(
        kind="server_update",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "server_update",
            "payload": payload,
            "warnings": warnings,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_server_disable_approval(
    db: Database,
    name: str,
    current_server_names: list[str],
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=server_disable 的核准請求。**建立請求當下**只檢查 server
    是否存在於目前設定，**不擋 running job**——那是核准當下的責任（狀態
    可能在等待期間變化，見 `approve()` 的 server_disable 分支）。
    """
    if name not in current_server_names:
        raise ServerNotFoundError(f"server {name} 不存在")

    payload = {"name": name}
    approval_id = db.insert_approval(
        kind="server_disable",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "server_disable", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_server_delete_approval(
    db: Database,
    name: str,
    current_server_names: list[str],
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=server_delete 的核准請求。同 `request_server_disable_approval()`
    ——建立時只檢查 server 存在，不擋 running job（第一版核准後的落地效果
    等同停用，見 `approve()` 的 server_delete 分支）。"""
    if name not in current_server_names:
        raise ServerNotFoundError(f"server {name} 不存在")

    payload = {"name": name}
    approval_id = db.insert_approval(
        kind="server_delete",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {"approval_id": approval_id, "kind": "server_delete", "payload": payload},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


# --------------------------------------------------------------------------
# 階段 12（PLAN.md M 節）：AI 改碼層次一——apply_patch 核准請求
# --------------------------------------------------------------------------

#: diff 全文字元數上限（PLAN.md M.2）。
MAX_DIFF_CHARS = 100 * 1024


def _looks_like_unified_diff(diff: str) -> bool:
    """粗略但足夠的格式檢查：`diff --git` 標頭，或同時有 `--- `／`+++ `
    標頭（unified diff 的兩行檔頭）。不追求完整解析 diff 語法——真正能不能
    套用交給核准後的 `git apply --check`（見 `approve()` 的 apply_patch
    分支），這裡只擋明顯不是 diff 的內容（例如整段自然語言）。"""
    if "diff --git" in diff:
        return True
    return "--- " in diff and "+++ " in diff


def _extract_diff_target_paths(diff: str) -> list[str]:
    """解析 diff 內所有目標檔路徑：`--- a/xxx` 與 `+++ b/xxx` 這兩種標頭行
    的 `xxx` 部分（`/dev/null`（新增/刪除檔案時的另一側）原樣保留，呼叫端
    自行判斷要不要跳過）。真實 unified diff 有時會在路徑後面接一個 tab
    再接時間戳，這裡用 `split("\\t")[0]` 去掉那段。"""
    paths: list[str] = []
    for raw in diff.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("--- a/"):
            paths.append(line[len("--- a/") :].split("\t")[0].strip())
        elif line.startswith("+++ b/"):
            paths.append(line[len("+++ b/") :].split("\t")[0].strip())
    return paths


def request_apply_patch_approval(
    db: Database,
    project: str,
    server: str,
    diff: str,
    description: Optional[str] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=apply_patch 的核准請求（PLAN.md M.2）：使用者明確要求越過
    原規格「只建議不改碼」的紅線；受控方式＝每個 diff 人工核准、改動只在
    git branch 上、永不 push、全文進稽核（見 `approve()` 的 apply_patch
    分支）。

    建立請求當下（不過就 400、不建 approval，鐵律第 2 條的延伸）驗證：

    1. `project`＋`server` 必須對應一個已登記的 `project_instance`
       （`app.activity.resolve_project_instance()`，`server` 在這裡是必填
       參數，不會有「省略時自動選」的模糊性——一個 apply_patch 請求只能
       對準一台機器）。
    2. `diff` 非空、字元數 ≤ `MAX_DIFF_CHARS`（100KB）。
    3. `diff` 長得像 unified diff（`_looks_like_unified_diff()`）。
    4. `diff` 內解析出的所有目標路徑（`--- a/xxx`／`+++ b/xxx`，
       `/dev/null` 除外）都要通過 `app.activity.validate_rel_path()`——
       不得含 `..`、不得是絕對路徑、不得命中秘密檔名 pattern（跟
       `read_instance_file()` 共用同一份驗證邏輯，見 `app/activity.py`
       模組 docstring）。diff 裡完全沒有目標路徑（格式雖然像 diff 但沒有
       `---`/`+++` 標頭）也視為不合法，一併拒絕。

    payload 存 `project`／`server`／`diff`（全文）／`description`，核准
    卡片前端用 `<pre>` 顯示 diff 全文。

    **只加在 MCP bridge（ChatGPT），不加給 vLLM `app/agent_tools.py`**
    ——7B 模型寫 diff 品質不可靠，只會製造核准垃圾（PLAN.md M.2 裁定）。
    """
    try:
        resolve_project_instance(db, project, server)
    except ProjectInstanceResolutionError as exc:
        raise InvalidApplyPatchRequestError(str(exc)) from exc

    if not diff or not diff.strip():
        raise InvalidApplyPatchRequestError("diff 不可為空")
    if len(diff) > MAX_DIFF_CHARS:
        raise InvalidApplyPatchRequestError(
            f"diff 過大（{len(diff)} 字元，上限 {MAX_DIFF_CHARS} 字元）"
        )
    if not _looks_like_unified_diff(diff):
        raise InvalidApplyPatchRequestError(
            "diff 內容不像 unified diff 格式（缺少 --- /+++ 或 diff --git 標頭）"
        )

    target_paths = [p for p in _extract_diff_target_paths(diff) if p != "/dev/null"]
    if not target_paths:
        raise InvalidApplyPatchRequestError("diff 內找不到任何合法的目標檔案路徑")
    for target_path in target_paths:
        err = validate_rel_path(target_path)
        if err is not None:
            raise InvalidApplyPatchRequestError(f"diff 目標路徑不合法（{target_path}）：{err}")

    payload = {
        "project": project,
        "server": server,
        "diff": diff,
        "description": description or "",
    }
    approval_id = db.insert_approval(
        kind="apply_patch",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "apply_patch",
            "project": project,
            "server": server,
            "description": description or "",
            "diff_chars": len(diff),
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


# --------------------------------------------------------------------------
# 階段 15 Phase B（PLAN.md P.2.1 節）：kind=git_init 核准請求——把一個尚未
# 受 git 管理的 project_instance 就地初始化成 git repo。
# --------------------------------------------------------------------------

#: `build_default_gitignore()` 預設排除的目錄（PLAN.md P.2.1 原文清單）：
#: 大檔資料/輸出/環境目錄——訓練任務常見會在專案目錄底下產生的東西，
#: 整包 commit 進版控既沒意義又容易撞 size guard。
DEFAULT_GITIGNORE_DIRS: tuple[str, ...] = (
    "checkpoints/",
    "outputs/",
    "runs/",
    "logs/",
    "results/",
    "data/",
    "datasets/",
    "Dataset/",
    "weights/",
    "weight/",
    "wandb/",
    "mlruns/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".cache/",
    ".pytest_cache/",
    ".ipynb_checkpoints/",
)

#: `build_default_gitignore()` 預設排除的檔案格式（PLAN.md P.2.1 原文
#: 清單）：常見權重/大型二進位檔案的副檔名。
DEFAULT_GITIGNORE_FILE_PATTERNS: tuple[str, ...] = (
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.safetensors",
    "*.bin",
    "*.npz",
    "*.h5",
    "*.hdf5",
    "*.onnx",
    "*.pb",
    "*.tar",
    "*.zip",
    "*.gz",
)

#: `extra_ignores` 每一項限單行 pattern，字元集白名單（PLAN.md P.2.1）：
#: 只允許 gitignore pattern 慣用的字元，避免把奇怪的內容（換行、shell
#: 特殊字元）拼進 .gitignore 全文（下一步是用 `printf` 單指令直接 SSH 寫
#: 到工作機，見 `approve()` 的 git_init 分支第 2 步）。
_GIT_IGNORE_PATTERN_RE = re.compile(r"^[A-Za-z0-9._*/-]+$")

#: `approve()` 的 git_init 分支第 5 步（size guard）：`git count-objects -v`
#: 回報的 `size`＋`size-pack`（兩者單位都是 KB）總和超過這個值就拒絕，
#: 500MB（PLAN.md P.2.1 原文：`512000KB`）。
GIT_INIT_SIZE_GUARD_KB = 512000


def _parse_git_count_objects(output: str) -> tuple[int, int]:
    """解析 `git count-objects -v` 的輸出，取出 `size:` 與 `size-pack:` 兩行
    （單位 KB，git 官方文件用語）。任何一行缺漏或格式不是整數 -> 該項算
    0（不拋例外，避免奇怪的輸出格式讓 size guard 整個失效噴例外、卡住
    approve() 流程；`approve()` 的 git_init 分支用兩者總和判斷是否超過
    `GIT_INIT_SIZE_GUARD_KB`）。"""
    size_kb = 0
    size_pack_kb = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("size-pack:"):
            try:
                size_pack_kb = int(line.split(":", 1)[1].strip())
            except ValueError:
                size_pack_kb = 0
        elif line.startswith("size:"):
            try:
                size_kb = int(line.split(":", 1)[1].strip())
            except ValueError:
                size_kb = 0
    return size_kb, size_pack_kb


def build_default_gitignore(extra_ignores: Optional[list[str]] = None) -> str:
    """組出 kind=git_init 核准卡要寫進工作機 `.gitignore` 的全文（PLAN.md
    P.2.1）：純函式，不做任何 I/O／SSH。第一行是說明用途的中文註解；接著
    是預設排除清單（`DEFAULT_GITIGNORE_DIRS` + `DEFAULT_GITIGNORE_FILE_
    PATTERNS`）；`extra_ignores` 有值時另起一段附加在後面，各自成一行。

    **不驗證 `extra_ignores` 的字元集**——那是 `request_git_init_approval()`
    建立請求當下的責任（`_GIT_IGNORE_PATTERN_RE`），這裡假設呼叫端已經
    驗證過，維持這個函式是純粹的字串組裝、不會拋例外。

    payload 存這裡回傳的全文，核准卡片用 `<pre>` 顯示，approve() 逐字寫進
    工作機的 `.gitignore`（見該函式 docstring 第 2 步）。
    """
    lines = [
        "# dispatch-center git 化：預設排除大檔資料/輸出/環境目錄與常見權重"
        "檔格式，避免整包 commit 進版控（見 PLAN.md P.2.1）",
        *DEFAULT_GITIGNORE_DIRS,
        *DEFAULT_GITIGNORE_FILE_PATTERNS,
    ]
    if extra_ignores:
        lines.append("")
        lines.append("# 使用者指定的額外排除項目")
        lines.extend(extra_ignores)
    return "\n".join(lines) + "\n"


async def request_git_init_approval(
    db: Database,
    project: str,
    server: str,
    extra_ignores: Optional[list[str]] = None,
    *,
    ssh_run,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=git_init 的核准請求，不真的動任何檔案（PLAN.md P.2.1）：
    真正的 `.gitignore`／`git init`／`git add -A`／size guard／commit 發生
    在 `POST /approve/{id}`（見 `approve()` 的 git_init 分支）。

    建立請求當下（不通過就 400，不建 approval，鐵律第 2 條的延伸）驗證：

    1. `project`＋`server` 必須對應一個已登記的 `project_instance`
       （`app.activity.resolve_project_instance()`，`server` 必填——git_init
       只能對準一台機器，跟 apply_patch 同樣的模式）。
    2. `extra_ignores`（選填）每一項都要符合單行 pattern 字元集
       `[A-Za-z0-9._*/-]+`（`_GIT_IGNORE_PATTERN_RE`），空字串或含換行一律
       不合法。
    3. **SSH 唯讀確認尚不是 git repo**（`test -d {path}/.git`）——已經是的
       話直接拒絕，不建立 approval（核准當下 `approve()` 還會再確認一次，
       雙重防線，見該函式 docstring）。

    payload 存 `project`／`server`／`extra_ignores`／`gitignore`（
    `build_default_gitignore()` 組出的全文，核准卡片 `<pre>` 顯示）。
    """
    extra_ignores = list(extra_ignores or [])
    for pattern in extra_ignores:
        if not pattern or not _GIT_IGNORE_PATTERN_RE.match(pattern):
            raise InvalidGitInitRequestError(
                f"extra_ignores 含不合法的 pattern（僅允許英數字、`.`、`_`、`*`、`/`、`-`）：{pattern!r}"
            )

    try:
        instance = resolve_project_instance(db, project, server)
    except ProjectInstanceResolutionError as exc:
        raise InvalidGitInitRequestError(str(exc)) from exc

    if ssh_run is None:
        raise ValueError("request_git_init_approval 需要 ssh_run，呼叫端未提供")
    check_cmd = f"test -d {shlex.quote(instance.path)}/.git && echo GIT_OK"
    check_result = await ssh_run(server, check_cmd, 10)
    if "GIT_OK" in (check_result.stdout or ""):
        raise InvalidGitInitRequestError(f"專案 {project} 在機器 {server} 已經是 git repo")

    gitignore_text = build_default_gitignore(extra_ignores)
    payload = {
        "project": project,
        "server": server,
        "extra_ignores": extra_ignores,
        "gitignore": gitignore_text,
    }
    approval_id = db.insert_approval(
        kind="git_init",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "git_init",
            "project": project,
            "server": server,
            "extra_ignores": extra_ignores,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


# --------------------------------------------------------------------------
# 階段 13（PLAN.md N 節，Codex Worker v2：Central Codex Runner）：
# coding_task 核准請求＋worktree 任務腳本組裝
# --------------------------------------------------------------------------

#: instruction 全文字元數上限（PLAN.md N.1）。比照 MAX_DIFF_CHARS 的模式，
#: 但 instruction 是自然語言需求（不是 diff），沒必要給到 100KB 那麼大。
MAX_INSTRUCTION_CHARS = 4000

#: base_branch 若提供，必須符合這個白名單——它會被嵌進生成的 `cmd.sh`
#: （git ref 名稱）。雖然 v2 不再把 instruction 拼進 shell，但 base_branch
#: 仍然是確定性組裝進腳本的值，這層字元白名單防呆繼續保留（鐵律：能事先
#: 擋掉的不合理輸入不要留給執行期才發現），只允許 git branch 名稱慣用的
#: 字元。
_BASE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: `projects.repo_or_path` 值「長得像 git remote」的判定前綴（PLAN.md
#: N.3 情況 B）。現制沒有獨立的 `projects.git_remote` 欄位（N.13）。
_GIT_REMOTE_PREFIXES = ("https://", "http://", "git@", "ssh://")


def build_codex_instruction_file(instruction: str, network_access: bool) -> str:
    """組出寫進 `{tasks_dir}/instruction.txt` 的全文（PLAN.md N.5）：固定
    guardrail 前言＋分隔線＋`instruction` 原文。`codex exec` 用 stdin 讀這個
    檔案（`- < instruction.txt`），instruction 全文**永遠不進 shell 字串**
    ——這是 v2 對 v1「shlex-quote 拼 shell」做法的取代（v1 的做法對超長/
    含特殊字元的 instruction 不安全，見 PLAN.md N.13）。

    guardrail 前言要點（繁中，寫給 Codex 讀）：獨立 git worktree 工作、
    修改最終 commit 到獨立 branch、永不 push；不得 sudo、不得安裝或修改
    系統套件、不得動系統 Python；Python 套件只能裝進 worktree 內 `.venv`
    或專案既有隔離環境；系統層依賴缺失時必須在最終回覆說明、不得自行
    提權；目前是否允許連網依 `network_access` 如實告知（即使允許連網，
    guardrail 仍然重申不允許 sudo/系統套件，見 PLAN.md N.1
    `CODEX_NETWORK_ACCESS` 說明）。
    """
    network_status = "允許" if network_access else "停用"
    preamble_lines = [
        "【操作限制（guardrail，請務必遵守）】",
        "- 你目前工作在一個獨立的 git worktree 裡；你所做的所有修改最終會",
        "  commit 到一個獨立的 branch 上，永遠不會被 push 到任何遠端。",
        "- 不得使用 sudo，不得安裝、升級或修改任何系統套件，不得修改系統",
        "  層級的 Python（系統 python/pip）。",
        "- 需要安裝 Python 套件時，只能安裝進這個 worktree 內的 .venv、",
        "  或專案既有的隔離環境（uv/venv/conda）；不得動系統環境。",
        f"- 網路存取目前【{network_status}】。即使網路存取被允許，也不代表",
        "  可以用 sudo 或修改系統套件——上述限制沒有例外。",
        "- 如果任務需要的系統層依賴（例如 apt 套件）缺失、而你自己無法在",
        "  上述限制下安裝，請在最終回覆中如實說明缺少什麼、建議怎麼處理，",
        "  絕不自行提權或繞過這些限制。",
        "",
        "以下【分隔線】之後是實際的任務需求全文：",
    ]
    separator = "=" * 40
    return "\n".join(preamble_lines) + f"\n{separator}\n{instruction}\n"


def _upgrade_coding_script_with_final_path_policy(
    script: str,
    *,
    path_policy_sha256: str,
    path_verifier_sha256: str,
) -> str:
    """Add the v2 final-Git-result gate without changing the v1 script.

    The approved path rules remain data in ``path-policy.json``.  Only the two
    system-generated policy/verifier digests enter the deterministic wrapper;
    the policy also binds that exact verifier-source digest.  No user path is
    ever interpreted by the shell.  Keeping this as an exact transformation
    also means old ``engineering-task-v1`` command journals continue to
    reproduce their original bytes.
    """

    for label, digest in (
        ("path policy", path_policy_sha256),
        ("path verifier", path_verifier_sha256),
    ):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid {label} sha256")

    preflight = (
        '  [ -s "$TASK_DIR/instruction.txt" ] || fail '
        "'instruction.txt 不存在（approve 時應已寫入）'\n"
    )
    v2_preflight = preflight + (
        '  [ -s "$TASK_DIR/path-policy.json" ] || fail '
        "'path-policy.json 不存在（approve 時應已寫入）'\n"
        '  [ -s "$TASK_DIR/path-policy-verifier.py" ] || fail '
        "'path policy verifier 不存在（approve 時應已寫入）'\n"
        '  python3 "$TASK_DIR/path-policy-verifier.py" check-only '
        '--policy "$TASK_DIR/path-policy.json" '
        f"--policy-sha256 {path_policy_sha256} "
        f"--verifier-sha256 {path_verifier_sha256} >/dev/null 2>&1 "
        "|| fail 'approved path policy/verifier contract 無法驗證'\n"
    )
    if script.count(preflight) != 1:
        raise RuntimeError("coding script preflight template drift")
    script = script.replace(preflight, v2_preflight, 1)

    legacy_finalize = r'''  git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A
  if ! git -C "$REPO_DIR" diff --cached --quiet; then
    git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgsign=false -c user.name='dispatch-center' -c user.email='dispatch@local' commit --no-verify -m 'AI coding task #__APPROVAL_ID__' || fail 'commit 失敗'
  fi
  export R_RESULT_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
  if [ "$R_RESULT_COMMIT" = "$R_BASE_COMMIT" ]; then
    export R_NO_CHANGES=1 R_STATUS=no_changes; write_result; log '完成：沒有任何變更（no_changes）'; exit 0
  fi
  SECRET_HITS="$(git -C "$REPO_DIR" diff --name-only "$R_BASE_COMMIT"..HEAD | awk -F/ '{print $NF}' | grep -E -i '^(\.env(\..*)?|\.envrc|auth\.json|credentials.*|secret\..*|secrets.*|id_rsa.*|id_ed25519.*)$|\.pem$|\.key$|\.p12$|\.pfx$' || true)"
  if [ -n "$SECRET_HITS" ]; then export R_STATUS=secret_violation; fail "修改了受保護檔案（不產 bundle）：$SECRET_HITS"; fi
'''
    v2_finalize = rf'''  CURRENT_BRANCH="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD)" || {{ export R_STATUS=path_policy_violation; fail '最終 Git 結果不在 approved task branch（不產 bundle）'; }}
  [ "$CURRENT_BRANCH" = "$R_BRANCH" ] || {{ export R_STATUS=path_policy_violation; fail '最終 Git 結果不在 approved task branch（不產 bundle）'; }}
  git -C "$REPO_DIR" merge-base --is-ancestor "$R_BASE_COMMIT" HEAD || {{ export R_STATUS=path_policy_violation; fail '最終 Git history 已偏離 approved base（不產 bundle）'; }}
  git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A || fail '無法建立最終 Git index'
  FINAL_TREE="$(git -C "$REPO_DIR" write-tree)" || fail '無法建立最終 Git tree'
  BASE_TREE="$(git -C "$REPO_DIR" rev-parse "$R_BASE_COMMIT^{{tree}}")" || fail '無法解析 approved base tree'
  if [ "$FINAL_TREE" = "$BASE_TREE" ]; then
    git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false reset --hard "$R_BASE_COMMIT" >/dev/null || fail '無法收斂 no_changes 結果'
  else
    git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false reset --soft "$R_BASE_COMMIT" || fail '無法收斂最終 Git history'
    git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgsign=false -c user.name='dispatch-center' -c user.email='dispatch@local' commit --no-verify -m 'AI coding task #__APPROVAL_ID__' || fail 'commit 失敗'
  fi
  export R_RESULT_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
  CURRENT_BRANCH="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD)" || {{ export R_STATUS=path_policy_violation; fail '最終 Git branch 驗證失敗（不產 bundle）'; }}
  BRANCH_COMMIT="$(git -C "$REPO_DIR" rev-parse "refs/heads/$R_BRANCH")" || {{ export R_STATUS=path_policy_violation; fail '最終 Git branch ref 不存在（不產 bundle）'; }}
  [ "$CURRENT_BRANCH" = "$R_BRANCH" ] && [ "$BRANCH_COMMIT" = "$R_RESULT_COMMIT" ] || {{ export R_STATUS=path_policy_violation; fail '最終 Git branch/ref 不一致（不產 bundle）'; }}
  git -C "$REPO_DIR" merge-base --is-ancestor "$R_BASE_COMMIT" "$R_RESULT_COMMIT" || {{ export R_STATUS=path_policy_violation; fail '最終 Git result 不是 approved base 後代（不產 bundle）'; }}
  git -C "$REPO_DIR" -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --quiet && git -C "$REPO_DIR" -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --cached --quiet || {{ export R_STATUS=path_policy_violation; fail '最終 Git worktree/index 不乾淨（不產 bundle）'; }}
  [ -z "$(git -C "$REPO_DIR" -c core.fsmonitor=false status --porcelain --untracked-files=all)" ] || {{ export R_STATUS=path_policy_violation; fail '最終 Git worktree 尚有未收斂變更（不產 bundle）'; }}
  python3 "$TASK_DIR/path-policy-verifier.py" verify --policy "$TASK_DIR/path-policy.json" --policy-sha256 {path_policy_sha256} --verifier-sha256 {path_verifier_sha256} --repo "$REPO_DIR" --base "$R_BASE_COMMIT" --result "$R_RESULT_COMMIT" >/dev/null 2>&1
  POLICY_EXIT=$?
  if [ "$POLICY_EXIT" = "42" ]; then export R_STATUS=secret_violation; fail '最終 Git 結果包含受保護檔案（不產 bundle）'; fi
  if [ "$POLICY_EXIT" = "43" ]; then export R_STATUS=path_policy_violation; fail '最終 Git 結果違反 approved path policy（不產 bundle）'; fi
  [ "$POLICY_EXIT" = "0" ] || fail 'path policy verifier/contract 驗證失敗（不產 bundle）'
  if [ "$R_RESULT_COMMIT" = "$R_BASE_COMMIT" ]; then
    export R_NO_CHANGES=1 R_STATUS=no_changes; write_result; log '完成：沒有任何變更（no_changes）'; exit 0
  fi
'''
    if script.count(legacy_finalize) != 1:
        raise RuntimeError("coding script finalization template drift")
    script = script.replace(legacy_finalize, v2_finalize, 1)

    artifact_start = (
        '  export R_DIFF_SUMMARY="$(git -C "$REPO_DIR" --no-pager diff '
        '--no-ext-diff --no-textconv --stat '
        '"$R_BASE_COMMIT"..HEAD | tail -c 4000)"\n'
    )
    # Repository tests execute project code and therefore may mutate Git state.
    # Revalidate the exact ref and policy immediately before packaging so the
    # bundle cannot differ from the result commit that passed the first gate.
    pre_bundle_gate = rf'''  CURRENT_BRANCH="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD)" || {{ export R_STATUS=path_policy_violation; fail '驗證後 Git branch 無法確認（不產 bundle）'; }}
  BRANCH_COMMIT="$(git -C "$REPO_DIR" rev-parse "refs/heads/$R_BRANCH")" || {{ export R_STATUS=path_policy_violation; fail '驗證後 Git branch ref 不存在（不產 bundle）'; }}
  [ "$CURRENT_BRANCH" = "$R_BRANCH" ] && [ "$BRANCH_COMMIT" = "$R_RESULT_COMMIT" ] && [ "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$R_RESULT_COMMIT" ] || {{ export R_STATUS=path_policy_violation; fail '驗證後 Git result 已漂移（不產 bundle）'; }}
  python3 "$TASK_DIR/path-policy-verifier.py" verify --policy "$TASK_DIR/path-policy.json" --policy-sha256 {path_policy_sha256} --verifier-sha256 {path_verifier_sha256} --repo "$REPO_DIR" --base "$R_BASE_COMMIT" --result "$R_RESULT_COMMIT" >/dev/null 2>&1
  POLICY_EXIT=$?
  if [ "$POLICY_EXIT" = "42" ]; then export R_STATUS=secret_violation; fail '驗證後 Git 結果包含受保護檔案（不產 bundle）'; fi
  if [ "$POLICY_EXIT" = "43" ]; then export R_STATUS=path_policy_violation; fail '驗證後 Git 結果違反 approved path policy（不產 bundle）'; fi
  [ "$POLICY_EXIT" = "0" ] || fail '驗證後 path policy/verifier contract 失效（不產 bundle）'
'''
    if script.count(artifact_start) != 1:
        raise RuntimeError("coding script artifact template drift")
    return script.replace(artifact_start, pre_bundle_gate + artifact_start, 1)


def build_coding_task_script(
    approval_id: int,
    workspace_rel: str,
    project: str,
    source_kind: str,
    source: str,
    base_branch: Optional[str],
    network_access: bool,
    exact_base_commit: Optional[str] = None,
    *,
    agent_provider_id: str = CODEX_AGENT_PROVIDER_ID,
    path_policy_sha256: Optional[str] = None,
    path_verifier_sha256: Optional[str] = None,
) -> str:
    """確定性組裝 coding 任務要跑的 `cmd.sh` 全文（PLAN.md N.4/N.5/N.9）。

    這份腳本本身**完全不含 instruction 內文**——instruction 走獨立的
    `instruction.txt`＋stdin（見 `build_codex_instruction_file()`），這裡
    只是一段固定模板，只依 `approval_id`／`workspace_rel`／`project`／
    `source_kind`／`source`／`base_branch`／`network_access` 這些「系統
    已知、不是模型自由輸入」的值做確定性組裝。

    `workspace_rel` 必須是相對 Runner SSH user home 的路徑（不帶 `~/`
    前綴，例如 `codex_workspaces`）——SFTP／`ssh_write_file()` 慣例是
    home-相對路徑（見 `app/jobqueue.py` 的 `AGENT_JOBS_DIR` 說明），呼叫端
    （`approve()`）負責把 `config.codex_workspace_root` 的 `~/` 前綴去掉再
    傳進來。

    腳本大致結構（`main()` 內按序執行，`main 2>&1 | tee task.log` 之後用
    `${PIPESTATUS[0]}` 當最終 exit code——`main` 是 pipeline 左側，若不這樣
    做，`main()` 內部的 `exit` 只會結束它自己所在的 subshell，外層拿到的
    會是 `tee` 的 exit code，不是我們真正關心的那個）：

    1. 前置檢查（`codex`/`git`/`codex login status`/root 檢查/可寫/磁碟
       空間/`instruction.txt` 存在）——任一失敗即 `fail()`：寫
       `R_ERROR`、`write_result()`、`exit 1`。`R_STATUS` 初始值固定是
       `failed`，`fail()` 本身**不覆寫** `R_STATUS`（唯一例外是
       secret_violation，呼叫 `fail()` 前先手動 `export R_STATUS=secret_violation`）。
    2. 依 `source_kind` 取得 repo（PLAN.md N.3）：`instance` → 直接對
       Runner 上既有 project_instance 路徑解出 base commit，**只讀，
       完全不 checkout/不動它的 branch／working tree**；`mirror` → 在
       `{workspace_rel}/mirrors/{project}.git` 建立/更新 bare mirror
       （只從 `source`——即 `projects.repo_or_path` 解出來的 URL——clone，
       不接受模型提供的任意 URL）。兩種情況都用 `git worktree add` 建出
       獨立工作目錄＋新 branch `ai-task-{approval_id}`（重名依序加
       `-2`/`-3`……，用 `git rev-parse --verify --quiet refs/heads/<名>`
       精確檢查，沿用 M 節「不能用子字串比對誤判已存在」的教訓）。
    3. 執行 `codex exec --cd repo --sandbox workspace-write
       -c approval_policy=never --json -o final_message.txt - <
       instruction.txt > codex.jsonl`：0.144.1 沒有 `--ask-for-approval`／
       `--full-auto`，用 `-c approval_policy=never` 顯式釘住非互動語意
       （PLAN.md N.5）；`network_access=True` 才附加
       `-c sandbox_workspace_write.network_access=true`。exit code 非 0
       → `fail()`，但**log 與 worktree 都保留**（PLAN.md N.9 鐵律 10）。
    4. `git add -A` 後才 commit（允許 codex 自己已經 commit 的情況）；
       `result_commit == base_commit` → 標 `no_changes`、直接成功結束
       （鐵律 9：修改後必須有 commit 或明確標記 no_changes）。
    5. secret 檔案守門（鐵律 8）：`git diff --name-only base..HEAD` 命中
       `.env`／`.env.*`（含 `.env.example` 這類範例檔）／`.envrc`／
       `*.pem`／`*.key`／`*.p12`／`*.pfx`／`auth.json`／`credentials*`／
       `secret.*`／`secrets*`／`id_rsa*`／`id_ed25519*` → 標
       `secret_violation`、**不產生 bundle**、`fail()`。
    6. Codex 回合結束後，外層 Runner **不會**直接執行 repository code：
       agent 可修改測試與 import-time code，而外層 shell 不在 Codex sandbox
       內。Legacy 與 immutable task 都在受控 validation sandbox／command
       approval 尚未完成前 fail closed 地跳過自動 pytest，`test_command`／
       `test_exit_code` 保持 `null`。使用者仍可要求 Codex 在其受控回合內執行
       relevant tests，或在結果收妥後走既有核准式 Worker validation。
    7. `git diff --stat`／`git diff`／`git bundle create`＋
       `git bundle verify` 產出 diff summary／`diff.patch`／
       `changes.bundle`，複製進 `$HOME/results/$JOB_ID/`（既有 E 節結果
       回收會自動把這個目錄 rsync 回 control plane，見 N.7 整合決策）。
    8. `write_result()` 用 `python3` 讀 `R_*` 環境變數組 `result.json`
       （避免在 bash 裡手工拼 JSON 字串跳脫問題）——`app.jobfinish` 回填
       `coding_runs` 就是解析這個檔案（PLAN.md N.6）。

    永不 push external origin（腳本裡完全沒有 `git push`）；永不直接修改
    正式 project instance（情況 A 只用 `git -C <instance> worktree add`，
    這個指令本身天然不會 checkout 或改動原 instance 的 working tree／
    HEAD）；永不以 root 執行（`id -u` 檢查）。呼叫端應該用 `bash -n` 驗證
    生成腳本語法（見 tests/test_coding_task.py）。
    """
    if base_branch is not None and not _BASE_BRANCH_RE.match(base_branch):
        raise ValueError(f"base_branch 格式不合法（{base_branch!r}）")
    if source_kind not in ("instance", "mirror", "hub_bundle"):
        raise ValueError(f"未知的 source_kind：{source_kind!r}")
    if source_kind == "hub_bundle":
        if base_branch is not None:
            raise ValueError("hub_bundle source 不接受 base_branch")
        if not exact_base_commit or not re.fullmatch(
            r"[0-9a-fA-F]{40,64}", exact_base_commit
        ):
            raise ValueError("hub_bundle source 需要 exact base commit")
        if not re.fullmatch(
            r"engineering_bundles/[0-9a-f-]{36}\.bundle", source
        ):
            raise ValueError("hub_bundle source path 格式不合法")

    provider = require_coding_agent_provider(agent_provider_id)
    launch = provider.start_turn(
        CodingAgentTurnRequest(network_access=network_access)
    )
    if (
        launch.provider_id != provider.descriptor.provider_id
        or launch.adapter != provider.descriptor.adapter
        or launch.outputs.final_response_file != "final_message.txt"
        or launch.outputs.checkpoint_file is not None
        or launch.outputs.event_stream
        or launch.outputs.machine_event_log_file != "codex.jsonl"
    ):
        raise ValueError("coding agent launch/output contract 與 reviewed provider 不一致")

    workspace_rel = workspace_rel.strip("/")

    # ---- 依 source_kind 組「取得 repo」那一段（定義 R_BASE_COMMIT／gitsrc()）----
    # 批次 3a 硬化：`GIT_ACCESS="git -C $SRC"` 這種「存字串、呼叫處
    # `$GIT_ACCESS ...` 靠 shell 無引號展開斷詞」的寫法，對含空白的路徑
    # （`$SRC`／`$MIRROR` 都可能是使用者在 servers.yaml／registered project
    # 路徑裡填的絕對路徑，理論上可以含空白）不安全——`$GIT_ACCESS` 展開時
    # `-C`／`$SRC` 之間的空白會被當成參數分隔，`$SRC` 裡如果本身有空白會
    # 被錯誤斷成兩個字。改成 bash 函式 `gitsrc()`：`$SRC`/`$MIRROR` 在函式
    # **定義時**已經是加引號賦值好的變數，函式內部用 `"$SRC"`／
    # `"$MIRROR"`（加引號）展開，呼叫處全部是 `gitsrc ...`（函式呼叫，不是
    # 字串展開），路徑含空白不再斷。
    if source_kind == "instance":
        src_quoted = shlex.quote(source)
        if base_branch:
            base_commit_block = (
                f'  R_BASE_COMMIT="$(git -C "$SRC" rev-parse --verify --quiet '
                f"'refs/heads/{base_branch}')\"\n"
                '  [ -n "$R_BASE_COMMIT" ] || fail \'base branch 不存在\'\n'
                "  export R_BASE_COMMIT\n"
            )
        else:
            base_commit_block = (
                '  export R_BASE_COMMIT="$(git -C "$SRC" rev-parse HEAD)"\n'
            )
        repo_block = (
            f"  SRC={src_quoted}\n"
            '  git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1 '
            "|| fail 'instance 路徑不是 git repo'\n"
            f"{base_commit_block}"
            '  gitsrc() { git -C "$SRC" "$@"; }\n'
        )
    elif source_kind == "mirror":
        url_quoted = shlex.quote(source)
        mirror_path = f'"$HOME/{workspace_rel}/mirrors/{project}.git"'
        if base_branch:
            base_commit_block = (
                f'  R_BASE_COMMIT="$(git --git-dir={mirror_path} rev-parse --verify '
                f"--quiet 'refs/heads/{base_branch}')\"\n"
                '  [ -n "$R_BASE_COMMIT" ] || fail \'base branch 不存在\'\n'
                "  export R_BASE_COMMIT\n"
            )
        else:
            base_commit_block = (
                f'  export R_BASE_COMMIT="$(git --git-dir={mirror_path} rev-parse HEAD)"\n'
            )
        repo_block = (
            f"  MIRROR={mirror_path}\n"
            '  if [ -d "$MIRROR" ]; then\n'
            '    git --git-dir="$MIRROR" remote update --prune '
            "|| fail 'mirror 更新失敗'\n"
            "  else\n"
            f'    git clone --mirror {url_quoted} "$MIRROR" || fail \'mirror clone 失敗\'\n'
            "  fi\n"
            f"{base_commit_block}"
            '  gitsrc() { git --git-dir="$MIRROR" "$@"; }\n'
        )
    else:  # hub_bundle：approval payload 已固定 exact ProjectVersion commit
        bundle_path = f'"$HOME/{source}"'
        mirror_path = '"$TASK_DIR/base.git"'
        exact_commit_quoted = shlex.quote(str(exact_base_commit))
        repo_block = (
            f"  BUNDLE={bundle_path}\n"
            f"  MIRROR={mirror_path}\n"
            '  [ -s "$BUNDLE" ] || fail \'ProjectVersion bundle 不存在\'\n'
            '  if [ -d "$MIRROR" ]; then\n'
            '    git --git-dir="$MIRROR" rev-parse --is-bare-repository >/dev/null 2>&1 '
            "|| fail '既有 ProjectVersion mirror 不完整'\n"
            '  else\n'
            '    git clone --mirror "$BUNDLE" "$MIRROR" '
            "|| fail 'ProjectVersion bundle clone 失敗'\n"
            '  fi\n'
            f"  export R_BASE_COMMIT={exact_commit_quoted}\n"
            '  git --git-dir="$MIRROR" cat-file -e "$R_BASE_COMMIT^{commit}" '
            "|| fail 'bundle 不含 approved base commit'\n"
            '  RESOLVED_BASE="$(git --git-dir="$MIRROR" rev-parse --verify '
            '"$R_BASE_COMMIT^{commit}")"\n'
            '  [ "$RESOLVED_BASE" = "$R_BASE_COMMIT" ] '
            "|| fail 'bundle base commit 驗證不一致'\n"
            '  gitsrc() { git --git-dir="$MIRROR" "$@"; }\n'
        )

    # The repository has just been modified by an untrusted agent.  Running
    # pytest here would execute attacker-controlled Python as the Runner OS
    # user, outside Codex's workspace-write/network sandbox.  Leave structured
    # validation unset until the separately reviewed controlled-validation
    # slice exists.  This applies to legacy and immutable sources alike.
    validation_block = (
        "  log '自動 repository validation 已安全跳過："
        "受控 validation sandbox 尚未啟用'\n"
    )

    script = r"""set -u
JOB_DIR="$(cd "$(dirname "$0")" && pwd)"
JOB_ID="$(basename "$JOB_DIR")"
TASK_DIR="$HOME/__WORKSPACE_REL__/tasks/__APPROVAL_ID__"
REPO_DIR="$TASK_DIR/repo"
RESULTS_DIR="$HOME/results/$JOB_ID"
log() { echo "[coding_task] $*"; }

export R_STATUS=failed R_ERROR="" R_BASE_COMMIT="" R_BRANCH="" R_RESULT_COMMIT="" \
       R_NO_CHANGES=0 R_TEST_CMD="" R_TEST_EXIT="" R_CODEX_EXIT="" R_DIFF_SUMMARY="" R_CODEX_VERSION=""
write_result() {
  python3 - "$TASK_DIR/result.json" <<'PYEOF'
import json, os, sys
def num(name):
    v = os.environ.get(name) or ""
    return int(v) if v else None
json.dump({
  "status": os.environ.get("R_STATUS"),
  "error_message": os.environ.get("R_ERROR") or None,
  "base_commit": os.environ.get("R_BASE_COMMIT") or None,
  "result_branch": os.environ.get("R_BRANCH") or None,
  "result_commit": os.environ.get("R_RESULT_COMMIT") or None,
  "no_changes": os.environ.get("R_NO_CHANGES") == "1",
  "test_command": os.environ.get("R_TEST_CMD") or None,
  "test_exit_code": num("R_TEST_EXIT"),
  "codex_exit": num("R_CODEX_EXIT"),
  "diff_summary": os.environ.get("R_DIFF_SUMMARY") or None,
  "codex_version": os.environ.get("R_CODEX_VERSION") or None,
}, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PYEOF
  mkdir -p "$RESULTS_DIR"; cp -f "$TASK_DIR/result.json" "$RESULTS_DIR/result.json" 2>/dev/null || true
}
fail() { export R_ERROR="$1"; log "FAIL: $1"; write_result; exit 1; }

main() {
  mkdir -p "$TASK_DIR" || { log "無法建立 TASK_DIR"; exit 1; }
  command -v codex >/dev/null 2>&1 || fail 'codex CLI 未安裝：請照 README §13 在 Codex Runner 安裝並登入'
  export R_CODEX_VERSION="$(codex --version 2>/dev/null | head -1)"
  codex login status >/dev/null 2>&1 || fail 'codex 未登入：請在 Runner 執行 codex login（或 codex login --with-api-key）'
  [ "$(id -u)" != "0" ] || fail '拒絕以 root 執行 Codex（PLAN.md N.9 鐵律 6）'
  git --version >/dev/null 2>&1 || fail 'git 不存在'
  ( touch "$TASK_DIR/.wtest" && rm -f "$TASK_DIR/.wtest" ) || fail 'workspace 不可寫'
  AVAIL_KB="$(df -Pk "$TASK_DIR" | awk 'NR==2{print $4}')"
  [ "${AVAIL_KB:-0}" -ge 1048576 ] || fail '磁碟空間不足（<1GB）'
  [ -s "$TASK_DIR/instruction.txt" ] || fail 'instruction.txt 不存在（approve 時應已寫入）'

__REPO_BLOCK__
  BR="ai-task-__APPROVAL_ID__"; N=2
  while [ -n "$(gitsrc rev-parse --verify --quiet "refs/heads/$BR")" ] && [ $N -lt 50 ]; do BR="ai-task-__APPROVAL_ID__-$N"; N=$((N+1)); done
  gitsrc worktree add "$REPO_DIR" -b "$BR" "$R_BASE_COMMIT" || fail 'worktree 建立失敗'
  export R_BRANCH="$BR"

__AGENT_START_COMMAND__
  export R_CODEX_EXIT=$?
  cp -f "$TASK_DIR/final_message.txt" "$RESULTS_DIR/final_message.txt" 2>/dev/null || true
  [ "$R_CODEX_EXIT" = "0" ] || fail "codex exec 失敗（exit $R_CODEX_EXIT），log 與 worktree 已保留供人工檢查"

  git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A
  if ! git -C "$REPO_DIR" diff --cached --quiet; then
    git -C "$REPO_DIR" -c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgsign=false -c user.name='dispatch-center' -c user.email='dispatch@local' commit --no-verify -m 'AI coding task #__APPROVAL_ID__' || fail 'commit 失敗'
  fi
  export R_RESULT_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
  if [ "$R_RESULT_COMMIT" = "$R_BASE_COMMIT" ]; then
    export R_NO_CHANGES=1 R_STATUS=no_changes; write_result; log '完成：沒有任何變更（no_changes）'; exit 0
  fi
  SECRET_HITS="$(git -C "$REPO_DIR" diff --name-only "$R_BASE_COMMIT"..HEAD | awk -F/ '{print $NF}' | grep -E -i '^(\.env(\..*)?|\.envrc|auth\.json|credentials.*|secret\..*|secrets.*|id_rsa.*|id_ed25519.*)$|\.pem$|\.key$|\.p12$|\.pfx$' || true)"
  if [ -n "$SECRET_HITS" ]; then export R_STATUS=secret_violation; fail "修改了受保護檔案（不產 bundle）：$SECRET_HITS"; fi

__POST_AGENT_VALIDATION_BLOCK__

  export R_DIFF_SUMMARY="$(git -C "$REPO_DIR" --no-pager diff --no-ext-diff --no-textconv --stat "$R_BASE_COMMIT"..HEAD | tail -c 4000)"
  git -C "$REPO_DIR" --no-pager diff --no-ext-diff --no-textconv "$R_BASE_COMMIT"..HEAD > "$TASK_DIR/diff.patch"
  git -C "$REPO_DIR" bundle create "$TASK_DIR/changes.bundle" "$R_BASE_COMMIT..$R_BRANCH" || fail 'bundle 建立失敗'
  git -C "$REPO_DIR" bundle verify "$TASK_DIR/changes.bundle" >/dev/null 2>&1 || fail 'bundle 驗證失敗'
  mkdir -p "$RESULTS_DIR" || fail '結果目錄建立失敗'
  cp -f "$TASK_DIR/diff.patch" "$TASK_DIR/changes.bundle" "$RESULTS_DIR/" || fail '結果檔複製失敗'
  export R_STATUS=done; write_result
  log "完成：branch=$R_BRANCH result_commit=$R_RESULT_COMMIT"
}
main 2>&1 | tee "$TASK_DIR/task.log"
exit "${PIPESTATUS[0]}"
"""
    if path_policy_sha256 is not None or path_verifier_sha256 is not None:
        if path_policy_sha256 is None or path_verifier_sha256 is None:
            raise ValueError("path policy 與 verifier digest 必須同時提供")
        script = _upgrade_coding_script_with_final_path_policy(
            script,
            path_policy_sha256=path_policy_sha256,
            path_verifier_sha256=path_verifier_sha256,
        )
    script = script.replace("__WORKSPACE_REL__", workspace_rel)
    script = script.replace("__APPROVAL_ID__", str(approval_id))
    script = script.replace("__REPO_BLOCK__", repo_block.rstrip("\n"))
    script = script.replace("__AGENT_START_COMMAND__", launch.shell_command)
    script = script.replace(
        "__POST_AGENT_VALIDATION_BLOCK__", validation_block.rstrip("\n")
    )
    return script


def request_coding_task_approval(
    db: Database,
    project: str,
    instruction: str,
    base_branch: Optional[str] = None,
    validation_target: Optional[str] = None,
    *,
    config: AppConfig,
    server_enabled: dict[str, bool],
    legacy_server: Optional[str] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=coding_task 的核准請求（PLAN.md N.2，Codex Worker v2）：
    核准的是一段自然語言需求（instruction），不是像 apply_patch 那樣人已經
    看過的 diff——受控方式是「核准後由程式確定性組裝的 `cmd.sh` 在獨立
    worktree／branch 上跑 `codex exec`，instruction 全文走獨立檔案＋stdin、
    永不進 shell 字串，永不 push external origin」（見
    `build_coding_task_script()`／`build_codex_instruction_file()`／
    `approve()` 的 coding_task 分支）。

    **v2 與 v1 的關鍵差異**（PLAN.md N.13）：呼叫端不再指定 Codex 執行
    `server`——執行機器永遠是 `.env` 的 `CODEX_RUNNER_SERVER`（唯一一台
    Central Codex Runner）；`server` 參數改叫 `legacy_server`，只為了相容
    舊客戶端（見下方第 2 點）。

    建立請求當下（不過就丟例外、不建 approval，鐵律第 2 條的延伸）依序
    驗證：

    1. `config.codex_runner_server is None` → Codex 功能整體停用，
       「未設定 CODEX_RUNNER_SERVER，Codex 功能停用」。
    2. 舊客戶端相容：`legacy_server` 有值時——等於 Runner → 接受（payload
       標記 `legacy_server_param: true`，稽核附 deprecation 說明）；不等於
       → 400「Codex 執行機器已固定為 CODEX_RUNNER_SERVER（{runner}），
       不接受指定其他 server」，**絕不**把 coding job 派到其他機器。
    3. `project` 必須存在（`db.get_project(project)`）。
    4. `instruction` 非空（strip 後）且字元數 ≤ `MAX_INSTRUCTION_CHARS`
       （4000）。
    5. `base_branch` 若提供：必須符合 `_BASE_BRANCH_RE` 白名單。
    6. `validation_target` 若提供：必須在 `server_enabled` 且值為
       `True`（可以等於 Runner 本身——它只代表後續驗證想在哪台機器跑，
       不代表 Codex 執行位置，見 PLAN.md N.2 第 2 點）。
    7. **N.3 三段式 repo 來源判定**（依序）：
       - 情況 A：Runner 上已有這個 project 的 `project_instance`
         （`resolve_project_instance(db, project, runner)`）→
         `source_kind="instance"`，`source=instance.path`。
       - 情況 B：找不到 Runner 上的 instance，但
         `projects.repo_or_path` 長得像 git remote（`https://`／
         `http://`／`git@`／`ssh://` 開頭）→ `source_kind="mirror"`，
         `source=repo_or_path`。**不接受模型提供任意 Git URL**——URL 只
         來自 `projects` 註冊表。
       - 情況 C：兩者皆無 → 400，錯誤訊息固定：「此專案沒有 Codex
         Runner instance，也沒有可用的 git_remote。請先匯入專案到
         Codex Runner 或登記 git_remote。」
    8. **不再對 instruction 組出來的指令做 `is_dangerous()` 掃描**（v1
       才有這一步）：v2 的 instruction 只會流進 `instruction.txt`＋
       stdin，`cmd.sh` 是不含 instruction 內文的固定模板
       （`build_coding_task_script()`），對這種確定性模板跑黑名單掃描
       沒有意義；`enqueue_job()` 仍會對組好的 `cmd.sh` 本身跑一次
       `is_dangerous()`（既有 enqueue 鐵律，跟所有 kind 一樣，不是
       coding_task 專屬防線）。

    payload 存 `{project, instruction, base_branch, validation_target,
    runner_server, source_kind, source, network_access}`（＋選填
    `legacy_server_param: true`），核准卡片前端用跟 apply_patch diff 相同
    的 `<pre>` 捲動框顯示 instruction 全文；`network_access` 抄一份
    `config.codex_network_access` 現值供核准卡顯示（PLAN.md N.10）。

    **只加在 MCP bridge（ChatGPT），不加給 vLLM `app/agent_tools.py`**——
    理由同 M.2（apply_patch）：7B 模型寫出來的 instruction 品質不可靠，只
    會製造核准垃圾。**這個 kind 永遠不會被 `maybe_auto_approve()` 自動
    核准**（該函式的 kind 白名單只有 "enqueue"/"stop"）。
    """
    runner = config.codex_runner_server
    if runner is None:
        raise InvalidCodingTaskRequestError("未設定 CODEX_RUNNER_SERVER，Codex 功能停用")

    legacy_server_param = False
    if legacy_server is not None:
        if legacy_server != runner:
            raise InvalidCodingTaskRequestError(
                f"Codex 執行機器已固定為 CODEX_RUNNER_SERVER（{runner}），"
                "不接受指定其他 server"
            )
        legacy_server_param = True

    if db.get_project(project) is None:
        raise InvalidCodingTaskRequestError(f"專案 {project} 不存在")

    if not instruction or not instruction.strip():
        raise InvalidCodingTaskRequestError("instruction 不可為空")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise InvalidCodingTaskRequestError(
            f"instruction 過長（{len(instruction)} 字元，上限 {MAX_INSTRUCTION_CHARS} 字元）"
        )
    try:
        reject_engineering_raw_credentials(instruction)
    except InvalidEngineeringTaskRequestError as exc:
        # Slice-1's structured wizard intentionally uses this compatibility
        # endpoint.  Apply the same pre-persistence credential boundary as the
        # native endpoint so approval payloads and audit-visible API responses
        # can never become a secret transport merely because the backend flag
        # is still disabled.
        raise InvalidCodingTaskRequestError(str(exc)) from None

    if base_branch is not None and not _BASE_BRANCH_RE.match(base_branch):
        raise InvalidCodingTaskRequestError(
            f"base_branch 格式不合法（{base_branch!r}），只允許英數字與 . _ / -"
        )

    if validation_target is not None and not server_enabled.get(validation_target, False):
        raise InvalidCodingTaskRequestError(
            f"validation_target={validation_target!r} 不是已啟用的 server"
        )

    # N.3 三段式 repo 來源判定：情況 A（Runner 上既有 instance）優先；
    # 找不到（含「project 完全沒有任何 instance」）進情況 B；都沒有 -> C。
    try:
        instance = resolve_project_instance(db, project, runner)
        source_kind = "instance"
        source = instance.path
    except ProjectInstanceResolutionError:
        project_row = db.get_project(project)
        repo_or_path = (project_row.repo_or_path if project_row else "") or ""
        if repo_or_path.startswith(_GIT_REMOTE_PREFIXES):
            source_kind = "mirror"
            source = repo_or_path
        else:
            raise InvalidCodingTaskRequestError(
                "此專案沒有 Codex Runner instance，也沒有可用的 git_remote。"
                "請先匯入專案到 Codex Runner 或登記 git_remote。"
            )

    if source_kind == "mirror":
        try:
            reject_engineering_raw_credentials(source)
        except InvalidEngineeringTaskRequestError:
            # Never echo a credential-bearing registered URL into the error,
            # audit record, or approval payload.
            raise InvalidCodingTaskRequestError(
                "專案 git_remote 含有 raw credential；請改用平台管理的認證設定"
            ) from None

    payload = {
        "project": project,
        "instruction": instruction,
        "base_branch": base_branch,
        "validation_target": validation_target,
        "runner_server": runner,
        "source_kind": source_kind,
        "source": source,
        "network_access": config.codex_network_access,
    }
    if legacy_server_param:
        payload["legacy_server_param"] = True

    approval_id = db.insert_approval(
        kind="coding_task",
        payload=payload,
        requester_actor_id=_actor_id(request_context),
    )
    audit_params = {
        "approval_id": approval_id,
        "kind": "coding_task",
        "project": project,
        "runner_server": runner,
        "base_branch": base_branch,
        "validation_target": validation_target,
        "source_kind": source_kind,
        "instruction_chars": len(instruction),
    }
    if legacy_server_param:
        audit_params["legacy_server_param"] = True
        audit_params["deprecation_warning"] = (
            "舊客戶端仍帶 server 參數；Codex 執行機器已固定為 "
            f"CODEX_RUNNER_SERVER（{runner}）"
        )
    append_audit(
        "approval_requested",
        audit_params,
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


async def request_engineering_task_approval(
    db: Database,
    project: str,
    *,
    project_version_id: str,
    agent_provider_id: str,
    structured_request: dict[str, Any],
    config: AppConfig,
    server_enabled: dict[str, bool],
    local_run,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> tuple[Any, Approval]:
    """建立 ProjectVersion-pinned AI Engineering Task 與 coding_task approval。

    這條新路徑與 legacy ``request_coding_task_approval`` 並存。建立當下只做
    Server A Hub 唯讀驗證與 deterministic metadata detection；bundle 建立、
    推送與 coding Job 都留到人工核准後。
    """

    if not config.engineering_task_backend_v1:
        raise InvalidEngineeringTaskRequestError("AI Engineering Task backend 未啟用")
    provider = get_coding_agent_provider(agent_provider_id)
    if provider is None or not provider.descriptor.capabilities.start_turn:
        raise InvalidEngineeringTaskRequestError(
            f"未核准或無法啟動的 coding agent provider：{agent_provider_id!r}"
        )
    descriptor = provider.descriptor
    # Goal 3 Phase D-1：新 task 綁定的 Runner 由 pool 決定性選擇（單
    # Runner 配置回傳 primary，行為不變）；綁定寫進 task.runner_server 與
    # execution_contract，之後 retry 的一致性檢查以綁定值為準，不漂移。
    runner = select_codex_runner(db, config)
    if runner is None:
        raise InvalidEngineeringTaskRequestError("未設定 CODEX_RUNNER_SERVER，Codex 功能停用")
    if not server_enabled.get(runner, False):
        raise InvalidEngineeringTaskRequestError("CODEX_RUNNER_SERVER 不存在或未啟用")

    project_row = db.get_project(project)
    if project_row is None:
        raise InvalidEngineeringTaskRequestError(f"專案 {project} 不存在")
    if not project_row.id:
        raise InvalidEngineeringTaskRequestError("專案尚無 canonical project id")
    version = db.get_project_version(project_version_id)
    if version is None:
        raise InvalidEngineeringTaskRequestError("ProjectVersion 不存在")
    if version.project_name != project or version.project_id != project_row.id:
        raise InvalidEngineeringTaskRequestError("ProjectVersion 不屬於目前這個 Project")

    normalized = normalize_engineering_task_spec(structured_request)
    try:
        path_policy, path_policy_sha256 = build_engineering_path_policy(
            normalized["allowed_paths"],
            normalized["prohibited_paths"],
        )
    except EngineeringPathPolicyError as exc:
        raise InvalidEngineeringTaskRequestError(
            "final Git path policy 無法建立；請重新檢查 allowed/prohibited paths"
        ) from exc
    validation_target = normalized["validation"]["worker_validation_target"]
    if validation_target is not None and not server_enabled.get(validation_target, False):
        raise InvalidEngineeringTaskRequestError(
            f"worker_validation_target={validation_target!r} 不是已啟用的 server"
        )
    instruction = render_engineering_task_instruction(normalized)
    exact_commit, detected_metadata = await inspect_hub_project_version(
        project_name=project,
        git_commit=version.git_commit,
        local_home_dir=config.local_home_dir,
        local_run=local_run,
    )

    task_id = str(uuid.uuid4())
    capabilities = descriptor.capability_snapshot()
    contract_version = ENGINEERING_TASK_CONTRACT_V2
    runner_cfg = next(
        (server for server in config.servers if server.name == runner), None
    )
    if runner_cfg is None or not runner_cfg.enabled:
        raise InvalidEngineeringTaskRequestError(
            "CODEX_RUNNER_SERVER 設定不存在或已停用"
        )
    workspace_rel = resolve_codex_workspace_rel(config.codex_workspace_root)
    execution_contract = {
        "runner": {
            "name": runner_cfg.name,
            "host": runner_cfg.host,
            "user": runner_cfg.user,
            "port": runner_cfg.port,
        },
        "workspace_rel": workspace_rel,
        "source_kind": "hub_bundle",
        "source": remote_engineering_bundle_path(task_id),
        "network_access": False,
        "dependency_installation": False,
        "path_policy": path_policy,
        "path_policy_sha256": path_policy_sha256,
        "path_verifier": path_policy["verifier"],
    }
    payload = {
        "contract_version": contract_version,
        "engineering_task_id": task_id,
        "project": project,
        "project_id": project_row.id,
        "project_version_id": version.id,
        "base_commit": exact_commit,
        "agent_provider_id": agent_provider_id,
        "provider_capabilities": capabilities,
        "execution_contract": execution_contract,
        "structured_request": normalized,
        "instruction": instruction,
        "detected_metadata": detected_metadata,
        "validation_target": validation_target,
        "runner_server": runner,
        "source_kind": "hub_bundle",
        "source": remote_engineering_bundle_path(task_id),
        "network_access": False,
        "dependency_installation": False,
    }
    task_id, approval_id = db.insert_engineering_task_request(
        project_id=project_row.id,
        project_name=project,
        project_version_id=version.id,
        base_commit=exact_commit,
        agent_provider_id=agent_provider_id,
        provider_capabilities=capabilities,
        execution_contract=execution_contract,
        contract_version=contract_version,
        structured_request=normalized,
        instruction=instruction,
        detected_metadata=detected_metadata,
        runner_server=runner,
        validation_target=validation_target,
        approval_payload=payload,
        requester_actor_id=_actor_id(request_context),
        task_id=task_id,
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "coding_task",
            "engineering_task_id": task_id,
            "project": project,
            "project_version_id": version.id,
            "base_commit": exact_commit,
            "agent_provider_id": agent_provider_id,
            "runner_server": runner,
            "contract_version": contract_version,
            "path_policy_sha256": path_policy_sha256,
            "instruction_chars": len(instruction),
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_engineering_task(task_id), db.get_approval(approval_id)


def _canonical_payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_engineering_task_retry_approval(
    db: Database,
    task_id: str,
    *,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=engineering_task_retry 的 pending approval（D3 第一批）。

    只做唯讀資格檢查、不接觸 Runner／Hub／SSH，也不建立任何 Job：真正重新
    驗證 contract 並原子建立 attempt N+1 的 staging/coding Job 留給
    `approve()` 的對應分支（核准時必須用當下的 DB／config 狀態重新確認一次，
    而不是信任這裡算出的值——這裡的 payload 只是不可變快照）。

    資格：task 存在、parent `coding_task` approval 仍是 approved、task 目前
    狀態是終態（`CODING_RUN_TERMINAL_STATUSES`——與 `cleanup_coding_run()`
    的可清理判定同一組常數）、且沒有任何 queued/running 的 owner Job 還在
    引用這個 task（不論哪個 attempt）。下一個 attempt number 由目前最大
    attempt 的下一位算出；核准時用資料庫當下狀態重算一次，不信任這裡的值。
    """

    task = db.get_engineering_task(task_id)
    if task is None:
        raise InvalidEngineeringTaskRequestError("AI Engineering Task 不存在")
    parent_approval = db.get_approval(task.approval_id)
    if (
        parent_approval is None
        or parent_approval.kind != "coding_task"
        or parent_approval.status != "approved"
    ):
        raise InvalidEngineeringTaskRequestError(
            "parent coding_task approval 不是 approved 狀態，不能建立 retry request"
        )
    if task.status not in CODING_RUN_TERMINAL_STATUSES:
        raise InvalidEngineeringTaskRequestError(
            f"task 目前狀態 {task.status!r} 不是終態，不能建立 retry request"
        )
    active_jobs = [
        job
        for job in db.list_engineering_task_jobs(task_id)
        if job.status in {"queued", "running"}
    ]
    if active_jobs:
        raise InvalidEngineeringTaskRequestError(
            "此 task 仍有 queued/running 的 owner Job，不能建立 retry request"
        )
    attempts = db.list_engineering_task_attempt_runs(task_id)
    if not attempts:
        raise InvalidEngineeringTaskRequestError("此 task 尚無任何 attempt，無法 retry")
    next_attempt_number = attempts[-1].attempt_number + 1

    payload = {
        "engineering_task_id": task.id,
        "attempt_number": next_attempt_number,
        "project": task.project_name,
        "project_id": task.project_id,
        "project_version_id": task.project_version_id,
        "base_commit": task.base_commit,
        "runner_server": task.runner_server,
        "parent_approval_id": task.approval_id,
        "parent_approval_payload_sha256": _canonical_payload_digest(
            parent_approval.payload
        ),
    }
    approval_id = db.insert_approval(
        "engineering_task_retry",
        payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "engineering_task_retry",
            "engineering_task_id": task.id,
            "attempt_number": next_attempt_number,
            "project": task.project_name,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


def request_engineering_task_discard_approval(
    db: Database,
    task_id: str,
    *,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=engineering_task_discard 的 pending approval（D3 第一批）。

    資格與 retry 共用同一個終態判定：task 必須存在且目前狀態是終態，且沒有
    任何 queued/running 的 owner Job 還在引用這個 task。核准後只標記
    `engineering_tasks.status = 'discarded'` 並讓可見性/下載端點視同
    withheld；不刪除 Runner 上的工作區（那仍是既有 `cleanup_coding_run()`
    的手動後續步驟）。
    """

    task = db.get_engineering_task(task_id)
    if task is None:
        raise InvalidEngineeringTaskRequestError("AI Engineering Task 不存在")
    if task.status not in CODING_RUN_TERMINAL_STATUSES:
        raise InvalidEngineeringTaskRequestError(
            f"task 目前狀態 {task.status!r} 不是終態，不能建立 discard request"
        )
    active_jobs = [
        job
        for job in db.list_engineering_task_jobs(task_id)
        if job.status in {"queued", "running"}
    ]
    if active_jobs:
        raise InvalidEngineeringTaskRequestError(
            "此 task 仍有 queued/running 的 owner Job，不能建立 discard request"
        )

    payload = {
        "engineering_task_id": task.id,
        "project": task.project_name,
        "project_id": task.project_id,
        "task_status_at_request": task.status,
    }
    approval_id = db.insert_approval(
        "engineering_task_discard",
        payload,
        requester_actor_id=_actor_id(request_context),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "engineering_task_discard",
            "engineering_task_id": task.id,
            "project": task.project_name,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)


# --------------------------------------------------------------------------
# 階段 13（PLAN.md N.7，Codex Worker v2）：changes.bundle 下游流——後續
# train／驗證 job 可選填 `source_coding_run_id`，核准時（kind=enqueue 分支）
# 額外建一個推送任務＋在主任務 command 前面加確定性的 bundle checkout 前置段
# ---------------------------------------------------------------------------


def resolve_codex_workspace_rel(workspace_root: str) -> str:
    """把 `CODEX_WORKSPACE_ROOT` 正規化成相對 Runner SSH user home 的路徑
    （去掉 `~/` 前綴）——`ssh_write_file()`/SFTP 是 home-相對路徑慣例（見
    `app/jobqueue.py` 的 `AGENT_JOBS_DIR` 說明）。設定成絕對路徑（`/...`）
    直接 `raise ValueError`。`approve()` 的 coding_task 分支與
    `cleanup_coding_run()` 共用這個轉換，行為必須一致（原本兩處各自重複
    這段邏輯，這裡抽成共用純函式，避免兩邊漂移）。
    """
    if not isinstance(workspace_root, str):
        raise ValueError("CODEX_WORKSPACE_ROOT 必須是字串")
    if workspace_root.startswith("~/"):
        rel = workspace_root[2:]
    elif workspace_root.startswith("/"):
        raise ValueError(
            "CODEX_WORKSPACE_ROOT 必須是相對 Runner SSH user home 的路徑"
            f"（不能是絕對路徑），目前是 {workspace_root!r}"
        )
    else:
        rel = workspace_root
    rel = rel.strip("/")
    parts = rel.split("/") if rel else []
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts)
    ):
        raise ValueError(
            "CODEX_WORKSPACE_ROOT 必須是安全的 home-relative path，"
            "且不能包含空白、空 segment、`.` 或 `..`"
        )
    return rel


def build_bundle_checkout_preamble(
    coding_run_id: int, result_commit: str, instance_path: str
) -> str:
    """組出下游 train／驗證 job 的 `cmd.sh` 開頭一段**確定性**前置段
    （PLAN.md N.7）：把已經由 `build_bundle_push_command()`（見
    `approve()` 的 enqueue 分支）推送到這台目標機
    `~/coding_bundles/{coding_run_id}/changes.bundle` 的 bundle 驗證、
    `fetch` 進 `instance_path` 這個既有 project instance 的 git repo，再用
    **獨立 worktree**（`git worktree add --detach`——`instance_path`
    本身完全不被 checkout／改動，鐵律第 5 條「永不直接修改正式 project
    instance」的延伸：這裡雖然不是 Codex 在寫，跑的是使用者自己核准的
    command，但取得 repo 的方式要跟 N.3 情況 A 一致，不能直接改動正式
    working tree／branch）簽出 `result_commit`，最後 `cd` 進這個 worktree，
    讓接在這段後面的使用者命令在正確目錄下執行（README 批次 4 會說明使用者
    命令要寫相對路徑、不要自己再 `cd`）。

    **刻意不用 `rm -rf`**——這段組出來的完整指令（前置段＋使用者 command）
    仍然會原樣傳進 `enqueue_job()`，跟所有其他 enqueue 指令一樣要過一次
    `is_dangerous()` 黑名單；用 `git worktree remove --force` 語意更精確，
    也保證不會不小心撞上 `rm -rf` pattern 被誤擋。`instance_path` 只來自
    `resolve_project_instance()` 的回傳值（系統解析，非使用者輸入）；
    `result_commit` 只來自 `coding_runs.result_commit`（Codex 任務腳本
    `write_result()` 寫入，同樣非使用者自由輸入）——兩者仍用
    `shlex.quote()` 包起來做防禦性處理（路徑/commit 值理論上不會有需要
    跳脫的字元，但比照全專案「系統值也 quote」的既有慣例）。
    """
    q_instance = shlex.quote(instance_path)
    q_result = shlex.quote(result_commit)
    lines = [
        f'CR_BUNDLE="$HOME/coding_bundles/{coding_run_id}/changes.bundle"',
        f'CR_WT="$HOME/codex_validation/{coding_run_id}/repo"',
        '[ -f "$CR_BUNDLE" ] || { echo \'[coding_bundle] 找不到 changes.bundle（推送任務失敗？）\'; exit 1; }',
        f'git -C {q_instance} bundle verify "$CR_BUNDLE" >/dev/null 2>&1 || '
        "{ echo '[coding_bundle] bundle 驗證失敗'; exit 1; }",
        f'git -C {q_instance} fetch "$CR_BUNDLE" '
        f"'+refs/heads/*:refs/coding-runs/{coding_run_id}/*' || exit 1",
        f"git -C {q_instance} cat-file -e {q_result} || "
        f"{{ echo '[coding_bundle] bundle 不含 result commit {result_commit}'; exit 1; }}",
        f'git -C {q_instance} worktree remove --force "$CR_WT" >/dev/null 2>&1 || true',
        f'git -C {q_instance} worktree add --detach "$CR_WT" {q_result} || exit 1',
        'cd "$CR_WT" || exit 1',
        f'echo "[coding_bundle] 驗證環境就緒：coding_run #{coding_run_id} @ '
        f'{result_commit}（使用者命令在此 worktree 內執行）"',
    ]
    return "\n".join(lines) + "\n"


class CodingRunNotFoundError(Exception):
    """`cleanup_coding_run()` 指定的 coding_run id 不存在。"""


class CodingRunNotCleanableError(ValueError):
    """coding_run 還不能清理（PLAN.md N.9 鐵律 11）：狀態不是終態，或仍有
    queued/running job 以 `source_coding_run_id` 引用它。"""


#: `cleanup_coding_run()` 允許清理的終態集合（PLAN.md N.9 鐵律 10/11）。
CODING_RUN_TERMINAL_STATUSES = {
    "done",
    "failed",
    "no_changes",
    "secret_violation",
    "path_policy_violation",
}


async def cleanup_coding_run(
    db: Database,
    coding_run_id: int,
    *,
    ssh_run,
    config: AppConfig,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> dict:
    """`POST /coding-runs/{id}/cleanup`（PLAN.md N.9 鐵律 11，web 觸發，
    **不給 MCP**）：刪掉 Runner 上 `CODEX_WORKSPACE_ROOT/tasks/{approval_id}`
    這個 task 目錄（獨立 worktree／instruction.txt／codex.jsonl 等中間產物
    ——不是 `results/{job_id}/` 那份已經回收的最終結果，那份不受這個端點
    影響）。

    - run 不存在 -> `CodingRunNotFoundError`（呼叫端轉 404）。
    - `run.status` 不在 `CODING_RUN_TERMINAL_STATUSES`（還在
      queued/running）-> `CodingRunNotCleanableError`（呼叫端轉 409）：
      失敗保留 log 與 worktree 供人工檢查是鐵律第 10 條，清理前必須先跑到
      終態。
    - 有任何 queued／running 的 job 以 `source_coding_run_id == coding_run_id`
      引用這次 run（下游 train／驗證任務可能還沒 checkout 完 bundle）
      -> 同樣 `CodingRunNotCleanableError`（409）：不能刪掉還在被引用的
      worktree／bundle。

    路徑由系統組裝（`config.codex_workspace_root` 轉換出的 `workspace_rel`
    ＋`run.approval_id`，兩者都不是使用者輸入）並驗證過（見
    `resolve_codex_workspace_rel()`），**不經 `app.jobqueue.enqueue_job()`，
    不受 enqueue 的 `is_dangerous()` 黑名單語境**——這是刻意的邊界：這裡的
    `rm -rf` 是系統對著自己組出來、已知安全的相對路徑操作，不是使用者可以
    影響的指令字串，跟「使用者提供的 command 一律要過黑名單」是兩件事，直接
    用 `ssh_run()` 執行、不入列成一個 job（PLAN.md N.9 鐵律 11 原文）。

    `source_kind == "instance"`（從對應 approval payload 取得）時額外對
    `source`（instance 路徑）跑一次 `git worktree prune`——情況 A 的 coding
    task 在這個 instance 上用 `git worktree add` 建過獨立 worktree（見
    `build_coding_task_script()`），task 目錄被 `rm -rf` 之後，instance 端
    會留下失效的 worktree 註冊資訊，`git worktree prune` 清掉這個殘留
    引用（**不影響 instance 本身的 branch／working tree**，只是清 metadata）。

    成功：`db.update_coding_run(coding_run_id, worktree_path=None)`（DB
    如實反映「worktree 已經不存在」）、稽核 `coding_cleanup`
    （`coding_run_id`/`approval_id`/`runner_server`），回傳 `{"ok": True}`。
    """
    run = db.get_coding_run(coding_run_id)
    if run is None:
        raise CodingRunNotFoundError(f"coding_run {coding_run_id} 不存在")
    if run.status not in CODING_RUN_TERMINAL_STATUSES:
        raise CodingRunNotCleanableError(
            f"coding_run {coding_run_id} 狀態是 {run.status!r}，還不是終態"
            f"（{sorted(CODING_RUN_TERMINAL_STATUSES)}），不能清理"
        )

    referencing = sorted(
        j.id
        for j in db.list_jobs(status="queued") + db.list_jobs(status="running")
        if j.source_coding_run_id == coding_run_id
    )
    if referencing:
        raise CodingRunNotCleanableError(
            f"coding_run {coding_run_id} 仍被 job {referencing} 以 "
            "source_coding_run_id 引用（queued/running），不能清理"
        )

    workspace_rel = resolve_codex_workspace_rel(config.codex_workspace_root)
    if run.engineering_task_id is not None:
        task = db.get_engineering_task(run.engineering_task_id)
        if (
            task is None
            or task.coding_run_id != run.id
            or task.approval_id != run.approval_id
            or task.runner_server != run.runner_server
        ):
            raise CodingRunNotCleanableError(
                "Engineering Task/Coding Run ownership contract 不一致，不能清理"
            )
        approved_workspace = task.execution_contract.get("workspace_rel")
        try:
            approved_workspace_rel = resolve_codex_workspace_rel(approved_workspace)
        except ValueError as exc:
            raise CodingRunNotCleanableError(
                "Engineering Task approved workspace contract 不合法，不能清理"
            ) from exc
        if workspace_rel != approved_workspace_rel:
            raise CodingRunNotCleanableError(
                "CODEX_WORKSPACE_ROOT 已與 Engineering Task approved contract 不同，"
                "不能重新導向清理位置"
            )
        runner_cfg = next(
            (server for server in config.servers if server.name == run.runner_server),
            None,
        )
        approved_runner = task.execution_contract.get("runner")
        current_runner = (
            {
                "name": runner_cfg.name,
                "host": runner_cfg.host,
                "user": runner_cfg.user,
                "port": runner_cfg.port,
            }
            if runner_cfg is not None
            else None
        )
        if (
            runner_cfg is None
            or not runner_cfg.enabled
            or not isinstance(approved_runner, dict)
            or current_runner != approved_runner
        ):
            raise CodingRunNotCleanableError(
                "Coding Runner identity/config 已與 Engineering Task approved contract 不同，"
                "不能清理"
            )
        workspace_rel = approved_workspace_rel
    approval = db.get_approval(run.approval_id)
    payload = approval.payload if approval is not None else {}
    runner = run.runner_server

    task_dir_rel = f"{workspace_rel}/tasks/{run.approval_id}"
    await ssh_run(runner, "rm -rf " + shlex.quote(task_dir_rel), 30)

    if payload.get("source_kind") == "instance" and payload.get("source"):
        await ssh_run(
            runner, f"git -C {shlex.quote(payload['source'])} worktree prune", 15
        )

    db.update_coding_run(coding_run_id, worktree_path=None)
    append_audit(
        "coding_cleanup",
        {
            "coding_run_id": coding_run_id,
            "approval_id": run.approval_id,
            "runner_server": runner,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return {"ok": True}


# --------------------------------------------------------------------------
# 核准 / 拒絕
# --------------------------------------------------------------------------


def _combine_note(context_note: Optional[str], detail_note: Optional[str]) -> Optional[str]:
    """把呼叫端傳入的「情境註記」（例如 K.2 的「網頁直接執行」／K.3 的「自動
    核准：規則 #N」）跟 `approve()` 自己針對某個 kind 決定要寫的細節註記
    （例如 stop 的「kill 失敗」／「任務已結束」）合併成一句話，兩者都有時用
    「；」分隔；只有一個或都沒有時直接回傳那一個／`None`（鐵律第 3 條：稽核
    要如實，不能因為加了情境註記反而蓋掉原本就該記錄的細節）。"""
    parts = [p for p in (context_note, detail_note) if p]
    return "；".join(parts) if parts else None


def _prepare_engineering_validation_approval_plan(
    db: Database,
    approval: Approval,
    *,
    server_configs: Optional[dict[str, Any]],
    app_state: Optional[Any],
) -> Optional[dict[str, Any]]:
    """Revalidate every immutable validation fact before creating any Job."""

    payload = approval.payload
    validation_request_id = (
        payload.get("validation_request_id") if isinstance(payload, dict) else None
    )
    if validation_request_id is None:
        return None
    validation = db.get_engineering_validation_request(validation_request_id)
    if validation is None or validation.approval_id != approval.id:
        raise InvalidEngineeringValidationRequestError(
            "worker validation approval linkage 不存在或已漂移"
        )
    snapshot = validation.request_snapshot
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("contract_version")
        != _ENGINEERING_VALIDATION_CONTRACT_VERSION
        or _validation_snapshot_digest(snapshot)
        != validation.request_snapshot_sha256
    ):
        raise InvalidEngineeringValidationRequestError(
            "worker validation immutable snapshot 驗證失敗"
        )
    task_snapshot = snapshot.get("task")
    job_snapshot = snapshot.get("job")
    target_snapshot = snapshot.get("target")
    instance_snapshot = snapshot.get("project_instance")
    bundle_snapshot = snapshot.get("bundle")
    parent_approval_snapshot = snapshot.get("parent_approval")
    execution_snapshot = snapshot.get("execution")
    if not all(
        isinstance(value, dict)
        for value in (
            task_snapshot,
            job_snapshot,
            target_snapshot,
            instance_snapshot,
            bundle_snapshot,
            parent_approval_snapshot,
            execution_snapshot,
        )
    ):
        raise InvalidEngineeringValidationRequestError(
            "worker validation immutable snapshot 格式不合法"
        )
    expected_payload = {
        "command": job_snapshot.get("command"),
        "type": job_snapshot.get("type"),
        "project": task_snapshot.get("project_name"),
        "require_tag": job_snapshot.get("require_tag"),
        "pin_server": target_snapshot.get("name"),
        "depends_on": [],
        "gpus_needed": job_snapshot.get("gpus_needed"),
        "priority": job_snapshot.get("priority"),
        "sync_plan": None,
        "setup_plan": None,
        "warning": None,
        "source": "engineering_validation",
        "source_coding_run_id": task_snapshot.get("coding_run_id"),
        "validation_request_id": validation.id,
        "validation_contract_version": _ENGINEERING_VALIDATION_CONTRACT_VERSION,
        "engineering_task_id": task_snapshot.get("id"),
        "attempt_number": task_snapshot.get("attempt_number"),
        "project_id": task_snapshot.get("project_id"),
        "project_version_id": task_snapshot.get("project_version_id"),
        "base_commit": task_snapshot.get("base_commit"),
        "result_commit": task_snapshot.get("result_commit"),
        "request_snapshot_sha256": validation.request_snapshot_sha256,
        "validation_execution_summary": {
            "contract_version": _ENGINEERING_VALIDATION_CONTRACT_VERSION,
            "parent_approval": parent_approval_snapshot,
            "target_identity": target_snapshot,
            "project_instance": instance_snapshot,
            "bundle": bundle_snapshot,
            "command_digests": execution_snapshot,
        },
    }
    if payload != expected_payload:
        raise InvalidEngineeringValidationRequestError(
            "worker validation approval payload 與 immutable snapshot 不一致"
        )
    task = db.get_engineering_task(validation.engineering_task_id)
    run = db.get_coding_run(validation.coding_run_id)
    version = db.get_project_version(validation.project_version_id)
    parent_approval = db.get_approval(task.approval_id) if task is not None else None
    if (
        task is None
        or run is None
        or version is None
        or task.id != task_snapshot.get("id")
        or task.project_id != validation.project_id
        or task.project_name != validation.project_name
        or task.project_version_id != validation.project_version_id
        or task.base_commit != validation.base_commit
        or task.coding_run_id != run.id
        or parent_approval is None
        or parent_approval.id != parent_approval_snapshot.get("id")
        or _validation_snapshot_digest(parent_approval.payload)
        != parent_approval_snapshot.get("payload_sha256")
        or not _engineering_task_parent_approval_matches(db, task)
        or run.engineering_task_id != task.id
        or run.attempt_number != validation.attempt_number
        or run.project_version_id != validation.project_version_id
        or run.base_binding != "project_version_pinned"
        or run.base_commit != validation.base_commit
        or run.status != "done"
        or run.result_commit != validation.result_commit
        or not run.bundle_path
        or version.project_id != validation.project_id
        or version.project_name != validation.project_name
        or version.git_commit != validation.base_commit
    ):
        raise InvalidEngineeringValidationRequestError(
            "Engineering Task/Run/ProjectVersion contract 已漂移"
        )
    if server_configs is None:
        raise InvalidEngineeringValidationRequestError("缺少 worker server configuration")
    target = server_configs.get(validation.target_server)
    if (
        target is None
        or not target.enabled
        or target.name == LOCAL_SERVER
        or _engineering_validation_target_snapshot(target) != target_snapshot
    ):
        raise InvalidEngineeringValidationRequestError(
            "worker target identity 已漂移或已停用"
        )
    instance = resolve_project_instance(db, validation.project_name, target.name)
    if _engineering_validation_instance_snapshot(instance) != instance_snapshot:
        raise InvalidEngineeringValidationRequestError(
            "worker target project instance identity 已漂移"
        )
    config = getattr(app_state, "config", None) if app_state is not None else None
    local_home_dir = getattr(config, "local_home_dir", None)
    if not isinstance(local_home_dir, str):
        raise InvalidEngineeringValidationRequestError("缺少 local result configuration")
    current_bundle = _verified_validation_bundle_descriptor(
        db,
        task_id=task.id,
        attempt_number=validation.attempt_number,
        coding_run_id=run.id,
        run_job_id=run.job_id,
        local_home_dir=local_home_dir,
    )
    if current_bundle != bundle_snapshot:
        raise InvalidEngineeringValidationRequestError(
            "changes.bundle descriptor 在核准前已漂移"
        )
    command = job_snapshot.get("command")
    if not isinstance(command, str):
        raise InvalidEngineeringValidationRequestError("validation command contract 不合法")
    try:
        reject_engineering_raw_credentials(command)
    except InvalidEngineeringTaskRequestError as exc:
        raise InvalidEngineeringValidationRequestError(str(exc)) from exc
    push_command = build_bundle_push_command(
        run.job_id, run.id, target, local_home_dir
    )
    downstream_command = build_bundle_checkout_preamble(
        run.id, run.result_commit, instance.path
    ) + command
    current_execution_digests = {
        "bundle_push_command_sha256": hashlib.sha256(
            push_command.encode("utf-8")
        ).hexdigest(),
        "downstream_command_sha256": hashlib.sha256(
            downstream_command.encode("utf-8")
        ).hexdigest(),
    }
    if current_execution_digests != execution_snapshot:
        raise InvalidEngineeringValidationRequestError(
            "worker validation generated command contract 已漂移"
        )
    for prepared in (push_command, downstream_command):
        dangerous, reason = is_dangerous(prepared)
        if dangerous:
            raise DangerousCommandError(reason)
    return {
        "validation": validation,
        "run": run,
        "push_command": push_command,
        "downstream_command": downstream_command,
        "job": job_snapshot,
    }


async def approve(
    db: Database,
    approval_id: int,
    ssh_run=None,
    audit_path: str = "audit.jsonl",
    server_configs: Optional[dict] = None,
    app_state: Optional[Any] = None,
    approved_by: str = "human",
    note: Optional[str] = None,
    local_run=None,
    request_context: Optional[RequestContext] = None,
) -> dict:
    """核准一筆 approval。

    - `local_run`（階段 15 Phase C，PLAN.md P.3）：kind=project_deploy 需要
      在 Server A 本地跑 `git bundle create/verify` 與 rsync 推送（介面形狀
      同 `app.hub.sync_project_to_hub()` 的 `local_run` 參數：`async
      local_run(command, timeout) -> CommandResult`）——沒有提供時該分支
      丟 `ValueError`（呼叫端轉 400），不會靜默跳過。其餘既有 kind 不需要
      這個參數，維持 `None` 也完全不受影響。
    - `approved_by`（階段 10，PLAN.md K 節）：如實記錄「是誰／什麼機制」
      核准的——`"human"`（預設，網頁二次點擊／既有 `POST /approve/{id}`）、
      `"web-direct"`（K.2：網頁一步生效，提案者＝批准者）、
      `"auto-rule-{N}"`（K.3：命中使用者預先寫的自動核准規則第 N 條）。寫進
      這裡對應的稽核紀錄，供事後追查「這筆核准到底是誰按的」。
    - `note`（階段 10）：呼叫端想附加的情境註記（K.2/K.3 用），只在
      kind=enqueue／kind=stop 生效——會跟這兩個 kind 原本就可能寫的細節註記
      （見下方 `_combine_note()`）合併，不會蓋掉既有行為（例如 stop 的
      「kill 失敗」／「任務已結束」仍然會被完整記錄）。其餘 kind 目前不需要
      呼叫端提供情境註記，維持 `None` 也完全不受影響。
    - `app_state`（階段 8 第二批）：server_add/server_update/server_disable/
      server_delete 這四個 kind 需要能改 in-memory 的
      `app_state.server_configs`/`server_states`（透過
      `app.server_config.reload_server_config_if_supported()`）與讀取
      `app_state.config.servers_yaml_path`。用 `Any` 型別（不 import
      `app.main.AppState`，避免循環 import）——只透過屬性存取，呼叫端
      （`app/main.py`）應該傳入 `app_state` 本身。其餘既有 kind 不需要這個
      參數，維持 `None` 也完全不受影響。**階段 12（PLAN.md M.3）**：
      kind=apply_patch 額外需要 `app_state.ssh_write_file`（同樣比照
      `app_state` 參數模式取得，不另外加一個獨立參數）——沒有
      `app_state`／沒有 `ssh_write_file` 屬性時丟 `ValueError`（呼叫端轉
      400），不會靜默跳過寫檔那一步。

    - kind=apply_patch（階段 12，PLAN.md M.3，全部 SSH，六步驟）：
      1) `git rev-parse --is-inside-work-tree` 確認是 git repo，不是的話
         approval 標 `rejected`，note 說明「未受 git 管理」，完全不碰任何
         檔案。2) 記下目前 branch（`git branch --show-current`）。3) diff
         寫到工作機 `agent_jobs/patch_{approval_id}.diff`（`ssh_write_file`
         ，`app_state` 取得，見上）。4) `git apply --check` 先驗，失敗（含
         跟未提交變更衝突）→ approval 標 `rejected`，stderr 進 note，**這一步
         之前完全沒有動過這個 git repo**。5) 通過 → `checkout -b
         ai-patch-{approval_id}`（重名加序號）→ `git apply --index` →
         `git -c user.name=... -c user.email=... commit`（只用 `-c` 帶入，
         不改全域 git 設定）。6) 稽核 `apply_patch` 記
         project/server/原branch/新branch/diff 全文；approval note 明確寫
         「已切到 branch ai-patch-N；回復方式：git checkout {原branch}」。
         **此 kind 永遠不會被 `maybe_auto_approve()` 自動核准**（該函式的
         kind 白名單只有 "enqueue"/"stop"）——人必須逐個看過 diff 才能按下
         核准。

    - kind=coding_task（階段 13，PLAN.md N.4/N.6，Codex Worker v2）：跟
      apply_patch 不一樣，這裡**不自己動手改碼**——只 SSH 到 Runner 寫
      `instruction.txt`，真正跑 `codex exec` 的是既有
      `jobqueue.enqueue_job()` 派出去的 `type="coding"` job（交給既有
      tmux/哨兵/log tail/卡死偵測基礎設施）。需要 `app_state.config`（取
      `codex_workspace_root`/`codex_network_access`）與
      `app_state.ssh_write_file`——沒有 `app_state` 或
      `config.codex_runner_server is None` → approval 標 `rejected`，note
      「未設定 CODEX_RUNNER_SERVER，Codex 功能停用」，不派工。
      **v1 遺留 payload 相容**（payload 沒有 `source_kind`，只有
      `server`——v1 的形狀）：`payload["server"] != runner` → approval 標
      `rejected`，note「架構已改為 Central Codex Runner，此舊請求的目標機
      非 Runner，請重建」；`== runner` → 用 v2 的 N.3 三段式邏輯重新解出
      `source_kind`/`source`（解不出 → `rejected`），接著照 v2 正常流程
      執行。v2 正常流程：1) `workspace_rel` = `config.codex_workspace_root`
      去掉 `~/` 前綴（若設定成絕對路徑則直接 `raise ValueError`——
      `ssh_write_file()`/SFTP 是 home-相對路徑慣例，見
      `app/jobqueue.py` 的 `AGENT_JOBS_DIR` 說明）；2)
      `db.insert_coding_run(status="queued", worktree_path=
      f"~/{workspace_rel}/tasks/{approval_id}", ...)`；3) SSH `mkdir -p`
      task 目錄＋`ssh_write_file()` 寫入
      `build_codex_instruction_file(instruction, config.codex_network_access)`
      到 `instruction.txt`；4) `build_coding_task_script(...)` 組
      `cmd.sh`，`enqueue_job(db, command=script, type="coding",
      project=project, pin_server=runner, audit_path=audit_path)`；5)
      `update_coding_run(run_id, job_id=job.id)`；approval 標 `approved`，
      note「已建立 coding 任務 #{job.id}（Runner {runner}，coding_run
      #{run_id}，branch ai-task-{approval_id}）」；6) 稽核 `coding_task`
      記 `approval_id`/`project`/`runner_server`/`job_id`/`coding_run_id`/
      `source_kind`（**刻意不含 instruction 全文**——已經存進
      `coding_runs.instruction`，稽核只需要能對得上是哪一筆，見
      `app.jobfinish` 對稱的取捨）。回傳 `{"approval": ..., "job": job,
      "coding_run_id": run_id}`。**此 kind 永遠不會被
      `maybe_auto_approve()` 自動核准**（理由同 apply_patch，見模組
      docstring）。

    - kind=enqueue：呼叫 `jobqueue.enqueue_job` 真正入列。若 payload 帶有
      `sync_plan`/`setup_plan`（見 `request_enqueue_approval()`），會先建立
      對應的 sync（pin `_local`）／setup（pin 目標機）任務，再把主任務的
      `depends_on` 串上這些任務 id——這是「整包計畫」實際落地成任務列的
      地方。`server_configs`（`server name -> app.config.ServerConfig`）
      只有建立 sync 任務時才需要（組 rsync 指令要知道目標機的
      host/user/key），呼叫端（API）應該傳入 `app_state.server_configs`。
      階段 13（PLAN.md N.7）：payload 帶有 `source_coding_run_id` 時，同樣
      在這裡（核准當下，不是請求當下）才真的落地——1) 用
      `app.results.build_bundle_push_command()` 組一個 pin `_local` 的
      `type="sync"` 推送任務（把本地已回收的 `results/{coding_run.job_id}/
      changes.bundle` 推到目標機 `coding_bundles/{coding_run_id}/`），串進
      `depends_on`（主任務要等 bundle 推送完成才能跑）；2) 用
      `build_bundle_checkout_preamble()` 組一段確定性前置段，接在使用者
      command **前面**（一起送進 `enqueue_job()`，一樣要過一次
      `is_dangerous()`）；3) `enqueue_job(..., source_coding_run_id=...)`
      記錄欄位，稽核 `approve` 額外附 `bundle_push_job_id`/
      `source_coding_run_id`/`result_commit`。這條路徑一樣走
      `_finalize_approval()`（K.2 網頁一步生效／K.3 自動核准規則）——
      這是普通的 enqueue 任務，不是 coding_task，不受「coding_task 永遠
      人工核准」那條規則限制（PLAN.md N.7 明文：這裡建立的是「引用某次
      coding run 產物」的一般任務，跟核准/派發那個 coding run 本身是兩件
      事）。
    - kind=stop：`ssh_run` 是 async callable（`sshpool` 提供的介面，測試可
      注入 FakeSSH）。核准前會重查一次任務目前狀態：
        - 已經不是 `running`（例如請求停止之後、核准之前自己跑完了）→
          approval 標 approved、note 說明「任務已結束，無需停止」，**不動
          job 任何欄位**，避免把一個已經 done/failed 的任務改寫成
          cancelled、汙染歷史紀錄。
        - 還是 `running` → SSH 到目標機 `tmux kill-session`，任務標
          `cancelled`、抓 log 尾存回 `log_tail`。**kill-session 失敗不會
          被靜默吞掉**：legacy Job 的稽核 `stop` 記錄保留既有
          `kill_ok`/`kill_error`；Engineering Task owner Job 則只記固定
          `failure_category`，避免 exception 夾帶 Server A 路徑或秘密資料。
          approval 的 `note` 也會寫明「kill 失敗，任務已標 cancelled 但
          工作機上行程可能仍在執行」——任務仍標 cancelled（使用者的核准
          意圖已經確定要停），但事實要留痕（鐵律第 3 條：稽核要如實）。
    """
    decision_audit_actor = audit_actor_from_request_context(request_context)
    db = _DecisionAttributingDatabase(
        db,
        approval_id,
        actor_id=_actor_id(request_context),
        mechanism=_decision_mechanism(approved_by),
    )

    # Keep every existing decision audit action/params/result/path untouched;
    # this local compatibility wrapper only adds the safe top-level envelope.
    def append_audit(action, params=None, result="ok", path="audit.jsonl"):
        return audit_module.append_audit(
            action,
            params,
            result=result,
            path=path,
            actor=decision_audit_actor,
        )

    approval = db.get_approval(approval_id)
    if approval is None:
        raise ApprovalNotFoundError(f"approval {approval_id} not found")
    if approval.status != "pending":
        raise ApprovalNotPendingError(f"approval {approval_id} 已經是 {approval.status}")

    if approval.kind in _IDENTITY_APPROVAL_KINDS:
        config = getattr(app_state, "config", None) if app_state is not None else None
        if not bool(getattr(config, "identity_admin_enabled", False)):
            raise IdentityAdministrationDisabledError(
                "identity administration is disabled"
            )

        def reject_identity_decision(reason: str) -> dict:
            db.update_approval(
                approval_id,
                status="rejected",
                decided_at=now_iso(),
                note=reason,
            )
            append_audit(
                approval.kind,
                {"approval_id": approval_id, "reason": reason},
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

    if approval.kind in _RUN_PROFILE_APPROVAL_KINDS:
        run_profile_config = (
            getattr(app_state, "config", None) if app_state is not None else None
        )
        _require_run_profile_v1_enabled(run_profile_config)

        def reject_run_profile_decision(reason: str) -> dict:
            db.update_approval(
                approval_id,
                status="rejected",
                decided_at=now_iso(),
                note=reason,
            )
            append_audit(
                approval.kind,
                {"approval_id": approval_id, "reason": reason},
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

    if approval.kind in _DISPATCH_POLICY_APPROVAL_KINDS:
        dispatch_policy_config = (
            getattr(app_state, "config", None) if app_state is not None else None
        )
        _require_dispatch_policy_v1_enabled(dispatch_policy_config)

        def reject_dispatch_policy_decision(reason: str) -> dict:
            db.update_approval(
                approval_id,
                status="rejected",
                decided_at=now_iso(),
                note=reason,
            )
            append_audit(
                approval.kind,
                {"approval_id": approval_id, "reason": reason},
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

    if approval.kind == "service_account_create":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "actor_id",
            "name",
            "description",
        }:
            return reject_identity_decision(
                "service account request payload is malformed"
            )

        actor_id = payload.get("actor_id")
        name = payload.get("name")
        description = payload.get("description")
        if not isinstance(actor_id, str):
            return reject_identity_decision(
                "service account request payload is malformed"
            )
        try:
            canonical_actor_id = str(uuid.UUID(actor_id))
            normalized_description = _normalize_optional_identity_text(
                description,
                field="description",
            )
        except (TypeError, ValueError):
            return reject_identity_decision(
                "service account request payload is malformed"
            )
        if (
            canonical_actor_id != actor_id
            or not isinstance(name, str)
            or not name.strip()
            or name.strip() != name
            or normalized_description != description
        ):
            return reject_identity_decision(
                "service account request payload is malformed"
            )

        actor = db.get_actor(actor_id)
        account = db.get_service_account(actor_id)
        account_by_name = _service_account_by_name(db, name)
        if account_by_name is not None and account_by_name.actor_id != actor_id:
            return reject_identity_decision(
                f"service account name {name} is no longer available"
            )

        if actor is not None and not (
            actor.actor_type is ActorType.SERVICE
            and actor.display_name == name
            and actor.email is None
            and actor.platform_admin is False
            and actor.disabled_at is None
        ):
            return reject_identity_decision(
                f"reserved actor {actor_id} conflicts with this request"
            )
        if account is not None and (
            account.name != name or account.description != description
        ):
            return reject_identity_decision(
                f"service account {actor_id} conflicts with this request"
            )

        if actor is None:
            db.insert_actor(
                actor_id=actor_id,
                actor_type=ActorType.SERVICE,
                display_name=name,
                platform_admin=False,
            )
        if account is None:
            try:
                account = db.insert_service_account(
                    actor_id=actor_id,
                    name=name,
                    description=description,
                    created_by_actor_id=_actor_id(request_context),
                )
            except sqlite3.IntegrityError:
                account = db.get_service_account(actor_id)
                if account is None or (
                    account.name != name or account.description != description
                ):
                    return reject_identity_decision(
                        f"service account name {name} is no longer available"
                    )

        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
        )
        append_audit(
            "service_account_create",
            {
                "approval_id": approval_id,
                "actor_id": actor_id,
                "name": name,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "service_account": account,
        }

    if approval.kind == "service_token_issue":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "service_account_actor_id",
            "label",
            "scopes",
            "expires_at",
        }:
            return reject_identity_decision(
                "service token request payload is malformed"
            )

        actor_id = payload.get("service_account_actor_id")
        try:
            _require_active_service_account(db, actor_id)
            label = _normalize_optional_identity_text(
                payload.get("label"),
                field="label",
            )
            scopes = _normalize_service_token_scopes(payload.get("scopes"))
            expires_at = _normalize_future_expiry(payload.get("expires_at"))
        except (IdentityTargetNotFoundError, ValueError):
            return reject_identity_decision(
                "service account or token request is no longer valid"
            )
        if (
            label != payload.get("label")
            or scopes != payload.get("scopes")
            or expires_at != payload.get("expires_at")
        ):
            return reject_identity_decision(
                "service token request payload is malformed"
            )

        issued = generate_service_token()
        service_token = db.insert_service_account_token(
            token_id=issued.id,
            service_account_actor_id=actor_id,
            secret_hash=issued.secret_hash,
            scopes=scopes,
            expires_at=expires_at,
            label=label,
            created_by_actor_id=_actor_id(request_context),
        )
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
        )
        append_audit(
            "service_token_issue",
            {
                "approval_id": approval_id,
                "token_id": service_token.id,
                "service_account_actor_id": actor_id,
                "label": label,
                "scopes": scopes,
                "expires_at": expires_at,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "service_token": service_token,
            "issued_service_token": issued,
        }

    if approval.kind == "service_token_revoke":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {"token_id"}:
            return reject_identity_decision(
                "service token revocation payload is malformed"
            )
        token_id = payload.get("token_id")
        service_token = (
            db.get_service_account_token(token_id)
            if isinstance(token_id, str) and token_id
            else None
        )
        if service_token is None:
            return reject_identity_decision(
                "service token no longer exists"
            )

        changed = False
        if service_token.revoked_at is None:
            changed = db.revoke_service_account_token(token_id)
            service_token = db.get_service_account_token(token_id)
        decision_note = None if changed else "service token was already revoked"
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
            note=decision_note,
        )
        append_audit(
            "service_token_revoke",
            {
                "approval_id": approval_id,
                "token_id": token_id,
                "changed": changed,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "service_token": service_token,
        }

    if approval.kind == "project_membership_upsert":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "actor_id",
            "role",
        }:
            return reject_identity_decision(
                "project membership request payload is malformed"
            )
        project_id = payload.get("project_id")
        actor_id = payload.get("actor_id")
        project = next(
            (item for item in db.list_projects() if item.id == project_id),
            None,
        )
        actor = db.get_actor(actor_id) if isinstance(actor_id, str) else None
        try:
            role = ProjectRole(payload.get("role"))
        except (TypeError, ValueError):
            role = None
        if (
            project is None
            or actor is None
            or actor.disabled_at is not None
            or role is None
        ):
            return reject_identity_decision(
                "project or membership actor is no longer valid"
            )

        membership = db.get_project_membership(project_id, actor_id)
        changed = membership is None or membership.role is not role
        if changed:
            membership = db.upsert_project_membership(
                project=project_id,
                actor_id=actor_id,
                role=role,
                created_by_actor_id=_actor_id(request_context),
            )
        decision_note = None if changed else "project membership already has this role"
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
            note=decision_note,
        )
        append_audit(
            "project_membership_upsert",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "actor_id": actor_id,
                "role": role.value,
                "changed": changed,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "membership": membership,
        }

    if approval.kind == "project_membership_remove":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "actor_id",
        }:
            return reject_identity_decision(
                "project membership removal payload is malformed"
            )
        project_id = payload.get("project_id")
        actor_id = payload.get("actor_id")
        project = next(
            (item for item in db.list_projects() if item.id == project_id),
            None,
        )
        actor = db.get_actor(actor_id) if isinstance(actor_id, str) else None
        if project is None or actor is None:
            return reject_identity_decision(
                "project or membership actor no longer exists"
            )

        membership_removed = db.delete_project_membership(project_id, actor_id)
        decision_note = None if membership_removed else "project membership was already absent"
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
            note=decision_note,
        )
        append_audit(
            "project_membership_remove",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "actor_id": actor_id,
                "changed": membership_removed,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "membership_removed": membership_removed,
        }

    if approval.kind == "run_profile_create":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "project_name",
            "name",
            "command",
            "setup_cmd",
            "require_tag",
        }:
            return reject_run_profile_decision(
                "run profile create payload is malformed"
            )
        project_id = payload.get("project_id")
        name = payload.get("name")
        project = next(
            (item for item in db.list_projects() if item.id == project_id), None
        )
        if project is None:
            return reject_run_profile_decision("project no longer exists")
        if db.get_run_profile_head(project_id, name) is not None:
            return reject_run_profile_decision(
                f"run profile {name!r} already exists for this project"
            )
        try:
            profile = db.insert_run_profile_revision(
                project_id=project_id,
                project_name=payload.get("project_name"),
                name=name,
                status="approved",
                command=payload.get("command"),
                setup_cmd=payload.get("setup_cmd"),
                require_tag=payload.get("require_tag"),
                supersedes_id=None,
                approval_id=approval_id,
                created_by_actor_id=_actor_id(request_context),
            )
        except sqlite3.IntegrityError:
            return reject_run_profile_decision(
                f"run profile {name!r} was concurrently created for this project"
            )
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "run_profile_create",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "name": name,
                "run_profile_id": profile.id,
                "revision": profile.revision,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "run_profile": profile}

    if approval.kind == "run_profile_update":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "project_name",
            "name",
            "based_on_revision",
            "command",
            "setup_cmd",
            "require_tag",
        }:
            return reject_run_profile_decision(
                "run profile update payload is malformed"
            )
        project_id = payload.get("project_id")
        name = payload.get("name")
        based_on_revision = payload.get("based_on_revision")
        project = next(
            (item for item in db.list_projects() if item.id == project_id), None
        )
        if project is None:
            return reject_run_profile_decision("project no longer exists")
        head = db.get_run_profile_head(project_id, name)
        if head is None:
            return reject_run_profile_decision(
                f"run profile {name!r} no longer exists"
            )
        if head.revision != based_on_revision or head.status != "approved":
            return reject_run_profile_decision(
                f"run profile {name!r} changed since this request was created"
            )
        try:
            profile = db.insert_run_profile_revision(
                project_id=project_id,
                project_name=payload.get("project_name"),
                name=name,
                status="approved",
                command=payload.get("command"),
                setup_cmd=payload.get("setup_cmd"),
                require_tag=payload.get("require_tag"),
                supersedes_id=head.id,
                approval_id=approval_id,
                created_by_actor_id=_actor_id(request_context),
            )
        except sqlite3.IntegrityError:
            return reject_run_profile_decision(
                f"run profile {name!r} was concurrently changed for this project"
            )
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "run_profile_update",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "name": name,
                "run_profile_id": profile.id,
                "revision": profile.revision,
                "supersedes_id": head.id,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "run_profile": profile}

    if approval.kind == "run_profile_archive":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "project_name",
            "name",
            "based_on_revision",
        }:
            return reject_run_profile_decision(
                "run profile archive payload is malformed"
            )
        project_id = payload.get("project_id")
        name = payload.get("name")
        based_on_revision = payload.get("based_on_revision")
        project = next(
            (item for item in db.list_projects() if item.id == project_id), None
        )
        if project is None:
            return reject_run_profile_decision("project no longer exists")
        head = db.get_run_profile_head(project_id, name)
        if head is None:
            return reject_run_profile_decision(
                f"run profile {name!r} no longer exists"
            )
        if head.revision != based_on_revision or head.status != "approved":
            return reject_run_profile_decision(
                f"run profile {name!r} changed since this request was created"
            )
        try:
            profile = db.insert_run_profile_revision(
                project_id=project_id,
                project_name=payload.get("project_name"),
                name=name,
                status="archived",
                command=head.command,
                setup_cmd=head.setup_cmd,
                require_tag=head.require_tag,
                supersedes_id=head.id,
                approval_id=approval_id,
                created_by_actor_id=_actor_id(request_context),
            )
        except sqlite3.IntegrityError:
            return reject_run_profile_decision(
                f"run profile {name!r} was concurrently changed for this project"
            )
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "run_profile_archive",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "name": name,
                "run_profile_id": profile.id,
                "revision": profile.revision,
                "supersedes_id": head.id,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "run_profile": profile}

    if approval.kind == "dispatch_policy_create":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "project_name",
            "name",
            "allowed_servers",
            "require_tag",
            "run_profile_id",
            "dataset_required",
            "max_concurrent_placements",
            "valid_until",
        }:
            return reject_dispatch_policy_decision(
                "dispatch policy create payload is malformed"
            )
        project_id = payload.get("project_id")
        name = payload.get("name")
        project = next(
            (item for item in db.list_projects() if item.id == project_id), None
        )
        if project is None:
            return reject_dispatch_policy_decision("project no longer exists")
        if db.get_dispatch_policy_head(project_id, name) is not None:
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} already exists for this project"
            )
        try:
            _validate_dispatch_policy_run_profile_reference(
                db, project_id, payload.get("run_profile_id")
            )
        except InvalidDispatchPolicyRequestError as exc:
            return reject_dispatch_policy_decision(str(exc))
        try:
            policy = db.insert_dispatch_policy_revision(
                project_id=project_id,
                project_name=payload.get("project_name"),
                name=name,
                status="approved",
                allowed_servers=payload.get("allowed_servers"),
                require_tag=payload.get("require_tag"),
                run_profile_id=payload.get("run_profile_id"),
                dataset_required=bool(payload.get("dataset_required")),
                max_concurrent_placements=payload.get("max_concurrent_placements"),
                valid_until=payload.get("valid_until"),
                approval_id=approval_id,
                created_by_actor_id=_actor_id(request_context),
            )
        except sqlite3.IntegrityError:
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} was concurrently created for this project"
            )
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "dispatch_policy_create",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "name": name,
                "dispatch_policy_id": policy.id,
                "revision": policy.revision,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "dispatch_policy": policy}

    if approval.kind == "dispatch_policy_update":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "project_name",
            "name",
            "based_on_revision",
            "allowed_servers",
            "require_tag",
            "run_profile_id",
            "dataset_required",
            "max_concurrent_placements",
            "valid_until",
        }:
            return reject_dispatch_policy_decision(
                "dispatch policy update payload is malformed"
            )
        project_id = payload.get("project_id")
        name = payload.get("name")
        based_on_revision = payload.get("based_on_revision")
        project = next(
            (item for item in db.list_projects() if item.id == project_id), None
        )
        if project is None:
            return reject_dispatch_policy_decision("project no longer exists")
        head = db.get_dispatch_policy_head(project_id, name)
        if head is None:
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} no longer exists"
            )
        if head.revision != based_on_revision or head.status != "approved":
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} changed since this request was created"
            )
        try:
            _validate_dispatch_policy_run_profile_reference(
                db, project_id, payload.get("run_profile_id")
            )
        except InvalidDispatchPolicyRequestError as exc:
            return reject_dispatch_policy_decision(str(exc))
        try:
            policy = db.insert_dispatch_policy_revision(
                project_id=project_id,
                project_name=payload.get("project_name"),
                name=name,
                status="approved",
                allowed_servers=payload.get("allowed_servers"),
                require_tag=payload.get("require_tag"),
                run_profile_id=payload.get("run_profile_id"),
                dataset_required=bool(payload.get("dataset_required")),
                max_concurrent_placements=payload.get("max_concurrent_placements"),
                valid_until=payload.get("valid_until"),
                approval_id=approval_id,
                created_by_actor_id=_actor_id(request_context),
            )
        except sqlite3.IntegrityError:
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} was concurrently changed for this project"
            )
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "dispatch_policy_update",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "name": name,
                "dispatch_policy_id": policy.id,
                "revision": policy.revision,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "dispatch_policy": policy}

    if approval.kind == "dispatch_policy_archive":
        payload = approval.payload
        if not isinstance(payload, dict) or set(payload) != {
            "project_id",
            "project_name",
            "name",
            "based_on_revision",
        }:
            return reject_dispatch_policy_decision(
                "dispatch policy archive payload is malformed"
            )
        project_id = payload.get("project_id")
        name = payload.get("name")
        based_on_revision = payload.get("based_on_revision")
        project = next(
            (item for item in db.list_projects() if item.id == project_id), None
        )
        if project is None:
            return reject_dispatch_policy_decision("project no longer exists")
        head = db.get_dispatch_policy_head(project_id, name)
        if head is None:
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} no longer exists"
            )
        if head.revision != based_on_revision or head.status != "approved":
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} changed since this request was created"
            )
        try:
            policy = db.insert_dispatch_policy_revision(
                project_id=project_id,
                project_name=payload.get("project_name"),
                name=name,
                status="archived",
                allowed_servers=head.allowed_servers,
                require_tag=head.require_tag,
                run_profile_id=head.run_profile_id,
                dataset_required=head.dataset_required,
                max_concurrent_placements=head.max_concurrent_placements,
                valid_until=head.valid_until,
                approval_id=approval_id,
                created_by_actor_id=_actor_id(request_context),
            )
        except sqlite3.IntegrityError:
            return reject_dispatch_policy_decision(
                f"dispatch policy {name!r} was concurrently changed for this project"
            )
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "dispatch_policy_archive",
            {
                "approval_id": approval_id,
                "project_id": project_id,
                "name": name,
                "dispatch_policy_id": policy.id,
                "revision": policy.revision,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "dispatch_policy": policy}

    if approval.kind == "auto_placement":
        auto_placement_config = (
            getattr(app_state, "config", None) if app_state is not None else None
        )
        _require_dispatch_policy_v1_enabled(auto_placement_config)

        def reject_auto_placement_decision(reason: str) -> dict:
            db.update_approval(
                approval_id,
                status="rejected",
                decided_at=now_iso(),
                note=reason,
            )
            append_audit(
                approval.kind,
                {"approval_id": approval_id, "reason": reason},
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        payload = approval.payload
        validation = _validate_auto_placement_payload(db, payload, server_configs)
        if not validation.ok:
            return reject_auto_placement_decision(validation.reason)
        policy_id = payload["policy_id"]
        server_name = payload["server"]
        policy_row = validation.policy_row
        head = validation.head

        project = next(
            (item for item in db.list_projects() if item.id == policy_row.project_id),
            None,
        )
        if project is None:
            return reject_auto_placement_decision("project no longer exists")

        try:
            plan = build_dispatch_plan(db, project, server_name)
        except ValueError as exc:
            return reject_auto_placement_decision(str(exc))

        depends_on: list[int] = []
        plan_job_ids: dict[str, int] = {}

        #: 這裡刻意重用跟手動 pin_server enqueue 分支相同的
        #: build_setup_script()/build_sync_script()/enqueue_job() 呼叫序列
        #: （見上方 kind == "enqueue" 分支），確保自動放置產生的 job 鏈跟
        #: 手動指定機器派工位元組級一致——不重新發明第二套指令組裝邏輯。
        if plan.setup_plan:
            setup_command = build_setup_script(
                plan.setup_plan["project"],
                plan.setup_plan["repo_or_path"],
                plan.setup_plan.get("setup_cmd"),
            )
            setup_job = enqueue_job(
                db,
                command=setup_command,
                type="setup",
                project=plan.setup_plan["project"],
                pin_server=plan.setup_plan["target_server"],
                audit_path=audit_path,
                audit_actor=SYSTEM_AUDIT_ACTOR,
                auto_placement_approval_id=approval_id,
            )
            depends_on.append(setup_job.id)
            plan_job_ids["setup_job_id"] = setup_job.id

        if plan.sync_plan:
            target_cfg = (server_configs or {}).get(plan.sync_plan["target_server"])
            if target_cfg is None:
                return reject_auto_placement_decision(
                    f"未知的目標機器設定: {plan.sync_plan['target_server']}"
                )
            dest_dir = dataset_remote_dir(
                plan.sync_plan["dataset_name"], plan.sync_plan["dataset_version"]
            )
            sync_command = build_sync_script(
                plan.sync_plan["source_path"],
                target_cfg.user,
                target_cfg.host,
                dest_dir,
                target_cfg.key_path,
                port=target_cfg.port,
            )
            sync_job = enqueue_job(
                db,
                command=sync_command,
                type="sync",
                project=policy_row.project_name,
                pin_server=LOCAL_SERVER,
                audit_path=audit_path,
                audit_actor=SYSTEM_AUDIT_ACTOR,
                auto_placement_approval_id=approval_id,
            )
            db.update_job(
                sync_job.id,
                target_server=plan.sync_plan["target_server"],
                dataset_name=plan.sync_plan["dataset_name"],
                dataset_version=plan.sync_plan["dataset_version"],
            )
            depends_on.append(sync_job.id)
            plan_job_ids["sync_job_id"] = sync_job.id

        job = enqueue_job(
            db,
            command=payload["command"],
            type="adhoc",
            project=policy_row.project_name,
            require_tag=payload.get("require_tag"),
            pin_server=server_name,
            depends_on=depends_on,
            priority=payload.get("priority", "normal"),
            audit_path=audit_path,
            audit_actor=SYSTEM_AUDIT_ACTOR,
            auto_placement_approval_id=approval_id,
        )
        db.update_approval(approval_id, status="approved", decided_at=now_iso(), note=note)
        append_audit(
            "auto_placement",
            {
                "approval_id": approval_id,
                "policy_id": policy_id,
                "server": server_name,
                "job_id": job.id,
                **plan_job_ids,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "job": job, **plan_job_ids}

    if approval.kind == "enqueue":
        payload = approval.payload
        validation_plan = _prepare_engineering_validation_approval_plan(
            db,
            approval,
            server_configs=server_configs,
            app_state=app_state,
        )
        if validation_plan is not None:
            validation = validation_plan["validation"]
            job_spec = validation_plan["job"]
            push_job_id, downstream_job_id = (
                db.finalize_engineering_validation_request(
                    validation_request_id=validation.id,
                    approval_id=approval_id,
                    request_snapshot_sha256=validation.request_snapshot_sha256,
                    bundle_push_command=validation_plan["push_command"],
                    downstream_command=validation_plan["downstream_command"],
                    job_type=job_spec["type"],
                    project=validation.project_name,
                    target_server=validation.target_server,
                    require_tag=job_spec.get("require_tag"),
                    gpus_needed=job_spec.get("gpus_needed"),
                    priority=job_spec["priority"],
                    source_coding_run_id=validation.coding_run_id,
                    approval_note=note,
                    decision_actor_id=_actor_id(request_context),
                    decision_mechanism=_decision_mechanism(approved_by),
                )
            )
            push_job = db.get_job(push_job_id)
            job = db.get_job(downstream_job_id)
            for queued_job, label in (
                (push_job, "worker_validation_bundle_push"),
                (job, "worker_validation"),
            ):
                append_audit(
                    "enqueue",
                    {
                        "job_id": queued_job.id,
                        "project": queued_job.project,
                        "priority": queued_job.priority,
                        "pin_server": queued_job.pin_server,
                        "require_tag": queued_job.require_tag,
                        "depends_on": queued_job.depends_on,
                        "command_display": label,
                        "command_digest": hashlib.sha256(
                            queued_job.command.encode("utf-8")
                        ).hexdigest(),
                        "command_digest_algorithm": "sha256",
                        "validation_request_id": validation.id,
                    },
                    path=audit_path,
                )
            append_audit(
                "approve",
                {
                    "approval_id": approval_id,
                    "kind": "enqueue",
                    "job_id": downstream_job_id,
                    "approved_by": approved_by,
                    "bundle_push_job_id": push_job_id,
                    "source_coding_run_id": validation.coding_run_id,
                    "result_commit": validation.result_commit,
                    "validation_request_id": validation.id,
                    "engineering_task_id": validation.engineering_task_id,
                },
                path=audit_path,
            )
            return {
                "approval": db.get_approval(approval_id),
                "job": job,
                "validation_request_id": validation.id,
                "bundle_push_job_id": push_job_id,
            }
        depends_on = list(payload.get("depends_on") or [])
        plan_job_ids: dict[str, int] = {}

        setup_plan = payload.get("setup_plan")
        if setup_plan:
            setup_command = build_setup_script(
                setup_plan["project"], setup_plan["repo_or_path"], setup_plan.get("setup_cmd")
            )
            setup_job = enqueue_job(
                db,
                command=setup_command,
                type="setup",
                project=setup_plan["project"],
                pin_server=setup_plan["target_server"],
                audit_path=audit_path,
                audit_actor=decision_audit_actor,
            )
            depends_on.append(setup_job.id)
            plan_job_ids["setup_job_id"] = setup_job.id

        sync_plan = payload.get("sync_plan")
        if sync_plan:
            target_cfg = (server_configs or {}).get(sync_plan["target_server"])
            if target_cfg is None:
                raise ValueError(f"未知的目標機器設定: {sync_plan['target_server']}")
            dest_dir = dataset_remote_dir(sync_plan["dataset_name"], sync_plan["dataset_version"])
            sync_command = build_sync_script(
                sync_plan["source_path"],
                target_cfg.user,
                target_cfg.host,
                dest_dir,
                target_cfg.key_path,
                port=target_cfg.port,
            )
            sync_job = enqueue_job(
                db,
                command=sync_command,
                type="sync",
                project=payload.get("project"),
                pin_server=LOCAL_SERVER,
                audit_path=audit_path,
                audit_actor=decision_audit_actor,
            )
            db.update_job(
                sync_job.id,
                target_server=sync_plan["target_server"],
                dataset_name=sync_plan["dataset_name"],
                dataset_version=sync_plan["dataset_version"],
            )
            depends_on.append(sync_job.id)
            plan_job_ids["sync_job_id"] = sync_job.id

        #: 階段 13（PLAN.md N.7）：source_coding_run_id 的下游 bundle 流——
        #: 建推送任務、把確定性前置段接在使用者 command 前面。驗證已經在
        #: `request_enqueue_approval()` 建立請求當下做過一次（run 存在／
        #: done／有 result_commit／本地有 bundle／pin_server 有值／目標機
        #: 有 instance）；這裡防禦性地再擋一次「理論上不會發生」的邊界
        #: 情況（run 或目標機設定在請求建立之後、核准之前消失），一律
        #: `ValueError`（呼叫端轉 400，比照 sync_plan 目標機消失的既有
        #: 慣例）。
        command = payload["command"]
        bundle_audit_extra: dict[str, Any] = {}
        source_coding_run_id = payload.get("source_coding_run_id")
        if source_coding_run_id is not None:
            if app_state is None:
                raise ValueError(
                    "enqueue 的 source_coding_run_id 分支需要 app_state"
                    "（取 local_home_dir 組推送指令用），呼叫端未提供"
                )
            coding_run = db.get_coding_run(source_coding_run_id)
            if coding_run is None:
                raise ValueError(f"coding_run {source_coding_run_id} 不存在")
            target_server = payload.get("pin_server")
            target_cfg = (server_configs or {}).get(target_server)
            if target_cfg is None:
                raise ValueError(f"未知的目標機器設定: {target_server}")
            instance = resolve_project_instance(db, coding_run.project, target_server)

            push_command = build_bundle_push_command(
                coding_run.job_id,
                coding_run.id,
                target_cfg,
                app_state.config.local_home_dir,
            )
            push_job = enqueue_job(
                db,
                command=push_command,
                type="sync",
                project=payload.get("project"),
                pin_server=LOCAL_SERVER,
                audit_path=audit_path,
                audit_actor=decision_audit_actor,
            )
            depends_on.append(push_job.id)
            plan_job_ids["bundle_push_job_id"] = push_job.id

            preamble = build_bundle_checkout_preamble(
                coding_run.id, coding_run.result_commit, instance.path
            )
            command = preamble + command
            bundle_audit_extra["source_coding_run_id"] = source_coding_run_id
            bundle_audit_extra["result_commit"] = coding_run.result_commit

        job = enqueue_job(
            db,
            command=command,
            type=payload.get("type", "adhoc"),
            project=payload.get("project"),
            require_tag=payload.get("require_tag"),
            pin_server=payload.get("pin_server"),
            depends_on=depends_on,
            gpus_needed=payload.get("gpus_needed"),
            priority=payload.get("priority", "normal"),
            audit_path=audit_path,
            source_coding_run_id=source_coding_run_id,
            audit_actor=decision_audit_actor,
        )
        db.update_approval(approval_id, status="approved", decided_at=now_iso(), note=note)
        append_audit(
            "approve",
            {
                "approval_id": approval_id,
                "kind": "enqueue",
                "job_id": job.id,
                "approved_by": approved_by,
                **plan_job_ids,
                **bundle_audit_extra,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "job": job}

    if approval.kind == "stop":
        job_id = approval.payload["job_id"]
        job = db.get_job(job_id)
        if job is None:
            db.update_approval(
                approval_id,
                status="approved",
                decided_at=now_iso(),
                note=_combine_note(note, "job 已不存在"),
            )
            append_audit(
                "stop",
                {"approval_id": approval_id, "job_id": job_id, "approved_by": approved_by},
                result="failed",
                path=audit_path,
            )
            raise JobNotFoundError(f"job {job_id} not found")

        # 修正 1：核准前重查一次任務目前狀態。請求停止之後、核准之前，
        # 任務有可能自己跑完了（reconcile 已經標成 done/failed）——這種
        # 情況下不能無條件把它改寫成 cancelled，否則歷史紀錄會出錯。
        # approval 本身仍標 approved（使用者的核准意圖確實生效了，只是
        # 已經沒有「可停止」的對象），但不動 job 任何欄位，正常回傳。
        if job.status != "running":
            skip_note = f"任務已結束（狀態 {job.status}），無需停止"
            db.update_approval(
                approval_id,
                status="approved",
                decided_at=now_iso(),
                note=_combine_note(note, skip_note),
            )
            append_audit(
                "stop",
                {
                    "approval_id": approval_id,
                    "job_id": job_id,
                    "job_status": job.status,
                    "note": skip_note,
                    "approved_by": approved_by,
                },
                result="skipped",
                path=audit_path,
            )
            if job.engineering_task_id is not None:
                db.refresh_engineering_task_status_from_jobs(
                    job.engineering_task_id
                )
            if job.engineering_validation_request_id is not None:
                db.refresh_engineering_validation_request_status(
                    job.engineering_validation_request_id
                )
            return {"approval": db.get_approval(approval_id), "job": job}

        log_tail = job.log_tail
        kill_ok = True
        kill_error: Optional[str] = None
        kill_failure_category: Optional[str] = None
        execution_contract_matches = True
        if job.engineering_validation_request_id is not None:
            config = getattr(app_state, "config", None) if app_state is not None else None
            local_home_dir = getattr(config, "local_home_dir", None)
            validation_failure = engineering_validation_job_contract_failure(
                db,
                job,
                server_configs or {},
                local_home_dir=local_home_dir,
            )
            execution_contract_matches = validation_failure is None
            if not execution_contract_matches:
                kill_ok = False
                kill_failure_category = validation_failure
                record_engineering_validation_contract_refusal(
                    db, job, validation_failure
                )
            elif ssh_run is None or not job.server:
                kill_ok = False
                kill_failure_category = "executor_unavailable"
        elif job.engineering_task_id is not None:
            if not engineering_job_command_contract_matches(db, job):
                execution_contract_matches = False
                mismatch_category = "execution_contract_mismatch"
                record_engineering_job_execution_contract_mismatch(db, job)
            elif job.engineering_task_role == "staging":
                execution_contract_matches = engineering_staging_job_contract_matches(
                    db, job
                )
                mismatch_category = "execution_contract_mismatch"
            else:
                current_runner = (
                    (server_configs or {}).get(job.server) if job.server else None
                )
                execution_contract_matches = (
                    engineering_coding_job_runner_contract_matches(
                        db, job, current_runner
                    )
                )
                mismatch_category = "runner_contract_mismatch"
            if not execution_contract_matches:
                kill_ok = False
                kill_failure_category = mismatch_category
            elif ssh_run is None or not job.server:
                kill_ok = False
                kill_failure_category = "executor_unavailable"
        if execution_contract_matches and ssh_run is not None and job.server:
            try:
                await ssh_run(job.server, f"tmux kill-session -t job_{job_id}", 15)
            except Exception as exc:  # noqa: BLE001
                # 修正 2：kill-session 失敗不能靜默吞掉。SSH 當下連不上的
                # 話，工作機上的訓練其實可能還在跑，但系統已經把它當成
                # 結束了——這個事實落差要留痕在稽核與 approval note 裡，
                # 而不是讓 "stop" 稽核記錄看起來像是成功的。任務仍然標
                # cancelled（使用者按下核准的意圖已經確定要停），只是明確
                # 記下「這只是我們這邊的認定，實際上可能沒殺成功」。
                kill_ok = False
                if (
                    job.engineering_task_id is None
                    and job.engineering_validation_request_id is None
                ):
                    kill_error = str(exc)
                else:
                    kill_failure_category = engineering_job_failure_category(exc)
            try:
                tail_res = await ssh_run(job.server, build_log_tail_command(job_id), 15)
                log_tail = tail_res.stdout
            except Exception:  # noqa: BLE001 - 抓不到最新 log 不影響 kill_ok 的判定
                pass

        db.update_job(
            job_id,
            status=CANCELLED,
            finished_at=now_iso(),
            log_tail=safe_persisted_engineering_log_tail(job, log_tail),
        )
        if job.engineering_task_id is not None:
            db.refresh_engineering_task_status_from_jobs(job.engineering_task_id)
        if job.engineering_validation_request_id is not None:
            db.refresh_engineering_validation_request_status(
                job.engineering_validation_request_id
            )
        if kill_ok:
            approval_note = None
        elif (
            job.engineering_task_id is not None
            or job.engineering_validation_request_id is not None
        ):
            approval_note = (
                f"kill 失敗（類別：{kill_failure_category}），任務已標 cancelled "
                "但工作機上行程可能仍在執行"
            )
        else:
            approval_note = (
                f"kill 失敗：{kill_error}，任務已標 cancelled 但工作機上行程可能仍在執行"
            )
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
            note=_combine_note(note, approval_note),
        )
        stop_audit_params = {
            "approval_id": approval_id,
            "job_id": job_id,
            "kill_ok": kill_ok,
            "note": approval_note,
            "approved_by": approved_by,
        }
        if (
            job.engineering_task_id is None
            and job.engineering_validation_request_id is None
        ):
            stop_audit_params["kill_error"] = kill_error
        else:
            stop_audit_params.update(
                {
                    "failure_category": kill_failure_category,
                    "engineering_task_id": job.engineering_task_id,
                    "engineering_task_role": job.engineering_task_role,
                    "engineering_attempt_number": job.engineering_attempt_number,
                    "validation_request_id": (
                        job.engineering_validation_request_id
                    ),
                }
            )
        append_audit(
            "stop",
            stop_audit_params,
            result="ok" if kill_ok else "partial",
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "job": db.get_job(job_id)}

    if approval.kind == "server_bootstrap":
        # Goal 3 Phase B（DG-B，docs/DECISIONS.md 2026-07-19）。比照
        # inventory_scan：核准當下才真的對外連線；SSH 連不上時例外原樣往上
        # 丟，approval 維持 pending 可重試（unreachable ≠ failed）。只有
        # 「連上且跑完」才落地報告並把 approval 標 approved——報告本身
        # 可能是未通過（passed=False），那是如實記錄，不是核准失敗。
        bootstrap_config = (
            getattr(app_state, "config", None) if app_state is not None else None
        )
        _require_server_bootstrap_v1_enabled(bootstrap_config)

        def reject_bootstrap_decision(reason: str) -> dict:
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reason
            )
            append_audit(
                "server_bootstrap",
                {"approval_id": approval_id, "reason": reason},
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        payload = approval.payload
        required_keys = {
            "host", "username", "port", "key", "components", "gpu",
            "script_version", "script_sha256",
        }
        if not isinstance(payload, dict) or not required_keys.issubset(payload):
            return reject_bootstrap_decision("server_bootstrap payload is malformed")
        if payload.get("script_sha256") != bootstrap_script_sha256():
            return reject_bootstrap_decision(
                "bootstrap 腳本已改版（SHA-256 與請求當下不符）；請重新建立請求"
            )
        try:
            components = validate_bootstrap_components(payload.get("components"))
        except ValueError as exc:
            return reject_bootstrap_decision(str(exc))
        if not isinstance(payload.get("key"), str) or not _BOOTSTRAP_KEY_RE.match(
            payload["key"]
        ):
            return reject_bootstrap_decision("key 路徑不符合 ~/.ssh/ 限制")

        ssh_pool = getattr(app_state, "ssh_pool", None) if app_state is not None else None
        if ssh_pool is None:
            raise ValueError("server_bootstrap 需要 app_state.ssh_pool，呼叫端未提供")

        server_cfg = ServerConfig(
            name=f"bootstrap:{payload['host']}:{payload['port']}",
            host=str(payload["host"]),
            user=str(payload["username"]),
            key=str(payload["key"]),
            gpu=bool(payload.get("gpu", False)),
            port=int(payload["port"]),
        )
        report = await run_server_bootstrap(
            server_cfg,
            ssh_run_direct=ssh_pool.run,
            write_file_direct=ssh_pool.write_file,
            components=components,
            gpu=bool(payload.get("gpu", False)),
        )
        report_row = db.insert_server_bootstrap_report(
            host=server_cfg.host,
            username=server_cfg.user,
            port=server_cfg.port,
            components=components,
            script_version=str(report.get("script_version") or ""),
            script_sha256=str(report.get("script_sha256") or ""),
            passed=bool(report.get("passed")),
            report=report,
            approval_id=approval_id,
        )
        if report_row.passed:
            note = f"bootstrap 通過（報告 #{report_row.id}）；可繼續 server_add 流程"
        else:
            note = (
                f"bootstrap 已執行但未通過（報告 #{report_row.id}）："
                f"{'；'.join(report.get('errors') or []) or '缺項未記錄'}"
            )
        db.update_approval(
            approval_id, status="approved", decided_at=now_iso(), note=note
        )
        append_audit(
            "server_bootstrap",
            {
                "approval_id": approval_id,
                "host": server_cfg.host,
                "username": server_cfg.user,
                "port": server_cfg.port,
                "components": components,
                "report_id": report_row.id,
                "passed": report_row.passed,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "report": report}

    if approval.kind == "inventory_scan":
        payload = approval.payload
        server = payload["server"]
        project_roots = list(payload.get("project_roots") or [])

        # 雙重防線第二關：核准後、真正發生 SSH 前再檢查一次禁止路徑。建立
        # 請求當下已經檢查過（`request_inventory_scan_approval()`），但
        # approval 可能等了一段時間才被核准，這裡是真正要對外發指令的地方，
        # 不能只信任當初建立請求時的檢查結果。
        for root in project_roots:
            if is_forbidden_root(root):
                note = f"禁止掃描的路徑：{root}"
                db.update_approval(
                    approval_id, status="rejected", decided_at=now_iso(), note=note
                )
                append_audit(
                    "inventory_scan",
                    {"approval_id": approval_id, "server": server, "reason": note},
                    result="rejected",
                    path=audit_path,
                )
                return {"approval": db.get_approval(approval_id)}

        if ssh_run is None:
            raise ValueError("inventory_scan 需要 ssh_run，呼叫端未提供")

        candidates = prune_nested_candidates(await scan_server(ssh_run, server, project_roots))
        for c in candidates:
            db.upsert_project_candidate(
                server=c.server,
                path=c.path,
                name_guess=c.name_guess,
                kind=c.kind,
                git_remote=c.git_remote,
                git_branch=c.git_branch,
                git_commit=c.git_commit,
                markers=c.markers,
                readme_excerpt=c.readme_excerpt,
                command_guess=c.command_guess,
                embedded_data_paths=c.embedded_data_paths,
                embedded_data_summary=c.embedded_data_summary,
                estimated_data_bytes=c.estimated_data_bytes,
                excluded_paths=c.excluded_paths,
                confidence=c.confidence,
            )

        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "inventory_scan",
            {
                "approval_id": approval_id,
                "server": server,
                "project_roots": project_roots,
                "candidates_found": len(candidates),
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "candidates_found": len(candidates)}

    if approval.kind == "import_project":
        payload = approval.payload
        candidate_id = payload["candidate_id"]
        candidate = db.get_project_candidate(candidate_id)
        if candidate is None:
            raise CandidateNotFoundError(f"candidate {candidate_id} 不存在")

        #: 切片 3:link_to_project 有值 → 連結既有 Project,不新建。
        #: INV-APPROVAL-3(核准當下重新驗證):等待期間目標可能已被刪除或
        #: 改名,重新用當下的 name/id 查一次,不信任建立請求時存的快照。
        link_to = payload.get("link_to_project")
        if link_to:
            target = db.get_project(link_to) or (
                db.get_project(payload["link_to_project_id"])
                if payload.get("link_to_project_id")
                else None
            )
            if target is None:
                raise ValueError(f"連結目標專案 {link_to} 已不存在，無法連結")
            name = target.name
        else:
            name = payload.get("name") or candidate.name_guess
            if not name:
                raise ValueError("candidate 沒有可用的名稱，無法匯入，請在匯入請求提供 name")

            dataset_mode = payload.get("dataset_mode") or "none"
            if dataset_mode not in VALID_DATASET_MODES:
                raise ValueError(f"不合法的 dataset_mode: {dataset_mode}")

            try:
                db.insert_project(
                    name=name,
                    repo_or_path=candidate.git_remote or candidate.path,
                    dataset_name=payload.get("dataset_name"),
                    dataset_version=payload.get("dataset_version"),
                    default_command=payload.get("default_command"),
                    require_tag=payload.get("require_tag"),
                    setup_cmd=payload.get("setup_cmd"),
                    summary=payload.get("summary"),
                    dataset_mode=dataset_mode,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"專案 {name} 已存在，無法重複匯入：{exc}") from exc

        # dataset_mode="embedded" 時刻意不碰 datasets/dataset_cache 表：
        # 那兩張表是給「Server A 統一管理來源、可以主動 rsync 同步到任意
        # 工作機」的資料集用的。embedded 資料只是掃描時發現「就長在這個
        # 專案目錄底下」，只存在 candidate 掃到的那台機器那個路徑——如果
        # 這裡順手登記進 dataset_cache，會讓使用者誤以為系統之後可以幫忙
        # 把這份資料同步到別台機器，但實際上完全沒有這回事（PLAN.md I 節：
        # 「只確認存在，不自動同步」）。要讓 embedded 資料真的可以被同步，
        # 使用者必須另外走 POST /datasets 走正規註冊流程。
        db.insert_project_instance(
            project_name=name,
            server=candidate.server,
            path=candidate.path,
            git_remote=candidate.git_remote,
            git_branch=candidate.git_branch,
            git_commit=candidate.git_commit,
            dirty=False,
            embedded_data_paths=candidate.embedded_data_paths,
        )
        db.update_project_candidate_status(candidate_id, "imported")
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "import_project",
            {"approval_id": approval_id, "candidate_id": candidate_id, "project": name},
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "project": db.get_project(name)}

    if approval.kind == "ignore_project_candidate":
        payload = approval.payload
        candidate_id = payload["candidate_id"]
        candidate = db.get_project_candidate(candidate_id)
        if candidate is None:
            raise CandidateNotFoundError(f"candidate {candidate_id} 不存在")

        db.update_project_candidate_status(candidate_id, "ignored")
        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "ignore_project_candidate",
            {"approval_id": approval_id, "candidate_id": candidate_id},
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id)}

    if approval.kind == "ignore_nested_candidates":
        # 階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）：批次卡，
        # 一次核准生效。**重查當下狀態**——payload 的 candidate_ids 是建卡
        # 當下算出來的，approval 可能擱置了一段時間，其間使用者可能已經
        # 手動處理過（單筆 import/ignore）其中幾筆；已經不是 pending 的
        # 直接跳過（不當作錯誤，這是預期會發生的競態，批次卡的意義就是
        # 「盡量清掉還沒處理的」，不是要求 payload 當下算出的集合原封不動
        # 生效）。
        payload = approval.payload
        candidate_ids = list(payload.get("candidate_ids") or [])
        ignored_ids: list[str] = []
        skipped_ids: list[str] = []
        for candidate_id in candidate_ids:
            candidate = db.get_project_candidate(candidate_id)
            if candidate is None or candidate.status != "pending":
                skipped_ids.append(candidate_id)
                continue
            db.update_project_candidate_status(candidate_id, "ignored")
            ignored_ids.append(candidate_id)

        note = f"已忽略 {len(ignored_ids)} 筆巢狀候選（跳過 {len(skipped_ids)} 筆）"
        db.update_approval(approval_id, status="approved", decided_at=now_iso(), note=note)
        append_audit(
            "candidates_ignore_nested",
            {
                "approval_id": approval_id,
                "ignored_count": len(ignored_ids),
                "ignored_ids": ignored_ids,
                "skipped_ids": skipped_ids,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "ignored_count": len(ignored_ids),
            "ignored_ids": ignored_ids,
            "skipped_ids": skipped_ids,
        }

    if approval.kind == "apply_patch":
        # PLAN.md M.3：這是本 kind 真正動手的地方，全部 SSH，六步驟：
        #   1. 是不是 git repo（不是 -> rejected，不留任何改動）
        #   2. 記下目前 branch（進稽核與 note，「回復方式」要靠它）
        #   3. diff 寫到工作機 agent_jobs/patch_{id}.diff（ssh_write_file）
        #   4. `git apply --check` 先驗（失敗 -> rejected + stderr 進 note，
        #      不留任何改動——這一步之前完全沒有碰過這個 git repo 本身）
        #   5. 通過 -> checkout -b ai-patch-{id}（重名加序號）-> apply
        #      --index -> commit（-c user.name/user.email，不改全域設定）
        #   6. 稽核記 project/server/原branch/新branch/diff 全文
        payload = approval.payload
        project = payload["project"]
        server = payload["server"]
        diff = payload["diff"]
        description = payload.get("description") or ""

        if ssh_run is None:
            raise ValueError("apply_patch 需要 ssh_run，呼叫端未提供")
        if app_state is None or getattr(app_state, "ssh_write_file", None) is None:
            raise ValueError("apply_patch 需要 app_state.ssh_write_file，呼叫端未提供")
        ssh_write_file = app_state.ssh_write_file

        instance = resolve_project_instance(db, project, server)
        remote_path = shlex.quote(instance.path)

        worktree_res = await ssh_run(
            server, f"git -C {remote_path} rev-parse --is-inside-work-tree", 10
        )
        is_work_tree = (worktree_res.stdout or "").strip() == "true"
        if not is_work_tree:
            reject_note = "此專案未受 git 管理，無法安全套用"
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "apply_patch",
                {
                    "approval_id": approval_id,
                    "project": project,
                    "server": server,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        branch_res = await ssh_run(server, f"git -C {remote_path} branch --show-current", 10)
        original_branch = (branch_res.stdout or "").strip() or None

        diff_rel_path = f"agent_jobs/patch_{approval_id}.diff"
        await ssh_run(server, "mkdir -p agent_jobs", 10)
        await ssh_write_file(server, diff_rel_path, diff)
        # `git -C <path>` 改變的是 git 自己的工作目錄，不是這整條 SSH 指令
        # 的 shell cwd——遠端 shell 的預設 cwd 仍然是 home（見
        # app/jobqueue.py 的 AGENT_JOBS_DIR 說明），所以這裡用 `$HOME/...`
        # 讓 diff 檔案路徑不受 `-C` 影響、由外層 shell 展開成絕對路徑，
        # 跟 ssh_write_file() 實際落地的相對路徑（同一個 home 目錄）一致。
        diff_ref = f"$HOME/{diff_rel_path}"

        check_res = await ssh_run(server, f"git -C {remote_path} apply --check {diff_ref}", 20)
        check_exit_status = getattr(check_res, "exit_status", None)
        if check_exit_status is not None:
            check_failed = check_exit_status != 0
        else:
            # 呼叫端沒提供 exit_status（理論上真實 CommandResult 一定有）
            # -> 退而求其次看 stderr 是否有內容。
            check_failed = bool((check_res.stderr or "").strip())

        if check_failed:
            stderr_text = (getattr(check_res, "stderr", "") or "").strip() or "（無 stderr 輸出）"
            reject_note = f"git apply --check 失敗，未套用任何改動：{stderr_text}"
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "apply_patch",
                {
                    "approval_id": approval_id,
                    "project": project,
                    "server": server,
                    "original_branch": original_branch,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        # 通過 --check：checkout 一個新 branch（重名加序號，approval_id 本身
        # 是遞增唯一值，正常情況下不會撞名，但機器上可能有人手動建過同名
        # branch，防禦性處理）。
        base_branch_name = f"ai-patch-{approval_id}"
        new_branch = base_branch_name
        suffix = 2
        while suffix < 50:
            exists_res = await ssh_run(
                server,
                f"git -C {remote_path} rev-parse --verify --quiet refs/heads/{new_branch}",
                10,
            )
            if not (exists_res.stdout or "").strip():
                break
            new_branch = f"{base_branch_name}-{suffix}"
            suffix += 1

        await ssh_run(server, f"git -C {remote_path} checkout -b {new_branch}", 15)
        await ssh_run(server, f"git -C {remote_path} apply --index {diff_ref}", 20)
        commit_msg = f"AI patch #{approval_id}: {description}" if description else f"AI patch #{approval_id}"
        commit_cmd = (
            f"git -C {remote_path} -c user.name='dispatch-center' "
            f"-c user.email='dispatch@local' commit -m {shlex.quote(commit_msg)}"
        )
        await ssh_run(server, commit_cmd, 20)

        revert_hint = original_branch or "(原分支未知，請自行確認)"
        approve_note = f"已切到 branch {new_branch}；回復方式：git checkout {revert_hint}"
        db.update_approval(
            approval_id, status="approved", decided_at=now_iso(), note=approve_note
        )
        append_audit(
            "apply_patch",
            {
                "approval_id": approval_id,
                "project": project,
                "server": server,
                "original_branch": original_branch,
                "new_branch": new_branch,
                "diff": diff,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id)}

    if approval.kind == "git_init":
        # PLAN.md P.2.1：這是本 kind 真正動手的地方，全部 SSH，八步驟：
        #   1. 再驗非 git repo（雙重防線，已是 -> rejected，不動任何檔案）
        #   2. 寫 .gitignore（printf 單指令，絕對路徑，不走 home 相對的
        #      ssh_write_file）
        #   3. git init
        #   4. git add -A
        #   5. size guard：git count-objects -v 的 size+size-pack 總和
        #      > 512000KB（500MB）-> rm -rf .git（剛 init 的、系統路徑組裝，
        #      安全回復）+ rejected，note 附實際 MB 數，.gitignore 保留
        #   6. commit（-c user.name/user.email，訊息含 approval id）
        #   7. 記 HEAD，更新 project_instances 的 git_branch/git_commit
        #   8. 稽核 git_init {approval_id, project, server, head, staged_kb}
        payload = approval.payload
        project = payload["project"]
        server = payload["server"]
        gitignore_text = payload["gitignore"]

        if ssh_run is None:
            raise ValueError("git_init 需要 ssh_run，呼叫端未提供")

        instance = resolve_project_instance(db, project, server)
        remote_path = shlex.quote(instance.path)

        check_res = await ssh_run(server, f"test -d {remote_path}/.git && echo GIT_OK", 10)
        if "GIT_OK" in (check_res.stdout or ""):
            reject_note = "此專案已經是 git repo（雙重防線攔截），未做任何改動"
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "git_init",
                {
                    "approval_id": approval_id,
                    "project": project,
                    "server": server,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        gitignore_cmd = f"printf '%s' {shlex.quote(gitignore_text)} > {remote_path}/.gitignore"
        await ssh_run(server, gitignore_cmd, 15)
        await ssh_run(server, f"git -C {remote_path} init", 15)
        await ssh_run(server, f"git -C {remote_path} add -A", 120)

        count_res = await ssh_run(server, f"git -C {remote_path} count-objects -v", 15)
        size_kb, size_pack_kb = _parse_git_count_objects(count_res.stdout or "")
        staged_kb = size_kb + size_pack_kb
        if staged_kb > GIT_INIT_SIZE_GUARD_KB:
            await ssh_run(server, f"rm -rf {remote_path}/.git", 15)
            staged_mb = staged_kb / 1024
            reject_note = f"staged 內容過大（{staged_mb:.1f} MB），請補 extra_ignores 後重試"
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "git_init",
                {
                    "approval_id": approval_id,
                    "project": project,
                    "server": server,
                    "staged_kb": staged_kb,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        commit_msg = f"Initial commit（dispatch-center git-init，approval #{approval_id}）"
        commit_cmd = (
            f"git -C {remote_path} -c user.name='dispatch-center' "
            f"-c user.email='dispatch@local' commit -m {shlex.quote(commit_msg)}"
        )
        await ssh_run(server, commit_cmd, 60)

        head_res = await ssh_run(server, f"git -C {remote_path} rev-parse HEAD", 10)
        head = (head_res.stdout or "").strip() or None
        branch_res = await ssh_run(server, f"git -C {remote_path} branch --show-current", 10)
        branch = (branch_res.stdout or "").strip() or None

        db.update_instance_git_state(instance.id, git_branch=branch, git_commit=head)

        approve_note = f"已初始化 git repo，HEAD {head[:8] if head else '(未知)'}"
        db.update_approval(
            approval_id, status="approved", decided_at=now_iso(), note=approve_note
        )
        append_audit(
            "git_init",
            {
                "approval_id": approval_id,
                "project": project,
                "server": server,
                "head": head,
                "staged_kb": staged_kb,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id)}

    if approval.kind == "project_deploy":
        # PLAN.md P.3：這是本 kind 真正動手的地方，inline 執行（比照
        # git_init，不像 coding_task 那樣派背景 job），五步：
        #   1. 本地 git bundle create --git-dir={hub} {ref} + bundle verify
        #   2. rsync 推 bundle 到目標機 deploy_bundles/{approval_id}.bundle
        #      （app.hub.build_deploy_push_command()，先 SSH mkdir -p
        #      deploy_bundles）
        #   3. 目標機：mkdir -p 目的地父目錄 -> git clone -b {ref}
        #      $HOME/deploy_bundles/{id}.bundle {dest} -> git -C {dest}
        #      remote remove origin
        #   4. 記 HEAD -> db.insert_project_instance() 建立全新 instance
        #   5. 稽核 project_deploy
        # 任何一步失敗 -> rejected，note 含失敗步驟 + stderr 摘要 + 清理
        # 指引；已建立的部分留給人工檢查，不自動 rm（PLAN.md P.3 原文）。
        payload = approval.payload
        project = payload["project"]
        target_server = payload["target_server"]
        dest_path = payload["dest_path"]
        ref = payload["ref"]

        def _stderr_tail(result) -> str:
            text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
            return text[-500:] or f"exit_status={getattr(result, 'exit_status', '?')}"

        def _reject_deploy(step: str, detail: str, *, cleanup: bool = True) -> dict:
            cleanup_hint = (
                f"；檢查後可手動移除 {target_server}:{dest_path} 與本地/目標機的 "
                f"deploy_bundles/hub_bundles 暫存 bundle（approval #{approval_id}），"
                "系統不會自動清理"
                if cleanup
                else "；尚未動任何檔案，無需清理"
            )
            reject_note = f"{step} 失敗：{detail}{cleanup_hint}"
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "project_deploy",
                {
                    "approval_id": approval_id,
                    "project": project,
                    "target_server": target_server,
                    "dest_path": dest_path,
                    "ref": ref,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        # 雙重防線：approve 時重查一次 target 是否已有 instance（建卡後、
        # 核准前這段期間可能有人透過其他管道先手動部署／核准了）。
        existing = [
            inst for inst in db.list_project_instances(project) if inst.server == target_server
        ]
        if existing:
            return _reject_deploy(
                "前置檢查",
                f"機器 {target_server} 已存在專案 {project} 的 instance（雙重防線攔截）",
                cleanup=False,
            )

        if local_run is None:
            raise ValueError("project_deploy 需要 local_run，呼叫端未提供")
        if ssh_run is None:
            raise ValueError("project_deploy 需要 ssh_run，呼叫端未提供")
        if app_state is None or getattr(app_state, "config", None) is None:
            raise ValueError("project_deploy 需要 app_state.config（取 local_home_dir），呼叫端未提供")
        deploy_config = app_state.config

        target_cfg = (server_configs or {}).get(target_server)
        if target_cfg is None:
            raise ValueError(f"未知的目標機器設定: {target_server}")

        repo_path = hub_repo_path(project, deploy_config.local_home_dir)
        bundle_local_path = local_deploy_bundle_path(approval_id, deploy_config.local_home_dir)

        # 1. 本地 bundle create + verify。
        create_cmd = (
            f"git --git-dir={shlex.quote(repo_path)} bundle create "
            f"{shlex.quote(bundle_local_path)} {shlex.quote(ref)}"
        )
        create_result = await local_run(create_cmd, 120)
        if getattr(create_result, "exit_status", 1) != 0:
            return _reject_deploy("步驟 1（建立 bundle）", _stderr_tail(create_result), cleanup=False)

        verify_cmd = (
            f"git --git-dir={shlex.quote(repo_path)} bundle verify {shlex.quote(bundle_local_path)}"
        )
        verify_result = await local_run(verify_cmd, 30)
        if getattr(verify_result, "exit_status", 1) != 0:
            return _reject_deploy("步驟 1（驗證 bundle）", _stderr_tail(verify_result))

        # 2. rsync 推到目標機。
        push_cmd = build_deploy_push_command(
            approval_id, project, target_cfg, deploy_config.local_home_dir
        )
        push_result = await local_run(push_cmd, deploy_config.result_pull_timeout_sec)
        if getattr(push_result, "exit_status", 1) != 0:
            return _reject_deploy(f"步驟 2（推送 bundle 到 {target_server}）", _stderr_tail(push_result))

        # 3. 目標機：mkdir 父目錄 -> clone -> remote remove origin。
        dest_parent = dest_path.rsplit("/", 1)[0] or "/"
        mkdir_result = await ssh_run(target_server, f"mkdir -p {shlex.quote(dest_parent)}", 15)
        if getattr(mkdir_result, "exit_status", 1) != 0:
            return _reject_deploy("步驟 3（建立目的地父目錄）", _stderr_tail(mkdir_result))

        clone_cmd = (
            f"git clone -b {shlex.quote(ref)} "
            f"$HOME/deploy_bundles/{approval_id}.bundle {shlex.quote(dest_path)}"
        )
        clone_result = await ssh_run(target_server, clone_cmd, 300)
        if getattr(clone_result, "exit_status", 1) != 0:
            return _reject_deploy("步驟 3（git clone）", _stderr_tail(clone_result))

        remote_remove_result = await ssh_run(
            target_server, f"git -C {shlex.quote(dest_path)} remote remove origin", 15
        )
        if getattr(remote_remove_result, "exit_status", 1) != 0:
            return _reject_deploy("步驟 3（移除 origin remote）", _stderr_tail(remote_remove_result))

        # 4. 記 HEAD -> 建立全新 project_instance。
        head_result = await ssh_run(
            target_server, f"git -C {shlex.quote(dest_path)} rev-parse HEAD", 15
        )
        if getattr(head_result, "exit_status", 1) != 0:
            return _reject_deploy("步驟 4（記錄 HEAD）", _stderr_tail(head_result))
        head = (head_result.stdout or "").strip() or None

        db.insert_project_instance(
            project_name=project,
            server=target_server,
            path=dest_path,
            git_branch=ref,
            git_commit=head,
        )

        #: 切片 4(canonical version service):部署的 commit 一定源自 hub
        #: （來源固定，見 request_project_deploy_approval() docstring）,
        #: 用同一個 get_or_create 確保跟 hub_sync 產生的版本記錄共用同一筆
        #: ——不會因為「先 sync 再 deploy」而重複建立同一個 commit 的
        #: ProjectVersion。這裡沒有來源 instance（方向是 hub → 新
        #: instance）,`source_instance_id` 一律 None。
        if head:
            db.get_or_create_project_version(project, head, git_ref=ref)

        # 5. 稽核。
        head_short = head[:8] if head else "(未知)"
        approve_note = f"已部署到 {target_server}:{dest_path}（{ref}@{head_short}）"
        db.update_approval(
            approval_id, status="approved", decided_at=now_iso(), note=approve_note
        )
        append_audit(
            "project_deploy",
            {
                "approval_id": approval_id,
                "project": project,
                "target_server": target_server,
                "dest_path": dest_path,
                "ref": ref,
                "head": head,
            },
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id)}

    if approval.kind == "coding_task":
        # PLAN.md N.4/N.6（Codex Worker v2）：不自己動手改碼——只 SSH 寫
        # instruction.txt，真正跑 codex exec 交給既有 jobqueue.enqueue_job()
        # 派出去的 type="coding" job。
        payload = approval.payload if isinstance(approval.payload, dict) else {}
        project = payload.get("project")
        instruction = payload.get("instruction")
        base_branch = payload.get("base_branch")
        validation_target = payload.get("validation_target")
        engineering_task_id = payload.get("engineering_task_id")
        # The approval→parent relationship is canonical.  Never decide whether
        # this is an immutable task solely from a mutable/corrupted payload:
        # removing or replacing ``engineering_task_id`` must fail closed rather
        # than falling through to the legacy branch and resolving a mutable HEAD.
        engineering_task = db.get_engineering_task_by_approval_id(approval_id)

        def _reject_coding_task(reject_note: str, **extra: Any) -> dict:
            if engineering_task is not None:
                db.reject_engineering_task_approval(
                    task_id=engineering_task.id,
                    approval_id=approval_id,
                    note=reject_note,
                    decision_actor_id=_actor_id(request_context),
                    decision_mechanism=_decision_mechanism(approved_by),
                )
            else:
                db.update_approval(
                    approval_id,
                    status="rejected",
                    decided_at=now_iso(),
                    note=reject_note,
                )
            append_audit(
                "coding_task",
                {
                    "approval_id": approval_id,
                    "project": project if isinstance(project, str) else None,
                    "reason": reject_note,
                    **extra,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        if app_state is None:
            raise ValueError("coding_task 需要 app_state（config／ssh_write_file），呼叫端未提供")
        config = app_state.config
        # Goal 3 Phase D-1：核准當下從 pool 決定性選擇 Runner（單 Runner
        # 配置回傳 primary，行為不變）。
        runner = select_codex_runner(db, config)
        if runner is None:
            return _reject_coding_task("未設定 CODEX_RUNNER_SERVER，Codex 功能停用")
        if (
            not isinstance(project, str)
            or not project
            or not isinstance(instruction, str)
            or not instruction
        ):
            return _reject_coding_task("coding_task approval payload 缺少必要欄位")

        exact_base_commit: Optional[str] = None
        network_access = config.codex_network_access
        if engineering_task is not None or engineering_task_id is not None:
            if not config.engineering_task_backend_v1:
                # Rollback switch pauses immutable tasks without consuming the
                # pending approval; re-enabling can safely resume the same payload.
                raise ValueError("AI Engineering Task backend 未啟用")
            if engineering_task is None:
                return _reject_coding_task("engineering_task parent 不存在")
            if engineering_task.approval_id != approval_id:
                return _reject_coding_task("engineering_task/approval 關聯不一致")
            if engineering_task.status not in {"pending_approval", "planning"}:
                return _reject_coding_task(
                    f"engineering_task 狀態不可核准：{engineering_task.status}"
                )
            task_contract_version = payload.get("contract_version")
            if task_contract_version not in {
                "engineering-task-v1",
                ENGINEERING_TASK_CONTRACT_V2,
            }:
                return _reject_coding_task("未知的 Engineering Task contract version")
            immutable_payload_matches = (
                payload.get("engineering_task_id") == engineering_task.id
                and payload.get("project") == engineering_task.project_name
                and payload.get("project_id") == engineering_task.project_id
                and payload.get("project_version_id")
                == engineering_task.project_version_id
                and str(payload.get("base_commit") or "").lower()
                == engineering_task.base_commit.lower()
                and payload.get("agent_provider_id")
                == engineering_task.agent_provider_id
                and payload.get("provider_capabilities")
                == engineering_task.provider_capabilities
                and payload.get("execution_contract")
                == engineering_task.execution_contract
                and payload.get("contract_version")
                == engineering_task.contract_version
                and payload.get("structured_request")
                == engineering_task.structured_request
                and payload.get("instruction") == engineering_task.instruction
                and payload.get("detected_metadata")
                == engineering_task.detected_metadata
                and payload.get("validation_target")
                == engineering_task.validation_target
                and payload.get("runner_server") == engineering_task.runner_server
                and payload.get("network_access") is False
                and payload.get("dependency_installation") is False
                and payload.get("source_kind")
                == engineering_task.execution_contract.get("source_kind")
                and payload.get("source")
                == engineering_task.execution_contract.get("source")
            )
            if not immutable_payload_matches:
                return _reject_coding_task(
                    "Engineering Task parent 與 approved payload 不一致；拒絕替換執行內容"
                )
            path_policy: Optional[dict[str, Any]] = None
            path_policy_sha256: Optional[str] = None
            path_verifier: Optional[dict[str, str]] = None
            execution_contract = engineering_task.execution_contract
            if task_contract_version == "engineering-task-v1":
                # Preserve already-pending v1 approvals exactly; never infer a
                # technical policy from their historically advisory strings.
                if any(
                    key in execution_contract
                    for key in (
                        "path_policy",
                        "path_policy_sha256",
                        "path_verifier",
                    )
                ):
                    return _reject_coding_task(
                        "legacy Engineering Task 含未知 path policy contract"
                    )
            else:
                path_policy_value = execution_contract.get("path_policy")
                path_policy_sha_value = execution_contract.get(
                    "path_policy_sha256"
                )
                path_verifier_value = execution_contract.get("path_verifier")
                try:
                    validated_policy = validate_engineering_path_policy(
                        path_policy_value,
                        path_policy_sha_value,
                    )
                    validated_verifier = (
                        validate_engineering_path_verifier_contract(
                            path_verifier_value
                        )
                    )
                    expected_policy, expected_policy_sha = (
                        build_engineering_path_policy(
                            engineering_task.structured_request.get(
                                "allowed_paths"
                            ),
                            engineering_task.structured_request.get(
                                "prohibited_paths"
                            ),
                        )
                    )
                except (EngineeringPathPolicyError, AttributeError, TypeError):
                    return _reject_coding_task(
                        "approved final Git path policy 無法驗證；請建立新請求"
                    )
                if (
                    validated_policy != expected_policy
                    or path_policy_sha_value != expected_policy_sha
                    or validated_policy.get("verifier") != validated_verifier
                    or path_verifier_value != validated_verifier
                ):
                    return _reject_coding_task(
                        "final Git path policy 與 structured request 不一致；請建立新請求"
                    )
                path_policy = validated_policy
                path_policy_sha256 = path_policy_sha_value
                path_verifier = validated_verifier
            if payload.get("runner_server") != runner or engineering_task.runner_server != runner:
                return _reject_coding_task(
                    "CODEX_RUNNER_SERVER 已與核准 payload 不同；請建立新請求"
                )
            runner_cfg = (server_configs or {}).get(runner)
            approved_runner = engineering_task.execution_contract.get("runner")
            current_runner = (
                {
                    "name": runner_cfg.name,
                    "host": runner_cfg.host,
                    "user": runner_cfg.user,
                    "port": runner_cfg.port,
                }
                if runner_cfg is not None
                else None
            )
            if (
                runner_cfg is None
                or not getattr(runner_cfg, "enabled", True)
                or current_runner != approved_runner
            ):
                return _reject_coding_task(
                    "Coding Runner identity/config 已與 approved contract 不同；請建立新請求"
                )
            try:
                current_workspace_rel = resolve_codex_workspace_rel(
                    config.codex_workspace_root
                )
            except ValueError:
                return _reject_coding_task(
                    "Coding workspace 設定無效；請修正平台設定後建立新請求"
                )
            if (
                engineering_task.execution_contract.get("workspace_rel")
                != current_workspace_rel
                or engineering_task.execution_contract.get("source_kind")
                != "hub_bundle"
                or engineering_task.execution_contract.get("source")
                != remote_engineering_bundle_path(engineering_task.id)
                or engineering_task.execution_contract.get("network_access") is not False
                or engineering_task.execution_contract.get("dependency_installation")
                is not False
            ):
                return _reject_coding_task(
                    "Engineering Task execution contract 已 stale 或不安全；請建立新請求"
                )
            approved_provider = get_coding_agent_provider(
                engineering_task.agent_provider_id
            )
            if (
                approved_provider is None
                or not approved_provider.descriptor.capabilities.start_turn
                or payload.get("agent_provider_id")
                != approved_provider.descriptor.provider_id
            ):
                return _reject_coding_task("coding agent provider 與核准 payload 不一致")
            approved_capabilities = (
                approved_provider.descriptor.capability_snapshot()
            )
            if (
                engineering_task.provider_capabilities != approved_capabilities
                or payload.get("provider_capabilities") != approved_capabilities
            ):
                return _reject_coding_task(
                    "coding agent capability snapshot 與核准 contract 不一致"
                )
            project_row = db.get_project(project)
            version = db.get_project_version(payload.get("project_version_id"))
            if (
                project_row is None
                or not project_row.id
                or payload.get("project_id") != project_row.id
                or engineering_task.project_id != project_row.id
            ):
                return _reject_coding_task("Project identity 已改變或不存在；請建立新請求")
            if (
                version is None
                or version.id != engineering_task.project_version_id
                or version.project_id != project_row.id
                or version.project_name != project
                or version.git_commit.lower() != engineering_task.base_commit.lower()
                or payload.get("base_commit", "").lower()
                != engineering_task.base_commit.lower()
            ):
                return _reject_coding_task("ProjectVersion contract 已 stale；請建立新請求")
            if local_run is None:
                raise ValueError("immutable coding_task 需要 local_run")
            try:
                resolved_commit, _metadata = await inspect_hub_project_version(
                    project_name=project,
                    git_commit=engineering_task.base_commit,
                    local_home_dir=config.local_home_dir,
                    local_run=local_run,
                )
            except InvalidEngineeringTaskRequestError:
                return _reject_coding_task(
                    "approved ProjectVersion 無法在 Hub 中安全驗證；請建立新請求"
                )
            if resolved_commit.lower() != engineering_task.base_commit.lower():
                return _reject_coding_task("Hub exact commit 與 approved base 不一致")
            exact_base_commit = engineering_task.base_commit
            source_kind = "hub_bundle"
            source = remote_engineering_bundle_path(engineering_task.id)
            if payload.get("source_kind") != source_kind or payload.get("source") != source:
                return _reject_coding_task("Hub staging path 與 approved payload 不一致")
            base_branch = None
            network_access = False
        elif "source_kind" in payload:
            source_kind = payload["source_kind"]
            source = payload["source"]
        else:
            # v1 遺留 payload（無 source_kind，有 server）：只有目標機恰好
            # 就是 Runner 才照 v2 邏輯重新解出來源，否則直接拒絕重建。
            legacy_server = payload.get("server")
            if legacy_server != runner:
                return _reject_coding_task(
                    "架構已改為 Central Codex Runner，此舊請求的目標機非 Runner，請重建",
                    legacy_server=legacy_server,
                )
            try:
                legacy_instance = resolve_project_instance(db, project, runner)
                source_kind = "instance"
                source = legacy_instance.path
            except ProjectInstanceResolutionError:
                project_row = db.get_project(project)
                repo_or_path = (project_row.repo_or_path if project_row else "") or ""
                if repo_or_path.startswith(_GIT_REMOTE_PREFIXES):
                    source_kind = "mirror"
                    source = repo_or_path
                else:
                    return _reject_coding_task(
                        "此專案沒有 Codex Runner instance，也沒有可用的 git_remote。"
                        "請先匯入專案到 Codex Runner 或登記 git_remote。"
                    )

        if engineering_task is not None:
            if getattr(app_state, "ssh_write_file", None) is None:
                raise ValueError(
                    "immutable coding_task 需要 app_state.ssh_write_file，呼叫端未提供"
                )
            # Stage instruction data on Server A only.  The local staging Job
            # transfers it together with the exact Hub bundle after approval;
            # this branch performs no Runner SSH/SFTP side effect.
            workspace_rel = engineering_task.execution_contract["workspace_rel"]
            instruction_file = build_codex_instruction_file(instruction, False)
            path_policy_file: Optional[str] = None
            path_verifier_file: Optional[str] = None
            if task_contract_version == ENGINEERING_TASK_CONTRACT_V2:
                if (
                    path_policy is None
                    or path_policy_sha256 is None
                    or path_verifier is None
                ):
                    return _reject_coding_task(
                        "approved final Git path policy contract 不完整"
                    )
                path_policy_file = render_engineering_path_policy_file(
                    path_policy
                )
                path_verifier_file = engineering_path_verifier_source()
            script = build_coding_task_script(
                approval_id,
                workspace_rel,
                project,
                source_kind,
                source,
                None,
                False,
                exact_base_commit=exact_base_commit,
                agent_provider_id=engineering_task.agent_provider_id,
                path_policy_sha256=path_policy_sha256,
                path_verifier_sha256=(
                    path_verifier["source_sha256"]
                    if path_verifier is not None
                    else None
                ),
            )
            staging_script = " && ".join(
                (
                    build_engineering_bundle_create_command(
                        engineering_task.id,
                        project,
                        engineering_task.base_commit,
                        config.local_home_dir,
                    ),
                    build_engineering_bundle_verify_command(
                        engineering_task.id,
                        project,
                        engineering_task.base_commit,
                        config.local_home_dir,
                    ),
                    build_engineering_staging_push_command(
                        engineering_task.id,
                        approval_id,
                        workspace_rel,
                        runner_cfg,
                        config.local_home_dir,
                        include_path_policy=(
                            task_contract_version
                            == ENGINEERING_TASK_CONTRACT_V2
                        ),
                    ),
                )
            )
            for command_role, command in (
                ("staging", staging_script),
                ("coding", script),
            ):
                dangerous, reason = is_dangerous(command)
                if dangerous:
                    return _reject_coding_task(
                        f"deterministic {command_role} command 被安全政策拒絕：{reason}"
                    )

            await app_state.ssh_write_file(
                LOCAL_SERVER,
                local_engineering_instruction_relpath(engineering_task.id),
                instruction_file,
            )
            if path_policy_file is not None and path_verifier_file is not None:
                await app_state.ssh_write_file(
                    LOCAL_SERVER,
                    local_engineering_path_policy_relpath(engineering_task.id),
                    path_policy_file,
                )
                await app_state.ssh_write_file(
                    LOCAL_SERVER,
                    local_engineering_path_verifier_relpath(engineering_task.id),
                    path_verifier_file,
                )

            try:
                run_id, staging_job_id, coding_job_id = (
                    db.finalize_engineering_task_approval_plan(
                        task_id=engineering_task.id,
                        approval_id=approval_id,
                        project=project,
                        runner_server=runner,
                        instruction=instruction,
                        base_commit=engineering_task.base_commit,
                        project_version_id=engineering_task.project_version_id,
                        validation_target=validation_target,
                        worktree_path=f"~/{workspace_rel}/tasks/{approval_id}",
                        staging_command=staging_script,
                        coding_command=script,
                        approval_note=None,
                        decision_actor_id=_actor_id(request_context),
                        decision_mechanism=_decision_mechanism(approved_by),
                    )
                )
            except (sqlite3.IntegrityError, ValueError) as exc:
                # The DB transaction rolls every run/job/decision write back.
                # Keep the approval pending so a corrected deterministic plan
                # can be retried without adopting partial execution rows.
                raise ValueError(
                    f"Engineering Task execution plan 未建立：{exc}"
                ) from exc

            staging_job = db.get_job(staging_job_id)
            job = db.get_job(coding_job_id)
            for planned_job in (staging_job, job):
                append_audit(
                    "enqueue",
                    {
                        "job_id": planned_job.id,
                        **engineering_job_command_audit_fields(
                            planned_job.command,
                            planned_job.engineering_task_role,
                        ),
                        "project": project,
                        "priority": planned_job.priority,
                        "pin_server": planned_job.pin_server,
                        "require_tag": planned_job.require_tag,
                        "depends_on": planned_job.depends_on,
                        "engineering_task_id": engineering_task.id,
                        "engineering_task_role": planned_job.engineering_task_role,
                        "engineering_attempt_number": 1,
                    },
                    path=audit_path,
                )
            append_audit(
                "coding_task",
                {
                    "approval_id": approval_id,
                    "project": project,
                    "runner_server": runner,
                    "job_id": job.id,
                    "coding_run_id": run_id,
                    "source_kind": source_kind,
                    "engineering_task_id": engineering_task.id,
                    "project_version_id": engineering_task.project_version_id,
                    "base_commit": engineering_task.base_commit,
                    "staging_job_id": staging_job.id,
                },
                path=audit_path,
            )
            return {
                "approval": db.get_approval(approval_id),
                "job": job,
                "coding_run_id": run_id,
                "engineering_task_id": engineering_task.id,
                "staging_job_id": staging_job.id,
            }

        if ssh_run is None:
            raise ValueError("coding_task 需要 ssh_run，呼叫端未提供")
        if getattr(app_state, "ssh_write_file", None) is None:
            raise ValueError("coding_task 需要 app_state.ssh_write_file，呼叫端未提供")
        ssh_write_file = app_state.ssh_write_file

        #: 相對 Runner SSH user home 的路徑轉換，跟 `cleanup_coding_run()`
        #: 共用同一個純函式（`resolve_codex_workspace_rel()`），行為必須一致。
        workspace_rel = resolve_codex_workspace_rel(config.codex_workspace_root)

        run_id = db.insert_coding_run(
            approval_id=approval_id,
            project=project,
            runner_server=runner,
            instruction=instruction,
            base_branch=base_branch,
            validation_target=validation_target,
            worktree_path=f"~/{workspace_rel}/tasks/{approval_id}",
            status="queued",
        )

        task_rel_dir = f"{workspace_rel}/tasks/{approval_id}"
        await ssh_run(runner, f"mkdir -p {shlex.quote(task_rel_dir)}", 15)
        instruction_file = build_codex_instruction_file(instruction, network_access)
        await ssh_write_file(runner, f"{task_rel_dir}/instruction.txt", instruction_file)

        script = build_coding_task_script(
            approval_id,
            workspace_rel,
            project,
            source_kind,
            source,
            base_branch,
            network_access,
        )

        job = enqueue_job(
            db,
            command=script,
            type="coding",
            project=project,
            pin_server=runner,
            audit_path=audit_path,
            audit_actor=decision_audit_actor,
        )
        db.update_coding_run(run_id, job_id=job.id)

        approve_note = (
            f"已建立 coding 任務 #{job.id}（Runner {runner}，coding_run #{run_id}，"
            f"branch ai-task-{approval_id}）"
        )
        db.update_approval(
            approval_id, status="approved", decided_at=now_iso(), note=approve_note
        )
        append_audit(
            "coding_task",
            {
                "approval_id": approval_id,
                "project": project,
                "runner_server": runner,
                "job_id": job.id,
                "coding_run_id": run_id,
                "source_kind": source_kind,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "job": job,
            "coding_run_id": run_id,
        }

    if approval.kind == "engineering_task_retry":
        # D3 first slice: a new attempt of the same immutable task contract.
        # This mirrors the immutable branch of kind=coding_task above almost
        # exactly (same Hub/path-policy/runner revalidation, same deterministic
        # script builders) but targets attempt N>1 through
        # ``finalize_engineering_task_retry_plan`` instead of attempt 1's
        # ``finalize_engineering_task_approval_plan``.
        payload = approval.payload if isinstance(approval.payload, dict) else {}
        retry_task_id = payload.get("engineering_task_id")
        attempt_number = payload.get("attempt_number")

        def _reject_retry(reject_note: str) -> dict:
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "engineering_task_retry",
                {
                    "approval_id": approval_id,
                    "engineering_task_id": retry_task_id,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        if (
            not isinstance(retry_task_id, str)
            or not retry_task_id
            or not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number < 2
        ):
            return _reject_retry("engineering_task_retry payload 缺少必要欄位")
        task = db.get_engineering_task(retry_task_id)
        if task is None:
            return _reject_retry("engineering_task 不存在")
        parent_approval = db.get_approval(task.approval_id)
        if (
            parent_approval is None
            or parent_approval.id != payload.get("parent_approval_id")
            or parent_approval.kind != "coding_task"
            or parent_approval.status != "approved"
            or _canonical_payload_digest(parent_approval.payload)
            != payload.get("parent_approval_payload_sha256")
        ):
            return _reject_retry(
                "parent coding_task approval 已改變或不再 approved；請建立新請求"
            )
        if (
            payload.get("project") != task.project_name
            or payload.get("project_id") != task.project_id
            or payload.get("project_version_id") != task.project_version_id
            or str(payload.get("base_commit") or "").lower()
            != task.base_commit.lower()
            or payload.get("runner_server") != task.runner_server
        ):
            return _reject_retry("engineering_task 與核准 payload 不一致；請建立新請求")
        if task.status not in CODING_RUN_TERMINAL_STATUSES:
            return _reject_retry(
                f"task 目前狀態 {task.status!r} 不是終態，無法核准 retry"
            )
        active_jobs = [
            j
            for j in db.list_engineering_task_jobs(retry_task_id)
            if j.status in {"queued", "running"}
        ]
        if active_jobs:
            return _reject_retry(
                "此 task 仍有 queued/running 的 owner Job，無法核准 retry"
            )
        attempts = db.list_engineering_task_attempt_runs(retry_task_id)
        if not attempts or attempts[-1].attempt_number + 1 != attempt_number:
            return _reject_retry(
                "attempt_number 已 stale（可能有其他 retry 先核准）；請建立新請求"
            )

        if app_state is None:
            raise ValueError(
                "engineering_task_retry 需要 app_state（config／ssh_write_file），"
                "呼叫端未提供"
            )
        config = app_state.config
        if not config.engineering_task_backend_v1:
            raise ValueError("AI Engineering Task backend 未啟用")
        runner = config.codex_runner_server
        if runner is None or runner != task.runner_server:
            return _reject_retry(
                "CODEX_RUNNER_SERVER 已與 approved payload 不同；請建立新請求"
            )
        runner_cfg = (server_configs or {}).get(runner)
        approved_runner = task.execution_contract.get("runner")
        current_runner = (
            {
                "name": runner_cfg.name,
                "host": runner_cfg.host,
                "user": runner_cfg.user,
                "port": runner_cfg.port,
            }
            if runner_cfg is not None
            else None
        )
        if (
            runner_cfg is None
            or not getattr(runner_cfg, "enabled", True)
            or current_runner != approved_runner
        ):
            return _reject_retry(
                "Coding Runner identity/config 已與 approved contract 不同；請建立新請求"
            )
        try:
            current_workspace_rel = resolve_codex_workspace_rel(
                config.codex_workspace_root
            )
        except ValueError:
            return _reject_retry(
                "Coding workspace 設定無效；請修正平台設定後建立新請求"
            )
        if task.execution_contract.get("workspace_rel") != current_workspace_rel:
            return _reject_retry(
                "Coding workspace 設定已與 approved contract 不同；請建立新請求"
            )

        path_policy: Optional[dict[str, Any]] = None
        path_policy_sha256: Optional[str] = None
        path_verifier: Optional[dict[str, str]] = None
        if task.contract_version == ENGINEERING_TASK_CONTRACT_V2:
            path_policy_value = task.execution_contract.get("path_policy")
            path_policy_sha_value = task.execution_contract.get(
                "path_policy_sha256"
            )
            path_verifier_value = task.execution_contract.get("path_verifier")
            try:
                validated_policy = validate_engineering_path_policy(
                    path_policy_value, path_policy_sha_value
                )
                validated_verifier = validate_engineering_path_verifier_contract(
                    path_verifier_value
                )
                expected_policy, expected_policy_sha = (
                    build_engineering_path_policy(
                        task.structured_request.get("allowed_paths"),
                        task.structured_request.get("prohibited_paths"),
                    )
                )
            except (EngineeringPathPolicyError, AttributeError, TypeError):
                return _reject_retry(
                    "approved final Git path policy 無法驗證；請建立新請求"
                )
            if (
                validated_policy != expected_policy
                or path_policy_sha_value != expected_policy_sha
                or validated_policy.get("verifier") != validated_verifier
                or path_verifier_value != validated_verifier
            ):
                return _reject_retry(
                    "final Git path policy 與 structured request 不一致；請建立新請求"
                )
            path_policy = validated_policy
            path_policy_sha256 = path_policy_sha_value
            path_verifier = validated_verifier

        if local_run is None:
            raise ValueError("engineering_task_retry 需要 local_run")
        try:
            resolved_commit, _metadata = await inspect_hub_project_version(
                project_name=task.project_name,
                git_commit=task.base_commit,
                local_home_dir=config.local_home_dir,
                local_run=local_run,
            )
        except InvalidEngineeringTaskRequestError:
            return _reject_retry(
                "approved ProjectVersion 無法在 Hub 中安全驗證；請建立新請求"
            )
        if resolved_commit.lower() != task.base_commit.lower():
            return _reject_retry("Hub exact commit 與 approved base 不一致")

        if getattr(app_state, "ssh_write_file", None) is None:
            raise ValueError(
                "engineering_task_retry 需要 app_state.ssh_write_file，呼叫端未提供"
            )

        workspace_rel = task.execution_contract["workspace_rel"]
        instruction_file = build_codex_instruction_file(task.instruction, False)
        path_policy_file: Optional[str] = None
        path_verifier_file: Optional[str] = None
        if task.contract_version == ENGINEERING_TASK_CONTRACT_V2:
            path_policy_file = render_engineering_path_policy_file(path_policy)
            path_verifier_file = engineering_path_verifier_source()

        script = build_coding_task_script(
            approval_id,
            workspace_rel,
            task.project_name,
            "hub_bundle",
            remote_engineering_bundle_path(task.id),
            None,
            False,
            exact_base_commit=task.base_commit,
            agent_provider_id=task.agent_provider_id,
            path_policy_sha256=path_policy_sha256,
            path_verifier_sha256=(
                path_verifier["source_sha256"] if path_verifier is not None else None
            ),
        )
        staging_script = " && ".join(
            (
                build_engineering_bundle_create_command(
                    task.id, task.project_name, task.base_commit, config.local_home_dir
                ),
                build_engineering_bundle_verify_command(
                    task.id, task.project_name, task.base_commit, config.local_home_dir
                ),
                build_engineering_staging_push_command(
                    task.id,
                    approval_id,
                    workspace_rel,
                    runner_cfg,
                    config.local_home_dir,
                    include_path_policy=(
                        task.contract_version == ENGINEERING_TASK_CONTRACT_V2
                    ),
                ),
            )
        )
        for command_role, command in (
            ("staging", staging_script),
            ("coding", script),
        ):
            dangerous, reason = is_dangerous(command)
            if dangerous:
                return _reject_retry(
                    f"deterministic {command_role} command 被安全政策拒絕：{reason}"
                )

        await app_state.ssh_write_file(
            LOCAL_SERVER,
            local_engineering_instruction_relpath(task.id),
            instruction_file,
        )
        if path_policy_file is not None and path_verifier_file is not None:
            await app_state.ssh_write_file(
                LOCAL_SERVER,
                local_engineering_path_policy_relpath(task.id),
                path_policy_file,
            )
            await app_state.ssh_write_file(
                LOCAL_SERVER,
                local_engineering_path_verifier_relpath(task.id),
                path_verifier_file,
            )

        try:
            run_id, staging_job_id, coding_job_id = (
                db.finalize_engineering_task_retry_plan(
                    task_id=task.id,
                    approval_id=approval_id,
                    attempt_number=attempt_number,
                    project=task.project_name,
                    runner_server=runner,
                    instruction=task.instruction,
                    base_commit=task.base_commit,
                    project_version_id=task.project_version_id,
                    validation_target=task.validation_target,
                    worktree_path=f"~/{workspace_rel}/tasks/{approval_id}",
                    staging_command=staging_script,
                    coding_command=script,
                    approval_note=None,
                    decision_actor_id=_actor_id(request_context),
                    decision_mechanism=_decision_mechanism(approved_by),
                )
            )
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise ValueError(
                f"Engineering Task retry plan 未建立：{exc}"
            ) from exc

        staging_job = db.get_job(staging_job_id)
        job = db.get_job(coding_job_id)
        for planned_job in (staging_job, job):
            append_audit(
                "enqueue",
                {
                    "job_id": planned_job.id,
                    **engineering_job_command_audit_fields(
                        planned_job.command, planned_job.engineering_task_role
                    ),
                    "project": task.project_name,
                    "priority": planned_job.priority,
                    "pin_server": planned_job.pin_server,
                    "require_tag": planned_job.require_tag,
                    "depends_on": planned_job.depends_on,
                    "engineering_task_id": task.id,
                    "engineering_task_role": planned_job.engineering_task_role,
                    "engineering_attempt_number": attempt_number,
                },
                path=audit_path,
            )
        append_audit(
            "engineering_task_retry",
            {
                "approval_id": approval_id,
                "project": task.project_name,
                "runner_server": runner,
                "job_id": job.id,
                "coding_run_id": run_id,
                "engineering_task_id": task.id,
                "attempt_number": attempt_number,
                "staging_job_id": staging_job.id,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "job": job,
            "coding_run_id": run_id,
            "engineering_task_id": task.id,
            "staging_job_id": staging_job.id,
        }

    if approval.kind == "engineering_task_discard":
        payload = approval.payload if isinstance(approval.payload, dict) else {}
        discard_task_id = payload.get("engineering_task_id")

        def _reject_discard(reject_note: str) -> dict:
            db.update_approval(
                approval_id, status="rejected", decided_at=now_iso(), note=reject_note
            )
            append_audit(
                "engineering_task_discard",
                {
                    "approval_id": approval_id,
                    "engineering_task_id": discard_task_id,
                    "reason": reject_note,
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        if not isinstance(discard_task_id, str) or not discard_task_id:
            return _reject_discard("engineering_task_discard payload 缺少必要欄位")
        task = db.get_engineering_task(discard_task_id)
        if task is None:
            return _reject_discard("engineering_task 不存在")
        if (
            payload.get("project") != task.project_name
            or payload.get("project_id") != task.project_id
        ):
            return _reject_discard("engineering_task 與核准 payload 不一致；請建立新請求")
        if task.status not in CODING_RUN_TERMINAL_STATUSES:
            return _reject_discard(
                f"task 目前狀態 {task.status!r} 不是終態，無法核准 discard"
            )
        active_jobs = [
            j
            for j in db.list_engineering_task_jobs(discard_task_id)
            if j.status in {"queued", "running"}
        ]
        if active_jobs:
            return _reject_discard(
                "此 task 仍有 queued/running 的 owner Job，無法核准 discard"
            )

        db.update_engineering_task(discard_task_id, status="discarded")
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
            note="AI Engineering Task 已作廢",
            decision_actor_id=_actor_id(request_context),
            decision_mechanism=_decision_mechanism(approved_by),
        )
        try:
            db.append_engineering_task_event(
                task_id=discard_task_id,
                attempt_number=None,
                event_key=f"approval:{approval_id}:discarded",
                event_type="task_discarded",
                phase="approval",
                state="discarded",
                source_kind="approval",
                source_id=str(approval_id),
                summary="AI Engineering Task 已標記作廢；結果與 artifact 視同 withheld",
                details={"approval_id": approval_id},
                actor_id=_actor_id(request_context),
            )
        except Exception:  # noqa: BLE001 - journal failure cannot reopen a decided approval
            logger.warning(
                "Engineering Task #%s discard event could not be recorded",
                discard_task_id,
            )
        append_audit(
            "engineering_task_discard",
            {
                "approval_id": approval_id,
                "project": task.project_name,
                "engineering_task_id": task.id,
            },
            path=audit_path,
        )
        return {
            "approval": db.get_approval(approval_id),
            "engineering_task_id": task.id,
        }

    if approval.kind == "server_add":
        if app_state is None:
            raise ValueError("核准 server_add 需要 app_state（reload 用），呼叫端未提供")
        payload = approval.payload
        name = payload.get("name")
        yaml_path = app_state.config.servers_yaml_path

        backup_path = backup_servers_yaml(yaml_path)
        servers_doc = load_servers_config(yaml_path)
        servers_list = list(servers_doc.get("servers") or [])
        if any(s.get("name") == name for s in servers_list):
            raise ValueError(f"server {name} 已存在，無法重複新增")
        servers_list.append(payload)
        servers_doc["servers"] = servers_list
        write_servers_yaml_atomically(yaml_path, servers_doc)
        reload_result = reload_server_config_if_supported(app_state)

        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "server_add",
            {"approval_id": approval_id, "name": name, "backup": backup_path},
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "reload": reload_result}

    if approval.kind == "server_update":
        if app_state is None:
            raise ValueError("核准 server_update 需要 app_state（reload 用），呼叫端未提供")
        payload = approval.payload
        name = payload["name"]
        updates = payload.get("updates") or {}
        yaml_path = app_state.config.servers_yaml_path

        backup_path = backup_servers_yaml(yaml_path)
        servers_doc = load_servers_config(yaml_path)
        servers_list = list(servers_doc.get("servers") or [])
        idx = next((i for i, s in enumerate(servers_list) if s.get("name") == name), None)
        if idx is None:
            raise ServerNotFoundError(f"server {name} 不存在")
        merged = dict(servers_list[idx])
        merged.update(updates)
        merged["name"] = name
        servers_list[idx] = merged
        servers_doc["servers"] = servers_list
        write_servers_yaml_atomically(yaml_path, servers_doc)
        reload_result = reload_server_config_if_supported(app_state)

        db.update_approval(approval_id, status="approved", decided_at=now_iso())
        append_audit(
            "server_update",
            {"approval_id": approval_id, "name": name, "updates": updates, "backup": backup_path},
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "reload": reload_result}

    if approval.kind in ("server_disable", "server_delete"):
        # 兩者第一版落地效果相同（設 enabled=false），差異只在 note／稽核
        # action 名稱（server_delete 記 note 說明「以停用取代刪除」，方便
        # 稽核追蹤使用者的真實意圖）。**核准前重查一次**該機器是否有
        # running job（仿既有 stop 核准「核准前重查狀態」模式，狀態可能在
        # 等待期間變化）——有 → approval 標 rejected，不改 servers.yaml。
        payload = approval.payload
        name = payload["name"]
        action_name = approval.kind

        running = [j for j in db.list_jobs(status="running") if j.server == name]
        if running:
            note = "該機器有執行中任務，無法停用" + ("/刪除" if action_name == "server_delete" else "")
            db.update_approval(approval_id, status="rejected", decided_at=now_iso(), note=note)
            append_audit(
                action_name,
                {
                    "approval_id": approval_id,
                    "name": name,
                    "note": note,
                    "running_job_ids": [j.id for j in running],
                },
                result="rejected",
                path=audit_path,
            )
            return {"approval": db.get_approval(approval_id)}

        if app_state is None:
            raise ValueError(f"核准 {action_name} 需要 app_state（reload 用），呼叫端未提供")
        yaml_path = app_state.config.servers_yaml_path

        backup_path = backup_servers_yaml(yaml_path)
        servers_doc = load_servers_config(yaml_path)
        servers_list = list(servers_doc.get("servers") or [])
        idx = next((i for i, s in enumerate(servers_list) if s.get("name") == name), None)
        if idx is None:
            raise ServerNotFoundError(f"server {name} 不存在")

        note: Optional[str] = None
        updated_entry = dict(servers_list[idx], enabled=False)
        if action_name == "server_delete":
            note = "第一版以停用取代刪除"
            updated_entry["note"] = note
        servers_list[idx] = updated_entry
        servers_doc["servers"] = servers_list
        write_servers_yaml_atomically(yaml_path, servers_doc)
        reload_result = reload_server_config_if_supported(app_state)

        db.update_approval(approval_id, status="approved", decided_at=now_iso(), note=note)
        append_audit(
            action_name,
            {"approval_id": approval_id, "name": name, "backup": backup_path, "note": note},
            path=audit_path,
        )
        return {"approval": db.get_approval(approval_id), "reload": reload_result}

    raise ValueError(f"unknown approval kind: {approval.kind}")


# --------------------------------------------------------------------------
# 階段 10（PLAN.md K.3）：自動核准規則諮詢
# --------------------------------------------------------------------------


async def maybe_auto_approve(
    db: Database,
    approval: Approval,
    *,
    source: str,
    rules: list[dict],
    ssh_run=None,
    server_configs: Optional[dict] = None,
    app_state: Optional[Any] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Optional[dict]:
    """approval **已經成功建立之後**，諮詢自動核准規則（`app.autoapprove`）
    決定要不要立刻核准（PLAN.md K.3）。

    **順序保證（鐵律，務必讀完再改這個函式）**：這個函式只可能在
    `request_enqueue_approval()`/`request_stop_approval()` 成功回傳、
    approval 已經寫進 DB 之後才會被呼叫——危險指令的黑名單檢查
    （`app.security.is_dangerous()`）在那之前就已經發生並且通過了，被擋下
    的指令連 `Approval` 物件都不存在，根本不會走到這裡。這裡**不**重做、
    也**不能**重做危險指令檢查（`app.autoapprove` 模組刻意不 import
    `app.security`/`app.approvals`，見該模組 docstring）。

    只支援 `kind` 為 `"enqueue"`／`"stop"`（PLAN.md K.3 規則欄位表也只定義
    了這兩種 kind＋`"any"`）；其他 kind 一律回傳 `None`（保持既有兩步流程，
    這個階段沒有替其他 kind 開放自動核准）。

    `source="web"` 且 `AppConfig.web_direct_execute` 為真的情況**不會**走到
    這裡——那個組合由呼叫端（`app/main.py` 的 `_finalize_approval()`）在
    K.2 的路徑直接處理掉了，不會呼叫這個函式；`web_direct_execute=false`
    時 web 來源一樣可以被這裡的規則命中（使用者自己配），跟其他來源沒有
    差別。

    kind=stop 且沒有提供 `ssh_run`（呼叫端沒有可用的 SSH 介面，例如某些
    只讀場景）-> 直接回傳 `None`，**不自動核准**，維持 pending，不報錯——
    寧可少自動化一點，也不要在缺少執行能力的情況下把 approval 標成
    approved 卻什麼都沒真的發生。

    命中規則（`app.autoapprove.evaluate()` 回傳非 `None` 的索引）->
    呼叫 `approve(..., approved_by=f"auto-rule-{index}", note=...)`，
    approval note 記「自動核准：規則 #{index}（source={source}）」，回傳
    `approve()` 的結果；沒有命中任何規則 -> 回傳 `None`（approval 保持
    pending，跟現狀完全一樣）。
    """
    if approval.kind not in ("enqueue", "stop"):
        return None
    if approval.kind == "stop" and ssh_run is None:
        return None

    payload = approval.payload or {}
    idx = autoapprove.evaluate(
        rules,
        source=source,
        kind=approval.kind,
        command=payload.get("command"),
        project=payload.get("project"),
        pin_server=payload.get("pin_server"),
    )
    if idx is None:
        return None

    note = f"自動核准：規則 #{idx}（source={source}）"
    return await approve(
        db,
        approval.id,
        ssh_run=ssh_run,
        audit_path=audit_path,
        server_configs=server_configs,
        app_state=app_state,
        approved_by=f"auto-rule-{idx}",
        note=note,
        request_context=request_context,
    )


async def maybe_auto_decide_placement(
    db: Database,
    approval: Approval,
    *,
    server_configs: Optional[dict] = None,
    app_state: Optional[Any] = None,
    audit_path: str = "audit.jsonl",
) -> Optional[dict]:
    """Goal 2 Slice 5 (INV-APPROVAL-4b): policy-scoped auto-decision for
    `kind=auto_placement` pending approvals only.

    This is a **separate** mechanism from `maybe_auto_approve()` — it does
    not touch that function or its `("enqueue", "stop")` kind gate
    (INV-APPROVAL-4 forbids expanding that whitelist). It may only ever act
    on `kind == "auto_placement"`.

    Gate order:
    1. Not `kind == "auto_placement"` or not currently `pending` → `None`
       (nothing to decide).
    2. `AUTO_PLACEMENT_KILL_SWITCH` on (the default) → `None`, always —
       proposals accumulate as ordinary pending approvals for a human,
       identical to Slice 4 behavior. This is checked *before* touching the
       DB for anything beyond the approval object already in hand.
    3. Read-only revalidation via `_validate_auto_placement_payload()` —
       the exact same INV-APPROVAL-4b conditions (2)-(6) enforced by the
       manual `approve()` branch. Any condition failing here means the
       approval **stays pending** (this function must never reject or
       downgrade to manual execution — only a human decision or a later,
       now-passing auto-decision tick may resolve it).
    4. All conditions pass → call `approve(..., approved_by=
       f"policy-{policy_id}-r{revision}", request_context=None)`. Passing
       `request_context=None` is deliberate: there is no human actor to
       attribute, and `_decision_mechanism()`/`_DecisionAttributingDatabase`
       already record `decision_actor_id=None` in that case — the decision
       is never attributed to a fabricated human. A dedicated
       `auto_placement_policy_decision` audit event (actor=
       `SYSTEM_AUDIT_ACTOR`) is appended afterward so the audit trail
       explicitly names the system as the decision-maker, on top of
       `approve()`'s own existing `auto_placement` audit event.

    Real errors from `approve()` itself (e.g. Dispatch Policy v1 disabled)
    propagate to the caller unchanged, mirroring `maybe_auto_approve()`'s
    existing contract — only the read-only pre-check step 3 is where "stay
    pending" is this function's own decision.
    """

    if approval.kind != "auto_placement" or approval.status != "pending":
        return None

    config = getattr(app_state, "config", None) if app_state is not None else None
    if bool(getattr(config, "auto_placement_kill_switch", True)):
        return None

    validation = _validate_auto_placement_payload(db, approval.payload, server_configs)
    if not validation.ok:
        logger.debug(
            "auto_placement: policy-scoped auto-decision leaves approval %s"
            " pending (%s)",
            approval.id,
            validation.reason,
        )
        return None

    policy_id = approval.payload["policy_id"]
    revision = approval.payload["policy_revision"]
    approved_by = f"policy-{policy_id}-r{revision}"

    result = await approve(
        db,
        approval.id,
        server_configs=server_configs,
        app_state=app_state,
        approved_by=approved_by,
        audit_path=audit_path,
        request_context=None,
    )
    append_audit(
        "auto_placement_policy_decision",
        {
            "approval_id": approval.id,
            "policy_id": policy_id,
            "policy_revision": revision,
            "decision_mechanism": approved_by,
        },
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )
    return result


def reject(
    db: Database,
    approval_id: int,
    note: Optional[str] = None,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    approval = db.get_approval(approval_id)
    if approval is None:
        raise ApprovalNotFoundError(f"approval {approval_id} not found")
    if approval.status != "pending":
        raise ApprovalNotPendingError(f"approval {approval_id} 已經是 {approval.status}")

    engineering_task = db.get_engineering_task_by_approval_id(approval_id)
    if engineering_task is not None:
        db.reject_engineering_task_approval(
            task_id=engineering_task.id,
            approval_id=approval_id,
            note=note,
            decision_actor_id=_actor_id(request_context),
            decision_mechanism="manual",
        )
    else:
        engineering_validation = (
            db.get_engineering_validation_request_by_approval_id(approval_id)
        )
        if engineering_validation is not None:
            db.reject_engineering_validation_approval(
                validation_request_id=engineering_validation.id,
                approval_id=approval_id,
                note=note,
                decision_actor_id=_actor_id(request_context),
                decision_mechanism="manual",
            )
        else:
            db.update_approval(
                approval_id,
                status="rejected",
                decided_at=now_iso(),
                note=note,
                decision_actor_id=_actor_id(request_context),
                decision_mechanism="manual",
            )
    append_audit(
        "reject",
        {"approval_id": approval_id, "kind": approval.kind, "note": note},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)
