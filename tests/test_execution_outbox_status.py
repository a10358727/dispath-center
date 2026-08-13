"""Read-only execution-attempt outbox evidence checks."""

from __future__ import annotations

import json
import sqlite3

from app.db import Database
from scripts.execution_outbox_status import (
    main as execution_outbox_status_main,
    read_execution_outbox_status,
)


def _create_projection_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE execution_operations (
            id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE execution_completion_operations (
            id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO execution_operations
            (id, operation, state, created_at, updated_at)
            VALUES
            ('prepare-1', 'prepare', 'pending', '2000-01-01T00:00:00+00:00',
             '2000-01-01T00:00:00+00:00'),
            ('launch-1', 'launch', 'uncertain', '2000-01-01T00:00:00+00:00',
             '2000-01-01T00:00:00+00:00'),
            ('collect-1', 'collect', 'failed', '2000-01-01T00:00:00+00:00',
             '2000-01-01T00:00:00+00:00');
        INSERT INTO execution_completion_operations
            (id, operation, state, created_at, updated_at)
            VALUES
            ('result-1', 'result_collection', 'processing',
             '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00'),
            ('notify-1', 'notification', 'failed',
             '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()


def test_execution_outbox_status_is_read_only_and_clear_for_empty_schema(
    tmp_path, capsys
):
    database_path = tmp_path / "execution-clear.db"
    database = Database(str(database_path))
    database.close()
    before = database_path.read_bytes()

    report = read_execution_outbox_status(database_path)

    assert report["status"] == "clear"
    assert report["backlog"] == 0
    assert report["uncertain"] == 0
    assert report["operations"]["total"] == 0
    assert report["completion"]["total"] == 0
    assert (
        execution_outbox_status_main(
            ["--db", str(database_path), "--require-clear", "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "clear"
    assert database_path.read_bytes() == before


def test_execution_outbox_status_reports_unresolved_and_terminal_counts(
    tmp_path, capsys
):
    database_path = tmp_path / "execution-attention.db"
    _create_projection_database(database_path)
    before = database_path.read_bytes()

    assert (
        execution_outbox_status_main(
            ["--db", str(database_path), "--require-clear", "--json"]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "attention"
    assert report["backlog"] == 3
    assert report["uncertain"] == 1
    assert report["reason_codes"] == [
        "execution_operations_unresolved",
        "execution_operations_uncertain",
        "execution_completion_operations_unresolved",
    ]
    assert report["operations"]["by_state"] == {
        "failed": 1,
        "pending": 1,
        "uncertain": 1,
    }
    assert report["operations"]["failed"] == 1
    assert report["completion"]["by_state"] == {"failed": 1, "processing": 1}
    assert report["completion"]["failed"] == 1
    assert report["operations"]["oldest_uncertain_age_seconds"] is not None
    assert report["completion"]["oldest_processing_age_seconds"] is not None
    assert database_path.read_bytes() == before


def test_execution_outbox_status_rejects_missing_tables(tmp_path, capsys):
    database_path = tmp_path / "empty.db"
    sqlite3.connect(database_path).close()

    assert execution_outbox_status_main(["--db", str(database_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "outbox" in payload["error"]
