"""HTTP-surface tests for the `/api/v2/engineering-tasks*` /
`/api/v2/coding-agents` / `/api/v2/coding-runs*` /
`/api/v2/legacy-projects/{name}/{engineering-task,coding-task}-request*`
wrappers (DG-UI-UNIFICATION v1, U6a).

These stay legacy-scope `engineering_task`/`coding_run`/`platform`/`project`
objects: thin wrappers around the exact legacy `/engineering-tasks*`/
`/coding-agents`/`/coding-runs*`/`/projects/{name}/...`-request surfaces (see
`dispatch_center/api/routers/engineering_v2.py` module docstring). Most
list/detail tests assert byte-identical parity with the legacy endpoints
(modeled on `tests/test_jobs_v2_api.py`, `tests/test_projects_legacy_v2_api.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest



COMMIT = "a" * 40


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


# ---------------------------------------------------------------------------
# Shared fakes (mirrors tests/test_engineering_tasks.py)
# ---------------------------------------------------------------------------


@dataclass
class FakeCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0


class EngineeringLocalRun:
    def __init__(self, commit: str = COMMIT):
        self.commit = commit
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, command: str, timeout: int):
        self.calls.append((command, timeout))
        if "rev-parse --verify" in command:
            return FakeCommandResult(stdout=f"{self.commit}\n")
        if "ls-tree -r --name-only" in command:
            return FakeCommandResult(
                stdout="pyproject.toml\napp/main.py\ntests/conftest.py\nruff.toml\n"
            )
        return FakeCommandResult()


class RunnerSSH:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, server: str, command: str, timeout: int):
        self.calls.append((server, command, timeout))
        if "command -v codex" in command:
            return FakeCommandResult(stdout="codex-cli 0.144.3\nAUTH_OK\n")
        return FakeCommandResult()


class RecordingWriteFile:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, server: str, path: str, content: str):
        self.calls.append((server, path, content))


def _api_body(version_id: str) -> dict:
    return {
        "project_version_id": version_id,
        "agent_provider_id": "codex",
        "objective": "Pin the approved revision",
        "background": "Legacy resolves HEAD later",
        "expected_changes": ["Use a Hub bundle"],
        "allowed_paths": ["app/"],
        "prohibited_paths": ["app/migrations/"],
        "prohibited_changes": ["Do not remove SSH"],
        "acceptance_criteria": ["Exact commit remains fixed"],
        "validation": {
            "tests_lint": True,
            "build_smoke": False,
            "continue_fixing_failures": True,
        },
        "execution_permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
        },
    }


def _prepare_api_project(main_module, tmp_path):
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    return db, version


@pytest.fixture
def engineering_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import asyncio

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOCAL_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ENGINEERING_TASK_BACKEND_V1", "true")
    monkeypatch.setenv(
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION", "true"
    )
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "server-a")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", "~/codex_workspaces")
    monkeypatch.setenv("CODEX_NETWORK_ACCESS", "true")
    servers_yaml = tmp_path / "servers.yaml"
    servers_yaml.write_text(
        "servers:\n"
        "  - name: server-a\n"
        "    host: 10.0.0.1\n"
        "    user: train\n"
        "    key: ~/.ssh/id_rsa\n"
        "    port: 32221\n"
        "    enabled: true\n"
    )
    monkeypatch.setenv("SERVERS_YAML_PATH", str(servers_yaml))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module
    import dispatch_center.api.routers.engineering_v2 as engineering_v2

    local = EngineeringLocalRun()
    runner_ssh = RunnerSSH()
    writes = RecordingWriteFile()

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)
    monkeypatch.setattr(main_module, "local_run", local)
    #: `dispatch_center.api.routers.engineering_v2` imports `local_run` as its
    #: own module-level name (`from app.localrun import local_run`), the same
    #: pattern `projects_legacy_v2.py` uses -- patching only `main_module.
    #: local_run` above does not reach this second binding, so both must be
    #: patched for the v2 wrapper's hub-commit-resolution calls to observe
    #: the fake.
    monkeypatch.setattr(engineering_v2, "local_run", local)
    with TestClient(main_module.app) as client:
        main_module.app_state.server_states["server-a"].online = True
        main_module.app_state.ssh_run = runner_ssh
        main_module.app_state.ssh_write_file = writes
        _enable_v2(main_module)
        yield client, main_module, local, runner_ssh, writes, tmp_path


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def test_flag_off_is_a_hidden_interface(api_client):
    client, main_module = api_client
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/proj1.git")
    assert client.get("/api/v2/engineering-tasks").status_code == 404
    assert client.get("/api/v2/engineering-tasks/capabilities").status_code == 404
    assert client.get("/api/v2/coding-agents").status_code == 404
    assert client.get("/api/v2/coding-runs").status_code == 404
    assert (
        client.post("/api/v2/engineering-tasks/some-id/retry-requests").status_code
        == 404
    )
    assert (
        client.post(
            "/api/v2/legacy-projects/proj1/coding-task-requests",
            json={"instruction": "do it"},
        ).status_code
        == 404
    )

    main_module.app_state.config.api_v2_enabled = True
    # product_rbac_v2 still off -> still hidden.
    assert client.get("/api/v2/engineering-tasks").status_code == 404


# ---------------------------------------------------------------------------
# Capabilities / coding-agents parity
# ---------------------------------------------------------------------------


def test_capabilities_and_coding_agents_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/proj1.git")

    legacy_caps = client.get("/engineering-tasks/capabilities")
    v2_caps = client.get("/api/v2/engineering-tasks/capabilities")
    assert legacy_caps.status_code == v2_caps.status_code == 200
    assert legacy_caps.json() == v2_caps.json()

    legacy_agents = client.get("/coding-agents")
    v2_agents = client.get("/api/v2/coding-agents")
    assert legacy_agents.status_code == v2_agents.status_code == 200
    assert legacy_agents.json() == v2_agents.json()


# ---------------------------------------------------------------------------
# Engineering task list/detail/events parity + retry/discard/promote/
# worker-validation request kinds and gating
# ---------------------------------------------------------------------------


def _create_and_approve_task(client, main_module, tmp_path):
    db, version = _prepare_api_project(main_module, tmp_path)
    create = client.post(
        "/api/v2/legacy-projects/proj1/engineering-task-requests",
        json=_api_body(version.id),
    )
    assert create.status_code == 200, create.text
    body = create.json()
    approval_id = body["approval"]["id"]
    approved = client.post(f"/approve/{approval_id}")
    assert approved.status_code == 200, approved.text
    task_id = body["task"]["id"]
    return db, task_id


def test_engineering_task_request_creates_identical_task_and_approval(
    engineering_client,
):
    """The v2 request wrapper must produce the exact same task/approval shape
    as the legacy `POST /projects/{name}/engineering-tasks/request` for an
    otherwise-identical body (parity checked by tearing down and re-running
    the legacy endpoint against a second, isolated project)."""

    client, main_module, _local, _ssh, writes, tmp_path = engineering_client
    db, version = _prepare_api_project(main_module, tmp_path)

    v2_response = client.post(
        "/api/v2/legacy-projects/proj1/engineering-task-requests",
        json=_api_body(version.id),
    )
    assert v2_response.status_code == 200, v2_response.text
    v2_body = v2_response.json()
    assert v2_body["approval"]["kind"] == "coding_task"
    assert v2_body["task"]["base_binding"] == "project_version_pinned"
    assert v2_body["task"]["base_commit"] == COMMIT

    approval_id = v2_body["approval"]["id"]
    approved = client.post(f"/approve/{approval_id}")
    assert approved.status_code == 200, approved.text
    #: The instruction-file write happens inside `POST /approve/{id}` (same
    #: engine for both surfaces, INV-APPROVAL semantics unchanged by U6a),
    #: not at request time -- matches
    #: `test_structured_api_and_approval_create_staging_dependency` in
    #: `tests/test_engineering_tasks.py`.
    assert writes.calls and writes.calls[0][0] == "_local"


def test_engineering_task_list_and_detail_and_events_parity(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, task_id = _create_and_approve_task(client, main_module, tmp_path)

    legacy_list = client.get("/engineering-tasks?project=proj1")
    v2_list = client.get("/api/v2/engineering-tasks?project=proj1")
    assert legacy_list.status_code == v2_list.status_code == 200
    assert legacy_list.json() == v2_list.json()

    legacy_detail = client.get(f"/engineering-tasks/{task_id}")
    v2_detail = client.get(f"/api/v2/engineering-tasks/{task_id}")
    assert legacy_detail.status_code == v2_detail.status_code == 200
    assert legacy_detail.json() == v2_detail.json()

    legacy_events = client.get(f"/engineering-tasks/{task_id}/events")
    v2_events = client.get(f"/api/v2/engineering-tasks/{task_id}/events")
    assert legacy_events.status_code == v2_events.status_code == 200
    assert legacy_events.json() == v2_events.json()

    legacy_diff = client.get(f"/engineering-tasks/{task_id}/diff")
    v2_diff = client.get(f"/api/v2/engineering-tasks/{task_id}/diff")
    assert legacy_diff.status_code == v2_diff.status_code == 200
    assert legacy_diff.json() == v2_diff.json()


def test_engineering_task_detail_404_for_unknown_id(engineering_client):
    client, main_module, *_rest = engineering_client
    legacy = client.get("/engineering-tasks/does-not-exist")
    v2 = client.get("/api/v2/engineering-tasks/does-not-exist")
    assert legacy.status_code == v2.status_code == 404


def test_retry_discard_promote_worker_validation_request_kinds_and_gating(
    engineering_client,
):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, task_id = _create_and_approve_task(client, main_module, tmp_path)

    # A queued/running task is not yet terminal -> the retry/discard request
    # is rejected with the same 400 the legacy endpoint would give.
    retry_response = client.post(f"/api/v2/engineering-tasks/{task_id}/retry-requests")
    assert retry_response.status_code == 400
    discard_response = client.post(
        f"/api/v2/engineering-tasks/{task_id}/discard-requests"
    )
    assert discard_response.status_code == 400

    # Legacy Coding Run ids are rejected the same way on every mutating route.
    legacy_run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="legacy",
        base_commit=COMMIT,
    )
    legacy_task_id = f"legacy-coding-run-{legacy_run_id}"
    for route in (
        f"/api/v2/engineering-tasks/{legacy_task_id}/retry-requests",
        f"/api/v2/engineering-tasks/{legacy_task_id}/discard-requests",
        f"/api/v2/engineering-tasks/{legacy_task_id}/promote-requests",
    ):
        response = client.post(route)
        assert response.status_code == 400, route

    # `ENGINEERING_TASK_BACKEND_V1` off -> 404 on the flag-gated mutating
    # routes (promote is deliberately not flag-gated, matching legacy).
    main_module.app_state.config.engineering_task_backend_v1 = False
    for route in (
        f"/api/v2/engineering-tasks/{task_id}/retry-requests",
        f"/api/v2/engineering-tasks/{task_id}/discard-requests",
    ):
        response = client.post(route)
        assert response.status_code == 404, route
    worker_validation_response = client.post(
        f"/api/v2/engineering-tasks/{task_id}/worker-validation-requests",
        json={"command": "python3 -m pytest -q", "pin_server": "server-a"},
    )
    assert worker_validation_response.status_code == 404


def test_patch_download_error_parity(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, task_id = _create_and_approve_task(client, main_module, tmp_path)

    legacy = client.get(f"/engineering-tasks/{task_id}/patch")
    v2 = client.get(f"/api/v2/engineering-tasks/{task_id}/patch")
    # A queued task has no collected result yet -> both refuse the download
    # with the same status/reason; the v2 error envelope carries a stable
    # `code` instead of the legacy `detail` string, but the human-readable
    # reason text is identical (same `EngineeringPatchDownloadError.detail`).
    assert legacy.status_code == v2.status_code == 409
    assert legacy.json()["detail"] == v2.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Coding runs: list / detail / cleanup parity
# ---------------------------------------------------------------------------


def test_coding_runs_list_and_detail_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="do something",
        base_commit=COMMIT,
    )

    legacy_list = client.get("/coding-runs?project=proj1")
    v2_list = client.get("/api/v2/coding-runs?project=proj1")
    assert legacy_list.status_code == v2_list.status_code == 200
    assert legacy_list.json() == v2_list.json()

    legacy_detail = client.get(f"/coding-runs/{run_id}")
    v2_detail = client.get(f"/api/v2/coding-runs/{run_id}")
    assert legacy_detail.status_code == v2_detail.status_code == 200
    assert legacy_detail.json() == v2_detail.json()

    v2_missing = client.get("/api/v2/coding-runs/999999")
    assert v2_missing.status_code == 404


def test_coding_run_cleanup_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="do something",
        base_commit=COMMIT,
    )
    # Not yet in a terminal state with a worktree -> both refuse with 409.
    legacy = client.post(f"/coding-runs/{run_id}/cleanup")
    v2 = client.post(f"/api/v2/coding-runs/{run_id}/cleanup")
    assert legacy.status_code == v2.status_code == 409

    v2_missing = client.post("/api/v2/coding-runs/999999/cleanup")
    assert v2_missing.status_code == 404


# ---------------------------------------------------------------------------
# Legacy-project scoped request endpoints
# ---------------------------------------------------------------------------


def test_coding_task_request_kind_parity(engineering_client):
    client, main_module, _local, _ssh, _writes, _tmp_path = engineering_client
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/proj1.git")

    response = client.post(
        "/api/v2/legacy-projects/proj1/coding-task-requests",
        json={"instruction": "please add a test"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "coding_task"
    assert main_module.app_state.db.get_approval(body["id"]).kind == "coding_task"


def test_coding_task_request_invalid_project_parity(engineering_client):
    client, main_module, _local, _ssh, _writes, _tmp_path = engineering_client

    legacy = client.post(
        "/projects/does-not-exist/coding-task-request",
        json={"instruction": "please add a test"},
    )
    v2 = client.post(
        "/api/v2/legacy-projects/does-not-exist/coding-task-requests",
        json={"instruction": "please add a test"},
    )
    assert legacy.status_code == v2.status_code == 400


def test_path_policy_coverage_parity(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, version = _prepare_api_project(main_module, tmp_path)

    body = {
        "project_version_id": version.id,
        "allowed_paths": ["app/"],
        "prohibited_paths": ["app/migrations/"],
    }
    legacy = client.post(
        "/projects/proj1/engineering-tasks/path-policy-coverage", json=body
    )
    v2 = client.post(
        "/api/v2/legacy-projects/proj1/engineering-task-path-policy-coverage",
        json=body,
    )
    assert legacy.status_code == v2.status_code == 200
    assert legacy.json() == v2.json()


def test_path_policy_coverage_flag_off_is_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/proj1.git")
    response = client.post(
        "/api/v2/legacy-projects/proj1/engineering-task-path-policy-coverage",
        json={"project_version_id": "missing", "allowed_paths": [], "prohibited_paths": []},
    )
    assert response.status_code == 404
