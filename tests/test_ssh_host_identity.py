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


# ---------------------------------------------------------------------------
# WP4 closeout pins (DG-SSH-HOSTKEY-v1 H-2/H-3/H-4/H-5): custom-port known_hosts
# derivation, persisted-mismatch short-circuit, SFTP/AsyncSSH consistency,
# authorization, readiness projection, and Job lifecycle preservation.
# ---------------------------------------------------------------------------


def test_known_hosts_files_derive_only_from_canonical_active_identity(tmp_path):
    from app.server_config import materialize_ssh_known_hosts_files

    db = Database(str(tmp_path / "state.db"))
    server = _server()
    untrusted = ServerConfig(name="compute-b", host="203.0.113.9", port=22, user="u", key="/keys/u")

    materialize_ssh_known_hosts_files(db, [server, untrusted], str(tmp_path))
    assert server.host_identity_known_hosts_file is None
    assert untrusted.host_identity_known_hosts_file is None

    trust(db)
    materialize_ssh_known_hosts_files(db, [server, untrusted], str(tmp_path))
    path = server.host_identity_known_hosts_file
    assert path is not None and path.startswith(str(tmp_path / ".dispatch" / "ssh_known_hosts"))
    import os
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    # Custom port uses the OpenSSH bracketed host token so rsync/OpenSSH verifies
    # the same complete public key AsyncSSH pins.
    assert open(path, encoding="ascii").read() == f"[203.0.113.8]:2222 {KEY_A}\n"
    assert untrusted.host_identity_known_hosts_file is None

    # Endpoint drift (host/port differs from the pinned record) never derives a file.
    drifted = ServerConfig(name="compute-a", host="203.0.113.8", port=2200, user="operator", key="/keys/operator")
    materialize_ssh_known_hosts_files(db, [drifted], str(tmp_path))
    assert drifted.host_identity_known_hosts_file is None
    assert not os.path.exists(path)

    # Persisted mismatch evidence removes the derived file (fail closed for rsync too).
    materialize_ssh_known_hosts_files(db, [server], str(tmp_path))
    assert server.host_identity_known_hosts_file == path
    db.record_ssh_host_identity_mismatch(server_name="compute-a", observed_fingerprint_sha256="SHA256:changed")
    materialize_ssh_known_hosts_files(db, [server], str(tmp_path))
    assert server.host_identity_known_hosts_file is None
    assert not os.path.exists(path)

    # Replace Identity is the only recovery: a new active record re-derives the file.
    db.trust_ssh_host_identity(
        server_name="compute-a", host="203.0.113.8", port=2222, public_key=KEY_B,
        algorithm="ssh-ed25519", fingerprint_sha256="SHA256:changed",
        verification_method="oob", actor_id="operator-1", action="replace", reason="reinstalled",
    )
    materialize_ssh_known_hosts_files(db, [server], str(tmp_path))
    assert server.host_identity_known_hosts_file == path
    assert open(path, encoding="ascii").read() == f"[203.0.113.8]:2222 {KEY_B}\n"
    assert db.get_active_ssh_host_identity("compute-a")["mismatch_observed_at"] is None

    db.revoke_ssh_host_identity(server_name="compute-a", actor_id="operator-1")
    materialize_ssh_known_hosts_files(db, [server], str(tmp_path))
    assert server.host_identity_known_hosts_file is None
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_ssh_pool_blocks_persisted_mismatch_and_endpoint_drift_before_network(monkeypatch):
    observed = 0

    async def get_server_host_key(*_args, **_kwargs):
        nonlocal observed
        observed += 1
        return _ObservedKey(KEY_A)

    async def connect(*_args, **_kwargs):
        raise AssertionError("authentication must not start")

    monkeypatch.setattr("app.sshpool.asyncssh.get_server_host_key", get_server_host_key)
    monkeypatch.setattr("app.sshpool.asyncssh.connect", connect)

    mismatched = {
        "id": "identity-a", "host": "203.0.113.8", "port": 2222,
        "public_key": KEY_A, "mismatch_observed_at": "2026-09-23T00:00:00+00:00",
    }
    pool = _pool(lambda _name: mismatched)
    with pytest.raises(SSHUnreachableError, match="changed"):
        await pool.run(_server(), "hostname")
    with pytest.raises(SSHUnreachableError, match="changed"):
        await pool.write_file(_server(), "/tmp/cmd.sh", "echo")
    with pytest.raises(SSHUnreachableError, match="changed"):
        await pool.put_file(_server(), "/local/image.bin", "/remote/image.bin")

    drifted = {
        "id": "identity-a", "host": "203.0.113.8", "port": 22,
        "public_key": KEY_A, "mismatch_observed_at": None,
    }
    pool = _pool(lambda _name: drifted)
    with pytest.raises(SSHUnreachableError, match="rebind"):
        await pool.run(_server(), "hostname")
    with pytest.raises(SSHUnreachableError, match="rebind"):
        await pool.write_file(_server(), "/tmp/cmd.sh", "echo")
    assert observed == 0


@pytest.mark.asyncio
async def test_sftp_paths_share_asyncssh_identity_check(monkeypatch):
    async def get_server_host_key(*_args, **_kwargs):
        return _ObservedKey(KEY_B, "SHA256:changed")

    monkeypatch.setattr("app.sshpool.asyncssh.get_server_host_key", get_server_host_key)
    mismatches: list[str] = []
    identity = {"id": "identity-a", "host": "203.0.113.8", "port": 2222, "public_key": KEY_A, "mismatch_observed_at": None}
    pool = _pool(lambda _name: identity, lambda **evidence: mismatches.append(evidence["observed_fingerprint_sha256"]))
    with pytest.raises(SSHUnreachableError, match="changed"):
        await pool.write_file(_server(), "/tmp/cmd.sh", "echo")
    assert mismatches == ["SHA256:changed"]


def test_reconcile_treats_host_identity_block_as_unreachable_not_failure():
    import asyncio

    from app.jobqueue import reconcile_job
    from app.sshpool import SSHHostIdentityError

    async def blocked(_server_name, _command, _timeout):
        raise SSHHostIdentityError("SSH host identity for compute-a changed")

    outcome = asyncio.run(reconcile_job(blocked, "compute-a", 42))
    assert outcome.status == "unreachable"


def test_host_identity_api_requires_authenticated_platform_manage_human(api_client):
    from app.identity import ActorType, generate_session_token

    client, main_module = api_client
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    main_module.app_state.server_configs = {"compute-a": _server()}
    calls = 0

    async def observe(_cfg):
        nonlocal calls
        calls += 1
        return {"algorithm": "ssh-ed25519", "fingerprint_sha256": "SHA256:x", "public_key": KEY_A}

    main_module.app_state.observe_ssh_host_identity = observe
    body = {"verification_method": "tofu", "acknowledge_tofu": True}

    anonymous = client.post("/api/v2/server-configs/compute-a/host-identity/actions/trust", json=body)
    assert anonymous.status_code == 401

    actor = main_module.app_state.db.insert_actor(
        actor_type=ActorType.HUMAN, display_name="Viewer", platform_admin=False
    )
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id, actor_id=actor.id, secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(config.session_cookie_name, issued.raw_token)
    for path in ("observe", "actions/trust", "actions/rebind", "actions/replace"):
        denied = client.post(f"/api/v2/server-configs/compute-a/host-identity/{path}", json=body)
        assert denied.status_code == 403, path
    revoke = client.post("/api/v2/server-configs/compute-a/host-identity/revoke", json={"reason": "x"})
    assert revoke.status_code == 403
    assert calls == 0
    assert main_module.app_state.db.get_active_ssh_host_identity("compute-a") is None


def test_readiness_projects_host_identity_block_without_job_state(api_client):
    from tests.test_execution_plan_v2_api import _enable_execution_plan_v2, _seed_execution_context
    from tests.test_run_templates_v2 import OPERATOR_ID, _session_for

    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    main_module.app_state.config.project_bootstrap_v2_enabled = True
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)
    db = main_module.app_state.db
    db.insert_server_observation(server_name="pilot-117", online=True, probe_ok=True, gpu_count=1)
    url = f"/api/v2/projects/{seed['project_id']}/workspace"

    def candidates():
        return client.get(url).json()["run_creation_options"]["ssh_target_candidates"]

    assert candidates()[0]["ready"] is True
    with db.cursor() as cursor:
        jobs_before = cursor.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
        plans_before = cursor.execute("SELECT id, job_id FROM execution_plans ORDER BY id").fetchall()

    db.record_ssh_host_identity_mismatch(server_name="pilot-117", observed_fingerprint_sha256="SHA256:other")
    changed = candidates()[0]
    assert changed["ready"] is False
    assert changed["readiness_state"] == "blocked"
    assert changed["readiness_reasons"] == ["ssh_host_identity_changed"]

    db.revoke_ssh_host_identity(server_name="pilot-117", actor_id=OPERATOR_ID, reason="test")
    assert candidates()[0]["readiness_reasons"] == ["ssh_host_identity_untrusted"]

    # H-5: host-identity blocking never adds a Job state or rewrites Job/plan truth.
    with db.cursor() as cursor:
        assert cursor.execute("SELECT id, status FROM jobs ORDER BY id").fetchall() == jobs_before
        assert cursor.execute("SELECT id, job_id FROM execution_plans ORDER BY id").fetchall() == plans_before
