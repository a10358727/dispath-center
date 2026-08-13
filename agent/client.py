"""Node Agent 的 control-plane 用戶端（純協議層，可注入傳輸）。

刻意設計成**傳輸無關**：建構時注入一個 `transport(method, path, json, headers)
-> (status, body)` 的 callable。正式使用時注入 httpx；測試時注入 in-process
fake（`tests/test_node_agent.py`），因此整包測試不需要真的開網路、也不需要
起服務（INV-TEST-2）。

INV-NODE-1：所有請求都是**出站**的，且一律帶 `X-Node-Token`；token 只從
建構參數進來，永不出現在任何回傳值、例外訊息或日誌字串裡。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Optional


# Keep the worker package self-contained: the control-plane module is not
# installed on nodes. Contract tests cross-check these values against
# ``app.node_protocol``.
NODE_PROTOCOL_VERSION = "2.0"
NODE_PROTOCOL_VERSION_HEADER = "X-Node-Protocol-Version"
NODE_PROTOCOL_CAPABILITIES = (
    "current-attempt",
    "staged-credential-rotation",
    "stop-receipt",
    "terminal-retry",
)


#: 與 `app.node_protocol.command_digest()` 必須逐位元一致。刻意各自實作
#: 而不是 import——工作機上不放 control plane 程式碼（見套件 docstring）。
#: 兩邊的一致性由 `tests/test_node_agent.py` 的 cross-check 測試釘住。
def command_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


class NodeClientError(Exception):
    """control plane 回了非預期狀態碼。訊息**不含** token。"""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"control plane returned {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class LeasedWork:
    """poll 拿到的一份工作。"""

    attempt_id: str
    job_id: int
    command: str
    command_sha256: str
    lease_expires_at: str
    #: True 表示這是**既有**的 attempt（上一輪已經拿過）——agent 必須據此
    #: 避免重複啟動（INV-NODE-2/5）。
    reused: bool
    #: True 表示 control plane 已經記下一個**已核准**的停止請求。agent 收到
    #: 這個旗標時應該停止（或不要啟動）這份工作，然後回報終態。
    stop_requested: bool = False


class NodeAgentClient:
    """薄用戶端：一個方法對應一個協議端點，不含任何排程/執行邏輯。"""

    def __init__(
        self,
        transport: Callable[..., tuple[int, Any]],
        *,
        node_token: str,
        agent_version: Optional[str] = None,
    ) -> None:
        from agent import __version__

        self._transport = transport
        self._token = node_token
        #: 預設回報套件版本，讓 control plane 的 `nodes.agent_version` 反映
        #: 實際跑的版本（canary 期間要能指認版本）。
        self.agent_version = agent_version or __version__

    def _headers(self) -> dict[str, str]:
        return {
            "X-Node-Token": self._token,
            NODE_PROTOCOL_VERSION_HEADER: NODE_PROTOCOL_VERSION,
        }

    def _post(
        self,
        path: str,
        payload: dict,
        *,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        headers = self._headers()
        headers.update(extra_headers or {})
        status, body = self._transport(
            "POST", path, json=payload, headers=headers
        )
        if status >= 400:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))
            raise NodeClientError(status, detail)
        return body

    def activate(self, activation_nonce: str) -> bool:
        """Promote this pending token before using any ordinary node route.

        Both values remain headers and are never included in a payload,
        response, exception detail, or log. A response-lost retry returns
        ``False`` (duplicate) after the first committed activation.
        """
        if not isinstance(activation_nonce, str) or not activation_nonce:
            raise ValueError("activation nonce is required")
        body = self._post(
            "/node-agent/activate",
            {},
            extra_headers={
                "X-Node-Activation-Nonce": activation_nonce,
            },
        )
        return not bool((body or {}).get("duplicate", False))

    def probe(self) -> dict[str, Any]:
        """Read-only protocol handshake used by ``agent --probe``.

        The response is limited to protocol/capability metadata; it never
        echoes the node token or activation nonce. A server that returns
        another version is not a compatible endpoint, even with HTTP 2xx.
        """

        body = self._post("/node-agent/probe", {})
        if not isinstance(body, dict):
            raise NodeClientError(502, "invalid probe response")
        if body.get("protocol_version") != NODE_PROTOCOL_VERSION:
            raise NodeClientError(426, "unsupported node protocol version")
        if tuple(body.get("capabilities", ())) != NODE_PROTOCOL_CAPABILITIES:
            raise NodeClientError(502, "invalid probe capabilities")
        return dict(body)

    def poll(self, job_id: Optional[int] = None) -> Optional[LeasedWork]:
        """要一份工作。沒工作時回 `None`（不是錯誤）。

        `job_id` 僅為舊版呼叫端的相容參數，絕不送到 control plane。
        重複呼叫是**冪等**的：同一個 attempt 會再回一次，`reused=True`。
        """
        body = self._post(
            "/node-agent/poll",
            {"agent_version": self.agent_version},
        )
        attempt = (body or {}).get("attempt")
        if not attempt:
            return None
        return LeasedWork(
            attempt_id=attempt["id"],
            job_id=attempt["job_id"],
            command=attempt["command"],
            command_sha256=attempt["command_sha256"],
            lease_expires_at=attempt["lease_expires_at"],
            reused=bool((body or {}).get("reused", False)),
            stop_requested=bool((body or {}).get("stop_requested", False)),
        )

    def current_attempt(self) -> Optional[dict[str, Any]]:
        """Recover the attempt the control plane still associates with node."""
        body = self._post("/node-agent/current-attempt", {})
        attempt = (body or {}).get("attempt")
        return dict(attempt) if isinstance(attempt, dict) else None

    def acknowledge(self, work: LeasedWork) -> bool:
        """acknowledge 一份工作。回傳 True 表示「**這一次**才是第一次 ack，
        可以啟動」；False 表示先前已經 ack 過（duplicate），**不得**再啟動
        （INV-NODE-2/5）。

        送出前先在本地驗一次 digest（INV-NODE-3 fail-closed）：control
        plane 給的指令內容與它自己宣告的 digest 不符就直接拒絕執行。
        """
        if command_digest(work.command) != work.command_sha256:
            raise NodeClientError(0, "command digest mismatch (local verification)")
        body = self._post(
            "/node-agent/ack",
            {"attempt_id": work.attempt_id, "command_sha256": work.command_sha256},
        )
        return not bool((body or {}).get("duplicate", False))

    def heartbeat(self, attempt_id: Optional[str] = None) -> bool:
        """回報還活著（INV-NODE-4：這永遠不會改變任務狀態）。

        回傳 True 表示 control plane 有一個**已核准**的停止請求要這個
        attempt 停下來——心跳是 stop-request 的第二條送達路徑，長時間執行
        的任務不會在兩次 poll 之間錯過它。
        """
        body = self._post(
            "/node-agent/heartbeat",
            {"attempt_id": attempt_id, "agent_version": self.agent_version},
        )
        return bool((body or {}).get("stop_requested", False))

    def report_artifacts(self, attempt_id: str, artifacts: list) -> int:
        """回報產出檔案的**中繼資料**（路徑/大小/SHA-256）。

        `artifacts` 是 `{"path", "size_bytes", "sha256"}` 的 list。
        **不上傳檔案內容**——這個呼叫只是告訴 control plane「工作機上有
        這些東西」，不代表結果已經被回收。回傳實際記下的筆數。

        重送是冪等的（同一個 (attempt, path) 只會更新，不會長出重複列）。
        """
        body = self._post(
            "/node-agent/artifacts",
            {"attempt_id": attempt_id, "artifacts": artifacts},
        )
        return int((body or {}).get("recorded", 0))

    def acknowledge_stop(self, attempt_id: str) -> bool:
        """回執：確認收到停止請求。純送達回執，不改變任務狀態——真正的
        收斂還是要 agent 停完之後回報終態。"""
        body = self._post("/node-agent/stop-ack", {"attempt_id": attempt_id})
        return bool((body or {}).get("acked", False))

    def report_terminal(
        self, attempt_id: str, *, exit_code: int, log_tail: str = ""
    ) -> bool:
        """回報終態。回傳 True 表示這是第一次被記錄；False 表示重送
        （冪等成功——agent 的重試機制需要這個語意）。"""
        body = self._post(
            "/node-agent/terminal",
            {
                "attempt_id": attempt_id,
                "exit_code": exit_code,
                "log_tail": log_tail,
            },
        )
        return not bool((body or {}).get("duplicate", False))
