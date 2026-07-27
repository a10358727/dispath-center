import base64
import sqlite3
import threading

import pytest

from app.config import AppConfig
from app.db import Database
from app.execution_contract import canonical_json, utf8_sha256
from app.main import AppState


def _insert_pinned_job(
    database: Database,
    *,
    approval_id: int,
    command: str,
    payload_sha256: str,
    contract_version: str = "enqueue-execution-v1",
    role: str = "main",
    pin_server: str = "compute-a",
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
                'adhoc', 'demo', ?, NULL, ?, '[]', NULL, 'queued', 'normal',
                '2026-07-27T00:00:00+00:00', ?, ?, ?, ?, ?
            )
            """,
            (
                command,
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


def _foundation_records(database: Database) -> dict:
    normalized_target = {
        "backend": "ssh",
        "host": "192.0.2.20",
        "port": 22,
        "user": "worker",
        "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"],
    }
    credential_ref = {
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
        with pytest.raises(ValueError, match="not implemented"):
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
