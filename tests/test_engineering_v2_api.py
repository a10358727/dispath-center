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
    monkeypatch.setattr(engineering_v2, "local_run", local, raising=False)
    with TestClient(main_module.app) as client:
        main_module.app_state.server_states["server-a"].online = True
        main_module.app_state.ssh_run = runner_ssh
        main_module.app_state.ssh_write_file = writes
        _enable_v2(main_module)
        yield client, main_module, local, runner_ssh, writes, tmp_path


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Capabilities / coding-agents parity
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Engineering task list/detail/events parity + retry/discard/promote/
# worker-validation request kinds and gating
# ---------------------------------------------------------------------------


def _create_and_approve_task(client, main_module, tmp_path):
    """Phase 1b: the request route is retired — seed the same immutable rows
    directly (`test_engineering_task_visibility` recipe); the task stays
    queued exactly like the old request+approve flow left it."""

    import uuid as _uuid

    db, version = _prepare_api_project(main_module, tmp_path)
    task_id = str(_uuid.uuid4())
    instruction = "AI Engineering Task\n\nTask objective:\n- parity"
    task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=db.get_project("proj1").id,
        project_name="proj1",
        project_version_id=version.id,
        base_commit=COMMIT,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract={
            "runner": {"name": "server-a", "host": "10.0.0.1", "user": "train", "port": 22},
            "workspace_rel": "codex_workspaces",
            "source_kind": "hub_bundle",
            "source": f"engineering_bundles/{task_id}.bundle",
            "network_access": False,
            "dependency_installation": False,
        },
        contract_version="engineering-task-v1",
        structured_request={"objective": "parity"},
        detected_metadata={},
        instruction=instruction,
        runner_server="server-a",
        validation_target=None,
        approval_payload={
            "engineering_task_id": task_id,
            "project": "proj1",
            "instruction": instruction,
            "project_version_id": version.id,
            "base_commit": COMMIT,
        },
    )
    db.finalize_engineering_task_approval_plan(
        task_id=task_id,
        approval_id=approval_id,
        project="proj1",
        runner_server="server-a",
        instruction=instruction,
        base_commit=COMMIT,
        project_version_id=version.id,
        validation_target=None,
        worktree_path=f"codex_workspaces/tasks/{approval_id}/repo",
        staging_command="stage --source /srv/hub",
        coding_command="cd /srv/worktree && echo retired",
        approval_note="seeded",
        decision_actor_id=None,
        decision_mechanism="manual",
    )
    return db, task_id






def test_engineering_task_detail_404_for_unknown_id(engineering_client):
    client, main_module, *_rest = engineering_client
    legacy = client.get("/engineering-tasks/does-not-exist")
    v2 = client.get("/api/v2/engineering-tasks/does-not-exist")
    assert legacy.status_code == v2.status_code == 404






# ---------------------------------------------------------------------------
# Coding runs: list / detail / cleanup parity
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Legacy-project scoped request endpoints
# ---------------------------------------------------------------------------








