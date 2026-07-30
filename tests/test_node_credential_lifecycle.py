"""WP-4B: staged rotation, draining, and revocation (DG-NODE-V2 N-3/N-4).

The distinction these tests exist to protect: **retiring a node and revoking
one are different operations.** Retirement drains — the node keeps its identity
and finishes its work. Revocation is immediate, and deliberately does *not*
mark that work failed.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import Database
from app.node_registry import (
    NodeAuthError,
    authenticate_node,
    enroll_node,
    revoke_node,
    rotate_node_credential,
    select_job_for_node,
)


@pytest.fixture()
def database(tmp_path):
    return Database(str(tmp_path / "nodes.db"))


# ---------------------------------------------------------------------------
# N-4: staged rotation
# ---------------------------------------------------------------------------


def test_both_credentials_work_during_the_overlap(database):
    """A hard cutover turns every rotation into an outage, which in practice
    means rotations stop happening."""
    enrolled = enroll_node(database, server_name="worker-a")
    old_token = enrolled.raw_token

    rotated = rotate_node_credential(database, enrolled.node.id, overlap_sec=300)

    assert authenticate_node(database, rotated.raw_token).id == enrolled.node.id
    assert authenticate_node(database, old_token).id == enrolled.node.id


def test_the_previous_credential_stops_working_once_it_expires(database):
    enrolled = enroll_node(database, server_name="worker-a")
    old_token = enrolled.raw_token
    rotate_node_credential(database, enrolled.node.id, overlap_sec=300)

    # Expire the overlap window.
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE nodes SET previous_secret_expires_at = '2000-01-01T00:00:00+00:00'"
        )

    with pytest.raises(NodeAuthError):
        authenticate_node(database, old_token)


def test_rotation_without_an_overlap_invalidates_immediately(database):
    """That is what an emergency re-key wants."""
    enrolled = enroll_node(database, server_name="worker-a")
    old_token = enrolled.raw_token

    rotate_node_credential(database, enrolled.node.id)

    with pytest.raises(NodeAuthError):
        authenticate_node(database, old_token)


def test_a_second_rotation_drops_the_first_previous_secret(database):
    """Only one outgoing credential is ever valid; rotating twice must not
    leave a chain of accepted old tokens."""
    enrolled = enroll_node(database, server_name="worker-a")
    original = enrolled.raw_token
    first = rotate_node_credential(database, enrolled.node.id, overlap_sec=300)
    rotate_node_credential(database, enrolled.node.id, overlap_sec=300)

    with pytest.raises(NodeAuthError):
        authenticate_node(database, original)
    # The immediately previous one is still inside its window.
    assert authenticate_node(database, first.raw_token).id == enrolled.node.id


def test_an_invalid_overlap_is_refused(database):
    enrolled = enroll_node(database, server_name="worker-a")
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="positive number of seconds"):
            database.update_node_secret(enrolled.node.id, "hash", overlap_sec=bad)


def test_rotation_keeps_the_node_identity_and_its_attempts(database):
    enrolled = enroll_node(database, server_name="worker-a")
    job_id = database.insert_job(command="python train.py", type="train")
    database.insert_node_attempt(
        attempt_id="attempt-1",
        job_id=job_id,
        node_id=enrolled.node.id,
        command_sha256="s",
        lease_expires_at="2099-01-01T00:00:00+00:00",
    )

    rotate_node_credential(database, enrolled.node.id, overlap_sec=60)

    assert database.get_node_attempt("attempt-1").node_id == enrolled.node.id


# ---------------------------------------------------------------------------
# N-3: draining vs revocation
# ---------------------------------------------------------------------------


def test_a_draining_node_is_offered_nothing_new(database):
    enrolled = enroll_node(database, server_name="worker-a")
    database.insert_job(command="python train.py", type="train", require_tag="node-canary")

    database.set_node_draining(enrolled.node.id)

    node = database.get_node(enrolled.node.id)
    assert node.is_draining is True
    assert select_job_for_node(database, node=node, canary_tag="node-canary") is None


def test_a_draining_node_keeps_its_identity_and_credential(database):
    """Retirement is not revocation: the agent must still be able to report on
    the work it already holds."""
    enrolled = enroll_node(database, server_name="worker-a")

    database.set_node_draining(enrolled.node.id)

    assert authenticate_node(database, enrolled.raw_token).id == enrolled.node.id


def test_draining_is_reversible(database):
    enrolled = enroll_node(database, server_name="worker-a")
    database.insert_job(command="python train.py", type="train", require_tag="node-canary")
    database.set_node_draining(enrolled.node.id)
    database.set_node_draining(enrolled.node.id, draining=False)

    node = database.get_node(enrolled.node.id)
    assert node.is_draining is False
    assert select_job_for_node(database, node=node, canary_tag="node-canary") is not None


def test_revocation_is_immediate_even_inside_a_rotation_overlap(database):
    """The overlap window protects a rotation, never a revoked credential."""
    enrolled = enroll_node(database, server_name="worker-a")
    old_token = enrolled.raw_token
    rotate_node_credential(database, enrolled.node.id, overlap_sec=300)

    revoke_node(database, enrolled.node.id)

    for token in (old_token,):
        with pytest.raises(NodeAuthError):
            authenticate_node(database, token)


def test_revocation_does_not_mark_in_flight_work_failed(database):
    """N-3: revoking a credential is not evidence about the workload. Marking
    it failed would fabricate a terminal nobody observed."""
    enrolled = enroll_node(database, server_name="worker-a")
    job_id = database.insert_job(command="python train.py", type="train")
    database.insert_node_attempt(
        attempt_id="attempt-1",
        job_id=job_id,
        node_id=enrolled.node.id,
        command_sha256="s",
        lease_expires_at="2099-01-01T00:00:00+00:00",
    )

    revoke_node(database, enrolled.node.id)

    attempt = database.get_node_attempt("attempt-1")
    assert attempt.terminal_at is None
    assert attempt.exit_code is None
    assert database.get_job(job_id).status == "queued"


def test_revoking_one_node_leaves_another_untouched(database):
    first = enroll_node(database, server_name="worker-a")
    second = enroll_node(database, server_name="worker-b")

    revoke_node(database, first.node.id)

    assert authenticate_node(database, second.raw_token).id == second.node.id


def test_legacy_node_rows_migrate_without_inventing_rotation_state(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, server_name TEXT NOT NULL,
            secret_hash TEXT NOT NULL, status TEXT NOT NULL,
            agent_version TEXT, last_heartbeat_at TEXT,
            created_at TEXT NOT NULL, revoked_at TEXT, approval_id INTEGER
        );
        INSERT INTO nodes (id, server_name, secret_hash, status, created_at)
        VALUES ('n1', 'worker-a', 'h', 'enrolled', 'legacy');
        """
    )
    raw.commit()
    raw.close()

    database = Database(str(path))
    node = database.get_node("n1")

    assert node.previous_secret_hash is None
    assert node.previous_secret_expires_at is None
    assert node.primary_credential_id is None
    assert node.pending_credential_id is None
    assert node.pending_secret_hash is None
    assert node.pending_activation_nonce_hash is None
    assert node.pending_expires_at is None
    assert node.pending_grace_sec is None
    assert node.pending_created_at is None
    assert node.pending_approval_id is None
    assert node.last_activation_credential_id is None
    assert node.last_activation_nonce_hash is None
    assert node.last_activation_expires_at is None
    assert node.last_activated_at is None
    assert node.is_draining is False
    assert node.retired_at is None
