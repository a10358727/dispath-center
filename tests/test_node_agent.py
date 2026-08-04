"""Goal 3 C2：Node Agent 套件 + 端點整合（INV-NODE-1…5）。

全部用 in-process fake 傳輸與 TestClient，不開網路、不碰真實工作機、
不起任何行程（`spawn` 一律注入假的）——INV-TEST-2。

涵蓋 roadmap Phase 3 明列的強制情境：duplicate poll、duplicate ack、
agent restart、control-plane restart、啟動前/後斷線、stale lease、
terminal upload retry、malformed/unauthorized payload。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.client import (
    NODE_PROTOCOL_VERSION as AGENT_PROTOCOL_VERSION,
    NODE_PROTOCOL_VERSION_HEADER as AGENT_PROTOCOL_VERSION_HEADER,
    LeasedWork,
    NodeAgentClient,
    NodeClientError,
    command_digest,
)
from agent.runner import (
    AttemptStore,
    LocalAttempt,
    build_launcher_argv,
    build_supervisor_argv,
    launch,
    plan_restart,
)
from app.db import Database
from app.node_protocol import (
    AttemptStatus,
    NODE_PROTOCOL_CAPABILITIES,
    NODE_PROTOCOL_VERSION,
    NODE_PROTOCOL_VERSION_HEADER,
    NodeAttempt,
    build_node_launcher_argv,
    command_digest as cp_command_digest,
    plan_agent_restart,
)
from app.node_registry import (
    NodeAuthError,
    authenticate_node,
    enroll_node,
    lease_job_for_node,
)


# ---------------------------------------------------------------------------
# 1. agent ↔ control plane 的實作一致性（兩邊刻意各自實作，這裡釘住等價）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["python train.py", "", "echo '$(whoami)'", "多位元組指令 🚀", "a" * 10_000],
)
def test_agent_and_control_plane_command_digest_agree(command):
    assert command_digest(command) == cp_command_digest(command)


@pytest.mark.parametrize(
    "attempt_id", ["abc-123", "11111111-1111-4111-8111-111111111111"]
)
def test_agent_and_control_plane_launcher_argv_agree(attempt_id):
    workdir = f"/home/w/.dc/{attempt_id}"
    assert build_launcher_argv(attempt_id, workdir) == build_node_launcher_argv(
        attempt_id, workdir
    )


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("acked", [False, True])
@pytest.mark.parametrize("alive", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_agent_and_control_plane_restart_rules_agree(terminal, acked, alive, expired):
    """完整情境矩陣：agent 端與 control plane 端的重啟判定必須一致。"""
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    local = LocalAttempt(
        attempt_id="abc-123",
        job_id=1,
        command_sha256="d" * 64,
        acked=acked,
        pid=999 if acked else None,
        terminal=terminal,
    )
    if terminal:
        status = AttemptStatus.DONE
    elif acked:
        status = AttemptStatus.ACKED
    else:
        status = AttemptStatus.LEASED
    control = NodeAttempt(
        id="abc-123",
        job_id=1,
        node_id="node-a",
        status=status,
        command_sha256="d" * 64,
        lease_expires_at=now - timedelta(seconds=1) if expired else now
        + timedelta(seconds=60),
        acked_at=now if acked else None,
    )

    agent_plan = plan_restart(local, process_alive=alive, lease_expired=expired)
    cp_plan = plan_agent_restart(control, local_process_alive=alive, now=now)

    assert agent_plan.may_launch == cp_plan.may_launch
    assert agent_plan.resume_monitoring == cp_plan.resume_monitoring


# ---------------------------------------------------------------------------
# 2. agent 用戶端（注入 fake 傳輸，不開網路）
# ---------------------------------------------------------------------------


class _FakeTransport:
    """把 agent 的 HTTP 呼叫導向可控的假回應，並記錄送出的 header/body。"""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str, dict, dict]] = []

    def __call__(self, method, path, json=None, headers=None):
        self.calls.append((method, path, json or {}, headers or {}))
        status, body = self.responses.get(path, (404, {"detail": "no fake"}))
        if callable(body):
            body = body(json or {})
        return status, body


def _client(responses) -> tuple[NodeAgentClient, _FakeTransport]:
    transport = _FakeTransport(responses)
    return NodeAgentClient(transport, node_token="dcn_secret.value"), transport


def test_client_always_sends_node_token_header():
    client, transport = _client({"/node-agent/heartbeat": (200, {"ok": True})})
    client.heartbeat()
    assert transport.calls[0][3]["X-Node-Token"] == "dcn_secret.value"


def test_agent_and_control_plane_protocol_contract_is_explicit():
    assert AGENT_PROTOCOL_VERSION == NODE_PROTOCOL_VERSION == "2.0"
    assert AGENT_PROTOCOL_VERSION_HEADER == NODE_PROTOCOL_VERSION_HEADER


def test_client_probe_is_read_only_and_sends_protocol_header():
    client, transport = _client(
        {
            "/node-agent/probe": (
                200,
                {
                    "ok": True,
                    "protocol_version": NODE_PROTOCOL_VERSION,
                    "capabilities": list(NODE_PROTOCOL_CAPABILITIES),
                },
            )
        }
    )
    assert client.probe()["ok"] is True
    method, path, payload, headers = transport.calls[0]
    assert (method, path, payload) == ("POST", "/node-agent/probe", {})
    assert headers[AGENT_PROTOCOL_VERSION_HEADER] == AGENT_PROTOCOL_VERSION


def test_client_probe_rejects_incompatible_success_response():
    client, _ = _client(
        {"/node-agent/probe": (200, {"ok": True, "protocol_version": "1.0"})}
    )
    with pytest.raises(NodeClientError, match="unsupported node protocol"):
        client.probe()


def test_client_poll_returns_none_when_no_work():
    client, _ = _client({"/node-agent/poll": (200, {"attempt": None, "reason": "x"})})
    assert client.poll(1) is None


def test_client_poll_parses_work_and_reused_flag():
    body = {
        "attempt": {
            "id": "a-1",
            "job_id": 7,
            "command": "python train.py",
            "command_sha256": command_digest("python train.py"),
            "lease_expires_at": "2026-07-25T12:02:00+00:00",
            "status": "leased",
        },
        "reused": True,
    }
    client, _ = _client({"/node-agent/poll": (200, body)})
    work = client.poll(7)
    assert work.attempt_id == "a-1"
    assert work.reused is True


def test_client_refuses_to_ack_when_local_digest_check_fails():
    """INV-NODE-3：control plane 給的內容與 digest 不符時 agent 自己就拒絕
    ——不送出、不執行。"""
    client, transport = _client({"/node-agent/ack": (200, {"accepted": True})})
    tampered = LeasedWork(
        attempt_id="a-1",
        job_id=7,
        command="rm -rf /",
        command_sha256=command_digest("python train.py"),
        lease_expires_at="",
        reused=False,
    )
    with pytest.raises(NodeClientError):
        client.acknowledge(tampered)
    assert transport.calls == []


def test_client_ack_returns_false_on_duplicate():
    client, _ = _client({"/node-agent/ack": (200, {"accepted": True, "duplicate": True})})
    work = LeasedWork(
        attempt_id="a-1",
        job_id=7,
        command="python train.py",
        command_sha256=command_digest("python train.py"),
        lease_expires_at="",
        reused=True,
    )
    assert client.acknowledge(work) is False


def test_client_raises_on_error_status_without_leaking_token():
    client, _ = _client({"/node-agent/ack": (409, {"detail": "lease expired"})})
    work = LeasedWork(
        attempt_id="a-1",
        job_id=7,
        command="python train.py",
        command_sha256=command_digest("python train.py"),
        lease_expires_at="",
        reused=False,
    )
    with pytest.raises(NodeClientError) as excinfo:
        client.acknowledge(work)
    assert "dcn_secret" not in str(excinfo.value)


def test_client_terminal_retry_is_idempotent():
    client, _ = _client(
        {"/node-agent/terminal": (200, {"accepted": True, "duplicate": True})}
    )
    assert client.report_terminal("a-1", exit_code=0) is False


# ---------------------------------------------------------------------------
# 3. agent 本機狀態與啟動（INV-NODE-2/3/5）
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid


def _spawn_recorder(recorded):
    def _spawn(argv, cwd=None):
        recorded.append((argv, cwd))
        return _FakeProcess()

    return _spawn


def test_launch_refuses_without_ack(tmp_path):
    """INV-NODE-2：ack 前不得產生任何副作用。"""
    store = AttemptStore(tmp_path)
    attempt = store.create(attempt_id="a-1", job_id=1, command_sha256="d" * 64)
    recorded = []
    with pytest.raises(RuntimeError, match="never acknowledged"):
        launch(store, attempt, spawn=_spawn_recorder(recorded))
    assert recorded == []


def test_launch_uses_argv_list_and_never_a_shell_string(tmp_path):
    store = AttemptStore(tmp_path)
    command = "python train.py --flag='a b'"
    attempt = store.create(
        attempt_id="a-1", job_id=1, command_sha256=command_digest(command)
    )
    store.record_ack(attempt)
    store.write_command("a-1", command)
    attempt = store.load("a-1")

    recorded = []
    launch(store, attempt, spawn=_spawn_recorder(recorded))

    argv, _cwd = recorded[0]
    assert isinstance(argv, list)
    assert argv == build_supervisor_argv(
        "a-1", store.attempt_dir("a-1"), command_digest(command)
    )
    #: 使用者指令原文絕不出現在 argv 裡。
    assert not any("train.py" in part for part in argv)


def test_launch_uses_an_explicit_process_group_and_no_shell(tmp_path):
    store = AttemptStore(tmp_path)
    command = "echo isolated"
    attempt = store.create(
        attempt_id="a-1", job_id=1, command_sha256=command_digest(command)
    )
    store.record_ack(attempt)
    store.write_command("a-1", command)
    attempt = store.load("a-1")
    observed = {}

    def _spawn(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return _FakeProcess()

    launch(store, attempt, spawn=_spawn)
    assert observed["shell"] is False
    assert observed["start_new_session"] is True


def test_launch_refuses_second_launch_of_same_attempt(tmp_path):
    store = AttemptStore(tmp_path)
    command = "true"
    attempt = store.create(
        attempt_id="a-1", job_id=1, command_sha256=command_digest(command)
    )
    store.record_ack(attempt)
    store.write_command("a-1", command)
    attempt = store.load("a-1")
    launch(store, attempt, spawn=_spawn_recorder([]))
    with pytest.raises(RuntimeError, match="already launched"):
        launch(store, attempt, spawn=_spawn_recorder([]))


def test_ack_is_persisted_before_launch_survives_restart(tmp_path):
    """重啟後讀本機狀態：已 ack 的 attempt 一定看得出來（INV-NODE-5）。"""
    store = AttemptStore(tmp_path)
    attempt = store.create(attempt_id="a-1", job_id=1, command_sha256="d" * 64)
    store.record_ack(attempt)

    reopened = AttemptStore(tmp_path).load("a-1")
    assert reopened.acked is True


def test_corrupt_local_state_is_fail_closed(tmp_path):
    store = AttemptStore(tmp_path)
    store.create(attempt_id="a-1", job_id=1, command_sha256="d" * 64)
    (store.attempt_dir("a-1") / "attempt.json").write_text("{not json")
    assert store.load("a-1") is None


@pytest.mark.parametrize("attempt_id", ["../escape", "a;rm -rf /", "a b", ""])
def test_store_rejects_unsafe_attempt_ids(tmp_path, attempt_id):
    with pytest.raises(ValueError):
        AttemptStore(tmp_path).attempt_dir(attempt_id)


def test_command_bytes_land_in_a_file_not_a_command_line(tmp_path):
    store = AttemptStore(tmp_path)
    payload = "echo $(whoami); rm -rf /tmp/x"
    attempt = store.create(
        attempt_id="a-1", job_id=1, command_sha256=command_digest(payload)
    )
    store.record_ack(attempt)
    path = store.write_command("a-1", payload)
    assert path.read_text() == payload


def test_command_digest_mismatch_never_materializes(tmp_path):
    store = AttemptStore(tmp_path)
    command = "echo safe"
    attempt = store.create(
        attempt_id="a-1", job_id=1, command_sha256=command_digest(command)
    )
    store.record_ack(attempt)
    with pytest.raises(ValueError, match="digest"):
        store.write_command("a-1", "echo tampered")
    assert not (store.attempt_dir("a-1") / "cmd.sh").exists()


# ---------------------------------------------------------------------------
# 4. 端點整合（TestClient，node 憑證真的走 middleware）
# ---------------------------------------------------------------------------


@pytest.fixture()
def node_api(api_client):
    """啟用 node agent 旗標，登錄一個 node，並建立一個 queued job。"""
    client, main_module = api_client
    state = main_module.app_state
    state.config.node_agent_v1_enabled = True

    #: roadmap Phase 3 的 canary 資格閘門：預設沒有任何 job 合格，所以
    #: 測試必須**明確指定**這個 job 是 canary 對象（正是操作者要做的事）。
    state.config.node_canary_require_tag = "node-canary"
    enrolled = enroll_node(state.db, server_name="worker-a")
    job_id = state.db.insert_job(
        command="python train.py", type="train", require_tag="node-canary"
    )
    return client, state, enrolled, state.db.get_job(job_id)


def test_node_endpoints_404_when_flag_disabled(api_client):
    client, main_module = api_client
    main_module.app_state.config.node_agent_v1_enabled = False
    resp = client.post("/node-agent/poll", json={})
    assert resp.status_code == 404


def test_node_endpoint_rejects_missing_credential(node_api):
    client, *_ = node_api
    assert client.post("/node-agent/poll", json={}).status_code == 401


def test_node_probe_returns_contract_metadata_and_rejects_wrong_version(node_api):
    client, _, enrolled, _ = node_api
    headers = {"X-Node-Token": enrolled.raw_token}

    probe = client.post("/node-agent/probe", headers=headers)
    assert probe.status_code == 200
    body = probe.json()
    assert body["ok"] is True
    assert body["node_id"] == enrolled.node.id
    assert body["server"] == "worker-a"
    assert body["protocol_version"] == NODE_PROTOCOL_VERSION
    assert body["capabilities"] == list(NODE_PROTOCOL_CAPABILITIES)
    assert body["assignment_enabled"] is True
    assert probe.headers[NODE_PROTOCOL_VERSION_HEADER] == NODE_PROTOCOL_VERSION

    incompatible = client.post(
        "/node-agent/probe",
        headers={
            **headers,
            NODE_PROTOCOL_VERSION_HEADER: "1.0",
        },
    )
    assert incompatible.status_code == 426
    assert incompatible.headers[NODE_PROTOCOL_VERSION_HEADER] == NODE_PROTOCOL_VERSION
    assert incompatible.json()["protocol_version"] == NODE_PROTOCOL_VERSION


@pytest.mark.parametrize(
    "token", ["", "garbage", "dcn_not-a-uuid.x", "dcs_11111111-1111-4111-8111-111111111111.x"]
)
def test_node_endpoint_rejects_malformed_or_wrong_kind_credential(node_api, token):
    """人類/服務 token 形狀在 node 通道一律無效（INV-NODE-1 分離）。"""
    client, *_ = node_api
    resp = client.post(
        "/node-agent/poll", json={}, headers={"X-Node-Token": token}
    )
    assert resp.status_code == 401


def test_revoked_node_is_rejected_immediately(node_api):
    client, state, enrolled, job = node_api
    state.db.revoke_node(enrolled.node.id)
    resp = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    )
    assert resp.status_code == 401


def test_full_lease_ack_heartbeat_terminal_round_trip(node_api):
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}

    poll = client.post("/node-agent/poll", json={}, headers=headers)
    assert poll.status_code == 200
    attempt = poll.json()["attempt"]
    assert poll.json()["reused"] is False
    assert attempt["command"] == "python train.py"

    ack = client.post(
        "/node-agent/ack",
        json={"attempt_id": attempt["id"], "command_sha256": attempt["command_sha256"]},
        headers=headers,
    )
    assert ack.status_code == 200 and ack.json()["duplicate"] is False

    hb = client.post(
        "/node-agent/heartbeat", json={"attempt_id": attempt["id"]}, headers=headers
    )
    assert hb.status_code == 200

    term = client.post(
        "/node-agent/terminal",
        json={"attempt_id": attempt["id"], "exit_code": 0, "log_tail": "ok"},
        headers=headers,
    )
    assert term.status_code == 200 and term.json()["duplicate"] is False

    row = state.db.get_node_attempt(attempt["id"])
    assert row.status == "done" and row.exit_code == 0


def test_duplicate_poll_returns_same_attempt(node_api):
    """零重複啟動的第一道保證：重複輪詢不產生第二個 attempt。"""
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}

    first = client.post("/node-agent/poll", json={}, headers=headers)
    second = client.post("/node-agent/poll", json={}, headers=headers)

    assert first.json()["attempt"]["id"] == second.json()["attempt"]["id"]
    assert second.json()["reused"] is True
    assert len(state.db.list_node_attempts(job_id=job.id)) == 1


def test_second_node_cannot_lease_same_job(node_api):
    """INV-NODE-2：一個 attempt 只能被一個 agent lease。"""
    client, state, enrolled, job = node_api
    other = enroll_node(state.db, server_name="worker-b")

    client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    )
    resp = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": other.raw_token},
    )
    assert resp.json()["attempt"] is None
    assert len(state.db.list_node_attempts(job_id=job.id)) == 1


def test_duplicate_ack_is_idempotent(node_api):
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]
    body = {"attempt_id": attempt["id"], "command_sha256": attempt["command_sha256"]}

    first = client.post("/node-agent/ack", json=body, headers=headers)
    second = client.post("/node-agent/ack", json=body, headers=headers)

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


def test_ack_with_wrong_digest_is_rejected(node_api):
    """INV-NODE-3 fail-closed。"""
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]

    resp = client.post(
        "/node-agent/ack",
        json={"attempt_id": attempt["id"], "command_sha256": command_digest("evil")},
        headers=headers,
    )
    assert resp.status_code == 409
    assert state.db.get_node_attempt(attempt["id"]).acked_at is None


def test_node_cannot_ack_another_nodes_attempt(node_api):
    client, state, enrolled, job = node_api
    other = enroll_node(state.db, server_name="worker-b")
    attempt = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    ).json()["attempt"]

    resp = client.post(
        "/node-agent/ack",
        json={"attempt_id": attempt["id"], "command_sha256": attempt["command_sha256"]},
        headers={"X-Node-Token": other.raw_token},
    )
    assert resp.status_code == 409


def test_terminal_before_ack_is_rejected(node_api):
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]

    resp = client.post(
        "/node-agent/terminal",
        json={"attempt_id": attempt["id"], "exit_code": 0},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "never acknowledged" in resp.json()["detail"]


def test_conflicting_terminal_retry_does_not_overwrite_first_result(node_api):
    """Only an exact terminal retry is idempotent; conflicts are rejected."""
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]
    client.post(
        "/node-agent/ack",
        json={"attempt_id": attempt["id"], "command_sha256": attempt["command_sha256"]},
        headers=headers,
    )
    client.post(
        "/node-agent/terminal",
        json={"attempt_id": attempt["id"], "exit_code": 0},
        headers=headers,
    )
    again = client.post(
        "/node-agent/terminal",
        json={"attempt_id": attempt["id"], "exit_code": 1},
        headers=headers,
    )

    assert again.status_code == 409
    assert "conflicts" in again.json()["detail"]
    row = state.db.get_node_attempt(attempt["id"])
    assert row.status == "done" and row.exit_code == 0


def test_heartbeat_never_changes_attempt_status(node_api):
    """INV-NODE-4：心跳只是觀測。"""
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]
    before = state.db.get_node_attempt(attempt["id"]).status

    client.post("/node-agent/heartbeat", json={"attempt_id": attempt["id"]}, headers=headers)

    assert state.db.get_node_attempt(attempt["id"]).status == before


def test_heartbeat_cannot_touch_another_nodes_attempt(node_api):
    client, state, enrolled, job = node_api
    other = enroll_node(state.db, server_name="worker-b")
    attempt = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    ).json()["attempt"]
    before = state.db.get_node_attempt(attempt["id"]).last_heartbeat_at

    client.post(
        "/node-agent/heartbeat",
        json={"attempt_id": attempt["id"]},
        headers={"X-Node-Token": other.raw_token},
    )
    assert state.db.get_node_attempt(attempt["id"]).last_heartbeat_at == before


def test_poll_rejects_an_agent_that_still_names_a_job(node_api):
    """DG-NODE-V2 N-1: the agent no longer chooses. A v1 agent still sending
    `job_id` is rejected rather than silently ignored — silence would leave
    someone believing the old behavior still works."""
    client, _state, enrolled, _job = node_api
    resp = client.post(
        "/node-agent/poll",
        json={"job_id": 999999},
        headers={"X-Node-Token": enrolled.raw_token},
    )
    assert resp.status_code == 422


def test_poll_returns_no_work_rather_than_an_error_when_nothing_is_eligible(node_api):
    """No work is a normal answer, not an error."""
    client, state, enrolled, job = node_api
    with state.db.cursor() as cursor:
        cursor.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job.id,))

    resp = client.post(
        "/node-agent/poll", json={}, headers={"X-Node-Token": enrolled.raw_token}
    )
    assert resp.status_code == 200
    assert resp.json()["attempt"] is None


def test_node_listing_never_exposes_credentials(node_api):
    client, _state, enrolled, _job = node_api
    body = client.get("/nodes").json()
    assert body["nodes"]
    for node in body["nodes"]:
        assert "secret_hash" not in node
        assert enrolled.raw_token not in str(node)


# ---------------------------------------------------------------------------
# 5. control-plane 重啟收斂（INV-NODE-5）
# ---------------------------------------------------------------------------


def test_control_plane_restart_preserves_lease_ownership(tmp_path):
    """control plane 重啟＝重新開 DB；lease 歸屬只看持久化的表。"""
    db_path = str(tmp_path / "cp.db")
    db = Database(db_path)
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(
        command="python train.py", type="train", require_tag="node-canary"
    )
    result = lease_job_for_node(
        db,
        node=node,
        job_id=job_id,
        command="python train.py",
        canary_tag="node-canary",
        job_type="train",
        require_tag="node-canary",
    )
    attempt_id = result.attempt.id
    db.ack_node_attempt(attempt_id, node.id)

    reopened = Database(db_path)
    rows = reopened.list_node_attempts(job_id=job_id)
    assert len(rows) == 1
    assert rows[0].id == attempt_id
    assert rows[0].acked_at is not None


def test_credential_survives_control_plane_restart(tmp_path):
    db_path = str(tmp_path / "cp.db")
    db = Database(db_path)
    enrolled = enroll_node(db, server_name="w1")

    reopened = Database(db_path)
    assert authenticate_node(reopened, enrolled.raw_token).id == enrolled.node.id


def test_revocation_survives_restart_and_is_scoped_to_one_node(tmp_path):
    db_path = str(tmp_path / "cp.db")
    db = Database(db_path)
    first = enroll_node(db, server_name="w1")
    second = enroll_node(db, server_name="w2")
    db.revoke_node(first.node.id)

    reopened = Database(db_path)
    with pytest.raises(NodeAuthError):
        authenticate_node(reopened, first.raw_token)
    #: 其他 node 完全不受影響。
    assert authenticate_node(reopened, second.raw_token).id == second.node.id


# ---------------------------------------------------------------------------
# 6. 登錄/撤銷走核准流程（INV-APPROVAL-*，INV-NODE-1）
# ---------------------------------------------------------------------------


def _approve_node(state, approval_id):
    import asyncio

    from app.approvals import approve

    return asyncio.run(
        approve(
            state.db,
            approval_id,
            server_configs=state.server_configs,
            app_state=state,
            audit_path=state.config.audit_path,
        )
    )


@pytest.fixture()
def enroll_ready(api_client):
    from app.config import ServerConfig

    client, main_module = api_client
    state = main_module.app_state
    state.config.node_agent_v1_enabled = True
    state.server_configs["worker-a"] = ServerConfig(
        name="worker-a", host="10.0.0.1", user="w", key="~/.ssh/k"
    )
    return client, state


def test_enroll_request_creates_pending_approval_without_any_secret(enroll_ready):
    client, state = enroll_ready
    resp = client.post("/nodes/enroll-request", json={"server": "worker-a"})
    assert resp.status_code == 200
    approval = state.db.get_approval(resp.json()["id"])
    assert approval.kind == "node_enroll" and approval.status == "pending"
    #: pending 請求裡不得有任何憑證痕跡。
    assert "token" not in str(approval.payload).lower()
    assert state.db.list_nodes() == []


def test_enroll_approval_returns_raw_token_exactly_once(enroll_ready):
    client, state = enroll_ready
    approval_id = client.post(
        "/nodes/enroll-request", json={"server": "worker-a"}
    ).json()["id"]

    result = _approve_node(state, approval_id)

    raw = result["node_token"]
    assert raw.startswith("dcn_")
    #: 憑證本身永不落庫——只有 digest。
    node = state.db.get_node(result["node"].id)
    assert node.secret_hash != raw
    assert raw not in str(node.secret_hash)
    #: 而且它真的可以用來驗證。
    assert authenticate_node(state.db, raw).id == node.id


def test_enroll_rejected_for_unknown_or_disabled_server(enroll_ready):
    from app.approvals import InvalidNodeRequestError, request_node_enroll_approval

    client, state = enroll_ready
    assert client.post("/nodes/enroll-request", json={"server": "nope"}).status_code == 400

    state.server_configs["worker-a"].enabled = False
    with pytest.raises(InvalidNodeRequestError):
        request_node_enroll_approval(
            state.db,
            {"server": "worker-a"},
            config=state.config,
            server_configs=state.server_configs,
        )


def test_second_active_node_for_same_server_is_refused(enroll_ready):
    client, state = enroll_ready
    enroll_node(state.db, server_name="worker-a")
    resp = client.post("/nodes/enroll-request", json={"server": "worker-a"})
    assert resp.status_code == 400
    assert "已經有啟用中的 node" in resp.json()["detail"]


def test_enroll_approval_revalidates_server_still_enabled(enroll_ready):
    client, state = enroll_ready
    approval_id = client.post(
        "/nodes/enroll-request", json={"server": "worker-a"}
    ).json()["id"]
    state.server_configs["worker-a"].enabled = False

    result = _approve_node(state, approval_id)
    assert result["approval"].status == "rejected"
    assert state.db.list_nodes() == []


def test_revoke_flow_disables_exactly_one_node(enroll_ready):
    client, state = enroll_ready
    first = enroll_node(state.db, server_name="worker-a")
    second = enroll_node(state.db, server_name="worker-b")

    approval_id = client.post(
        "/nodes/revoke-request", json={"node_id": first.node.id}
    ).json()["id"]
    result = _approve_node(state, approval_id)

    assert result["approval"].status == "approved"
    with pytest.raises(NodeAuthError):
        authenticate_node(state.db, first.raw_token)
    assert authenticate_node(state.db, second.raw_token).id == second.node.id


def test_routine_retirement_is_approved_drain_then_active_zero_completion(
    enroll_ready,
):
    client, state = enroll_ready
    enrolled = enroll_node(state.db, server_name="worker-a")

    drain_request = client.post(
        "/nodes/retire-request",
        json={"node_id": enrolled.node.id, "action": "start_drain"},
    )
    assert drain_request.status_code == 200
    drained = _approve_node(state, drain_request.json()["id"])
    assert drained["approval"].status == "approved"
    assert drained["node"].is_active is True
    assert drained["node"].is_draining is True
    # Drain closes new assignment, not protocol access.
    assert authenticate_node(state.db, enrolled.raw_token).id == enrolled.node.id

    complete_request = client.post(
        "/nodes/retire-request",
        json={
            "node_id": enrolled.node.id,
            "action": "complete_retirement",
        },
    )
    assert complete_request.status_code == 200
    completed = _approve_node(state, complete_request.json()["id"])

    assert completed["approval"].status == "approved"
    assert completed["node"].status == "retired"
    assert completed["node"].retired_at is not None
    assert completed["node"].revoked_at is None
    with pytest.raises(NodeAuthError):
        authenticate_node(state.db, enrolled.raw_token)


def test_retirement_revalidates_active_zero_at_approval_time(enroll_ready):
    client, state = enroll_ready
    enrolled = enroll_node(state.db, server_name="worker-a")
    drain_id = client.post(
        "/nodes/retire-request",
        json={"node_id": enrolled.node.id, "action": "start_drain"},
    ).json()["id"]
    assert _approve_node(state, drain_id)["approval"].status == "approved"

    complete_id = client.post(
        "/nodes/retire-request",
        json={
            "node_id": enrolled.node.id,
            "action": "complete_retirement",
        },
    ).json()["id"]
    job_id = state.db.insert_job(command="echo still-owned")
    state.db.insert_node_attempt(
        attempt_id="retirement-race-attempt",
        job_id=job_id,
        node_id=enrolled.node.id,
        command_sha256=command_digest("echo still-owned"),
        lease_expires_at="2099-01-01T00:00:00Z",
    )

    rejected = _approve_node(state, complete_id)

    assert rejected["approval"].status == "rejected"
    assert "active=0" in rejected["approval"].note
    node = state.db.get_node(enrolled.node.id)
    assert node.is_active is True
    assert node.is_draining is True
    assert node.retired_at is None


def test_node_kinds_are_fail_closed_when_flag_disabled(enroll_ready):
    from app.approvals import NodeAgentDisabledError

    client, state = enroll_ready
    approval_id = client.post(
        "/nodes/enroll-request", json={"server": "worker-a"}
    ).json()["id"]
    state.config.node_agent_v1_enabled = False

    with pytest.raises(NodeAgentDisabledError):
        _approve_node(state, approval_id)
    assert state.db.get_approval(approval_id).status == "pending"
    assert state.db.list_nodes() == []


@pytest.mark.parametrize("kind", ["node_enroll", "node_revoke"])
def test_node_kinds_are_never_auto_approved(enroll_ready, kind):
    """INV-APPROVAL-4：自動核准白名單恰好 enqueue|stop，不因 C2 擴大。"""
    import asyncio

    from app.approvals import maybe_auto_approve

    client, state = enroll_ready
    if kind == "node_enroll":
        approval_id = client.post(
            "/nodes/enroll-request", json={"server": "worker-a"}
        ).json()["id"]
    else:
        node = enroll_node(state.db, server_name="worker-b").node
        approval_id = client.post(
            "/nodes/revoke-request", json={"node_id": node.id}
        ).json()["id"]

    decided = asyncio.run(
        maybe_auto_approve(
            state.db,
            state.db.get_approval(approval_id),
            source="api",
            rules=[{"kind": "any", "action": "approve"}],
            server_configs=state.server_configs,
            app_state=state,
            audit_path=state.config.audit_path,
        )
    )
    assert decided is None
    assert state.db.get_approval(approval_id).status == "pending"


def test_operator_node_routes_404_when_flag_disabled(api_client):
    client, main_module = api_client
    main_module.app_state.config.node_agent_v1_enabled = False
    assert client.get("/nodes").status_code == 404
    assert client.post("/nodes/enroll-request", json={"server": "x"}).status_code == 404
    assert client.post("/nodes/revoke-request", json={"node_id": "x"}).status_code == 404
    assert (
        client.post(
            "/nodes/retire-request",
            json={"node_id": "x", "action": "start_drain"},
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 7. stop-request 的端點與 agent 端（roadmap Phase 3 協議項目）
# ---------------------------------------------------------------------------


def test_poll_surfaces_an_approved_stop_request(node_api):
    from app.node_registry import request_job_stop

    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    first = client.post("/node-agent/poll", json={}, headers=headers)
    assert first.json()["stop_requested"] is False

    request_job_stop(state.db, job.id)

    again = client.post("/node-agent/poll", json={}, headers=headers)
    assert again.json()["stop_requested"] is True
    #: 停止請求不會憑空產生第二個 attempt。
    assert len(state.db.list_node_attempts(job_id=job.id)) == 1


def test_heartbeat_is_the_second_delivery_path_for_stop(node_api):
    """長時間執行的任務不會在兩次 poll 之間錯過停止請求。"""
    from app.node_registry import request_job_stop

    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]

    quiet = client.post(
        "/node-agent/heartbeat", json={"attempt_id": attempt["id"]}, headers=headers
    )
    assert quiet.json()["stop_requested"] is False

    request_job_stop(state.db, job.id)
    noticed = client.post(
        "/node-agent/heartbeat", json={"attempt_id": attempt["id"]}, headers=headers
    )
    assert noticed.json()["stop_requested"] is True


def test_heartbeat_does_not_leak_stop_state_across_nodes(node_api):
    from app.node_registry import request_job_stop

    client, state, enrolled, job = node_api
    other = enroll_node(state.db, server_name="worker-b")
    attempt = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    ).json()["attempt"]
    request_job_stop(state.db, job.id)

    resp = client.post(
        "/node-agent/heartbeat",
        json={"attempt_id": attempt["id"]},
        headers={"X-Node-Token": other.raw_token},
    )
    assert resp.json()["stop_requested"] is False


def test_stop_ack_endpoint_is_a_receipt_not_a_state_change(node_api):
    from app.node_registry import request_job_stop

    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]
    request_job_stop(state.db, job.id)

    resp = client.post(
        "/node-agent/stop-ack", json={"attempt_id": attempt["id"]}, headers=headers
    )
    assert resp.json()["acked"] is True
    row = state.db.get_node_attempt(attempt["id"])
    assert row.stop_acked_at is not None
    #: 回執不讓任務收斂——仍要等 agent 停完並回報終態。
    assert row.terminal_at is None and row.status != "done"


def test_stop_ack_from_another_node_is_refused_at_the_endpoint(node_api):
    from app.node_registry import request_job_stop

    client, state, enrolled, job = node_api
    other = enroll_node(state.db, server_name="worker-b")
    attempt = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    ).json()["attempt"]
    request_job_stop(state.db, job.id)

    resp = client.post(
        "/node-agent/stop-ack",
        json={"attempt_id": attempt["id"]},
        headers={"X-Node-Token": other.raw_token},
    )
    assert resp.json()["acked"] is False


def test_stopped_attempt_converges_only_via_terminal_report(node_api):
    """完整停止流程：請求 → 送達 → 回執 → agent 停完 → 回報終態才收斂。"""
    from app.node_registry import request_job_stop

    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]
    client.post(
        "/node-agent/ack",
        json={"attempt_id": attempt["id"], "command_sha256": attempt["command_sha256"]},
        headers=headers,
    )
    request_job_stop(state.db, job.id)
    client.post("/node-agent/stop-ack", json={"attempt_id": attempt["id"]}, headers=headers)

    assert state.db.get_node_attempt(attempt["id"]).terminal_at is None

    client.post(
        "/node-agent/terminal",
        json={"attempt_id": attempt["id"], "exit_code": 143, "log_tail": "stopped"},
        headers=headers,
    )
    row = state.db.get_node_attempt(attempt["id"])
    assert row.status == "failed" and row.exit_code == 143


def test_client_poll_parses_stop_requested_flag():
    body = {
        "attempt": {
            "id": "a-1",
            "job_id": 7,
            "command": "python train.py",
            "command_sha256": command_digest("python train.py"),
            "lease_expires_at": "",
            "status": "leased",
        },
        "reused": True,
        "stop_requested": True,
    }
    client, _ = _client({"/node-agent/poll": (200, body)})
    assert client.poll(7).stop_requested is True


def test_client_heartbeat_returns_stop_flag():
    client, _ = _client(
        {"/node-agent/heartbeat": (200, {"ok": True, "stop_requested": True})}
    )
    assert client.heartbeat("a-1") is True


def test_client_acknowledge_stop_sends_receipt():
    client, transport = _client({"/node-agent/stop-ack": (200, {"acked": True})})
    assert client.acknowledge_stop("a-1") is True
    assert transport.calls[0][1] == "/node-agent/stop-ack"
    assert transport.calls[0][2] == {"attempt_id": "a-1"}


# ---------------------------------------------------------------------------
# 8. artifact-metadata 協議（roadmap Phase 3 最後一個協議項目）
# ---------------------------------------------------------------------------


def _acked_attempt(client, state, enrolled, job):
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]
    client.post(
        "/node-agent/ack",
        json={"attempt_id": attempt["id"], "command_sha256": attempt["command_sha256"]},
        headers=headers,
    )
    return headers, attempt


_SENTINEL = object()


def _artifact(path="out/model.pt", size=10, digest=_SENTINEL):
    #: 不能寫 `digest or default`——空字串是**要測的輸入**，不是「沒給」。
    return {
        "path": path,
        "size_bytes": size,
        "sha256": ("a" * 64) if digest is _SENTINEL else digest,
    }


def test_artifact_metadata_is_recorded(node_api):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)

    resp = client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact()]},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["recorded"] == 1

    rows = state.db.list_node_attempt_artifacts(attempt["id"])
    assert rows[0]["relative_path"] == "out/model.pt"
    assert rows[0]["sha256"] == "a" * 64


def test_artifact_report_transfers_no_file_content(node_api):
    """語意保證：這個端點沒有任何檔案內容欄位。"""
    from app.main import NodeArtifactEntry

    fields = set(NodeArtifactEntry.model_fields)
    assert fields == {"path", "size_bytes", "sha256"}
    assert not any("content" in f or "data" in f or "body" in f for f in fields)


def test_artifact_report_is_idempotent(node_api):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)
    body = {"attempt_id": attempt["id"], "artifacts": [_artifact()]}

    client.post("/node-agent/artifacts", json=body, headers=headers)
    client.post("/node-agent/artifacts", json=body, headers=headers)

    assert len(state.db.list_node_attempt_artifacts(attempt["id"])) == 1


def test_artifact_resend_updates_rather_than_duplicates(node_api):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)

    client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact(size=10)]},
        headers=headers,
    )
    client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact(size=99)]},
        headers=headers,
    )

    rows = state.db.list_node_attempt_artifacts(attempt["id"])
    assert len(rows) == 1 and rows[0]["size_bytes"] == 99


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.txt",
        "a/../../etc/passwd",
        "/absolute/path",
        "trailing/",
        "double//slash",
        "with\x00nul",
        "with\nnewline",
        "back\\slash",
        "",
        ".",
        "..",
    ],
)
def test_artifact_path_traversal_and_junk_are_rejected(node_api, bad_path):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)

    resp = client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact(path=bad_path)]},
        headers=headers,
    )
    assert resp.status_code in (400, 422)
    assert state.db.list_node_attempt_artifacts(attempt["id"]) == []


@pytest.mark.parametrize("bad_digest", ["", "abc", "z" * 64, "A" * 63, "a" * 65])
def test_artifact_digest_must_be_hex_sha256(node_api, bad_digest):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)

    resp = client.post(
        "/node-agent/artifacts",
        json={
            "attempt_id": attempt["id"],
            "artifacts": [_artifact(digest=bad_digest)],
        },
        headers=headers,
    )
    assert resp.status_code in (400, 422)


def test_artifact_negative_size_is_rejected(node_api):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)

    resp = client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact(size=-1)]},
        headers=headers,
    )
    assert resp.status_code in (400, 422)


def test_artifact_batch_is_all_or_nothing(node_api):
    """一批裡有一筆壞的就整批拒絕，不做部分寫入。"""
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)

    resp = client.post(
        "/node-agent/artifacts",
        json={
            "attempt_id": attempt["id"],
            "artifacts": [_artifact(path="good.txt"), _artifact(path="../bad")],
        },
        headers=headers,
    )
    assert resp.status_code in (400, 422)
    assert state.db.list_node_attempt_artifacts(attempt["id"]) == []


def test_artifact_report_rejected_before_ack(node_api):
    client, state, enrolled, job = node_api
    headers = {"X-Node-Token": enrolled.raw_token}
    attempt = client.post(
        "/node-agent/poll", json={}, headers=headers
    ).json()["attempt"]

    resp = client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact()]},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "never acknowledged" in resp.json()["detail"]


def test_artifact_report_rejected_from_another_node(node_api):
    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)
    other = enroll_node(state.db, server_name="worker-b")

    resp = client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": [_artifact()]},
        headers={"X-Node-Token": other.raw_token},
    )
    assert resp.status_code == 400
    assert state.db.list_node_attempt_artifacts(attempt["id"]) == []


def test_artifact_batch_size_is_capped(node_api):
    from app.node_protocol import MAX_ARTIFACTS_PER_REPORT

    client, state, enrolled, job = node_api
    headers, attempt = _acked_attempt(client, state, enrolled, job)
    too_many = [_artifact(path=f"f{i}.bin") for i in range(MAX_ARTIFACTS_PER_REPORT + 1)]

    resp = client.post(
        "/node-agent/artifacts",
        json={"attempt_id": attempt["id"], "artifacts": too_many},
        headers=headers,
    )
    assert resp.status_code == 422
    assert state.db.list_node_attempt_artifacts(attempt["id"]) == []


def test_client_report_artifacts_returns_recorded_count():
    client, transport = _client({"/node-agent/artifacts": (200, {"recorded": 2})})
    assert client.report_artifacts("a-1", [_artifact(), _artifact(path="b.txt")]) == 2
    assert transport.calls[0][1] == "/node-agent/artifacts"


def test_rotate_flow_issues_new_credential_and_keeps_identity(enroll_ready):
    """roadmap Phase 3 rotation：核准流程換發憑證，node 身分不變。"""
    client, state = enroll_ready
    original = enroll_node(state.db, server_name="worker-a")

    approval_id = client.post(
        "/nodes/rotate-request", json={"node_id": original.node.id}
    ).json()["id"]
    result = _approve_node(state, approval_id)

    assert result["approval"].status == "approved"
    new_token = result["node_token"]
    assert new_token.startswith("dcn_") and new_token != original.raw_token
    #: 同一個 node 身分，新憑證可用、舊憑證失效。
    assert authenticate_node(state.db, new_token).id == original.node.id
    with pytest.raises(NodeAuthError):
        authenticate_node(state.db, original.raw_token)


def test_rotate_request_refused_for_revoked_node(enroll_ready):
    client, state = enroll_ready
    original = enroll_node(state.db, server_name="worker-a")
    state.db.revoke_node(original.node.id)

    resp = client.post("/nodes/rotate-request", json={"node_id": original.node.id})
    assert resp.status_code == 400
    assert "已撤銷" in resp.json()["detail"]


def test_rotate_route_404s_when_flag_disabled(api_client):
    client, main_module = api_client
    main_module.app_state.config.node_agent_v1_enabled = False
    assert client.post("/nodes/rotate-request", json={"node_id": "x"}).status_code == 404


def test_node_rotate_is_never_auto_approved(enroll_ready):
    import asyncio

    from app.approvals import maybe_auto_approve

    client, state = enroll_ready
    node = enroll_node(state.db, server_name="worker-a").node
    approval_id = client.post(
        "/nodes/rotate-request", json={"node_id": node.id}
    ).json()["id"]

    decided = asyncio.run(
        maybe_auto_approve(
            state.db,
            state.db.get_approval(approval_id),
            source="api",
            rules=[{"kind": "any", "action": "approve"}],
            server_configs=state.server_configs,
            app_state=state,
            audit_path=state.config.audit_path,
        )
    )
    assert decided is None
    assert state.db.get_approval(approval_id).status == "pending"


def test_an_ineligible_job_is_never_selected(node_api):
    """The canary gate holds at the endpoint. Under v2 the agent cannot name a
    job, so this asserts the *selector* refuses one rather than that a named
    job was rejected."""
    client, state, enrolled, job = node_api
    # Remove the eligible fixture job so only an ineligible one remains.
    with state.db.cursor() as cursor:
        cursor.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job.id,))
    state.db.insert_job(command="python prod.py", type="train", require_tag="production")

    resp = client.post(
        "/node-agent/poll", json={}, headers={"X-Node-Token": enrolled.raw_token}
    )

    assert resp.status_code == 200
    assert resp.json()["attempt"] is None


def test_no_job_is_eligible_when_canary_tag_unset(node_api):
    """預設（未設定標籤）時連被標記的 job 也領不走——第三道煞車。"""
    client, state, enrolled, job = node_api
    state.config.node_canary_require_tag = ""

    resp = client.post(
        "/node-agent/poll",
        json={},
        headers={"X-Node-Token": enrolled.raw_token},
    )
    assert resp.json()["attempt"] is None
    assert state.db.list_node_attempts(job_id=job.id) == []
