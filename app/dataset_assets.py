"""Canonical Product v2 Dataset Asset, adoption, and alias contracts."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution_contract import canonical_json, utf8_sha256
from app.project_bootstrap import canonical_uuid


DATASET_ASSET_CONTRACT_VERSION = "dataset-asset-v2"
DATASET_ASSET_ADOPTION_CONTRACT_VERSION = "dataset-asset-adoption-v2"
DATASET_ALIAS_CONTRACT_VERSION = "dataset-alias-v2"
DATASET_ALIAS_CHANGE_CONTRACT_VERSION = "dataset-alias-change-v2"
DATASET_SHARE_OFFER_CONTRACT_VERSION = "dataset-share-offer-v2"
DATASET_GRANT_CONTRACT_VERSION = "dataset-grant-v2"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_DATA_CARD_CANONICAL_BYTES = 16_384


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _bounded_text(value: str, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{field_name} must be non-empty canonical text")
    if _CONTROL_RE.search(value):
        raise ValueError(f"{field_name} contains a control character")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} UTF-8 bytes")
    return value


def canonical_snapshot_id(value: str, field_name: str = "snapshot_id") -> str:
    return _bounded_text(value, field_name, maximum=255)


class DatasetCountV2(_StrictContract):
    name: str
    value: int = Field(ge=0, le=9_223_372_036_854_775_807)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = _bounded_text(value, "data_card.counts.name", maximum=64)
        if not _ALIAS_RE.fullmatch(value):
            raise ValueError("data_card count name is invalid")
        return value


class DatasetDataCardV2(_StrictContract):
    collection_method: str
    processing_method: str
    license: str | None = None
    use_restrictions: str | None = None
    sensitive_data: str | None = None
    known_issues: str | None = None
    recommended_use: str | None = None
    counts: list[DatasetCountV2] = Field(default_factory=list, max_length=64)

    @field_validator("collection_method", "processing_method")
    @classmethod
    def _required_text(cls, value: str, info: Any) -> str:
        return _bounded_text(value, f"data_card.{info.field_name}", maximum=2000)

    @field_validator(
        "license",
        "use_restrictions",
        "sensitive_data",
        "known_issues",
        "recommended_use",
    )
    @classmethod
    def _optional_text(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, f"data_card.{info.field_name}", maximum=2000)

    @model_validator(mode="after")
    def _canonical_counts(self) -> "DatasetDataCardV2":
        names = [item.name for item in self.counts]
        if names != sorted(set(names)):
            raise ValueError("data_card counts must be sorted and unique")
        if (
            len(canonical_json(self.model_dump(mode="json")).encode("utf-8"))
            > _MAX_DATA_CARD_CANONICAL_BYTES
        ):
            raise ValueError("data_card exceeds 16384 canonical UTF-8 bytes")
        return self


class DatasetAssetInput(_StrictContract):
    name: str
    description: str
    data_card: DatasetDataCardV2

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = _bounded_text(value, "dataset_asset.name", maximum=64)
        if not _NAME_RE.fullmatch(value):
            raise ValueError("dataset asset name is invalid")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return _bounded_text(value, "dataset_asset.description", maximum=2000)


class DatasetAssetContract(_StrictContract):
    contract_version: Literal["dataset-asset-v2"] = "dataset-asset-v2"
    asset_id: str
    owning_project_id: str
    name: str
    description: str
    data_card: DatasetDataCardV2
    asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("asset_id", "owning_project_id")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @model_validator(mode="after")
    def _canonical_contract(self) -> "DatasetAssetContract":
        normalized = DatasetAssetInput.model_validate(
            self.model_dump(
                mode="json",
                include={"name", "description", "data_card"},
            )
        )
        if normalized.name != self.name or normalized.description != self.description:
            raise ValueError("dataset asset contract is not canonical")
        body = self.model_dump(mode="json", exclude={"asset_digest"})
        if utf8_sha256(canonical_json(body)) != self.asset_digest:
            raise ValueError("dataset asset digest mismatch")
        return self


class DatasetAdoptionRequest(_StrictContract):
    snapshot_id: str
    asset: DatasetAssetInput

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return canonical_snapshot_id(value)


class DatasetAssetAdoptionPayload(_StrictContract):
    contract_version: Literal["dataset-asset-adoption-v2"] = (
        "dataset-asset-adoption-v2"
    )
    project_id: str
    snapshot_id: str
    expected_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_asset: DatasetAssetContract

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return canonical_snapshot_id(value)

    @model_validator(mode="after")
    def _references(self) -> "DatasetAssetAdoptionPayload":
        if self.target_asset.owning_project_id != self.project_id:
            raise ValueError("dataset adoption project reference is invalid")
        return self


class _AliasChangeBase(_StrictContract):
    project_id: str
    alias_name: str
    snapshot_id: str

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("alias_name")
    @classmethod
    def _alias(cls, value: str) -> str:
        value = _bounded_text(value, "alias_name", maximum=64)
        if not _ALIAS_RE.fullmatch(value):
            raise ValueError("dataset alias name is invalid")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return canonical_snapshot_id(value)


class DatasetAliasCreateRequest(_AliasChangeBase):
    operation: Literal["create"]
    expected_revision: Literal[0]
    expected_head_revision_id: None = None
    expected_head_revision_digest: None = None


class DatasetAliasMoveRequest(_AliasChangeBase):
    operation: Literal["move"]
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_revision_id: str
    expected_head_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("expected_head_revision_id")
    @classmethod
    def _head(cls, value: str) -> str:
        return canonical_uuid(value, "expected_head_revision_id")


DatasetAliasChangeRequest = Annotated[
    Union[DatasetAliasCreateRequest, DatasetAliasMoveRequest],
    Field(discriminator="operation"),
]


class DatasetAliasRevisionContract(_StrictContract):
    contract_version: Literal["dataset-alias-v2"] = "dataset-alias-v2"
    revision_id: str
    project_id: str
    asset_id: str
    alias_name: str
    revision: int = Field(ge=1, le=2_147_483_647)
    snapshot_id: str
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("revision_id", "project_id", "asset_id")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("alias_name")
    @classmethod
    def _alias(cls, value: str) -> str:
        value = _bounded_text(value, "alias_name", maximum=64)
        if not _ALIAS_RE.fullmatch(value):
            raise ValueError("dataset alias name is invalid")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return canonical_snapshot_id(value)

    @model_validator(mode="after")
    def _digest(self) -> "DatasetAliasRevisionContract":
        body = self.model_dump(mode="json", exclude={"revision_digest"})
        if utf8_sha256(canonical_json(body)) != self.revision_digest:
            raise ValueError("dataset alias revision digest mismatch")
        return self


class DatasetAliasChangePayload(_StrictContract):
    contract_version: Literal["dataset-alias-change-v2"] = (
        "dataset-alias-change-v2"
    )
    operation: Literal["create", "move"]
    project_id: str
    asset_id: str
    expected_revision: int = Field(ge=0, le=2_147_483_646)
    expected_head_revision_id: str | None = None
    expected_head_revision_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    target_revision: DatasetAliasRevisionContract

    @field_validator("project_id", "asset_id", "expected_head_revision_id")
    @classmethod
    def _ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return canonical_uuid(value, info.field_name)

    @model_validator(mode="after")
    def _transition(self) -> "DatasetAliasChangePayload":
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
            raise ValueError("dataset alias transition is invalid")
        if target.project_id != self.project_id or target.asset_id != self.asset_id:
            raise ValueError("dataset alias references are invalid")
        return self


def build_dataset_asset_contract(
    asset: DatasetAssetInput,
    *,
    asset_id: str,
    owning_project_id: str,
) -> DatasetAssetContract:
    body = {
        **asset.model_dump(mode="json"),
        "contract_version": DATASET_ASSET_CONTRACT_VERSION,
        "asset_id": canonical_uuid(asset_id, "asset_id"),
        "owning_project_id": canonical_uuid(owning_project_id, "owning_project_id"),
    }
    body["asset_digest"] = utf8_sha256(canonical_json(body))
    return DatasetAssetContract.model_validate(body)


def build_dataset_alias_revision(
    *,
    revision_id: str,
    project_id: str,
    asset_id: str,
    alias_name: str,
    revision: int,
    snapshot_id: str,
) -> DatasetAliasRevisionContract:
    body = {
        "contract_version": DATASET_ALIAS_CONTRACT_VERSION,
        "revision_id": canonical_uuid(revision_id, "revision_id"),
        "project_id": canonical_uuid(project_id, "project_id"),
        "asset_id": canonical_uuid(asset_id, "asset_id"),
        "alias_name": alias_name,
        "revision": revision,
        "snapshot_id": canonical_snapshot_id(snapshot_id),
    }
    body["revision_digest"] = utf8_sha256(canonical_json(body))
    return DatasetAliasRevisionContract.model_validate(body)


def dataset_adoption_payload_digest(payload: DatasetAssetAdoptionPayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def dataset_alias_change_payload_digest(payload: DatasetAliasChangePayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def parse_dataset_adoption_payload(value: Any) -> DatasetAssetAdoptionPayload:
    return DatasetAssetAdoptionPayload.model_validate(value)


def parse_dataset_alias_change_payload(value: Any) -> DatasetAliasChangePayload:
    return DatasetAliasChangePayload.model_validate(value)


def canonical_snapshot_set(values: list[str]) -> tuple[str, ...]:
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("snapshot set must contain between 1 and 100 identities")
    normalized = tuple(canonical_snapshot_id(value) for value in values)
    if normalized != tuple(sorted(set(normalized))):
        raise ValueError("snapshot set must be sorted and unique")
    if len(canonical_json(list(normalized)).encode("utf-8")) > 8192:
        raise ValueError("snapshot set exceeds 8192 canonical UTF-8 bytes")
    return normalized


__all__ = [
    "DATASET_ALIAS_CHANGE_CONTRACT_VERSION",
    "DATASET_ALIAS_CONTRACT_VERSION",
    "DATASET_ASSET_ADOPTION_CONTRACT_VERSION",
    "DATASET_ASSET_CONTRACT_VERSION",
    "DATASET_GRANT_CONTRACT_VERSION",
    "DATASET_SHARE_OFFER_CONTRACT_VERSION",
    "DatasetAliasChangePayload",
    "DatasetAliasChangeRequest",
    "DatasetAliasCreateRequest",
    "DatasetAliasMoveRequest",
    "DatasetAliasRevisionContract",
    "DatasetAssetAdoptionPayload",
    "DatasetAssetContract",
    "DatasetAssetInput",
    "DatasetAdoptionRequest",
    "DatasetCountV2",
    "DatasetDataCardV2",
    "build_dataset_alias_revision",
    "build_dataset_asset_contract",
    "canonical_snapshot_id",
    "canonical_snapshot_set",
    "dataset_adoption_payload_digest",
    "dataset_alias_change_payload_digest",
    "parse_dataset_adoption_payload",
    "parse_dataset_alias_change_payload",
]
