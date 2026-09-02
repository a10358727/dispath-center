"""Clean-config defaults follow the personal-pilot posture (DG-CONSOLIDATION-v1 C-2/C-3).

One table is the single source for "what a clean configuration enables".
Product/platform flags default on; the safety-posture group and the
execution-attempt chain stay off until their own rulings (C-2, C-3 /
RB-LAUNCH-001). A second test boots the app under the exact pilot combination
so the deployed posture is exercised on every run, not only the legacy one.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.settings.features import FEATURE_FLAGS_BY_KEY

#: attribute -> expected clean default. Flipping any safety-posture entry to
#: True needs its own named ruling (AUTHORIZATION_MODE/OIDC/service tokens/
#: identity admin/self-approval/NODE_*/ALLOW_ROOT_SSH), and the execution
#: chain stays off until the WP-2D v2 window is repeated (C-3).
EXPECTED_DEFAULTS: dict[str, object] = {
    # product / platform chain — on
    "api_v2_enabled": True,
    "product_rbac_v2_enabled": True,
    "project_bootstrap_v2_enabled": True,
    "project_environments_v1_enabled": True,
    "run_template_v2_enabled": True,
    "run_experience_v2_enabled": True,
    "experiment_v2_enabled": True,
    "dataset_assets_v2_enabled": True,
    "dataset_sharing_v2_enabled": True,
    "dataset_publish_v2_enabled": True,
    "dataset_snapshot_v1_enabled": True,
    "dataset_snapshot_publish_enabled": True,
    "run_profile_v1_enabled": True,
    "dispatch_policy_v1_enabled": True,
    "auto_placement_proposals_enabled": True,
    "dataset_prewarm_v1_enabled": True,
    "server_bootstrap_v1_enabled": True,
    "code_promotion_v1_enabled": True,
    "metrics_v1_enabled": True,
    "agent_runtime_v3_enabled": True,
    "agent_session_v1_enabled": True,
    "assistant_tools_v1_enabled": True,
    "web_direct_execute": True,
    "server_observations_enabled": True,
    # safety posture — off, own ruling required
    "authorization_mode": "off",
    "oidc_enabled": False,
    "service_token_auth_enabled": False,
    "legacy_shared_token_enabled": True,
    "identity_admin_enabled": False,
    "allow_high_risk_self_approval": False,
    "node_protocol_drain_enabled": False,
    "node_new_assignment_enabled": False,
    "allow_root_ssh": False,
    # execution-attempt chain — off until RB-LAUNCH-001 is repeated (C-3)
    "execution_attempt_shadow_enabled": False,
    "execution_attempt_new_claims_enabled": False,
    "execution_attempt_reconcile_existing": False,
    "execution_outbox_worker_enabled": False,
    "execution_attempt_ssh_launch_enabled": False,
    # retired / retiring surfaces — off
    # operator-installed workers — off (one .env line on the pilot)
    "audit_export_worker_enabled": False,
}

_FLAG_KEY_BY_ATTRIBUTE = {flag.attribute: key for key, flag in FEATURE_FLAGS_BY_KEY.items()}


def test_clean_config_defaults_match_the_expected_posture(monkeypatch):
    # The autouse fixture pins the OFF posture for legacy tests; clear those
    # so the dataclass/env-fallback defaults themselves are observed.
    for attribute in EXPECTED_DEFAULTS:
        for key, flag in FEATURE_FLAGS_BY_KEY.items():
            if flag.attribute == attribute:
                monkeypatch.delenv(flag.env_name, raising=False)
    for env in ("AUTHORIZATION_MODE", "OIDC_ENABLED", "SERVICE_TOKEN_AUTH_ENABLED", "IDENTITY_ADMIN_ENABLED", "ALLOW_HIGH_RISK_SELF_APPROVAL", "ALLOW_ROOT_SSH", "WEB_DIRECT_EXECUTE"):
        monkeypatch.delenv(env, raising=False)
    config = AppConfig(servers=[])
    mismatches = {name: (getattr(config, name), expected) for name, expected in EXPECTED_DEFAULTS.items() if getattr(config, name) != expected}
    assert mismatches == {}


def test_flag_registry_defaults_agree_with_the_expected_posture():
    mismatches = {}
    for attribute, expected in EXPECTED_DEFAULTS.items():
        key = _FLAG_KEY_BY_ATTRIBUTE.get(attribute)
        if key is None or not isinstance(expected, bool):
            continue
        if FEATURE_FLAGS_BY_KEY[key].default != expected:
            mismatches[attribute] = (FEATURE_FLAGS_BY_KEY[key].default, expected)
    assert mismatches == {}


PILOT_ENV = {
    "AUTHORIZATION_MODE": "enforce",
    "ALLOW_HIGH_RISK_SELF_APPROVAL": "true",
    "IDENTITY_ADMIN_ENABLED": "true",
    "LEGACY_SHARED_TOKEN_ENABLED": "false",
    "EXECUTION_ATTEMPT_RECONCILE_EXISTING": "true",
    "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED": "true",
    "EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED": "true",
    "EXECUTION_OUTBOX_WORKER_ENABLED": "true",
    "AUDIT_EXPORT_WORKER_ENABLED": "true",
}


@pytest.fixture
def pilot_posture(monkeypatch, tmp_path):
    """The personal pilot's .env combination on top of the clean defaults
    (OIDC stays off: tests have no identity provider)."""

    for env, value in PILOT_ENV.items():
        monkeypatch.setenv(env, value)
    for key, flag in FEATURE_FLAGS_BY_KEY.items():
        if EXPECTED_DEFAULTS.get(flag.attribute) is True:
            monkeypatch.setenv(flag.env_name, "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pilot.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)


def test_app_boots_under_the_pilot_posture(pilot_posture):
    import app.main as main_module

    with TestClient(main_module.app) as client:
        config = main_module.app_state.config
        assert config.authorization_mode == "enforce"
        assert config.api_v2_enabled and config.experiment_v2_enabled and config.agent_runtime_v3_enabled
        assert config.execution_attempt_new_claims_enabled and config.execution_outbox_worker_enabled
        # INV-APPROVAL-5: only GET / and the OIDC handshake are unauthenticated.
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code in (200, 401)
        # The Studio's v2 routes are mounted and gated by auth, not by a flag.
        assert client.get("/api/v2/studio/cost-summary").status_code in (200, 401, 403)
