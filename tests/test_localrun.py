"""`app/localrun.py`：sync 任務在 Server A 本地執行的介面。跟 `app/sshpool.py`
不同，這裡不牽涉網路／SSH，是純粹的本機 subprocess，可以直接測試真實行為
（不需要 FakeSSH），驗證它和 sshpool 的介面形狀（`.stdout`／逾時語意）一致。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.localrun import LocalRunError, local_run, local_write_file


def test_local_run_returns_stdout_and_exit_status():
    result = asyncio.run(local_run("echo hello", timeout=5))
    assert result.stdout.strip() == "hello"
    assert result.exit_status == 0


def test_local_run_supports_shell_syntax_like_ssh_run():
    """跟 asyncssh 的 `conn.run(command)`（整串指令丟給遠端 shell 執行）語意
    一致：`&&`、管線、重導向都要能用，因為 dispatch 用到的
    mkdir/tmux/cat/tail 指令都仰賴這個語法。"""
    result = asyncio.run(local_run("mkdir -p /tmp/nonexistent_check_dir && echo ok", timeout=5))
    assert "ok" in result.stdout


def test_local_run_nonzero_exit_status_not_an_exception():
    result = asyncio.run(local_run("exit 7", timeout=5))
    assert result.exit_status == 7


def test_local_run_uses_cwd():
    async def run():
        return await local_run("pwd", timeout=5, cwd="/tmp")

    result = asyncio.run(run())
    assert result.stdout.strip() == "/tmp"


def test_local_run_timeout_raises_local_run_error():
    with pytest.raises(LocalRunError):
        asyncio.run(local_run("sleep 5", timeout=0.1))


def test_local_run_timeout_actually_kills_subprocess_no_orphan(tmp_path):
    """Fable 覆核修正 4：逾時前只取消了 `communicate()`，子行程（這裡是
    `bash -c "sleep 0.3 && touch marker"`）沒有真的被殺掉的話，bash 會在
    背景繼續跑完 `&&` 之後的 `touch`，marker 檔案會冒出來——即使
    `local_run()` 已經回報逾時錯誤。修正後 `proc.kill()` 真的殺掉 bash，
    bash 死了就不會再執行 `&& touch`，marker 永遠不會出現。"""
    marker = tmp_path / "marker.txt"
    with pytest.raises(LocalRunError):
        asyncio.run(local_run(f"sleep 0.3 && touch {marker}", timeout=0.05))

    # 等過原本 sleep 0.3 秒 + 一點餘裕，讓「沒被殺掉」的情況有機會把 marker
    # 寫出來，藉此跟「有殺掉」的情況區分開。
    time.sleep(0.6)
    assert not marker.exists()


def test_local_write_file_creates_parent_dirs_and_writes_content(tmp_path):
    asyncio.run(local_write_file("agent_jobs/123/cmd.sh", "echo hi\n", cwd=str(tmp_path)))
    written = tmp_path / "agent_jobs" / "123" / "cmd.sh"
    assert written.exists()
    assert written.read_text() == "echo hi\n"


# ---------------------------------------------------------------------------
# 整合測試：哨兵檔案協議在本地真的用 tmux 跑一次（sync 任務走的就是這條路，
# 只是目的地是 "_local" 而不是遠端機）。這裡不 mock 任何東西，直接驗證
# app.jobqueue 的 dispatch/reconcile 邏輯搭配 app.localrun 真的能動。
# ---------------------------------------------------------------------------


def test_local_dispatch_and_reconcile_end_to_end_with_real_tmux(tmp_path):
    from app.jobqueue import (
        build_dispatch_paths,
        build_launch_command,
        build_mkdir_command,
        build_run_sh_content,
        reconcile_job,
    )

    job_id = 999
    cwd = str(tmp_path)

    async def ssh_run(server_name, command, timeout):
        return await local_run(command, timeout, cwd=cwd)

    async def ssh_write_file(server_name, path, content):
        return await local_write_file(path, content, cwd=cwd)

    async def dispatch_and_wait():
        paths = build_dispatch_paths(job_id)
        await ssh_run("_local", build_mkdir_command(job_id), 15)
        await ssh_write_file("_local", paths["cmd_sh"], "echo local sync ok\n")
        await ssh_write_file("_local", paths["run_sh"], build_run_sh_content(job_id))
        await ssh_run("_local", build_launch_command(job_id), 15)

        # 輪詢等哨兵檔案出現（真的 tmux + bash 執行需要一點時間）
        for _ in range(50):
            outcome = await reconcile_job(ssh_run, "_local", job_id)
            if outcome.status in ("done", "failed"):
                return outcome
            await asyncio.sleep(0.1)
        return outcome

    outcome = asyncio.run(dispatch_and_wait())
    assert outcome.status == "done"
    assert outcome.exit_code == 0
    assert "local sync ok" in (outcome.log_tail or "")

    # 哨兵檔案確實寫在本地相對路徑（不帶 ~/），驗證 cwd 解析正確
    assert (tmp_path / "agent_jobs" / str(job_id) / "exit_code").exists()
