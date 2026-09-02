"""CI gate presence pins (整頓 C4, DG-CONSOLIDATION-v1 C-1).

The workflow is split into independent jobs; these pins assert that every
required gate exists somewhere, that the pytest job selects the whole suite,
and that the offline/temp-path posture holds for every job — not the exact
step order or command strings, which are bookkeeping.
"""

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _all_steps() -> list[tuple[str, dict]]:
    return [(job_name, step) for job_name, job in _workflow()["jobs"].items() for step in job["steps"]]


def _all_commands() -> str:
    return "\n".join(str(step.get("run", "")) for _, step in _all_steps())


REQUIRED_GATE_COMMANDS = (
    "python scripts/check_requirements_lock.py",
    "python -m ruff check app agent dispatch_center scripts tests",
    "python -m mypy app agent dispatch_center scripts",
    "python scripts/coverage_gate.py",
    "bash .claude/skills/release-gate/scripts/static_checks.sh",
    "python scripts/audit_adoption_gate.py",
    "python scripts/openapi_snapshot.py --check",
    "python scripts/check_wheel_boundaries.py dist",
    "python scripts/frontend_smoke.py --require-studio",
    "python scripts/node_primitives_smoke.py",
    "python scripts/testclient_smoke.py",
    "npm ci --prefix studio",
    "npm run build --prefix studio",
    "npm test --prefix studio",
    "git diff --exit-code",
)


def test_ci_runs_every_required_gate_somewhere():
    commands = _all_commands()
    missing = [gate for gate in REQUIRED_GATE_COMMANDS if gate not in commands]
    assert missing == [], missing


def test_ci_pytest_job_selects_the_whole_suite_exactly_once():
    pytest_runs = [
        str(step["run"])
        for _, step in _all_steps()
        if "python -m pytest" in str(step.get("run", "")) and "--collect-only" not in str(step.get("run", ""))
    ]
    assert len(pytest_runs) == 1, pytest_runs
    run = pytest_runs[0]
    # Only the arguments after `pytest` count (`python -m pytest` itself is fine).
    arguments = run.split("pytest", 1)[1]
    for narrowing in ("-k ", "--ignore", "--deselect", " -m "):
        assert narrowing not in arguments, run
    assert "--durations" in run


def test_ci_every_job_installs_the_exact_locks_before_python_gates():
    for job_name, job in _workflow()["jobs"].items():
        commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
        if "python " not in commands:
            continue  # the Studio job is Node-only
        assert "--require-hashes -r requirements.lock" in commands, job_name
        assert "--require-hashes -r requirements-dev.lock" in commands, job_name


def test_ci_packaging_smokes_the_control_plane_and_node_wheels_only():
    package_gate = next(
        str(step["run"]) for _, step in _all_steps() if step.get("name") == "Build and smoke-test distributions"
    )
    assert "python -m build --no-isolation --outdir dist ." in package_gate
    assert "python -m build --no-isolation --outdir dist agent" in package_gate
    assert "python -m build --no-isolation --outdir dist dispatch_agent" in package_gate
    # the runner-agent wheel declares dependencies outside requirements.lock;
    # it is built and boundary-checked, never installed into the smoke venv
    assert "dist/dispatch_center-*.whl dist/dispatch_node_agent-*.whl" in package_gate
    assert "--no-deps dist/*.whl" not in package_gate
    assert '"$RUNNER_TEMP/package-smoke/bin/dispatch-node-agent" --check' in package_gate
    artifact = next(step for _, step in _all_steps() if step.get("name") == "Publish review wheel artifacts")
    assert artifact["uses"].startswith("actions/upload-artifact@")
    assert artifact["with"]["path"] == "dist/*.whl"
    assert artifact["with"]["if-no-files-found"] == "error"


def test_ci_release_gate_is_offline_and_uses_temp_runtime_paths():
    workflow = _workflow()
    environment = workflow["env"]
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
        # mutate a tracked file, and the "did not pollute" step stays
        # meaningful.  The literal path matters: workflow/job-level `env`
        # cannot expand the `runner` context, so `${{ runner.temp }}` here
        # makes the whole workflow file invalid and no job ever starts.
        assert value.startswith("/tmp/dispatch-center-tests")
        assert "${{" not in value
    # No job may override the offline posture.
    for job_name, job in workflow["jobs"].items():
        job_env = job.get("env") or {}
        assert job_env.get("DISPATCH_TEST_NETWORK", "deny") == "deny", job_name


def test_ci_never_treats_python_module_agent_as_a_phase0_gate():
    commands = _all_commands()
    assert "scripts/node_primitives_smoke.py" in commands
    assert "python -m agent" not in commands
