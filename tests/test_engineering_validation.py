"""Immutable contract and side-effect gates for Worker validation Jobs."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

from app.approvals import (
    ApprovalNotPendingError,
    InvalidEngineeringValidationRequestError,
    approve,
    reject,
    request_engineering_worker_validation_approval,
    request_stop_approval,
)
from app.audit import now_iso, read_audit
from app.config import AppConfig, ServerConfig
from app.db import (
    Database,
    _safe_engineering_validation_exit_code,
    _safe_engineering_validation_timestamp,
)
from app.engineering_validation import engineering_validation_job_contract_failure
from app.jobfinish import handle_job_finished
from app.jobqueue import enqueue_job
from app.monitor import ServerState
from app.results import local_result_dir
from app.scheduler import scheduler_tick


BASE_COMMIT = "a" * 40
RESULT_COMMIT = "b" * 40


class _Result:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


def _target(*, host: str = "192.0.2.20", key: str = "/tmp/worker-key") -> ServerConfig:
    return ServerConfig(
        name="worker-a",
        host=host,
        user="worker",
        key=key,
        tags=["validation"],
    )


def _app_state(local_home_dir: Path):
    return SimpleNamespace(
        config=SimpleNamespace(local_home_dir=str(local_home_dir))
    )


def _eligible_task(db, tmp_path: Path) -> dict:
    project = "validation-project"
    project_id = db.insert_project(
        project,
        f"https://example.invalid/{project}.git",
        default_command="python -m pytest -q",
    )
    version = db.get_or_create_project_version(
        project, BASE_COMMIT, git_ref="main"
    )
    task_id = str(uuid.uuid4())
    structured_request = {
        "objective": "Validate the approved change bundle on a worker"
    }
    instruction = "AI Engineering Task\n\nTask objective:\nValidate the bundle."
    execution_contract = {
        "base_binding": "project_version_pinned",
        "runner": {
            "name": "server-a",
            "host": "192.0.2.10",
            "user": "runner",
            "port": 22,
        },
        "workspace_rel": "codex_workspaces",
    }
    parent_payload = {
        "contract_version": "engineering-task-v1",
        "engineering_task_id": task_id,
        "project": project,
        "project_id": project_id,
        "project_version_id": version.id,
        "base_commit": BASE_COMMIT,
        "agent_provider_id": "codex",
        "execution_contract": execution_contract,
        "structured_request": structured_request,
        "instruction": instruction,
        "runner_server": "server-a",
    }
    task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract=execution_contract,
        contract_version="engineering-task-v1",
        structured_request=structured_request,
        instruction=instruction,
        detected_metadata={},
        runner_server="server-a",
        validation_target="worker-a",
        approval_payload=parent_payload,
    )
    run_id, staging_job_id, coding_job_id = (
        db.finalize_engineering_task_approval_plan(
            task_id=task_id,
            approval_id=approval_id,
            project=project,
            runner_server="server-a",
            instruction=instruction,
            base_commit=BASE_COMMIT,
            project_version_id=version.id,
            validation_target="worker-a",
            worktree_path="codex_workspaces/task/worktree",
            staging_command="echo stage-immutable-bundle",
            coding_command="echo run-approved-codex-turn",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="human",
        )
    )
    db.update_job(
        staging_job_id,
        status="done",
        server="_local",
        started_at=now_iso(),
        finished_at=now_iso(),
        exit_code=0,
    )
    db.update_job(
        coding_job_id,
        status="done",
        server="server-a",
        started_at=now_iso(),
        finished_at=now_iso(),
        exit_code=0,
    )
    db.update_coding_run(
        run_id,
        status="done",
        result_commit=RESULT_COMMIT,
        bundle_path="changes.bundle",
        finished_at=now_iso(),
    )
    db.update_engineering_task(task_id, status="done")

    bundle_path = Path(
        local_result_dir(coding_job_id, str(tmp_path))
    ) / "changes.bundle"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_bytes = b"immutable worker validation bundle\n"
    bundle_path.write_bytes(bundle_bytes)
    bundle_digest = hashlib.sha256(bundle_bytes).hexdigest()
    artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="bundle",
        kind="bundle",
        label="Change bundle",
        storage_kind="managed_local_result",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        storage_key="changes.bundle",
        content_type="application/x-git-bundle",
        verification_status="pending",
        redaction_status="not_applicable",
        availability="pending",
    )
    db.record_engineering_task_artifact_collection(
        artifact.id,
        source_sha256=bundle_digest,
        source_size_bytes=len(bundle_bytes),
        verification_status="verified",
        redaction_status="not_applicable",
        availability="available",
    )
    instance_id = db.insert_project_instance(
        project_name=project,
        server="worker-a",
        path="projects/validation-project",
        git_commit=BASE_COMMIT,
    )
    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "project": project,
        "project_id": project_id,
        "version_id": version.id,
        "run_id": run_id,
        "coding_job_id": coding_job_id,
        "instance_id": instance_id,
        "bundle_digest": bundle_digest,
        "bundle_size": len(bundle_bytes),
    }


def _request(db, tmp_path: Path, audit_path: str, target: ServerConfig):
    fixture = _eligible_task(db, tmp_path)
    validation, approval = request_engineering_worker_validation_approval(
        db,
        fixture["task_id"],
        command="python -m pytest -q",
        pin_server=target.name,
        server_configs={target.name: target},
        local_home_dir=str(tmp_path),
        require_tag="validation",
        audit_path=audit_path,
    )
    return fixture, validation, approval


def _approve_validation(
    db,
    validation_approval_id: int,
    tmp_path: Path,
    audit_path: str,
    target: ServerConfig,
):
    return asyncio.run(
        approve(
            db,
            validation_approval_id,
            server_configs={target.name: target},
            app_state=_app_state(tmp_path),
            audit_path=audit_path,
        )
    )


def _worker_validation_events(db, task_id: str):
    return [
        event
        for event in db.list_engineering_task_events(task_id, limit=200)
        if event.event_type.startswith("worker_validation_")
    ]


def test_request_is_atomic_pending_and_snapshots_exact_generated_commands(
    db, tmp_path, audit_path
):
    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)

    assert approval.kind == "enqueue"
    assert approval.status == "pending"
    assert validation.status == "pending_approval"
    assert validation.bundle_push_job_id is None
    assert validation.downstream_job_id is None
    assert not [
        job
        for job in db.list_jobs()
        if job.engineering_validation_request_id == validation.id
    ]
    snapshot = validation.request_snapshot
    assert snapshot["task"]["id"] == fixture["task_id"]
    assert snapshot["parent_approval"]["id"] == fixture["approval_id"]
    assert snapshot["bundle"] == {
        "storage_key": "changes.bundle",
        "sha256": fixture["bundle_digest"],
        "size_bytes": fixture["bundle_size"],
    }
    assert set(snapshot["execution"]) == {
        "bundle_push_command_sha256",
        "downstream_command_sha256",
    }
    assert all(len(value) == 64 for value in snapshot["execution"].values())


def test_reject_atomically_updates_validation_and_journal_without_jobs(
    db, tmp_path, audit_path
):
    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    immutable_payload = approval.payload

    rejected = reject(
        db,
        approval.id,
        note="worker validation 不需要",
        audit_path=audit_path,
    )

    # These facts are durable immediately after the decision.  No detail/list
    # read or refresh is needed to repair the linked projection.
    persisted = db.get_engineering_validation_request(validation.id)
    assert rejected.status == "rejected"
    assert rejected.note == "worker validation 不需要"
    assert rejected.decision_mechanism == "manual"
    assert rejected.payload == immutable_payload
    assert persisted.status == "rejected"
    assert persisted.result_status is None
    assert persisted.result_exit_code is None
    assert persisted.result_finished_at is None
    assert persisted.bundle_push_job_id is None
    assert persisted.downstream_job_id is None
    assert not [
        job
        for job in db.list_jobs()
        if job.engineering_validation_request_id == validation.id
    ]

    events = _worker_validation_events(db, fixture["task_id"])
    assert [event.state for event in events] == ["requested", "rejected"]
    rejection = events[-1]
    assert rejection.event_key == f"worker-validation:{validation.id}:rejected"
    assert rejection.details == {
        "approval_id": approval.id,
        "previous_state": "requested",
        "validation_request_id": validation.id,
    }
    assert rejection.source_kind == "approval"
    assert rejection.source_id == str(approval.id)

    event_count = len(events)
    assert db.refresh_engineering_validation_request_status(validation.id).status == (
        "rejected"
    )
    assert len(_worker_validation_events(db, fixture["task_id"])) == event_count
    with pytest.raises(ApprovalNotPendingError):
        reject(db, approval.id, audit_path=audit_path)
    assert len(_worker_validation_events(db, fixture["task_id"])) == event_count
    assert not [
        job
        for job in db.list_jobs()
        if job.engineering_validation_request_id == validation.id
    ]


def test_approval_creates_two_ordinary_jobs_with_valid_backrefs_and_digests(
    db, tmp_path, audit_path
):
    target = _target()
    _fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)

    refreshed = db.get_engineering_validation_request(validation.id)
    push = db.get_job(result["bundle_push_job_id"])
    downstream = result["job"]
    assert db.get_approval(approval.id).status == "approved"
    assert refreshed.status == "queued"
    assert push.engineering_validation_request_id == validation.id
    assert downstream.engineering_validation_request_id == validation.id
    assert push.engineering_task_id is None
    assert downstream.engineering_task_id is None
    assert downstream.depends_on == [push.id]
    assert downstream.source_coding_run_id == validation.coding_run_id
    assert hashlib.sha256(push.command.encode()).hexdigest() == (
        refreshed.bundle_push_command_sha256
    )
    assert hashlib.sha256(downstream.command.encode()).hexdigest() == (
        refreshed.downstream_command_sha256
    )
    materialized = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_materialized"
        and event["approval_id"] == approval.id
    ]
    assert {event["params"]["job_role"] for event in materialized} == {
        "bundle_push",
        "main",
    }
    assert {event["resource_id"] for event in materialized} == {
        str(push.id),
        str(downstream.id),
    }
    assert not any(
        record["action"] == "enqueue"
        and record.get("params", {}).get("approval_id") == approval.id
        for record in read_audit(audit_path)
    )
    configs = {target.name: target}
    assert (
        engineering_validation_job_contract_failure(
            db, push, configs, local_home_dir=str(tmp_path)
        )
        is None
    )
    assert (
        engineering_validation_job_contract_failure(
            db, downstream, configs, local_home_dir=str(tmp_path)
        )
        is None
    )


def test_validation_materialization_audit_failure_rolls_back_jobs_and_decision(
    db, tmp_path, audit_path, monkeypatch
):
    target = _target()
    _fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    original = db.append_durable_audit_event_in_transaction

    def fail_materialization(*args, **kwargs):
        if kwargs.get("action") == "execution_job_materialized":
            raise RuntimeError("validation audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        db, "append_durable_audit_event_in_transaction", fail_materialization
    )
    with pytest.raises(RuntimeError, match="validation audit append fault"):
        _approve_validation(db, approval.id, tmp_path, audit_path, target)

    assert db.get_approval(approval.id).status == "pending"
    assert db.get_engineering_validation_request(validation.id).status == (
        "pending_approval"
    )
    assert not [
        job
        for job in db.list_jobs()
        if job.engineering_validation_request_id == validation.id
    ]
    assert not any(
        event["action"] in {"approval_decided", "execution_job_materialized"}
        and event.get("approval_id") == approval.id
        for event in db.list_durable_audit_events(limit=100)
    )


def test_validation_status_journal_is_idempotent_and_survives_restart(
    tmp_path, audit_path
):
    database_path = tmp_path / "validation-events.db"
    database = Database(str(database_path))
    target = _target()
    try:
        fixture, validation, approval = _request(
            database, tmp_path, audit_path, target
        )
        result = _approve_validation(
            database, approval.id, tmp_path, audit_path, target
        )
        downstream = result["job"]
        assert [
            event.state
            for event in _worker_validation_events(database, fixture["task_id"])
        ] == ["requested", "queued"]

        database.update_job(
            downstream.id,
            status="running",
            server=target.name,
            started_at="2026-07-15T10:00:00+00:00",
        )
        assert database.refresh_engineering_validation_request_status(
            validation.id
        ).status == "running"
        running_count = len(
            _worker_validation_events(database, fixture["task_id"])
        )
        database.refresh_engineering_validation_request_status(validation.id)
        assert len(_worker_validation_events(database, fixture["task_id"])) == (
            running_count
        )

        # Requeue is a real Job transition and may occur more than once.  The
        # persisted ordinal keeps each occurrence unique without random keys.
        database.update_job(
            downstream.id, status="queued", server=None, started_at=None
        )
        database.refresh_engineering_validation_request_status(validation.id)
        database.update_job(
            downstream.id,
            status="running",
            server=target.name,
            started_at="2026-07-15T10:01:00+00:00",
        )
        database.refresh_engineering_validation_request_status(validation.id)
        database.update_job(
            downstream.id,
            status="done",
            exit_code=0,
            finished_at="2026-07-15T10:02:00+00:00",
        )
    finally:
        database.close()

    reopened = Database(str(database_path))
    try:
        refreshed = reopened.refresh_engineering_validation_request_status(
            validation.id
        )
        assert refreshed.status == "done"
        events = _worker_validation_events(reopened, fixture["task_id"])
        assert [event.state for event in events] == [
            "requested",
            "queued",
            "running",
            "queued",
            "running",
            "done",
        ]
        transition_keys = [
            event.event_key for event in events if ":transition:" in event.event_key
        ]
        assert len(transition_keys) == len(set(transition_keys))
        event_count = len(events)
        reopened.refresh_engineering_validation_request_status(validation.id)
        assert len(_worker_validation_events(reopened, fixture["task_id"])) == (
            event_count
        )
    finally:
        reopened.close()


def test_validation_status_refresh_is_idempotent_across_database_connections(
    tmp_path, audit_path
):
    database_path = tmp_path / "validation-concurrent-refresh.db"
    primary = Database(str(database_path))
    observers = []
    locker = None
    threads: list[threading.Thread] = []
    try:
        target = _target()
        fixture, validation, approval = _request(
            primary, tmp_path, audit_path, target
        )
        result = _approve_validation(
            primary, approval.id, tmp_path, audit_path, target
        )
        primary.update_job(
            result["job"].id,
            status="running",
            server=target.name,
            started_at="2026-07-15T10:30:00+00:00",
        )

        # Open both observers before the external write lock because Database
        # construction performs additive schema/index checks.
        observers = [Database(str(database_path)), Database(str(database_path))]
        reached_updates = [threading.Event(), threading.Event()]
        for observer in observers:
            observer._conn.execute("PRAGMA busy_timeout = 10000")

        def make_trace_callback(reached):
            def trace(statement):
                if statement.lstrip().upper().startswith(
                    "UPDATE ENGINEERING_VALIDATION_REQUESTS"
                ):
                    reached.set()

            return trace

        for observer, reached in zip(observers, reached_updates):
            observer._conn.set_trace_callback(make_trace_callback(reached))

        outcomes = Queue()

        def refresh(observer):
            try:
                refreshed = observer.refresh_engineering_validation_request_status(
                    validation.id
                )
                outcomes.put(("ok", refreshed.status))
            except BaseException as exc:  # pragma: no cover - asserted below
                outcomes.put(("error", type(exc).__name__, str(exc)))

        locker = sqlite3.connect(str(database_path), timeout=10)
        locker.execute("PRAGMA busy_timeout = 10000")
        locker.execute("BEGIN IMMEDIATE")
        lock_held = True
        try:
            threads = [
                threading.Thread(target=refresh, args=(observer,))
                for observer in observers
            ]
            for thread in threads:
                thread.start()
            both_reached = all(reached.wait(5) for reached in reached_updates)
        finally:
            if lock_held:
                locker.commit()
                lock_held = False

        for thread in threads:
            thread.join(timeout=10)

        assert both_reached, "both refreshes must reach the CAS from the same row"
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcomes.get(timeout=1) for _observer in observers) == [
            ("ok", "running"),
            ("ok", "running"),
        ]
        running = [
            event
            for event in _worker_validation_events(primary, fixture["task_id"])
            if ":transition:" in event.event_key and event.state == "running"
        ]
        assert len(running) == 1
        assert running[0].details["previous_state"] == "queued"
    finally:
        if locker is not None:
            try:
                locker.rollback()
            except sqlite3.Error:
                pass
            locker.close()
        for thread in threads:
            thread.join(timeout=1)
        for observer in observers:
            observer._conn.set_trace_callback(None)
            observer.close()
        primary.close()


def test_transition_ordinal_ignores_malformed_keys_and_fills_gaps_without_collision(
    db, tmp_path, audit_path
):
    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    transition_prefix = f"worker-validation:{validation.id}:transition:"

    # Three pre-existing rows make COUNT(*) + 1 choose ordinal 4, which would
    # collide with the final seed below.  The durable journal may contain both
    # malformed keys and gaps after a restore, so allocation must inspect the
    # numeric ordinals rather than infer them from row count.
    for suffix in ("malformed", "2:queued", "4:running"):
        db.append_engineering_task_event(
            task_id=fixture["task_id"],
            attempt_number=1,
            event_key=f"{transition_prefix}{suffix}",
            event_type="validation_transition_seed",
            phase="validation",
            state="seed",
            source_kind="system",
            source_id=validation.id,
            summary="Validation transition seed",
            details={"seed": suffix},
        )

    db.update_job(
        result["job"].id,
        status="running",
        server=target.name,
        started_at="2026-07-15T10:45:00+00:00",
    )
    refreshed = db.refresh_engineering_validation_request_status(validation.id)

    assert refreshed.status == "running"
    transition_keys = {
        event.event_key
        for event in db.list_engineering_task_events(fixture["task_id"], limit=200)
        if event.event_key.startswith(transition_prefix)
    }
    assert f"{transition_prefix}4:running" in transition_keys
    assert f"{transition_prefix}1:running" in transition_keys
    assert len(transition_keys) == 4


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (False, None),
        (-1, None),
        (256, None),
        (1.0, None),
        ("1", None),
        (0, 0),
        (255, 255),
    ],
)
def test_validation_exit_code_boundary_is_int_non_bool(value, expected):
    assert _safe_engineering_validation_exit_code(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (123, None),
        ("not-a-timestamp", None),
        ("2026-07-15T11:00:00", None),
        ("x" * 65, None),
        (
            "2026-07-15T11:00:00+00:00",
            "2026-07-15T11:00:00+00:00",
        ),
    ],
)
def test_validation_timestamp_boundary_requires_bounded_timezone_aware_iso(
    value, expected
):
    assert _safe_engineering_validation_timestamp(value) == expected


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_validation_terminal_status_is_journalled_once(
    db, tmp_path, audit_path, terminal_status
):
    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    db.update_job(
        result["job"].id,
        status=terminal_status,
        exit_code=1,
        finished_at="2026-07-15T11:00:00+00:00",
    )

    refreshed = db.refresh_engineering_validation_request_status(validation.id)
    assert refreshed.status == terminal_status
    assert refreshed.result_status == terminal_status
    assert refreshed.result_exit_code == 1
    assert refreshed.result_finished_at == "2026-07-15T11:00:00+00:00"
    events = _worker_validation_events(db, fixture["task_id"])
    assert events[-1].state == terminal_status
    assert events[-1].event_type == f"worker_validation_{terminal_status}"
    count = len(events)
    db.refresh_engineering_validation_request_status(validation.id)
    assert len(_worker_validation_events(db, fixture["task_id"])) == count


def test_stale_and_malformed_result_metadata_is_cleared_and_withheld(
    db, tmp_path, audit_path, monkeypatch
):
    import app.main as main_module

    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)

    # Non-terminal projections cannot retain stale terminal result metadata.
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE engineering_validation_requests
            SET result_exit_code = 77,
                result_finished_at = '2026-07-15T12:00:00+00:00'
            WHERE id = ?
            """,
            (validation.id,),
        )
    normalized = db.refresh_engineering_validation_request_status(validation.id)
    assert normalized.status == "queued"
    assert normalized.result_status == "queued"
    assert normalized.result_exit_code is None
    assert normalized.result_finished_at is None

    invalid_finished_at = "2026-07-15T13:00:00"  # parseable but timezone-naive
    db.update_job(
        result["job"].id,
        status="failed",
        exit_code=999,
        finished_at=invalid_finished_at,
    )
    terminal = db.refresh_engineering_validation_request_status(validation.id)
    assert terminal.status == "failed"
    assert terminal.result_status == "failed"
    assert terminal.result_exit_code is None
    assert terminal.result_finished_at is None

    event = _worker_validation_events(db, fixture["task_id"])[-1]
    assert event.state == "failed"
    assert event.occurred_at != invalid_finished_at
    assert invalid_finished_at not in str(event.details)
    assert "999" not in str(event.details)

    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=SimpleNamespace(local_home_dir=str(tmp_path)),
            server_configs={},
            server_states={},
        ),
    )
    projection = main_module._engineering_validation_request_to_dict(terminal)
    assert projection["result"] == {
        "status": "failed",
        "exit_code": None,
        "finished_at": None,
    }
    assert invalid_finished_at not in str(projection)
    assert "999" not in str(projection["result"])


@pytest.mark.parametrize(
    ("approval_status", "expected_state"),
    [("rejected", "rejected"), ("approved", "unknown")],
)
def test_validation_approval_projection_journals_rejected_or_unknown_without_failure(
    db, tmp_path, audit_path, approval_status, expected_state
):
    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    fields = {"status": approval_status}
    if approval_status == "rejected":
        fields["decided_at"] = "2026-07-15T12:00:00+00:00"
    db.update_approval(approval.id, **fields)

    refreshed = db.refresh_engineering_validation_request_status(validation.id)
    assert refreshed.status == expected_state
    event = _worker_validation_events(db, fixture["task_id"])[-1]
    assert event.state == expected_state
    assert event.event_type == f"worker_validation_{expected_state}"
    assert event.state != "failed"
    count = len(_worker_validation_events(db, fixture["task_id"]))
    db.refresh_engineering_validation_request_status(validation.id)
    assert len(_worker_validation_events(db, fixture["task_id"])) == count


def test_unrecognized_job_status_is_projected_and_journalled_as_unknown(
    db, tmp_path, audit_path, monkeypatch
):
    import app.main as main_module

    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    raw_status = "future_remote_state"
    db.update_job(
        result["job"].id,
        status=raw_status,
        exit_code=93,
        finished_at="2026-07-15T12:30:00+00:00",
    )

    refreshed = db.refresh_engineering_validation_request_status(validation.id)
    assert refreshed.status == "unknown"
    assert refreshed.result_status == "unknown"
    assert refreshed.result_exit_code is None
    assert refreshed.result_finished_at is None
    events = _worker_validation_events(db, fixture["task_id"])
    assert events[-1].state == "unknown"
    assert events[-1].event_type == "worker_validation_unknown"
    assert raw_status not in str(events[-1].details)
    event_count = len(events)
    db.refresh_engineering_validation_request_status(validation.id)
    assert len(_worker_validation_events(db, fixture["task_id"])) == event_count

    # A corrupt/future derived row that is already semantically unknown is
    # normalized without fabricating a second transition.
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE engineering_validation_requests
            SET status = ?, result_status = ?
            WHERE id = ?
            """,
            (raw_status, raw_status, validation.id),
        )
    normalized = db.refresh_engineering_validation_request_status(validation.id)
    assert normalized.status == "unknown"
    assert normalized.result_status == "unknown"
    assert len(_worker_validation_events(db, fixture["task_id"])) == event_count

    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=SimpleNamespace(local_home_dir=str(tmp_path)),
            server_configs={},
            server_states={},
        ),
    )
    projection = main_module._engineering_validation_request_to_dict(normalized)
    assert projection["status"] == "unknown"
    assert projection["result"] == {
        "status": "unknown",
        "exit_code": None,
        "finished_at": None,
    }
    assert projection["connection"] == {
        "code": "unknown",
        "reason": "job_status_unrecognized",
    }
    assert raw_status not in str(projection)


def test_task_detail_combines_safe_sorted_deduplicated_approval_history(
    db, tmp_path, audit_path, monkeypatch
):
    import app.main as main_module

    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    _approve_validation(db, approval.id, tmp_path, audit_path, target)
    requester = db.insert_actor(
        actor_id="validation-requester",
        actor_type="human",
        display_name="Validation Requester",
        email="requester@example.invalid",
    )
    approver = db.insert_actor(
        actor_id="validation-approver",
        actor_type="human",
        display_name="Validation Approver",
        email="approver@example.invalid",
    )
    db.update_approval(
        fixture["approval_id"],
        requester_actor_id=requester.id,
        decision_actor_id=approver.id,
        decision_mechanism="manual",
    )
    db.update_approval(
        validation.approval_id,
        requester_actor_id=requester.id,
        decision_actor_id=approver.id,
        decision_mechanism="manual",
    )

    approvals = db.list_engineering_task_approvals(fixture["task_id"])
    assert [item.id for item in approvals] == [
        fixture["approval_id"],
        validation.approval_id,
    ]
    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=AppConfig(
                servers=[target],
                engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
                local_home_dir=str(tmp_path),
            ),
            server_configs={target.name: target},
            server_states={},
        ),
    )
    history = main_module._engineering_approval_history(
        [approvals[1], approvals[0], approvals[1]]
    )
    detail = main_module._build_engineering_task_detail(fixture["task_id"])

    assert detail["approval_history"] == history
    assert [item["approval_id"] for item in history] == [
        fixture["approval_id"],
        validation.approval_id,
    ]
    assert all(
        set(item)
        == {
            "approval_id",
            "kind",
            "status",
            "created_at",
            "decided_at",
            "requester",
            "approver",
            "decision_mechanism",
        }
        for item in history
    )
    assert detail["requester"]["id"] == requester.id
    assert detail["approver"]["id"] == approver.id
    encoded = str(history)
    assert "requester@example.invalid" not in encoded
    assert "approver@example.invalid" not in encoded
    assert "python -m pytest -q" not in encoded
    assert target.key_path not in encoded
    assert "payload" not in encoded
    assert detail["available_actions"]["request_changes"] == {
        "enabled": False,
        "reason": "此動作需要後續受控執行切片",
    }
    download_bundle = detail["available_actions"]["download_bundle"]
    assert download_bundle["enabled"] is False
    assert "raw bundle" in download_bundle["reason"]
    assert "未經去敏" in download_bundle["reason"]
    assert "withheld" in download_bundle["reason"]


def test_approval_fails_closed_when_generated_push_command_changes(
    db, tmp_path, audit_path
):
    original_target = _target(key="/tmp/key-a")
    _fixture, validation, approval = _request(
        db, tmp_path, audit_path, original_target
    )
    rotated_command_target = _target(key="/tmp/key-b")

    with pytest.raises(
        InvalidEngineeringValidationRequestError,
        match="generated command contract",
    ):
        _approve_validation(
            db,
            approval.id,
            tmp_path,
            audit_path,
            rotated_command_target,
        )

    assert db.get_approval(approval.id).status == "pending"
    assert db.get_engineering_validation_request(validation.id).status == (
        "pending_approval"
    )
    assert not [
        job
        for job in db.list_jobs()
        if job.engineering_validation_request_id == validation.id
    ]


def test_approval_fails_closed_when_parent_approval_payload_drifts(
    db, tmp_path, audit_path
):
    target = _target()
    fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    parent = db.get_approval(fixture["approval_id"])
    changed_payload = dict(parent.payload)
    changed_payload["instruction"] = "drifted after approval"
    db.update_approval(parent.id, payload=changed_payload)

    with pytest.raises(
        InvalidEngineeringValidationRequestError,
        match="Engineering Task/Run/ProjectVersion contract",
    ):
        _approve_validation(db, approval.id, tmp_path, audit_path, target)

    assert db.get_approval(approval.id).status == "pending"
    assert db.get_engineering_validation_request(validation.id).status == (
        "pending_approval"
    )


@pytest.mark.parametrize("drift", ["command", "target"])
def test_scheduler_refuses_drifted_validation_and_does_not_starve_legacy_job(
    db, tmp_path, audit_path, drift
):
    target = _target()
    _fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    push = db.get_job(result["bundle_push_job_id"])
    downstream = result["job"]
    db.update_job(push.id, status="done", server="_local", finished_at=now_iso())
    if drift == "command":
        db.update_job(downstream.id, command="python -m changed-after-approval")
        current_target = target
    else:
        current_target = _target(host="198.51.100.99")
    legacy = enqueue_job(
        db,
        command="echo legacy-still-runs",
        type="adhoc",
        pin_server="worker-a",
        audit_path=audit_path,
    )
    calls: list[tuple] = []

    async def executor(server, command, timeout):
        calls.append((server, command, timeout))
        return _Result()

    async def write_file(server, path, content):
        calls.append((server, path, content))

    asyncio.run(
        scheduler_tick(
            db,
            {
                "worker-a": ServerState(
                    name="worker-a", online=True, load1=0.1
                )
            },
            {"worker-a": current_target},
            executor,
            write_file,
            audit_path=audit_path,
            local_home_dir=str(tmp_path),
        )
    )

    assert db.get_job(downstream.id).status == "queued"
    assert db.get_job(legacy.id).status == "running"
    assert any(call[2] == "echo legacy-still-runs" for call in calls)
    assert not any("changed-after-approval" in str(call) for call in calls)


def test_local_bundle_push_is_gated_before_local_executor_contact(
    db, tmp_path, audit_path
):
    target = _target()
    _fixture, _validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    push = db.get_job(result["bundle_push_job_id"])
    db.update_job(push.id, command="echo drifted-local-push")
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("drifted validation push reached local executor")

    asyncio.run(
        scheduler_tick(
            db,
            {},
            {"worker-a": target},
            must_not_contact,
            must_not_contact,
            audit_path=audit_path,
            local_home_dir=str(tmp_path),
        )
    )

    assert calls == []
    assert db.get_job(push.id).status == "queued"


@pytest.mark.parametrize("job_role", ["push", "downstream"])
def test_reconcile_and_stall_probes_refuse_drifted_validation_contract(
    db, tmp_path, audit_path, job_role
):
    target = _target()
    _fixture, _validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    push = db.get_job(result["bundle_push_job_id"])
    downstream = result["job"]
    if job_role == "push":
        selected = push
        db.update_job(selected.id, status="running", server="_local")
    else:
        db.update_job(push.id, status="done", server="_local", finished_at=now_iso())
        selected = downstream
        db.update_job(selected.id, status="running", server="worker-a")
    db.update_job(selected.id, command="echo drifted-after-dispatch")
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("drifted validation reached reconcile or stall probe")

    asyncio.run(
        scheduler_tick(
            db,
            {
                "worker-a": ServerState(
                    name="worker-a", online=True, load1=0.1
                )
            },
            {"worker-a": target},
            must_not_contact,
            must_not_contact,
            audit_path=audit_path,
            local_home_dir=str(tmp_path),
        )
    )

    assert calls == []
    assert db.get_job(selected.id).status == "running"


def test_result_collection_refuses_drifted_validation_without_transport(
    db, tmp_path, audit_path
):
    target = _target()
    _fixture, _validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    push = db.get_job(result["bundle_push_job_id"])
    downstream = result["job"]
    db.update_job(push.id, status="done", server="_local", finished_at=now_iso())
    db.update_job(
        downstream.id,
        status="done",
        server="worker-a",
        command="python -m changed-before-result-pull",
        finished_at=now_iso(),
        exit_code=0,
    )
    calls: list[str] = []

    async def must_not_pull(*_args):
        calls.append("pull")
        raise AssertionError("drifted validation attempted result transport")

    async def send_mail(*_args):
        calls.append("mail")
        return True

    async def summarize(*_args):
        return None

    config = AppConfig(
        servers=[target],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=60,
        smtp_host="smtp.example.invalid",
        smtp_port=587,
        mail_from="from@example.invalid",
        mail_to="to@example.invalid",
    )
    asyncio.run(
        handle_job_finished(
            db.get_job(downstream.id),
            server_cfg=target,
            local_run=must_not_pull,
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=send_mail,
            summarize_mail=summarize,
        )
    )

    assert calls == ["mail"]


def test_approved_stop_refuses_drifted_validation_without_remote_contact(
    db, tmp_path, audit_path
):
    target = _target()
    _fixture, validation, approval = _request(db, tmp_path, audit_path, target)
    result = _approve_validation(db, approval.id, tmp_path, audit_path, target)
    push = db.get_job(result["bundle_push_job_id"])
    downstream = result["job"]
    db.update_job(push.id, status="done", server="_local", finished_at=now_iso())
    db.update_job(downstream.id, status="running", server="worker-a")
    db.update_job(downstream.id, command="python -m changed-after-approval")
    stop = request_stop_approval(db, downstream.id, audit_path=audit_path)
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("drifted validation stop reached worker")

    stopped = asyncio.run(
        approve(
            db,
            stop.id,
            ssh_run=must_not_contact,
            server_configs={"worker-a": target},
            app_state=_app_state(tmp_path),
            audit_path=audit_path,
        )
    )

    assert calls == []
    assert stopped["job"].status == "running"
    assert "validation_job_contract_mismatch" in stopped["approval"].note
    assert db.refresh_engineering_validation_request_status(validation.id).status == (
        "running"
    )


def test_native_api_is_always_pending_and_job_surfaces_withhold_executor_details(
    api_client, tmp_path
):
    client, main_module = api_client
    target = _target()
    main_module.app_state.config = replace(
        main_module.app_state.config,
        engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
        servers=[target],
        local_home_dir=str(tmp_path),
    )
    main_module.app_state.server_configs = {target.name: target}
    fixture = _eligible_task(main_module.app_state.db, tmp_path)
    before_job_ids = {job.id for job in main_module.app_state.db.list_jobs()}

    requested = client.post(
        f"/engineering-tasks/{fixture['task_id']}/worker-validation-request",
        json={
            "command": "python -m pytest -q",
            "pin_server": "worker-a",
            "priority": "normal",
            "require_tag": "validation",
        },
    )
    assert requested.status_code == 200
    body = requested.json()
    assert body["approval"]["kind"] == "enqueue"
    assert body["approval"]["status"] == "pending"
    assert body["validation_request"]["status"] == "pending_approval"
    assert {job.id for job in main_module.app_state.db.list_jobs()} == before_job_ids

    detail = client.get(f"/engineering-tasks/{fixture['task_id']}")
    assert detail.status_code == 200
    action = detail.json()["available_actions"]["request_worker_validation"]
    assert action == {
        "enabled": True,
        "reason": None,
        "coding_run_id": fixture["run_id"],
        "engineering_task_id": fixture["task_id"],
        "request_mode": "native_pending_approval",
    }

    approved = client.post(f"/approve/{body['approval']['id']}")
    assert approved.status_code == 200
    approved_body = approved.json()
    assert approved_body["validation_request_id"] == (
        body["validation_request"]["id"]
    )
    job_id = approved_body["job"]["id"]
    raw_job = main_module.app_state.db.get_job(job_id)
    assert "coding_bundles" in raw_job.command
    projected = client.get(f"/jobs/{job_id}")
    assert projected.status_code == 200
    projected_body = projected.json()
    assert projected_body["command"] == (
        "Run approved Engineering Task worker validation"
    )
    assert projected_body["validation_engineering_task_id"] == fixture["task_id"]
    assert "coding_bundles" not in str(projected_body)
    assert target.key_path not in str(projected_body)
    assert "projects/validation-project" not in str(projected_body)

    main_module.app_state.db.update_job(
        job_id,
        status="failed",
        log_tail="Authorization: Bearer raw-validation-secret",
        exit_code=1,
        finished_at=now_iso(),
    )
    log_response = client.get(f"/jobs/{job_id}/log")
    assert log_response.status_code == 200
    assert "raw-validation-secret" not in str(log_response.json())
    diagnosis = client.post(f"/jobs/{job_id}/diagnose")
    assert diagnosis.status_code == 409


def test_native_validation_api_rejects_automatic_or_local_placement(
    api_client, tmp_path
):
    client, main_module = api_client
    target = _target()
    main_module.app_state.config = replace(
        main_module.app_state.config,
        engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
        servers=[target],
        local_home_dir=str(tmp_path),
    )
    main_module.app_state.server_configs = {target.name: target}
    fixture = _eligible_task(main_module.app_state.db, tmp_path)

    for pin_server in ("", "_local"):
        response = client.post(
            f"/engineering-tasks/{fixture['task_id']}/worker-validation-request",
            json={"command": "python -m pytest -q", "pin_server": pin_server},
        )
        assert response.status_code in {400, 422}
    assert not main_module.app_state.db.list_engineering_validation_requests(
        fixture["task_id"]
    )
