"""Migration 21: an existing database gains the P1 device columns.

Regression for the personal pilot (2026-09-02): the columns had only been
added to the legacy column list (migration 1), so a database already past
that version never received them and observation persistence failed.
"""

import sqlite3

from app.db import Database, SERVER_OBSERVATION_DEVICE_COLUMNS_MIGRATION_VERSION
from app.migrations import CURRENT_SCHEMA_VERSION

_PRE_P1_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "server_name TEXT NOT NULL",
    "observed_at TEXT NOT NULL",
    "online INTEGER NOT NULL",
    "probe_ok INTEGER NOT NULL",
    "gpu_count INTEGER",
    "gpu_util_max REAL",
    "gpu_mem_used_mb REAL",
    "gpu_mem_total_mb REAL",
    "load1 REAL",
    "mem_total_bytes INTEGER",
    "mem_available_bytes INTEGER",
    "disk_avail_bytes INTEGER",
)


def _columns(path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(server_observations)")}


def _downgrade_to_pre_p1_pilot_shape(path) -> None:
    """Rebuild server_observations without the P1 columns and roll the
    ledger back to version 20, i.e. the pilot database before this fix."""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            "DROP INDEX IF EXISTS idx_server_observations_server_time;"
            "ALTER TABLE server_observations RENAME TO server_observations_p1;"
            f"CREATE TABLE server_observations ({', '.join(_PRE_P1_COLUMNS)});"
            "DROP TABLE server_observations_p1;"
            "CREATE INDEX idx_server_observations_server_time ON server_observations(server_name, observed_at);"
        )
        # Every later (idempotent) migration is rewound with it so the ledger has
        # no gap; the fixture stands for "the pilot database at version 20".
        connection.execute(
            "DELETE FROM schema_migrations WHERE version >= ?",
            (SERVER_OBSERVATION_DEVICE_COLUMNS_MIGRATION_VERSION,),
        )
        connection.execute(f"PRAGMA user_version = {SERVER_OBSERVATION_DEVICE_COLUMNS_MIGRATION_VERSION - 1}")


def test_existing_database_gains_the_device_columns_and_can_persist_observations(tmp_path):
    path = tmp_path / "pilot.db"
    Database(str(path)).close()
    _downgrade_to_pre_p1_pilot_shape(path)
    assert "devices_json" not in _columns(path)
    assert "executables_json" not in _columns(path)

    database = Database(str(path))
    try:
        assert _columns(path) >= {"devices_json", "executables_json"}
        assert database.schema_version() == CURRENT_SCHEMA_VERSION
        database.insert_server_observation(
            server_name="server-a",
            online=True,
            probe_ok=True,
            devices_json='[{"kind": "esp32"}]',
        )
        row = database._conn.execute(
            "SELECT devices_json FROM server_observations WHERE server_name = 'server-a'"
        ).fetchone()
        assert row[0] == '[{"kind": "esp32"}]'
    finally:
        database.close()


def test_fresh_database_records_the_migration_as_a_no_op(tmp_path):
    database = Database(str(tmp_path / "fresh.db"))
    try:
        versions = {
            row[0]
            for row in database._conn.execute("SELECT version FROM schema_migrations")
        }
        assert SERVER_OBSERVATION_DEVICE_COLUMNS_MIGRATION_VERSION in versions
        assert _columns(tmp_path / "fresh.db") >= {"devices_json", "executables_json"}
    finally:
        database.close()
