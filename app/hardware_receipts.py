"""`hardware-receipt-v1` (DG-HARDWARE-EXECUTION v1 H-4, P3b).

A physical action's job writes ``results/{job_id}/hardware_receipt.json``: a
flat object with the closed keys ``device_id``, ``device_serial_observed``,
``image_sha256``, ``tool``, ``tool_version``, ``verify``
(``verified`` | ``unverified`` | ``skipped``) and ``exit_code``. After the
existing result pull the job-finish hook parses it with the pure function
below, cross-checks it against the approved pins (device, image) and stores
one ``hardware_receipts`` row (``collected`` | ``missing`` | ``invalid`` |
``oversize``; missing = unknown). The receipt never changes the job's
terminal state (INV-SSH-6); the sentinel exit code stays the only source.

A ``verified`` receipt is also the only automatic way an image becomes
known-good: when the decider of a ``hil_test`` card asked for it at decision
time (H-6 (a)), the mark is applied here, against the image the receipt
names, and audited.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional

from app.audit import SYSTEM_AUDIT_ACTOR, append_audit, now_iso
from app.results import local_result_dir

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig
    from app.db import Database, Job

logger = logging.getLogger(__name__)

RECEIPT_FILENAME = "hardware_receipt.json"
RECEIPT_CONTRACT_VERSION = "hardware-receipt-v1"
MAX_RECEIPT_BYTES = 16 * 1024
RECEIPT_KEYS: tuple[str, ...] = (
    "device_id",
    "device_serial_observed",
    "image_sha256",
    "tool",
    "tool_version",
    "verify",
    "exit_code",
)
VERIFY_VALUES: tuple[str, ...] = ("verified", "unverified", "skipped")
_OPTIONAL_KEYS = frozenset({"device_serial_observed", "image_sha256", "tool_version"})
_MAX_TEXT_BYTES = 256
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

ReceiptStatus = Literal["collected", "missing", "invalid", "oversize"]


@dataclass(frozen=True)
class ParsedReceipt:
    status: ReceiptStatus
    reason: Optional[str]
    fields: dict[str, Any]


def _invalid(reason: str) -> ParsedReceipt:
    return ParsedReceipt(status="invalid", reason=reason, fields={})


def _bounded_text(value: object, *, required: bool) -> bool:
    if value is None:
        return not required
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= _MAX_TEXT_BYTES
        and _CONTROL_RE.search(value) is None
    )


def parse_hardware_receipt_v1(raw: bytes) -> ParsedReceipt:
    """Parse raw receipt bytes. Pure and total: never raises, no I/O."""

    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RECEIPT_BYTES:
        return ParsedReceipt(status="oversize", reason="raw_bytes_exceed_16kib", fields={})
    try:
        parsed = json.loads(bytes(raw).decode("utf-8"))
    except UnicodeDecodeError:
        return _invalid("invalid_utf8")
    except json.JSONDecodeError:
        return _invalid("invalid_json")
    if not isinstance(parsed, dict):
        return _invalid("top_level_not_object")
    if set(parsed) != set(RECEIPT_KEYS):
        return _invalid("keys_not_closed")
    if not isinstance(parsed["device_id"], str) or _DEVICE_ID_RE.fullmatch(parsed["device_id"]) is None:
        return _invalid("invalid_device_id")
    if not _bounded_text(parsed["device_serial_observed"], required=False):
        return _invalid("invalid_device_serial_observed")
    image = parsed["image_sha256"]
    if image is not None and (not isinstance(image, str) or _SHA256_RE.fullmatch(image) is None):
        return _invalid("invalid_image_sha256")
    if not _bounded_text(parsed["tool"], required=True):
        return _invalid("invalid_tool")
    if not _bounded_text(parsed["tool_version"], required=False):
        return _invalid("invalid_tool_version")
    if parsed["verify"] not in VERIFY_VALUES:
        return _invalid("invalid_verify")
    exit_code = parsed["exit_code"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not (-2**31 <= exit_code < 2**31):
        return _invalid("invalid_exit_code")
    return ParsedReceipt(status="collected", reason=None, fields={key: parsed[key] for key in RECEIPT_KEYS})


def cross_check_receipt(fields: dict[str, Any], *, device_id: str, image_sha256: Optional[str]) -> Optional[str]:
    """The receipt must name the approved device and (for program) the approved image."""

    if fields.get("device_id") != device_id:
        return "device_id_mismatch"
    if image_sha256 is not None and fields.get("image_sha256") != image_sha256:
        return "image_sha256_mismatch"
    return None


def _read_bounded_receipt(result_dir: str) -> Optional[bytes]:
    """Read at most MAX_RECEIPT_BYTES + 1 bytes of the receipt; None when absent."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(result_dir, os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec)
    except OSError:
        return None
    file_fd: Optional[int] = None
    try:
        try:
            file_fd = os.open(RECEIPT_FILENAME, os.O_RDONLY | nofollow | cloexec, dir_fd=directory_fd)
        except OSError:
            return None
        chunks: list[bytes] = []
        remaining = MAX_RECEIPT_BYTES + 1
        while remaining > 0:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def collect_hardware_receipt(
    job: "Job",
    *,
    db: Optional["Database"],
    config: "AppConfig",
    audit_path: str,
) -> None:
    """Job-finish hook: store the receipt of a finished physical action. Never raises."""

    if db is None:
        return
    try:
        action = db.hardware_action_for_job(job.id)
        if action is None:
            return
        approval_id = int(action["approval_id"])
        payload = action["payload"]
        result_dir = local_result_dir(job.id, config.local_home_dir)
        raw = _read_bounded_receipt(result_dir)
        if raw is None:
            db.replace_hardware_receipt(
                job.id, approval_id=approval_id, action_class=str(payload["action_class"]),
                status="missing", reason=None, fields=None, source_sha256=None,
            )
            append_audit(
                "hardware_receipt_collection_failed",
                {"job_id": job.id, "approval_id": approval_id, "status": "missing", "reason": None},
                result="failed", path=audit_path, actor=SYSTEM_AUDIT_ACTOR,
            )
            return
        parsed = parse_hardware_receipt_v1(raw)
        status: str = parsed.status
        reason = parsed.reason
        fields: Optional[dict[str, Any]] = parsed.fields if parsed.status == "collected" else None
        if parsed.status == "collected":
            mismatch = cross_check_receipt(
                parsed.fields, device_id=str(payload["device_id"]), image_sha256=payload.get("image_sha256"),
            )
            if mismatch is not None:
                status, reason, fields = "invalid", mismatch, None
        source_sha256 = hashlib.sha256(raw).hexdigest() if len(raw) <= MAX_RECEIPT_BYTES else None
        db.replace_hardware_receipt(
            job.id, approval_id=approval_id, action_class=str(payload["action_class"]),
            status=status, reason=reason, fields=fields, source_sha256=source_sha256,
        )
        if status == "collected":
            assert fields is not None
            append_audit(
                "hardware_receipt_collected",
                {
                    "job_id": job.id, "approval_id": approval_id, "device_id": fields["device_id"],
                    "image_sha256": fields["image_sha256"], "verify": fields["verify"],
                    "exit_code": fields["exit_code"],
                },
                path=audit_path, actor=SYSTEM_AUDIT_ACTOR,
            )
            if fields["verify"] == "verified":
                _apply_known_good_intent(db, approval_id=approval_id, image_sha256=fields["image_sha256"], job_id=job.id, audit_path=audit_path)
        else:
            append_audit(
                "hardware_receipt_collection_failed",
                {"job_id": job.id, "approval_id": approval_id, "status": status, "reason": reason},
                result="failed", path=audit_path, actor=SYSTEM_AUDIT_ACTOR,
            )
    except Exception as exc:  # noqa: BLE001 - never let receipt collection affect finalization
        logger.warning("hardware receipt collection failed for job %s (%s)", job.id, type(exc).__name__)


def _apply_known_good_intent(
    db: "Database", *, approval_id: int, image_sha256: Optional[str], job_id: int, audit_path: str
) -> None:
    """H-6 (a): the decider asked for the mark; a verified receipt naming a registered image applies it."""

    intent = db.get_known_good_intent(approval_id)
    if intent is None or image_sha256 is None:
        return
    marked = db.mark_hardware_image_known_good(
        image_sha256,
        source="decision",
        approval_id=approval_id,
        actor_id=str(intent["actor_id"]),
        marked_at=now_iso(),
    )
    if marked is None:
        append_audit(
            "hardware_image_known_good_skipped",
            {"job_id": job_id, "approval_id": approval_id, "image_sha256": image_sha256, "reason": "image_not_registered_or_already_marked"},
            result="failed", path=audit_path, actor=SYSTEM_AUDIT_ACTOR,
        )
        return
    append_audit(
        "hardware_image_known_good_marked",
        {
            "job_id": job_id, "approval_id": approval_id, "image_id": marked["id"],
            "image_sha256": image_sha256, "source": "decision", "actor_id": str(intent["actor_id"]),
        },
        path=audit_path, actor=SYSTEM_AUDIT_ACTOR,
    )
