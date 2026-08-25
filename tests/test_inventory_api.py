"""階段 8 第一批：Project Inventory 端到端 API + agent 工具測試（PLAN.md
I.7/I.6 節第一批範圍）。fastapi TestClient，不依賴真實 SSH。

涵蓋 I.11 測試清單（第一批範圍）：
- `/inventory/scan` 建 approval 不直接掃；`server="all"` 回明確錯誤（501）；
  非法 project_roots 直接 400 不建 approval
- `/inventory/candidates` CRUD
- import-request/ignore-request 只建 approval，不直接改
  projects/candidates 狀態
- 核准 import_project 後才真的寫 projects/project_instances 且 candidate
  狀態變 imported
- 核准 inventory_scan 後 candidates 表才有資料
- agent 工具（scan_project_inventory/request_import_project_candidate/
  request_ignore_project_candidate）呼叫後只有 approval 產生，db 不變
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_tools import AgentContext, dispatch_tool
from app.config import AppConfig, ServerConfig


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


class InventoryFakeSSH:
    """依指令內容回傳固定的假掃描結果：一個候選專案 proj1（.git/README.md），
    沒有 embedded dataset。記錄所有呼叫過的指令方便斷言。"""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if command.startswith("find") and "-print" in command:
            return FakeCommandResult(
                "/data/projects/proj1/.git\n/data/projects/proj1/README.md\n"
            )
        if "head -c" in command:
            return FakeCommandResult("# proj1\nA demo project.\n")
        if "branch --show-current" in command:
            return FakeCommandResult("main\n")
        if "rev-parse HEAD" in command:
            return FakeCommandResult("abc123\n")
        if "remote get-url origin" in command:
            return FakeCommandResult("https://tok@github.com/x/proj1.git\n")
        if command.startswith("for d in"):
            return FakeCommandResult("")
        return FakeCommandResult("")


def _scan_and_approve(client, main_module, server="server-a", project_roots=None):
    """輔助函式：建立 inventory_scan approval 並核准，回傳 (approval_id, candidates)。"""
    project_roots = project_roots or ["/data/projects"]
    resp = client.post(
        "/inventory/scan", json={"server": server, "project_roots": project_roots}
    )
    assert resp.status_code == 200
    approval = resp.json()
    main_module.app_state.ssh_run = InventoryFakeSSH()
    approve_resp = client.post(f"/approve/{approval['id']}")
    assert approve_resp.status_code == 200
    candidates = client.get("/inventory/candidates").json()
    return approval["id"], candidates


# ---------------------------------------------------------------------------
# POST /inventory/scan：建 approval 不直接掃
# ---------------------------------------------------------------------------


def test_inventory_scan_creates_pending_approval_without_scanning(api_client):
    client, main_module = api_client
    resp = client.post(
        "/inventory/scan", json={"server": "server-a", "project_roots": ["/data/projects"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "inventory_scan"
    assert body["status"] == "pending"
    assert body["payload"]["server"] == "server-a"
    assert body["payload"]["project_roots"] == ["/data/projects"]

    # 還沒核准，project_candidates 表應該是空的，且完全沒有發生任何 SSH。
    assert client.get("/inventory/candidates").json() == []


def _make_server_config(name: str, project_roots=None, enabled: bool = True) -> ServerConfig:
    return ServerConfig(
        name=name,
        host="10.0.0.1",
        user="train",
        key="~/.ssh/id_rsa",
        project_roots=project_roots or [],
        enabled=enabled,
    )


def test_inventory_scan_server_all_with_no_configured_servers_returns_empty_approvals(api_client):
    """階段 8 第二批：`server="all"` 現在真的支援，但沒有任何機器設定
    `project_roots`（這裡是預設空的 `server_configs`）時，回傳空的
    approvals 列表，不是錯誤——沒有東西可以掃。"""
    client, _main = api_client
    resp = client.post("/inventory/scan", json={"server": "all"})
    assert resp.status_code == 200
    assert resp.json() == {"approvals": []}
    assert client.get("/approvals").json() == []


def test_inventory_scan_server_all_creates_one_approval_per_enabled_server(api_client):
    """`server="all"` 對每一台 enabled 機器各自建立一筆獨立的
    inventory_scan approval（用各自的 project_roots），不是單一 approval
    掃全部機器；disabled 的機器不會被掃。"""
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a", project_roots=["/data/a"]),
        "server-b": _make_server_config("server-b", project_roots=["/data/b"]),
        "server-c": _make_server_config("server-c", project_roots=["/data/c"], enabled=False),
    }
    resp = client.post("/inventory/scan", json={"server": "all"})
    assert resp.status_code == 200
    approvals = resp.json()["approvals"]
    assert len(approvals) == 2
    servers_scanned = {a["payload"]["server"] for a in approvals}
    assert servers_scanned == {"server-a", "server-b"}
    for a in approvals:
        assert a["kind"] == "inventory_scan"
        assert a["status"] == "pending"


def test_inventory_scan_project_roots_omitted_autofills_from_server_config(api_client):
    """`project_roots` 省略（`None`）時，從 `server_configs[server].
    project_roots` 自動代入。"""
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a", project_roots=["~/projects"]),
    }
    resp = client.post("/inventory/scan", json={"server": "server-a"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"]["project_roots"] == ["~/projects"]


def test_inventory_scan_explicit_project_roots_overrides_server_config(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a", project_roots=["~/projects"]),
    }
    resp = client.post(
        "/inventory/scan", json={"server": "server-a", "project_roots": ["/data/only-this"]}
    )
    assert resp.status_code == 200
    assert resp.json()["payload"]["project_roots"] == ["/data/only-this"]


def test_inventory_scan_unconfigured_server_without_project_roots_returns_400(api_client):
    """`server` 不在 `server_configs` 裡、且沒有明確帶 `project_roots`
    ——沒有任何資訊可以自動代入，回 400。"""
    client, _main = api_client
    resp = client.post("/inventory/scan", json={"server": "unknown-server"})
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


@pytest.mark.parametrize("bad_root", ["/etc", "/", "/tmp", "/home"])
def test_inventory_scan_forbidden_root_returns_400_and_creates_no_approval(api_client, bad_root):
    client, _main = api_client
    resp = client.post(
        "/inventory/scan", json={"server": "server-a", "project_roots": [bad_root]}
    )
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


def test_inventory_scan_empty_project_roots_returns_400(api_client):
    """明確帶入空列表（不是省略）仍然視為不合法請求。"""
    client, _main = api_client
    resp = client.post("/inventory/scan", json={"server": "server-a", "project_roots": []})
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


# ---------------------------------------------------------------------------
# 核准 inventory_scan 後 candidates 表才有資料
# ---------------------------------------------------------------------------


def test_approve_inventory_scan_populates_candidates(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["server"] == "server-a"
    assert cand["path"] == "/data/projects/proj1"
    assert cand["name_guess"] == "proj1"
    assert cand["status"] == "pending"
    assert cand["git_remote"] == "https://***@github.com/x/proj1.git"
    assert "readme_excerpt" in cand and cand["readme_excerpt"].startswith("# proj1")


def test_approve_inventory_scan_rechecks_forbidden_root_before_scanning(api_client):
    """雙重防線第二關：假設 approval 建立時是合法路徑，但這裡直接構造一筆
    payload 帶禁止路徑的 approval（模擬邊界情況），核准時仍應該被擋下，不
    會發生任何 SSH。"""
    client, main_module = api_client
    db = main_module.app_state.db
    approval_id = db.insert_approval(
        kind="inventory_scan", payload={"server": "server-a", "project_roots": ["/etc"]}
    )
    ssh = InventoryFakeSSH()
    main_module.app_state.ssh_run = ssh
    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "rejected"
    assert ssh.calls == []
    assert client.get("/inventory/candidates").json() == []


# ---------------------------------------------------------------------------
# GET /inventory/candidates 與 /inventory/candidates/{id}：CRUD 讀取
# ---------------------------------------------------------------------------


def test_get_inventory_candidate_not_found_returns_404(api_client):
    client, _main = api_client
    resp = client.get("/inventory/candidates/doesnotexist")
    assert resp.status_code == 404


def test_list_inventory_candidates_filters_by_server_status_and_query(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    assert len(client.get("/inventory/candidates?server=server-a").json()) == 1
    assert client.get("/inventory/candidates?server=server-b").json() == []
    assert len(client.get("/inventory/candidates?status=pending").json()) == 1
    assert client.get("/inventory/candidates?status=imported").json() == []
    assert len(client.get("/inventory/candidates?q=proj1").json()) == 1
    assert client.get("/inventory/candidates?q=nomatch").json() == []

    detail = client.get(f"/inventory/candidates/{cand_id}").json()
    assert detail["id"] == cand_id
    assert detail["markers"] == [".git", "README.md"]


# ---------------------------------------------------------------------------
# import-request / ignore-request：只建 approval，不直接改狀態
# ---------------------------------------------------------------------------


def test_import_request_creates_approval_without_writing_project(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    resp = client.post(f"/inventory/candidates/{cand_id}/import-request", json={"name": "proj1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "import_project"
    assert body["status"] == "pending"
    assert body["payload"]["candidate_id"] == cand_id
    assert body["payload"]["name"] == "proj1"

    # 還沒核准：projects 表是空的，candidate 狀態仍是 pending。
    assert client.get("/projects").json() == []
    assert client.get(f"/inventory/candidates/{cand_id}").json()["status"] == "pending"


def test_import_request_nonexistent_candidate_returns_404(api_client):
    client, _main = api_client
    resp = client.post("/inventory/candidates/doesnotexist/import-request", json={})
    assert resp.status_code == 404


def test_import_request_on_already_imported_candidate_returns_400(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]
    import_approval_id = client.post(
        f"/inventory/candidates/{cand_id}/import-request", json={"name": "proj1"}
    ).json()["id"]
    client.post(f"/approve/{import_approval_id}")

    resp = client.post(f"/inventory/candidates/{cand_id}/import-request", json={"name": "proj1"})
    assert resp.status_code == 400


def test_ignore_request_creates_approval_without_changing_status(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    resp = client.post(f"/inventory/candidates/{cand_id}/ignore-request")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "ignore_project_candidate"
    assert body["status"] == "pending"

    assert client.get(f"/inventory/candidates/{cand_id}").json()["status"] == "pending"


def test_ignore_request_nonexistent_candidate_returns_404(api_client):
    client, _main = api_client
    resp = client.post("/inventory/candidates/doesnotexist/ignore-request")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 核准 import_project 後才真的寫 projects/project_instances、candidate
# 狀態變 imported
# ---------------------------------------------------------------------------


def test_approve_import_project_writes_project_and_instance_and_marks_imported(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    import_approval_id = client.post(
        f"/inventory/candidates/{cand_id}/import-request",
        json={"name": "proj1", "dataset_mode": "none"},
    ).json()["id"]

    resp = client.post(f"/approve/{import_approval_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval"]["status"] == "approved"
    assert body["project"]["name"] == "proj1"
    assert body["project"]["dataset_mode"] == "none"

    projects = client.get("/projects").json()
    assert len(projects) == 1
    assert projects[0]["name"] == "proj1"

    instances = client.get("/projects/proj1/instances").json()
    assert len(instances) == 1
    assert instances[0]["server"] == "server-a"
    assert instances[0]["path"] == "/data/projects/proj1"

    cand_after = client.get(f"/inventory/candidates/{cand_id}").json()
    assert cand_after["status"] == "imported"


def test_project_instances_endpoint_empty_for_unknown_project(api_client):
    client, _main = api_client
    assert client.get("/projects/nope/instances").json() == []


# ---------------------------------------------------------------------------
# PLAN.md 2026-07-11 版 §14 切片 3：candidate 連結既有 Project（不新建）
# ---------------------------------------------------------------------------


def test_candidate_get_includes_link_suggestions_for_matching_remote(api_client):
    client, main_module = api_client
    # 先用 remote git@x:org/proj1.git 匯入一個既有 Project。
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]
    assert candidates[0]["git_remote"] == "https://***@github.com/x/proj1.git"
    import_approval_id = client.post(
        f"/inventory/candidates/{cand_id}/import-request", json={"name": "proj1"}
    ).json()["id"]
    client.post(f"/approve/{import_approval_id}")

    # 第二次掃描出同一台機器另一個路徑,同一個 remote → 應該被提示連結。
    _approval_id2, candidates2 = _scan_and_approve(
        client, main_module, project_roots=["/data/projects2"]
    )
    cand2_id = candidates2[0]["id"]

    detail = client.get(f"/inventory/candidates/{cand2_id}").json()
    assert detail["link_suggestions"]
    assert detail["link_suggestions"][0]["project_name"] == "proj1"


def test_import_request_with_link_to_project_creates_pending_link_payload(api_client):
    client, main_module = api_client
    client.post("/projects", json={"name": "existing-proj", "repo_or_path": "/repo/x"})
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    resp = client.post(
        f"/inventory/candidates/{cand_id}/import-request",
        json={"link_to_project": "existing-proj"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"]["link_to_project"] == "existing-proj"
    assert body["payload"]["link_to_project_id"]

    # 還沒核准:projects 表還是只有那一筆既有專案,不會多一筆。
    assert len(client.get("/projects").json()) == 1


def test_import_request_with_link_to_nonexistent_project_returns_400(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    resp = client.post(
        f"/inventory/candidates/{cand_id}/import-request",
        json={"link_to_project": "no-such-project"},
    )
    assert resp.status_code == 400


def test_approve_import_project_with_link_adds_instance_not_new_project(api_client):
    """核准 link_to_project 的 import_project:只多一個 project_instance,
    不新建 Project、不改既有 Project 的欄位。"""
    client, main_module = api_client
    client.post("/projects", json={"name": "existing-proj", "repo_or_path": "/repo/x"})
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    import_approval_id = client.post(
        f"/inventory/candidates/{cand_id}/import-request",
        json={"link_to_project": "existing-proj"},
    ).json()["id"]

    resp = client.post(f"/approve/{import_approval_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["name"] == "existing-proj"

    projects = client.get("/projects").json()
    assert len(projects) == 1  # 沒有多建 Project

    instances = client.get("/projects/existing-proj/instances").json()
    assert len(instances) == 1
    assert instances[0]["server"] == "server-a"
    assert instances[0]["project_id"] == projects[0]["id"]

    cand_after = client.get(f"/inventory/candidates/{cand_id}").json()
    assert cand_after["status"] == "imported"


def test_approve_import_project_link_target_deleted_before_approval_rejected(api_client):
    """INV-APPROVAL-3(核准當下重新驗證):建立請求後、核准前目標專案被
    刪除 → approve 應該拒絕,不建立孤兒 instance。"""
    client, main_module = api_client
    client.post("/projects", json={"name": "existing-proj", "repo_or_path": "/repo/x"})
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    import_approval_id = client.post(
        f"/inventory/candidates/{cand_id}/import-request",
        json={"link_to_project": "existing-proj"},
    ).json()["id"]

    main_module.app_state.db.delete_project("existing-proj")

    resp = client.post(f"/approve/{import_approval_id}")
    assert resp.status_code == 400
    # candidate 仍是 pending,沒有被誤標成 imported。
    assert client.get(f"/inventory/candidates/{cand_id}").json()["status"] == "pending"


def test_approve_ignore_project_candidate_marks_ignored(api_client):
    client, main_module = api_client
    _approval_id, candidates = _scan_and_approve(client, main_module)
    cand_id = candidates[0]["id"]

    ignore_approval_id = client.post(
        f"/inventory/candidates/{cand_id}/ignore-request"
    ).json()["id"]
    resp = client.post(f"/approve/{ignore_approval_id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"

    cand_after = client.get(f"/inventory/candidates/{cand_id}").json()
    assert cand_after["status"] == "ignored"


# ---------------------------------------------------------------------------
# agent 工具：scan_project_inventory / request_import_project_candidate /
# request_ignore_project_candidate 只建 approval，不直接改 DB
# ---------------------------------------------------------------------------


def _make_ctx(db) -> AgentContext:
    return AgentContext(db=db, server_states={}, config=AppConfig(servers=[]))


def test_agent_tool_scan_project_inventory_only_creates_approval(db):
    ctx = _make_ctx(db)
    result = asyncio.run(
        dispatch_tool(
            "scan_project_inventory",
            {"server": "server-a", "project_roots": ["/data/projects"]},
            ctx,
        )
    )
    assert "approval" in result
    assert result["approval"]["kind"] == "inventory_scan"
    assert result["approval"]["status"] == "pending"
    # DB 完全沒有 candidates（沒有發生任何掃描）。
    assert db.list_project_candidates() == []


def test_agent_tool_scan_project_inventory_server_all_with_no_server_configs_is_empty(db):
    """階段 8 第二批：`server="all"` 現在真的支援。沒有注入 `server_configs`
    （`ctx.server_configs` 為 `None`）時，沒有機器可掃，回傳空的 approvals
    列表，不是錯誤。"""
    ctx = _make_ctx(db)
    result = asyncio.run(dispatch_tool("scan_project_inventory", {"server": "all"}, ctx))
    assert result == {"approvals": []}
    assert db.list_approvals() == []


def test_agent_tool_scan_project_inventory_server_all_creates_approval_per_server(db):
    ctx = AgentContext(
        db=db,
        server_states={},
        config=AppConfig(servers=[]),
        server_configs={
            "server-a": ServerConfig(
                name="server-a", host="10.0.0.1", user="train", key="~/.ssh/id_rsa",
                project_roots=["/data/a"],
            ),
        },
    )
    result = asyncio.run(dispatch_tool("scan_project_inventory", {"server": "all"}, ctx))
    assert len(result["approvals"]) == 1
    assert result["approvals"][0]["payload"]["server"] == "server-a"


def test_agent_tool_scan_project_inventory_autofills_project_roots_from_server_configs(db):
    ctx = AgentContext(
        db=db,
        server_states={},
        config=AppConfig(servers=[]),
        server_configs={
            "server-a": ServerConfig(
                name="server-a", host="10.0.0.1", user="train", key="~/.ssh/id_rsa",
                project_roots=["~/projects"],
            ),
        },
    )
    result = asyncio.run(dispatch_tool("scan_project_inventory", {"server": "server-a"}, ctx))
    assert result["approval"]["payload"]["project_roots"] == ["~/projects"]


def test_agent_tool_scan_project_inventory_rejects_forbidden_root(db):
    ctx = _make_ctx(db)
    result = asyncio.run(
        dispatch_tool(
            "scan_project_inventory", {"server": "server-a", "project_roots": ["/etc"]}, ctx
        )
    )
    assert result.get("rejected") is True
    assert db.list_approvals() == []


def test_agent_tool_request_import_project_candidate_only_creates_approval(db):
    cand_id = db.upsert_project_candidate(
        server="server-a", path="/data/projects/proj1", name_guess="proj1", markers=[".git"]
    )
    ctx = _make_ctx(db)
    result = asyncio.run(
        dispatch_tool(
            "request_import_project_candidate",
            {"candidate_id": cand_id, "overrides": {"name": "proj1"}},
            ctx,
        )
    )
    assert "approval" in result
    assert result["approval"]["kind"] == "import_project"
    assert result["approval"]["status"] == "pending"
    # DB 沒有被直接改動：projects 表是空的，candidate 狀態仍是 pending。
    assert db.list_projects() == []
    assert db.get_project_candidate(cand_id).status == "pending"


def test_agent_tool_request_import_project_candidate_nonexistent_returns_error(db):
    ctx = _make_ctx(db)
    result = asyncio.run(
        dispatch_tool("request_import_project_candidate", {"candidate_id": "nope"}, ctx)
    )
    assert "error" in result


def test_agent_tool_request_ignore_project_candidate_only_creates_approval(db):
    cand_id = db.upsert_project_candidate(server="server-a", path="/data/projects/proj1")
    ctx = _make_ctx(db)
    result = asyncio.run(
        dispatch_tool("request_ignore_project_candidate", {"candidate_id": cand_id}, ctx)
    )
    assert "approval" in result
    assert result["approval"]["kind"] == "ignore_project_candidate"
    assert result["approval"]["status"] == "pending"
    assert db.get_project_candidate(cand_id).status == "pending"


def test_agent_tool_request_ignore_project_candidate_nonexistent_returns_error(db):
    ctx = _make_ctx(db)
    result = asyncio.run(
        dispatch_tool("request_ignore_project_candidate", {"candidate_id": "nope"}, ctx)
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# 唯讀 agent 工具：list_project_candidates / get_project_candidate /
# search_projects / get_project_profile / list_project_instances
# ---------------------------------------------------------------------------


def test_agent_tool_list_and_get_project_candidate(db):
    cand_id = db.upsert_project_candidate(
        server="server-a", path="/data/projects/proj1", name_guess="proj1", markers=[".git"]
    )
    ctx = _make_ctx(db)
    listed = asyncio.run(dispatch_tool("list_project_candidates", {}, ctx))
    assert len(listed) == 1
    assert listed[0]["id"] == cand_id

    detail = asyncio.run(
        dispatch_tool("get_project_candidate", {"candidate_id": cand_id}, ctx)
    )
    assert detail["path"] == "/data/projects/proj1"


def test_agent_tool_search_projects_and_profile_and_instances(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git", summary="demo project")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/projects/proj1")
    ctx = _make_ctx(db)

    found = asyncio.run(dispatch_tool("search_projects", {"query": "demo"}, ctx))
    assert len(found) == 1
    assert found[0]["name"] == "proj1"

    profile = asyncio.run(dispatch_tool("get_project_profile", {"project_name": "proj1"}, ctx))
    assert profile["project"]["name"] == "proj1"
    assert len(profile["instances"]) == 1

    instances = asyncio.run(dispatch_tool("list_project_instances", {"project_name": "proj1"}, ctx))
    assert len(instances) == 1
    assert instances[0]["server"] == "server-a"


# ---------------------------------------------------------------------------
# GET /projects/matrix（階段 15 Phase A，PLAN.md P.1.3）：唯讀、純 DB，不即時
# SSH。
# ---------------------------------------------------------------------------


def test_projects_matrix_empty_db_shape(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a"),
    }
    resp = client.get("/projects/matrix")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "servers": ["server-a"],
        "projects": [],
        "pending_candidates": {"server-a": 0},
    }


def test_projects_matrix_groups_instances_and_pending_candidates(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-b": _make_server_config("server-b"),
        "server-c": _make_server_config("server-c"),
        "pro6000": _make_server_config("pro6000"),
    }
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(
        project_name="proj1",
        server="server-c",
        path="/home/bgab141/Howard/proj1",
        git_branch="main",
        git_commit="abc123def456",
        dirty=True,
    )
    db.insert_project("proj2", "/local/proj2")
    db.upsert_project_candidate(server="server-c", path="/home/bgab141/Howard/other1")
    db.upsert_project_candidate(server="server-c", path="/home/bgab141/Howard/other2")
    db.upsert_project_candidate(server="server-b", path="/home/bgab141/Howard/other3")

    resp = client.get("/projects/matrix")
    assert resp.status_code == 200
    body = resp.json()
    assert body["servers"] == ["server-b", "server-c", "pro6000"]
    assert body["pending_candidates"] == {"server-b": 1, "server-c": 2, "pro6000": 0}

    by_name = {p["name"]: p for p in body["projects"]}
    assert set(by_name.keys()) == {"proj1", "proj2"}
    assert by_name["proj2"]["instances"] == {}
    proj1_instances = by_name["proj1"]["instances"]
    assert set(proj1_instances.keys()) == {"server-c"}
    assert proj1_instances["server-c"] == {
        "path": "/home/bgab141/Howard/proj1",
        "git_remote": None,
        "git_branch": "main",
        "git_commit": "abc123def456",
        "dirty": True,
        #: PLAN.md 2026-07-11 版 §14 切片 2/5:切片 2 reconcile 之前一律
        #: 'unknown'。
        "state": "unknown",
    }


def test_projects_matrix_includes_disabled_servers(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a"),
        "server-disabled": _make_server_config("server-disabled", enabled=False),
    }
    resp = client.get("/projects/matrix")
    assert resp.status_code == 200
    assert resp.json()["servers"] == ["server-a", "server-disabled"]
    assert resp.json()["pending_candidates"] == {"server-a": 0, "server-disabled": 0}


def test_projects_matrix_ignores_non_pending_candidates_in_count(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    db = main_module.app_state.db
    cand_id = db.upsert_project_candidate(server="server-a", path="/data/projects/proj1")
    db.update_project_candidate_status(cand_id, "ignored")
    resp = client.get("/projects/matrix")
    assert resp.json()["pending_candidates"] == {"server-a": 0}


# ---------------------------------------------------------------------------
# POST /inventory/candidates/ignore-nested-request（階段 15 Phase A，
# PLAN.md P.1.2 節，Fable 裁定第 2 點）：批次巢狀候選清理走核准流，一次
# 核准全部生效。
# ---------------------------------------------------------------------------


def test_ignore_nested_request_creates_pending_approval_with_nested_only(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    top_id = db.upsert_project_candidate(server="server-a", path="/data/proj", name_guess="proj")
    nested_id = db.upsert_project_candidate(
        server="server-a", path="/data/proj/data", name_guess="data"
    )
    db.upsert_project_candidate(server="server-a", path="/data/proj-sibling", name_guess="sibling")

    resp = client.post("/inventory/candidates/ignore-nested-request")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "ignore_nested_candidates"
    assert body["status"] == "pending"
    assert body["payload"]["candidate_ids"] == [nested_id]
    assert body["payload"]["items"] == [
        {"id": nested_id, "path": "/data/proj/data", "name_guess": "data"}
    ]
    # 沒有立刻改任何狀態——只建立 approval。
    assert db.get_project_candidate(top_id).status == "pending"
    assert db.get_project_candidate(nested_id).status == "pending"


def test_ignore_nested_request_sibling_prefix_not_misjudged_as_nested(api_client):
    """`/a/bc` 不是 `/a/b` 的子目錄——跟 prune_nested_candidates() 用同一套
    判定，這裡確認批次核准的候選集合計算也不會誤判。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.upsert_project_candidate(server="server-a", path="/a/b")
    db.upsert_project_candidate(server="server-a", path="/a/bc")

    resp = client.post("/inventory/candidates/ignore-nested-request")
    assert resp.status_code == 400


def test_ignore_nested_request_scopes_nesting_by_server(api_client):
    """不同機器路徑字串剛好相同不代表誰在誰底下——巢狀判定只在同一台
    server 內比較。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.upsert_project_candidate(server="server-b", path="/data/proj")
    db.upsert_project_candidate(server="server-c", path="/data/proj/data")

    resp = client.post("/inventory/candidates/ignore-nested-request")
    assert resp.status_code == 400


def test_ignore_nested_request_imported_top_level_still_counts_as_outer(api_client):
    """頂層候選已經匯入（imported）不代表底下的巢狀噪音就不用清——只有
    ignored 才不算數。"""
    client, main_module = api_client
    db = main_module.app_state.db
    top_id = db.upsert_project_candidate(server="server-a", path="/data/proj")
    db.update_project_candidate_status(top_id, "imported")
    nested_id = db.upsert_project_candidate(server="server-a", path="/data/proj/data")

    resp = client.post("/inventory/candidates/ignore-nested-request")
    assert resp.status_code == 200
    assert resp.json()["payload"]["candidate_ids"] == [nested_id]


def test_ignore_nested_request_ignored_outer_does_not_count(api_client):
    """外層候選已經是 ignored -> 不算「其他非 ignored 候選」，底下的候選
    不會被視為巢狀（跟一支獨立的候選一樣看待）。"""
    client, main_module = api_client
    db = main_module.app_state.db
    top_id = db.upsert_project_candidate(server="server-a", path="/data/proj")
    db.update_project_candidate_status(top_id, "ignored")
    db.upsert_project_candidate(server="server-a", path="/data/proj/data")

    resp = client.post("/inventory/candidates/ignore-nested-request")
    assert resp.status_code == 400


def test_ignore_nested_request_no_candidates_returns_400(api_client):
    client, _main = api_client
    resp = client.post("/inventory/candidates/ignore-nested-request")
    assert resp.status_code == 400
    assert db_has_no_approvals(_main)


def db_has_no_approvals(main_module) -> bool:
    return main_module.app_state.db.list_approvals() == []


def test_approve_ignore_nested_candidates_marks_pending_ignored_and_skips_others(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    top_id = db.upsert_project_candidate(server="server-a", path="/data/proj")
    nested_a = db.upsert_project_candidate(server="server-a", path="/data/proj/data")
    nested_b = db.upsert_project_candidate(server="server-a", path="/data/proj/scripts")

    approval_id = client.post("/inventory/candidates/ignore-nested-request").json()["id"]

    # 模擬競態：approval 建立之後、核准之前，使用者已經手動處理過其中一筆
    # （單獨匯入了 nested_b）——approve() 必須重查當下狀態，不能盲目套用
    # payload 建立當下算出的舊集合。
    db.update_project_candidate_status(nested_b, "imported")

    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval"]["status"] == "approved"
    assert body["ignored_count"] == 1
    assert body["ignored_ids"] == [nested_a]
    assert body["skipped_ids"] == [nested_b]
    assert body["approval"]["note"] == "已忽略 1 筆巢狀候選（跳過 1 筆）"

    assert db.get_project_candidate(top_id).status == "pending"
    assert db.get_project_candidate(nested_a).status == "ignored"
    assert db.get_project_candidate(nested_b).status == "imported"


def test_approve_ignore_nested_candidates_writes_audit(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.upsert_project_candidate(server="server-a", path="/data/proj")
    nested_id = db.upsert_project_candidate(server="server-a", path="/data/proj/data")

    approval_id = client.post("/inventory/candidates/ignore-nested-request").json()["id"]
    client.post(f"/approve/{approval_id}")

    events = client.get("/events?n=50").json()
    matching = [e for e in events if e["action"] == "candidates_ignore_nested"]
    assert len(matching) == 1
    assert matching[0]["params"]["approval_id"] == approval_id
    assert matching[0]["params"]["ignored_count"] == 1
    assert matching[0]["params"]["ignored_ids"] == [nested_id]
    assert matching[0]["params"]["skipped_ids"] == []


def test_ignore_nested_candidates_not_in_auto_approve_whitelist(db, audit_path):
    """跟 apply_patch/coding_task 一樣：這個 kind 天然不在
    `maybe_auto_approve()` 白名單（只認 enqueue/stop），就算使用者寫了
    `kind: any` 的規則也救不回，永遠要人工在網頁核准。"""
    from app.approvals import maybe_auto_approve, request_ignore_nested_candidates_approval

    db.upsert_project_candidate(server="server-a", path="/data/proj")
    db.upsert_project_candidate(server="server-a", path="/data/proj/data")
    approval = request_ignore_nested_candidates_approval(db, audit_path=audit_path)

    rules = [{"kind": "any"}]
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="api", rules=rules, audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


def test_index_page_has_ignore_nested_button(api_client):
    """階段 15 Phase A：候選分頁「一鍵清理巢狀候選」按鈕與對應的
    KIND_LABEL。

    DG-UI-UNIFICATION v1 U8: see
    `tests/test_git_init.py::test_index_page_renders_git_init_kind` -- the
    legacy inlined-SPA `resp.text` pin moves to the ported
    `workspace.html`/`workspace.js` source directly (the button's wording
    was reworded to「忽略巢狀候選」during the U4 port; same action, same
    endpoint, same kind)."""
    from pathlib import Path

    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="workspace-navigation"' in resp.text

    root = Path(__file__).parents[1] / "static"
    assert 'id="infra-ignore-nested-btn"' in (root / "workspace.html").read_text(encoding="utf-8")
    javascript = (root / "workspace.js").read_text(encoding="utf-8")
    assert "ignore-nested-request" in javascript
    assert "ignore_nested_candidates" in javascript


# ---------------------------------------------------------------------------
# POST /inventory/candidates/manual（P.1.5，PLAN.md，2026-07-10 追加，Fable
# 定案）：手動新增候選——不走核准流，直接建立 status=pending 候選；後續
# 匯入仍走既有 import_project 核准流。
# ---------------------------------------------------------------------------


class ManualCandidateFakeSSH:
    """只需要回應 `test -d ... && echo DIR_OK` 這一種指令；記錄所有呼叫過
    的指令方便斷言唯讀性質（只有 `test -d`，絕不寫檔）。"""

    def __init__(self, dir_ok: bool = True):
        self.calls: list[str] = []
        self.dir_ok = dir_ok

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if command.startswith("test -d"):
            return FakeCommandResult("DIR_OK\n" if self.dir_ok else "")
        return FakeCommandResult("")


def test_manual_candidate_rejects_unknown_server(api_client):
    client, main_module = api_client
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "unknown-server", "path": "/data/projects/foo"},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []


def test_manual_candidate_rejects_disabled_server(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a", enabled=False),
    }
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/foo"},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []


def test_manual_candidate_rejects_relative_path(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "relative/path"},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []


def test_manual_candidate_rejects_dotdot_path(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/../etc"},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []


@pytest.mark.parametrize("forbidden_path", ["/", "/etc"])
def test_manual_candidate_rejects_forbidden_root(api_client, forbidden_path):
    """跟 inventory_scan 的雙重防線慣例一致：手動輸入的路徑一樣不能是
    `app.inventory.is_forbidden_root()` 命中的禁止路徑——不會發生任何 SSH
    （在唯讀 SSH 確認之前就被擋下）。"""
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    ssh = ManualCandidateFakeSSH()
    main_module.app_state.ssh_run = ssh
    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": forbidden_path},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []
    assert ssh.calls == []


def test_manual_candidate_rejects_when_ssh_check_fails(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    ssh = ManualCandidateFakeSSH(dir_ok=False)
    main_module.app_state.ssh_run = ssh
    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/nope"},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []
    assert any(c.startswith("test -d") for c in ssh.calls)


def test_manual_candidate_duplicate_returns_409_with_existing_id_and_status(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    first = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet"},
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    second = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet"},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["id"] == first_id
    assert detail["status"] == "pending"


def test_manual_candidate_duplicate_regardless_of_status(api_client):
    """已經 imported/ignored 的候選一樣算重複（不是只擋 pending）。"""
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    db = main_module.app_state.db

    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet"},
    )
    cand_id = resp.json()["id"]
    db.update_project_candidate_status(cand_id, "ignored")

    resp2 = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet"},
    )
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["status"] == "ignored"


def test_manual_candidate_success_default_name_is_basename(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet/"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["server"] == "server-a"
    assert body["path"] == "/data/projects/controlnet"
    assert body["name_guess"] == "controlnet"
    assert body["kind"] == "project"
    assert body["markers"] == ["manual"]
    assert body["status"] == "pending"


def test_manual_candidate_explicit_name_overrides_basename(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet", "name": "my-controlnet"},
    )
    assert resp.status_code == 200
    assert resp.json()["name_guess"] == "my-controlnet"


def test_manual_candidate_invalid_name_charset_returns_400(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet", "name": "bad name!"},
    )
    assert resp.status_code == 400
    assert client.get("/inventory/candidates").json() == []


def test_manual_candidate_writes_audit(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet"},
    )
    cand_id = resp.json()["id"]

    events = client.get("/events?n=50").json()
    matching = [e for e in events if e["action"] == "candidate_manual_added"]
    assert len(matching) == 1
    assert matching[0]["params"]["server"] == "server-a"
    assert matching[0]["params"]["path"] == "/data/projects/controlnet"
    assert matching[0]["params"]["name_guess"] == "controlnet"
    assert matching[0]["params"]["candidate_id"] == cand_id


def test_manual_candidate_then_import_request_and_approve_flow_works(api_client):
    """手動候選建立後，走既有 import 核准流程可用（跟掃描產生的候選走同一
    套流程，沒有任何特權）。"""
    client, main_module = api_client
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    cand_resp = client.post(
        "/inventory/candidates/manual",
        json={"server": "server-a", "path": "/data/projects/controlnet"},
    )
    cand_id = cand_resp.json()["id"]

    import_resp = client.post(
        f"/inventory/candidates/{cand_id}/import-request",
        json={"name": "controlnet", "dataset_mode": "none"},
    )
    assert import_resp.status_code == 200
    approval_id = import_resp.json()["id"]

    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200
    assert approve_resp.json()["approval"]["status"] == "approved"

    projects = client.get("/projects").json()
    assert len(projects) == 1
    assert projects[0]["name"] == "controlnet"

    cand_after = client.get(f"/inventory/candidates/{cand_id}").json()
    assert cand_after["status"] == "imported"


def test_index_page_has_manual_candidate_form(api_client):
    """前端 smoke：候選分頁有手動新增候選的表單，指向
    POST /inventory/candidates/manual。

    DG-UI-UNIFICATION v1 U8: see
    `tests/test_git_init.py::test_index_page_renders_git_init_kind` -- the
    legacy inlined-SPA `resp.text` pin moves to the ported
    `workspace.html`/`workspace.js` source directly. The v2 wrapper
    disambiguates by method instead of a `/manual` path suffix (`POST
    /api/v2/inventory/candidates` -- see `dispatch_center/api/routers/
    infrastructure_v2.py`), same endpoint that the legacy `/inventory/
    candidates/manual` POST ultimately reused server-side."""
    from pathlib import Path

    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True
    resp = client.get("/")
    assert resp.status_code == 200
    assert "手動新增候選" in resp.text

    javascript = (Path(__file__).parents[1] / "static" / "workspace.js").read_text(
        encoding="utf-8"
    )
    assert 'productMutation("/api/v2/inventory/candidates", { server, path, name });' in javascript
