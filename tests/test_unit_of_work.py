from __future__ import annotations

import pytest

from app.db import Database
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
