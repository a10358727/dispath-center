"""階段 15 Phase B（PLAN.md P.2.2/P.2.3 節）：中央 hub 同步——直接執行的
web 動作＋稽核 `hub_sync`，不出核准卡；`GET /projects/matrix` 加 `hub` 欄。

涵蓋：
- `build_hub_pull_command()`：純函式，含 port（`build_ssh_opts()` 統一
  入口）。
- `sync_project_to_hub()`：成功路徑指令斷言（bundle create --all／rsync
  含 port／init --bare 冪等／verify → fetch）；非 git instance 400
  （`HubSyncError`）；instance 不存在（`ProjectInstanceResolutionError`
  往上傳）；稽核 hub_sync。
- `POST /projects/{name}/hub-sync` 端點：instance 不存在 404，不是 git
  repo 400。
- `GET /projects/matrix` 的 `hub` 欄：不存在 exists:false；存在（測試建
  一個本地 bare repo）head/last_sync 有值。
- 前端 smoke（見 tests/test_git_init.py 的
  test_index_page_renders_git_init_kind，一併涵蓋 hub-sync 關鍵字）。
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from app.activity import ProjectInstanceResolutionError
from app.config import AppConfig, ServerConfig
from app.hub import (
    HubSyncError,
    build_hub_pull_command,
    get_project_hub_info,
    hub_repo_path,
    local_hub_bundle_dir,
    sync_project_to_hub,
)
from app.audit import read_audit
from app.sshpool import CommandResult


def _setup_project(db, server="server-a", path="/data/proj1"):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server=server, path=path)


def make_server(name="server-a", port=22) -> ServerConfig:
    return ServerConfig(name=name, host="10.0.0.5", user="train", key="~/.ssh/id_rsa", port=port)


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------


def test_hub_repo_path_uses_local_home_dir():
    assert hub_repo_path("proj1", "/srv/dispatch") == "/srv/dispatch/git/proj1.git"
    assert hub_repo_path("proj1", ".") == "./git/proj1.git"


def test_local_hub_bundle_dir_uses_local_home_dir():
    assert local_hub_bundle_dir("proj1", "/srv/dispatch") == "/srv/dispatch/hub_bundles/proj1/"


def test_build_hub_pull_command_contains_expected_pieces():
    server = make_server()
    cmd = build_hub_pull_command("proj1", server, "/srv/dispatch")
    assert "mkdir -p" in cmd
    assert "rsync -a -e" in cmd
    assert "train@10.0.0.5:hub_bundles/proj1.bundle" in cmd
    assert "/srv/dispatch/hub_bundles/proj1/" in cmd
    assert "-p 22" not in cmd


def test_build_hub_pull_command_non_default_port_appends_dash_p():
    server = make_server(name="pro6000", port=32221)
    cmd = build_hub_pull_command("proj1", server, "/srv/dispatch")
    assert "-p 32221" in cmd


# ---------------------------------------------------------------------------
# sync_project_to_hub()
# ---------------------------------------------------------------------------


class HubFakeSSH:
    def __init__(self, *, is_git: bool = True):
        self.calls: list[str] = []
        self.is_git = is_git

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if "test -d" in command and "GIT_OK" in command:
            return CommandResult(exit_status=0, stdout="GIT_OK\n" if self.is_git else "", stderr="")
        return CommandResult(exit_status=0, stdout="", stderr="")


class HubFakeLocalRun:
    def __init__(
        self,
        *,
        pull_ok: bool = True,
        init_ok: bool = True,
        verify_ok: bool = True,
        fetch_ok: bool = True,
        head: str = "abc1234",
        full_head: str = "abc1234full0000000000000000000000000000",
    ):
        self.calls: list[str] = []
        self.pull_ok = pull_ok
        self.init_ok = init_ok
        self.verify_ok = verify_ok
        self.fetch_ok = fetch_ok
        self.head = head
        self.full_head = full_head

    async def __call__(self, command, timeout):
        self.calls.append(command)
        if "rsync" in command:
            ok = self.pull_ok
            return CommandResult(exit_status=0 if ok else 1, stdout="", stderr="" if ok else "rsync failed")
        if "init --bare" in command:
            ok = self.init_ok
            return CommandResult(exit_status=0 if ok else 1, stdout="", stderr="" if ok else "init failed")
        if "bundle verify" in command:
            ok = self.verify_ok
            return CommandResult(exit_status=0 if ok else 1, stdout="", stderr="" if ok else "verify failed")
        if "fetch" in command:
            ok = self.fetch_ok
            return CommandResult(exit_status=0 if ok else 1, stdout="", stderr="" if ok else "fetch failed")
        if "rev-parse --short HEAD" in command:
            return CommandResult(exit_status=0, stdout=f"{self.head}\n", stderr="")
        if "rev-parse HEAD" in command:
            return CommandResult(exit_status=0, stdout=f"{self.full_head}\n", stderr="")
        return CommandResult(exit_status=0, stdout="", stderr="")


def _make_config(tmp_path, port=22) -> AppConfig:
    return AppConfig(
        servers=[ServerConfig(name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa", port=port)],
        local_home_dir=str(tmp_path),
    )


def test_sync_project_to_hub_success_full_command_sequence(db, audit_path, tmp_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    ssh = HubFakeSSH(is_git=True)
    local_run = HubFakeLocalRun(head="cafef00d")
    config = _make_config(tmp_path)

    result = asyncio.run(
        sync_project_to_hub(
            db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
        )
    )
    assert result["head"] == "cafef00d"

    ssh_joined = "\n".join(ssh.calls)
    assert "test -d" in ssh_joined and "GIT_OK" in ssh_joined
    assert "bundle create" in ssh_joined and "--all" in ssh_joined
    assert "hub_bundles/proj1.bundle" in ssh_joined

    local_calls = local_run.calls
    assert any("rsync" in c and "-p 22" not in c for c in local_calls)
    assert any("init --bare" in c for c in local_calls)
    assert any("bundle verify" in c for c in local_calls)
    # 實跑發現的 bug 修正：`git bundle verify` 沒有 repo context 會直接失敗
    # 「error: need a repository to verify a bundle」，指令必須帶
    # `--git-dir={hub_repo_path}`（`init --bare` 那一步已經保證這個 repo
    # 存在）。
    verify_call = next(c for c in local_calls if "bundle verify" in c)
    assert "--git-dir=" in verify_call
    assert hub_repo_path("proj1", str(tmp_path)) in verify_call
    assert any("fetch" in c and "+refs/heads/*:refs/heads/*" in c for c in local_calls)

    idx_pull = next(i for i, c in enumerate(local_calls) if "rsync" in c)
    idx_init = next(i for i, c in enumerate(local_calls) if "init --bare" in c)
    idx_verify = next(i for i, c in enumerate(local_calls) if "bundle verify" in c)
    idx_fetch = next(i for i, c in enumerate(local_calls) if "fetch" in c and "verify" not in c)
    assert idx_pull < idx_init < idx_verify < idx_fetch

    records = read_audit(audit_path)
    hub_sync_events = [r for r in records if r["action"] == "hub_sync"]
    assert len(hub_sync_events) == 1
    assert hub_sync_events[0]["params"]["project"] == "proj1"
    assert hub_sync_events[0]["params"]["server"] == "server-a"
    assert hub_sync_events[0]["params"]["head"] == "cafef00d"


def test_sync_project_to_hub_creates_project_version_with_full_commit(db, audit_path, tmp_path):
    """PLAN.md 2026-07-11 版 §14 切片 4:hub_sync 成功後登記一筆
    ProjectVersion,用**完整** commit(不是 `--short` 那個只夠顯示的
    值),`source_instance_id` 指向這次同步的 instance。"""
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    instance_id = db.insert_project_instance(
        project_name="proj1", server="server-a", path="/data/proj1", git_branch="main"
    )
    ssh = HubFakeSSH(is_git=True)
    local_run = HubFakeLocalRun(head="cafef00d", full_head="cafef00dfull1234567890abcdef1234567890")
    config = _make_config(tmp_path)

    result = asyncio.run(
        sync_project_to_hub(
            db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
        )
    )
    assert result["version_id"]

    versions = db.list_project_versions("proj1")
    assert len(versions) == 1
    assert versions[0].id == result["version_id"]
    assert versions[0].git_commit == "cafef00dfull1234567890abcdef1234567890"
    assert versions[0].git_ref == "main"
    assert versions[0].source_instance_id == instance_id

    records = read_audit(audit_path)
    hub_sync_events = [r for r in records if r["action"] == "hub_sync"]
    assert hub_sync_events[0]["params"]["version_id"] == result["version_id"]


def test_sync_project_to_hub_same_commit_reuses_project_version(db, audit_path, tmp_path):
    """同一個 commit 重複同步(冪等 hub_sync 的既有慣例)不應該產生第二筆
    ProjectVersion。"""
    _setup_project(db, server="server-a", path="/data/proj1")
    ssh = HubFakeSSH(is_git=True)
    config = _make_config(tmp_path)

    first = asyncio.run(
        sync_project_to_hub(
            db, "proj1", "server-a", ssh_run=ssh, local_run=HubFakeLocalRun(),
            config=config, audit_path=audit_path,
        )
    )
    second = asyncio.run(
        sync_project_to_hub(
            db, "proj1", "server-a", ssh_run=ssh, local_run=HubFakeLocalRun(),
            config=config, audit_path=audit_path,
        )
    )
    assert first["version_id"] == second["version_id"]
    assert len(db.list_project_versions("proj1")) == 1


def test_sync_project_to_hub_pull_command_includes_port(db, audit_path, tmp_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    ssh = HubFakeSSH(is_git=True)
    local_run = HubFakeLocalRun()
    config = _make_config(tmp_path, port=32221)

    asyncio.run(
        sync_project_to_hub(
            db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
        )
    )
    assert any("-p 32221" in c for c in local_run.calls)


def test_sync_project_to_hub_not_git_repo_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    ssh = HubFakeSSH(is_git=False)
    local_run = HubFakeLocalRun()
    config = _make_config(tmp_path)

    with pytest.raises(HubSyncError):
        asyncio.run(
            sync_project_to_hub(
                db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
            )
        )
    # 不是 git repo 就不該建立任何 bundle 或動到本地。
    assert not any("bundle create" in c for c in ssh.calls)
    assert local_run.calls == []
    records = read_audit(audit_path)
    assert not any(r["action"] == "hub_sync" for r in records)


def test_sync_project_to_hub_instance_not_found_propagates(db, audit_path, tmp_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    ssh = HubFakeSSH(is_git=True)
    local_run = HubFakeLocalRun()
    config = _make_config(tmp_path)

    with pytest.raises(ProjectInstanceResolutionError):
        asyncio.run(
            sync_project_to_hub(
                db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
            )
        )
    assert ssh.calls == []
    assert local_run.calls == []


def test_sync_project_to_hub_pull_failure_raises_and_stops(db, audit_path, tmp_path):
    _setup_project(db)
    ssh = HubFakeSSH(is_git=True)
    local_run = HubFakeLocalRun(pull_ok=False)
    config = _make_config(tmp_path)

    with pytest.raises(HubSyncError):
        asyncio.run(
            sync_project_to_hub(
                db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
            )
        )
    assert not any("init --bare" in c for c in local_run.calls)
    records = read_audit(audit_path)
    assert not any(r["action"] == "hub_sync" for r in records)


def test_sync_project_to_hub_verify_uses_git_dir_and_failure_stops_before_fetch(db, audit_path, tmp_path):
    """實跑發現的 bug 修正：`git bundle verify` 沒帶 `--git-dir` 會直接失敗
    （「error: need a repository to verify a bundle」）。這裡用
    `verify_ok=False` 模擬 verify 失敗（不管是不是這個原因）：驗證失敗要
    整個中止、不繼續 fetch、不寫 hub_sync 稽核。"""
    _setup_project(db)
    ssh = HubFakeSSH(is_git=True)
    local_run = HubFakeLocalRun(verify_ok=False)
    config = _make_config(tmp_path)

    with pytest.raises(HubSyncError):
        asyncio.run(
            sync_project_to_hub(
                db, "proj1", "server-a", ssh_run=ssh, local_run=local_run, config=config, audit_path=audit_path
            )
        )
    verify_call = next(c for c in local_run.calls if "bundle verify" in c)
    assert "--git-dir=" in verify_call
    assert not any("fetch" in c and "verify" not in c for c in local_run.calls)
    records = read_audit(audit_path)
    assert not any(r["action"] == "hub_sync" for r in records)


# ---------------------------------------------------------------------------
# main.py：POST /projects/{name}/hub-sync
# ---------------------------------------------------------------------------


def _configure_server(main_module, tmp_path, port=22):
    from app.config import ServerConfig as SC

    server_cfg = SC(name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa", port=port)
    main_module.app_state.server_configs = {"server-a": server_cfg}
    main_module.app_state.config.servers = [server_cfg]
    main_module.app_state.config.local_home_dir = str(tmp_path)


def test_hub_sync_endpoint_success(api_client, tmp_path):
    client, main_module = api_client
    db = main_module.app_state.db
    _setup_project(db, server="server-a", path="/data/proj1")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=True)
    # `app.main.hub_sync_endpoint()` 用模組全域的 `local_run`（不是
    # `app_state` 的屬性）——monkeypatch 模組屬性，不真的對外發起本地
    # rsync/git 子行程（那些已經在 sync_project_to_hub() 的單元測試涵蓋）。
    main_module.local_run = HubFakeLocalRun(head="cafef00d")

    resp = client.post("/projects/proj1/hub-sync", json={"server": "server-a"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"] == "proj1"
    assert body["server"] == "server-a"
    assert body["head"] == "cafef00d"


def test_hub_sync_endpoint_success_then_versions_endpoint_lists_it(api_client, tmp_path):
    """PLAN.md 2026-07-11 版 §14 切片 4:`GET /projects/{name}/versions`
    回傳 hub_sync 登記的 ProjectVersion。"""
    client, main_module = api_client
    db = main_module.app_state.db
    _setup_project(db, server="server-a", path="/data/proj1")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=True)
    main_module.local_run = HubFakeLocalRun(head="cafef00d", full_head="cafef00dfull")

    resp = client.post("/projects/proj1/hub-sync", json={"server": "server-a"})
    version_id = resp.json()["version_id"]
    assert version_id

    versions_resp = client.get("/projects/proj1/versions")
    assert versions_resp.status_code == 200
    versions = versions_resp.json()
    assert len(versions) == 1
    assert versions[0]["id"] == version_id
    assert versions[0]["git_commit"] == "cafef00dfull"


def test_versions_endpoint_empty_for_unknown_project(api_client):
    client, _main = api_client
    resp = client.get("/projects/nope/versions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_hub_sync_endpoint_instance_not_found_404(api_client, tmp_path):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=True)

    resp = client.post("/projects/proj1/hub-sync", json={"server": "server-a"})
    assert resp.status_code == 404


def test_hub_sync_endpoint_not_git_repo_400(api_client, tmp_path):
    client, main_module = api_client
    db = main_module.app_state.db
    _setup_project(db, server="server-a", path="/data/proj1")
    _configure_server(main_module, tmp_path)
    main_module.app_state.ssh_run = HubFakeSSH(is_git=False)

    resp = client.post("/projects/proj1/hub-sync", json={"server": "server-a"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /projects/matrix：hub 欄（PLAN.md P.2.3）
# ---------------------------------------------------------------------------


def test_get_project_hub_info_not_exists(tmp_path):
    async def local_run(command, timeout):
        raise AssertionError("不存在的 repo 不該發起任何 local_run 呼叫")

    result = asyncio.run(get_project_hub_info("proj1", str(tmp_path), local_run=local_run))
    assert result == {"exists": False, "head": None, "last_sync": None}


def test_get_project_hub_info_exists_with_head_and_last_sync(tmp_path):
    from app.localrun import local_run as real_local_run

    repo_dir = tmp_path / "git" / "proj1.git"
    subprocess.run(["git", "init", "--bare", str(repo_dir)], check=True, capture_output=True)
    # 建一個 commit 讓 HEAD 真的能被解析（bare repo 剛 init 完是 unborn HEAD）。
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    subprocess.run(["git", "-C", str(work_dir), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "push", str(repo_dir), "HEAD:refs/heads/master"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(repo_dir), "symbolic-ref", "HEAD", "refs/heads/master"],
        check=True, capture_output=True,
    )

    result = asyncio.run(
        get_project_hub_info("proj1", str(tmp_path), local_run=real_local_run)
    )
    assert result["exists"] is True
    assert result["head"] is not None
    assert result["last_sync"] is not None


def test_projects_matrix_endpoint_hub_field_not_exists(api_client, tmp_path):
    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")

    resp = client.get("/projects/matrix")
    assert resp.status_code == 200
    body = resp.json()
    proj1 = next(p for p in body["projects"] if p["name"] == "proj1")
    assert proj1["hub"] == {"exists": False, "head": None, "last_sync": None}


def test_projects_matrix_endpoint_hub_field_exists(api_client, tmp_path):
    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")

    repo_dir = tmp_path / "git" / "proj1.git"
    subprocess.run(["git", "init", "--bare", str(repo_dir)], check=True, capture_output=True)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    subprocess.run(["git", "-C", str(work_dir), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "push", str(repo_dir), "HEAD:refs/heads/master"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(repo_dir), "symbolic-ref", "HEAD", "refs/heads/master"],
        check=True, capture_output=True,
    )

    resp = client.get("/projects/matrix")
    assert resp.status_code == 200
    body = resp.json()
    proj1 = next(p for p in body["projects"] if p["name"] == "proj1")
    assert proj1["hub"]["exists"] is True
    assert proj1["hub"]["head"] is not None
    assert proj1["hub"]["last_sync"] is not None
