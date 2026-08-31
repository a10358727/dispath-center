"""Phase 1b: retry retires with the job-backed channel; discard stays live.

The old suite created tasks through the retired request path — this one seeds
the same immutable rows directly (the `test_engineering_task_visibility`
recipe) so the surviving discard semantics keep their protection."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.approvals import (
    InvalidEngineeringTaskRequestError,
    approve,
    request_engineering_task_discard_approval,
    request_engineering_task_retry_approval,
)
from app.db import Database

BASE_COMMIT = "a" * 40


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


def _seed_terminal_task(db: Database, *, project: str = "proj1") -> tuple[str, int]:
    project_id = db.insert_project(project, f"https://example.invalid/{project}.git")
    version = db.get_or_create_project_version(project, BASE_COMMIT, git_ref="main")
    task_id = str(uuid.uuid4())
    instruction = "AI Engineering Task\n\nTask objective:\n- retire safely"
    task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract={
            "runner": {"name": "server-a", "host": "192.0.2.10", "user": "runner", "port": 22},
            "workspace_rel": "codex_workspaces",
            "source_kind": "hub_bundle",
            "source": f"engineering_bundles/{task_id}.bundle",
            "network_access": False,
            "dependency_installation": False,
        },
        contract_version="engineering-task-v1",
        structured_request={"objective": "retire safely"},
        instruction=instruction,
        detected_metadata={"detection": "path-markers-v1"},
        runner_server="server-a",
        validation_target=None,
        approval_payload={
            "engineering_task_id": task_id,
            "project": project,
            "instruction": instruction,
            "project_version_id": version.id,
            "base_commit": BASE_COMMIT,
        },
    )
    run_id, staging_job_id, coding_job_id = db.finalize_engineering_task_approval_plan(
        task_id=task_id,
        approval_id=approval_id,
        project=project,
        runner_server="server-a",
        instruction=instruction,
        base_commit=BASE_COMMIT,
        project_version_id=version.id,
        validation_target=None,
        worktree_path=f"codex_workspaces/tasks/{approval_id}/repo",
        staging_command="stage --source /srv/hub",
        coding_command="cd /srv/worktree && echo retired",
        approval_note="seeded",
        decision_actor_id=None,
        decision_mechanism="manual",
    )
    db.update_job(staging_job_id, status="done", server="_local", exit_code=0)
    db.update_job(coding_job_id, status="done", server="server-a", exit_code=1)
    db.update_coding_run(run_id, status="failed")
    task = db.get_engineering_task(task_id)
    assert task is not None and task.status in ("failed", "done")
    return task_id, approval_id


def test_retry_request_is_retired_even_for_a_terminal_task(db):
    task_id, _approval_id = _seed_terminal_task(db)
    with pytest.raises(InvalidEngineeringTaskRequestError) as excinfo:
        request_engineering_task_retry_approval(db, task_id)
    assert "已退役" in str(excinfo.value) and "Studio" in str(excinfo.value)


def test_pending_retry_card_is_rejected_honestly_at_decision_time(db, tmp_path):
    approval_id = db.insert_approval(kind="engineering_task_retry", payload={"engineering_task_id": "x"})
    result = asyncio.run(approve(db, approval_id, audit_path=str(tmp_path / "a.jsonl")))
    assert result["approval"].status == "rejected"
    assert "已退役" in (result["approval"].note or "")


def test_discard_request_and_approve_still_work_end_to_end(db, tmp_path):
    task_id, _approval_id = _seed_terminal_task(db)
    approval = request_engineering_task_discard_approval(
        db, task_id, audit_path=str(tmp_path / "a.jsonl")
    )
    assert approval.status == "pending"
    result = asyncio.run(approve(db, approval.id, audit_path=str(tmp_path / "a.jsonl")))
    assert result["approval"].status == "approved"
    task = db.get_engineering_task(task_id)
    assert task is not None and task.status == "discarded"


def test_discard_request_still_rejects_non_terminal_tasks(db, tmp_path):
    project_id = db.insert_project("proj2", "https://example.invalid/proj2.git")
    version = db.get_or_create_project_version("proj2", BASE_COMMIT, git_ref="main")
    fresh_task_id = str(uuid.uuid4())
    task_id, _approval_id = db.insert_engineering_task_request(
        task_id=fresh_task_id,
        project_id=project_id,
        project_name="proj2",
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract={"workspace_rel": "codex_workspaces", "source_kind": "hub_bundle", "source": "engineering_bundles/x.bundle", "network_access": False, "dependency_installation": False, "runner": {"name": "server-a", "host": "192.0.2.10", "user": "runner", "port": 22}},
        contract_version="engineering-task-v1",
        structured_request={"objective": "x"},
        instruction="AI Engineering Task\n\nTask objective:\n- x",
        detected_metadata={},
        runner_server="server-a",
        validation_target=None,
        approval_payload={"engineering_task_id": fresh_task_id, "project": "proj2"},
    )
    with pytest.raises(InvalidEngineeringTaskRequestError):
        request_engineering_task_discard_approval(db, task_id, audit_path=str(tmp_path / "a.jsonl"))
