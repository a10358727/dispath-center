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
