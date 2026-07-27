"""Phase 6 §11.3: the restore drill must fail on an unusable backup.

A drill that reports PASS for a corrupt file is worse than no drill, so these
tests drive it from deliberately broken inputs.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from app.db import Database
from scripts.restore_drill import run_drill


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
