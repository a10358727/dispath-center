"""HTTP-surface tests for the `/api/v2/servers*` / `/api/v2/server-configs*` /
`/api/v2/inventory/*` / `/api/v2/codex-runner/status` wrappers
(DG-UI-UNIFICATION v1, U4).

These stay legacy-scope `platform` objects: thin wrappers around the exact
legacy `/servers*`, `/server-config*`, `/inventory/*`, and
`/codex-runner/status` surfaces (see
`dispatch_center/api/routers/infrastructure_v2.py` module docstring). The
list/detail tests assert byte-identical parity with the legacy endpoints
(model on `tests/test_server_config_api.py` / `tests/test_inventory_api.py`
fixtures).

DG-INFRA-DIRECT-ACTIONS v1 (2026-08-26 user ruling): `server_add`/
`server_update`/`server_disable` (incl. `enabled=true` re-enable) are
direct-execute web actions now (`POST /api/v2/server-configs`,
`.../{name}/update`, `.../{name}/disable`) -- see
`tests/test_server_config_api.py` for the matching legacy-path rewrite and
`dispatch_center/api/routers/infrastructure_v2.py` for the implementation.
`server_delete` is the one infrastructure action that keeps its approval
card, so `delete-requests` below is unchanged.
"""

from __future__ import annotations

import pytest

import os


from app.config import ServerConfig
from app.monitor import GpuReading, ServerState
from app.server_attempt_preflight import ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND
from app.server_config import load_servers_config, write_servers_yaml_atomically


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


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


def _make_server_config(name: str, project_roots=None, enabled: bool = True) -> ServerConfig:
    return ServerConfig(
        name=name,
        host="10.0.0.1",
        user="train",
        key="~/.ssh/id_rsa",
        project_roots=project_roots or [],
        enabled=enabled,
    )


def _seed_server_on_disk(main_module, tmp_path, name="server-a", **overrides) -> None:
    """Writes a server into `servers.yaml` (not just `app_state.server_configs`)
    so `update`/`disable`/`delete-requests` -- which check
    the on-disk document, not the in-memory cache -- find it (mirrors
    `tests/test_server_config_api.py::_seed_server`)."""

    payload = _valid_server_payload(tmp_path, name=name, **overrides)
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


class ManualCandidateFakeSSH:
    """Only answers `test -d ... && echo DIR_OK` (manual candidate probe)."""

    def __init__(self, dir_ok: bool = True):
        self.calls: list[str] = []
        self.dir_ok = dir_ok

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if command.startswith("test -d"):
            return FakeCommandResult("DIR_OK\n" if self.dir_ok else "")
        return FakeCommandResult("")


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("legacy_posture")
def test_flag_off_is_a_hidden_interface(api_client):
    client, main_module = api_client
    assert client.get("/api/v2/servers").status_code == 404
    assert client.get("/api/v2/servers/idle-summary").status_code == 404
    assert client.get("/api/v2/server-configs").status_code == 404
    assert client.get("/api/v2/inventory/candidates").status_code == 404
    assert client.get("/api/v2/codex-runner/status").status_code == 404
    assert (
        client.post(
            "/api/v2/inventory/scan-requests", json={"server": "server-a"}
        ).status_code
        == 404
    )

    main_module.app_state.config.api_v2_enabled = True
    # product_rbac_v2 still off -> still hidden.
    assert client.get("/api/v2/servers").status_code == 404


# ---------------------------------------------------------------------------
# Servers / idle-summary
# ---------------------------------------------------------------------------


def test_servers_list_is_byte_identical_to_legacy(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_states["server-a"] = ServerState(
        name="server-a",
        online=True,
        gpus=[GpuReading(util_percent=10.0, mem_used_mb=100.0, mem_total_mb=1000.0)],
        load1=0.5,
        cpu_count=8,
    )

    legacy = client.get("/servers").json()
    v2 = client.get("/api/v2/servers").json()
    assert v2 == legacy
    assert len(v2) == 1
    assert v2[0]["name"] == "server-a"
    assert v2[0]["gpu_util_max"] == 10.0
    #: Part A（總覽儀表板改版）：`asdict(ServerState)` picks up the new
    #: `cpu_count` field automatically -- no projection change needed, just
    #: parity coverage.
    assert v2[0]["cpu_count"] == 8
    #: config-less state -> fail-closed `enabled=False` (INV-STATE-1, see
    #: `_server_state_to_dict` docstring in `app/main.py`).
    assert v2[0]["enabled"] is False


def test_idle_summary_matches_legacy_shape(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a"),
    }

    legacy = client.get("/servers/idle-summary").json()
    v2 = client.get("/api/v2/servers/idle-summary").json()
    assert v2 == legacy
    assert v2["window_hours"] == 24
    assert len(v2["servers"]) == 1
    assert v2["servers"][0]["server_name"] == "server-a"
    assert v2["servers"][0]["status"] == "unknown"

    v2_hours = client.get("/api/v2/servers/idle-summary", params={"hours": 6}).json()
    assert v2_hours["window_hours"] == 6


# ---------------------------------------------------------------------------
# Server config: list / detail / test-ssh
# ---------------------------------------------------------------------------


def test_server_configs_list_and_detail_are_byte_identical_to_legacy(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a", project_roots=["~/projects"]),
    }

    legacy_list = client.get("/server-config").json()
    v2_list = client.get("/api/v2/server-configs").json()
    assert v2_list == legacy_list
    assert len(v2_list) == 1
    #: only the key *path*, never file contents.
    assert v2_list[0]["key"] == "~/.ssh/id_rsa"

    legacy_detail = client.get("/server-config/server-a").json()
    v2_detail = client.get("/api/v2/server-configs/server-a").json()
    assert v2_detail == legacy_detail


def test_server_config_detail_404_for_unknown_server(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert client.get("/api/v2/server-configs/does-not-exist").status_code == 404


def test_test_ssh_invalid_config_returns_ok_false_without_any_ssh_call(api_client, tmp_path):
    """驗證失敗（host=0.0.0.0）→ 不做任何 SSH 呼叫，回 ok=False + errors，
    HTTP 200；也不寫稽核（沒有實際測試發生）。"""

    client, main_module = api_client
    _enable_v2(main_module)
    calls = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        calls.append(command)
        return FakeCommandResult("ok\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    payload = _valid_server_payload(tmp_path, host="0.0.0.0")
    resp = client.post("/api/v2/server-configs/test-ssh", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["results"] == {}
    assert body["errors"]
    assert calls == []

    events = client.get("/events").json()
    assert not any(e["action"] == "server_test_ssh" for e in events)


def test_test_ssh_valid_config_runs_fixed_commands_and_writes_audit(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    calls: list[str] = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        calls.append(command)
        if command == "hostname":
            return FakeCommandResult("host-x\n")
        return FakeCommandResult("")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    payload = _valid_server_payload(tmp_path)
    resp = client.post("/api/v2/server-configs/test-ssh", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["results"]["hostname"] == "host-x"
    assert len(calls) == 5  # hostname/whoami/tmux/gpu + 1 project_root

    # test-ssh 不建 approval、不改 servers.yaml
    assert client.get("/approvals").json() == []
    assert not os.path.exists(main_module.app_state.config.servers_yaml_path)

    events = client.get("/events").json()
    assert any(e["action"] == "server_test_ssh" for e in events)


# ---------------------------------------------------------------------------
# D-5 attempt filesystem preflight: `POST /api/v2/server-configs/{name}/
# attempt-preflight` mirrors legacy `POST /server-config/{name}/
# attempt-preflight` verbatim via
# `app.server_attempt_preflight.run_attempt_filesystem_preflight()` -- see
# `dispatch_center/api/routers/infrastructure_v2.py` for the shared-helper
# reasoning. `tests/test_server_config_api.py` covers the fixed-command
# contract, revision CAS, and durable-audit rollback in depth; the tests
# below only need to prove the v2 surface reaches the identical body and
# translates its typed errors to the v2 error envelope.
# ---------------------------------------------------------------------------


def _add_server_via_v2(client, tmp_path, **overrides) -> dict:
    payload = _valid_server_payload(tmp_path, **overrides)
    resp = client.post("/api/v2/server-configs", json=payload)
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"
    return payload


def test_attempt_preflight_records_eligible_evidence_and_audit(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _add_server_via_v2(client, tmp_path)
    calls = []

    async def fake_ssh_pool_run(server_cfg, command, timeout):
        calls.append((server_cfg.name, command, timeout))
        return FakeCommandResult("DISPATCH_FS_TYPE=ext4\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    before = client.get("/api/v2/server-configs/server-x").json()
    assert before["attempt_backend_preflight"] is None
    assert before["attempt_backend_eligible"] is False

    resp = client.post("/api/v2/server-configs/server-x/attempt-preflight")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "eligible"
    assert body["filesystem_type"] == "ext4"
    assert calls == [("server-x", ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND, 15)]

    after = client.get("/api/v2/server-configs/server-x").json()
    assert after["attempt_backend_preflight"] == "eligible"
    assert after["attempt_backend_eligible"] is True

    events = client.get("/events").json()
    assert any(
        e["action"] == "server_attempt_backend_preflight"
        and e["params"]["status"] == "eligible"
        for e in events
    )


def test_attempt_preflight_response_matches_legacy_on_same_fixture(api_client, tmp_path):
    """Same server, same fake SSH output, same revision: the legacy endpoint
    and the v2 mirror must return the identical body (`observed_at` may tick
    forward between the two calls, so it is compared separately)."""

    client, main_module = api_client
    _enable_v2(main_module)
    _add_server_via_v2(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run

    legacy_body = client.post("/server-config/server-x/attempt-preflight").json()
    v2_body = client.post("/api/v2/server-configs/server-x/attempt-preflight").json()

    assert legacy_body.pop("observed_at") is not None
    assert v2_body.pop("observed_at") is not None
    assert legacy_body == v2_body


def test_attempt_preflight_unknown_server_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post("/api/v2/server-configs/does-not-exist/attempt-preflight")
    assert resp.status_code == 404


def test_attempt_preflight_requires_an_active_approved_revision(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
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
    resp = client.post("/api/v2/server-configs/server-x/attempt-preflight")

    assert resp.status_code == 409
    assert called is False


def test_attempt_preflight_non_ssh_backend_returns_400(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _add_server_via_v2(client, tmp_path, execution_backend="node")

    resp = client.post("/api/v2/server-configs/server-x/attempt-preflight")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "server_preflight_non_ssh_backend"


def test_attempt_preflight_refuses_revision_drift_during_probe(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    payload = _add_server_via_v2(client, tmp_path)

    async def fake_ssh_pool_run(_server_cfg, _command, _timeout):
        with open(payload["key"], "a", encoding="utf-8") as key_file:
            key_file.write("rotated-during-preflight\n")
        return FakeCommandResult("DISPATCH_FS_TYPE=xfs\n")

    main_module.app_state.ssh_pool.run = fake_ssh_pool_run
    resp = client.post("/api/v2/server-configs/server-x/attempt-preflight")

    assert resp.status_code == 409
    revision = main_module.app_state.db.get_active_server_config_revision("server-x")
    assert revision["attempt_backend_preflight"] is None


# ---------------------------------------------------------------------------
# Server config: add/update/disable are DG-INFRA-DIRECT-ACTIONS v1
# (2026-08-26 user ruling) direct-execute web actions now; delete-requests
# stays an approval-card creator (the one infrastructure action the ruling
# keeps gated on human approval).
# ---------------------------------------------------------------------------


def test_add_writes_yaml_and_activates_directly(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    payload = _valid_server_payload(tmp_path)

    resp = client.post("/api/v2/server-configs", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    approval = body["approval"]
    assert approval["kind"] == "server_add"
    assert approval["status"] == "approved"
    assert body["reload"]["ok"] is True
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert any(s["name"] == "server-x" for s in on_disk["servers"])
    assert "server-x" in main_module.app_state.server_configs

    legacy_approvals = client.get("/approvals").json()
    assert any(a["id"] == approval["id"] for a in legacy_approvals)


def test_add_invalid_config_returns_400_and_no_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    payload = _valid_server_payload(tmp_path, host="0.0.0.0")

    resp = client.post("/api/v2/server-configs", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_server_config"
    assert client.get("/approvals").json() == []
    assert not os.path.exists(main_module.app_state.config.servers_yaml_path)


def test_update_merges_and_writes_directly(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)

    resp = client.post(
        "/api/v2/server-configs/server-a/update",
        json={"name": "server-a", "updates": {"idle_gpu_util": 42.0}},
    )
    assert resp.status_code == 200
    body = resp.json()
    approval = body["approval"]
    assert approval["kind"] == "server_update"
    assert approval["status"] == "approved"
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    updated = next(s for s in on_disk["servers"] if s["name"] == "server-a")
    assert updated["idle_gpu_util"] == 42.0


def test_update_path_name_body_name_mismatch_returns_400(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)

    resp = client.post(
        "/api/v2/server-configs/server-a/update",
        json={"name": "server-b", "updates": {"idle_gpu_util": 42.0}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "server_name_mismatch"


def test_update_unknown_server_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/server-configs/does-not-exist/update",
        json={"name": "does-not-exist", "updates": {}},
    )
    assert resp.status_code == 404


def test_update_rename_rejected(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a"),
    }
    resp = client.post(
        "/api/v2/server-configs/server-a/update",
        json={"name": "server-a", "updates": {"name": "server-b"}},
    )
    assert resp.status_code == 400


def test_disable_writes_enabled_false_directly(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)
    resp = client.post("/api/v2/server-configs/server-a/disable")
    assert resp.status_code == 200
    body = resp.json()
    approval = body["approval"]
    assert approval["kind"] == "server_disable"
    assert approval["status"] == "approved"
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is False
    assert main_module.app_state.server_configs["server-a"].enabled is False


def test_disable_unknown_server_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post("/api/v2/server-configs/does-not-exist/disable")
    assert resp.status_code == 404


def test_disable_then_update_enabled_true_reenables_server(api_client, tmp_path):
    """使用者裁定明文提到「含重新啟用」：走 `.../update`
    （`updates={"enabled": true}`），不是另一個 enable 端點。"""
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)

    assert client.post("/api/v2/server-configs/server-a/disable").status_code == 200
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is False

    resp = client.post(
        "/api/v2/server-configs/server-a/update",
        json={"name": "server-a", "updates": {"enabled": True}},
    )
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["enabled"] is True
    assert main_module.app_state.server_configs["server-a"].enabled is True


def test_pending_server_update_card_from_before_this_change_is_still_decidable(
    api_client, tmp_path
):
    """DG-INFRA-DIRECT-ACTIONS v1 相容保證：既有（例如 agent 工具建立、或
    這次改動之前建立的）pending `server_update` 卡仍可透過既有通用核准路徑
    決定——這裡直接用 `request_server_update_approval()` 模擬那張舊卡（不
    透過現在已經 direct-execute 的 v2 端點建立），再用既有
    `POST /approve/{id}` 決定它。"""
    from app.approvals import request_server_update_approval

    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)
    current_document = load_servers_config(main_module.app_state.config.servers_yaml_path)

    approval = request_server_update_approval(
        main_module.app_state.db,
        "server-a",
        {"idle_gpu_util": 7.0},
        main_module.app_state.config,
        current_document.get("servers") or [],
        audit_path=main_module.app_state.config.audit_path,
        current_document=current_document,
    )
    assert approval.status == "pending"

    resp = client.post(f"/approve/{approval.id}")
    assert resp.status_code == 200
    assert resp.json()["approval"]["status"] == "approved"
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    updated = next(s for s in on_disk["servers"] if s["name"] == "server-a")
    assert updated["idle_gpu_util"] == 7.0


def test_delete_request_creates_pending_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)
    resp = client.post(
        "/api/v2/server-configs/delete-requests", json={"name": "server-a"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_delete"
    assert body["status"] == "pending"


def test_delete_request_unknown_server_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/server-configs/delete-requests", json={"name": "does-not-exist"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Inventory: scan-requests / candidates
# ---------------------------------------------------------------------------


def test_scan_request_creates_pending_approval_without_scanning(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/inventory/scan-requests",
        json={"server": "server-a", "project_roots": ["/data/projects"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "inventory_scan"
    assert body["status"] == "pending"
    assert body["payload"]["server"] == "server-a"
    assert client.get("/api/v2/inventory/candidates").json() == []


def test_scan_request_server_all_creates_one_approval_per_enabled_server(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a", project_roots=["/data/a"]),
        "server-b": _make_server_config("server-b", project_roots=["/data/b"]),
        "server-c": _make_server_config(
            "server-c", project_roots=["/data/c"], enabled=False
        ),
    }
    resp = client.post("/api/v2/inventory/scan-requests", json={"server": "all"})
    assert resp.status_code == 200
    approvals = resp.json()["approvals"]
    assert len(approvals) == 2
    servers_scanned = {a["payload"]["server"] for a in approvals}
    assert servers_scanned == {"server-a", "server-b"}
    for a in approvals:
        assert a["kind"] == "inventory_scan"
        assert a["status"] == "pending"


def test_scan_request_forbidden_root_returns_400_and_no_approval(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/inventory/scan-requests",
        json={"server": "server-a", "project_roots": ["/etc"]},
    )
    assert resp.status_code == 400
    assert client.get("/approvals").json() == []


def _manual_add(client, server: str = "server-a", path: str = "/data/projects/proj1"):
    resp = client.post(
        "/api/v2/inventory/candidates", json={"server": server, "path": path}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_manual_add_creates_pending_candidate_directly_no_approval(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    candidate = _manual_add(client)
    assert candidate["status"] == "pending"
    assert candidate["server"] == "server-a"
    assert candidate["path"] == "/data/projects/proj1"
    #: 不走核准流。
    assert client.get("/approvals").json() == []

    legacy = client.get("/inventory/candidates").json()
    v2 = client.get("/api/v2/inventory/candidates").json()
    assert v2 == legacy
    assert len(v2) == 1


def test_manual_add_rejects_unknown_server(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    resp = client.post(
        "/api/v2/inventory/candidates",
        json={"server": "unknown-server", "path": "/data/projects/foo"},
    )
    assert resp.status_code == 400
    assert client.get("/api/v2/inventory/candidates").json() == []


def test_manual_add_duplicate_returns_409(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()

    first = _manual_add(client)
    resp = client.post(
        "/api/v2/inventory/candidates",
        json={"server": "server-a", "path": "/data/projects/proj1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "manual_candidate_duplicate"
    assert resp.json()["error"]["details"]["id"] == first["id"]


def test_import_request_creates_pending_approval_without_importing(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    candidate = _manual_add(client)

    resp = client.post(
        f"/api/v2/inventory/candidates/{candidate['id']}/import-requests",
        json={"name": "proj1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "import_project"
    assert body["status"] == "pending"
    assert client.get("/projects").json() == []


def test_import_request_nonexistent_candidate_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/inventory/candidates/does-not-exist/import-requests", json={}
    )
    assert resp.status_code == 404


def test_ignore_request_creates_pending_approval_without_changing_status(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    candidate = _manual_add(client)

    resp = client.post(
        f"/api/v2/inventory/candidates/{candidate['id']}/ignore-requests"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "ignore_project_candidate"
    assert body["status"] == "pending"
    assert (
        client.get("/api/v2/inventory/candidates").json()[0]["status"] == "pending"
    )


def test_ignore_request_nonexistent_candidate_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post("/api/v2/inventory/candidates/does-not-exist/ignore-requests")
    assert resp.status_code == 404


def test_ignore_nested_request_creates_pending_approval_with_nested_only(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {"server-a": _make_server_config("server-a")}
    main_module.app_state.ssh_run = ManualCandidateFakeSSH()
    _manual_add(client, path="/data/projects/outer")
    _manual_add(client, path="/data/projects/outer/nested")

    resp = client.post("/api/v2/inventory/candidates/ignore-nested-requests")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "ignore_nested_candidates"
    assert body["status"] == "pending"


def test_ignore_nested_request_no_candidates_returns_400(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post("/api/v2/inventory/candidates/ignore-nested-requests")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# codex-runner status
# ---------------------------------------------------------------------------


def test_codex_runner_status_unconfigured_shape(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    assert main_module.app_state.config.codex_runner_server is None

    legacy = client.get("/codex-runner/status").json()
    v2 = client.get("/api/v2/codex-runner/status").json()
    assert v2 == legacy
    assert v2 == {"configured": False}
