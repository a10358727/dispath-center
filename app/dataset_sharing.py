"""Canonical Product v2 Dataset sharing contracts and digests."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.dataset_assets import (
    DATASET_GRANT_CONTRACT_VERSION,
    DATASET_SHARE_OFFER_CONTRACT_VERSION,
    canonical_snapshot_id,
    canonical_snapshot_set,
)
from app.execution_contract import canonical_json, utf8_sha256
from app.project_bootstrap import canonical_uuid


DATASET_SHARE_OFFER_APPROVAL_CONTRACT_VERSION = "dataset-share-offer-approval-v2"
DATASET_SHARE_ACCEPT_CONTRACT_VERSION = "dataset-share-accept-v2"
DATASET_GRANT_REVOKE_CONTRACT_VERSION = "dataset-grant-revoke-v2"

_UTC_MICROSECOND_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def canonical_utc_microsecond_z(value: str, field_name: str = "expires_at") -> str:
    if not isinstance(value, str) or not _UTC_MICROSECOND_Z_RE.fullmatch(value):
        raise ValueError(f"{field_name} must use UTC microsecond Z format")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise ValueError(f"{field_name} must use UTC microsecond Z format") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise ValueError(f"{field_name} must use UTC microsecond Z format")
    return value


def parse_utc_timestamp(value: str, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} is invalid")
    return parsed


def snapshot_set_digest(snapshot_ids: list[str] | tuple[str, ...]) -> str:
    normalized = canonical_snapshot_set(list(snapshot_ids))
    return utf8_sha256(canonical_json(list(normalized)))


class DatasetShareOfferRequest(_StrictContract):
    source_project_id: str
    target_project_id: str
    snapshot_ids: list[str] = Field(min_length=1, max_length=100)
    expected_asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: str

    @field_validator("source_project_id", "target_project_id")
    @classmethod
    def _project_ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("expires_at")
    @classmethod
    def _expiry(cls, value: str) -> str:
        return canonical_utc_microsecond_z(value)

    @model_validator(mode="after")
    def _references(self) -> "DatasetShareOfferRequest":
        normalized = canonical_snapshot_set(self.snapshot_ids)
        if list(normalized) != self.snapshot_ids:
            raise ValueError("snapshot_ids must be canonical sorted unique text")
        if self.source_project_id == self.target_project_id:
            raise ValueError("source and target projects must differ")
        return self


class DatasetShareOfferContract(_StrictContract):
    contract_version: Literal["dataset-share-offer-v2"] = "dataset-share-offer-v2"
    offer_id: str
    asset_id: str
    source_project_id: str
    target_project_id: str
    snapshot_ids: list[str] = Field(min_length=1, max_length=100)
    snapshot_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: str
    offer_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "offer_id",
        "asset_id",
        "source_project_id",
        "target_project_id",
    )
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("expires_at")
    @classmethod
    def _expiry(cls, value: str) -> str:
        return canonical_utc_microsecond_z(value)

    @model_validator(mode="after")
    def _canonical_contract(self) -> "DatasetShareOfferContract":
        normalized = canonical_snapshot_set(self.snapshot_ids)
        if list(normalized) != self.snapshot_ids:
            raise ValueError("offer snapshot set is not canonical")
        if self.source_project_id == self.target_project_id:
            raise ValueError("offer projects must differ")
        if snapshot_set_digest(normalized) != self.snapshot_set_digest:
            raise ValueError("offer snapshot set digest mismatch")
        body = self.model_dump(mode="json", exclude={"offer_digest"})
        if utf8_sha256(canonical_json(body)) != self.offer_digest:
            raise ValueError("offer digest mismatch")
        return self


class DatasetShareOfferPayload(_StrictContract):
    contract_version: Literal["dataset-share-offer-approval-v2"] = (
        "dataset-share-offer-approval-v2"
    )
    project_id: str
    expected_asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_offer: DatasetShareOfferContract

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @model_validator(mode="after")
    def _references(self) -> "DatasetShareOfferPayload":
        if self.project_id != self.target_offer.source_project_id:
            raise ValueError("offer approval project reference is invalid")
        return self


def build_dataset_share_offer(
    *,
    offer_id: str,
    asset_id: str,
    source_project_id: str,
    target_project_id: str,
    snapshot_ids: list[str],
    expires_at: str,
) -> DatasetShareOfferContract:
    normalized = canonical_snapshot_set(snapshot_ids)
    body: dict[str, Any] = {
        "contract_version": DATASET_SHARE_OFFER_CONTRACT_VERSION,
        "offer_id": canonical_uuid(offer_id, "offer_id"),
        "asset_id": canonical_uuid(asset_id, "asset_id"),
        "source_project_id": canonical_uuid(source_project_id, "source_project_id"),
        "target_project_id": canonical_uuid(target_project_id, "target_project_id"),
        "snapshot_ids": list(normalized),
        "snapshot_set_digest": snapshot_set_digest(normalized),
        "expires_at": canonical_utc_microsecond_z(expires_at),
    }
    body["offer_digest"] = utf8_sha256(canonical_json(body))
    return DatasetShareOfferContract.model_validate(body)


class DatasetShareAcceptRequest(_StrictContract):
    offer_id: str
    asset_id: str
    source_project_id: str
    target_project_id: str
    offer_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: str
    snapshot_ids: list[str] = Field(min_length=1, max_length=100)
    snapshot_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "offer_id",
        "asset_id",
        "source_project_id",
        "target_project_id",
    )
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("expires_at")
    @classmethod
    def _expiry(cls, value: str) -> str:
        return canonical_utc_microsecond_z(value)

    @model_validator(mode="after")
    def _envelope(self) -> "DatasetShareAcceptRequest":
        normalized = canonical_snapshot_set(self.snapshot_ids)
        if list(normalized) != self.snapshot_ids:
            raise ValueError("accept snapshot set is not canonical")
        if snapshot_set_digest(normalized) != self.snapshot_set_digest:
            raise ValueError("accept snapshot set digest mismatch")
        if self.source_project_id == self.target_project_id:
            raise ValueError("accept projects must differ")
        return self


class DatasetGrantTarget(_StrictContract):
    grant_id: str
    snapshot_id: str

    @field_validator("grant_id")
    @classmethod
    def _grant(cls, value: str) -> str:
        return canonical_uuid(value, "grant_id")

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return canonical_snapshot_id(value)


class DatasetShareAcceptPayload(_StrictContract):
    contract_version: Literal["dataset-share-accept-v2"] = "dataset-share-accept-v2"
    project_id: str
    offer: DatasetShareOfferContract
    grants: list[DatasetGrantTarget] = Field(min_length=1, max_length=100)

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @model_validator(mode="after")
    def _references(self) -> "DatasetShareAcceptPayload":
        if self.project_id != self.offer.target_project_id:
            raise ValueError("accept approval project reference is invalid")
        snapshots = [grant.snapshot_id for grant in self.grants]
        if snapshots != self.offer.snapshot_ids:
            raise ValueError("accept grants must exactly match the offer snapshot set")
        if len({grant.grant_id for grant in self.grants}) != len(self.grants):
            raise ValueError("accept grant identities must be unique")
        return self


class DatasetGrantRevokeRequest(_StrictContract):
    operation: Literal["revoke", "unlink"]
    project_id: str
    expected_grant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")


class DatasetGrantRevokePayload(_StrictContract):
    contract_version: Literal["dataset-grant-revoke-v2"] = "dataset-grant-revoke-v2"
    operation: Literal["revoke", "unlink"]
    project_id: str
    grant_id: str
    offer_id: str
    asset_id: str
    source_project_id: str
    target_project_id: str
    snapshot_id: str
    expected_grant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "project_id",
        "grant_id",
        "offer_id",
        "asset_id",
        "source_project_id",
        "target_project_id",
    )
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return canonical_snapshot_id(value)

    @model_validator(mode="after")
    def _scope(self) -> "DatasetGrantRevokePayload":
        expected_project = (
            self.source_project_id
            if self.operation == "revoke"
            else self.target_project_id
        )
        if self.project_id != expected_project:
            raise ValueError("grant withdrawal project scope is invalid")
        return self


def sharing_payload_digest(payload: BaseModel) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def dataset_grant_digest(
    *,
    grant_id: str,
    offer_id: str,
    asset_id: str,
    source_project_id: str,
    target_project_id: str,
    snapshot_id: str,
    accept_approval_id: int,
    accept_payload_sha256: str,
    accepted_by_actor_id: str,
    granted_at: str,
    offer_digest: str,
) -> str:
    body = {
        "contract_version": DATASET_GRANT_CONTRACT_VERSION,
        "grant_id": canonical_uuid(grant_id, "grant_id"),
        "offer_id": canonical_uuid(offer_id, "offer_id"),
        "asset_id": canonical_uuid(asset_id, "asset_id"),
        "source_project_id": canonical_uuid(source_project_id, "source_project_id"),
        "target_project_id": canonical_uuid(target_project_id, "target_project_id"),
        "snapshot_id": canonical_snapshot_id(snapshot_id),
        "accept_approval_id": accept_approval_id,
        "accept_payload_sha256": accept_payload_sha256,
        "accepted_by_actor_id": canonical_uuid(
            accepted_by_actor_id,
            "accepted_by_actor_id",
        ),
        "granted_at": granted_at,
        "offer_digest": offer_digest,
    }
    return utf8_sha256(canonical_json(body))


__all__ = [
    "DATASET_GRANT_REVOKE_CONTRACT_VERSION",
    "DATASET_SHARE_ACCEPT_CONTRACT_VERSION",
    "DATASET_SHARE_OFFER_APPROVAL_CONTRACT_VERSION",
    "DatasetGrantRevokePayload",
    "DatasetGrantRevokeRequest",
    "DatasetGrantTarget",
    "DatasetShareAcceptPayload",
    "DatasetShareAcceptRequest",
    "DatasetShareOfferContract",
    "DatasetShareOfferPayload",
    "DatasetShareOfferRequest",
    "build_dataset_share_offer",
    "canonical_utc_microsecond_z",
    "dataset_grant_digest",
    "parse_utc_timestamp",
    "sharing_payload_digest",
    "snapshot_set_digest",
]
