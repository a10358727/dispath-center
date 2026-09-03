"""Versioned SQLite migration runner and control-plane CLI tests."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from app.db import (
    AGENT_SESSION_ACTIVE_TURN_MIGRATION_CHECKSUM,
    AGENT_SESSION_ACTIVE_TURN_MIGRATION_NAME,
    API_IDEMPOTENCY_MIGRATION_CHECKSUM,
    API_IDEMPOTENCY_MIGRATION_NAME,
    AGENT_RUNTIME_V3_MIGRATION_CHECKSUM,
    AGENT_SESSION_OPTIONS_MIGRATION_CHECKSUM,
    AGENT_SESSION_OPTIONS_MIGRATION_NAME,
    apply_agent_session_options_migration,
    AGENT_RUNTIME_V3_MIGRATION_NAME,
    ASSISTANT_TURN_TOKENS_MIGRATION_CHECKSUM,
    ASSISTANT_TURN_TOKENS_MIGRATION_NAME,
    ASSISTANT_USAGE_MIGRATION_CHECKSUM,
    ASSISTANT_USAGE_MIGRATION_NAME,
    DATASET_GOVERNANCE_MIGRATION_CHECKSUM,
    DATASET_GOVERNANCE_MIGRATION_NAME,
    Database,
    EXECUTION_PLAN_V2_MIGRATION_CHECKSUM,
    EXECUTION_PLAN_V2_MIGRATION_NAME,
    EXPERIMENT_PLAN_SPECS_MIGRATION_CHECKSUM,
    EXPERIMENT_PLAN_SPECS_MIGRATION_NAME,
    EXPERIMENT_V2_MIGRATION_CHECKSUM,
    EXPERIMENT_V2_MIGRATION_NAME,
    PROJECT_EXPERIENCE_MIGRATION_CHECKSUM,
    PROJECT_EXPERIENCE_MIGRATION_NAME,
    PROJECT_INSTANCE_DIVERGED_TRIGGER_MIGRATION_CHECKSUM,
    PROJECT_INSTANCE_DIVERGED_TRIGGER_MIGRATION_NAME,
    PROJECT_ROLE_BINDINGS_MIGRATION_CHECKSUM,
    PROJECT_ROLE_BINDINGS_MIGRATION_NAME,
    RUN_METRICS_V1_MIGRATION_CHECKSUM,
    RUN_METRICS_V1_MIGRATION_NAME,
    apply_agent_runtime_v3_migration,
    apply_agent_session_active_turn_migration,
    apply_api_idempotency_migration,
    apply_assistant_turn_tokens_migration,
    apply_assistant_usage_migration,
    apply_dataset_governance_migration,
    apply_execution_plan_v2_migration,
    apply_experiment_plan_specs_migration,
    apply_experiment_v2_migration,
    apply_project_experience_migration,
    apply_project_instance_diverged_trigger_migration,
    apply_project_role_bindings_migration,
    apply_run_metrics_v1_migration,
)
from app.execution_contract import canonical_json, utf8_sha256
from app.metrics_v1 import MetricsEntry
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationError,
    MigrationRunner,
    backup_database,
    restore_verify_database,
)
from dispatch_center import cli

# Single source of truth for the migration name ledger. Every schema bump
# updates this list once instead of two duplicated inline copies; the
# `CURRENT_SCHEMA_VERSION == EXPECTED_MIGRATIONS[-1][0]` assertion below is
# the one intentional bookkeeping pin left in this module.
EXPECTED_MIGRATIONS = [
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
    (11, "agent_sessions"),
    (12, "agent_sessions_active_turn"),
    (13, "run_metrics_v1"),
    (14, "experiments_v2"),
    (15, "experiment_plan_specs"),
    (16, "assistant_usage"),
    (17, "project_instance_diverged_trigger"),
    (18, "assistant_turn_tokens"),
    (19, "agent_runtime_v3"),
    (20, "agent_session_options"),
    (21, "server_observation_device_columns"),
    (22, "hardware_images"),
    (23, "hardware_action_v2_triggers"),
    (24, "hardware_receipts"),
]
assert CURRENT_SCHEMA_VERSION == EXPECTED_MIGRATIONS[-1][0]


def test_database_records_version_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "control.db"
    first = Database(str(path))
    assert first.schema_version() == CURRENT_SCHEMA_VERSION
    first_records = first._conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(row[0], row[1]) for row in first_records] == EXPECTED_MIGRATIONS
    assert first._conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    first.close()

    second = Database(str(path))
    assert second.schema_version() == CURRENT_SCHEMA_VERSION
    second_records = second._conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(row[0], row[1]) for row in second_records] == EXPECTED_MIGRATIONS
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
            # migration 24 (DG-HARDWARE-EXECUTION v1 H-6): appended by ALTER TABLE
            "physical_tools_json",
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
            # migration 22 (DG-HARDWARE-EXECUTION v1 H-2): appended by ALTER TABLE
            "action_class",
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
    #: migration 23 (DG-HARDWARE-EXECUTION v1 P3) admits `hardware_action_v2`
    assert "kind NOT IN ('execution_plan_v2', 'hardware_action_v2')" in normalized_job_guard
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


def _drop_agent_session_schema(connection: sqlite3.Connection) -> None:
    """DG-AGENT-SESSION-V1 (migration 11): purely additive new table (plus
    its two indexes, dropped automatically with the table) — no
    trigger/legacy-shape to restore. `agent_sessions` references
    `ai_conversations`, so callers must drop this before
    `_drop_ai_conversation_schema()`. Dropping the whole table also removes
    migration 12's `active_turn_no`/`active_turn_started_at` columns for
    free, so every caller reverting to a pre-v11 snapshot needs nothing
    extra for v12 (see `_drop_agent_session_active_turn_columns()` for the
    one case — reverting to a *v11* snapshot — where the table must survive
    but those two columns must not)."""
    connection.execute("DROP TABLE agent_sessions")


def _drop_agent_session_active_turn_columns(connection: sqlite3.Connection) -> None:
    """DG-AGENT-SESSION-V1 P2 (migration 12): purely additive columns on the
    already-existing `agent_sessions` table (SQLite >= 3.35 `DROP COLUMN`,
    same version this project already requires)."""
    connection.execute("ALTER TABLE agent_sessions DROP COLUMN active_turn_no")
    connection.execute("ALTER TABLE agent_sessions DROP COLUMN active_turn_started_at")


def _drop_run_metrics_v1_schema(connection: sqlite3.Connection) -> None:
    """DG-METRICS-CONTRACT v1 (migration 13): purely additive new tables
    (plus their index, dropped automatically with the table) — no
    trigger/legacy-shape to restore, and no other table has a foreign key
    into either of these two, so drop order does not matter."""
    connection.execute("DROP TABLE run_metrics")
    connection.execute("DROP TABLE run_metrics_collection")


def _drop_experiment_v2_schema(connection: sqlite3.Connection) -> None:
    """DG-EXPERIMENT-V1 P1 (migration 14): purely additive new tables (plus
    the membership table's index, dropped automatically with the table) --
    no trigger/legacy-shape to restore. `experiment_plan_members` holds a
    foreign key into `experiments`, so callers must drop it first."""
    connection.execute("DROP TABLE experiment_plan_members")
    connection.execute("DROP TABLE experiments")


def _drop_experiment_plan_specs_schema(connection: sqlite3.Connection) -> None:
    """DG-EXPERIMENT-V1 P2 implementation note (migration 15): purely
    additive companion table (plus its indexes and immutability/consistency
    triggers, all dropped automatically with the table). It holds foreign
    keys into `experiments` and `execution_plans`, so callers must drop it
    before `_drop_experiment_v2_schema`."""
    connection.execute("DROP TABLE experiment_plan_specs")


def _drop_agent_runtime_v3_schema(connection: sqlite3.Connection) -> None:
    """DG-AGENT-RUNTIME-V3 (migration 19): revert the four additive tables."""
    for statement in (
        "DROP INDEX idx_agent_permission_requests_session_status",
        "DROP TABLE agent_permission_requests",
        "DROP INDEX idx_agent_session_events_session_id",
        "DROP TABLE agent_session_events",
        "DROP TABLE agent_session_runtime",
        "DROP INDEX idx_agent_runners_one_active_per_server",
        "DROP TABLE agent_runners",
    ):
        connection.execute(statement)


def _drop_assistant_turn_tokens_schema(connection: sqlite3.Connection) -> None:
    """Packet P1a (migration 18): revert only the new `assistant_turn_tokens`
    table and its index so representative snapshots stop just before v18."""
    connection.execute("DROP INDEX idx_assistant_turn_tokens_expires_at")
    connection.execute("DROP TABLE assistant_turn_tokens")


def _drop_assistant_usage_schema(connection: sqlite3.Connection) -> None:
    """Packet D3 (migration 16): purely additive, no-FK accounting table
    (plus its index, dropped automatically with the table) -- no other
    schema object references it, so it can be dropped independently of the
    other companion tables' drop order."""
    connection.execute("DROP TABLE assistant_usage")


def _revert_to_representative_v16(path) -> None:
    """(migration 17): revert only the trigger widening -- migration 17 does
    not add a table/column, it only redefines two existing BEFORE INSERT
    consistency triggers to accept `instance.state IN ('available',
    'diverged')` -- so there is nothing to drop for the "just before v17"
    snapshot. Instead, restore both triggers to their pre-migration-17
    (`state = 'available'`-only) text, leaving migration 16's
    `assistant_usage` table untouched, so the snapshot's on-disk shape
    matches a real v16 database."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "DROP TRIGGER trg_execution_plan_v2_specs_insert_consistency"
    )
    connection.execute(
        """
        CREATE TRIGGER trg_execution_plan_v2_specs_insert_consistency
        BEFORE INSERT ON execution_plan_v2_specs
        WHEN
            NOT EXISTS (
                SELECT 1
                FROM execution_plans AS plan
                JOIN projects AS project
                  ON project.id = NEW.project_id
                 AND project.name = plan.project_name
                WHERE plan.id = NEW.execution_plan_id
                  AND plan.contract_version = NEW.contract_version
                  AND plan.plan_digest = NEW.plan_digest
                  AND plan.project_version_id = NEW.project_version_id
                  AND plan.run_profile_id = NEW.run_profile_id
                  AND plan.server_config_revision_id = NEW.server_config_revision_id
                  AND plan.request_approval_id = NEW.created_approval_id
                  AND plan.command_sha256 = NEW.job_command_sha256
                  AND (
                      (json_array_length(NEW.dataset_bindings_json) = 0
                       AND plan.dataset_none = 1
                       AND plan.dataset_snapshot_id IS NULL
                       AND plan.reproducible = 1)
                      OR
                      (json_array_length(NEW.dataset_bindings_json) = 1
                       AND plan.dataset_none = 0
                       AND plan.dataset_snapshot_id = json_extract(
                           NEW.dataset_bindings_json, '$[0].snapshot_id'
                       )
                       AND plan.reproducible = 1)
                      OR
                      (json_array_length(NEW.dataset_bindings_json) > 1
                       AND plan.dataset_none = 0
                       AND plan.dataset_snapshot_id IS NULL
                       AND plan.reproducible = 0)
                  )
            )
            OR NOT EXISTS (
                SELECT 1
                FROM approvals AS approval
                WHERE approval.id = NEW.created_approval_id
                  AND approval.kind = 'execution_plan_v2'
                  AND approval.status = 'pending'
                  AND approval.requester_actor_id = NEW.created_by_actor_id
                  AND approval.payload_contract_version =
                      'execution-plan-v2-approval-v1'
                  AND approval.payload_sha256 IS NOT NULL
                  AND approval.payload_immutable_at IS NOT NULL
                  AND json_valid(approval.payload)
                  AND json_type(approval.payload) = 'object'
                  AND (SELECT COUNT(*) FROM json_each(approval.payload)) = 4
                  AND json_extract(approval.payload, '$.contract_version') =
                      'execution-plan-v2-approval-v1'
                  AND json_extract(approval.payload, '$.execution_plan_id') =
                      NEW.execution_plan_id
                  AND json_extract(approval.payload, '$.project_id') = NEW.project_id
                  AND json_extract(approval.payload, '$.plan_digest') = NEW.plan_digest
            )
            OR json_extract(NEW.canonical_spec_json, '$.contract_version')
                <> NEW.contract_version
            OR json_extract(NEW.canonical_spec_json, '$.project_id') <> NEW.project_id
            OR json_extract(NEW.canonical_spec_json, '$.plan_digest') <> NEW.plan_digest
            OR json_extract(
                NEW.canonical_spec_json, '$.project_version.project_version_id'
            ) <> NEW.project_version_id
            OR json_extract(
                NEW.canonical_spec_json, '$.run_profile.run_profile_id'
            ) <> NEW.run_profile_id
            OR json_extract(
                NEW.canonical_spec_json, '$.environment.environment_revision_id'
            ) <> NEW.environment_revision_id
            OR json_extract(
                NEW.canonical_spec_json, '$.target.server_config_revision_id'
            ) <> NEW.server_config_revision_id
            OR json_extract(
                NEW.canonical_spec_json, '$.project_instance.project_instance_id'
            ) <> NEW.project_instance_id
            OR json_extract(
                NEW.canonical_spec_json, '$.job_command_sha256'
            ) <> NEW.job_command_sha256
            OR NOT EXISTS (
                SELECT 1 FROM project_versions AS version
                WHERE version.id = NEW.project_version_id
                  AND version.project_id = NEW.project_id
                  AND version.promotion_state = 'promoted'
                  AND version.promotion_approval_id IS NOT NULL
                  AND version.bundle_sha256 IS NOT NULL
            )
            OR NOT EXISTS (
                SELECT 1
                FROM run_profiles AS profile
                JOIN run_profile_specs AS spec ON spec.run_profile_id = profile.id
                WHERE profile.id = NEW.run_profile_id
                  AND profile.project_id = NEW.project_id
                  AND profile.status = 'approved'
                  AND spec.project_id = NEW.project_id
                  AND spec.spec_digest = NEW.run_profile_spec_digest
                  AND spec.environment_revision_id = NEW.environment_revision_id
                  AND NOT EXISTS (
                      SELECT 1 FROM run_profiles AS newer
                      WHERE newer.project_id = profile.project_id
                        AND newer.name = profile.name
                        AND newer.revision > profile.revision
                  )
            )
            OR NOT EXISTS (
                SELECT 1 FROM environment_revisions AS environment
                WHERE environment.id = NEW.environment_revision_id
                  AND environment.project_id = NEW.project_id
                  AND environment.status = 'approved'
                  AND environment.revision_digest = NEW.environment_revision_digest
                  AND NOT EXISTS (
                      SELECT 1 FROM environment_revisions AS newer
                      WHERE newer.environment_id = environment.environment_id
                        AND newer.revision > environment.revision
                  )
            )
            OR (
                NEW.project_defaults_revision_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM project_default_revisions AS defaults
                    WHERE defaults.id = NEW.project_defaults_revision_id
                      AND defaults.project_id = NEW.project_id
                      AND defaults.revision_digest =
                          NEW.project_defaults_revision_digest
                      AND defaults.run_profile_id = NEW.run_profile_id
                      AND defaults.run_profile_spec_digest =
                          NEW.run_profile_spec_digest
                      AND defaults.environment_revision_id =
                          NEW.environment_revision_id
                      AND NOT EXISTS (
                          SELECT 1 FROM project_default_revisions AS newer
                          WHERE newer.project_id = defaults.project_id
                            AND newer.revision > defaults.revision
                      )
                )
            )
            OR NOT EXISTS (
                SELECT 1
                FROM server_config_revisions AS revision
                JOIN approvals AS creator
                  ON creator.id = revision.created_by_approval_id
                WHERE revision.id = NEW.server_config_revision_id
                  AND revision.assignment_eligibility = 'approved'
                  AND revision.publication_state = 'active'
                  AND revision.target_identity_sha256 =
                      NEW.target_identity_sha256
                  AND creator.status = 'approved'
                  AND creator.payload_contract_version = 'server-config-v1'
            )
            OR NOT EXISTS (
                SELECT 1
                FROM project_instances AS instance
                JOIN projects AS project
                  ON project.id = NEW.project_id
                 AND project.name = instance.project_name
                JOIN project_versions AS version
                  ON version.id = NEW.project_version_id
                JOIN server_config_revisions AS revision
                  ON revision.id = NEW.server_config_revision_id
                 AND revision.server_name = instance.server
                WHERE instance.id = NEW.project_instance_id
                  AND instance.project_id = NEW.project_id
                  AND instance.state = 'available'
                  AND instance.dirty = 0
                  AND instance.git_commit = version.git_commit
            )
            OR (
                NEW.dispatch_policy_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM dispatch_policies AS policy
                    WHERE policy.id = NEW.dispatch_policy_id
                      AND policy.project_id = NEW.project_id
                      AND policy.status = 'approved'
                      AND NOT EXISTS (
                          SELECT 1 FROM dispatch_policies AS newer
                          WHERE newer.project_id = policy.project_id
                            AND newer.name = policy.name
                            AND newer.revision > policy.revision
                      )
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'execution_plan_v2_specs consistency violation');
        END
        """
    )
    connection.execute(
        "DROP TRIGGER trg_experiment_plan_specs_insert_consistency"
    )
    connection.execute(
        """
        CREATE TRIGGER trg_experiment_plan_specs_insert_consistency
        BEFORE INSERT ON experiment_plan_specs
        WHEN
            NOT EXISTS (
                SELECT 1
                FROM execution_plans AS plan
                JOIN projects AS project
                  ON project.id = NEW.project_id
                 AND project.name = plan.project_name
                WHERE plan.id = NEW.execution_plan_id
                  AND plan.contract_version = NEW.contract_version
                  AND plan.plan_digest = NEW.plan_digest
                  AND plan.project_version_id = NEW.project_version_id
                  AND plan.run_profile_id = NEW.run_profile_id
                  AND plan.server_config_revision_id = NEW.server_config_revision_id
                  AND plan.request_approval_id = NEW.created_approval_id
                  AND plan.command_sha256 = NEW.job_command_sha256
                  AND (
                      (json_array_length(NEW.dataset_bindings_json) = 0
                       AND plan.dataset_none = 1
                       AND plan.dataset_snapshot_id IS NULL
                       AND plan.reproducible = 1)
                      OR
                      (json_array_length(NEW.dataset_bindings_json) = 1
                       AND plan.dataset_none = 0
                       AND plan.dataset_snapshot_id = json_extract(
                           NEW.dataset_bindings_json, '$[0].snapshot_id'
                       )
                       AND plan.reproducible = 1)
                      OR
                      (json_array_length(NEW.dataset_bindings_json) > 1
                       AND plan.dataset_none = 0
                       AND plan.dataset_snapshot_id IS NULL
                       AND plan.reproducible = 0)
                  )
            )
            OR NOT EXISTS (
                SELECT 1
                FROM experiments AS experiment
                WHERE experiment.id = NEW.experiment_id
                  AND experiment.project_id = NEW.project_id
                  AND experiment.approval_id = NEW.created_approval_id
            )
            OR NOT EXISTS (
                SELECT 1
                FROM experiment_plan_members AS member
                WHERE member.experiment_id = NEW.experiment_id
                  AND member.plan_id = NEW.execution_plan_id
            )
            OR NOT EXISTS (
                SELECT 1 FROM approvals AS approval
                WHERE approval.id = NEW.created_approval_id
                  AND approval.kind = 'experiment_create_v2'
                  AND approval.status = 'pending'
                  AND approval.requester_actor_id = NEW.created_by_actor_id
                  AND approval.payload_contract_version =
                      'experiment-v2-approval-v1'
                  AND approval.payload_sha256 IS NOT NULL
                  AND approval.payload_immutable_at IS NOT NULL
                  AND json_valid(approval.payload)
                  AND json_type(approval.payload) = 'object'
                  AND json_extract(approval.payload, '$.contract_version') =
                      'experiment-v2-approval-v1'
                  AND json_extract(approval.payload, '$.project_id') = NEW.project_id
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(approval.payload, '$.plan_digests') AS digest
                      WHERE digest.value = NEW.plan_digest
                  )
            )
            OR json_extract(NEW.canonical_spec_json, '$.contract_version')
                <> NEW.contract_version
            OR json_extract(NEW.canonical_spec_json, '$.project_id') <> NEW.project_id
            OR json_extract(NEW.canonical_spec_json, '$.plan_digest') <> NEW.plan_digest
            OR json_extract(
                NEW.canonical_spec_json, '$.project_version.project_version_id'
            ) <> NEW.project_version_id
            OR json_extract(
                NEW.canonical_spec_json, '$.run_profile.run_profile_id'
            ) <> NEW.run_profile_id
            OR json_extract(
                NEW.canonical_spec_json, '$.environment.environment_revision_id'
            ) <> NEW.environment_revision_id
            OR json_extract(
                NEW.canonical_spec_json, '$.target.server_config_revision_id'
            ) <> NEW.server_config_revision_id
            OR json_extract(
                NEW.canonical_spec_json, '$.project_instance.project_instance_id'
            ) <> NEW.project_instance_id
            OR json_extract(
                NEW.canonical_spec_json, '$.job_command_sha256'
            ) <> NEW.job_command_sha256
            OR NOT EXISTS (
                SELECT 1 FROM project_versions AS version
                WHERE version.id = NEW.project_version_id
                  AND version.project_id = NEW.project_id
                  AND version.promotion_state = 'promoted'
                  AND version.promotion_approval_id IS NOT NULL
                  AND version.bundle_sha256 IS NOT NULL
            )
            OR NOT EXISTS (
                SELECT 1
                FROM run_profiles AS profile
                JOIN run_profile_specs AS spec ON spec.run_profile_id = profile.id
                WHERE profile.id = NEW.run_profile_id
                  AND profile.project_id = NEW.project_id
                  AND profile.status = 'approved'
                  AND spec.project_id = NEW.project_id
                  AND spec.spec_digest = NEW.run_profile_spec_digest
                  AND spec.environment_revision_id = NEW.environment_revision_id
                  AND NOT EXISTS (
                      SELECT 1 FROM run_profiles AS newer
                      WHERE newer.project_id = profile.project_id
                        AND newer.name = profile.name
                        AND newer.revision > profile.revision
                  )
            )
            OR NOT EXISTS (
                SELECT 1 FROM environment_revisions AS environment
                WHERE environment.id = NEW.environment_revision_id
                  AND environment.project_id = NEW.project_id
                  AND environment.status = 'approved'
                  AND environment.revision_digest = NEW.environment_revision_digest
                  AND NOT EXISTS (
                      SELECT 1 FROM environment_revisions AS newer
                      WHERE newer.environment_id = environment.environment_id
                        AND newer.revision > environment.revision
                  )
            )
            OR (
                NEW.project_defaults_revision_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM project_default_revisions AS defaults
                    WHERE defaults.id = NEW.project_defaults_revision_id
                      AND defaults.project_id = NEW.project_id
                      AND defaults.revision_digest =
                          NEW.project_defaults_revision_digest
                      AND defaults.run_profile_id = NEW.run_profile_id
                      AND defaults.run_profile_spec_digest =
                          NEW.run_profile_spec_digest
                      AND defaults.environment_revision_id =
                          NEW.environment_revision_id
                      AND NOT EXISTS (
                          SELECT 1 FROM project_default_revisions AS newer
                          WHERE newer.project_id = defaults.project_id
                            AND newer.revision > defaults.revision
                      )
                )
            )
            OR NOT EXISTS (
                SELECT 1
                FROM server_config_revisions AS revision
                JOIN approvals AS creator
                  ON creator.id = revision.created_by_approval_id
                WHERE revision.id = NEW.server_config_revision_id
                  AND revision.assignment_eligibility = 'approved'
                  AND revision.publication_state = 'active'
                  AND revision.target_identity_sha256 =
                      NEW.target_identity_sha256
                  AND creator.status = 'approved'
                  AND creator.payload_contract_version = 'server-config-v1'
            )
            OR NOT EXISTS (
                SELECT 1
                FROM project_instances AS instance
                JOIN projects AS project
                  ON project.id = NEW.project_id
                 AND project.name = instance.project_name
                JOIN project_versions AS version
                  ON version.id = NEW.project_version_id
                JOIN server_config_revisions AS revision
                  ON revision.id = NEW.server_config_revision_id
                 AND revision.server_name = instance.server
                WHERE instance.id = NEW.project_instance_id
                  AND instance.project_id = NEW.project_id
                  AND instance.state = 'available'
                  AND instance.dirty = 0
                  AND instance.git_commit = version.git_commit
            )
            OR (
                NEW.dispatch_policy_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM dispatch_policies AS policy
                    WHERE policy.id = NEW.dispatch_policy_id
                      AND policy.project_id = NEW.project_id
                      AND policy.status = 'approved'
                      AND NOT EXISTS (
                          SELECT 1 FROM dispatch_policies AS newer
                          WHERE newer.project_id = policy.project_id
                            AND newer.name = policy.name
                            AND newer.revision > policy.revision
                      )
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'experiment_plan_specs consistency violation');
        END
        """
    )
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version >= 17")
    connection.execute("PRAGMA user_version = 16")
    connection.commit()
    connection.close()


def _revert_to_representative_v15(path) -> None:
    """Packet D3 (migration 16): revert only the new `assistant_usage` table
    (and its ledger row), leaving migration 15's `experiment_plan_specs`
    table in place -- the representative "just before v16" snapshot."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version >= 16")
    connection.execute("PRAGMA user_version = 15")
    connection.commit()
    connection.close()


def _revert_to_representative_v14(path) -> None:
    """DG-EXPERIMENT-V1 P2 implementation note (migration 15): revert only
    the new companion table (and its ledger row), leaving migration 14's
    `experiments` / `experiment_plan_members` tables in place -- the
    representative "just before v15" snapshot."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version >= 15")
    connection.execute("PRAGMA user_version = 14")
    connection.commit()
    connection.close()


def _revert_to_representative_v13(path) -> None:
    """DG-EXPERIMENT-V1 P1 (migration 14): revert only the new tables (and
    their ledger row), leaving migration 13's `run_metrics` /
    `run_metrics_collection` tables in place -- the representative "just
    before v14" snapshot."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version >= 14")
    connection.execute("PRAGMA user_version = 13")
    connection.commit()
    connection.close()


def _revert_to_representative_v12(path) -> None:
    """DG-METRICS-CONTRACT v1 (migration 13): revert only the new tables
    (and their ledger row), leaving migration 12's `agent_sessions` columns
    in place -- the representative "just before v13" snapshot."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version >= 13")
    connection.execute("PRAGMA user_version = 12")
    connection.commit()
    connection.close()


def _revert_to_representative_v11(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    _drop_agent_session_active_turn_columns(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version >= 12")
    connection.execute("PRAGMA user_version = 11")
    connection.commit()
    connection.close()


def _revert_to_representative_v5(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    _drop_agent_session_schema(connection)
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    _drop_project_experience_schema(connection)
    connection.execute("DROP TABLE project_role_bindings")
    connection.execute(
        "DELETE FROM schema_migrations WHERE version >= 6"
    )
    connection.execute("PRAGMA user_version = 5")
    connection.commit()
    connection.close()


def _revert_to_representative_v6(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    _drop_agent_session_schema(connection)
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    _drop_project_experience_schema(connection)
    connection.execute(
        "DELETE FROM schema_migrations WHERE version >= 7"
    )
    connection.execute("PRAGMA user_version = 6")
    connection.commit()
    connection.close()


def _revert_to_representative_v7(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    _drop_agent_session_schema(connection)
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    connection.execute(
        "DELETE FROM schema_migrations WHERE version >= 8"
    )
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()


def _revert_to_representative_v8(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    _drop_agent_session_schema(connection)
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    connection.execute(
        "DELETE FROM schema_migrations WHERE version >= 9"
    )
    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    connection.close()


def test_representative_v11_upgrade_installs_active_turn_tracking_without_backfill(
    tmp_path,
):
    """DG-AGENT-SESSION-V1 P2, migration 12 dual-track test: a v11 database
    with an existing `agent_sessions` row upgrades to v12 with both new
    columns `NULL`-backfilled (no turn was ever in flight before this
    column existed) and the row otherwise untouched."""

    path = tmp_path / "agent-session-active-turn-v11-upgrade.db"
    database = Database(str(path))
    project_id = database.insert_project("legacy-project", "/tmp/legacy-project")
    conversation = database.get_or_create_project_conversation("legacy-project")
    approval_id = database.insert_approval(
        kind="agent_session_open", payload={}, requester_actor_id=None
    )
    session = database.apply_agent_session_open_decision(
        approval_id=approval_id,
        project_id=project_id,
        conversation_id=conversation.id,
        provider_id="claude-code",
        workspace_branch="ai-session-11111111-2222-3333-4444-555555555555",
        base_version_id=None,
        max_turns=200,
        turn_timeout_sec=600,
    )
    database.close()
    _revert_to_representative_v11(path)

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")}
    assert "active_turn_no" not in columns
    assert "active_turn_started_at" not in columns
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    upgraded_columns = {
        row[1] for row in upgraded._conn.execute("PRAGMA table_info(agent_sessions)")
    }
    assert "active_turn_no" in upgraded_columns
    assert "active_turn_started_at" in upgraded_columns
    restored = upgraded.get_agent_session(session.id)
    assert restored.id == session.id
    assert restored.status == "active"
    assert restored.active_turn_no is None
    assert restored.active_turn_started_at is None
    record = upgraded._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 12"
    ).fetchone()
    source_checksum = Migration(
        12,
        AGENT_SESSION_ACTIVE_TURN_MIGRATION_NAME,
        apply_agent_session_active_turn_migration,
    ).content_checksum()
    assert tuple(record) == (
        AGENT_SESSION_ACTIVE_TURN_MIGRATION_NAME,
        AGENT_SESSION_ACTIVE_TURN_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == AGENT_SESSION_ACTIVE_TURN_MIGRATION_CHECKSUM
    upgraded.close()


def test_run_metrics_v1_schema_is_exact_and_source_pinned(tmp_path):
    """DG-METRICS-CONTRACT v1, migration 13 fresh-database track: schema
    shape, FK/UNIQUE constraints, and the checked-in source-pinned checksum
    (`docs/DG_METRICS_CONTRACT_DECISION.md`, `docs/DECISIONS.md`
    2026-08-24: A 核准)."""

    database = Database(str(tmp_path / "run-metrics-schema.db"))
    metrics_columns = database._conn.execute(
        "PRAGMA table_info(run_metrics)"
    ).fetchall()
    metrics_fks = database._conn.execute(
        "PRAGMA foreign_key_list(run_metrics)"
    ).fetchall()
    metrics_indexes = database._conn.execute(
        "PRAGMA index_list(run_metrics)"
    ).fetchall()
    collection_columns = database._conn.execute(
        "PRAGMA table_info(run_metrics_collection)"
    ).fetchall()
    collection_fks = database._conn.execute(
        "PRAGMA foreign_key_list(run_metrics_collection)"
    ).fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 13"
    ).fetchone()

    assert [row["name"] for row in metrics_columns] == [
        "job_id",
        "key",
        "value_type",
        "value_text",
        "recorded_at",
    ]
    assert all(row["notnull"] == 1 for row in metrics_columns)
    assert [(row["from"], row["table"], row["to"], row["on_delete"]) for row in metrics_fks] == [
        ("job_id", "jobs", "id", "RESTRICT")
    ]
    assert any(
        row["unique"] == 1 and row["origin"] == "u" for row in metrics_indexes
    ) or any(  # SQLite may implement UNIQUE(job_id, key) via an autoindex.
        row["name"].startswith("sqlite_autoindex_run_metrics") for row in metrics_indexes
    )
    assert [row["name"] for row in collection_columns] == [
        "job_id",
        "status",
        "reason",
        "source_sha256",
        "collected_at",
    ]
    assert [
        bool(row["notnull"]) for row in collection_columns
    ] == [True, True, False, False, True]
    assert [
        (row["from"], row["table"], row["to"], row["on_delete"]) for row in collection_fks
    ] == [("job_id", "jobs", "id", "RESTRICT")]

    source_checksum = Migration(
        13, RUN_METRICS_V1_MIGRATION_NAME, apply_run_metrics_v1_migration
    ).content_checksum()
    assert tuple(record) == (
        RUN_METRICS_V1_MIGRATION_NAME,
        RUN_METRICS_V1_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == RUN_METRICS_V1_MIGRATION_CHECKSUM
    database.close()


def test_experiment_v2_schema_is_exact_and_source_pinned(tmp_path):
    """DG-EXPERIMENT-V1 P1, migration 14 fresh-database track: schema shape,
    FK/UNIQUE constraints, and the checked-in source-pinned checksum
    (`docs/DG_EXPERIMENT_V1_DECISION.md`, `docs/DECISIONS.md` 2026-08-25:
    「DG-EXPERIMENT-V1：A 核准」). This packet only registers the storage
    shape and approval kind -- no store/decision code writes to these tables
    yet."""

    database = Database(str(tmp_path / "experiment-v2-schema.db"))
    experiment_columns = database._conn.execute(
        "PRAGMA table_info(experiments)"
    ).fetchall()
    experiment_fks = database._conn.execute(
        "PRAGMA foreign_key_list(experiments)"
    ).fetchall()
    member_columns = database._conn.execute(
        "PRAGMA table_info(experiment_plan_members)"
    ).fetchall()
    member_fks = database._conn.execute(
        "PRAGMA foreign_key_list(experiment_plan_members)"
    ).fetchall()
    member_indexes = database._conn.execute(
        "PRAGMA index_list(experiment_plan_members)"
    ).fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 14"
    ).fetchone()

    assert [row["name"] for row in experiment_columns] == [
        "id",
        "project_id",
        "approval_id",
        "matrix_json",
        "guard_json",
        "run_count",
        "created_at",
    ]
    assert [bool(row["notnull"]) for row in experiment_columns] == [
        False,  # id: INTEGER PRIMARY KEY AUTOINCREMENT (implicit NOT NULL rowid alias)
        True,
        True,
        True,
        True,
        True,
        True,
    ]
    assert [
        (row["from"], row["table"], row["to"], row["on_delete"]) for row in experiment_fks
    ] == [("approval_id", "approvals", "id", "RESTRICT")]

    assert [row["name"] for row in member_columns] == ["experiment_id", "plan_id"]
    assert all(row["notnull"] == 1 for row in member_columns)
    assert sorted(
        (row["from"], row["table"], row["to"], row["on_delete"]) for row in member_fks
    ) == sorted(
        [
            ("experiment_id", "experiments", "id", "RESTRICT"),
            ("plan_id", "execution_plans", "id", "RESTRICT"),
        ]
    )
    assert any(
        row["unique"] == 1 and row["origin"] == "u" for row in member_indexes
    ) or any(  # SQLite may implement UNIQUE(plan_id) via an autoindex.
        row["name"].startswith("sqlite_autoindex_experiment_plan_members")
        for row in member_indexes
    )

    source_checksum = Migration(
        14, EXPERIMENT_V2_MIGRATION_NAME, apply_experiment_v2_migration
    ).content_checksum()
    assert tuple(record) == (
        EXPERIMENT_V2_MIGRATION_NAME,
        EXPERIMENT_V2_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == EXPERIMENT_V2_MIGRATION_CHECKSUM
    database.close()


def test_representative_v12_upgrade_installs_run_metrics_v1_without_backfill(tmp_path):
    """DG-METRICS-CONTRACT v1, migration 13 upgrade-path track: a v12
    database with an existing job upgrades to v13 with the two new tables
    present and empty (purely additive, no backfill of historical
    `results/{job_id}/metrics.json` files -- explicitly deferred, see the
    decision packet's §2 "明文不做")."""

    path = tmp_path / "run-metrics-v12-upgrade.db"
    database = Database(str(path))
    job_id = database.insert_job(command="echo hi")
    database.close()
    _revert_to_representative_v12(path)

    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'run_metrics'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded.get_run_metrics_collection(job_id) is None
    assert upgraded._conn.execute("SELECT COUNT(*) FROM run_metrics").fetchone()[0] == 0
    assert (
        upgraded._conn.execute("SELECT COUNT(*) FROM run_metrics_collection").fetchone()[0]
        == 0
    )

    # The freshly-installed tables are immediately usable, not just present.
    upgraded.replace_run_metrics(
        job_id,
        (MetricsEntry(key="loss", value_type="decimal", value_text="0.5000"),),
        status="collected",
        reason=None,
        source_sha256="a" * 64,
    )
    assert upgraded.get_run_metrics_collection(job_id)["status"] == "collected"
    upgraded.close()


def test_representative_v13_upgrade_installs_experiment_v2_without_backfill(tmp_path):
    """DG-EXPERIMENT-V1 P1, migration 14 upgrade-path track: a v13 database
    upgrades to v14 with the two new tables present and empty (purely
    additive -- P1 registers the kind/storage shape only; no request/store
    path exists yet in this packet, so there is nothing to backfill)."""

    path = tmp_path / "experiment-v2-v13-upgrade.db"
    database = Database(str(path))
    database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v13(path)

    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'experiments'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded.get_project("legacy-project") is not None
    assert upgraded._conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0
    assert (
        upgraded._conn.execute(
            "SELECT COUNT(*) FROM experiment_plan_members"
        ).fetchone()[0]
        == 0
    )
    upgraded.close()


def test_experiment_plan_specs_schema_is_exact_and_source_pinned(tmp_path):
    """DG-EXPERIMENT-V1 P2 implementation note, migration 15 fresh-database
    track: schema shape, FK constraints, and the checked-in source-pinned
    checksum (`docs/DG_EXPERIMENT_V1_DECISION.md` "P2 實作註記"). This
    additive companion table exists purely so member plans can be
    revalidated at the same depth as a single-run plan; no store/decision
    code writes to it yet in this packet's schema-only test track."""

    database = Database(str(tmp_path / "experiment-plan-specs-schema.db"))
    columns = database._conn.execute(
        "PRAGMA table_info(experiment_plan_specs)"
    ).fetchall()
    foreign_keys = database._conn.execute(
        "PRAGMA foreign_key_list(experiment_plan_specs)"
    ).fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 15"
    ).fetchone()

    assert {row["name"] for row in columns} == {
        "execution_plan_id",
        "experiment_id",
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
    }
    assert {
        (row["from"], row["table"], row["to"], row["on_delete"]) for row in foreign_keys
    } == {
        ("execution_plan_id", "execution_plans", "id", "RESTRICT"),
        ("experiment_id", "experiments", "id", "RESTRICT"),
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

    # `execution_plan_id` is the table's PRIMARY KEY (one spec per plan,
    # mirroring the single-run companion's one-spec-per-plan shape) while
    # `created_approval_id` is deliberately NOT unique -- unlike
    # `execution_plan_v2_specs`, N members legitimately share one
    # `experiment_create_v2` approval.
    pk_columns = [row["name"] for row in columns if row["pk"] == 1]
    assert pk_columns == ["execution_plan_id"]

    source_checksum = Migration(
        15,
        EXPERIMENT_PLAN_SPECS_MIGRATION_NAME,
        apply_experiment_plan_specs_migration,
    ).content_checksum()
    assert tuple(record) == (
        EXPERIMENT_PLAN_SPECS_MIGRATION_NAME,
        EXPERIMENT_PLAN_SPECS_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == EXPERIMENT_PLAN_SPECS_MIGRATION_CHECKSUM
    database.close()


def test_representative_v14_upgrade_installs_experiment_plan_specs_without_backfill(
    tmp_path,
):
    """DG-EXPERIMENT-V1 P2 implementation note, migration 15 upgrade-path
    track: a v14 database upgrades to v15 with the new companion table
    present and empty (purely additive -- nothing to backfill, no
    pre-existing experiment ever had a resolved member spec before this
    table existed)."""

    path = tmp_path / "experiment-plan-specs-v14-upgrade.db"
    database = Database(str(path))
    database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v14(path)

    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'experiment_plan_specs'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded.get_project("legacy-project") is not None
    assert (
        upgraded._conn.execute(
            "SELECT COUNT(*) FROM experiment_plan_specs"
        ).fetchone()[0]
        == 0
    )
    upgraded.close()


def test_assistant_usage_schema_is_exact_and_source_pinned(tmp_path):
    """Packet D3 (usage accounting), migration 16 fresh-database track:
    schema shape and the checked-in source-pinned checksum. Purely
    additive, no-FK accounting table -- no store/decision code writes to it
    yet in this packet's schema-only test track (recording happens at each
    channel's call site, tested elsewhere)."""

    database = Database(str(tmp_path / "assistant-usage-schema.db"))
    columns = database._conn.execute("PRAGMA table_info(assistant_usage)").fetchall()
    foreign_keys = database._conn.execute(
        "PRAGMA foreign_key_list(assistant_usage)"
    ).fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 16"
    ).fetchone()

    assert [row["name"] for row in columns] == [
        "id",
        "ts",
        "channel",
        "server",
        "model",
        "input_tokens",
        "output_tokens",
        "duration_ms",
    ]
    assert foreign_keys == []
    pk_columns = [row["name"] for row in columns if row["pk"] == 1]
    assert pk_columns == ["id"]

    source_checksum = Migration(
        16,
        ASSISTANT_USAGE_MIGRATION_NAME,
        apply_assistant_usage_migration,
    ).content_checksum()
    assert tuple(record) == (
        ASSISTANT_USAGE_MIGRATION_NAME,
        ASSISTANT_USAGE_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == ASSISTANT_USAGE_MIGRATION_CHECKSUM

    # Insert-only usability check: every numeric/label column is nullable
    # except the ledger's own `ts`/`channel`.
    database._conn.execute(
        "INSERT INTO assistant_usage (ts, channel) VALUES ('2026-08-26T00:00:00Z', 'vllm')"
    )
    database._conn.commit()
    assert database._conn.execute(
        "SELECT COUNT(*) FROM assistant_usage"
    ).fetchone()[0] == 1
    database.close()


def test_representative_v15_upgrade_installs_assistant_usage_without_backfill(
    tmp_path,
):
    """Packet D3, migration 16 upgrade-path track: a v15 database upgrades
    to v16 with the new accounting table present and empty (purely additive
    -- no historical assistant turn ever had a recorded usage row before
    this table existed, so there is nothing to backfill)."""

    path = tmp_path / "assistant-usage-v15-upgrade.db"
    database = Database(str(path))
    database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v15(path)

    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'assistant_usage'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 15
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded.get_project("legacy-project") is not None
    assert (
        upgraded._conn.execute("SELECT COUNT(*) FROM assistant_usage").fetchone()[0]
        == 0
    )
    upgraded.record_assistant_usage(channel="runner_claude", server="s1")
    assert upgraded.get_assistant_usage_summary(7)["totals"]["turns"] == 1
    upgraded.close()


def test_project_instance_diverged_trigger_schema_is_exact_and_source_pinned(tmp_path):
    """Bug fix track (fdbc922 follow-up), migration 17: a clean project
    instance whose checkout is an exact promoted `ProjectVersion` commit but
    differs from the hub default-branch HEAD is a first-class ready state
    (`derive_instance_state()` returns `"diverged"`), and every
    application-layer consumer already accepts `state IN ('available',
    'diverged')`. Migration 17 widens only that one clause on the two
    BEFORE INSERT consistency triggers to match -- verify the checked-in
    source-pinned checksum, that both triggers' SQL now carries the widened
    clause, and that a fresh database and a database upgraded from v16 end
    up with byte-identical trigger SQL (no fresh-vs-upgraded schema
    drift)."""

    fresh = Database(str(tmp_path / "diverged-trigger-fresh.db"))
    record = fresh._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 17"
    ).fetchone()
    source_checksum = Migration(
        17,
        PROJECT_INSTANCE_DIVERGED_TRIGGER_MIGRATION_NAME,
        apply_project_instance_diverged_trigger_migration,
    ).content_checksum()
    assert tuple(record) == (
        PROJECT_INSTANCE_DIVERGED_TRIGGER_MIGRATION_NAME,
        PROJECT_INSTANCE_DIVERGED_TRIGGER_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == PROJECT_INSTANCE_DIVERGED_TRIGGER_MIGRATION_CHECKSUM

    trigger_names = (
        "trg_execution_plan_v2_specs_insert_consistency",
        "trg_experiment_plan_specs_insert_consistency",
    )
    fresh_sql = {
        row["name"]: row["sql"]
        for row in fresh._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name IN (?, ?)",
            trigger_names,
        ).fetchall()
    }
    assert set(fresh_sql) == set(trigger_names)
    for sql in fresh_sql.values():
        assert "instance.state IN ('available', 'diverged')" in sql
        # Every other clause -- dirty/git_commit/server revision match and
        # the immutability triggers elsewhere -- must be untouched.
        assert "instance.dirty = 0" in sql
        assert "instance.git_commit = version.git_commit" in sql
    fresh.close()

    upgraded_path = tmp_path / "diverged-trigger-upgraded.db"
    seed = Database(str(upgraded_path))
    seed.close()
    _revert_to_representative_v16(upgraded_path)
    upgraded = Database(str(upgraded_path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    upgraded_sql = {
        row["name"]: row["sql"]
        for row in upgraded._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name IN (?, ?)",
            trigger_names,
        ).fetchall()
    }
    upgraded.close()

    assert fresh_sql == upgraded_sql


def test_representative_v16_upgrade_installs_diverged_trigger_without_backfill(
    tmp_path,
):
    """Bug fix track (fdbc922 follow-up), migration 17 upgrade-path track: a
    v16 database upgrades to v17 with the widened trigger clause present and
    migration 16's `assistant_usage` table untouched (purely a trigger
    redefinition -- nothing to backfill)."""

    path = tmp_path / "diverged-trigger-v16-upgrade.db"
    database = Database(str(path))
    database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v16(path)

    connection = sqlite3.connect(path)
    pre_upgrade_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'trg_execution_plan_v2_specs_insert_consistency'"
    ).fetchone()[0]
    assert "instance.state = 'available'" in pre_upgrade_sql
    assert "IN ('available', 'diverged')" not in pre_upgrade_sql
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 16
    connection.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded.get_project("legacy-project") is not None
    assert (
        upgraded._conn.execute("SELECT COUNT(*) FROM assistant_usage").fetchone()[0]
        == 0
    )
    for trigger_name in (
        "trg_execution_plan_v2_specs_insert_consistency",
        "trg_experiment_plan_specs_insert_consistency",
    ):
        sql = upgraded._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()[0]
        assert "instance.state IN ('available', 'diverged')" in sql
    assert upgraded._conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    upgraded.close()


def test_representative_v8_upgrade_installs_execution_plan_v2_without_backfill(
    tmp_path,
):
    path = tmp_path / "execution-plan-v2-v8-upgrade.db"
    database = Database(str(path))
    database.insert_project("legacy-project", "/tmp/legacy-project")
    database.close()
    _revert_to_representative_v8(path)

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded.get_project("legacy-project") is not None
    assert upgraded._conn.execute(
        "SELECT COUNT(*) FROM execution_plan_v2_specs"
    ).fetchone()[0] == 0
    assert upgraded._conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
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
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
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
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
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
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
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
    _drop_agent_runtime_v3_schema(connection)
    _drop_assistant_turn_tokens_schema(connection)
    _drop_assistant_usage_schema(connection)
    _drop_experiment_plan_specs_schema(connection)
    _drop_experiment_v2_schema(connection)
    _drop_run_metrics_v1_schema(connection)
    _drop_agent_session_schema(connection)
    _drop_ai_conversation_schema(connection)
    _drop_execution_plan_v2_schema(connection)
    _drop_dataset_governance_schema(connection)
    _drop_project_experience_schema(connection)
    connection.execute("DROP TABLE project_role_bindings")
    connection.execute("DROP TABLE api_idempotency_keys")
    connection.execute(
        "DELETE FROM schema_migrations WHERE version >= 5"
    )
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
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
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
    assert versions == [CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION]
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
        "schema_version": CURRENT_SCHEMA_VERSION,
        "audit_hash_chain": "ok",
    }


def test_dispatch_db_cli_upgrade_current_check_backup_and_restore_verify(tmp_path, capsys):
    path = tmp_path / "cli.db"
    backup = tmp_path / "cli-backup.db"

    assert cli.main(["db", "upgrade", "--db", str(path)]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION

    assert cli.main(["db", "current", "--db", str(path)]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["schema_version"] == CURRENT_SCHEMA_VERSION
    assert current["migrations"][0]["name"] == "legacy_schema_compatibility"

    assert cli.main(["db", "check", "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "backup", "--db", str(path), "--output", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.main(["db", "restore-verify", "--db", str(backup)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["schema_version"] == CURRENT_SCHEMA_VERSION
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


def test_assistant_turn_tokens_schema_is_exact_and_source_pinned(tmp_path):
    """DG-ASSISTANT-TOOLS v1 T-2 (packet P1a), migration 18 fresh-database
    track: additive digest-only ledger, no FK, source-pinned checksum, and the
    Database helpers round-trip without ever returning a raw secret."""

    database = Database(str(tmp_path / "assistant-turn-tokens.db"))
    columns = database._conn.execute("PRAGMA table_info(assistant_turn_tokens)").fetchall()
    foreign_keys = database._conn.execute(
        "PRAGMA foreign_key_list(assistant_turn_tokens)"
    ).fetchall()
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 18"
    ).fetchone()

    assert [row["name"] for row in columns] == [
        "id",
        "secret_hash",
        "actor_id",
        "project_id",
        "turn_ref",
        "created_at",
        "expires_at",
        "last_used_at",
        "revoked_at",
    ]
    assert foreign_keys == []
    assert [row["name"] for row in columns if row["pk"] == 1] == ["id"]

    source_checksum = Migration(
        18,
        ASSISTANT_TURN_TOKENS_MIGRATION_NAME,
        apply_assistant_turn_tokens_migration,
    ).content_checksum()
    assert tuple(record) == (
        ASSISTANT_TURN_TOKENS_MIGRATION_NAME,
        ASSISTANT_TURN_TOKENS_MIGRATION_CHECKSUM,
        source_checksum,
    )
    assert source_checksum == ASSISTANT_TURN_TOKENS_MIGRATION_CHECKSUM

    token_id = "11111111-2222-4333-8444-555555555555"
    database.insert_assistant_turn_token(
        token_id=token_id,
        secret_hash="a" * 64,
        actor_id="actor-1",
        project_id=None,
        turn_ref="sess:1",
        expires_at="2026-08-30T00:02:30Z",
        now="2026-08-30T00:00:00Z",
    )
    row = database.get_assistant_turn_token(token_id)
    assert row is not None
    assert row["actor_id"] == "actor-1" and row["revoked_at"] is None and row["last_used_at"] is None
    assert "raw_token" not in row and "secret" not in row
    with pytest.raises(sqlite3.IntegrityError):
        database.insert_assistant_turn_token(
            token_id="66666666-2222-4333-8444-555555555555",
            secret_hash="a" * 64,
            actor_id="actor-1",
            project_id=None,
            turn_ref="sess:2",
            expires_at="2026-08-30T00:02:30Z",
        )
    database.touch_assistant_turn_token(token_id, now="2026-08-30T00:01:00Z")
    assert database.get_assistant_turn_token(token_id)["last_used_at"] == "2026-08-30T00:01:00Z"
    assert database.revoke_assistant_turn_token(token_id, now="2026-08-30T00:02:00Z") is True
    assert database.revoke_assistant_turn_token(token_id, now="2026-08-30T00:02:01Z") is False
    assert database.get_assistant_turn_token(token_id)["revoked_at"] == "2026-08-30T00:02:00Z"
    assert database.purge_expired_assistant_turn_tokens(cutoff_iso="2026-08-30T00:01:00Z") == 0
    assert database.purge_expired_assistant_turn_tokens(cutoff_iso="2026-08-31T00:00:00Z") == 1
    assert database.get_assistant_turn_token(token_id) is None
    database.close()


def test_representative_v17_upgrade_installs_assistant_turn_tokens_without_backfill(tmp_path):
    """Upgrade track: a database that stopped at migration 17 gains the new
    table on reopen with zero rows and no change to any earlier table."""

    path = tmp_path / "v17.db"
    database = Database(str(path))
    _drop_agent_runtime_v3_schema(database._conn)
    database._conn.execute("DELETE FROM schema_migrations WHERE version >= 18")
    database._conn.execute("DROP INDEX idx_assistant_turn_tokens_expires_at")
    database._conn.execute("DROP TABLE assistant_turn_tokens")
    database._conn.execute("PRAGMA user_version = 17")
    database._conn.commit()
    database.close()

    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert (
        upgraded._conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'assistant_turn_tokens'"
        ).fetchone()
        is not None
    )
    assert upgraded._conn.execute("SELECT COUNT(*) FROM assistant_turn_tokens").fetchone()[0] == 0
    upgraded.close()


def test_agent_runtime_v3_schema_is_exact_and_source_pinned(tmp_path):
    """DG-AGENT-RUNTIME-V3 (migration 19): four additive tables, source-pinned
    checksum, one active runner per server, idempotent event append."""

    database = Database(str(tmp_path / "agent-runtime-v3.db"))
    names = {
        row["name"]
        for row in database._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'agent_%'"
        ).fetchall()
    }
    assert {"agent_runners", "agent_session_runtime", "agent_session_events", "agent_permission_requests"} <= names
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 19"
    ).fetchone()
    source_checksum = Migration(19, AGENT_RUNTIME_V3_MIGRATION_NAME, apply_agent_runtime_v3_migration).content_checksum()
    assert tuple(record) == (AGENT_RUNTIME_V3_MIGRATION_NAME, AGENT_RUNTIME_V3_MIGRATION_CHECKSUM, source_checksum)
    assert source_checksum == AGENT_RUNTIME_V3_MIGRATION_CHECKSUM
    columns = [row["name"] for row in database._conn.execute("PRAGMA table_info(agent_runners)").fetchall()]
    assert columns == ["id", "server_name", "label", "secret_hash", "status", "protocol_version", "agent_version", "agent_card", "last_seen_at", "created_at", "revoked_at", "approval_id"]
    database._conn.execute(
        "INSERT INTO agent_runners (id, server_name, secret_hash, status, created_at) VALUES ('11111111-2222-4333-8444-555555555555', 'w', ?, 'enrolled', 't')",
        ("a" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        database._conn.execute(
            "INSERT INTO agent_runners (id, server_name, secret_hash, status, created_at) VALUES ('22222222-2222-4333-8444-555555555555', 'w', ?, 'enrolled', 't')",
            ("b" * 64,),
        )
    database.close()


def test_representative_v18_upgrade_installs_agent_runtime_v3_without_backfill(tmp_path):
    path = tmp_path / "v18.db"
    database = Database(str(path))
    _drop_agent_runtime_v3_schema(database._conn)
    database._conn.execute("DELETE FROM schema_migrations WHERE version >= 19")
    database._conn.execute("PRAGMA user_version = 18")
    database._conn.commit()
    database.close()
    upgraded = Database(str(path))
    assert upgraded.schema_version() == CURRENT_SCHEMA_VERSION
    assert upgraded._conn.execute("SELECT COUNT(*) FROM agent_runners").fetchone()[0] == 0
    assert upgraded.list_agent_runners() == []
    upgraded.close()


def test_agent_session_options_migration_is_additive_and_source_pinned(tmp_path):
    """DG-STUDIO-UI v1 Phase 2 (migration 20): one additive column on the v3
    runtime companion row; the ledger records the pinned source checksum."""
    database = Database(str(tmp_path / "options.db"))
    columns = {row["name"]: row for row in database._conn.execute("PRAGMA table_info(agent_session_runtime)")}
    assert columns["options_json"]["notnull"] == 1 and columns["options_json"]["dflt_value"] == "'{}'"
    record = database._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 20"
    ).fetchone()
    source_checksum = Migration(20, AGENT_SESSION_OPTIONS_MIGRATION_NAME, apply_agent_session_options_migration).content_checksum()
    assert tuple(record) == (AGENT_SESSION_OPTIONS_MIGRATION_NAME, AGENT_SESSION_OPTIONS_MIGRATION_CHECKSUM, source_checksum)
    database.insert_project("p1", "https://example.invalid/p1.git")
    project = database.get_project("p1")
    conversation = database.get_or_create_project_conversation("p1")
    database._conn.execute(
        "INSERT INTO agent_sessions (id, project_id, conversation_id, provider_id, workspace_branch, status, turn_count, max_turns, turn_timeout_sec, created_at, last_used_at)"
        " VALUES ('11111111-1111-4111-8111-111111111111', ?, ?, 'claude-agent-sdk', 'ai-session-x', 'active', 0, 50, 600, '2026-08-31T00:00:00Z', '2026-08-31T00:00:00Z')",
        (project.id, conversation.id),
    )
    sid = "11111111-1111-4111-8111-111111111111"
    database.upsert_agent_session_runtime(sid, task_state="unknown", options={"model": "opus", "permission_mode": "plan"})
    database.upsert_agent_session_runtime(sid, task_state="working")  # None keeps the options
    assert database.get_agent_session_runtime(sid)["options"] == {"model": "opus", "permission_mode": "plan"}
    database.upsert_agent_session_runtime(sid, options={})
    assert database.get_agent_session_runtime(sid)["options"] == {}
    database.close()
