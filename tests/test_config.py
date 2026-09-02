"""階段 13（PLAN.md N.1）：六個 CODEX_* 設定鍵的預設值、.env 載入、以及
`apply_codex_config_rules()` 的驗證/降級規則。"""

import pytest

from app.config import AppConfig, load_app_config


#: 整頓 C6: product-chain flags default on now, so a dependency rule can only
#: be exercised alone from an all-off baseline.
_PRODUCT_CHAIN_ATTRS = (
    "api_v2_enabled",
    "product_rbac_v2_enabled",
    "project_bootstrap_v2_enabled",
    "project_environments_v1_enabled",
    "run_template_v2_enabled",
    "run_experience_v2_enabled",
    "experiment_v2_enabled",
    "dataset_assets_v2_enabled",
    "dataset_sharing_v2_enabled",
    "dataset_publish_v2_enabled",
    "dataset_snapshot_v1_enabled",
    "dataset_snapshot_publish_enabled",
    "run_profile_v1_enabled",
    "dispatch_policy_v1_enabled",
    "auto_placement_proposals_enabled",
    "dataset_prewarm_v1_enabled",
    "server_bootstrap_v1_enabled",
    "code_promotion_v1_enabled",
    "metrics_v1_enabled",
    "agent_runtime_v3_enabled",
    "agent_session_v1_enabled",
    "assistant_tools_v1_enabled",
)


def _off_config(**overrides):
    values = {attr: False for attr in _PRODUCT_CHAIN_ATTRS}
    values.update(overrides)
    return AppConfig(servers=[], **values)


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




def test_load_app_config_reads_product_v2_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("API_V2_ENABLED", "yes")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "on")
    monkeypatch.setenv("PROJECT_BOOTSTRAP_V2_ENABLED", "true")
    monkeypatch.setenv("PROJECT_ENVIRONMENTS_V1_ENABLED", "true")
    monkeypatch.setenv("RUN_TEMPLATE_V2_ENABLED", "true")
    monkeypatch.setenv("RUN_EXPERIENCE_V2_ENABLED", "true")
    monkeypatch.setenv("DATASET_ASSETS_V2_ENABLED", "true")
    monkeypatch.setenv("DATASET_SHARING_V2_ENABLED", "true")
    monkeypatch.setenv("DATASET_SNAPSHOT_V1_ENABLED", "true")
    monkeypatch.setenv("DATASET_SNAPSHOT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("DATASET_PUBLISH_V2_ENABLED", "true")
    monkeypatch.setenv(
        "DATASET_PUBLISH_LOCAL_ROOTS",
        f"{tmp_path / 'publish-a'}, {tmp_path / 'publish-b'}, {tmp_path / 'publish-a'}",
    )

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.api_v2_enabled is True
    assert config.product_rbac_v2_enabled is True
    assert config.project_bootstrap_v2_enabled is True
    assert config.project_environments_v1_enabled is True
    assert config.run_template_v2_enabled is True
    assert config.run_experience_v2_enabled is True
    assert config.dataset_assets_v2_enabled is True
    assert config.dataset_sharing_v2_enabled is True
    assert config.dataset_publish_v2_enabled is True
    assert config.dataset_publish_local_roots == (
        str(tmp_path / "publish-a"),
        str(tmp_path / "publish-b"),
    )


@pytest.mark.usefixtures("legacy_posture")
def test_product_rbac_requires_api_v2():
    with pytest.raises(
        ValueError,
        match="PRODUCT_RBAC_V2_ENABLED=true requires API_V2_ENABLED=true",
    ):
        _off_config(
            api_v2_enabled=False,
            product_rbac_v2_enabled=True,
        )


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled"),
    [(False, False), (True, False)],
)
def test_project_bootstrap_requires_api_v2_and_product_rbac(
    api_enabled,
    rbac_enabled,
):
    with pytest.raises(
        ValueError,
        match="PROJECT_BOOTSTRAP_V2_ENABLED=true requires API_V2_ENABLED=true",
    ):
        _off_config(
            api_v2_enabled=api_enabled,
            product_rbac_v2_enabled=rbac_enabled,
            project_bootstrap_v2_enabled=True,
        )


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled"),
    [(False, False), (True, False)],
)
def test_project_environments_requires_api_v2_and_product_rbac(
    api_enabled,
    rbac_enabled,
):
    with pytest.raises(
        ValueError,
        match="PROJECT_ENVIRONMENTS_V1_ENABLED=true requires API_V2_ENABLED=true",
    ):
        _off_config(
            api_v2_enabled=api_enabled,
            product_rbac_v2_enabled=rbac_enabled,
            project_environments_v1_enabled=True,
        )


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled", "environments_enabled"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
    ],
)
def test_run_template_requires_all_product_dependencies(
    api_enabled,
    rbac_enabled,
    environments_enabled,
):
    with pytest.raises(ValueError, match="RUN_TEMPLATE_V2_ENABLED=true"):
        _off_config(
            api_v2_enabled=api_enabled,
            product_rbac_v2_enabled=rbac_enabled,
            project_environments_v1_enabled=environments_enabled,
            run_template_v2_enabled=True,
        )


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    "disabled_dependency",
    (
        "api_v2_enabled",
        "product_rbac_v2_enabled",
        "project_environments_v1_enabled",
        "run_template_v2_enabled",
        "dataset_assets_v2_enabled",
    ),
)
def test_run_experience_requires_all_product_dependencies(disabled_dependency):
    values = {
        "api_v2_enabled": True,
        "product_rbac_v2_enabled": True,
        "project_environments_v1_enabled": True,
        "run_template_v2_enabled": True,
        "dataset_assets_v2_enabled": True,
        "run_experience_v2_enabled": True,
    }
    values[disabled_dependency] = False
    prerequisite_dependants = {
        "api_v2_enabled": (
            "product_rbac_v2_enabled",
            "project_environments_v1_enabled",
            "run_template_v2_enabled",
            "dataset_assets_v2_enabled",
        ),
        "product_rbac_v2_enabled": (
            "project_environments_v1_enabled",
            "run_template_v2_enabled",
            "dataset_assets_v2_enabled",
        ),
        "project_environments_v1_enabled": ("run_template_v2_enabled",),
    }
    for dependant in prerequisite_dependants.get(disabled_dependency, ()):
        values[dependant] = False
    with pytest.raises(ValueError, match="RUN_EXPERIENCE_V2_ENABLED=true"):
        _off_config(**values)


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled"),
    [(False, False), (True, False)],
)
def test_dataset_assets_requires_api_v2_and_product_rbac(
    api_enabled,
    rbac_enabled,
):
    with pytest.raises(ValueError, match="DATASET_ASSETS_V2_ENABLED=true"):
        _off_config(
            api_v2_enabled=api_enabled,
            product_rbac_v2_enabled=rbac_enabled,
            dataset_assets_v2_enabled=True,
        )


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    ("api_enabled", "rbac_enabled", "assets_enabled"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
    ],
)
def test_dataset_sharing_requires_all_product_dependencies(
    api_enabled,
    rbac_enabled,
    assets_enabled,
):
    with pytest.raises(ValueError, match="DATASET_SHARING_V2_ENABLED=true"):
        _off_config(
            api_v2_enabled=api_enabled,
            product_rbac_v2_enabled=rbac_enabled,
            dataset_assets_v2_enabled=assets_enabled,
            dataset_sharing_v2_enabled=True,
        )


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    ("disabled_dependency", "expected_interlock"),
    (
        ("api_v2_enabled", "PRODUCT_RBAC_V2_ENABLED=true"),
        ("product_rbac_v2_enabled", "DATASET_ASSETS_V2_ENABLED=true"),
        ("dataset_assets_v2_enabled", "DATASET_PUBLISH_V2_ENABLED=true"),
        ("dataset_snapshot_v1_enabled", "DATASET_SNAPSHOT_PUBLISH_ENABLED=true"),
        ("dataset_snapshot_publish_enabled", "DATASET_PUBLISH_V2_ENABLED=true"),
    ),
)
def test_dataset_publish_requires_all_product_and_snapshot_dependencies(
    disabled_dependency,
    expected_interlock,
):
    values = {
        "api_v2_enabled": True,
        "product_rbac_v2_enabled": True,
        "dataset_assets_v2_enabled": True,
        "dataset_snapshot_v1_enabled": True,
        "dataset_snapshot_publish_enabled": True,
        "dataset_publish_v2_enabled": True,
    }
    values[disabled_dependency] = False
    with pytest.raises(ValueError, match=expected_interlock):
        _off_config(**values)


def test_dataset_publish_local_roots_must_be_absolute_even_when_feature_is_off():
    with pytest.raises(ValueError, match="entries must be absolute paths"):
        AppConfig(servers=[], dataset_publish_local_roots=("relative/publish",))


def test_load_app_config_reads_phase6_operations_settings(monkeypatch, tmp_path):
    backup_root = tmp_path / "off-host-mounted-backups"
    monkeypatch.setenv("PROCESS_ROLE", "scheduler")
    monkeypatch.setenv("BACKUP_ROOT", str(backup_root))

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.process_role == "scheduler"
    assert config.backup_root == str(backup_root)


def test_load_app_config_reads_legacy_audit_retirement_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGACY_AUDIT_JSONL_ENABLED", "false")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.legacy_audit_jsonl_enabled is False


def test_invalid_process_role_fails_at_configuration_time():
    with pytest.raises(ValueError, match="PROCESS_ROLE"):
        AppConfig(servers=[], process_role="worker-ish")


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "localhost",
        "10.10.0.8",
        "172.16.0.8",
        "192.168.20.8",
        "100.100.10.8",
        "::1",
        "fd00::8",
    ],
)
def test_private_api_bind_addresses_are_accepted(host):
    assert AppConfig(servers=[], api_host=host).api_host == host


@pytest.mark.parametrize(
    "host",
    [
        "",
        " 127.0.0.1",
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "dispatch.example.com",
        "192.0.2.10",
    ],
)
def test_public_wildcard_or_unverified_api_bind_fails_closed(host):
    with pytest.raises(ValueError, match="API_HOST"):
        AppConfig(servers=[], api_host=host)


@pytest.mark.parametrize("port", [True, 0, 65536])
def test_invalid_api_port_fails_closed(port):
    with pytest.raises(ValueError, match="API_PORT"):
        AppConfig(servers=[], api_port=port)


def test_load_app_config_reads_node_rotation_deadlines(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_ROTATION_OVERLAP_SEC", "600")
    monkeypatch.setenv("NODE_ROTATION_PENDING_TTL_SEC", "7200")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.node_rotation_overlap_sec == 600
    assert config.node_rotation_pending_ttl_sec == 7200


@pytest.mark.parametrize("value", [True, 59, 604801])
def test_invalid_node_pending_credential_ttl_is_rejected(value):
    with pytest.raises(ValueError, match="NODE_ROTATION_PENDING_TTL_SEC"):
        AppConfig(servers=[], node_rotation_pending_ttl_sec=value)


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


def test_load_app_config_reads_audit_export_worker_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_EXPORT_WORKER_ENABLED", "yes")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.audit_export_worker_enabled is True


def test_audit_export_worker_requires_a_worker_capable_process_role():
    with pytest.raises(ValueError, match="AUDIT_EXPORT_WORKER_ENABLED"):
        _off_config(
            process_role="scheduler",
            audit_export_worker_enabled=True,
        )


def test_load_app_config_reads_code_promotion_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_PROMOTION_V1_ENABLED", "yes")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.code_promotion_v1_enabled is True


def test_code_promotion_defaults_enabled():
    # 整頓 C6 (DG-CONSOLIDATION-v1 C-2): promotion UI/API on by default; P-1 still human-only.
    assert AppConfig(servers=[]).code_promotion_v1_enabled is True


def test_load_app_config_reads_high_risk_self_approval_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_HIGH_RISK_SELF_APPROVAL", "yes")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )

    assert config.allow_high_risk_self_approval is True


def test_high_risk_self_approval_defaults_disabled():
    assert AppConfig(servers=[]).allow_high_risk_self_approval is False


@pytest.mark.usefixtures("legacy_posture")
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


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "execution_attempt_reconcile_existing": False,
            "execution_outbox_worker_enabled": True,
        },
        {
            "execution_attempt_reconcile_existing": True,
            "execution_outbox_worker_enabled": False,
        },
    ],
)
def test_split_node_v2_assignment_requires_durable_reconcile_and_outbox(overrides):
    with pytest.raises(ValueError, match="split Node v2 assignment"):
        _off_config(
            node_protocol_drain_enabled=True,
            node_new_assignment_enabled=True,
            **overrides,
        )


def test_legacy_node_aggregate_flag_keeps_compatibility_without_new_interlock():
    config = AppConfig(servers=[], node_agent_v1_enabled=True)
    assert config.node_protocol_drain_enabled is True
    assert config.node_new_assignment_enabled is True
    assert config.node_protocol_allow_missing_version is False


@pytest.mark.usefixtures("legacy_posture")
@pytest.mark.parametrize(
    "field,value,setting",
    [
        ("node_agent_lease_ttl_sec", 0, "NODE_AGENT_LEASE_TTL_SEC"),
        ("node_agent_lease_ttl_sec", float("inf"), "NODE_AGENT_LEASE_TTL_SEC"),
        (
            "node_agent_heartbeat_ttl_sec",
            -1,
            "NODE_AGENT_HEARTBEAT_TTL_SEC",
        ),
        (
            "node_agent_heartbeat_grace_sec",
            -0.1,
            "NODE_AGENT_HEARTBEAT_GRACE_SEC",
        ),
        (
            "node_agent_heartbeat_grace_sec",
            float("nan"),
            "NODE_AGENT_HEARTBEAT_GRACE_SEC",
        ),
    ],
)
def test_node_timing_configuration_must_be_finite_and_safe(
    field, value, setting
):
    with pytest.raises(ValueError, match=setting):
        AppConfig(servers=[], **{field: value})










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


@pytest.mark.usefixtures("legacy_posture")
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


@pytest.mark.parametrize("invalid_mode", ["audit", "SHADOW", ""])
def test_load_app_config_rejects_unsupported_authorization_mode(
    monkeypatch, tmp_path, invalid_mode
):
    monkeypatch.setenv("AUTHORIZATION_MODE", invalid_mode)

    with pytest.raises(ValueError, match="AUTHORIZATION_MODE"):
        load_app_config(
            servers_yaml_path=str(tmp_path / "servers.yaml"),
            dotenv_path=str(tmp_path / ".env"),
        )


def test_app_config_accepts_enforcement_mode():
    config = AppConfig(servers=[], authorization_mode="enforce")
    assert config.authorization_mode == "enforce"


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














