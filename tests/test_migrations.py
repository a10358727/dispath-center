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
    assert first.schema_version() == 4
    first_records = first._conn.execute(
        "SELECT version, name FROM schema_migrations"
    ).fetchall()
    assert [(row[0], row[1]) for row in first_records] == [
        (1, "legacy_schema_compatibility"),
        (2, "durable_audit_export_outbox"),
        (3, "durable_audit_hash_contract_version"),
        (4, "node_artifact_kind_metadata"),
    ]
    assert first._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    first.close()

    second = Database(str(path))
    assert second.schema_version() == 4
    second_records = second._conn.execute(
        "SELECT version, name FROM schema_migrations"
    ).fetchall()
    assert [(row[0], row[1]) for row in second_records] == [
        (1, "legacy_schema_compatibility"),
        (2, "durable_audit_export_outbox"),
        (3, "durable_audit_hash_contract_version"),
        (4, "node_artifact_kind_metadata"),
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
        "schema_version": 4,
        "audit_hash_chain": "ok",
    }


def test_dispatch_db_cli_upgrade_current_check_backup_and_restore_verify(
    tmp_path, capsys
):
    path = tmp_path / "cli.db"
    backup = tmp_path / "cli-backup.db"

    assert cli.main(["db", "upgrade", "--db", str(path)]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert upgraded["schema_version"] == 4

    assert cli.main(["db", "current", "--db", str(path)]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["schema_version"] == 4
    assert current["migrations"][0]["name"] == "legacy_schema_compatibility"

    assert cli.main(["db", "check", "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "backup", "--db", str(path), "--output", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "restore-verify", "--db", str(backup)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["schema_version"] == 4
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


def test_migration_content_checksum_drift_is_rejected(tmp_path):
    path = tmp_path / "checksum.db"
    connection = sqlite3.connect(path)

    def apply_original(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE checksum_probe (id INTEGER)")

    original = Migration(1, "checksum_probe", apply_original)
    MigrationRunner(connection, path, [original]).upgrade()

    def apply_changed(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE checksum_probe (id INTEGER, value TEXT)")

    changed = Migration(
        1,
        "checksum_probe",
        apply_changed,
        checksum=original.content_checksum(),
    )
    with pytest.raises(MigrationError, match="content checksum"):
        MigrationRunner(connection, path, [changed]).status()
    connection.close()


def test_explicit_reviewed_artifact_checksum_is_persisted(tmp_path):
    path = tmp_path / "reviewed-checksum.db"
    connection = sqlite3.connect(path)

    def apply_reviewed(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE reviewed_checksum (id INTEGER)")

    runner = MigrationRunner(
        connection,
        path,
        [Migration(1, "reviewed", apply_reviewed, checksum="reviewed-sha256")],
    )
    runner.upgrade()
    record = connection.execute(
        "SELECT checksum, content_checksum FROM schema_migrations"
    ).fetchone()
    assert tuple(record) == ("reviewed-sha256", Migration(1, "reviewed", apply_reviewed).content_checksum())
    connection.close()


def test_migration_ledger_version_gap_is_rejected(tmp_path):
    path = tmp_path / "gap.db"
    connection = sqlite3.connect(path)

    def apply_one(_conn: sqlite3.Connection) -> None:
        pass

    def apply_two(_conn: sqlite3.Connection) -> None:
        pass

    def apply_three(_conn: sqlite3.Connection) -> None:
        pass

    migrations = [
        Migration(1, "one", apply_one),
        Migration(2, "two", apply_two),
        Migration(3, "three", apply_three),
    ]
    connection.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    for migration in (migrations[0], migrations[2]):
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, migration.resolved_checksum(), "now"),
        )
    connection.commit()
    with pytest.raises(MigrationError, match="version gap"):
        MigrationRunner(connection, path, migrations).status()
    connection.close()
