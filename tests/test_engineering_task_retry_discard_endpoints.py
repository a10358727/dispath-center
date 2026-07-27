"""HTTP-boundary coverage for the D3 retry/discard request endpoints.

Deeper eligibility/revalidation logic is covered directly in
``tests/test_engineering_task_retry_discard.py`` (DB/jobqueue boundary) and
``tests/test_engineering_task_retry_discard_approve.py`` (request/approve
boundary). This file only proves the two new routes wire flag/legacy/error
handling correctly and return the expected shape.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.datasets import LOCAL_SERVER


COMMIT = "a" * 40


@dataclass
class _CommandResult:
    stdout: str = ""
    exit_status: int = 0


class _EngineeringLocalRun:
    def __init__(self, commit: str = COMMIT):
        self.commit = commit

    async def __call__(self, command: str, timeout: int):
        if "rev-parse --verify" in command:
            return _CommandResult(stdout=f"{self.commit}\n")
        if "ls-tree -r --name-only" in command:
            return _CommandResult(stdout="pyproject.toml\napp/main.py\n")
        return _CommandResult()


class _RunnerSSH:
    async def __call__(self, server: str, command: str, timeout: int):
        if "command -v codex" in command:
            return _CommandResult(stdout="codex-cli 0.144.3\nAUTH_OK\n")
        return _CommandResult()


class _RecordingWriteFile:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, server: str, path: str, content: str):
        self.calls.append((server, path, content))


@pytest.fixture
def engineering_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

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

    local = _EngineeringLocalRun()
    runner_ssh = _RunnerSSH()
    writes = _RecordingWriteFile()

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)
    monkeypatch.setattr(main_module, "local_run", local)
    with TestClient(main_module.app) as client:
        main_module.app_state.server_states["server-a"].online = True
        main_module.app_state.ssh_run = runner_ssh
        main_module.app_state.ssh_write_file = writes
        yield client, main_module, local, tmp_path


def _api_body(version_id: str) -> dict:
    return {
        "project_version_id": version_id,
        "agent_provider_id": "codex",
        "objective": "Pin the approved revision",
        "allowed_paths": ["app/"],
        "validation": {"tests_lint": False, "build_smoke": False},
        "execution_permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
        },
    }


def _prepare_terminal_task(client, main_module, tmp_path) -> str:
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)

    create = client.post(
        "/projects/proj1/engineering-tasks/request", json=_api_body(version.id)
    ).json()
    approved = client.post(f"/approve/{create['approval']['id']}").json()
    task_id = create["task"]["id"]
    db.update_job(
        approved["staging_job_id"], status="done", server=LOCAL_SERVER, exit_code=0
    )
    db.update_job(approved["job"]["id"], status="done", server="server-a", exit_code=1)
    db.update_coding_run(approved["coding_run_id"], status="failed")
    return task_id


def test_retry_request_404s_when_backend_flag_is_off(api_client):
    client, main_module = api_client
    response = client.post("/engineering-tasks/some-task-id/retry-request")
    assert response.status_code == 404


def test_discard_request_404s_when_backend_flag_is_off(api_client):
    client, main_module = api_client
    response = client.post("/engineering-tasks/some-task-id/discard-request")
    assert response.status_code == 404


def test_retry_request_400s_for_legacy_task_id(engineering_client):
    client, _main_module, _local, _tmp_path = engineering_client
    response = client.post("/engineering-tasks/legacy-coding-run-1/retry-request")
    assert response.status_code == 400
    assert "legacy" in response.json()["detail"]


def test_discard_request_400s_for_legacy_task_id(engineering_client):
    client, _main_module, _local, _tmp_path = engineering_client
    response = client.post("/engineering-tasks/legacy-coding-run-1/discard-request")
    assert response.status_code == 400
    assert "legacy" in response.json()["detail"]


def test_retry_request_400s_when_task_not_terminal(engineering_client):
    client, main_module, _local, tmp_path = engineering_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    create = client.post(
        "/projects/proj1/engineering-tasks/request", json=_api_body(version.id)
    ).json()
    client.post(f"/approve/{create['approval']['id']}")  # leaves task "queued"

    response = client.post(f"/engineering-tasks/{create['task']['id']}/retry-request")
    assert response.status_code == 400
    assert "終態" in response.json()["detail"]


def test_retry_request_returns_pending_approval_for_terminal_task(engineering_client):
    client, main_module, _local, tmp_path = engineering_client
    task_id = _prepare_terminal_task(client, main_module, tmp_path)

    response = client.post(f"/engineering-tasks/{task_id}/retry-request")
    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["kind"] == "engineering_task_retry"
    assert body["approval"]["status"] == "pending"


def test_discard_request_returns_pending_approval_for_terminal_task(engineering_client):
    client, main_module, _local, tmp_path = engineering_client
    task_id = _prepare_terminal_task(client, main_module, tmp_path)

    response = client.post(f"/engineering-tasks/{task_id}/discard-request")
    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["kind"] == "engineering_task_discard"
    assert body["approval"]["status"] == "pending"


def test_engineering_task_detail_reflects_retry_and_discard_availability(
    engineering_client,
):
    client, main_module, _local, tmp_path = engineering_client
    task_id = _prepare_terminal_task(client, main_module, tmp_path)

    detail = client.get(f"/engineering-tasks/{task_id}").json()
    assert detail["available_actions"]["retry"]["enabled"] is True
    assert detail["available_actions"]["discard"]["enabled"] is True

    discard = client.post(f"/engineering-tasks/{task_id}/discard-request").json()
    client.post(f"/approve/{discard['approval']['id']}")

    detail_after = client.get(f"/engineering-tasks/{task_id}").json()
    assert detail_after["status"] == "discarded"
    assert detail_after["available_actions"]["retry"]["enabled"] is False
