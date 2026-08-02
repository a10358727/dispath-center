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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.db import Database, Node, NodeAttemptRow, now_iso
from app.identity import (
    generate_node_token,
    generate_secret,
    hash_secret,
    parse_node_token,
    verify_secret,
)
from app.node_protocol import (
    MAX_ARTIFACTS_PER_REPORT,
    is_node_canary_eligible,
    AttemptStatus,
    NodeAttempt,
    can_dispatch_job,
    command_digest,
    evaluate_ack,
    next_lease_expiry,
    summarize_node_operations,
    validate_artifact_digest,
    validate_artifact_path,
    validate_artifact_size,
)


#: lease 預設存活時間（秒）。agent 必須在這段時間內 acknowledge，否則
#: control plane 可以把 attempt 交給別的 node（因為還沒有副作用產生）。
DEFAULT_LEASE_TTL_SEC = 120.0
MAX_NODE_AGENT_VERSION_LENGTH = 128
MAX_NODE_LOG_TAIL_BYTES = 16 * 1024


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
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
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


@dataclass
class StagedNodeCredential:
    """One-time delivery for a credential that is not primary yet."""

    node: Node
    credential_id: str
    raw_token: str = field(repr=False)
    activation_nonce: str = field(repr=False)


@dataclass(frozen=True)
class NodeActivationAuth:
    node: Node
    credential_id: str
    duplicate: bool


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


def rotate_node_credential(
    db: Database, node_id: str, *, overlap_sec: Optional[int] = None
) -> Optional[EnrolledNode]:
    """替既有 node 換發憑證（roadmap Phase 3 的 "rotation"）。

    **保留同一個 node 身分**（id 不變，所以它已 lease/ack 的 attempt 歸屬
    完全不受影響）。這是 legacy/emergency one-step primitive；routine split
    protocol rotation uses :func:`stage_node_credential` instead.

    這與「撤銷後重新登錄」的差別很重要：撤銷會讓那個 node 失去身分，正在
    跑的 attempt 變成沒有主人；rotation 讓 agent 換一把鑰匙繼續認領自己的
    工作。已撤銷的 node 不能 rotate（要重新登錄）。

    `overlap_sec`（DG-NODE-V2 N-4）：給定時舊憑證在該秒數內仍然有效，
    agent 還沒拿到新 token 也不會開始失敗。不給則維持立即失效——那是緊急
    換鑰匙要的語意。
    """
    node = db.get_node(node_id)
    if node is None or not node.is_active:
        return None
    issued = generate_node_token(node_id)
    db.update_node_secret(node_id, issued.secret_hash, overlap_sec=overlap_sec)
    refreshed = db.get_node(node_id)
    if refreshed is None:
        return None
    return EnrolledNode(node=refreshed, raw_token=issued.raw_token)


def stage_node_credential(
    db: Database,
    node_id: str,
    *,
    pending_ttl_sec: int,
    grace_sec: int,
    approval_id: Optional[int] = None,
    replace_pending_credential_id: Optional[str] = None,
) -> Optional[StagedNodeCredential]:
    """Create a pending token/nonce pair without changing the primary.

    Raw values are returned once and never persisted. Replacing a response-lost
    pending delivery requires pinning the exact pending credential ID.
    """
    node = db.get_node(node_id)
    if node is None or not node.is_active:
        return None
    issued = generate_node_token(node_id)
    activation_nonce = generate_secret()
    credential_id = str(uuid.uuid4())
    staged = db.stage_node_credential(
        node_id=node_id,
        credential_id=credential_id,
        secret_hash=issued.secret_hash,
        activation_nonce_hash=hash_secret(activation_nonce),
        pending_ttl_sec=pending_ttl_sec,
        grace_sec=grace_sec,
        approval_id=approval_id,
        replace_pending_credential_id=replace_pending_credential_id,
    )
    if staged is None:
        return None
    return StagedNodeCredential(
        node=staged,
        credential_id=credential_id,
        raw_token=issued.raw_token,
        activation_nonce=activation_nonce,
    )


def _previous_secret_is_valid(node: Node, now: datetime) -> bool:
    """DG-NODE-V2 N-4: the outgoing secret stays usable until it expires.

    An expired or absent previous secret is simply not accepted; expiry is
    checked against the stored timestamp rather than assumed from its presence.
    """
    if not node.previous_secret_hash or not node.previous_secret_expires_at:
        return False
    expires = parse_iso(node.previous_secret_expires_at)
    if expires is None:
        return False
    return now < expires


def authenticate_node(db: Database, raw_token: Optional[str]) -> Node:
    """驗證 node 憑證並回傳該 node，失敗一律 `NodeAuthError`。

    fail-closed 順序：格式 → 查列 → 常數時間比對 → 撤銷檢查。任何一步
    失敗都拋同一種例外、同一段訊息（不洩漏哪一步失敗）。

    Rotation 期間（N-4）新舊憑證都接受，直到舊的過期為止。撤銷永遠優先於
    兩者——撤銷是立即的，不受重疊窗口保護。
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
    accepted = verify_secret(raw_token, node.secret_hash)
    if not accepted and _previous_secret_is_valid(node, datetime.now(timezone.utc)):
        accepted = verify_secret(raw_token, node.previous_secret_hash)
    if not accepted:
        raise NodeAuthError("invalid node credential")
    # Revocation is checked last and is absolute: the overlap window protects a
    # rotation, never a revoked credential.
    if not node.is_active:
        raise NodeAuthError("invalid node credential")
    return node


def authenticate_node_activation(
    db: Database,
    raw_token: Optional[str],
    activation_nonce: Optional[str],
) -> NodeActivationAuth:
    """Authenticate only the staged activation exchange.

    Pending tokens are deliberately invalid on every ordinary node route.
    The most recent primary+nonce pair is accepted only during its bounded
    receipt window so a lost activation response can be retried idempotently.
    """
    if not raw_token or not activation_nonce:
        raise NodeAuthError("invalid node credential")
    try:
        node_id, _secret = parse_node_token(raw_token)
    except ValueError:
        raise NodeAuthError("invalid node credential") from None
    node = db.get_node(node_id)
    if node is None or not node.is_active:
        raise NodeAuthError("invalid node credential")
    now = datetime.now(timezone.utc)
    pending_expires = parse_iso(node.pending_expires_at)
    if (
        node.pending_credential_id
        and node.pending_secret_hash
        and node.pending_activation_nonce_hash
        and pending_expires is not None
        and now < pending_expires
        and verify_secret(raw_token, node.pending_secret_hash)
        and verify_secret(activation_nonce, node.pending_activation_nonce_hash)
    ):
        return NodeActivationAuth(
            node=node,
            credential_id=node.pending_credential_id,
            duplicate=False,
        )
    receipt_expires = parse_iso(node.last_activation_expires_at)
    if (
        node.primary_credential_id
        and node.last_activation_credential_id == node.primary_credential_id
        and node.last_activation_nonce_hash
        and receipt_expires is not None
        and now < receipt_expires
        and verify_secret(raw_token, node.secret_hash)
        and verify_secret(activation_nonce, node.last_activation_nonce_hash)
    ):
        return NodeActivationAuth(
            node=node,
            credential_id=node.primary_credential_id,
            duplicate=True,
        )
    raise NodeAuthError("invalid node credential")


def activate_node_credential(
    db: Database,
    raw_token: Optional[str],
    activation_nonce: Optional[str],
) -> Optional[dict[str, Any]]:
    """Promote a staged credential, or replay its committed activation."""
    authenticated = authenticate_node_activation(
        db, raw_token, activation_nonce
    )
    return db.activate_pending_node_credential(
        node_id=authenticated.node.id,
        credential_id=authenticated.credential_id,
        secret_hash=hash_secret(str(raw_token)),
        activation_nonce_hash=hash_secret(str(activation_nonce)),
    )


def revoke_node(db: Database, node_id: str) -> Optional[Node]:
    """撤銷單一 node（INV-NODE-1）。冪等；不影響其他 node，也不影響這個
    node 已經 acknowledge 的 attempt——那些仍要靠終態或人工收斂
    （INV-NODE-4：撤銷不等於把任務判失敗）。"""
    return db.revoke_node(node_id)


def revoke_node_with_evidence(
    db: Database, node_id: str
) -> Optional[dict[str, Any]]:
    """Security-revoke one Node and return only non-secret audit evidence.

    The database performs credential invalidation and generic-attempt hold in
    one transaction.  Keeping this richer projection separate preserves the
    legacy ``revoke_node() -> Node`` interface used by older callers.
    """
    return db.revoke_node_with_execution_hold(node_id)


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


def select_job_for_node(
    db: Database,
    *,
    node: Node,
    canary_tag: Optional[str] = None,
) -> Optional[Any]:
    """DG-NODE-V2 N-1: the *control plane* picks the work.

    v1 let the agent name the `job_id` it wanted, which inverts the trust
    relationship — deciding what runs where is the control plane's job, and an
    agent naming its own work can ask for work it was never scheduled for.

    Selection is deterministic (FIFO by id) and applies the same eligibility
    the SSH path uses, plus the canary gate. It returns `None` rather than
    raising when nothing is eligible: no work is a normal answer, not an error.

    A draining node (N-3 routine retirement) is offered nothing new. It keeps
    its identity and finishes what it already holds — that is the whole
    difference between retiring a node and revoking it.
    """
    if node.is_draining:
        return None
    for job in db.list_jobs(status="queued"):
        # A pinned job belongs to exactly one machine.
        if job.pin_server is not None and job.pin_server != node.server_name:
            continue
        if not is_node_canary_eligible(
            job.type, job.require_tag, canary_tag=canary_tag
        ):
            continue
        if not job_is_dispatchable(db, job.id):
            continue
        return job
    return None


def lease_job_for_node(
    db: Database,
    *,
    node: Node,
    job_id: int,
    command: str,
    now: Optional[datetime] = None,
    lease_ttl_sec: float = DEFAULT_LEASE_TTL_SEC,
    canary_tag: Optional[str] = None,
    job_type: Optional[str] = None,
    require_tag: Optional[str] = None,
) -> LeaseResult:
    """把一個 job lease 給這個 node（INV-NODE-2）。

    canary 資格先由純函式判定；實際的「過期與建立 attempt」由單一
    `BEGIN IMMEDIATE` 交易重驗並寫入。**DB 寫入先於任何遠端副作用**。

    重複輪詢（同一 node、lease 未過期、尚未終態）回傳既有 attempt 且
    `reused=True`——**不會**產生第二個 attempt，這是「零重複啟動」的第一
    道保證。
    """
    now = now or datetime.now(timezone.utc)

    #: roadmap Phase 3：canary 資格閘門。**先於**任何 lease 判斷——不合格
    #: 的 job 連 attempt 都不會被建立。呼叫端沒傳 job_type/require_tag 時
    #: 一律不合格（fail-closed，不猜）。
    if not is_node_canary_eligible(job_type, require_tag, canary_tag=canary_tag):
        return LeaseResult(attempt=None, reason="job is not node-canary eligible")

    claimed = db.lease_legacy_node_attempt(
        attempt_id=str(uuid.uuid4()),
        job_id=job_id,
        node_id=node.id,
        command_sha256=command_digest(command),
        lease_expires_at=next_lease_expiry(now, lease_ttl_sec).isoformat(),
        observed_at=now.isoformat(),
        expected_job_type=str(job_type),
        expected_require_tag=require_tag,
    )
    return LeaseResult(
        attempt=claimed["attempt"],
        reused=bool(claimed["reused"]),
        reason=str(claimed["reason"]),
    )


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
    if row is not None and row.execution_attempt_id is not None:
        try:
            result = db.acknowledge_node_execution_attempt(
                node_attempt_id=attempt_id,
                node_id=node.id,
                command_sha256=command_sha256,
            )
        except ValueError as exc:
            return AckResult(False, reason=str(exc))
        return AckResult(
            bool(result["accepted"]),
            duplicate=bool(result["duplicate"]),
        )
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
    if agent_version is not None and (
        not isinstance(agent_version, str)
        or not agent_version
        or len(agent_version) > MAX_NODE_AGENT_VERSION_LENGTH
    ):
        raise ValueError("agent_version is invalid")
    db.touch_node_heartbeat(node.id, agent_version=agent_version)
    if attempt_id is None:
        return
    row = db.get_node_attempt(attempt_id)
    if row is None or row.node_id != node.id:
        return
    if to_protocol_attempt(row).is_terminal:
        return
    if row.execution_attempt_id is not None:
        db.observe_node_execution_heartbeat(
            node_attempt_id=attempt_id,
            node_id=node.id,
        )
        return
    db.update_node_attempt(attempt_id, last_heartbeat_at=now_iso())


@dataclass
class TerminalResult:
    accepted: bool
    duplicate: bool = False
    reason: str = ""
    job_id: Optional[int] = None
    execution_attempt_id: Optional[str] = None
    job_transitioned: bool = False


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
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        return TerminalResult(False, reason="exit_code must be between 0 and 255")
    if not isinstance(log_tail, str) or len(log_tail.encode("utf-8")) > MAX_NODE_LOG_TAIL_BYTES:
        return TerminalResult(False, reason="log_tail exceeds 16384 UTF-8 bytes")
    row = db.get_node_attempt(attempt_id)
    if row is None:
        return TerminalResult(False, reason="attempt not found")
    if row.node_id != node.id:
        return TerminalResult(False, reason="attempt belongs to another node")
    if row.execution_attempt_id is not None:
        try:
            result = db.record_node_execution_terminal(
                node_attempt_id=attempt_id,
                node_id=node.id,
                exit_code=exit_code,
                log_tail=log_tail,
            )
        except ValueError as exc:
            return TerminalResult(False, reason=str(exc))
        return TerminalResult(
            bool(result["accepted"]),
            duplicate=bool(result["duplicate"]),
            job_id=int(result["job_id"]),
            execution_attempt_id=str(result["execution_attempt_id"]),
        )

    try:
        result = db.record_legacy_node_terminal(
            attempt_id=attempt_id,
            node_id=node.id,
            exit_code=exit_code,
            log_tail=log_tail,
        )
    except ValueError as exc:
        return TerminalResult(False, reason=str(exc))
    return TerminalResult(
        True,
        duplicate=bool(result["duplicate"]),
        job_id=int(result["job_id"]),
        job_transitioned=bool(result["job_transitioned"]),
    )


@dataclass
class ArtifactReportResult:
    accepted: bool
    recorded: int = 0
    reason: str = ""


def record_artifact_metadata(
    db: Database, *, node: Node, attempt_id: str, artifacts: list
) -> ArtifactReportResult:
    """記錄 agent 回報的 artifact **中繼資料**（Goal 3 C3，roadmap Phase 3）。

    只存路徑/大小/digest，**不傳輸也不儲存任何檔案內容**——因此不需要決定
    上傳儲存位置、配額或清理策略（那些是 C4／另外的設計決策）。這張表的
    語意是「agent 說工作機上有這些檔案」，**不是**「Server A 已經取得這些
    檔案」；不得拿它當作結果已回收的證據。

    fail-closed：任一筆不合法（路徑穿越、digest 格式錯、負數大小、超過
    單次上限）就整批拒絕，不做部分寫入——避免 agent 用一批混雜資料塞進
    半套紀錄。
    """
    row = db.get_node_attempt(attempt_id)
    if row is None:
        return ArtifactReportResult(False, reason="attempt not found")
    if row.node_id != node.id:
        return ArtifactReportResult(False, reason="attempt belongs to another node")
    if row.acked_at is None:
        return ArtifactReportResult(False, reason="attempt was never acknowledged")
    if not isinstance(artifacts, list):
        return ArtifactReportResult(False, reason="artifacts must be a list")
    if len(artifacts) > MAX_ARTIFACTS_PER_REPORT:
        return ArtifactReportResult(
            False, reason=f"too many artifacts (max {MAX_ARTIFACTS_PER_REPORT})"
        )

    #: 先全部驗完再寫——全有或全無。
    validated = []
    seen_paths: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            return ArtifactReportResult(False, reason="artifact entry must be an object")
        if set(item) != {"path", "size_bytes", "sha256"}:
            return ArtifactReportResult(
                False, reason="artifact entry has unexpected fields"
            )
        try:
            relative_path = validate_artifact_path(item.get("path"))
            if relative_path in seen_paths:
                return ArtifactReportResult(
                    False, reason="duplicate artifact path in one report"
                )
            seen_paths.add(relative_path)
            validated.append(
                (
                    relative_path,
                    validate_artifact_size(item.get("size_bytes")),
                    validate_artifact_digest(item.get("sha256")),
                )
            )
        except ValueError as exc:
            return ArtifactReportResult(False, reason=str(exc))

    try:
        recorded = db.upsert_node_attempt_artifacts_batch(
            attempt_id=attempt_id,
            node_id=node.id,
            artifacts=validated,
        )
    except ValueError as exc:
        return ArtifactReportResult(False, reason=str(exc))
    return ArtifactReportResult(True, recorded=recorded)


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


def build_node_operations_report(
    db: Database,
    *,
    now: Optional[datetime] = None,
    heartbeat_ttl_sec: float = 60.0,
    heartbeat_grace_sec: float = 60.0,
) -> list:
    """所有 node 的維運視圖（roadmap Phase 4 的 operational views）。

    **唯讀**：不寫 DB、不連線、不改任何任務狀態。canary 期間就是靠這個
    盯「有沒有重複啟動、有沒有假失敗、有沒有 attempt 卡在 unknown」。
    """
    now = now or datetime.now(timezone.utc)
    report = []
    for node in db.list_nodes():
        attempts = [
            to_protocol_attempt(row)
            for row in db.list_node_attempts(node_id=node.id)
        ]
        report.append(
            summarize_node_operations(
                node_id=node.id,
                server_name=node.server_name,
                status=node.status,
                agent_version=node.agent_version,
                last_heartbeat_at=parse_iso(node.last_heartbeat_at),
                attempts=attempts,
                now=now,
                heartbeat_ttl_sec=heartbeat_ttl_sec,
                heartbeat_grace_sec=heartbeat_grace_sec,
            )
        )
    return report


def job_is_dispatchable(
    db: Database, job_id: int, now: Optional[datetime] = None
) -> bool:
    """這個 job 現在可不可以派給**任何**通道（含 SSH）。

    INV-NODE-2 對既有 SSH 排程路徑的接點：C3 之後 `scheduler_tick()` 會
    在派工前問這個問題。C2 本輪**尚未接上**——沒有任何 node、也沒有任何
    attempt 時它恆回 True，所以接上之後對現行行為也是零變更。
    """
    if db.has_unresolved_legacy_job_stop_intent(job_id):
        return False
    now = now or datetime.now(timezone.utc)
    attempts = [to_protocol_attempt(row) for row in db.list_node_attempts(job_id=job_id)]
    return can_dispatch_job(attempts, now)
