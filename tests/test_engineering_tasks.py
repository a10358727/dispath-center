"""Plan v2 Slice 2: immutable AI Engineering Task contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.approvals as approvals_module
from app.approvals import (
    approve,
    build_coding_task_script,
    request_engineering_task_approval,
)
from app.audit import read_audit
from app.coding_agents import require_coding_agent_provider
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
from app.jobfinish import _backfill_coding_run
from app.jobqueue import list_dispatchable_jobs
from app.results import local_result_dir


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
        codex_runner_server="server-a",
        codex_network_access=True,
        engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
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


def test_pinned_wrapper_is_valid_bash_and_does_not_resolve_base_from_head():
    script = build_coding_task_script(
        7,
        "codex_workspaces",
        "proj1",
        "hub_bundle",
        "engineering_bundles/11111111-1111-4111-8111-111111111111.bundle",
        None,
        False,
        exact_base_commit=COMMIT,
    )
    syntax = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    assert f"export R_BASE_COMMIT={COMMIT}" in script
    assert "cat-file -e" in script
    # Reading the result worktree's HEAD after Codex runs is legitimate.  The
    # immutable contract only forbids deriving the *base* from a mutable HEAD.
    assert 'export R_BASE_COMMIT="$(git -C "$SRC" rev-parse HEAD)"' not in script
    assert 'export R_BASE_COMMIT="$(git --git-dir=' not in script
    assert "remote update" not in script
    assert "network_access=true" not in script
    assert "python3 -m pytest -q" not in script
    assert "自動 repository validation 已安全跳過" in script
    assert "core.hooksPath=/dev/null" in script
    assert "commit.gpgsign=false" in script
    assert "commit --no-verify" in script
    assert "--no-ext-diff --no-textconv" in script


def test_legacy_wrapper_also_refuses_unsandboxed_post_agent_pytest():
    script = build_coding_task_script(
        8,
        "codex_workspaces",
        "proj1",
        "instance",
        "/srv/proj1",
        None,
        False,
    )

    assert "python3 -m pytest -q" not in script
    assert "自動 repository validation 已安全跳過" in script
    assert "core.hooksPath=/dev/null" in script
    assert "commit.gpgsign=false" in script
    assert "commit --no-verify" in script


def test_approval_plan_rolls_back_atomically_and_can_retry(db, tmp_path, audit_path):
    """A failure during Job planning leaves no dispatchable partial owner rows."""

    version = _project_with_version(db, tmp_path)
    config = _config(tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=config,
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    app_state = SimpleNamespace(config=config, ssh_write_file=RecordingWriteFile())
    db._conn.execute(
        """
        CREATE TRIGGER abort_engineering_coding_job
        BEFORE INSERT ON jobs
        WHEN NEW.engineering_task_role = 'coding'
        BEGIN
            SELECT RAISE(ABORT, 'simulated atomic planner interruption');
        END
        """
    )
    db._conn.commit()
    with pytest.raises(ValueError, match="execution plan 未建立"):
        asyncio.run(
            approve(
                db,
                approval.id,
                ssh_run=RunnerSSH(),
                audit_path=audit_path,
                server_configs={"server-a": _server()},
                app_state=app_state,
                local_run=local,
            )
        )

    assert db.get_coding_run_by_engineering_attempt(task.id, 1) is None
    assert db.get_engineering_task_job(task.id, "staging", 1) is None
    assert db.get_engineering_task_job(task.id, "coding", 1) is None
    assert db.get_approval(approval.id).status == "pending"
    assert db.get_engineering_task(task.id).status == "pending_approval"

    db._conn.execute("DROP TRIGGER abort_engineering_coding_job")
    db._conn.commit()
    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=RunnerSSH(),
            audit_path=audit_path,
            server_configs={"server-a": _server()},
            app_state=app_state,
            local_run=local,
        )
    )

    assert db.get_approval(approval.id).status == "approved"
    assert db.get_engineering_task_job(task.id, "coding", 1).id == result["job"].id
    assert len([r for r in db.list_coding_runs() if r.engineering_task_id == task.id]) == 1
    owned_jobs = [j for j in db.list_jobs() if j.engineering_task_id == task.id]
    assert sorted(j.engineering_task_role for j in owned_jobs) == ["coding", "staging"]
    owner_enqueue_records = [
        record
        for record in read_audit(audit_path)
        if record["action"] == "enqueue"
        and record["params"].get("engineering_task_id") == task.id
    ]
    assert len(owner_enqueue_records) == 2
    jobs_by_id = {job.id: job for job in owned_jobs}
    for record in owner_enqueue_records:
        params = record["params"]
        planned_job = jobs_by_id[params["job_id"]]
        assert "command" not in params
        assert params["command_digest"] == hashlib.sha256(
            planned_job.command.encode()
        ).hexdigest()
        assert params["command_digest_algorithm"] == "sha256"
        assert params["command_display"] in {
            "Stage approved immutable ProjectVersion bundle",
            "Codex agent turn in isolated worktree",
        }


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


def test_engineering_task_status_tracks_staging_failure(db, tmp_path, audit_path):
    version = _project_with_version(db, tmp_path)
    config = _config(tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=config,
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=RunnerSSH(),
            audit_path=audit_path,
            server_configs={"server-a": _server()},
            app_state=SimpleNamespace(
                config=config, ssh_write_file=RecordingWriteFile()
            ),
            local_run=local,
        )
    )
    staging = db.get_engineering_task_job(task.id, "staging", 1)
    db.update_job(staging.id, status="running")
    assert db.refresh_engineering_task_status_from_jobs(task.id) == "staging"
    db.update_job(staging.id, status="failed")
    assert db.refresh_engineering_task_status_from_jobs(task.id) == "staging_failed"


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


def test_request_creates_exact_task_and_pending_approval(db, tmp_path, audit_path):
    version = _project_with_version(db, tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    assert task.project_version_id == version.id
    assert task.base_commit == COMMIT
    assert task.status == "pending_approval"
    assert approval.kind == "coding_task"
    assert approval.status == "pending"
    assert approval.payload["engineering_task_id"] == task.id
    assert approval.payload["base_commit"] == COMMIT
    assert approval.payload["network_access"] is False
    assert task.detected_metadata["languages"] == ["python"]


def test_request_rejects_project_version_from_different_project(db, tmp_path):
    _project_with_version(db, tmp_path, "proj1")
    other = _project_with_version(db, tmp_path, "proj2")
    with pytest.raises(InvalidEngineeringTaskRequestError, match="不屬於"):
        asyncio.run(
            request_engineering_task_approval(
                db,
                "proj1",
                project_version_id=other.id,
                agent_provider_id="codex",
                structured_request=_structured_request(),
                config=_config(tmp_path),
                server_enabled={"server-a": True},
                local_run=EngineeringLocalRun(),
            )
        )
    assert db.list_approvals() == []
    assert db.list_engineering_tasks() == []


def test_request_rejects_unapproved_provider_before_hub_inspection(
    db, tmp_path, audit_path
):
    version = _project_with_version(db, tmp_path)
    local = EngineeringLocalRun()

    with pytest.raises(InvalidEngineeringTaskRequestError, match="未核准"):
        asyncio.run(
            request_engineering_task_approval(
                db,
                "proj1",
                project_version_id=version.id,
                agent_provider_id="unapproved-provider",
                structured_request=_structured_request(),
                config=_config(tmp_path),
                server_enabled={"server-a": True},
                local_run=local,
                audit_path=audit_path,
            )
        )

    assert local.calls == []
    assert db.list_approvals() == []
    assert db.list_engineering_tasks() == []


def test_request_rejects_registered_provider_without_start_before_hub_inspection(
    db, tmp_path, audit_path, monkeypatch
):
    version = _project_with_version(db, tmp_path)
    local = EngineeringLocalRun()
    reviewed = require_coding_agent_provider("codex")
    unavailable_descriptor = replace(
        reviewed.descriptor,
        capabilities=replace(
            reviewed.descriptor.capabilities,
            start_turn=False,
        ),
    )
    unavailable_provider = SimpleNamespace(descriptor=unavailable_descriptor)
    monkeypatch.setattr(
        approvals_module,
        "get_coding_agent_provider",
        lambda _provider_id: unavailable_provider,
    )

    with pytest.raises(InvalidEngineeringTaskRequestError, match="無法啟動"):
        asyncio.run(
            request_engineering_task_approval(
                db,
                "proj1",
                project_version_id=version.id,
                agent_provider_id="codex",
                structured_request=_structured_request(),
                config=_config(tmp_path),
                server_enabled={"server-a": True},
                local_run=local,
                audit_path=audit_path,
            )
        )

    assert local.calls == []
    assert db.list_approvals() == []
    assert db.list_engineering_tasks() == []


def test_approval_refuses_provider_launch_drift_before_instruction_or_job_side_effect(
    db, tmp_path, audit_path, monkeypatch
):
    version = _project_with_version(db, tmp_path)
    config = _config(tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=config,
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    reviewed = require_coding_agent_provider("codex")

    class DriftedProvider:
        descriptor = reviewed.descriptor

        def start_turn(self, request):
            return replace(
                reviewed.start_turn(request), adapter="unreviewed-adapter"
            )

    monkeypatch.setattr(
        approvals_module,
        "require_coding_agent_provider",
        lambda _provider_id: DriftedProvider(),
    )
    runner = RunnerSSH()
    writer = RecordingWriteFile()

    with pytest.raises(ValueError, match="launch/output contract"):
        asyncio.run(
            approve(
                db,
                approval.id,
                ssh_run=runner,
                audit_path=audit_path,
                server_configs={"server-a": _server()},
                app_state=SimpleNamespace(config=config, ssh_write_file=writer),
                local_run=local,
            )
        )

    assert db.get_approval(approval.id).status == "pending"
    assert db.get_engineering_task(task.id).status == "pending_approval"
    assert db.get_coding_run_by_engineering_attempt(task.id, 1) is None
    assert db.list_engineering_task_jobs(task.id) == []
    assert writer.calls == []
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_version_id", "00000000-0000-4000-8000-000000000001"),
        ("base_commit", "b" * 40),
        ("agent_provider_id", "unapproved-provider"),
        ("provider_capabilities", {"adapter": "tampered"}),
        ("execution_contract", {"runner": {"name": "other"}}),
        ("contract_version", "engineering-task-v3"),
        ("structured_request", {"objective": "tampered"}),
        ("instruction", "tampered"),
        ("detected_metadata", {"languages": ["tampered"]}),
        ("runner_server", "other"),
    ],
)
def test_engineering_task_contract_columns_are_not_updateable(
    db, tmp_path, field, value
):
    version = _project_with_version(db, tmp_path)
    task, _approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=EngineeringLocalRun(),
        )
    )
    before = db.get_engineering_task(task.id)

    with pytest.raises(ValueError, match="invalid engineering_task field"):
        db.update_engineering_task(task.id, **{field: value})

    assert db.get_engineering_task(task.id) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, []),
        ("engineering_task_id", None),
        ("engineering_task_id", "00000000-0000-4000-8000-000000000002"),
        ("project", None),
        ("project", "other-project"),
        ("project_id", "00000000-0000-4000-8000-000000000003"),
        ("project_version_id", "00000000-0000-4000-8000-000000000004"),
        ("base_commit", "b" * 40),
        ("agent_provider_id", "unapproved-provider"),
        ("provider_capabilities", {"adapter": "tampered"}),
        ("execution_contract", {"runner": {"name": "other"}}),
        ("contract_version", "engineering-task-v3"),
        ("structured_request", {"objective": "tampered"}),
        ("instruction", ""),
        ("instruction", "tampered"),
        ("detected_metadata", {"languages": ["tampered"]}),
        ("validation_target", "server-a"),
        ("runner_server", "other"),
        ("network_access", True),
        ("dependency_installation", True),
        ("source_kind", "mirror"),
        ("source", "https://example.invalid/tampered.git"),
    ],
)
def test_engineering_approval_rejects_tampered_payload_without_side_effects(
    db, tmp_path, audit_path, field, value
):
    version = _project_with_version(db, tmp_path)
    config = _config(tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=config,
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    payload = dict(approval.payload) if field is not None else value
    if field is not None:
        payload[field] = value
    db.update_approval(approval.id, payload=payload)
    local_calls_before = list(local.calls)
    runner = RunnerSSH()
    writer = RecordingWriteFile()

    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=runner,
            audit_path=audit_path,
            server_configs={"server-a": _server()},
            app_state=SimpleNamespace(config=config, ssh_write_file=writer),
            local_run=local,
        )
    )

    assert result["approval"].status == "rejected"
    assert db.get_engineering_task(task.id).status == "rejected"
    assert db.get_coding_run_by_engineering_attempt(task.id, 1) is None
    assert db.list_engineering_task_jobs(task.id) == []
    assert runner.calls == []
    assert writer.calls == []
    assert local.calls == local_calls_before
    assert db.get_project_version(version.id).git_commit == COMMIT


def test_engineering_approval_rejects_provider_removed_from_registry(
    db, tmp_path, audit_path
):
    version = _project_with_version(db, tmp_path)
    config = _config(tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=config,
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    unapproved_snapshot = {
        **task.provider_capabilities,
        "provider_id": "unapproved-provider",
    }
    payload = {
        **approval.payload,
        "agent_provider_id": "unapproved-provider",
        "provider_capabilities": unapproved_snapshot,
    }
    db._conn.execute(
        "UPDATE engineering_tasks "
        "SET agent_provider_id = ?, provider_capabilities = ? WHERE id = ?",
        ("unapproved-provider", json.dumps(unapproved_snapshot), task.id),
    )
    db._conn.execute(
        "UPDATE approvals SET payload = ? WHERE id = ?",
        (json.dumps(payload), approval.id),
    )
    db._conn.commit()
    local_calls_before = list(local.calls)
    runner = RunnerSSH()
    writer = RecordingWriteFile()

    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=runner,
            audit_path=audit_path,
            server_configs={"server-a": _server()},
            app_state=SimpleNamespace(config=config, ssh_write_file=writer),
            local_run=local,
        )
    )

    assert result["approval"].status == "rejected"
    assert db.get_engineering_task(task.id).status == "rejected"
    assert db.get_coding_run_by_engineering_attempt(task.id, 1) is None
    assert db.list_engineering_task_jobs(task.id) == []
    assert runner.calls == []
    assert writer.calls == []
    assert local.calls == local_calls_before


def test_pending_engineering_approval_pauses_and_resumes_with_backend_flag(
    db, tmp_path, audit_path
):
    version = _project_with_version(db, tmp_path)
    config = _config(tmp_path)
    local = EngineeringLocalRun()
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(),
            config=config,
            server_enabled={"server-a": True},
            local_run=local,
            audit_path=audit_path,
        )
    )
    raw_payload_before = db._conn.execute(
        "SELECT payload FROM approvals WHERE id = ?", (approval.id,)
    ).fetchone()[0]
    writer = RecordingWriteFile()
    app_state = SimpleNamespace(config=config, ssh_write_file=writer)
    config.engineering_task_backend_v1 = False

    with pytest.raises(ValueError, match="backend 未啟用"):
        asyncio.run(
            approve(
                db,
                approval.id,
                ssh_run=RunnerSSH(),
                audit_path=audit_path,
                server_configs={"server-a": _server()},
                app_state=app_state,
                local_run=local,
            )
        )

    assert db.get_approval(approval.id).status == "pending"
    assert db.get_engineering_task(task.id).status == "pending_approval"
    assert db.get_coding_run_by_engineering_attempt(task.id, 1) is None
    assert db.list_engineering_task_jobs(task.id) == []
    assert writer.calls == []
    assert db._conn.execute(
        "SELECT payload FROM approvals WHERE id = ?", (approval.id,)
    ).fetchone()[0] == raw_payload_before

    config.engineering_task_backend_v1 = True
    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=RunnerSSH(),
            audit_path=audit_path,
            server_configs={"server-a": _server()},
            app_state=app_state,
            local_run=local,
        )
    )

    assert result["approval"].status == "approved"
    assert db.get_engineering_task_job(task.id, "coding", 1) is not None
    assert db._conn.execute(
        "SELECT payload FROM approvals WHERE id = ?", (approval.id,)
    ).fetchone()[0] == raw_payload_before


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


def test_capabilities_and_create_route_are_flagged(api_client):
    client, main_module = api_client
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/proj1.git")
    capabilities = client.get("/engineering-tasks/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["enabled"] is False
    coding_agents = client.get("/coding-agents")
    assert coding_agents.status_code == 200
    assert coding_agents.json() == {
        "providers": [
            {
                "provider_id": "codex",
                "display_name": "Codex",
                "adapter": "codex-exec-v1",
                "operations": {
                    "start_turn": True,
                    "resume_turn": False,
                    "cancel_turn": False,
                    "event_stream": False,
                    "command_approval_callback": False,
                },
                "execution_mode": "single_turn_process",
                "protocol_stability": "reviewed_legacy_adapter",
                "outputs": {
                    "final_response": True,
                    "checkpoint": False,
                    "event_stream": False,
                    "machine_event_log": True,
                },
                "policy_scope": {
                    "engineering_task_network": "disabled",
                    "legacy_network_override": "platform_config_only",
                    "dependency_installation": "not_authorized",
                    "inner_command_approval": "unavailable",
                    "inner_command_enforcement": "sandbox_only",
                    "final_git_path_policy": (
                        "runner_pre_bundle_and_server_a_pre_accept"
                    ),
                    "turn_time_path_confinement": "unavailable",
                },
            }
        ]
    }
    response = client.post(
        "/projects/proj1/engineering-tasks/request", json=_api_body("missing")
    )
    assert response.status_code == 404
    assert main_module.app_state.db.list_approvals() == []


def test_coding_agents_endpoint_appends_unwired_app_server_only_when_enabled(
    tmp_path, monkeypatch
):
    """D1 bounded first slice (docs/DECISIONS.md): ``CONTROLLED_CODING_RUNNER_V1``
    only changes ``GET /coding-agents`` visibility. It must never widen the
    Engineering Task provider-selection/approval-payload contract exposed by
    ``GET /engineering-tasks/capabilities``."""

    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.setenv("CONTROLLED_CODING_RUNNER_V1", "true")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        coding_agents = client.get("/coding-agents")
        capabilities = client.get("/engineering-tasks/capabilities")

    assert coding_agents.status_code == 200
    providers = coding_agents.json()["providers"]
    assert [provider["provider_id"] for provider in providers] == [
        "codex",
        "codex-app-server",
    ]
    app_server = providers[1]
    assert app_server["adapter"] == "codex-app-server-v1"
    assert app_server["execution_mode"] == "not_wired"
    assert app_server["operations"] == {
        "start_turn": False,
        "resume_turn": False,
        "cancel_turn": False,
        "event_stream": False,
        "command_approval_callback": False,
    }

    assert capabilities.status_code == 200
    assert [
        provider["provider_id"] for provider in capabilities.json()["providers"]
    ] == ["codex"]


def test_structured_api_and_approval_create_staging_dependency(engineering_client):
    client, main_module, _local, runner_ssh, writes, tmp_path = engineering_client
    db, version = _prepare_api_project(main_module, tmp_path)

    create = client.post(
        "/projects/proj1/engineering-tasks/request", json=_api_body(version.id)
    )
    assert create.status_code == 200, create.text
    body = create.json()
    assert body["task"]["base_binding"] == "project_version_pinned"
    assert body["task"]["base_commit"] == COMMIT
    assert body["approval"]["kind"] == "coding_task"
    approval_id = body["approval"]["id"]

    approved = client.post(f"/approve/{approval_id}")
    assert approved.status_code == 200, approved.text
    result = approved.json()
    task = db.get_engineering_task(result["engineering_task_id"])
    run = db.get_coding_run(result["coding_run_id"])
    staging = db.get_job(result["staging_job_id"])
    coding = db.get_job(result["job"]["id"])
    assert task.status == "queued"
    assert run.project_version_id == version.id
    assert run.base_commit == COMMIT
    assert run.base_binding == "project_version_pinned"
    assert staging.type == "sync" and staging.pin_server == "_local"
    assert staging.engineering_task_role == "staging"
    assert coding.depends_on == [staging.id]
    assert coding.engineering_task_role == "coding"
    assert 'export R_BASE_COMMIT="$(git -C "$SRC" rev-parse HEAD)"' not in coding.command
    assert 'export R_BASE_COMMIT="$(git --git-dir=' not in coding.command
    assert "remote update" not in coding.command
    assert writes.calls and writes.calls[0][0] == "_local"
    assert writes.calls[0][1].endswith(f"{task.id}.instruction.txt")
    assert COMMIT not in writes.calls[0][2]
    assert not any("mkdir -p codex_workspaces/tasks" in call[1] for call in runner_ssh.calls)


def test_structured_api_reports_runner_probe_failure_without_creating_request(
    engineering_client,
):
    client, main_module, _local, _runner_ssh, _writes, tmp_path = engineering_client
    db, version = _prepare_api_project(main_module, tmp_path)

    async def failed_probe(_server, _command, _timeout):
        raise ConnectionError("synthetic private runner detail")

    main_module.app_state.ssh_run = failed_probe
    main_module.app_state._codex_probe_cache = None
    main_module.app_state._codex_probe_cache_at = None
    response = client.post(
        "/projects/proj1/engineering-tasks/request", json=_api_body(version.id)
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Coding Runner 能力探測失敗"
    assert "synthetic private runner detail" not in response.text
    assert db.list_engineering_tasks() == []
    assert db.list_approvals() == []


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
    response = client.get("/engineering-tasks?project=proj1")
    assert response.status_code == 200
    legacy = next(row for row in response.json() if row["coding_run_id"] == legacy_id)
    assert legacy["id"] == f"legacy-coding-run-{legacy_id}"
    assert legacy["base_binding"] == "legacy_unpinned"
    assert legacy["project_version_id"] is None
    assert legacy["base_commit"] is None
    assert legacy["observed_base_commit"] == COMMIT


def test_pinned_completion_mismatch_preserves_base_and_rejects_bundle(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, version = _prepare_api_project(main_module, tmp_path)
    create = client.post(
        "/projects/proj1/engineering-tasks/request", json=_api_body(version.id)
    ).json()
    approved = client.post(f"/approve/{create['approval']['id']}").json()
    run = db.get_coding_run(approved["coding_run_id"])
    job = db.get_job(approved["job"]["id"])
    result_dir = Path(local_result_dir(job.id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "done",
                "base_commit": "b" * 40,
                "result_commit": "c" * 40,
            }
        )
    )
    (result_dir / "changes.bundle").write_bytes(b"untrusted")

    _backfill_coding_run(
        job,
        db=db,
        config=main_module.app_state.config,
        audit_path=main_module.app_state.config.audit_path,
    )
    updated = db.get_coding_run(run.id)
    task = db.get_engineering_task(updated.engineering_task_id)
    assert updated.base_commit == COMMIT
    assert updated.status == "failed"
    assert updated.result_commit is None
    assert updated.bundle_path is None
    assert task.status == "failed"


def test_pinned_completion_accepts_only_verified_descendant_bundle(engineering_client):
    client, main_module, local, _ssh, _writes, tmp_path = engineering_client
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str, cwd: Path = source) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Engineering Test")
    (source / "app.py").write_text("BASE = True\n")
    git("add", "app.py")
    git("commit", "-qm", "base")
    base_commit = git("rev-parse", "HEAD")

    hub = tmp_path / "git" / "proj1.git"
    hub.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(hub)],
        text=True,
        capture_output=True,
        check=True,
    )

    git("checkout", "-qb", "task")
    (source / "app.py").write_text("BASE = True\nRESULT = True\n")
    git("add", "app.py")
    git("commit", "-qm", "result")
    result_commit = git("rev-parse", "HEAD")

    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version(
        "proj1", base_commit, git_ref="main"
    )
    local.commit = base_commit
    request_body = _api_body(version.id)
    request_body["allowed_paths"] = ["app.py"]
    created = client.post(
        "/projects/proj1/engineering-tasks/request", json=request_body
    ).json()
    approved = client.post(f"/approve/{created['approval']['id']}").json()
    run = db.get_coding_run(approved["coding_run_id"])
    job = db.get_job(approved["job"]["id"])

    result_dir = Path(local_result_dir(job.id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    git(
        "bundle",
        "create",
        str(result_dir / "changes.bundle"),
        f"{base_commit}..task",
    )
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "done",
                "base_commit": base_commit,
                "result_branch": "task",
                "result_commit": result_commit,
                "test_command": "python -m pytest -q",
                "test_exit_code": 0,
            }
        )
    )

    _backfill_coding_run(
        job,
        db=db,
        config=main_module.app_state.config,
        audit_path=main_module.app_state.config.audit_path,
    )
    updated = db.get_coding_run(run.id)
    assert updated.status == "done"
    assert updated.base_commit == base_commit
    assert updated.result_commit == result_commit
    assert updated.bundle_path == str(result_dir / "changes.bundle")
    assert db.get_engineering_task(updated.engineering_task_id).status == "done"
