"""Store/decision-transaction tests for Experiment v2 (DG-EXPERIMENT-V1, P2).

All fake/temp SQLite databases -- no real infrastructure, no HTTP layer (P2
adds no routes). Fixtures build a fully-resolvable single-run context exactly
like `tests/test_execution_plan_v2_api.py::_seed_execution_context`, then
reuse the same store-level entry points a future P3 route layer will call.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, Database
from app.execution_plan_v2 import (
    DatasetNoneSelection,
    RunProfileTemplateSelection,
)
from app.experiment_v2 import (
    EXPERIMENT_V2_APPROVAL_KIND,
    ExperimentGuard,
    ExperimentMatrix,
    ExperimentMatrixError,
    MatrixAxis,
)
from app.experiment_v2_store import (
    apply_experiment_v2_decision_in_transaction,
    create_experiment_v2_request_in_transaction,
    get_experiment_v2,
    get_experiment_v2_by_approval,
    list_experiments_v2_for_project,
    reject_experiment_v2_decision_in_transaction,
)
from app.server_attempt_preflight import ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION
from tests.test_project_environments_v1 import _publish_server_evidence
from tests.test_run_templates_v2 import (
    OPERATOR_ID,
    REVIEWER_ID,
    _compiler_template,
    _create_environment,
    _request_template,
    _seed_project,
)


def _seed_experiment_context(database: Database, *, server_names: tuple[str, ...]) -> dict:
    project_id = _seed_project(database)
    with database.cursor() as cursor:
        project_name = str(
            cursor.execute(
                "SELECT name FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()["name"]
        )
    environment = _create_environment(database, project_id)
    template_approval_id = _request_template(
        database,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    template = database.apply_run_template_change_decision(
        approval_id=template_approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )

    git_commit = "a" * 40
    promotion_id = database.insert_pinned_approval(
        kind="engineering_task_promote",
        contract_version="code-promotion-v1",
        payload={
            "engineering_task_id": str(uuid.uuid4()),
            "project_name": project_name,
            "base_project_version_id": "legacy-base",
            "git_commit": git_commit,
            "bundle_sha256": "b" * 64,
        },
    )
    prepared = database.prepare_project_version_promotion(
        approval_id=promotion_id,
        project_name=project_name,
        git_commit=git_commit,
        bundle_sha256="b" * 64,
    )
    version = database.finalize_project_version_promotion(
        approval_id=promotion_id,
        version_id=prepared["version"]["id"],
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )

    revisions: dict[str, dict] = {}
    for index, server_name in enumerate(server_names):
        _publish_server_evidence(
            database,
            server_name=server_name,
            tags=["gpu"],
            verified_yaml_bytes=True,
        )
        revision = database.get_active_server_config_revision(server_name)
        assert revision is not None
        revision = database.record_server_attempt_backend_preflight(
            server_name=server_name,
            revision_id=revision["id"],
            status="eligible",
            contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
            filesystem_type="ext2/ext3/ext4",
        )
        database.insert_server_observation(
            server_name=server_name,
            online=True,
            probe_ok=True,
            gpu_count=2,
            gpu_mem_used_mb=1024,
            gpu_mem_total_mb=24576,
            mem_total_bytes=64 * 1024 * 1024 * 1024,
            mem_available_bytes=32 * 1024 * 1024 * 1024,
            disk_avail_bytes=512 * 1024 * 1024 * 1024,
        )
        instance_id = database.insert_project_instance(
            project_name=project_name,
            server=server_name,
            path=f"/srv/projects/product-v2-{index}",
            git_branch="main",
            git_commit=git_commit,
            dirty=False,
        )
        database.update_instance_reconcile(
            instance_id,
            state="available",
            git_branch="main",
            git_commit=git_commit,
            dirty=False,
            touch_last_seen=True,
        )
        revisions[server_name] = revision

    return {
        "project_id": project_id,
        "project_name": project_name,
        "environment": environment,
        "template": template,
        "version": version,
        "revisions": revisions,
    }


def _counts(database: Database) -> dict[str, int]:
    with database.cursor() as cursor:
        return {
            table: int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "approvals",
                "experiments",
                "experiment_plan_members",
                "experiment_plan_specs",
                "execution_plans",
                "jobs",
            )
        }


def _matrix_2x2() -> ExperimentMatrix:
    # `epochs`/`mode` are the real 2x2 axes; `enabled`/`ratio`/`label` are
    # the template's other *required* parameters, carried as single-value
    # axes since `parameter_overrides` (the matrix combination) is the only
    # override surface `ExecutionPlanV2Request` exposes -- P2's request
    # signature does not add a separate "fixed overrides" field beyond what
    # `expand_matrix()` already produces per combination.
    return ExperimentMatrix(
        axes=[
            MatrixAxis(name="epochs", values=[10, 20]),
            MatrixAxis(name="mode", values=["fast", "safe"]),
            MatrixAxis(name="enabled", values=[True]),
            MatrixAxis(name="ratio", values=["0.25"]),
            MatrixAxis(name="label", values=["model"]),
        ]
    )


def _guard(*, total_runs: int, target_servers: list[str]) -> ExperimentGuard:
    return ExperimentGuard(total_runs=total_runs, target_servers=target_servers)


def _request(
    database: Database,
    seed: dict,
    *,
    matrix: ExperimentMatrix,
    guard: ExperimentGuard,
    experiment_v2_enabled: bool = True,
    requester_actor_id: str = OPERATOR_ID,
) -> dict:
    with database.cursor() as cursor:
        return create_experiment_v2_request_in_transaction(
            database,
            cursor,
            project_id=seed["project_id"],
            template_selection=RunProfileTemplateSelection(
                kind="run_profile_revision",
                run_profile_id=seed["template"]["run_profile_id"],
            ),
            dataset_selection=DatasetNoneSelection(kind="none"),
            project_version_id=seed["version"]["id"],
            matrix=matrix,
            guard=guard,
            requester_actor_id=requester_actor_id,
            sharing_enabled=False,
            experiment_v2_enabled=experiment_v2_enabled,
        )


def test_happy_path_2x2_matrix_creates_one_approval_and_four_members(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    matrix = _matrix_2x2()
    guard = _guard(total_runs=4, target_servers=["pilot-a", "pilot-b"])

    before = _counts(database)
    result = _request(database, seed, matrix=matrix, guard=guard)
    after = _counts(database)

    assert result["run_count"] == 4
    assert len(result["plan_ids"]) == 4
    assert len(set(result["plan_digests"])) == 4
    assert after["approvals"] - before["approvals"] == 1
    assert after["experiments"] - before["experiments"] == 1
    assert after["experiment_plan_members"] - before["experiment_plan_members"] == 4
    assert after["experiment_plan_specs"] - before["experiment_plan_specs"] == 4
    assert after["execution_plans"] - before["execution_plans"] == 4
    assert after["jobs"] == before["jobs"]

    detail = get_experiment_v2_by_approval(database, result["approval_id"])
    assert detail is not None
    assert detail["run_count"] == 4
    assert detail["status"] == "pending"
    assert {member["execution_plan_id"] for member in detail["members"]} == set(
        result["plan_ids"]
    )
    # Deterministic round-robin: combination i -> target_servers[i % 2].
    servers_in_order = [
        next(
            member["server_name"]
            for member in detail["members"]
            if member["execution_plan_id"] == plan_id
        )
        for plan_id in result["plan_ids"]
    ]
    assert servers_in_order == ["pilot-a", "pilot-b", "pilot-a", "pilot-b"]

    with database.cursor() as cursor:
        approval = cursor.execute(
            "SELECT kind, status FROM approvals WHERE id = ?",
            (result["approval_id"],),
        ).fetchone()
    assert approval["kind"] == EXPERIMENT_V2_APPROVAL_KIND
    assert approval["status"] == "pending"

    listed = list_experiments_v2_for_project(database, seed["project_id"])
    assert [entry["experiment_id"] for entry in listed] == [result["experiment_id"]]
    database.close()


def _single_run_matrix() -> ExperimentMatrix:
    return ExperimentMatrix(
        axes=[
            MatrixAxis(name="epochs", values=[10]),
            MatrixAxis(name="mode", values=["safe"]),
            MatrixAxis(name="enabled", values=[True]),
            MatrixAxis(name="ratio", values=["0.25"]),
            MatrixAxis(name="label", values=["model"]),
        ]
    )


@pytest.mark.parametrize(
    ("instance_state", "commit_matches", "should_succeed"),
    [
        ("available", True, True),
        ("diverged", True, True),
        ("dirty", True, False),
        ("missing", True, False),
        ("unknown", True, False),
        ("available", False, False),
    ],
)
def test_experiment_request_accepts_diverged_exact_commit_instance_like_available(
    tmp_path, instance_state, commit_matches, should_succeed
):
    """Bug fix (fdbc922 follow-up, migration 17): same first-class
    `diverged` ready state as the single-run `execution_plan_v2` path
    (`tests/test_execution_plan_v2_api.py::
    test_run_request_accepts_diverged_exact_commit_instance_like_available`),
    exercised through the `experiment_plan_specs` consistency trigger
    instead. `create_experiment_v2_request_in_transaction` shares
    `resolve_execution_plan_v2`'s candidate query per matrix combination, so
    dirty/missing/unknown states and a commit mismatch remain rejected with
    the same typed `ValueError` both before and after this migration --
    only the diverged-with-exact-commit INSERT itself changes from raising
    `sqlite3.IntegrityError` to succeeding."""
    database = Database(str(tmp_path / "experiment-diverged.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a",))
    with database.cursor() as cursor:
        instance_row = cursor.execute(
            "SELECT id, git_commit FROM project_instances "
            "WHERE project_name = ? AND server = ?",
            (seed["project_name"], "pilot-a"),
        ).fetchone()
    promoted_commit = str(instance_row["git_commit"])
    reconcile_commit = promoted_commit if commit_matches else "f" * 40
    database.update_instance_reconcile(
        instance_row["id"],
        state=instance_state,
        git_branch="main",
        git_commit=reconcile_commit,
        dirty=(instance_state == "dirty"),
        touch_last_seen=True,
    )
    matrix = _single_run_matrix()
    guard = _guard(total_runs=1, target_servers=["pilot-a"])
    before = _counts(database)

    if should_succeed:
        result = _request(database, seed, matrix=matrix, guard=guard)
        after = _counts(database)
        assert result["run_count"] == 1
        assert after["experiment_plan_specs"] - before["experiment_plan_specs"] == 1
        assert after["approvals"] - before["approvals"] == 1
    else:
        with pytest.raises(ValueError, match="target_project_instance_unavailable"):
            _request(database, seed, matrix=matrix, guard=guard)
        assert _counts(database) == before
    database.close()




def test_flag_off_is_typed_refusal_with_zero_rows(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a",))
    before = _counts(database)

    with pytest.raises(ValueError, match="experiment_v2_disabled"):
        _request(
            database,
            seed,
            matrix=_matrix_2x2(),
            guard=_guard(total_runs=4, target_servers=["pilot-a"]),
            experiment_v2_enabled=False,
        )

    assert _counts(database) == before
    database.close()


def test_guard_run_count_mismatch_leaves_zero_rows(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a",))
    before = _counts(database)

    with pytest.raises(ValueError, match="experiment_guard_run_count_mismatch"):
        _request(
            database,
            seed,
            matrix=_matrix_2x2(),
            guard=_guard(total_runs=3, target_servers=["pilot-a"]),
        )

    assert _counts(database) == before
    database.close()


def test_one_failing_combination_rolls_back_the_whole_request(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a",))
    # epochs=999 violates the template's parameter_schema maximum (100), so
    # resolving that one combination raises during `_resolve_template` --
    # after the *first* (epochs=10) combination already resolved cleanly.
    matrix = ExperimentMatrix(
        axes=[
            MatrixAxis(name="epochs", values=[10, 999]),
            MatrixAxis(name="mode", values=["safe"]),
            MatrixAxis(name="enabled", values=[True]),
            MatrixAxis(name="ratio", values=["0.25"]),
            MatrixAxis(name="label", values=["model"]),
        ]
    )
    before = _counts(database)

    with pytest.raises(ValueError):
        _request(
            database,
            seed,
            matrix=matrix,
            guard=_guard(total_runs=2, target_servers=["pilot-a"]),
        )

    assert _counts(database) == before
    database.close()


def test_matrix_expansion_over_cap_leaves_zero_rows(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a",))
    matrix = ExperimentMatrix(
        axes=[
            MatrixAxis(name="epochs", values=list(range(1, 6))),
            MatrixAxis(name="mode", values=["fast", "safe"] * 4),
        ]
    )
    before = _counts(database)

    with pytest.raises(ExperimentMatrixError):
        _request(
            database,
            seed,
            matrix=matrix,
            guard=_guard(total_runs=32, target_servers=["pilot-a"]),
        )

    assert _counts(database) == before
    database.close()


def test_external_job_on_target_server_fails_whole_request_batch_aware_succeeds(
    tmp_path,
):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    matrix = _matrix_2x2()
    guard = _guard(total_runs=4, target_servers=["pilot-a", "pilot-b"])

    # Two members legitimately round-robin onto the same server with no
    # external work present -- this must succeed (EX-3 batch-aware).
    before = _counts(database)
    result = _request(database, seed, matrix=matrix, guard=guard)
    assert result["run_count"] == 4
    assert _counts(database)["execution_plans"] - before["execution_plans"] == 4
    database.close()

    # A fresh database: seed identically, but this time an external queued
    # job already pins one of the two target servers -- the whole request
    # must fail (fail-closed), zero new rows.
    database = Database(str(tmp_path / "experiment-external.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (type, project, command, status, priority,
                               pin_server, depends_on, created_at)
            VALUES ('adhoc', ?, 'echo external', 'queued', 'normal', 'pilot-a',
                    '[]', ?)
            """,
            (seed["project_name"], database._sqlite_now(cursor)),
        )
    before = _counts(database)
    with pytest.raises(ValueError, match="target_worker_not_exclusive"):
        _request(database, seed, matrix=matrix, guard=guard)
    assert _counts(database) == before
    database.close()


def test_never_auto_approved_and_transaction_only(tmp_path):
    assert EXPERIMENT_V2_APPROVAL_KIND not in ("enqueue", "stop")
    assert EXPERIMENT_V2_APPROVAL_KIND in TRANSACTION_ONLY_APPROVAL_KINDS


def _approve(database: Database, approval_id: int, *, decision_actor_id: str = REVIEWER_ID):
    with database.cursor() as cursor:
        return apply_experiment_v2_decision_in_transaction(
            database,
            cursor,
            approval_id=approval_id,
            decision_actor_id=decision_actor_id,
            decision_mechanism="session",
            sharing_enabled=False,
        )


def test_approve_materializes_n_jobs_atomically(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    result = _request(
        database,
        seed,
        matrix=_matrix_2x2(),
        guard=_guard(total_runs=4, target_servers=["pilot-a", "pilot-b"]),
    )

    before = _counts(database)
    decision = _approve(database, result["approval_id"])
    after = _counts(database)

    assert decision["status"] == "approved"
    assert decision["created"] is True
    assert len(decision["job_ids"]) == 4
    assert len(set(decision["job_ids"])) == 4
    assert after["jobs"] - before["jobs"] == 4

    with database.cursor() as cursor:
        approval = cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (result["approval_id"],),
        ).fetchone()
        job_ids_for_plans = [
            row["job_id"]
            for row in cursor.execute(
                "SELECT job_id FROM execution_plans WHERE id IN "
                "(SELECT plan_id FROM experiment_plan_members WHERE experiment_id = ?)",
                (result["experiment_id"],),
            ).fetchall()
        ]
    assert approval["status"] == "approved"
    assert sorted(job_ids_for_plans) == sorted(decision["job_ids"])
    database.close()


def test_replay_approve_returns_same_job_ids_created_false(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    result = _request(
        database,
        seed,
        matrix=_matrix_2x2(),
        guard=_guard(total_runs=4, target_servers=["pilot-a", "pilot-b"]),
    )
    first = _approve(database, result["approval_id"])
    before = _counts(database)
    second = _approve(database, result["approval_id"])
    after = _counts(database)

    assert second["created"] is False
    assert second["status"] == "approved"
    assert sorted(second["job_ids"]) == sorted(first["job_ids"])
    assert after == before
    database.close()


def test_forced_failure_during_materialization_rolls_back_and_retry_succeeds(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    result = _request(
        database,
        seed,
        matrix=_matrix_2x2(),
        guard=_guard(total_runs=4, target_servers=["pilot-a", "pilot-b"]),
    )

    calls = {"n": 0}
    original = Database.append_durable_audit_event_in_transaction

    def _fail_on_third_job_event(self, cursor, *, action, **kwargs):
        if action == "experiment_v2_job_materialized":
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("injected materialization failure")
        return original(self, cursor, action=action, **kwargs)

    monkeypatch.setattr(
        Database,
        "append_durable_audit_event_in_transaction",
        _fail_on_third_job_event,
    )
    before = _counts(database)
    with pytest.raises(RuntimeError, match="injected materialization failure"):
        _approve(database, result["approval_id"])
    after_failed_attempt = _counts(database)
    assert after_failed_attempt == before
    with database.cursor() as cursor:
        approval = cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (result["approval_id"],),
        ).fetchone()
    assert approval["status"] == "pending"

    monkeypatch.setattr(
        Database,
        "append_durable_audit_event_in_transaction",
        original,
    )
    retried = _approve(database, result["approval_id"])
    assert retried["created"] is True
    assert len(retried["job_ids"]) == 4
    database.close()


def test_stale_external_job_between_request_and_decision_rejects_with_zero_jobs(
    tmp_path,
):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    result = _request(
        database,
        seed,
        matrix=_matrix_2x2(),
        guard=_guard(total_runs=4, target_servers=["pilot-a", "pilot-b"]),
    )
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (type, project, command, status, priority,
                               pin_server, depends_on, created_at)
            VALUES ('adhoc', ?, 'echo external', 'queued', 'normal', 'pilot-b',
                    '[]', ?)
            """,
            (seed["project_name"], database._sqlite_now(cursor)),
        )

    before = _counts(database)
    decision = _approve(database, result["approval_id"])
    after = _counts(database)

    assert decision["status"] == "rejected"
    assert decision["job_ids"] == []
    assert after["jobs"] == before["jobs"]
    with database.cursor() as cursor:
        approval = cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (result["approval_id"],),
        ).fetchone()
    assert approval["status"] == "rejected"
    database.close()


def test_reject_path(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    result = _request(
        database,
        seed,
        matrix=_matrix_2x2(),
        guard=_guard(total_runs=4, target_servers=["pilot-a", "pilot-b"]),
    )
    before = _counts(database)
    with database.cursor() as cursor:
        decision = reject_experiment_v2_decision_in_transaction(
            database,
            cursor,
            approval_id=result["approval_id"],
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="session",
        )
    after = _counts(database)

    assert decision["status"] == "rejected"
    assert decision["job_ids"] == []
    assert after["jobs"] == before["jobs"]
    with database.cursor() as cursor:
        approval = cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (result["approval_id"],),
        ).fetchone()
    assert approval["status"] == "rejected"

    detail = get_experiment_v2(database, result["experiment_id"])
    assert detail["status"] == "rejected"
    database.close()
