"""Console entry points for the Dispatch Center control-plane processes."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from dispatch_center import __version__
from app.migrations import CURRENT_SCHEMA_VERSION, MigrationError, backup_database, restore_verify_database


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )


def _serve(role: str) -> int:
    """Start the existing server with one unambiguous process role."""

    previous_role = os.environ.get("PROCESS_ROLE")
    os.environ["PROCESS_ROLE"] = role
    # Keep ``--help`` and ``--version`` side-effect free: importing the ASGI
    # application constructs its state, so it must happen only after parsing.
    from app.main import run

    try:
        run()
        return 0
    finally:
        # A normal service process exits after ``run``. Restoring the caller's
        # environment also makes embedded launchers and repeated tests safe.
        if previous_role is None:
            os.environ.pop("PROCESS_ROLE", None)
        else:
            os.environ["PROCESS_ROLE"] = previous_role


def _role_cli(
    *,
    role: str,
    program: str,
    description: str,
    argv: Sequence[str] | None,
) -> int:
    parser = argparse.ArgumentParser(prog=program, description=description)
    _add_common_options(parser)
    parser.parse_args(argv)
    return _serve(role)


def api(argv: Sequence[str] | None = None) -> int:
    """Run the API process without scheduler-owned background loops."""

    return _role_cli(
        role="api",
        program="dispatch-api",
        description="Run the Dispatch Center API process.",
        argv=argv,
    )


def scheduler(argv: Sequence[str] | None = None) -> int:
    """Run the scheduler process using the existing role-gated application."""

    return _role_cli(
        role="scheduler",
        program="dispatch-scheduler",
        description="Run the Dispatch Center scheduler process.",
        argv=argv,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dispatch",
        description="Dispatch Center control-plane command line.",
    )
    _add_common_options(parser)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    api_parser = commands.add_parser(
        "api", help="run the API process without scheduler loops"
    )
    _add_common_options(api_parser)

    scheduler_parser = commands.add_parser(
        "scheduler", help="run scheduler-owned loops"
    )
    _add_common_options(scheduler_parser)

    db_parser = commands.add_parser("db", help="inspect and migrate SQLite state")
    _add_common_options(db_parser)
    db_parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="SQLite path (default: DB_PATH or jobqueue.db)",
    )
    db_commands = db_parser.add_subparsers(dest="db_command", metavar="DB_COMMAND")
    for name, help_text in (
        ("current", "show the recorded schema version"),
        ("upgrade", "apply pending versioned migrations"),
        ("check", "verify integrity and whether migrations are current"),
        ("restore-verify", "verify a restored SQLite copy"),
    ):
        child = db_commands.add_parser(name, help=help_text)
        _add_common_options(child)
        child.add_argument(
            "--db",
            dest="sub_db_path",
            default=None,
            help="SQLite path (default: DB_PATH or jobqueue.db)",
        )
    backup_parser = db_commands.add_parser(
        "backup", help="create a consistent SQLite backup"
    )
    _add_common_options(backup_parser)
    backup_parser.add_argument(
        "--db",
        dest="sub_db_path",
        default=None,
        help="SQLite path (default: DB_PATH or jobqueue.db)",
    )
    backup_parser.add_argument("--output", required=True, help="backup destination path")
    export_parser = db_commands.add_parser(
        "audit-export", help="export durable audit events to append-only JSONL"
    )
    _add_common_options(export_parser)
    export_parser.add_argument(
        "--db",
        dest="sub_db_path",
        default=None,
        help="SQLite path (default: DB_PATH or jobqueue.db)",
    )
    export_parser.add_argument("--output", required=True, help="JSONL output path")
    export_parser.add_argument("--owner", default="dispatch-cli-audit-exporter")
    export_parser.add_argument("--limit", type=int, default=100)
    replay_parser = db_commands.add_parser(
        "audit-replay", help="explicitly replay one dead-letter audit export"
    )
    _add_common_options(replay_parser)
    replay_parser.add_argument(
        "--db",
        dest="sub_db_path",
        default=None,
        help="SQLite path (default: DB_PATH or jobqueue.db)",
    )
    replay_parser.add_argument("--operation-id", required=True)
    replay_parser.add_argument("--operator", required=True)
    replay_parser.add_argument("--reason-code", default="manual_replay")
    return parser


def _database_path(args: argparse.Namespace) -> str:
    return str(
        getattr(args, "sub_db_path", None)
        or getattr(args, "db_path", None)
        or os.environ.get("DB_PATH", "jobqueue.db")
    )


def _database_current(path: str) -> tuple[int, list[dict[str, object]]]:
    if not Path(path).exists():
        return 0, []
    connection = sqlite3.connect(path)
    try:
        pragma_row = connection.execute("PRAGMA user_version").fetchone()
        pragma_version = int(pragma_row[0] or 0) if pragma_row else 0
        try:
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        records = [
            {
                "version": int(row[0]),
                "name": str(row[1]),
                "checksum": str(row[2]),
                "applied_at": str(row[3]),
            }
            for row in rows
        ]
        record_versions = [cast(int, record["version"]) for record in records]
        return max([pragma_version, *record_versions], default=0), records
    finally:
        connection.close()


def _db_command(args: argparse.Namespace) -> int:
    path = _database_path(args)
    command = getattr(args, "db_command", None)
    if command is None:
        print(
            "usage: dispatch db "
            "{current,upgrade,check,backup,restore-verify,audit-export,audit-replay}"
        )
        return 2
    if command == "current":
        version, records = _database_current(path)
        print(json.dumps({"db": path, "schema_version": version, "migrations": records}))
        return 0
    if command == "upgrade":
        from app.db import Database

        database = Database(path)
        try:
            version, records = _database_current(path)
        finally:
            database.close()
        print(json.dumps({"db": path, "schema_version": version, "migrations": records}))
        return 0
    if command == "check":
        if not Path(path).exists():
            print(f"database does not exist: {path}")
            return 1
        try:
            verified = restore_verify_database(path)
        except (MigrationError, sqlite3.Error) as exc:
            print(f"database check failed: {exc}")
            return 1
        if int(verified["schema_version"]) != CURRENT_SCHEMA_VERSION:
            print(
                f"database schema is not current: {verified['schema_version']} "
                f"(expected {CURRENT_SCHEMA_VERSION})"
            )
            return 1
        print(json.dumps({"db": path, "status": "ok", **verified}))
        return 0
    if command == "backup":
        try:
            backup_database(path, args.output)
        except (MigrationError, sqlite3.Error, OSError) as exc:
            print(f"database backup failed: {exc}")
            return 1
        print(json.dumps({"source": path, "backup": args.output, "status": "ok"}))
        return 0
    if command == "restore-verify":
        try:
            verified = restore_verify_database(path)
        except (MigrationError, sqlite3.Error, OSError) as exc:
            print(f"restore verification failed: {exc}")
            return 1
        print(json.dumps({"db": path, "status": "ok", **verified}))
        return 0
    if command == "audit-export":
        from app.audit import export_durable_audit_events
        from app.db import Database

        database = Database(path)
        try:
            result = export_durable_audit_events(
                database,
                args.output,
                owner=args.owner,
                limit=args.limit,
            )
        except (ValueError, sqlite3.Error, OSError) as exc:
            print(f"audit export failed: {exc}")
            return 1
        finally:
            database.close()
        print(json.dumps({"db": path, "output": args.output, **result}))
        return 0
    if command == "audit-replay":
        from app.db import Database

        database = Database(path)
        try:
            replayed = database.replay_durable_audit_export(
                args.operation_id,
                operator=args.operator,
                reason_code=args.reason_code,
            )
        except (ValueError, sqlite3.Error, OSError) as exc:
            print(f"audit replay failed: {exc}")
            return 1
        finally:
            database.close()
        print(
            json.dumps(
                {
                    "db": path,
                    "operation_id": args.operation_id,
                    "replayed": replayed,
                }
            )
        )
        return 0
    raise AssertionError(f"unknown database command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to a role-specific process; no command prints safe help."""

    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "api":
        return _serve("api")
    if args.command == "scheduler":
        return _serve("scheduler")
    if args.command == "db":
        return _db_command(args)
    parser.print_help()
    return 0
