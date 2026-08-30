"""Pure Experiment v2 matrix and approval contracts (DG-EXPERIMENT-V1, P1).

Approved 2026-08-25, `docs/DG_EXPERIMENT_V1_DECISION.md` +
`docs/DECISIONS.md` same-day entry ("DG-EXPERIMENT-V1：A 核准，EX-1…EX-7").
This module is deliberately pure -- no database, filesystem, or network
access -- following the `app/execution_plan_v2.py` convention: parsing,
canonicalisation, and digesting perform no I/O so the exact bytes an
approver reviews are reproducible from the request alone.

Only the request/approval-payload contract and deterministic matrix
expansion live here.  The resolver that runs the existing single-run
resolver once per expanded combination, the additive storage layer, and the
HTTP routes are later packets (P2/P3) -- this kind is registered
(`EXPERIMENT_V2_APPROVAL_KIND`) but has no request path yet, so it cannot
produce an approval by itself (EX-1).
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution_contract import canonical_json, utf8_sha256
from app.execution_plan_v2 import DatasetSelection, TemplateSelection
from app.project_bootstrap import ScalarParameterValue, canonical_uuid


EXPERIMENT_V2_CONTRACT_VERSION = "experiment-v2"
EXPERIMENT_V2_APPROVAL_KIND = "experiment_create_v2"
EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION: Literal["experiment-v2-approval-v1"] = "experiment-v2-approval-v1"

#: EX-5: the hard combinatorial ceiling for one Experiment matrix -- the same
#: family as the various Product v2 32-element caps (dataset bindings,
#: parameter schemas, ...).  Matrix expansion beyond this is rejected before
#: any approval can be requested.
MAX_EXPERIMENT_RUNS = 32
#: EX-1: matrix axes are bounded well below the run cap so a reviewer can
#: read the axis declarations at a glance; 8 axes at 2 values each already
#: reaches the 256-combination territory the run cap forecloses.
MAX_MATRIX_AXES = 8
#: EX-5: byte caps follow the `execution_plan_v2.py:29-34` convention (one
#: fixed cap per bounded sub-document, checked via canonical JSON bytes).
MAX_MATRIX_BYTES = 32 * 1024
MAX_GUARD_BYTES = 4 * 1024
MAX_APPROVAL_PAYLOAD_BYTES = 64 * 1024
MAX_TARGET_SERVERS = 8
#: EX-5: est_gpu_hours / est_storage are informational-only display strings
#: -- V1 has no resource estimation engine, so these are bounded free text
#: carried into the approval card for a human to read, never computed or
#: enforced.
MAX_ESTIMATION_STRING_BYTES = 128

#: Same identifier grammar as Run Template v2 parameter names
#: (`app/project_bootstrap.py` `_PARAMETER_NAME_RE`).
_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ExperimentMatrixError(ValueError):
    """Typed error for matrix expansion failures (fail-closed, no approval)."""


def _bounded_canonical_json(value: Any, maximum: int, field_name: str) -> str:
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{field_name} exceeds its canonical UTF-8 byte limit")
    return encoded


def _canonical_sha256(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MatrixAxis(_StrictModel):
    """One named parameter axis: a bounded, ordered list of scalar values.

    ``values`` uses the same ``ScalarParameterValue`` domain as
    `app/project_bootstrap.py` parameter overrides (``str | int | bool``).
    Pydantic strict mode already excludes JSON floats -- none of the three
    union members accepts one -- so a float value fails closed here with a
    typed ``ValidationError`` before it can reach the digest.
    """

    name: str
    values: list[ScalarParameterValue] = Field(
        min_length=1,
        max_length=MAX_EXPERIMENT_RUNS,
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not isinstance(value, str) or _PARAMETER_NAME_RE.fullmatch(value) is None:
            raise ValueError("matrix axis name is invalid")
        return value


class ExperimentMatrix(_StrictModel):
    """A bounded set of uniquely-named axes, declared order preserved.

    Axes are kept in the order the requester declared them (this is the
    "matrix (verbatim)" body carried into the approval payload); canonical
    (sorted-by-name) axis order is applied only where determinism is
    required -- expansion -- via :func:`expand_matrix`.
    """

    axes: list[MatrixAxis] = Field(min_length=1, max_length=MAX_MATRIX_AXES)

    @field_validator("axes")
    @classmethod
    def _unique_axis_names(cls, values: list[MatrixAxis]) -> list[MatrixAxis]:
        names = [axis.name for axis in values]
        if len(names) != len(set(names)):
            raise ValueError("matrix axis names must be unique")
        return values

    @model_validator(mode="after")
    def _bounded(self) -> "ExperimentMatrix":
        _bounded_canonical_json(
            self.model_dump(mode="json"),
            MAX_MATRIX_BYTES,
            "experiment matrix",
        )
        return self


def expand_matrix(matrix: ExperimentMatrix) -> list[dict[str, ScalarParameterValue]]:
    """Deterministically expand a matrix into its full run parameter sets.

    Pure and total: axes are iterated in canonical (sorted-by-name) order and
    each axis's declared value order is preserved, so the same matrix always
    expands to the same ordered list of combinations (`Resolver` in a later
    packet round-robins targets over this exact order). The only failure
    mode is the combinatorial bound: a matrix that expands to more than
    ``MAX_EXPERIMENT_RUNS`` runs is rejected with a typed
    :class:`ExperimentMatrixError` instead of silently truncating.
    """

    ordered_axes = sorted(matrix.axes, key=lambda axis: axis.name)
    total = 1
    for axis in ordered_axes:
        total *= len(axis.values)
        if total > MAX_EXPERIMENT_RUNS:
            raise ExperimentMatrixError(
                "experiment matrix expands to more than "
                f"{MAX_EXPERIMENT_RUNS} runs"
            )
    if total < 1:
        raise ExperimentMatrixError("experiment matrix must expand to at least one run")
    names = [axis.name for axis in ordered_axes]
    combinations = itertools.product(*(axis.values for axis in ordered_axes))
    return [dict(zip(names, combination)) for combination in combinations]


class ExperimentGuard(_StrictModel):
    """Hard, reviewer-facing bounds on one Experiment request.

    ``total_runs`` and ``target_servers`` are the two enforceable limits
    (EX-5); ``total_runs`` is cross-checked against the actual matrix
    expansion count by :class:`ExperimentV2ApprovalPayload`, not here --
    this model alone cannot see the matrix it is guarding. ``est_gpu_hours``
    / ``est_storage`` are bounded display-only declarations: V1 ships no
    resource estimation engine, so nothing downstream computes or enforces
    them.
    """

    total_runs: int = Field(ge=1, le=MAX_EXPERIMENT_RUNS)
    target_servers: list[str] = Field(min_length=1, max_length=MAX_TARGET_SERVERS)
    est_gpu_hours: str | None = Field(default=None, max_length=MAX_ESTIMATION_STRING_BYTES)
    est_storage: str | None = Field(default=None, max_length=MAX_ESTIMATION_STRING_BYTES)

    @field_validator("target_servers")
    @classmethod
    def _target_servers(cls, values: list[str]) -> list[str]:
        for value in values:
            if not isinstance(value, str) or _SERVER_NAME_RE.fullmatch(value) is None:
                raise ValueError("guard target server name is invalid")
        if len(values) != len(set(values)):
            raise ValueError("guard target servers must be unique")
        return values

    @field_validator("est_gpu_hours", "est_storage")
    @classmethod
    def _bounded_display_string(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > MAX_ESTIMATION_STRING_BYTES
        ):
            raise ValueError(f"{info.field_name} must be a bounded display string")
        return value

    @model_validator(mode="after")
    def _bounded(self) -> "ExperimentGuard":
        _bounded_canonical_json(
            self.model_dump(mode="json"),
            MAX_GUARD_BYTES,
            "experiment guard",
        )
        return self


class ExperimentV2ApprovalPayload(_StrictModel):
    """The immutable body a human reviews and decides (EX-1).

    Closes ``matrix`` (verbatim), ``guard``, and the full ``plan_digests``
    list (one SHA-256 per resolved run, in expansion order) into one
    canonical-JSON-digestable, float-free document. ``run_count``,
    ``guard.total_runs``, and ``len(plan_digests)`` must all agree with the
    actual matrix expansion -- any mismatch fails closed before an approval
    can exist, matching EX-1's "any combination resolve failure = whole
    request failure" rule.
    """

    contract_version: Literal["experiment-v2-approval-v1"] = (
        EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION
    )
    project_id: str
    matrix: ExperimentMatrix
    guard: ExperimentGuard
    plan_digests: list[str] = Field(min_length=1, max_length=MAX_EXPERIMENT_RUNS)
    run_count: int = Field(ge=1, le=MAX_EXPERIMENT_RUNS)

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("plan_digests")
    @classmethod
    def _plan_digests(cls, values: list[str]) -> list[str]:
        normalized = [_canonical_sha256(value, "plan_digests[]") for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("plan_digests must be unique")
        return normalized

    @model_validator(mode="after")
    def _closed_contract(self) -> "ExperimentV2ApprovalPayload":
        expansion = expand_matrix(self.matrix)
        if self.run_count != len(expansion):
            raise ValueError("run_count must equal the matrix expansion count")
        if self.guard.total_runs != self.run_count:
            raise ValueError("guard total_runs must equal run_count")
        if len(self.plan_digests) != self.run_count:
            raise ValueError("plan_digests length must equal run_count")
        _bounded_canonical_json(
            self.model_dump(mode="json"),
            MAX_APPROVAL_PAYLOAD_BYTES,
            "experiment v2 approval payload",
        )
        return self


class ExperimentV2Request(_StrictModel):
    """EX-1 request body: shared selection + matrix + guard, no target list.

    Unlike a single `ExecutionPlanV2Request`, target assignment is not a
    caller-supplied selection -- `guard.target_servers` is the declared
    server-name list the resolver round-robins over (`app/experiment_v2_
    store.py`), so this deliberately has no `target_selection` field.
    """

    project_version_id: str
    template_selection: TemplateSelection
    dataset_selection: DatasetSelection
    matrix: ExperimentMatrix
    guard: ExperimentGuard

    @field_validator("project_version_id")
    @classmethod
    def _project_version_id(cls, value: str) -> str:
        return canonical_uuid(value, "project_version_id")


class ExperimentV2SubmitRequest(ExperimentV2Request):
    #: Optional optimistic-concurrency guard, generalizing single-run
    #: `ExecutionPlanV2SubmitRequest.expected_plan_digest` to the N-member
    #: list a preview already returned in expansion order.
    expected_plan_digests: list[str] | None = Field(
        default=None,
        max_length=MAX_EXPERIMENT_RUNS,
    )

    @field_validator("expected_plan_digests")
    @classmethod
    def _expected_plan_digests(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [_canonical_sha256(item, "expected_plan_digests[]") for item in value]


def experiment_v2_approval_payload_digest(payload: ExperimentV2ApprovalPayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def parse_experiment_v2_approval_payload(value: Any) -> ExperimentV2ApprovalPayload:
    return ExperimentV2ApprovalPayload.model_validate(value)


__all__ = [
    "EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION",
    "EXPERIMENT_V2_APPROVAL_KIND",
    "EXPERIMENT_V2_CONTRACT_VERSION",
    "ExperimentGuard",
    "ExperimentMatrix",
    "ExperimentMatrixError",
    "ExperimentV2ApprovalPayload",
    "ExperimentV2Request",
    "ExperimentV2SubmitRequest",
    "MAX_APPROVAL_PAYLOAD_BYTES",
    "MAX_ESTIMATION_STRING_BYTES",
    "MAX_EXPERIMENT_RUNS",
    "MAX_GUARD_BYTES",
    "MAX_MATRIX_AXES",
    "MAX_MATRIX_BYTES",
    "MAX_TARGET_SERVERS",
    "MatrixAxis",
    "expand_matrix",
    "experiment_v2_approval_payload_digest",
    "parse_experiment_v2_approval_payload",
]
