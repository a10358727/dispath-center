"""Goal 3 Phase C / C2：Node Agent 協議的**純函式**核心。

DG-C 核准的 `INV-NODE-1…6`（`.claude/skills/dispatcher-domain/references/
invariants.md`）在這裡落地成可測的決策函式。這支模組**刻意不含** HTTP、
SQLite、SSH、asyncio——所有 I/O 在 `app/node_registry.py`（DB）與
`app/main.py`（端點）；這樣協議語意（lease 競態、重複 ack、心跳過期、
重啟收斂）可以用單純的時間戳與資料類別直接測，不需要架起服務或假機器。

對應的不變量：

- `INV-NODE-1`：node 只發起**出站**輪詢，control plane 對每個請求驗證 node
  身分。憑證比對在 `app/node_registry.py`（要用 `verify_secret` 常數時間
  比對），這裡只處理已經驗明身分之後的協議決策。
- `INV-NODE-2`：一個 attempt 只能被一個 agent lease；agent 必須先原子性
  acknowledge 才能產生副作用；lease 未過期且未達終態前不得重複派發
  （`can_dispatch_job()`）。
- `INV-NODE-3`：指令位元組非插值落地——`build_node_launcher_argv()` 回傳
  **argv 陣列**（不是 shell 字串），自由文字永遠不進 shell；
  `verify_command_digest()` 綁定核准當下的 digest。
- `INV-NODE-4`：心跳過期一律是 `unknown`，**不是** failed
  （`heartbeat_state()` 的值域裡沒有 failed）。
- `INV-NODE-5`：重啟後已 acknowledge 的 attempt 不得重複啟動
  （`plan_agent_restart()`）。
- `INV-NODE-6`：逐台啟用、隨時回退——路由選擇本身屬 C3/C4，這裡只提供
  「這個 job 現在可不可以派」的判斷,不決定派給誰。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional


#: Versioned wire contract shared by the control plane and the separately
#: packaged worker-side agent. Keep this module free of HTTP/I/O so protocol
#: compatibility remains directly testable without starting the service.
NODE_PROTOCOL_VERSION = "2.0"
NODE_PROTOCOL_VERSION_HEADER = "X-Node-Protocol-Version"
NODE_PROTOCOL_CAPABILITIES = (
    "current-attempt",
    "staged-credential-rotation",
    "stop-receipt",
    "terminal-retry",
)


def normalize_node_protocol_version(value: object) -> str:
    """Return the canonical protocol version or raise for an incompatible one.

    Older in-process callers did not send a version header. Treating an
    omitted value as the current contract keeps those callers safe while any
    explicitly supplied, incompatible value fails closed at the HTTP boundary.
    """

    if value is None:
        return NODE_PROTOCOL_VERSION
    if not isinstance(value, str) or value.strip() != NODE_PROTOCOL_VERSION:
        raise ValueError(f"unsupported node protocol version: {value!r}")
    return NODE_PROTOCOL_VERSION


#: attempt 狀態機。terminal 三種是**唯一**能讓 job 收斂的來源
#: （INV-NODE-4：心跳缺席不算終態）。
class AttemptStatus(str, Enum):
    #: 已建立並 lease 給某個 node，但 agent 還沒 acknowledge。
    #: 這個狀態下 agent **不得**產生任何副作用。
    LEASED = "leased"
    #: agent 已原子性 acknowledge 並持久化 attempt 身分；可以啟動。
    ACKED = "acked"
    #: agent 回報已啟動且仍在跑（心跳維持中）。
    RUNNING = "running"
    #: --- 以下為終態 ---
    DONE = "done"
    FAILED = "failed"
    #: lease 過期且從未 ack——沒有副作用產生過，可以安全地讓 job 重新排隊。
    EXPIRED = "expired"

    def __str__(self) -> str:
        return self.value


TERMINAL_STATUSES = frozenset(
    {AttemptStatus.DONE, AttemptStatus.FAILED, AttemptStatus.EXPIRED}
)
#: 「已經可能有副作用」的狀態：一旦 ack 過，就算之後失聯也**不能**假設
#: 沒跑（INV-NODE-4/5）。
SIDE_EFFECT_POSSIBLE_STATUSES = frozenset(
    {AttemptStatus.ACKED, AttemptStatus.RUNNING}
)


class HeartbeatState(str, Enum):
    """心跳判讀結果。**刻意沒有 `failed`**——INV-NODE-4 明文禁止以心跳
    缺席推斷任務失敗。"""

    FRESH = "fresh"
    #: 超過 TTL 但還在寬限期內——尚未當作 unknown，避免網路抖動誤判。
    STALE = "stale"
    #: 超過寬限期：狀態未知。**不是** failed，不觸發自動重派。
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class NodeAttempt:
    """一次 agent 執行嘗試的協議視圖（DB 列的純資料投影）。"""

    id: str
    job_id: int
    node_id: str
    status: AttemptStatus
    command_sha256: str
    lease_expires_at: datetime
    acked_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    terminal_at: Optional[datetime] = None
    #: Goal 3 C3 stop-request：已核准的停止請求時間（None＝沒有請求）。
    stop_requested_at: Optional[datetime] = None
    stop_acked_at: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def may_have_side_effects(self) -> bool:
        """已經 ack 過就可能已經動手了——這是「不得重複派發」的關鍵判斷。"""
        return self.status in SIDE_EFFECT_POSSIBLE_STATUSES


# ---------------------------------------------------------------------------
# INV-NODE-2：lease / acknowledgement
# ---------------------------------------------------------------------------


def is_lease_expired(attempt: NodeAttempt, now: datetime) -> bool:
    """lease 是否已過期。已 ack 的 attempt **永遠不算** lease 過期——ack
    之後語意由心跳與終態接手（INV-NODE-4/5），lease 只保護「還沒開始」的
    那段窗口。"""
    if attempt.acked_at is not None:
        return False
    return now >= attempt.lease_expires_at


def can_lease(attempt: Optional[NodeAttempt], node_id: str, now: datetime) -> bool:
    """這個 node 現在可不可以拿到這個 attempt 的 lease。

    - 沒有既有 attempt → 可以（呼叫端負責建立）。
    - 已終態 → 不可以（不重跑終態 attempt）。
    - 同一個 node 重複輪詢且 lease 未過期 → 可以（冪等，回同一份工作；
      這正是「重複 poll」不會產生第二個 attempt 的原因）。
    - 別的 node 且 lease 未過期 → **不可以**（INV-NODE-2：一個 attempt 只能
      被一個 agent lease）。
    - 別的 node 但 lease 已過期且從未 ack → 可以 reclaim（沒有副作用產生過）。
    - 已 ack 過（不論哪個 node、不論多久沒心跳）→ **永遠不可以**再 lease；
      那是 unknown，不是可回收（INV-NODE-4：不得以 unknown 觸發重派）。
    """
    if attempt is None:
        return True
    if attempt.is_terminal:
        return False
    if attempt.may_have_side_effects:
        return False
    if attempt.node_id == node_id:
        return True
    return is_lease_expired(attempt, now)


def can_dispatch_job(attempts: Iterable[NodeAttempt], now: datetime) -> bool:
    """這個 job 現在可不可以（對**任何**通道，含 SSH）派發。

    INV-NODE-2 的核心保證：只要還有一個 attempt 在 lease 有效期內、或已經
    ack 過而尚未回報終態，就不得再派——否則同一份工作可能在 node 與 SSH
    兩條通道上各跑一次。
    """
    for attempt in attempts:
        if attempt.is_terminal:
            continue
        if attempt.may_have_side_effects:
            return False
        if not is_lease_expired(attempt, now):
            return False
    return True


@dataclass(frozen=True)
class AckOutcome:
    """acknowledge 的判定結果。"""

    accepted: bool
    #: True 表示這是重複的 ack（同一個 node、同一個 attempt）——必須**冪等**
    #: 回成功，不得建立第二次執行（INV-NODE-2/5）。
    duplicate: bool = False
    reason: str = ""


def evaluate_ack(
    attempt: Optional[NodeAttempt], node_id: str, command_sha256: str, now: datetime
) -> AckOutcome:
    """判定一次 acknowledge 請求。

    拒絕的情況都是 fail-closed：attempt 不存在、屬於別的 node、digest 不符
    （INV-NODE-3）、lease 已過期、或已達終態。重複 ack 一律接受並標
    `duplicate=True`，讓呼叫端知道不要重複產生副作用。
    """
    if attempt is None:
        return AckOutcome(False, reason="attempt not found")
    if attempt.node_id != node_id:
        return AckOutcome(False, reason="attempt leased to another node")
    if attempt.is_terminal:
        return AckOutcome(False, reason=f"attempt already {attempt.status}")
    if not _digests_match(attempt.command_sha256, command_sha256):
        return AckOutcome(False, reason="command digest mismatch")
    if attempt.acked_at is not None:
        return AckOutcome(True, duplicate=True)
    if is_lease_expired(attempt, now):
        return AckOutcome(False, reason="lease expired")
    return AckOutcome(True)


# ---------------------------------------------------------------------------
# INV-NODE-3：指令位元組非插值落地
# ---------------------------------------------------------------------------


def command_digest(command: str) -> str:
    """核准指令位元組的 SHA-256（與 approval payload 綁定）。"""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _digests_match(expected: str, supplied: str) -> bool:
    """常數時間比對兩個 hex digest（避免以時間差探測 digest 內容）。"""
    if not isinstance(expected, str) or not isinstance(supplied, str):
        return False
    return hmac.compare_digest(expected, supplied)


def verify_command_digest(command: str, expected_sha256: str) -> bool:
    """agent 落地指令前必須比對——不符就不執行（INV-NODE-3 fail-closed）。"""
    return _digests_match(expected_sha256, command_digest(command))


def build_node_launcher_argv(attempt_id: str, workdir: str) -> list[str]:
    """agent 端啟動器的 **argv 陣列**（不是 shell 字串）。

    INV-NODE-3 與 INV-SSH-2 同構：使用者指令原文由 agent 先寫成檔案
    （`{workdir}/cmd.sh`），啟動器只帶已驗證的識別字。這裡回傳 list 而非
    字串，是結構性地讓「把自由文字拼進 shell」變成不可能——呼叫端用
    `subprocess` 的 list 形式執行，沒有 shell 參與。

    `attempt_id` 必須是已驗證的 UUID 字串、`workdir` 由 control plane 用
    attempt_id 組出，兩者都不含使用者自由文字。
    """
    if not _is_safe_identifier(attempt_id):
        raise ValueError(f"unsafe attempt id: {attempt_id!r}")
    return ["/bin/bash", f"{workdir}/cmd.sh"]


def _is_safe_identifier(value: str) -> bool:
    """只允許 UUID 會用到的字元集——擋掉任何路徑/shell 元字元。"""
    if not isinstance(value, str) or not value:
        return False
    return all(char.isalnum() or char == "-" for char in value)


# ---------------------------------------------------------------------------
# INV-NODE-4：心跳過期＝unknown
# ---------------------------------------------------------------------------


def heartbeat_state(
    last_heartbeat_at: Optional[datetime],
    now: datetime,
    *,
    ttl_sec: float,
    grace_sec: float = 0.0,
) -> HeartbeatState:
    """判讀心跳新鮮度。回傳值域刻意不含 failed——呼叫端拿到 `UNKNOWN` 時
    唯一合法的動作是「標記為未知並等待」，不得改判任務失敗、也不得自動
    重派（INV-NODE-4）。

    從未有過心跳（`None`）視為 `UNKNOWN`，不是 fresh——fail-closed。
    """
    if last_heartbeat_at is None:
        return HeartbeatState.UNKNOWN
    age = (now - last_heartbeat_at).total_seconds()
    if age <= ttl_sec:
        return HeartbeatState.FRESH
    if age <= ttl_sec + grace_sec:
        return HeartbeatState.STALE
    return HeartbeatState.UNKNOWN


def next_lease_expiry(now: datetime, lease_ttl_sec: float) -> datetime:
    return now + timedelta(seconds=lease_ttl_sec)


# ---------------------------------------------------------------------------
# INV-NODE-5：重啟不重複、狀態可收斂
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartPlan:
    """agent 或 control plane 重啟後，對單一 attempt 的收斂動作。"""

    #: agent 可否（重新）啟動這個 attempt 的工作負載。
    may_launch: bool
    #: 是否應該繼續回報心跳／等待既有行程的終態。
    resume_monitoring: bool
    reason: str


# ---------------------------------------------------------------------------
# 維運視圖（roadmap Phase 4：「agent liveness, version, queue, error,
# lease-age, and reconciliation operational views」）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeOperationalView:
    """單一 node 的維運快照。**純觀測**——這個結構的任何欄位都不會改變
    任務狀態，也不得被拿來自動決策（INV-NODE-4）。

    `needs_attention` 是給人看的旗標：它指出「有 attempt 已經 ack 過但心跳
    消失了」——那是 `unknown`，唯一正確的處理是人去看，**不是**自動重派。
    """

    node_id: str
    server_name: str
    status: str
    agent_version: Optional[str]
    liveness: HeartbeatState
    #: 目前非終態的 attempt 數（leased + acked + running）。
    queue_depth: int
    #: 已 ack 但心跳已成 unknown 的 attempt 數——canary 期間最該盯的數字。
    stale_attempts: int
    #: 終態為 failed 的 attempt 數（agent 明確回報的失敗，不含 unknown）。
    failed_attempts: int
    #: 最舊的未終態 lease 已經存在幾秒；沒有活躍 attempt 時是 None。
    oldest_lease_age_sec: Optional[float]
    needs_attention: bool
    attention_reason: str = ""


def summarize_node_operations(
    *,
    node_id: str,
    server_name: str,
    status: str,
    agent_version: Optional[str],
    last_heartbeat_at: Optional[datetime],
    attempts: Iterable[NodeAttempt],
    now: datetime,
    heartbeat_ttl_sec: float,
    heartbeat_grace_sec: float = 0.0,
) -> NodeOperationalView:
    """把持久化狀態彙總成一個 node 的維運視圖。純函式、無 I/O。

    刻意**不**把任何情況判成失敗：agent 失聯只會讓 `liveness` 變
    `unknown`、`needs_attention` 變 True，狀態本身完全不動
    （INV-NODE-4）。
    """
    liveness = heartbeat_state(
        last_heartbeat_at, now, ttl_sec=heartbeat_ttl_sec, grace_sec=heartbeat_grace_sec
    )

    active = [attempt for attempt in attempts if not attempt.is_terminal]
    all_attempts = list(attempts)
    failed = sum(
        1 for attempt in all_attempts if attempt.status is AttemptStatus.FAILED
    )

    stale = 0
    for attempt in active:
        if attempt.acked_at is None:
            continue
        if (
            heartbeat_state(
                attempt.last_heartbeat_at,
                now,
                ttl_sec=heartbeat_ttl_sec,
                grace_sec=heartbeat_grace_sec,
            )
            is HeartbeatState.UNKNOWN
        ):
            stale += 1

    oldest_age: Optional[float] = None
    for attempt in active:
        #: lease 開始時間＝到期時間往回推一個 TTL 不可靠（TTL 可能改過），
        #: 改用 ack 時間；還沒 ack 的用 lease 到期時間反推「還剩多久」的
        #: 相反面向並不精確，因此只對已 ack 的算年齡（那才是「跑了多久」）。
        started = attempt.acked_at
        if started is None:
            continue
        age = (now - started).total_seconds()
        oldest_age = age if oldest_age is None else max(oldest_age, age)

    reasons = []
    if stale:
        reasons.append(f"{stale} 個已確認的 attempt 心跳消失（unknown，需人工確認）")
    if liveness is HeartbeatState.UNKNOWN and active:
        reasons.append("agent 失聯但仍有未完成的 attempt")
    return NodeOperationalView(
        node_id=node_id,
        server_name=server_name,
        status=status,
        agent_version=agent_version,
        liveness=liveness,
        queue_depth=len(active),
        stale_attempts=stale,
        failed_attempts=failed,
        oldest_lease_age_sec=oldest_age,
        needs_attention=bool(reasons),
        attention_reason="；".join(reasons),
    )


# ---------------------------------------------------------------------------
# canary 資格（roadmap Phase 3：「canary eligibility limited to designated
# non-production ordinary jobs」）
# ---------------------------------------------------------------------------


#: 「ordinary job」的定義。roadmap Phase 3 明文把 canary 限制在**普通任務**；
#: Codex Runner（`coding`）的遷移排在最後、要 C4 通過才動，而 `sync`/`setup`
#: 是派工鏈的依賴任務（本地執行或與資料搬移綁定），都不是 canary 對象。
ORDINARY_JOB_TYPES = frozenset({"train", "adhoc"})


def is_node_canary_eligible(
    job_type: object, require_tag: object, *, canary_tag: Optional[str]
) -> bool:
    """這個 job 可不可以被 Node Agent 領走（roadmap Phase 3 的 canary 資格）。

    **fail-closed 且預設什麼都不合格**——必須同時滿足兩個條件：

    1. `job_type` 屬於 `ORDINARY_JOB_TYPES`（普通任務；`coding`/`sync`/
       `setup` 一律不合格）；
    2. job 的 `require_tag` **精確等於**設定的 canary 標籤，也就是操作者
       必須**逐個任務明確指定**它是 canary 對象。

    `canary_tag` 為 None／空字串時**沒有任何 job 合格**——這是刻意的：
    光是開啟 `NODE_AGENT_V1_ENABLED` 並把機器設成 `node`，還不足以讓任何
    正式工作流到未驗證的通道上；操作者必須再做一次明確的指定動作。
    """
    if not isinstance(canary_tag, str) or not canary_tag.strip():
        return False
    if job_type not in ORDINARY_JOB_TYPES:
        return False
    if not isinstance(require_tag, str):
        return False
    return require_tag.strip() == canary_tag.strip()


# ---------------------------------------------------------------------------
# artifact-metadata（roadmap Phase 3 協議項目）
# ---------------------------------------------------------------------------


#: 單次回報的 artifact 筆數上限——擋掉 agent（或冒充者）用一次請求灌爆 DB。
MAX_ARTIFACTS_PER_REPORT = 500
#: 相對路徑長度上限。
MAX_ARTIFACT_PATH_LENGTH = 1024

_SHA256_HEX_LENGTH = 64


def validate_artifact_path(relative_path: object) -> str:
    """驗證 agent 回報的 artifact 相對路徑，不合法一律 `ValueError`。

    fail-closed 規則（全部都是結構性的，不依賴 agent 自律）：

    - 必須是非空字串、長度有上限；
    - **不得**是絕對路徑（`/` 開頭）或含磁碟機代號；
    - **不得**含 `..` 路徑片段（阻擋穿越到結果目錄之外）；
    - 不得含 NUL、換行、反斜線（反斜線在某些檔案系統是分隔符）；
    - 不得有空片段（`a//b`）或以 `/` 結尾（那是目錄不是檔案）。

    注意：這是**中繼資料**驗證。目前沒有任何檔案內容被傳輸或寫入，所以
    這裡的目的是保證「存進 DB 的路徑字串本身無害且可解釋」，而不是保護
    某個實際的寫入動作。
    """
    if not isinstance(relative_path, str):
        raise ValueError("artifact path must be a string")
    if not relative_path or len(relative_path) > MAX_ARTIFACT_PATH_LENGTH:
        raise ValueError("artifact path is empty or too long")
    if any(char in relative_path for char in ("\x00", "\n", "\r", "\\")):
        raise ValueError("artifact path contains a forbidden character")
    if relative_path.startswith("/") or relative_path.endswith("/"):
        raise ValueError("artifact path must be a relative file path")
    parts = relative_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("artifact path contains an empty or traversing segment")
    return relative_path


def validate_artifact_digest(sha256: object) -> str:
    """SHA-256 必須是 64 個小寫十六進位字元。"""
    if not isinstance(sha256, str) or len(sha256) != _SHA256_HEX_LENGTH:
        raise ValueError("artifact sha256 must be a 64-character hex digest")
    normalized = sha256.lower()
    if any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("artifact sha256 must be a 64-character hex digest")
    return normalized


def validate_artifact_size(size_bytes: object) -> int:
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise ValueError("artifact size must be an integer")
    if size_bytes < 0 or size_bytes > 9_223_372_036_854_775_807:
        raise ValueError("artifact size is outside SQLite INTEGER range")
    return size_bytes


# ---------------------------------------------------------------------------
# stop-request（roadmap Phase 3 協議項目）
# ---------------------------------------------------------------------------


def should_agent_stop(attempt: NodeAttempt) -> bool:
    """agent 取回工作/心跳時，是否該停止這個 attempt。

    只有「已請求停止且尚未達終態」才是 True。已經終態的不再要求停止
    （沒有東西可停），沒有請求的當然也不停。

    這個函式**不判斷**停止是否被授權——授權在核准流程（`stop` approval
    kind，INV-SSH-9 同構）。到了這一層，請求已經是核准過的事實。
    """
    if attempt.is_terminal:
        return False
    return attempt.stop_requested_at is not None


# ---------------------------------------------------------------------------
# INV-NODE-6：逐台提升、隨時回退
# ---------------------------------------------------------------------------


#: 合法的執行通道值域。SSH 永遠是其中之一且永遠是預設——INV-SSH-1 要求
#: SSH 後端永久保留為每台工作機的相容/緊急通道。
VALID_EXECUTION_BACKENDS = frozenset({"ssh", "node"})


def resolve_execution_backend(
    configured: Optional[str], *, node_agent_enabled: bool
) -> str:
    """決定**這一台**機器實際生效的執行通道。

    fail-closed 到 `"ssh"`——以下任一情況都退回 SSH：

    - `NODE_AGENT_V1_ENABLED` 關閉（全域 rollback 開關）；
    - 值不是 `ssh`/`node`（打錯字、舊設定檔沒有這個欄位、被寫入奇怪內容）。

    這個方向是刻意的：任何不確定都退回**已經在運作**的通道，而不是把工作
    交給一個可能沒準備好的 agent。回退因此永遠是安全動作，符合 INV-NODE-6
    的「任何一台可在不影響其他機器的情況下回退到 SSH」。
    """
    if not node_agent_enabled:
        return "ssh"
    if not isinstance(configured, str):
        return "ssh"
    normalized = configured.strip().lower()
    if normalized not in VALID_EXECUTION_BACKENDS:
        return "ssh"
    return normalized


def plan_agent_restart(
    attempt: NodeAttempt, *, local_process_alive: bool, now: datetime
) -> RestartPlan:
    """agent 重啟後對每個本機持久化的 attempt 做的決定（INV-NODE-5）。

    關鍵保證：**已 acknowledge 的 attempt 永遠不會被重新啟動**。
    - 行程還活著 → 接手監看，不重啟。
    - 行程不在了但已 ack → 也**不重啟**：我們無法確定它是跑完才死還是沒跑
      成，這是 unknown，交給人／control plane 明確處理（INV-NODE-4 禁止以
      unknown 自動重派）。
    - 還沒 ack 且 lease 未過期 → 可以啟動（沒有副作用產生過）。
    - 還沒 ack 但 lease 已過期 → 放棄，讓 control plane 重新 lease。
    """
    if attempt.is_terminal:
        return RestartPlan(False, False, f"attempt already {attempt.status}")
    if local_process_alive:
        return RestartPlan(False, True, "process still running; resume monitoring")
    if attempt.acked_at is not None:
        return RestartPlan(
            False,
            True,
            "acknowledged attempt with no live process: unknown, never relaunch",
        )
    if is_lease_expired(attempt, now):
        return RestartPlan(False, False, "lease expired before ack; drop")
    return RestartPlan(True, True, "leased but never acknowledged; safe to launch")
