from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPOSITORY_ROOT / "deploy" / "backup.sh"
RESTORE_SCRIPT = REPOSITORY_ROOT / "deploy" / "restore.sh"


def _create_database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence(value) VALUES (?)", (value,))


def _read_database(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT value FROM evidence").fetchone()
    assert row is not None
    return row[0]


def _run(
    script: Path,
    *args: str,
    home: Path,
    user_input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LOCAL_HOME_DIR"] = str(home)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPOSITORY_ROOT,
        env=environment,
        input=user_input,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _seed_complete_home(home: Path, label: str) -> None:
    home.mkdir(parents=True)
    _create_database(home / "jobqueue.db", label)
    (home / "audit.jsonl").write_text(f'{{"source":"{label}"}}\n')
    (home / "servers.yaml").write_text(f"servers: [] # {label}\n")
    (home / "auto_approve.yaml").write_text(f"rules: [] # {label}\n")
    (home / ".env").write_text(f"TEST_ONLY_VALUE={label}\n")
    for directory in ("git", "datasets", "results"):
        target = home / directory
        target.mkdir()
        (target / "evidence.txt").write_text(label)


def test_backup_and_restore_round_trip_uses_only_temp_fixtures(tmp_path):
    source = tmp_path / "source"
    restore_target = tmp_path / "restore-target"
    backup_root = tmp_path / "backups"
    _seed_complete_home(source, "backup")
    _seed_complete_home(restore_target, "displaced")

    backup = _run(
        BACKUP_SCRIPT,
        "--with-data",
        str(backup_root),
        home=source,
    )
    assert backup.returncode == 0, backup.stderr

    backup_directories = list(backup_root.iterdir())
    assert len(backup_directories) == 1
    backup_directory = backup_directories[0]
    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup_directory.stat().st_mode) == 0o700
    assert _read_database(backup_directory / "jobqueue.db") == "backup"
    assert "with_data=1" in (backup_directory / "MANIFEST").read_text()

    restore = _run(
        RESTORE_SCRIPT,
        str(backup_directory),
        home=restore_target,
        user_input="yes\n",
    )
    assert restore.returncode == 0, restore.stderr
    assert _read_database(restore_target / "jobqueue.db") == "backup"
    assert (restore_target / "audit.jsonl").read_text() == '{"source":"backup"}\n'
    for directory in ("git", "datasets", "results"):
        assert (restore_target / directory / "evidence.txt").read_text() == "backup"

    displaced = list(restore_target.glob("restore-displaced-*"))
    assert len(displaced) == 1
    assert _read_database(displaced[0] / "jobqueue.db") == "displaced"
    assert (displaced[0] / "git" / "evidence.txt").read_text() == "displaced"


def test_restore_requires_explicit_yes_before_displacing_state(tmp_path):
    source = tmp_path / "source"
    restore_target = tmp_path / "restore-target"
    backup_root = tmp_path / "backups"
    _seed_complete_home(source, "backup")
    _seed_complete_home(restore_target, "untouched")

    backup = _run(BACKUP_SCRIPT, str(backup_root), home=source)
    assert backup.returncode == 0, backup.stderr
    backup_directory = next(backup_root.iterdir())

    restore = _run(
        RESTORE_SCRIPT,
        str(backup_directory),
        home=restore_target,
        user_input="no\n",
    )
    assert restore.returncode != 0
    assert "已取消" in restore.stdout
    assert _read_database(restore_target / "jobqueue.db") == "untouched"
    assert list(restore_target.glob("restore-displaced-*")) == []
