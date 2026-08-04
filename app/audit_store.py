"""Durable audit ledger and export-outbox primitives.

The legacy ``audit.jsonl`` writer remains in :mod:`app.audit` and keeps its
best-effort append-only contract.  This module is the additive, database-backed
source of truth for newly migrated transactions.  It deliberately contains no
FastAPI or scheduler imports so the database layer can install the schema
without creating runtime side effects.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from app.execution_contract import canonical_json


# The version is part of the bytes hashed for every newly-created event.  The
# v0 compatibility value is used only for rows written by the first additive
# migration before the version column existed; it keeps existing local DBs
# verifiable instead of silently rewriting their evidence.
AUDIT_HASH_CONTRACT_VERSION = "durable-audit-v1"
LEGACY_AUDIT_HASH_CONTRACT_VERSION = "durable-audit-v0"
AUDIT_PARAMS_MAX_BYTES = 16 * 1024
AUDIT_PARAMS_MAX_DEPTH = 8

_SENSITIVE_PARAM_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "environment",
        "exception",
        "password",
        "private_key",
        "raw_command",
        "raw_environment",
        "secret",
        "stderr",
        "stdout",
        "stacktrace",
        "token",
    }
)
_SAFE_DIGEST_SUFFIXES = ("_sha256", "_digest")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


DURABLE_AUDIT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 1),
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL,
    result TEXT NOT NULL,
    actor_id TEXT,
    actor_kind TEXT,
    authentication TEXT,
    request_id TEXT,
    resource_type TEXT,
    resource_id TEXT,
    approval_id INTEGER,
    previous_event_sha256 TEXT,
    hash_contract_version TEXT NOT NULL DEFAULT '{AUDIT_HASH_CONTRACT_VERSION}',
    event_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (
        (actor_id IS NULL AND actor_kind IS NULL AND authentication IS NULL)
        OR
        (actor_id IS NOT NULL AND actor_kind IS NOT NULL AND authentication IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred
    ON audit_events(occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_audit_events_action
    ON audit_events(action, id);
CREATE INDEX IF NOT EXISTS idx_audit_events_resource
    ON audit_events(resource_type, resource_id, id);
CREATE TRIGGER IF NOT EXISTS audit_events_append_only_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_append_only_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TABLE IF NOT EXISTS audit_export_operations (
    id TEXT PRIMARY KEY,
    audit_event_id INTEGER NOT NULL UNIQUE
        REFERENCES audit_events(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'processing', 'exported', 'failed', 'dead_letter')
    ),
    claim_owner TEXT,
    claim_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    retry_at TEXT,
    last_error_category TEXT,
    sanitized_error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    exported_at TEXT,
    CHECK (
        (state = 'exported' AND exported_at IS NOT NULL)
        OR (state <> 'exported')
    )
);
CREATE INDEX IF NOT EXISTS idx_audit_export_claim
    ON audit_export_operations(state, retry_at, claim_expires_at, id);
"""


def validate_audit_timestamp(value: str, field_name: str = "occurred_at") -> None:
    """Require the UTC millisecond timestamp used by the hash contract."""

    if not isinstance(value, str) or not _UTC_TIMESTAMP_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must use UTC ISO-8601 milliseconds (YYYY-MM-DDTHH:MM:SS.mmmZ)"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid UTC timestamp") from exc


def validate_durable_audit_params(params: dict[str, Any]) -> None:
    """Reject secrets/raw evidence before it reaches immutable audit storage.

    Audit callers retain an object-shaped payload for compatibility, but the
    durable path is deliberately an allow-by-shape boundary: bounded JSON,
    scalar/list/object values only, and no key that suggests credentials,
    raw commands, environments, output, or exception text.  Digest fields are
    explicitly allowed because they bind evidence without storing the secret
    itself.
    """

    if not isinstance(params, dict):
        raise ValueError("durable audit params must be an object")

    def walk(value: Any, depth: int, path: str) -> None:
        if depth > AUDIT_PARAMS_MAX_DEPTH:
            raise ValueError(f"durable audit params exceed nesting limit at {path}")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"durable audit param keys must be text at {path}")
                normalized = key.casefold().replace("-", "_")
                if not normalized.endswith(_SAFE_DIGEST_SUFFIXES):
                    parts = set(normalized.split("_"))
                    if normalized in _SENSITIVE_PARAM_KEY_PARTS or parts & {
                        "authorization",
                        "cookie",
                        "credential",
                        "environment",
                        "exception",
                        "password",
                        "secret",
                        "stderr",
                        "stdout",
                        "stacktrace",
                        "token",
                    } or normalized in {
                        "command",
                        "output",
                        "raw_output",
                    }:
                        raise ValueError(
                            f"durable audit param key is not allowlisted: {path}.{key}"
                        )
                walk(child, depth + 1, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, depth + 1, f"{path}[{index}]")
            return
        # canonical_json performs the exact scalar type validation (including
        # rejecting floats/NaN), so no duplicate type matrix is needed here.
        if value is not None and not isinstance(value, (str, int, bool)):
            raise ValueError(f"durable audit param value is not canonical at {path}")

    walk(params, 0, "$")
    encoded = canonical_json(params).encode("utf-8")
    if len(encoded) > AUDIT_PARAMS_MAX_BYTES:
        raise ValueError(
            f"durable audit params exceed {AUDIT_PARAMS_MAX_BYTES} UTF-8 bytes"
        )


def canonical_event_payload(
    *,
    event_id: str,
    sequence: int,
    occurred_at: str,
    action: str,
    params: dict[str, Any],
    result: str,
    actor_id: str | None,
    actor_kind: str | None,
    authentication: str | None,
    request_id: str | None,
    resource_type: str | None,
    resource_id: str | None,
    approval_id: int | None,
    previous_event_sha256: str | None,
) -> dict[str, Any]:
    """Build the exact field set covered by an event hash.

    Optional values are retained as ``null`` in the canonical representation;
    omitting a field would make two otherwise equivalent events hash
    differently after export/import.
    """

    return {
        "event_id": event_id,
        "sequence": sequence,
        "occurred_at": occurred_at,
        "action": action,
        "params": params,
        "result": result,
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "authentication": authentication,
        "request_id": request_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "approval_id": approval_id,
        "previous_event_sha256": previous_event_sha256,
    }


def event_sha256(
    *,
    previous_event_sha256: str | None,
    payload: dict[str, Any],
    contract_version: str | None = AUDIT_HASH_CONTRACT_VERSION,
) -> str:
    """Hash one canonical event together with its predecessor hash.

    The default is the current versioned envelope.  Callers verifying a
    pre-v1 row pass ``contract_version=None`` to retain the original bytes.
    New durable rows are therefore self-describing and a future
    canonicalisation change cannot be mistaken for the same evidence format.
    """

    if contract_version is None:
        # The old PR-08 envelope had no contract-version field.  Preserve that
        # exact compatibility hash for rows written before migration 3.
        chain_input = {
            "previous_event_sha256": previous_event_sha256,
            "event": payload,
        }
    else:
        if contract_version != AUDIT_HASH_CONTRACT_VERSION:
            raise ValueError(f"unknown durable audit hash contract: {contract_version}")
        chain_input = {
            "contract_version": contract_version,
            "previous_event_sha256": previous_event_sha256,
            "event": payload,
        }
    return hashlib.sha256(canonical_json(chain_input).encode("utf-8")).hexdigest()


def event_row_to_record(row: Any) -> dict[str, Any]:
    """Project a SQLite row into the legacy JSONL-compatible response shape."""

    record: dict[str, Any] = {
        "id": row["id"],
        "ts": row["occurred_at"],
        "action": row["action"],
        "params": json.loads(row["params_json"] or "{}"),
        "result": row["result"],
        "event_id": row["event_id"],
        "sequence": row["sequence"],
        "event_sha256": row["event_sha256"],
    }
    if "hash_contract_version" in row.keys():
        record["hash_contract_version"] = row["hash_contract_version"]
    if row["actor_id"] is not None:
        record["actor"] = {
            "id": row["actor_id"],
            "kind": row["actor_kind"],
            "authentication": row["authentication"],
        }
    return record


def verify_hash_chain(rows: list[Any]) -> tuple[bool, str | None]:
    """Verify ordered event rows and return ``(ok, failure_reason)``.

    This is intentionally a pure helper so backup/restore checks can validate
    the chain without opening the application or writing an audit record.
    """

    previous: str | None = None
    expected_sequence = 1
    for row in rows:
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            return False, f"sequence_gap:{expected_sequence}:{sequence}"
        if row["previous_event_sha256"] != previous:
            return False, f"previous_hash_mismatch:{sequence}"
        try:
            validate_audit_timestamp(str(row["occurred_at"]), "occurred_at")
        except ValueError:
            return False, f"timestamp_format:{sequence}"
        payload = canonical_event_payload(
            event_id=str(row["event_id"]),
            sequence=sequence,
            occurred_at=str(row["occurred_at"]),
            action=str(row["action"]),
            params=json.loads(row["params_json"] or "{}"),
            result=str(row["result"]),
            actor_id=row["actor_id"],
            actor_kind=row["actor_kind"],
            authentication=row["authentication"],
            request_id=row["request_id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            approval_id=row["approval_id"],
            previous_event_sha256=previous,
        )
        hash_contract_version = (
            row["hash_contract_version"]
            if "hash_contract_version" in row.keys()
            else LEGACY_AUDIT_HASH_CONTRACT_VERSION
        )
        if hash_contract_version not in {
            AUDIT_HASH_CONTRACT_VERSION,
            LEGACY_AUDIT_HASH_CONTRACT_VERSION,
        }:
            return False, f"unknown_hash_contract:{sequence}"
        calculated = event_sha256(
            previous_event_sha256=previous,
            payload=payload,
            contract_version=(
                str(hash_contract_version)
                if hash_contract_version != LEGACY_AUDIT_HASH_CONTRACT_VERSION
                else None
            ),
        )
        if calculated != row["event_sha256"]:
            return False, f"event_hash_mismatch:{sequence}"
        previous = str(row["event_sha256"])
        expected_sequence += 1
    return True, None


def install_durable_audit_schema(connection: Any) -> None:
    """Install schema statements without ``executescript`` transaction breaks."""

    statement = ""
    for line in DURABLE_AUDIT_SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():  # pragma: no cover - checked-in schema is complete
        raise ValueError("durable audit schema contains an incomplete statement")


__all__ = [
    "AUDIT_HASH_CONTRACT_VERSION",
    "AUDIT_PARAMS_MAX_BYTES",
    "AUDIT_PARAMS_MAX_DEPTH",
    "DURABLE_AUDIT_SCHEMA",
    "LEGACY_AUDIT_HASH_CONTRACT_VERSION",
    "canonical_event_payload",
    "event_row_to_record",
    "event_sha256",
    "install_durable_audit_schema",
    "validate_audit_timestamp",
    "validate_durable_audit_params",
    "verify_hash_chain",
]
