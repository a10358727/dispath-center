"""Integration evidence for the Engineering Task v2 final-Git path policy.

These tests intentionally cross the request, approval, deterministic Runner
wrapper, and Server-A result-acceptance boundaries.  The smaller matching and
Git parser cases live in ``test_engineering_path_policy.py``; this module proves
that the independently approved policy actually reaches every enforcement
point without changing already-pending v1 tasks.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.approvals import (
    approve,
    build_coding_task_script,
    request_engineering_task_approval,
)
from app.coding_agents import require_coding_agent_provider
from app.config import AppConfig, ServerConfig
from app.db import Database
from app.engineering_path_policy import (
    CONTRACT_VERSION,
    build_engineering_path_policy,
    current_engineering_path_verifier_contract,
    engineering_path_verifier_source,
    render_engineering_path_policy_file,
)
from app.engineering_tasks import (
    local_engineering_instruction_relpath,
    local_engineering_path_policy_relpath,
    local_engineering_path_verifier_relpath,
    remote_engineering_bundle_path,
)
from app.jobfinish import _backfill_coding_run
from app.results import local_result_dir


FAKE_COMMIT = "a" * 40
TASK_ID = "11111111-1111-4111-8111-111111111111"


@dataclass
class _CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0


class _HubInspection:
    def __init__(self, commit: str):
        self.commit = commit
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, command: str, timeout: int):
        self.calls.append((command, timeout))
        if "rev-parse --verify" in command:
            return _CommandResult(stdout=f"{self.commit}\n")
        if "ls-tree -r --name-only" in command:
            return _CommandResult(stdout="allowed.txt\nseed.txt\npyproject.toml\n")
        return _CommandResult()


class _RunnerProbe:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, server: str, command: str, timeout: int):
        self.calls.append((server, command, timeout))
        if "command -v codex" in command:
            return _CommandResult(stdout="codex-cli 0.144.3\nAUTH_OK\n")
        return _CommandResult()


class _Writes:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, server: str, path: str, content: str):
        self.calls.append((server, path, content))


def _server() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="~/.ssh/id_rsa",
        port=32221,
        enabled=True,
    )


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        servers=[_server()],
        local_home_dir=str(tmp_path),
        codex_runner_server="server-a",
        codex_workspace_root="~/codex_workspaces",
        engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
    )


def _structured_request(**overrides) -> dict:
    request = {
        "objective": "Enforce the approved final Git path policy",
        "background": "The final result must be independently reviewable",
        "expected_changes": ["Change only the approved scope"],
        "non_goals": [],
        "allowed_paths": ["approved-scope/"],
        "prohibited_paths": ["approved-scope/private/"],
        "prohibited_changes": ["Do not alter deployment behavior"],
        "acceptance_criteria": ["Reject every out-of-scope final tree"],
        "validation": {
            "tests_lint": False,
            "build_smoke": False,
            "continue_fixing_failures": False,
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


def _insert_fake_project(db: Database, tmp_path: Path):
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", FAKE_COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    return version


def _request_v2(db: Database, tmp_path: Path, *, structured_request=None):
    version = _insert_fake_project(db, tmp_path)
    local = _HubInspection(FAKE_COMMIT)
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=structured_request or _structured_request(),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=local,
        )
    )
    return task, approval, local


def _approve(
    db: Database,
    tmp_path: Path,
    approval_id: int,
    local: _HubInspection,
):
    writes = _Writes()
    runner = _RunnerProbe()
    result = asyncio.run(
        approve(
            db,
            approval_id,
            ssh_run=runner,
            server_configs={"server-a": _server()},
            app_state=SimpleNamespace(
                config=_config(tmp_path),
                ssh_write_file=writes,
            ),
            local_run=local,
        )
    )
    return result, writes, runner


def test_v2_approval_binds_exact_policy_and_stages_three_data_files(db, tmp_path):
    task, approval, local = _request_v2(db, tmp_path)
    expected_policy, expected_digest = build_engineering_path_policy(
        ["approved-scope/"], ["approved-scope/private/"]
    )
    contract = task.execution_contract

    assert task.contract_version == CONTRACT_VERSION
    assert contract["path_policy"] == expected_policy
    assert contract["path_policy_sha256"] == expected_digest
    assert contract["path_verifier"] == current_engineering_path_verifier_contract()
    assert approval.payload["execution_contract"] == contract
    canonical_policy = render_engineering_path_policy_file(expected_policy)
    assert hashlib.sha256(canonical_policy.encode()).hexdigest() == expected_digest

    result, writes, _runner = _approve(db, tmp_path, approval.id, local)
    staging = db.get_job(result["staging_job_id"])
    coding = db.get_job(result["job"].id)
    assert [(server, path) for server, path, _content in writes.calls] == [
        ("_local", local_engineering_instruction_relpath(task.id)),
        ("_local", local_engineering_path_policy_relpath(task.id)),
        ("_local", local_engineering_path_verifier_relpath(task.id)),
    ]
    assert writes.calls[1][2] == canonical_policy
    assert writes.calls[2][2] == engineering_path_verifier_source()
    assert hashlib.sha256(writes.calls[2][2].encode()).hexdigest() == (
        contract["path_verifier"]["source_sha256"]
    )

    assert ".path-policy.json" in staging.command
    assert ".path-policy-verifier.py" in staging.command
    assert "path-policy.json" in coding.command
    assert "path-policy-verifier.py" in coding.command
    assert expected_digest in coding.command
    # Approved rules are data.  They may appear in the staged instruction and
    # policy JSON, but never in an executable shell command.
    for approved_rule in ("approved-scope/", "approved-scope/private/"):
        assert approved_rule not in staging.command
        assert approved_rule not in coding.command
    assert canonical_policy not in staging.command
    assert canonical_policy not in coding.command

    for command in (staging.command, coding.command):
        syntax = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr


def _insert_pending_v1(db: Database, tmp_path: Path):
    version = _insert_fake_project(db, tmp_path)
    project = db.get_project("proj1")
    provider = require_coding_agent_provider("codex").descriptor
    execution_contract = {
        "runner": {
            "name": "server-a",
            "host": "192.0.2.10",
            "user": "runner",
            "port": 32221,
        },
        "workspace_rel": "codex_workspaces",
        "source_kind": "hub_bundle",
        "source": remote_engineering_bundle_path(TASK_ID),
        "network_access": False,
        "dependency_installation": False,
    }
    structured_request = _structured_request()
    instruction = "AI Engineering Task\n\nLegacy v1 advisory path wording"
    metadata = {
        "detection": "path-markers-v1",
        "languages": ["python"],
        "frameworks": [],
        "validation_tools": [],
    }
    payload = {
        "contract_version": "engineering-task-v1",
        "engineering_task_id": TASK_ID,
        "project": "proj1",
        "project_id": project.id,
        "project_version_id": version.id,
        "base_commit": FAKE_COMMIT,
        "agent_provider_id": "codex",
        "provider_capabilities": provider.capability_snapshot(),
        "execution_contract": execution_contract,
        "structured_request": structured_request,
        "instruction": instruction,
        "detected_metadata": metadata,
        "validation_target": None,
        "runner_server": "server-a",
        "source_kind": "hub_bundle",
        "source": remote_engineering_bundle_path(TASK_ID),
        "network_access": False,
        "dependency_installation": False,
    }
    task_id, approval_id = db.insert_engineering_task_request(
        project_id=project.id,
        project_name="proj1",
        project_version_id=version.id,
        base_commit=FAKE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities=provider.capability_snapshot(),
        execution_contract=execution_contract,
        contract_version="engineering-task-v1",
        structured_request=structured_request,
        instruction=instruction,
        detected_metadata=metadata,
        runner_server="server-a",
        validation_target=None,
        approval_payload=payload,
        task_id=TASK_ID,
    )
    return task_id, approval_id, _HubInspection(FAKE_COMMIT)


def test_pending_v1_approval_keeps_single_instruction_stage_and_legacy_wrapper(
    db, tmp_path
):
    task_id, approval_id, local = _insert_pending_v1(db, tmp_path)

    result, writes, _runner = _approve(db, tmp_path, approval_id, local)
    staging = db.get_job(result["staging_job_id"])
    coding = db.get_job(result["job"].id)

    assert db.get_approval(approval_id).status == "approved"
    assert db.get_engineering_task(task_id).contract_version == "engineering-task-v1"
    assert [(server, path) for server, path, _content in writes.calls] == [
        ("_local", local_engineering_instruction_relpath(task_id))
    ]
    assert ".path-policy.json" not in staging.command
    assert ".path-policy-verifier.py" not in staging.command
    assert "path-policy.json" not in coding.command
    assert "path-policy-verifier.py" not in coding.command
    assert "path_policy_violation" not in coding.command
    syntax = subprocess.run(
        ["bash", "-n"],
        input=coding.command,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr


@pytest.mark.parametrize("drift", ["policy", "digest", "verifier", "structured"])
def test_v2_policy_contract_drift_is_rederived_and_rejected_before_writes_or_jobs(
    db, tmp_path, drift
):
    task, approval, local = _request_v2(db, tmp_path)
    execution_contract = copy.deepcopy(task.execution_contract)
    structured_request = copy.deepcopy(task.structured_request)

    if drift == "policy":
        changed_policy, changed_digest = build_engineering_path_policy(
            ["different-scope/"], []
        )
        execution_contract["path_policy"] = changed_policy
        execution_contract["path_policy_sha256"] = changed_digest
        execution_contract["path_verifier"] = changed_policy["verifier"]
    elif drift == "digest":
        execution_contract["path_policy_sha256"] = "0" * 64
    elif drift == "verifier":
        execution_contract["path_verifier"] = {
            "version": "engineering-path-verifier-v1",
            "source_sha256": "0" * 64,
        }
    else:
        structured_request["allowed_paths"] = ["different-scope/"]

    payload = copy.deepcopy(approval.payload)
    payload["execution_contract"] = execution_contract
    payload["structured_request"] = structured_request
    db._conn.execute(
        "UPDATE engineering_tasks SET execution_contract = ?, structured_request = ? "
        "WHERE id = ?",
        (json.dumps(execution_contract), json.dumps(structured_request), task.id),
    )
    db._conn.execute(
        "UPDATE approvals SET payload = ? WHERE id = ?",
        (json.dumps(payload), approval.id),
    )
    db._conn.commit()
    calls_before = list(local.calls)

    result, writes, runner = _approve(db, tmp_path, approval.id, local)

    assert result["approval"].status == "rejected"
    assert db.get_approval(approval.id).status == "rejected"
    assert db.get_engineering_task(task.id).status == "rejected"
    assert db.list_engineering_task_jobs(task.id) == []
    assert db.get_coding_run_by_engineering_attempt(task.id, 1) is None
    assert writes.calls == []
    assert runner.calls == []
    assert local.calls == calls_before


def test_generated_v2_wrapper_has_valid_bash_and_v1_has_no_policy_gate():
    policy, digest = build_engineering_path_policy(
        ["approved-rule-never-in-shell/"], ["approved-rule-never-in-shell/private/"]
    )
    common = (
        17,
        "codex_workspaces",
        "proj1",
        "hub_bundle",
        remote_engineering_bundle_path(TASK_ID),
        None,
        False,
    )
    v1 = build_coding_task_script(*common, exact_base_commit=FAKE_COMMIT)
    v2 = build_coding_task_script(
        *common,
        exact_base_commit=FAKE_COMMIT,
        path_policy_sha256=digest,
        path_verifier_sha256=policy["verifier"]["source_sha256"],
    )

    for script in (v1, v2):
        syntax = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr
    assert "path-policy.json" not in v1
    assert "path-policy-verifier.py" not in v1
    assert "path_policy_violation" not in v1
    assert "path-policy.json" in v2
    assert "path-policy-verifier.py" in v2
    assert "path_policy_violation" in v2
    assert digest in v2
    for rule in (*policy["allowed_paths"], *policy["prohibited_paths"]):
        assert rule not in v2


def _git(repo: Path, *arguments: str, capture: bool = False) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip() if capture else ""


def _make_source_repo(root: Path) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "dispatch@example.invalid")
    _git(source, "config", "user.name", "Dispatch Test")
    (source / "seed.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "seed.txt")
    _git(source, "commit", "-qm", "base")
    return source, _git(source, "rev-parse", "HEAD", capture=True)


def _write_fake_codex(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import subprocess
            import sys

            args = sys.argv[1:]
            if args == ["--version"]:
                print("codex-cli 0.144.3")
                raise SystemExit(0)
            if args == ["login", "status"]:
                raise SystemExit(0)
            if not args or args[0] != "exec":
                raise SystemExit(2)

            repo = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("-o") + 1])
            mode = os.environ["FAKE_CODEX_MODE"]
            if mode == "already-committed":
                (repo / "allowed.txt").write_text("allowed result\\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "allowed.txt"], check=True)
                subprocess.run(
                    [
                        "git", "-C", str(repo),
                        "-c", "user.name=Fake Agent",
                        "-c", "user.email=fake@example.invalid",
                        "commit", "-qm", "agent-created commit",
                    ],
                    check=True,
                )
            elif mode == "outside-newline":
                (repo / "outside\\nfile.txt").write_text("outside\\n", encoding="utf-8")
            elif mode == "detached":
                subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
                (repo / "allowed.txt").write_text("detached\\n", encoding="utf-8")
            else:
                raise SystemExit(3)
            output.write_text("fake final response\\n", encoding="utf-8")
            print('{"type":"turn.completed"}')
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_real_wrapper(tmp_path: Path, mode: str):
    home = tmp_path / "home"
    home.mkdir()
    source, base = _make_source_repo(tmp_path)
    bundle_path = home / remote_engineering_bundle_path(TASK_ID)
    bundle_path.parent.mkdir(parents=True)
    _git(source, "bundle", "create", str(bundle_path), "--all")

    approval_id = 17
    job_id = 501
    task_dir = home / "codex_workspaces" / "tasks" / str(approval_id)
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.txt").write_text("test instruction\n", encoding="utf-8")
    policy, digest = build_engineering_path_policy(["allowed.txt"], [])
    (task_dir / "path-policy.json").write_text(
        render_engineering_path_policy_file(policy), encoding="utf-8"
    )
    (task_dir / "path-policy-verifier.py").write_text(
        engineering_path_verifier_source(), encoding="utf-8"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_codex(fake_bin / "codex")
    job_dir = home / "agent_jobs" / str(job_id)
    job_dir.mkdir(parents=True)
    script = build_coding_task_script(
        approval_id,
        "codex_workspaces",
        "proj1",
        "hub_bundle",
        remote_engineering_bundle_path(TASK_ID),
        None,
        False,
        exact_base_commit=base,
        path_policy_sha256=digest,
        path_verifier_sha256=policy["verifier"]["source_sha256"],
    )
    script_path = job_dir / "cmd.sh"
    script_path.write_text(script, encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_CODEX_MODE": mode,
        }
    )
    completed = subprocess.run(
        ["bash", str(script_path)],
        cwd=job_dir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    result_path = home / "results" / str(job_id) / "result.json"
    assert result_path.is_file(), completed.stdout + completed.stderr
    return completed, json.loads(result_path.read_text(encoding="utf-8")), task_dir, base


def test_real_v2_wrapper_squashes_an_agent_commit_to_one_base_child(tmp_path):
    completed, result, task_dir, base = _run_real_wrapper(
        tmp_path, "already-committed"
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["status"] == "done"
    assert result["result_commit"] != base
    repo = task_dir / "repo"
    assert _git(
        repo, "rev-list", "--count", f"{base}..{result['result_commit']}", capture=True
    ) == "1"
    assert _git(repo, "rev-parse", f"{result['result_commit']}^", capture=True) == base
    assert _git(repo, "show", "-s", "--format=%s", "HEAD", capture=True) == (
        "AI coding task #17"
    )
    assert (task_dir / "changes.bundle").is_file()


@pytest.mark.parametrize("mode", ["outside-newline", "detached"])
def test_real_v2_wrapper_rejects_unreviewable_path_or_detached_branch_without_bundle(
    tmp_path, mode
):
    completed, result, task_dir, _base = _run_real_wrapper(tmp_path, mode)

    assert completed.returncode != 0
    assert result["status"] == "path_policy_violation"
    assert not (task_dir / "changes.bundle").exists()
    assert not (tmp_path / "home" / "results" / "501" / "changes.bundle").exists()
    assert "outside\nfile.txt" not in completed.stdout


def _prepare_real_native_task(db: Database, tmp_path: Path, *, allowed_path: str):
    source, base = _make_source_repo(tmp_path)
    hub = tmp_path / "git" / "proj1.git"
    hub.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(hub)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", base, git_ref="main")
    local = _HubInspection(base)
    task, approval = asyncio.run(
        request_engineering_task_approval(
            db,
            "proj1",
            project_version_id=version.id,
            agent_provider_id="codex",
            structured_request=_structured_request(
                allowed_paths=[allowed_path], prohibited_paths=[]
            ),
            config=_config(tmp_path),
            server_enabled={"server-a": True},
            local_run=local,
        )
    )
    approved, _writes, _runner = _approve(db, tmp_path, approval.id, local)
    return source, base, task, db.get_coding_run(approved["coding_run_id"]), db.get_job(
        approved["job"].id
    )


@pytest.mark.parametrize(
    ("changed_path", "expected_status", "accepted"),
    [
        ("allowed.txt", "done", True),
        ("outside.txt", "path_policy_violation", False),
    ],
)
def test_server_a_reverifies_runner_bundle_before_accepting_artifact(
    db, tmp_path, changed_path, expected_status, accepted
):
    source, base, task, run, job = _prepare_real_native_task(
        db, tmp_path, allowed_path="allowed.txt"
    )
    _git(source, "checkout", "-qb", "runner-result")
    (source / changed_path).write_text("runner result\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "runner result")
    result_commit = _git(source, "rev-parse", "HEAD", capture=True)

    result_dir = Path(local_result_dir(job.id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    _git(
        source,
        "bundle",
        "create",
        str(result_dir / "changes.bundle"),
        f"{base}..runner-result",
    )
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "done",
                "base_commit": base,
                "result_branch": "ai-task-1",
                "result_commit": result_commit,
            }
        ),
        encoding="utf-8",
    )

    _backfill_coding_run(
        job,
        db=db,
        config=_config(tmp_path),
        audit_path=str(tmp_path / "audit.jsonl"),
    )

    updated = db.get_coding_run(run.id)
    assert updated.status == expected_status
    assert db.get_engineering_task(task.id).status == expected_status
    bundle_artifact = next(
        artifact
        for artifact in db.list_engineering_task_artifacts(task.id)
        if artifact.artifact_key == "bundle"
    )
    if accepted:
        assert updated.result_commit == result_commit
        assert updated.bundle_path == str(result_dir / "changes.bundle")
        assert bundle_artifact.verification_status == "verified"
        assert bundle_artifact.availability == "available"
    else:
        assert updated.result_commit is None
        assert updated.bundle_path is None
        assert bundle_artifact.verification_status == "rejected"
        assert bundle_artifact.availability != "available"

