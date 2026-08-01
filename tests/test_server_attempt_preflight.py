"""DG-AMBIGUOUS-LAUNCH D-5 fixed filesystem preflight contract."""

import sqlite3

import pytest

from app.db import Database
from app.server_attempt_preflight import (
    ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND,
    classify_attempt_filesystem_preflight,
)


def test_preflight_command_is_fixed_read_only_and_has_no_caller_placeholder():
    command = ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND

    assert "stat -f" in command
    assert "agent_jobs" in command
    assert "mkdir" not in command
    assert "touch" not in command
    assert "rm " not in command
    assert "{" not in command
    assert "}" not in command


def test_known_local_filesystems_are_eligible():
    for filesystem_type in (
        "ext4",
        "ext2/ext3",
        "ext2/ext3/ext4",
        "xfs",
        "btrfs",
    ):
        result = classify_attempt_filesystem_preflight(
            f"DISPATCH_FS_TYPE={filesystem_type}\n"
        )
        assert result.status == "eligible"
        assert result.filesystem_type == filesystem_type


def test_non_local_and_fuse_filesystems_are_ineligible():
    for filesystem_type in ("nfs", "nfs4", "cifs", "ceph", "fuse.sshfs"):
        result = classify_attempt_filesystem_preflight(
            f"DISPATCH_FS_TYPE={filesystem_type}\n"
        )
        assert result.status == "ineligible_non_local_fs"
        assert result.filesystem_type == filesystem_type


def test_unknown_or_malformed_output_never_becomes_eligible():
    for output in (
        None,
        "",
        "ext4\n",
        "DISPATCH_FS_TYPE=mysteryfs\n",
        "DISPATCH_FS_TYPE=xfs\nextra\n",
        "DISPATCH_FS_TYPE=xfs;touch /tmp/nope\n",
    ):
        result = classify_attempt_filesystem_preflight(output)
        assert result.status == "unknown"


def test_legacy_revision_schema_migrates_without_fabricating_preflight(tmp_path):
    path = tmp_path / "legacy-revision.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE server_config_revisions (
            id TEXT PRIMARY KEY,
            server_name TEXT NOT NULL,
            revision INTEGER NOT NULL,
            normalized_target_json TEXT NOT NULL,
            credential_ref_json TEXT NOT NULL,
            target_identity_sha256 TEXT NOT NULL,
            assignment_eligibility TEXT NOT NULL,
            publication_state TEXT NOT NULL,
            created_by_approval_id INTEGER,
            created_at TEXT NOT NULL,
            activated_at TEXT,
            retired_at TEXT,
            attempt_backend_preflight TEXT
        );
        CREATE TRIGGER server_config_revisions_preflight_domain
        BEFORE UPDATE OF attempt_backend_preflight ON server_config_revisions
        BEGIN
            SELECT CASE WHEN
                NEW.attempt_backend_preflight IS NOT NULL
                AND NEW.attempt_backend_preflight NOT IN
                    ('eligible', 'ineligible_non_local_fs', 'unknown')
            THEN RAISE(ABORT, 'invalid attempt_backend_preflight') END;
        END;
        INSERT INTO server_config_revisions
            (id, server_name, revision, normalized_target_json,
             credential_ref_json, target_identity_sha256,
             assignment_eligibility, publication_state, created_at)
        VALUES
            ('legacy-revision', 'legacy-server', 1,
             '{"backend":"ssh"}', '{}', 'digest',
             'legacy_observed', 'active', 'legacy');
        """
    )
    raw.commit()
    raw.close()

    database = Database(str(path))
    database.close()
    migrated = sqlite3.connect(path)
    migrated.row_factory = sqlite3.Row
    columns = {
        row[1]
        for row in migrated.execute("PRAGMA table_info(server_config_revisions)")
    }
    assert {
        "attempt_backend_preflight_observed_at",
        "attempt_backend_preflight_contract_version",
        "attempt_backend_preflight_filesystem_type",
    }.issubset(columns)
    triggers = {
        row[0]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    assert "server_config_revisions_preflight_domain" in triggers
    assert "server_config_revisions_preflight_evidence_v1" in triggers
    row = migrated.execute(
        "SELECT attempt_backend_preflight,"
        " attempt_backend_preflight_observed_at,"
        " attempt_backend_preflight_contract_version,"
        " attempt_backend_preflight_filesystem_type"
        " FROM server_config_revisions WHERE id = 'legacy-revision'"
    ).fetchone()
    assert tuple(row) == (None, None, None, None)
    with pytest.raises(
        sqlite3.IntegrityError,
        match="incomplete attempt backend preflight evidence",
    ):
        migrated.execute(
            "UPDATE server_config_revisions"
            " SET attempt_backend_preflight = 'eligible'"
            " WHERE id = 'legacy-revision'"
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="incomplete attempt backend preflight evidence",
    ):
        migrated.execute(
            "INSERT INTO server_config_revisions"
            " (id, server_name, revision, normalized_target_json,"
            " credential_ref_json, target_identity_sha256,"
            " assignment_eligibility, publication_state, created_at,"
            " attempt_backend_preflight,"
            " attempt_backend_preflight_observed_at,"
            " attempt_backend_preflight_contract_version)"
            " VALUES ('incomplete', 'legacy-server', 2,"
            " '{\"backend\":\"ssh\"}', '{}', 'digest-2',"
            " 'legacy_observed', 'prepared', 'legacy', 'eligible',"
            " 'legacy', 'attempt-fs-preflight-v1')"
        )
    migrated.close()
