from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"]["python-tests"]


def test_ci_release_gate_has_the_required_ordered_stages():
    names = [step["name"] for step in _workflow_job()["steps"]]
    required_order = [
        "Check out repository",
        "Set up Python 3.10",
        "Install exact locked dependencies",
        "Prepare isolated runtime paths",
        "Verify direct requirements match the lock",
        "Verify TestClient can enter and exit",
        "Verify Node protocol primitives import",
        "Collect the complete suite",
        "Run static invariant checks",
        "Run migration and core state suites",
        "Run complete suite",
        "Ensure tests did not pollute the checkout",
    ]
    assert names == required_order


def test_ci_release_gate_is_offline_and_uses_temp_runtime_paths():
    environment = _workflow_job()["env"]
    assert environment["DISPATCH_TEST_NETWORK"] == "deny"
    for credential_name in (
        "ANTHROPIC_API_KEY",
        "VLLM_BASE_URL",
        "VLLM_MODEL",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "DISPATCH_SERVICE_TOKEN",
    ):
        assert environment[credential_name] == ""

    for path_name in (
        "DB_PATH",
        "AUDIT_PATH",
        "SERVERS_YAML_PATH",
        "AUTO_APPROVE_RULES_PATH",
        "LOCAL_HOME_DIR",
    ):
        value = environment[path_name]
        # Runtime paths must live outside the checkout so a test can never
        # mutate a tracked file, and the final "did not pollute" step stays
        # meaningful.  The literal path matters: job-level `env` cannot
        # expand the `runner` context (only github/needs/strategy/matrix/
        # vars/secrets/inputs are available there), so `${{ runner.temp }}`
        # here makes the whole workflow file invalid and no job ever starts.
        assert value.startswith("/tmp/dispatch-center-tests")
        assert "${{" not in value


def test_ci_never_treats_python_module_agent_as_a_phase0_gate():
    steps = _workflow_job()["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "scripts/node_primitives_smoke.py" in commands
    assert "python -m agent" not in commands
