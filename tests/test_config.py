"""階段 13（PLAN.md N.1）：六個 CODEX_* 設定鍵的預設值、.env 載入、以及
`apply_codex_config_rules()` 的驗證/降級規則。"""

import pytest

from app.config import AppConfig, apply_codex_config_rules, load_app_config


def _enabled_oidc_config(**overrides):
    values = {
        "oidc_enabled": True,
        "oidc_issuer": "https://idp.example.test/tenant",
        "oidc_client_id": "dispatch-center",
        "oidc_client_secret": "fake-client-secret",
        "oidc_redirect_uri": "https://dispatch.example.test/auth/callback",
    }
    values.update(overrides)
    return AppConfig(servers=[], **values)


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


def test_goal1_auth_transport_defaults():
    config = AppConfig(servers=[])
    assert config.legacy_shared_token_enabled is True
    assert config.service_token_auth_enabled is False
    assert config.authorization_mode == "off"
    assert config.session_cookie_name == "dispatch_session"
    assert config.identity_admin_enabled is False
    assert config.engineering_task_backend_v1 is False
    assert config.oidc_enabled is False
    assert config.oidc_issuer is None
    assert config.oidc_client_id is None
    assert config.oidc_client_secret is None
    assert config.oidc_redirect_uri is None
    assert config.oidc_scopes == ("openid", "profile", "email")
    assert config.oidc_platform_admin_subjects == frozenset()
    assert config.oidc_login_flow_ttl_sec == 600
    assert config.oidc_session_ttl_sec == 28800
    assert config.oidc_flow_cookie_name == "dispatch_oidc_flow"
    assert config.oidc_provider_timeout_sec == 10.0
    assert config.oidc_clock_skew_leeway_sec == 60
    assert config.execution_attempt_shadow_enabled is False
    assert config.execution_attempt_new_claims_enabled is False
    assert config.execution_attempt_reconcile_existing is False
    assert config.execution_outbox_worker_enabled is False


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


def test_load_app_config_reads_goal1_auth_transport_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGACY_SHARED_TOKEN_ENABLED", "false")
    monkeypatch.setenv("SERVICE_TOKEN_AUTH_ENABLED", "yes")
    monkeypatch.setenv("SESSION_COOKIE_NAME", "custom_session")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.legacy_shared_token_enabled is False
    assert config.service_token_auth_enabled is True
    assert config.session_cookie_name == "custom_session"


def test_load_app_config_reads_engineering_task_backend_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINEERING_TASK_BACKEND_V1", "yes")
    monkeypatch.setenv(
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION", "yes"
    )

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.engineering_task_backend_v1 is True
    assert (
        config.engineering_task_backend_v1_accept_unsandboxed_finalization is True
    )


def test_load_app_config_reads_execution_attempt_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_ATTEMPT_SHADOW_ENABLED", "yes")
    monkeypatch.setenv("EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_ATTEMPT_RECONCILE_EXISTING", "on")
    monkeypatch.setenv("EXECUTION_OUTBOX_WORKER_ENABLED", "1")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.execution_attempt_shadow_enabled is True
    assert config.execution_attempt_new_claims_enabled is True
    assert config.execution_attempt_reconcile_existing is True
    assert config.execution_outbox_worker_enabled is True


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "execution_attempt_new_claims_enabled": True,
            "execution_attempt_reconcile_existing": False,
            "execution_outbox_worker_enabled": True,
        },
        {
            "execution_attempt_new_claims_enabled": True,
            "execution_attempt_reconcile_existing": True,
            "execution_outbox_worker_enabled": False,
        },
    ],
)
def test_execution_new_claims_require_reconcile_and_outbox(overrides):
    with pytest.raises(ValueError, match="NEW_CLAIMS_ENABLED"):
        AppConfig(servers=[], **overrides)


def test_engineering_backend_alone_fails_closed_without_d2_acknowledgment():
    with pytest.raises(ValueError, match="ACCEPT_UNSANDBOXED_FINALIZATION"):
        AppConfig(servers=[], engineering_task_backend_v1=True)


def test_load_app_config_engineering_backend_alone_fails_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ENGINEERING_TASK_BACKEND_V1", "true")
    monkeypatch.delenv(
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION",
        raising=False,
    )

    with pytest.raises(ValueError, match="ACCEPT_UNSANDBOXED_FINALIZATION"):
        load_app_config(
            servers_yaml_path=str(tmp_path / "servers.yaml"),
            dotenv_path=str(tmp_path / ".env"),
        )


def test_d2_acknowledgment_alone_does_not_enable_the_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINEERING_TASK_BACKEND_V1", raising=False)
    monkeypatch.setenv(
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION", "true"
    )

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.engineering_task_backend_v1 is False


def test_load_app_config_engineering_task_backend_defaults_disabled(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("ENGINEERING_TASK_BACKEND_V1", raising=False)

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.engineering_task_backend_v1 is False


def test_load_app_config_reads_complete_oidc_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.test/tenant")
    monkeypatch.setenv("OIDC_CLIENT_ID", "dispatch-center")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "fake-config-secret")
    monkeypatch.setenv(
        "OIDC_REDIRECT_URI", "https://dispatch.example.test/auth/callback"
    )
    monkeypatch.setenv("OIDC_SCOPES", "openid profile groups profile")
    monkeypatch.setenv("OIDC_PLATFORM_ADMIN_SUBJECTS", "subject-b, subject-a,subject-b")
    monkeypatch.setenv("OIDC_LOGIN_FLOW_TTL_SEC", "420")
    monkeypatch.setenv("OIDC_SESSION_TTL_SEC", "14400")
    monkeypatch.setenv("OIDC_FLOW_COOKIE_NAME", "dispatch_flow_custom")
    monkeypatch.setenv("OIDC_PROVIDER_TIMEOUT_SEC", "7.5")
    monkeypatch.setenv("OIDC_CLOCK_SKEW_LEEWAY_SEC", "30")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.oidc_enabled is True
    assert config.oidc_issuer == "https://idp.example.test/tenant"
    assert config.oidc_client_id == "dispatch-center"
    assert config.oidc_client_secret == "fake-config-secret"
    assert config.oidc_redirect_uri == "https://dispatch.example.test/auth/callback"
    assert config.oidc_scopes == ("openid", "profile", "groups")
    assert config.oidc_platform_admin_subjects == frozenset(
        {"subject-a", "subject-b"}
    )
    assert config.oidc_login_flow_ttl_sec == 420
    assert config.oidc_session_ttl_sec == 14400
    assert config.oidc_flow_cookie_name == "dispatch_flow_custom"
    assert config.oidc_provider_timeout_sec == 7.5
    assert config.oidc_clock_skew_leeway_sec == 30
    assert "fake-config-secret" not in repr(config)


@pytest.mark.parametrize(
    ("field_name", "setting_name", "missing_value"),
    [
        ("oidc_issuer", "OIDC_ISSUER", None),
        ("oidc_client_id", "OIDC_CLIENT_ID", ""),
        ("oidc_client_secret", "OIDC_CLIENT_SECRET", "   "),
        ("oidc_redirect_uri", "OIDC_REDIRECT_URI", None),
    ],
)
def test_oidc_enabled_requires_complete_provider_config(
    field_name, setting_name, missing_value
):
    with pytest.raises(ValueError, match=setting_name):
        _enabled_oidc_config(**{field_name: missing_value})


def test_oidc_disabled_is_a_rollback_switch_for_incomplete_provider_config():
    config = AppConfig(
        servers=[],
        oidc_enabled=False,
        oidc_issuer="not-an-active-url",
        oidc_redirect_uri="http://not-used.invalid/callback",
    )
    assert config.oidc_enabled is False


@pytest.mark.parametrize(
    ("overrides", "setting_name"),
    [
        (
            {"oidc_issuer": "http://127.0.0.1:9000"},
            "OIDC_ISSUER",
        ),
        (
            {"oidc_issuer": "https://idp.example.test/tenant?unsafe=1"},
            "OIDC_ISSUER",
        ),
        (
            {"oidc_issuer": "https://idp.example.test:not-a-port/tenant"},
            "OIDC_ISSUER",
        ),
        (
            {"oidc_redirect_uri": "http://127.0.0.1:8000/auth/callback"},
            "OIDC_REDIRECT_URI",
        ),
        (
            {"oidc_redirect_uri": "https://dispatch.example.test/wrong-callback"},
            "OIDC_REDIRECT_URI",
        ),
        (
            {
                "oidc_redirect_uri": (
                    "https://dispatch.example.test/auth/callback?unsafe=1"
                )
            },
            "OIDC_REDIRECT_URI",
        ),
    ],
)
def test_enabled_oidc_requires_https_issuer_and_exact_https_callback(
    overrides, setting_name
):
    with pytest.raises(ValueError, match=setting_name):
        _enabled_oidc_config(**overrides)


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "setting_name"),
    [
        ("oidc_login_flow_ttl_sec", 0, "OIDC_LOGIN_FLOW_TTL_SEC"),
        ("oidc_login_flow_ttl_sec", 1.5, "OIDC_LOGIN_FLOW_TTL_SEC"),
        ("oidc_session_ttl_sec", -1, "OIDC_SESSION_TTL_SEC"),
        ("oidc_session_ttl_sec", 1.5, "OIDC_SESSION_TTL_SEC"),
        ("oidc_provider_timeout_sec", 0.0, "OIDC_PROVIDER_TIMEOUT_SEC"),
        ("oidc_provider_timeout_sec", float("nan"), "OIDC_PROVIDER_TIMEOUT_SEC"),
        ("oidc_provider_timeout_sec", float("inf"), "OIDC_PROVIDER_TIMEOUT_SEC"),
        ("oidc_clock_skew_leeway_sec", -1, "OIDC_CLOCK_SKEW_LEEWAY_SEC"),
        ("oidc_clock_skew_leeway_sec", 1.5, "OIDC_CLOCK_SKEW_LEEWAY_SEC"),
        ("oidc_clock_skew_leeway_sec", 301, "OIDC_CLOCK_SKEW_LEEWAY_SEC"),
    ],
)
def test_oidc_durations_and_clock_leeway_are_bounded(
    field_name, invalid_value, setting_name
):
    with pytest.raises(ValueError, match=setting_name):
        AppConfig(servers=[], **{field_name: invalid_value})


@pytest.mark.parametrize("leeway", [0, 300])
def test_oidc_clock_leeway_accepts_bounded_endpoints(leeway):
    assert AppConfig(
        servers=[], oidc_clock_skew_leeway_sec=leeway
    ).oidc_clock_skew_leeway_sec == leeway


@pytest.mark.parametrize(
    "scopes",
    [(), ("profile", "email"), "openid profile", ("openid", "bad scope")],
)
def test_oidc_scopes_require_a_tokenized_openid_scope(scopes):
    with pytest.raises(ValueError, match="OIDC_SCOPES"):
        AppConfig(servers=[], oidc_scopes=scopes)


@pytest.mark.parametrize(
    ("field_name", "cookie_name", "setting_name"),
    [
        ("session_cookie_name", "bad cookie", "SESSION_COOKIE_NAME"),
        ("oidc_flow_cookie_name", "bad=cookie", "OIDC_FLOW_COOKIE_NAME"),
    ],
)
def test_auth_cookie_names_must_be_safe_http_tokens(
    field_name, cookie_name, setting_name
):
    with pytest.raises(ValueError, match=setting_name):
        AppConfig(servers=[], **{field_name: cookie_name})


def test_oidc_flow_and_session_cookie_names_must_be_distinct():
    with pytest.raises(ValueError, match="must be distinct"):
        AppConfig(
            servers=[],
            session_cookie_name="same_cookie",
            oidc_flow_cookie_name="same_cookie",
        )


def test_load_app_config_reads_shadow_authorization_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTHORIZATION_MODE", "shadow")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.authorization_mode == "shadow"


def test_load_app_config_authorization_mode_defaults_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTHORIZATION_MODE", raising=False)

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.authorization_mode == "off"


@pytest.mark.parametrize("invalid_mode", ["enforce", "audit", "SHADOW", ""])
def test_load_app_config_rejects_unsupported_authorization_mode(
    monkeypatch, tmp_path, invalid_mode
):
    monkeypatch.setenv("AUTHORIZATION_MODE", invalid_mode)

    with pytest.raises(ValueError, match="AUTHORIZATION_MODE"):
        load_app_config(
            servers_yaml_path=str(tmp_path / "servers.yaml"),
            dotenv_path=str(tmp_path / ".env"),
        )


def test_app_config_rejects_enforcement_mode():
    with pytest.raises(ValueError, match="AUTHORIZATION_MODE"):
        AppConfig(servers=[], authorization_mode="enforce")


def test_blank_session_cookie_name_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_COOKIE_NAME", "   ")
    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )
    assert config.session_cookie_name == "dispatch_session"


def test_load_app_config_reads_identity_admin_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("IDENTITY_ADMIN_ENABLED", "yes")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.identity_admin_enabled is True


def test_load_app_config_identity_admin_defaults_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("IDENTITY_ADMIN_ENABLED", raising=False)

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.identity_admin_enabled is False


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
