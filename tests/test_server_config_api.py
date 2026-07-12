"""`/server-config/*` 端到端 API 測試（PLAN.md I.7，階段 8 第二批）。

涵蓋 I.11 測試清單：
- 全端點；AUTH_TOKEN 保護（無 token 401）
- test-ssh 只跑六類固定指令（mock ssh_run，斷言呼叫的指令內容）
- add-request 只建 approval，不直接改 servers.yaml
- 核准 server_add 後才 atomic write
- update-request 只建 approval
- disable-request 對有 running job 的 server 核准時會失敗（approval 變
  rejected，servers.yaml 不變）
- `GET /server-config` 不回傳 key 內容

**重要**：`tests/conftest.py` 的 `api_client` fixture 已經把
`SERVERS_YAML_PATH` 指向 `tmp_path/servers.yaml`（同一個測試函式共用同一個
`tmp_path`），所以這裡任何核准動作的 atomic write 都落在 tmp_path 底下，
不會碰到專案根目錄真正的 servers.yaml。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.config import ServerConfig
from app.server_config import load_servers_config, write_servers_yaml_atomically


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


def _make_key_file(tmp_path, name="id_test") -> str:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / name
    key_path.write_text("fake-private-key-content\n")
    os.chmod(key_path, 0o600)
    return str(key_path)


def _valid_server_payload(tmp_path, **overrides) -> dict:
    payload = {
        "name": "server-x",
        "host": "10.0.0.5",
        "user": "train",
        "key": _make_key_file(tmp_path),
        "port": 22,
        "gpu": True,
        "idle_gpu_util": 15.0,
        "idle_load": 2.0,
        "tags": ["gpu"],
        "project_roots": ["~/projects"],
        "dataset_roots": [],
        "enabled": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """同 tests/test_auth.py 的 auth_client，但額外隔離 SERVERS_YAML_PATH
    （這個測試檔會核准 server_add/update/disable/delete，必須避免寫到真實
    servers.yaml）。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


# ---------------------------------------------------------------------------
# AUTH_TOKEN 保護
# ---------------------------------------------------------------------------


def test_server_config_endpoints_require_auth_token(auth_client):
    client, _main = auth_client
    assert client.get("/server-config").status_code == 401
    assert client.get("/server-config/server-x").status_code == 401
    assert client.post("/server-config/add-request", json={}).status_code == 401


def test_server_config_endpoints_work_with_correct_token(auth_client):
    client, _main = auth_client
    resp = client.get("/server-config", headers={"X-Auth-Token": "secret-token"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /server-config, GET /server-config/{name}：不洩漏 key 內容
# ---------------------------------------------------------------------------


def test_list_server_config_empty_initially(api_client):
    client, _main = api_client
    assert client.get("/server-config").json() == []


def test_get_server_config_not_found_returns_404(api_client):
    client, _main = api_client
    resp = client.get("/server-config/does-not-exist")
    assert resp.status_code == 404


def test_get_server_config_does_not_leak_key_content(api_client, tmp_path):
    client, main_module = api_client
    key_path = _make_key_file(tmp_path)
    main_module.app_state.server_configs = {
        "server-x": ServerConfig(name="server-x", host="10.0.0.5", user="train", key=key_path),
    }
    resp = client.get("/server-config/server-x")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == key_path
    assert "fake-private-key-content" not in resp.text

    list_resp = client.get("/server-config")
    assert "fake-private-key-content" not in list_resp.text


# ---------------------------------------------------------------------------
# POST /server-config/test-ssh：唯讀，只跑六類固定指令
# ---------------------------------------------------------------------------


def test_test_ssh_endpoint_runs_only_fixed_commands(api_client, tmp_path):
    client, main_module = api_client
    calls: list[str] = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        calls.append(command)
        if command == "hostname":
            return FakeCommandResult("host-x\n")
        if "test -d" in command:
            return FakeCommandResult("exists\n")
        return FakeCommandResult("")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    payload = _valid_server_payload(tmp_path, project_roots=["~/projects"], dataset_roots=["/data"])
    resp = client.post("/server-config/test-ssh", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["results"]["hostname"] == "host-x"
    assert len(calls) == 6  # hostname/whoami/tmux/gpu + 1 project_root + 1 dataset_root

    # test-ssh 不建 approval、不建 agent_jobs、不改 servers.yaml
    assert client.get("/approvals").json() == []
    assert not os.path.exists(main_module.app_state.config.servers_yaml_path)


def test_test_ssh_endpoint_writes_audit_event(api_client, tmp_path):
    client, main_module = api_client

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        return FakeCommandResult("ok\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    client.post("/server-config/test-ssh", json=_valid_server_payload(tmp_path))
    events = client.get("/events").json()
    assert any(e["action"] == "server_test_ssh" for e in events)


# ---------------------------------------------------------------------------
# bug 修正的釘住測試：test-ssh 必須用 payload 的設定連線，不是按名字查
# app_state.server_configs（原本的 bug：payload 的 host/port/user/key 全部
# 被無視，新機器 KeyError、已改但未核准的設定測到舊值）。
# ---------------------------------------------------------------------------


def test_test_ssh_uses_payload_config_even_when_name_unknown(api_client, tmp_path):
    """payload 的 name 不存在於 server_configs 裡（「還沒加入 servers.yaml
    的機器」）也能測試成功，且收到的 cfg 是 payload 組出來的那個。"""
    client, main_module = api_client
    assert "brand-new-server" not in main_module.app_state.server_configs

    seen_configs = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        seen_configs.append(server_cfg)
        return FakeCommandResult("ok\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    payload = _valid_server_payload(
        tmp_path, name="brand-new-server", host="10.9.9.9", port=32221
    )
    resp = client.post("/server-config/test-ssh", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert seen_configs, "應該真的呼叫了 ssh_pool.run"
    assert all(cfg.host == "10.9.9.9" and cfg.port == 32221 for cfg in seen_configs)


def test_test_ssh_uses_payload_config_not_stored_config_when_name_exists(api_client, tmp_path):
    """payload 的 name 存在於 server_configs，但 host/port 與已儲存設定
    不同（例如使用者在表單改了還沒核准）——必須用 payload 的值，不是
    儲存的舊值。"""
    client, main_module = api_client
    from app.config import ServerConfig

    main_module.app_state.server_configs["server-old"] = ServerConfig(
        name="server-old", host="1.1.1.1", user="old-user", key="/nonexistent", port=22
    )

    seen_configs = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        seen_configs.append(server_cfg)
        return FakeCommandResult("ok\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    payload = _valid_server_payload(
        tmp_path, name="server-old", host="10.0.0.5", port=32221
    )
    resp = client.post("/server-config/test-ssh", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert all(cfg.host == "10.0.0.5" and cfg.port == 32221 for cfg in seen_configs)


def test_test_ssh_invalid_config_returns_ok_false_without_any_ssh_call(api_client, tmp_path):
    """驗證失敗（host=0.0.0.0）→ 不做任何 SSH 呼叫，回 ok=False + errors，
    HTTP 200（不是 400，這是唯讀測試不是建立請求）。"""
    client, main_module = api_client
    calls = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        calls.append(command)
        return FakeCommandResult("ok\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    payload = _valid_server_payload(tmp_path, host="0.0.0.0")
    resp = client.post("/server-config/test-ssh", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["results"] == {}
    assert body["errors"]
    assert calls == []  # 完全沒有發生任何 SSH 呼叫

    # 也不寫稽核（沒有實際測試發生）
    events = client.get("/events").json()
    assert not any(e["action"] == "server_test_ssh" for e in events)


# ---------------------------------------------------------------------------
# add-request：只建 approval，不直接改 servers.yaml
# ---------------------------------------------------------------------------


def test_add_request_creates_approval_without_writing_yaml(api_client, tmp_path):
    client, main_module = api_client
    payload = _valid_server_payload(tmp_path)
    resp = client.post("/server-config/add-request", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_add"
    assert body["status"] == "pending"
    assert body["payload"]["name"] == "server-x"

    # 還沒核准：yaml 檔案完全不存在（連 backup 都不會發生）
    assert not os.path.exists(main_module.app_state.config.servers_yaml_path)


def test_add_request_invalid_config_returns_400_and_no_approval(api_client, tmp_path):
    client, _main = api_client
    payload = _valid_server_payload(tmp_path, name="bad name!")
    resp = client.post("/server-config/add-request", json=payload)
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


def test_add_request_root_user_rejected_without_allow_root_ssh(api_client, tmp_path):
    client, _main = api_client
    payload = _valid_server_payload(tmp_path, user="root")
    resp = client.post("/server-config/add-request", json=payload)
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


# ---------------------------------------------------------------------------
# 核准 server_add 後才 atomic write + reload
# ---------------------------------------------------------------------------


def test_approve_server_add_writes_yaml_and_reloads_in_memory(api_client, tmp_path):
    client, main_module = api_client
    payload = _valid_server_payload(tmp_path)
    approval_id = client.post("/server-config/add-request", json=payload).json()["id"]

    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"

    # servers.yaml 現在存在，內容含 server-x
    yaml_path = main_module.app_state.config.servers_yaml_path
    assert os.path.exists(yaml_path)
    on_disk = load_servers_config(yaml_path)
    assert any(s["name"] == "server-x" for s in on_disk["servers"])

    # in-memory 立即生效，不需要重啟服務
    assert "server-x" in main_module.app_state.server_configs
    assert main_module.app_state.server_configs["server-x"].host == "10.0.0.5"
    assert "server-x" in main_module.app_state.server_states

    # GET /server-config 也看得到新機器
    listed = client.get("/server-config").json()
    assert any(s["name"] == "server-x" for s in listed)


def test_approve_server_add_duplicate_name_fails(api_client, tmp_path):
    client, main_module = api_client
    write_servers_yaml_atomically(
        main_module.app_state.config.servers_yaml_path,
        {"servers": [_valid_server_payload(tmp_path, name="server-x")]},
    )
    payload = _valid_server_payload(tmp_path, name="server-x")
    approval_id = client.post("/server-config/add-request", json=payload).json()["id"]
    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# update-request：只建 approval
# ---------------------------------------------------------------------------


def _seed_server(main_module, tmp_path, **overrides) -> None:
    payload = _valid_server_payload(tmp_path, **overrides)
    write_servers_yaml_atomically(
        main_module.app_state.config.servers_yaml_path, {"servers": [payload]}
    )
    main_module.app_state.server_configs = {
        payload["name"]: ServerConfig(
            name=payload["name"],
            host=payload["host"],
            user=payload["user"],
            key=payload["key"],
            gpu=payload["gpu"],
            tags=list(payload["tags"]),
            project_roots=list(payload["project_roots"]),
            dataset_roots=list(payload["dataset_roots"]),
            enabled=payload["enabled"],
        )
    }
    main_module.app_state.server_states.setdefault(payload["name"], None)


def test_update_request_creates_approval_without_writing_yaml(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")

    resp = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"host": "10.0.0.99"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_update"
    assert body["payload"] == {"name": "server-x", "updates": {"host": "10.0.0.99"}}

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["host"] == "10.0.0.5"  # 還沒被改


def test_update_request_rename_rejected(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    resp = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"name": "server-y"}},
    )
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


def test_update_request_unknown_server_returns_404(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    resp = client.post(
        "/server-config/update-request",
        json={"name": "does-not-exist", "updates": {"host": "10.0.0.1"}},
    )
    assert resp.status_code == 404


def test_approve_server_update_merges_and_writes(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")

    approval_id = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"host": "10.0.0.99", "tags": ["gpu", "fast"]}},
    ).json()["id"]

    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    updated = next(s for s in on_disk["servers"] if s["name"] == "server-x")
    assert updated["host"] == "10.0.0.99"
    assert updated["tags"] == ["gpu", "fast"]

    assert main_module.app_state.server_configs["server-x"].host == "10.0.0.99"


# ---------------------------------------------------------------------------
# disable-request：running job 時核准會失敗（rejected，yaml 不變）
# ---------------------------------------------------------------------------


def test_disable_request_creates_approval_without_touching_yaml(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")

    resp = client.post("/server-config/disable-request", json={"name": "server-x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_disable"
    assert body["payload"] == {"name": "server-x"}

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is True


def test_disable_request_unknown_server_returns_404(api_client):
    client, _main = api_client
    resp = client.post("/server-config/disable-request", json={"name": "does-not-exist"})
    assert resp.status_code == 404


def test_approve_disable_with_running_job_is_rejected_and_yaml_unchanged(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    main_module.app_state.db.insert_job(command="sleep 600")
    job = main_module.app_state.db.list_jobs()[0]
    main_module.app_state.db.update_job(job.id, status="running", server="server-x")

    approval_id = client.post(
        "/server-config/disable-request", json={"name": "server-x"}
    ).json()["id"]
    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval"]["status"] == "rejected"
    assert "執行中任務" in (body["approval"]["note"] or "")

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is True
    # in-memory 也沒被改
    assert main_module.app_state.server_configs["server-x"].enabled is True


def test_approve_disable_without_running_job_sets_enabled_false(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")

    approval_id = client.post(
        "/server-config/disable-request", json={"name": "server-x"}
    ).json()["id"]
    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is False
    assert main_module.app_state.server_configs["server-x"].enabled is False


# ---------------------------------------------------------------------------
# delete-request：第一版只設 enabled=false，不會真的移除
# ---------------------------------------------------------------------------


def test_delete_request_creates_approval(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    resp = client.post("/server-config/delete-request", json={"name": "server-x"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "server_delete"


def test_approve_delete_with_running_job_is_rejected(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    main_module.app_state.db.insert_job(command="sleep 600")
    job = main_module.app_state.db.list_jobs()[0]
    main_module.app_state.db.update_job(job.id, status="running", server="server-x")

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    resp = client.post(f"/approve/{approval_id}")
    assert resp.json()["approval"]["status"] == "rejected"

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is True


def test_approve_delete_without_running_job_sets_enabled_false_not_removed(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"
    assert resp.json()["approval"]["note"] == "第一版以停用取代刪除"

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    # 第一版沒有真的刪除，仍然有這一筆，只是 enabled=false。
    assert len(on_disk["servers"]) == 1
    assert on_disk["servers"][0]["name"] == "server-x"
    assert on_disk["servers"][0]["enabled"] is False


# ---------------------------------------------------------------------------
# POST /server-config/reload
# ---------------------------------------------------------------------------


def test_reload_endpoint_picks_up_manual_yaml_edit(api_client, tmp_path):
    client, main_module = api_client
    yaml_path = main_module.app_state.config.servers_yaml_path
    write_servers_yaml_atomically(
        yaml_path, {"servers": [_valid_server_payload(tmp_path, name="server-manual")]}
    )
    assert "server-manual" not in main_module.app_state.server_configs

    resp = client.post("/server-config/reload")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "server-manual" in main_module.app_state.server_configs
    assert "server-manual" in main_module.app_state.server_states


# ---------------------------------------------------------------------------
# 階段 13（PLAN.md N.1，批次 3a）：reload 成功後重跑 apply_codex_config_rules
# ——「啟動時硬失敗、運行中軟警告」的取捨：ValueError 只 log error、不中斷
# 這個請求（回應仍是 {"ok": True}），warnings 照常 log。
# ---------------------------------------------------------------------------


def test_reload_endpoint_reruns_codex_rules_downgrades_concurrency_with_warning(
    api_client, tmp_path, caplog
):
    client, main_module = api_client
    yaml_path = main_module.app_state.config.servers_yaml_path
    write_servers_yaml_atomically(
        yaml_path, {"servers": [_valid_server_payload(tmp_path, name="server-a")]}
    )
    main_module.app_state.config.codex_runner_server = "server-a"
    main_module.app_state.config.codex_auth_mode = "chatgpt"
    main_module.app_state.config.codex_max_concurrency = 3

    with caplog.at_level("WARNING"):
        resp = client.post("/server-config/reload")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert main_module.app_state.config.codex_max_concurrency == 1
    assert any("降為 1" in r.message for r in caplog.records)


def test_reload_endpoint_invalid_codex_runner_after_reload_logs_error_not_500(
    api_client, tmp_path, caplog
):
    """CODEX_RUNNER_SERVER 指向的機器在這次 reload 之後消失（servers.yaml
    被人工改成不再包含它）：reload 本身（讀檔＋替換 in-memory dict）仍然
    成功，不因為 Codex 設定現在不合法而讓這個請求失敗或讓服務跟著炸掉——
    只 log error，運行中軟警告。"""
    client, main_module = api_client
    yaml_path = main_module.app_state.config.servers_yaml_path
    write_servers_yaml_atomically(
        yaml_path, {"servers": [_valid_server_payload(tmp_path, name="server-manual")]}
    )
    main_module.app_state.config.codex_runner_server = "server-does-not-exist"

    with caplog.at_level("ERROR"):
        resp = client.post("/server-config/reload")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "server-manual" in main_module.app_state.server_configs
    assert any(
        "CODEX_RUNNER_SERVER" in r.message or "server-does-not-exist" in r.message
        for r in caplog.records
    )
