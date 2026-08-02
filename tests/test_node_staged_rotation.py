"""Plan §9.4/§9.6: true two-phase Node credential rotation.

The pending token must not replace or authenticate as the current credential
until token+activation nonce atomically promote it. Every test is local and
uses temporary SQLite/HTTP fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.__main__ import AgentConfig, NodeAgentDaemon
from agent.client import NodeAgentClient
from app.db import Database
from app.node_registry import (
    NodeAuthError,
    activate_node_credential,
    authenticate_node,
    enroll_node,
    revoke_node,
    stage_node_credential,
)


@pytest.fixture()
def database(tmp_path):
    database = Database(str(tmp_path / "staged-node.db"))
    try:
        yield database
    finally:
        database.close()


def test_pending_credential_does_not_change_or_authenticate_as_primary(database):
    enrolled = enroll_node(database, server_name="worker-a")
    staged = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
        approval_id=11,
    )

    assert staged is not None
    assert authenticate_node(database, enrolled.raw_token).id == enrolled.node.id
    with pytest.raises(NodeAuthError):
        authenticate_node(database, staged.raw_token)
    node = database.get_node(enrolled.node.id)
    assert node.pending_credential_id == staged.credential_id
    assert node.pending_approval_id == 11


def test_activation_promotes_pending_and_is_response_loss_idempotent(database):
    enrolled = enroll_node(database, server_name="worker-a")
    staged = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
    )
    assert staged is not None

    activated = activate_node_credential(
        database, staged.raw_token, staged.activation_nonce
    )
    duplicate = activate_node_credential(
        database, staged.raw_token, staged.activation_nonce
    )

    assert activated is not None and activated["duplicate"] is False
    assert duplicate is not None and duplicate["duplicate"] is True
    assert authenticate_node(database, staged.raw_token).id == enrolled.node.id
    # Activation begins a bounded grace instead of cutting off an in-flight
    # agent before it can report terminal evidence.
    assert authenticate_node(database, enrolled.raw_token).id == enrolled.node.id

    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE nodes SET previous_secret_expires_at = "
            "'2000-01-01T00:00:00Z'"
        )
    with pytest.raises(NodeAuthError):
        authenticate_node(database, enrolled.raw_token)
    assert authenticate_node(database, staged.raw_token).id == enrolled.node.id


def test_emergency_revoke_invalidates_primary_grace_pending_and_receipt(database):
    enrolled = enroll_node(database, server_name="worker-a")
    pending = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
    )
    assert pending is not None
    activate_node_credential(
        database, pending.raw_token, pending.activation_nonce
    )
    next_pending = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
    )
    assert next_pending is not None

    revoke_node(database, enrolled.node.id)

    for token in (
        enrolled.raw_token,
        pending.raw_token,
        next_pending.raw_token,
    ):
        with pytest.raises(NodeAuthError):
            authenticate_node(database, token)
    with pytest.raises(NodeAuthError):
        activate_node_credential(
            database,
            next_pending.raw_token,
            next_pending.activation_nonce,
        )
    node = database.get_node(enrolled.node.id)
    assert node.pending_credential_id is None
    assert node.previous_secret_hash is None
    assert node.last_activation_nonce_hash is None


def test_wrong_nonce_and_expired_pending_leave_current_untouched(database):
    enrolled = enroll_node(database, server_name="worker-a")
    staged = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
    )
    assert staged is not None

    with pytest.raises(NodeAuthError):
        activate_node_credential(database, staged.raw_token, "wrong")
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE nodes SET pending_expires_at = '2000-01-01T00:00:00Z'"
        )
    with pytest.raises(NodeAuthError):
        activate_node_credential(
            database, staged.raw_token, staged.activation_nonce
        )
    assert authenticate_node(database, enrolled.raw_token).id == enrolled.node.id


def test_exact_pending_replacement_invalidates_only_lost_delivery(database):
    enrolled = enroll_node(database, server_name="worker-a")
    first = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
    )
    assert first is not None

    with pytest.raises(ValueError, match="pending"):
        stage_node_credential(
            database,
            enrolled.node.id,
            pending_ttl_sec=3600,
            grace_sec=300,
        )
    with pytest.raises(ValueError, match="target changed"):
        stage_node_credential(
            database,
            enrolled.node.id,
            pending_ttl_sec=3600,
            grace_sec=300,
            replace_pending_credential_id="not-the-pinned-id",
        )

    second = stage_node_credential(
        database,
        enrolled.node.id,
        pending_ttl_sec=3600,
        grace_sec=300,
        replace_pending_credential_id=first.credential_id,
    )
    assert second is not None
    with pytest.raises(NodeAuthError):
        activate_node_credential(
            database, first.raw_token, first.activation_nonce
        )
    assert authenticate_node(database, enrolled.raw_token).id == enrolled.node.id
    assert activate_node_credential(
        database, second.raw_token, second.activation_nonce
    ) is not None


def _enable_split_protocol(state) -> None:
    state.config.node_agent_v1_enabled = False
    state.config.node_protocol_drain_enabled = True
    state.config.node_new_assignment_enabled = False


def _request_and_approve_rotation(client, node_id: str, **extra):
    requested = client.post(
        "/nodes/rotate-request",
        json={"node_id": node_id, **extra},
    )
    assert requested.status_code == 200, requested.text
    approved = client.post(f"/approve/{requested.json()['id']}")
    assert approved.status_code == 200, approved.text
    return approved.json()


def test_public_split_rotation_requires_activation_and_never_exposes_hashes(
    api_client,
):
    client, main_module = api_client
    _enable_split_protocol(main_module.app_state)
    enrolled = enroll_node(main_module.app_state.db, server_name="worker-a")

    response = _request_and_approve_rotation(client, enrolled.node.id)

    assert response["activation_required"] is True
    assert response["credential_id"]
    assert response["node"]["pending_credential_id"] == response["credential_id"]
    assert all("hash" not in key for key in response["node"])
    pending_token = response["node_token"]
    nonce = response["activation_nonce"]

    # Pending token is valid on exactly the activation route.
    assert client.post(
        "/node-agent/current-attempt",
        headers={"X-Node-Token": pending_token},
        json={},
    ).status_code == 401
    activated = client.post(
        "/node-agent/activate",
        headers={
            "X-Node-Token": pending_token,
            "X-Node-Activation-Nonce": nonce,
        },
        json={},
    )
    assert activated.status_code == 200
    assert activated.json()["duplicate"] is False
    duplicate = client.post(
        "/node-agent/activate",
        headers={
            "X-Node-Token": pending_token,
            "X-Node-Activation-Nonce": nonce,
        },
        json={},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert client.post(
        "/node-agent/current-attempt",
        headers={"X-Node-Token": pending_token},
        json={},
    ).status_code == 200
    # Old token remains usable only as bounded grace evidence.
    assert client.post(
        "/node-agent/current-attempt",
        headers={"X-Node-Token": enrolled.raw_token},
        json={},
    ).status_code == 200

    audit = Path(main_module.app_state.config.audit_path).read_text()
    assert pending_token not in audit
    assert nonce not in audit


def test_public_response_lost_replacement_is_explicit_and_exact(api_client):
    client, main_module = api_client
    _enable_split_protocol(main_module.app_state)
    enrolled = enroll_node(main_module.app_state.db, server_name="worker-a")
    first = _request_and_approve_rotation(client, enrolled.node.id)

    refused = client.post(
        "/nodes/rotate-request", json={"node_id": enrolled.node.id}
    )
    assert refused.status_code == 400
    second = _request_and_approve_rotation(
        client, enrolled.node.id, replace_pending=True
    )

    assert second["credential_id"] != first["credential_id"]
    assert client.post(
        "/node-agent/activate",
        headers={
            "X-Node-Token": first["node_token"],
            "X-Node-Activation-Nonce": first["activation_nonce"],
        },
        json={},
    ).status_code == 401
    assert authenticate_node(
        main_module.app_state.db, enrolled.raw_token
    ).id == enrolled.node.id
    assert client.post(
        "/node-agent/activate",
        headers={
            "X-Node-Token": second["node_token"],
            "X-Node-Activation-Nonce": second["activation_nonce"],
        },
        json={},
    ).status_code == 200


def test_node_client_keeps_activation_nonce_out_of_payload_and_result():
    calls = []

    def transport(method, path, *, json, headers):
        calls.append((method, path, json, headers))
        return 200, {"duplicate": False}

    client = NodeAgentClient(transport, node_token="dcn_token")

    assert client.activate("one-time-nonce") is True
    method, path, payload, headers = calls[0]
    assert (method, path, payload) == ("POST", "/node-agent/activate", {})
    assert headers["X-Node-Token"] == "dcn_token"
    assert headers["X-Node-Activation-Nonce"] == "one-time-nonce"


def test_daemon_retries_response_lost_activation_before_any_node_operation(
    tmp_path,
):
    class Client:
        def __init__(self):
            self.activations = 0
            self.operations = []

        def activate(self, nonce):
            self.activations += 1
            assert nonce == "nonce"
            if self.activations == 1:
                raise ConnectionError("response lost")
            return False

        def heartbeat(self, attempt_id=None):
            self.operations.append("heartbeat")
            return False

        def poll(self):
            self.operations.append("poll")
            return None

    class Store:
        def list_all(self):
            return []

    client = Client()
    daemon = NodeAgentDaemon(
        AgentConfig(
            control_plane_url="https://control.example.test",
            node_token="pending-token",
            activation_nonce="nonce",
            workdir=str(tmp_path),
            heartbeat_interval_sec=1,
        ),
        client,
        Store(),
    )

    assert daemon.tick() == "unreachable"
    assert client.operations == []
    assert daemon.tick() == "idle"
    assert client.activations == 2
    assert client.operations == ["heartbeat", "poll"]
