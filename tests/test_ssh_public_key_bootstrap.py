"""DG-SSH-KEY-BOOTSTRAP-v1 pins: one-shot public-key install.

- the remote command is a closed builder over a validated key;
- the password connection is pinned to the trusted host key, uses no client
  key, and is closed before returning;
- the API requires an authenticated human platform admin and a trusted host
  identity, and the password never reaches the response, audit, or DB.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import asyncssh
import pytest

from app.config import ServerConfig
from app.ssh_public_key_bootstrap import (
    InstallOutcome,
    build_install_authorized_key_command,
    classify_trusted_identity,
    derive_public_key,
    install_public_key_with_password,
    validate_public_key,
)

SECRET = "hunter2-correct-horse"


def _server() -> ServerConfig:
    return ServerConfig(name="compute-a", host="203.0.113.8", port=2222, user="operator", key="/keys/operator")


def _keypair(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    private = tmp_path / "id_ed25519"
    private.write_bytes(key.export_private_key("openssh"))
    private.chmod(0o600)
    public = key.export_public_key("openssh").decode("ascii").strip()
    return str(private), public


def test_builder_is_closed_and_quotes_the_validated_key():
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa dispatch"
    assert build_install_authorized_key_command(key) == (
        "umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys"
        f" && (grep -qxF -- '{key}' ~/.ssh/authorized_keys || printf '%s\\n' '{key}' >> ~/.ssh/authorized_keys)"
        " && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo PUBLIC_KEY_INSTALLED"
    )
    for bad in ("", "ssh-ed25519", "ssh-ed25519 AAAA; rm -rf ~", "ssh-ed25519 AAAA\ncurl x", "ssh-ed25519 AAAA $(id)", "gpg AAAA"):
        with pytest.raises(ValueError):
            build_install_authorized_key_command(bad)
    assert validate_public_key("  ssh-rsa AAAAB3Nza==  ") == "ssh-rsa AAAAB3Nza=="


def test_derive_public_key_reads_private_key_without_shell(tmp_path):
    private, public = _keypair(tmp_path)
    assert derive_public_key(private) == public
    with pytest.raises(Exception):
        derive_public_key(str(tmp_path / "missing"))


def test_classify_trusted_identity_blocks_untrusted_drift_and_mismatch():
    server = _server()
    assert classify_trusted_identity(None, server) == "ssh_host_identity_untrusted"
    good = {"host": "203.0.113.8", "port": 2222, "mismatch_observed_at": None}
    assert classify_trusted_identity(good, server) is None
    assert classify_trusted_identity(good | {"port": 22}, server) == "ssh_host_identity_rebind_required"
    assert classify_trusted_identity(good | {"mismatch_observed_at": "t"}, server) == "ssh_host_identity_changed"


class _FakeConn:
    def __init__(self, exit_status=0, stdout="PUBLIC_KEY_INSTALLED\n"):
        self.exit_status, self.stdout, self.closed, self.commands = exit_status, stdout, False, []

    async def run(self, command, check=False):
        self.commands.append(command)
        return SimpleNamespace(exit_status=self.exit_status, stdout=self.stdout, stderr="")

    def close(self):
        self.closed = True


def _install(tmp_path, connect, **kwargs):
    _private, public = _keypair(tmp_path)
    trusted = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode("ascii").strip()
    return asyncio.run(
        install_public_key_with_password(
            _server(), trusted_public_key=trusted, public_key=public, password=SECRET,
            connect_timeout=1, command_timeout=1, connect=connect, **kwargs,
        )
    ), public, trusted


def test_password_connection_is_pinned_keyless_single_use(tmp_path):
    calls: list[dict] = []
    conn = _FakeConn()

    async def connect(host, **kwargs):
        calls.append({"host": host, **kwargs})
        return conn

    outcome, public, trusted = _install(tmp_path, connect)
    assert outcome == InstallOutcome(True, "installed", "public key present in authorized_keys")
    assert calls[0]["host"] == "203.0.113.8" and calls[0]["port"] == 2222 and calls[0]["username"] == "operator"
    assert calls[0]["password"] == SECRET
    assert calls[0]["client_keys"] is None and calls[0]["agent_path"] is None
    pinned_keys, _, _ = calls[0]["known_hosts"]
    assert pinned_keys[0].export_public_key("openssh").decode("ascii").strip() == trusted
    assert conn.commands == [build_install_authorized_key_command(public)]
    assert conn.closed is True


@pytest.mark.parametrize(
    "exc, outcome",
    [
        (asyncssh.PermissionDenied("no"), "auth_failed"),
        (asyncssh.HostKeyNotVerifiable("changed"), "host_identity_mismatch"),
        (OSError("down"), "unreachable"),
        (asyncio.TimeoutError(), "unreachable"),
    ],
)
def test_connection_failures_are_classified_without_secrets(tmp_path, exc, outcome):
    async def connect(host, **kwargs):
        raise exc

    result, _public, _trusted = _install(tmp_path, connect)
    assert result.ok is False and result.outcome == outcome
    assert SECRET not in result.detail


def test_remote_failure_closes_connection(tmp_path):
    conn = _FakeConn(exit_status=1, stdout="")

    async def connect(host, **kwargs):
        return conn

    result, _public, _trusted = _install(tmp_path, connect)
    assert result.outcome == "remote_failed" and result.ok is False
    assert conn.closed is True


# --- API --------------------------------------------------------------------

def _admin_session(client, main_module, *, platform_admin=True):
    from app.identity import ActorType, generate_session_token

    db = main_module.app_state.db
    actor = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Admin", platform_admin=platform_admin)
    issued = generate_session_token()
    db.insert_actor_session(session_id=issued.id, actor_id=actor.id, secret_hash=issued.secret_hash, expires_at="2099-01-01T00:00:00+00:00")
    client.cookies.clear()
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)
    return actor


def _prepare(api_client, tmp_path):
    client, main_module = api_client
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    private, _public = _keypair(tmp_path)
    cfg = ServerConfig(name="compute-a", host="203.0.113.8", port=2222, user="operator", key=private)
    main_module.app_state.server_configs = {"compute-a": cfg}
    return client, main_module, cfg


URL = "/api/v2/server-configs/compute-a/install-public-key"


def test_api_requires_authenticated_human_platform_admin(api_client, tmp_path):
    client, main_module, _cfg = _prepare(api_client, tmp_path)
    called = 0

    async def installer(*args, **kwargs):
        nonlocal called
        called += 1
        return InstallOutcome(True, "installed", "x")

    main_module.app_state.install_ssh_public_key = installer
    assert client.post(URL, json={"password": SECRET}).status_code == 401
    _admin_session(client, main_module, platform_admin=False)
    assert client.post(URL, json={"password": SECRET}).status_code == 403
    assert called == 0


def test_api_refuses_until_host_identity_is_trusted(api_client, tmp_path):
    client, main_module, _cfg = _prepare(api_client, tmp_path)
    _admin_session(client, main_module)
    called = 0

    async def installer(*args, **kwargs):
        nonlocal called
        called += 1
        return InstallOutcome(True, "installed", "x")

    main_module.app_state.install_ssh_public_key = installer
    untrusted = client.post(URL, json={"password": SECRET})
    assert untrusted.status_code == 409
    assert untrusted.json()["error"]["code"] == "ssh_host_identity_untrusted"
    assert client.post("/api/v2/server-configs/nope/install-public-key", json={"password": SECRET}).status_code == 404
    assert client.post(URL, json={"password": ""}).status_code == 400
    assert called == 0


def test_api_installs_with_trusted_identity_and_never_persists_the_password(api_client, tmp_path):
    client, main_module, cfg = _prepare(api_client, tmp_path)
    actor = _admin_session(client, main_module)
    db = main_module.app_state.db
    trusted = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode("ascii").strip()
    db.trust_ssh_host_identity(
        server_name="compute-a", host=cfg.host, port=cfg.port, public_key=trusted, algorithm="ssh-ed25519",
        fingerprint_sha256="SHA256:t", verification_method="tofu", actor_id=str(actor.id),
    )
    seen: dict = {}

    async def installer(server, **kwargs):
        seen.update(kwargs, server=server.name)
        return InstallOutcome(True, "installed", "public key present in authorized_keys")

    main_module.app_state.install_ssh_public_key = installer
    response = client.post(URL, json={"password": SECRET})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["outcome"] == "installed"
    assert body["public_key"] == derive_public_key(cfg.key_path)
    assert SECRET not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert seen["server"] == "compute-a" and seen["password"] == SECRET
    assert seen["trusted_public_key"] == trusted and seen["public_key"] == body["public_key"]

    audit_lines = [json.loads(line) for line in open(main_module.app_state.config.audit_path, encoding="utf-8")]
    record = [line for line in audit_lines if line.get("action") == "server_install_public_key"][-1]
    assert record["params"] == {"name": "compute-a", "ok": True, "outcome": "installed"}
    assert SECRET not in json.dumps(audit_lines)
    assert "203.0.113.8" not in json.dumps(record)
    assert SECRET.encode() not in open(main_module.app_state.config.db_path, "rb").read()


def test_api_reports_auth_failure_as_outcome_not_error(api_client, tmp_path):
    client, main_module, cfg = _prepare(api_client, tmp_path)
    actor = _admin_session(client, main_module)
    trusted = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode("ascii").strip()
    main_module.app_state.db.trust_ssh_host_identity(
        server_name="compute-a", host=cfg.host, port=cfg.port, public_key=trusted, algorithm="ssh-ed25519",
        fingerprint_sha256="SHA256:t", verification_method="tofu", actor_id=str(actor.id),
    )

    async def installer(server, **kwargs):
        return InstallOutcome(False, "auth_failed", "password rejected for the configured SSH user")

    main_module.app_state.install_ssh_public_key = installer
    response = client.post(URL, json={"password": SECRET})
    assert response.status_code == 200
    assert response.json()["ok"] is False and response.json()["outcome"] == "auth_failed"
    assert SECRET not in response.text
