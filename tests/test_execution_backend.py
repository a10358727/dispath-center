"""Golden tests for Goal 3 C1: the ExecutionBackend seam must reproduce the
current SSH call surface byte-for-byte. Every assertion here compares
`SSHExecutionBackend` output against the exact same pure builders / functions
the pre-existing call sites (`app.scheduler.dispatch_job`,
`app.jobqueue.reconcile_job`, the `approvals.py` stop branch, and
`app.results.pull_job_results`) already use — proving zero behavior change,
per INV-SSH-1's C1 requirement.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import ServerConfig
from app.db import Job
from app.execution_backend import SSHExecutionBackend
from app.jobqueue import (
    build_dispatch_paths,
    build_launch_command,
    build_mkdir_command,
    build_run_sh_content,
    reconcile_job,
)
from app.results import pull_job_results
from app.scheduler import dispatch_job


class FakeCommandResult:
    def __init__(self, stdout: str = "", exit_status: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.exit_status = exit_status
        self.stderr = stderr


class RecordingSSH:
    """Records every (server_name, command, timeout) call; responds per
    substring match, same shape as tests/test_scheduler.py's FakeSSH."""

    def __init__(self, responses: dict[str, str] | None = None, unreachable: bool = False):
        self.responses = responses or {}
        self.unreachable = unreachable
        self.calls: list[tuple[str, str, float]] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append((server_name, command, timeout))
        if self.unreachable:
            raise ConnectionError("simulated unreachable")
        for key, value in self.responses.items():
            if key in command:
                return FakeCommandResult(value)
        return FakeCommandResult("")


class RecordingWriteFile:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, server_name, path, content):
        self.calls.append((server_name, path, content))


class RecordingLocalRun:
    def __init__(self, result: FakeCommandResult | None = None, exc: Exception | None = None):
        self.result = result or FakeCommandResult(exit_status=0)
        self.exc = exc
        self.calls: list[tuple[str, float]] = []

    async def __call__(self, command, timeout):
        self.calls.append((command, timeout))
        if self.exc is not None:
            raise self.exc
        return self.result


def _job(job_id: int = 42, command: str = "python train.py") -> Job:
    return Job(
        id=job_id,
        type="train",
        project=None,
        command=command,
        require_tag=None,
        pin_server=None,
    )


# --- prepare()/launch() vs. the exact builders dispatch_job used to inline ---


def test_prepare_issues_same_mkdir_and_writes_as_dispatch_job_did():
    ssh = RecordingSSH()
    write = RecordingWriteFile()
    backend = SSHExecutionBackend(ssh_run=ssh, ssh_write_file=write)
    job = _job()

    asyncio.run(backend.prepare("server-a", job))

    paths = build_dispatch_paths(job.id)
    assert ssh.calls == [("server-a", build_mkdir_command(job.id), 15)]
    assert write.calls == [
        ("server-a", paths["cmd_sh"], job.command),
        ("server-a", paths["run_sh"], build_run_sh_content(job.id)),
    ]


def test_launch_issues_same_tmux_command_as_dispatch_job_did():
    ssh = RecordingSSH()
    backend = SSHExecutionBackend(ssh_run=ssh, ssh_write_file=RecordingWriteFile())
    job = _job()

    asyncio.run(backend.launch("server-a", job))

    assert ssh.calls == [("server-a", build_launch_command(job.id), 15)]


def test_dispatch_job_via_scheduler_still_produces_identical_call_sequence():
    """dispatch_job() now routes through the backend internally; its
    observable ssh_run/ssh_write_file call sequence must be unchanged."""
    ssh = RecordingSSH()
    write = RecordingWriteFile()
    job = _job()

    asyncio.run(dispatch_job(ssh, write, "server-a", job))

    paths = build_dispatch_paths(job.id)
    assert ssh.calls == [
        ("server-a", build_mkdir_command(job.id), 15),
        ("server-a", build_launch_command(job.id), 15),
    ]
    assert write.calls == [
        ("server-a", paths["cmd_sh"], job.command),
        ("server-a", paths["run_sh"], build_run_sh_content(job.id)),
    ]


# --- inspect() vs. reconcile_job() directly, across every sentinel branch ---


@pytest.mark.parametrize(
    "responses,unreachable",
    [
        ({"exit_code": "0\n", "tail -n 40": "ok\n"}, False),
        ({"exit_code": "1\n", "tail -n 40": "boom\n"}, False),
        ({"exit_code": "", "tmux has-session": "EXISTS\n"}, False),
        ({"exit_code": "", "tmux has-session": "GONE\n"}, False),
        ({}, True),
    ],
)
def test_inspect_matches_reconcile_job_directly(responses, unreachable):
    ssh_for_backend = RecordingSSH(responses, unreachable=unreachable)
    ssh_for_direct = RecordingSSH(responses, unreachable=unreachable)
    backend = SSHExecutionBackend(ssh_run=ssh_for_backend, ssh_write_file=RecordingWriteFile())

    backend_outcome = asyncio.run(backend.inspect("server-a", 42))
    direct_outcome = asyncio.run(reconcile_job(ssh_for_direct, "server-a", 42))

    assert backend_outcome == direct_outcome
    assert ssh_for_backend.calls == ssh_for_direct.calls


# --- stop() matches the literal tmux kill-session string used in approvals.py ---


def test_stop_issues_exact_tmux_kill_session_command():
    ssh = RecordingSSH()
    backend = SSHExecutionBackend(ssh_run=ssh, ssh_write_file=RecordingWriteFile())

    asyncio.run(backend.stop("server-a", 42))

    # Literal string kept in sync with app/approvals.py's stop-approval branch
    # (INV-SSH-9: this is the only place a running job may be killed).
    assert ssh.calls == [("server-a", "tmux kill-session -t job_42", 15)]


# --- collect() vs. pull_job_results() directly, success and failure paths ---


def _server_cfg() -> ServerConfig:
    return ServerConfig(
        name="server-a", host="127.0.0.1", user="formosa", key="/tmp/key", port=22
    )


def test_collect_matches_pull_job_results_directly_on_success():
    local_backend = RecordingLocalRun(FakeCommandResult(exit_status=0))
    local_direct = RecordingLocalRun(FakeCommandResult(exit_status=0))
    backend = SSHExecutionBackend(
        ssh_run=RecordingSSH(), ssh_write_file=RecordingWriteFile(), local_run=local_backend
    )
    server = _server_cfg()

    backend_result = asyncio.run(backend.collect(42, server, "/home/formosa", 30.0))
    direct_result = asyncio.run(pull_job_results(local_direct, 42, server, "/home/formosa", 30.0))

    assert backend_result == direct_result
    assert local_backend.calls == local_direct.calls


def test_collect_matches_pull_job_results_directly_on_rsync_failure():
    local_backend = RecordingLocalRun(FakeCommandResult(exit_status=23, stderr="rsync: no such dir"))
    local_direct = RecordingLocalRun(FakeCommandResult(exit_status=23, stderr="rsync: no such dir"))
    backend = SSHExecutionBackend(
        ssh_run=RecordingSSH(), ssh_write_file=RecordingWriteFile(), local_run=local_backend
    )
    server = _server_cfg()

    backend_result = asyncio.run(backend.collect(42, server, "/home/formosa", 30.0))
    direct_result = asyncio.run(pull_job_results(local_direct, 42, server, "/home/formosa", 30.0))

    assert backend_result == direct_result
    assert backend_result.ok is False


def test_collect_without_local_run_configured_raises_instead_of_silently_no_op():
    backend = SSHExecutionBackend(ssh_run=RecordingSSH(), ssh_write_file=RecordingWriteFile())
    with pytest.raises(RuntimeError):
        asyncio.run(backend.collect(42, _server_cfg(), "/home/formosa", 30.0))


# --- cleanup() is a true no-op today: no SSH/local side effect at all ---


def test_cleanup_is_a_documented_no_op_with_no_side_effects():
    ssh = RecordingSSH()
    write = RecordingWriteFile()
    local = RecordingLocalRun()
    backend = SSHExecutionBackend(ssh_run=ssh, ssh_write_file=write, local_run=local)

    asyncio.run(backend.cleanup("server-a", 42))

    assert ssh.calls == []
    assert write.calls == []
    assert local.calls == []
