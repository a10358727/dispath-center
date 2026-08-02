from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
import tarfile
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
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LOCAL_HOME_DIR"] = str(home)
    environment.update(extra_env or {})
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
    assert "checksums_sha256=" in (backup_directory / "MANIFEST").read_text()
    assert "jobqueue.db" in (backup_directory / "CHECKSUMS.sha256").read_text()

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


def test_restore_rejects_tampered_backup_before_displacing_state(tmp_path):
    source = tmp_path / "source"
    restore_target = tmp_path / "restore-target"
    backup_root = tmp_path / "backups"
    _seed_complete_home(source, "backup")
    _seed_complete_home(restore_target, "untouched")

    backup = _run(BACKUP_SCRIPT, str(backup_root), home=source)
    assert backup.returncode == 0, backup.stderr
    backup_directory = next(backup_root.iterdir())
    with (backup_directory / "jobqueue.db").open("ab") as stream:
        stream.write(b"tampered")

    restore = _run(
        RESTORE_SCRIPT,
        str(backup_directory),
        home=restore_target,
        user_input="yes\n",
    )

    assert restore.returncode != 0
    assert "checksum" in (restore.stdout + restore.stderr).lower()
    assert _read_database(restore_target / "jobqueue.db") == "untouched"
    assert list(restore_target.glob("restore-displaced-*")) == []


def test_automated_backup_fails_closed_without_explicit_backup_root(tmp_path):
    source = tmp_path / "source"
    _seed_complete_home(source, "source")

    result = _run(
        BACKUP_SCRIPT,
        home=source,
        extra_env={
            "REQUIRE_BACKUP_ROOT": "true",
            "BACKUP_ROOT": "",
        },
    )

    assert result.returncode != 0
    assert "BACKUP_ROOT" in result.stdout
    assert not (REPOSITORY_ROOT / "backups").exists()


def test_backup_refuses_to_publish_without_a_database(tmp_path):
    source = tmp_path / "empty-source"
    source.mkdir()
    backup_root = tmp_path / "backups"

    result = _run(BACKUP_SCRIPT, str(backup_root), home=source)

    assert result.returncode != 0
    assert "jobqueue.db" in result.stdout
    assert not backup_root.exists()


def test_two_backups_never_merge_even_with_close_timestamps(tmp_path):
    source = tmp_path / "source"
    backup_root = tmp_path / "backups"
    _seed_complete_home(source, "source")

    first = _run(BACKUP_SCRIPT, str(backup_root), home=source)
    second = _run(BACKUP_SCRIPT, str(backup_root), home=source)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    backup_directories = sorted(backup_root.iterdir())
    assert len(backup_directories) == 2
    assert backup_directories[0].name != backup_directories[1].name
    for directory in backup_directories:
        assert _read_database(directory / "jobqueue.db") == "source"


def test_restore_rejects_link_archive_before_displacing_state(tmp_path):
    restore_target = tmp_path / "restore-target"
    backup_directory = tmp_path / "malicious-backup"
    restore_target.mkdir()
    backup_directory.mkdir()
    _create_database(restore_target / "jobqueue.db", "untouched")
    _create_database(backup_directory / "jobqueue.db", "backup")

    archive = backup_directory / "hub-git.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        directory = tarfile.TarInfo("git")
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        link = tarfile.TarInfo("git/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        bundle.addfile(link)

    checksum_lines = []
    for artifact in (backup_directory / "jobqueue.db", archive):
        checksum_lines.append(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n"
        )
    checksum_path = backup_directory / "CHECKSUMS.sha256"
    checksum_path.write_text("".join(checksum_lines), encoding="utf-8")
    (backup_directory / "MANIFEST").write_text(
        "created_at=20260730T000000Z\n"
        f"checksums_sha256={hashlib.sha256(checksum_path.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )

    restore = _run(
        RESTORE_SCRIPT,
        str(backup_directory),
        home=restore_target,
        user_input="yes\n",
    )

    assert restore.returncode != 0
    assert "UNUSABLE" in (restore.stdout + restore.stderr)
    assert _read_database(restore_target / "jobqueue.db") == "untouched"
    assert list(restore_target.glob("restore-displaced-*")) == []
    assert list(restore_target.glob(".restore-staging.*")) == []
