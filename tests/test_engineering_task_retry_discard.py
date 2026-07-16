"""Deterministic-layer coverage for the D3 retry/discard slice.

Covers ``app.db.Database.finalize_engineering_task_retry_plan`` (the atomic
attempt-N plan builder) and the three ``app.jobqueue`` executor contract
gates now that they accept any attempt number instead of pinning attempt 1.
Approval-kind wiring, the ``approve()`` dispatch branches and the HTTP
endpoints are covered separately in ``tests/test_approvals.py``-style
integration tests; this file stays at the DB/jobqueue boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from app.config import ServerConfig
from app.datasets import LOCAL_SERVER
from app.db import Database
from app.engineering_path_policy import build_engineering_path_policy
from app.jobqueue import (
    engineering_coding_job_runner_contract_matches,
    engineering_staging_job_contract_matches,
    engineering_staging_job_runner_contract_matches,
)


BASE_COMMIT = "a" * 40


def _server_cfg() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="~/.ssh/id_rsa",
        port=22,
        enabled=True,
    )


def _create_attempt_one(db: Database) -> dict:
    """Build one native task through attempt 1, matching the pattern used in
    ``tests/test_engineering_patch_download.py``."""

    project = f"retry-project-{uuid.uuid4().hex[:8]}"
    project_id = db.insert_project(project, f"https://example.invalid/{project}.git")
    version = db.get_or_create_project_version(project, BASE_COMMIT, git_ref="main")
    task_id = str(uuid.uuid4())
    source = f"engineering_bundles/{task_id}.bundle"
    path_policy, path_policy_sha256 = build_engineering_path_policy(["app/"], [])
    execution_contract = {
        "runner": {
            "name": "server-a",
            "host": "192.0.2.10",
            "user": "runner",
            "port": 22,
        },
        "workspace_rel": "codex_workspaces",
        "source_kind": "hub_bundle",
        "source": source,
        "network_access": False,
        "dependency_installation": False,
        "path_policy": path_policy,
        "path_policy_sha256": path_policy_sha256,
        "path_verifier": path_policy["verifier"],
    }
    provider_capabilities = {"provider_id": "codex", "adapter": "codex-exec-v1"}
    structured_request = {
        "objective": "Produce a retryable attempt",
        "allowed_paths": ["app/"],
        "prohibited_paths": [],
    }
    instruction = "AI Engineering Task\n\nTask objective:\n- Produce a retryable attempt"
    detected_metadata = {"detection": "path-markers-v1", "languages": ["python"]}
    approval_payload = {
        "contract_version": "engineering-task-v2",
        "engineering_task_id": task_id,
        "project": project,
        "project_id": project_id,
        "project_version_id": version.id,
        "base_commit": BASE_COMMIT,
        "agent_provider_id": "codex",
        "provider_capabilities": provider_capabilities,
        "execution_contract": execution_contract,
        "structured_request": structured_request,
        "instruction": instruction,
        "detected_metadata": detected_metadata,
        "validation_target": None,
        "runner_server": "server-a",
        "source_kind": "hub_bundle",
        "source": source,
        "network_access": False,
        "dependency_installation": False,
    }
    inserted_task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities=provider_capabilities,
        execution_contract=execution_contract,
        contract_version="engineering-task-v2",
        structured_request=structured_request,
        instruction=instruction,
        detected_metadata=detected_metadata,
        runner_server="server-a",
        validation_target=None,
        approval_payload=approval_payload,
    )
    assert inserted_task_id == task_id
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
        staging_command="stage immutable input",
        coding_command="run reviewed codex adapter",
        approval_note="approved for retry/discard test",
        decision_actor_id=None,
        decision_mechanism="manual",
    )
    return {
        "project": project,
        "project_id": project_id,
        "version": version,
        "task_id": task_id,
        "run_id": run_id,
        "staging_job_id": staging_job_id,
        "coding_job_id": coding_job_id,
        "instruction": instruction,
        "worktree_path": f"codex_workspaces/tasks/{approval_id}/repo",
    }


def _mark_attempt_one_failed(db: Database, ctx: dict) -> None:
    db.update_job(ctx["staging_job_id"], status="done", server=LOCAL_SERVER, exit_code=0)
    db.update_job(ctx["coding_job_id"], status="done", server="server-a", exit_code=1)
    db.update_coding_run(ctx["run_id"], status="failed")


def _dispatch_stamp(db: Database, staging_job_id: int, coding_job_id: int) -> None:
    """Stamp ``job.server`` the way the real scheduler would at dispatch time.

    The runner-contract gates intentionally check the job's *actual* server
    assignment (populated once dispatch begins), not just ``pin_server``
    (the pre-dispatch target); a freshly queued job has ``server is None``.
    """

    db.update_job(staging_job_id, server=LOCAL_SERVER)
    db.update_job(coding_job_id, server="server-a")


def _create_retry_approval(db: Database, ctx: dict, *, attempt_number: int = 2) -> int:
    payload = {
        "engineering_task_id": ctx["task_id"],
        "attempt_number": attempt_number,
        "project": ctx["project"],
        "project_id": ctx["project_id"],
        "project_version_id": ctx["version"].id,
        "base_commit": BASE_COMMIT,
    }
    return db.insert_approval("engineering_task_retry", payload)


# ---------------------------------------------------------------------------
# finalize_engineering_task_retry_plan()
# ---------------------------------------------------------------------------


def test_retry_plan_creates_attempt_two_rows(db: Database):
    ctx = _create_attempt_one(db)
    _mark_attempt_one_failed(db, ctx)
    approval_id = _create_retry_approval(db, ctx)

    run_id, staging_job_id, coding_job_id = db.finalize_engineering_task_retry_plan(
        task_id=ctx["task_id"],
        approval_id=approval_id,
        attempt_number=2,
        project=ctx["project"],
        runner_server="server-a",
        instruction=ctx["instruction"],
        base_commit=BASE_COMMIT,
        project_version_id=ctx["version"].id,
        validation_target=None,
        worktree_path=ctx["worktree_path"],
        staging_command="stage immutable input attempt 2",
        coding_command="run reviewed codex adapter attempt 2",
        approval_note=None,
        decision_actor_id=None,
        decision_mechanism="manual",
    )

    assert run_id != ctx["run_id"]
    run = db.get_coding_run(run_id)
    assert run.attempt_number == 2
    assert run.engineering_task_id == ctx["task_id"]

    staging_job = db.get_job(staging_job_id)
    coding_job = db.get_job(coding_job_id)
    assert staging_job.engineering_attempt_number == 2
    assert coding_job.engineering_attempt_number == 2
    assert coding_job.depends_on == [staging_job_id]

    task = db.get_engineering_task(ctx["task_id"])
    assert task.coding_run_id == run_id
    assert task.status == "queued"

    approval = db.get_approval(approval_id)
    assert approval.status == "approved"

    attempts = db.list_engineering_task_attempt_runs(ctx["task_id"])
    assert [a.attempt_number for a in attempts] == [1, 2]

    commands = db.list_engineering_task_commands(ctx["task_id"], attempt_number=2)
    assert {c.command_role for c in commands} == {"staging", "agent_turn"}
    assert all(c.approval_id == approval_id for c in commands)


def test_retry_plan_rejects_non_pending_approval(db: Database):
    ctx = _create_attempt_one(db)
    _mark_attempt_one_failed(db, ctx)
    approval_id = _create_retry_approval(db, ctx)
    db.update_approval(approval_id, status="approved", decided_at="2026-07-16T00:00:00+00:00")

    with pytest.raises(ValueError, match="not pending"):
        db.finalize_engineering_task_retry_plan(
            task_id=ctx["task_id"],
            approval_id=approval_id,
            attempt_number=2,
            project=ctx["project"],
            runner_server="server-a",
            instruction=ctx["instruction"],
            base_commit=BASE_COMMIT,
            project_version_id=ctx["version"].id,
            validation_target=None,
            worktree_path=ctx["worktree_path"],
            staging_command="stage",
            coding_command="run",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="manual",
        )


def test_retry_plan_rejects_wrong_approval_kind(db: Database):
    ctx = _create_attempt_one(db)
    _mark_attempt_one_failed(db, ctx)
    approval_id = db.insert_approval("enqueue", {"unrelated": True})

    with pytest.raises(ValueError, match="not pending"):
        db.finalize_engineering_task_retry_plan(
            task_id=ctx["task_id"],
            approval_id=approval_id,
            attempt_number=2,
            project=ctx["project"],
            runner_server="server-a",
            instruction=ctx["instruction"],
            base_commit=BASE_COMMIT,
            project_version_id=ctx["version"].id,
            validation_target=None,
            worktree_path=ctx["worktree_path"],
            staging_command="stage",
            coding_command="run",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="manual",
        )


def test_retry_plan_rejects_non_terminal_task_status(db: Database):
    ctx = _create_attempt_one(db)
    # Attempt 1 left at "queued" (not terminal) — no _mark_attempt_one_failed.
    approval_id = _create_retry_approval(db, ctx)

    with pytest.raises(ValueError, match="not finalizable"):
        db.finalize_engineering_task_retry_plan(
            task_id=ctx["task_id"],
            approval_id=approval_id,
            attempt_number=2,
            project=ctx["project"],
            runner_server="server-a",
            instruction=ctx["instruction"],
            base_commit=BASE_COMMIT,
            project_version_id=ctx["version"].id,
            validation_target=None,
            worktree_path=ctx["worktree_path"],
            staging_command="stage",
            coding_command="run",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="manual",
        )


def test_retry_plan_rejects_duplicate_attempt_number(db: Database):
    ctx = _create_attempt_one(db)
    _mark_attempt_one_failed(db, ctx)
    approval_id = _create_retry_approval(db, ctx, attempt_number=1)

    with pytest.raises(ValueError, match="attempt_number >= 2"):
        db.finalize_engineering_task_retry_plan(
            task_id=ctx["task_id"],
            approval_id=approval_id,
            attempt_number=1,
            project=ctx["project"],
            runner_server="server-a",
            instruction=ctx["instruction"],
            base_commit=BASE_COMMIT,
            project_version_id=ctx["version"].id,
            validation_target=None,
            worktree_path=ctx["worktree_path"],
            staging_command="stage",
            coding_command="run",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="manual",
        )


def test_retry_plan_rejects_second_call_for_same_attempt(db: Database):
    ctx = _create_attempt_one(db)
    _mark_attempt_one_failed(db, ctx)
    approval_id = _create_retry_approval(db, ctx)
    db.finalize_engineering_task_retry_plan(
        task_id=ctx["task_id"],
        approval_id=approval_id,
        attempt_number=2,
        project=ctx["project"],
        runner_server="server-a",
        instruction=ctx["instruction"],
        base_commit=BASE_COMMIT,
        project_version_id=ctx["version"].id,
        validation_target=None,
        worktree_path=ctx["worktree_path"],
        staging_command="stage",
        coding_command="run",
        approval_note=None,
        decision_actor_id=None,
        decision_mechanism="manual",
    )
    # Bring the task back to a terminal state (as if attempt 2 also finished)
    # so a stale/duplicate approval for the *same* attempt number reaches the
    # per-attempt row guard instead of being blocked earlier by the
    # non-terminal-status check.
    attempt_two_run = db.list_engineering_task_attempt_runs(ctx["task_id"])[-1]
    db.update_coding_run(attempt_two_run.id, status="failed")

    second_approval_id = _create_retry_approval(db, ctx, attempt_number=2)
    with pytest.raises(ValueError, match="already has this attempt"):
        db.finalize_engineering_task_retry_plan(
            task_id=ctx["task_id"],
            approval_id=second_approval_id,
            attempt_number=2,
            project=ctx["project"],
            runner_server="server-a",
            instruction=ctx["instruction"],
            base_commit=BASE_COMMIT,
            project_version_id=ctx["version"].id,
            validation_target=None,
            worktree_path=ctx["worktree_path"],
            staging_command="stage",
            coding_command="run",
            approval_note=None,
            decision_actor_id=None,
            decision_mechanism="manual",
        )


# ---------------------------------------------------------------------------
# jobqueue executor contract gates now accept any attempt number
# ---------------------------------------------------------------------------


def _finalize_retry(db: Database, ctx: dict) -> dict:
    _mark_attempt_one_failed(db, ctx)
    approval_id = _create_retry_approval(db, ctx)
    run_id, staging_job_id, coding_job_id = db.finalize_engineering_task_retry_plan(
        task_id=ctx["task_id"],
        approval_id=approval_id,
        attempt_number=2,
        project=ctx["project"],
        runner_server="server-a",
        instruction=ctx["instruction"],
        base_commit=BASE_COMMIT,
        project_version_id=ctx["version"].id,
        validation_target=None,
        worktree_path=ctx["worktree_path"],
        staging_command="stage attempt 2",
        coding_command="run attempt 2",
        approval_note=None,
        decision_actor_id=None,
        decision_mechanism="manual",
    )
    return {"run_id": run_id, "staging_job_id": staging_job_id, "coding_job_id": coding_job_id}


def test_coding_job_gate_accepts_attempt_one_and_attempt_two(db: Database):
    ctx = _create_attempt_one(db)
    server_cfg = _server_cfg()
    _dispatch_stamp(db, ctx["staging_job_id"], ctx["coding_job_id"])
    attempt_one_job = db.get_job(ctx["coding_job_id"])
    assert engineering_coding_job_runner_contract_matches(db, attempt_one_job, server_cfg)

    attempt_two = _finalize_retry(db, ctx)
    _dispatch_stamp(db, attempt_two["staging_job_id"], attempt_two["coding_job_id"])
    attempt_two_job = db.get_job(attempt_two["coding_job_id"])
    assert engineering_coding_job_runner_contract_matches(db, attempt_two_job, server_cfg)

    # The stale attempt-1 job row still passes its own contract: retrying
    # does not retroactively invalidate a terminal attempt's own record.
    attempt_one_job = db.get_job(ctx["coding_job_id"])
    assert engineering_coding_job_runner_contract_matches(db, attempt_one_job, server_cfg)


def test_coding_job_gate_rejects_attempt_number_journal_mismatch(db: Database):
    ctx = _create_attempt_one(db)
    attempt_two = _finalize_retry(db, ctx)
    _dispatch_stamp(db, attempt_two["staging_job_id"], attempt_two["coding_job_id"])
    job = db.get_job(attempt_two["coding_job_id"])
    # Corrupt the in-memory job's own claimed attempt number without a
    # matching journal row at that number: must fail closed, not fall back.
    tampered = replace(job, engineering_attempt_number=99)
    assert not engineering_coding_job_runner_contract_matches(
        db, tampered, _server_cfg()
    )


def test_staging_job_gates_accept_attempt_one_and_attempt_two(db: Database):
    ctx = _create_attempt_one(db)
    _dispatch_stamp(db, ctx["staging_job_id"], ctx["coding_job_id"])
    attempt_one_job = db.get_job(ctx["staging_job_id"])
    assert engineering_staging_job_contract_matches(db, attempt_one_job)
    assert engineering_staging_job_runner_contract_matches(
        db, attempt_one_job, _server_cfg()
    )

    attempt_two = _finalize_retry(db, ctx)
    _dispatch_stamp(db, attempt_two["staging_job_id"], attempt_two["coding_job_id"])
    attempt_two_job = db.get_job(attempt_two["staging_job_id"])
    assert engineering_staging_job_contract_matches(db, attempt_two_job)
    assert engineering_staging_job_runner_contract_matches(
        db, attempt_two_job, _server_cfg()
    )


def test_staging_job_gate_rejects_attempt_number_journal_mismatch(db: Database):
    ctx = _create_attempt_one(db)
    attempt_two = _finalize_retry(db, ctx)
    _dispatch_stamp(db, attempt_two["staging_job_id"], attempt_two["coding_job_id"])
    job = db.get_job(attempt_two["staging_job_id"])
    tampered = replace(job, engineering_attempt_number=99)
    assert not engineering_staging_job_contract_matches(db, tampered)
    assert not engineering_staging_job_runner_contract_matches(
        db, tampered, _server_cfg()
    )
