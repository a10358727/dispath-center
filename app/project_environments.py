"""Canonical Product v2 Host Environment mutation and readiness contracts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution_contract import canonical_json, utf8_sha256
from app.project_bootstrap import (
    ENVIRONMENT_CONTRACT_VERSION,
    EnvironmentPreflightCheck,
    EnvironmentRevisionInput,
)


ENVIRONMENT_CHANGE_CONTRACT_VERSION = "environment-change-v2"
ENVIRONMENT_READINESS_CONTRACT_VERSION = "host-environment-readiness-v1"

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:^|[;&|\s])(?:[A-Z0-9_]*(?:secret|token|password|passwd|api_?key|credential)"
    r"[A-Z0-9_]*)\s*="
)
_CREDENTIAL_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")


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


def _revision_digest_body(model: BaseModel) -> str:
    return utf8_sha256(
        canonical_json(model.model_dump(mode="json", exclude={"revision_digest"}))
    )


def validate_secret_free_setup(environment: EnvironmentRevisionInput) -> None:
    """Reject explicit credential material from the persisted setup text.

    The contract has no secret-value field.  This additional guard prevents the
    setup command from referencing declared secrets or carrying common inline
    credential assignments; PR-05 never resolves or interpolates a secret.
    """

    command = environment.setup_command
    if _SECRET_ASSIGNMENT_RE.search(command) or _CREDENTIAL_URL_RE.search(command):
        raise ValueError("setup command must not contain credential material")
    for reference in environment.secret_references:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(reference)}(?![A-Za-z0-9_])", command):
            raise ValueError("setup command must not reference a declared secret")


class EnvironmentRevisionContractV1(EnvironmentRevisionInput):
    """One exact immutable ``host-environment-v1`` revision contract."""

    contract_version: Literal["host-environment-v1"] = "host-environment-v1"
    environment_id: str
    revision_id: str
    revision: int = Field(ge=1, le=2_147_483_647)
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("environment_id", "revision_id")
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, f"target_revision.{info.field_name}")

    @model_validator(mode="after")
    def _closed_revision(self) -> "EnvironmentRevisionContractV1":
        if self.revision_digest != _revision_digest_body(self):
            raise ValueError("environment revision digest mismatch")
        return self


class EnvironmentChangePayload(BaseModel):
    """Strict immutable payload stored by ``environment_change_v2``."""

    contract_version: Literal["environment-change-v2"] = "environment-change-v2"
    operation: Literal["create", "update", "archive"]
    project_id: str
    expected_revision: int = Field(ge=0, le=2_147_483_646)
    expected_head_revision_id: str | None = None
    target_revision: EnvironmentRevisionContractV1

    model_config = ConfigDict(extra="forbid")

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("expected_head_revision_id")
    @classmethod
    def _head_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return canonical_uuid(value, "expected_head_revision_id")

    @model_validator(mode="after")
    def _revision_transition(self) -> "EnvironmentChangePayload":
        target = self.target_revision
        if self.operation == "create":
            if (
                self.expected_revision != 0
                or self.expected_head_revision_id is not None
                or target.revision != 1
            ):
                raise ValueError("create must materialize revision 1 without a head")
        elif (
            self.expected_revision < 1
            or self.expected_head_revision_id is None
            or target.revision != self.expected_revision + 1
        ):
            raise ValueError("revision mutation must target the exact N+1 successor")
        return self


class _EnvironmentChangeRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvironmentCreateRequest(_EnvironmentChangeRequestBase):
    operation: Literal["create"]
    expected_revision: Literal[0]
    expected_head_revision_id: None = None
    environment: EnvironmentRevisionInput


class EnvironmentUpdateRequest(_EnvironmentChangeRequestBase):
    operation: Literal["update"]
    environment_id: str
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_revision_id: str
    environment: EnvironmentRevisionInput

    @field_validator("environment_id", "expected_head_revision_id")
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)


class EnvironmentArchiveRequest(_EnvironmentChangeRequestBase):
    operation: Literal["archive"]
    environment_id: str
    expected_revision: int = Field(ge=1, le=2_147_483_646)
    expected_head_revision_id: str

    @field_validator("environment_id", "expected_head_revision_id")
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)


EnvironmentChangeRequest = Annotated[
    Union[
        EnvironmentCreateRequest,
        EnvironmentUpdateRequest,
        EnvironmentArchiveRequest,
    ],
    Field(discriminator="operation"),
]


def build_environment_revision_contract(
    environment: EnvironmentRevisionInput,
    *,
    environment_id: str,
    revision_id: str,
    revision: int,
    enforce_secret_free: bool = True,
) -> EnvironmentRevisionContractV1:
    if enforce_secret_free:
        validate_secret_free_setup(environment)
    body = {
        **environment.model_dump(mode="json"),
        "contract_version": ENVIRONMENT_CONTRACT_VERSION,
        "environment_id": canonical_uuid(environment_id, "environment_id"),
        "revision_id": canonical_uuid(revision_id, "revision_id"),
        "revision": revision,
    }
    body["revision_digest"] = utf8_sha256(canonical_json(body))
    return EnvironmentRevisionContractV1.model_validate(body)


def parse_environment_change_payload(value: Any) -> EnvironmentChangePayload:
    return EnvironmentChangePayload.model_validate(value)


def environment_change_payload_digest(payload: EnvironmentChangePayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


CheckState = Literal["satisfied", "missing", "unknown"]
ReadinessState = Literal["ready", "not_ready", "unknown"]


@dataclass(frozen=True)
class HostObservationEvidence:
    observed_at: str
    online: bool
    probe_ok: bool
    #: DG-HARDWARE-EXECUTION v1 P1：`command -v` 探測證據（`{name: bool}`）。
    #: None＝該輪未探測（unknown，不是 missing）。
    executables: Mapping[str, bool] | None = None


@dataclass(frozen=True)
class VerifiedHostCandidate:
    tags: tuple[str, ...]
    activated_at: str
    observation: HostObservationEvidence | None


def _parse_timestamp(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _check_key(check: EnvironmentPreflightCheck) -> tuple[str, str, str]:
    return (check.kind, check.name or "", check.path or "")


def evaluate_environment_readiness(
    revision: EnvironmentRevisionContractV1,
    *,
    status: Literal["approved", "archived"],
    candidates: list[VerifiedHostCandidate],
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any]:
    """Derive conservative host readiness without probing or persistence."""

    if now.tzinfo is None:
        raise ValueError("readiness now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)
    required_tags = tuple(revision.required_server_tags)
    qualifying = [
        candidate
        for candidate in candidates
        if set(required_tags).issubset(candidate.tags)
    ]

    tag_state: CheckState
    if qualifying:
        tag_state = "satisfied"
    elif candidates:
        tag_state = "missing"
    else:
        tag_state = "unknown"

    explicit_non_tag = sorted(
        (
            check
            for check in revision.preflight_checks
            if check.kind != "server_tag_present"
        ),
        key=_check_key,
    )
    checks: list[dict[str, Any]] = [
        {"kind": "server_tag_present", "name": tag, "state": tag_state}
        for tag in required_tags
    ]
    #: DG-HARDWARE-EXECUTION v1 P1（H-1）：executable_present 由觀測證據
    #: （`command -v`，HostObservationEvidence.executables）決定聚合狀態：
    #: 任一合格主機 satisfied ＝ satisfied；有證據但沒有一台 satisfied ＝
    #: missing；沒有任何證據＝unknown。其餘 runtime kind 維持 unknown。
    executable_states: dict[str, CheckState] = {}
    for check in explicit_non_tag:
        if check.kind != "executable_present" or check.name is None:
            continue
        seen_evidence = False
        satisfied = False
        for candidate in qualifying:
            evidence = candidate.observation
            if evidence is None or evidence.executables is None:
                continue
            if check.name in evidence.executables:
                seen_evidence = True
                if evidence.executables[check.name]:
                    satisfied = True
                    break
        executable_states[check.name] = (
            "satisfied" if satisfied else ("missing" if seen_evidence else "unknown")
        )
    checks.extend(
        {
            "kind": check.kind,
            "name": check.name,
            "path": check.path,
            "state": (
                executable_states.get(check.name or "", "unknown")
                if check.kind == "executable_present"
                else "unknown"
            ),
        }
        for check in explicit_non_tag
    )

    reasons: set[str] = set()
    if status == "archived":
        state: ReadinessState = "not_ready"
        reasons.add("environment_archived")
    elif not candidates:
        state = "unknown"
        reasons.add("verified_host_configuration_unavailable")
    elif not qualifying:
        state = "not_ready"
        reasons.add("required_server_tags_missing")
    else:
        candidate_states: list[ReadinessState] = []
        has_runtime_checks = bool(explicit_non_tag)
        for candidate in qualifying:
            activated_at = _parse_timestamp(candidate.activated_at)
            observation = candidate.observation
            observed_at = (
                _parse_timestamp(observation.observed_at)
                if observation is not None
                else None
            )
            if (
                observation is None
                or activated_at is None
                or observed_at is None
                or observed_at < activated_at
                or observed_at > now_utc
            ):
                candidate_states.append("unknown")
                reasons.add("host_observation_unknown")
                continue
            age = (now_utc - observed_at).total_seconds()
            if age > stale_after_seconds:
                candidate_states.append("unknown")
                reasons.add("host_observation_stale")
                continue
            if not observation.online or not observation.probe_ok:
                candidate_states.append("not_ready")
                reasons.add("host_observation_not_ready")
                continue
            if has_runtime_checks:
                #: executable 檢查以該主機的觀測證據逐一判定；其他 runtime
                #: kind（env／secret／path）仍無 typed 證據 → unknown。
                candidate_state: ReadinessState = "ready"
                for check in explicit_non_tag:
                    if check.kind == "executable_present" and check.name is not None:
                        executable_evidence = (
                            observation.executables
                            if observation is not None
                            else None
                        )
                        if (
                            executable_evidence is None
                            or check.name not in executable_evidence
                        ):
                            candidate_state = "unknown"
                            reasons.add("typed_host_evidence_unavailable")
                        elif not executable_evidence[check.name]:
                            candidate_state = "not_ready"
                            reasons.add("executable_missing")
                            break
                    else:
                        candidate_state = "unknown"
                        reasons.add("typed_host_evidence_unavailable")
                candidate_states.append(candidate_state)
                continue
            candidate_states.append("ready")

        if "ready" in candidate_states:
            state = "ready"
            reasons.clear()
        elif "unknown" in candidate_states:
            state = "unknown"
        else:
            state = "not_ready"

    return {
        "contract_version": ENVIRONMENT_READINESS_CONTRACT_VERSION,
        "state": state,
        "reasons": sorted(reasons),
        "checks": checks,
    }


__all__ = [
    "ENVIRONMENT_CHANGE_CONTRACT_VERSION",
    "ENVIRONMENT_READINESS_CONTRACT_VERSION",
    "EnvironmentArchiveRequest",
    "EnvironmentChangePayload",
    "EnvironmentChangeRequest",
    "EnvironmentCreateRequest",
    "EnvironmentRevisionContractV1",
    "EnvironmentUpdateRequest",
    "HostObservationEvidence",
    "VerifiedHostCandidate",
    "build_environment_revision_contract",
    "environment_change_payload_digest",
    "evaluate_environment_readiness",
    "parse_environment_change_payload",
    "validate_secret_free_setup",
]
