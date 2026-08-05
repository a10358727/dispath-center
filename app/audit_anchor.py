"""Signed, backup-bound checkpoints for the durable audit chain.

The SQLite hash chain remains the internal source of truth.  This module adds
an operator-invoked checkpoint that can be written to an off-host immutable
location with a signing key kept outside the database.  It never mutates the
audit ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.audit_store import verify_hash_chain
from app.execution_contract import canonical_json


CHECKPOINT_CONTRACT_VERSION = "audit-checkpoint-v1"
SIGNATURE_ALGORITHM = "hmac-sha256"


def _key_bytes(signing_key: bytes | str) -> bytes:
    if isinstance(signing_key, str):
        signing_key = signing_key.encode("utf-8")
    if not isinstance(signing_key, bytes) or not signing_key:
        raise ValueError("audit checkpoint signing key is required")
    return signing_key


def _sign(payload: dict[str, Any], signing_key: bytes | str) -> str:
    encoded = canonical_json(payload).encode("utf-8")
    return hmac.new(_key_bytes(signing_key), encoded, hashlib.sha256).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_audit_checkpoint(
    database_path: str | os.PathLike[str],
    *,
    database_id: str,
    signing_key: bytes | str,
    backup_manifest_sha256: str | None = None,
    application_version: str | None = None,
) -> dict[str, Any]:
    """Build and sign a checkpoint after verifying the complete chain."""

    if not isinstance(database_id, str) or not database_id.strip():
        raise ValueError("database_id is required")
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        try:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence ASC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("durable audit ledger is missing") from exc
        ok, reason = verify_hash_chain(rows)
        if not ok:
            raise ValueError(f"audit chain is corrupt: {reason}")
        first = rows[0] if rows else None
        last = rows[-1] if rows else None
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0] or 0
        )
    finally:
        connection.close()

    payload: dict[str, Any] = {
        "contract_version": CHECKPOINT_CONTRACT_VERSION,
        "database_id": database_id,
        "first_sequence": int(first["sequence"]) if first is not None else None,
        "last_sequence": int(last["sequence"]) if last is not None else 0,
        "last_event_sha256": str(last["event_sha256"]) if last is not None else None,
        "event_count": len(rows),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "application_version": application_version,
        "schema_version": schema_version,
        "backup_manifest_sha256": backup_manifest_sha256,
        "signature_algorithm": SIGNATURE_ALGORITHM,
    }
    return {**payload, "signature": _sign(payload, signing_key)}


def write_audit_checkpoint(
    database_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    database_id: str,
    signing_key: bytes | str,
    backup_manifest_sha256: str | None = None,
    application_version: str | None = None,
) -> dict[str, Any]:
    """Write a signed checkpoint with flush/fsync/atomic replace semantics."""

    checkpoint = build_audit_checkpoint(
        database_path,
        database_id=database_id,
        signing_key=signing_key,
        backup_manifest_sha256=backup_manifest_sha256,
        application_version=application_version,
    )
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(checkpoint, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    directory_fd = os.open(target.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return checkpoint


def verify_audit_checkpoint(
    checkpoint: dict[str, Any],
    *,
    signing_key: bytes | str,
    backup_manifest_sha256: str | None = None,
) -> bool:
    """Verify signature and optional backup binding without changing state."""

    if not isinstance(checkpoint, dict):
        return False
    signature = checkpoint.get("signature")
    if not isinstance(signature, str):
        return False
    payload = dict(checkpoint)
    payload.pop("signature", None)
    if payload.get("contract_version") != CHECKPOINT_CONTRACT_VERSION:
        return False
    if payload.get("signature_algorithm") != SIGNATURE_ALGORITHM:
        return False
    if backup_manifest_sha256 is not None and payload.get("backup_manifest_sha256") != backup_manifest_sha256:
        return False
    expected = _sign(payload, signing_key)
    return hmac.compare_digest(signature, expected)


def verify_audit_checkpoint_against_database(
    database_path: str | os.PathLike[str],
    checkpoint: dict[str, Any],
    *,
    signing_key: bytes | str,
    backup_manifest_sha256: str | None = None,
) -> dict[str, int | str]:
    """Verify a signed checkpoint still describes a restored database.

    Signature verification alone proves only that the checkpoint was signed.
    This second check validates the restored database's complete internal
    chain and compares its boundary values with the signed snapshot.  A
    caller may also require the checkpoint's backup-manifest binding.
    """

    if not verify_audit_checkpoint(
        checkpoint,
        signing_key=signing_key,
        backup_manifest_sha256=backup_manifest_sha256,
    ):
        raise ValueError("audit checkpoint signature or backup binding is invalid")
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        try:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence ASC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("durable audit ledger is missing") from exc
        valid, reason = verify_hash_chain(rows)
        if not valid:
            raise ValueError(f"audit chain is corrupt: {reason}")
        first_sequence = int(rows[0]["sequence"]) if rows else None
        last_sequence = int(rows[-1]["sequence"]) if rows else 0
        last_event_sha256 = str(rows[-1]["event_sha256"]) if rows else ""
        event_count = len(rows)
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0] or 0
        )
    finally:
        connection.close()

    expected = {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "last_event_sha256": last_event_sha256 or None,
        "event_count": event_count,
        "schema_version": schema_version,
    }
    for key, actual in expected.items():
        if checkpoint.get(key) != actual:
            raise ValueError(f"audit checkpoint does not match database: {key}")
    return {
        "first_sequence": first_sequence if first_sequence is not None else 0,
        "last_sequence": last_sequence,
        "event_count": event_count,
        "schema_version": schema_version,
    }


__all__ = [
    "CHECKPOINT_CONTRACT_VERSION",
    "SIGNATURE_ALGORITHM",
    "build_audit_checkpoint",
    "file_sha256",
    "verify_audit_checkpoint",
    "verify_audit_checkpoint_against_database",
    "write_audit_checkpoint",
]
