from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import ServerConfig
from app.db import Database
from app.sshpool import SSHPool, SSHUnreachableError


KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def trust(db: Database, **overrides):
    values = {
        "server_name": "compute-a",
        "host": "203.0.113.8",
        "port": 2222,
        "public_key": KEY_A,
        "algorithm": "ssh-ed25519",
        "fingerprint_sha256": "SHA256:first",
        "verification_method": "tofu",
        "actor_id": "operator-1",
    }
    values.update(overrides)
    return db.trust_ssh_host_identity(**values)


def test_trust_is_canonical_audited_and_tofu_is_labeled(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    identity = trust(db)
    assert identity["state"] == "active"
    assert identity["independently_verified"] == 0
    assert db.get_active_ssh_host_identity("compute-a")["public_key"] == KEY_A
    event = db._conn.execute(
        "SELECT action, params_json FROM audit_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["action"] == "ssh_host_identity_trust"
    assert KEY_A not in event["params_json"]


def test_rebind_preserves_key_replace_changes_key_and_revoke_closes_trust(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    first = trust(db)
    rebound = trust(db, action="rebind", host="203.0.113.9", port=22)
    assert rebound["public_key"] == KEY_A
    assert rebound["replaces_identity_id"] == first["id"]
    with pytest.raises(ValueError, match="rebind_key_mismatch"):
        trust(db, action="rebind", public_key=KEY_B)
    replaced = trust(
        db,
        action="replace",
        host="203.0.113.9",
        port=22,
        public_key=KEY_B,
        fingerprint_sha256="SHA256:second",
        verification_method="oob",
    )
    assert replaced["independently_verified"] == 1
    revoked = db.revoke_ssh_host_identity(server_name="compute-a", actor_id="operator-1")
    assert revoked["state"] == "revoked"
    assert db.get_active_ssh_host_identity("compute-a") is None


def test_mismatch_persists_blocking_evidence_without_job_state(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    trust(db)
    mismatch = db.record_ssh_host_identity_mismatch(
        server_name="compute-a", observed_fingerprint_sha256="SHA256:changed"
    )
    assert mismatch["state"] == "active"
    assert mismatch["mismatch_fingerprint_sha256"] == "SHA256:changed"
    assert mismatch["mismatch_observed_at"]


class _ObservedKey:
    def __init__(self, public_key: str, fingerprint: str = "SHA256:observed"):
        self.public_key = public_key
        self.fingerprint = fingerprint

    def export_public_key(self, _format: str) -> bytes:
        return (self.public_key + "\n").encode("ascii")

    def get_algorithm(self) -> str:
        return "ssh-ed25519"

    def get_fingerprint(self, _format: str) -> str:
        return self.fingerprint


def _pool(identity_resolver, mismatch_recorder=None) -> SSHPool:
    config = SimpleNamespace(
        ssh_max_concurrency=2,
        ssh_connect_timeout=1.0,
        ssh_command_timeout=1.0,
    )
    return SSHPool(
        config,  # type: ignore[arg-type]
        identity_resolver=identity_resolver,
        mismatch_recorder=mismatch_recorder,
    )


def _server() -> ServerConfig:
    return ServerConfig(
        name="compute-a",
        host="203.0.113.8",
        port=2222,
        user="operator",
        key="/keys/operator",
    )


@pytest.mark.asyncio
async def test_ssh_pool_refuses_untrusted_identity_before_network(monkeypatch):
    observed = False

    async def get_server_host_key(*_args, **_kwargs):
        nonlocal observed
        observed = True
        return _ObservedKey(KEY_A)

    monkeypatch.setattr("app.sshpool.asyncssh.get_server_host_key", get_server_host_key)
    pool = _pool(lambda _name: None)

    with pytest.raises(SSHUnreachableError, match="not trusted"):
        await pool.run(_server(), "hostname")
    assert observed is False


@pytest.mark.asyncio
async def test_ssh_pool_records_full_key_mismatch_and_never_authenticates(monkeypatch):
    connect_called = False
    mismatches: list[str] = []

    async def get_server_host_key(*_args, **_kwargs):
        return _ObservedKey(KEY_B, "SHA256:changed")

    async def connect(*_args, **_kwargs):
        nonlocal connect_called
        connect_called = True
        raise AssertionError("authentication must not start after a key mismatch")

    monkeypatch.setattr("app.sshpool.asyncssh.get_server_host_key", get_server_host_key)
    monkeypatch.setattr("app.sshpool.asyncssh.connect", connect)
    identity = {
        "id": "identity-a",
        "host": "203.0.113.8",
        "port": 2222,
        "public_key": KEY_A,
        "mismatch_observed_at": None,
    }
    pool = _pool(
        lambda _name: identity,
        lambda **evidence: mismatches.append(evidence["observed_fingerprint_sha256"]),
    )

    with pytest.raises(SSHUnreachableError, match="changed"):
        await pool.run(_server(), "hostname")
    assert mismatches == ["SHA256:changed"]
    assert connect_called is False
