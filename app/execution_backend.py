"""ExecutionBackend seam（Goal 3 C1，DG-C 2026-07-19 核准後開工）。

背景：INV-SSH-1（2026-07-19 DG-C 修訂）允許工作機上未來另外存在
`INV-NODE-*` 管理的 Node Agent，但明文要求 SSH 後端的行為與依賴永遠不得
假設它存在。這支模組把現行「任務生命週期」的遠端呼叫面
（`app.scheduler.dispatch_job`、`app.jobqueue.reconcile_job`、
`app.approvals` 的 stop 分支、`app.results.pull_job_results`）收攏成一組
明確的 prepare/launch/inspect/stop/collect/cleanup 合約，讓將來的 Node
Agent 後端可以實作同一個 Protocol 插進來，而不必改動呼叫端。

這一輪（C1）只做 SSH 實作＋合約本身，**零行為變更**：`SSHExecutionBackend`
的每個方法都直接呼叫既有的純函式（`build_mkdir_command`、
`build_launch_command`…）與既有的 `reconcile_job`/`pull_job_results`，
不重寫任何指令組裝邏輯，用 golden tests（`tests/test_execution_backend.py`）
逐字比對指令字串證明與現行呼叫路徑等價。

只有 `app.scheduler.dispatch_job()` 這一個真實呼叫點在本輪改成透過
`SSHExecutionBackend` 分派（prepare 再 launch），因為它本來就只接
`ssh_run`/`ssh_write_file` 兩個 callable、外部簽名與行為完全不變。stop
（`app.approvals`）與 collect（`app.jobfinish`）的真實呼叫點本輪不動——
那兩處的稽核/例外處理邏輯跟 SSH 呼叫本身交織在一起，等 C2（真的有第二種
後端要選）再一併切換，避免這輪徒增風險。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from app.config import ServerConfig
from app.db import Job
from app.jobqueue import (
    ReconcileOutcome,
    build_dispatch_paths,
    build_launch_command,
    build_mkdir_command,
    build_run_sh_content,
    reconcile_job,
)
from app.results import PullResult, pull_job_results


class ExecutionBackend(Protocol):
    """任務生命週期的六個動作。實作者（SSH 或未來的 Node Agent）對每台
    工作機負責回答這六個問題，呼叫端不假設底層是 SSH 還是別的通道。"""

    async def prepare(self, server_name: str, job: Job) -> None:
        """建立遠端工作目錄、把 `job.command` 落地成 cmd.sh/run.sh。"""
        ...

    async def launch(self, server_name: str, job: Job) -> None:
        """啟動（tmux detached session 或等價物），立即返回不等待完成。"""
        ...

    async def inspect(self, server_name: str, job_id: int) -> ReconcileOutcome:
        """依哨兵協議判定目前狀態（running/done/failed/requeued/unreachable）。"""
        ...

    async def stop(self, server_name: str, job_id: int) -> None:
        """終止一個 running 任務（僅供已核准的 stop 流程呼叫，見 INV-SSH-9）。"""
        ...

    async def collect(
        self, job_id: int, server: ServerConfig, local_home_dir: str, timeout: float
    ) -> PullResult:
        """把 `results/{id}/` 拉回 Server A 本地。"""
        ...

    async def cleanup(self, server_name: str, job_id: int) -> None:
        """任務結束後的遠端清理（例如移除 agent_jobs/{id}/）。"""
        ...


@dataclass
class SSHExecutionBackend:
    """現行 SSH/rsync 後端。每個方法只是既有 build_*/reconcile_job/
    pull_job_results 的薄封裝，指令字串與呼叫順序跟現行路徑逐字相同
    （見 `tests/test_execution_backend.py` 的 golden tests）。"""

    ssh_run: object
    ssh_write_file: object
    local_run: Optional[object] = None

    async def prepare(self, server_name: str, job: Job) -> None:
        paths = build_dispatch_paths(job.id)
        await self.ssh_run(server_name, build_mkdir_command(job.id), 15)
        await self.ssh_write_file(server_name, paths["cmd_sh"], job.command)
        await self.ssh_write_file(
            server_name, paths["run_sh"], build_run_sh_content(job.id)
        )

    async def launch(self, server_name: str, job: Job) -> None:
        await self.ssh_run(server_name, build_launch_command(job.id), 15)

    async def inspect(self, server_name: str, job_id: int) -> ReconcileOutcome:
        return await reconcile_job(self.ssh_run, server_name, job_id)

    async def stop(self, server_name: str, job_id: int) -> None:
        await self.ssh_run(server_name, f"tmux kill-session -t job_{job_id}", 15)

    async def collect(
        self, job_id: int, server: ServerConfig, local_home_dir: str, timeout: float
    ) -> PullResult:
        if self.local_run is None:
            raise RuntimeError(
                "SSHExecutionBackend.collect() requires local_run to be set"
            )
        return await pull_job_results(
            self.local_run, job_id, server, local_home_dir, timeout
        )

    async def cleanup(self, server_name: str, job_id: int) -> None:
        # SSH 後端目前不刪除遠端 agent_jobs/{id}/：哨兵檔案保留供事後排查，
        # 這是現行行為（從沒清過），這裡忠實維持，不新增副作用。
        return None


@dataclass
class NodeExecutionBackend:
    """Goal 3 C3：Node Agent 後端（INV-NODE-*）。

    與 `SSHExecutionBackend` 的**根本差異**：control plane 從不主動連線到
    工作機。agent 出站輪詢領工作（`/node-agent/poll`）、自己 ack、自己
    啟動、自己回報終態。因此這裡的 `prepare()`/`launch()` 是**刻意的
    no-op**——不是還沒實作，而是這個通道的正確語意：把工作標記成「可被
    領取」是 DB 狀態（`node_attempts`），不是遠端動作。

    `inspect()` 讀持久化的 attempt 狀態，**永不**把心跳缺席判成 failed
    （INV-NODE-4）；連不上的 agent 回 `running`（＝現況不明、保持不動），
    與 SSH 後端 unreachable 的既有語意一致。

    本輪（C3 程式碼部分）這個類別**尚未被任何呼叫端使用**——真正的切換
    需要 canary 通過（≥100 jobs／≥2 nodes／7 天），那是操作行為。這裡先
    把合約實作出來並測到，讓 C4 逐台提升時不必再改架構。
    """

    db: object
    #: 心跳 TTL/寬限期，判讀 attempt 是否仍「看起來活著」用。
    heartbeat_ttl_sec: float = 60.0
    heartbeat_grace_sec: float = 60.0

    async def prepare(self, server_name: str, job: Job) -> None:
        """no-op：node 通道不預先在遠端建目錄／寫檔。指令位元組隨 poll 回應
        交給 agent，由 agent 自己非插值落地（INV-NODE-3）。"""
        return None

    async def launch(self, server_name: str, job: Job) -> None:
        """no-op：control plane 不啟動遠端行程。agent ack 之後自行啟動
        （INV-NODE-2：ack 先於任何副作用）。"""
        return None

    async def inspect(self, server_name: str, job_id: int) -> ReconcileOutcome:
        """由持久化的 attempt 狀態判定（INV-NODE-4/5）。

        - 有終態紀錄 → done/failed（agent 回報的 exit_code 為準）。
        - 尚未 ack、lease 已過期 → `requeued`（沒有副作用產生過，可安全重排）。
        - 其餘（含心跳過期、agent 失聯）→ `running`：**狀態未知不等於失敗**，
          保持不動等 agent 回來或人工處理。
        """
        from app.node_protocol import AttemptStatus
        from app.node_registry import to_protocol_attempt

        rows = self.db.list_node_attempts(job_id=job_id)
        if not rows:
            return ReconcileOutcome(status="running")

        attempt = to_protocol_attempt(rows[-1])
        if attempt.status is AttemptStatus.DONE:
            return ReconcileOutcome(
                status="done", exit_code=rows[-1].exit_code, log_tail=rows[-1].log_tail or ""
            )
        if attempt.status is AttemptStatus.FAILED:
            return ReconcileOutcome(
                status="failed",
                exit_code=rows[-1].exit_code,
                log_tail=rows[-1].log_tail or "",
            )
        if attempt.status is AttemptStatus.EXPIRED:
            return ReconcileOutcome(status="requeued")
        return ReconcileOutcome(status="running")

    async def stop(self, server_name: str, job_id: int) -> None:
        """停止是**請求**而不是命令：control plane 不能直接殺工作機上的
        行程（沒有入站通道）。C4 會把它落成 agent 下次輪詢時取回的
        stop-request；本輪合約先留著，不假裝已經停掉。"""
        raise NotImplementedError(
            "node backend stop-request lands with C4 per-node promotion"
        )

    async def collect(
        self, job_id: int, server: ServerConfig, local_home_dir: str, timeout: float
    ) -> PullResult:
        """結果回收在 node 通道由 agent 主動上傳（roadmap Phase 3 的
        artifact-metadata 協議），不是 Server A 發起 rsync。C4 接。"""
        raise NotImplementedError(
            "node backend result upload lands with C4 per-node promotion"
        )

    async def cleanup(self, server_name: str, job_id: int) -> None:
        """no-op：agent 端的 attempt 目錄由 agent 自己管理。"""
        return None
