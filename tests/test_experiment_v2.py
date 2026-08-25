"""DG-EXPERIMENT-V1 P1: pure Experiment v2 matrix/guard/approval contracts.

This module is pure (`app/experiment_v2.py`) -- no database, filesystem, or
network access is exercised anywhere in this file.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from pydantic import ValidationError

from app.execution_contract import canonical_json, utf8_sha256
from app.experiment_v2 import (
    EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION,
    EXPERIMENT_V2_APPROVAL_KIND,
    EXPERIMENT_V2_CONTRACT_VERSION,
    MAX_APPROVAL_PAYLOAD_BYTES,
    MAX_ESTIMATION_STRING_BYTES,
    MAX_EXPERIMENT_RUNS,
    MAX_GUARD_BYTES,
    MAX_MATRIX_AXES,
    MAX_MATRIX_BYTES,
    MAX_TARGET_SERVERS,
    ExperimentGuard,
    ExperimentMatrix,
    ExperimentMatrixError,
    ExperimentV2ApprovalPayload,
    MatrixAxis,
    expand_matrix,
    experiment_v2_approval_payload_digest,
    parse_experiment_v2_approval_payload,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def test_contract_version_and_kind_constants_are_pinned() -> None:
    assert EXPERIMENT_V2_CONTRACT_VERSION == "experiment-v2"
    assert EXPERIMENT_V2_APPROVAL_KIND == "experiment_create_v2"
    assert EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION == "experiment-v2-approval-v1"


def test_byte_cap_and_bound_constants_are_pinned() -> None:
    """Byte caps chosen for this packet, pinned so a later drive-by change
    cannot silently loosen a contract bound."""

    assert MAX_EXPERIMENT_RUNS == 32
    assert MAX_MATRIX_AXES == 8
    assert MAX_MATRIX_BYTES == 32 * 1024
    assert MAX_GUARD_BYTES == 4 * 1024
    assert MAX_APPROVAL_PAYLOAD_BYTES == 64 * 1024
    assert MAX_TARGET_SERVERS == 8
    assert MAX_ESTIMATION_STRING_BYTES == 128


def test_expand_matrix_uses_canonical_axis_order_and_preserves_value_order() -> None:
    """Axes are declared out of name order; expansion must still iterate in
    canonical (sorted-by-name) axis order while keeping each axis's declared
    value order -- this is what makes round-robin target assignment over the
    expansion deterministic and reviewable."""

    matrix = ExperimentMatrix(
        axes=[
            {"name": "zeta", "values": ["b", "a"]},
            {"name": "alpha", "values": [2, 1]},
        ]
    )

    combinations = expand_matrix(matrix)

    expected = [
        dict(zip(("alpha", "zeta"), combo))
        for combo in itertools.product([2, 1], ["b", "a"])
    ]
    assert combinations == expected
    assert list(combinations[0].keys()) == ["alpha", "zeta"]


def test_expand_matrix_cross_product_count() -> None:
    matrix = ExperimentMatrix(
        axes=[
            {"name": "lr", "values": ["0.1", "0.01", "0.001"]},
            {"name": "batch", "values": [8, 16]},
        ]
    )

    combinations = expand_matrix(matrix)

    assert len(combinations) == 6
    assert len({tuple(sorted(c.items())) for c in combinations}) == 6


def test_expand_matrix_accepts_exactly_the_run_cap() -> None:
    matrix = ExperimentMatrix(
        axes=[
            {"name": "a", "values": list(range(1, 17))},
            {"name": "b", "values": [1, 2]},
        ]
    )

    combinations = expand_matrix(matrix)

    assert len(combinations) == MAX_EXPERIMENT_RUNS


def test_expand_matrix_rejects_more_than_max_experiment_runs() -> None:
    """A single axis is bounded at `MAX_EXPERIMENT_RUNS` values by
    `MatrixAxis` itself; the 33-combination case that must be rejected here
    is the cross-*product* overflow -- two axes each individually valid
    whose product exceeds the run cap."""

    matrix = ExperimentMatrix(
        axes=[
            {"name": "a", "values": list(range(1, 33))},  # 32 values, valid alone
            {"name": "b", "values": [1, 2]},  # 2 values, valid alone
        ]
    )

    with pytest.raises(ExperimentMatrixError, match="more than 32 runs"):
        expand_matrix(matrix)


def test_matrix_axis_values_bounded_at_max_experiment_runs() -> None:
    with pytest.raises(ValidationError):
        MatrixAxis(name="a", values=list(range(MAX_EXPERIMENT_RUNS + 1)))
    # Exactly the bound is fine.
    axis = MatrixAxis(name="a", values=list(range(MAX_EXPERIMENT_RUNS)))
    assert len(axis.values) == MAX_EXPERIMENT_RUNS


def test_matrix_axis_rejects_float_values() -> None:
    with pytest.raises(ValidationError):
        MatrixAxis(name="lr", values=[0.1])
    with pytest.raises(ValidationError):
        MatrixAxis(name="lr", values=[1, 2.5])


def test_matrix_axis_accepts_canonical_decimal_strings_as_ordinary_text() -> None:
    """Canonical-number rules reject JSON floats, but a *string* value such
    as ``"0.5000"`` is just ordinary text at this layer -- ScalarParameterValue
    (`str | int | bool`) carries it verbatim; canonicality of decimal strings
    is a Run Template concern applied later against a typed parameter
    schema, not a Matrix axis concern."""

    axis = MatrixAxis(name="lr", values=["0.5000", "1"])
    assert axis.values == ["0.5000", "1"]


def test_matrix_axis_name_grammar_matches_run_template_parameter_names() -> None:
    with pytest.raises(ValidationError):
        MatrixAxis(name="1bad", values=[1])
    with pytest.raises(ValidationError):
        MatrixAxis(name="", values=[1])
    axis = MatrixAxis(name="valid_name", values=[1])
    assert axis.name == "valid_name"


def test_experiment_matrix_rejects_duplicate_axis_names() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ExperimentMatrix(
            axes=[
                {"name": "a", "values": [1]},
                {"name": "a", "values": [2]},
            ]
        )


def test_experiment_matrix_bounded_axis_count() -> None:
    with pytest.raises(ValidationError):
        ExperimentMatrix(
            axes=[{"name": f"a{i}", "values": [1]} for i in range(MAX_MATRIX_AXES + 1)]
        )
    matrix = ExperimentMatrix(
        axes=[{"name": f"a{i}", "values": [1]} for i in range(MAX_MATRIX_AXES)]
    )
    assert len(matrix.axes) == MAX_MATRIX_AXES


def test_experiment_matrix_byte_cap_is_enforced() -> None:
    """A matrix within every structural bound (axes <= 8, values per axis
    <= 32) can still be rejected once its canonical JSON exceeds
    `MAX_MATRIX_BYTES` -- long string values are the deliberate way to prove
    the byte cap is load-bearing independent of the count caps."""

    long_value = "x" * 2000
    with pytest.raises(ValidationError):
        ExperimentMatrix(
            axes=[
                {"name": f"a{i}", "values": [long_value] * 32}
                for i in range(MAX_MATRIX_AXES)
            ]
        )


def _valid_matrix_body() -> dict:
    return {
        "axes": [
            {"name": "lr", "values": ["0.1", "0.01"]},
            {"name": "batch", "values": [8, 16, 32]},
        ]
    }


def _valid_guard_body(total_runs: int) -> dict:
    return {"total_runs": total_runs, "target_servers": ["worker-1", "worker-2"]}


def test_experiment_guard_accepts_display_only_estimation_strings() -> None:
    guard = ExperimentGuard(
        total_runs=6,
        target_servers=["worker-1"],
        est_gpu_hours="12.5",
        est_storage="200GiB",
    )
    assert guard.est_gpu_hours == "12.5"
    assert guard.est_storage == "200GiB"


def test_experiment_guard_rejects_duplicate_and_invalid_target_servers() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ExperimentGuard(total_runs=1, target_servers=["worker-1", "worker-1"])
    with pytest.raises(ValidationError):
        ExperimentGuard(total_runs=1, target_servers=[""])


def test_experiment_guard_bounded_target_servers_and_runs() -> None:
    with pytest.raises(ValidationError):
        ExperimentGuard(total_runs=0, target_servers=["worker-1"])
    with pytest.raises(ValidationError):
        ExperimentGuard(total_runs=MAX_EXPERIMENT_RUNS + 1, target_servers=["worker-1"])
    with pytest.raises(ValidationError):
        ExperimentGuard(
            total_runs=1,
            target_servers=[f"worker-{i}" for i in range(MAX_TARGET_SERVERS + 1)],
        )


def test_experiment_guard_at_every_structural_bound_stays_within_byte_cap() -> None:
    """A guard at every structural maximum (8 servers, 128-byte estimation
    strings) is still well inside `MAX_GUARD_BYTES` -- the byte cap is a
    defense-in-depth backstop below the field-level bounds, following the
    `execution_plan_v2.py` convention of one fixed cap per bounded
    sub-document, not something a structurally valid guard can reach."""

    guard = ExperimentGuard(
        total_runs=MAX_EXPERIMENT_RUNS,
        target_servers=[f"worker-{i}" for i in range(MAX_TARGET_SERVERS)],
        est_gpu_hours="x" * MAX_ESTIMATION_STRING_BYTES,
        est_storage="y" * MAX_ESTIMATION_STRING_BYTES,
    )
    encoded = canonical_json(guard.model_dump(mode="json")).encode("utf-8")
    assert len(encoded) <= MAX_GUARD_BYTES


def _digests(count: int) -> list[str]:
    return [format(i, "064x") for i in range(count)]


def _valid_payload_body(run_count: int = 6) -> dict:
    return {
        "project_id": _uuid(),
        "matrix": _valid_matrix_body(),
        "guard": _valid_guard_body(run_count),
        "plan_digests": _digests(run_count),
        "run_count": run_count,
    }


def test_approval_payload_round_trips_and_digests() -> None:
    payload = ExperimentV2ApprovalPayload.model_validate(_valid_payload_body())
    digest = experiment_v2_approval_payload_digest(payload)
    assert len(digest) == 64
    assert digest == utf8_sha256(canonical_json(payload.model_dump(mode="json")))

    reparsed = parse_experiment_v2_approval_payload(payload.model_dump(mode="json"))
    assert reparsed == payload


def test_approval_payload_rejects_run_count_mismatch_with_expansion() -> None:
    body = _valid_payload_body(run_count=6)
    body["run_count"] = 5
    body["guard"]["total_runs"] = 5
    body["plan_digests"] = _digests(5)
    with pytest.raises(ValidationError, match="expansion"):
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_rejects_guard_total_runs_mismatch() -> None:
    """EX-1: guard.total_runs must agree with the actual matrix expansion
    count, even when run_count itself is internally consistent with the
    plan_digests list -- the guard cannot silently under- or over-declare
    the blast radius a reviewer approves."""

    body = _valid_payload_body(run_count=6)
    body["guard"]["total_runs"] = 5
    with pytest.raises(ValidationError, match="guard total_runs"):
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_rejects_plan_digest_count_mismatch() -> None:
    body = _valid_payload_body(run_count=6)
    body["plan_digests"] = _digests(5)
    with pytest.raises(ValidationError):
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_rejects_duplicate_plan_digests() -> None:
    body = _valid_payload_body(run_count=6)
    body["plan_digests"] = [body["plan_digests"][0]] * 6
    with pytest.raises(ValidationError, match="unique"):
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_rejects_malformed_plan_digest() -> None:
    body = _valid_payload_body(run_count=6)
    body["plan_digests"][0] = "not-hex"
    with pytest.raises(ValidationError):
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_rejects_non_canonical_uuid_project_id() -> None:
    body = _valid_payload_body(run_count=6)
    body["project_id"] = "not-a-uuid"
    with pytest.raises(ValidationError):
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_bounded_run_count_and_digest_list() -> None:
    with pytest.raises(ValidationError):
        body = _valid_payload_body(run_count=6)
        body["run_count"] = MAX_EXPERIMENT_RUNS + 1
        ExperimentV2ApprovalPayload.model_validate(body)


def test_approval_payload_carries_matrix_verbatim() -> None:
    """The matrix body inside the approval payload is exactly what the
    requester declared -- expansion/canonical axis order is used only to
    compute `run_count`/`plan_digests`, never to rewrite the reviewed
    matrix."""

    body = _valid_payload_body(run_count=6)
    payload = ExperimentV2ApprovalPayload.model_validate(body)
    assert [axis.name for axis in payload.matrix.axes] == ["lr", "batch"]


def test_experiment_matrix_error_is_a_value_error() -> None:
    assert issubclass(ExperimentMatrixError, ValueError)
