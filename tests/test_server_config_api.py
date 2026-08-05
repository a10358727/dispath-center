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

from app.audit import read_audit
from app.config import ServerConfig
from app.identity import ActorType, generate_session_token
from app.server_config import load_servers_config, write_servers_yaml_atomically
from app.server_attempt_preflight import ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND


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
    assert (
        client.post("/server-config/server-x/attempt-preflight").status_code
        == 401
    )


def test_server_config_endpoints_work_with_correct_token(auth_client):
    client, _main = auth_client
    resp = client.get("/server-config", headers={"X-Auth-Token": "secret-token"})
    assert resp.status_code == 200


def test_recovery_journal_resolution_uses_request_context(api_client):
    """The operator route must use middleware's authenticated request context.

    A missing mutation is enough to exercise the seam: it must reach the DB and
    return the domain validation error, not fail with an undefined helper.
    """

    client, _main = api_client
    response = client.post(
        "/server-config/journal/missing-mutation/resolve",
        json={
            "observed_yaml_sha256": "0" * 64,
            "resolution": "rolled_back",
        },
    )

    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


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
# D-5：attempt-driven SSH 的 agent_jobs filesystem 必須由固定唯讀命令觀測，
# 並綁到 exact active approved revision。NULL/unknown/non-local 都 fail closed。
# ---------------------------------------------------------------------------


def _approve_server_for_attempt_preflight(client, tmp_path, **overrides):
    payload = _valid_server_payload(tmp_path, **overrides)
    approval_id = client.post("/server-config/add-request", json=payload).json()["id"]
    response = client.post(f"/approve/{approval_id}")
    assert response.status_code == 200
    return payload


def test_attempt_preflight_records_exact_local_filesystem_revision(api_client, tmp_path):
    client, main_module = api_client
    payload = _approve_server_for_attempt_preflight(client, tmp_path)
    calls = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        calls.append((server_cfg.name, command, timeout))
        return FakeCommandResult("DISPATCH_FS_TYPE=ext2/ext3/ext4\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    before = client.get("/server-config/server-x").json()
    assert before["attempt_backend_preflight"] is None
    assert before["attempt_backend_eligible"] is False
    assert before["attempt_backend_preflight_available"] is True

    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "eligible"
    assert body["filesystem_type"] == "ext2/ext3/ext4"
    assert calls == [("server-x", ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND, 15)]
    after = client.get("/server-config/server-x").json()
    assert after["attempt_backend_preflight"] == "eligible"
    assert after["attempt_backend_eligible"] is True
    assert after["attempt_backend_preflight_observed_at"] is not None
    events = client.get("/events").json()
    assert any(
        event["action"] == "server_attempt_backend_preflight"
        and event["params"]["status"] == "eligible"
        for event in events
    )
    durable = main_module.app_state.db.list_durable_audit_events(limit=100)
    recorded = next(
        event
        for event in durable
        if event["action"] == "server_attempt_backend_preflight_recorded"
    )
    assert recorded["resource_type"] == "server_config_revision"
    assert recorded["resource_id"] == before["server_config_revision_id"]
    assert recorded["result"] == "eligible"
    assert recorded["params"] == {
        "contract_version": "attempt-fs-preflight-v1",
        "filesystem_type": "ext2/ext3/ext4",
        "reason_code": "local_filesystem_observed",
        "server_config_revision_id": before["server_config_revision_id"],
        "server_name": "server-x",
        "status": "eligible",
    }
    assert "stdout" not in str(recorded)
    assert payload["key"] not in str(recorded)


def test_attempt_preflight_rolls_back_revision_when_durable_append_fails(
    api_client, tmp_path, monkeypatch
):
    client, main_module = api_client
    _approve_server_for_attempt_preflight(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run

    def fail_append(*_args, **_kwargs):
        raise ValueError("injected durable audit failure")

    monkeypatch.setattr(
        main_module.app_state.db,
        "append_durable_audit_event_in_transaction",
        fail_append,
    )

    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 409
    assert response.json()["detail"] == "injected durable audit failure"
    revision = main_module.app_state.db.get_active_server_config_revision("server-x")
    assert revision["attempt_backend_preflight"] is None
    assert not any(
        event["action"] == "server_attempt_backend_preflight_recorded"
        for event in main_module.app_state.db.list_durable_audit_events(limit=100)
    )
    assert not any(
        record["action"] == "server_attempt_backend_preflight"
        for record in read_audit(main_module.app_state.config.audit_path)
    )


def test_attempt_preflight_durable_event_uses_authenticated_actor(
    auth_client, tmp_path
):
    client, main_module = auth_client
    actor = main_module.app_state.db.insert_actor(
        actor_type=ActorType.HUMAN,
        display_name="Preflight Reviewer",
        email="preflight-reviewer@example.invalid",
    )
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)

    payload = _approve_server_for_attempt_preflight(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 200
    event = next(
        event
        for event in main_module.app_state.db.list_durable_audit_events(limit=100)
        if event["action"] == "server_attempt_backend_preflight_recorded"
    )
    assert event["actor"] == {
        "id": actor.id,
        "kind": "human",
        "authentication": "session",
    }
    assert payload["key"] not in str(event)


@pytest.mark.parametrize("filesystem_type", ["nfs", "nfs4", "cifs", "fuse.sshfs"])
def test_attempt_preflight_records_non_local_as_ineligible(
    api_client, tmp_path, filesystem_type
):
    client, main_module = api_client
    _approve_server_for_attempt_preflight(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        return FakeCommandResult(f"DISPATCH_FS_TYPE={filesystem_type}\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 200
    assert response.json()["status"] == "ineligible_non_local_fs"
    assert response.json()["ok"] is False
    assert main_module.app_state._attempt_revision_ids() == {}


def test_attempt_preflight_transport_failure_records_unknown_without_error_text(
    api_client, tmp_path
):
    client, main_module = api_client
    _approve_server_for_attempt_preflight(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        raise RuntimeError("SECRET-REMOTE-DETAIL")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
    assert "SECRET-REMOTE-DETAIL" not in response.text
    revision = main_module.app_state.db.get_active_server_config_revision("server-x")
    assert revision["attempt_backend_preflight"] == "unknown"
    assert main_module.app_state._attempt_revision_ids() == {}


def test_attempt_preflight_requires_an_active_approved_revision(api_client, tmp_path):
    client, main_module = api_client
    payload = _valid_server_payload(tmp_path)
    main_module.app_state.server_configs["server-x"] = ServerConfig(
        name=payload["name"],
        host=payload["host"],
        user=payload["user"],
        key=payload["key"],
        port=payload["port"],
    )
    called = False

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        nonlocal called
        called = True
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 409
    assert called is False
    server = client.get("/server-config/server-x").json()
    assert server["attempt_backend_preflight_available"] is False


def test_attempt_preflight_refuses_target_or_key_drift_during_probe(api_client, tmp_path):
    client, main_module = api_client
    payload = _approve_server_for_attempt_preflight(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        with open(payload["key"], "a", encoding="utf-8") as key_file:
            key_file.write("rotated-during-preflight\n")
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    response = client.post("/server-config/server-x/attempt-preflight")

    assert response.status_code == 409
    revision = main_module.app_state.db.get_active_server_config_revision("server-x")
    assert revision["attempt_backend_preflight"] is None


def test_new_server_revision_resets_prior_filesystem_evidence(api_client, tmp_path):
    client, main_module = api_client
    _approve_server_for_attempt_preflight(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    assert client.post("/server-config/server-x/attempt-preflight").json()["ok"]
    old_revision = main_module.app_state.db.get_active_server_config_revision(
        "server-x"
    )

    update = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"note": "new revision"}},
    )
    assert update.status_code == 200
    assert client.post(f"/approve/{update.json()['id']}").status_code == 200

    new_revision = main_module.app_state.db.get_active_server_config_revision(
        "server-x"
    )
    assert new_revision["id"] != old_revision["id"]
    assert new_revision["attempt_backend_preflight"] is None
    assert client.get("/server-config/server-x").json()["attempt_backend_eligible"] is False


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
    assert body["payload_contract_version"] == "server-config-v1"
    assert body["payload"]["operation"] == "add"
    assert body["payload"]["server_name"] == "server-x"
    assert body["payload"]["yaml_after_utf8_b64"]
    assert body["review_payload"]["name"] == "server-x"

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

    revision = main_module.app_state.db.get_active_server_config_revision(
        "server-x"
    )
    assert revision is not None
    assert revision["assignment_eligibility"] == "approved"
    journal = main_module.app_state.db.list_server_config_mutations()
    assert len(journal) == 1
    assert journal[0]["state"] == "activated"


def test_legacy_server_jsonl_summary_can_be_retired(api_client, tmp_path):
    """The compatibility line is reversible; durable evidence remains."""
    _client, main_module = api_client
    payload = _valid_server_payload(tmp_path, name="legacy-server")
    approval_id = main_module.app_state.db.insert_approval(
        kind="server_add", payload=payload
    )
    main_module.app_state.config.legacy_audit_jsonl_enabled = False

    result = _approve(main_module, approval_id)

    assert result["approval"].status == "approved"
    records = read_audit(main_module.app_state.config.audit_path)
    assert not any(record["action"] == "server_add" for record in records)
    actions = {
        event["action"]
        for event in main_module.app_state.db.list_durable_audit_events(limit=100)
    }
    assert {
        "server_legacy_mutation_intent",
        "server_legacy_mutation_applied",
        "approval_decided",
    } <= actions
    assert main_module.app_state.db.get_active_server_config_revision("legacy-server") is None


def test_legacy_server_jsonl_summary_defaults_to_compatibility(api_client, tmp_path):
    _client, main_module = api_client
    payload = _valid_server_payload(tmp_path, name="legacy-default")
    approval_id = main_module.app_state.db.insert_approval(
        kind="server_add", payload=payload
    )

    result = _approve(main_module, approval_id)

    assert result["approval"].status == "approved"
    records = read_audit(main_module.app_state.config.audit_path)
    assert any(record["action"] == "server_add" for record in records)


def test_server_add_rejects_yaml_drift_after_request(api_client, tmp_path):
    client, main_module = api_client
    payload = _valid_server_payload(tmp_path, name="server-x")
    approval_id = client.post(
        "/server-config/add-request", json=payload
    ).json()["id"]

    unrelated = _valid_server_payload(tmp_path, name="other-server")
    write_servers_yaml_atomically(
        main_module.app_state.config.servers_yaml_path,
        {"servers": [unrelated]},
    )

    response = client.post(f"/approve/{approval_id}")

    assert response.status_code == 400
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert [server["name"] for server in on_disk["servers"]] == [
        "other-server"
    ]
    approval = main_module.app_state.db.get_approval(approval_id)
    assert approval.status == "pending"
    assert approval.materialization_started_at is None
    assert main_module.app_state.db.list_server_config_mutations() == []


def test_server_add_duplicate_name_is_rejected_before_approval(api_client, tmp_path):
    client, main_module = api_client
    write_servers_yaml_atomically(
        main_module.app_state.config.servers_yaml_path,
        {"servers": [_valid_server_payload(tmp_path, name="server-x")]},
    )
    payload = _valid_server_payload(tmp_path, name="server-x")
    resp = client.post("/server-config/add-request", json=payload)
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


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
    assert body["payload_contract_version"] == "server-config-v1"
    assert body["payload"]["operation"] == "update"
    assert body["payload"]["server_name"] == "server-x"
    assert body["review_payload"]["updates"]["host"] == "10.0.0.99"

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
    revision = main_module.app_state.db.get_active_server_config_revision(
        "server-x"
    )
    assert revision is not None
    assert revision["revision"] == 1
    assert revision["assignment_eligibility"] == "approved"


def test_noop_update_reapproval_adopts_existing_legacy_server_for_preflight(
    api_client, tmp_path
):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    before = client.get("/server-config/server-x").json()
    assert before["server_config_revision_id"] is None
    assert before["attempt_backend_preflight_available"] is False

    request = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {}},
    )
    assert request.status_code == 200
    assert request.json()["kind"] == "server_update"
    assert request.json()["review_payload"]["updates"]["host"] == "10.0.0.5"
    document_before_approval = load_servers_config(
        main_module.app_state.config.servers_yaml_path
    )
    approval_id = request.json()["id"]

    response = client.post(f"/approve/{approval_id}")

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "approved"
    after = client.get("/server-config/server-x").json()
    assert after["server_config_revision_id"] is not None
    assert after["attempt_backend_preflight_available"] is True
    assert after["attempt_backend_preflight"] is None
    assert (
        load_servers_config(main_module.app_state.config.servers_yaml_path)
        == document_before_approval
    )
    revision = main_module.app_state.db.get_active_server_config_revision(
        "server-x"
    )
    assert revision["created_by_approval_id"] == approval_id
    mutation = main_module.app_state.db.list_server_config_mutations()[0]
    assert mutation["operation"] == "update"
    assert mutation["prior_revision_id"] is None


def test_target_update_rejected_while_server_has_enrolled_node(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    main_module.app_state.db.insert_node(
        node_id="node-server-x",
        server_name="server-x",
        secret_hash="fake-node-secret-digest",
    )

    approval_id = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"host": "10.0.0.99"}},
    ).json()["id"]
    response = client.post(f"/approve/{approval_id}")

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "rejected"
    assert "execution ownership" in response.json()["approval"]["note"]
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["host"] == "10.0.0.5"


def test_display_only_update_allowed_while_server_has_enrolled_node(
    api_client, tmp_path
):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    main_module.app_state.db.insert_node(
        node_id="node-server-x",
        server_name="server-x",
        secret_hash="fake-node-secret-digest",
    )

    approval_id = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"note": "maintenance soon"}},
    ).json()["id"]
    response = client.post(f"/approve/{approval_id}")

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "approved"
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["note"] == "maintenance soon"


def test_legacy_server_mutation_cannot_bypass_unresolved_publication_journal(
    api_client, tmp_path
):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    target = {
        "backend": "ssh",
        "host": "192.0.2.80",
        "port": 22,
        "user": "worker",
        "project_roots": [],
        "dataset_roots": [],
    }
    credential = {
        "provider": "ssh-key-file-v1",
        "version_id": "test-key-v1",
        "real_path": "/tmp/fake-key",
        "file_identity": {
            "device": 1,
            "inode": 2,
            "size": 3,
            "mtime_ns": 4,
        },
    }
    before_digest = "a" * 64
    after_digest = "b" * 64
    journal_approval_id = main_module.app_state.db.insert_pinned_approval(
        kind="server_add",
        contract_version="server-config-v1",
        payload={
            "operation": "add",
            "server_name": "other-server",
            "normalized_target": target,
            "credential_ref": credential,
            "yaml_before_sha256": before_digest,
            "yaml_after_sha256": after_digest,
        },
    )
    mutation = main_module.app_state.db.prepare_server_config_mutation(
        approval_id=journal_approval_id,
        operation="add",
        server_name="other-server",
        normalized_target=target,
        credential_ref=credential,
        yaml_before_sha256=before_digest,
        yaml_after_sha256=after_digest,
        decision_actor_id="human-reviewer",
    )

    approval_id = client.post(
        "/server-config/update-request",
        json={"name": "server-x", "updates": {"note": "must not bypass"}},
    ).json()["id"]
    response = client.post(f"/approve/{approval_id}")

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "rejected"
    assert mutation["id"] in response.json()["approval"]["note"]
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0].get("note") is None


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
    assert body["payload_contract_version"] == "server-config-v1"
    assert body["payload"]["operation"] == "disable"
    assert body["payload"]["server_name"] == "server-x"
    assert body["review_payload"]["name"] == "server-x"

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
# delete-request：2026-07-26 起真的從 servers.yaml 移除（先備份）
# ---------------------------------------------------------------------------


def test_delete_request_creates_approval(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    resp = client.post("/server-config/delete-request", json={"name": "server-x"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "server_delete"


def test_approve_delete_with_running_job_is_rejected(api_client, tmp_path):
    """WP-0B characterization: the current running-Job guard is preserved."""
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


def test_approve_delete_removes_the_entry_through_the_http_path(api_client, tmp_path):
    """2026-07-26 行為變更（使用者要求）：這條測試原本釘的是「刪除只是設
    enabled=false，設定列仍在」。UI 上按「刪除」卻不會消失會誤導人，因此
    後端改為真的移除；這裡改釘新行為，並保留同樣的 HTTP 路徑覆蓋。"""
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"
    #: note 要說明移除了、並指出備份位置（救得回來）。
    assert "移除" in resp.json()["approval"]["note"]
    assert "備份" in resp.json()["approval"]["note"]

    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"] == []


def test_approve_delete_rejected_while_server_has_enrolled_node(api_client, tmp_path):
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    main_module.app_state.db.insert_node(
        node_id="node-server-x",
        server_name="server-x",
        secret_hash="fake-node-secret-digest",
    )

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    response = client.post(f"/approve/{approval_id}")

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "rejected"
    assert "execution ownership" in response.json()["approval"]["note"]
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert [server["name"] for server in on_disk["servers"]] == ["server-x"]


def test_server_mutation_foundation_schema_remains_default_off_after_guard_wiring(
    api_client,
):
    """The WP-1B guard is dual-read protection, not attempt-path activation.

    Durable tables and the guard can protect legacy server mutations while new
    generic claims remain disabled.
    """
    _client, main_module = api_client
    with main_module.app_state.db.cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row["name"] for row in cursor.fetchall()}
    assert "execution_attempts" in tables
    assert "execution_operations" in tables
    assert "server_config_revisions" in tables
    assert main_module.app_state.config.execution_attempt_new_claims_enabled is False


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


# ---------------------------------------------------------------------------
# 2026-07-26：server_delete 真的移除（先前只是停用，UI 上按了不會消失）
# ---------------------------------------------------------------------------


def _approve(main_module, approval_id):
    import asyncio

    from app.approvals import approve

    return asyncio.run(
        approve(
            main_module.app_state.db,
            approval_id,
            server_configs=main_module.app_state.server_configs,
            app_state=main_module.app_state,
            audit_path=main_module.app_state.config.audit_path,
        )
    )


def _yaml_names(path):
    import yaml

    doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    return [s["name"] for s in (doc.get("servers") or [])]


def test_approved_delete_actually_removes_the_entry(api_client, tmp_path):
    """核准後那台機器要從 servers.yaml 消失，不是留著 enabled=false。"""
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    yaml_path = main_module.app_state.config.servers_yaml_path
    assert "server-x" in _yaml_names(yaml_path)

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    result = _approve(main_module, approval_id)

    assert result["approval"].status == "approved"
    assert result["removed"] is True
    assert "server-x" not in _yaml_names(yaml_path)


def test_disable_still_only_flips_the_flag(api_client, tmp_path):
    """停用維持原語意——設定列必須留著。"""
    import yaml

    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    yaml_path = main_module.app_state.config.servers_yaml_path

    approval_id = client.post(
        "/server-config/disable-request", json={"name": "server-x"}
    ).json()["id"]
    result = _approve(main_module, approval_id)

    assert result["removed"] is False
    doc = yaml.safe_load(open(yaml_path, encoding="utf-8"))
    entry = next(s for s in doc["servers"] if s["name"] == "server-x")
    assert entry["enabled"] is False


def test_delete_records_the_removed_entry_in_the_audit(api_client, tmp_path):
    """被移除的整筆是它唯一的線上紀錄，必須進稽核才救得回來。"""
    import json

    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    audit_path = main_module.app_state.config.audit_path

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    _approve(main_module, approval_id)

    entries = [
        json.loads(line)
        for line in open(audit_path, encoding="utf-8")
        if '"server_delete"' in line
    ]
    removed = [e for e in entries if e.get("params", {}).get("removed_entry")]
    assert removed, "稽核裡找不到被移除的設定內容"
    assert removed[-1]["params"]["removed_entry"]["name"] == "server-x"
    assert removed[-1]["params"]["backup"]


def test_delete_refuses_to_remove_the_configured_codex_runner(api_client, tmp_path):
    """移除設定中的 Runner 會讓下次啟動驗證失敗——等於把服務弄成開不起來。"""
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="runner-x")
    main_module.app_state.config.codex_runner_server = "runner-x"
    yaml_path = main_module.app_state.config.servers_yaml_path

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "runner-x"}
    ).json()["id"]
    result = _approve(main_module, approval_id)

    assert result["approval"].status == "rejected"
    assert "Codex Runner" in result["approval"].note
    #: 設定檔完全沒被動過。
    assert "runner-x" in _yaml_names(yaml_path)


def test_delete_refuses_when_queued_jobs_are_pinned_to_that_server(api_client, tmp_path):
    """釘在這台的排隊任務會永遠等不到機器。"""
    client, main_module = api_client
    _seed_server(main_module, tmp_path, name="server-x")
    main_module.app_state.db.insert_job(command="sleep 1", pin_server="server-x")
    yaml_path = main_module.app_state.config.servers_yaml_path

    approval_id = client.post(
        "/server-config/delete-request", json={"name": "server-x"}
    ).json()["id"]
    result = _approve(main_module, approval_id)

    assert result["approval"].status == "rejected"
    assert "排隊中" in result["approval"].note
    assert "server-x" in _yaml_names(yaml_path)
