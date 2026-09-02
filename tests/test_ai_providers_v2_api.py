"""DG-ASSISTANT-CLAUDE-TURN v1 C2: `GET /api/v2/ai-providers/status`,
`POST`/`DELETE /api/v2/ai-providers/anthropic-key`.

Covers: feature-gate 404, `_parse_claude_probe_output()` parser (mirrors
`app.main._parse_codex_probe_output()`), the (Phase 1b two-tier) `assistant_brain`
mode derivation, and the `.env` key set/clear round trip (tmp `.env`
fixture, key value never in response/audit/logs, invalid shape 400 with
zero writes, chmod)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.audit import read_audit

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








# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("legacy_posture")
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


# ---------------------------------------------------------------------------
# Packet D2: POST /api/v2/ai-providers/assistant-model, .../api-model
# ---------------------------------------------------------------------------


def test_set_assistant_model_updates_env_config_and_audits_value(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.post(
        "/api/v2/ai-providers/assistant-model", json={"model": "sonnet"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"assistant_claude_model": "sonnet"}
    assert main_module.app_state.config.assistant_claude_model == "sonnet"

    env_path = main_module.app_state.config.env_file_path
    with open(env_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "ASSISTANT_CLAUDE_MODEL=sonnet" in content.splitlines()

    audit_records = read_audit(main_module.app_state.config.audit_path)
    configured = [
        r for r in audit_records if r["action"] == "assistant_claude_model_configured"
    ]
    assert len(configured) == 1
    # Unlike the Anthropic key, a model name IS included in the audit params.
    assert configured[0]["params"] == {"model": "sonnet"}


def test_set_assistant_model_empty_clears(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    client.post("/api/v2/ai-providers/assistant-model", json={"model": "opus"})

    resp = client.post("/api/v2/ai-providers/assistant-model", json={"model": ""})
    assert resp.status_code == 200
    assert resp.json() == {"assistant_claude_model": ""}
    assert main_module.app_state.config.assistant_claude_model == ""

    env_path = main_module.app_state.config.env_file_path
    with open(env_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "ASSISTANT_CLAUDE_MODEL" not in content


@pytest.mark.parametrize(
    "value",
    ["sonnet; rm -rf /", "sonnet with space", "$(whoami)", "a" * 65],
)
def test_set_assistant_model_rejects_shell_metacharacters(ai_providers_client, value):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.post("/api/v2/ai-providers/assistant-model", json={"model": value})
    assert resp.status_code == 400
    assert main_module.app_state.config.assistant_claude_model == ""


def test_set_api_model_updates_llm_model_and_audits_value(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.post("/api/v2/ai-providers/api-model", json={"model": "opus"})
    assert resp.status_code == 200
    assert resp.json() == {"llm_model": "opus"}
    assert main_module.app_state.config.llm_model == "opus"

    audit_records = read_audit(main_module.app_state.config.audit_path)
    configured = [r for r in audit_records if r["action"] == "llm_model_configured"]
    assert len(configured) == 1
    assert configured[0]["params"] == {"model": "opus"}


def test_set_api_model_rejects_shell_metacharacters_writes_nothing(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    original_model = main_module.app_state.config.llm_model

    resp = client.post(
        "/api/v2/ai-providers/api-model", json={"model": "sonnet\nEVIL=1"}
    )
    assert resp.status_code == 400
    assert main_module.app_state.config.llm_model == original_model


@pytest.mark.usefixtures("legacy_posture")
def test_model_endpoints_are_hidden_when_flag_off(ai_providers_client):
    client, _main = ai_providers_client
    assert (
        client.post(
            "/api/v2/ai-providers/assistant-model", json={"model": "sonnet"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v2/ai-providers/api-model", json={"model": "sonnet"}
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Packet D3: GET /api/v2/ai-providers/usage
# ---------------------------------------------------------------------------


def test_get_usage_empty_ledger_returns_zeroed_totals(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.get("/api/v2/ai-providers/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 7
    assert body["totals"] == {"turns": 0, "input_tokens": 0, "output_tokens": 0}
    assert body["breakdown"] == []


def test_get_usage_aggregates_recorded_rows(ai_providers_client):
    client, main_module = ai_providers_client
    _enable_v2(main_module)
    main_module.app_state.db.record_assistant_usage(
        channel="runner_claude",
        server="server-a",
        model=None,
        input_tokens=10,
        output_tokens=20,
        duration_ms=500,
    )
    main_module.app_state.db.record_assistant_usage(
        channel="api", model="claude-sonnet-5", input_tokens=5, output_tokens=6
    )

    resp = client.get("/api/v2/ai-providers/usage?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 30
    assert body["totals"] == {"turns": 2, "input_tokens": 15, "output_tokens": 26}
    channels = {row["channel"] for row in body["breakdown"]}
    assert channels == {"runner_claude", "api"}


@pytest.mark.parametrize("days", [0, -1, 91])
def test_get_usage_rejects_out_of_range_days(ai_providers_client, days):
    client, main_module = ai_providers_client
    _enable_v2(main_module)

    resp = client.get(f"/api/v2/ai-providers/usage?days={days}")
    assert resp.status_code == 422


@pytest.mark.usefixtures("legacy_posture")
def test_usage_endpoint_is_hidden_when_flag_off(ai_providers_client):
    client, _main = ai_providers_client
    assert client.get("/api/v2/ai-providers/usage").status_code == 404
