"""DG-ASSISTANT-CLAUDE-TURN v1 C2: `GET /api/v2/ai-providers/status`,
`POST`/`DELETE /api/v2/ai-providers/anthropic-key`.

Covers: feature-gate 404, `_parse_claude_probe_output()` parser (mirrors
`app.main._parse_codex_probe_output()`), the three-tier `assistant_brain`
mode derivation, and the `.env` key set/clear round trip (tmp `.env`
fixture, key value never in response/audit/logs, invalid shape 400 with
zero writes, chmod)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.audit import read_audit
from app.main import _parse_claude_probe_output

VALID_KEY = "sk-ant-" + "a" * 20


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


@pytest.fixture
def ai_providers_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ENV_FILE_PATH", str(tmp_path / ".env"))

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


# ---------------------------------------------------------------------------
# Probe parser (pure, mirrors `_parse_codex_probe_output`)
# ---------------------------------------------------------------------------


def test_parse_claude_probe_output_installed_and_authenticated():
    parsed = _parse_claude_probe_output("claude-code 1.5.0\nCLAUDE_AUTH_OK\n")
    assert parsed == {
        "claude_installed": True,
        "claude_version": "claude-code 1.5.0",
        "authenticated": True,
    }


def test_parse_claude_probe_output_not_installed_and_not_authenticated():
    parsed = _parse_claude_probe_output("NO_CLAUDE\nCLAUDE_AUTH_NO\n")
    assert parsed == {
        "claude_installed": False,
        "claude_version": None,
        "authenticated": False,
    }


def test_parse_claude_probe_output_empty_output_degrades_safely():
    assert _parse_claude_probe_output("") == {
        "claude_installed": False,
        "claude_version": None,
        "authenticated": False,
    }


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def test_flag_off_is_a_hidden_interface(ai_providers_client):
    client, _main = ai_providers_client
    assert client.get("/api/v2/ai-providers/status").status_code == 404
    assert (
        client.post(
            "/api/v2/ai-providers/anthropic-key", json={"api_key": VALID_KEY}
        ).status_code
        == 404
    )
    assert client.delete("/api/v2/ai-providers/anthropic-key").status_code == 404


# ---------------------------------------------------------------------------
# GET status: brain mode derivation
# ---------------------------------------------------------------------------


def test_status_unconfigured_runner_and_no_key_is_rule_based(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    body = client.get("/api/v2/ai-providers/status").json()
    assert body["claude_runner"] == {"configured": False}
    assert body["codex_runner"] == {"configured": False}
    assert body["anthropic"] == {"package_installed": True, "key_configured": False}
    assert body["assistant_brain"]["mode"] == "rule_based"
    assert "未設定 Runner" in body["assistant_brain"]["reason"]


def test_status_configured_authenticated_runner_is_runner_claude(
    ai_providers_client, monkeypatch
):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    main_module.app_state.config.codex_runner_server = "server-a"

    from app.monitor import ServerState

    main_module.app_state.server_states["server-a"] = ServerState(
        name="server-a", online=True
    )

    async def fake_ssh_run(server, command, timeout):
        class _R:
            stdout = "claude-code 1.5.0\nCLAUDE_AUTH_OK\n"

        return _R()

    main_module.app_state.ssh_run = fake_ssh_run

    body = client.get("/api/v2/ai-providers/status").json()
    assert body["claude_runner"] == {
        "configured": True,
        "server": "server-a",
        "online": True,
        "probe_status": "ok",
        "claude_installed": True,
        "claude_version": "claude-code 1.5.0",
        "authenticated": True,
    }
    assert body["assistant_brain"]["mode"] == "runner_claude"


def test_status_configured_but_not_authenticated_is_rule_based_with_reason(
    ai_providers_client,
):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    main_module.app_state.config.codex_runner_server = "server-a"

    from app.monitor import ServerState

    main_module.app_state.server_states["server-a"] = ServerState(
        name="server-a", online=True
    )

    async def fake_ssh_run(server, command, timeout):
        class _R:
            stdout = "claude-code 1.5.0\nCLAUDE_AUTH_NO\n"

        return _R()

    main_module.app_state.ssh_run = fake_ssh_run

    body = client.get("/api/v2/ai-providers/status").json()
    assert body["claude_runner"]["authenticated"] is False
    assert body["assistant_brain"]["mode"] == "rule_based"
    assert "未登入 Claude" in body["assistant_brain"]["reason"]


def test_status_never_leaks_probe_raw_output(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    main_module.app_state.config.codex_runner_server = "server-a"

    from app.monitor import ServerState

    main_module.app_state.server_states["server-a"] = ServerState(
        name="server-a", online=True
    )

    async def fake_ssh_run(server, command, timeout):
        class _R:
            stdout = "claude-code 1.5.0\nCLAUDE_AUTH_OK\nsomeone@example.com\n"

        return _R()

    main_module.app_state.ssh_run = fake_ssh_run

    resp = client.get("/api/v2/ai-providers/status")
    assert "someone@example.com" not in resp.text


# ---------------------------------------------------------------------------
# POST/DELETE anthropic-key
# ---------------------------------------------------------------------------


def test_set_key_success_updates_status_and_never_echoes_value(
    ai_providers_client, caplog
):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    with caplog.at_level("DEBUG"):
        resp = client.post(
            "/api/v2/ai-providers/anthropic-key", json={"api_key": VALID_KEY}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"key_configured": True}
    assert VALID_KEY not in resp.text
    assert VALID_KEY not in caplog.text

    status = client.get("/api/v2/ai-providers/status").json()
    assert status["anthropic"]["key_configured"] is True
    assert main_module.app_state.config.anthropic_api_key == VALID_KEY

    env_path = main_module.app_state.config.env_file_path
    with open(env_path, encoding="utf-8") as fh:
        content = fh.read()
    assert f"ANTHROPIC_API_KEY={VALID_KEY}" in content.splitlines()

    audit_records = read_audit(main_module.app_state.config.audit_path)
    configured_records = [
        r for r in audit_records if r["action"] == "anthropic_api_key_configured"
    ]
    assert len(configured_records) == 1
    assert configured_records[0]["params"] == {}
    assert VALID_KEY not in json.dumps(audit_records)


def test_set_key_invalid_shape_returns_400_and_writes_nothing(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.post(
        "/api/v2/ai-providers/anthropic-key", json={"api_key": "not-a-real-key"}
    )
    assert resp.status_code == 400
    assert main_module.app_state.config.anthropic_api_key in (None, "")

    import os

    env_path = main_module.app_state.config.env_file_path
    assert not os.path.exists(env_path)

    status = client.get("/api/v2/ai-providers/status").json()
    assert status["anthropic"]["key_configured"] is False

    audit_records = read_audit(main_module.app_state.config.audit_path)
    assert not any(
        r["action"] in ("anthropic_api_key_configured", "anthropic_api_key_cleared")
        for r in audit_records
    )


def test_clear_key_removes_line_and_updates_status(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    client.post("/api/v2/ai-providers/anthropic-key", json={"api_key": VALID_KEY})

    resp = client.delete("/api/v2/ai-providers/anthropic-key")
    assert resp.status_code == 200
    assert resp.json() == {"key_configured": False}
    assert main_module.app_state.config.anthropic_api_key is None

    env_path = main_module.app_state.config.env_file_path
    with open(env_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "ANTHROPIC_API_KEY" not in content

    status = client.get("/api/v2/ai-providers/status").json()
    assert status["anthropic"]["key_configured"] is False

    audit_records = read_audit(main_module.app_state.config.audit_path)
    cleared_records = [
        r for r in audit_records if r["action"] == "anthropic_api_key_cleared"
    ]
    assert len(cleared_records) == 1
    assert cleared_records[0]["params"] == {}


def test_set_key_response_never_has_cache_and_chmods_env_file(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.post(
        "/api/v2/ai-providers/anthropic-key", json={"api_key": VALID_KEY}
    )
    assert resp.headers.get("cache-control") == "no-store"

    import os
    import stat

    env_path = main_module.app_state.config.env_file_path
    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600
