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
"""

from __future__ import annotations

import os

import pytest

from app.config import ServerConfig
from app.monitor import GpuReading, ServerState
from app.server_config import write_servers_yaml_atomically


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
    so `update-requests`/`disable-requests`/`delete-requests` -- which check
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
    )

    legacy = client.get("/servers").json()
    v2 = client.get("/api/v2/servers").json()
    assert v2 == legacy
    assert len(v2) == 1
    assert v2[0]["name"] == "server-a"
    assert v2[0]["gpu_util_max"] == 10.0
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
# Server config: add/update/disable/delete-requests
# ---------------------------------------------------------------------------


def test_add_request_creates_pending_approval_without_writing_yaml(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    payload = _valid_server_payload(tmp_path)

    resp = client.post("/api/v2/server-configs/add-requests", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_add"
    assert body["status"] == "pending"
    assert not os.path.exists(main_module.app_state.config.servers_yaml_path)

    legacy_approvals = client.get("/approvals").json()
    assert any(a["id"] == body["id"] for a in legacy_approvals)


def test_add_request_invalid_config_returns_400_and_no_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    payload = _valid_server_payload(tmp_path, host="0.0.0.0")

    resp = client.post("/api/v2/server-configs/add-requests", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_server_config"
    assert client.get("/approvals").json() == []


def test_update_request_creates_pending_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)

    resp = client.post(
        "/api/v2/server-configs/update-requests",
        json={"name": "server-a", "updates": {"idle_gpu_util": 42.0}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_update"
    assert body["status"] == "pending"


def test_update_request_unknown_server_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/server-configs/update-requests",
        json={"name": "does-not-exist", "updates": {}},
    )
    assert resp.status_code == 404


def test_update_request_rename_rejected(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.server_configs = {
        "server-a": _make_server_config("server-a"),
    }
    resp = client.post(
        "/api/v2/server-configs/update-requests",
        json={"name": "server-a", "updates": {"name": "server-b"}},
    )
    assert resp.status_code == 400


def test_disable_request_creates_pending_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    _seed_server_on_disk(main_module, tmp_path)
    resp = client.post(
        "/api/v2/server-configs/disable-requests", json={"name": "server-a"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "server_disable"
    assert body["status"] == "pending"


def test_disable_request_unknown_server_returns_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    resp = client.post(
        "/api/v2/server-configs/disable-requests", json={"name": "does-not-exist"}
    )
    assert resp.status_code == 404


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
        client.get(f"/api/v2/inventory/candidates").json()[0]["status"] == "pending"
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
