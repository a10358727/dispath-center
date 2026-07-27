"""本地執行介面：sync 任務在 Server A 本地跑（rsync 是從 A 推出去的，不需要
SSH 到工作機才能執行 rsync 本身；SSH 只在 rsync 內部連到目標機）。

介面刻意跟 `app/sshpool.py` 的 `run()` / `write_file()` 同形狀：
    local_run(command, timeout, cwd) -> CommandResult（有 `.stdout`）
    local_write_file(path, content, cwd) -> None
呼叫端（`app/jobqueue.py` 的 `dispatch_job`/`reconcile_job`）不需要知道背後
是 SSH 還是本地 subprocess，只要拿到同樣形狀的物件即可。哨兵檔案協議完全
沿用：本地也是 `mkdir -p agent_jobs/{id}` + 寫 `cmd.sh`/`run.sh` + `tmux
new-session -d` + 查 `exit_code`/`tmux has-session`，只是全部發生在 Server A
自己身上，不透過 asyncssh。

用 `asyncio.create_subprocess_exec("/bin/bash", "-c", command, ...)`
（`create_subprocess_exec`，不是 `create_subprocess_shell`）呼叫 bash 執行
指令字串——這樣仍然能支援 `mkdir -p ... && tmux ...` 這種 shell 語法
（`&&`、管線、重導向等），跟 asyncssh 的 `conn.run(command)`（透過遠端預設
shell 執行整串指令）語意一致。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from app.sshpool import CommandResult

logger = logging.getLogger(__name__)


class LocalRunError(RuntimeError):
    """本地指令執行失敗（逾時、bash 找不到等）。呼叫端應比照
    `SSHUnreachableError` 處理（reconcile_job 會轉成 `unreachable`，本輪跳過，
    不對任務做任何狀態變更）。"""


async def local_run(command: str, timeout: Optional[float] = None, cwd: Optional[str] = None) -> CommandResult:
    """在本機（Server A）用 bash 執行一段指令，回傳形狀跟 `sshpool.run()` 一致
    的 `CommandResult`。`cwd` 是「本地版的 home 目錄」（見
    `app.config.AppConfig.local_home_dir`），讓 `agent_jobs/{id}/...` 這類
    相對路徑（不帶 `~/`，沿用既有慣例）在本地也能正確解析。

    逾時時（Fable 覆核修正 4）：`asyncio.wait_for` 逾時只會取消
    `proc.communicate()` 這個 coroutine，子行程本身**不會**被殺掉——
    `bash -c command` 這個行程（以及它可能衍生出的孫行程，例如
    `tmux new-session` 之後的整個 session）會變成孤兒繼續跑，形成資源
    洩漏。逾時發生時明確 `proc.kill()`（包 try/except：行程可能剛好在
    這個瞬間自然結束，`kill()` 對已結束的行程會丟 `ProcessLookupError`，
    不影響我們還是要回報逾時錯誤這件事）。
    """
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        if proc is not None:
            try:
                proc.kill()
                # ``wait()`` only waits for the process return code; cancelled
                # ``communicate()`` pipe transports can otherwise survive until
                # GC and try to notify an event loop which has already closed.
                # Drain stdout/stderr after kill so the transport lifecycle also
                # finishes inside this caller's event loop.
                await proc.communicate()
            except ProcessLookupError:
                pass
        raise LocalRunError(f"本地指令執行失敗: {exc}") from exc
    except OSError as exc:
        raise LocalRunError(f"本地指令執行失敗: {exc}") from exc
    return CommandResult(
        exit_status=proc.returncode,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
    )


async def local_write_file(path: str, content: str, cwd: Optional[str] = None) -> None:
    """本地版的 `sshpool.write_file()`：直接寫入本地檔案系統，不用 SFTP。"""
    base = Path(cwd) if cwd else Path.cwd()
    full_path = base / path

    def _write() -> None:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        raise LocalRunError(f"本地寫檔失敗 {full_path}: {exc}") from exc
