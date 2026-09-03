"""Hardware physical actions (DG-HARDWARE-EXECUTION v1 H-2/H-3/H-6, P3a).

A physical action (``program`` / ``power`` / ``hil_test``) is an ExecutionPlan v2
whose approval kind is ``hardware_action_v2`` instead of ``execution_plan_v2``:
same immutable spec, same worker launch shape (SSH/tmux/sentinel), plus a payload
that pins the exact device, the image digest it programs, and the power
sequence. The kind exists so the card, the audit trail and the authorization
classification can all tell "this touches a board" apart from compute, and so
the structural exclusions hold: never auto-approved, never in an experiment
matrix, never decided by the dev-operator direct path.

Image bytes come only from Server A's content-addressed store
(``{local_home_dir}/images/{sha256}``, filled by ``app.hardware_images``); the
decision handler re-verifies the digest and pushes the file by SFTP to a path
beside the project checkout that the compiled argv references literally.
"""

from __future__ import annotations

import posixpath
import shlex
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.execution_contract import canonical_json, utf8_sha256
from app.execution_plan_v2 import (
    ExecutionPlanV2Request,
    _canonical_sha256,
    _canonical_uuid,
    _StrictModel,
)

HARDWARE_ACTION_V2_APPROVAL_KIND = "hardware_action_v2"
HARDWARE_ACTION_V2_APPROVAL_CONTRACT_VERSION = "hardware-action-v2-approval-v1"
PHYSICAL_ACTION_CLASSES: tuple[str, ...] = ("program", "power", "hil_test")
POWER_SEQUENCES: tuple[str, ...] = ("off_on", "reset")
#: Where Server A drops the verified image on the worker: a sibling of the
#: project checkout (never inside the git tree, never a home-relative path),
#: content-addressed so a re-push of the same bytes is idempotent.
WORKER_IMAGE_DIRNAME = ".dispatch-images"

PhysicalActionClass = Literal["program", "power", "hil_test"]
PowerSequence = Literal["off_on", "reset"]


def _device_id(value: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise ValueError("device_id is not a valid device id")
    return value


class HardwareActionV2Request(ExecutionPlanV2Request):
    """The compute request plus the physical pins (H-2)."""

    device_id: str
    image_sha256: str | None = None
    power_sequence: PowerSequence | None = None

    @field_validator("device_id")
    @classmethod
    def _device(cls, value: str) -> str:
        return _device_id(value)

    @field_validator("image_sha256")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_sha256(value, "image_sha256")


class HardwareActionV2SubmitRequest(HardwareActionV2Request):
    expected_plan_digest: str

    @field_validator("expected_plan_digest")
    @classmethod
    def _expected_digest(cls, value: str) -> str:
        return _canonical_sha256(value, "expected_plan_digest")


class HardwareActionV2ApprovalPayload(_StrictModel):
    """Immutable approval payload: the plan reference plus the physical pins.

    Attribute-compatible with ``ExecutionPlanV2ApprovalPayload`` (the plan
    store reads ``execution_plan_id`` / ``project_id`` / ``plan_digest``).
    """

    contract_version: Literal["hardware-action-v2-approval-v1"] = (
        "hardware-action-v2-approval-v1"
    )
    execution_plan_id: str
    project_id: str
    plan_digest: str
    action_class: PhysicalActionClass
    server_name: str = Field(min_length=1, max_length=128)
    device_id: str
    device_kind: str = Field(min_length=1, max_length=32)
    image_sha256: str | None = None
    image_remote_path: str | None = None
    power_sequence: PowerSequence | None = None

    @field_validator("execution_plan_id", "project_id")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _canonical_uuid(value, info.field_name)

    @field_validator("plan_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _canonical_sha256(value, "plan_digest")

    @field_validator("device_id")
    @classmethod
    def _device(cls, value: str) -> str:
        return _device_id(value)

    @field_validator("image_sha256")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_sha256(value, "image_sha256")

    @model_validator(mode="after")
    def _closed_shape(self) -> "HardwareActionV2ApprovalPayload":
        if self.action_class == "program":
            if self.image_sha256 is None or self.image_remote_path is None:
                raise ValueError("program actions pin an image")
            if not self.image_remote_path.startswith("/"):
                raise ValueError("image_remote_path must be absolute")
        elif self.image_sha256 is not None or self.image_remote_path is not None:
            raise ValueError("only program actions pin an image")
        if self.action_class == "power":
            if self.power_sequence is None:
                raise ValueError("power actions pin a power_sequence")
        elif self.power_sequence is not None:
            raise ValueError("only power actions pin a power_sequence")
        return self


def parse_hardware_action_v2_approval_payload(value: Any) -> HardwareActionV2ApprovalPayload:
    return HardwareActionV2ApprovalPayload.model_validate(value)


def hardware_action_v2_approval_payload_digest(payload: HardwareActionV2ApprovalPayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def worker_image_path(checkout_path: str, sha256: str) -> str:
    """Absolute worker-side path the compiled argv references for the image."""

    if not isinstance(checkout_path, str) or not checkout_path.startswith("/"):
        raise ValueError("checkout path is not absolute")
    parent = posixpath.dirname(checkout_path.rstrip("/")) or "/"
    return posixpath.join(parent, WORKER_IMAGE_DIRNAME, f"{_canonical_sha256(sha256, 'image_sha256')}.bin")


def image_preflight_lines(image_path: str, sha256: str) -> tuple[str, ...]:
    """Bridge lines that fail closed unless the pushed image has the pinned digest.

    This is the worker-side half of INV-APPROVAL-3 for images: the approved
    digest is checked against the bytes actually on disk right before exec.
    """

    quoted = shlex.quote(image_path)
    return (
        f"test -f {quoted}",
        f'test "$(sha256sum -- {quoted} | cut -d " " -f 1)" = {shlex.quote(sha256)}',
    )
