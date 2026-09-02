"""Plan v2 Slice 2: immutable AI Engineering Task contract tests."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config import AppConfig, ServerConfig
from app.db import Database
from app.engineering_tasks import (
    InvalidEngineeringTaskRequestError,
    build_engineering_bundle_create_command,
    build_engineering_bundle_push_command,
    build_engineering_bundle_verify_command,
    build_engineering_staging_push_command,
    detect_project_metadata,
    normalize_engineering_task_spec,
    render_engineering_task_instruction,
)
from app.jobqueue import list_dispatchable_jobs


COMMIT = "a" * 40


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


def _structured_request(**overrides):
    request = {
        "objective": "Pin the approved revision",
        "background": "The legacy flow resolves HEAD at execution time",
        "expected_changes": ["Use a Hub bundle", "Keep the legacy adapter"],
        "non_goals": [],
        "allowed_paths": ["app/", "tests/"],
        "prohibited_paths": ["app/migrations/"],
        "prohibited_changes": ["Do not remove SSH fallback"],
        "acceptance_criteria": ["The exact base commit is immutable"],
        "validation": {
            "tests_lint": True,
            "build_smoke": False,
            "continue_fixing_failures": True,
            "worker_validation_target": None,
        },
        "permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
            "environment_references": [],
            "secret_references": [],
        },
    }
    request.update(overrides)
    return request


def _server() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="10.0.0.1",
        user="train",
        key="~/.ssh/id_rsa",
        port=32221,
        enabled=True,
    )


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        servers=[_server()],
        local_home_dir=str(tmp_path),
    )


def _project_with_version(db: Database, tmp_path: Path, name: str = "proj1"):
    db.insert_project(name, f"https://example.invalid/{name}.git")
    version = db.get_or_create_project_version(name, COMMIT, git_ref="main")
    (tmp_path / "git" / f"{name}.git").mkdir(parents=True)
    return version


def test_structured_renderer_has_fixed_order_and_normalizes_items():
    instruction = render_engineering_task_instruction(
        _structured_request(objective="  first line\nsecond line  ")
    )
    headings = [
        "Task objective:",
        "Background and relevant context:",
        "Expected changes:",
        "Allowed modification scope:",
        "Prohibited paths:",
        "Prohibited changes:",
        "Acceptance criteria:",
        "Validation strategy:",
        "Requested execution behavior:",
    ]
    offsets = [instruction.index(heading) for heading in headings]
    assert offsets == sorted(offsets)
    assert "- first line\n- second line" in instruction
    assert "Dependency installation is not authorized" in instruction
    assert "External network access is not authorized" in instruction
    assert len(instruction) <= 4000


@pytest.mark.parametrize("permission", ["install_dependencies", "external_network"])
def test_structured_request_cannot_grant_unimplemented_permissions(permission):
    spec = _structured_request()
    spec["permissions"][permission] = True
    with pytest.raises(InvalidEngineeringTaskRequestError):
        normalize_engineering_task_spec(spec)


def test_structured_request_rejects_secret_or_environment_references():
    spec = _structured_request()
    spec["permissions"]["secret_references"] = ["production-token"]
    with pytest.raises(InvalidEngineeringTaskRequestError, match="broker"):
        normalize_engineering_task_spec(spec)


@pytest.mark.parametrize(
    "raw_credential",
    [
        "Authorization: Bearer abcdefghijklmnop",
        "Authorization: Basic ZGlzcGF0Y2g6c2VjcmV0",
        "Authorization: Ba\x1b[31msic ZGlzcGF0Y2g6c2VjcmV0",
        "api_key=abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "glpat-1234567890abcdefghij",
        "xoxb-123456789012-abcdefghijklmnopqrstuvwxyz",
        "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz1234567890",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI_K7MDENG_bPxRfiCYEXAMPLEKEY",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJabcdefgh.ijklmnop.qrstuvwx",
        "ssh://dispatch:runner-password@worker.invalid/project.git",
        "ssh://dispatch:runner\u200b-password@worker.invalid/project.git",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nsynthetic\n-----END ENCRYPTED PRIVATE KEY-----",
        "-----BEGIN \x1b[31mPRIVATE KEY-----\nsynthetic",
        "xoxb-123456789012-abc\x00defghijklmnopqrstuvwxyz",
    ],
)
def test_structured_request_rejects_raw_credentials_before_persistence(
    raw_credential,
):
    with pytest.raises(InvalidEngineeringTaskRequestError, match="raw credential"):
        normalize_engineering_task_spec(
            _structured_request(background=f"Do not expose {raw_credential}")
        )


def test_structured_request_rejects_absolute_path_policy_scope():
    with pytest.raises(InvalidEngineeringTaskRequestError, match="canonical relative"):
        normalize_engineering_task_spec(
            _structured_request(allowed_paths=["/home/project/src"])
        )


def test_structured_request_allows_canonical_paths_and_noncredential_secret_wording():
    normalized = normalize_engineering_task_spec(
        _structured_request(
            background=(
                "A secret broker is not configured; use bearer authentication. "
                "The public source is ssh://git@code.invalid/project.git."
            ),
            allowed_paths=["app/engineering_tasks.py", "app/"],
        )
    )

    assert normalized["background"] == (
        "A secret broker is not configured; use bearer authentication. "
        "The public source is ssh://git@code.invalid/project.git."
    )
    assert normalized["allowed_paths"] == [
        "app/",
    ]
    assert normalized["prohibited_paths"] == ["app/migrations/"]


def test_structured_instruction_limit_is_enforced_server_side():
    with pytest.raises(InvalidEngineeringTaskRequestError, match="上限 4000"):
        render_engineering_task_instruction(_structured_request(objective="x" * 5000))


def test_metadata_detection_is_deterministic_and_path_only():
    metadata = detect_project_metadata(
        ["app/main.py", "pyproject.toml", "manage.py", "tests/conftest.py", "ruff.toml"]
    )
    assert metadata == {
        "detection": "path-markers-v1",
        "languages": ["python"],
        "frameworks": ["django"],
        "validation_tools": ["pytest", "ruff"],
    }


def test_bundle_command_builders_quote_paths_and_non_default_port(tmp_path):
    task_id = "11111111-1111-4111-8111-111111111111"
    create = build_engineering_bundle_create_command(
        task_id, "project name", COMMIT, str(tmp_path)
    )
    verify = build_engineering_bundle_verify_command(
        task_id, "project name", COMMIT, str(tmp_path)
    )
    push = build_engineering_bundle_push_command(
        task_id, _server(), str(tmp_path)
    )
    staging_push = build_engineering_staging_push_command(
        task_id, 7, "codex_workspaces", _server(), str(tmp_path)
    )
    syntax = subprocess.run(
        ["bash", "-n"],
        input=" && ".join((create, verify, push, staging_push)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert "bundle create" in create
    assert COMMIT in create and "refs/heads/approved" in create
    assert "--all" not in create
    assert "bundle verify" in verify
    assert COMMIT in verify and "refs/heads/approved" in verify
    assert "-p 32221" in push
    assert f"engineering_bundles/{task_id}.bundle" in push
    assert f"engineering_bundles/{task_id}.instruction.txt" in staging_push
    assert "codex_workspaces/tasks/7/instruction.txt" in staging_push


def test_task_and_approval_insert_is_atomic_on_task_uniqueness(db):
    version = _project_with_version(db, Path(db.path).parent)
    task_id = "11111111-1111-4111-8111-111111111111"
    payload = {"engineering_task_id": task_id, "project": "proj1"}
    kwargs = dict(
        project_id=version.project_id,
        project_name="proj1",
        project_version_id=version.id,
        base_commit=COMMIT,
        agent_provider_id="codex",
        provider_capabilities={},
        execution_contract={},
        contract_version="engineering-task-v1",
        structured_request=_structured_request(),
        instruction="AI Engineering Task",
        detected_metadata={},
        runner_server="server-a",
        validation_target=None,
        approval_payload=payload,
        task_id=task_id,
    )
    db.insert_engineering_task_request(**kwargs)
    before = len(db.list_approvals())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_engineering_task_request(**kwargs)
    assert len(db.list_approvals()) == before


def test_pending_engineering_owner_job_is_never_dispatchable(db, tmp_path):
    version = _project_with_version(db, tmp_path)
    task_id = "11111111-1111-4111-8111-111111111111"
    payload = {"engineering_task_id": task_id, "project": "proj1"}
    db.insert_engineering_task_request(
        project_id=version.project_id,
        project_name="proj1",
        project_version_id=version.id,
        base_commit=COMMIT,
        agent_provider_id="codex",
        provider_capabilities={},
        execution_contract={},
        contract_version="engineering-task-v1",
        structured_request=_structured_request(),
        instruction="AI Engineering Task",
        detected_metadata={},
        runner_server="server-a",
        validation_target=None,
        approval_payload=payload,
        task_id=task_id,
    )
    owner_job_id = db.insert_job(
        command="true",
        type="sync",
        project="proj1",
        pin_server="_local",
        engineering_task_id=task_id,
        engineering_task_role="staging",
        engineering_attempt_number=1,
    )

    assert owner_job_id not in {job.id for job in list_dispatchable_jobs(db)}


def test_legacy_coding_rows_are_not_backfilled_as_pinned(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE coding_runs (id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'queued')"
    )
    raw.execute("INSERT INTO coding_runs (id, status) VALUES (1, 'queued')")
    raw.commit()
    raw.close()

    db = Database(str(path))
    try:
        columns = {
            row[1] for row in db._conn.execute("PRAGMA table_info(coding_runs)").fetchall()
        }
        assert {
            "engineering_task_id",
            "project_version_id",
            "base_binding",
            "attempt_number",
        } <= columns
        row = db._conn.execute(
            "SELECT engineering_task_id, project_version_id, base_binding, attempt_number "
            "FROM coding_runs WHERE id = 1"
        ).fetchone()
        assert tuple(row) == (None, None, "legacy_unpinned", None)
        assert db.list_engineering_tasks() == []
    finally:
        db.close()


@pytest.fixture
def engineering_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOCAL_HOME_DIR", str(tmp_path))
    monkeypatch.setenv(
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION", "true"
    )
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

    local = EngineeringLocalRun()
    runner_ssh = RunnerSSH()
    writes = RecordingWriteFile()

    # The synthetic Runner is observed exclusively through ``runner_ssh`` in
    # these tests.  A live monitor loop would race the fixture's explicit
    # online state and attempt an unintended probe of 10.0.0.1.
    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        main_module.AppState,
        "monitor_loop",
        isolated_monitor_loop,
    )
    monkeypatch.setattr(main_module, "local_run", local)
    with TestClient(main_module.app) as client:
        main_module.app_state.server_states["server-a"].online = True
        main_module.app_state.ssh_run = runner_ssh
        main_module.app_state.ssh_write_file = writes
        yield client, main_module, local, runner_ssh, writes, tmp_path


def _prepare_api_project(main_module, tmp_path):
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    return db, version


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


def test_engineering_task_list_includes_honest_legacy_adapter(engineering_client):
    client, main_module, _local, _ssh, _writes, _tmp_path = engineering_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    legacy_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="legacy",
        base_commit=COMMIT,
    )
    response = client.get("/api/v2/engineering-tasks?project=proj1")
    assert response.status_code == 200
    legacy = next(row for row in response.json() if row["coding_run_id"] == legacy_id)
    assert legacy["id"] == f"legacy-coding-run-{legacy_id}"
    assert legacy["base_binding"] == "legacy_unpinned"
    assert legacy["project_version_id"] is None
    assert legacy["base_commit"] is None
    assert legacy["observed_base_commit"] == COMMIT


