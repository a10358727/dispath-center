"""Durable audit ledger, hash-chain, pagination, and export outbox tests."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

import app.audit as audit_module
from app.audit import (
    audit_export_alert_snapshot,
    deduplicate_audit_records,
    export_durable_audit_events,
    tail_audit,
)
from app.audit_adoption import (
    AUDIT_ADOPTION,
    REQUIRED_MUTATIONS,
    audit_coverage,
    validate_audit_catalog,
)
from app.db import Database
from app.identity import ActorType
from app.migrations import backup_database, restore_verify_database
from app.node_registry import (
    activate_node_credential,
    enroll_node,
    revoke_node,
    rotate_node_credential,
    stage_node_credential,
)
from tests.test_execution_attempt_foundation import _foundation_records
from dispatch_center import cli
from dispatch_center.infrastructure.db import SQLiteUnitOfWork
from scripts.audit_export_status import (
    main as audit_export_status_main,
    read_audit_export_status,
)


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


def test_audit_export_alert_signal_requires_dead_letter_for_critical_alert():
    assert audit_export_alert_snapshot(backlog=0, dead_letter=0) == {
        "status": "clear",
        "alert": {
            "active": False,
            "severity": "none",
            "reason_codes": [],
        },
    }
    assert audit_export_alert_snapshot(backlog=2, dead_letter=0) == {
        "status": "attention",
        "alert": {
            "active": False,
            "severity": "none",
            "reason_codes": [],
        },
    }
    assert audit_export_alert_snapshot(backlog=1, dead_letter=2) == {
        "status": "attention",
        "alert": {
            "active": True,
            "severity": "critical",
            "reason_codes": ["dead_letter_present"],
        },
    }

    with pytest.raises(ValueError, match="backlog"):
        audit_export_alert_snapshot(backlog=-1, dead_letter=0)
    with pytest.raises(ValueError, match="dead-letter"):
        audit_export_alert_snapshot(backlog=0, dead_letter=-1)


def test_durable_audit_is_versioned_and_hash_chained(tmp_path):
    database = Database(str(tmp_path / "audit.db"))
    assert database.schema_version() == 16
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
    assert AUDIT_ADOPTION["execution_attempt.create"].durability == "durable"
    assert AUDIT_ADOPTION["node_attempt.create"].durability == "durable"
    for mutation in (
        "identity.service_account",
        "identity.service_token",
        "identity.membership",
        "identity.session",
    ):
        assert AUDIT_ADOPTION[mutation].durability == "durable"
    assert AUDIT_ADOPTION["approval.create"].durability == "durable"
    assert AUDIT_ADOPTION["approval.decide"].durability == "durable"
    assert AUDIT_ADOPTION["approval.reject"].durability == "durable"
    assert AUDIT_ADOPTION["execution_attempt.stop"].durability == "durable"
    assert AUDIT_ADOPTION["execution_attempt.launch_resolution"].durability == "durable"
    assert AUDIT_ADOPTION["execution_attempt.result"].durability == "durable"
    assert AUDIT_ADOPTION["run.create"].durability == "durable"
    assert AUDIT_ADOPTION["project.create"].durability == "durable"
    assert AUDIT_ADOPTION["project.update"].durability == "durable"
    assert AUDIT_ADOPTION["project.delete"].durability == "durable"
    assert AUDIT_ADOPTION["project.version"].durability == "durable"
    assert AUDIT_ADOPTION["project.remote_mutation"].durability == "durable"
    assert AUDIT_ADOPTION["project.candidate"].durability == "durable"
    assert AUDIT_ADOPTION["project.instance"].durability == "durable"
    assert AUDIT_ADOPTION["project_snapshot.publish"].durability == "durable"
    assert AUDIT_ADOPTION["experiment_record.mutate"].durability == "durable"
    assert AUDIT_ADOPTION["dataset.mutate"].durability == "durable"
    assert AUDIT_ADOPTION["dataset.sync_verification"].durability == "durable"
    for mutation in (
        "server.add",
        "server.update",
        "server.disable",
        "server.delete",
    ):
        assert AUDIT_ADOPTION[mutation].durability == "durable"
    assert AUDIT_ADOPTION["server.publication"].durability == "durable"
    assert AUDIT_ADOPTION["server.legacy_observed"].durability == "durable"
    assert AUDIT_ADOPTION["server.compatibility"].durability == "legacy"
    assert AUDIT_ADOPTION["engineering_task.result"].durability == "durable"
    assert AUDIT_ADOPTION["engineering_task.promote"].durability == "durable"
    assert AUDIT_ADOPTION["engineering_task.mutate"].durability == "durable"
    assert AUDIT_ADOPTION["engineering_task.retry"].durability == "durable"
    assert AUDIT_ADOPTION["engineering_task.discard"].durability == "durable"
    assert AUDIT_ADOPTION["engineering_task.compatibility"].durability == "legacy"
    assert AUDIT_ADOPTION["run_profile.mutate"].durability == "durable"
    assert AUDIT_ADOPTION["run_profile.compatibility"].durability == "legacy"
    assert AUDIT_ADOPTION["dispatch_policy.mutate"].durability == "durable"
    assert AUDIT_ADOPTION["dispatch_policy.compatibility"].durability == "legacy"
    assert AUDIT_ADOPTION["approval.approve"].durability == "durable"
    assert AUDIT_ADOPTION["approval.compatibility"].durability == "legacy"
    assert AUDIT_ADOPTION["execution.job_materialize"].durability == "durable"
    assert AUDIT_ADOPTION["execution.job_lifecycle"].durability == "durable"
    assert AUDIT_ADOPTION["execution.auto_placement"].durability == "durable"
    assert AUDIT_ADOPTION["execution.compatibility"].durability == "legacy"
    for mutation in (
        "node.enroll",
        "node.rotate",
        "node.drain",
        "node.revoke",
        "node.retire",
    ):
        assert AUDIT_ADOPTION[mutation].durability == "durable"
    coverage = audit_coverage()
    assert coverage["mode"] == "partial"
    assert "project.update" in coverage["durable_actions"]
    assert "project.create" in coverage["durable_actions"]
    assert "project.delete" in coverage["durable_actions"]
    assert "project.version" in coverage["durable_actions"]
    assert "project.remote_mutation" in coverage["durable_actions"]
    assert "project.candidate" in coverage["durable_actions"]
    assert "project.instance" in coverage["durable_actions"]
    assert "dataset.mutate" in coverage["durable_actions"]
    assert "dataset.sync_verification" in coverage["durable_actions"]
    assert "experiment_record.mutate" in coverage["durable_actions"]
    assert "server.publication" in coverage["durable_actions"]
    assert "server.legacy_observed" in coverage["durable_actions"]
    assert {
        "server.add",
        "server.update",
        "server.disable",
        "server.delete",
    } <= set(coverage["durable_actions"])
    assert "engineering_task.result" in coverage["durable_actions"]
    assert "engineering_task.retry" in coverage["durable_actions"]
    assert "engineering_task.discard" in coverage["durable_actions"]
    assert "approval.decide" in coverage["durable_actions"]
    assert "approval.create" in coverage["durable_actions"]
    assert "execution_attempt.stop" in coverage["durable_actions"]
    assert {
        "identity.service_account",
        "identity.service_token",
        "identity.membership",
        "identity.session",
    } <= set(coverage["durable_actions"])
    assert "approval.approve" in coverage["durable_actions"]
    assert "execution.job_materialize" in coverage["durable_actions"]
    assert "execution.job_lifecycle" in coverage["durable_actions"]
    assert "execution.auto_placement" in coverage["durable_actions"]
    assert "execution.compatibility" in coverage["legacy_actions"]
    assert "server.compatibility" in coverage["legacy_actions"]
    assert "run_profile.mutate" in coverage["durable_actions"]
    assert "dispatch_policy.mutate" in coverage["durable_actions"]
    assert "run_profile.compatibility" in coverage["legacy_actions"]
    assert "dispatch_policy.compatibility" in coverage["legacy_actions"]
    assert "approval.compatibility" in coverage["legacy_actions"]
    assert "approval.reject" in coverage["durable_actions"]
    assert {
        "node.enroll",
        "node.rotate",
        "node.drain",
        "node.revoke",
        "node.retire",
        "node_attempt.create",
    } <= set(coverage["durable_actions"])
    assert coverage["legacy_without_migration_issue"] == []
    assert coverage["entries_without_owner"] == []
    assert coverage["required_missing"] == []
    assert coverage["required_legacy_actions"] == [
        "engineering_task.compatibility"
    ]
    assert set(coverage["required_durable_actions"]) == (
        set(REQUIRED_MUTATIONS) - {"engineering_task.compatibility"}
    )
    assert validate_audit_catalog() == ()


def test_approval_decision_and_durable_event_commit_atomically(tmp_path):
    database = Database(str(tmp_path / "approval-decision.db"))
    approval_id = database.insert_approval(
        "enqueue", {"command": "echo approved", "server": "local"}
    )

    database.update_approval(
        approval_id,
        status="approved",
        decided_at="2026-08-05T00:00:00+00:00",
        decision_actor_id="reviewer-1",
        decision_mechanism="manual",
    )

    approval = database.get_approval(approval_id)
    assert approval is not None
    assert approval.status == "approved"
    events = database.list_durable_audit_events(limit=10)
    decision = next(event for event in events if event["action"] == "approval_decided")
    assert decision["result"] == "approved"
    assert decision["approval_id"] == approval_id
    assert decision["resource_type"] == "approval"
    assert decision["resource_id"] == str(approval_id)
    assert decision["actor"] == {
        "id": "reviewer-1",
        "kind": "actor",
        "authentication": "manual",
    }

    # A retry/update of an already terminal approval must not create a second
    # decision event for the same transition.
    database.update_approval(approval_id, note="reviewed")
    assert database.count_durable_audit_events() == 2
    database.close()


def test_approval_decision_rolls_back_when_durable_audit_append_fails(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "approval-decision-rollback.db"))
    approval_id = database.insert_approval("enqueue", {"command": "echo safe"})

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.update_approval(approval_id, status="rejected")

    approval = database.get_approval(approval_id)
    assert approval is not None
    assert approval.status == "pending"
    assert approval.decided_at is None
    assert database.count_durable_audit_events() == 1
    database.close()


def test_approval_creation_and_durable_event_commit_atomically(tmp_path):
    database = Database(str(tmp_path / "approval-create.db"))
    approval_id = database.insert_approval(
        "enqueue", {"command": "echo safe"}, requester_actor_id="requester-1"
    )
    approval = database.get_approval(approval_id)
    assert approval is not None
    assert approval.status == "pending"
    event = database.list_durable_audit_events(limit=10)[0]
    assert event["action"] == "approval_created"
    assert event["result"] == "pending"
    assert event["approval_id"] == approval_id
    assert event["resource_id"] == str(approval_id)
    assert event["params"] == {
        "approval_kind": "enqueue",
        "requester_actor_present": True,
    }
    assert event["actor"] == {
        "id": "requester-1",
        "kind": "actor",
        "authentication": "approval_request",
    }
    database.close()


def test_coding_run_creation_is_durable_and_does_not_copy_instruction(tmp_path):
    database = Database(str(tmp_path / "coding-run-create.db"))
    run_id = database.insert_coding_run(
        approval_id=77,
        project="demo",
        runner_server="runner-a",
        instruction="private instruction must stay in the run row",
    )
    event = next(
        event
        for event in database.list_durable_audit_events(limit=20)
        if event["action"] == "run_created"
    )
    assert event["resource_type"] == "coding_run"
    assert event["resource_id"] == str(run_id)
    assert event["approval_id"] == 77
    assert event["params"] == {
        "base_binding": "legacy_unpinned",
        "engineering_bound": False,
        "project": "demo",
        "runner_server": "runner-a",
        "status": "queued",
    }
    assert "private instruction" not in json.dumps(event)
    database.close()


def test_coding_run_creation_rolls_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "coding-run-create-rollback.db"))

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.insert_coding_run(
            approval_id=77,
            project="demo",
            runner_server="runner-a",
            instruction="not committed",
        )
    assert database.list_coding_runs() == []
    assert database.count_durable_audit_events() == 0
    database.close()


def test_project_update_audit_records_only_changed_field_names(tmp_path):
    database = Database(str(tmp_path / "project-update.db"))
    project_id = database.insert_project("demo", "/repo/demo")
    database.update_project("demo", goal="private project goal", progress="50%")
    event = next(
        event
        for event in database.list_durable_audit_events(limit=20)
        if event["action"] == "project_updated"
    )
    assert event["resource_type"] == "project"
    assert event["resource_id"] == project_id
    assert event["params"] == {
        "field_count": 2,
        "field_names": ["goal", "progress"],
    }
    assert "private project goal" not in json.dumps(event)
    database.close()


def test_project_update_rolls_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "project-update-rollback.db"))
    database.insert_project("demo", "/repo/demo")
    original = database.append_durable_audit_event_in_transaction

    def fail_project_audit(*args, **kwargs):
        if kwargs.get("action") == "project_updated":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_project_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.update_project("demo", goal="not committed")
    assert database.get_project("demo").goal is None
    assert not any(
        event["action"] == "project_updated"
        for event in database.list_durable_audit_events(limit=20)
    )
    database.close()


def test_project_create_rolls_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "project-create-rollback.db"))
    original = database.append_durable_audit_event_in_transaction

    def fail_project_audit(*args, **kwargs):
        if kwargs.get("action") == "project_created":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_project_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.insert_project("demo", "/repo/demo")
    assert database.get_project("demo") is None
    assert not any(
        event["action"] == "project_created"
        for event in database.list_durable_audit_events(limit=20)
    )
    database.close()


def test_project_candidate_upsert_and_status_are_bounded_and_idempotent(tmp_path):
    database = Database(str(tmp_path / "project-candidate-audit.db"))
    candidate_id = database.upsert_project_candidate(
        server="server-a",
        path="/private/projects/demo",
        name_guess="demo",
        git_remote="https://example.invalid/demo.git",
        git_branch="main",
        git_commit="abc123",
        markers=[".git", "README.md"],
        readme_excerpt="private README text",
        embedded_data_paths=["/private/data"],
        embedded_data_summary="private data summary",
    )
    events = database.list_durable_audit_events(limit=20)
    created = next(
        event for event in events if event["action"] == "project_candidate_created"
    )
    assert created["resource_type"] == "project_candidate"
    assert created["resource_id"] == candidate_id
    assert created["params"] == {
        "kind": "project",
        "server": "server-a",
        "status": "pending",
    }
    assert "/private/projects/demo" not in json.dumps(created)
    assert "private README text" not in json.dumps(created)
    assert "/private/data" not in json.dumps(created)

    # A repeated scan with the same metadata refreshes the row but does not
    # create an unbounded audit stream.
    database.upsert_project_candidate(
        server="server-a",
        path="/private/projects/demo",
        name_guess="demo",
        git_remote="https://example.invalid/demo.git",
        git_branch="main",
        git_commit="abc123",
        markers=[".git", "README.md"],
        readme_excerpt="private README text",
        embedded_data_paths=["/private/data"],
        embedded_data_summary="private data summary",
    )
    assert sum(
        event["action"] == "project_candidate_created"
        for event in database.list_durable_audit_events(limit=20)
    ) == 1
    assert not any(
        event["action"] == "project_candidate_updated"
        for event in database.list_durable_audit_events(limit=20)
    )

    database.upsert_project_candidate(
        server="server-a",
        path="/private/projects/demo",
        name_guess="demo",
        git_remote="https://example.invalid/demo.git",
        git_branch="main",
        git_commit="def456",
        markers=[".git", "README.md"],
        readme_excerpt="private README text",
        embedded_data_paths=["/private/data"],
        embedded_data_summary="private data summary",
    )
    updated = next(
        event
        for event in database.list_durable_audit_events(limit=20)
        if event["action"] == "project_candidate_updated"
    )
    assert updated["params"] == {
        "changed_field_count": 1,
        "kind": "project",
        "server": "server-a",
        "status": "pending",
    }

    database.update_project_candidate_status(candidate_id, "imported")
    status_event = next(
        event
        for event in database.list_durable_audit_events(limit=20)
        if event["action"] == "project_candidate_status_changed"
    )
    assert status_event["params"] == {
        "from_status": "pending",
        "server": "server-a",
        "to_status": "imported",
    }
    database.update_project_candidate_status(candidate_id, "imported")
    assert sum(
        event["action"] == "project_candidate_status_changed"
        for event in database.list_durable_audit_events(limit=20)
    ) == 1
    database.close()


def test_project_candidate_mutations_roll_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "project-candidate-rollback.db"))
    original = database.append_durable_audit_event_in_transaction

    def fail_candidate_audit(*args, **kwargs):
        if kwargs.get("action") == "project_candidate_created":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_candidate_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.upsert_project_candidate(
            server="server-a", path="/private/projects/demo"
        )
    assert database.list_project_candidates() == []
    assert not database.list_durable_audit_events(limit=20)

    monkeypatch.setattr(database, "append_durable_audit_event_in_transaction", original)
    candidate_id = database.upsert_project_candidate(
        server="server-a", path="/private/projects/demo"
    )

    def fail_status_audit(*args, **kwargs):
        if kwargs.get("action") == "project_candidate_status_changed":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_status_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.update_project_candidate_status(candidate_id, "ignored")
    assert database.get_project_candidate(candidate_id).status == "pending"
    assert not any(
        event["action"] == "project_candidate_status_changed"
        for event in database.list_durable_audit_events(limit=20)
    )
    database.close()


def test_project_instance_upsert_is_durable_and_quiet_on_noop(tmp_path):
    database = Database(str(tmp_path / "project-instance-audit.db"))
    database.insert_project("demo", "/repo/demo")
    instance_id = database.insert_project_instance(
        project_name="demo",
        server="server-a",
        path="/private/demo",
        git_branch="main",
        git_commit="abc123",
        embedded_data_paths=["/private/data"],
    )
    created = next(
        event
        for event in database.list_durable_audit_events(limit=20)
        if event["action"] == "project_instance_created"
    )
    assert created["resource_type"] == "project_instance"
    assert created["resource_id"] == instance_id
    assert created["params"] == {"project": "demo", "server": "server-a"}
    assert "/private/demo" not in json.dumps(created)
    assert "/private/data" not in json.dumps(created)

    database.insert_project_instance(
        project_name="demo",
        server="server-a",
        path="/private/demo",
        git_branch="main",
        git_commit="abc123",
        embedded_data_paths=["/private/data"],
    )
    assert not any(
        event["action"] == "project_instance_updated"
        for event in database.list_durable_audit_events(limit=20)
    )

    database.insert_project_instance(
        project_name="demo",
        server="server-a",
        path="/private/demo",
        git_branch="main",
        git_commit="def456",
        embedded_data_paths=["/private/data"],
    )
    updated = next(
        event
        for event in database.list_durable_audit_events(limit=20)
        if event["action"] == "project_instance_updated"
    )
    assert updated["params"] == {
        "changed_field_count": 1,
        "project": "demo",
        "server": "server-a",
    }
    database.close()


def test_project_instance_create_rolls_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "project-instance-rollback.db"))
    database.insert_project("demo", "/repo/demo")
    original = database.append_durable_audit_event_in_transaction

    def fail_instance_audit(*args, **kwargs):
        if kwargs.get("action") == "project_instance_created":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_instance_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.insert_project_instance(
            project_name="demo", server="server-a", path="/private/demo"
        )
    assert database.list_project_instances("demo") == []
    assert not any(
        event["action"] == "project_instance_created"
        for event in database.list_durable_audit_events(limit=20)
    )
    database.close()


def test_project_version_creation_is_durable_immutable_and_idempotent(tmp_path):
    database = Database(str(tmp_path / "project-version-audit.db"))
    database.insert_project("demo", "/repo/demo")
    version = database.get_or_create_project_version(
        "demo",
        "abc123full",
        git_ref="main",
        source_instance_id="instance-1",
        metadata={"private": "metadata"},
    )
    created = next(
        event
        for event in database.list_durable_audit_events(limit=30)
        if event["action"] == "project_version_created"
    )
    assert created["resource_type"] == "project_version"
    assert created["resource_id"] == version.id
    assert created["params"] == {
        "git_commit": "abc123full",
        "git_ref": "main",
        "project": "demo",
        "source_instance_present": True,
    }
    assert "private metadata" not in json.dumps(created)

    same = database.get_or_create_project_version(
        "demo",
        "abc123full",
        git_ref="other-ref",
        source_instance_id="other-instance",
        metadata={"changed": True},
    )
    assert same.id == version.id
    assert sum(
        event["action"] == "project_version_created"
        for event in database.list_durable_audit_events(limit=30)
    ) == 1
    assert database.get_project_version(version.id).git_ref == "main"
    database.close()


def test_project_version_creation_rolls_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "project-version-rollback.db"))
    database.insert_project("demo", "/repo/demo")
    original = database.append_durable_audit_event_in_transaction

    def fail_version_audit(*args, **kwargs):
        if kwargs.get("action") == "project_version_created":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_version_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.get_or_create_project_version("demo", "abc123full", git_ref="main")
    assert database.list_project_versions("demo") == []
    assert not any(
        event["action"] == "project_version_created"
        for event in database.list_durable_audit_events(limit=30)
    )
    database.close()


def test_import_project_decision_rolls_back_all_rows_on_audit_failure(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "import-project-uow.db"))
    candidate_id = database.upsert_project_candidate(
        server="server-a", path="/private/demo", name_guess="demo"
    )
    approval_id = database.insert_approval(
        "import_project", {"candidate_id": candidate_id, "name": "demo"}
    )
    original = database.append_durable_audit_event_in_transaction

    def fail_import_audit(*args, **kwargs):
        if kwargs.get("action") == "project_instance_created":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_import_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.apply_import_project_decision(
            approval_id=approval_id,
            candidate_id=candidate_id,
            project_name="demo",
            create_project=True,
            repo_or_path="/private/demo",
        )
    assert database.get_approval(approval_id).status == "pending"
    assert database.get_project("demo") is None
    assert database.list_project_instances("demo") == []
    assert database.get_project_candidate(candidate_id).status == "pending"
    assert not any(
        event["action"] in {
            "project_created",
            "project_instance_created",
            "project_candidate_status_changed",
            "approval_decided",
        }
        for event in database.list_durable_audit_events(limit=50)
    )
    database.close()


def test_ignore_candidate_decision_rolls_back_status_and_approval_on_audit_failure(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "ignore-candidate-uow.db"))
    candidate_id = database.upsert_project_candidate(
        server="server-a", path="/private/demo"
    )
    approval_id = database.insert_approval(
        "ignore_project_candidate", {"candidate_id": candidate_id}
    )
    original = database.append_durable_audit_event_in_transaction

    def fail_ignore_audit(*args, **kwargs):
        if kwargs.get("action") == "project_candidate_status_changed":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_ignore_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.apply_project_candidate_ignore_decision(
            approval_id=approval_id, candidate_id=candidate_id
        )
    assert database.get_approval(approval_id).status == "pending"
    assert database.get_project_candidate(candidate_id).status == "pending"
    assert not any(
        event["action"] == "project_candidate_status_changed"
        for event in database.list_durable_audit_events(limit=50)
    )
    database.close()


def test_inventory_scan_decision_rolls_back_all_candidates_on_audit_failure(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "inventory-scan-uow.db"))
    approval_id = database.insert_approval(
        "inventory_scan",
        {"server": "server-a", "project_roots": ["/private/projects"]},
    )
    original = database.append_durable_audit_event_in_transaction

    def fail_inventory_audit(*args, **kwargs):
        if kwargs.get("action") == "project_candidate_created":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_inventory_audit
    )
    candidates = [
        {
            "server": "server-a",
            "path": "/private/projects/one",
            "name_guess": "one",
            "markers": [".git"],
        },
        {
            "server": "server-a",
            "path": "/private/projects/two",
            "name_guess": "two",
            "markers": ["README.md"],
        },
    ]
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.apply_inventory_scan_decision(
            approval_id=approval_id, candidates=candidates
        )
    assert database.get_approval(approval_id).status == "pending"
    assert database.list_project_candidates() == []
    assert not any(
        event["action"] in {"project_candidate_created", "approval_decided"}
        for event in database.list_durable_audit_events(limit=50)
    )
    database.close()


def test_inventory_scan_decision_correlates_candidate_events_and_is_quiet_on_rescan(
    tmp_path,
):
    database = Database(str(tmp_path / "inventory-scan-success.db"))
    approval_id = database.insert_approval(
        "inventory_scan", {"server": "server-a", "project_roots": ["/projects"]}
    )
    candidates = [
        {
            "server": "server-a",
            "path": "/projects/demo",
            "name_guess": "demo",
            "markers": [".git"],
        }
    ]
    assert database.apply_inventory_scan_decision(
        approval_id=approval_id, candidates=candidates
    ) == 1
    created = next(
        event
        for event in database.list_durable_audit_events(limit=30)
        if event["action"] == "project_candidate_created"
    )
    assert created["approval_id"] == approval_id
    assert database.get_approval(approval_id).status == "approved"

    # A second scan is a new approval but unchanged metadata does not append
    # another candidate lifecycle event.
    second_approval_id = database.insert_approval(
        "inventory_scan", {"server": "server-a", "project_roots": ["/projects"]}
    )
    assert database.apply_inventory_scan_decision(
        approval_id=second_approval_id, candidates=candidates
    ) == 1
    assert sum(
        event["action"] == "project_candidate_created"
        for event in database.list_durable_audit_events(limit=50)
    ) == 1
    assert not any(
        event["action"] == "project_candidate_updated"
        for event in database.list_durable_audit_events(limit=50)
    )
    database.close()


def test_ignore_nested_decision_is_atomic_and_preserves_competition_skips(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "ignore-nested-uow.db"))
    ignored_id = database.upsert_project_candidate(
        server="server-a", path="/private/projects/ignored"
    )
    skipped_id = database.upsert_project_candidate(
        server="server-a", path="/private/projects/skipped"
    )
    database.update_project_candidate_status(skipped_id, "imported")
    approval_id = database.insert_approval(
        "ignore_nested_candidates", {"candidate_ids": [ignored_id, skipped_id]}
    )
    result = database.apply_ignore_nested_candidates_decision(
        approval_id=approval_id, candidate_ids=[ignored_id, skipped_id]
    )
    assert result["ignored_ids"] == [ignored_id]
    assert result["skipped_ids"] == [skipped_id]
    assert database.get_project_candidate(ignored_id).status == "ignored"
    assert database.get_project_candidate(skipped_id).status == "imported"
    assert database.get_approval(approval_id).status == "approved"

    rollback_candidate = database.upsert_project_candidate(
        server="server-a", path="/private/projects/rollback"
    )
    rollback_approval = database.insert_approval(
        "ignore_nested_candidates", {"candidate_ids": [rollback_candidate]}
    )
    original = database.append_durable_audit_event_in_transaction

    def fail_nested_audit(*args, **kwargs):
        if kwargs.get("action") == "project_candidate_status_changed":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_nested_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.apply_ignore_nested_candidates_decision(
            approval_id=rollback_approval, candidate_ids=[rollback_candidate]
        )
    assert database.get_project_candidate(rollback_candidate).status == "pending"
    assert database.get_approval(rollback_approval).status == "pending"
    database.close()


def test_project_delete_rolls_back_when_audit_append_fails(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "project-delete-rollback.db"))
    project_id = database.insert_project("demo", "/repo/demo")
    database.insert_project_instance(
        project_name="demo", server="server-a", path="/srv/demo"
    )
    original = database.append_durable_audit_event_in_transaction

    def fail_project_audit(*args, **kwargs):
        if kwargs.get("action") == "project_deleted":
            raise RuntimeError("audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_project_audit
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.delete_project("demo")
    restored = database.get_project("demo")
    assert restored is not None and restored.id == project_id
    assert len(database.list_project_instances("demo")) == 1
    assert not any(
        event["action"] == "project_deleted"
        for event in database.list_durable_audit_events(limit=20)
    )
    database.close()


def test_dataset_registration_and_card_update_keep_payloads_out_of_audit(tmp_path):
    database = Database(str(tmp_path / "dataset-audit.db"))
    database.insert_dataset(
        "demo",
        "v1",
        123,
        "/private/source",
        {"files": [{"path": "secret.csv", "size": 123}]},
    )
    database.update_dataset_card(
        "demo", "v1", {"description": "private card", "method": "internal"}
    )
    events = database.list_durable_audit_events(limit=20)
    created = next(event for event in events if event["action"] == "dataset_created")
    updated = next(event for event in events if event["action"] == "dataset_updated")
    assert created["params"] == {
        "dataset": "demo",
        "size_bytes": 123,
        "sync_mode": "packed",
        "version": "v1",
    }
    assert updated["params"] == {"dataset": "demo", "version": "v1"}
    assert "/private/source" not in json.dumps(events)
    assert "private card" not in json.dumps(events)
    database.close()


def test_dataset_cache_mutations_are_idempotent_and_durable(tmp_path):
    database = Database(str(tmp_path / "dataset-cache-audit.db"))
    database.upsert_dataset_cache("worker-a", "demo", "v1")
    database.upsert_dataset_cache("worker-a", "demo", "v1")
    database.delete_dataset_cache("worker-a", "demo", "v1")
    database.delete_dataset_cache("worker-a", "demo", "v1")

    events = database.list_durable_audit_events(limit=20)
    assert [event["action"] for event in reversed(events)] == [
        "dataset_cache_added",
        "dataset_cache_removed",
    ]
    assert all(event["resource_id"] == "demo@v1" for event in events)
    database.close()


def test_dataset_mutation_rolls_back_when_durable_audit_append_fails(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "dataset-rollback.db"))

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.upsert_dataset_cache("worker-a", "demo", "v1")
    assert database.list_dataset_cache() == []
    assert database.count_durable_audit_events() == 0
    database.close()


def test_approval_creation_rolls_back_when_durable_audit_append_fails(
    tmp_path, monkeypatch
):
    database = Database(str(tmp_path / "approval-create-rollback.db"))

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.insert_approval("enqueue", {"command": "echo safe"})
    assert database.list_approvals() == []
    assert database.count_durable_audit_events() == 0
    database.close()


def test_identity_mutations_write_transactional_durable_events(tmp_path):
    database = Database(str(tmp_path / "identity-audit.db"))
    operator = database.insert_actor(
        actor_type=ActorType.HUMAN, display_name="Identity operator"
    )
    service_actor = database.insert_actor(
        actor_type=ActorType.SERVICE, display_name="Worker service"
    )
    database.insert_project("identity-project", "git@example/identity")
    database.insert_service_account(
        actor_id=service_actor.id,
        name="worker-service",
        created_by_actor_id=operator.id,
    )
    token = database.insert_service_account_token(
        token_id="token-identity-audit",
        service_account_actor_id=service_actor.id,
        secret_hash="a" * 64,
        scopes=["jobs:read"],
        expires_at="2099-01-01T00:00:00+00:00",
        created_by_actor_id=operator.id,
    )
    session = database.insert_actor_session(
        actor_id=operator.id,
        secret_hash="b" * 64,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    database.upsert_project_membership(
        project="identity-project",
        actor_id=operator.id,
        role="viewer",
        created_by_actor_id=operator.id,
    )
    database.delete_project_membership(
        database.get_project("identity-project").id, operator.id
    )
    assert database.revoke_actor_session(session.id) is True
    assert database.revoke_service_account_token(token.id) is True
    database.update_actor(service_actor.id, disabled_at="2099-01-01T00:00:00+00:00")

    actions = {
        event["action"] for event in database.list_durable_audit_events(limit=50)
    }
    assert {
        "service_account_created",
        "service_token_created",
        "membership_granted",
        "membership_revoked",
        "session_authenticated",
        "session_revoked",
        "service_token_revoked",
        "service_account_disabled",
    } <= actions
    serialized = json.dumps(database.list_durable_audit_events(limit=50))
    assert "secret_hash" not in serialized
    assert "" + "a" * 64 not in serialized
    database.close()


def test_identity_mutation_rolls_back_with_durable_audit_failure(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "identity-audit-rollback.db"))
    service_actor = database.insert_actor(
        actor_type=ActorType.SERVICE, display_name="Rollback service"
    )

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(database, "_append_identity_audit_event", fail_append)
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.insert_service_account(
            actor_id=service_actor.id, name="rollback-service"
        )
    assert database.get_service_account(service_actor.id) is None
    database.close()


def test_node_lifecycle_mutations_write_safe_transactional_events(tmp_path):
    database = Database(str(tmp_path / "node-lifecycle-audit.db"))
    enrolled = enroll_node(database, server_name="worker-a", approval_id=7)
    rotated = rotate_node_credential(
        database, enrolled.node.id, overlap_sec=300, approval_id=8
    )
    assert rotated is not None
    staged = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
        approval_id=9,
    )
    assert staged is not None
    assert activate_node_credential(
        database, staged.raw_token, staged.activation_nonce
    ) is not None
    database.set_node_draining(enrolled.node.id, approval_id=10)
    assert database.retire_node(enrolled.node.id, approval_id=11) is not None

    revoked = enroll_node(database, server_name="worker-b", approval_id=12)
    job_id = database.insert_job(command="echo node-attempt")
    database.insert_node_attempt(
        attempt_id="node-attempt-audit",
        job_id=job_id,
        node_id=revoked.node.id,
        command_sha256="a" * 64,
        lease_expires_at="2099-01-01T00:00:00.000Z",
    )
    assert revoke_node(database, revoked.node.id) is not None

    events = database.list_durable_audit_events(limit=100)
    actions = {event["action"] for event in events}
    assert {
        "node_enrolled",
        "node_rotated",
        "node_credential_activated",
        "node_drained",
        "node_retired",
        "node_revoked",
        "node_attempt_created",
    } <= actions
    serialized = json.dumps(events)
    assert enrolled.raw_token not in serialized
    assert rotated.raw_token not in serialized
    assert staged.raw_token not in serialized
    assert staged.activation_nonce not in serialized
    assert enrolled.node.secret_hash not in serialized
    database.close()


def test_node_lifecycle_and_durable_event_roll_back_together(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "node-lifecycle-rollback.db"))

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        enroll_node(database, server_name="worker-a")
    assert database.list_nodes() == []
    assert database.count_durable_audit_events() == 0

    monkeypatch.undo()
    enrolled = enroll_node(database, server_name="worker-a")
    before = database.get_node(enrolled.node.id)
    assert before is not None and before.is_active
    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.revoke_node_with_execution_hold(enrolled.node.id)
    after = database.get_node(enrolled.node.id)
    assert after is not None and after.is_active
    assert database.count_durable_audit_events() == 1

    job_id = database.insert_job(command="echo rollback")
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.insert_node_attempt(
            attempt_id="node-attempt-rollback",
            job_id=job_id,
            node_id=enrolled.node.id,
            command_sha256="b" * 64,
            lease_expires_at="2099-01-01T00:00:00.000Z",
        )
    assert database.get_node_attempt("node-attempt-rollback") is None
    database.close()


def test_node_terminal_convergence_writes_one_durable_summary(tmp_path):
    database = Database(str(tmp_path / "node-terminal-audit.db"))
    node = database.insert_node(
        node_id="node-terminal-audit",
        server_name="worker-a",
        secret_hash="c" * 64,
    )
    job_id = database.insert_job(command="echo terminal")
    database.insert_node_attempt(
        attempt_id="node-terminal-attempt",
        job_id=job_id,
        node_id=node.id,
        command_sha256="d" * 64,
        lease_expires_at="2099-01-01T00:00:00.000Z",
    )
    assert database.ack_node_attempt("node-terminal-attempt", node.id) is True
    first = database.record_legacy_node_terminal(
        attempt_id="node-terminal-attempt",
        node_id=node.id,
        exit_code=0,
        log_tail="terminal evidence stays outside audit",
    )
    duplicate = database.record_legacy_node_terminal(
        attempt_id="node-terminal-attempt",
        node_id=node.id,
        exit_code=0,
        log_tail="terminal evidence stays outside audit",
    )
    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    events = database.list_durable_audit_events(limit=100)
    terminal_events = [
        event
        for event in events
        if event["action"] == "execution_terminal_recorded"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["result"] == "done"
    assert terminal_events[0]["params"] == {
        "exit_code": 0,
        "job_id": job_id,
        "node_id": node.id,
        "terminal_state": "done",
    }
    assert "terminal evidence" not in json.dumps(terminal_events)
    database.close()


def test_artifact_reports_write_one_digest_bound_summary(tmp_path):
    database = Database(str(tmp_path / "artifact-audit.db"))
    node = database.insert_node(
        node_id="node-artifact-audit",
        server_name="worker-a",
        secret_hash="1" * 64,
    )
    job_id = database.insert_job(command="echo artifacts")
    database.insert_node_attempt(
        attempt_id="node-artifact-attempt",
        job_id=job_id,
        node_id=node.id,
        command_sha256="2" * 64,
        lease_expires_at="2099-01-01T00:00:00.000Z",
    )
    assert database.ack_node_attempt("node-artifact-attempt", node.id) is True
    artifacts = [
        ("results/model.bin", 10, "a" * 64, "model"),
        ("metrics.json", 2, "b" * 64, "file"),
    ]
    assert (
        database.upsert_node_attempt_artifacts_batch(
            attempt_id="node-artifact-attempt",
            node_id=node.id,
            artifacts=artifacts,
        )
        == 2
    )
    # Reordered retries are the same report digest and do not create a second
    # durable event.
    assert (
        database.upsert_node_attempt_artifacts_batch(
            attempt_id="node-artifact-attempt",
            node_id=node.id,
            artifacts=list(reversed(artifacts)),
        )
        == 2
    )
    events = database.list_durable_audit_events(limit=100)
    artifact_events = [
        event
        for event in events
        if event["action"] == "execution_artifact_recorded"
    ]
    assert len(artifact_events) == 1
    assert artifact_events[0]["params"]["artifact_count"] == 2
    assert artifact_events[0]["params"]["artifact_kinds"] == ["file", "model"]
    serialized = json.dumps(artifact_events)
    assert "results/model.bin" not in serialized
    assert "metrics.json" not in serialized
    database.close()


def test_artifact_mutation_and_durable_summary_roll_back_together(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "artifact-rollback.db"))
    node = database.insert_node(
        node_id="node-artifact-rollback",
        server_name="worker-a",
        secret_hash="3" * 64,
    )
    job_id = database.insert_job(command="echo artifact rollback")
    database.insert_node_attempt(
        attempt_id="node-artifact-rollback-attempt",
        job_id=job_id,
        node_id=node.id,
        command_sha256="4" * 64,
        lease_expires_at="2099-01-01T00:00:00.000Z",
    )
    assert (
        database.ack_node_attempt("node-artifact-rollback-attempt", node.id)
        is True
    )

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.upsert_node_attempt_artifacts_batch(
            attempt_id="node-artifact-rollback-attempt",
            node_id=node.id,
            artifacts=[("artifact.bin", 1, "5" * 64, "file")],
        )
    assert database.list_node_attempt_artifacts("node-artifact-rollback-attempt") == []
    database.close()


def _ssh_attempt_for_artifact_audit(database: Database) -> tuple[dict, dict]:
    records = _foundation_records(database)
    attempt = database.create_execution_attempt(
        job_id=records["job_id"],
        backend="ssh",
        server_config_revision_id=records["revision"]["id"],
        leader_owner_id=records["lease"]["owner_id"],
        scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
    )
    return records, attempt


def test_ssh_artifact_reports_write_one_path_free_digest_bound_summary(tmp_path):
    database = Database(str(tmp_path / "ssh-artifact-audit.db"))
    records, attempt = _ssh_attempt_for_artifact_audit(database)
    artifacts = [
        ("results/model.bin", 10, "a" * 64, "file"),
        ("metrics.json", 2, "b" * 64, "file"),
    ]
    assert database.upsert_execution_attempt_artifacts_batch(
        attempt_id=attempt["id"], artifacts=artifacts
    ) == 2
    assert database.upsert_execution_attempt_artifacts_batch(
        attempt_id=attempt["id"], artifacts=list(reversed(artifacts))
    ) == 2
    events = [
        event
        for event in database.list_durable_audit_events(limit=100)
        if event["action"] == "execution_artifact_recorded"
        and event["resource_id"] == attempt["id"]
    ]
    assert len(events) == 1
    assert events[0]["approval_id"] == records["execution_approval_id"]
    assert events[0]["params"] == {
        "backend": "ssh",
        "artifact_count": 2,
        "artifact_kinds": ["file"],
        "report_digest": events[0]["params"]["report_digest"],
    }
    serialized = json.dumps(events)
    assert "results/model.bin" not in serialized
    assert "metrics.json" not in serialized
    assert "compute-a" not in serialized
    database.close()


def test_ssh_artifact_mutation_and_durable_summary_roll_back_together(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "ssh-artifact-rollback.db"))
    _records, attempt = _ssh_attempt_for_artifact_audit(database)

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.upsert_execution_attempt_artifacts_batch(
            attempt_id=attempt["id"],
            artifacts=[("artifact.bin", 1, "5" * 64, "file")],
        )
    assert database.list_execution_attempt_artifacts(attempt["id"]) == []
    database.close()


def test_node_terminal_and_durable_event_roll_back_together(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "node-terminal-rollback.db"))
    node = database.insert_node(
        node_id="node-terminal-rollback",
        server_name="worker-a",
        secret_hash="e" * 64,
    )
    job_id = database.insert_job(command="echo terminal rollback")
    database.insert_node_attempt(
        attempt_id="node-terminal-rollback-attempt",
        job_id=job_id,
        node_id=node.id,
        command_sha256="f" * 64,
        lease_expires_at="2099-01-01T00:00:00.000Z",
    )
    assert (
        database.ack_node_attempt("node-terminal-rollback-attempt", node.id)
        is True
    )

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit append fault")

    monkeypatch.setattr(
        database, "append_durable_audit_event_in_transaction", fail_append
    )
    with pytest.raises(RuntimeError, match="audit append fault"):
        database.record_legacy_node_terminal(
            attempt_id="node-terminal-rollback-attempt",
            node_id=node.id,
            exit_code=1,
            log_tail="failure evidence stays outside audit",
        )
    attempt = database.get_node_attempt("node-terminal-rollback-attempt")
    assert attempt is not None and attempt.terminal_at is None
    assert database.get_job(job_id).status == "queued"
    database.close()


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
    telemetry = database.get_durable_audit_export_telemetry()
    assert telemetry["by_state"] == {"dead_letter": 1}
    assert telemetry["backlog"] == 0
    assert telemetry["dead_letter"] == 1
    assert telemetry["status"] == "attention"
    assert telemetry["alert"] == {
        "active": True,
        "severity": "critical",
        "reason_codes": ["dead_letter_present"],
    }
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
        "schema_version": 16,
        "audit_hash_chain": "ok",
    }


def test_export_receipt_cas_failure_is_reported_and_retried_at_least_once(
    tmp_path, monkeypatch
):
    """A successful append with a lost receipt must remain recoverable.

    The exporter cannot safely mark a row failed after its lease has been
    fenced by another owner.  It reports the receipt failure, leaves the row
    in lease-recovery state, and a later owner may append a deliberate
    at-least-once duplicate before completing the durable operation.
    """

    database = Database(str(tmp_path / "receipt-race.db"))
    event = _append(database, "receipt-race")
    output = tmp_path / "receipt-race.jsonl"
    original_complete = database.complete_durable_audit_export

    monkeypatch.setattr(
        database,
        "complete_durable_audit_export",
        lambda *_args, **_kwargs: False,
    )
    first = export_durable_audit_events(
        database,
        output,
        owner="owner-a",
        lease_seconds=60,
        max_attempts=2,
    )
    assert first == {"claimed": 1, "exported": 0, "failed": 1, "dead_letter": 0}
    assert json.loads(output.read_text(encoding="utf-8"))["event_id"] == event[
        "event_id"
    ]
    with database.cursor() as cursor:
        state = cursor.execute(
            "SELECT state, claim_owner FROM audit_export_operations"
        ).fetchone()
    assert tuple(state) == ("processing", "owner-a")

    # Simulate lease expiry, then let a new owner recover and complete.  The
    # two lines are expected under the documented at-least-once contract.
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE audit_export_operations SET claim_expires_at = "
            "'2000-01-01T00:00:00.000Z'"
        )
    monkeypatch.setattr(database, "complete_durable_audit_export", original_complete)
    second = export_durable_audit_events(
        database,
        output,
        owner="owner-b",
        lease_seconds=60,
        max_attempts=2,
    )
    assert second == {"claimed": 1, "exported": 1, "failed": 0, "dead_letter": 0}
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [line["event_id"] for line in lines] == [event["event_id"], event["event_id"]]
    assert database.get_durable_audit_export_telemetry()["backlog"] == 0
    database.close()


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


def test_active_final_processing_lease_is_not_dead_lettered(tmp_path):
    database = Database(str(tmp_path / "active-final.db"))
    event = _append(database, "active-final")

    claimed = database.claim_durable_audit_exports(
        owner="owner-a", lease_seconds=60, max_attempts=1
    )
    assert claimed and claimed[0]["event"] == event
    operation_id = claimed[0]["operation_id"]

    # A second claimer must not consume an active final-attempt lease.
    assert database.claim_durable_audit_exports(
        owner="owner-b", lease_seconds=60, max_attempts=1
    ) == []
    with database.cursor() as cursor:
        row = cursor.execute(
            "SELECT state, claim_owner, attempt_count FROM audit_export_operations"
            " WHERE id = ?",
            (operation_id,),
        ).fetchone()
    assert tuple(row) == ("processing", "owner-a", 1)

    # The original owner can still complete while its lease is valid.
    assert database.complete_durable_audit_export(
        operation_id, owner="owner-a"
    ) is True
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT state FROM audit_export_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()[0] == "exported"
    database.close()


def test_expired_final_processing_lease_can_be_dead_lettered(tmp_path):
    database = Database(str(tmp_path / "expired-final.db"))
    _append(database, "expired-final")
    claimed = database.claim_durable_audit_exports(
        owner="owner-a", lease_seconds=60, max_attempts=1
    )
    operation_id = claimed[0]["operation_id"]
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE audit_export_operations SET claim_expires_at = "
            "'2000-01-01T00:00:00.000Z' WHERE id = ?",
            (operation_id,),
        )

    assert database.claim_durable_audit_exports(
        owner="owner-b", lease_seconds=60, max_attempts=1
    ) == []
    with database.cursor() as cursor:
        row = cursor.execute(
            "SELECT state, claim_owner, claim_expires_at FROM audit_export_operations"
            " WHERE id = ?",
            (operation_id,),
        ).fetchone()
    assert tuple(row) == ("dead_letter", None, None)
    database.close()


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


def test_audit_export_status_is_read_only_and_clear_when_outbox_is_empty(
    tmp_path, capsys
):
    database_path = tmp_path / "status-clear.db"
    database = Database(str(database_path))
    database.close()
    before = database_path.read_bytes()

    report = read_audit_export_status(database_path)

    assert report["status"] == "clear"
    assert report["backlog"] == 0
    assert report["dead_letter"] == 0
    assert audit_export_status_main(
        ["--db", str(database_path), "--require-clear", "--json"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clear"
    assert database_path.read_bytes() == before


def test_audit_export_status_gate_reports_backlog_and_dead_letter(
    tmp_path, capsys
):
    database_path = tmp_path / "status-attention.db"
    database = Database(str(database_path))
    _append(database, "status-dead-letter")
    claimed = database.claim_durable_audit_exports(owner="status-test", max_attempts=1)
    assert len(claimed) == 1
    assert (
        database.fail_durable_audit_export(
            claimed[0]["operation_id"],
            owner="status-test",
            error_category="OSError",
            max_attempts=1,
        )
        == "dead_letter"
    )
    _append(database, "status-pending")
    database.close()

    assert (
        audit_export_status_main(
            ["--db", str(database_path), "--require-clear", "--json"]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "attention"
    assert report["backlog"] == 1
    assert report["dead_letter"] == 1
    assert report["alert"] == {
        "active": True,
        "severity": "critical",
        "reason_codes": ["dead_letter_present"],
    }


def test_audit_export_status_rejects_missing_or_uninitialized_outbox(
    tmp_path, capsys
):
    database_path = tmp_path / "empty.db"
    sqlite3.connect(database_path).close()

    assert audit_export_status_main(["--db", str(database_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "outbox" in payload["error"]


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
