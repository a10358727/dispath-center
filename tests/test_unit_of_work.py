from __future__ import annotations

import sqlite3

import pytest

from app.db import Database
from dispatch_center.api.idempotency import (
    IdempotencyIdentity,
    IdempotencyResource,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


def test_uow_run_commits_one_explicit_transaction(tmp_path):
    database = Database(str(tmp_path / "uow.db"))
    with SQLiteUnitOfWork(database) as uow:
        uow.run(
            lambda cursor: cursor.execute(
                "CREATE TABLE uow_commit_marker (value TEXT NOT NULL)"
            )
        )
        uow.run(
            lambda cursor: cursor.execute(
                "INSERT INTO uow_commit_marker (value) VALUES (?)", ("ok",)
            )
        )
    with database.cursor() as cursor:
        row = cursor.execute(
            "SELECT value FROM uow_commit_marker"
        ).fetchone()
    assert row[0] == "ok"
    database.close()


def test_uow_run_rolls_back_fault_injection(tmp_path):
    database = Database(str(tmp_path / "uow-rollback.db"))

    def failing_operation(cursor):
        cursor.execute("CREATE TABLE uow_rollback_marker (value TEXT NOT NULL)")
        cursor.execute("INSERT INTO uow_rollback_marker (value) VALUES ('bad')")
        raise RuntimeError("injected repository failure")

    with pytest.raises(RuntimeError, match="injected repository failure"):
        with SQLiteUnitOfWork(database) as uow:
            uow.run(failing_operation)
    with database.cursor() as cursor:
        row = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("uow_rollback_marker",),
        ).fetchone()
    assert row is None
    database.close()


def test_uow_requires_context_and_rejects_nested_transaction(tmp_path):
    database = Database(str(tmp_path / "uow-context.db"))
    uow = SQLiteUnitOfWork(database)
    with pytest.raises(RuntimeError, match="must be entered"):
        uow.run(lambda _cursor: None)

    def nested(_cursor):
        uow.run(lambda _nested_cursor: None)

    with uow:
        with pytest.raises(RuntimeError, match="already active"):
            uow.run(nested)
    database.close()


def test_idempotency_insert_failure_rolls_back_resource_and_audit(tmp_path):
    database = Database(str(tmp_path / "uow-idempotency.db"))
    database.insert_actor(
        actor_id="actor-1",
        actor_type="human",
        display_name="Actor One",
    )
    with database.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE uow_idempotency_resources "
            "(id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        cursor.execute(
            """
            CREATE TRIGGER fail_idempotency_insert
            BEFORE INSERT ON api_idempotency_keys
            BEGIN
                SELECT RAISE(ABORT, 'injected idempotency insert failure');
            END
            """
        )
    identity = IdempotencyIdentity(
        actor_id="actor-1",
        route_key="POST /api/v2/test-resources",
        key_sha256="a" * 64,
        request_sha256="b" * 64,
    )

    with SQLiteUnitOfWork(database) as uow:

        def create(cursor):
            cursor.execute(
                "INSERT INTO uow_idempotency_resources VALUES ('resource-1', 'created')"
            )
            uow.audit.append(
                cursor,
                action="api_v2_test_resource_created",
                params={"resource_id": "resource-1"},
                actor_id="actor-1",
                actor_kind="human",
                authentication="session",
            )
            return IdempotencyResource("test_resource", "resource-1")

        with pytest.raises(
            sqlite3.IntegrityError, match="injected idempotency insert failure"
        ):
            uow.run_idempotent(identity, create)

    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM uow_idempotency_resources"
        ).fetchone()[0] == 0
        assert cursor.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
        assert cursor.execute(
            "SELECT COUNT(*) FROM api_idempotency_keys"
        ).fetchone()[0] == 0
    database.close()
