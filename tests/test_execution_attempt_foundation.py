import base64
import asyncio
import sqlite3
import threading

import pytest

from app.config import AppConfig, ServerConfig
from app.db import Database
from app.execution_contract import canonical_json, utf8_sha256
from app.main import AppState
from app.node_registry import enroll_node
from app.server_publication import credential_reference
from app.server_attempt_preflight import (
    ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
)


def _insert_pinned_job(
    database: Database,
    *,
    approval_id: int,
    command: str,
    payload_sha256: str,
    contract_version: str = "enqueue-execution-v1",
    role: str = "main",
    pin_server: str = "compute-a",
    require_tag: str | None = None,
) -> int:
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs
                (type, project, command, require_tag, pin_server, depends_on,
                 gpus_needed, status, priority, created_at,
                 execution_approval_id, approved_payload_sha256,
                 execution_contract_version, execution_contract_role,
                 approved_command_sha256)
            VALUES (
                'adhoc', 'demo', ?, ?, ?, '[]', NULL, 'queued', 'normal',
                '2026-07-27T00:00:00+00:00', ?, ?, ?, ?, ?
            )
            """,
            (
                command,
                require_tag,
                pin_server,
                approval_id,
                payload_sha256,
                contract_version,
                role,
                utf8_sha256(command),
            ),
        )
        return int(cursor.lastrowid)


def _job_spec(
    *,
    role: str,
    command: str,
    depends_on_roles: list[str] | None = None,
    **extra,
) -> dict:
    return {
        "role": role,
        "type": "adhoc",
        "command_utf8_b64": base64.b64encode(command.encode("utf-8")).decode(
            "ascii"
        ),
        "command_sha256": utf8_sha256(command),
        "depends_on_roles": depends_on_roles or [],
        **extra,
    }


def _foundation_records(
    database: Database,
    *,
    backend: str = "ssh",
    key_path: str = "/dispatch-test/nonexistent-key",
) -> dict:
    node_canary_tag = "node-canary" if backend == "node" else None
    normalized_target = {
        "backend": backend,
        "host": "192.0.2.20",
        "port": 22,
        "user": "worker",
        "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"],
    }
    credential_ref = credential_reference({"key": key_path})
    yaml_before_sha256 = "a" * 64
    yaml_after_sha256 = "b" * 64
    server_approval_id = database.insert_pinned_approval(
        kind="server_add",
        contract_version="server-config-v1",
        payload={
            "operation": "add",
            "server_name": "compute-a",
            "normalized_target": normalized_target,
            "credential_ref": credential_ref,
            "yaml_before_sha256": yaml_before_sha256,
            "yaml_after_sha256": yaml_after_sha256,
        },
    )
    mutation = database.prepare_server_config_mutation(
        approval_id=server_approval_id,
        operation="add",
        server_name="compute-a",
        normalized_target=normalized_target,
        credential_ref=credential_ref,
        yaml_before_sha256=yaml_before_sha256,
        yaml_after_sha256=yaml_after_sha256,
        decision_actor_id="human-reviewer",
    )
    database.transition_server_config_mutation(
        mutation_id=mutation["id"],
        expected_state="intent",
        new_state="yaml_applied",
        observed_yaml_sha256=yaml_after_sha256,
    )
    database.activate_server_config_mutation(
        mutation_id=mutation["id"],
        observed_yaml_sha256=yaml_after_sha256,
    )
    revision = mutation["prepared_revision"]
    if backend == "ssh":
        revision = database.record_server_attempt_backend_preflight(
            server_name="compute-a",
            revision_id=revision["id"],
            status="eligible",
            contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
            filesystem_type="ext2/ext3/ext4",
        )

    command = "python train.py --epochs 1"
    execution_approval_id = database.insert_pinned_approval(
        kind="enqueue",
        contract_version="enqueue-execution-v1",
        payload={
            "authorized_operations": ["prepare", "launch", "collect"],
            "job_specs": [
                {
                    "role": "main",
                    "type": "adhoc",
                    "command_utf8_b64": base64.b64encode(
                        command.encode("utf-8")
                    ).decode("ascii"),
                    "command_sha256": utf8_sha256(command),
                    **(
                        {"require_tag": node_canary_tag}
                        if node_canary_tag is not None
                        else {}
                    ),
                }
            ],
        },
    )
    execution_approval = database.get_approval(execution_approval_id)
    database.update_approval(execution_approval_id, status="approved")
    job_id = _insert_pinned_job(
        database,
        approval_id=execution_approval_id,
        command=command,
        payload_sha256=execution_approval.payload_sha256,
        require_tag=node_canary_tag,
    )
    lease = database.acquire_scheduler_lease(
        owner_id="scheduler-a",
        lease_seconds=120,
    )
    return {
        "server_approval_id": server_approval_id,
        "revision": revision,
        "execution_approval_id": execution_approval_id,
        "execution_payload_sha256": execution_approval.payload_sha256,
        "job_id": job_id,
        "lease": lease,
        "command": command,
        "node_canary_tag": node_canary_tag,
        "key_path": key_path,
    }


def _create_attempt(database: Database, records: dict, *, attempt_id: str) -> dict:
    return database.create_execution_attempt(
        job_id=records["job_id"],
        backend="ssh",
        server_config_revision_id=records["revision"]["id"],
        leader_owner_id=records["lease"]["owner_id"],
        scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        attempt_id=attempt_id,
        fencing_token=f"{attempt_id}-fence",
    )


def test_ssh_attempt_claim_refuses_missing_or_nonlocal_revision_preflight():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        database.record_server_attempt_backend_preflight(
            server_name="compute-a",
            revision_id=records["revision"]["id"],
            status="ineligible_non_local_fs",
            contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
            filesystem_type="nfs",
        )

        with pytest.raises(ValueError, match="target_preflight_ineligible"):
            _create_attempt(database, records, attempt_id="attempt-nonlocal")

        assert database.get_job(records["job_id"]).status == "queued"
        assert database.get_latest_execution_attempt_for_job(records["job_id"]) is None
    finally:
        database.close()


def test_fresh_schema_enables_foreign_keys_and_closed_domains():
    database = Database(":memory:")
    try:
        with database.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys")
            assert cursor.fetchone()[0] == 1
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            tables = {row["name"] for row in cursor.fetchall()}
            assert {
                "server_config_revisions",
                "server_config_mutations",
                "execution_attempts",
                "execution_operations",
                "execution_attempt_events",
                "execution_shadow_observations",
                "legacy_job_stop_intents",
                "scheduler_leases",
            }.issubset(tables)

            with pytest.raises(sqlite3.IntegrityError):
                cursor.execute(
                    """
                    INSERT INTO execution_attempts
                        (id, job_id, attempt_number, backend, server_name,
                         server_config_revision_id, target_identity_sha256,
                         execution_approval_id, approved_payload_sha256,
                         execution_contract_version, state, liveness,
                         fencing_token, scheduler_fencing_epoch, created_at)
                    VALUES (
                        'bad', 999, 1, 'local', 'x', 'missing', 'digest',
                        999, 'digest', 'v1', 'queued', 'maybe', 'fence', 1, 'now'
                    )
                    """
                )
    finally:
        database.close()


def test_representative_legacy_migration_preserves_null_ownership(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'adhoc',
            project TEXT,
            command TEXT NOT NULL,
            require_tag TEXT,
            pin_server TEXT,
            depends_on TEXT NOT NULL DEFAULT '[]',
            gpus_needed INTEGER,
            status TEXT NOT NULL DEFAULT 'queued',
            server TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            exit_code INTEGER,
            log_tail TEXT,
            target_server TEXT,
            dataset_name TEXT,
            dataset_version TEXT,
            log_size INTEGER,
            log_size_changed_at TEXT,
            stalled_suspect INTEGER NOT NULL DEFAULT 0,
            stall_notified INTEGER NOT NULL DEFAULT 0,
            source_coding_run_id INTEGER,
            engineering_task_id TEXT,
            engineering_task_role TEXT,
            engineering_attempt_number INTEGER,
            engineering_validation_request_id TEXT,
            auto_placement_approval_id INTEGER
        );
        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT,
            note TEXT,
            requester_actor_id TEXT,
            decision_actor_id TEXT,
            decision_mechanism TEXT
        );
        CREATE TABLE node_attempts (
            id TEXT PRIMARY KEY,
            job_id INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL,
            command_sha256 TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            acked_at TEXT,
            last_heartbeat_at TEXT,
            terminal_at TEXT,
            exit_code INTEGER,
            log_tail TEXT,
            stop_requested_at TEXT,
            stop_acked_at TEXT
        );
        INSERT INTO jobs (command, created_at) VALUES ('legacy command', 'legacy');
        INSERT INTO approvals (kind, payload, created_at)
            VALUES ('enqueue', '{"legacy":true}', 'legacy');
        INSERT INTO node_attempts
            (id, job_id, node_id, status, command_sha256, lease_expires_at,
             created_at)
            VALUES ('legacy-node', 1, 'node-a', 'leased', 'old', 'future', 'legacy');
        """
    )
    connection.commit()
    connection.close()

    database = Database(str(path))
    try:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT execution_approval_id, approved_payload_sha256,
                       execution_contract_version, execution_contract_role,
                       approved_command_sha256
                FROM jobs WHERE id = 1
                """
            )
            assert tuple(cursor.fetchone()) == (None, None, None, None, None)
            cursor.execute(
                """
                SELECT payload_sha256, payload_contract_version,
                       payload_immutable_at, materialization_started_at
                FROM approvals WHERE id = 1
                """
            )
            assert tuple(cursor.fetchone()) == (None, None, None, None)
            cursor.execute(
                "SELECT execution_attempt_id FROM node_attempts WHERE id = 'legacy-node'"
            )
            assert cursor.fetchone()[0] is None
            cursor.execute("PRAGMA foreign_keys")
            assert cursor.fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError, match="invalid node attempt status"):
                cursor.execute(
                    "UPDATE node_attempts SET status = 'invented' WHERE id = 'legacy-node'"
                )
            with pytest.raises(sqlite3.IntegrityError, match="foreign key mismatch"):
                cursor.execute(
                    """
                    INSERT INTO node_attempts
                        (id, job_id, node_id, status, command_sha256,
                         lease_expires_at, created_at)
                    VALUES ('future-orphan', 999, 'missing-node', 'leased',
                            ?, '2999-01-01T00:00:00Z', 'now')
                    """,
                    ("a" * 64,),
                )
    finally:
        database.close()


def test_pinned_contracts_and_execution_evidence_are_sql_immutable():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-immutable")
        operation = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="launch",
            payload={"launcher": "ssh-v1"},
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            authorization_approval_id=records["execution_approval_id"],
            authorized_contract_sha256=records["execution_payload_sha256"],
            operation_id="operation-immutable",
        )

        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="pinned approval"):
                cursor.execute(
                    "UPDATE approvals SET payload = '{}' WHERE id = ?",
                    (records["execution_approval_id"],),
                )
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="pinned job"):
                cursor.execute(
                    "UPDATE jobs SET command = 'tampered' WHERE id = ?",
                    (records["job_id"],),
                )
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="attempt identity"):
                cursor.execute(
                    "UPDATE execution_attempts SET server_name = 'other' WHERE id = ?",
                    (attempt["id"],),
                )
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="operation identity"):
                cursor.execute(
                    "UPDATE execution_operations SET payload_json = '{}' WHERE id = ?",
                    (operation["id"],),
                )
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                cursor.execute(
                    "UPDATE execution_attempt_events SET reason_code = 'claim_conflict'"
                )
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                cursor.execute(
                    "DELETE FROM execution_attempts WHERE id = ?",
                    (attempt["id"],),
                )

        legacy_approval_id = database.insert_approval("enqueue", {"legacy": True})
        database.update_approval(legacy_approval_id, payload={"legacy": "still mutable"})
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="pinned approval"):
                cursor.execute(
                    """
                    UPDATE approvals
                    SET payload_sha256 = 'fake',
                        payload_contract_version = 'enqueue-execution-v1',
                        payload_immutable_at = 'now'
                    WHERE id = ?
                    """,
                    (legacy_approval_id,),
                )
    finally:
        database.close()


def test_pinned_multi_job_publication_is_atomic_and_role_bound():
    database = Database(":memory:")
    try:
        approval_id = database.insert_pinned_approval(
            kind="enqueue",
            contract_version="enqueue-execution-v1",
            payload={
                "authorized_operations": ["prepare", "launch", "collect"],
                # Deliberately list the dependent Job first.  Publication must
                # topologically resolve roles without changing their identity.
                "job_specs": [
                    _job_spec(
                        role="main",
                        command="python train.py",
                        depends_on_roles=["setup"],
                        pin_server="compute-a",
                    ),
                    _job_spec(
                        role="setup",
                        command="python -m pip check",
                        pin_server="compute-a",
                    ),
                ],
            },
        )
        approval = database.get_approval(approval_id)
        jobs_by_role = database.materialize_pinned_execution_jobs(
            approval_id=approval_id,
            expected_payload_sha256=approval.payload_sha256,
            decision_actor_id="human-reviewer",
        )
        assert set(jobs_by_role) == {"setup", "main"}
        setup = database.get_job(jobs_by_role["setup"])
        main = database.get_job(jobs_by_role["main"])
        assert main.depends_on == [setup.id]
        assert setup.execution_contract_role == "setup"
        assert main.execution_contract_role == "main"
        assert setup.execution_approval_id == approval_id
        assert main.execution_approval_id == approval_id
        assert setup.approved_payload_sha256 == approval.payload_sha256
        assert main.approved_command_sha256 == utf8_sha256("python train.py")
        decided = database.get_approval(approval_id)
        assert decided.status == "approved"
        assert decided.materialization_started_at is not None
        assert decided.decision_actor_id == "human-reviewer"
    finally:
        database.close()


def test_pinned_multi_job_mid_publication_failure_leaves_no_partial_chain():
    database = Database(":memory:")
    try:
        approval_id = database.insert_pinned_approval(
            kind="enqueue",
            contract_version="enqueue-execution-v1",
            payload={
                "authorized_operations": ["launch"],
                "job_specs": [
                    _job_spec(role="setup", command="echo setup"),
                    _job_spec(
                        role="main",
                        command="echo main",
                        depends_on_roles=["setup"],
                        # Shape validation occurs while publishing this second
                        # row, after the first INSERT, to exercise rollback.
                        gpus_needed=-1,
                    ),
                ],
            },
        )
        approval = database.get_approval(approval_id)
        with pytest.raises(ValueError, match="gpus_needed"):
            database.materialize_pinned_execution_jobs(
                approval_id=approval_id,
                expected_payload_sha256=approval.payload_sha256,
                decision_actor_id="human-reviewer",
            )
        assert database.list_jobs() == []
        unchanged = database.get_approval(approval_id)
        assert unchanged.status == "pending"
        assert unchanged.materialization_started_at is None
        assert unchanged.decision_actor_id is None
    finally:
        database.close()


def test_two_connections_materialize_one_pinned_job_graph_once(tmp_path):
    path = tmp_path / "materialization-race.db"
    setup_database = Database(str(path))
    approval_id = setup_database.insert_pinned_approval(
        kind="enqueue",
        contract_version="enqueue-execution-v1",
        payload={
            "authorized_operations": ["launch"],
            "job_specs": [
                _job_spec(role="setup", command="echo setup"),
                _job_spec(
                    role="main",
                    command="echo main",
                    depends_on_roles=["setup"],
                ),
            ],
        },
    )
    approval = setup_database.get_approval(approval_id)
    contender_a = Database(str(path))
    contender_b = Database(str(path))
    barrier = threading.Barrier(2)
    outcomes = []

    def materialize(database: Database) -> None:
        barrier.wait()
        try:
            result = database.materialize_pinned_execution_jobs(
                approval_id=approval_id,
                expected_payload_sha256=approval.payload_sha256,
                decision_actor_id="human-reviewer",
            )
        except ValueError as exc:
            outcomes.append(("lost", str(exc)))
        else:
            outcomes.append(("won", result))

    thread_a = threading.Thread(target=materialize, args=(contender_a,))
    thread_b = threading.Thread(target=materialize, args=(contender_b,))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    try:
        assert [status for status, _ in outcomes].count("won") == 1
        assert [status for status, _ in outcomes].count("lost") == 1
        jobs = setup_database.list_jobs()
        assert len(jobs) == 2
        assert {job.execution_contract_role for job in jobs} == {"setup", "main"}
        assert setup_database.get_approval(approval_id).status == "approved"
    finally:
        contender_a.close()
        contender_b.close()
        setup_database.close()


def test_server_publication_drift_holds_and_exact_after_digest_resumes():
    database = Database(":memory:")
    target = {
        "backend": "ssh",
        "host": "192.0.2.30",
        "port": 22,
        "user": "worker",
        "project_roots": ["/srv/projects"],
        "dataset_roots": [],
    }
    credential = {
        "provider": "ssh-key-file-v1",
        "version_id": "key-v1",
        "real_path": "/tmp/test-key",
        "file_identity": {
            "device": 1,
            "inode": 2,
            "size": 3,
            "mtime_ns": 4,
        },
    }
    before_digest = "1" * 64
    after_digest = "2" * 64
    try:
        approval_id = database.insert_pinned_approval(
            kind="server_add",
            contract_version="server-config-v1",
            payload={
                "operation": "add",
                "server_name": "compute-drift",
                "normalized_target": target,
                "credential_ref": credential,
                "yaml_before_sha256": before_digest,
                "yaml_after_sha256": after_digest,
            },
        )
        mutation = database.prepare_server_config_mutation(
            approval_id=approval_id,
            operation="add",
            server_name="compute-drift",
            normalized_target=target,
            credential_ref=credential,
            yaml_before_sha256=before_digest,
            yaml_after_sha256=after_digest,
            decision_actor_id="human-reviewer",
        )
        held = database.transition_server_config_mutation(
            mutation_id=mutation["id"],
            expected_state="intent",
            new_state="recovery_hold",
            observed_yaml_sha256="3" * 64,
            last_error_category="yaml_digest_drift",
        )
        assert held["state"] == "recovery_hold"
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT publication_state FROM server_config_revisions
                WHERE id = ?
                """,
                (mutation["prepared_revision_id"],),
            )
            assert cursor.fetchone()[0] == "prepared"

        resumed = database.transition_server_config_mutation(
            mutation_id=mutation["id"],
            expected_state="recovery_hold",
            new_state="yaml_applied",
            observed_yaml_sha256=after_digest,
        )
        assert resumed["state"] == "yaml_applied"
        activated = database.activate_server_config_mutation(
            mutation_id=mutation["id"],
            observed_yaml_sha256=after_digest,
        )
        assert activated["state"] == "activated"
        assert database.get_approval(approval_id).status == "approved"
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT publication_state FROM server_config_revisions
                WHERE id = ?
                """,
                (mutation["prepared_revision_id"],),
            )
            assert cursor.fetchone()[0] == "active"
    finally:
        database.close()


def test_server_publication_compensates_only_to_exact_before_digest():
    database = Database(":memory:")
    target = {
        "backend": "ssh",
        "host": "192.0.2.31",
        "port": 22,
        "user": "worker",
        "project_roots": [],
        "dataset_roots": [],
    }
    credential = {
        "provider": "ssh-key-file-v1",
        "version_id": "key-v1",
        "real_path": "/tmp/test-key",
        "file_identity": {
            "device": 1,
            "inode": 2,
            "size": 3,
            "mtime_ns": 4,
        },
    }
    before_digest = "4" * 64
    after_digest = "5" * 64
    try:
        approval_id = database.insert_pinned_approval(
            kind="server_add",
            contract_version="server-config-v1",
            payload={
                "operation": "add",
                "server_name": "compute-rollback",
                "normalized_target": target,
                "credential_ref": credential,
                "yaml_before_sha256": before_digest,
                "yaml_after_sha256": after_digest,
            },
        )
        mutation = database.prepare_server_config_mutation(
            approval_id=approval_id,
            operation="add",
            server_name="compute-rollback",
            normalized_target=target,
            credential_ref=credential,
            yaml_before_sha256=before_digest,
            yaml_after_sha256=after_digest,
            decision_actor_id="human-reviewer",
        )
        with pytest.raises(ValueError, match="target_identity_mismatch"):
            database.transition_server_config_mutation(
                mutation_id=mutation["id"],
                expected_state="intent",
                new_state="rolled_back",
                observed_yaml_sha256="6" * 64,
            )
        rolled_back = database.transition_server_config_mutation(
            mutation_id=mutation["id"],
            expected_state="intent",
            new_state="rolled_back",
            observed_yaml_sha256=before_digest,
        )
        assert rolled_back["state"] == "rolled_back"
        assert database.get_approval(approval_id).status == "rejected"
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT publication_state FROM server_config_revisions
                WHERE id = ?
                """,
                (mutation["prepared_revision_id"],),
            )
            assert cursor.fetchone()[0] == "retired"
    finally:
        database.close()


def test_two_connections_racing_create_exactly_one_active_attempt(tmp_path):
    path = tmp_path / "race.db"
    setup_database = Database(str(path))
    records = _foundation_records(setup_database)
    contender_a = Database(str(path))
    contender_b = Database(str(path))
    barrier = threading.Barrier(2)
    outcomes = []

    def contend(database: Database, attempt_id: str) -> None:
        barrier.wait()
        try:
            _create_attempt(database, records, attempt_id=attempt_id)
        except (sqlite3.IntegrityError, ValueError) as exc:
            outcomes.append(("lost", str(exc)))
        else:
            outcomes.append(("won", attempt_id))

    thread_a = threading.Thread(
        target=contend,
        args=(contender_a, "attempt-race-a"),
    )
    thread_b = threading.Thread(
        target=contend,
        args=(contender_b, "attempt-race-b"),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    try:
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert [result for result, _ in outcomes].count("won") == 1
        assert [result for result, _ in outcomes].count("lost") == 1
        with setup_database.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM execution_attempts
                WHERE job_id = ? AND state IN ('leased', 'dispatching', 'running')
                """,
                (records["job_id"],),
            )
            assert cursor.fetchone()[0] == 1
    finally:
        contender_a.close()
        contender_b.close()
        setup_database.close()


def test_partial_unique_index_rejects_second_active_attempt_via_raw_sql():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-index-a")
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError):
                cursor.execute(
                    """
                    INSERT INTO execution_attempts
                        (id, job_id, attempt_number, backend, server_name,
                         server_config_revision_id, target_identity_sha256,
                         execution_approval_id, approved_payload_sha256,
                         execution_contract_version, state, liveness,
                         fencing_token, scheduler_fencing_epoch, created_at)
                    VALUES (?, ?, 2, 'ssh', ?, ?, ?, ?, ?, ?,
                            'dispatching', 'known', ?, ?, ?)
                    """,
                    (
                        "attempt-index-b",
                        records["job_id"],
                        attempt["server_name"],
                        attempt["server_config_revision_id"],
                        attempt["target_identity_sha256"],
                        attempt["execution_approval_id"],
                        attempt["approved_payload_sha256"],
                        attempt["execution_contract_version"],
                        "attempt-index-b-fence",
                        records["lease"]["fencing_epoch"],
                        "2026-07-27T00:00:00+00:00",
                    ),
                )
    finally:
        database.close()


def test_new_live_leader_can_reconcile_but_stale_leader_is_fenced():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-failover")
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduler_leases
                SET lease_expires_at = '2000-01-01T00:00:00.000Z'
                WHERE name = 'execution-attempt-v1'
                """
            )
        new_lease = database.acquire_scheduler_lease(
            owner_id="scheduler-b",
            lease_seconds=120,
        )
        assert new_lease["fencing_epoch"] == records["lease"]["fencing_epoch"] + 1

        with pytest.raises(ValueError, match="leader_lease_lost"):
            database.transition_execution_attempt(
                attempt_id=attempt["id"],
                expected_state="dispatching",
                expected_liveness="known",
                new_liveness="unknown",
                leader_owner_id="scheduler-a",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
                reason_code="remote_unreachable",
                evidence={"transport": "timeout"},
            )

        reconciled = database.transition_execution_attempt(
            attempt_id=attempt["id"],
            expected_state="dispatching",
            expected_liveness="known",
            new_liveness="unknown",
            leader_owner_id="scheduler-b",
            scheduler_fencing_epoch=new_lease["fencing_epoch"],
            reason_code="remote_unreachable",
            evidence={"transport": "timeout"},
        )
        assert reconciled["liveness"] == "unknown"
        assert (
            reconciled["scheduler_fencing_epoch"]
            == records["lease"]["fencing_epoch"]
        )
    finally:
        database.close()


def test_outbox_reclaims_only_before_effect_boundary():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-outbox")
        operation = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="launch",
            payload={"launcher": "ssh-v1"},
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            authorization_approval_id=records["execution_approval_id"],
            authorized_contract_sha256=records["execution_payload_sha256"],
            operation_id="operation-outbox",
        )
        first_claim = database.claim_execution_operation(
            operation_id=operation["id"],
            claim_owner="worker-a",
            claim_seconds=60,
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert first_claim["attempt_count"] == 1
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE execution_operations
                SET claim_expires_at = '2000-01-01T00:00:00.000Z'
                WHERE id = ?
                """,
                (operation["id"],),
            )
        second_claim = database.claim_execution_operation(
            operation_id=operation["id"],
            claim_owner="worker-b",
            claim_seconds=60,
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert second_claim["attempt_count"] == 2
        assert second_claim["claim_owner"] == "worker-b"
        assert database.mark_execution_operation_effect_started(
            operation_id=operation["id"],
            claim_owner="worker-b",
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE execution_operations
                SET claim_expires_at = '2000-01-01T00:00:00.000Z'
                WHERE id = ?
                """,
                (operation["id"],),
            )
        assert (
            database.claim_execution_operation(
                operation_id=operation["id"],
                claim_owner="worker-c",
                claim_seconds=60,
                leader_owner_id="scheduler-a",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            )
            is None
        )
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT state, attempt_count FROM execution_operations WHERE id = ?",
                (operation["id"],),
            )
            row = cursor.fetchone()
            assert row["state"] == "uncertain"
            assert row["attempt_count"] == 2
    finally:
        database.close()


def test_operation_authorization_class_cannot_reuse_execution_for_stop():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-authz")
        with pytest.raises(ValueError, match="approval_kind_mismatch"):
            database.insert_execution_operation(
                attempt_id=attempt["id"],
                operation="stop",
                payload={"attempt_id": attempt["id"]},
                leader_owner_id="scheduler-a",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
                authorization_approval_id=records["execution_approval_id"],
                authorized_contract_sha256=records["execution_payload_sha256"],
            )
        # WP-1A rejected every inspect operation because no allowlist existed.
        # WP-2B pinned one, so the rejection reason moved from "not implemented"
        # to "not in the allowlist" — an arbitrary payload must still fail
        # closed, because an inspect operation carries no mutation approval.
        with pytest.raises(ValueError, match="not in the pinned allowlist"):
            database.insert_execution_operation(
                attempt_id=attempt["id"],
                operation="inspect",
                payload={"command": "arbitrary shell"},
                leader_owner_id="scheduler-a",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            )
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE attempt_id = ?",
                (attempt["id"],),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        database.close()


def test_server_blockers_include_pending_operation_after_workload_terminal():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-blocker")
        operation = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="collect",
            payload={"collector": "result-v1"},
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            authorization_approval_id=records["execution_approval_id"],
            authorized_contract_sha256=records["execution_payload_sha256"],
            operation_id="operation-blocker",
        )
        database.transition_execution_attempt(
            attempt_id=attempt["id"],
            expected_state="dispatching",
            expected_liveness="known",
            new_state="done",
            exit_code=0,
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            reason_code="terminal_evidence_valid",
            evidence={"sentinel_sha256": "9" * 64, "exit_code": 0},
        )
        blockers = database.get_server_execution_blockers("compute-a")
        assert blockers["generic_attempt_ids"] == []
        assert blockers["running_job_ids"] == []
        assert blockers["operation_ids"] == [operation["id"]]
        assert database.server_has_execution_blockers("compute-a") is True
    finally:
        database.close()


def test_server_blockers_keep_unlinked_acknowledged_legacy_node_after_revoke():
    database = Database(":memory:")
    try:
        database.insert_node(
            node_id="legacy-node",
            server_name="compute-legacy",
            secret_hash="secret-digest",
        )
        job_id = database.insert_job(command="echo legacy")
        database.insert_node_attempt(
            attempt_id="legacy-attempt",
            job_id=job_id,
            node_id="legacy-node",
            command_sha256=utf8_sha256("echo legacy"),
            lease_expires_at="2099-01-01T00:00:00+00:00",
        )
        assert database.ack_node_attempt("legacy-attempt", "legacy-node")
        database.revoke_node("legacy-node")

        blockers = database.get_server_execution_blockers("compute-legacy")
        assert blockers["legacy_node_attempt_ids"] == ["legacy-attempt"]
        assert blockers["enrolled_node_ids"] == []
    finally:
        database.close()


def test_security_revoke_atomically_holds_node_attempt_without_job_projection():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        other = enroll_node(database, server_name="compute-b")
        attempt = database.create_execution_attempt(
            job_id=records["job_id"],
            backend="node",
            server_config_revision_id=records["revision"]["id"],
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            lease_expires_at="2099-01-01T00:00:00Z",
            node_id=enrolled.node.id,
            node_attempt_id="node-attempt-security-revoke",
            attempt_id="execution-attempt-security-revoke",
            fencing_token="security-revoke-fence",
            node_canary_tag=records["node_canary_tag"],
            node_server_enabled=True,
            node_server_tags=(records["node_canary_tag"],),
        )
        operation = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="launch",
            payload={"launcher": "node-v2"},
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            authorization_approval_id=records["execution_approval_id"],
            authorized_contract_sha256=records["execution_payload_sha256"],
            operation_id="operation-before-security-revoke",
        )
        before_job = database.get_job(records["job_id"])

        result = database.revoke_node_with_execution_hold(enrolled.node.id)

        assert result is not None
        assert result["execution_attempt_ids"] == [attempt["id"]]
        assert result["node_attempt_ids"] == ["node-attempt-security-revoke"]
        assert result["legacy_node_attempt_ids"] == []
        assert result["already_revoked"] is False
        assert result["node"].is_active is False
        assert database.get_node(other.node.id).is_active is True

        held = database.get_execution_attempt(attempt["id"])
        assert held["state"] == "leased"
        assert held["liveness"] == "unknown"
        assert held["recovery_hold_reason"] == "security_credential_revoked"
        node_attempt = database.get_node_attempt("node-attempt-security-revoke")
        assert node_attempt.status == "leased"
        assert node_attempt.terminal_at is None

        after_job = database.get_job(records["job_id"])
        assert after_job.status == before_job.status
        assert after_job.server == before_job.server
        assert after_job.finished_at == before_job.finished_at
        assert after_job.exit_code == before_job.exit_code

        assert database.list_execution_operations_for_worker() == []
        assert (
            database.claim_execution_operation(
                operation_id=operation["id"],
                claim_owner="worker-after-revoke",
                claim_seconds=60,
                leader_owner_id="scheduler-a",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            )
            is None
        )
        with pytest.raises(ValueError, match="recovery_hold"):
            database.insert_execution_operation(
                attempt_id=attempt["id"],
                operation="collect",
                payload={"collector": "result-v1"},
                leader_owner_id="scheduler-a",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
                authorization_approval_id=records["execution_approval_id"],
                authorized_contract_sha256=records["execution_payload_sha256"],
            )
        # The only operation admissible during a security hold is the already
        # pinned read-only exact-target inspect path.
        inspect = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="inspect",
            payload={"command_kind": "attempt_inspect"},
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert [row["id"] for row in database.list_execution_operations_for_worker()] == [
            inspect["id"]
        ]

        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT reason_code, from_state, to_state,
                       from_liveness, to_liveness, evidence_json
                FROM execution_attempt_events
                WHERE attempt_id = ? AND event_type = 'security_credential_revoked'
                """,
                (attempt["id"],),
            )
            event = cursor.fetchone()
        assert event["reason_code"] == "security_credential_revoked"
        assert event["from_state"] == event["to_state"] == "leased"
        assert event["from_liveness"] == "known"
        assert event["to_liveness"] == "unknown"
        assert enrolled.raw_token not in event["evidence_json"]
    finally:
        database.close()


def _node_claim(
    database: Database,
    records: dict,
    *,
    node_id: str,
    node_attempt_id: str,
    attempt_id: str,
    lease_expires_at: str = "2099-01-01T00:00:00Z",
    server_enabled: bool = True,
    server_tags: tuple[str, ...] = ("node-canary",),
) -> dict:
    return database.create_execution_attempt(
        job_id=records["job_id"],
        backend="node",
        server_config_revision_id=records["revision"]["id"],
        leader_owner_id=records["lease"]["owner_id"],
        scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        lease_expires_at=lease_expires_at,
        node_id=node_id,
        node_attempt_id=node_attempt_id,
        attempt_id=attempt_id,
        fencing_token=f"{attempt_id}-fence",
        node_canary_tag=records["node_canary_tag"],
        node_server_enabled=server_enabled,
        node_server_tags=server_tags,
    )


def test_linked_node_ack_heartbeat_terminal_is_one_atomic_state_machine():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        execution = _node_claim(
            database,
            records,
            node_id=enrolled.node.id,
            node_attempt_id="node-attempt-round-trip",
            attempt_id="execution-attempt-round-trip",
        )

        assert execution["state"] == "leased"
        assert database.get_job(records["job_id"]).status == "queued"

        ack = database.acknowledge_node_execution_attempt(
            node_attempt_id="node-attempt-round-trip",
            node_id=enrolled.node.id,
            command_sha256=utf8_sha256(records["command"]),
        )
        assert ack["duplicate"] is False
        assert database.get_execution_attempt(execution["id"])["state"] == "dispatching"
        job = database.get_job(records["job_id"])
        assert job.status == "running"
        assert job.server == "compute-a"

        duplicate_ack = database.acknowledge_node_execution_attempt(
            node_attempt_id="node-attempt-round-trip",
            node_id=enrolled.node.id,
            command_sha256=utf8_sha256(records["command"]),
        )
        assert duplicate_ack["duplicate"] is True

        heartbeat = database.observe_node_execution_heartbeat(
            node_attempt_id="node-attempt-round-trip",
            node_id=enrolled.node.id,
        )
        assert heartbeat == {
            "observed": True,
            "transitioned": True,
            "execution_attempt_id": execution["id"],
        }
        assert database.get_execution_attempt(execution["id"])["state"] == "running"
        assert database.get_node_attempt("node-attempt-round-trip").status == "running"

        assert database.request_node_attempt_stop("node-attempt-round-trip") is True
        stopped = database.get_execution_attempt(execution["id"])
        assert stopped["stop_requested_at"] is not None
        assert (
            database.ack_node_attempt_stop(
                "node-attempt-round-trip", enrolled.node.id
            )
            is True
        )
        stopped = database.get_execution_attempt(execution["id"])
        assert stopped["stop_acknowledged_at"] is not None

        terminal = database.record_node_execution_terminal(
            node_attempt_id="node-attempt-round-trip",
            node_id=enrolled.node.id,
            exit_code=0,
            log_tail="ok",
        )
        assert terminal["duplicate"] is False
        assert database.get_execution_attempt(execution["id"])["state"] == "done"
        node_attempt = database.get_node_attempt("node-attempt-round-trip")
        assert node_attempt.status == "done"
        assert node_attempt.exit_code == 0
        job = database.get_job(records["job_id"])
        assert job.status == "done"
        assert job.exit_code == 0
        assert job.log_tail == "ok"
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT operation, state, idempotency_key
                FROM execution_operations WHERE attempt_id = ?
                """,
                (execution["id"],),
            )
            collection = [dict(row) for row in cursor.fetchall()]
        assert len(collection) == 1
        assert collection[0]["operation"] == "collect"
        assert collection[0]["state"] == "pending"
        assert collection[0]["idempotency_key"].startswith(
            f"collect:{execution['id']}:"
        )
        completion = database.list_execution_completion_operations(
            attempt_id=execution["id"]
        )
        assert [row["operation"] for row in completion] == [
            "dependency_refresh",
            "notification",
            "owner_projection",
            "result_collection",
        ]
        assert {row["state"] for row in completion} == {"pending"}
        assert len({row["idempotency_key"] for row in completion}) == 4

        duplicate_terminal = database.record_node_execution_terminal(
            node_attempt_id="node-attempt-round-trip",
            node_id=enrolled.node.id,
            exit_code=0,
            log_tail="ok",
        )
        assert duplicate_terminal["duplicate"] is True
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM execution_operations WHERE attempt_id = ?",
                (execution["id"],),
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                """
                SELECT COUNT(*) FROM execution_completion_operations
                WHERE attempt_id = ?
                """,
                (execution["id"],),
            )
            assert cursor.fetchone()[0] == 4
        with pytest.raises(ValueError, match="terminal conflicts"):
            database.record_node_execution_terminal(
                node_attempt_id="node-attempt-round-trip",
                node_id=enrolled.node.id,
                exit_code=9,
                log_tail="different",
            )
        assert database.get_job(records["job_id"]).status == "done"
        assert database.get_job(records["job_id"]).exit_code == 0
    finally:
        database.close()


def test_node_terminal_completion_bundle_is_fenced_claimed_and_append_only():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        execution = _node_claim(
            database,
            records,
            node_id=enrolled.node.id,
            node_attempt_id="node-attempt-completion-bundle",
            attempt_id="execution-attempt-completion-bundle",
        )
        database.acknowledge_node_execution_attempt(
            node_attempt_id="node-attempt-completion-bundle",
            node_id=enrolled.node.id,
            command_sha256=utf8_sha256(records["command"]),
        )
        database.record_node_execution_terminal(
            node_attempt_id="node-attempt-completion-bundle",
            node_id=enrolled.node.id,
            exit_code=0,
            log_tail="ok",
        )

        with pytest.raises(ValueError, match="leader_lease_lost"):
            database.claim_execution_completion_bundle(
                attempt_id=execution["id"],
                claim_owner="stale-owner",
                claim_seconds=60,
                leader_owner_id="stale-owner",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            )
        assert {
            row["state"]
            for row in database.list_execution_completion_operations(
                attempt_id=execution["id"]
            )
        } == {"pending"}

        claim = database.claim_execution_completion_bundle(
            attempt_id=execution["id"],
            claim_owner=records["lease"]["owner_id"],
            claim_seconds=60,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert claim["attempt_id"] == execution["id"]
        assert len(claim["operations"]) == 4
        assert database.claim_execution_completion_bundle(
            attempt_id=execution["id"],
            claim_owner=records["lease"]["owner_id"],
            claim_seconds=60,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        ) is None

        names = {row["operation"] for row in claim["operations"]}
        completed = database.complete_execution_completion_bundle(
            attempt_id=execution["id"],
            claim_owner=records["lease"]["owner_id"],
            outcomes={
                name: {
                    "state": "delivered",
                    "evidence": {"job_id": records["job_id"], "ok": True},
                }
                for name in names
            },
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert {row["state"] for row in completed} == {"delivered"}
        assert database.claim_execution_completion_bundle(
            attempt_id=execution["id"],
            claim_owner=records["lease"]["owner_id"],
            claim_seconds=60,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        ) is None

        with database.cursor() as cursor:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="completion operation identity is immutable",
            ):
                cursor.execute(
                    """
                    UPDATE execution_completion_operations
                    SET payload_json = '{}'
                    WHERE attempt_id = ?
                    """,
                    (execution["id"],),
                )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="completion operations are append-only",
            ):
                cursor.execute(
                    """
                    DELETE FROM execution_completion_operations
                    WHERE attempt_id = ?
                    """,
                    (execution["id"],),
                )
    finally:
        database.close()


def test_pending_node_completion_bundle_survives_database_restart(tmp_path):
    path = tmp_path / "completion-restart.db"
    database = Database(str(path))
    records = _foundation_records(database, backend="node")
    enrolled = enroll_node(database, server_name="compute-a")
    execution = _node_claim(
        database,
        records,
        node_id=enrolled.node.id,
        node_attempt_id="node-attempt-completion-restart",
        attempt_id="execution-attempt-completion-restart",
    )
    database.acknowledge_node_execution_attempt(
        node_attempt_id="node-attempt-completion-restart",
        node_id=enrolled.node.id,
        command_sha256=utf8_sha256(records["command"]),
    )
    database.record_node_execution_terminal(
        node_attempt_id="node-attempt-completion-restart",
        node_id=enrolled.node.id,
        exit_code=0,
        log_tail="ok",
    )
    database.close()

    reopened = Database(str(path))
    try:
        lease = reopened.acquire_scheduler_lease(
            owner_id="scheduler-a",
            lease_seconds=120,
        )
        claim = reopened.claim_execution_completion_bundle(
            claim_owner="scheduler-a",
            claim_seconds=60,
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
        assert claim is not None
        assert claim["attempt_id"] == execution["id"]
        assert len(claim["operations"]) == 4
    finally:
        reopened.close()


def test_app_state_executes_and_records_durable_node_completion(
    api_client, monkeypatch
):
    _client, main_module = api_client
    state = main_module.app_state
    records = _foundation_records(state.db, backend="node")
    enrolled = enroll_node(state.db, server_name="compute-a")
    execution = _node_claim(
        state.db,
        records,
        node_id=enrolled.node.id,
        node_attempt_id="node-attempt-completion-worker",
        attempt_id="execution-attempt-completion-worker",
    )
    state.db.acknowledge_node_execution_attempt(
        node_attempt_id="node-attempt-completion-worker",
        node_id=enrolled.node.id,
        command_sha256=utf8_sha256(records["command"]),
    )
    state.db.record_node_execution_terminal(
        node_attempt_id="node-attempt-completion-worker",
        node_id=enrolled.node.id,
        exit_code=0,
        log_tail="ok",
    )
    state.config.execution_outbox_worker_enabled = True
    state.execution_scheduler_owner_id = records["lease"]["owner_id"]
    state._execution_scheduler_is_leader = True
    state._execution_scheduler_fencing_epoch = records["lease"]["fencing_epoch"]

    calls = []

    async def fake_handle(job, **kwargs):
        calls.append((job.id, kwargs["server_cfg"]))
        return {
            "result_collection_required": True,
            "result_collection_ok": True,
            "result_path_available": True,
            "notification_attempted": True,
            "mailed": False,
            "owner_projection_attempted": False,
        }

    monkeypatch.setattr(main_module, "handle_job_finished", fake_handle)

    async def run_completion():
        assert state.schedule_durable_job_completion(
            attempt_id=execution["id"],
            job_id=records["job_id"],
        )
        await asyncio.gather(*tuple(state._background_tasks))

    asyncio.run(run_completion())
    assert calls == [(records["job_id"], None)]
    completed = state.db.list_execution_completion_operations(
        attempt_id=execution["id"]
    )
    assert {row["state"] for row in completed} == {"delivered"}
    notification = next(
        row for row in completed if row["operation"] == "notification"
    )
    assert '"mailed":false' in notification["output_json"]


def test_stale_linked_node_heartbeat_becomes_unknown_without_job_projection():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        execution = _node_claim(
            database,
            records,
            node_id=enrolled.node.id,
            node_attempt_id="node-attempt-stale-heartbeat",
            attempt_id="execution-attempt-stale-heartbeat",
        )
        database.acknowledge_node_execution_attempt(
            node_attempt_id="node-attempt-stale-heartbeat",
            node_id=enrolled.node.id,
            command_sha256=utf8_sha256(records["command"]),
        )
        database.observe_node_execution_heartbeat(
            node_attempt_id="node-attempt-stale-heartbeat",
            node_id=enrolled.node.id,
        )
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE node_attempts
                SET last_heartbeat_at = '2000-01-01T00:00:00Z'
                WHERE id = 'node-attempt-stale-heartbeat'
                """
            )

        changed = database.mark_stale_node_execution_attempts_unknown(
            heartbeat_ttl_sec=60,
            heartbeat_grace_sec=30,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert changed == [execution["id"]]
        stale = database.get_execution_attempt(execution["id"])
        assert stale["state"] == "running"
        assert stale["liveness"] == "unknown"
        assert stale["recovery_hold_reason"] is None
        job = database.get_job(records["job_id"])
        assert job.status == "running"
        assert job.server == "compute-a"
        assert database.get_node_attempt(
            "node-attempt-stale-heartbeat"
        ).status == "running"

        # An unchanged second sweep is idempotent and creates no extra event.
        assert database.mark_stale_node_execution_attempts_unknown(
            heartbeat_ttl_sec=60,
            heartbeat_grace_sec=30,
            leader_owner_id=records["lease"]["owner_id"],
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        ) == []
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM execution_attempt_events
                WHERE attempt_id = ? AND reason_code = 'remote_unreachable'
                """,
                (execution["id"],),
            )
            assert cursor.fetchone()[0] == 1

        recovered = database.observe_node_execution_heartbeat(
            node_attempt_id="node-attempt-stale-heartbeat",
            node_id=enrolled.node.id,
        )
        assert recovered["observed"] is True
        assert recovered["transitioned"] is True
        assert database.get_execution_attempt(execution["id"])["liveness"] == "known"
        assert database.get_job(records["job_id"]).status == "running"
    finally:
        database.close()


def test_stale_node_liveness_sweep_requires_current_scheduler_fence():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        execution = _node_claim(
            database,
            records,
            node_id=enrolled.node.id,
            node_attempt_id="node-attempt-stale-fenced",
            attempt_id="execution-attempt-stale-fenced",
        )
        database.acknowledge_node_execution_attempt(
            node_attempt_id="node-attempt-stale-fenced",
            node_id=enrolled.node.id,
            command_sha256=utf8_sha256(records["command"]),
        )
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE node_attempts
                SET last_heartbeat_at = '2000-01-01T00:00:00Z'
                WHERE id = 'node-attempt-stale-fenced'
                """
            )

        with pytest.raises(ValueError, match="leader_lease_lost"):
            database.mark_stale_node_execution_attempts_unknown(
                heartbeat_ttl_sec=60,
                heartbeat_grace_sec=0,
                leader_owner_id="stale-scheduler",
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            )
        assert database.get_execution_attempt(execution["id"])["liveness"] == "known"
        assert database.get_job(records["job_id"]).status == "running"
    finally:
        database.close()


def test_linked_node_ack_rejects_cross_node_digest_and_expiry_without_projection():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        other = enroll_node(database, server_name="compute-a")
        execution = _node_claim(
            database,
            records,
            node_id=enrolled.node.id,
            node_attempt_id="node-attempt-ack-guards",
            attempt_id="execution-attempt-ack-guards",
        )

        with pytest.raises(ValueError, match="another node"):
            database.acknowledge_node_execution_attempt(
                node_attempt_id="node-attempt-ack-guards",
                node_id=other.node.id,
                command_sha256=utf8_sha256(records["command"]),
            )
        with pytest.raises(ValueError, match="digest mismatch"):
            database.acknowledge_node_execution_attempt(
                node_attempt_id="node-attempt-ack-guards",
                node_id=enrolled.node.id,
                command_sha256="0" * 64,
            )
        assert database.get_execution_attempt(execution["id"])["state"] == "leased"
        assert database.get_job(records["job_id"]).status == "queued"

        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE execution_attempts SET lease_expires_at = ?
                WHERE id = ?
                """,
                ("2000-01-01T00:00:00Z", execution["id"]),
            )
            cursor.execute(
                """
                UPDATE node_attempts SET lease_expires_at = ?
                WHERE id = ?
                """,
                ("2000-01-01T00:00:00Z", "node-attempt-ack-guards"),
            )
        with pytest.raises(ValueError, match="lease expired"):
            database.acknowledge_node_execution_attempt(
                node_attempt_id="node-attempt-ack-guards",
                node_id=enrolled.node.id,
                command_sha256=utf8_sha256(records["command"]),
            )
        assert database.get_execution_attempt(execution["id"])["state"] == "leased"
        assert database.get_node_attempt("node-attempt-ack-guards").acked_at is None
        assert database.get_job(records["job_id"]).status == "queued"
    finally:
        database.close()


def test_unacknowledged_expired_node_lease_is_released_before_a_new_claim():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        first_node = enroll_node(database, server_name="compute-a")
        second_node = enroll_node(database, server_name="compute-a")
        first = _node_claim(
            database,
            records,
            node_id=first_node.node.id,
            node_attempt_id="node-attempt-expired",
            attempt_id="execution-attempt-expired",
        )
        with database.cursor() as cursor:
            cursor.execute(
                "UPDATE execution_attempts SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", first["id"]),
            )
            cursor.execute(
                "UPDATE node_attempts SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", "node-attempt-expired"),
            )

        second = _node_claim(
            database,
            records,
            node_id=second_node.node.id,
            node_attempt_id="node-attempt-replacement",
            attempt_id="execution-attempt-replacement",
        )
        expired_execution = database.get_execution_attempt(first["id"])
        expired_node = database.get_node_attempt("node-attempt-expired")
        assert expired_execution["state"] == "expired"
        assert expired_node.status == "expired"
        assert expired_node.terminal_at is not None
        assert second["state"] == "leased"
        assert database.get_job(records["job_id"]).status == "queued"
        assert [
            attempt["id"] for attempt in database.list_active_execution_attempts()
        ] == [second["id"]]
    finally:
        database.close()


@pytest.mark.parametrize(
    "server_enabled,server_tags,error",
    [
        (False, ("node-canary",), "exact eligibility"),
        (True, ("ordinary",), "node_canary_ineligible"),
    ],
)
def test_node_claim_revalidates_server_and_canary_without_partial_rows(
    server_enabled, server_tags, error
):
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")
        with pytest.raises(ValueError, match=error):
            _node_claim(
                database,
                records,
                node_id=enrolled.node.id,
                node_attempt_id="node-attempt-refused",
                attempt_id="execution-attempt-refused",
                server_enabled=server_enabled,
                server_tags=server_tags,
            )
        assert database.list_active_execution_attempts() == []
        assert database.list_node_attempts() == []
        assert database.get_job(records["job_id"]).status == "queued"
    finally:
        database.close()


def test_node_claim_revalidates_dependency_and_dataset_inside_transaction():
    database = Database(":memory:")
    try:
        records = _foundation_records(database, backend="node")
        enrolled = enroll_node(database, server_name="compute-a")

        approval_id = database.insert_pinned_approval(
            kind="enqueue",
            contract_version="enqueue-execution-v1",
            payload={
                "authorized_operations": ["prepare", "launch", "collect"],
                "job_specs": [
                    _job_spec(
                        role="setup",
                        command="echo setup",
                        require_tag="node-canary",
                        pin_server="compute-a",
                    ),
                    _job_spec(
                        role="main",
                        command="echo main",
                        depends_on_roles=["setup"],
                        require_tag="node-canary",
                        pin_server="compute-a",
                    ),
                ],
            },
        )
        approval = database.get_approval(approval_id)
        jobs = database.materialize_pinned_execution_jobs(
            approval_id=approval_id,
            expected_payload_sha256=approval.payload_sha256,
            decision_actor_id="human-reviewer",
        )
        with pytest.raises(ValueError, match="dependency_ineligible"):
            database.create_execution_attempt(
                job_id=jobs["main"],
                backend="node",
                server_config_revision_id=records["revision"]["id"],
                leader_owner_id=records["lease"]["owner_id"],
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
                lease_expires_at="2099-01-01T00:00:00Z",
                node_id=enrolled.node.id,
                node_attempt_id="node-attempt-dependency",
                attempt_id="execution-attempt-dependency",
                node_canary_tag="node-canary",
                node_server_enabled=True,
                node_server_tags=("node-canary",),
            )

        database.insert_project(
            "demo",
            "/srv/demo",
            dataset_name="training-data",
            dataset_version="v1",
            dataset_mode="registered",
        )
        train_approval_id = database.insert_pinned_approval(
            kind="enqueue",
            contract_version="enqueue-execution-v1",
            payload={
                "authorized_operations": ["prepare", "launch", "collect"],
                "job_specs": [
                    _job_spec(
                        role="train",
                        command="python train.py",
                        type="train",
                        project="demo",
                        require_tag="node-canary",
                        pin_server="compute-a",
                    )
                ],
            },
        )
        train_approval = database.get_approval(train_approval_id)
        train_jobs = database.materialize_pinned_execution_jobs(
            approval_id=train_approval_id,
            expected_payload_sha256=train_approval.payload_sha256,
            decision_actor_id="human-reviewer",
        )
        with pytest.raises(ValueError, match="dataset_ineligible"):
            database.create_execution_attempt(
                job_id=train_jobs["train"],
                backend="node",
                server_config_revision_id=records["revision"]["id"],
                leader_owner_id=records["lease"]["owner_id"],
                scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
                lease_expires_at="2099-01-01T00:00:00Z",
                node_id=enrolled.node.id,
                node_attempt_id="node-attempt-dataset",
                attempt_id="execution-attempt-dataset",
                node_canary_tag="node-canary",
                node_server_enabled=True,
                node_server_tags=("node-canary",),
            )
        assert database.list_active_execution_attempts() == []
        assert database.list_node_attempts() == []
    finally:
        database.close()


def test_split_node_v2_endpoint_uses_linked_generic_ownership(api_client):
    client, main_module = api_client
    state = main_module.app_state
    records = _foundation_records(state.db, backend="node")
    enrolled = enroll_node(state.db, server_name="compute-a")
    state.server_configs = {
        "compute-a": ServerConfig(
            name="compute-a",
            host="192.0.2.20",
            user="worker",
            key=records["key_path"],
            port=22,
            tags=["node-canary"],
            project_roots=["/srv/projects"],
            dataset_roots=["/srv/datasets"],
            execution_backend="node",
        )
    }
    state.config.node_agent_v1_enabled = False
    state.config.node_protocol_drain_enabled = True
    state.config.node_new_assignment_enabled = True
    state.config.node_canary_require_tag = "node-canary"
    state.config.execution_attempt_reconcile_existing = True
    state.config.execution_outbox_worker_enabled = True
    state.execution_scheduler_owner_id = records["lease"]["owner_id"]
    state._execution_scheduler_is_leader = True
    state._execution_scheduler_fencing_epoch = records["lease"]["fencing_epoch"]
    state._execution_scheduler_lease_expires_at = records["lease"]["lease_expires_at"]
    scheduled_completions = []

    async def _schedule_completion(**kwargs):
        scheduled_completions.append(kwargs)
        return True

    state.schedule_durable_job_completion_async = _schedule_completion
    headers = {"X-Node-Token": enrolled.raw_token}

    leased = client.post("/node-agent/poll", json={}, headers=headers)
    assert leased.status_code == 200
    payload = leased.json()
    assert payload["protocol_version"] == "2.0"
    assert payload["reused"] is False
    assert payload["attempt"]["execution_attempt_id"] is not None
    node_attempt_id = payload["attempt"]["id"]
    execution_attempt_id = payload["attempt"]["execution_attempt_id"]
    assert state.db.get_execution_attempt(execution_attempt_id)["state"] == "leased"
    assert state.db.get_job(records["job_id"]).status == "queued"

    current = client.post(
        "/node-agent/current-attempt", json={}, headers=headers
    ).json()["attempt"]
    assert current["id"] == node_attempt_id
    assert current["execution_attempt_id"] == execution_attempt_id
    assert current["acked"] is False

    ack = client.post(
        "/node-agent/ack",
        json={
            "attempt_id": node_attempt_id,
            "command_sha256": payload["attempt"]["command_sha256"],
        },
        headers=headers,
    )
    assert ack.status_code == 200
    assert ack.json()["duplicate"] is False
    assert state.db.get_execution_attempt(execution_attempt_id)["state"] == "dispatching"
    assert state.db.get_job(records["job_id"]).status == "running"

    duplicate_ack = client.post(
        "/node-agent/ack",
        json={
            "attempt_id": node_attempt_id,
            "command_sha256": payload["attempt"]["command_sha256"],
        },
        headers=headers,
    )
    assert duplicate_ack.status_code == 200
    assert duplicate_ack.json()["duplicate"] is True

    heartbeat = client.post(
        "/node-agent/heartbeat",
        json={"attempt_id": node_attempt_id, "agent_version": "2.0.0"},
        headers=headers,
    )
    assert heartbeat.status_code == 200
    assert state.db.get_execution_attempt(execution_attempt_id)["state"] == "running"

    terminal = client.post(
        "/node-agent/terminal",
        json={"attempt_id": node_attempt_id, "exit_code": 0, "log_tail": "ok"},
        headers=headers,
    )
    assert terminal.status_code == 200
    assert terminal.json() == {
        "accepted": True,
        "duplicate": False,
        "execution_attempt_id": execution_attempt_id,
    }
    assert state.db.get_execution_attempt(execution_attempt_id)["state"] == "done"
    assert state.db.get_node_attempt(node_attempt_id).status == "done"
    assert state.db.get_job(records["job_id"]).status == "done"
    assert scheduled_completions == [
        {
            "attempt_id": execution_attempt_id,
            "job_id": records["job_id"],
        }
    ]
    completion_operations = state.db.list_execution_completion_operations(
        attempt_id=execution_attempt_id
    )
    assert {
        operation["operation"] for operation in completion_operations
    } == {
        "dependency_refresh",
        "result_collection",
        "notification",
        "owner_projection",
    }
    assert {operation["state"] for operation in completion_operations} == {
        "pending"
    }

    duplicate_terminal = client.post(
        "/node-agent/terminal",
        json={"attempt_id": node_attempt_id, "exit_code": 0, "log_tail": "ok"},
        headers=headers,
    )
    assert duplicate_terminal.status_code == 200
    assert duplicate_terminal.json()["duplicate"] is True
    assert len(scheduled_completions) == 1

    conflicting_terminal = client.post(
        "/node-agent/terminal",
        json={"attempt_id": node_attempt_id, "exit_code": 7, "log_tail": "other"},
        headers=headers,
    )
    assert conflicting_terminal.status_code == 409
    assert state.db.get_job(records["job_id"]).exit_code == 0


def test_attempt_revision_mapping_rejects_replaced_credential_bytes(
    api_client, tmp_path
):
    _client, main_module = api_client
    state = main_module.app_state
    key_path = tmp_path / "worker-key"
    key_path.write_text("approved-key", encoding="utf-8")
    records = _foundation_records(
        state.db,
        backend="node",
        key_path=str(key_path),
    )
    state.server_configs = {
        "compute-a": ServerConfig(
            name="compute-a",
            host="192.0.2.20",
            user="worker",
            key=str(key_path),
            port=22,
            tags=["node-canary"],
            project_roots=["/srv/projects"],
            dataset_roots=["/srv/datasets"],
            execution_backend="node",
        )
    }

    assert state._attempt_revision_ids() == {
        "compute-a": records["revision"]["id"]
    }

    key_path.write_text("replaced-key-with-different-bytes", encoding="utf-8")
    assert state._attempt_revision_ids() == {}


def test_two_node_claimants_create_only_one_linked_attempt(tmp_path):
    path = tmp_path / "node-claim-race.db"
    setup = Database(str(path))
    records = _foundation_records(setup, backend="node")
    node_a = enroll_node(setup, server_name="compute-a").node
    node_b = enroll_node(setup, server_name="compute-a").node
    contender_a = Database(str(path))
    contender_b = Database(str(path))
    barrier = threading.Barrier(2)
    outcomes = []

    def claim(
        database: Database,
        node_id: str,
        node_attempt_id: str,
        attempt_id: str,
    ) -> None:
        barrier.wait()
        try:
            _node_claim(
                database,
                records,
                node_id=node_id,
                node_attempt_id=node_attempt_id,
                attempt_id=attempt_id,
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            outcomes.append(("lost", type(exc).__name__, str(exc)))
        else:
            outcomes.append(("won", attempt_id, node_attempt_id))

    thread_a = threading.Thread(
        target=claim,
        args=(contender_a, node_a.id, "node-attempt-a", "execution-attempt-a"),
    )
    thread_b = threading.Thread(
        target=claim,
        args=(contender_b, node_b.id, "node-attempt-b", "execution-attempt-b"),
    )
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    try:
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert [outcome[0] for outcome in outcomes].count("won") == 1
        assert [outcome[0] for outcome in outcomes].count("lost") == 1
        assert len(setup.list_active_execution_attempts()) == 1
        assert len(setup.list_node_attempts()) == 1
        assert setup.get_job(records["job_id"]).status == "queued"
    finally:
        contender_a.close()
        contender_b.close()
        setup.close()


def test_shadow_tick_is_append_only_measurement_with_zero_canonical_writes():
    database = Database(":memory:")
    canonical_tables = (
        "approvals",
        "jobs",
        "server_config_revisions",
        "server_config_mutations",
        "execution_attempts",
        "execution_operations",
        "execution_attempt_events",
        "legacy_job_stop_intents",
        "scheduler_leases",
        "node_attempts",
    )
    try:
        records = _foundation_records(database)
        _create_attempt(database, records, attempt_id="attempt-shadow")

        with database.cursor() as cursor:
            before = {}
            for table in canonical_tables:
                cursor.execute(f"SELECT * FROM {table} ORDER BY rowid")
                before[table] = [dict(row) for row in cursor.fetchall()]

        observations = database.record_execution_shadow_tick(
            tick_id="shadow-tick-1"
        )

        assert len(observations) == 1
        observation = observations[0]
        assert observation["job_id"] == records["job_id"]
        assert observation["proposed_backend"] == "ssh"
        assert observation["proposed_server_name"] == "compute-a"
        assert observation["proposed_revision_id"] == records["revision"]["id"]
        assert observation["approved_payload_sha256"] == records[
            "execution_payload_sha256"
        ]
        assert observation["legacy_status_before"] == "running"
        assert observation["legacy_status_after"] == "running"
        assert observation["parity_result"] == "eligible_stable"
        assert observation["reason_code"] == "contract_validated"

        with database.cursor() as cursor:
            after = {}
            for table in canonical_tables:
                cursor.execute(f"SELECT * FROM {table} ORDER BY rowid")
                after[table] = [dict(row) for row in cursor.fetchall()]
        assert after == before

        # Stable status does not generate duplicate evidence on every poll.
        assert database.record_execution_shadow_tick(
            tick_id="shadow-tick-2"
        ) == []

        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                cursor.execute(
                    """
                    UPDATE execution_shadow_observations
                    SET reason_code = 'claim_conflict'
                    """
                )
        with database.cursor() as cursor:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                cursor.execute("DELETE FROM execution_shadow_observations")
    finally:
        database.close()


def test_shadow_tick_records_ineligible_target_without_claiming_ownership():
    database = Database(":memory:")
    try:
        command = "echo shadow-only"
        approval_id = database.insert_pinned_approval(
            kind="enqueue",
            contract_version="enqueue-execution-v1",
            payload={
                "authorized_operations": ["launch"],
                "job_specs": [
                    _job_spec(
                        role="main",
                        command=command,
                        pin_server="missing-target",
                    )
                ],
            },
        )
        approval = database.get_approval(approval_id)
        jobs_by_role = database.materialize_pinned_execution_jobs(
            approval_id=approval_id,
            expected_payload_sha256=approval.payload_sha256,
            decision_actor_id="human-reviewer",
        )

        observations = database.record_execution_shadow_tick(
            tick_id="shadow-missing-target"
        )

        assert len(observations) == 1
        assert observations[0]["job_id"] == jobs_by_role["main"]
        assert observations[0]["proposed_server_name"] == "missing-target"
        assert observations[0]["proposed_revision_id"] is None
        assert observations[0]["parity_result"] == "ineligible_stable"
        assert observations[0]["reason_code"] == "target_revision_missing"
        with database.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM execution_attempts")
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT COUNT(*) FROM execution_operations")
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT COUNT(*) FROM scheduler_leases")
            assert cursor.fetchone()[0] == 0
        assert database.get_job(jobs_by_role["main"]).status == "queued"
    finally:
        database.close()


def test_shadow_tick_reports_honest_legacy_job_as_ineligible():
    database = Database(":memory:")
    try:
        job_id = database.insert_job(
            command="echo legacy",
            pin_server="legacy-target",
        )

        observations = database.record_execution_shadow_tick(
            tick_id="shadow-legacy"
        )

        assert len(observations) == 1
        assert observations[0]["job_id"] == job_id
        assert observations[0]["approved_payload_sha256"] is None
        assert observations[0]["proposed_server_name"] == "legacy-target"
        assert observations[0]["parity_result"] == "ineligible_stable"
        assert observations[0]["reason_code"] == "approval_missing"
        assert database.get_job(job_id).status == "queued"
    finally:
        database.close()


def test_positive_terminal_evidence_projects_attempt_and_job_done():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-terminal")
        running = database.transition_execution_attempt(
            attempt_id=attempt["id"],
            expected_state="dispatching",
            expected_liveness="known",
            new_state="running",
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            reason_code="remote_state_observed",
            evidence={"receipt_sha256": "7" * 64},
        )
        assert running["state"] == "running"
        done = database.transition_execution_attempt(
            attempt_id=attempt["id"],
            expected_state="running",
            expected_liveness="known",
            new_state="done",
            exit_code=0,
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            reason_code="terminal_evidence_valid",
            evidence={"sentinel_sha256": "8" * 64, "exit_code": 0},
        )
        assert done["state"] == "done"
        assert done["terminal_at"] is not None
        job = database.get_job(records["job_id"])
        assert job.status == "done"
        assert job.exit_code == 0
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_type, reason_code, event_sha256
                FROM execution_attempt_events
                WHERE attempt_id = ? ORDER BY id
                """,
                (attempt["id"],),
            )
            events = cursor.fetchall()
            assert [row["reason_code"] for row in events] == [
                "contract_validated",
                "remote_state_observed",
                "terminal_evidence_valid",
            ]
            assert all(len(row["event_sha256"]) == 64 for row in events)
    finally:
        database.close()


def test_unknown_liveness_does_not_fail_or_requeue_the_job():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(database, records, attempt_id="attempt-unknown")
        changed = database.transition_execution_attempt(
            attempt_id=attempt["id"],
            expected_state="dispatching",
            expected_liveness="known",
            new_liveness="unknown",
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            reason_code="remote_unreachable",
            evidence={"transport": "timeout"},
        )
        assert changed["state"] == "dispatching"
        assert changed["liveness"] == "unknown"
        job = database.get_job(records["job_id"])
        assert job.status == "running"
        assert job.finished_at is None
        assert job.exit_code is None
    finally:
        database.close()


def test_startup_fails_closed_if_durable_ownership_would_be_abandoned(tmp_path):
    path = tmp_path / "active-ownership.db"
    database = Database(str(path))
    records = _foundation_records(database)
    _create_attempt(database, records, attempt_id="attempt-owned")
    database.close()

    with pytest.raises(RuntimeError, match="RECONCILE_EXISTING=true"):
        AppState(AppConfig(servers=[], db_path=str(path)))

    app_state = AppState(
        AppConfig(
            servers=[],
            db_path=str(path),
            execution_attempt_reconcile_existing=True,
        )
    )
    app_state.db.close()


def test_startup_fails_closed_if_pending_completion_outbox_would_be_abandoned(
    tmp_path,
):
    path = tmp_path / "pending-completion.db"
    database = Database(str(path))
    records = _foundation_records(database, backend="node")
    enrolled = enroll_node(database, server_name="compute-a")
    _node_claim(
        database,
        records,
        node_id=enrolled.node.id,
        node_attempt_id="node-attempt-pending-startup",
        attempt_id="execution-attempt-pending-startup",
    )
    database.acknowledge_node_execution_attempt(
        node_attempt_id="node-attempt-pending-startup",
        node_id=enrolled.node.id,
        command_sha256=utf8_sha256(records["command"]),
    )
    database.record_node_execution_terminal(
        node_attempt_id="node-attempt-pending-startup",
        node_id=enrolled.node.id,
        exit_code=0,
        log_tail="ok",
    )
    database.close()

    with pytest.raises(RuntimeError, match="OUTBOX_WORKER_ENABLED=true"):
        AppState(
            AppConfig(
                servers=[],
                db_path=str(path),
                execution_attempt_reconcile_existing=True,
            )
        )

    state = AppState(
        AppConfig(
            servers=[],
            db_path=str(path),
            execution_attempt_reconcile_existing=True,
            execution_outbox_worker_enabled=True,
        )
    )
    state.db.close()


def test_startup_fails_closed_if_node_protocol_would_be_drained_with_active_work(
    tmp_path,
):
    path = tmp_path / "active-node.db"
    database = Database(str(path))
    job_id = database.insert_job(command="true", type="adhoc")
    node_id = enroll_node(database, server_name="worker-a").node.id
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO node_attempts
                (id, job_id, node_id, status, command_sha256,
                 lease_expires_at, created_at)
            VALUES ('node-attempt-1', ?, ?, 'running', ?, ?, ?)
            """,
            (
                job_id,
                node_id,
                "a" * 64,
                "2999-01-01T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
            ),
        )
    database.close()

    with pytest.raises(RuntimeError, match="NODE_PROTOCOL_DRAIN_ENABLED=true"):
        AppState(AppConfig(servers=[], db_path=str(path)))

    app_state = AppState(
        AppConfig(
            servers=[],
            db_path=str(path),
            node_protocol_drain_enabled=True,
            node_new_assignment_enabled=False,
        )
    )
    app_state.db.close()


def test_execution_control_telemetry_reports_only_durable_evidence():
    database = Database(":memory:")
    try:
        records = _foundation_records(database)
        attempt = _create_attempt(
            database,
            records,
            attempt_id="attempt-telemetry",
        )
        database.insert_job(command="echo queued")
        operation = database.insert_execution_operation(
            attempt_id=attempt["id"],
            operation="collect",
            payload={"collector": "result-v1"},
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            authorization_approval_id=records["execution_approval_id"],
            authorized_contract_sha256=records["execution_payload_sha256"],
            operation_id="operation-telemetry",
        )
        database.transition_execution_attempt(
            attempt_id=attempt["id"],
            expected_state="dispatching",
            expected_liveness="known",
            new_liveness="unknown",
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
            reason_code="remote_unreachable",
            evidence={"transport": "timeout"},
        )
        claimed = database.claim_execution_operation(
            operation_id=operation["id"],
            claim_owner="worker-a",
            claim_seconds=60,
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        assert claimed is not None
        assert database.mark_execution_operation_effect_started(
            operation_id=operation["id"],
            claim_owner="worker-a",
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        database.transition_execution_operation(
            operation_id=operation["id"],
            expected_state="processing",
            new_state="uncertain",
            reason_code="effect_outcome_unknown",
            evidence={"transport": "timeout"},
            claim_owner="worker-a",
            leader_owner_id="scheduler-a",
            scheduler_fencing_epoch=records["lease"]["fencing_epoch"],
        )
        with database.cursor() as cursor:
            database._append_execution_event(
                cursor,
                attempt_id=attempt["id"],
                event_type="duplicate_prevented",
                reason_code="claim_conflict",
                evidence={"scope": "test-recorded-conflict"},
            )

        telemetry = database.get_execution_control_plane_telemetry()

        assert telemetry["lease"]["owner_id"] == "scheduler-a"
        assert telemetry["lease"]["is_live"] is True
        assert telemetry["attempts"]["total"] == 1
        assert telemetry["attempts"]["by_state"] == {"dispatching": 1}
        assert telemetry["attempts"]["by_backend"] == {"ssh": 1}
        assert telemetry["attempts"]["by_backend_state"] == {
            "ssh": {"dispatching": 1}
        }
        assert telemetry["attempts"]["oldest_active_unknown_age_seconds"] >= 0
        assert telemetry["outbox"]["backlog"] == 1
        assert telemetry["outbox"]["uncertain"] == 1
        assert telemetry["outbox"]["oldest_uncertain_age_seconds"] >= 0
        assert telemetry["collection"] == {
            "by_state": {"uncertain": 1},
            "backlog": 1,
        }
        assert telemetry["queue"]["depth"] == 1
        assert telemetry["queue"]["oldest_age_seconds"] >= 0
        assert telemetry["duplicate_prevention"]["recorded_count"] == 1
        assert "lower bound" in telemetry["duplicate_prevention"]["source"]
    finally:
        database.close()


@pytest.mark.parametrize(
    "value",
    [
        {"bad": 1.5},
        {"bad": float("nan")},
        {1: "non-string-key"},
    ],
)
def test_canonical_execution_json_rejects_non_contract_values(value):
    with pytest.raises(ValueError):
        canonical_json(value)


def test_pinned_execution_contract_requires_exact_command_bytes_and_digest():
    database = Database(":memory:")
    try:
        with pytest.raises(ValueError, match="command bytes"):
            database.insert_pinned_approval(
                kind="enqueue",
                contract_version="enqueue-execution-v1",
                payload={
                    "authorized_operations": ["launch"],
                    "job_specs": [
                        {
                            "role": "main",
                            "type": "adhoc",
                            "command_sha256": utf8_sha256("echo safe"),
                        }
                    ],
                },
            )
    finally:
        database.close()
