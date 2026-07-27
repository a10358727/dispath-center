"""WP-2C: attempt-driven SSH dispatch closes RB-LAUNCH-001.

The decisive test here is `test_response_lost_after_tmux_keeps_the_job_running`:
run the exact scenario that the legacy scheduler mishandles and assert the Job
does *not* return to the queue. Everything else guards the boundaries around
that — DB intent before remote effect, only proven non-launch requeues, and a
disabled flag leaves the legacy path byte-identical.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.db import Database
from app.execution_dispatch import (
    AttemptLaunchContext,
    arbitrate_unknown_attempt,
    dispatch_job_via_attempt,
    reconcile_attempt,
)
from tests.test_execution_attempt_foundation import _foundation_records


class FakeResult:
    def __init__(self, stdout: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.exit_status = exit_status


class ScriptedSSH:
    """Records every remote call and fails on demand at a chosen step."""

    def __init__(self, *, fail_on: str | None = None, exc: Exception | None = None,
                 responses: dict[str, str] | None = None):
        self.calls: list[tuple[str, str, float]] = []
        self.writes: list[tuple[str, str, str]] = []
        self.fail_on = fail_on
        self.exc = exc or TimeoutError()
        self.responses = responses or {}

    async def run(self, server_name, command, timeout):
        self.calls.append((server_name, command, timeout))
        if self.fail_on and self.fail_on in command:
            raise self.exc
        for needle, stdout in self.responses.items():
            if needle in command:
                return FakeResult(stdout)
        return FakeResult()

    async def write_file(self, server_name, path, content):
        if self.fail_on == "write_file":
            raise self.exc
        self.writes.append((server_name, path, content))


@pytest.fixture()
def wired(tmp_path):
    """A leader-owned database with one approved queued Job and target."""
    database = Database(str(tmp_path / "wp2c.db"))
    records = _foundation_records(database)
    return database, records


def _queued_job(database, records):
    job = database.get_job(records["job_id"])
    assert job.status == "queued", "fixture must provide a queued pinned job"
    return job


def _dispatch(database, records, ssh, job):
    return asyncio.run(
        dispatch_job_via_attempt(
            database,
            ssh.run,
            ssh.write_file,
            job=job,
            server_config_revision_id=records["revision"]["id"],
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )


# ---------------------------------------------------------------------------
# The defect this package exists to close
# ---------------------------------------------------------------------------


def test_response_lost_after_tmux_keeps_the_job_running(wired):
    """RB-LAUNCH-001, end to end.

    The launch command times out *after* the remote session could already have
    started. The legacy path reverts to queued here and can double-dispatch;
    the attempt path must keep the Job running with unknown liveness.
    """
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="launch.sh", exc=TimeoutError())

    outcome = _dispatch(database, records, ssh, job)

    assert outcome.state == "unknown"
    assert outcome.requeued is False
    assert database.get_job(job.id).status == "running"

    attempt = database.get_execution_attempt(outcome.attempt_id)
    assert attempt["liveness"] == "unknown"
    assert attempt["state"] == "dispatching"
    assert attempt["server_name"] == "compute-a"


def test_ambiguous_launch_never_creates_a_second_attempt(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="launch.sh", exc=TimeoutError())

    first = _dispatch(database, records, ssh, job)
    second = _dispatch(database, records, ssh, job)

    assert first.state == "unknown"
    # The Job is no longer queued, so a second attempt cannot be created at all.
    assert second.state == "not_attempted"
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE job_id = ?", (job.id,)
        )
        assert cursor.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Ordering: DB intent strictly precedes any remote effect
# ---------------------------------------------------------------------------


def test_attempt_and_job_transition_are_committed_before_any_remote_call(wired):
    database, records = wired
    job = _queued_job(database, records)

    observed: list[str] = []

    class OrderingSSH(ScriptedSSH):
        async def run(self, server_name, command, timeout):
            observed.append(database.get_job(job.id).status)
            return await super().run(server_name, command, timeout)

    ssh = OrderingSSH()
    outcome = _dispatch(database, records, ssh, job)

    assert outcome.state == "running"
    # Every remote call saw an already-running Job: nothing remote happened
    # before the DB intent was durable.
    assert observed and set(observed) == {"running"}


def test_successful_dispatch_writes_files_before_launching(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH()

    outcome = _dispatch(database, records, ssh, job)

    assert outcome.state == "running"
    written = [path for _, path, _ in ssh.writes]
    assert any(path.endswith("/cmd.sh") for path in written)
    assert any(path.endswith("/run.sh") for path in written)
    assert any(path.endswith("/launch.sh") for path in written)
    launch_index = next(
        i for i, (_, command, _) in enumerate(ssh.calls) if "launch.sh" in command
    )
    mkdir_index = next(
        i for i, (_, command, _) in enumerate(ssh.calls) if command.startswith("mkdir -p")
    )
    assert mkdir_index < launch_index

    attempt = database.get_execution_attempt(outcome.attempt_id)
    assert attempt["state"] == "running"
    assert attempt["liveness"] == "known"


def test_user_command_bytes_travel_only_as_file_content(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH()

    _dispatch(database, records, ssh, job)

    assert any(content == job.command for _, _, content in ssh.writes)
    for _, command, _ in ssh.calls:
        assert job.command not in command


# ---------------------------------------------------------------------------
# Only proven non-launch requeues
# ---------------------------------------------------------------------------


def test_definite_transport_failure_before_launch_requeues(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="mkdir -p", exc=ConnectionRefusedError())

    outcome = _dispatch(database, records, ssh, job)

    assert outcome.requeued is True
    assert database.get_job(job.id).status == "queued"
    assert database.get_job(job.id).server is None
    attempt = database.get_execution_attempt(outcome.attempt_id)
    assert attempt["state"] == "abandoned_before_launch"


def test_requeue_is_impossible_from_an_ambiguous_attempt(wired):
    """The DB guard, not just the caller, refuses the unsafe requeue."""
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="launch.sh", exc=TimeoutError())
    outcome = _dispatch(database, records, ssh, job)

    with pytest.raises(ValueError, match="abandoned_before_launch"):
        database.requeue_job_after_abandoned_attempt(attempt_id=outcome.attempt_id)
    assert database.get_job(job.id).status == "running"


def test_arbitration_win_converts_an_unknown_attempt_into_a_requeue(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="launch.sh", exc=TimeoutError())
    outcome = _dispatch(database, records, ssh, job)
    attempt = database.get_execution_attempt(outcome.attempt_id)

    winner = ScriptedSSH(responses={"mkdir": "CLAIM_WON"})
    result = asyncio.run(
        arbitrate_unknown_attempt(
            database,
            winner.run,
            attempt=attempt,
            job_id=job.id,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )

    assert result.requeued is True
    assert database.get_job(job.id).status == "queued"


def test_arbitration_loss_leaves_everything_untouched(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="launch.sh", exc=TimeoutError())
    outcome = _dispatch(database, records, ssh, job)
    attempt = database.get_execution_attempt(outcome.attempt_id)

    loser = ScriptedSSH(responses={"mkdir": "CLAIM_TAKEN"})
    result = asyncio.run(
        arbitrate_unknown_attempt(
            database,
            loser.run,
            attempt=attempt,
            job_id=job.id,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )

    assert result.requeued is False
    assert database.get_job(job.id).status == "running"


def test_unreachable_during_arbitration_changes_nothing(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH(fail_on="launch.sh", exc=TimeoutError())
    outcome = _dispatch(database, records, ssh, job)
    attempt = database.get_execution_attempt(outcome.attempt_id)

    down = ScriptedSSH(fail_on="mkdir", exc=OSError("host down"))
    result = asyncio.run(
        arbitrate_unknown_attempt(
            database,
            down.run,
            attempt=attempt,
            job_id=job.id,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )

    assert result.requeued is False
    assert database.get_job(job.id).status == "running"


# ---------------------------------------------------------------------------
# Reconciliation from remote evidence
# ---------------------------------------------------------------------------


def _inspect_output(**fields) -> str:
    defaults = {
        "ATTEMPT_DIR": "present",
        "CLAIM": "present",
        "CLAIM_ABANDONED": "no",
        "RECEIPT": "",
        "EXIT_CODE": "",
        "BOOT_ID": "boot-1",
        "TMUX": "GONE",
        "EXIT_CODE_AFTER": "",
    }
    defaults.update(fields)
    return "\n".join(f"{key}={value}" for key, value in defaults.items())


def test_reconcile_converges_a_terminal_sentinel_to_the_job(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH()
    outcome = _dispatch(database, records, ssh, job)
    attempt = database.get_execution_attempt(outcome.attempt_id)

    reader = ScriptedSSH(
        responses={
            "ATTEMPT_DIR": _inspect_output(
                CLAIM_ATTEMPT_ID=attempt["id"],
                CLAIM_FENCING_TOKEN=attempt["fencing_token"],
                EXIT_CODE="0",
                EXIT_CODE_AFTER="0",
            )
        }
    )
    asyncio.run(
        reconcile_attempt(
            database,
            reader.run,
            attempt=attempt,
            job_id=job.id,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )

    assert database.get_job(job.id).status == "done"
    assert database.get_execution_attempt(attempt["id"])["state"] == "done"


def test_reconcile_never_requeues_on_missing_evidence(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH()
    outcome = _dispatch(database, records, ssh, job)
    attempt = database.get_execution_attempt(outcome.attempt_id)

    reader = ScriptedSSH(
        responses={
            "ATTEMPT_DIR": _inspect_output(
                CLAIM_ATTEMPT_ID=attempt["id"],
                CLAIM_FENCING_TOKEN=attempt["fencing_token"],
            )
        }
    )
    asyncio.run(
        reconcile_attempt(
            database,
            reader.run,
            attempt=attempt,
            job_id=job.id,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )

    assert database.get_job(job.id).status == "running"


def test_reconcile_treats_unreachable_as_no_change(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH()
    outcome = _dispatch(database, records, ssh, job)
    attempt = database.get_execution_attempt(outcome.attempt_id)

    down = ScriptedSSH(fail_on="ATTEMPT_DIR", exc=OSError("unreachable"))
    asyncio.run(
        reconcile_attempt(
            database,
            down.run,
            attempt=attempt,
            job_id=job.id,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    )

    assert database.get_job(job.id).status == "running"
    assert database.get_execution_attempt(attempt["id"])["state"] == "running"


# ---------------------------------------------------------------------------
# Rollout and rollback
# ---------------------------------------------------------------------------


def test_context_declines_every_server_when_the_flag_is_off():
    context = AttemptLaunchContext(
        leader_owner_id="scheduler-a",
        scheduler_fencing_epoch=1,
        enabled=False,
        revision_ids={"compute-a": "rev-1"},
    )
    assert context.owns("compute-a") is False


def test_context_declines_when_this_process_is_not_the_leader():
    context = AttemptLaunchContext(
        leader_owner_id="scheduler-a",
        scheduler_fencing_epoch=None,
        enabled=True,
        revision_ids={"compute-a": "rev-1"},
    )
    assert context.owns("compute-a") is False


def test_context_declines_a_server_without_a_pinned_revision():
    context = AttemptLaunchContext(
        leader_owner_id="scheduler-a",
        scheduler_fencing_epoch=1,
        enabled=True,
        revision_ids={"compute-a": "rev-1"},
    )
    assert context.owns("compute-a") is True
    assert context.owns("legacy-observed-server") is False


def test_launch_flag_requires_new_claims_ownership():
    from app.config import AppConfig

    with pytest.raises(ValueError, match="EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED"):
        AppConfig(
            servers=[],
            execution_attempt_ssh_launch_enabled=True,
        ).validate()


def test_inspect_operations_accept_only_the_pinned_command_kinds(wired):
    database, records = wired
    job = _queued_job(database, records)
    ssh = ScriptedSSH()
    outcome = _dispatch(database, records, ssh, job)

    with pytest.raises(ValueError, match="not in the pinned allowlist"):
        database.insert_execution_operation(
            attempt_id=outcome.attempt_id,
            operation="inspect",
            payload={"command": "cat /etc/shadow"},
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
    accepted = database.insert_execution_operation(
        attempt_id=outcome.attempt_id,
        operation="inspect",
        payload={"command_kind": "attempt_inspect"},
        leader_owner_id=records["lease"]["owner_id"],
        scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
    )
    assert accepted["authorization_class"] == "inspect"
    assert accepted["authorization_approval_id"] is None
