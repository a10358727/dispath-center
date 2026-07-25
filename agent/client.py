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
        agent_version: str = "node-agent-v1",
    ) -> None:
        self._transport = transport
        self._token = node_token
        self.agent_version = agent_version

    def _headers(self) -> dict:
        return {"X-Node-Token": self._token}

    def _post(self, path: str, payload: dict) -> Any:
        status, body = self._transport(
            "POST", path, json=payload, headers=self._headers()
        )
        if status >= 400:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))
            raise NodeClientError(status, detail)
        return body

    def poll(self, job_id: int) -> Optional[LeasedWork]:
        """要一份工作。沒工作時回 `None`（不是錯誤）。

        重複呼叫是**冪等**的：同一個 attempt 會再回一次，`reused=True`。
        """
        body = self._post(
            "/node-agent/poll",
            {"job_id": job_id, "agent_version": self.agent_version},
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
