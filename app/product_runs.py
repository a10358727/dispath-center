"""Pure Product Run request and comparison contracts.

Run state remains a projection of ExecutionPlan, approval, Job, attempt, and
outbox evidence.  This module intentionally defines no persisted Product Run
record and performs no I/O.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.execution_contract import canonical_json, canonical_json_sha256
from app.execution_plan_v2 import (
    MAX_PARAMETER_VALUES_BYTES,
    DatasetBindingRequest,
    DatasetBindingsSelection,
    DatasetNoneSelection,
    ExactSnapshotSelection,
    ExecutionPlanV2Request,
    ExecutionPlanV2Spec,
    RunProfileTemplateSelection,
    ServerConfigTargetSelection,
)
from app.project_bootstrap import ScalarParameterValue


ProductRunState = Literal[
    "awaiting_approval",
    "rejected",
    "queued",
    "preparing",
    "running",
    "stopping",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
    "needs_attention",
]
DimensionAvailability = Literal["known", "unknown"]

PRODUCT_RUN_STATES = frozenset(
    {
        "awaiting_approval",
        "rejected",
        "queued",
        "preparing",
        "running",
        "stopping",
        "succeeded",
        "failed",
        "cancelled",
        "blocked",
        "needs_attention",
    }
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProductRunClonePreviewRequest(_StrictModel):
    """The only caller-controlled part of a Clone preview."""

    parameter_overrides: dict[str, ScalarParameterValue] = Field(
        default_factory=dict,
        max_length=32,
    )

    @field_validator("parameter_overrides")
    @classmethod
    def _bounded_overrides(
        cls,
        values: dict[str, ScalarParameterValue],
    ) -> dict[str, ScalarParameterValue]:
        for name in values:
            if _IDENTIFIER_RE.fullmatch(name) is None:
                raise ValueError("parameter override name is invalid")
        ordered = dict(sorted(values.items()))
        if len(canonical_json(ordered).encode("utf-8")) > MAX_PARAMETER_VALUES_BYTES:
            raise ValueError("parameter overrides exceed the canonical size limit")
        return ordered


class ProductRunStopRequest(_StrictModel):
    """Stop has no caller-defined execution data; source is server-fixed."""


def build_product_run_clone_request(
    spec: ExecutionPlanV2Spec,
    overrides: ProductRunClonePreviewRequest,
) -> ExecutionPlanV2Request:
    """Rebuild a preview request from exact immutable source references.

    Alias and policy provenance are deliberately collapsed to their already
    resolved snapshot and server-config revision.  Clone cannot follow a new
    alias/default/policy head or reselect a different worker.
    """

    parameter_values = dict(spec.parameter_values)
    parameter_values.update(overrides.parameter_overrides)
    if spec.dataset_none:
        dataset_selection: DatasetNoneSelection | DatasetBindingsSelection = DatasetNoneSelection(
            kind="none"
        )
    else:
        dataset_selection = DatasetBindingsSelection(
            kind="bindings",
            bindings=[
                DatasetBindingRequest(
                    name=binding.name,
                    asset_id=binding.asset_id,
                    selection=ExactSnapshotSelection(
                        kind="snapshot",
                        snapshot_id=binding.snapshot_id,
                    ),
                )
                for binding in spec.dataset_bindings
            ],
        )
    return ExecutionPlanV2Request(
        project_version_id=spec.project_version.project_version_id,
        template_selection=RunProfileTemplateSelection(
            kind="run_profile_revision",
            run_profile_id=spec.run_profile.run_profile_id,
        ),
        parameter_overrides=parameter_values,
        dataset_selection=dataset_selection,
        target_selection=ServerConfigTargetSelection(
            kind="server_config_revision",
            server_config_revision_id=spec.target.server_config_revision_id,
        ),
    )


def _known_dimension(left: Any, right: Any) -> dict[str, Any]:
    return {
        "availability": "known",
        "equal": left == right,
        "left": left,
        "right": right,
    }


def _unknown_dimension() -> dict[str, Any]:
    return {
        "availability": "unknown",
        "equal": None,
        "left": None,
        "right": None,
    }


def _safe_parameter_comparison(
    left: dict[str, ScalarParameterValue],
    right: dict[str, ScalarParameterValue],
) -> dict[str, Any]:
    left_keys = sorted(left)
    right_keys = sorted(right)
    changed_keys = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
    return _known_dimension(
        {
            "keys": left_keys,
            "digest": canonical_json_sha256(left),
        },
        {
            "keys": right_keys,
            "digest": canonical_json_sha256(right),
        },
    ) | {"changed_keys": changed_keys}


def compare_product_run_dimensions(
    *,
    left_spec: ExecutionPlanV2Spec | None,
    right_spec: ExecutionPlanV2Spec | None,
    left_terminal: dict[str, Any] | None,
    right_terminal: dict[str, Any] | None,
    left_artifacts: dict[str, Any] | None,
    right_artifacts: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return fixed, safe comparison dimensions without raw parameter values."""

    if left_spec is None or right_spec is None:
        typed: dict[str, dict[str, Any]] = {
            name: _unknown_dimension()
            for name in (
                "project_version",
                "template",
                "environment",
                "parameters",
                "datasets",
                "resources",
                "target",
            )
        }
    else:
        typed = {
            "project_version": _known_dimension(
                {
                    "id": left_spec.project_version.project_version_id,
                    "bundle_sha256": left_spec.project_version.bundle_sha256,
                    "git_commit": left_spec.project_version.git_commit,
                },
                {
                    "id": right_spec.project_version.project_version_id,
                    "bundle_sha256": right_spec.project_version.bundle_sha256,
                    "git_commit": right_spec.project_version.git_commit,
                },
            ),
            "template": _known_dimension(
                {
                    "run_profile_id": left_spec.run_profile.run_profile_id,
                    "spec_digest": left_spec.run_profile.spec_digest,
                },
                {
                    "run_profile_id": right_spec.run_profile.run_profile_id,
                    "spec_digest": right_spec.run_profile.spec_digest,
                },
            ),
            "environment": _known_dimension(
                {
                    "revision_id": left_spec.environment.environment_revision_id,
                    "revision_digest": left_spec.environment.revision_digest,
                },
                {
                    "revision_id": right_spec.environment.environment_revision_id,
                    "revision_digest": right_spec.environment.revision_digest,
                },
            ),
            "parameters": _safe_parameter_comparison(
                left_spec.parameter_values,
                right_spec.parameter_values,
            ),
            "datasets": _known_dimension(
                [
                    {
                        "name": item.name,
                        "asset_id": item.asset_id,
                        "snapshot_id": item.snapshot_id,
                    }
                    for item in left_spec.dataset_bindings
                ],
                [
                    {
                        "name": item.name,
                        "asset_id": item.asset_id,
                        "snapshot_id": item.snapshot_id,
                    }
                    for item in right_spec.dataset_bindings
                ],
            ),
            "resources": _known_dimension(
                left_spec.resource_requirements.model_dump(mode="json"),
                right_spec.resource_requirements.model_dump(mode="json"),
            ),
            "target": _known_dimension(
                {
                    "backend": left_spec.backend,
                    "server_config_revision_id": (left_spec.target.server_config_revision_id),
                    "target_identity_sha256": left_spec.target.target_identity_sha256,
                },
                {
                    "backend": right_spec.backend,
                    "server_config_revision_id": (right_spec.target.server_config_revision_id),
                    "target_identity_sha256": right_spec.target.target_identity_sha256,
                },
            ),
        }
    typed["terminal_result"] = (
        _known_dimension(left_terminal, right_terminal)
        if left_terminal is not None and right_terminal is not None
        else _unknown_dimension()
    )
    typed["artifact_metadata"] = (
        _known_dimension(left_artifacts, right_artifacts)
        if left_artifacts is not None and right_artifacts is not None
        else _unknown_dimension()
    )
    return typed


__all__ = [
    "PRODUCT_RUN_STATES",
    "ProductRunClonePreviewRequest",
    "ProductRunState",
    "ProductRunStopRequest",
    "build_product_run_clone_request",
    "compare_product_run_dimensions",
]
