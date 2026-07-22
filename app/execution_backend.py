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
