import asyncio

from app.config import ServerConfig
from app.results import build_result_pull_command, local_result_dir, pull_job_results
from app.sshpool import CommandResult


def make_server(name="server-a") -> ServerConfig:
    return ServerConfig(name=name, host="10.0.0.5", user="train", key="~/.ssh/id_rsa")


def test_local_result_dir_uses_local_home_dir():
    assert local_result_dir(42, "/srv/dispatch") == "/srv/dispatch/results/42/"
    assert local_result_dir(42, ".") == "./results/42/"


def test_build_result_pull_command_contains_expected_pieces():
    server = make_server()
    cmd = build_result_pull_command(7, server, "/srv/dispatch")
    assert "mkdir -p" in cmd
    assert "rsync -a -e" in cmd
    assert "train@10.0.0.5:results/7/" in cmd
    assert "/srv/dispatch/results/7/" in cmd
    assert "-p 22" not in cmd


def test_build_result_pull_command_non_default_port_appends_dash_p():
    server = ServerConfig(
        name="pro6000", host="10.0.0.9", user="train", key="~/.ssh/id_rsa", port=32221
    )
    cmd = build_result_pull_command(7, server, "/srv/dispatch")
    assert "-p 32221" in cmd


class _FakeLocalRun:
    def __init__(self, exit_status=0, stdout="", stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[str] = []

    async def __call__(self, command, timeout):
        self.calls.append(command)
        return CommandResult(exit_status=self.exit_status, stdout=self.stdout, stderr=self.stderr)


def test_pull_job_results_success():
    server = make_server()
    local_run = _FakeLocalRun(exit_status=0)
    result = asyncio.run(pull_job_results(local_run, 3, server, "/srv/dispatch", 600))
    assert result.ok is True
    assert result.path == "/srv/dispatch/results/3/"
    assert result.error is None
    assert len(local_run.calls) == 1


def test_pull_job_results_missing_remote_dir_fails_gracefully():
    """工作機沒有 results/{id}/（任務本來就不產出結果）：rsync 非 0 結束碼，
    但這裡只回報 ok=False，不丟例外——呼叫端才能照常繼續寄信。"""
    server = make_server()
    local_run = _FakeLocalRun(exit_status=23, stderr="rsync: link_stat ... No such file or directory\n")
    result = asyncio.run(pull_job_results(local_run, 3, server, "/srv/dispatch", 600))
    assert result.ok is False
    assert result.path is None
    assert "No such file" in result.error


def test_pull_job_results_local_run_exception_is_caught():
    async def raising_local_run(command, timeout):
        raise TimeoutError("simulated rsync timeout")

    server = make_server()
    result = asyncio.run(pull_job_results(raising_local_run, 3, server, "/srv/dispatch", 600))
    assert result.ok is False
    assert "simulated rsync timeout" in result.error
