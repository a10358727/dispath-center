from pathlib import Path

from app.config import AppConfig
from app.db import Database, EXECUTION_REASON_CODES, VALID_APPROVAL_KINDS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DECISION_PATH = REPOSITORY_ROOT / "docs" / "DG_EXEC_ATTEMPT_DECISION.md"
DECISIONS_PATH = REPOSITORY_ROOT / "docs" / "DECISIONS.md"
PLAN_PATH = REPOSITORY_ROOT / "docs" / "NEXT_IMPLEMENTATION_PLAN.md"
CONFIG_PATH = REPOSITORY_ROOT / "app" / "config.py"


def _decision_text() -> str:
    return DECISION_PATH.read_text(encoding="utf-8")


def test_exec_attempt_draft_covers_every_named_gate_dimension():
    text = _decision_text()
    for required in (
        "Exact state domains",
        "Exact additive schema contract",
        "Immutable execution contract",
        "Transaction and concurrency rules",
        "Server mutation and credential rules",
        "Feature flags and rollout",
        "Invariant compatibility and deferred decisions",
        "server_config_revisions",
        "server_config_mutations",
        "execution_attempts",
        "execution_operations",
        "execution_attempt_events",
        "execution_shadow_observations",
        "legacy_job_stop_intents",
        "scheduler_leases",
        "effect_started_at",
        "execution_contract_role",
        "rolled_back",
        "recovery_hold",
        "CREATE UNIQUE INDEX",
    ):
        assert required in text


def test_exec_attempt_decision_is_explicitly_approved_and_hash_pinned():
    text = _decision_text()
    decisions = DECISIONS_PATH.read_text(encoding="utf-8")

    assert "Status: **approved on 2026-07-27" in text
    assert "Contract revision: `DG-EXEC-ATTEMPT-v1`" in text
    assert "- [x] **Approve recommended contract**" in text
    assert "DG-EXEC-ATTEMPT：核准本文件的 recommended contract" in decisions
    assert (
        "5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d"
        in text
    )
    assert (
        "5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d"
        in decisions
    )


def test_exec_attempt_contract_closes_reviewed_consistency_gaps():
    text = _decision_text()
    plan = PLAN_PATH.read_text(encoding="utf-8")

    assert "uncertain  -> delivered | failed" in text
    assert "execution_attempt_id     TEXT" in text
    assert "does not open or hash the private key" in text
    assert "content_sha256}`" not in text
    assert "EXECUTION_ATTEMPT_RECONCILE_EXISTING=false" in text
    assert "(approval_id, payload_sha256, payload_contract_version)" in text
    assert "execution_operations(operation=stop)" in text
    assert "never dual-written as competing stop truth" in text
    assert "server_config_mutations.approval_id -> approvals.id" in text
    assert "intent       -> yaml_applied | rolled_back | recovery_hold" in text
    assert "In one final DB transaction, activate/retire" in text
    assert "after revision activation but before in-memory reload" not in text
    assert "execution_approval_id    NOT NULL for every new generic attempt" in plan
    assert "nullable only for honest legacy" not in plan
    assert "(approval_id, payload_sha256, payload_contract_version)" in plan


def test_approved_gate_installs_additive_schema_with_default_off_runtime():
    database = Database(":memory:")
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            table_names = {row["name"] for row in cursor.fetchall()}
            cursor.execute("PRAGMA table_info(node_attempts)")
            node_attempt_columns = {row["name"] for row in cursor.fetchall()}
    finally:
        database.close()

    assert {
        "server_config_revisions",
        "server_config_mutations",
        "execution_attempts",
        "execution_operations",
        "execution_attempt_events",
        "execution_shadow_observations",
        "legacy_job_stop_intents",
        "scheduler_leases",
    }.issubset(table_names)
    assert "execution_attempt_id" in node_attempt_columns
    assert "attempt_cleanup" not in VALID_APPROVAL_KINDS
    assert "contract_validated" in EXECUTION_REASON_CODES

    config_source = CONFIG_PATH.read_text(encoding="utf-8")
    for flag in (
        "EXECUTION_ATTEMPT_SHADOW_ENABLED",
        "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED",
        "EXECUTION_ATTEMPT_RECONCILE_EXISTING",
        "EXECUTION_OUTBOX_WORKER_ENABLED",
    ):
        assert flag in config_source

    config = AppConfig(servers=[])
    assert config.execution_attempt_shadow_enabled is False
    assert config.execution_attempt_new_claims_enabled is False
    assert config.execution_attempt_reconcile_existing is False
    assert config.execution_outbox_worker_enabled is False
