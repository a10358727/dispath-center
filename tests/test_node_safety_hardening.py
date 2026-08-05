"""Regression coverage for the post-canary Node safety audit."""

from __future__ import annotations

import inspect
import os
import signal
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent.__main__ import AgentConfig, NodeAgentDaemon
from agent.client import command_digest
from agent.runner import (
    AttemptStore,
    build_supervisor_argv,
    read_process_identity,
    signal_verified_supervisor,
)
from app.db import Database
from app.node_registry import (
    enroll_node,
    lease_job_for_node,
    record_artifact_metadata,
    record_terminal_result,
)


CANARY_TAG = "node-canary"


def _job(database: Database, command: str = "true") -> int:
    return database.insert_job(
        command=command,
        type="adhoc",
        require_tag=CANARY_TAG,
    )


def _lease(database: Database, node, job_id: int, command: str = "true"):
    return lease_job_for_node(
        database,
        node=node,
        job_id=job_id,
        command=command,
        canary_tag=CANARY_TAG,
        job_type="adhoc",
        require_tag=CANARY_TAG,
    )


@pytest.mark.parametrize("collision", ["job", "node"])
def test_legacy_claim_is_atomic_across_database_connections(tmp_path, collision):
    path = tmp_path / f"claim-{collision}.db"
    setup = Database(str(path))
    first_node = enroll_node(setup, server_name="worker-a").node
    second_node = enroll_node(setup, server_name="worker-b").node
    first_job = _job(setup)
    second_job = _job(setup)
    setup.close()

    left = Database(str(path))
    right = Database(str(path))
    if collision == "job":
        claims = ((left, first_node, first_job), (right, second_node, first_job))
    else:
        claims = ((left, first_node, first_job), (right, first_node, second_job))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: _lease(*args), claims))

    observed = Database(str(path))
    active = [
        row
        for row in observed.list_node_attempts()
        if row.status in {"leased", "acked", "running"}
    ]
    assert len(active) == 1
    assert sum(result.attempt is not None for result in results) == 1
    left.close()
    right.close()
    observed.close()


def test_fresh_node_schema_has_foreign_keys_checks_and_active_uniqueness():
    database = Database(":memory:")
    node = enroll_node(database, server_name="worker-a").node
    other_node = enroll_node(database, server_name="worker-b").node
    first_job = _job(database)
    second_job = _job(database)

    with database.cursor() as cursor:
        node_fks = {
            (row["from"], row["table"])
            for row in cursor.execute("PRAGMA foreign_key_list(node_attempts)")
        }
        artifact_fks = {
            (row["from"], row["table"])
            for row in cursor.execute(
                "PRAGMA foreign_key_list(node_attempt_artifacts)"
            )
        }
    assert {
        ("job_id", "jobs"),
        ("node_id", "nodes"),
        ("execution_attempt_id", "execution_attempts"),
    } <= node_fks
    assert ("attempt_id", "node_attempts") in artifact_fks

    database.insert_node_attempt(
        attempt_id="active-one",
        job_id=first_job,
        node_id=node.id,
        command_sha256="a" * 64,
        lease_expires_at="2999-01-01T00:00:00+00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.insert_node_attempt(
            attempt_id="same-node",
            job_id=second_job,
            node_id=node.id,
            command_sha256="b" * 64,
            lease_expires_at="2999-01-01T00:00:00+00:00",
        )
    with pytest.raises(sqlite3.IntegrityError):
        database.insert_node_attempt(
            attempt_id="same-job",
            job_id=first_job,
            node_id=other_node.id,
            command_sha256="c" * 64,
            lease_expires_at="2999-01-01T00:00:00+00:00",
        )
    with database.cursor() as cursor:
        with pytest.raises(sqlite3.IntegrityError, match="invalid node attempt status"):
            cursor.execute(
                "UPDATE node_attempts SET status = 'invented' WHERE id = 'active-one'"
            )
    database.close()


def test_terminal_and_job_projection_roll_back_together(tmp_path):
    database = Database(str(tmp_path / "terminal.db"))
    node = enroll_node(database, server_name="worker-a").node
    job_id = _job(database)
    leased = _lease(database, node, job_id).attempt
    assert leased is not None
    assert database.ack_node_attempt(leased.id, node.id)
    with database.cursor() as cursor:
        cursor.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        cursor.execute(
            """
            CREATE TRIGGER fail_job_terminal
            BEFORE UPDATE OF status ON jobs
            WHEN NEW.status IN ('done', 'failed')
            BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected projection failure"):
        record_terminal_result(
            database,
            node=node,
            attempt_id=leased.id,
            exit_code=0,
            log_tail="complete",
        )
    assert database.get_node_attempt(leased.id).status == "acked"
    assert database.get_job(job_id).status == "running"
    database.close()


def test_exact_terminal_retry_repairs_a_historical_partial_projection(tmp_path):
    database = Database(str(tmp_path / "repair.db"))
    node = enroll_node(database, server_name="worker-a").node
    job_id = _job(database)
    leased = _lease(database, node, job_id).attempt
    assert leased is not None
    assert database.ack_node_attempt(leased.id, node.id)
    with database.cursor() as cursor:
        cursor.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        cursor.execute(
            """
            UPDATE node_attempts
            SET status = 'done', terminal_at = '2026-08-02T00:00:00+00:00',
                exit_code = 0, log_tail = 'complete'
            WHERE id = ?
            """,
            (leased.id,),
        )

    result = record_terminal_result(
        database,
        node=node,
        attempt_id=leased.id,
        exit_code=0,
        log_tail="complete",
    )
    assert result.accepted and result.duplicate and result.job_transitioned
    assert database.get_job(job_id).status == "done"
    database.close()


def test_artifact_report_rolls_back_the_whole_batch_on_write_failure(tmp_path):
    database = Database(str(tmp_path / "artifact.db"))
    node = enroll_node(database, server_name="worker-a").node
    leased = _lease(database, node, _job(database)).attempt
    assert leased is not None
    assert database.ack_node_attempt(leased.id, node.id)
    with database.cursor() as cursor:
        cursor.execute(
            """
            CREATE TRIGGER fail_second_artifact
            BEFORE INSERT ON node_attempt_artifacts
            WHEN NEW.relative_path = 'second.bin'
            BEGIN SELECT RAISE(ABORT, 'injected artifact failure'); END
            """
        )
    artifacts = [
        {"path": "first.bin", "size_bytes": 1, "sha256": "a" * 64},
        {"path": "second.bin", "size_bytes": 2, "sha256": "b" * 64},
    ]
    with pytest.raises(sqlite3.IntegrityError, match="injected artifact failure"):
        record_artifact_metadata(
            database, node=node, attempt_id=leased.id, artifacts=artifacts
        )
    assert database.list_node_attempt_artifacts(leased.id) == []
    database.close()


def test_artifact_metadata_trigger_rejects_direct_overwrite(tmp_path):
    database = Database(str(tmp_path / "artifact-immutable.db"))
    node = enroll_node(database, server_name="worker-a").node
    leased = _lease(database, node, _job(database)).attempt
    assert leased is not None
    assert database.ack_node_attempt(leased.id, node.id)
    database.upsert_node_attempt_artifact(
        attempt_id=leased.id,
        relative_path="model.bin",
        kind="model",
        size_bytes=1,
        sha256="a" * 64,
    )
    with database.cursor() as cursor:
        with pytest.raises(sqlite3.IntegrityError, match="metadata is immutable"):
            cursor.execute(
                "UPDATE node_attempt_artifacts SET size_bytes = 2 WHERE attempt_id = ?",
                (leased.id,),
            )
    assert database.list_node_attempt_artifacts(leased.id)[0]["size_bytes"] == 1
    database.close()


class _TerminalClient:
    def __init__(self):
        self.terminals: list[tuple[str, int, str]] = []

    def report_artifacts(self, attempt_id, artifacts):
        return len(artifacts)

    def report_terminal(self, attempt_id, *, exit_code, log_tail=""):
        self.terminals.append((attempt_id, exit_code, log_tail))
        return True


def test_supervisor_terminal_survives_agent_waitpid_ownership_loss(
    tmp_path, monkeypatch
):
    store = AttemptStore(tmp_path / "attempts")
    command = (
        'if [ -n "${DISPATCH_NODE_TOKEN+x}" ]; then exit 91; fi; '
        "printf durable-supervisor; exit 7"
    )
    attempt = store.create(
        attempt_id="supervised-1",
        job_id=1,
        command_sha256=command_digest(command),
    )
    store.record_ack(attempt)
    store.write_command(attempt.attempt_id, command)
    attempt = store.load(attempt.attempt_id)
    assert attempt is not None
    process = subprocess.Popen(
        build_supervisor_argv(
            attempt.attempt_id,
            store.attempt_dir(attempt.attempt_id),
            attempt.command_sha256,
        ),
        cwd=str(Path(__file__).resolve().parents[1]),
        shell=False,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DISPATCH_NODE_TOKEN": "must-not-reach-workload"},
    )
    identity = read_process_identity(process.pid)
    assert identity is not None
    store.record_launch(
        attempt,
        process.pid,
        process_boot_id=identity[0],
        process_start_time_ticks=identity[1],
    )
    assert process.wait(timeout=5) == 0

    # A restarted daemon cannot wait() a process created by its predecessor.
    monkeypatch.setattr(
        "agent.__main__.os.waitpid",
        lambda *_args: (_ for _ in ()).throw(ChildProcessError()),
    )
    client = _TerminalClient()
    config = AgentConfig(
        control_plane_url="https://control.example",
        node_token="redacted-test-token",
        workdir=str(store.root),
    )
    daemon = NodeAgentDaemon(config, client, AttemptStore(store.root))

    assert daemon._monitor_local_attempts() == "terminal_reported"
    recovered = store.load(attempt.attempt_id)
    assert recovered is not None and recovered.terminal
    assert recovered.exit_code == 7
    assert client.terminals == [(attempt.attempt_id, 7, "")]


def test_pid_reuse_identity_mismatch_never_sends_a_signal(monkeypatch, tmp_path):
    store = AttemptStore(tmp_path)
    attempt = store.create(attempt_id="pid-guard", job_id=1, command_sha256="a" * 64)
    attempt = store.record_launch(
        attempt,
        4242,
        process_boot_id="old-boot",
        process_start_time_ticks=99,
    )
    monkeypatch.setattr(
        "agent.runner.read_process_identity", lambda _pid: ("new-boot", 100)
    )
    sent = []
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: sent.append(args))

    assert signal_verified_supervisor(attempt, signal.SIGTERM) is False
    assert sent == []


def test_node_api_models_reject_extra_and_oversized_payloads(api_client):
    client, main_module = api_client
    state = main_module.app_state
    state.config.node_agent_v1_enabled = True
    state.config.node_canary_require_tag = CANARY_TAG
    enrolled = enroll_node(state.db, server_name="worker-a")
    headers = {
        "X-Node-Token": enrolled.raw_token,
        "X-Node-Protocol-Version": "2.0",
    }

    assert client.post(
        "/node-agent/heartbeat",
        json={"agent_version": "v" * 129},
        headers=headers,
    ).status_code == 422
    assert client.post(
        "/node-agent/terminal",
        json={
            "attempt_id": "attempt-1",
            "exit_code": 0,
            "log_tail": "界" * 6000,
        },
        headers=headers,
    ).status_code == 422
    assert client.post(
        "/node-agent/ack",
        json={
            "attempt_id": "attempt-1",
            "command_sha256": "a" * 64,
            "unexpected": True,
        },
        headers=headers,
    ).status_code == 422


def test_high_frequency_node_routes_do_not_run_sync_db_on_event_loop():
    import app.main as main_module

    for route in (
        main_module.node_agent_poll_endpoint,
        main_module.node_agent_ack_endpoint,
        main_module.node_agent_heartbeat_endpoint,
        main_module.node_agent_artifacts_endpoint,
        main_module.node_agent_stop_ack_endpoint,
    ):
        assert not inspect.iscoroutinefunction(route)
    assert inspect.iscoroutinefunction(main_module.node_agent_terminal_endpoint)
    assert "_run_tracked_blocking" in inspect.getsource(
        main_module.node_agent_terminal_endpoint
    )
    assert "_run_tracked_blocking" in inspect.getsource(main_module.auth_middleware)
