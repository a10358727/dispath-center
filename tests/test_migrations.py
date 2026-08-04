"""Versioned SQLite migration runner and control-plane CLI tests."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import Database
from app.migrations import (
    Migration,
    MigrationError,
    MigrationRunner,
    backup_database,
    restore_verify_database,
)
from dispatch_center import cli


def test_database_records_version_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "control.db"
    first = Database(str(path))
    assert first.schema_version() == 3
    first_records = first._conn.execute(
        "SELECT version, name FROM schema_migrations"
    ).fetchall()
    assert [(row[0], row[1]) for row in first_records] == [
        (1, "legacy_schema_compatibility"),
        (2, "durable_audit_export_outbox"),
        (3, "durable_audit_hash_contract_version"),
    ]
    assert first._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    first.close()

    second = Database(str(path))
    assert second.schema_version() == 3
    second_records = second._conn.execute(
        "SELECT version, name FROM schema_migrations"
    ).fetchall()
    assert [(row[0], row[1]) for row in second_records] == [
        (1, "legacy_schema_compatibility"),
        (2, "durable_audit_export_outbox"),
        (3, "durable_audit_hash_contract_version"),
    ]
    second.close()


def test_failed_migration_rolls_back_step_and_ledger(tmp_path):
    path = tmp_path / "failed.db"
    connection = sqlite3.connect(path)

    def fail_after_ddl(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE transient_migration_row (id INTEGER)")
        raise RuntimeError("injected migration failure")

    runner = MigrationRunner(
        connection,
        path,
        [Migration(1, "injected_failure", fail_after_ddl)],
    )
    with pytest.raises(RuntimeError, match="injected migration failure"):
        runner.upgrade()
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'transient_migration_row'"
    ).fetchone() is None
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    connection.close()


def test_backup_and_restore_verify_are_offline_and_consistent(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup" / "copy.db"
    database = Database(str(source))
    database.close()

    backup_database(source, backup)
    verified = restore_verify_database(backup)
    assert verified == {
        "integrity": "ok",
        "schema_version": 3,
        "audit_hash_chain": "ok",
    }


def test_dispatch_db_cli_upgrade_current_check_backup_and_restore_verify(
    tmp_path, capsys
):
    path = tmp_path / "cli.db"
    backup = tmp_path / "cli-backup.db"

    assert cli.main(["db", "upgrade", "--db", str(path)]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert upgraded["schema_version"] == 3

    assert cli.main(["db", "current", "--db", str(path)]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["schema_version"] == 3
    assert current["migrations"][0]["name"] == "legacy_schema_compatibility"

    assert cli.main(["db", "check", "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "backup", "--db", str(path), "--output", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "restore-verify", "--db", str(backup)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["schema_version"] == 3
    assert restored["audit_hash_chain"] == "ok"


def test_dispatch_db_check_rejects_missing_database(tmp_path, capsys):
    missing = tmp_path / "missing.db"
    assert cli.main(["db", "check", "--db", str(missing)]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_unknown_migration_metadata_is_rejected(tmp_path):
    path = tmp_path / "unknown.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (99, 'future', 'sha', 'now')"
    )
    connection.commit()
    runner = MigrationRunner(connection, path, [Migration(1, "one", lambda _conn: None)])
    with pytest.raises(MigrationError, match="unknown migration version"):
        runner.status()
    connection.close()
