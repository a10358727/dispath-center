"""Goal 3 C2：Node Agent 註冊與協議的 DB 接線層。

分工（刻意跟 `app/node_protocol.py` 分開）：

- `app/node_protocol.py`：**純**決策函式（lease/ack/心跳/重啟），無 I/O。
- 本模組：把 DB 列翻譯成協議物件、驗證 node 身分、把協議決策落地成 DB
  寫入。**不含 HTTP**——端點在 `app/main.py`。
- agent 端：`agent/`，只透過 HTTP 講話，不 import 這裡任何東西。

安全邊界（INV-NODE-1）：`authenticate_node()` 是 node 端所有請求的唯一
入口。它做三件事且缺一不可——格式解析、常數時間 digest 比對、撤銷檢查。
raw token 永遠不落庫、不進稽核、不進日誌（只用 `redact_node_token()`）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db import Database, Node, NodeAttemptRow, now_iso
from app.identity import (
    generate_node_token,
    hash_secret,
    parse_node_token,
    verify_secret,
)
from app.node_protocol import (
    AttemptStatus,
    NodeAttempt,
    can_dispatch_job,
    can_lease,
    command_digest,
    evaluate_ack,
    next_lease_expiry,
)


#: lease 預設存活時間（秒）。agent 必須在這段時間內 acknowledge，否則
#: control plane 可以把 attempt 交給別的 node（因為還沒有副作用產生）。
DEFAULT_LEASE_TTL_SEC = 120.0


class NodeAuthError(Exception):
    """node 憑證無效、格式錯誤、或該 node 已被撤銷。

    **刻意不區分**這三者對外的錯誤訊息——不讓呼叫者用錯誤差異探測哪些
    node id 存在。
    """


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """把 DB 的 ISO 字串轉成 aware datetime；壞資料一律 None（fail-closed，
    交給協議層當作『沒有這個時間戳』處理）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_protocol_attempt(row: NodeAttemptRow) -> NodeAttempt:
    """DB 列 → 協議物件。無法解析的 status 一律當 `EXPIRED`（fail-closed：
    寧可視為不可再用，也不要把未知狀態當成可執行）。"""
    try:
        status = AttemptStatus(row.status)
    except ValueError:
        status = AttemptStatus.EXPIRED
    lease_expires = parse_iso(row.lease_expires_at)
    if lease_expires is None:
        #: 解析不出到期時間 ⇒ 視為早已過期（不給無限期 lease）。
        lease_expires = datetime.fromtimestamp(0, tz=timezone.utc)
    return NodeAttempt(
        id=row.id,
        job_id=row.job_id,
        node_id=row.node_id,
        status=status,
        command_sha256=row.command_sha256,
        lease_expires_at=lease_expires,
        acked_at=parse_iso(row.acked_at),
        last_heartbeat_at=parse_iso(row.last_heartbeat_at),
        terminal_at=parse_iso(row.terminal_at),
        stop_requested_at=parse_iso(row.stop_requested_at),
        stop_acked_at=parse_iso(row.stop_acked_at),
    )


# ---------------------------------------------------------------------------
# INV-NODE-1：身分
# ---------------------------------------------------------------------------


@dataclass
class EnrolledNode:
    """登錄結果。`raw_token` 只在這裡出現一次，呼叫端顯示給操作者之後
    必須丟棄——它不會被存下來，遺失只能撤銷後重新登錄。"""

    node: Node
    raw_token: str


def enroll_node(
    db: Database,
    *,
    server_name: str,
    approval_id: Optional[int] = None,
    node_id: Optional[str] = None,
) -> EnrolledNode:
    """替一台既有工作機登錄一個 Node Agent 身分（INV-NODE-1）。

    只寫 digest；raw token 隨回傳值交給呼叫端顯示一次。`server_name` 必須
    是 servers.yaml 既有的機器——驗證在 approval 層（呼叫端），這裡不重複
    查設定檔。
    """
    issued = generate_node_token(node_id)
    node = db.insert_node(
        node_id=issued.id,
        server_name=server_name,
        secret_hash=issued.secret_hash,
        approval_id=approval_id,
    )
    return EnrolledNode(node=node, raw_token=issued.raw_token)


def authenticate_node(db: Database, raw_token: Optional[str]) -> Node:
    """驗證 node 憑證並回傳該 node，失敗一律 `NodeAuthError`。

    fail-closed 順序：格式 → 查列 → 常數時間比對 → 撤銷檢查。任何一步
    失敗都拋同一種例外、同一段訊息（不洩漏哪一步失敗）。
    """
    if not raw_token:
        raise NodeAuthError("invalid node credential")
    try:
        node_id, _secret = parse_node_token(raw_token)
    except ValueError:
        raise NodeAuthError("invalid node credential") from None

    node = db.get_node(node_id)
    if node is None:
        raise NodeAuthError("invalid node credential")
    if not verify_secret(raw_token, node.secret_hash):
        raise NodeAuthError("invalid node credential")
    if not node.is_active:
        raise NodeAuthError("invalid node credential")
    return node


def revoke_node(db: Database, node_id: str) -> Optional[Node]:
    """撤銷單一 node（INV-NODE-1）。冪等；不影響其他 node，也不影響這個
    node 已經 acknowledge 的 attempt——那些仍要靠終態或人工收斂
    （INV-NODE-4：撤銷不等於把任務判失敗）。"""
    return db.revoke_node(node_id)


# ---------------------------------------------------------------------------
# INV-NODE-2：lease / ack 的落地
# ---------------------------------------------------------------------------


@dataclass
class LeaseResult:
    attempt: Optional[NodeAttemptRow]
    #: True 表示這次 poll 回的是**既有**的 attempt（重複輪詢的冪等結果），
    #: 不是新建立的。
    reused: bool = False
    reason: str = ""


def lease_job_for_node(
    db: Database,
    *,
    node: Node,
    job_id: int,
    command: str,
    now: Optional[datetime] = None,
    lease_ttl_sec: float = DEFAULT_LEASE_TTL_SEC,
) -> LeaseResult:
    """把一個 job lease 給這個 node（INV-NODE-2）。

    決策全部委給純函式 `can_lease()`／`can_dispatch_job()`；這裡只負責讀
    現況與寫入。**DB 寫入先於任何遠端副作用**——agent 是在拿到回應之後才
    可能動手，所以這一步完成時 lease 就已經持久化了。

    重複輪詢（同一 node、lease 未過期、尚未終態）回傳既有 attempt 且
    `reused=True`——**不會**產生第二個 attempt，這是「零重複啟動」的第一
    道保證。
    """
    now = now or datetime.now(timezone.utc)
    rows = db.list_node_attempts(job_id=job_id)
    attempts = [to_protocol_attempt(row) for row in rows]

    #: 已經有這個 node 的活躍 attempt → 冪等回傳，不新建。
    for row, attempt in zip(rows, attempts):
        if (
            attempt.node_id == node.id
            and not attempt.is_terminal
            and can_lease(attempt, node.id, now)
        ):
            return LeaseResult(attempt=row, reused=True, reason="existing lease")

    if not can_dispatch_job(attempts, now):
        return LeaseResult(attempt=None, reason="job already leased or in flight")

    latest = attempts[-1] if attempts else None
    if not can_lease(latest, node.id, now):
        return LeaseResult(attempt=None, reason="cannot lease")

    row = db.insert_node_attempt(
        attempt_id=str(uuid.uuid4()),
        job_id=job_id,
        node_id=node.id,
        command_sha256=command_digest(command),
        lease_expires_at=next_lease_expiry(now, lease_ttl_sec).isoformat(),
    )
    return LeaseResult(attempt=row, reused=False)


@dataclass
class AckResult:
    accepted: bool
    duplicate: bool = False
    reason: str = ""


def acknowledge_attempt(
    db: Database,
    *,
    node: Node,
    attempt_id: str,
    command_sha256: str,
    now: Optional[datetime] = None,
) -> AckResult:
    """agent 的 acknowledge（INV-NODE-2/3）。

    先用純函式判定，通過才走**原子性** UPDATE（`acked_at IS NULL` 條件在
    SQLite 層保證只有第一個請求會寫進去）。並行的第二個請求 rowcount=0，
    在這裡被翻譯成 `duplicate=True` 的成功回應——冪等，且絕不代表可以再
    啟動一次。
    """
    now = now or datetime.now(timezone.utc)
    row = db.get_node_attempt(attempt_id)
    attempt = to_protocol_attempt(row) if row else None

    outcome = evaluate_ack(attempt, node.id, command_sha256, now)
    if not outcome.accepted:
        return AckResult(False, reason=outcome.reason)
    if outcome.duplicate:
        return AckResult(True, duplicate=True)

    if db.ack_node_attempt(attempt_id, node.id):
        return AckResult(True)
    #: 競態：另一個並行請求先寫進去了。仍然是冪等成功，但標記為 duplicate
    #: 讓 agent 知道「不要再啟動一次」。
    return AckResult(True, duplicate=True)


def record_heartbeat(
    db: Database,
    *,
    node: Node,
    attempt_id: Optional[str] = None,
    agent_version: Optional[str] = None,
) -> None:
    """記錄心跳（INV-NODE-4：只是觀測，永不改任務狀態）。

    attempt 心跳只對**屬於這個 node 且尚未終態**的 attempt 生效——別的
    node 的 attempt 不會被這個 node 的心跳續命。
    """
    db.touch_node_heartbeat(node.id, agent_version=agent_version)
    if attempt_id is None:
        return
    row = db.get_node_attempt(attempt_id)
    if row is None or row.node_id != node.id:
        return
    if to_protocol_attempt(row).is_terminal:
        return
    db.update_node_attempt(attempt_id, last_heartbeat_at=now_iso())


@dataclass
class TerminalResult:
    accepted: bool
    duplicate: bool = False
    reason: str = ""


def record_terminal_result(
    db: Database,
    *,
    node: Node,
    attempt_id: str,
    exit_code: int,
    log_tail: str = "",
) -> TerminalResult:
    """agent 回報終態（INV-NODE-4：這是狀態收斂的**唯一**依據之一）。

    只接受這個 node 自己、已經 ack 過、尚未終態的 attempt。重複回報同一
    個終態是冪等成功（agent 的重送機制需要——roadmap Phase 3 明列
    "terminal upload retry"）。
    """
    row = db.get_node_attempt(attempt_id)
    if row is None:
        return TerminalResult(False, reason="attempt not found")
    if row.node_id != node.id:
        return TerminalResult(False, reason="attempt belongs to another node")

    attempt = to_protocol_attempt(row)
    if attempt.is_terminal:
        #: 重送：同樣的結果視為冪等成功；不同的結果**不覆蓋**已記錄的終態
        #: （第一個終態才算數，避免事後被改寫）。
        return TerminalResult(True, duplicate=True)
    if attempt.acked_at is None:
        return TerminalResult(False, reason="attempt was never acknowledged")

    db.update_node_attempt(
        attempt_id,
        status=(AttemptStatus.DONE if exit_code == 0 else AttemptStatus.FAILED).value,
        terminal_at=now_iso(),
        exit_code=exit_code,
        log_tail=log_tail,
    )
    return TerminalResult(True)


def request_job_stop(db: Database, job_id: int) -> list[str]:
    """對一個 job 目前所有非終態的 node attempt 記下停止請求（Goal 3 C3）。

    由**已核准的** stop 流程呼叫（`stop` approval kind；INV-SSH-9 對 SSH
    後端的要求在 node 通道同構）。回傳實際被記下請求的 attempt id 列表。

    重要語意：這是**請求**不是命令。control plane 沒有入站通道，不能直接
    殺掉工作機上的行程；agent 下次輪詢/心跳時取回並自行停止，再回報終態。
    因此這個函式回傳成功**不代表任務已停**——狀態仍以 agent 回報的終態
    為準（INV-NODE-4）。
    """
    requested: list[str] = []
    for row in db.list_node_attempts(job_id=job_id):
        if to_protocol_attempt(row).is_terminal:
            continue
        if db.request_node_attempt_stop(row.id):
            requested.append(row.id)
    return requested


def acknowledge_stop(db: Database, *, node: Node, attempt_id: str) -> bool:
    """agent 確認收到停止請求。回傳 True 表示這一次確實記下了 ack。"""
    row = db.get_node_attempt(attempt_id)
    if row is None or row.node_id != node.id:
        return False
    return db.ack_node_attempt_stop(attempt_id, node.id)


def job_is_dispatchable(
    db: Database, job_id: int, now: Optional[datetime] = None
) -> bool:
    """這個 job 現在可不可以派給**任何**通道（含 SSH）。

    INV-NODE-2 對既有 SSH 排程路徑的接點：C3 之後 `scheduler_tick()` 會
    在派工前問這個問題。C2 本輪**尚未接上**——沒有任何 node、也沒有任何
    attempt 時它恆回 True，所以接上之後對現行行為也是零變更。
    """
    now = now or datetime.now(timezone.utc)
    attempts = [to_protocol_attempt(row) for row in db.list_node_attempts(job_id=job_id)]
    return can_dispatch_job(attempts, now)
