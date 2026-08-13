"""Phase 6 §11.3: the restore drill must fail on an unusable backup.

A drill that reports PASS for a corrupt file is worse than no drill, so these
tests drive it from deliberately broken inputs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.audit_anchor import file_sha256, write_audit_checkpoint
from app.db import Database
from scripts.restore_drill import run_backup_directory_drill, run_drill


def _backup(tmp_path, name="backup.db"):
    source = tmp_path / "live.db"
    Database(str(source))
    backup = tmp_path / name
    shutil.copy2(source, backup)
    return str(source), str(backup)


def test_a_good_backup_passes_and_reports_evidence(tmp_path):
    source, backup = _backup(tmp_path)

    report = run_drill(backup, source, keep=False)

    assert report["pass"] is True
    assert report["integrity_ok"] is True
    assert report["restored_tables"] > 0
    assert report["missing_critical_tables"] == []
    assert isinstance(report["restore_seconds"], float)


def test_a_corrupt_backup_fails(tmp_path):
    _, backup = _backup(tmp_path)
    with open(backup, "r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00" * 4096)

    report = run_drill(backup, None, keep=False)

    assert report["pass"] is False


def test_a_backup_missing_critical_tables_fails(tmp_path):
    """A file that opens cleanly but lacks `jobs` is not a usable restore."""
    backup = tmp_path / "empty.db"
    sqlite3.connect(str(backup)).execute("CREATE TABLE unrelated (x INTEGER)")

    report = run_drill(str(backup), None, keep=False)

    assert report["pass"] is False
    assert "jobs" in report["missing_critical_tables"]


def test_a_missing_backup_is_unusable_not_a_pass(tmp_path):
    with pytest.raises(SystemExit, match="UNUSABLE"):
        run_drill(str(tmp_path / "nope.db"), None, keep=False)


def test_a_restore_ahead_of_the_source_fails(tmp_path):
    """More rows in the restore than in the source means this backup is not of
    this database — the one drift direction that is never benign."""
    source, backup = _backup(tmp_path)
    conn = sqlite3.connect(backup)
    conn.execute(
        "INSERT INTO jobs (type, command, status, priority, created_at, depends_on)"
        " VALUES ('adhoc', 'echo hi', 'queued', 'normal', 'now', '[]')"
    )
    conn.commit()
    conn.close()

    report = run_drill(backup, source, keep=False)

    assert "jobs" in report["restored_ahead_of_source"]
    assert report["pass"] is False


def test_the_drill_never_writes_to_its_inputs(tmp_path):
    source, backup = _backup(tmp_path)
    source_before = open(source, "rb").read()
    backup_before = open(backup, "rb").read()

    run_drill(backup, source, keep=False)

    assert open(source, "rb").read() == source_before
    assert open(backup, "rb").read() == backup_before


def test_restore_drill_verifies_signed_audit_anchor_and_manifest_binding(tmp_path):
    source, backup = _backup(tmp_path)
    database = Database(source)
    database.append_durable_audit_event(
        action="restore-drill-checkpoint",
        params={"fixture": True},
        result="ok",
        actor_id="test",
        actor_kind="system",
        authentication="system",
    )
    database.close()
    shutil.copy2(source, backup)
    manifest = tmp_path / "MANIFEST"
    manifest.write_text("checksums_sha256=fixture\n", encoding="utf-8")
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(b"restore-drill-key")
    anchor_path = tmp_path / "checkpoint.json"
    write_audit_checkpoint(
        source,
        anchor_path,
        database_id="restore-drill",
        signing_key=key_path.read_bytes(),
        backup_manifest_sha256=file_sha256(manifest),
    )

    report = run_drill(
        backup,
        source,
        keep=False,
        audit_anchor=str(anchor_path),
        signing_key_file=str(key_path),
        backup_manifest=str(manifest),
    )

    assert report["pass"] is True
    assert report["audit_anchor"]["requested"] is True
    assert report["audit_anchor"]["verified"] is True
    assert report["audit_anchor"]["last_sequence"] == 1


def test_restore_drill_anchor_failure_is_a_failed_drill(tmp_path):
    source, backup = _backup(tmp_path)
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(b"correct-key")
    anchor_path = tmp_path / "checkpoint.json"
    write_audit_checkpoint(
        source,
        anchor_path,
        database_id="restore-drill",
        signing_key=key_path.read_bytes(),
    )

    report = run_drill(
        backup,
        source,
        keep=False,
        audit_anchor=str(anchor_path),
        signing_key_file=str(tmp_path / "wrong.key"),
    )

    assert report["pass"] is False
    assert report["audit_anchor"]["verified"] is False
    assert "FileNotFoundError" in report["audit_anchor"]["error"]


def test_full_backup_directory_drill_verifies_archives_refs_and_results(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    database = Database(str(root / "jobqueue.db"))
    database.close()
    (root / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "servers.yaml").write_text("servers: []\n", encoding="utf-8")
    (root / "auto_approve.yaml").write_text("rules: []\n", encoding="utf-8")
    (root / ".env").write_text("TEST_ONLY=true\n", encoding="utf-8")
    (root / "results").mkdir()
    (root / "results" / "sample.txt").write_text("result", encoding="utf-8")
    (root / "datasets").mkdir()
    (root / "datasets" / "sample.txt").write_text("dataset", encoding="utf-8")
    (root / "git").mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(root / "git" / "demo.git")],
        check=True,
        capture_output=True,
        text=True,
    )
    backup_root = tmp_path / "backups"
    repository = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        ["bash", str(repository / "deploy" / "backup.sh"), "--with-data", str(backup_root)],
        cwd=repository,
        env={
            "PATH": os.environ["PATH"],
            "LOCAL_HOME_DIR": str(root),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    backup_directory = next(backup_root.iterdir())

    report = run_backup_directory_drill(
        str(backup_directory),
        str(root / "jobqueue.db"),
        keep=False,
    )

    assert report["pass"] is True
    assert report["backup_directory_verified"] is True
    assert report["full_restore"] is True
    assert report["git_ref_evidence"]["repositories"] == 1
    assert report["git_ref_evidence"]["errors"] == []
    assert report["result_sample"][0]["path"] == "results/sample.txt"
    assert len(report["result_sample"][0]["sha256"]) == 64


def test_full_backup_directory_drill_rejects_inventory_tamper(tmp_path):
    source, backup = _backup(tmp_path)
    backup_directory = tmp_path / "set"
    backup_directory.mkdir()
    shutil.copy2(backup, backup_directory / "jobqueue.db")
    (backup_directory / "CHECKSUMS.sha256").write_text(
        "0" * 64 + "  jobqueue.db\n",
        encoding="utf-8",
    )
    inventory_digest = hashlib.sha256(
        (backup_directory / "CHECKSUMS.sha256").read_bytes()
    ).hexdigest()
    (backup_directory / "MANIFEST").write_text(
        f"checksums_sha256={inventory_digest}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="checksum mismatch"):
        run_backup_directory_drill(str(backup_directory), source, keep=False)
