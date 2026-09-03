"""Canonical Product v2 Run Template, Defaults, and argv compiler contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution_contract import canonical_json, utf8_sha256
from app.project_bootstrap import (
    PROJECT_DEFAULTS_CONTRACT_VERSION,
    RUN_TEMPLATE_SPEC_CONTRACT_VERSION,
    ProjectDefaultsContract,
    ProjectDefaultsInput,
    RunParameterSpec,
    RunTemplateContract,
    RunTemplateSpecInput,
    canonical_uuid,
    validate_parameter_defaults,
)


RUN_TEMPLATE_CHANGE_CONTRACT_VERSION = "run-template-change-v2"
PROJECT_DEFAULTS_CHANGE_CONTRACT_VERSION = "project-defaults-change-v2"
RUN_TEMPLATE_CLASSIFICATION_TYPED = "typed_spec"
RUN_TEMPLATE_CLASSIFICATION_LEGACY = "legacy_raw_command"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_ARGV_ELEMENT_BYTES = 4096
_MAX_CANONICAL_ARGV_BYTES = 65536


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _RunTemplateChangeBase(_StrictContract):
    operation: Literal["create", "adopt", "update", "archive"]


class RunTemplateCreateRequest(_RunTemplateChangeBase):
    operation: Literal["create"]
    expected_revision: Literal[0]
    expected_head_run_profile_id: None = None
    expected_head_classification: None = None
    expected_head_spec_digest: None = None
    expected_environment_head_revision_id: str
    template: RunTemplateSpecInput

    @field_validator("expected_environment_head_revision_id")
    @classmethod
    def _environment_id(cls, value: str) -> str:
        return canonical_uuid(value, "expected_environment_head_revision_id")


class RunTemplateAdoptRequest(_RunTemplateChangeBase):
    operation: Literal["adopt"]
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_run_profile_id: str
    expected_head_classification: Literal["legacy_raw_command"]
    expected_head_spec_digest: None = None
    expected_environment_head_revision_id: str
    template: RunTemplateSpecInput

    @field_validator(
        "expected_head_run_profile_id",
        "expected_environment_head_revision_id",
    )
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)


class RunTemplateUpdateRequest(_RunTemplateChangeBase):
    operation: Literal["update"]
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_run_profile_id: str
    expected_head_classification: Literal["typed_spec"]
    expected_head_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_environment_head_revision_id: str
    template: RunTemplateSpecInput

    @field_validator(
        "expected_head_run_profile_id",
        "expected_environment_head_revision_id",
    )
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)


class RunTemplateArchiveRequest(_RunTemplateChangeBase):
    operation: Literal["archive"]
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_run_profile_id: str
    expected_head_classification: Literal["typed_spec"]
    expected_head_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("expected_head_run_profile_id")
    @classmethod
    def _head_id(cls, value: str) -> str:
        return canonical_uuid(value, "expected_head_run_profile_id")


RunTemplateChangeRequest = Annotated[
    Union[
        RunTemplateCreateRequest,
        RunTemplateAdoptRequest,
        RunTemplateUpdateRequest,
        RunTemplateArchiveRequest,
    ],
    Field(discriminator="operation"),
]


class RunTemplateChangePayload(_StrictContract):
    contract_version: Literal["run-template-change-v2"] = "run-template-change-v2"
    operation: Literal["create", "adopt", "update", "archive"]
    project_id: str
    expected_revision: int = Field(ge=0, le=2_147_483_646)
    expected_head_run_profile_id: str | None = None
    expected_head_classification: Literal["legacy_raw_command", "typed_spec"] | None = (
        None
    )
    expected_head_spec_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_environment_head_revision_id: str | None = None
    target_revision: RunTemplateContract

    @field_validator(
        "project_id",
        "expected_head_run_profile_id",
        "expected_environment_head_revision_id",
    )
    @classmethod
    def _ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return canonical_uuid(value, info.field_name)

    @model_validator(mode="after")
    def _transition(self) -> "RunTemplateChangePayload":
        target = self.target_revision
        if self.operation == "create":
            valid = (
                self.expected_revision == 0
                and self.expected_head_run_profile_id is None
                and self.expected_head_classification is None
                and self.expected_head_spec_digest is None
                and self.expected_environment_head_revision_id is not None
                and target.revision == 1
            )
        elif self.operation == "adopt":
            valid = (
                self.expected_revision >= 1
                and self.expected_head_run_profile_id is not None
                and self.expected_head_classification
                == RUN_TEMPLATE_CLASSIFICATION_LEGACY
                and self.expected_head_spec_digest is None
                and self.expected_environment_head_revision_id is not None
                and target.revision == self.expected_revision + 1
            )
        elif self.operation == "update":
            valid = (
                self.expected_revision >= 1
                and self.expected_head_run_profile_id is not None
                and self.expected_head_classification
                == RUN_TEMPLATE_CLASSIFICATION_TYPED
                and self.expected_head_spec_digest is not None
                and self.expected_environment_head_revision_id is not None
                and target.revision == self.expected_revision + 1
            )
        else:
            valid = (
                self.expected_revision >= 1
                and self.expected_head_run_profile_id is not None
                and self.expected_head_classification
                == RUN_TEMPLATE_CLASSIFICATION_TYPED
                and self.expected_head_spec_digest is not None
                and self.expected_environment_head_revision_id is None
                and target.revision == self.expected_revision + 1
            )
        if not valid:
            raise ValueError("run template revision transition is invalid")
        if (
            self.operation != "archive"
            and target.environment_revision_id
            != self.expected_environment_head_revision_id
        ):
            raise ValueError("run template environment head binding is invalid")
        return self


class _ProjectDefaultsChangeBase(_StrictContract):
    expected_template_head_run_profile_id: str
    expected_template_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_environment_head_revision_id: str
    defaults: ProjectDefaultsInput

    @field_validator(
        "expected_template_head_run_profile_id",
        "expected_environment_head_revision_id",
    )
    @classmethod
    def _reference_ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)


class ProjectDefaultsCreateRequest(_ProjectDefaultsChangeBase):
    operation: Literal["create"]
    expected_revision: Literal[0]
    expected_head_revision_id: None = None
    expected_head_revision_digest: None = None


class ProjectDefaultsUpdateRequest(_ProjectDefaultsChangeBase):
    operation: Literal["update"]
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_revision_id: str
    expected_head_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("expected_head_revision_id")
    @classmethod
    def _head_id(cls, value: str) -> str:
        return canonical_uuid(value, "expected_head_revision_id")


ProjectDefaultsChangeRequest = Annotated[
    Union[ProjectDefaultsCreateRequest, ProjectDefaultsUpdateRequest],
    Field(discriminator="operation"),
]


class ProjectDefaultsChangePayload(_StrictContract):
    contract_version: Literal["project-defaults-change-v2"] = (
        "project-defaults-change-v2"
    )
    operation: Literal["create", "update"]
    project_id: str
    expected_revision: int = Field(ge=0, le=2_147_483_646)
    expected_head_revision_id: str | None = None
    expected_head_revision_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_template_head_run_profile_id: str
    expected_template_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_environment_head_revision_id: str
    target_revision: ProjectDefaultsContract

    @field_validator(
        "project_id",
        "expected_head_revision_id",
        "expected_template_head_run_profile_id",
        "expected_environment_head_revision_id",
    )
    @classmethod
    def _ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return canonical_uuid(value, info.field_name)

    @model_validator(mode="after")
    def _transition(self) -> "ProjectDefaultsChangePayload":
        target = self.target_revision
        if self.operation == "create":
            valid = (
                self.expected_revision == 0
                and self.expected_head_revision_id is None
                and self.expected_head_revision_digest is None
                and target.revision == 1
            )
        else:
            valid = (
                self.expected_revision >= 1
                and self.expected_head_revision_id is not None
                and self.expected_head_revision_digest is not None
                and target.revision == self.expected_revision + 1
            )
        if not valid:
            raise ValueError("project defaults revision transition is invalid")
        if (
            target.run_profile_id != self.expected_template_head_run_profile_id
            or target.run_profile_spec_digest
            != self.expected_template_spec_digest
            or target.environment_revision_id
            != self.expected_environment_head_revision_id
        ):
            raise ValueError("project defaults exact references are invalid")
        return self


@dataclass(frozen=True)
class CompiledArgv:
    argv: tuple[str, ...]
    canonical_argv_bytes: bytes
    argv_sha256: str


def _parameter_element(parameter: RunParameterSpec, value: object) -> str:
    normalized = parameter.validate_value(value)
    if parameter.type == "boolean":
        return "true" if normalized else "false"
    if parameter.type == "integer":
        return str(normalized)
    if parameter.type == "number":
        return str(normalized)
    if not isinstance(normalized, str):  # pragma: no cover - model invariant
        raise ValueError(f"parameter {parameter.name} did not compile to text")
    return normalized


def compile_structured_argv(
    template: RunTemplateContract | RunTemplateSpecInput,
    parameter_values: Mapping[str, object],
    *,
    image_path: str | None = None,
    power_literal: str | None = None,
) -> CompiledArgv:
    """Compile exact argv elements without shell parsing, interpolation, or I/O.

    ``image_path`` fills the single ``image`` token of a ``program`` template
    (DG-HARDWARE-EXECUTION v1 P3); it is an absolute worker path Server A
    derived, never requester input.
    """

    if not isinstance(parameter_values, Mapping):
        raise ValueError("parameter values must be an object")
    parameters = {parameter.name: parameter for parameter in template.parameter_schema}
    supplied = set(parameter_values)
    if any(not isinstance(name, str) for name in supplied):
        raise ValueError("parameter names must be strings")
    unknown = supplied - set(parameters)
    if unknown:
        raise ValueError("parameter values contain an unknown parameter")
    missing = {
        parameter.name
        for parameter in template.parameter_schema
        if parameter.required and parameter.name not in supplied
    }
    if missing:
        raise ValueError("parameter values omit a required parameter")

    argv: list[str] = []
    for token in template.argv_template:
        if token.kind == "literal":
            assert token.value is not None
            element = token.value
        elif token.kind == "image":
            if not isinstance(image_path, str) or not image_path.startswith("/"):
                raise ValueError("image argv token requires an absolute image path")
            element = image_path
        elif token.kind == "power_sequence":
            if not isinstance(power_literal, str) or not power_literal:
                raise ValueError("power_sequence argv token requires a power literal")
            element = power_literal
        else:
            assert token.name is not None
            if token.name not in supplied:
                continue
            element = _parameter_element(parameters[token.name], parameter_values[token.name])
        if _CONTROL_RE.search(element):
            raise ValueError("compiled argv contains a control character")
        if len(element.encode("utf-8")) > _MAX_ARGV_ELEMENT_BYTES:
            raise ValueError("compiled argv element exceeds 4096 UTF-8 bytes")
        argv.append(element)

    canonical_text = canonical_json(argv)
    canonical_bytes = canonical_text.encode("utf-8")
    if len(canonical_bytes) > _MAX_CANONICAL_ARGV_BYTES:
        raise ValueError("canonical argv exceeds 65536 bytes")
    return CompiledArgv(
        argv=tuple(argv),
        canonical_argv_bytes=canonical_bytes,
        argv_sha256=utf8_sha256(canonical_text),
    )


def build_run_template_revision(
    template: RunTemplateSpecInput,
    *,
    run_profile_id: str,
    environment_revision_id: str,
    revision: int,
) -> RunTemplateContract:
    body = {
        **template.model_dump(mode="json"),
        "run_profile_id": canonical_uuid(run_profile_id, "run_profile_id"),
        "revision": revision,
        "contract_version": RUN_TEMPLATE_SPEC_CONTRACT_VERSION,
        "environment_revision_id": canonical_uuid(
            environment_revision_id,
            "environment_revision_id",
        ),
    }
    body["spec_digest"] = utf8_sha256(canonical_json(body))
    return RunTemplateContract.model_validate(body)


def build_project_defaults_revision(
    defaults: ProjectDefaultsInput,
    *,
    revision_id: str,
    revision: int,
    environment_revision_id: str,
    run_profile_id: str,
    run_profile_spec_digest: str,
    parameter_schema: list[RunParameterSpec],
) -> ProjectDefaultsContract:
    validate_parameter_defaults(parameter_schema, defaults.parameter_values)
    body = {
        **defaults.model_dump(mode="json"),
        "revision_id": canonical_uuid(revision_id, "revision_id"),
        "revision": revision,
        "contract_version": PROJECT_DEFAULTS_CONTRACT_VERSION,
        "environment_revision_id": canonical_uuid(
            environment_revision_id,
            "environment_revision_id",
        ),
        "run_profile_id": canonical_uuid(run_profile_id, "run_profile_id"),
        "run_profile_spec_digest": run_profile_spec_digest,
    }
    body["revision_digest"] = utf8_sha256(canonical_json(body))
    return ProjectDefaultsContract.model_validate(body)


def run_template_change_payload_digest(payload: RunTemplateChangePayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def project_defaults_change_payload_digest(
    payload: ProjectDefaultsChangePayload,
) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def parse_run_template_change_payload(value: Any) -> RunTemplateChangePayload:
    return RunTemplateChangePayload.model_validate(value)


def parse_project_defaults_change_payload(
    value: Any,
) -> ProjectDefaultsChangePayload:
    return ProjectDefaultsChangePayload.model_validate(value)


__all__ = [
    "CompiledArgv",
    "PROJECT_DEFAULTS_CHANGE_CONTRACT_VERSION",
    "ProjectDefaultsChangePayload",
    "ProjectDefaultsChangeRequest",
    "ProjectDefaultsCreateRequest",
    "ProjectDefaultsUpdateRequest",
    "RUN_TEMPLATE_CHANGE_CONTRACT_VERSION",
    "RUN_TEMPLATE_CLASSIFICATION_LEGACY",
    "RUN_TEMPLATE_CLASSIFICATION_TYPED",
    "RunTemplateAdoptRequest",
    "RunTemplateArchiveRequest",
    "RunTemplateChangePayload",
    "RunTemplateChangeRequest",
    "RunTemplateCreateRequest",
    "RunTemplateUpdateRequest",
    "build_project_defaults_revision",
    "build_run_template_revision",
    "compile_structured_argv",
    "parse_project_defaults_change_payload",
    "parse_run_template_change_payload",
    "project_defaults_change_payload_digest",
    "run_template_change_payload_digest",
]
