"""階段 13（PLAN.md N.1）：六個 CODEX_* 設定鍵的預設值、.env 載入、以及
`apply_codex_config_rules()` 的驗證/降級規則。"""

import pytest

from app.config import AppConfig, apply_codex_config_rules, load_app_config


# ---------------------------------------------------------------------------
# 六個 CODEX_* 鍵：預設值
# ---------------------------------------------------------------------------


def test_codex_config_defaults():
    config = AppConfig(servers=[])
    assert config.codex_runner_server is None
    assert config.codex_workspace_root == "~/codex_workspaces"
    assert config.codex_max_concurrency == 1
    assert config.codex_runner_reserve is True
    assert config.codex_network_access is False
    assert config.codex_auth_mode == "chatgpt"


# ---------------------------------------------------------------------------
# load_app_config()：.env / 環境變數載入
# ---------------------------------------------------------------------------


def test_load_app_config_reads_codex_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "server-c")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", "/srv/codex_ws")
    monkeypatch.setenv("CODEX_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("CODEX_RUNNER_RESERVE", "false")
    monkeypatch.setenv("CODEX_NETWORK_ACCESS", "true")
    monkeypatch.setenv("CODEX_AUTH_MODE", "api_key")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )
    assert config.codex_runner_server == "server-c"
    assert config.codex_workspace_root == "/srv/codex_ws"
    assert config.codex_max_concurrency == 3
    assert config.codex_runner_reserve is False
    assert config.codex_network_access is True
    assert config.codex_auth_mode == "api_key"


def test_load_app_config_empty_or_blank_runner_server_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "   ")
    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )
    assert config.codex_runner_server is None


def test_load_app_config_codex_defaults_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_RUNNER_SERVER", raising=False)
    monkeypatch.delenv("CODEX_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("CODEX_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("CODEX_RUNNER_RESERVE", raising=False)
    monkeypatch.delenv("CODEX_NETWORK_ACCESS", raising=False)
    monkeypatch.delenv("CODEX_AUTH_MODE", raising=False)

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )
    assert config.codex_runner_server is None
    assert config.codex_workspace_root == "~/codex_workspaces"
    assert config.codex_max_concurrency == 1
    assert config.codex_runner_reserve is True
    assert config.codex_network_access is False
    assert config.codex_auth_mode == "chatgpt"


# ---------------------------------------------------------------------------
# apply_codex_config_rules()
# ---------------------------------------------------------------------------


def test_apply_codex_config_rules_unset_runner_is_noop():
    config = AppConfig(servers=[])
    assert config.codex_runner_server is None
    warnings = apply_codex_config_rules(config, {})
    assert warnings == []
    # 沒有被動過
    assert config.codex_max_concurrency == 1


def test_apply_codex_config_rules_unknown_server_raises():
    config = AppConfig(servers=[], codex_runner_server="server-x")
    with pytest.raises(ValueError, match="servers.yaml 既有的 server"):
        apply_codex_config_rules(config, {"server-a": True})


def test_apply_codex_config_rules_disabled_server_raises():
    config = AppConfig(servers=[], codex_runner_server="server-a")
    with pytest.raises(ValueError, match="servers.yaml 既有的 server"):
        apply_codex_config_rules(config, {"server-a": False})


def test_apply_codex_config_rules_invalid_auth_mode_raises():
    config = AppConfig(
        servers=[], codex_runner_server="server-a", codex_auth_mode="password"
    )
    with pytest.raises(ValueError, match="CODEX_AUTH_MODE"):
        apply_codex_config_rules(config, {"server-a": True})


def test_apply_codex_config_rules_chatgpt_mode_downgrades_concurrency_with_warning():
    config = AppConfig(
        servers=[],
        codex_runner_server="server-a",
        codex_auth_mode="chatgpt",
        codex_max_concurrency=3,
    )
    warnings = apply_codex_config_rules(config, {"server-a": True})
    assert config.codex_max_concurrency == 1
    assert len(warnings) == 1
    assert "降為 1" in warnings[0]
    assert "3" in warnings[0]


def test_apply_codex_config_rules_api_key_mode_does_not_downgrade():
    config = AppConfig(
        servers=[],
        codex_runner_server="server-a",
        codex_auth_mode="api_key",
        codex_max_concurrency=3,
    )
    warnings = apply_codex_config_rules(config, {"server-a": True})
    assert config.codex_max_concurrency == 3
    assert warnings == []


def test_apply_codex_config_rules_valid_chatgpt_single_concurrency_no_warning():
    config = AppConfig(
        servers=[],
        codex_runner_server="server-a",
        codex_auth_mode="chatgpt",
        codex_max_concurrency=1,
    )
    warnings = apply_codex_config_rules(config, {"server-a": True})
    assert warnings == []
    assert config.codex_max_concurrency == 1
