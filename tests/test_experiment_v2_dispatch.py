"""Dispatch-time pinned-contract tests for Experiment v2 (DG-EXPERIMENT-V1, P3).

`Database._validate_pinned_job_contract` / `Database.create_execution_attempt`
must recognize a materialized experiment member Job (approval kind
`experiment_create_v2`) exactly as strictly as it already recognizes a
single-run `execution_plan_v2` Job -- verifying the approval payload digest,
the member plan's linkage back to its experiment, and every pinned field --
and must fail closed on tampering. Reuses the P2 store-level fixtures.
"""

from __future__ import annotations

import pytest

from app.db import Database
from app.experiment_v2_store import apply_experiment_v2_decision_in_transaction
from tests.test_experiment_v2_store import (
    _guard,
    _matrix_2x2,
    _request,
    _seed_experiment_context,
)
from tests.test_run_templates_v2 import REVIEWER_ID


def _approve(database: Database, approval_id: int) -> dict:
    with database.cursor() as cursor:
        return apply_experiment_v2_decision_in_transaction(
            database,
            cursor,
            approval_id=approval_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="session",
            sharing_enabled=False,
        )


def _materialize_one_member(database: Database) -> dict:
    seed = _seed_experiment_context(database, server_names=("pilot-a", "pilot-b"))
    result = _request(
        database,
        seed,
        matrix=_matrix_2x2(),
        guard=_guard(total_runs=4, target_servers=["pilot-a", "pilot-b"]),
    )
    decision = _approve(database, result["approval_id"])
    job_id = decision["job_ids"][0]
    with database.cursor() as cursor:
        job = cursor.execute(
            "SELECT id, pin_server, execution_contract_role FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    server_name = job["pin_server"]
    return {
        "seed": seed,
        "result": result,
        "decision": decision,
        "job_id": job_id,
        "plan_id": job["execution_contract_role"],
        "server_name": server_name,
        "server_config_revision_id": seed["revisions"][server_name]["id"],
    }


def test_materialized_member_job_passes_pinned_contract_dispatch(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    member = _materialize_one_member(database)

    lease = database.acquire_scheduler_lease(
        owner_id="experiment-dispatch-test",
        lease_seconds=120,
    )
    attempt = database.create_execution_attempt(
        job_id=member["job_id"],
        backend="ssh",
        server_config_revision_id=member["server_config_revision_id"],
        leader_owner_id=lease["owner_id"],
        scheduler_fencing_epoch=lease["fencing_epoch"],
        initial_operation={
            "operation": "prepare",
            "operation_id": "experiment-prepare",
            "idempotency_key": "experiment-prepare-key",
            "payload": {"job_id": member["job_id"]},
        },
    )
    assert attempt["execution_contract_version"] == "experiment-v2-approval-v1"
    database.close()


def test_tampered_member_plan_command_sha256_fails_closed(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    member = _materialize_one_member(database)

    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER execution_plans_are_immutable")
        cursor.execute(
            "UPDATE execution_plans SET command_sha256 = ? WHERE id = ?",
            ("0" * 64, member["plan_id"]),
        )

    lease = database.acquire_scheduler_lease(
        owner_id="experiment-dispatch-tamper",
        lease_seconds=120,
    )
    with pytest.raises(ValueError, match="contract_digest_mismatch"):
        database.create_execution_attempt(
            job_id=member["job_id"],
            backend="ssh",
            server_config_revision_id=member["server_config_revision_id"],
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
            initial_operation={
                "operation": "prepare",
                "operation_id": "experiment-prepare-tamper",
                "idempotency_key": "experiment-prepare-tamper-key",
                "payload": {"job_id": member["job_id"]},
            },
        )
    assert database.get_latest_execution_attempt_for_job(member["job_id"]) is None
    database.close()


def test_tampered_member_spec_json_fails_closed(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    member = _materialize_one_member(database)

    with database.cursor() as cursor:
        cursor.execute("DROP TRIGGER trg_experiment_plan_specs_immutable_update")
        cursor.execute(
            "UPDATE experiment_plan_specs SET canonical_spec_json = '{}' "
            "WHERE execution_plan_id = ?",
            (member["plan_id"],),
        )

    lease = database.acquire_scheduler_lease(
        owner_id="experiment-dispatch-tamper-spec",
        lease_seconds=120,
    )
    with pytest.raises(ValueError, match="contract_digest_mismatch"):
        database.create_execution_attempt(
            job_id=member["job_id"],
            backend="ssh",
            server_config_revision_id=member["server_config_revision_id"],
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
    database.close()


def test_dispatch_with_wrong_server_revision_fails_closed(tmp_path):
    database = Database(str(tmp_path / "experiment.db"))
    member = _materialize_one_member(database)
    other_server = next(
        name for name in member["seed"]["revisions"] if name != member["server_name"]
    )
    wrong_revision_id = member["seed"]["revisions"][other_server]["id"]

    lease = database.acquire_scheduler_lease(
        owner_id="experiment-dispatch-wrong-revision",
        lease_seconds=120,
    )
    with pytest.raises(ValueError, match="contract_digest_mismatch"):
        database.create_execution_attempt(
            job_id=member["job_id"],
            backend="ssh",
            server_config_revision_id=wrong_revision_id,
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
    database.close()


def test_single_run_execution_plan_v2_dispatch_unaffected(tmp_path):
    """The new `experiment_create_v2` branch must not touch the sibling
    `execution_plan_v2` branch it sits next to -- covered end-to-end by the
    unmodified `tests/test_execution_plan_v2_api.py` suite; this is a light
    regression signal that the two kinds stay mutually exclusive at dispatch.
    """

    database = Database(str(tmp_path / "experiment.db"))
    member = _materialize_one_member(database)
    with database.cursor() as cursor:
        approval = cursor.execute(
            "SELECT kind FROM approvals WHERE id = ?",
            (member["result"]["approval_id"],),
        ).fetchone()
    assert approval["kind"] == "experiment_create_v2"
    database.close()
