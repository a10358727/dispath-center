"""SQLite repository adapters and Unit of Work.

The adapters deliberately delegate to the existing ``Database`` methods.  All
Node/Execution state transitions already have carefully tested atomic guards;
this seam centralizes their ownership without duplicating that state machine
or changing the legacy facade.  ``run`` is the new transaction primitive for
multi-repository work and is covered with rollback fault injection tests.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, TypeVar

from app.db import Database
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import (
    IDEMPOTENCY_TTL_SECONDS,
    IdempotencyIdentity,
    IdempotencyOutcome,
    IdempotencyResource,
)


ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class SQLiteNodeRepository:
    """Compatibility adapter for atomic Node protocol operations."""

    database: Database

    def lease_legacy(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.lease_legacy_node_attempt(**kwargs)

    def acknowledge(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.acknowledge_node_execution_attempt(**kwargs)

    def acknowledge_legacy(self, attempt_id: str, node_id: str) -> bool:
        return self.database.ack_node_attempt(attempt_id, node_id)

    def terminal(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.record_node_execution_terminal(**kwargs)

    def legacy_terminal(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.record_legacy_node_terminal(**kwargs)

    def artifacts(self, **kwargs: Any) -> int:
        return self.database.upsert_node_attempt_artifacts_batch(**kwargs)

    def request_stop(self, attempt_id: str) -> bool:
        return self.database.request_node_attempt_stop(attempt_id)

    def acknowledge_stop(self, attempt_id: str, node_id: str) -> bool:
        return self.database.ack_node_attempt_stop(attempt_id, node_id)


@dataclass(frozen=True)
class SQLiteExecutionRepository:
    """Compatibility adapter for canonical execution-attempt CAS methods."""

    database: Database

    def create(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.create_execution_attempt(**kwargs)

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        return self.database.get_execution_attempt(attempt_id)

    def transition(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.transition_execution_attempt(**kwargs)

    def refresh_observation(self, **kwargs: Any) -> dict[str, Any]:
        return self.database.refresh_execution_attempt_observation(**kwargs)


@dataclass(frozen=True)
class SQLiteAuditRepository:
    """Adapter that requires the caller's active transaction cursor."""

    database: Database

    def append(self, cursor: sqlite3.Cursor, **kwargs: Any) -> dict[str, Any]:
        return self.database.append_durable_audit_event_in_transaction(cursor, **kwargs)


def _canonical_utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("idempotency timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class SQLiteAPIIdempotencyRepository:
    """Evaluate replay or create one completed result on an active cursor."""

    def execute(
        self,
        cursor: sqlite3.Cursor,
        *,
        identity: IdempotencyIdentity,
        callback: Callable[[sqlite3.Cursor], IdempotencyResource],
        now: datetime | None = None,
    ) -> IdempotencyOutcome:
        observed_at = now or datetime.now(timezone.utc)
        created_at = _canonical_utc_timestamp(observed_at)
        expires_at = _canonical_utc_timestamp(
            observed_at + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)
        )
        if expires_at <= created_at:
            raise ValueError("idempotency expiry must be after creation")

        scope = (identity.actor_id, identity.route_key, identity.key_sha256)
        row = cursor.execute(
            """
            SELECT request_sha256, result_resource_type, result_resource_id,
                   created_at, expires_at
            FROM api_idempotency_keys
            WHERE actor_id = ? AND route_key = ? AND key_sha256 = ?
            """,
            scope,
        ).fetchone()
        if row is not None and str(row["expires_at"]) <= created_at:
            cursor.execute(
                """
                DELETE FROM api_idempotency_keys
                WHERE actor_id = ? AND route_key = ? AND key_sha256 = ?
                """,
                scope,
            )
            row = None
        if row is not None:
            if str(row["request_sha256"]) != identity.request_sha256:
                raise APIError(
                    code="idempotency_key_reused",
                    message="Idempotency-Key was already used for another request",
                    status_code=409,
                )
            return IdempotencyOutcome(
                resource=IdempotencyResource(
                    resource_type=str(row["result_resource_type"]),
                    resource_id=str(row["result_resource_id"]),
                ),
                replayed=True,
            )

        resource = callback(cursor)
        if not isinstance(resource, IdempotencyResource):
            raise ValueError("idempotency callback must return IdempotencyResource")
        cursor.execute(
            """
            INSERT INTO api_idempotency_keys (
                actor_id, route_key, key_sha256, request_sha256,
                result_resource_type, result_resource_id, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity.actor_id,
                identity.route_key,
                identity.key_sha256,
                identity.request_sha256,
                resource.resource_type,
                resource.resource_id,
                created_at,
                expires_at,
            ),
        )
        return IdempotencyOutcome(resource=resource, replayed=False)


@dataclass
class SQLiteUnitOfWork:
    """Unit of Work over one existing SQLite ``Database`` facade.

    Repository methods retain their existing per-operation atomicity.  Callers
    that must update multiple repositories together should use ``run``; the
    callback receives the transaction cursor and any exception rolls back all
    writes.  This explicit seam avoids pretending that separate legacy facade
    calls share a transaction while the migration is still additive.
    """

    database: Database
    nodes: SQLiteNodeRepository = field(init=False)
    executions: SQLiteExecutionRepository = field(init=False)
    audit: SQLiteAuditRepository = field(init=False)
    idempotency: SQLiteAPIIdempotencyRepository = field(init=False)
    _entered: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.nodes = SQLiteNodeRepository(self.database)
        self.executions = SQLiteExecutionRepository(self.database)
        self.audit = SQLiteAuditRepository(self.database)
        self.idempotency = SQLiteAPIIdempotencyRepository()

    def __enter__(self) -> "SQLiteUnitOfWork":
        if self._entered:
            raise RuntimeError("Unit of Work cannot be entered twice")
        self._entered = True
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self._entered = False

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if not self._entered:
            raise RuntimeError("Unit of Work must be entered before running a transaction")
        with self.database.transaction() as cursor:
            yield cursor

    def run(self, operation: Callable[[sqlite3.Cursor], ResultT]) -> ResultT:
        """Run one multi-repository callback atomically."""

        with self._transaction() as cursor:
            return operation(cursor)

    def run_idempotent(
        self,
        identity: IdempotencyIdentity,
        callback: Callable[[sqlite3.Cursor], IdempotencyResource],
        *,
        now: datetime | None = None,
    ) -> IdempotencyOutcome:
        """Commit a local mutation and its completed replay identity together."""

        try:
            with self._transaction() as cursor:
                return self.idempotency.execute(
                    cursor,
                    identity=identity,
                    callback=callback,
                    now=now,
                )
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            raise APIError(
                code="idempotency_in_progress",
                message="An idempotent request is still in progress",
                status_code=409,
                details={"retryable": True},
            ) from exc
