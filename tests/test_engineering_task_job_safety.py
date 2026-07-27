"""Security boundaries for Engineering Task-owned internal Jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest

from app.approvals import approve, request_stop_approval
from app.audit import now_iso, read_audit
from app.config import ServerConfig
from app.datasets import LOCAL_SERVER
from app.jobqueue import (
    DangerousCommandError,
    EngineeringTaskJobCancellationError,
    ReconcileOutcome,
    apply_reconcile_outcome,
    cancel_job,
    enqueue_job,
)
from app.monitor import ServerState
from app.scheduler import scheduler_tick


BASE_COMMIT = "a" * 40
PRIVATE_SENTINEL = "SENSITIVE_SENTINEL"


class _Result:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


def _insert_task(db, *, approved: bool = True) -> tuple[str, int]:
    project = "engineering-audit-project"
    project_id = db.insert_project(
        project, f"https://example.invalid/{project}.git"
    )
    version = db.get_or_create_project_version(
        project, BASE_COMMIT, git_ref="main"
    )
    task_id = str(uuid.uuid4())
    task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract={
            "base_binding": "project_version_pinned",
            "runner": {
                "name": "server-a",
                "host": "192.0.2.1",
                "user": "runner",
                "port": 22,
            },
            "workspace_rel": "codex_workspaces",
        },
        contract_version="engineering-task-v1",
        structured_request={"objective": "Verify safe audit projection"},
        instruction="AI Engineering Task",
        detected_metadata={},
        runner_server="server-a",
        validation_target=None,
        approval_payload={
            "engineering_task_id": task_id,
            "project": project,
            "project_version_id": version.id,
            "base_commit": BASE_COMMIT,
        },
    )
    if approved:
        db.update_approval(
            approval_id,
            status="approved",
            decided_at=now_iso(),
            note="approved by synthetic test",
        )
    return task_id, approval_id


def _enqueue_owner(
    db,
    audit_path,
    task_id: str,
    *,
    role: str = "coding",
    command: str | None = None,
    type: str = "adhoc",
    pin_server: str | None = "server-a",
    depends_on: list[int] | None = None,
):
    job = enqueue_job(
        db,
        command=command
        or f"python -m approved_tool --workdir /home/private/{PRIVATE_SENTINEL}",
        type=type,
        project="engineering-audit-project",
        pin_server=pin_server,
        depends_on=depends_on,
        audit_path=audit_path,
        engineering_task_id=task_id,
        engineering_task_role=role,
        engineering_attempt_number=1,
    )
    task = db.get_engineering_task(task_id)
    command_role = {
        "staging": "staging",
        "coding": "agent_turn",
        "validation": "validation",
    }[role]
    db.register_engineering_task_command(
        task_id=task_id,
        attempt_number=1,
        sequence={"staging": 1, "coding": 2, "validation": 3}[role],
        command_key=f"synthetic-{role}-job-{job.id}",
        job_id=job.id,
        command_role=command_role,
        display_command=f"Synthetic approved {command_role}",
        command_digest=hashlib.sha256(job.command.encode("utf-8")).hexdigest(),
        execution_location="server_a" if role == "staging" else "coding_runner",
        working_directory_label="Synthetic approved workspace",
        policy_family=(
            "immutable_base_staging"
            if role == "staging"
            else "approved_agent_execution"
        ),
        policy_disposition="task_approved",
        approval_id=task.approval_id,
        status_source="job",
        recorded_status=job.status,
    )
    return job


def _audit_record(audit_path, action: str, job_id: int):
    matches = [
        record
        for record in read_audit(audit_path)
        if record["action"] == action
        and record["params"].get("job_id") == job_id
    ]
    assert len(matches) == 1
    return matches[0]


def _assert_execution_contract_mismatch_event(db, task_id: str, job_id: int):
    matches = [
        event
        for event in db.list_engineering_task_events(task_id)
        if event.event_type == "execution_contract_mismatch"
    ]
    assert len(matches) == 1
    event = matches[0]
    assert event.state == "unknown"
    assert event.source_kind == "system"
    assert event.summary == (
        "Approved Engineering Task execution contract mismatch; executor not contacted"
    )
    assert event.details == {"job_id": job_id}
    serialized = json.dumps(
        {"summary": event.summary, "details": event.details}, ensure_ascii=False
    )
    assert PRIVATE_SENTINEL not in serialized
    assert "/home/private" not in serialized
    return event


def test_owner_enqueue_audit_uses_semantic_display_and_digest(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    command = f"python -m approved_tool --workdir /home/private/{PRIVATE_SENTINEL}"
    job = _enqueue_owner(db, audit_path, task_id, command=command)

    record = _audit_record(audit_path, "enqueue", job.id)
    params = record["params"]
    assert "command" not in params
    assert params["command_display"] == "Codex agent turn in isolated worktree"
    assert params["command_digest"] == hashlib.sha256(command.encode()).hexdigest()
    assert params["command_digest_algorithm"] == "sha256"
    assert PRIVATE_SENTINEL not in json.dumps(record)
    assert "/home/private" not in json.dumps(record)
    journal = db.get_engineering_task_command_by_job_id(job.id)
    assert journal is not None
    assert journal.engineering_task_id == task_id
    assert journal.job_id == job.id
    assert journal.command_digest == hashlib.sha256(command.encode()).hexdigest()


def test_legacy_enqueue_audit_retains_raw_command_contract(db, audit_path):
    command = "sleep 60"
    job = enqueue_job(db, command=command, audit_path=audit_path)

    params = _audit_record(audit_path, "enqueue", job.id)["params"]
    assert params["command"] == command
    assert "command_display" not in params
    assert "command_digest" not in params


def test_owner_terminal_log_is_sanitized_before_database_persistence(
    db, audit_path
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(db, audit_path, task_id)
    db.update_job(job.id, status="running", server="server-a")
    running = db.get_job(job.id)
    raw_secret = "synthetic-basic-value"

    apply_reconcile_outcome(
        db,
        running,
        ReconcileOutcome(
            status="done",
            exit_code=0,
            log_tail=(
                f"Authorization: Basic {raw_secret}\n"
                "/home/runner/private/task.log\ncompleted"
            ),
        ),
        audit_path=audit_path,
    )

    persisted = db.get_job(job.id)
    assert persisted.status == "done"
    assert persisted.log_tail is not None
    assert persisted.log_tail.count("[REDACTED]") == 2
    assert raw_secret not in persisted.log_tail
    assert "/home/runner/private" not in persisted.log_tail


def test_owner_private_key_log_is_withheld_before_database_persistence(
    db, audit_path
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(db, audit_path, task_id)
    db.update_job(job.id, status="running", server="server-a")

    apply_reconcile_outcome(
        db,
        db.get_job(job.id),
        ReconcileOutcome(
            status="failed",
            exit_code=1,
            log_tail="-----BEGIN PRIVATE KEY-----\nsynthetic\n",
        ),
        audit_path=audit_path,
    )

    persisted = db.get_job(job.id)
    assert persisted.status == "failed"
    assert persisted.log_tail is None


def test_legacy_terminal_log_retains_existing_raw_persistence(db, audit_path):
    job = enqueue_job(db, command="echo legacy", audit_path=audit_path)
    db.update_job(job.id, status="running", server="server-a")
    raw = "Authorization: Basic legacy-compatible-value"

    apply_reconcile_outcome(
        db,
        db.get_job(job.id),
        ReconcileOutcome(status="done", exit_code=0, log_tail=raw),
        audit_path=audit_path,
    )

    assert db.get_job(job.id).log_tail == raw


def test_legacy_coding_log_is_sanitized_before_database_persistence(
    db, audit_path
):
    job = enqueue_job(
        db,
        command="echo compatibility-coding-wrapper",
        type="coding",
        audit_path=audit_path,
    )
    db.update_job(job.id, status="running", server="server-a")
    raw_secret = "legacy-coding-basic-value"

    apply_reconcile_outcome(
        db,
        db.get_job(job.id),
        ReconcileOutcome(
            status="done",
            exit_code=0,
            log_tail=f"Authorization: Basic {raw_secret}",
        ),
        audit_path=audit_path,
    )

    persisted = db.get_job(job.id)
    assert persisted.log_tail == "Authorization: Basic [REDACTED]"
    assert raw_secret not in persisted.log_tail


def test_owner_dangerous_rejection_does_not_audit_raw_command(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    command = f"rm -rf /tmp/{PRIVATE_SENTINEL}"

    with pytest.raises(DangerousCommandError):
        _enqueue_owner(db, audit_path, task_id, command=command)

    record = [r for r in read_audit(audit_path) if r["action"] == "reject"][-1]
    params = record["params"]
    assert "command" not in params
    assert params["reason"] == "dangerous_command"
    assert params["command_digest"] == hashlib.sha256(command.encode()).hexdigest()
    assert PRIVATE_SENTINEL not in json.dumps(record)


def test_generic_cancel_rejects_queued_owner_job_without_mutation(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(db, audit_path, task_id)

    with pytest.raises(EngineeringTaskJobCancellationError, match="安全取消流程"):
        cancel_job(db, job.id, audit_path=audit_path)

    assert db.get_job(job.id).status == "queued"
    assert not any(
        record["action"] == "cancel" for record in read_audit(audit_path)
    )


@pytest.mark.parametrize(
    ("local", "server_name", "job_type", "role"),
    [
        (False, "server-a", "coding", "coding"),
        (True, "_local", "sync", "staging"),
    ],
)
def test_owner_dispatch_audit_never_contains_raw_command(
    db, audit_path, local, server_name, job_type, role
):
    task_id, _approval_id = _insert_task(db)
    command = f"python -m approved_tool --workdir /home/private/{PRIVATE_SENTINEL}"
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        command=command,
        role=role,
        type=job_type,
        pin_server=server_name,
    )

    async def ssh_run(_server, _command, _timeout):
        return _Result()

    async def write_file(_server, _path, _content):
        return None

    states = (
        {}
        if local
        else {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    )
    configs = {
        "server-a": ServerConfig(
            name="server-a",
            host="192.0.2.1",
            user="runner",
            key="/tmp/synthetic-key",
        )
    }
    asyncio.run(
        scheduler_tick(
            db,
            states,
            configs,
            ssh_run,
            write_file,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    record = _audit_record(audit_path, "dispatch", job.id)
    params = record["params"]
    assert "command" not in params
    assert params["command_digest"] == hashlib.sha256(command.encode()).hexdigest()
    assert params["command_digest_algorithm"] == "sha256"
    assert PRIVATE_SENTINEL not in json.dumps(record)
    assert "/home/private" not in json.dumps(record)


@pytest.mark.parametrize(
    ("local", "server_name", "job_type", "role"),
    [
        (False, "server-a", "coding", "coding"),
        (True, "_local", "sync", "staging"),
    ],
)
def test_owner_dispatch_failure_audit_uses_fixed_category(
    db, audit_path, local, server_name, job_type, role
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role=role,
        type=job_type,
        pin_server=server_name,
    )

    async def failing_ssh_run(_server, _command, _timeout):
        raise ConnectionError(f"cannot reach /home/private/{PRIVATE_SENTINEL}")

    async def write_file(_server, _path, _content):
        return None

    states = (
        {}
        if local
        else {"server-a": ServerState(name="server-a", online=True, load1=0.1)}
    )
    configs = {
        "server-a": ServerConfig(
            name="server-a",
            host="192.0.2.1",
            user="runner",
            key="/tmp/synthetic-key",
        )
    }
    asyncio.run(
        scheduler_tick(
            db,
            states,
            configs,
            failing_ssh_run,
            write_file,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    record = _audit_record(audit_path, "dispatch_failed", job.id)
    params = record["params"]
    assert "error" not in params
    assert params["failure_category"] == "connection_error"
    assert "command" not in params
    assert PRIVATE_SENTINEL not in json.dumps(record)
    assert "/home/private" not in json.dumps(record)


def test_owner_stop_sanitizes_failure_and_refreshes_parent(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    staging = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="staging",
        type="sync",
        pin_server="_local",
        command="true",
    )
    db.update_job(staging.id, status="done", finished_at=now_iso(), exit_code=0)
    coding = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
        depends_on=[staging.id],
    )
    db.update_job(coding.id, status="running", server="server-a")
    db.refresh_engineering_task_status_from_jobs(task_id)
    assert db.get_engineering_task(task_id).status == "running"
    stop = request_stop_approval(db, coding.id, audit_path=audit_path)

    async def failing_ssh(_server, _command, _timeout):
        raise ConnectionError(f"cannot reach /home/private/{PRIVATE_SENTINEL}")

    result = asyncio.run(
        approve(
            db,
            stop.id,
            ssh_run=failing_ssh,
            server_configs={
                "server-a": ServerConfig(
                    name="server-a",
                    host="192.0.2.1",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            audit_path=audit_path,
        )
    )

    assert result["job"].status == "running"
    assert db.get_engineering_task(task_id).status == "running"
    assert result["approval"].note is not None
    assert "connection_error" in result["approval"].note
    assert PRIVATE_SENTINEL not in result["approval"].note
    record = _audit_record(audit_path, "stop", coding.id)
    assert record["params"]["failure_category"] == "connection_error"
    assert "kill_error" not in record["params"]
    assert PRIVATE_SENTINEL not in json.dumps(record)


@pytest.mark.parametrize("initial_status", ["queued", "running"])
def test_scheduler_never_contacts_repointed_runner_for_owner_coding_job(
    db, audit_path, initial_status
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
    )
    if initial_status == "running":
        db.update_job(job.id, status="running", server="server-a")

    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("repointed Runner must not be contacted")

    asyncio.run(
        scheduler_tick(
            db,
            {"server-a": ServerState(name="server-a", online=True, load1=0.1)},
            {
                "server-a": ServerConfig(
                    name="server-a",
                    host="198.51.100.99",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            must_not_contact,
            must_not_contact,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    assert calls == []
    assert db.get_job(job.id).status == initial_status
    mismatch_events = [
        event
        for event in db.list_engineering_task_events(task_id)
        if event.event_type == "runner_contract_mismatch"
    ]
    assert len(mismatch_events) == 1
    assert mismatch_events[0].state == "disconnected"


def test_owner_stop_does_not_contact_repointed_runner(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
    )
    db.update_job(job.id, status="running", server="server-a")
    stop = request_stop_approval(db, job.id, audit_path=audit_path)
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("repointed Runner must not be contacted")

    result = asyncio.run(
        approve(
            db,
            stop.id,
            ssh_run=must_not_contact,
            server_configs={
                "server-a": ServerConfig(
                    name="server-a",
                    host="198.51.100.99",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            audit_path=audit_path,
        )
    )

    assert calls == []
    assert result["job"].status == "running"
    assert "runner_contract_mismatch" in result["approval"].note
    record = _audit_record(audit_path, "stop", job.id)
    assert record["params"]["failure_category"] == "runner_contract_mismatch"


def test_owner_staging_stop_uses_approved_local_executor(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="staging",
        type="sync",
        pin_server=LOCAL_SERVER,
    )
    db.update_job(job.id, status="running", server=LOCAL_SERVER)
    stop = request_stop_approval(db, job.id, audit_path=audit_path)
    calls: list[tuple[str, str, int]] = []

    async def local_executor(server, command, timeout):
        calls.append((server, command, timeout))
        return _Result()

    result = asyncio.run(
        approve(
            db,
            stop.id,
            ssh_run=local_executor,
            server_configs={},
            audit_path=audit_path,
        )
    )

    assert result["job"].status == "running"
    assert "等待執行端終態證據" in result["approval"].note
    assert len(calls) == 2
    assert all(call[0] == LOCAL_SERVER for call in calls)
    assert "tmux kill-session" in calls[0][1]
    assert "tail" in calls[1][1]
    record = _audit_record(audit_path, "stop", job.id)
    assert record["params"]["kill_ok"] is True
    assert "kill_error" not in record["params"]


def test_scheduler_never_stages_to_repointed_runner(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="staging",
        type="sync",
        pin_server=LOCAL_SERVER,
    )
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("repointed Runner must not receive staging data")

    asyncio.run(
        scheduler_tick(
            db,
            {},
            {
                "server-a": ServerConfig(
                    name="server-a",
                    host="198.51.100.99",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            must_not_contact,
            must_not_contact,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    assert calls == []
    assert db.get_job(job.id).status == "queued"
    mismatch_events = [
        event
        for event in db.list_engineering_task_events(task_id)
        if event.event_type == "runner_contract_mismatch"
    ]
    assert len(mismatch_events) == 1
    assert mismatch_events[0].state == "disconnected"


@pytest.mark.parametrize("initial_status", ["queued", "running"])
def test_scheduler_never_contacts_executor_after_owner_coding_command_mutation(
    db, audit_path, initial_status
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
    )
    if initial_status == "running":
        db.update_job(job.id, status="running", server="server-a")
    db.update_job(
        job.id,
        command=f"python -m replacement --marker {PRIVATE_SENTINEL}",
    )
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("mutated owner command must not reach an executor")

    asyncio.run(
        scheduler_tick(
            db,
            {"server-a": ServerState(name="server-a", online=True, load1=0.1)},
            {
                "server-a": ServerConfig(
                    name="server-a",
                    host="192.0.2.1",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            must_not_contact,
            must_not_contact,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    assert calls == []
    assert db.get_job(job.id).status == initial_status
    _assert_execution_contract_mismatch_event(db, task_id, job.id)
    assert not any(
        event.event_type == "runner_contract_mismatch"
        for event in db.list_engineering_task_events(task_id)
    )


@pytest.mark.parametrize("initial_status", ["queued", "running"])
def test_scheduler_never_stages_or_reconciles_after_owner_command_mutation(
    db, audit_path, initial_status
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="staging",
        type="sync",
        pin_server=LOCAL_SERVER,
    )
    if initial_status == "running":
        db.update_job(job.id, status="running", server=LOCAL_SERVER)
    db.update_job(
        job.id,
        command=f"python -m replacement --marker {PRIVATE_SENTINEL}",
    )
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("mutated staging command must not reach an executor")

    asyncio.run(
        scheduler_tick(
            db,
            {},
            {
                "server-a": ServerConfig(
                    name="server-a",
                    host="192.0.2.1",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            must_not_contact,
            must_not_contact,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    assert calls == []
    assert db.get_job(job.id).status == initial_status
    _assert_execution_contract_mismatch_event(db, task_id, job.id)


def test_owner_stop_never_contacts_executor_after_command_mutation(db, audit_path):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
    )
    db.update_job(job.id, status="running", server="server-a")
    db.update_job(
        job.id,
        command=f"python -m replacement --marker {PRIVATE_SENTINEL}",
    )
    stop = request_stop_approval(db, job.id, audit_path=audit_path)
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("mutated owner command must not reach an executor")

    result = asyncio.run(
        approve(
            db,
            stop.id,
            ssh_run=must_not_contact,
            server_configs={
                "server-a": ServerConfig(
                    name="server-a",
                    host="192.0.2.1",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            audit_path=audit_path,
        )
    )

    assert calls == []
    assert result["job"].status == "running"
    assert "execution_contract_mismatch" in result["approval"].note
    record = _audit_record(audit_path, "stop", job.id)
    assert record["params"]["failure_category"] == "execution_contract_mismatch"
    assert PRIVATE_SENTINEL not in json.dumps(record)
    _assert_execution_contract_mismatch_event(db, task_id, job.id)


def test_legacy_job_dispatch_is_unaffected_by_command_journal_gate(db, audit_path):
    job = enqueue_job(
        db,
        command="echo legacy-before-update",
        type="adhoc",
        pin_server="server-a",
        audit_path=audit_path,
    )
    db.update_job(job.id, command="echo legacy-after-update")
    calls: list[tuple] = []

    async def executor(server, command, timeout):
        calls.append((server, command, timeout))
        return _Result()

    async def write_file(server, path, content):
        calls.append((server, path, content))

    asyncio.run(
        scheduler_tick(
            db,
            {"server-a": ServerState(name="server-a", online=True, load1=0.1)},
            {
                "server-a": ServerConfig(
                    name="server-a",
                    host="192.0.2.1",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            executor,
            write_file,
            audit_path=audit_path,
        )
    )

    assert calls
    assert db.get_job(job.id).status == "running"
    assert db.get_engineering_task_command_by_job_id(job.id) is None
    assert _audit_record(audit_path, "dispatch", job.id)["params"]["command"] == (
        "echo legacy-after-update"
    )


@pytest.mark.parametrize("mismatch_kind", ["command", "runner"])
def test_invalid_owner_cannot_starve_legacy_job_when_mismatch_event_fails(
    db, audit_path, monkeypatch, caplog, mismatch_kind
):
    task_id, _approval_id = _insert_task(db)
    owner = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
        pin_server="server-a",
    )
    if mismatch_kind == "command":
        db.update_job(
            owner.id,
            command=f"python -m replacement --marker {PRIVATE_SENTINEL}",
        )
    legacy = enqueue_job(
        db,
        command="echo legacy-still-runs",
        type="adhoc",
        pin_server="server-a",
        audit_path=audit_path,
    )

    def broken_event(**_kwargs):
        raise ValueError(f"Bearer {PRIVATE_SENTINEL} /home/private/event")

    monkeypatch.setattr(db, "append_engineering_task_event", broken_event)
    caplog.set_level("WARNING", logger="app.jobqueue")
    calls: list[tuple] = []

    async def executor(server, command, timeout):
        calls.append((server, command, timeout))
        return _Result()

    async def write_file(server, path, content):
        calls.append((server, path, content))

    asyncio.run(
        scheduler_tick(
            db,
            {"server-a": ServerState(name="server-a", online=True, load1=0.1)},
            {
                "server-a": ServerConfig(
                    name="server-a",
                    host=(
                        "192.0.2.1"
                        if mismatch_kind == "command"
                        else "198.51.100.99"
                    ),
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            executor,
            write_file,
            audit_path=audit_path,
            codex_runner_server="server-a",
        )
    )

    assert db.get_job(owner.id).status == "queued"
    assert db.get_job(legacy.id).status == "running"
    assert any(call[2] == "echo legacy-still-runs" for call in calls)
    assert not any(PRIVATE_SENTINEL in str(call) for call in calls)
    assert PRIVATE_SENTINEL not in caplog.text
    assert "/home/private/event" not in caplog.text


def test_stop_remains_fail_closed_when_mismatch_event_cannot_be_recorded(
    db, audit_path, monkeypatch, caplog
):
    task_id, _approval_id = _insert_task(db)
    job = _enqueue_owner(
        db,
        audit_path,
        task_id,
        role="coding",
        type="coding",
    )
    db.update_job(job.id, status="running", server="server-a")
    db.update_job(
        job.id,
        command=f"python -m replacement --marker {PRIVATE_SENTINEL}",
    )
    stop = request_stop_approval(db, job.id, audit_path=audit_path)

    def broken_event(**_kwargs):
        raise ValueError(f"Bearer {PRIVATE_SENTINEL} /home/private/event")

    monkeypatch.setattr(db, "append_engineering_task_event", broken_event)
    caplog.set_level("WARNING", logger="app.jobqueue")
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("mismatched owner must not reach executor")

    result = asyncio.run(
        approve(
            db,
            stop.id,
            ssh_run=must_not_contact,
            server_configs={
                "server-a": ServerConfig(
                    name="server-a",
                    host="192.0.2.1",
                    user="runner",
                    key="/tmp/synthetic-key",
                )
            },
            audit_path=audit_path,
        )
    )

    assert calls == []
    assert result["job"].status == "running"
    assert "execution_contract_mismatch" in result["approval"].note
    assert PRIVATE_SENTINEL not in caplog.text
    assert "/home/private/event" not in caplog.text
