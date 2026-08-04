"""Small, SQLite-native schema migration runner.

The control plane intentionally does not require Alembic for its single-file
SQLite state.  This module supplies the pieces that matter for the deployment
contract: an explicit version ledger, a process-safe migration lock, atomic
upgrade steps, and backup/integrity helpers used by the ``dispatch db`` CLI.

The runner is deliberately independent from :mod:`app.db`; callers provide the
ordered migration functions so importing the runner never opens a database or
causes a migration as a side effect.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

from app.audit_store import verify_hash_chain

try:  # pragma: no cover - Windows is not a supported deployment target today.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


CURRENT_SCHEMA_VERSION = 3
MIGRATION_TABLE = "schema_migrations"
MIGRATION_LOCK_SUFFIX = ".migration.lock"


class MigrationError(RuntimeError):
    """Raised when the schema ledger or an upgrade step is inconsistent."""


@dataclass(frozen=True)
class Migration:
    """One ordered, additive migration step."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]

    @property
    def checksum(self) -> str:
        # The callable's qualified name is stable for the checked-in plan and
        # avoids serializing executable code into the database.
        source = f"{self.version}:{self.name}:{self.apply.__module__}.{self.apply.__qualname__}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationRecord:
    version: int
    name: str
    checksum: str
    applied_at: str


@dataclass(frozen=True)
class MigrationStatus:
    current_version: int
    target_version: int
    pending_versions: tuple[int, ...]
    applied: tuple[MigrationRecord, ...]

    @property
    def up_to_date(self) -> bool:
        return not self.pending_versions and self.current_version == self.target_version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationRunner:
    """Apply an ordered migration plan to one SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        database_path: str | os.PathLike[str],
        migrations: Sequence[Migration],
    ) -> None:
        self.connection = connection
        self.database_path = str(database_path)
        self.migrations = tuple(sorted(migrations, key=lambda migration: migration.version))
        versions = [migration.version for migration in self.migrations]
        if versions != sorted(set(versions)) or any(version < 1 for version in versions):
            raise MigrationError("migration versions must be unique positive integers")

    @property
    def target_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    def _ensure_metadata_table(self) -> None:
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    def _records(self) -> tuple[MigrationRecord, ...]:
        try:
            rows = self.connection.execute(
                f"SELECT version, name, checksum, applied_at FROM {MIGRATION_TABLE}"
                " ORDER BY version"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if MIGRATION_TABLE not in str(exc):
                raise
            return ()
        return tuple(MigrationRecord(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows)

    def status(self) -> MigrationStatus:
        records = self._records()
        known = {migration.version: migration for migration in self.migrations}
        for record in records:
            migration = known.get(record.version)
            if migration is None:
                raise MigrationError(f"database has unknown migration version {record.version}")
            if migration.checksum != record.checksum or migration.name != record.name:
                raise MigrationError(
                    f"migration {record.version} metadata does not match the checked-in plan"
                )
        applied_versions = {record.version for record in records}
        pending = tuple(
            migration.version
            for migration in self.migrations
            if migration.version not in applied_versions
        )
        current = max(applied_versions, default=0)
        return MigrationStatus(current, self.target_version, pending, records)

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        """Serialize migration runners across processes and connections."""

        if self.database_path == ":memory:" or fcntl is None:
            yield
            return
        lock_path = Path(self.database_path).with_name(
            Path(self.database_path).name + MIGRATION_LOCK_SUFFIX
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def upgrade(self) -> MigrationStatus:
        """Apply every pending migration in one SQLite transaction.

        SQLite DDL and data changes issued with ``execute`` are transactional.
        A failed step rolls back both its changes and its ledger row; a later
        retry can safely re-run the same version.
        """

        self.connection.execute("PRAGMA busy_timeout = 5000")
        with self._migration_lock():
            self._ensure_metadata_table()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                status = self.status()
                for migration in self.migrations:
                    if migration.version not in status.pending_versions:
                        continue
                    migration.apply(self.connection)
                    self.connection.execute(
                        f"INSERT INTO {MIGRATION_TABLE}"
                        " (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (migration.version, migration.name, migration.checksum, _now()),
                    )
                # Keep SQLite's native version pragma aligned with the
                # append-only ledger for tooling that cannot import Python.
                self.connection.execute(f"PRAGMA user_version = {self.target_version}")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.status()


def backup_database(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Create a consistent SQLite backup without mutating the source."""

    source_path = str(source)
    destination_path = str(destination)
    if source_path == ":memory:":
        raise MigrationError("an in-memory database cannot be backed up by path")
    if not Path(source_path).exists():
        raise MigrationError(f"database does not exist: {source_path}")
    destination_parent = Path(destination_path).parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source_path)
    destination_conn = sqlite3.connect(destination_path)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()


def restore_verify_database(path: str | os.PathLike[str]) -> dict[str, str | int]:
    """Run SQLite integrity checks on a restored copy without opening the app."""

    database_path = str(path)
    if not Path(database_path).exists():
        raise MigrationError(f"database does not exist: {database_path}")
    connection = sqlite3.connect(database_path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(result[0]) if result else "unknown"
        if integrity != "ok":
            raise MigrationError(f"SQLite integrity check failed: {integrity}")
        try:
            row = connection.execute(
                f"SELECT MAX(version) FROM {MIGRATION_TABLE}"
            ).fetchone()
            version = int(row[0] or 0) if row else 0
        except sqlite3.OperationalError:
            version = 0
        try:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            # Pre-durable-audit backups remain verifiable; the application
            # readiness check will reject them until the additive migration is
            # applied.  Do not invent a chain for historical JSONL lines.
            audit_chain = "absent"
        else:
            try:
                event_count, operation_count, distinct_event_count = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM audit_events), "
                    "(SELECT COUNT(*) FROM audit_export_operations), "
                    "(SELECT COUNT(DISTINCT audit_event_id) "
                    " FROM audit_export_operations)"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise MigrationError(
                    "durable audit export outbox is missing or unreadable"
                ) from exc
            if (
                int(event_count or 0) != int(operation_count or 0)
                or int(operation_count or 0) != int(distinct_event_count or 0)
            ):
                raise MigrationError(
                    "durable audit event/export outbox cardinality is inconsistent"
                )
            columns = [description[0] for description in connection.execute(
                "SELECT * FROM audit_events LIMIT 0"
            ).description or ()]
            row_objects = [dict(zip(columns, row)) for row in rows]
            valid, reason = verify_hash_chain(row_objects)
            if not valid:
                raise MigrationError(f"durable audit hash chain failed: {reason}")
            audit_chain = "ok"
        return {
            "integrity": integrity,
            "schema_version": version,
            "audit_hash_chain": audit_chain,
        }
    finally:
        connection.close()


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATION_TABLE",
    "Migration",
    "MigrationError",
    "MigrationRecord",
    "MigrationRunner",
    "MigrationStatus",
    "backup_database",
    "restore_verify_database",
]
