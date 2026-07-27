"""RB-SERVER-001: an approved server must become claimable.

The blocker was never about bookkeeping. A target that does not materialize an
approved immutable revision stays `legacy_observed`, and
`create_execution_attempt()` refuses it — so generic new claims could never be
enabled for any server. These tests assert the eligibility actually changes,
not merely that a row appears.
"""

from __future__ import annotations

import pytest
import yaml as yaml_module

from app.db import Database
from app.server_publication import (
    SERVER_CONFIG_CONTRACT_VERSION,
    credential_reference,
    normalize_target,
    publish_approved_server_mutation,
    yaml_digest,
)

_SERVER = {
    "name": "compute-a",
    "host": "192.0.2.20",
    "user": "worker",
    "key": "/tmp/does-not-exist-key",
    "port": 22,
    "project_roots": ["/srv/projects"],
    "dataset_roots": ["/srv/datasets"],
}


def _pinned_approval(database):
    approval_id = database.insert_pinned_approval(
        kind="server_add",
        contract_version=SERVER_CONFIG_CONTRACT_VERSION,
        payload={
            "operation": "add",
            "server_name": "compute-a",
            "normalized_target": normalize_target(_SERVER),
            "credential_ref": credential_reference(_SERVER),
            "yaml_before_sha256": yaml_digest({"servers": []}),
            "yaml_after_sha256": yaml_digest({"servers": [_SERVER]}),
        },
    )
    # Deliberately left `pending`: the publication protocol owns the
    # transition to approved, exactly as `approvals.approve()` calls it before
    # marking the approval decided.
    return approval_id


def test_publication_makes_the_target_assignment_eligible(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)
    written = []

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: written.append(True),
    )

    assert outcome.state == "activated"
    assert written == [True]
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT assignment_eligibility, publication_state"
            " FROM server_config_revisions WHERE server_name = 'compute-a'"
        )
        row = cursor.fetchone()
    assert row["assignment_eligibility"] == "approved"
    assert row["publication_state"] == "active"


def test_yaml_is_written_after_the_durable_intent(tmp_path):
    """INV-STATE-2 ordering: the journal exists before the file changes."""
    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)
    seen_states = []

    def _write():
        with database.cursor() as cursor:
            cursor.execute("SELECT state FROM server_config_mutations")
            seen_states.extend(row["state"] for row in cursor.fetchall())

    publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=_write,
    )

    assert seen_states == ["intent"]


def test_clean_write_failure_rolls_back_and_leaves_no_active_revision(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)

    def _boom():
        raise OSError("disk full")

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=_boom,
        observe_yaml=lambda: yaml_digest({"servers": []}),
    )

    assert outcome.state == "rolled_back"
    with database.cursor() as cursor:
        cursor.execute("SELECT state FROM server_config_mutations")
        assert cursor.fetchone()["state"] == "rolled_back"
        cursor.execute(
            "SELECT publication_state FROM server_config_revisions"
            " WHERE server_name = 'compute-a'"
        )
        row = cursor.fetchone()
    # The prepared revision is compensated to `retired`, never `active`: a
    # rolled-back mutation must not leave a claimable target behind.
    assert row is None or row["publication_state"] == "retired"


def test_unpinned_legacy_approval_writes_yaml_but_publishes_nothing(tmp_path):
    """Publication requires the reviewed contract. A legacy approval keeps
    working and stays honestly ineligible rather than being back-dated."""
    database = Database(str(tmp_path / "pub.db"))
    approval_id = database.insert_approval("server_add", _SERVER)
    written = []

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: written.append(True),
    )

    assert outcome.state == "skipped_legacy"
    assert written == [True]
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM server_config_revisions")
        assert cursor.fetchone()["n"] == 0


def test_target_identity_ignores_operator_metadata():
    """A note or tag change must not invalidate a pinned attempt's target."""
    base = normalize_target(_SERVER)
    annotated = normalize_target({**_SERVER, "note": "maintenance", "tags": ["a"]})
    assert base == annotated

    repointed = normalize_target({**_SERVER, "host": "192.0.2.99"})
    assert repointed != base


def test_credential_reference_never_contains_key_material(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
    reference = credential_reference({**_SERVER, "key": str(key)})
    flattened = repr(reference)
    assert "BEGIN OPENSSH" not in flattened
    assert "secret" not in flattened
    assert reference["file_identity"]["inode"] != 0


def test_yaml_digest_matches_what_lands_on_disk(tmp_path):
    document = {"servers": [_SERVER]}
    path = tmp_path / "servers.yaml"
    path.write_text(
        yaml_module.safe_dump(document, allow_unicode=True, sort_keys=False)
    )
    from app.execution_contract import utf8_sha256

    assert yaml_digest(document) == utf8_sha256(path.read_text())


def test_unknown_disk_state_after_a_failed_write_fails_closed(tmp_path):
    """A third digest means nobody knows what is on disk. Blocking the server
    is correct; guessing that the write failed cleanly is not."""
    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)

    def _boom():
        raise OSError("partial write")

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=_boom,
        observe_yaml=lambda: yaml_digest({"servers": [{"name": "half-written"}]}),
    )

    assert outcome.state == "recovery_hold"
    with database.cursor() as cursor:
        cursor.execute("SELECT state FROM server_config_mutations")
        assert cursor.fetchone()["state"] == "recovery_hold"


def test_write_that_landed_despite_an_error_is_not_rolled_back(tmp_path):
    """Rolling back a target that is already on disk would leave the file and
    the journal disagreeing."""
    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)

    def _boom():
        raise OSError("fsync reported an error after writing")

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=_boom,
        observe_yaml=lambda: yaml_digest({"servers": [_SERVER]}),
    )

    assert outcome.state == "activated"
