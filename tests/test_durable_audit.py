"""Durable audit ledger, hash-chain, pagination, and export outbox tests."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

import app.audit as audit_module
from app.audit import (
    deduplicate_audit_records,
    export_durable_audit_events,
    tail_audit,
)
from app.audit_adoption import AUDIT_ADOPTION, audit_coverage
from app.db import Database
from app.migrations import backup_database, restore_verify_database
from dispatch_center import cli
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


def _append(database: Database, action: str, *, event_id: str | None = None):
    return database.append_durable_audit_event(
        action=action,
        params={"action_index": action},
        result="ok",
        actor_id="system",
        actor_kind="system",
        authentication="system",
        event_id=event_id,
    )


def test_durable_audit_is_versioned_and_hash_chained(tmp_path):
    database = Database(str(tmp_path / "audit.db"))
    assert database.schema_version() == 3
    first = _append(database, "first", event_id="event-1")
    second = _append(database, "second", event_id="event-2")

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["hash_contract_version"] == "durable-audit-v1"
    assert database.durable_audit_hash_chain_status() == {
        "status": "ok",
        "events": 2,
        "reason": None,
    }
    assert database.list_durable_audit_events(limit=10) == [second, first]
    assert database.list_durable_audit_events(limit=10, after_id=first["id"]) == [
        second
    ]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        database._conn.execute(
            "UPDATE audit_events SET result = 'tampered' WHERE id = ?",
            (first["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        database._conn.execute(
            "DELETE FROM audit_events WHERE id = ?", (first["id"],)
        )
    database.close()


def test_durable_audit_rejects_secret_and_unbounded_evidence(tmp_path):
    database = Database(str(tmp_path / "safe-params.db"))
    with pytest.raises(ValueError, match="not allowlisted"):
        database.append_durable_audit_event(
            action="unsafe",
            params={"access_token": "do-not-store"},
        )
    with pytest.raises(ValueError, match="not allowlisted"):
        database.append_durable_audit_event(
            action="unsafe",
            params={"command": "rm -rf /"},
        )
    with pytest.raises(ValueError, match="exceed"):
        database.append_durable_audit_event(
            action="unsafe",
            params={"evidence": "x" * 20_000},
        )
    database.close()


def test_audit_adoption_catalog_is_explicitly_partial():
    assert AUDIT_ADOPTION["execution_attempt.create"] == "durable"
    assert AUDIT_ADOPTION["node_attempt.create"] == "durable"
    coverage = audit_coverage()
    assert coverage["mode"] == "partial"
    assert "project.update" in coverage["legacy_actions"]
    assert "approval.decide" in coverage["legacy_actions"]
    assert set(coverage["legacy_without_migration_issue"]) == {
        "approval.decide",
        "project.update",
    }


def test_concurrent_connections_append_one_serialized_chain(tmp_path):
    path = tmp_path / "concurrent.db"
    bootstrap = Database(str(path))
    bootstrap.close()
    first = Database(str(path))
    second = Database(str(path))
    barrier = threading.Barrier(2)
    results: list[dict] = []
    failures: list[BaseException] = []

    def append(database: Database, action: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(database.append_durable_audit_event(action=action))
        except BaseException as exc:  # pragma: no cover - assertion below
            failures.append(exc)

    threads = [
        threading.Thread(target=append, args=(first, "connection-a")),
        threading.Thread(target=append, args=(second, "connection-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert failures == []
    assert sorted(item["sequence"] for item in results) == [1, 2]
    assert first.durable_audit_hash_chain_status()["status"] == "ok"
    first.close()
    second.close()


def test_explicit_event_id_is_idempotent_but_conflict_fails(tmp_path):
    database = Database(str(tmp_path / "idempotent.db"))
    first = _append(database, "same", event_id="stable-event")
    assert _append(database, "same", event_id="stable-event") == first
    with pytest.raises(ValueError, match="payload conflict"):
        _append(database, "different", event_id="stable-event")
    database.close()


def test_uow_audit_and_mutation_rollback_together(tmp_path):
    database = Database(str(tmp_path / "uow-audit.db"))

    def fail(cursor):
        cursor.execute("CREATE TABLE durable_audit_marker (value TEXT NOT NULL)")
        cursor.execute("INSERT INTO durable_audit_marker VALUES ('written')")
        with SQLiteUnitOfWork(database) as nested:
            nested.audit.append(
                cursor,
                action="marker_created",
                params={"marker": "durable_audit_marker"},
                actor_id="system",
                actor_kind="system",
                authentication="system",
            )
        raise RuntimeError("fault injection")

    with pytest.raises(RuntimeError, match="fault injection"):
        with SQLiteUnitOfWork(database) as uow:
            uow.run(fail)
    assert database.count_durable_audit_events() == 0
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'durable_audit_marker'"
        ).fetchone() is None
    database.close()


def test_export_failure_keeps_durable_event_and_dead_letters_after_limit(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "export.db"))
    event = _append(database, "export-me")

    def fail_open(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(audit_module, "open", fail_open, raising=False)
    result = export_durable_audit_events(
        database,
        tmp_path / "events.jsonl",
        owner="test-exporter",
        max_attempts=1,
    )
    assert result == {"claimed": 1, "exported": 0, "failed": 1, "dead_letter": 1}
    assert database.list_durable_audit_events(limit=10) == [event]
    assert database.durable_audit_hash_chain_status()["status"] == "ok"
    with database.cursor() as cursor:
        state = cursor.execute(
            "SELECT state, last_error_category FROM audit_export_operations"
        ).fetchone()
    assert tuple(state) == ("dead_letter", "OSError")
    database.close()


def test_export_success_is_append_only_and_backup_restores_hash_chain(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    output = tmp_path / "audit.jsonl"
    database = Database(str(source))
    event = _append(database, "exported")
    result = export_durable_audit_events(database, output, owner="test-exporter")
    assert result == {"claimed": 1, "exported": 1, "failed": 0, "dead_letter": 0}
    exported_record = json.loads(output.read_text(encoding="utf-8"))
    assert exported_record["event_id"] == event["event_id"]
    assert exported_record["id"] == event["id"]
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT state FROM audit_export_operations"
        ).fetchone()[0] == "exported"
    database.close()

    backup_database(source, backup)
    verified = restore_verify_database(backup)
    assert verified == {
        "integrity": "ok",
        "schema_version": 3,
        "audit_hash_chain": "ok",
    }


def test_outbox_claim_race_lease_recovery_ceiling_and_manual_replay(tmp_path):
    path = tmp_path / "claim-race.db"
    bootstrap = Database(str(path))
    _append(bootstrap, "claim-me")
    bootstrap.close()
    left = Database(str(path))
    right = Database(str(path))
    barrier = threading.Barrier(2)
    claims: list[list[dict]] = []
    failures: list[BaseException] = []

    def claim(database: Database, owner: str) -> None:
        try:
            barrier.wait(timeout=5)
            claims.append(
                database.claim_durable_audit_exports(
                    owner=owner, lease_seconds=60, max_attempts=2
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below
            failures.append(exc)

    threads = [
        threading.Thread(target=claim, args=(left, "left")),
        threading.Thread(target=claim, args=(right, "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert failures == []
    assert sorted(len(batch) for batch in claims) == [0, 1]
    claimed = next(batch for batch in claims if batch)
    operation_id = claimed[0]["operation_id"]
    with left.cursor() as cursor:
        cursor.execute(
            "UPDATE audit_export_operations SET claim_expires_at = '2000-01-01T00:00:00.000Z'"
            " WHERE id = ?",
            (operation_id,),
        )
    recovered = right.claim_durable_audit_exports(
        owner="recovery", lease_seconds=60, max_attempts=2
    )
    assert recovered[0]["attempt_count"] == 2
    assert (
        right.fail_durable_audit_export(
            operation_id, owner="recovery", error_category="OSError", max_attempts=2
        )
        == "dead_letter"
    )
    assert right.claim_durable_audit_exports(owner="nobody", max_attempts=2) == []
    assert right.replay_durable_audit_export(
        operation_id, operator="operator-1", reason_code="disk_repaired"
    )
    assert right.claim_durable_audit_exports(owner="operator-1")[0]["attempt_count"] == 1
    left.close()
    right.close()


def test_two_export_readers_deduplicate_at_least_once_lines(tmp_path):
    complete = {"event_id": "e1", "event_sha256": "h1", "action": "x"}
    duplicate = dict(complete)
    legacy = {"action": "legacy"}
    assert deduplicate_audit_records([complete, duplicate, legacy]) == [
        complete,
        legacy,
    ]
    path = tmp_path / "partial.jsonl"
    path.write_text(
        json.dumps({"action": "complete"}) + "\n{" + '"action":"partial"',
        encoding="utf-8",
    )
    assert tail_audit(path, n=10) == [{"action": "complete"}]


def test_audit_export_cli_uses_the_durable_outbox(tmp_path, capsys):
    database_path = tmp_path / "cli-audit.db"
    output = tmp_path / "cli-audit.jsonl"
    database = Database(str(database_path))
    event = _append(database, "cli-export")
    database.close()

    assert (
        cli.main(
            [
                "db",
                "audit-export",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["exported"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["event_id"] == event["event_id"]


def test_audit_replay_cli_requires_explicit_operator_action(tmp_path, capsys):
    database_path = tmp_path / "cli-replay.db"
    database = Database(str(database_path))
    _append(database, "cli-replay")
    claimed = database.claim_durable_audit_exports(
        owner="test", max_attempts=1
    )
    operation_id = claimed[0]["operation_id"]
    assert (
        database.fail_durable_audit_export(
            operation_id,
            owner="test",
            error_category="OSError",
            max_attempts=1,
        )
        == "dead_letter"
    )
    database.close()

    assert (
        cli.main(
            [
                "db",
                "audit-replay",
                "--db",
                str(database_path),
                "--operation-id",
                operation_id,
                "--operator",
                "operator-1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["replayed"] is True
