"""Canonical Product ExecutionPlan v2 request, digest, and bridge contracts.

This module is deliberately pure: parsing, canonicalisation, digesting, and
the argv-to-SSH bridge perform no database, filesystem, or network work.  The
database resolver supplies already-verified immutable references and this
module closes them into the exact bytes reviewed by an approver.
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.dataset_assets import canonical_snapshot_id
from app.execution_contract import canonical_json, utf8_sha256
from app.project_bootstrap import ResourceRequirements, ScalarParameterValue


EXECUTION_PLAN_V2_CONTRACT_VERSION = "execution-plan-v2"
EXECUTION_PLAN_V2_APPROVAL_KIND = "execution_plan_v2"
EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION = "execution-plan-v2-approval-v1"
COMMAND_BRIDGE_VERSION: Literal["bash-argv-bridge-v1"] = "bash-argv-bridge-v1"
OBSERVATION_MAX_AGE_SECONDS: Literal[60] = 60

MAX_DATASET_BINDINGS = 32
MAX_CANONICAL_SPEC_BYTES = 256 * 1024
MAX_DATASET_BINDINGS_BYTES = 128 * 1024
MAX_PARAMETER_VALUES_BYTES = 32 * 1024
MAX_RESOURCE_REQUIREMENTS_BYTES = 8 * 1024
MAX_OBSERVATION_BYTES = 4 * 1024

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _canonical_uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UUID")
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, ValueError):
        raise ValueError(f"{field_name} must be a canonical UUID") from None
    if normalized != value:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return normalized


def _canonical_sha256(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _bounded_canonical_json(value: Any, maximum: int, field_name: str) -> str:
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{field_name} exceeds its canonical UTF-8 byte limit")
    return encoded


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectDefaultsTemplateSelection(_StrictModel):
    kind: Literal["project_defaults"]
    project_defaults_revision_id: str | None = None

    @field_validator("project_defaults_revision_id")
    @classmethod
    def _revision_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_uuid(value, "project_defaults_revision_id")


class RunProfileTemplateSelection(_StrictModel):
    kind: Literal["run_profile_revision"]
    run_profile_id: str

    @field_validator("run_profile_id")
    @classmethod
    def _run_profile_id(cls, value: str) -> str:
        return _canonical_uuid(value, "run_profile_id")


TemplateSelection = Annotated[
    Union[ProjectDefaultsTemplateSelection, RunProfileTemplateSelection],
    Field(discriminator="kind"),
]


class ExactSnapshotSelection(_StrictModel):
    kind: Literal["snapshot"]
    snapshot_id: str

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        return canonical_snapshot_id(value)


class DatasetAliasSelection(_StrictModel):
    kind: Literal["alias"]
    alias_name: str

    @field_validator("alias_name")
    @classmethod
    def _alias_name(cls, value: str) -> str:
        if not isinstance(value, str) or _ALIAS_RE.fullmatch(value) is None:
            raise ValueError("alias_name is invalid")
        return value


DatasetBindingSelection = Annotated[
    Union[ExactSnapshotSelection, DatasetAliasSelection],
    Field(discriminator="kind"),
]


class DatasetBindingRequest(_StrictModel):
    name: str
    asset_id: str
    selection: DatasetBindingSelection

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("dataset binding name is invalid")
        return value

    @field_validator("asset_id")
    @classmethod
    def _asset_id(cls, value: str) -> str:
        return _canonical_uuid(value, "asset_id")


class DatasetNoneSelection(_StrictModel):
    kind: Literal["none"]


class DatasetBindingsSelection(_StrictModel):
    kind: Literal["bindings"]
    bindings: list[DatasetBindingRequest] = Field(
        min_length=1,
        max_length=MAX_DATASET_BINDINGS,
    )

    @field_validator("bindings")
    @classmethod
    def _bindings(cls, values: list[DatasetBindingRequest]) -> list[DatasetBindingRequest]:
        names = [value.name for value in values]
        if len(names) != len(set(names)):
            raise ValueError("dataset binding names must be unique")
        ordered = sorted(
            values,
            key=lambda value: (
                value.name,
                value.asset_id,
                getattr(value.selection, "snapshot_id", ""),
                getattr(value.selection, "alias_name", ""),
            ),
        )
        _bounded_canonical_json(
            [value.model_dump(mode="json") for value in ordered],
            MAX_DATASET_BINDINGS_BYTES,
            "dataset bindings",
        )
        return ordered


DatasetSelection = Annotated[
    Union[DatasetNoneSelection, DatasetBindingsSelection],
    Field(discriminator="kind"),
]


class ServerConfigTargetSelection(_StrictModel):
    kind: Literal["server_config_revision"]
    server_config_revision_id: str

    @field_validator("server_config_revision_id")
    @classmethod
    def _revision_id(cls, value: str) -> str:
        return _canonical_uuid(value, "server_config_revision_id")


class DispatchPolicyTargetSelection(_StrictModel):
    kind: Literal["dispatch_policy_revision"]
    dispatch_policy_id: str

    @field_validator("dispatch_policy_id")
    @classmethod
    def _policy_id(cls, value: str) -> str:
        return _canonical_uuid(value, "dispatch_policy_id")


TargetSelection = Annotated[
    Union[ServerConfigTargetSelection, DispatchPolicyTargetSelection],
    Field(discriminator="kind"),
]


class ExecutionPlanV2Request(_StrictModel):
    project_version_id: str
    template_selection: TemplateSelection
    parameter_overrides: dict[str, ScalarParameterValue] = Field(
        default_factory=dict,
        max_length=32,
    )
    dataset_selection: DatasetSelection
    target_selection: TargetSelection

    @field_validator("project_version_id")
    @classmethod
    def _project_version_id(cls, value: str) -> str:
        return _canonical_uuid(value, "project_version_id")

    @field_validator("parameter_overrides")
    @classmethod
    def _parameter_overrides(
        cls,
        values: dict[str, ScalarParameterValue],
    ) -> dict[str, ScalarParameterValue]:
        for name in values:
            if _IDENTIFIER_RE.fullmatch(name) is None:
                raise ValueError("parameter override name is invalid")
        ordered = dict(sorted(values.items()))
        _bounded_canonical_json(
            ordered,
            MAX_PARAMETER_VALUES_BYTES,
            "parameter overrides",
        )
        return ordered


class ExecutionPlanV2SubmitRequest(ExecutionPlanV2Request):
    expected_plan_digest: str

    @field_validator("expected_plan_digest")
    @classmethod
    def _expected_digest(cls, value: str) -> str:
        return _canonical_sha256(value, "expected_plan_digest")


class ProjectVersionReference(_StrictModel):
    project_version_id: str
    git_commit: str = Field(min_length=40, max_length=64)
    bundle_sha256: str

    @field_validator("project_version_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _canonical_uuid(value, "project_version_id")

    @field_validator("git_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("git_commit must be lowercase hexadecimal")
        return value

    @field_validator("bundle_sha256")
    @classmethod
    def _bundle(cls, value: str) -> str:
        return _canonical_sha256(value, "bundle_sha256")


class ProjectDefaultsReference(_StrictModel):
    project_defaults_revision_id: str
    revision_digest: str

    @field_validator("project_defaults_revision_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _canonical_uuid(value, "project_defaults_revision_id")

    @field_validator("revision_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _canonical_sha256(value, "project defaults revision_digest")


class RunProfileReference(_StrictModel):
    run_profile_id: str
    spec_digest: str

    @field_validator("run_profile_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _canonical_uuid(value, "run_profile_id")

    @field_validator("spec_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _canonical_sha256(value, "run profile spec_digest")


class EnvironmentReference(_StrictModel):
    environment_revision_id: str
    revision_digest: str

    @field_validator("environment_revision_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _canonical_uuid(value, "environment_revision_id")

    @field_validator("revision_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _canonical_sha256(value, "environment revision_digest")


class DatasetSelectionProvenance(_StrictModel):
    kind: Literal["snapshot", "alias"]
    alias_revision_id: str | None = None
    alias_revision_digest: str | None = None

    @field_validator("alias_revision_id")
    @classmethod
    def _alias_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_uuid(value, "alias_revision_id")

    @field_validator("alias_revision_digest")
    @classmethod
    def _alias_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_sha256(value, "alias_revision_digest")

    @model_validator(mode="after")
    def _closed_shape(self) -> "DatasetSelectionProvenance":
        alias = self.kind == "alias"
        if alias != (
            self.alias_revision_id is not None
            and self.alias_revision_digest is not None
        ):
            raise ValueError("alias selection provenance is incomplete")
        if not alias and (
            self.alias_revision_id is not None
            or self.alias_revision_digest is not None
        ):
            raise ValueError("snapshot selection cannot carry alias provenance")
        return self


class DatasetEntitlementProvenance(_StrictModel):
    access_mode: Literal["owned", "shared"]
    asset_digest: str
    manifest_digest: str
    grant_id: str | None = None
    grant_digest: str | None = None
    offer_id: str | None = None
    offer_digest: str | None = None
    accept_approval_id: int | None = Field(default=None, ge=1)

    @field_validator("grant_id", "offer_id")
    @classmethod
    def _ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _canonical_uuid(value, info.field_name)

    @field_validator("asset_digest", "manifest_digest", "grant_digest", "offer_digest")
    @classmethod
    def _digests(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _canonical_sha256(value, info.field_name)

    @model_validator(mode="after")
    def _closed_shape(self) -> "DatasetEntitlementProvenance":
        shared_fields = (
            self.grant_id,
            self.grant_digest,
            self.offer_id,
            self.offer_digest,
            self.accept_approval_id,
        )
        if self.access_mode == "shared":
            if any(value is None for value in shared_fields):
                raise ValueError("shared Dataset entitlement provenance is incomplete")
        elif any(value is not None for value in shared_fields):
            raise ValueError("owned Dataset cannot carry share provenance")
        return self


class ResolvedDatasetBinding(_StrictModel):
    name: str
    asset_id: str
    snapshot_id: str
    selection: DatasetSelectionProvenance
    entitlement: DatasetEntitlementProvenance

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("dataset binding name is invalid")
        return value

    @field_validator("asset_id")
    @classmethod
    def _asset_id(cls, value: str) -> str:
        return _canonical_uuid(value, "asset_id")

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        return canonical_snapshot_id(value)


class TargetReference(_StrictModel):
    selection_kind: Literal["server_config_revision", "dispatch_policy_revision"]
    dispatch_policy_id: str | None = None
    dispatch_policy_digest: str | None = None
    server_config_revision_id: str
    target_identity_sha256: str
    server_name: str = Field(min_length=1, max_length=128)

    @field_validator("dispatch_policy_id", "server_config_revision_id")
    @classmethod
    def _ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _canonical_uuid(value, info.field_name)

    @field_validator("dispatch_policy_digest", "target_identity_sha256")
    @classmethod
    def _digests(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _canonical_sha256(value, info.field_name)

    @model_validator(mode="after")
    def _policy_pair(self) -> "TargetReference":
        policy = self.selection_kind == "dispatch_policy_revision"
        if policy != (
            self.dispatch_policy_id is not None
            and self.dispatch_policy_digest is not None
        ):
            raise ValueError("dispatch policy target provenance is incomplete")
        return self


class ProjectInstanceReference(_StrictModel):
    project_instance_id: str = Field(min_length=1, max_length=128)
    checkout_evidence_digest: str

    @field_validator("checkout_evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _canonical_sha256(value, "checkout_evidence_digest")


class ExecutionPlanV2Spec(_StrictModel):
    contract_version: Literal["execution-plan-v2"] = "execution-plan-v2"
    project_id: str
    project_version: ProjectVersionReference
    project_defaults: ProjectDefaultsReference | None
    run_profile: RunProfileReference
    environment: EnvironmentReference
    parameter_values: dict[str, ScalarParameterValue] = Field(max_length=32)
    dataset_none: bool
    dataset_bindings: list[ResolvedDatasetBinding] = Field(
        max_length=MAX_DATASET_BINDINGS,
    )
    target: TargetReference
    backend: Literal["ssh"] = "ssh"
    project_instance: ProjectInstanceReference
    resource_requirements: ResourceRequirements
    resource_requirements_digest: str
    observation_max_age_seconds: Literal[60] = OBSERVATION_MAX_AGE_SECONDS
    canonical_argv_sha256: str
    output_declarations_digest: str
    command_bridge_version: Literal["bash-argv-bridge-v1"] = COMMAND_BRIDGE_VERSION
    compiled_command_sha256: str
    job_command_sha256: str
    plan_digest: str

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return _canonical_uuid(value, "project_id")

    @field_validator("parameter_values")
    @classmethod
    def _parameters(
        cls,
        values: dict[str, ScalarParameterValue],
    ) -> dict[str, ScalarParameterValue]:
        ordered = dict(sorted(values.items()))
        if values != ordered:
            raise ValueError("parameter values must be in canonical key order")
        for name in ordered:
            if _IDENTIFIER_RE.fullmatch(name) is None:
                raise ValueError("parameter value name is invalid")
        _bounded_canonical_json(
            ordered,
            MAX_PARAMETER_VALUES_BYTES,
            "parameter values",
        )
        return ordered

    @field_validator("dataset_bindings")
    @classmethod
    def _bindings(
        cls,
        values: list[ResolvedDatasetBinding],
    ) -> list[ResolvedDatasetBinding]:
        keys = [(value.name, value.asset_id, value.snapshot_id) for value in values]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("resolved Dataset bindings must be sorted and unique")
        _bounded_canonical_json(
            [value.model_dump(mode="json") for value in values],
            MAX_DATASET_BINDINGS_BYTES,
            "resolved Dataset bindings",
        )
        return values

    @field_validator(
        "resource_requirements_digest",
        "canonical_argv_sha256",
        "output_declarations_digest",
        "compiled_command_sha256",
        "job_command_sha256",
        "plan_digest",
    )
    @classmethod
    def _digests(cls, value: str, info: Any) -> str:
        return _canonical_sha256(value, info.field_name)

    @model_validator(mode="after")
    def _closed_contract(self) -> "ExecutionPlanV2Spec":
        if self.dataset_none != (not self.dataset_bindings):
            raise ValueError("dataset_none must exactly represent an empty binding set")
        resources = self.resource_requirements.model_dump(mode="json")
        _bounded_canonical_json(
            resources,
            MAX_RESOURCE_REQUIREMENTS_BYTES,
            "resource requirements",
        )
        if utf8_sha256(canonical_json(resources)) != self.resource_requirements_digest:
            raise ValueError("resource requirements digest mismatch")
        if self.compiled_command_sha256 != self.canonical_argv_sha256:
            raise ValueError("compiled command digest must equal canonical argv digest")
        body = self.model_dump(mode="json", exclude={"plan_digest"})
        if utf8_sha256(canonical_json(body)) != self.plan_digest:
            raise ValueError("ExecutionPlan v2 digest mismatch")
        _bounded_canonical_json(
            self.model_dump(mode="json"),
            MAX_CANONICAL_SPEC_BYTES,
            "ExecutionPlan v2 spec",
        )
        return self


class ExecutionPlanV2ApprovalPayload(_StrictModel):
    contract_version: Literal["execution-plan-v2-approval-v1"] = (
        "execution-plan-v2-approval-v1"
    )
    execution_plan_id: str
    project_id: str
    plan_digest: str

    @field_validator("execution_plan_id", "project_id")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _canonical_uuid(value, info.field_name)

    @field_validator("plan_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _canonical_sha256(value, "plan_digest")


class SubmitObservationProvenance(_StrictModel):
    observation_id: int = Field(ge=1)
    server_name: str = Field(min_length=1, max_length=128)
    observed_at: str = Field(min_length=1, max_length=64)
    online: Literal[True]
    probe_ok: Literal[True]
    gpu_count: int = Field(ge=0)
    gpu_mem_used_mb: int | str
    gpu_mem_total_mb: int | str
    mem_available_bytes: int = Field(ge=0)
    disk_avail_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> "SubmitObservationProvenance":
        _bounded_canonical_json(
            self.model_dump(mode="json"),
            MAX_OBSERVATION_BYTES,
            "submit observation provenance",
        )
        return self


def build_execution_plan_v2_spec(**body: Any) -> ExecutionPlanV2Spec:
    """Close a verified semantic body by adding its canonical plan digest."""

    normalized = dict(body)
    normalized.pop("plan_digest", None)
    normalized["plan_digest"] = utf8_sha256(canonical_json(normalized))
    return ExecutionPlanV2Spec.model_validate(normalized)


def execution_plan_v2_approval_payload_digest(
    payload: ExecutionPlanV2ApprovalPayload,
) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def parse_execution_plan_v2_spec(value: Any) -> ExecutionPlanV2Spec:
    return ExecutionPlanV2Spec.model_validate(value)


def parse_execution_plan_v2_approval_payload(
    value: Any,
) -> ExecutionPlanV2ApprovalPayload:
    return ExecutionPlanV2ApprovalPayload.model_validate(value)


def project_instance_checkout_digest(
    *,
    project_instance_id: str,
    project_id: str,
    server_name: str,
    path: str,
    git_commit: str,
) -> str:
    """Digest exact checkout evidence without exposing the checkout path."""

    return utf8_sha256(
        canonical_json(
            {
                "dirty": False,
                "git_commit": git_commit,
                "path_sha256": utf8_sha256(path),
                "project_id": _canonical_uuid(project_id, "project_id"),
                "project_instance_id": project_instance_id,
                "server_name": server_name,
                "state": "available",
            }
        )
    )


def dispatch_policy_digest(policy: Mapping[str, Any]) -> str:
    """Digest the immutable fields that make one policy revision meaningful."""

    body = {
        "allowed_servers": sorted(set(policy["allowed_servers"])),
        "dataset_required": bool(policy["dataset_required"]),
        "dispatch_policy_id": policy["id"],
        "max_concurrent_placements": int(policy["max_concurrent_placements"]),
        "name": policy["name"],
        "project_id": policy["project_id"],
        "require_tag": policy.get("require_tag"),
        "revision": int(policy["revision"]),
        "run_profile_id": policy.get("run_profile_id"),
        "status": policy["status"],
        "valid_until": policy.get("valid_until"),
    }
    return utf8_sha256(canonical_json(body))


def build_bash_argv_bridge(
    *,
    checkout_path: str,
    git_commit: str,
    setup_command: str,
    argv: tuple[str, ...],
    preflight_lines: Sequence[str] = (),
) -> str:
    """Build the only Product v2 Job command from closed, approved inputs."""

    if not isinstance(checkout_path, str) or not checkout_path:
        raise ValueError("checkout path is required")
    if not isinstance(git_commit, str) or not git_commit:
        raise ValueError("git commit is required")
    if not isinstance(setup_command, str):
        raise ValueError("setup command must be text")
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise ValueError("compiled argv must contain non-empty text elements")
    prefix = "\n".join(
        (
            "set -euo pipefail",
            f"cd -- {shlex.quote(checkout_path)}",
            f'test "$(git rev-parse HEAD)" = {shlex.quote(git_commit)}',
            'test -z "$(git status --porcelain)"',
        )
    )
    command = prefix + "\n"
    for line in preflight_lines:
        if not isinstance(line, str) or not line or "\n" in line:
            raise ValueError("preflight lines must be single non-empty lines")
        command += line + "\n"
    if setup_command:
        command += setup_command
        if not setup_command.endswith("\n"):
            command += "\n"
    command += "exec " + " ".join(shlex.quote(value) for value in argv)
    return command


__all__ = [
    "COMMAND_BRIDGE_VERSION",
    "DatasetBindingRequest",
    "DatasetBindingsSelection",
    "DatasetNoneSelection",
    "DispatchPolicyTargetSelection",
    "EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION",
    "EXECUTION_PLAN_V2_APPROVAL_KIND",
    "EXECUTION_PLAN_V2_CONTRACT_VERSION",
    "ExecutionPlanV2ApprovalPayload",
    "ExecutionPlanV2Request",
    "ExecutionPlanV2Spec",
    "ExecutionPlanV2SubmitRequest",
    "MAX_CANONICAL_SPEC_BYTES",
    "MAX_DATASET_BINDINGS_BYTES",
    "MAX_OBSERVATION_BYTES",
    "MAX_PARAMETER_VALUES_BYTES",
    "MAX_RESOURCE_REQUIREMENTS_BYTES",
    "OBSERVATION_MAX_AGE_SECONDS",
    "ResolvedDatasetBinding",
    "ServerConfigTargetSelection",
    "SubmitObservationProvenance",
    "build_bash_argv_bridge",
    "build_execution_plan_v2_spec",
    "dispatch_policy_digest",
    "execution_plan_v2_approval_payload_digest",
    "parse_execution_plan_v2_approval_payload",
    "parse_execution_plan_v2_spec",
    "project_instance_checkout_digest",
]
