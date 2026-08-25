import json
import logging
from dataclasses import FrozenInstanceError, replace
from datetime import date

import pytest
from pydantic import SecretStr

from app.config import AppConfig, ServerConfig, load_app_config
from app.settings import (
    FEATURE_FLAGS,
    FEATURE_FLAGS_BY_KEY,
    Settings,
    validate_feature_flag_metadata,
)
from app.settings.features import LIFECYCLE_REVIEW_DATE


def test_app_config_exposes_one_complete_typed_settings_composition():
    server = ServerConfig(
        name="worker-a",
        host="10.0.0.8",
        user="worker",
        key="/keys/worker-a",
    )
    config = AppConfig(
        servers=[server],
        db_path="state/control-plane.db",
        api_host="10.0.0.9",
        api_port=8443,
        scheduler_interval_sec=17,
        dataset_snapshot_store_root="state/datasets",
        audit_path="state/audit.jsonl",
    )

    settings = config.settings

    assert isinstance(settings, Settings)
    assert settings.http.host == "10.0.0.9"
    assert settings.http.port == 8443
    assert settings.http.v2_enabled is False
    assert settings.http.product_rbac_v2_enabled is False
    assert settings.http.project_bootstrap_v2_enabled is False
    assert settings.http.project_environments_v1_enabled is False
    assert settings.http.run_template_v2_enabled is False
    assert settings.http.run_experience_v2_enabled is False
    assert settings.http.dataset_assets_v2_enabled is False
    assert settings.http.dataset_sharing_v2_enabled is False
    assert settings.http.experiment_v2_enabled is False
    assert settings.auth.allow_high_risk_self_approval is False
    assert settings.database.path == "state/control-plane.db"
    assert settings.scheduler.interval_sec == 17
    assert settings.dataset.snapshot_store_root == "state/datasets"
    assert settings.dataset.publish_v2_enabled is False
    assert settings.dataset.publish_local_roots == ()
    assert settings.observability.audit_path == "state/audit.jsonl"
    assert settings.observability.audit_export_worker_enabled is False
    assert settings.observability.legacy_audit_jsonl_enabled is True
    assert settings.machines.servers == (server,)


def test_typed_settings_are_fresh_immutable_views_of_legacy_app_config():
    config = AppConfig(servers=[])
    first = config.settings

    config.api_port = 8123
    second = config.settings

    assert first.http.port == 8000
    assert second.http.port == 8123
    with pytest.raises(FrozenInstanceError):
        second.http.port = 9000  # type: ignore[misc]


def test_bounded_setting_validation_is_independent():
    settings = AppConfig(servers=[]).settings

    # An inactive provider may remain partially configured for rollback, and
    # validating it does not require scheduler, SSH, or execution settings.
    inactive_oidc = replace(
        settings.oidc,
        issuer="http://inactive.invalid",
        redirect_uri="not-an-active-callback",
    )
    inactive_oidc.validate(session_cookie_name=settings.auth.session_cookie_name)
    settings.http.validate()

    invalid_execution = replace(
        settings.execution,
        new_claims_enabled=True,
        reconcile_existing=False,
        outbox_worker_enabled=True,
    )
    with pytest.raises(ValueError, match="NEW_CLAIMS_ENABLED"):
        invalid_execution.validate()

    # The execution failure is local to that group.
    settings.http.validate()
    settings.oidc.validate(session_cookie_name=settings.auth.session_cookie_name)


def test_secret_values_are_masked_from_repr_and_startup_report():
    secret = "sentinel-secret-that-must-never-be-logged"
    config = AppConfig(
        servers=[],
        auth_token=secret,
        oidc_client_secret=secret,
        smtp_pass=secret,
        anthropic_api_key=secret,
        vllm_api_key=secret,
    )
    settings = config.settings

    assert isinstance(settings.auth.shared_token, SecretStr)
    assert settings.auth.shared_token.get_secret_value() == secret
    assert isinstance(settings.oidc.client_secret, SecretStr)
    assert isinstance(settings.observability.smtp_password, SecretStr)
    assert isinstance(settings.llm.anthropic_api_key, SecretStr)
    assert isinstance(settings.llm.vllm_api_key, SecretStr)

    rendered = "\n".join(
        (
            repr(config),
            repr(settings),
            json.dumps(settings.safe_summary(), sort_keys=True),
            json.dumps(settings.feature_report(), sort_keys=True),
        )
    )
    assert secret not in rendered
    assert settings.safe_summary()["secrets"] == {
        "shared_token_configured": True,
        "oidc_client_secret_configured": True,
        "smtp_password_configured": True,
        "anthropic_api_key_configured": True,
        "vllm_api_key_configured": True,
    }
    assert settings.safe_summary()["audit"]["legacy_jsonl_enabled"] is True
    assert (
        settings.safe_summary()["http"]["project_environments_v1_enabled"]
        is False
    )


@pytest.mark.asyncio
async def test_lifespan_logs_only_the_secret_safe_startup_report(
    monkeypatch, caplog
):
    import app.main as main_module

    secret = "startup-log-secret-sentinel"
    config = AppConfig(
        servers=[],
        auth_token=secret,
        oidc_client_secret=secret,
        smtp_pass=secret,
        anthropic_api_key=secret,
        vllm_api_key=secret,
    )

    class StubState:
        def __init__(self, loaded_config):
            self.config = loaded_config

        def start_background_tasks(self):
            return None

        async def stop_background_tasks(self):
            return None

    monkeypatch.setattr(main_module, "load_app_config", lambda: config)
    monkeypatch.setattr(main_module, "AppState", StubState)
    # Record and restore this mutable module global after lifespan replaces it.
    monkeypatch.setattr(main_module, "app_state", main_module.app_state)

    with caplog.at_level(logging.INFO, logger="app.main"):
        async with main_module.lifespan(main_module.app):
            pass

    startup_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.main" and record.getMessage().startswith("startup settings:")
    ]
    assert len(startup_messages) == 1
    assert secret not in startup_messages[0]
    assert '"shared_token_configured": true' in startup_messages[0]
    assert '"oidc_client_secret_configured": true' in startup_messages[0]


def test_feature_flags_have_reviewed_lifecycle_metadata_and_default_values():
    settings = AppConfig(servers=[]).settings

    assert len(FEATURE_FLAGS_BY_KEY) == len(FEATURE_FLAGS)
    assert validate_feature_flag_metadata() == ()
    assert LIFECYCLE_REVIEW_DATE == date(2027, 2, 3)
    for spec in FEATURE_FLAGS:
        assert spec.key
        assert spec.env_name
        assert spec.owner
        assert spec.rollout_state
        assert isinstance(spec.incompatible_with, tuple)
        assert spec.review_by == LIFECYCLE_REVIEW_DATE
        assert spec.retirement_condition
        assert spec.retirement_date == spec.sunset_after
        assert spec.value_from(settings) == spec.default

    legacy = FEATURE_FLAGS_BY_KEY["node_agent_v1_compatibility_alias"]
    assert legacy.deprecated is True
    assert legacy.replacement == (
        "NODE_PROTOCOL_DRAIN_ENABLED + NODE_NEW_ASSIGNMENT_ENABLED"
    )
    legacy_audit = FEATURE_FLAGS_BY_KEY["legacy_audit_jsonl"]
    assert legacy_audit.default is True
    assert legacy_audit.retirement_condition

    report = settings.feature_report()
    assert report["api_v2"]["rollout_state"] == "default_off"
    assert report["api_v2"]["value"] is False
    assert report["product_rbac_v2"]["rollout_state"] == "default_off"
    assert report["product_rbac_v2"]["value"] is False
    assert report["product_rbac_v2"]["dependencies"] == ["api_v2"]
    assert report["project_bootstrap_v2"]["rollout_state"] == "default_off"
    assert report["project_bootstrap_v2"]["value"] is False
    assert report["project_bootstrap_v2"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
    ]
    assert report["project_environments_v1"]["rollout_state"] == "default_off"
    assert report["project_environments_v1"]["value"] is False
    assert report["project_environments_v1"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
    ]
    assert report["run_template_v2"]["rollout_state"] == "default_off"
    assert report["run_template_v2"]["value"] is False
    assert report["run_template_v2"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
        "project_environments_v1",
    ]
    assert report["run_experience_v2"]["rollout_state"] == "default_off"
    assert report["run_experience_v2"]["value"] is False
    assert report["run_experience_v2"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
        "project_environments_v1",
        "run_template_v2",
        "dataset_assets_v2",
    ]
    assert report["experiment_v2"]["rollout_state"] == "default_off"
    assert report["experiment_v2"]["value"] is False
    assert report["experiment_v2"]["dependencies"] == ["run_experience_v2"]
    assert report["dataset_assets_v2"]["rollout_state"] == "default_off"
    assert report["dataset_assets_v2"]["value"] is False
    assert report["dataset_assets_v2"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
    ]
    assert report["dataset_sharing_v2"]["rollout_state"] == "default_off"
    assert report["dataset_sharing_v2"]["value"] is False
    assert report["dataset_sharing_v2"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
        "dataset_assets_v2",
    ]
    assert report["dataset_publish_v2"]["rollout_state"] == "default_off"
    assert report["dataset_publish_v2"]["value"] is False
    assert report["dataset_publish_v2"]["dependencies"] == [
        "api_v2",
        "product_rbac_v2",
        "dataset_assets_v2",
        "dataset_snapshot",
        "dataset_snapshot_publish",
    ]
    assert report["audit_export_worker"]["rollout_state"] == "default_off"
    assert report["audit_export_worker"]["value"] is False
    assert report["legacy_audit_jsonl"]["rollout_state"] == "default_on"
    assert report["legacy_audit_jsonl"]["incompatible_with"] == []
    assert report["legacy_audit_jsonl"]["retirement_date"] is None


def test_typed_http_settings_reject_product_rbac_without_api_v2():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=False,
        product_rbac_v2_enabled=True,
    )

    with pytest.raises(
        ValueError,
        match="PRODUCT_RBAC_V2_ENABLED=true requires API_V2_ENABLED=true",
    ):
        invalid.validate()


def test_typed_http_settings_reject_bootstrap_without_both_dependencies():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=True,
        product_rbac_v2_enabled=False,
        project_bootstrap_v2_enabled=True,
    )

    with pytest.raises(ValueError, match="PROJECT_BOOTSTRAP_V2_ENABLED=true"):
        invalid.validate()


def test_typed_http_settings_reject_environments_without_both_dependencies():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=True,
        product_rbac_v2_enabled=False,
        project_environments_v1_enabled=True,
    )

    with pytest.raises(ValueError, match="PROJECT_ENVIRONMENTS_V1_ENABLED=true"):
        invalid.validate()


def test_typed_http_settings_reject_run_templates_without_environments():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=True,
        product_rbac_v2_enabled=True,
        project_environments_v1_enabled=False,
        run_template_v2_enabled=True,
    )

    with pytest.raises(ValueError, match="RUN_TEMPLATE_V2_ENABLED=true"):
        invalid.validate()


def test_typed_http_settings_reject_dataset_assets_without_product_rbac():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=True,
        product_rbac_v2_enabled=False,
        dataset_assets_v2_enabled=True,
    )

    with pytest.raises(ValueError, match="DATASET_ASSETS_V2_ENABLED=true"):
        invalid.validate()


def test_typed_http_settings_reject_dataset_sharing_without_assets():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=True,
        product_rbac_v2_enabled=True,
        dataset_assets_v2_enabled=False,
        dataset_sharing_v2_enabled=True,
    )

    with pytest.raises(ValueError, match="DATASET_SHARING_V2_ENABLED=true"):
        invalid.validate()


def test_typed_http_settings_reject_experiment_v2_without_run_experience():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings.http,
        v2_enabled=True,
        product_rbac_v2_enabled=True,
        project_environments_v1_enabled=True,
        run_template_v2_enabled=True,
        dataset_assets_v2_enabled=True,
        run_experience_v2_enabled=False,
        experiment_v2_enabled=True,
    )

    with pytest.raises(ValueError, match="EXPERIMENT_V2_ENABLED=true"):
        invalid.validate()


def test_typed_settings_reject_dataset_publish_without_every_dependency():
    settings = AppConfig(servers=[]).settings
    invalid = replace(
        settings,
        http=replace(
            settings.http,
            v2_enabled=True,
            product_rbac_v2_enabled=True,
            dataset_assets_v2_enabled=False,
        ),
        dataset=replace(
            settings.dataset,
            snapshot_enabled=True,
            snapshot_publish_enabled=True,
            publish_v2_enabled=True,
        ),
    )

    with pytest.raises(ValueError, match="DATASET_PUBLISH_V2_ENABLED=true"):
        invalid.validate()


def test_typed_dataset_publish_roots_are_absolute_and_unique():
    settings = AppConfig(servers=[]).settings
    with pytest.raises(ValueError, match="entries must be absolute paths"):
        replace(settings.dataset, publish_local_roots=("relative",)).validate()
    with pytest.raises(ValueError, match="entries must be unique"):
        replace(
            settings.dataset,
            publish_local_roots=("/srv/publish", "/srv/publish"),
        ).validate()


def test_legacy_node_aggregate_environment_emits_deprecation_warning(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("NODE_AGENT_V1_ENABLED", "false")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        with pytest.warns(DeprecationWarning, match="NODE_AGENT_V1_ENABLED"):
            config = load_app_config(
                servers_yaml_path=tmp_path / "servers.yaml",
                dotenv_path=tmp_path / ".env",
            )

    assert config.node_agent_v1_enabled is False
    assert "NODE_PROTOCOL_DRAIN_ENABLED" in caplog.text
    assert "NODE_NEW_ASSIGNMENT_ENABLED" in caplog.text
