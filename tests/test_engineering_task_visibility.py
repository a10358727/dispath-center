"""Plan v2 Slice 3: AI Engineering Task visibility contracts.

The visibility journal is deliberately presentation-only.  These tests keep
the immutable Approval/Job/CodingRun records authoritative while checking that
the API exposes safe, honest projections for both native and legacy records.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engineering_tasks as engineering_tasks
import app.jobfinish as jobfinish
from app.approvals import CodingRunNotCleanableError, cleanup_coding_run, reject
from app.db import Database
from app.config import AppConfig, ServerConfig
from app.engineering_tasks import (
    inspect_engineering_result_file,
    load_engineering_result_json,
    redact_engineering_text,
)
from app.jobfinish import recover_engineering_task_result
from app.monitor import ServerState
from app.results import local_result_dir
from app.sshpool import CommandResult


BASE_COMMIT = "a" * 40
SYNTHETIC_BEARER = "synthetic-redaction-value-1234567890"
SYNTHETIC_ACCESS_TOKEN = "synthetic-access-value-1234567890"


def _insert_native_task(
    db: Database, *, project: str = "visibility-project"
) -> tuple[str, int, str]:
    project_id = db.insert_project(project, f"https://example.invalid/{project}.git")
    version = db.get_or_create_project_version(project, BASE_COMMIT, git_ref="main")
    task_id = str(uuid.uuid4())
    instruction = "AI Engineering Task\n\nTask objective:\n- Show safe progress"
    approval_payload = {
        "engineering_task_id": task_id,
        "project": project,
        "instruction": instruction,
        "project_version_id": version.id,
        "base_commit": BASE_COMMIT,
    }
    inserted_task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract={
            "runner": {
                "name": "server-a",
                "host": "192.0.2.10",
                "user": "runner",
                "port": 22,
            },
            "workspace_rel": "codex_workspaces",
            "source_kind": "hub_bundle",
            "source": f"engineering_bundles/{task_id}.bundle",
            "network_access": False,
            "dependency_installation": False,
        },
        contract_version="engineering-task-v1",
        structured_request={"objective": "Show safe progress"},
        instruction=instruction,
        detected_metadata={"detection": "path-markers-v1"},
        runner_server="server-a",
        validation_target=None,
        approval_payload=approval_payload,
    )
    assert inserted_task_id == task_id
    return task_id, approval_id, version.id


def _finalize_native_task(
    db: Database,
    *,
    task_id: str,
    approval_id: int,
    project_version_id: str,
    project: str = "visibility-project",
    staging_command: str = "stage --source /home/private/hub",
    coding_command: str = (
        "cd /home/private/worktree && "
        f"AUTHORIZATION='Bearer {SYNTHETIC_BEARER}' codex exec"
    ),
) -> tuple[int, int, int]:
    task = db.get_engineering_task(task_id)
    assert task is not None
    return db.finalize_engineering_task_approval_plan(
        task_id=task_id,
        approval_id=approval_id,
        project=project,
        runner_server="server-a",
        instruction=task.instruction,
        base_commit=BASE_COMMIT,
        project_version_id=project_version_id,
        validation_target=None,
        worktree_path=f"codex_workspaces/tasks/{approval_id}/repo",
        staging_command=staging_command,
        coding_command=coding_command,
        approval_note="approved for visibility test",
        decision_actor_id=None,
        decision_mechanism="manual",
    )


def test_event_journal_is_idempotent_conflict_checked_and_id_ordered(db):
    task_id, _approval_id, _version_id = _insert_native_task(db)

    first = db.append_engineering_task_event(
        task_id=task_id,
        attempt_number=1,
        event_key="test:event:first",
        event_type="validation_started",
        phase="validation",
        state="running",
        source_kind="system",
        source_id="first",
        summary="Validation started",
        details={"check_count": 2},
        occurred_at="2026-07-14T00:02:00+00:00",
    )
    duplicate = db.append_engineering_task_event(
        task_id=task_id,
        attempt_number=1,
        event_key="test:event:first",
        event_type="validation_started",
        phase="validation",
        state="running",
        source_kind="system",
        source_id="first",
        summary="Validation started",
        details={"check_count": 2},
        occurred_at="2026-07-14T00:02:00+00:00",
    )
    second = db.append_engineering_task_event(
        task_id=task_id,
        attempt_number=1,
        event_key="test:event:second",
        event_type="validation_finished",
        phase="validation",
        state="done",
        source_kind="system",
        source_id="second",
        summary="Validation finished",
        details={"exit_code": 0},
        occurred_at="2026-07-14T00:01:00+00:00",
    )

    assert duplicate.id == first.id
    events = db.list_engineering_task_events(task_id)
    assert [event.id for event in events] == sorted(event.id for event in events)
    assert [event.event_key for event in events][-2:] == [
        "test:event:first",
        "test:event:second",
    ]
    assert db.list_engineering_task_events(task_id, after_id=first.id) == [second]

    with pytest.raises(ValueError, match="idempotency conflict"):
        db.append_engineering_task_event(
            task_id=task_id,
            attempt_number=1,
            event_key="test:event:first",
            event_type="validation_started",
            phase="validation",
            state="running",
            source_kind="system",
            source_id="first",
            summary="Conflicting replacement",
            details={"check_count": 2},
            occurred_at="2026-07-14T00:02:00+00:00",
        )


def test_presentation_flags_scan_the_full_journal_and_survive_restart(tmp_path):
    database_path = tmp_path / "presentation-flags.db"
    database = Database(str(database_path))
    try:
        task_id, _approval_id, _version_id = _insert_native_task(database)
        for index in range(105):
            database.append_engineering_task_event(
                task_id=task_id,
                attempt_number=1,
                event_key=f"test:presentation:filler:{index}",
                event_type="progress_observed",
                phase="execution",
                state="running",
                source_kind="system",
                source_id="test",
                summary="Progress observed",
                details={"sequence": index},
            )
        database.append_engineering_task_event(
            task_id=task_id,
            attempt_number=1,
            event_key="test:presentation:interrupted",
            event_type="execution_interrupted",
            phase="execution",
            state="interrupted",
            source_kind="system",
            source_id="test",
            summary="Execution interrupted",
        )
        database.append_engineering_task_event(
            task_id=task_id,
            attempt_number=1,
            event_key="test:presentation:contract-mismatch",
            event_type="runner_contract_mismatch",
            phase="execution",
            state="disconnected",
            source_kind="system",
            source_id="test",
            summary="Runner contract mismatch",
        )

        first_page = database.list_engineering_task_events(task_id, limit=100)
        assert len(first_page) == 100
        assert not {
            "execution_interrupted",
            "runner_contract_mismatch",
        }.intersection(event.event_type for event in first_page)
        assert database.get_engineering_task_presentation_flags(task_id) == {
            "execution_interrupted": True,
            "runner_contract_mismatch": True,
        }
    finally:
        database.close()

    reopened = Database(str(database_path))
    try:
        assert reopened.get_engineering_task_presentation_flags(task_id) == {
            "execution_interrupted": True,
            "runner_contract_mismatch": True,
        }
    finally:
        reopened.close()


def test_detail_and_list_presentation_use_full_journal_safety_flags(
    db, tmp_path, monkeypatch
):
    import app.main as main_module

    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_job(
        staging_job_id,
        status="done",
        exit_code=0,
        finished_at="2026-07-15T09:00:00+00:00",
    )
    db.update_job(coding_job_id, status="queued")
    for index in range(105):
        db.append_engineering_task_event(
            task_id=task_id,
            attempt_number=1,
            event_key=f"test:wiring:filler:{index}",
            event_type="progress_observed",
            phase="execution",
            state="running",
            source_kind="system",
            source_id="test",
            summary="Progress observed",
            details={"sequence": index},
        )
    for event_type, state in (
        ("execution_interrupted", "interrupted"),
        ("runner_contract_mismatch", "disconnected"),
    ):
        db.append_engineering_task_event(
            task_id=task_id,
            attempt_number=1,
            event_key=f"test:wiring:{event_type}",
            event_type=event_type,
            phase="execution",
            state=state,
            source_kind="system",
            source_id="test",
            summary="Safety-significant state observed",
        )

    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=AppConfig(
                servers=[],
                engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
                local_home_dir=str(tmp_path),
            ),
            server_configs={},
            server_states={},
        ),
    )

    task = db.get_engineering_task(task_id)
    approval = db.get_approval(approval_id)
    run = db.get_coding_run(run_id)
    jobs = db.list_engineering_task_jobs(task_id)
    first_page = [
        main_module._engineering_event_to_dict(event)
        for event in db.list_engineering_task_events(task_id, limit=100)
    ]
    interrupted = main_module._engineering_presentation(
        task_data=main_module._engineering_task_to_dict(task),
        approval=approval,
        run=run,
        jobs=jobs,
        events=first_page,
        event_flags={
            "execution_interrupted": True,
            "runner_contract_mismatch": False,
        },
    )
    assert interrupted["execution_health"]["code"] == "interrupted"

    detail = main_module._build_engineering_task_detail(task_id)
    assert len(detail["events"]) == 100
    assert detail["presentation"]["execution_health"]["code"] == "disconnected"
    assert detail["presentation"]["runner_connection"] == {
        "code": "disconnected",
        "label": "Runner execution contract 不一致",
        "observed_at": detail["updated_at"],
        "reason": "runner_contract_mismatch",
    }

    listed = asyncio.run(main_module.list_engineering_tasks_endpoint(limit=50))
    listed_task = next(item for item in listed if item["id"] == task_id)
    assert listed_task["presentation"]["execution_health"]["code"] == (
        "disconnected"
    )

    main_module.app_state.server_configs["server-a"] = ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="/tmp/synthetic-runner-key",
        port=22,
    )
    restored = main_module._build_engineering_task_detail(task_id)
    assert restored["presentation"]["execution_health"]["code"] == "interrupted"
    assert restored["presentation"]["runner_connection"]["reason"] != (
        "runner_contract_mismatch"
    )
    assert any("目前設定已恢復" in warning for warning in restored["warnings"])


def test_unrecognized_task_run_and_owner_job_statuses_fail_closed(
    db, tmp_path, monkeypatch
):
    import app.main as main_module

    raw_status = "future_private_status_value"
    raw_exit_code = "future_private_exit_value"
    raw_timestamp = "future_private_timestamp_value"
    task_id, approval_id, version_id = _insert_native_task(db)
    _run_id, staging_job_id, _coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_job(
        staging_job_id,
        status=raw_status,
        exit_code=raw_exit_code,
        started_at=raw_timestamp,
        finished_at=raw_timestamp,
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE engineering_tasks SET status = ? WHERE id = ?",
            (raw_status, task_id),
        )
    legacy_run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="legacy-status-project",
        runner_server="server-a",
        instruction="Legacy status projection",
        status=raw_status,
    )

    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=AppConfig(
                servers=[],
                engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
                local_home_dir=str(tmp_path),
            ),
            server_configs={},
            server_states={},
        ),
    )
    detail = main_module._build_engineering_task_detail(task_id)
    assert detail["status"] == "unknown"
    assert detail["presentation"]["state"]["code"] == "unknown"
    assert detail["presentation"]["execution_health"]["code"] == "unknown"
    assert raw_status not in str(detail)
    assert raw_exit_code not in str(detail)
    assert raw_timestamp not in str(detail)
    staging_command = detail["commands"][0]
    assert staging_command["execution_location_label"] == "Server A"
    assert staging_command["working_directory_label"]
    assert staging_command["started_at"] is None
    assert staging_command["finished_at"] is None
    assert staging_command["exit_code"] is None

    legacy_run = db.get_coding_run(legacy_run_id)
    legacy = main_module._legacy_coding_run_to_engineering_task(legacy_run)
    assert legacy["status"] == "unknown"
    legacy_presentation = main_module._engineering_presentation(
        task_data=legacy,
        approval=db.get_approval(approval_id),
        run=legacy_run,
        jobs=[],
        events=main_module._legacy_engineering_events(legacy_run),
    )
    assert legacy_presentation["state"]["code"] == "unknown"
    assert raw_status not in str(legacy_presentation)


def test_path_policy_violation_records_a_fixed_safe_terminal_event(db):
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, _coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )

    db.update_coding_run(
        run_id,
        status="path_policy_violation",
        error_message=f"untrusted Runner detail {SYNTHETIC_BEARER}",
        finished_at="2026-07-14T00:03:00+00:00",
    )

    task = db.get_engineering_task(task_id)
    assert task is not None
    assert task.status == "path_policy_violation"
    result_events = [
        event
        for event in db.list_engineering_task_events(task_id)
        if event.event_type == "result_collected"
    ]
    assert len(result_events) == 1
    assert result_events[0].phase == "complete"
    assert result_events[0].state == "path_policy_violation"
    assert result_events[0].summary == "結果因路徑政策違規而拒絕"
    assert SYNTHETIC_BEARER not in result_events[0].summary
    durable = [
        event
        for event in db.list_durable_audit_events(limit=200)
        if event["action"] == "engineering_task_result_recorded"
        and event["resource_id"] == task_id
    ]
    assert len(durable) == 1
    assert durable[0]["result"] == "path_policy_violation"
    assert SYNTHETIC_BEARER not in json.dumps(durable[0])


def test_coding_run_result_and_parent_status_roll_back_on_audit_failure(
    db, monkeypatch
):
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, _coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    before_run = db.get_coding_run(run_id)
    before_task = db.get_engineering_task(task_id)
    assert before_run is not None and before_task is not None

    original = db.append_durable_audit_event_in_transaction

    def fail_result(*args, **kwargs):
        if kwargs.get("action") == "engineering_task_result_recorded":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_result)
    with pytest.raises(RuntimeError, match="audit append fault"):
        db.update_coding_run(run_id, status="failed")

    after_run = db.get_coding_run(run_id)
    after_task = db.get_engineering_task(task_id)
    assert after_run is not None and after_task is not None
    assert after_run.status == before_run.status
    assert after_task.status == before_task.status


def test_commands_store_safe_semantics_digest_and_follow_live_job_status(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    task_id, approval_id, version_id = _insert_native_task(db)
    _run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )

    raw_job = db.get_job(coding_job_id)
    assert raw_job is not None
    commands = db.list_engineering_task_commands(task_id)
    assert len(commands) == 2
    assert commands[1].display_command == "Codex agent turn in isolated worktree"
    assert commands[1].working_directory_label == "Isolated task worktree"
    assert commands[1].command_digest == hashlib.sha256(
        raw_job.command.encode("utf-8")
    ).hexdigest()
    assert not hasattr(commands[1], "command")

    payload = client.get(f"/engineering-tasks/{task_id}/commands")
    assert payload.status_code == 200
    encoded = json.dumps(payload.json())
    assert "/home/private" not in encoded
    assert SYNTHETIC_BEARER not in encoded
    assert payload.json()[1]["status"] == "queued"
    assert payload.json()[1]["status_source"] == "job"

    db.update_job(
        coding_job_id,
        status="running",
        started_at="2026-07-14T01:00:00+00:00",
    )
    running = client.get(f"/engineering-tasks/{task_id}/commands").json()[1]
    assert running["status"] == "running"
    assert running["started_at"] == "2026-07-14T01:00:00+00:00"

    db.update_job(
        coding_job_id,
        status="done",
        finished_at="2026-07-14T01:00:09+00:00",
        exit_code=0,
    )
    done = client.get(f"/engineering-tasks/{task_id}/commands").json()[1]
    assert done["status"] == "done"
    assert done["exit_code"] == 0
    assert done["duration_seconds"] == 9


def test_command_projection_uses_safe_labels_and_status_bounded_timestamps(
    db, tmp_path, monkeypatch
):
    import app.main as main_module

    task_id, approval_id, version_id = _insert_native_task(db)
    _run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=AppConfig(servers=[], local_home_dir=str(tmp_path)),
            server_configs={},
            server_states={},
        ),
    )
    command = db.get_engineering_task_command_by_job_id(coding_job_id)
    assert command is not None

    queued = main_module._engineering_command_to_dict(command)
    assert queued["execution_location_label"] == "Coding Runner"
    assert queued["working_directory_label"] == "Isolated task worktree"
    assert queued["started_at"] is None
    assert queued["finished_at"] is None
    assert queued["exit_code"] is None

    db.update_job(
        coding_job_id,
        status="running",
        started_at="2026-07-14T01:00:00+00:00",
        finished_at="2026-07-14T01:00:09+00:00",
        exit_code=7,
    )
    running = main_module._engineering_command_to_dict(command)
    assert running["started_at"] == "2026-07-14T01:00:00+00:00"
    assert running["finished_at"] is None
    assert running["exit_code"] is None
    assert running["duration_seconds"] is None

    db.update_job(
        coding_job_id,
        status="done",
        finished_at="2026-07-14T01:00:09+00:00",
        exit_code=0,
    )
    done = main_module._engineering_command_to_dict(command)
    assert done["finished_at"] == "2026-07-14T01:00:09+00:00"
    assert done["exit_code"] == 0
    assert done["duration_seconds"] == 9


def test_artifacts_allow_multiple_keys_keep_hashes_and_mark_cleanup(db):
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    first = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff-checkpoint-1",
        kind="diff",
        label="First checkpoint diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    second = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff-checkpoint-2",
        kind="diff",
        label="Second checkpoint diff",
        storage_kind="local_result",
        storage_key="diff-2.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    assert first.id != second.id

    digest = hashlib.sha256(b"checkpoint one").hexdigest()
    collected = db.record_engineering_task_artifact_collection(
        first.id,
        source_sha256=digest,
        source_size_bytes=14,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    assert collected is not None
    assert collected.source_sha256 == digest
    assert collected.collected_at is not None
    with pytest.raises(ValueError, match="digest is immutable"):
        db.record_engineering_task_artifact_collection(
            first.id,
            source_sha256=hashlib.sha256(b"replacement").hexdigest(),
            source_size_bytes=14,
            verification_status="not_required",
            redaction_status="redacted",
            availability="available",
        )

    assert db.mark_engineering_task_artifacts_cleaned(task_id=task_id) == 2
    retained = db.list_engineering_task_artifacts(task_id)
    assert {artifact.artifact_key for artifact in retained} == {
        "diff-checkpoint-1",
        "diff-checkpoint-2",
    }
    assert {artifact.availability for artifact in retained} == {"cleaned"}
    retained_first = next(
        artifact
        for artifact in retained
        if artifact.artifact_key == "diff-checkpoint-1"
    )
    assert retained_first.source_sha256 == digest
    assert retained_first.collected_at == collected.collected_at


def test_generic_reject_path_atomically_syncs_parent_and_event(db, audit_path):
    task_id, approval_id, _version_id = _insert_native_task(db)

    decision = reject(db, approval_id, note="needs revision", audit_path=audit_path)

    assert decision.status == "rejected"
    assert decision.note == "needs revision"
    assert db.get_engineering_task(task_id).status == "rejected"
    events = db.list_engineering_task_events(task_id)
    assert events[-1].event_type == "approval_rejected"
    assert events[-1].state == "rejected"
    assert events[-1].details == {"approval_id": approval_id}


def test_native_detail_and_visibility_endpoints_are_safe_and_complete(
    api_client, tmp_path
):
    client, main_module = api_client
    db = main_module.app_state.db
    main_module.app_state.config.local_home_dir = str(tmp_path)
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff",
        kind="diff",
        label="Code diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    (result_dir / "diff.patch").write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        f"+Authorization: Bearer {SYNTHETIC_BEARER}\n"
        f"+access_token={SYNTHETIC_ACCESS_TOKEN}\n",
        encoding="utf-8",
    )
    (result_dir / "final_message.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nsynthetic material\n"
        "-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    diff_payload = (result_dir / "diff.patch").read_bytes()
    artifact = db.record_engineering_task_artifact_collection(
        artifact.id,
        source_sha256=hashlib.sha256(diff_payload).hexdigest(),
        source_size_bytes=len(diff_payload),
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    assert artifact is not None
    db.update_job(
        coding_job_id,
        log_tail=(
            f"Authorization: Bearer {SYNTHETIC_BEARER}\n"
            f"access_token={SYNTHETIC_ACCESS_TOKEN}\nfinished"
        ),
    )

    response = client.get(f"/engineering-tasks/{task_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["record_kind"] == "engineering_task"
    assert detail["legacy"] is False
    assert detail["base_binding"] == "project_version_pinned"
    assert detail["base_commit"] == BASE_COMMIT
    assert detail["project_version_id"] == version_id
    assert detail["attempts"][0]["attempt_number"] == 1
    assert detail["events"] and detail["events"][0]["origin"] == "journal"
    assert len(detail["commands"]) == 2
    assert detail["artifacts"][0]["id"] == artifact.id
    assert detail["approval_history"][0]["status"] == "approved"
    assert detail["final_response"]["withheld"] is True
    assert detail["final_response"]["content"] is None

    attempts = client.get(f"/engineering-tasks/{task_id}/attempts")
    events = client.get(f"/engineering-tasks/{task_id}/events")
    commands = client.get(f"/engineering-tasks/{task_id}/commands")
    artifacts = client.get(f"/engineering-tasks/{task_id}/artifacts")
    artifact_detail = client.get(
        f"/engineering-tasks/{task_id}/artifacts/{artifact.id}"
    )
    diff = client.get(f"/engineering-tasks/{task_id}/diff")
    log = client.get(
        f"/engineering-tasks/{task_id}/commands/{commands.json()[1]['id']}/log"
    )
    for endpoint in (
        attempts,
        events,
        commands,
        artifacts,
        artifact_detail,
        diff,
        log,
    ):
        assert endpoint.status_code == 200, endpoint.text

    assert attempts.json()[0]["base_commit"] == BASE_COMMIT
    assert all(event["origin"] == "journal" for event in events.json())
    assert artifacts.json()[0]["storage_key"] == "diff.patch"
    assert artifact_detail.json()["sha256"] == hashlib.sha256(diff_payload).hexdigest()
    assert diff.json()["available"] is True
    assert diff.json()["redacted"] is True
    assert "[REDACTED]" in diff.json()["patch"]
    assert SYNTHETIC_BEARER not in diff.text
    assert SYNTHETIC_ACCESS_TOKEN not in diff.text
    assert log.json()["redacted"] is True
    assert "[REDACTED]" in log.json()["content"]
    assert SYNTHETIC_BEARER not in log.text
    assert SYNTHETIC_ACCESS_TOKEN not in log.text


def test_engineering_owned_compatibility_surfaces_never_expose_executor_data(
    api_client, tmp_path
):
    client, main_module = api_client
    db = main_module.app_state.db
    main_module.app_state.config.local_home_dir = str(tmp_path)
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(
        coding_job_id,
        status="failed",
        exit_code=1,
        log_tail=(
            f"Authorization: Bearer {SYNTHETIC_BEARER}\n"
            "/home/private/worktree/task.log\n"
        ),
    )
    db.update_coding_run(
        run_id,
        status="failed",
        test_command="pytest /home/private/worktree/tests",
        error_message=f"access_token={SYNTHETIC_ACCESS_TOKEN}",
    )
    result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    (result_dir / "diff.patch").write_text(
        f"+Authorization: Bearer {SYNTHETIC_BEARER}\n", encoding="utf-8"
    )
    (result_dir / "final_message.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    diff_payload = (result_dir / "diff.patch").read_bytes()
    diff_artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff",
        kind="diff",
        label="Code diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    db.record_engineering_task_artifact_collection(
        diff_artifact.id,
        source_sha256=hashlib.sha256(diff_payload).hexdigest(),
        source_size_bytes=len(diff_payload),
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )

    responses = [
        client.get("/jobs"),
        client.get(f"/jobs/{coding_job_id}"),
        client.get(f"/jobs/{coding_job_id}/log"),
        client.get("/coding-runs"),
        client.get(f"/coding-runs/{run_id}"),
        client.get("/projects/visibility-project/activity"),
    ]
    assert all(response.status_code == 200 for response in responses)
    combined = "\n".join(response.text for response in responses)
    assert SYNTHETIC_BEARER not in combined
    assert SYNTHETIC_ACCESS_TOKEN not in combined
    assert "/home/private" not in combined
    assert "BEGIN PRIVATE KEY" not in combined

    job_detail = responses[1].json()
    assert job_detail["command"] == "Run Codex agent in an isolated worktree"
    assert job_detail["execution_details_withheld"] is True
    assert job_detail["log_redacted"] is True
    assert "[REDACTED]" in job_detail["log_tail"]

    log_detail = responses[2].json()
    assert log_detail["redacted"] is True
    assert "[REDACTED]" in log_detail["log_tail"]

    run_detail = responses[4].json()
    assert run_detail["test_command"] == "Validation command (details withheld)"
    assert run_detail["final_message"] is None
    assert run_detail["result_visibility"]["final_message"]["withheld"] is True
    assert run_detail["result_visibility"]["diff_patch"]["redacted"] is True
    assert "[REDACTED]" in run_detail["diff_patch"]

    # Generic diagnosis would otherwise send raw command/log content to an LLM.
    diagnosis = client.post(f"/jobs/{coding_job_id}/diagnose")
    assert diagnosis.status_code == 409
    assert SYNTHETIC_BEARER not in diagnosis.text


def test_snapshot_artifact_list_ids_are_resolvable_by_detail_endpoint(
    api_client, tmp_path
):
    client, main_module = api_client
    db = main_module.app_state.db
    main_module.app_state.config.local_home_dir = str(tmp_path)
    task_id, approval_id, version_id = _insert_native_task(db)
    _run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    (result_dir / "diff.patch").write_text("+safe change\n", encoding="utf-8")

    listed = client.get(f"/engineering-tasks/{task_id}/artifacts")
    assert listed.status_code == 200
    artifacts = listed.json()
    assert artifacts and artifacts[0]["id"].startswith("snapshot-")
    detail = client.get(
        f"/engineering-tasks/{task_id}/artifacts/{artifacts[0]['id']}"
    )
    assert detail.status_code == 200
    assert detail.json() == artifacts[0]


def test_cleanup_action_is_disabled_while_downstream_job_references_bundle(
    api_client
):
    client, main_module = api_client
    db = main_module.app_state.db
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    cancelled = client.post(f"/jobs/{coding_job_id}/cancel")
    assert cancelled.status_code == 409
    assert "安全取消流程" in cancelled.json()["detail"]
    assert db.get_job(coding_job_id).status == "queued"

    db.update_coding_run(run_id, status="done")
    downstream_id = db.insert_job(
        command="python validate.py",
        type="adhoc",
        status="queued",
        source_coding_run_id=run_id,
    )

    detail = client.get(f"/engineering-tasks/{task_id}").json()
    cleanup = detail["available_actions"]["cleanup"]
    assert cleanup["enabled"] is False
    assert "下游 Job" in cleanup["reason"]

    blocked = client.post(f"/coding-runs/{run_id}/cleanup")
    assert blocked.status_code == 409
    assert "下游 Job" in blocked.json()["detail"]
    assert db.get_job(downstream_id).status == "queued"


def test_cleanup_cannot_be_redirected_by_changed_workspace_config(db, audit_path):
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(coding_job_id, status="done", exit_code=0)
    db.update_coding_run(
        run_id,
        status="failed",
        worktree_path=f"~/codex_workspaces/tasks/{approval_id}",
    )
    calls: list[tuple] = []

    async def must_not_contact(*args):
        calls.append(args)
        raise AssertionError("changed workspace must not receive cleanup")

    config = AppConfig(
        servers=[
            ServerConfig(
                name="server-a",
                host="192.0.2.10",
                user="runner",
                key="/tmp/synthetic-key",
            )
        ],
        codex_runner_server="server-a",
        codex_workspace_root="~/different-workspace",
    )
    with pytest.raises(CodingRunNotCleanableError, match="重新導向清理位置"):
        asyncio.run(
            cleanup_coding_run(
                db,
                run_id,
                ssh_run=must_not_contact,
                config=config,
                audit_path=audit_path,
            )
        )

    assert calls == []
    assert db.get_coding_run(run_id).worktree_path is not None


def test_legacy_detail_is_synthesized_without_visibility_backfill(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("legacy-project", "https://example.invalid/legacy.git")
    approval_id = db.insert_approval(
        "coding_task",
        {"project": "legacy-project", "instruction": "legacy instruction"},
    )
    run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="legacy-project",
        runner_server="server-a",
        instruction="legacy instruction",
        base_commit="b" * 40,
        status="done",
    )
    job_id = db.insert_job(
        type="coding",
        project="legacy-project",
        command="cd /home/private/legacy && codex exec",
        status="done",
        source_coding_run_id=run_id,
    )
    db.update_coding_run(
        run_id,
        job_id=job_id,
        started_at="2026-07-14T02:00:00+00:00",
        finished_at="2026-07-14T02:01:00+00:00",
    )
    legacy_id = f"legacy-coding-run-{run_id}"

    detail_response = client.get(f"/engineering-tasks/{legacy_id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["record_kind"] == "legacy_coding_run"
    assert detail["legacy"] is True
    assert detail["project_version_id"] is None
    assert detail["base_binding"] == "legacy_unpinned"
    assert detail["base_commit"] is None
    assert detail["observed_base_commit"] == "b" * 40
    assert detail["attempts"][0]["source"] == "legacy_snapshot"
    assert detail["attempts"][0]["attempt_number"] is None
    assert all(event["origin"] == "legacy_snapshot" for event in detail["events"])
    assert detail["commands"][0]["display_command"].endswith(
        "(command withheld)"
    )
    assert "/home/private" not in json.dumps(detail["commands"])

    assert client.get(f"/engineering-tasks/{legacy_id}/attempts").status_code == 200
    assert client.get(f"/engineering-tasks/{legacy_id}/events").status_code == 200
    assert client.get(f"/engineering-tasks/{legacy_id}/commands").status_code == 200
    assert client.get(f"/engineering-tasks/{legacy_id}/artifacts").json() == []
    assert db._conn.execute(
        "SELECT COUNT(*) FROM engineering_task_events"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM engineering_task_commands"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM engineering_task_artifacts"
    ).fetchone()[0] == 0


def test_redaction_replaces_tokens_and_withholds_private_keys():
    redacted = redact_engineering_text(
        f"Authorization: Bearer {SYNTHETIC_BEARER}\n"
        f"access_token={SYNTHETIC_ACCESS_TOKEN}\n"
    )
    assert redacted["redacted"] is True
    assert redacted["withheld"] is False
    assert redacted["redaction_count"] == 2
    assert redacted["content"].count("[REDACTED]") == 2
    assert SYNTHETIC_BEARER not in redacted["content"]
    assert SYNTHETIC_ACCESS_TOKEN not in redacted["content"]

    private_key = redact_engineering_text(
        "before\n-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n"
        "-----END OPENSSH PRIVATE KEY-----\nafter"
    )
    assert private_key == {
        "content": None,
        "redacted": True,
        "withheld": True,
        "redaction_count": 1,
        "truncated": False,
        "reason": "private_key_detected",
    }

    for header in (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
    ):
        withheld = redact_engineering_text(f"before\n{header}\nsynthetic\nafter")
        assert withheld["content"] is None
        assert withheld["withheld"] is True
        assert withheld["reason"] == "private_key_detected"


def test_native_artifact_policy_refusal_prevents_independent_diff_reread(db):
    from app.main import _engineering_artifact_preview_refusal

    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_coding_run(run_id, status="path_policy_violation")
    artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff",
        kind="diff",
        label="Rejected diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="rejected",
        redaction_status="withheld",
        availability="rejected",
    )

    refusal = _engineering_artifact_preview_refusal(
        db.get_coding_run(run_id),
        "diff.patch",
        [artifact],
    )

    assert refusal == {
        "available": False,
        "reason": "artifact_policy_withheld",
        "content": None,
        "redacted": False,
        "withheld": True,
        "truncated": False,
    }


def test_native_artifact_visibility_row_must_match_run_and_job(db):
    from app.main import _engineering_artifact_preview_refusal

    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff",
        kind="diff",
        label="Code diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    payload = b"diff"
    artifact = db.record_engineering_task_artifact_collection(
        artifact.id,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    assert artifact is not None
    run = db.get_coding_run(run_id)
    inspected = {
        "available": True,
        "reason": None,
        "storage_key": "diff.patch",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }

    assert _engineering_artifact_preview_refusal(
        run, "diff.patch", [artifact], inspected=inspected
    ) is None
    assert _engineering_artifact_preview_refusal(
        run,
        "final_message.txt",
        [artifact],
    )["reason"] == "artifact_visibility_unverified"


@pytest.mark.parametrize(
    ("artifacts_mode", "descriptor_drift", "expected_reason"),
    (
        ("missing", None, "artifact_visibility_unverified"),
        ("uncollected", None, "artifact_integrity_unverified"),
        ("collected", "digest", "artifact_integrity_unverified"),
        ("collected", "size", "artifact_integrity_unverified"),
    ),
)
def test_native_artifact_preview_fails_closed_without_matching_source_descriptor(
    db, artifacts_mode, descriptor_drift, expected_reason
):
    from app.main import _engineering_artifact_preview_refusal

    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    run = db.get_coding_run(run_id)
    payload = b"canonical diff"
    artifacts = []
    if artifacts_mode != "missing":
        artifact = db.register_engineering_task_artifact(
            task_id=task_id,
            attempt_number=1,
            artifact_key="diff",
            kind="diff",
            label="Code diff",
            storage_kind="local_result",
            storage_key="diff.patch",
            coding_run_id=run_id,
            source_job_id=coding_job_id,
            verification_status="not_required",
            redaction_status="redacted",
            availability="available",
        )
        if artifacts_mode == "collected":
            artifact = db.record_engineering_task_artifact_collection(
                artifact.id,
                source_sha256=hashlib.sha256(payload).hexdigest(),
                source_size_bytes=len(payload),
                verification_status="not_required",
                redaction_status="redacted",
                availability="available",
            )
            assert artifact is not None
        artifacts = [artifact]

    inspected = {
        "available": True,
        "reason": None,
        "storage_key": "diff.patch",
        "sha256": hashlib.sha256(
            b"drifted diff" if descriptor_drift == "digest" else payload
        ).hexdigest(),
        "size_bytes": len(payload) + (1 if descriptor_drift == "size" else 0),
    }

    refusal = _engineering_artifact_preview_refusal(
        run,
        "diff.patch",
        artifacts,
        inspected=inspected,
    )

    assert refusal == {
        "available": False,
        "reason": expected_reason,
        "content": None,
        "redacted": False,
        "withheld": True,
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("current", "expected_reason"),
    (
        (b"+safe\n", None),
        (b"+evil\n", "artifact_integrity_unverified"),
    ),
)
def test_native_result_preview_serves_only_matching_current_file_descriptor(
    db, tmp_path, monkeypatch, current, expected_reason
):
    import app.main as main_module

    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, _staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    original = b"+safe\n"
    result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    diff_path = result_dir / "diff.patch"
    diff_path.write_bytes(original)
    artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff",
        kind="diff",
        label="Code diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    db.record_engineering_task_artifact_collection(
        artifact.id,
        source_sha256=hashlib.sha256(original).hexdigest(),
        source_size_bytes=len(original),
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    diff_path.write_bytes(current)
    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(
            db=db,
            config=SimpleNamespace(local_home_dir=str(tmp_path)),
        ),
    )

    preview = main_module._engineering_result_preview(
        db.get_coding_run(run_id),
        "diff.patch",
    )

    if expected_reason is None:
        assert preview["available"] is True
        assert preview["content"] == "+safe\n"
        assert preview["sha256"] == hashlib.sha256(original).hexdigest()
        assert preview["size_bytes"] == len(original)
    else:
        assert preview == {
            "available": False,
            "reason": expected_reason,
            "content": None,
            "redacted": False,
            "withheld": True,
            "truncated": False,
        }


@pytest.mark.parametrize(
    "unsafe_details",
    (
        {"context": {"access_token": SYNTHETIC_ACCESS_TOKEN}},
        {"context": [{"note": f"Authorization: Bearer {SYNTHETIC_BEARER}"}]},
        {
            "context": {
                "note": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nsynthetic"
            }
        },
        {"context": {"note": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nsynthetic"}},
        {"context": {"note": "/root/private/result.txt"}},
    ),
)
def test_event_journal_rejects_nested_secret_fields_and_values(db, unsafe_details):
    task_id, _approval_id, _version_id = _insert_native_task(db)
    before = len(db.list_engineering_task_events(task_id))

    with pytest.raises(ValueError, match="forbidden field|unsafe content"):
        db.append_engineering_task_event(
            task_id=task_id,
            event_key=f"unsafe:{uuid.uuid4()}",
            event_type="unsafe_test",
            source_kind="system",
            summary="Nested detail safety test",
            details=unsafe_details,
        )

    assert len(db.list_engineering_task_events(task_id)) == before


def test_internal_event_insert_also_validates_nested_details(db):
    task_id, _approval_id, _version_id = _insert_native_task(db)
    before = len(db.list_engineering_task_events(task_id))

    with pytest.raises(ValueError, match="forbidden field"):
        with db.cursor() as cur:
            db._insert_engineering_task_event_cur(
                cur,
                task_id=task_id,
                event_key="unsafe:internal",
                event_type="unsafe_test",
                source_kind="system",
                summary="Internal detail safety test",
                details={"outer": [{"password": "synthetic"}]},
            )

    assert len(db.list_engineering_task_events(task_id)) == before


def test_artifact_inspection_uses_safe_descriptor_and_bounded_read(tmp_path, monkeypatch):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    diff = result_dir / "diff.patch"
    original = b"abc"
    diff.write_bytes(original)

    real_read = engineering_tasks.os.read
    requested: list[int] = []

    def grow_then_read(fd: int, size: int) -> bytes:
        requested.append(size)
        if len(requested) == 1:
            with diff.open("ab") as handle:
                handle.write(b"x" * 1024)
        return real_read(fd, size)

    monkeypatch.setattr(engineering_tasks.os, "read", grow_then_read)
    inspected = inspect_engineering_result_file(
        result_dir=str(result_dir), filename="diff.patch", include_text=True
    )

    assert inspected == {
        "available": False,
        "reason": "source_changed",
        "storage_key": "diff.patch",
    }
    assert sum(requested) <= len(original)


def test_artifact_inspection_rejects_symlinks_and_does_not_read_oversized_source(
    tmp_path, monkeypatch
):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (result_dir / "diff.patch").symlink_to(outside)

    symlink = inspect_engineering_result_file(
        result_dir=str(result_dir), filename="diff.patch", include_text=True
    )
    assert symlink == {
        "available": False,
        "reason": "unsafe_file_type",
        "storage_key": "diff.patch",
    }

    oversized = result_dir / "result.json"
    oversized.write_bytes(b"x" * 5)
    monkeypatch.setattr(engineering_tasks, "ENGINEERING_TASK_SOURCE_FILE_LIMIT", 4)

    def unexpected_read(_fd: int, _size: int) -> bytes:
        raise AssertionError("oversized source must not be read")

    monkeypatch.setattr(engineering_tasks.os, "read", unexpected_read)
    inspected = inspect_engineering_result_file(
        result_dir=str(result_dir), filename="result.json"
    )
    assert inspected == {
        "available": False,
        "reason": "source_too_large",
        "storage_key": "result.json",
        "size_bytes": 5,
    }


def test_json_loader_parses_the_same_bounded_descriptor_bytes(tmp_path):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    payload = {"status": "done", "base_commit": BASE_COMMIT}
    encoded = json.dumps(payload).encode()
    (result_dir / "result.json").write_bytes(encoded)

    inspected, parsed = load_engineering_result_json(result_dir=str(result_dir))

    assert parsed == payload
    assert inspected["available"] is True
    assert inspected["size_bytes"] == len(encoded)
    assert inspected["sha256"] == hashlib.sha256(encoded).hexdigest()

    (result_dir / "result.json").write_text("[]", encoding="utf-8")
    invalid, parsed = load_engineering_result_json(result_dir=str(result_dir))
    assert parsed is None
    assert invalid["available"] is False
    assert invalid["reason"] == "invalid_json"


def test_failed_interrupted_disconnected_and_unknown_remain_distinct(
    api_client
):
    client, main_module = api_client
    db = main_module.app_state.db

    unknown_task, unknown_approval, _unknown_version = _insert_native_task(
        db, project="unknown-project"
    )
    db.update_approval(unknown_approval, status="approved")
    unknown = client.get(f"/engineering-tasks/{unknown_task}").json()
    assert unknown["presentation"]["state"]["code"] == "unknown"
    assert unknown["presentation"]["execution_health"]["code"] == "unknown"

    rejected_task, rejected_approval, _rejected_version = _insert_native_task(
        db, project="rejected-project"
    )
    reject(db, rejected_approval, note="not approved")
    rejected = client.get(f"/engineering-tasks/{rejected_task}").json()
    assert rejected["presentation"]["state"]["code"] == "rejected"
    assert rejected["presentation"]["execution_health"]["code"] == "not_started"

    task_id, approval_id, version_id = _insert_native_task(db)
    _run_id, staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    initial = client.get(f"/engineering-tasks/{task_id}").json()
    assert initial["presentation"]["runner_connection"]["code"] == "unknown"

    db.update_job(
        staging_job_id,
        status="done",
        started_at="2026-07-14T03:00:00+00:00",
        finished_at="2026-07-14T03:00:05+00:00",
        exit_code=0,
    )
    db.update_job(
        coding_job_id,
        status="running",
        started_at="2026-07-14T03:00:06+00:00",
    )
    db.update_job(coding_job_id, status="queued")
    interrupted = client.get(f"/engineering-tasks/{task_id}").json()
    assert interrupted["presentation"]["execution_health"]["code"] == "interrupted"
    assert interrupted["presentation"]["state"]["code"] == "queued"

    main_module.app_state.server_states["server-a"] = ServerState(
        name="server-a",
        online=False,
        updated_at="2026-07-14T03:01:00+00:00",
        error="synthetic disconnect",
    )
    disconnected = client.get(f"/engineering-tasks/{task_id}").json()
    assert disconnected["presentation"]["runner_connection"]["code"] == "disconnected"
    assert disconnected["presentation"]["execution_health"]["code"] == "interrupted"

    db.update_job(coding_job_id, status="blocked")
    blocked = client.get(f"/engineering-tasks/{task_id}").json()
    assert blocked["presentation"]["state"]["code"] == "blocked"
    assert blocked["presentation"]["execution_health"]["code"] == "blocked"

    db.update_job(coding_job_id, status="cancelled")
    cancelled = client.get(f"/engineering-tasks/{task_id}").json()
    assert cancelled["presentation"]["state"]["code"] == "cancelled"
    assert cancelled["presentation"]["execution_health"]["code"] == "cancelled"

    db.update_job(
        coding_job_id,
        status="failed",
        finished_at="2026-07-14T03:02:00+00:00",
        exit_code=1,
    )
    failed = client.get(f"/engineering-tasks/{task_id}").json()
    assert failed["presentation"]["execution_health"]["code"] == "failed"
    assert failed["presentation"]["runner_connection"]["code"] == "disconnected"

    db.update_job(coding_job_id, status="done", exit_code=1)
    run_id = db.get_engineering_task(task_id).coding_run_id
    db.update_coding_run(run_id, status="secret_violation")
    secret_violation = client.get(f"/engineering-tasks/{task_id}").json()
    assert secret_violation["presentation"]["state"]["code"] == "secret_violation"
    assert secret_violation["presentation"]["execution_health"]["code"] == "failed"

    db.update_coding_run(run_id, status="path_policy_violation")
    path_violation = client.get(f"/engineering-tasks/{task_id}").json()
    assert path_violation["presentation"]["state"] == {
        "code": "path_policy_violation",
        "label": "路徑政策拒絕",
    }
    assert path_violation["presentation"]["phase"] == {
        "code": "complete",
        "label": "完成",
    }
    assert path_violation["presentation"]["execution_health"]["code"] == "failed"
    assert path_violation["presentation"]["warnings"] == [
        "Runner result was rejected by the enforced path policy"
    ]
    assert (
        path_violation["available_actions"]["request_worker_validation"]["enabled"]
        is False
    )
    assert (
        path_violation["available_actions"]["request_worker_validation"]["reason"]
        == "需要已驗證的 change bundle"
    )
    assert (
        path_violation["available_actions"]["cleanup"]["reason"]
        != "Coding Run 尚未到達終態"
    )
    assert {
        unknown["presentation"]["execution_health"]["code"],
        interrupted["presentation"]["execution_health"]["code"],
        disconnected["presentation"]["runner_connection"]["code"],
        failed["presentation"]["execution_health"]["code"],
    } == {"unknown", "interrupted", "disconnected", "failed"}


def test_terminal_engineering_result_collection_recovers_idempotently_after_restart(
    db, tmp_path
):
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(
        coding_job_id,
        status="done",
        exit_code=0,
        finished_at="2026-07-14T04:00:00+00:00",
    )
    db.refresh_engineering_task_status_from_jobs(task_id)
    assert db.get_engineering_task(task_id).status == "finalizing"

    calls = 0

    async def recovered_pull(_command, _timeout):
        nonlocal calls
        calls += 1
        result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": "no_changes",
                    "base_commit": BASE_COMMIT,
                    "result_commit": BASE_COMMIT,
                    "result_branch": f"ai-task-{approval_id}",
                    "test_command": None,
                    "test_exit_code": None,
                    "codex_version": "codex-cli 0.144.3",
                }
            ),
            encoding="utf-8",
        )
        return CommandResult(exit_status=0, stdout="", stderr="")

    config = AppConfig(
        servers=[],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=30,
    )
    server = ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="/tmp/synthetic-key",
    )
    job = db.get_job(coding_job_id)
    assert asyncio.run(
        recover_engineering_task_result(
            job,
            server_cfg=server,
            local_run=recovered_pull,
            config=config,
            audit_path=str(tmp_path / "audit.jsonl"),
            db=db,
        )
    ) is True
    assert db.get_coding_run(run_id).status == "no_changes"
    assert db.get_engineering_task(task_id).status == "no_changes"

    # A later recovery sweep observes the terminal CodingRun and does not pull,
    # duplicate events, or alter the immutable result again.
    assert asyncio.run(
        recover_engineering_task_result(
            job,
            server_cfg=server,
            local_run=recovered_pull,
            config=config,
            audit_path=str(tmp_path / "audit.jsonl"),
            db=db,
        )
    ) is True
    assert calls == 1
    result_events = [
        event
        for event in db.list_engineering_task_events(task_id)
        if event.event_type == "result_collected"
    ]
    assert len(result_events) == 1


def test_terminal_recovery_repairs_visibility_without_repulling_results(
    db, tmp_path, monkeypatch, caplog
):
    task_id, approval_id, version_id = _insert_native_task(db)
    run_id, staging_job_id, coding_job_id = _finalize_native_task(
        db,
        task_id=task_id,
        approval_id=approval_id,
        project_version_id=version_id,
    )
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(
        coding_job_id,
        status="done",
        exit_code=0,
        finished_at="2026-07-14T04:30:00+00:00",
    )

    pull_calls = 0

    async def recovered_pull(_command, _timeout):
        nonlocal pull_calls
        pull_calls += 1
        result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": "no_changes",
                    "base_commit": BASE_COMMIT,
                    "result_commit": BASE_COMMIT,
                    "result_branch": f"ai-task-{approval_id}",
                    "test_command": None,
                    "test_exit_code": None,
                    "codex_version": "codex-cli 0.144.3",
                }
            ),
            encoding="utf-8",
        )
        return CommandResult(exit_status=0, stdout="", stderr="")

    original_register = jobfinish._register_engineering_visibility_artifacts
    registration_attempts = 0

    def flaky_register(**kwargs):
        nonlocal registration_attempts
        registration_attempts += 1
        if registration_attempts == 1:
            raise RuntimeError(
                "Bearer synthetic-artifact-secret /home/private/artifact"
            )
        return original_register(**kwargs)

    monkeypatch.setattr(
        jobfinish, "_register_engineering_visibility_artifacts", flaky_register
    )
    caplog.set_level("WARNING", logger="app.jobfinish")
    config = AppConfig(
        servers=[],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=30,
    )
    server = ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="/tmp/synthetic-key",
    )
    job = db.get_job(coding_job_id)

    assert asyncio.run(
        recover_engineering_task_result(
            job,
            server_cfg=server,
            local_run=recovered_pull,
            config=config,
            audit_path=str(tmp_path / "artifact-recovery-audit.jsonl"),
            db=db,
        )
    ) is True
    assert db.get_coding_run(run_id).status == "no_changes"
    assert db.list_engineering_task_artifacts(task_id) == []
    assert "synthetic-artifact-secret" not in caplog.text
    assert "/home/private/artifact" not in caplog.text

    # The next sweep repairs only presentation metadata.  It must not pull the
    # already-canonical result again or alter the task/run terminal status.
    assert asyncio.run(
        recover_engineering_task_result(
            job,
            server_cfg=server,
            local_run=recovered_pull,
            config=config,
            audit_path=str(tmp_path / "artifact-recovery-audit.jsonl"),
            db=db,
        )
    ) is True
    assert pull_calls == 1
    assert registration_attempts == 2
    assert db.get_coding_run(run_id).status == "no_changes"
    assert db.get_engineering_task(task_id).status == "no_changes"
    assert {
        artifact.artifact_key
        for artifact in db.list_engineering_task_artifacts(task_id)
    } == {"result-metadata", "final-response", "diff", "bundle"}


def test_terminal_engineering_recovery_survives_real_database_reopen(tmp_path):
    db_path = tmp_path / "restart-recovery.db"
    database = Database(str(db_path))
    try:
        task_id, approval_id, version_id = _insert_native_task(database)
        run_id, staging_job_id, coding_job_id = _finalize_native_task(
            database,
            task_id=task_id,
            approval_id=approval_id,
            project_version_id=version_id,
        )
        database.update_job(staging_job_id, status="done", exit_code=0)
        database.update_job(
            coding_job_id,
            status="done",
            exit_code=0,
            finished_at="2026-07-14T05:00:00+00:00",
        )
        assert database.refresh_engineering_task_status_from_jobs(task_id) == "finalizing"

        task_before = database.get_engineering_task(task_id)
        approval_before = database.get_approval(approval_id)
        run_before = database.get_coding_run(run_id)
        immutable_task_snapshot = {
            "approval_id": task_before.approval_id,
            "project_id": task_before.project_id,
            "project_name": task_before.project_name,
            "project_version_id": task_before.project_version_id,
            "base_commit": task_before.base_commit,
            "agent_provider_id": task_before.agent_provider_id,
            "provider_capabilities": task_before.provider_capabilities,
            "execution_contract": task_before.execution_contract,
            "contract_version": task_before.contract_version,
            "structured_request": task_before.structured_request,
            "instruction": task_before.instruction,
            "runner_server": task_before.runner_server,
        }
        immutable_approval_payload = json.dumps(
            approval_before.payload, sort_keys=True, separators=(",", ":")
        )
        immutable_run_snapshot = {
            "approval_id": run_before.approval_id,
            "project_version_id": run_before.project_version_id,
            "base_commit": run_before.base_commit,
            "base_binding": run_before.base_binding,
            "runner_server": run_before.runner_server,
            "attempt_number": run_before.attempt_number,
        }
    finally:
        database.close()

    reopened = Database(str(db_path))
    pull_calls = 0

    async def recovered_pull(_command, _timeout):
        nonlocal pull_calls
        pull_calls += 1
        result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": "no_changes",
                    "base_commit": BASE_COMMIT,
                    "result_commit": BASE_COMMIT,
                    "result_branch": f"ai-task-{approval_id}",
                    "test_command": None,
                    "test_exit_code": None,
                    "codex_version": "codex-cli 0.144.3",
                }
            ),
            encoding="utf-8",
        )
        return CommandResult(exit_status=0, stdout="", stderr="")

    config = AppConfig(
        servers=[],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=30,
    )
    approved_runner = ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="/tmp/synthetic-key",
    )
    audit_path = str(tmp_path / "restart-recovery-audit.jsonl")

    try:
        reopened_task = reopened.get_engineering_task(task_id)
        reopened_approval = reopened.get_approval(approval_id)
        reopened_run = reopened.get_coding_run(run_id)
        assert reopened_task.status == "finalizing"
        assert reopened.get_job(coding_job_id).status == "done"
        assert {
            field: getattr(reopened_task, field)
            for field in immutable_task_snapshot
        } == immutable_task_snapshot
        assert json.dumps(
            reopened_approval.payload, sort_keys=True, separators=(",", ":")
        ) == immutable_approval_payload
        assert reopened_approval.status == "approved"
        assert {
            field: getattr(reopened_run, field)
            for field in immutable_run_snapshot
        } == immutable_run_snapshot

        recovered_job = reopened.get_job(coding_job_id)
        assert asyncio.run(
            recover_engineering_task_result(
                recovered_job,
                server_cfg=approved_runner,
                local_run=recovered_pull,
                config=config,
                audit_path=audit_path,
                db=reopened,
            )
        ) is True
        recovered_run = reopened.get_coding_run(run_id)
        assert recovered_run.status == "no_changes"
        assert {
            field: getattr(recovered_run, field)
            for field in immutable_run_snapshot
        } == immutable_run_snapshot
        recovered_task = reopened.get_engineering_task(task_id)
        assert recovered_task.status == "no_changes"
        assert {
            field: getattr(recovered_task, field)
            for field in immutable_task_snapshot
        } == immutable_task_snapshot
        assert json.dumps(
            reopened.get_approval(approval_id).payload,
            sort_keys=True,
            separators=(",", ":"),
        ) == immutable_approval_payload

        result_events = [
            event
            for event in reopened.list_engineering_task_events(task_id)
            if event.event_type == "result_collected"
        ]
        assert len(result_events) == 1
        artifacts = reopened.list_engineering_task_artifacts(task_id)
        assert {artifact.artifact_key for artifact in artifacts} == {
            "result-metadata",
            "final-response",
            "diff",
            "bundle",
        }
        artifact_snapshot = [
            (
                artifact.id,
                artifact.artifact_key,
                artifact.source_sha256,
                artifact.source_size_bytes,
                artifact.verification_status,
                artifact.redaction_status,
                artifact.availability,
                artifact.collected_at,
            )
            for artifact in artifacts
        ]
        result_metadata = next(
            artifact
            for artifact in artifacts
            if artifact.artifact_key == "result-metadata"
        )
        assert result_metadata.source_sha256 is not None
        assert result_metadata.availability == "available"

        # A later sweep after recovery observes the persisted terminal result.
        # It must not pull again or duplicate journal/artifact rows.
        assert asyncio.run(
            recover_engineering_task_result(
                recovered_job,
                server_cfg=approved_runner,
                local_run=recovered_pull,
                config=config,
                audit_path=audit_path,
                db=reopened,
            )
        ) is True
        assert pull_calls == 1
        assert len(
            [
                event
                for event in reopened.list_engineering_task_events(task_id)
                if event.event_type == "result_collected"
            ]
        ) == 1
        assert [
            (
                artifact.id,
                artifact.artifact_key,
                artifact.source_sha256,
                artifact.source_size_bytes,
                artifact.verification_status,
                artifact.redaction_status,
                artifact.availability,
                artifact.collected_at,
            )
            for artifact in reopened.list_engineering_task_artifacts(task_id)
        ] == artifact_snapshot
    finally:
        reopened.close()
