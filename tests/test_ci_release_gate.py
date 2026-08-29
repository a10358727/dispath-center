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
        "Build and smoke-test distributions",
        "Publish review wheel artifacts",
        "Verify TestClient can enter and exit",
        "Verify dependency-free frontend assets",
        "Verify Node protocol primitives import",
        "Collect the complete suite",
        "Run Ruff lint",
        "Run mypy type checks",
        "Run core coverage gate",
        "Run static invariant checks",
        "Verify durable-audit adoption boundary",
        "Run complete suite once and report slow tests",
        "Ensure tests did not pollute the checkout",
    ]
    assert names == required_order


def test_ci_installs_and_runs_locked_quality_tools():
    job = _workflow_job()
    install = next(
        step for step in job["steps"] if step["name"] == "Install exact locked dependencies"
    )["run"]
    assert "--require-hashes -r requirements.lock" in install
    assert "--require-hashes -r requirements-dev.lock" in install

    commands = {
        step["name"]: str(step.get("run", "")) for step in job["steps"]
    }
    assert commands["Run Ruff lint"] == (
        "python -m ruff check app agent dispatch_center scripts tests"
    )
    assert commands["Run mypy type checks"] == (
        "python -m mypy app agent dispatch_center scripts"
    )
    assert commands["Run core coverage gate"] == "python scripts/coverage_gate.py"
    assert commands["Run complete suite once and report slow tests"] == (
        "python -m pytest -q --durations=25 --durations-min=0.5"
    )
    package_gate = commands["Build and smoke-test distributions"]
    assert "python -m build --no-isolation --outdir dist ." in package_gate
    assert "python -m build --no-isolation --outdir dist agent" in package_gate
    assert "python scripts/check_wheel_boundaries.py dist" in package_gate
    assert "--require-hashes -r requirements.lock" in package_gate
    assert '"$RUNNER_TEMP/package-smoke/bin/python" -m pip check' in package_gate
    assert '"$RUNNER_TEMP/package-smoke/bin/dispatch" --help' in package_gate
    assert '"$RUNNER_TEMP/package-smoke/bin/dispatch-api" --help' in package_gate
    assert '"$RUNNER_TEMP/package-smoke/bin/dispatch-worker" --help' in package_gate
    assert '"$RUNNER_TEMP/package-smoke/bin/dispatch-node-agent" --check' in package_gate

    artifact = next(
        step
        for step in job["steps"]
        if step["name"] == "Publish review wheel artifacts"
    )
    assert artifact["uses"] == "actions/upload-artifact@v4"
    assert artifact["with"] == {
        "name": "dispatch-wheels-${{ github.sha }}",
        "path": "dist/*.whl",
        "if-no-files-found": "error",
        "retention-days": 14,
    }

    frontend_gate = commands["Verify dependency-free frontend assets"]
    assert "python scripts/frontend_smoke.py" in frontend_gate
    assert "node --check static/workspace.js" in frontend_gate
    assert "node --check static/workspace-features.js" in frontend_gate


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
