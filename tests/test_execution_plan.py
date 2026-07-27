"""WP-3B: ExecutionPlan derivation.

The property under test is that a plan states what it pins and refuses to
claim more. Reproducibility is never inferred from a hopeful default.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import Database
from app.execution_plan import (
    PLAN_CONTRACT_VERSION,
    PLAN_REASON_CODES,
    PlanInputs,
    ResolvedInputs,
    compute_plan_digest,
    derive_plan_draft,
    plan_matches_draft,
)


def _ready_inputs(**overrides):
    base = dict(
        project_name="demo",
        command="python train.py",
        project_version_id="pv-1",
        run_profile_id="rp-1",
        dataset_snapshot_id="snap-1",
        server_config_revision_id="rev-1",
    )
    base.update(overrides)
    return PlanInputs(**base)


def _ready_resolved(**overrides):
    base = dict(
        project_version_exists=True,
        run_profile_status="active",
        dataset_snapshot_state="published",
        target_eligibility="approved",
    )
    base.update(overrides)
    return ResolvedInputs(**base)


def test_a_fully_pinned_plan_is_ready_and_reproducible():
    draft = derive_plan_draft(_ready_inputs(), _ready_resolved())
    assert draft.ready is True
    assert draft.reproducible is True
    assert draft.reason_codes == ("plan_ready",)
    assert draft.plan_digest is not None


def test_a_legacy_registry_dataset_is_rejected_not_downgraded():
    """Gate D-5: a run claiming reproducibility it does not have is worse than
    a refused one."""
    draft = derive_plan_draft(
        _ready_inputs(dataset_snapshot_id=None),
        _ready_resolved(dataset_is_legacy_registry=True, dataset_snapshot_state=None),
    )
    assert "dataset_not_reproducible" in draft.reason_codes
    assert draft.reproducible is False
    assert draft.ready is False
    assert draft.plan_digest is None


def test_an_unpublished_snapshot_cannot_back_a_plan():
    for state in ("candidate", "building", "aborted", "verification_unknown", None):
        draft = derive_plan_draft(
            _ready_inputs(), _ready_resolved(dataset_snapshot_state=state)
        )
        assert "dataset_snapshot_not_published" in draft.reason_codes
        assert draft.reproducible is False


def test_dataset_none_is_a_pinned_statement_not_a_missing_input():
    """'No data' is reproducible; 'some directory' is not."""
    draft = derive_plan_draft(
        _ready_inputs(dataset_snapshot_id=None, dataset_none=True),
        _ready_resolved(dataset_snapshot_state=None),
    )
    assert draft.ready is True
    assert draft.reproducible is True
    assert "dataset" not in draft.missing


def test_an_unset_dataset_is_missing_not_none():
    draft = derive_plan_draft(
        _ready_inputs(dataset_snapshot_id=None),
        _ready_resolved(dataset_snapshot_state=None),
    )
    assert "dataset" in draft.missing
    assert draft.reproducible is False


def test_every_blocking_reason_is_reported_at_once():
    """One round trip per missing prerequisite is a worse experience than
    being told everything."""
    draft = derive_plan_draft(
        PlanInputs(project_name="demo", command="python train.py"),
        ResolvedInputs(),
    )
    assert {
        "project_version_missing",
        "run_profile_missing",
        "dataset_snapshot_not_published",
        "target_missing",
    } <= set(draft.reason_codes)


def test_an_archived_run_profile_blocks_the_plan():
    draft = derive_plan_draft(
        _ready_inputs(), _ready_resolved(run_profile_status="archived")
    )
    assert "run_profile_archived" in draft.reason_codes
    assert draft.ready is False


def test_a_legacy_observed_target_cannot_be_planned_against():
    draft = derive_plan_draft(
        _ready_inputs(), _ready_resolved(target_eligibility="legacy_observed")
    )
    assert "target_not_approved" in draft.reason_codes


def test_a_dangerous_command_blocks_at_preview_time():
    draft = derive_plan_draft(
        _ready_inputs(), _ready_resolved(command_is_dangerous=True)
    )
    assert "command_dangerous" in draft.reason_codes
    assert draft.plan_digest is None


def test_reason_codes_stay_inside_the_closed_set():
    draft = derive_plan_draft(_ready_inputs(), _ready_resolved())
    assert set(draft.reason_codes) <= PLAN_REASON_CODES

    with pytest.raises(ValueError, match="closed set"):
        derive_plan_draft(
            _ready_inputs(),
            _ready_resolved(extra_reason_codes=("invented_reason",)),
        )


def test_the_digest_changes_when_any_bound_revision_changes():
    """This is what makes approve-time re-verification meaningful."""
    base = _ready_inputs()
    baseline = derive_plan_draft(base, _ready_resolved()).plan_digest
    for field, value in (
        ("project_version_id", "pv-2"),
        ("run_profile_id", "rp-2"),
        ("dataset_snapshot_id", "snap-2"),
        ("server_config_revision_id", "rev-2"),
        ("command", "python other.py"),
    ):
        moved = derive_plan_draft(
            _ready_inputs(**{field: value}), _ready_resolved()
        ).plan_digest
        assert moved != baseline, f"digest ignored a change to {field}"


def test_dataset_none_and_unset_dataset_are_different_plans():
    with_none = compute_plan_digest(
        _ready_inputs(dataset_snapshot_id=None, dataset_none=True),
        command_sha256="x",
        reproducible=True,
    )
    unset = compute_plan_digest(
        _ready_inputs(dataset_snapshot_id=None, dataset_none=False),
        command_sha256="x",
        reproducible=True,
    )
    assert with_none != unset


def test_preview_is_pure_and_repeatable():
    inputs, resolved = _ready_inputs(), _ready_resolved()
    first = derive_plan_draft(inputs, resolved)
    second = derive_plan_draft(inputs, resolved)
    assert first == second


def test_approve_time_verification_rejects_a_moved_plan():
    draft = derive_plan_draft(_ready_inputs(), _ready_resolved())
    persisted = {
        "plan_digest": draft.plan_digest,
        "contract_version": PLAN_CONTRACT_VERSION,
    }
    assert plan_matches_draft(persisted, draft) is True

    moved = derive_plan_draft(_ready_inputs(project_version_id="pv-9"), _ready_resolved())
    assert plan_matches_draft(persisted, moved) is False


def test_an_unready_draft_can_never_satisfy_approve_time_verification():
    draft = derive_plan_draft(
        _ready_inputs(), _ready_resolved(dataset_snapshot_state="building")
    )
    assert plan_matches_draft({"plan_digest": None}, draft) is False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _plan_row(conn, **overrides):
    row = dict(
        id="plan-1",
        project_name="demo",
        contract_version=PLAN_CONTRACT_VERSION,
        plan_digest="digest-1",
        command_sha256="cmd-1",
        reproducible=1,
        project_version_id=None,
        run_profile_id=None,
        dataset_snapshot_id=None,
        dataset_none=1,
        server_config_revision_id="rev-1",
        created_at="now",
    )
    row.update(overrides)
    conn.execute(
        f"INSERT INTO execution_plans ({','.join(row)})"
        f" VALUES ({','.join('?' * len(row))})",
        tuple(row.values()),
    )


def test_a_reproducible_plan_row_must_pin_code_and_profile(tmp_path):
    path = tmp_path / "plan.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        # reproducible=1 with no project_version and no run_profile
        _plan_row(conn)


def test_a_plan_row_cannot_be_both_dataset_none_and_pinned(tmp_path):
    path = tmp_path / "plan.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        _plan_row(
            conn,
            reproducible=0,
            dataset_none=1,
            dataset_snapshot_id="snap-1",
        )


def test_execution_plans_are_immutable_and_append_only(tmp_path):
    path = tmp_path / "plan.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    _plan_row(conn, reproducible=0, dataset_none=0)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE execution_plans SET plan_digest='x' WHERE id='plan-1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM execution_plans WHERE id='plan-1'")
