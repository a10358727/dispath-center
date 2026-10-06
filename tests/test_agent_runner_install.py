"""DG-AGENT-RUNNER-INSTALL-v1 pins: platform-managed runner install / keepalive.

- remote commands are closed builders; user text never reaches a shell;
- the credential and Claude token are memory-only: never in audit, DB,
  response (success path) or detail text;
- install needs a trusted host identity; failure keeps the enrolment and shows
  the credential once; keepalive is throttled and flag-gated;
- the Claude token route is human platform-admin only and managed-only.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent_runner_install as ari
from app.audit import read_audit
from app.config import ServerConfig
from app.sshpool import SSHUnreachableError

HOME = "/home/w"
CRED = "dar_123e4567-e89b-12d3-a456-426614174000.abcdefghijklmnopqrstuvwxyz0123456789"
CLAUDE = "sk-ant-oat01-SECRET-TOKEN-ABCDEFGHIJKLMNOP"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


# --- builders ----------------------------------------------------------------

def test_install_spec_is_closed():
    assert ari.validate_install_spec({}) == {"workspace_root": "~/dispatch_workspaces", "launch_mode": "platform_tmux"}
    assert ari.validate_install_spec({"workspace_root": "~/ws/a/"})["workspace_root"] == "~/ws/a"
    for bad in ({"workspace_root": "/tmp/x"}, {"workspace_root": "~/../etc"}, {"workspace_root": "~/a;rm"}, {"launch_mode": "systemd"}, {"extra": 1}, "x"):
        with pytest.raises(ari.InvalidRunnerInstallSpecError):
            ari.validate_install_spec(bad)
    assert ari.is_managed_install({"server": "a", "install": {"workspace_root": "~/w", "launch_mode": "platform_tmux"}})
    assert not ari.is_managed_install({"server": "a"})


def test_commands_are_pinned_and_quoted():
    assert ari.parse_home("/home/w\n") == "/home/w"
    for bad in ("", "relative", "/home/w/../x", "/home/w;id"):
        with pytest.raises(ari.InvalidRunnerInstallSpecError):
            ari.parse_home(bad)
    p = ari.remote_paths(HOME)
    assert p["config_json"] == "/home/w/.config/dispatch-agent/config.json"
    assert ari.build_prepare_command(HOME) == (
        "umask 077 && mkdir -p /home/w/.config/dispatch-agent /home/w/.local/share/dispatch-agent/src"
        " && chmod 700 /home/w/.config/dispatch-agent"
    )
    assert ari.build_install_command(HOME) == (
        "rm -rf /home/w/.local/share/dispatch-agent/src && mkdir -p /home/w/.local/share/dispatch-agent/src"
        " && tar -xzf /home/w/.local/share/dispatch-agent/dispatch_agent_src.tar.gz -C /home/w/.local/share/dispatch-agent/src"
        " && python3 -m pip install --user --quiet --upgrade /home/w/.local/share/dispatch-agent/src && echo RUNNER_INSTALLED"
    )
    assert ari.build_check_command(HOME) == "/home/w/.local/bin/dispatch-agent --check && echo RUNNER_CHECK_OK"
    assert ari.build_launch_command(HOME) == (
        "tmux has-session -t dispatch-agent 2>/dev/null"
        " || tmux new-session -d -s dispatch-agent 'exec /home/w/.local/bin/dispatch-agent run' && echo RUNNER_LAUNCHED"
    )
    assert ari.build_restart_command(HOME).startswith("tmux kill-session -t dispatch-agent 2>/dev/null; tmux new-session -d -s dispatch-agent ")
    assert ari.build_secure_files_command(HOME, with_claude_env=True).endswith("/home/w/.config/dispatch-agent/claude.env")
    quoted = ari.build_prepare_command("/home/o'neil")
    assert "'/home/o'\"'\"'neil/.config/dispatch-agent'" in quoted


def test_renderers_validate_inputs():
    cfg = json.loads(ari.render_config_json(server_url="https://pilot.example:443", runner_name="w1", workspace_root="~/ws"))
    assert cfg == {"server_url": "https://pilot.example:443", "runner_name": "w1", "workspace_root": "~/ws"}
    with pytest.raises(ari.InvalidRunnerInstallSpecError):
        ari.render_config_json(server_url="https://u:p@host/x", runner_name="w1", workspace_root="~/ws")
    assert ari.render_agent_env(CRED) == f"DISPATCH_AGENT_CREDENTIAL={CRED}\n"
    with pytest.raises(ari.InvalidRunnerInstallSpecError):
        ari.render_agent_env("not-a-credential\nEVIL=1")
    assert ari.render_claude_env(CLAUDE) == f"CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}\n"
    with pytest.raises(ari.InvalidRunnerInstallSpecError):
        ari.render_claude_env("short")


def test_source_archive_contains_package_only():
    data = ari.build_source_archive()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = set(archive.getnames())
    assert {"pyproject.toml", "__main__.py", "config.py", "dispatch-agent.service"} <= names
    assert not any("__pycache__" in n or n.endswith(".pyc") or "egg-info" in n for n in names)


# --- orchestration with fake SSH ----------------------------------------------

class FakeSSH:
    def __init__(self, *, fail_step: str | None = None, unreachable: bool = False, stderr: str = ""):
        self.commands: list[str] = []
        self.files: dict[str, str] = {}
        self.uploads: list[tuple[str, str]] = []
        self.fail_step, self.unreachable, self.stderr = fail_step, unreachable, stderr

    async def run(self, server, command, timeout):
        self.commands.append(command)
        if self.unreachable:
            raise SSHUnreachableError("down")
        marker = {"pip install": "RUNNER_INSTALLED", "--check": "RUNNER_CHECK_OK", "tmux": "RUNNER_LAUNCHED"}
        if command == ari.build_home_command():
            return SimpleNamespace(exit_status=0, stdout=HOME, stderr="")
        for needle, step in (("pip install", "install"), ("--check", "check"), ("tmux", "launch")):
            if needle in command:
                if self.fail_step == step:
                    return SimpleNamespace(exit_status=1, stdout="", stderr=self.stderr or f"{step} failed")
                return SimpleNamespace(exit_status=0, stdout=marker[needle] + "\n", stderr="")
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def write_file(self, server, path, content):
        self.files[path] = content

    async def put_file(self, server, local_path, remote_path):
        self.uploads.append((local_path, remote_path))


def _install(ssh: FakeSSH, **kw):
    return asyncio.run(
        ari.install_runner(
            server_name="w1", ssh_run=ssh.run, ssh_write_file=ssh.write_file, ssh_put_file=ssh.put_file,
            server_url="https://pilot.example", runner_name="w1", workspace_root="~/ws", credential=CRED, **kw,
        )
    )


def test_install_runs_every_step_in_order_and_writes_private_files():
    ssh = FakeSSH()
    out = _install(ssh, claude_token=CLAUDE)
    assert out.ok and out.step == "launched"
    assert ssh.commands[0] == ari.build_home_command()
    assert ssh.commands[1] == ari.build_prepare_command(HOME)
    assert ssh.commands[-3:] == [ari.build_install_command(HOME), ari.build_check_command(HOME), ari.build_launch_command(HOME)]
    assert ssh.uploads[0][1] == "/home/w/.local/share/dispatch-agent/dispatch_agent_src.tar.gz"
    assert json.loads(ssh.files["/home/w/.config/dispatch-agent/config.json"])["runner_name"] == "w1"
    assert ssh.files["/home/w/.config/dispatch-agent/agent.env"] == f"DISPATCH_AGENT_CREDENTIAL={CRED}\n"
    assert ssh.files["/home/w/.config/dispatch-agent/claude.env"] == f"CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}\n"
    assert ari.build_secure_files_command(HOME, with_claude_env=True) in ssh.commands
    assert CRED not in out.detail and CLAUDE not in out.detail


@pytest.mark.parametrize("fail_step", ["install", "check", "launch"])
def test_install_failures_are_classified_and_scrubbed(fail_step):
    ssh = FakeSSH(fail_step=fail_step, stderr=f"boom {CRED} {CLAUDE}")
    out = _install(ssh, claude_token=CLAUDE)
    assert out.ok is False and out.step == fail_step
    assert CRED not in out.detail and CLAUDE not in out.detail and "<redacted>" in out.detail


def test_unreachable_host_stops_before_any_upload():
    ssh = FakeSSH(unreachable=True)
    out = _install(ssh)
    assert out.ok is False and out.step == "home"
    assert ssh.uploads == [] and ssh.files == {}


def test_relaunch_and_claude_token_flows():
    ssh = FakeSSH()
    out = asyncio.run(ari.relaunch_runner(server_name="w1", ssh_run=ssh.run))
    assert out.ok and ssh.commands[-1] == ari.build_launch_command(HOME)
    down = asyncio.run(ari.relaunch_runner(server_name="w1", ssh_run=FakeSSH(unreachable=True).run))
    assert down.ok is False and down.step == "launch"

    ssh = FakeSSH()
    out = asyncio.run(ari.set_claude_token(server_name="w1", ssh_run=ssh.run, ssh_write_file=ssh.write_file, claude_token=CLAUDE))
    assert out.ok and ssh.files["/home/w/.config/dispatch-agent/claude.env"].endswith(f"{CLAUDE}\n")
    assert ssh.commands[-1] == ari.build_restart_command(HOME)
    bad = asyncio.run(ari.set_claude_token(server_name="w1", ssh_run=ssh.run, ssh_write_file=ssh.write_file, claude_token="nope"))
    assert bad.ok is False and bad.step == "config"


# --- approval flow, keepalive, API -------------------------------------------

@pytest.fixture
def install_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("API_V2_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUNTIME_V3_ENABLED", "true")
    import app.main as main_module

    with TestClient(main_module.app) as client:
        state = main_module.app_state
        state.server_configs["worker-a"] = ServerConfig(name="worker-a", host="10.0.0.1", port=22, user="w", key="~/.ssh/k")
        ssh = FakeSSH()
        state.ssh_run = ssh.run
        state.ssh_write_file = ssh.write_file
        state.ssh_put_file = ssh.put_file
        yield client, state, ssh


def _trust(state, name="worker-a"):
    state.db.trust_ssh_host_identity(
        server_name=name, host="10.0.0.1", port=22, public_key=KEY, algorithm="ssh-ed25519",
        fingerprint_sha256="SHA256:x", verification_method="tofu", actor_id="t",
    )


def _admin(client, state, *, platform_admin=True):
    from app.identity import ActorType, generate_session_token

    actor = state.db.insert_actor(actor_type=ActorType.HUMAN, display_name="Admin", platform_admin=platform_admin)
    issued = generate_session_token()
    state.db.insert_actor_session(session_id=issued.id, actor_id=actor.id, secret_hash=issued.secret_hash, expires_at="2099-01-01T00:00:00+00:00")
    client.cookies.clear()
    client.cookies.set(state.config.session_cookie_name, issued.raw_token)
    return actor


def _approve(state, approval_id):
    from app.approvals import approve

    return asyncio.run(approve(state.db, approval_id, server_configs=state.server_configs, app_state=state, audit_path=state.config.audit_path))


def test_install_request_requires_trusted_identity_and_approval_provisions(install_client):
    client, state, ssh = install_client
    body = {"server": "worker-a", "install": {"workspace_root": "~/ws"}}
    assert client.post("/api/v2/agent-runners/enroll-requests", json=body).status_code == 400
    _trust(state)
    created = client.post("/api/v2/agent-runners/enroll-requests", json=body)
    assert created.status_code == 202, created.text
    approval = created.json()["approval"]
    assert approval["payload"] == {"server": "worker-a", "install": {"workspace_root": "~/ws", "launch_mode": "platform_tmux"}}

    result = _approve(state, approval["id"])
    assert result["approval"].status == "approved"
    assert result["agent_runner_install"]["ok"] is True and result["agent_runner_install"]["step"] == "launched"
    assert "agent_runner_token" not in result  # consumed by the install
    runner = result["agent_runner"]
    cred = ssh.files["/home/w/.config/dispatch-agent/agent.env"].split("=", 1)[1].strip()
    assert cred.startswith(f"dar_{runner.id}.")
    cfg = json.loads(ssh.files["/home/w/.config/dispatch-agent/config.json"])
    assert cfg["runner_name"] == "worker-a" and cfg["workspace_root"] == "~/ws" and cfg["server_url"].startswith("http")
    audit_text = open(state.config.audit_path, encoding="utf-8").read()
    assert cred not in audit_text and cred.split(".", 1)[1] not in audit_text
    actions = [row["action"] for row in read_audit(state.config.audit_path)]
    assert "agent_runner_install" in actions
    listed = client.get("/api/v2/agent-runners").json()["agent_runners"]
    assert listed[0]["managed"] is True and listed[0]["active"] is True


def test_failed_install_keeps_enrolment_and_returns_credential_once(install_client):
    client, state, ssh = install_client
    ssh.fail_step = "install"
    _trust(state)
    approval = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a", "install": {}}).json()["approval"]
    result = _approve(state, approval["id"])
    assert result["approval"].status == "approved"
    assert result["agent_runner_install"]["ok"] is False and result["agent_runner_install"]["step"] == "install"
    assert result["agent_runner_token"].startswith("dar_")
    assert result["agent_runner"].is_active
    assert result["agent_runner_token"] not in open(state.config.audit_path, encoding="utf-8").read()


def test_plain_enrol_without_install_is_unchanged(install_client):
    client, state, ssh = install_client
    approval = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).json()["approval"]
    assert approval["payload"] == {"server": "worker-a"}
    result = _approve(state, approval["id"])
    assert result["agent_runner_token"].startswith("dar_") and "agent_runner_install" not in result
    assert ssh.commands == [] and ssh.files == {}
    assert client.get("/api/v2/agent-runners").json()["agent_runners"][0]["managed"] is False


def test_keepalive_relaunches_managed_disconnected_runners_with_throttle(install_client, monkeypatch):
    client, state, ssh = install_client
    _trust(state)
    approval = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a", "install": {}}).json()["approval"]
    _approve(state, approval["id"])
    ssh.commands.clear()

    first = asyncio.run(state.agent_runner_keepalive_tick())
    assert len(first) == 1 and first[0]["ok"] is True
    assert ssh.commands[-1] == ari.build_launch_command(HOME)
    second = asyncio.run(state.agent_runner_keepalive_tick())
    assert second == []  # throttled

    state._agent_runner_relaunch_at.clear()
    state.config.agent_runner_platform_launch_enabled = False
    assert asyncio.run(state.agent_runner_keepalive_tick()) == []
    state.config.agent_runner_platform_launch_enabled = True

    runner_id = first[0]["runner_id"]
    from app.agent_gateway import ensure_agent_gateway

    monkeypatch.setattr(ensure_agent_gateway(state), "connected_runner_ids", lambda: {runner_id})
    assert asyncio.run(state.agent_runner_keepalive_tick()) == []  # connected => nothing to do
    actions = [row for row in read_audit(state.config.audit_path) if row["action"] == "agent_runner_relaunch"]
    assert len(actions) == 1 and actions[0]["params"]["runner_id"] == runner_id


def test_claude_token_route_is_human_admin_managed_only_and_never_persists(install_client):
    client, state, ssh = install_client
    _trust(state)
    plain = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).json()["approval"]
    plain_runner = _approve(state, plain["id"])["agent_runner"]
    url = f"/api/v2/agent-runners/{plain_runner.id}/claude-token"
    assert client.post(url, json={"token": CLAUDE}).status_code == 401
    _admin(client, state, platform_admin=False)
    assert client.post(url, json={"token": CLAUDE}).status_code == 403
    _admin(client, state)
    assert client.post(url, json={"token": CLAUDE}).json()["error"]["code"] == "runner_not_managed"

    state.server_configs["worker-b"] = ServerConfig(name="worker-b", host="10.0.0.1", port=22, user="w", key="~/.ssh/k")
    _trust(state, "worker-b")
    managed = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-b", "install": {}}).json()["approval"]
    managed_runner = _approve(state, managed["id"])["agent_runner"]
    url = f"/api/v2/agent-runners/{managed_runner.id}/claude-token"
    assert client.post(url, json={"token": ""}).status_code == 400
    ok = client.post(url, json={"token": CLAUDE})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True and CLAUDE not in ok.text
    assert ok.headers["Cache-Control"] == "no-store"
    assert ssh.files["/home/w/.config/dispatch-agent/claude.env"].endswith(f"{CLAUDE}\n")
    assert CLAUDE not in open(state.config.audit_path, encoding="utf-8").read()
    assert CLAUDE.encode() not in open(state.config.db_path, "rb").read()
    assert any(row["action"] == "agent_runner_claude_token" for row in read_audit(state.config.audit_path))
