from __future__ import annotations

import json

from app.audit_anchor import (
    build_audit_checkpoint,
    file_sha256,
    verify_audit_checkpoint,
    verify_audit_checkpoint_against_database,
    write_audit_checkpoint,
)
from app.db import Database
from dispatch_center.cli import main


def _append(database: Database, action: str) -> None:
    database.append_durable_audit_event(
        action=action,
        params={"action_index": action},
        result="ok",
        actor_id="system",
        actor_kind="system",
        authentication="system",
    )


def test_signed_checkpoint_is_bound_to_backup_manifest(tmp_path):
    database_path = tmp_path / "audit.db"
    database = Database(str(database_path))
    _append(database, "checkpointed")
    database.close()
    manifest = tmp_path / "MANIFEST"
    manifest.write_text("backup=verified\n", encoding="utf-8")
    key = b"off-host-signing-key"

    checkpoint = build_audit_checkpoint(
        database_path,
        database_id="worker-117-control-plane",
        signing_key=key,
        backup_manifest_sha256=file_sha256(manifest),
        application_version="test",
    )
    assert checkpoint["last_sequence"] == 1
    assert verify_audit_checkpoint(
        checkpoint,
        signing_key=key,
        backup_manifest_sha256=file_sha256(manifest),
    )
    assert not verify_audit_checkpoint(checkpoint, signing_key=b"wrong")
    assert not verify_audit_checkpoint(
        checkpoint, signing_key=key, backup_manifest_sha256="0" * 64
    )


def test_checkpoint_writer_and_cli_use_atomic_json(tmp_path, capsys):
    database_path = tmp_path / "audit.db"
    database = Database(str(database_path))
    _append(database, "checkpointed")
    database.close()
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(b"cli-key")
    output = tmp_path / "off-host" / "checkpoint.json"

    checkpoint = write_audit_checkpoint(
        database_path,
        output,
        database_id="control-plane",
        signing_key=b"cli-key",
    )
    assert json.loads(output.read_text(encoding="utf-8")) == checkpoint

    assert (
        main(
            [
                "db",
                "audit-anchor",
                "--db",
                str(database_path),
                "--output",
                str(output),
                "--database-id",
                "control-plane",
                "--signing-key-file",
                str(key_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"


def test_restore_verify_can_check_signed_checkpoint_boundary(tmp_path, capsys):
    database_path = tmp_path / "restore.db"
    database = Database(str(database_path))
    _append(database, "checkpointed")
    database.close()
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(b"restore-key")
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = write_audit_checkpoint(
        database_path,
        checkpoint_path,
        database_id="restore-drill",
        signing_key=b"restore-key",
    )

    verified = verify_audit_checkpoint_against_database(
        database_path, checkpoint, signing_key=b"restore-key"
    )
    assert verified["last_sequence"] == 1
    assert (
        main(
            [
                "db",
                "restore-verify",
                "--db",
                str(database_path),
                "--audit-anchor",
                str(checkpoint_path),
                "--signing-key-file",
                str(key_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["audit_anchor"] == "ok"
