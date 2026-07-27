"""AI Engineering Task path-policy coverage preview（唯讀、advisory-only）。

覆蓋兩層：直接呼叫 ``app.engineering_tasks.preview_hub_path_policy_coverage``
的純函式行為（fake ``local_run``，不碰 DB／HTTP），以及
``POST /projects/{name}/engineering-tasks/path-policy-coverage`` 端點的
flag/project/version gate 與序列化邊界。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.engineering_path_policy import _scope_matches
from app.engineering_tasks import (
    InvalidEngineeringTaskRequestError,
    preview_hub_path_policy_coverage,
)


COMMIT = "a" * 40
TREE_PATHS = [
    "pyproject.toml",
    "app/main.py",
    "app/engineering_tasks.py",
    "tests/conftest.py",
    "docs/.env.example",
]


@dataclass
class _CommandResult:
    stdout: str = ""
    exit_status: int = 0


class _FakeLocalRun:
    def __init__(self, commit: str = COMMIT, tree_paths=None):
        self.commit = commit
        self.tree_paths = TREE_PATHS if tree_paths is None else tree_paths
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, command: str, timeout: int):
        self.calls.append((command, timeout))
        if "rev-parse --verify" in command:
            return _CommandResult(stdout=f"{self.commit}\n")
        if "ls-tree -r --name-only" in command:
            return _CommandResult(stdout="\n".join(self.tree_paths) + "\n")
        return _CommandResult()


def _hub_dir(tmp_path, project="proj1"):
    hub = tmp_path / "git" / f"{project}.git"
    hub.mkdir(parents=True)
    return hub


class _RunnerSSH:
    async def __call__(self, server: str, command: str, timeout: int):
        if "command -v codex" in command:
            return _CommandResult(stdout="codex-cli 0.144.3\nAUTH_OK\n")
        return _CommandResult()


class _RecordingWriteFile:
    async def __call__(self, server: str, path: str, content: str):
        return None


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

    local = _FakeLocalRun(tree_paths=TREE_PATHS + ["ruff.toml"])
    runner_ssh = _RunnerSSH()
    writes = _RecordingWriteFile()

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


# ---------------------------------------------------------------------------
# 純函式層：preview_hub_path_policy_coverage()
# ---------------------------------------------------------------------------


def test_missing_hub_directory_fails_closed(tmp_path):
    local = _FakeLocalRun()
    with pytest.raises(InvalidEngineeringTaskRequestError, match="hub sync"):
        asyncio.run(
            preview_hub_path_policy_coverage(
                project_name="proj1",
                git_commit=COMMIT,
                allowed_paths=["."],
                prohibited_paths=[],
                local_home_dir=str(tmp_path),
                local_run=local,
            )
        )
    assert local.calls == []


def test_commit_mismatch_fails_closed(tmp_path):
    _hub_dir(tmp_path)
    local = _FakeLocalRun(commit="b" * 40)
    with pytest.raises(InvalidEngineeringTaskRequestError, match="exact commit"):
        asyncio.run(
            preview_hub_path_policy_coverage(
                project_name="proj1",
                git_commit=COMMIT,
                allowed_paths=["."],
                prohibited_paths=[],
                local_home_dir=str(tmp_path),
                local_run=local,
            )
        )


def test_invalid_rules_reject_without_echoing_rule_text(tmp_path):
    _hub_dir(tmp_path)
    local = _FakeLocalRun()
    with pytest.raises(InvalidEngineeringTaskRequestError) as caught:
        asyncio.run(
            preview_hub_path_policy_coverage(
                project_name="proj1",
                git_commit=COMMIT,
                allowed_paths=[],
                prohibited_paths=[],
                local_home_dir=str(tmp_path),
                local_run=local,
            )
        )
    assert "canonical relative POSIX scope" in str(caught.value)


def test_exact_and_subtree_and_zero_hit_counts(tmp_path):
    _hub_dir(tmp_path)
    local = _FakeLocalRun()
    result = asyncio.run(
        preview_hub_path_policy_coverage(
            project_name="proj1",
            git_commit=COMMIT,
            allowed_paths=["app/", "pyproject.toml", "missing-dir/"],
            prohibited_paths=["docs/.env.example"],
            local_home_dir=str(tmp_path),
            local_run=local,
        )
    )
    assert result["base_commit"] == COMMIT
    assert result["tree_file_count"] == len(TREE_PATHS)
    assert result["truncated"] is False

    by_scope = {row["scope"]: row for row in result["allowed"]}
    assert by_scope["app/"]["file_hits"] == 2
    assert by_scope["pyproject.toml"]["file_hits"] == 1
    assert by_scope["missing-dir/"]["file_hits"] == 0

    prohibited = result["prohibited"][0]
    assert prohibited["scope"] == "docs/.env.example"
    assert prohibited["file_hits"] == 1


def test_exact_rule_matching_existing_directory_is_flagged(tmp_path):
    _hub_dir(tmp_path)
    local = _FakeLocalRun()
    result = asyncio.run(
        preview_hub_path_policy_coverage(
            project_name="proj1",
            git_commit=COMMIT,
            allowed_paths=["app", "pyproject.toml"],
            prohibited_paths=[],
            local_home_dir=str(tmp_path),
            local_run=local,
        )
    )
    by_scope = {row["scope"]: row for row in result["allowed"]}
    # "app" (exact) collides with the existing "app/" directory — the classic
    # allowed_paths mistake this preview exists to surface before finalize.
    assert by_scope["app"]["matches_existing_directory"] is True
    assert by_scope["app"]["file_hits"] == 0
    # "pyproject.toml" is a real file, not a directory prefix of anything.
    assert by_scope["pyproject.toml"]["matches_existing_directory"] is False


def test_rule_targeting_secret_basename_is_flagged(tmp_path):
    _hub_dir(tmp_path)
    local = _FakeLocalRun()
    result = asyncio.run(
        preview_hub_path_policy_coverage(
            project_name="proj1",
            git_commit=COMMIT,
            allowed_paths=["docs/.env.example", "pyproject.toml"],
            prohibited_paths=[],
            local_home_dir=str(tmp_path),
            local_run=local,
        )
    )
    by_scope = {row["scope"]: row for row in result["allowed"]}
    assert by_scope["docs/.env.example"]["secret_protected"] is True
    assert by_scope["pyproject.toml"]["secret_protected"] is False


def test_tree_larger_than_cap_is_truncated_not_rejected(tmp_path, monkeypatch):
    import app.engineering_tasks as engineering_tasks

    monkeypatch.setattr(
        engineering_tasks, "ENGINEERING_TASK_PATH_COVERAGE_MAX_TREE_PATHS", 3
    )
    _hub_dir(tmp_path)
    local = _FakeLocalRun(tree_paths=[f"file{i}.py" for i in range(10)])
    result = asyncio.run(
        preview_hub_path_policy_coverage(
            project_name="proj1",
            git_commit=COMMIT,
            allowed_paths=["."],
            prohibited_paths=[],
            local_home_dir=str(tmp_path),
            local_run=local,
        )
    )
    assert result["truncated"] is True
    assert result["tree_file_count"] == 3


def test_coverage_counts_agree_with_brute_force_scope_matches(tmp_path):
    _hub_dir(tmp_path)
    local = _FakeLocalRun()
    allowed = ["app/", "pyproject.toml", "."]
    result = asyncio.run(
        preview_hub_path_policy_coverage(
            project_name="proj1",
            git_commit=COMMIT,
            allowed_paths=allowed,
            prohibited_paths=[],
            local_home_dir=str(tmp_path),
            local_run=local,
        )
    )
    for row in result["allowed"]:
        expected = sum(
            1 for path in TREE_PATHS if _scope_matches(row["scope"], path)
        )
        assert row["file_hits"] == expected


def test_response_never_serializes_a_repo_path_string(tmp_path):
    import json

    _hub_dir(tmp_path)
    local = _FakeLocalRun()
    # "app/" is the only caller-supplied scope; echoing a caller's own input
    # back is not a leak.  What must never appear is a tree path the caller
    # did not already name, e.g. the individual filenames inside app/.
    result = asyncio.run(
        preview_hub_path_policy_coverage(
            project_name="proj1",
            git_commit=COMMIT,
            allowed_paths=["app/"],
            prohibited_paths=[],
            local_home_dir=str(tmp_path),
            local_run=local,
        )
    )
    serialized = json.dumps(result)
    for path in TREE_PATHS:
        if path in ("app/",):
            continue
        assert path not in serialized


# ---------------------------------------------------------------------------
# 端點層：POST /projects/{name}/engineering-tasks/path-policy-coverage
# ---------------------------------------------------------------------------


def test_endpoint_404s_when_backend_flag_is_off(api_client):
    client, main_module = api_client
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/proj1.git")
    response = client.post(
        "/projects/proj1/engineering-tasks/path-policy-coverage",
        json={"project_version_id": "missing", "allowed_paths": ["."]},
    )
    assert response.status_code == 404


def test_endpoint_404s_for_missing_project(engineering_client):
    client, _main_module, _local, _ssh, _writes, _tmp_path = engineering_client
    response = client.post(
        "/projects/does-not-exist/engineering-tasks/path-policy-coverage",
        json={"project_version_id": "missing", "allowed_paths": ["."]},
    )
    assert response.status_code == 404


def test_endpoint_400s_for_version_not_belonging_to_project(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, version = _prepare(main_module, tmp_path)
    other_id = "not-a-real-version-id"
    response = client.post(
        "/projects/proj1/engineering-tasks/path-policy-coverage",
        json={"project_version_id": other_id, "allowed_paths": ["."]},
    )
    assert response.status_code == 400


def test_endpoint_400s_for_invalid_rules_without_leaking_rule_text(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, version = _prepare(main_module, tmp_path)
    response = client.post(
        "/projects/proj1/engineering-tasks/path-policy-coverage",
        json={"project_version_id": version.id, "allowed_paths": []},
    )
    assert response.status_code == 400
    assert "canonical relative POSIX scope" in response.json()["detail"]


def test_endpoint_returns_coverage_for_a_valid_request(engineering_client):
    client, main_module, _local, _ssh, _writes, tmp_path = engineering_client
    db, version = _prepare(main_module, tmp_path)
    response = client.post(
        "/projects/proj1/engineering-tasks/path-policy-coverage",
        json={
            "project_version_id": version.id,
            "allowed_paths": ["app/"],
            "prohibited_paths": ["ruff.toml"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["base_commit"] == COMMIT
    assert body["allowed"][0]["scope"] == "app/"
    assert body["allowed"][0]["file_hits"] == 2
    assert body["prohibited"][0]["scope"] == "ruff.toml"
    assert body["prohibited"][0]["file_hits"] == 1


def _prepare(main_module, tmp_path):
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    return db, version
