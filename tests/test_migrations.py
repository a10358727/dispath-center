"""Versioned SQLite migration runner and control-plane CLI tests."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from app.db import (
    API_IDEMPOTENCY_MIGRATION_CHECKSUM,
    API_IDEMPOTENCY_MIGRATION_NAME,
    DATASET_GOVERNANCE_MIGRATION_CHECKSUM,
    DATASET_GOVERNANCE_MIGRATION_NAME,
    Database,
    EXECUTION_PLAN_V2_MIGRATION_CHECKSUM,
    EXECUTION_PLAN_V2_MIGRATION_NAME,
    PROJECT_EXPERIENCE_MIGRATION_CHECKSUM,
    PROJECT_EXPERIENCE_MIGRATION_NAME,
    PROJECT_ROLE_BINDINGS_MIGRATION_CHECKSUM,
    PROJECT_ROLE_BINDINGS_MIGRATION_NAME,
    apply_api_idempotency_migration,
    apply_dataset_governance_migration,
    apply_execution_plan_v2_migration,
    apply_project_experience_migration,
    apply_project_role_bindings_migration,
)
from app.execution_contract import canonical_json, utf8_sha256
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationError,
    MigrationRunner,
    backup_database,
    restore_verify_database,
)
from dispatch_center import cli


def test_database_records_version_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "control.db"
    first = Database(str(path))
    assert first.schema_version() == 10
    first_records = first._conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(row[0], row[1]) for row in first_records] == [
        (1, "legacy_schema_compatibility"),
        (2, "durable_audit_export_outbox"),
        (3, "durable_audit_hash_contract_version"),
        (4, "node_artifact_kind_metadata"),
        (5, "api_idempotency_keys"),
        (6, "project_role_bindings"),
        (7, "project_experience"),
        (8, "dataset_governance"),
        (9, "execution_plan_v2_specs"),
        (10, "ai_conversations"),
    ]
    assert first._conn.execute("PRAGMA user_version").fetchone()[0] == 10
    first.close()

    second = Database(str(path))
    assert second.schema_version() == 10
    second_records = second._conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(row[0], row[1]) for row in second_records] == [
        (1, "legacy_schema_compatibility"),
        (2, "durable_audit_export_outbox"),
        (3, "durable_audit_hash_contract_version"),
        (4, "node_artifact_kind_metadata"),
        (5, "api_idempotency_keys"),
        (6, "project_role_bindings"),
        (7, "project_experience"),
        (8, "dataset_governance"),
        (9, "execution_plan_v2_specs"),
        (10, "ai_conversations"),
    ]
    second.close()


def test_api_idempotency_migration_schema_is_exact_and_source_pinned(tmp_path):
    database = Database(str(tmp_path / "schema.db"))
    columns = database._conn.execute("PRAGMA table_info(api_idempotency_keys)").fetchall()
    foreign_keys = database._conn.execute(
        "PRAGMA foreign_key_list(api_idempotency_keys)"
    ).fetchall()
    indexes = database._conn.execute("PRAGMA index_list(api_idempotency_keys)").fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 5"
    ).fetchone()

    assert [row["name"] for row in columns] == [
        "actor_id",
        "route_key",
        "key_sha256",
        "request_sha256",
        "result_resource_type",
        "result_resource_id",
        "created_at",
        "expires_at",
    ]
    assert all(row["notnull"] == 1 for row in columns)
    assert [row["pk"] for row in columns] == [1, 2, 3, 0, 0, 0, 0, 0]
    assert [(row["from"], row["table"], row["to"], row["on_delete"]) for row in foreign_keys] == [
        ("actor_id", "actors", "id", "RESTRICT")
    ]
    assert any(
        row["name"] == "idx_api_idempotency_keys_expires_at" and row["unique"] == 0
        for row in indexes
    )
    source_checksum = Migration(
        5, API_IDEMPOTENCY_MIGRATION_NAME, apply_api_idempotency_migration
    ).content_checksum()
    assert tuple(record) == (
        API_IDEMPOTENCY_MIGRATION_NAME,
        API_IDEMPOTENCY_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == API_IDEMPOTENCY_MIGRATION_CHECKSUM
    assert CURRENT_SCHEMA_VERSION == 10
    database.close()


def test_project_role_bindings_schema_is_exact_and_source_pinned(tmp_path):
    database = Database(str(tmp_path / "role-schema.db"))
    columns = database._conn.execute("PRAGMA table_info(project_role_bindings)").fetchall()
    foreign_keys = database._conn.execute(
        "PRAGMA foreign_key_list(project_role_bindings)"
    ).fetchall()
    indexes = database._conn.execute("PRAGMA index_list(project_role_bindings)").fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 6"
    ).fetchone()

    assert [row["name"] for row in columns] == [
        "id",
        "project_id",
        "actor_id",
        "role",
        "grant_provenance",
        "grant_approval_id",
        "granted_at",
        "revocation_approval_id",
        "revoked_at",
    ]
    assert [row["notnull"] for row in columns] == [1, 1, 1, 1, 1, 0, 1, 0, 0]
    assert [row["pk"] for row in columns] == [1, 0, 0, 0, 0, 0, 0, 0, 0]
    assert {(row["from"], row["table"], row["to"], row["on_delete"]) for row in foreign_keys} == {
        ("project_id", "projects", "id", "RESTRICT"),
        ("actor_id", "actors", "id", "RESTRICT"),
        ("grant_approval_id", "approvals", "id", "RESTRICT"),
        ("revocation_approval_id", "approvals", "id", "RESTRICT"),
    }
    active_index = next(row for row in indexes if row["name"] == "idx_project_role_bindings_active")
    assert active_index["unique"] == 1
    assert active_index["partial"] == 1
    source_checksum = Migration(
        6,
        PROJECT_ROLE_BINDINGS_MIGRATION_NAME,
        apply_project_role_bindings_migration,
    ).content_checksum()
    assert tuple(record) == (
        PROJECT_ROLE_BINDINGS_MIGRATION_NAME,
        PROJECT_ROLE_BINDINGS_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == PROJECT_ROLE_BINDINGS_MIGRATION_CHECKSUM
    database.close()


def test_project_experience_schema_is_exact_immutable_and_source_pinned(tmp_path):
    database = Database(str(tmp_path / "project-experience-schema.db"))
    expected_columns = {
        "project_environments": [
            "id",
            "project_id",
            "name",
            "created_approval_id",
            "created_by_actor_id",
            "created_at",
        ],
        "environment_revisions": [
            "id",
            "environment_id",
            "project_id",
            "revision",
            "status",
            "contract_version",
            "setup_command",
            "required_server_tags_json",
            "working_directory_policy",
            "non_secret_env_json",
            "secret_references_json",
            "preflight_checks_json",
            "revision_digest",
            "supersedes_id",
            "approval_id",
            "created_by_actor_id",
            "created_at",
        ],
        "run_profile_specs": [
            "run_profile_id",
            "project_id",
            "contract_version",
            "environment_revision_id",
            "argv_template_json",
            "parameter_schema_json",
            "resource_requirements_json",
            "output_declarations_json",
            "spec_digest",
            "approval_id",
            "created_by_actor_id",
            "created_at",
        ],
        "project_default_revisions": [
            "id",
            "project_id",
            "revision",
            "contract_version",
            "environment_revision_id",
            "run_profile_id",
            "run_profile_spec_digest",
            "parameter_values_json",
            "revision_digest",
            "supersedes_id",
            "approval_id",
            "created_by_actor_id",
            "created_at",
        ],
    }
    for table_name, columns in expected_columns.items():
        rows = database._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        assert [row["name"] for row in rows] == columns
        trigger_names = {
            row["name"]
            for row in database._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                (table_name,),
            ).fetchall()
        }
        assert trigger_names == {
            f"trg_{table_name}_immutable_update",
            f"trg_{table_name}_immutable_delete",
        }

    def foreign_key_groups(table_name):
        groups = {}
        for row in database._conn.execute(
            f"PRAGMA foreign_key_list({table_name})"
        ).fetchall():
            group = groups.setdefault(
                row["id"],
                {"table": row["table"], "columns": []},
            )
            group["columns"].append((row["seq"], row["from"], row["to"]))
        return {
            (
                group["table"],
                tuple(
                    (source, target)
                    for _, source, target in sorted(group["columns"])
                ),
            )
            for group in groups.values()
        }

    spec_foreign_keys = foreign_key_groups("run_profile_specs")
    assert (
        "run_profiles",
        (("run_profile_id", "id"), ("project_id", "project_id")),
    ) in spec_foreign_keys
    assert (
        "environment_revisions",
        (("environment_revision_id", "id"), ("project_id", "project_id")),
    ) in spec_foreign_keys
    defaults_foreign_keys = foreign_key_groups("project_default_revisions")
    assert (
        "run_profile_specs",
        (
            ("project_id", "project_id"),
            ("run_profile_id", "run_profile_id"),
            ("run_profile_spec_digest", "spec_digest"),
            ("environment_revision_id", "environment_revision_id"),
        ),
    ) in defaults_foreign_keys
    run_profile_indexes = {
        row["name"]: row
        for row in database._conn.execute(
            "PRAGMA index_list(run_profiles)"
        ).fetchall()
    }
    assert run_profile_indexes["idx_run_profiles_id_project_v2"]["unique"] == 1

    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 7"
    ).fetchone()
    source_checksum = Migration(
        7,
        PROJECT_EXPERIENCE_MIGRATION_NAME,
        apply_project_experience_migration,
    ).content_checksum()
    assert tuple(record) == (
        PROJECT_EXPERIENCE_MIGRATION_NAME,
        PROJECT_EXPERIENCE_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == PROJECT_EXPERIENCE_MIGRATION_CHECKSUM
    database.close()


def test_dataset_governance_schema_is_exact_immutable_and_source_pinned(tmp_path):
    database = Database(str(tmp_path / "dataset-governance-schema.db"))
    expected_columns = {
        "dataset_assets": [
            "id",
            "owning_project_id",
            "name",
            "description",
            "contract_version",
            "data_card_json",
            "asset_digest",
            "created_approval_id",
            "created_by_actor_id",
            "created_at",
        ],
        "dataset_asset_snapshots": [
            "asset_id",
            "owning_project_id",
            "snapshot_id",
            "link_kind",
            "link_approval_id",
            "linked_by_actor_id",
            "linked_at",
        ],
        "dataset_share_offers": [
            "id",
            "asset_id",
            "source_project_id",
            "target_project_id",
            "contract_version",
            "snapshot_ids_json",
            "snapshot_set_digest",
            "offer_digest",
            "expires_at",
            "created_approval_id",
            "created_by_actor_id",
            "created_at",
        ],
        "project_dataset_grants": [
            "id",
            "offer_id",
            "asset_id",
            "source_project_id",
            "target_project_id",
            "snapshot_id",
            "contract_version",
            "accept_approval_id",
            "accepted_by_actor_id",
            "granted_at",
            "revocation_approval_id",
            "revoked_at",
        ],
        "dataset_alias_revisions": [
            "id",
            "project_id",
            "asset_id",
            "alias_name",
            "revision",
            "contract_version",
            "snapshot_id",
            "revision_digest",
            "supersedes_id",
            "approval_id",
            "created_by_actor_id",
            "created_at",
        ],
        "dataset_lineage_edges": [
            "id",
            "input_asset_id",
            "input_snapshot_id",
            "output_asset_id",
            "output_snapshot_id",
            "producing_execution_plan_id",
            "output_declaration_name",
            "publish_approval_id",
            "created_at",
        ],
    }
    for table_name, column_names in expected_columns.items():
        columns = database._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        assert [row["name"] for row in columns] == column_names
        assert database._conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] == 0

    immutable_tables = set(expected_columns).difference({"project_dataset_grants"})
    triggers = {
        row["name"]
        for row in database._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    for table_name in immutable_tables:
        assert f"trg_{table_name}_immutable_update" in triggers
        assert f"trg_{table_name}_immutable_delete" in triggers
    assert "trg_project_dataset_grants_monotonic_update" in triggers
    assert "trg_project_dataset_grants_no_delete" in triggers

    alias_foreign_keys = {
        (row["from"], row["table"], row["to"], row["on_delete"])
        for row in database._conn.execute(
            "PRAGMA foreign_key_list(dataset_alias_revisions)"
        ).fetchall()
    }
    assert ("snapshot_id", "dataset_asset_snapshots", "snapshot_id", "RESTRICT") in alias_foreign_keys
    assert ("asset_id", "dataset_asset_snapshots", "asset_id", "RESTRICT") in alias_foreign_keys

    def unique_index_columns(table_name: str) -> set[tuple[str, ...]]:
        indexes = database._conn.execute(
            f"PRAGMA index_list({table_name})"
        ).fetchall()
        return {
            tuple(
                row["name"]
                for row in database._conn.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()
            )
            for index in indexes
            if index["unique"]
        }

    assert (
        "project_id",
        "asset_id",
        "alias_name",
        "revision",
    ) in unique_index_columns("dataset_alias_revisions")
    assert (
        "input_snapshot_id",
        "output_snapshot_id",
    ) in unique_index_columns("dataset_lineage_edges")

    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 8"
    ).fetchone()
    source_checksum = Migration(
        8,
        DATASET_GOVERNANCE_MIGRATION_NAME,
        apply_dataset_governance_migration,
    ).content_checksum()
    assert tuple(record) == (
        DATASET_GOVERNANCE_MIGRATION_NAME,
        DATASET_GOVERNANCE_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == DATASET_GOVERNANCE_MIGRATION_CHECKSUM
    database.close()


def test_execution_plan_v2_schema_is_exact_immutable_and_source_pinned(tmp_path):
    database = Database(str(tmp_path / "execution-plan-v2-schema.db"))
    columns = database._conn.execute(
        "PRAGMA table_info(execution_plan_v2_specs)"
    ).fetchall()
    assert [row["name"] for row in columns] == [
        "execution_plan_id",
        "project_id",
        "contract_version",
        "canonical_spec_json",
        "plan_digest",
        "project_version_id",
        "project_defaults_revision_id",
        "project_defaults_revision_digest",
        "run_profile_id",
        "run_profile_spec_digest",
        "environment_revision_id",
        "environment_revision_digest",
        "parameter_values_json",
        "dataset_none",
        "dataset_bindings_json",
        "dataset_bindings_digest",
        "target_selection_kind",
        "dispatch_policy_id",
        "dispatch_policy_digest",
        "server_config_revision_id",
        "target_identity_sha256",
        "backend",
        "project_instance_id",
        "project_instance_checkout_digest",
        "resource_requirements_json",
        "resource_requirements_digest",
        "output_declarations_digest",
        "canonical_argv_json",
        "compiled_command_sha256",
        "command_bridge_version",
        "job_command_sha256",
        "observation_max_age_seconds",
        "submit_observation_id",
        "submit_observation_json",
        "submit_observation_digest",
        "created_approval_id",
        "created_by_actor_id",
        "created_at",
    ]
    foreign_keys = {
        (row["from"], row["table"], row["to"], row["on_delete"])
        for row in database._conn.execute(
            "PRAGMA foreign_key_list(execution_plan_v2_specs)"
        ).fetchall()
    }
    assert foreign_keys == {
        ("execution_plan_id", "execution_plans", "id", "RESTRICT"),
        ("project_id", "projects", "id", "RESTRICT"),
        ("project_version_id", "project_versions", "id", "RESTRICT"),
        (
            "project_defaults_revision_id",
            "project_default_revisions",
            "id",
            "RESTRICT",
        ),
        ("run_profile_id", "run_profiles", "id", "RESTRICT"),
        ("environment_revision_id", "environment_revisions", "id", "RESTRICT"),
        ("dispatch_policy_id", "dispatch_policies", "id", "RESTRICT"),
        (
            "server_config_revision_id",
            "server_config_revisions",
            "id",
            "RESTRICT",
        ),
        ("project_instance_id", "project_instances", "id", "RESTRICT"),
        ("created_approval_id", "approvals", "id", "RESTRICT"),
        ("created_by_actor_id", "actors", "id", "RESTRICT"),
    }
    indexes = {
        row["name"]: (row["unique"], row["partial"])
        for row in database._conn.execute(
            "PRAGMA index_list(execution_plan_v2_specs)"
        ).fetchall()
    }
    assert indexes["idx_execution_plan_v2_specs_project_time"] == (0, 0)
    assert indexes["idx_execution_plan_v2_specs_server_revision"] == (0, 0)
    assert indexes["idx_execution_plan_v2_specs_policy"] == (0, 1)
    assert indexes["idx_execution_plan_v2_specs_dataset_usage"] == (0, 0)
    triggers = {
        row["name"]
        for row in database._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'execution_plan_v2_specs'"
        ).fetchall()
    }
    assert triggers == {
        "trg_execution_plan_v2_specs_insert_consistency",
        "trg_execution_plan_v2_specs_immutable_update",
        "trg_execution_plan_v2_specs_immutable_delete",
    }
    job_guard = database._conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'jobs_execution_pin_insert_guard'"
    ).fetchone()["sql"]
    normalized_job_guard = " ".join(job_guard.split())
    assert "kind <> 'execution_plan_v2'" in normalized_job_guard
    assert (
        "payload_contract_version = NEW.execution_contract_version"
        in normalized_job_guard
    )
    assert "'execution-plan-v2-approval-v1'" in normalized_job_guard
    assert (
        "NEW.execution_contract_version = 'execution-plan-v2'"
        in normalized_job_guard
    )
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 9"
    ).fetchone()
    source_checksum = Migration(
        9,
        EXECUTION_PLAN_V2_MIGRATION_NAME,
        apply_execution_plan_v2_migration,
    ).content_checksum()
    assert tuple(record) == (
        EXECUTION_PLAN_V2_MIGRATION_NAME,
        EXECUTION_PLAN_V2_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == EXECUTION_PLAN_V2_MIGRATION_CHECKSUM
    database.close()


@pytest.mark.parametrize(
    ("kind", "approval_contract", "job_contract", "digest_override", "status"),
    [
        (
            "execution_plan_v2",
            "execution-plan-v2",
            "execution-plan-v2",
            None,
            "pending",
        ),
        (
            "execution_plan_v2",
            "execution-plan-v2-approval-v1",
            "execution-plan-v2-approval-v1",
            None,
            "pending",
        ),
        (
            "enqueue",
            "execution-plan-v2-approval-v1",
            "execution-plan-v2",
            None,
            "pending",
        ),
        (
            "execution_plan_v2",
            "execution-plan-v2-approval-v1",
            "execution-plan-v2",
            "0" * 64,
            "pending",
        ),
        (
            "execution_plan_v2",
            "execution-plan-v2-approval-v1",
            "execution-plan-v2",
            None,
            "rejected",
        ),
    ],
)
def test_execution_plan_v2_job_pin_trigger_rejects_every_non_exact_mapping(
    tmp_path,
    kind,
    approval_contract,
    job_contract,
    digest_override,
    status,
):
    database = Database(str(tmp_path / "execution-plan-v2-job-guard.db"))
    payload_json = canonical_json(
        {
            "contract_version": "execution-plan-v2-approval-v1",
            "execution_plan_id": "10000000-0000-0000-0000-000000000001",
            "project_id": "10000000-0000-0000-0000-000000000002",
            "plan_digest": "1" * 64,
        }
    )
    payload_sha256 = utf8_sha256(payload_json)
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO approvals (
                kind, payload, status, created_at, payload_sha256,
                payload_contract_version, payload_immutable_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                payload_json,
                status,
                "2026-08-10T00:00:00+00:00",
                payload_sha256,
                approval_contract,
                "2026-08-10T00:00:00+00:00",
            ),
        )
        approval_id = int(cursor.lastrowid)
    with pytest.raises(sqlite3.IntegrityError, match="approval linkage mismatch"):
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs (
                    type, command, status, priority, created_at,
                    execution_approval_id, approved_payload_sha256,
                    execution_contract_version, execution_contract_role,
                    approved_command_sha256
                ) VALUES ('adhoc', 'true', 'queued', 'normal', ?, ?, ?, ?,
                          'main', ?)
                """,
                (
                    "2026-08-10T00:00:00+00:00",
                    approval_id,
                    digest_override or payload_sha256,
                    job_contract,
                    "b5bea41b6c623f7c09f1bf24dcae58ebab3c0cdd90ad966bc43a45b44867e12b",
                ),
            )
    database.close()


def test_execution_plan_v2_job_pin_trigger_requires_all_fields(tmp_path):
    database = Database(str(tmp_path / "execution-plan-v2-job-fields.db"))
    payload_json = canonical_json(
        {
            "contract_version": "execution-plan-v2-approval-v1",
            "execution_plan_id": "10000000-0000-0000-0000-000000000001",
            "project_id": "10000000-0000-0000-0000-000000000002",
            "plan_digest": "1" * 64,
        }
    )
    payload_sha256 = utf8_sha256(payload_json)
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO approvals (
                kind, payload, status, created_at, payload_sha256,
                payload_contract_version, payload_immutable_at
            ) VALUES ('execution_plan_v2', ?, 'pending', ?, ?,
                      'execution-plan-v2-approval-v1', ?)
            """,
            (
                payload_json,
                "2026-08-10T00:00:00+00:00",
                payload_sha256,
                "2026-08-10T00:00:00+00:00",
            ),
        )
        approval_id = int(cursor.lastrowid)
    with pytest.raises(sqlite3.IntegrityError, match="fields must be all-or-none"):
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs (
                    type, command, status, priority, created_at,
                    execution_approval_id, approved_payload_sha256,
                    execution_contract_version, approved_command_sha256
                ) VALUES ('adhoc', 'true', 'queued', 'normal', ?, ?, ?,
                          'execution-plan-v2', ?)
                """,
                (
                    "2026-08-10T00:00:00+00:00",
                    approval_id,
                    payload_sha256,
                    "b5bea41b6c623f7c09f1bf24dcae58ebab3c0cdd90ad966bc43a45b44867e12b",
                ),
            )
    database.close()


def _drop_project_experience_schema(connection: sqlite3.Connection) -> None:
    for table_name in (
        "project_default_revisions",
        "run_profile_specs",
        "environment_revisions",
        "project_environments",
    ):
        connection.execute(f"DROP TABLE {table_name}")
    connection.execute("DROP INDEX IF EXISTS idx_run_profiles_id_project_v2")


def _drop_dataset_governance_schema(connection: sqlite3.Connection) -> None:
    for table_name in (
        "dataset_lineage_edges",
        "dataset_alias_revisions",
        "project_dataset_grants",
        "dataset_share_offers",
        "dataset_asset_snapshots",
        "dataset_assets",
    ):
        connection.execute(f"DROP TABLE {table_name}")


def _drop_execution_plan_v2_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE execution_plan_v2_specs")
    connection.execute("DROP TRIGGER jobs_execution_pin_insert_guard")
    connection.execute(
        """
        CREATE TRIGGER jobs_execution_pin_insert_guard
        BEFORE INSERT ON jobs
        WHEN
            NEW.execution_approval_id IS NOT NULL
            OR NEW.approved_payload_sha256 IS NOT NULL
            OR NEW.execution_contract_version IS NOT NULL
            OR NEW.execution_contract_role IS NOT NULL
            OR NEW.approved_command_sha256 IS NOT NULL
        BEGIN
            SELECT CASE WHEN
                NEW.execution_approval_id IS NULL
                OR NEW.approved_payload_sha256 IS NULL
                OR NEW.execution_contract_version IS NULL
                OR NEW.execution_contract_role IS NULL
                OR NEW.approved_command_sha256 IS NULL
            THEN RAISE(
                ABORT, 'job execution pin fields must be all-or-none'
            ) END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM approvals
                WHERE id = NEW.execution_approval_id
                  AND payload_sha256 = NEW.approved_payload_sha256
                  AND payload_contract_version = NEW.execution_contract_version
                  AND status IN ('pending', 'approved')
            )
            THEN RAISE(
                ABORT, 'job execution approval linkage mismatch'
            ) END;
        END
        """
    )


def _drop_ai_conversation_schema(connection: sqlite3.Connection) -> None:
    """DG-CONVERSATION-V1 (migration 10): purely additive new tables, no
    trigger/legacy-shape to restore — dropping child-before-parent is enough
    to get back to a representative pre-v10 snapshot."""
    connection.execute("DROP TABLE ai_conversation_messages")
    connection.execute("DROP TABLE ai_conversations")


def _revert_to_representative_v5(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    _drop_project_experience_schema(connection)
    connection.execute("DROP TABLE project_role_bindings")
    connection.execute("DELETE FROM schema_migrations WHERE version IN (6, 7, 8, 9, 10)")
    connection.execute("PRAGMA user_version = 5")
    connection.commit()
    connection.close()


def _revert_to_representative_v6(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    _drop_project_experience_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version IN (7, 8, 9, 10)")
    connection.execute("PRAGMA user_version = 6")
    connection.commit()
    connection.close()


def _revert_to_representative_v7(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version IN (8, 9, 10)")
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()


def _revert_to_representative_v8(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version IN (9, 10)")
    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    connection.close()


def test_representative_v8_upgrade_installs_execution_plan_v2_without_backfill(
    tmp_path,
):
    path = tmp_path / "execution-plan-v2-v8-upgrade.db"
    database = Database(str(path))
    database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v8(path)

    upgraded = Database(str(path))
    assert upgraded.schema_version() == 10
    assert upgraded.get_project("legacy-project") is not None
    assert upgraded._conn.execute(
        "SELECT COUNT(*) FROM execution_plan_v2_specs"
    ).fetchone()[0] == 0
    assert upgraded._conn.execute("PRAGMA user_version").fetchone()[0] == 10
    upgraded.close()


def test_representative_v7_upgrade_installs_dataset_governance_without_backfill(
    tmp_path,
):
    path = tmp_path / "dataset-governance-v7-upgrade.db"
    database = Database(str(path))
    database.insert_dataset(
        "legacy-dataset",
        "v1",
        1,
        "/sensitive/legacy/source",
        {"files": 1},
    )
    database.close()
    _revert_to_representative_v7(path)

    upgraded = Database(str(path))
    assert upgraded.schema_version() == 10
    assert upgraded.get_dataset("legacy-dataset", "v1") is not None
    for table_name in (
        "dataset_assets",
        "dataset_asset_snapshots",
        "dataset_share_offers",
        "project_dataset_grants",
        "dataset_alias_revisions",
        "dataset_lineage_edges",
    ):
        assert upgraded._conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] == 0
    upgraded.close()


def test_representative_v6_upgrade_installs_project_experience_without_backfill(
    tmp_path,
):
    path = tmp_path / "project-experience-v6-upgrade.db"
    database = Database(str(path))
    project_id = database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v6(path)

    upgraded = Database(str(path))
    assert upgraded.schema_version() == 10
    assert upgraded.get_project("legacy-project").id == project_id
    for table_name in (
        "project_environments",
        "environment_revisions",
        "run_profile_specs",
        "project_default_revisions",
    ):
        assert upgraded._conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] == 0
    upgraded.close()


def test_v5_role_migration_maps_only_valid_legacy_evidence(tmp_path):
    path = tmp_path / "role-v5-upgrade.db"
    database = Database(str(path))

    one_admin = database.insert_project("one-admin", "/tmp/one-admin")
    two_admins = database.insert_project("two-admins", "/tmp/two-admins")
    service_project = database.insert_project("service-admin", "/tmp/service-admin")
    legacy_project = database.insert_project("legacy-admin", "/tmp/legacy-admin")
    disabled_project = database.insert_project("disabled-admin", "/tmp/disabled-admin")
    orphan_project = database.insert_project("orphan", "/tmp/orphan")
    invalid_time_project = database.insert_project("invalid-time", "/tmp/invalid-time")
    database.insert_project("no-membership", "/tmp/no-membership")

    human_one = database.insert_actor(actor_type="human", display_name="Human One")
    human_two = database.insert_actor(actor_type="human", display_name="Human Two")
    service = database.insert_actor(actor_type="service", display_name="Service")
    legacy = database.insert_actor(actor_type="legacy", display_name="Legacy")
    disabled = database.insert_actor(actor_type="human", display_name="Disabled")
    invalid_time_actor = database.insert_actor(actor_type="human", display_name="Invalid Time")

    database.upsert_project_membership(project=one_admin, actor_id=human_one.id, role="admin")
    database.upsert_project_membership(project=two_admins, actor_id=human_one.id, role="admin")
    database.upsert_project_membership(project=two_admins, actor_id=human_two.id, role="admin")
    database.upsert_project_membership(project=service_project, actor_id=service.id, role="admin")
    database.upsert_project_membership(project=legacy_project, actor_id=legacy.id, role="admin")
    database.upsert_project_membership(project=disabled_project, actor_id=disabled.id, role="admin")
    database.update_actor(disabled.id, disabled_at="2026-08-01T00:00:00+00:00")
    database.upsert_project_membership(
        project=invalid_time_project,
        actor_id=invalid_time_actor.id,
        role="viewer",
    )
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE project_memberships SET updated_at = 'not-a-time' WHERE project_id = ?",
            (invalid_time_project,),
        )
        cursor.execute(
            """
            INSERT INTO project_memberships (
                project_id, actor_id, role, created_by_actor_id,
                created_at, updated_at
            ) VALUES (?, ?, 'admin', NULL, ?, ?)
            """,
            (
                orphan_project,
                "00000000-0000-0000-0000-000000000099",
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
        )
    observed_times = {
        (row["project_id"], row["actor_id"]): row["updated_at"]
        for row in database._conn.execute(
            "SELECT project_id, actor_id, updated_at FROM project_memberships"
        ).fetchall()
    }
    database.close()
    _revert_to_representative_v5(path)

    upgraded = Database(str(path))
    assert upgraded.schema_version() == 10
    assert {
        binding.role.value
        for binding in upgraded.list_project_role_bindings(project_id=one_admin, active_only=True)
    } == {"owner", "operator", "reviewer", "dataset_manager"}
    assert {
        binding.role.value
        for binding in upgraded.list_project_role_bindings(
            project_id=service_project, active_only=True
        )
    } == {"operator", "dataset_manager"}
    assert {
        binding.role.value
        for binding in upgraded.list_project_role_bindings(
            project_id=legacy_project, active_only=True
        )
    } == {"operator", "dataset_manager"}
    disabled_bindings = upgraded.list_project_role_bindings(
        project_id=disabled_project, active_only=True
    )
    assert {binding.role.value for binding in disabled_bindings} == {
        "owner",
        "operator",
        "reviewer",
        "dataset_manager",
    }
    assert upgraded.list_project_role_bindings(project_id=orphan_project, active_only=True) == []
    assert (
        upgraded.list_project_role_bindings(project_id=invalid_time_project, active_only=True) == []
    )
    all_mapped = upgraded.list_project_role_bindings(active_only=True)
    assert all(
        binding.grant_provenance.value == "legacy_membership"
        and binding.grant_approval_id is None
        and binding.granted_at == observed_times[(binding.project_id, binding.actor_id)]
        for binding in all_mapped
    )
    assert upgraded.get_project_role_snapshot(one_admin)["readiness"].state.value == (
        "needs_role_repair"
    )
    assert upgraded.get_project_role_snapshot(two_admins)["readiness"].state.value == ("ready")
    assert (
        upgraded.get_project_role_snapshot(service_project)["readiness"].state.value
        == "needs_role_repair"
    )
    assert "orphan_legacy_membership" in {
        reason.value
        for reason in upgraded.get_project_role_snapshot(orphan_project)["readiness"].reasons
    }
    upgraded.close()


def test_active_role_binding_is_unique_and_can_be_regranted_after_revoke(tmp_path):
    database = Database(str(tmp_path / "role-regrant.db"))
    project_id = database.insert_project("roles", "/tmp/roles")
    actor = database.insert_actor(actor_type="human", display_name="Actor")
    approval_id = database.insert_approval(
        "project_membership_upsert",
        {"project_id": project_id, "actor_id": actor.id, "role": "viewer"},
        requester_actor_id=actor.id,
    )
    values = (
        "binding-one",
        project_id,
        actor.id,
        "viewer",
        "legacy_membership",
        "2026-08-01T00:00:00+00:00",
    )
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_role_bindings (
                id, project_id, actor_id, role, grant_provenance, granted_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO project_role_bindings (
                    id, project_id, actor_id, role,
                    grant_provenance, granted_at
                ) VALUES ('binding-two', ?, ?, 'viewer',
                          'legacy_membership', ?)
                """,
                (project_id, actor.id, "2026-08-02T00:00:00+00:00"),
            )
    with database.cursor() as cursor:
        cursor.execute(
            """
            UPDATE project_role_bindings
            SET revocation_approval_id = ?, revoked_at = ?
            WHERE id = 'binding-one'
            """,
            (approval_id, "2026-08-03T00:00:00+00:00"),
        )
        cursor.execute(
            """
            INSERT INTO project_role_bindings (
                id, project_id, actor_id, role,
                grant_provenance, grant_approval_id, granted_at
            ) VALUES ('binding-two', ?, ?, 'viewer',
                      'legacy_membership', ?, ?)
            """,
            (
                project_id,
                actor.id,
                approval_id,
                "2026-08-03T00:00:00+00:00",
            ),
        )
    assert [
        binding.id
        for binding in database.list_project_role_bindings(project_id=project_id, active_only=True)
    ] == ["binding-two"]
    database.close()


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("route_key", ""),
        ("route_key", "x" * 513),
        ("key_sha256", "A" * 64),
        ("key_sha256", "a" * 63),
        ("request_sha256", "g" * 64),
        ("result_resource_type", ""),
        ("result_resource_type", "x" * 129),
        ("result_resource_id", ""),
        ("result_resource_id", "x" * 513),
    ],
)
def test_api_idempotency_schema_checks_reject_invalid_rows(tmp_path, column, invalid_value):
    database = Database(str(tmp_path / f"invalid-{column}-{len(invalid_value)}.db"))
    database.insert_actor(
        actor_id="actor-1",
        actor_type="human",
        display_name="Actor One",
    )
    values = {
        "actor_id": "actor-1",
        "route_key": "POST /api/v2/resources",
        "key_sha256": "a" * 64,
        "request_sha256": "b" * 64,
        "result_resource_type": "resource",
        "result_resource_id": "resource-1",
        "created_at": "2026-08-07T00:00:00.000000Z",
        "expires_at": "2026-08-08T00:00:00.000000Z",
    }
    values[column] = invalid_value
    with pytest.raises(sqlite3.IntegrityError):
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO api_idempotency_keys (
                    actor_id, route_key, key_sha256, request_sha256,
                    result_resource_type, result_resource_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(values.values()),
            )
    database.close()


def _revert_to_representative_v4(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    _drop_project_experience_schema(connection)
    connection.execute("DROP TABLE project_role_bindings")
    connection.execute("DROP TABLE api_idempotency_keys")
    connection.execute("DELETE FROM schema_migrations WHERE version IN (5, 6, 7, 8, 9, 10)")
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()


def test_representative_v4_upgrade_preserves_existing_rows(tmp_path):
    path = tmp_path / "v4-upgrade.db"
    initial = Database(str(path))
    initial.insert_actor(
        actor_id="legacy-actor",
        actor_type="human",
        display_name="Legacy Actor",
    )
    with initial.cursor() as cursor:
        cursor.execute(
            "INSERT INTO approvals (kind, payload, status, created_at) "
            "VALUES ('enqueue', '{}', 'pending', '2026-08-01T00:00:00Z')"
        )
    initial.close()
    _revert_to_representative_v4(path)

    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'api_idempotency_keys'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == 10
    assert upgraded.schema_is_initialized() is True
    assert upgraded.get_actor("legacy-actor").display_name == "Legacy Actor"
    assert upgraded._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    assert upgraded._conn.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0] == 0
    assert (
        upgraded._conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
        ).fetchone()[0]
        == 1
    )
    assert (
        upgraded._conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
        ).fetchone()[0]
        == 1
    )
    upgraded.close()


def test_two_connections_compete_for_product_migrations_once(tmp_path):
    path = tmp_path / "concurrent-product.db"
    bootstrap = Database(str(path))
    bootstrap.close()
    _revert_to_representative_v4(path)
    barrier = threading.Barrier(2)
    versions: list[int] = []
    failures: list[BaseException] = []

    def upgrade() -> None:
        try:
            barrier.wait(timeout=5)
            database = Database(str(path))
            versions.append(database.schema_version())
            database.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=upgrade) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert versions == [10, 10]
    connection = sqlite3.connect(path)
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 5").fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'api_idempotency_keys'"
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 6").fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'project_role_bindings'"
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 7").fetchone()[0]
        == 1
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 8").fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'project_default_revisions'"
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_v5_schema_and_ledger_roll_back_together_then_retry(tmp_path):
    path = tmp_path / "v5-rollback.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("CREATE TABLE actors (id TEXT PRIMARY KEY)")

    def migration_one(_connection):
        pass

    def migration_two(_connection):
        pass

    def migration_three(_connection):
        pass

    def migration_four(_connection):
        pass

    first_four = [
        Migration(1, "one", migration_one),
        Migration(2, "two", migration_two),
        Migration(3, "three", migration_three),
        Migration(4, "four", migration_four),
    ]
    MigrationRunner(connection, path, first_four).upgrade()

    def fail_after_v5_ddl(conn):
        apply_api_idempotency_migration(conn)
        raise RuntimeError("injected v5 migration failure")

    with pytest.raises(RuntimeError, match="injected v5 migration failure"):
        MigrationRunner(
            connection,
            path,
            [*first_four, Migration(5, API_IDEMPOTENCY_MIGRATION_NAME, fail_after_v5_ddl)],
        ).upgrade()
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'api_idempotency_keys'"
        ).fetchone()
        is None
    )
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 4
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4

    MigrationRunner(
        connection,
        path,
        [
            *first_four,
            Migration(
                5,
                API_IDEMPOTENCY_MIGRATION_NAME,
                apply_api_idempotency_migration,
                checksum=API_IDEMPOTENCY_MIGRATION_CHECKSUM,
                validate_source=True,
            ),
        ],
    ).upgrade()
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 5").fetchone()[0]
        == 1
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    connection.close()


def test_v6_schema_backfill_and_ledger_roll_back_together_then_retry(tmp_path):
    path = tmp_path / "v6-rollback.db"
    database = Database(str(path))
    project_id = database.insert_project("v6-rollback", "/tmp/v6-rollback")
    actor = database.insert_actor(
        actor_type="human",
        display_name="V6 rollback actor",
    )
    database.upsert_project_membership(
        project=project_id,
        actor_id=actor.id,
        role="admin",
    )
    database.close()
    _revert_to_representative_v5(path)

    connection = sqlite3.connect(path)
    existing = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations WHERE version <= 5 ORDER BY version"
    ).fetchall()
    connection.execute("UPDATE schema_migrations SET content_checksum = NULL WHERE version <= 5")
    connection.commit()

    def already_applied(_connection):
        pass

    first_five = [
        Migration(
            version,
            name,
            already_applied,
            checksum=checksum,
        )
        for version, name, checksum in existing
    ]

    def fail_after_v6_backfill(conn):
        apply_project_role_bindings_migration(conn)
        assert conn.execute("SELECT COUNT(*) FROM project_role_bindings").fetchone()[0] == 4
        raise RuntimeError("injected v6 migration failure")

    with pytest.raises(RuntimeError, match="injected v6 migration failure"):
        MigrationRunner(
            connection,
            path,
            [
                *first_five,
                Migration(
                    6,
                    PROJECT_ROLE_BINDINGS_MIGRATION_NAME,
                    fail_after_v6_backfill,
                ),
            ],
        ).upgrade()

    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'project_role_bindings'"
        ).fetchone()
        is None
    )
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 5

    MigrationRunner(
        connection,
        path,
        [
            *first_five,
            Migration(
                6,
                PROJECT_ROLE_BINDINGS_MIGRATION_NAME,
                apply_project_role_bindings_migration,
                checksum=PROJECT_ROLE_BINDINGS_MIGRATION_CHECKSUM,
                validate_source=True,
            ),
        ],
    ).upgrade()
    assert (
        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 6").fetchone()[0]
        == 1
    )
    assert connection.execute("SELECT COUNT(*) FROM project_role_bindings").fetchone()[0] == 4
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    connection.close()


def test_v7_schema_and_ledger_roll_back_together_then_retry(tmp_path):
    path = tmp_path / "v7-rollback.db"
    database = Database(str(path))
    database.close()
    _revert_to_representative_v6(path)

    connection = sqlite3.connect(path)
    existing = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations WHERE version <= 6 ORDER BY version"
    ).fetchall()
    connection.execute("UPDATE schema_migrations SET content_checksum = NULL WHERE version <= 6")
    connection.commit()

    def already_applied(_connection):
        pass

    first_six = [
        Migration(version, name, already_applied, checksum=checksum)
        for version, name, checksum in existing
    ]

    def fail_after_v7_ddl(conn):
        apply_project_experience_migration(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'project_environments'"
        ).fetchone()[0] == 1
        raise RuntimeError("injected v7 migration failure")

    with pytest.raises(RuntimeError, match="injected v7 migration failure"):
        MigrationRunner(
            connection,
            path,
            [
                *first_six,
                Migration(7, PROJECT_EXPERIENCE_MIGRATION_NAME, fail_after_v7_ddl),
            ],
        ).upgrade()

    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'project_environments'"
    ).fetchone() is None
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6

    MigrationRunner(
        connection,
        path,
        [
            *first_six,
            Migration(
                7,
                PROJECT_EXPERIENCE_MIGRATION_NAME,
                apply_project_experience_migration,
                checksum=PROJECT_EXPERIENCE_MIGRATION_CHECKSUM,
                validate_source=True,
            ),
        ],
    ).upgrade()
    assert connection.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 7"
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    connection.close()


def test_v8_schema_and_ledger_roll_back_together_then_retry(tmp_path):
    path = tmp_path / "v8-rollback.db"
    database = Database(str(path))
    database.close()
    _revert_to_representative_v7(path)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    existing = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations "
        "WHERE version <= 7 ORDER BY version"
    ).fetchall()
    connection.execute("UPDATE schema_migrations SET content_checksum = NULL WHERE version <= 7")
    connection.commit()

    def already_applied(_connection):
        pass

    first_seven = [
        Migration(row["version"], row["name"], already_applied, checksum=row["checksum"])
        for row in existing
    ]

    def fail_after_v8_ddl(conn):
        apply_dataset_governance_migration(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'dataset_assets'"
        ).fetchone()[0] == 1
        raise RuntimeError("injected v8 migration failure")

    with pytest.raises(RuntimeError, match="injected v8 migration failure"):
        MigrationRunner(
            connection,
            path,
            [
                *first_seven,
                Migration(8, DATASET_GOVERNANCE_MIGRATION_NAME, fail_after_v8_ddl),
            ],
        ).upgrade()

    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'dataset_assets'"
    ).fetchone() is None
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 7

    MigrationRunner(
        connection,
        path,
        [
            *first_seven,
            Migration(
                8,
                DATASET_GOVERNANCE_MIGRATION_NAME,
                apply_dataset_governance_migration,
                checksum=DATASET_GOVERNANCE_MIGRATION_CHECKSUM,
                validate_source=True,
            ),
        ],
    ).upgrade()
    assert connection.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 8"
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    connection.close()


def test_v9_schema_and_ledger_roll_back_together_then_retry(tmp_path):
    path = tmp_path / "v9-rollback.db"
    database = Database(str(path))
    database.close()
    _revert_to_representative_v8(path)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    legacy_job_guard = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'jobs_execution_pin_insert_guard'"
    ).fetchone()["sql"]
    assert "execution-plan-v2-approval-v1" not in legacy_job_guard
    existing = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations "
        "WHERE version <= 8 ORDER BY version"
    ).fetchall()
    connection.execute(
        "UPDATE schema_migrations SET content_checksum = NULL WHERE version <= 8"
    )
    connection.commit()

    def already_applied(_connection):
        pass

    first_eight = [
        Migration(row["version"], row["name"], already_applied, checksum=row["checksum"])
        for row in existing
    ]

    def fail_after_v9_ddl(conn):
        apply_execution_plan_v2_migration(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'execution_plan_v2_specs'"
        ).fetchone()[0] == 1
        raise RuntimeError("injected v9 migration failure")

    with pytest.raises(RuntimeError, match="injected v9 migration failure"):
        MigrationRunner(
            connection,
            path,
            [
                *first_eight,
                Migration(9, EXECUTION_PLAN_V2_MIGRATION_NAME, fail_after_v9_ddl),
            ],
        ).upgrade()

    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'execution_plan_v2_specs'"
    ).fetchone() is None
    rolled_back_job_guard = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'jobs_execution_pin_insert_guard'"
    ).fetchone()["sql"]
    assert " ".join(rolled_back_job_guard.split()) == " ".join(
        legacy_job_guard.split()
    )
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 8
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 8

    MigrationRunner(
        connection,
        path,
        [
            *first_eight,
            Migration(
                9,
                EXECUTION_PLAN_V2_MIGRATION_NAME,
                apply_execution_plan_v2_migration,
                checksum=EXECUTION_PLAN_V2_MIGRATION_CHECKSUM,
                validate_source=True,
            ),
        ],
    ).upgrade()
    assert connection.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 9"
    ).fetchone()[0] == 1
    upgraded_job_guard = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'jobs_execution_pin_insert_guard'"
    ).fetchone()["sql"]
    assert "execution-plan-v2-approval-v1" in upgraded_job_guard
    assert "NEW.execution_contract_version = 'execution-plan-v2'" in (
        " ".join(upgraded_job_guard.split())
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    connection.close()


def test_failed_migration_rolls_back_step_and_ledger(tmp_path):
    path = tmp_path / "failed.db"
    connection = sqlite3.connect(path)

    def fail_after_ddl(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE transient_migration_row (id INTEGER)")
        raise RuntimeError("injected migration failure")

    runner = MigrationRunner(
        connection,
        path,
        [Migration(1, "injected_failure", fail_after_ddl)],
    )
    with pytest.raises(RuntimeError, match="injected migration failure"):
        runner.upgrade()
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'transient_migration_row'"
        ).fetchone()
        is None
    )
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    connection.close()


def test_backup_and_restore_verify_are_offline_and_consistent(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup" / "copy.db"
    database = Database(str(source))
    database.close()

    backup_database(source, backup)
    verified = restore_verify_database(backup)
    assert verified == {
        "integrity": "ok",
        "schema_version": 10,
        "audit_hash_chain": "ok",
    }


def test_dispatch_db_cli_upgrade_current_check_backup_and_restore_verify(tmp_path, capsys):
    path = tmp_path / "cli.db"
    backup = tmp_path / "cli-backup.db"

    assert cli.main(["db", "upgrade", "--db", str(path)]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert upgraded["schema_version"] == 10

    assert cli.main(["db", "current", "--db", str(path)]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["schema_version"] == 10
    assert current["migrations"][0]["name"] == "legacy_schema_compatibility"

    assert cli.main(["db", "check", "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "backup", "--db", str(path), "--output", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "restore-verify", "--db", str(backup)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["schema_version"] == 10
    assert restored["audit_hash_chain"] == "ok"


def test_dispatch_db_check_rejects_missing_database(tmp_path, capsys):
    missing = tmp_path / "missing.db"
    assert cli.main(["db", "check", "--db", str(missing)]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_unknown_migration_metadata_is_rejected(tmp_path):
    path = tmp_path / "unknown.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES (99, 'future', 'sha', 'now')")
    connection.commit()
    runner = MigrationRunner(connection, path, [Migration(1, "one", lambda _conn: None)])
    with pytest.raises(MigrationError, match="unknown migration version"):
        runner.status()
    connection.close()


def test_migration_content_checksum_drift_is_rejected(tmp_path):
    path = tmp_path / "checksum.db"
    connection = sqlite3.connect(path)

    def apply_original(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE checksum_probe (id INTEGER)")

    original = Migration(1, "checksum_probe", apply_original)
    MigrationRunner(connection, path, [original]).upgrade()

    def apply_changed(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE checksum_probe (id INTEGER, value TEXT)")

    changed = Migration(
        1,
        "checksum_probe",
        apply_changed,
        checksum=original.content_checksum(),
    )
    with pytest.raises(MigrationError, match="content checksum"):
        MigrationRunner(connection, path, [changed]).status()
    connection.close()


def test_explicit_reviewed_artifact_checksum_is_persisted(tmp_path):
    path = tmp_path / "reviewed-checksum.db"
    connection = sqlite3.connect(path)

    def apply_reviewed(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE reviewed_checksum (id INTEGER)")

    runner = MigrationRunner(
        connection,
        path,
        [Migration(1, "reviewed", apply_reviewed, checksum="reviewed-sha256")],
    )
    runner.upgrade()
    record = connection.execute(
        "SELECT checksum, content_checksum FROM schema_migrations"
    ).fetchone()
    assert tuple(record) == (
        "reviewed-sha256",
        Migration(1, "reviewed", apply_reviewed).content_checksum(),
    )
    connection.close()


def test_migration_ledger_version_gap_is_rejected(tmp_path):
    path = tmp_path / "gap.db"
    connection = sqlite3.connect(path)

    def apply_one(_conn: sqlite3.Connection) -> None:
        pass

    def apply_two(_conn: sqlite3.Connection) -> None:
        pass

    def apply_three(_conn: sqlite3.Connection) -> None:
        pass

    migrations = [
        Migration(1, "one", apply_one),
        Migration(2, "two", apply_two),
        Migration(3, "three", apply_three),
    ]
    connection.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    for migration in (migrations[0], migrations[2]):
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, migration.resolved_checksum(), "now"),
        )
    connection.commit()
    with pytest.raises(MigrationError, match="version gap"):
        MigrationRunner(connection, path, migrations).status()
    connection.close()
