"""Pure regression tests for immutable Engineering Task result recovery."""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import app.jobfinish as jobfinish
from app.config import AppConfig, ServerConfig
from app.results import local_result_dir


BASE_COMMIT = "a" * 40


def _finalized_plan(db):
    project = "recovery-safety-project"
    project_id = db.insert_project(
        project, f"https://example.invalid/{project}.git"
    )
    version = db.get_or_create_project_version(project, BASE_COMMIT, git_ref="main")
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
            "runner": {
                "name": "server-a",
                "host": "192.0.2.10",
                "user": "runner",
                "port": 22,
            },
            "workspace_rel": "codex_workspaces",
        },
        contract_version="engineering-task-v1",
        structured_request={"objective": "Recover immutable metadata"},
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
    run_id, staging_job_id, coding_job_id = (
        db.finalize_engineering_task_approval_plan(
            task_id=task_id,
            approval_id=approval_id,
            project=project,
            runner_server="server-a",
            instruction="AI Engineering Task",
            base_commit=BASE_COMMIT,
            project_version_id=version.id,
            validation_target=None,
            worktree_path="~/codex_workspaces/task",
            staging_command="true",
            coding_command="true",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="manual",
        )
    )
    return task_id, run_id, staging_job_id, coding_job_id


def _write_no_changes_result(tmp_path: Path, job_id: int) -> Path:
    result_dir = Path(local_result_dir(job_id, str(tmp_path)))
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "no_changes",
                "base_commit": BASE_COMMIT,
                "result_commit": BASE_COMMIT,
                "result_branch": "ai-task-1",
                "test_command": None,
                "test_exit_code": None,
                "codex_version": "codex-cli 0.144.3",
            }
        ),
        encoding="utf-8",
    )
    return result_dir


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        servers=[],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=30,
    )


def _runner() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="/tmp/synthetic-key",
    )


def test_visibility_event_conflict_cannot_rollback_terminal_result_or_repull(
    db, tmp_path
):
    task_id, run_id, staging_job_id, coding_job_id = _finalized_plan(db)
    finished_at = "2026-07-14T08:00:00+00:00"
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(
        coding_job_id,
        status="done",
        exit_code=0,
        finished_at=finished_at,
    )
    _write_no_changes_result(tmp_path, coding_job_id)
    db.append_engineering_task_event(
        task_id=task_id,
        attempt_number=1,
        event_key=f"coding-run:{run_id}:result:no_changes:{finished_at}",
        event_type="synthetic_conflict",
        phase="complete",
        state="unknown",
        source_kind="system",
        source_id=str(run_id),
        summary="Synthetic conflicting presentation row",
        details={"coding_run_id": run_id},
        occurred_at=finished_at,
    )
    pull_calls = 0

    async def recovered_pull(_command, _timeout):
        nonlocal pull_calls
        pull_calls += 1
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    job = db.get_job(coding_job_id)
    assert asyncio.run(
        jobfinish.recover_engineering_task_result(
            job,
            server_cfg=_runner(),
            local_run=recovered_pull,
            config=_config(tmp_path),
            audit_path=str(tmp_path / "event-conflict-audit.jsonl"),
            db=db,
        )
    ) is True
    assert db.get_coding_run(run_id).status == "no_changes"
    assert db.get_engineering_task(task_id).status == "no_changes"

    assert asyncio.run(
        jobfinish.recover_engineering_task_result(
            job,
            server_cfg=_runner(),
            local_run=recovered_pull,
            config=_config(tmp_path),
            audit_path=str(tmp_path / "event-conflict-audit.jsonl"),
            db=db,
        )
    ) is True
    assert pull_calls == 1


def test_job_visibility_conflict_cannot_rollback_canonical_job_state(db):
    _task_id, _run_id, _staging_job_id, coding_job_id = _finalized_plan(db)
    finished_at = "2026-07-14T08:15:00+00:00"
    db.update_job(
        coding_job_id,
        status="failed",
        exit_code=1,
        finished_at=finished_at,
    )

    # Same timestamp intentionally reuses the idempotency key with different
    # presentation content.  The journal must fail closed without reopening or
    # rolling back the canonical Job transition.
    db.update_job(coding_job_id, status="done", exit_code=0)

    job = db.get_job(coding_job_id)
    command = db.get_engineering_task_command_by_job_id(coding_job_id)
    assert job.status == "done"
    assert job.exit_code == 0
    assert command.recorded_status == "done"
    assert command.recorded_exit_code == 0


def test_terminal_recovery_repairs_partial_collection_and_missing_test_result(
    db, tmp_path
):
    task_id, run_id, staging_job_id, coding_job_id = _finalized_plan(db)
    finished_at = "2026-07-14T08:30:00+00:00"
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(
        coding_job_id,
        status="done",
        exit_code=0,
        finished_at=finished_at,
    )
    result_dir = _write_no_changes_result(tmp_path, coding_job_id)
    db.update_coding_run(
        run_id,
        status="no_changes",
        result_commit=BASE_COMMIT,
        test_command="python3 -m pytest -q",
        finished_at=finished_at,
    )
    run = db.get_coding_run(run_id)
    job = db.get_job(coding_job_id)
    jobfinish._register_engineering_visibility_artifacts(
        db=db,
        coding_run=run,
        job=job,
        fields={
            "status": run.status,
            "bundle_path": run.bundle_path,
            "test_command": run.test_command,
        },
        result_dir=result_dir,
    )
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM engineering_task_artifacts "
            "WHERE engineering_task_id = ? AND artifact_key = 'test-result'",
            (task_id,),
        )
        cur.execute(
            "UPDATE engineering_task_artifacts "
            "SET source_sha256 = NULL, source_size_bytes = NULL, collected_at = NULL "
            "WHERE engineering_task_id = ? AND artifact_key = 'result-metadata'",
            (task_id,),
        )

    async def must_not_pull(_command, _timeout):
        raise AssertionError("terminal metadata repair must not pull results again")

    assert asyncio.run(
        jobfinish.recover_engineering_task_result(
            job,
            server_cfg=None,
            local_run=must_not_pull,
            config=_config(tmp_path),
            audit_path=str(tmp_path / "partial-artifact-audit.jsonl"),
            db=db,
        )
    ) is True
    artifacts = {
        artifact.artifact_key: artifact
        for artifact in db.list_engineering_task_artifacts(task_id)
    }
    assert artifacts["result-metadata"].collected_at is not None
    assert artifacts["result-metadata"].source_sha256 is not None
    assert artifacts["test-result"].availability == "available"


def test_recovery_pull_branch_converges_metrics_v1_collection(db, tmp_path):
    """DG-METRICS-CONTRACT v1: `recover_engineering_task_result()`'s pull
    branch (the crash window where `job.status` is already terminal but
    `coding_runs.status` was never backfilled -- left at its default
    `'queued'` here, unlike the metadata-repair tests above which set a
    terminal `coding_runs.status`) must converge metrics-v1 collection the
    same way `handle_job_finished()` does."""

    task_id, run_id, staging_job_id, coding_job_id = _finalized_plan(db)
    finished_at = "2026-07-14T09:00:00+00:00"
    db.update_job(staging_job_id, status="done", exit_code=0)
    db.update_job(
        coding_job_id, status="done", exit_code=0, finished_at=finished_at
    )
    result_dir = _write_no_changes_result(tmp_path, coding_job_id)
    (result_dir / "metrics.json").write_bytes(b'{"loss": "0.5"}')

    job = db.get_job(coding_job_id)
    config = AppConfig(
        servers=[],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=30,
        metrics_v1_enabled=True,
    )

    async def fake_successful_pull(_command, _timeout):
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    recovered = asyncio.run(
        jobfinish.recover_engineering_task_result(
            job,
            server_cfg=_runner(),
            local_run=fake_successful_pull,
            config=config,
            audit_path=str(tmp_path / "recovery-metrics-audit.jsonl"),
            db=db,
        )
    )

    assert recovered is True
    collection = db.get_run_metrics_collection(coding_job_id)
    assert collection is not None
    assert collection["status"] == "collected"
    metrics = {row["key"]: row for row in db.list_run_metrics(coding_job_id)}
    assert metrics["loss"]["value_type"] == "decimal"


def test_pinned_bundle_verification_rejects_symlink_even_when_target_is_valid(
    tmp_path,
):
    project = "symlink-bundle-project"
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str, cwd: Path = source) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Recovery Safety Test")
    (source / "app.py").write_text("BASE = True\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-qm", "base")
    base_commit = git("rev-parse", "HEAD")

    hub = tmp_path / "git" / f"{project}.git"
    hub.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(hub)],
        text=True,
        capture_output=True,
        check=True,
    )

    git("checkout", "-qb", "task")
    (source / "app.py").write_text(
        "BASE = True\nRESULT = True\n", encoding="utf-8"
    )
    git("add", "app.py")
    git("commit", "-qm", "result")
    result_commit = git("rev-parse", "HEAD")
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    real_bundle = result_dir / "real.bundle"
    git("bundle", "create", str(real_bundle), f"{base_commit}..task")

    assert jobfinish._verify_pinned_result_bundle(
        project=project,
        base_commit=base_commit,
        result_commit=result_commit,
        bundle_path=real_bundle,
        local_home_dir=str(tmp_path),
    ) == (True, "")

    linked_bundle = result_dir / "changes.bundle"
    linked_bundle.symlink_to(real_bundle.name)
    verified, reason = jobfinish._verify_pinned_result_bundle(
        project=project,
        base_commit=base_commit,
        result_commit=result_commit,
        bundle_path=linked_bundle,
        local_home_dir=str(tmp_path),
    )
    assert verified is False
    assert "安全" in reason
