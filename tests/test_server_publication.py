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

    reloaded = []
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
        reload_yaml=lambda: reloaded.append(True),
    )

    assert outcome.state == "activated"
    assert reloaded == [True]


def test_reload_runs_while_publication_is_unresolved(tmp_path):
    """The process must load the exact new bytes before they become active."""

    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)

    def _reload():
        mutation = database.list_server_config_mutations()[0]
        approval = database.get_approval(approval_id)
        assert mutation["state"] == "yaml_applied"
        assert approval.status == "pending"

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
        observe_yaml=lambda: yaml_digest({"servers": [_SERVER]}),
        reload_yaml=_reload,
    )

    assert outcome.state == "activated"
    assert database.get_approval(approval_id).status == "approved"


def test_reload_failure_compensates_exact_bytes_and_rejects(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)
    observed = {"document": {"servers": []}}

    def _write():
        observed["document"] = {"servers": [_SERVER]}

    def _reload():
        raise ValueError("invalid runtime config")

    def _compensate():
        observed["document"] = {"servers": []}

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=_write,
        observe_yaml=lambda: yaml_digest(observed["document"]),
        reload_yaml=_reload,
        compensate_yaml=_compensate,
    )

    assert outcome.state == "rolled_back"
    assert observed["document"] == {"servers": []}
    assert database.get_approval(approval_id).status == "rejected"
    assert database.get_active_server_config_revision("compute-a") is None


# ---------------------------------------------------------------------------
# Update / disable / delete, and the operator recovery surface
# ---------------------------------------------------------------------------


def _pinned(database, kind, operation, *, before, after, target=None):
    """The approval pins the exact digests. If the YAML moves between request
    and approval, publication fails closed rather than publishing a target
    nobody reviewed."""
    payload = {
        "operation": operation,
        "server_name": "compute-a",
        "yaml_before_sha256": yaml_digest(before),
        "yaml_after_sha256": yaml_digest(after),
    }
    if target is not None:
        payload["normalized_target"] = normalize_target(target)
        payload["credential_ref"] = credential_reference(target)
    return database.insert_pinned_approval(
        kind=kind, contract_version=SERVER_CONFIG_CONTRACT_VERSION, payload=payload
    )


def test_update_publishes_a_new_revision_and_retires_the_old_one(tmp_path):
    """An attempt pinned to the old revision must still resolve its original
    target, so the old row is retired rather than edited."""
    database = Database(str(tmp_path / "pub.db"))
    publish_approved_server_mutation(
        database,
        approval_id=_pinned_approval(database),
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    moved = {**_SERVER, "host": "192.0.2.99"}
    publish_approved_server_mutation(
        database,
        approval_id=_pinned(database, "server_update", "update",
                    before={"servers": [_SERVER]},
                    after={"servers": [moved]}, target=moved),
        operation="update",
        server_name="compute-a",
        server_payload=moved,
        yaml_before={"servers": [_SERVER]},
        yaml_after={"servers": [moved]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT revision, publication_state, target_identity_sha256"
            " FROM server_config_revisions WHERE server_name = 'compute-a'"
            " ORDER BY revision"
        )
        rows = cursor.fetchall()
    assert len(rows) == 2
    assert rows[0]["publication_state"] == "retired"
    assert rows[1]["publication_state"] == "active"
    assert rows[0]["target_identity_sha256"] != rows[1]["target_identity_sha256"]


def test_exact_update_reapproval_adopts_a_never_published_legacy_server(tmp_path):
    """A fresh human decision, not migration inference, creates revision 1."""
    database = Database(str(tmp_path / "pub.db"))
    document = {"servers": [_SERVER]}
    approval_id = _pinned(
        database,
        "server_update",
        "update",
        before=document,
        after=document,
        target=_SERVER,
    )
    written = []

    outcome = publish_approved_server_mutation(
        database,
        approval_id=approval_id,
        operation="update",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before=document,
        yaml_after=document,
        decision_actor_id="human-reviewer",
        write_yaml=lambda: written.append(True),
    )

    assert outcome.state == "activated"
    assert written == [True]
    revision = database.get_active_server_config_revision("compute-a")
    assert revision["revision"] == 1
    assert revision["assignment_eligibility"] == "approved"
    assert revision["created_by_approval_id"] == approval_id
    mutation = database.list_server_config_mutations()[0]
    assert mutation["operation"] == "update"
    assert mutation["prior_revision_id"] is None
    assert mutation["prepared_revision_id"] == revision["id"]


def test_update_cannot_resurrect_retired_revision_history_as_legacy(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    document = {"servers": [_SERVER]}
    publish_approved_server_mutation(
        database,
        approval_id=_pinned_approval(database),
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after=document,
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    disabled = {**_SERVER, "enabled": False}
    disabled_document = {"servers": [disabled]}
    publish_approved_server_mutation(
        database,
        approval_id=_pinned(
            database,
            "server_disable",
            "disable",
            before=document,
            after=disabled_document,
        ),
        operation="disable",
        server_name="compute-a",
        server_payload=None,
        yaml_before=document,
        yaml_after=disabled_document,
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    assert database.get_active_server_config_revision("compute-a") is None

    approval_id = _pinned(
        database,
        "server_update",
        "update",
        before=disabled_document,
        after=disabled_document,
        target=disabled,
    )
    written = []
    with pytest.raises(ValueError, match="changed since approval"):
        publish_approved_server_mutation(
            database,
            approval_id=approval_id,
            operation="update",
            server_name="compute-a",
            server_payload=disabled,
            yaml_before=disabled_document,
            yaml_after=disabled_document,
            decision_actor_id="human-reviewer",
            write_yaml=lambda: written.append(True),
        )
    assert written == []


def test_disable_retires_without_creating_a_new_target(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    publish_approved_server_mutation(
        database,
        approval_id=_pinned_approval(database),
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    outcome = publish_approved_server_mutation(
        database,
        approval_id=_pinned(database, "server_disable", "disable",
                    before={"servers": [_SERVER]},
                    after={"servers": [{**_SERVER, "enabled": False}]}),
        operation="disable",
        server_name="compute-a",
        server_payload=None,
        yaml_before={"servers": [_SERVER]},
        yaml_after={"servers": [{**_SERVER, "enabled": False}]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )

    assert outcome.state == "activated"
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM server_config_revisions"
            " WHERE server_name = 'compute-a' AND publication_state = 'active'"
        )
        # Disable creates no new target; it must not leave two active rows.
        assert cursor.fetchone()["n"] <= 1


def test_operator_can_resolve_a_recovery_hold_only_with_a_matching_digest(tmp_path):
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
        observe_yaml=lambda: yaml_digest({"servers": [{"name": "half"}]}),
    )
    assert outcome.state == "recovery_hold"

    # An operator cannot assert an outcome the journal does not record.
    with pytest.raises(ValueError, match="does not match the journal"):
        database.resolve_server_config_recovery_hold(
            mutation_id=outcome.mutation_id,
            observed_yaml_sha256=yaml_digest({"servers": [{"name": "invented"}]}),
            resolution="yaml_applied",
            operator_actor_id="operator-1",
        )

    resolved = database.resolve_server_config_recovery_hold(
        mutation_id=outcome.mutation_id,
        observed_yaml_sha256=yaml_digest({"servers": []}),
        resolution="rolled_back",
        operator_actor_id="operator-1",
    )
    assert resolved["state"] == "rolled_back"


def test_recovery_resolution_rejects_an_unknown_outcome(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    with pytest.raises(ValueError, match="invalid recovery resolution"):
        database.resolve_server_config_recovery_hold(
            mutation_id="whatever",
            observed_yaml_sha256="x",
            resolution="activated",
            operator_actor_id="operator-1",
        )


def test_journal_never_returns_yaml_content_or_credentials(tmp_path):
    database = Database(str(tmp_path / "pub.db"))
    publish_approved_server_mutation(
        database,
        approval_id=_pinned_approval(database),
        operation="add",
        server_name="compute-a",
        server_payload=_SERVER,
        yaml_before={"servers": []},
        yaml_after={"servers": [_SERVER]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: None,
    )
    rows = database.list_server_config_mutations()
    assert rows
    flattened = repr(rows)
    assert _SERVER["key"] not in flattened
    assert _SERVER["host"] not in flattened
    assert "credential" not in flattened


def test_disabling_a_legacy_server_still_writes_yaml(tmp_path):
    """Regression: a legacy machine has no pinned revision to supersede, but
    the operator's disable must still take effect. Returning early without
    writing would make the action silently do nothing."""
    database = Database(str(tmp_path / "pub.db"))
    written = []

    outcome = publish_approved_server_mutation(
        database,
        approval_id=_pinned(
            database,
            "server_disable",
            "disable",
            before={"servers": [_SERVER]},
            after={"servers": [{**_SERVER, "enabled": False}]},
        ),
        operation="disable",
        server_name="compute-a",
        server_payload=None,
        yaml_before={"servers": [_SERVER]},
        yaml_after={"servers": [{**_SERVER, "enabled": False}]},
        decision_actor_id="human-reviewer",
        write_yaml=lambda: written.append(True),
    )

    assert outcome.state == "skipped_legacy"
    assert written == [True], "the YAML mutation must still be applied"


def test_digest_drift_between_request_and_approval_fails_closed(tmp_path):
    """Someone edited servers.yaml while the approval was pending. Publishing
    would activate a target nobody reviewed, so nothing is written at all."""
    from app.server_publication import ServerPublicationRejected

    database = Database(str(tmp_path / "pub.db"))
    approval_id = _pinned_approval(database)  # pinned to before={} after=[_SERVER]
    written = []

    with pytest.raises(ServerPublicationRejected, match="changed since approval"):
        publish_approved_server_mutation(
            database,
            approval_id=approval_id,
            operation="add",
            server_name="compute-a",
            server_payload=_SERVER,
            # The file gained an unrelated machine since the approval was made.
            yaml_before={"servers": [{"name": "someone-else"}]},
            yaml_after={"servers": [{"name": "someone-else"}, _SERVER]},
            decision_actor_id="human-reviewer",
            write_yaml=lambda: written.append(True),
        )

    assert written == [], "no YAML write may happen on a rejected publication"
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM server_config_mutations")
        assert cursor.fetchone()["n"] == 0
