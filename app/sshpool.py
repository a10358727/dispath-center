"""asyncssh 封裝：金鑰認證、逾時、每機序列化、全域併發上限。

這一層直接碰真實網路/SSH，因此不寫單元測試（PLAN.md 也明講「測試不得
依賴真實 SSH」）；monitor.py / scheduler.py 對外只依賴這裡提供的
`CommandResult` 形狀與 `run()` 這個 async 介面，測試時用假的
callable/物件替換即可，不需要理解 asyncssh 細節。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import asyncssh

from app.config import AppConfig, ServerConfig

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    exit_status: Optional[int]
    stdout: str
    stderr: str


class SSHUnreachableError(RuntimeError):
    """SSH 連線失敗（連不上/認證失敗/逾時）。呼叫端應視為「本輪跳過」。"""


class SSHPool:
    """管理每台伺服器一條可重用連線；每機序列化指令、全域併發上限。"""

    def __init__(self, config: AppConfig):
        self._config = config
        self._connections: dict[str, asyncssh.SSHClientConnection] = {}
        self._server_locks: dict[str, asyncio.Lock] = {}
        self._global_semaphore = asyncio.Semaphore(config.ssh_max_concurrency)

    def _lock_for(self, server_name: str) -> asyncio.Lock:
        if server_name not in self._server_locks:
            self._server_locks[server_name] = asyncio.Lock()
        return self._server_locks[server_name]

    async def _get_connection(self, server: ServerConfig) -> asyncssh.SSHClientConnection:
        conn = self._connections.get(server.name)
        if conn is not None and not conn.is_closed():
            return conn
        conn = await asyncio.wait_for(
            asyncssh.connect(
                server.host,
                port=getattr(server, "port", 22) or 22,
                username=server.user,
                client_keys=[server.key_path],
                known_hosts=None,
            ),
            timeout=self._config.ssh_connect_timeout,
        )
        self._connections[server.name] = conn
        return conn

    async def run(
        self,
        server: ServerConfig,
        command: str,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        timeout = timeout or self._config.ssh_command_timeout
        async with self._global_semaphore:
            async with self._lock_for(server.name):
                try:
                    conn = await self._get_connection(server)
                    result = await asyncio.wait_for(
                        conn.run(command, check=False), timeout=timeout
                    )
                except (
                    asyncssh.Error,
                    OSError,
                    asyncio.TimeoutError,
                ) as exc:
                    self._connections.pop(server.name, None)
                    raise SSHUnreachableError(
                        f"SSH 到 {server.name} 失敗: {exc}"
                    ) from exc
                return CommandResult(
                    exit_status=result.exit_status,
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                )

    async def write_file(self, server: ServerConfig, remote_path: str, content: str) -> None:
        """用 SFTP 寫檔（避免 shell heredoc 的跳脫問題），寫 cmd.sh 用。"""
        async with self._global_semaphore:
            async with self._lock_for(server.name):
                try:
                    conn = await self._get_connection(server)
                    async with conn.start_sftp_client() as sftp:
                        async with sftp.open(remote_path, "w") as f:
                            await f.write(content)
                except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                    self._connections.pop(server.name, None)
                    raise SSHUnreachableError(
                        f"SFTP 寫入 {server.name}:{remote_path} 失敗: {exc}"
                    ) from exc

    async def close_all(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
