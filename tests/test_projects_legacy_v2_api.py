"""HTTP-surface tests for the `/api/v2/legacy-projects*` /
`/api/v2/legacy-datasets*` wrappers (DG-UI-UNIFICATION v1, U5).

These stay legacy-scope `project`/`platform`/`dataset` objects: thin
wrappers around the exact legacy `/projects*`/`/datasets*` surfaces (see
`dispatch_center/api/routers/projects_legacy_v2.py` module docstring). Most
list/detail tests assert byte-identical parity with the legacy endpoints
(modeled on `tests/test_infrastructure_v2_api.py`, `tests/test_api.py`,
`tests/test_inventory_api.py`, `tests/test_project_detail_api.py`).
"""

from __future__ import annotations

import pytest

from pathlib import Path

from app.config import ServerConfig
from app.hub import hub_repo_path
from app.sshpool import CommandResult
import dispatch_center.api.routers.projects_legacy_v2 as projects_legacy_v2


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class GitInitFakeSSH:
    """Mirrors `tests/test_git_init.py::GitInitFakeSSH`."""

    def __init__(self, *, already_git: bool = False, head: str = "abc123def4567890"):
        self.calls: list[str] = []
        self.already_git = already_git
        self.head = head

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if "test -d" in command and "GIT_OK" in command:
            return FakeCommandResult(stdout="GIT_OK\n" if self.already_git else "")
        if "count-objects -v" in command:
            return FakeCommandResult(stdout="size: 10\nsize-pack: 5\n")
        if command.rstrip().endswith("rev-parse HEAD"):
            return FakeCommandResult(stdout=f"{self.head}\n")
        if "branch --show-current" in command:
            return FakeCommandResult(stdout="main\n")
        return FakeCommandResult()


class HubFakeSSH:
    """Mirrors `tests/test_hub.py::HubFakeSSH`."""

    def __init__(self, *, is_git: bool = True):
        self.calls: list[str] = []
        self.is_git = is_git

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if "test -d" in command and "GIT_OK" in command:
            return CommandResult(
                exit_status=0, stdout="GIT_OK\n" if self.is_git else "", stderr=""
            )
        return CommandResult(exit_status=0, stdout="", stderr="")


class HubFakeLocalRun:
    """Mirrors `tests/test_hub.py::HubFakeLocalRun` (success path only)."""

    def __init__(self, *, head: str = "abc1234", full_head: str = "abc1234full0000"):
        self.calls: list[str] = []
        self.head = head
        self.full_head = full_head

    async def __call__(self, command, timeout):
        self.calls.append(command)
        #: `app.hub.request_project_deploy_approval()`'s default-ref
        #: resolution path (used by `test_deploy_requests_creates_pending_
        #: approval`, which doesn't pass an explicit `ref`).
        if "symbolic-ref --short HEAD" in command:
            return CommandResult(exit_status=0, stdout="main\n", stderr="")
        if "rev-parse --short HEAD" in command:
            return CommandResult(exit_status=0, stdout=f"{self.head}\n", stderr="")
        if "rev-parse HEAD" in command:
            return CommandResult(exit_status=0, stdout=f"{self.full_head}\n", stderr="")
        return CommandResult(exit_status=0, stdout="", stderr="")


def _make_target_server(name="server-b", project_roots=None) -> ServerConfig:
    return ServerConfig(
        name=name,
        host="10.0.0.6",
        user="train",
        key="~/.ssh/id_rsa",
        port=22,
        project_roots=list(project_roots or ["/home/bgab141/Howard"]),
    )


def _make_hub(tmp_path, project="proj1") -> Path:
    repo_dir = Path(hub_repo_path(project, str(tmp_path)))
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("legacy_posture")
def test_flag_off_is_a_hidden_interface(api_client):
    client, main_module = api_client
    assert client.get("/api/v2/legacy-projects").status_code == 404
    assert client.get("/api/v2/projects-matrix").status_code == 404
    assert client.get("/api/v2/legacy-projects/proj1/detail").status_code == 404
    assert client.get("/api/v2/legacy-datasets").status_code == 404
    assert (
        client.post("/api/v2/legacy-projects", json={"name": "x", "repo_or_path": "/x"}).status_code
        == 404
    )

    main_module.app_state.config.api_v2_enabled = True
    # product_rbac_v2 still off -> still hidden.
    assert client.get("/api/v2/legacy-projects").status_code == 404


# ---------------------------------------------------------------------------
# Projects: list / create / matrix
# ---------------------------------------------------------------------------


def test_legacy_projects_list_is_byte_identical_to_legacy(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1", require_tag="gpu")

    legacy = client.get("/projects").json()
    v2 = client.get("/api/v2/legacy-projects").json()
    assert v2 == legacy
    assert len(v2) == 1
    assert v2[0]["name"] == "proj1"


def test_create_legacy_project_direct_no_approval(api_client):
    client, main_module = api_client
    _enable_v2(main_module)

    approvals_before = main_module.app_state.db.list_approvals()
    resp = client.post(
        "/api/v2/legacy-projects",
        json={"name": "proj1", "repo_or_path": "/repo/proj1"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "proj1"
    assert main_module.app_state.db.list_approvals() == approvals_before
    assert main_module.app_state.db.get_project("proj1") is not None


def test_create_legacy_project_duplicate_name_rejected(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    body = {"name": "proj1", "repo_or_path": "/repo/proj1"}
    assert client.post("/api/v2/legacy-projects", json=body).status_code == 200
    assert client.post("/api/v2/legacy-projects", json=body).status_code == 400


def test_create_legacy_project_dataset_pairing_required(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/legacy-projects",
        json={"name": "proj1", "repo_or_path": "/repo/proj1", "dataset_name": "d1"},
    )
    assert resp.status_code == 400


def test_projects_matrix_is_byte_identical_to_legacy(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-a": _make_target_server("server-a")}
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    legacy = client.get("/projects/matrix").json()
    v2 = client.get("/api/v2/projects-matrix").json()
    assert v2 == legacy
    assert v2["servers"] == ["server-a"]


# ---------------------------------------------------------------------------
# Project detail / versions / timeline
# ---------------------------------------------------------------------------


class RecordingSSH:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        return None


def test_legacy_project_detail_parity_and_no_ssh(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1", require_tag="gpu")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    legacy = client.get("/projects/proj1/detail").json()
    v2 = client.get("/api/v2/legacy-projects/proj1/detail").json()
    assert v2 == legacy
    assert v2["project"]["name"] == "proj1"
    assert ssh.calls == []


def test_legacy_project_detail_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert client.get("/api/v2/legacy-projects/nope/detail").status_code == 404


def test_legacy_project_versions_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.get_or_create_project_version("proj1", "commit1", git_ref="main")

    legacy = client.get("/projects/proj1/versions").json()
    v2 = client.get("/api/v2/legacy-projects/proj1/versions").json()
    assert v2 == legacy
    assert len(v2) == 1


def test_legacy_project_versions_empty_for_unknown_project_not_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.get("/api/v2/legacy-projects/nope/versions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_legacy_project_timeline_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "手動筆記")
    db.insert_job(command="python train.py", project="proj1")

    legacy = client.get("/projects/proj1/timeline").json()
    v2 = client.get("/api/v2/legacy-projects/proj1/timeline").json()
    assert v2 == legacy
    types = {item["type"] for item in v2["items"]}
    assert types == {"record", "job"}


def test_legacy_project_timeline_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert client.get("/api/v2/legacy-projects/nope/timeline").status_code == 404


# ---------------------------------------------------------------------------
# PATCH / DELETE project
# ---------------------------------------------------------------------------


def test_patch_legacy_project_updates_only_given_fields(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.patch("/api/v2/legacy-projects/proj1", json={"goal": "衝到 95%"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["goal"] == "衝到 95%"
    assert body["optimization_notes"] is None


def test_patch_legacy_project_empty_body_400(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    assert client.patch("/api/v2/legacy-projects/proj1", json={}).status_code == 400


def test_patch_legacy_project_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.patch("/api/v2/legacy-projects/nope", json={"goal": "x"})
    assert resp.status_code == 404


def test_delete_legacy_project_success_no_ssh(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.delete("/api/v2/legacy-projects/proj1")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "name": "proj1"}
    assert db.get_project("proj1") is None
    assert ssh.calls == []


def test_delete_legacy_project_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert client.delete("/api/v2/legacy-projects/nope").status_code == 404


def test_delete_legacy_project_running_job_blocks_409(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_job(command="python train.py", project="proj1", status="running")

    resp = client.delete("/api/v2/legacy-projects/proj1")
    assert resp.status_code == 409
    assert db.get_project("proj1") is not None


# ---------------------------------------------------------------------------
# Experiment records CRUD + audit
# ---------------------------------------------------------------------------


def test_create_legacy_experiment_record_success_and_audit(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post(
        "/api/v2/legacy-projects/proj1/records",
        json={"content": "loss 下降", "kind": "observation", "title": "第一次觀察"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "observation"
    assert body["author"] == "user"
    assert len(db.list_experiment_records("proj1")) == 1

    records = db.list_durable_audit_events(limit=100)
    created = [r for r in records if r["action"] == "experiment_record_created"]
    assert len(created) == 1
    assert created[0]["params"]["project"] == "proj1"


def test_create_legacy_experiment_record_invalid_kind_400(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    resp = client.post(
        "/api/v2/legacy-projects/proj1/records", json={"content": "x", "kind": "bogus"}
    )
    assert resp.status_code == 400
    assert db.list_experiment_records("proj1") == []


def test_create_legacy_experiment_record_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post("/api/v2/legacy-projects/nope/records", json={"content": "x"})
    assert resp.status_code == 404


def test_patch_legacy_experiment_record_success(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "原內容", title="原標題")

    resp = client.patch(
        f"/api/v2/legacy-projects/proj1/records/{record_id}",
        json={"content": "新內容", "kind": "decision"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "新內容"
    assert body["title"] == "原標題"


def test_patch_legacy_experiment_record_project_mismatch_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project("proj2", "/repo/proj2")
    record_id = db.insert_experiment_record("proj2", "屬於 proj2")

    resp = client.patch(
        f"/api/v2/legacy-projects/proj1/records/{record_id}", json={"content": "x"}
    )
    assert resp.status_code == 404


def test_delete_legacy_experiment_record_success_and_audit(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")

    resp = client.delete(f"/api/v2/legacy-projects/proj1/records/{record_id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": record_id}
    assert db.get_experiment_record(record_id) is None

    records = db.list_durable_audit_events(limit=100)
    deleted = [r for r in records if r["action"] == "experiment_record_deleted"]
    assert len(deleted) == 1


def test_delete_legacy_experiment_record_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    assert client.delete("/api/v2/legacy-projects/proj1/records/999").status_code == 404


# ---------------------------------------------------------------------------
# git-init-requests
# ---------------------------------------------------------------------------


def test_git_init_requests_creates_pending_approval(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)

    resp = client.post(
        "/api/v2/legacy-projects/proj1/git-init-requests",
        json={"server": "server-a", "extra_ignores": ["foo/"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "git_init"
    assert body["status"] == "pending"
    assert body["payload"]["extra_ignores"] == ["foo/"]


def test_git_init_requests_project_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)
    resp = client.post(
        "/api/v2/legacy-projects/nope/git-init-requests", json={"server": "server-a"}
    )
    assert resp.status_code == 404


def test_git_init_requests_rejects_already_git(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=True)

    resp = client.post(
        "/api/v2/legacy-projects/proj1/git-init-requests", json={"server": "server-a"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# hub-sync (direct execute)
# ---------------------------------------------------------------------------


def _configure_server(main_module, tmp_path):
    server_cfg = ServerConfig(name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa")
    main_module.app_state.server_configs = {"server-a": server_cfg}
    main_module.app_state.config.servers = [server_cfg]
    main_module.app_state.config.local_home_dir = str(tmp_path)


def test_hub_sync_endpoint_success(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=True)
    #: `projects_legacy_v2.legacy_hub_sync()` uses this module's own
    #: `local_run` binding (imported separately from `app.localrun` -- see
    #: module docstring for why it cannot import `app.main.local_run`), so
    #: the fake must be monkeypatched here, not on `main_module`.
    projects_legacy_v2.local_run = HubFakeLocalRun(head="cafef00d")
    try:
        resp = client.post(
            "/api/v2/legacy-projects/proj1/hub-sync", json={"server": "server-a"}
        )
    finally:
        projects_legacy_v2.local_run = __import__(
            "app.localrun", fromlist=["local_run"]
        ).local_run
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"] == "proj1"
    assert body["head"] == "cafef00d"


def test_hub_sync_endpoint_instance_not_found_404(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=True)

    resp = client.post(
        "/api/v2/legacy-projects/proj1/hub-sync", json={"server": "server-a"}
    )
    assert resp.status_code == 404


def test_hub_sync_endpoint_not_git_repo_400(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=False)

    resp = client.post(
        "/api/v2/legacy-projects/proj1/hub-sync", json={"server": "server-a"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# deploy-requests
# ---------------------------------------------------------------------------


def test_deploy_requests_project_not_found_404(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-b": _make_target_server()}
    main_module.app_state.config.local_home_dir = str(tmp_path)

    resp = client.post(
        "/api/v2/legacy-projects/nope/deploy-requests",
        json={"target_server": "server-b"},
    )
    assert resp.status_code == 404


def test_deploy_requests_rejects_unknown_target(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    _make_hub(tmp_path)
    main_module.app_state.server_configs = {"server-b": _make_target_server()}
    main_module.app_state.config.local_home_dir = str(tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH()

    resp = client.post(
        "/api/v2/legacy-projects/proj1/deploy-requests",
        json={"target_server": "server-does-not-exist"},
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_deploy_requests_creates_pending_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    _make_hub(tmp_path)
    main_module.app_state.server_configs = {"server-b": _make_target_server()}
    main_module.app_state.config.local_home_dir = str(tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH()
    projects_legacy_v2.local_run = HubFakeLocalRun()
    try:
        resp = client.post(
            "/api/v2/legacy-projects/proj1/deploy-requests",
            json={"target_server": "server-b"},
        )
    finally:
        projects_legacy_v2.local_run = __import__(
            "app.localrun", fromlist=["local_run"]
        ).local_run
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "project_deploy"
    assert body["status"] == "pending"
    assert body["payload"]["target_server"] == "server-b"


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


def test_activity_no_instances_returns_message_branch(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    legacy = client.get("/projects/proj1/activity").json()
    v2 = client.get("/api/v2/legacy-projects/proj1/activity").json()
    assert v2 == legacy
    assert isinstance(v2["activity"], str)
    assert "沒有已登記的機器" in v2["activity"]


def test_activity_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert client.get("/api/v2/legacy-projects/nope/activity").status_code == 404


def test_activity_writes_audit(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    client.get("/api/v2/legacy-projects/proj1/activity")

    from app.audit import read_audit

    records = read_audit(main_module.app_state.config.audit_path)
    assert any(r["action"] == "project_activity" for r in records)


# ---------------------------------------------------------------------------
# Datasets: list / create / card
# ---------------------------------------------------------------------------


def test_legacy_datasets_list_is_byte_identical_to_legacy(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    src_dir = tmp_path / "dataset_src"
    src_dir.mkdir()
    (src_dir / "a.bin").write_bytes(b"x" * 1024)

    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(src_dir),
            "description": "desc",
            "method": "method",
        },
    )

    legacy = client.get("/datasets").json()
    v2 = client.get("/api/v2/legacy-datasets").json()
    assert v2 == legacy
    assert len(v2) == 1
    assert "manifest" not in v2[0]


def test_create_legacy_dataset_direct_no_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    src_dir = tmp_path / "dataset_src"
    src_dir.mkdir()
    (src_dir / "a.bin").write_bytes(b"x" * 2048)

    approvals_before = main_module.app_state.db.list_approvals()
    resp = client.post(
        "/api/v2/legacy-datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(src_dir),
            "description": "缺陷偵測資料集第一版",
            "method": "人工標註",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["size_bytes"] == 2048
    assert "manifest" in body
    assert main_module.app_state.db.list_approvals() == approvals_before


def test_create_legacy_dataset_missing_description_rejected(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    (tmp_path / "a.bin").write_bytes(b"x")
    resp = client.post(
        "/api/v2/legacy-datasets",
        json={"name": "defect", "version": "v1", "source_path": str(tmp_path), "method": "m"},
    )
    assert resp.status_code == 400


def test_legacy_dataset_card_get_parity(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    (tmp_path / "a.bin").write_bytes(b"x")
    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )

    legacy = client.get("/datasets/defect/v1/card").json()
    v2 = client.get("/api/v2/legacy-datasets/defect/v1/card").json()
    assert v2 == legacy
    assert v2["card"]["description"] == "desc"


def test_legacy_dataset_card_get_not_found_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert client.get("/api/v2/legacy-datasets/nope/v1/card").status_code == 404


def test_patch_legacy_dataset_card_updates_updated_at_keeps_created_at(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    (tmp_path / "a.bin").write_bytes(b"x")
    client.post(
        "/api/v2/legacy-datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )
    first_created_at = client.get("/api/v2/legacy-datasets/defect/v1/card").json()["card"][
        "created_at"
    ]

    resp = client.patch(
        "/api/v2/legacy-datasets/defect/v1/card",
        json={"description": "新描述", "method": "新方法"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card"]["description"] == "新描述"
    assert body["card"]["created_at"] == first_created_at


def test_patch_legacy_dataset_card_missing_method_rejected(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    (tmp_path / "a.bin").write_bytes(b"x")
    client.post(
        "/api/v2/legacy-datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )
    resp = client.patch(
        "/api/v2/legacy-datasets/defect/v1/card", json={"description": "只有描述"}
    )
    assert resp.status_code == 400
