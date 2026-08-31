"""dispatch-agent configuration (INV-AGENT-1: secrets never reach the environment)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dispatch_agent.config import (
    AgentConfig,
    ConfigError,
    load_config,
    scrub_environment,
    sdk_environment,
)

CRED = "dar_11111111-2222-4333-8444-555555555555.abcdefghijklmnopqrstuvwxyz012345"


def _write(dir_: Path, *, config=None, agent_env=f"DISPATCH_AGENT_CREDENTIAL={CRED}\n", claude_env="CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n", mode=0o600):
    dir_.mkdir(parents=True, exist_ok=True)
    payload = {"server_url": "https://a.example.ts.net", "runner_name": "worker_5090_106", "workspace_root": str(dir_ / "ws")}
    payload.update(config or {})
    (dir_ / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    for name, text in (("agent.env", agent_env), ("claude.env", claude_env)):
        if text is None:
            continue
        path = dir_ / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)


def test_load_config_reads_secrets_into_memory_only(tmp_path):
    cfg_dir = tmp_path / "cfg"
    _write(cfg_dir)
    config = load_config(cfg_dir, home=tmp_path, euid=1000)
    assert isinstance(config, AgentConfig)
    assert config.runner_name == "worker_5090_106"
    assert config.websocket_url == "wss://a.example.ts.net/agent-runner/ws"
    assert config.credential == CRED and config.claude_oauth_token == "sk-ant-oat-test"
    assert CRED not in repr(config) and "sk-ant" not in repr(config)
    assert "DISPATCH_AGENT_CREDENTIAL" not in os.environ
    assert config.workspace_root == (cfg_dir / "ws").resolve()
    assert "pytest" in config.validation_allowlist


def test_load_config_fails_closed(tmp_path):
    cfg_dir = tmp_path / "cfg"
    _write(cfg_dir)
    with pytest.raises(ConfigError, match="root"):
        load_config(cfg_dir, home=tmp_path, euid=0)
    _write(cfg_dir, mode=0o644)
    with pytest.raises(ConfigError, match="0600"):
        load_config(cfg_dir, home=tmp_path, euid=1000)
    _write(cfg_dir, agent_env="DISPATCH_AGENT_CREDENTIAL=nope\n")
    with pytest.raises(ConfigError, match="DISPATCH_AGENT_CREDENTIAL"):
        load_config(cfg_dir, home=tmp_path, euid=1000)
    _write(cfg_dir, config={"server_url": "ftp://x"})
    with pytest.raises(ConfigError, match="server_url"):
        load_config(cfg_dir, home=tmp_path, euid=1000)
    _write(cfg_dir, config={"workspace_root": "/srv/elsewhere"})
    with pytest.raises(ConfigError, match="home"):
        load_config(cfg_dir, home=tmp_path, euid=1000)
    _write(cfg_dir, config={"validation_allowlist": ["pytest; rm -rf /"]})
    with pytest.raises(ConfigError, match="validation_allowlist"):
        load_config(cfg_dir, home=tmp_path, euid=1000)
    _write(cfg_dir, config={"max_turns": 0})
    with pytest.raises(ConfigError, match="max_turns"):
        load_config(cfg_dir, home=tmp_path, euid=1000)


def test_claude_env_is_optional_but_must_be_private(tmp_path):
    cfg_dir = tmp_path / "cfg"
    _write(cfg_dir, claude_env=None)
    assert load_config(cfg_dir, home=tmp_path, euid=1000).claude_oauth_token is None
    _write(cfg_dir)
    os.chmod(cfg_dir / "claude.env", 0o640)
    with pytest.raises(ConfigError, match="claude.env"):
        load_config(cfg_dir, home=tmp_path, euid=1000)


def test_scrub_environment_and_sdk_environment_whitelist(tmp_path):
    env = {"DISPATCH_AGENT_CREDENTIAL": CRED, "DISPATCH_NODE_TOKEN": "x", "AUTH_TOKEN": "y", "ANTHROPIC_API_KEY": "z", "CLAUDECODE": "1", "HOME": "/home/r", "PATH": "/usr/bin", "SECRET_OTHER": "keep"}
    removed = scrub_environment(env)
    assert set(removed) == {"DISPATCH_AGENT_CREDENTIAL", "DISPATCH_NODE_TOKEN", "AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDECODE"}
    assert "SECRET_OTHER" in env and "HOME" in env

    cfg_dir = tmp_path / "cfg"
    _write(cfg_dir)
    config = load_config(cfg_dir, home=tmp_path, euid=1000)
    sdk_env = sdk_environment(config, base={"HOME": "/home/r", "PATH": "/usr/bin", "DISPATCH_AGENT_CREDENTIAL": CRED, "AUTH_TOKEN": "y"})
    assert sdk_env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
    assert sdk_env["HOME"] == "/home/r" and sdk_env["PATH"] == "/usr/bin"
    assert "DISPATCH_AGENT_CREDENTIAL" not in sdk_env and "AUTH_TOKEN" not in sdk_env
    assert CRED not in json.dumps(sdk_env)


def test_extra_mcp_servers_are_validated_and_never_shadow_dispatch(tmp_path):
    import pytest as _pytest

    from dispatch_agent.config import ConfigError, _parse_extra_mcp_servers

    assert _parse_extra_mcp_servers(None) == {}
    parsed = _parse_extra_mcp_servers({"docs": {"command": " npx ", "args": ["-y", "docs-mcp"], "env": {"A": "1"}}})
    assert parsed == {"docs": {"command": "npx", "args": ["-y", "docs-mcp"], "env": {"A": "1"}}}
    for bad in (
        {"dispatch": {"command": "x"}},
        {"Bad Name": {"command": "x"}},
        {"docs": {"command": ""}},
        {"docs": {"command": "x", "args": [1]}},
        {"docs": {"command": "x", "env": {"A": 1}}},
        "nope",
    ):
        with _pytest.raises(ConfigError):
            _parse_extra_mcp_servers(bad)
