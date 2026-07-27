"""Integration coverage for the D3 retry/discard approval-kind wiring.

Exercises the real request/approve boundary end to end (fake Hub/SSH, real
DB, real approve() dispatch) — complements
``tests/test_engineering_task_retry_discard.py``, which stays at the
DB/jobqueue boundary.  Fixture shape mirrors
``tests/test_engineering_path_policy_integration.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.approvals import (
    approve,
    request_engineering_task_approval,
    request_engineering_task_discard_approval,
    request_engineering_task_retry_approval,
)
from app.config import AppConfig, ServerConfig
from app.datasets import LOCAL_SERVER
from app.db import Database
from app.engineering_tasks import InvalidEngineeringTaskRequestError


FAKE_COMMIT = "a" * 40


@dataclass
class _CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0


class _HubInspection:
    def __init__(self, commit: str):
        self.commit = commit
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, command: str, timeout: int):
        self.calls.append((command, timeout))
        if "rev-parse --verify" in command:
            return _CommandResult(stdout=f"{self.commit}\n")
        if "ls-tree -r --name-only" in command:
            return _CommandResult(stdout="allowed.txt\nseed.txt\npyproject.toml\n")
        return _CommandResult()


class _RunnerProbe:
    async def __call__(self, server: str, command: str, timeout: int):
        if "command -v codex" in command:
            return _CommandResult(stdout="codex-cli 0.144.3\nAUTH_OK\n")
        return _CommandResult()


class _Writes:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, server: str, path: str, content: str):
        self.calls.append((server, path, content))


def _server() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="~/.ssh/id_rsa",
        port=32221,
        enabled=True,
    )


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        servers=[_server()],
        local_home_dir=str(tmp_path),
        codex_runner_server="server-a",
        codex_workspace_root="~/codex_workspaces",
        engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
    )


def _structured_request(**overrides) -> dict:
    request = {
        "objective": "Produce a retryable attempt",
        "allowed_paths": ["app/"],
        "prohibited_paths": [],
        "validation": {
            "tests_lint": False,
            "build_smoke": False,
            "continue_fixing_failures": False,
            "worker_validation_target": None,
        },
        "permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
            "environment_references": [],
            "secret_references": [],
        },
    }
    request.update(overrides)
    return request


def _insert_fake_project(db: Database, tmp_path: Path):
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", FAKE_COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    return version


def _approve(db: Database, tmp_path: Path, approval_id: int, local: _HubInspection):
    writes = _Writes()
    runner = _RunnerProbe()
    result = asyncio.run(
        approve(
            db,
            approval_id,
            ssh_run=runner,
            server_configs={"server-a": _server()},
            app_state=SimpleNamespace(config=_config(tmp_path), ssh_write_file=writes),
            local_run=local,
        )
    )
    return result, writes


def _create_terminal_attempt_one(db: Database, tmp_path: Path) -> str:
    """Request + approve attempt 1, then push it into a terminal state."""

    version = _insert_fake_project(db, tmp_path)
    local = _HubInspection(FAKE_COMMIT)
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=local,
        )
    )
    result, _writes = _approve(db, tmp_path, approval.id, local)
    db.update_job(result["staging_job_id"], status="done", server=LOCAL_SERVER, exit_code=0)
    db.update_job(result["job"].id, status="done", server="server-a", exit_code=1)
    db.update_coding_run(result["coding_run_id"], status="failed")
    return task.id


# ---------------------------------------------------------------------------
# request_engineering_task_retry_approval()
# ---------------------------------------------------------------------------


def test_retry_request_rejects_non_terminal_task(db, tmp_path):
    version = _insert_fake_project(db, tmp_path)
    local = _HubInspection(FAKE_COMMIT)
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=local,
        )
    )
    _approve(db, tmp_path, approval.id, local)  # leaves attempt 1 "queued"
    with pytest.raises(InvalidEngineeringTaskRequestError, match="不是終態"):
        request_engineering_task_retry_approval(db, task.id)


def test_retry_request_creates_pending_approval_with_next_attempt(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    approval = request_engineering_task_retry_approval(db, task_id)
    assert approval.kind == "engineering_task_retry"
    assert approval.status == "pending"
    assert approval.payload["engineering_task_id"] == task_id
    assert approval.payload["attempt_number"] == 2


def test_retry_request_rejects_unknown_task(db):
    with pytest.raises(InvalidEngineeringTaskRequestError, match="不存在"):
        request_engineering_task_retry_approval(db, "00000000-0000-4000-8000-000000000000")


# ---------------------------------------------------------------------------
# approve() — kind=engineering_task_retry
# ---------------------------------------------------------------------------


def test_retry_approve_creates_attempt_two_job(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    retry_approval = request_engineering_task_retry_approval(db, task_id)
    local = _HubInspection(FAKE_COMMIT)
    result, writes = _approve(db, tmp_path, retry_approval.id, local)

    assert result["engineering_task_id"] == task_id
    coding_job = db.get_job(result["job"].id)
    staging_job = db.get_job(result["staging_job_id"])
    assert coding_job.engineering_attempt_number == 2
    assert staging_job.engineering_attempt_number == 2

    task = db.get_engineering_task(task_id)
    assert task.status == "queued"
    assert task.coding_run_id == result["coding_run_id"]

    # Instruction/policy staged again for the new attempt (task_id-keyed paths
    # are shared across attempts; content is byte-identical since the
    # underlying task contract is immutable).
    assert any(call[0] == LOCAL_SERVER for call in writes.calls)

    approval = db.get_approval(retry_approval.id)
    assert approval.status == "approved"


def test_retry_approve_rejects_stale_attempt_number(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    retry_approval = request_engineering_task_retry_approval(db, task_id)
    # Tamper the pending approval to claim a later attempt than the task has
    # actually reached (structurally valid — >= 2 — but stale/wrong), as if
    # a different retry had already been approved and skipped ahead.
    tampered_payload = dict(retry_approval.payload)
    tampered_payload["attempt_number"] = 3
    db.update_approval(retry_approval.id, payload=tampered_payload)

    local = _HubInspection(FAKE_COMMIT)
    result, _writes = _approve(db, tmp_path, retry_approval.id, local)
    assert result["approval"].status == "rejected"
    assert "attempt_number" in result["approval"].note


def test_retry_approve_rejects_parent_approval_payload_drift(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    retry_approval = request_engineering_task_retry_approval(db, task_id)
    tampered_payload = dict(retry_approval.payload)
    tampered_payload["parent_approval_payload_sha256"] = "0" * 64
    db.update_approval(retry_approval.id, payload=tampered_payload)

    local = _HubInspection(FAKE_COMMIT)
    result, _writes = _approve(db, tmp_path, retry_approval.id, local)
    assert result["approval"].status == "rejected"
    assert "parent coding_task approval" in result["approval"].note


def test_retry_approve_rejects_when_task_has_active_job(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    retry_approval = request_engineering_task_retry_approval(db, task_id)
    # A separate ordinary Job appears active for the task between request and
    # approval (e.g. a worker-validation Job); approve-time revalidation must
    # catch this even though request-time eligibility already passed.
    db.update_job(
        db.list_engineering_task_jobs(task_id)[0].id, status="running"
    )

    local = _HubInspection(FAKE_COMMIT)
    result, _writes = _approve(db, tmp_path, retry_approval.id, local)
    assert result["approval"].status == "rejected"
    assert "queued/running" in result["approval"].note


def test_retry_approve_rejects_unknown_approval_kind_payload(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    bogus_id = db.insert_approval(
        "engineering_task_retry", {"engineering_task_id": task_id}
    )
    local = _HubInspection(FAKE_COMMIT)
    result, _writes = _approve(db, tmp_path, bogus_id, local)
    assert result["approval"].status == "rejected"
    assert "缺少必要欄位" in result["approval"].note


# ---------------------------------------------------------------------------
# request_engineering_task_discard_approval() + approve()
# ---------------------------------------------------------------------------


def test_discard_request_rejects_non_terminal_task(db, tmp_path):
    version = _insert_fake_project(db, tmp_path)
    local = _HubInspection(FAKE_COMMIT)
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=local,
        )
    )
    _approve(db, tmp_path, approval.id, local)
    with pytest.raises(InvalidEngineeringTaskRequestError, match="不是終態"):
        request_engineering_task_discard_approval(db, task.id)


def test_discard_approve_marks_task_discarded(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    discard_approval = request_engineering_task_discard_approval(db, task_id)
    local = _HubInspection(FAKE_COMMIT)
    result, _writes = _approve(db, tmp_path, discard_approval.id, local)

    assert result["engineering_task_id"] == task_id
    task = db.get_engineering_task(task_id)
    assert task.status == "discarded"
    approval = db.get_approval(discard_approval.id)
    assert approval.status == "approved"


def test_discard_approve_rejects_when_task_has_active_job(db, tmp_path):
    task_id = _create_terminal_attempt_one(db, tmp_path)
    discard_approval = request_engineering_task_discard_approval(db, task_id)
    db.update_job(
        db.list_engineering_task_jobs(task_id)[0].id, status="running"
    )

    local = _HubInspection(FAKE_COMMIT)
    result, _writes = _approve(db, tmp_path, discard_approval.id, local)
    assert result["approval"].status == "rejected"
    assert "queued/running" in result["approval"].note


def test_second_retry_after_discard_is_impossible_once_discarded(db, tmp_path):
    """A discarded task's status is no longer in the terminal-run set that
    retry eligibility checks — discard is a one-way door for this slice."""

    task_id = _create_terminal_attempt_one(db, tmp_path)
    discard_approval = request_engineering_task_discard_approval(db, task_id)
    local = _HubInspection(FAKE_COMMIT)
    _approve(db, tmp_path, discard_approval.id, local)

    with pytest.raises(InvalidEngineeringTaskRequestError, match="不是終態"):
        request_engineering_task_retry_approval(db, task_id)
