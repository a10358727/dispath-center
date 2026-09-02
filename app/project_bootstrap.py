"""Canonical Product v2 Project bootstrap contracts.

The preview and the approval materializer share these models.  That keeps the
immutable bytes reviewed by a human identical to the bytes accepted inside the
approval transaction; no handler is allowed to reinterpret free-form JSON at
approval time.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.execution_contract import canonical_json, utf8_sha256
from app.security import is_dangerous


PROJECT_BOOTSTRAP_CONTRACT_VERSION = "project-bootstrap-v2"
ENVIRONMENT_CONTRACT_VERSION = "host-environment-v1"
RUN_TEMPLATE_SPEC_CONTRACT_VERSION = "run-template-spec-v2"
PROJECT_DEFAULTS_CONTRACT_VERSION = "project-defaults-v1"

ProjectRoleValue = Literal[
    "owner",
    "operator",
    "reviewer",
    "dataset_manager",
    "viewer",
]
_ROLE_ORDER = {
    "owner": 0,
    "operator": 1,
    "reviewer": 2,
    "dataset_manager": 3,
    "viewer": 4,
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: DG-HARDWARE-EXECUTION v1 P1（H-1）：executable_present 檢查的工具名。
#: 真實工具鏈名稱含 . 與 -（esptool.py、st-flash、openFPGALoader），
#: `command -v` 的引數仍是單一驗證過的識別字，永不含空白／分隔符。
_EXECUTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_GIT_SCP_RE = re.compile(
    r"^git@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?:"
    r"[A-Za-z0-9._/-]{1,1024}$"
)
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CANONICAL_DECIMAL_RE = re.compile(
    r"^-?(?:0|[1-9][0-9]*)\.[0-9]*[1-9]$"
)
_MAX_CANONICAL_DECIMAL_BYTES = 128


class _ContractModel(BaseModel):
    # Product contracts are reviewed and digest-bound bytes. Coercing strings
    # into booleans/integers would make the accepted type differ from the
    # submitted JSON type, so all shared bootstrap/standalone models are strict.
    model_config = ConfigDict(extra="forbid", strict=True)


def canonical_uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UUID")
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{field_name} must be a canonical UUID") from None
    if normalized != value:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return normalized


def _canonical_name(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not _NAME_RE.fullmatch(value)
    ):
        raise ValueError(
            f"{field_name} must be 1-64 path-safe ASCII characters"
        )
    return value


def _canonical_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _PARAMETER_NAME_RE.fullmatch(value):
        raise ValueError(f"{field_name} is not a valid identifier")
    return value


def _sorted_unique(
    values: list[str],
    *,
    field_name: str,
    validator: Callable[[str, str], str],
) -> list[str]:
    normalized = [validator(value, field_name) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must be unique")
    return sorted(normalized)


def _tag(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _TAG_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains an invalid tag")
    return value


def _env_name(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be an uppercase environment name")
    return value


def _relative_path(value: str, field_name: str, *, allow_glob: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or _CONTROL_RE.search(value)
        or "\\" in value
    ):
        raise ValueError(f"{field_name} must be a bounded POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must stay below the declared root")
    wildcard_count = sum(value.count(character) for character in "*?[")
    if not allow_glob and wildcard_count:
        raise ValueError(f"{field_name} must not contain a glob")
    if allow_glob and ("**" in value or wildcard_count > 4 or len(path.parts) > 12):
        raise ValueError(f"{field_name} glob is not bounded")
    return value


def _digest_body(model: BaseModel, *, digest_field: str) -> str:
    body = model.model_dump(mode="json", exclude={digest_field})
    return utf8_sha256(canonical_json(body))


class ProjectSourceReference(_ContractModel):
    kind: Literal["repo", "local_path"]
    reference: str = Field(min_length=1, max_length=2048)

    @field_validator("reference")
    @classmethod
    def _safe_reference(cls, value: str, info: Any) -> str:
        if value != value.strip() or _CONTROL_RE.search(value):
            raise ValueError("source reference contains invalid characters")
        kind = info.data.get("kind")
        if kind == "local_path":
            if not (value.startswith("/") or value.startswith("~/")):
                raise ValueError("local_path must be absolute or start with '~/'")
            path_value = value[2:] if value.startswith("~/") else value[1:]
            path = PurePosixPath(path_value)
            if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError("local_path must be normalized and project-specific")
            return value
        if kind != "repo":
            return value
        if _GIT_SCP_RE.fullmatch(value):
            return value
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("repo reference must be a credential-free URL") from exc
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname is None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or any(part == ".." for part in PurePosixPath(parsed.path).parts)
        ):
            raise ValueError("repo reference must be a credential-free HTTPS/SSH URL")
        if parsed.scheme == "https" and parsed.username is not None:
            raise ValueError("HTTPS repo reference must not contain user credentials")
        return value


class ProjectBootstrapIdentity(_ContractModel):
    id: str
    name: str
    source: ProjectSourceReference

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return canonical_uuid(value, "project.id")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, "project.name")


class InitialRoleAssignment(_ContractModel):
    actor_id: str
    roles: list[ProjectRoleValue] = Field(min_length=1, max_length=5)

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        return canonical_uuid(value, "role_assignments.actor_id")

    @field_validator("roles")
    @classmethod
    def _roles(cls, values: list[ProjectRoleValue]) -> list[ProjectRoleValue]:
        if len(values) != len(set(values)):
            raise ValueError("role assignment roles must be unique")
        return sorted(values, key=_ROLE_ORDER.__getitem__)


class InitialRoleBinding(_ContractModel):
    id: str
    role: ProjectRoleValue

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return canonical_uuid(value, "role_assignments.bindings.id")


class InitialRoleAssignmentContract(_ContractModel):
    actor_id: str
    bindings: list[InitialRoleBinding] = Field(min_length=1, max_length=5)

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        return canonical_uuid(value, "role_assignments.actor_id")

    @field_validator("bindings")
    @classmethod
    def _bindings(
        cls,
        values: list[InitialRoleBinding],
    ) -> list[InitialRoleBinding]:
        ids = [binding.id for binding in values]
        roles = [binding.role for binding in values]
        if len(ids) != len(set(ids)) or len(roles) != len(set(roles)):
            raise ValueError("initial role bindings must have unique IDs and roles")
        return sorted(values, key=lambda binding: _ROLE_ORDER[binding.role])


PreflightKind = Literal[
    "executable_present",
    "non_secret_env_present",
    "secret_reference_present",
    "project_relative_path_present",
    "server_tag_present",
]


class EnvironmentPreflightCheck(_ContractModel):
    kind: PreflightKind
    name: str | None = Field(default=None, max_length=128)
    path: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _closed_shape(self) -> "EnvironmentPreflightCheck":
        if self.kind == "project_relative_path_present":
            if self.name is not None or self.path is None:
                raise ValueError("path preflight requires only path")
            self.path = _relative_path(
                self.path,
                "preflight.path",
                allow_glob=False,
            )
            return self
        if self.path is not None or self.name is None:
            raise ValueError("named preflight requires only name")
        if self.kind in {"non_secret_env_present", "secret_reference_present"}:
            self.name = _env_name(self.name, "preflight.name")
        elif self.kind == "server_tag_present":
            self.name = _tag(self.name, "preflight.name")
        elif self.kind == "executable_present":
            if not isinstance(self.name, str) or not _EXECUTABLE_NAME_RE.fullmatch(
                self.name
            ):
                raise ValueError("preflight.name is not a valid executable name")
        else:
            self.name = _canonical_identifier(self.name, "preflight.name")
        return self


class EnvironmentRevisionInput(_ContractModel):
    name: str
    setup_command: str = Field(max_length=4096)
    required_server_tags: list[str] = Field(default_factory=list, max_length=32)
    working_directory_policy: Literal["project_checkout"] = "project_checkout"
    non_secret_env: list[str] = Field(default_factory=list, max_length=64)
    secret_references: list[str] = Field(default_factory=list, max_length=64)
    preflight_checks: list[EnvironmentPreflightCheck] = Field(
        default_factory=list,
        max_length=64,
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, "environment.name")

    @field_validator("setup_command")
    @classmethod
    def _setup_command(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 4096:
            raise ValueError("setup command exceeds the 4096-byte storage limit")
        if _CONTROL_RE.search(value.replace("\n", "")):
            raise ValueError("setup command contains a control character")
        return value

    @field_validator("required_server_tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        return _sorted_unique(
            values,
            field_name="required_server_tags",
            validator=_tag,
        )

    @field_validator("non_secret_env", "secret_references")
    @classmethod
    def _env_names(cls, values: list[str], info: Any) -> list[str]:
        return _sorted_unique(
            values,
            field_name=info.field_name,
            validator=_env_name,
        )

    @field_validator("preflight_checks")
    @classmethod
    def _checks(
        cls,
        checks: list[EnvironmentPreflightCheck],
    ) -> list[EnvironmentPreflightCheck]:
        keys = [(check.kind, check.name or "", check.path or "") for check in checks]
        if len(keys) != len(set(keys)):
            raise ValueError("preflight checks must be unique")
        return [check for _, check in sorted(zip(keys, checks), key=lambda item: item[0])]

    @model_validator(mode="after")
    def _references_exist(self) -> "EnvironmentRevisionInput":
        if set(self.non_secret_env) & set(self.secret_references):
            raise ValueError("non-secret and secret environment names must be disjoint")
        for check in self.preflight_checks:
            if (
                check.kind == "non_secret_env_present"
                and check.name not in self.non_secret_env
            ):
                raise ValueError("non-secret env preflight is outside the allowlist")
            if (
                check.kind == "secret_reference_present"
                and check.name not in self.secret_references
            ):
                raise ValueError("secret preflight is outside the reference list")
            if (
                check.kind == "server_tag_present"
                and check.name not in self.required_server_tags
            ):
                raise ValueError("server-tag preflight is outside required tags")
        return self


class EnvironmentRevisionContract(EnvironmentRevisionInput):
    environment_id: str
    revision_id: str
    revision: Literal[1] = 1
    contract_version: Literal["host-environment-v1"] = "host-environment-v1"
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("environment_id", "revision_id")
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, f"environment.{info.field_name}")

    @model_validator(mode="after")
    def _digest_matches(self) -> "EnvironmentRevisionContract":
        if self.revision_digest != _digest_body(
            self,
            digest_field="revision_digest",
        ):
            raise ValueError("environment revision digest mismatch")
        return self


class ArgvTemplateToken(_ContractModel):
    kind: Literal["literal", "parameter"]
    value: str | None = Field(default=None, max_length=512)
    name: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _closed_shape(self) -> "ArgvTemplateToken":
        if self.kind == "literal":
            if self.name is not None or not isinstance(self.value, str) or not self.value:
                raise ValueError("literal argv token requires only a non-empty value")
            if _CONTROL_RE.search(self.value):
                raise ValueError("literal argv token contains a control character")
            return self
        if self.value is not None or self.name is None:
            raise ValueError("parameter argv token requires only name")
        self.name = _canonical_identifier(self.name, "argv_template.name")
        return self


ParameterType = Literal["string", "integer", "number", "boolean", "enum"]
CanonicalNumber = int | str


def canonical_decimal(value: object, field_name: str) -> Decimal:
    """Return an exact Decimal for the Product ``number`` representation.

    JSON floats are deliberately absent from canonical Product contracts.
    Integral values remain JSON integers; fractional values use one closed,
    exponent-free decimal string grammar so the existing float-free canonical
    JSON and every previously valid integer-only digest remain unchanged.
    """

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer or canonical decimal")
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an integer or canonical decimal")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(
            f"{field_name} must be an integer or canonical decimal"
        ) from None
    if (
        len(encoded) > _MAX_CANONICAL_DECIMAL_BYTES
        or _CANONICAL_DECIMAL_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be an integer or canonical decimal")
    return Decimal(value)


class RunParameterSpec(_ContractModel):
    name: str
    type: ParameterType
    required: bool = True
    min_length: int | None = Field(default=None, ge=0, le=4096)
    max_length: int | None = Field(default=None, ge=1, le=4096)
    minimum: CanonicalNumber | None = None
    maximum: CanonicalNumber | None = None
    enum_values: list[str] | None = Field(default=None, min_length=1, max_length=100)
    sensitive: Literal[False] = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_identifier(value, "parameter.name")

    @model_validator(mode="after")
    def _type_bounds(self) -> "RunParameterSpec":
        if self.type == "string":
            if (
                self.min_length is None
                or self.max_length is None
                or self.min_length > self.max_length
                or self.minimum is not None
                or self.maximum is not None
                or self.enum_values is not None
            ):
                raise ValueError("string parameter requires only valid length bounds")
            return self
        if self.type in {"integer", "number"}:
            if (
                self.minimum is None
                or self.maximum is None
                or self.min_length is not None
                or self.max_length is not None
                or self.enum_values is not None
            ):
                raise ValueError("numeric parameter requires only finite range bounds")
            if self.type == "integer":
                minimum_int = self.minimum
                maximum_int = self.maximum
                if (
                    isinstance(minimum_int, bool)
                    or isinstance(maximum_int, bool)
                    or not isinstance(minimum_int, int)
                    or not isinstance(maximum_int, int)
                ):
                    raise ValueError("integer parameter bounds must be integers")
                if minimum_int > maximum_int:
                    raise ValueError("integer parameter bounds are reversed")
                return self
            minimum = canonical_decimal(
                self.minimum,
                f"parameter {self.name} minimum",
            )
            maximum = canonical_decimal(
                self.maximum,
                f"parameter {self.name} maximum",
            )
            if minimum > maximum:
                raise ValueError("number parameter bounds are reversed")
            return self
        if self.type == "enum":
            values = self.enum_values
            if (
                values is None
                or len(values) != len(set(values))
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value.encode("utf-8")) > 512
                    or _CONTROL_RE.search(value)
                    for value in values
                )
                or self.min_length is not None
                or self.max_length is not None
                or self.minimum is not None
                or self.maximum is not None
            ):
                raise ValueError("enum parameter requires only a bounded allowlist")
            self.enum_values = sorted(values)
            return self
        if any(
            value is not None
            for value in (
                self.min_length,
                self.max_length,
                self.minimum,
                self.maximum,
                self.enum_values,
            )
        ):
            raise ValueError("boolean parameter must not declare extra bounds")
        return self

    def validate_value(self, value: Any) -> Any:
        if self.type == "string":
            if not isinstance(value, str):
                raise ValueError(f"default {self.name} must be a string")
            assert self.min_length is not None and self.max_length is not None
            if not self.min_length <= len(value) <= self.max_length:
                raise ValueError(f"default {self.name} is outside length bounds")
            return value
        if self.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"default {self.name} must be an integer")
            assert self.minimum is not None and self.maximum is not None
            if not int(self.minimum) <= value <= int(self.maximum):
                raise ValueError(f"default {self.name} is outside range bounds")
            return value
        if self.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError(
                    f"default {self.name} must be an integer or canonical decimal"
                )
            assert self.minimum is not None and self.maximum is not None
            numeric = canonical_decimal(value, f"default {self.name}")
            minimum = canonical_decimal(
                self.minimum,
                f"parameter {self.name} minimum",
            )
            maximum = canonical_decimal(
                self.maximum,
                f"parameter {self.name} maximum",
            )
            if not minimum <= numeric <= maximum:
                raise ValueError(f"default {self.name} is outside range bounds")
            return value
        if self.type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"default {self.name} must be a boolean")
            return value
        if not isinstance(value, str) or value not in (self.enum_values or []):
            raise ValueError(f"default {self.name} is outside the enum allowlist")
        return value


class DeviceRequirement(_ContractModel):
    """DG-HARDWARE-EXECUTION v1 P1（H-1）：模板對附掛裝置的**需求描述**。

    agent／模板只能描述需求（「需要一片 mcu、tag=esp32」），不能指定連線方式
    或指令（憲章 §4.2）。匹配語意：目標 revision 宣告的某個裝置
    `DeviceSpec` 滿足 `kind` 相等、（有給 `id` 時）id 精確相等、
    `tags` ⊆ 裝置 tags。
    """

    kind: Literal["fpga", "mcu", "programmer", "power"]
    id: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("id")
    @classmethod
    def _device_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            raise ValueError("device requirement id is not a valid device id")
        return value

    @field_validator("tags")
    @classmethod
    def _device_tags(cls, values: list[str]) -> list[str]:
        return _sorted_unique(
            values,
            field_name="required_devices.tags",
            validator=_tag,
        )


class ResourceRequirements(_ContractModel):
    required_tags: list[str] = Field(default_factory=list, max_length=32)
    #: DG-HARDWARE-EXECUTION v1 P1：附掛裝置需求（預設空）。**序列化相容性**：
    #: 空列表不進 `model_dump`（見 `_digest_stable_dump`）——`spec_digest`／
    #: `resource_requirements_digest` 是對 dump 的 canonical JSON 計算的，
    #: 沒宣告裝置的既有 revision 的 digest 必須一個 byte 都不變。
    required_devices: list[DeviceRequirement] = Field(
        default_factory=list, max_length=8
    )
    min_gpu_count: int = Field(default=0, ge=0, le=64)
    min_gpu_memory_mb: int = Field(default=0, ge=0, le=1048576)
    min_available_ram_mb: int = Field(default=0, ge=0, le=16777216)
    min_available_disk_mb: int = Field(default=0, ge=0, le=1073741824)
    exclusive_worker: Literal[True] = True

    @field_validator("required_tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        return _sorted_unique(
            values,
            field_name="resource_requirements.required_tags",
            validator=_tag,
        )

    @field_validator("required_devices")
    @classmethod
    def _devices(cls, values: list[DeviceRequirement]) -> list[DeviceRequirement]:
        keys = [
            (value.kind, value.id or "", tuple(value.tags)) for value in values
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("required_devices must be unique")
        return [value for _, value in sorted(zip(keys, values), key=lambda i: i[0])]

    @model_serializer(mode="wrap")
    def _digest_stable_dump(self, handler: Any) -> Any:
        """Drop the empty `required_devices` key from every dump.

        Digest-bound contracts stored before this field existed must keep
        producing byte-identical canonical JSON; a template that actually
        declares device requirements includes (and therefore pins) the key.
        """

        data = handler(self)
        if isinstance(data, dict) and not data.get("required_devices"):
            data.pop("required_devices", None)
        return data


#: DG-HARDWARE-EXECUTION v1 H-2: closed action classes. `compute`/`build`
#: run through `execution_plan_v2`; `program`/`power`/`hil_test` need the
#: `hardware_action_v2` kind (P3) and are refused on the compute path.
ACTION_CLASSES: tuple[str, ...] = ("compute", "build", "program", "power", "hil_test")
COMPUTE_ACTION_CLASSES = frozenset({"compute", "build"})
ActionClass = Literal["compute", "build", "program", "power", "hil_test"]
#: H-3: the only outputs Server A registers as programmable images.
ARTIFACT_CLASSES: tuple[str, ...] = ("bitstream", "firmware")
ArtifactClass = Literal["bitstream", "firmware"]


class OutputDeclaration(_ContractModel):
    name: str
    kind: Literal["file", "directory"]
    path_pattern: str
    required: bool = True
    #: H-3 (P2): a `build` template marks the outputs Server A must register
    #: as images. `path_pattern` is then matched inside the pulled
    #: `results/{job_id}/` directory. Omitted from dumps when unset so every
    #: pre-existing spec digest stays byte-identical (see `required_devices`).
    artifact_class: ArtifactClass | None = None

    @model_validator(mode="after")
    def _artifact_requires_file(self) -> "OutputDeclaration":
        if self.artifact_class is not None and self.kind != "file":
            raise ValueError("artifact_class outputs must be files")
        return self

    @model_serializer(mode="wrap")
    def _digest_stable_dump(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("artifact_class") is None:
            data.pop("artifact_class", None)
        return data

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, "output.name")

    @field_validator("path_pattern")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value, "output.path_pattern", allow_glob=True)


class RunTemplateSpecInput(_ContractModel):
    name: str
    #: H-2: default `compute`; omitted from dumps at the default so existing
    #: spec digests are unchanged. Only `build` may declare image outputs.
    action_class: ActionClass = "compute"
    argv_template: list[ArgvTemplateToken] = Field(min_length=1, max_length=64)
    parameter_schema: list[RunParameterSpec] = Field(
        default_factory=list,
        max_length=32,
    )
    resource_requirements: ResourceRequirements
    output_declarations: list[OutputDeclaration] = Field(
        default_factory=list,
        max_length=32,
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, "run_template.name")

    @field_validator("parameter_schema")
    @classmethod
    def _parameters(cls, values: list[RunParameterSpec]) -> list[RunParameterSpec]:
        names = [value.name for value in values]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        return sorted(values, key=lambda value: value.name)

    @field_validator("output_declarations")
    @classmethod
    def _outputs(cls, values: list[OutputDeclaration]) -> list[OutputDeclaration]:
        names = [value.name for value in values]
        if len(names) != len(set(names)):
            raise ValueError("output names must be unique")
        return sorted(values, key=lambda value: value.name)

    @model_validator(mode="after")
    def _argv_parameters(self) -> "RunTemplateSpecInput":
        if self.argv_template[0].kind != "literal":
            raise ValueError("argv_template must start with a literal executable")
        declared = {parameter.name for parameter in self.parameter_schema}
        referenced = {
            token.name
            for token in self.argv_template
            if token.kind == "parameter" and token.name is not None
        }
        if referenced != declared:
            raise ValueError("argv parameter references must exactly match the schema")
        if self.action_class != "build" and any(
            output.artifact_class is not None for output in self.output_declarations
        ):
            raise ValueError("artifact_class outputs require action_class build")
        return self

    @model_serializer(mode="wrap")
    def _digest_stable_dump(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("action_class") == "compute":
            data.pop("action_class", None)
        return data


class RunTemplateContract(_ContractModel):
    run_profile_id: str
    name: str
    revision: int = Field(ge=1, le=2_147_483_647)
    contract_version: Literal["run-template-spec-v2"] = "run-template-spec-v2"
    environment_revision_id: str
    action_class: ActionClass = "compute"
    argv_template: list[ArgvTemplateToken] = Field(min_length=1, max_length=64)
    parameter_schema: list[RunParameterSpec] = Field(
        default_factory=list,
        max_length=32,
    )
    resource_requirements: ResourceRequirements
    output_declarations: list[OutputDeclaration] = Field(
        default_factory=list,
        max_length=32,
    )
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("run_profile_id", "environment_revision_id")
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, f"run_template.{info.field_name}")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, "run_template.name")

    @model_validator(mode="after")
    def _validate_spec(self) -> "RunTemplateContract":
        body = {
            "name": self.name,
            "action_class": self.action_class,
            "argv_template": self.argv_template,
            "parameter_schema": self.parameter_schema,
            "resource_requirements": self.resource_requirements,
            "output_declarations": self.output_declarations,
        }
        normalized = RunTemplateSpecInput.model_validate(body)
        if normalized.model_dump(mode="json") != self.model_dump(
            mode="json",
            include=set(body),
        ):
            raise ValueError("run template spec is not in canonical order")
        if self.spec_digest != _digest_body(self, digest_field="spec_digest"):
            raise ValueError("run template spec digest mismatch")
        return self

    @model_serializer(mode="wrap")
    def _digest_stable_dump(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("action_class") == "compute":
            data.pop("action_class", None)
        return data


ScalarParameterValue = str | int | bool


class ProjectDefaultsInput(_ContractModel):
    parameter_values: dict[str, ScalarParameterValue] = Field(
        default_factory=dict,
        max_length=32,
    )

    @field_validator("parameter_values")
    @classmethod
    def _parameter_names(
        cls,
        values: dict[str, ScalarParameterValue],
    ) -> dict[str, ScalarParameterValue]:
        for name in values:
            _canonical_identifier(name, "defaults.parameter_values")
        return dict(sorted(values.items()))


class ProjectDefaultsContract(ProjectDefaultsInput):
    revision_id: str
    revision: int = Field(ge=1, le=2_147_483_647)
    contract_version: Literal["project-defaults-v1"] = "project-defaults-v1"
    environment_revision_id: str
    run_profile_id: str
    run_profile_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("revision_id", "environment_revision_id", "run_profile_id")
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, f"defaults.{info.field_name}")

    @model_validator(mode="after")
    def _digest_matches(self) -> "ProjectDefaultsContract":
        if self.revision_digest != _digest_body(
            self,
            digest_field="revision_digest",
        ):
            raise ValueError("project defaults revision digest mismatch")
        return self


class ProjectBootstrapPreviewRequest(_ContractModel):
    project_id: str
    name: str
    slug: str | None = None
    source: ProjectSourceReference
    role_assignments: list[InitialRoleAssignment] = Field(
        min_length=1,
        max_length=100,
    )
    environment: EnvironmentRevisionInput
    run_template: RunTemplateSpecInput
    defaults: ProjectDefaultsInput = Field(default_factory=ProjectDefaultsInput)
    # PR-07/08 own the eventual element contracts.  Until then these arrays
    # are opaque capability probes: non-empty input is blocked before any
    # payload can be submitted or persisted.
    dataset_grants: list[Any] = Field(default_factory=list, max_length=100)
    dataset_aliases: list[Any] = Field(default_factory=list, max_length=100)

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("name", "slug")
    @classmethod
    def _names(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _canonical_name(value, info.field_name)

    @field_validator("role_assignments")
    @classmethod
    def _assignments(
        cls,
        values: list[InitialRoleAssignment],
    ) -> list[InitialRoleAssignment]:
        actors = [assignment.actor_id for assignment in values]
        if len(actors) != len(set(actors)):
            raise ValueError("role assignments must contain unique actors")
        return sorted(values, key=lambda assignment: assignment.actor_id)

    @model_validator(mode="after")
    def _cross_contract(self) -> "ProjectBootstrapPreviewRequest":
        if self.slug is not None and self.slug != self.name:
            raise ValueError("slug must exactly equal the canonical project name")
        if not set(self.environment.required_server_tags).issubset(
            self.run_template.resource_requirements.required_tags
        ):
            raise ValueError("template resource tags must include environment tags")
        validate_parameter_defaults(
            self.run_template.parameter_schema,
            self.defaults.parameter_values,
        )
        return self


class ProjectBootstrapPayload(_ContractModel):
    contract_version: Literal["project-bootstrap-v2"] = "project-bootstrap-v2"
    project: ProjectBootstrapIdentity
    role_assignments: list[InitialRoleAssignmentContract] = Field(
        min_length=1,
        max_length=100,
    )
    environment: EnvironmentRevisionContract
    run_template: RunTemplateContract
    defaults: ProjectDefaultsContract
    dataset_grants: list[Any] = Field(default_factory=list, max_length=100)
    dataset_aliases: list[Any] = Field(default_factory=list, max_length=100)

    @field_validator("role_assignments")
    @classmethod
    def _assignments(
        cls,
        values: list[InitialRoleAssignmentContract],
    ) -> list[InitialRoleAssignmentContract]:
        actors = [assignment.actor_id for assignment in values]
        if actors != sorted(actors) or len(actors) != len(set(actors)):
            raise ValueError("role assignments must be canonical and unique")
        binding_ids = [
            binding.id
            for assignment in values
            for binding in assignment.bindings
        ]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("role binding IDs must be globally unique")
        return values

    @model_validator(mode="after")
    def _exact_references(self) -> "ProjectBootstrapPayload":
        if self.run_template.environment_revision_id != self.environment.revision_id:
            raise ValueError("run template must reference the exact environment revision")
        if self.defaults.environment_revision_id != self.environment.revision_id:
            raise ValueError("defaults must reference the exact environment revision")
        if self.defaults.run_profile_id != self.run_template.run_profile_id:
            raise ValueError("defaults must reference the exact run profile revision")
        if self.defaults.run_profile_spec_digest != self.run_template.spec_digest:
            raise ValueError("defaults must reference the exact run profile spec")
        resource_ids = [
            self.project.id,
            self.environment.environment_id,
            self.environment.revision_id,
            self.run_template.run_profile_id,
            self.defaults.revision_id,
            *[
                binding.id
                for assignment in self.role_assignments
                for binding in assignment.bindings
            ],
        ]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("bootstrap resource UUIDs must be globally unique")
        validate_parameter_defaults(
            self.run_template.parameter_schema,
            self.defaults.parameter_values,
        )
        return self


class ProjectBootstrapRequest(_ContractModel):
    payload: ProjectBootstrapPayload
    expected_payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_parameter_defaults(
    parameter_schema: list[RunParameterSpec],
    parameter_values: dict[str, ScalarParameterValue],
) -> None:
    parameters = {parameter.name: parameter for parameter in parameter_schema}
    unknown = set(parameter_values) - set(parameters)
    missing = {
        parameter.name
        for parameter in parameter_schema
        if parameter.required and parameter.name not in parameter_values
    }
    if unknown:
        raise ValueError("defaults contain undeclared parameters")
    if missing:
        raise ValueError("defaults omit required parameters")
    for name, value in parameter_values.items():
        parameters[name].validate_value(value)


def _new_uuid(uuid_factory: Callable[[], uuid.UUID | str]) -> str:
    return canonical_uuid(str(uuid_factory()), "generated resource id")


def build_bootstrap_payload(
    preview: ProjectBootstrapPreviewRequest,
    *,
    uuid_factory: Callable[[], uuid.UUID | str] = uuid.uuid4,
) -> ProjectBootstrapPayload:
    """Create the complete immutable payload without touching persistent state."""

    environment_data = {
        **preview.environment.model_dump(mode="json"),
        "environment_id": _new_uuid(uuid_factory),
        "revision_id": _new_uuid(uuid_factory),
        "revision": 1,
        "contract_version": ENVIRONMENT_CONTRACT_VERSION,
    }
    environment_data["revision_digest"] = utf8_sha256(
        canonical_json(environment_data)
    )
    environment = EnvironmentRevisionContract.model_validate(environment_data)

    template_data = {
        **preview.run_template.model_dump(mode="json"),
        "run_profile_id": _new_uuid(uuid_factory),
        "revision": 1,
        "contract_version": RUN_TEMPLATE_SPEC_CONTRACT_VERSION,
        "environment_revision_id": environment.revision_id,
    }
    template_data["spec_digest"] = utf8_sha256(canonical_json(template_data))
    run_template = RunTemplateContract.model_validate(template_data)

    defaults_data = {
        **preview.defaults.model_dump(mode="json"),
        "revision_id": _new_uuid(uuid_factory),
        "revision": 1,
        "contract_version": PROJECT_DEFAULTS_CONTRACT_VERSION,
        "environment_revision_id": environment.revision_id,
        "run_profile_id": run_template.run_profile_id,
        "run_profile_spec_digest": run_template.spec_digest,
    }
    defaults_data["revision_digest"] = utf8_sha256(canonical_json(defaults_data))
    defaults = ProjectDefaultsContract.model_validate(defaults_data)

    return ProjectBootstrapPayload(
        project=ProjectBootstrapIdentity(
            id=preview.project_id,
            name=preview.name,
            source=preview.source,
        ),
        role_assignments=[
            InitialRoleAssignmentContract(
                actor_id=assignment.actor_id,
                bindings=[
                    InitialRoleBinding(id=_new_uuid(uuid_factory), role=role)
                    for role in assignment.roles
                ],
            )
            for assignment in preview.role_assignments
        ],
        environment=environment,
        run_template=run_template,
        defaults=defaults,
        dataset_grants=preview.dataset_grants,
        dataset_aliases=preview.dataset_aliases,
    )


def bootstrap_payload_digest(payload: ProjectBootstrapPayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def dangerous_setup_reason(command: str) -> str | None:
    """Evaluate mutable setup-command policy outside schema validation."""

    dangerous, reason = is_dangerous(command)
    return reason if dangerous else None


def parse_bootstrap_payload(payload: Any) -> ProjectBootstrapPayload:
    return ProjectBootstrapPayload.model_validate(payload)


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and _HEX_SHA256_RE.fullmatch(value) is not None


__all__ = [
    "ArgvTemplateToken",
    "CanonicalNumber",
    "ENVIRONMENT_CONTRACT_VERSION",
    "PROJECT_BOOTSTRAP_CONTRACT_VERSION",
    "PROJECT_DEFAULTS_CONTRACT_VERSION",
    "RUN_TEMPLATE_SPEC_CONTRACT_VERSION",
    "EnvironmentRevisionContract",
    "InitialRoleAssignment",
    "InitialRoleAssignmentContract",
    "ProjectBootstrapPayload",
    "ProjectBootstrapPreviewRequest",
    "ProjectBootstrapRequest",
    "ProjectDefaultsContract",
    "ProjectDefaultsInput",
    "ResourceRequirements",
    "RunParameterSpec",
    "RunTemplateContract",
    "RunTemplateSpecInput",
    "ScalarParameterValue",
    "OutputDeclaration",
    "bootstrap_payload_digest",
    "build_bootstrap_payload",
    "canonical_uuid",
    "canonical_decimal",
    "is_sha256",
    "parse_bootstrap_payload",
    "validate_parameter_defaults",
]
