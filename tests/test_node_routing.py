"""Goal 3 C3（程式碼部分）：per-node 執行通道路由與 SSH 重複派發防護。

對應 `INV-NODE-2`（lease 期間不得再派給任何通道，含 SSH）與 `INV-NODE-6`
（逐台提升、隨時回退、禁止全域一刀切）。

全部用 FakeSSH 與暫時 DB，不碰真實工作機（INV-TEST-2）。

**本檔不測 canary**：≥100 jobs／≥2 nodes／7 天零重複啟動的門檻是操作行為，
需要真實第二台機器與時間窗，不可能在單元測試裡成立——這裡只證明「程式碼
層的重複派發防護與逐台回退是對的」。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import ServerConfig
from app.db import Database
from app.execution_backend import NodeExecutionBackend
from app.monitor import ServerState
from app.node_protocol import VALID_EXECUTION_BACKENDS, resolve_execution_backend
from app.node_registry import enroll_node, lease_job_for_node
from app.scheduler import scheduler_tick


#: roadmap Phase 3 的 canary 標籤。測試裡凡是「應該可以被 node 領走」的
#: job 都必須明確帶上它——這正是閘門要求操作者做的指定動作。
CANARY_TAG = "node-canary"


def _eligible_lease(db, node, job_id, command):
    """把 canary 資格參數補齊的 lease（測試專用便利函式）。"""
    return lease_job_for_node(
        db,
        node=node,
        job_id=job_id,
        command=command,
        canary_tag=CANARY_TAG,
        job_type="train",
        require_tag=CANARY_TAG,
    )


# ---------------------------------------------------------------------------
# 1. resolve_execution_backend()：fail-closed 到 ssh（INV-NODE-6）
# ---------------------------------------------------------------------------


def test_ssh_is_always_valid_and_is_the_default():
    assert "ssh" in VALID_EXECUTION_BACKENDS
    assert ServerConfig(name="a", host="h", user="u", key="k").execution_backend == "ssh"


def test_node_backend_requires_the_global_flag():
    """旗標關閉時 per-node 設定完全無效——回退永遠是安全的方向。"""
    assert resolve_execution_backend("node", node_agent_enabled=False) == "ssh"
    assert resolve_execution_backend("node", node_agent_enabled=True) == "node"


@pytest.mark.parametrize(
    "configured", [None, "", "  ", "sshh", "agent", 123, [], "node;rm", "no de"]
)
def test_invalid_or_missing_backend_values_fail_closed_to_ssh(configured):
    assert resolve_execution_backend(configured, node_agent_enabled=True) == "ssh"


def test_backend_value_is_case_and_space_insensitive_for_valid_values():
    assert resolve_execution_backend("  NODE  ", node_agent_enabled=True) == "node"
    assert resolve_execution_backend("SSH", node_agent_enabled=True) == "ssh"


def test_backend_is_per_machine_not_global():
    """INV-NODE-6 禁止全域一刀切：同一個旗標下，兩台機器可以走不同通道。"""
    assert resolve_execution_backend("node", node_agent_enabled=True) == "node"
    assert resolve_execution_backend("ssh", node_agent_enabled=True) == "ssh"


def test_servers_yaml_round_trips_the_field(tmp_path):
    from app.config import load_servers_yaml

    path = tmp_path / "servers.yaml"
    path.write_text(
        "servers:\n"
        "  - name: a\n    host: h\n    user: u\n    key: k\n"
        "    execution_backend: node\n"
        "  - name: b\n    host: h\n    user: u\n    key: k\n",
        encoding="utf-8",
    )
    loaded = {s.name: s.execution_backend for s in load_servers_yaml(path)}
    #: 沒寫這個欄位的舊設定檔一律是 ssh（向下相容）。
    assert loaded == {"a": "node", "b": "ssh"}


# ---------------------------------------------------------------------------
# 2. scheduler_tick 的 per-node 路由（INV-NODE-6）
# ---------------------------------------------------------------------------


class _FakeSSH:
    """記錄所有 SSH 呼叫；任何真的派工都會在這裡留下痕跡。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def run(self, server, command, timeout=None):
        self.calls.append((server, command))

        class _R:
            ok, stdout, stderr, exit_code = True, "", "", 0

        return _R()

    async def write_file(self, server, path, content):
        self.calls.append((server, f"WRITE {path}"))


def _tick(db, configs, states, ssh, *, node_agent_enabled):
    asyncio.run(
        scheduler_tick(
            db,
            states,
            configs,
            ssh.run,
            ssh.write_file,
            audit_path="/dev/null",
            node_agent_enabled=node_agent_enabled,
        )
    )


def _setup(tmp_path, backend="ssh"):
    db = Database(str(tmp_path / "t.db"))
    cfg = ServerConfig(
        name="w1", host="h", user="u", key="k", execution_backend=backend
    )
    states = {"w1": ServerState(name="w1", online=True, load1=0.0)}
    job_id = db.insert_job(command="python train.py", type="train")
    return db, {"w1": cfg}, states, job_id


def test_ssh_backed_server_still_gets_dispatched(tmp_path):
    """預設路徑完全不變。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "running"
    assert ssh.calls


def test_node_backed_server_is_not_ssh_dispatched(tmp_path):
    """INV-NODE-6：提升為 node 的機器，排程器不主動 SSH 派工。"""
    db, configs, states, job_id = _setup(tmp_path, backend="node")
    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert ssh.calls == []


def test_node_backed_server_falls_back_to_ssh_when_flag_off(tmp_path):
    """全域旗標關閉＝完整回退，per-node 設定無效。"""
    db, configs, states, job_id = _setup(tmp_path, backend="node")
    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=False)

    assert db.get_job(job_id).status == "running"
    assert ssh.calls


def test_reverting_the_field_restores_ssh_dispatch_with_no_migration(tmp_path):
    """INV-NODE-6：回退不需要資料遷移——改回 ssh 下一輪就恢復派工。"""
    db, configs, states, job_id = _setup(tmp_path, backend="node")
    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)
    assert db.get_job(job_id).status == "queued"

    configs["w1"].execution_backend = "ssh"
    _tick(db, configs, states, ssh, node_agent_enabled=True)
    assert db.get_job(job_id).status == "running"


def test_one_node_backed_machine_does_not_affect_another(tmp_path):
    """逐台提升：w1 走 node、w2 照常走 SSH，互不影響。"""
    db = Database(str(tmp_path / "t.db"))
    configs = {
        "w1": ServerConfig(
            name="w1", host="h", user="u", key="k", execution_backend="node"
        ),
        "w2": ServerConfig(name="w2", host="h", user="u", key="k"),
    }
    states = {
        "w1": ServerState(name="w1", online=True, load1=0.0),
        "w2": ServerState(name="w2", online=True, load1=0.0),
    }
    job_id = db.insert_job(command="python train.py", type="train")
    ssh = _FakeSSH()

    _tick(db, configs, states, ssh, node_agent_enabled=True)

    job = db.get_job(job_id)
    assert job.status == "running" and job.server == "w2"


# ---------------------------------------------------------------------------
# 3. INV-NODE-2：node 已 lease/ack 的 job 不得再從 SSH 派一次
# ---------------------------------------------------------------------------


def test_live_node_lease_blocks_ssh_dispatch_of_the_same_job(tmp_path):
    """核心保證：同一份工作不會在 node 與 SSH 兩條通道各跑一次。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    _eligible_lease(db, node, job_id, "python train.py")

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert ssh.calls == []


def test_acked_node_attempt_blocks_ssh_dispatch_indefinitely(tmp_path):
    """已 ack ⇒ 可能已經動手 ⇒ 就算失聯也不得從 SSH 重跑（INV-NODE-4）。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    result = _eligible_lease(db, node, job_id, "python train.py")
    db.ack_node_attempt(result.attempt.id, node.id)

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert ssh.calls == []


def test_terminal_node_attempt_releases_the_job_for_ssh(tmp_path):
    """attempt 收斂成終態之後，這個 job 又可以正常被派了。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    result = _eligible_lease(db, node, job_id, "python train.py")
    db.ack_node_attempt(result.attempt.id, node.id)
    db.update_node_attempt(
        result.attempt.id, status="failed", terminal_at="2026-07-25T12:00:00+00:00"
    )

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "running"


def test_guard_is_a_noop_when_no_nodes_exist(tmp_path):
    """沒有任何 node 時這個檢查恆為 True——對現行部署零行為變更。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)
    assert db.get_job(job_id).status == "running"


def test_blocked_job_does_not_starve_other_work(tmp_path):
    """被 node lease 卡住的 job 不得擋住同一輪其他可派的工作。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    _eligible_lease(db, node, job_id, "python train.py")
    second = db.insert_job(command="python other.py", type="train")

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert db.get_job(second).status == "running"


# ---------------------------------------------------------------------------
# 4. NodeExecutionBackend 合約（C1 seam 的第二個實作）
# ---------------------------------------------------------------------------


def _backend_with(db):
    return NodeExecutionBackend(db=db)


def test_node_backend_prepare_and_launch_are_deliberate_noops(tmp_path):
    """control plane 從不主動連線工作機——這是語意，不是缺實作。"""
    db = Database(str(tmp_path / "t.db"))
    backend = _backend_with(db)
    job = db.get_job(db.insert_job(command="x", type="train", require_tag=CANARY_TAG))
    assert asyncio.run(backend.prepare("w1", job)) is None
    assert asyncio.run(backend.launch("w1", job)) is None
    assert asyncio.run(backend.cleanup("w1", job.id)) is None


def test_node_backend_inspect_reports_running_when_no_attempt(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    outcome = asyncio.run(_backend_with(db).inspect("w1", 1))
    assert outcome.status == "running"


@pytest.mark.parametrize(
    "status,exit_code,expected",
    [("done", 0, "done"), ("failed", 1, "failed"), ("expired", None, "requeued")],
)
def test_node_backend_inspect_maps_terminal_states(tmp_path, status, exit_code, expected):
    db = Database(str(tmp_path / "t.db"))
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(command="x", type="train", require_tag=CANARY_TAG)
    result = _eligible_lease(db, node, job_id, "x")
    db.update_node_attempt(
        result.attempt.id, status=status, exit_code=exit_code, log_tail="tail"
    )

    outcome = asyncio.run(_backend_with(db).inspect("w1", job_id))
    assert outcome.status == expected


def test_node_backend_inspect_never_reports_failed_for_silence(tmp_path):
    """INV-NODE-4：agent 失聯 ⇒ running（未知），永遠不是 failed。"""
    db = Database(str(tmp_path / "t.db"))
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(command="x", type="train", require_tag=CANARY_TAG)
    result = _eligible_lease(db, node, job_id, "x")
    db.ack_node_attempt(result.attempt.id, node.id)
    #: 之後完全沒有心跳、沒有終態。

    outcome = asyncio.run(_backend_with(db).inspect("w1", job_id))
    assert outcome.status == "running"


def test_node_backend_refuses_to_pretend_about_result_collection(tmp_path):
    """誠實 fail：結果回收在 node 通道是 agent 主動上傳（C4），這裡不假裝
    做得到。`stop()` 已經實作（見下方 stop-request 測試），所以不在此列。"""
    db = Database(str(tmp_path / "t.db"))
    with pytest.raises(NotImplementedError):
        asyncio.run(
            _backend_with(db).collect(
                1, ServerConfig(name="w1", host="h", user="u", key="k"), ".", 60.0
            )
        )


# ---------------------------------------------------------------------------
# 5. stop-request 協議（roadmap Phase 3；INV-NODE-4：請求停止 ≠ 已經停止）
# ---------------------------------------------------------------------------


from app.node_protocol import should_agent_stop  # noqa: E402
from app.node_registry import (  # noqa: E402
    acknowledge_stop,
    request_job_stop,
    to_protocol_attempt,
)


def _leased(db, job_command="python train.py"):
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(
        command=job_command, type="train", require_tag=CANARY_TAG
    )
    result = _eligible_lease(db, node, job_id, job_command)
    return node, job_id, result.attempt.id


def test_stop_request_is_recorded_and_visible_to_the_agent(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    _node, job_id, attempt_id = _leased(db)

    assert request_job_stop(db, job_id) == [attempt_id]
    assert should_agent_stop(to_protocol_attempt(db.get_node_attempt(attempt_id)))


def test_stop_request_does_not_change_job_or_attempt_status(tmp_path):
    """INV-NODE-4：control plane 記下請求 ≠ 任務已停。"""
    db = Database(str(tmp_path / "t.db"))
    _node, job_id, attempt_id = _leased(db)
    before = db.get_node_attempt(attempt_id).status

    request_job_stop(db, job_id)

    assert db.get_node_attempt(attempt_id).status == before
    assert db.get_node_attempt(attempt_id).terminal_at is None


def test_stop_request_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    _node, job_id, attempt_id = _leased(db)

    assert request_job_stop(db, job_id) == [attempt_id]
    #: 第二次不重複記錄（已經有請求了）。
    assert request_job_stop(db, job_id) == []


def test_stop_request_skips_terminal_attempts(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    _node, job_id, attempt_id = _leased(db)
    db.update_node_attempt(
        attempt_id, status="done", terminal_at="2026-07-25T12:00:00+00:00"
    )

    assert request_job_stop(db, job_id) == []
    assert should_agent_stop(to_protocol_attempt(db.get_node_attempt(attempt_id))) is False


def test_stop_ack_is_delivery_receipt_only(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    node, job_id, attempt_id = _leased(db)
    request_job_stop(db, job_id)

    assert acknowledge_stop(db, node=node, attempt_id=attempt_id) is True
    row = db.get_node_attempt(attempt_id)
    assert row.stop_acked_at is not None
    #: 回執不讓任務收斂——仍要等 agent 回報終態。
    assert row.terminal_at is None
    #: 重複回執是 no-op。
    assert acknowledge_stop(db, node=node, attempt_id=attempt_id) is False


def test_stop_ack_from_another_node_is_refused(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    _node, job_id, attempt_id = _leased(db)
    other = enroll_node(db, server_name="w2").node
    request_job_stop(db, job_id)

    assert acknowledge_stop(db, node=other, attempt_id=attempt_id) is False
    assert db.get_node_attempt(attempt_id).stop_acked_at is None


def test_node_backend_stop_records_a_request_instead_of_pretending(tmp_path):
    """`NodeExecutionBackend.stop()` 現在是真的——但它記的是請求。"""
    db = Database(str(tmp_path / "t.db"))
    _node, job_id, attempt_id = _leased(db)

    asyncio.run(_backend_with(db).stop("w1", job_id))

    assert db.get_node_attempt(attempt_id).stop_requested_at is not None
    assert db.get_node_attempt(attempt_id).terminal_at is None


def test_node_backend_stop_on_job_without_attempts_is_a_noop(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    job_id = db.insert_job(command="x", type="train", require_tag=CANARY_TAG)
    asyncio.run(_backend_with(db).stop("w1", job_id))  # 不得拋例外
    assert db.list_node_attempts(job_id=job_id) == []


def test_stop_requested_attempt_still_blocks_ssh_dispatch(tmp_path):
    """請求停止期間仍然不得從 SSH 重派——還沒收斂就還沒結束。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    result = _eligible_lease(db, node, job_id, "python train.py")
    db.ack_node_attempt(result.attempt.id, node.id)
    request_job_stop(db, job_id)

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert ssh.calls == []


# ---------------------------------------------------------------------------
# 6. canary 資格閘門（roadmap Phase 3：限定為「指定的非正式普通任務」）
# ---------------------------------------------------------------------------


from app.node_protocol import ORDINARY_JOB_TYPES, is_node_canary_eligible  # noqa: E402


def test_nothing_is_eligible_by_default():
    """第三道煞車：沒設定 canary 標籤時，**任何** job 都不合格。

    光是開 NODE_AGENT_V1_ENABLED 並把機器設成 node，還不足以讓正式工作
    流到未驗證的通道上。
    """
    for tag in (None, "", "   ", 123):
        assert is_node_canary_eligible("train", "anything", canary_tag=tag) is False


def test_eligible_only_with_exact_tag_match():
    assert is_node_canary_eligible("train", "node-canary", canary_tag="node-canary")
    assert is_node_canary_eligible("train", " node-canary ", canary_tag="node-canary")
    #: 近似值不算——必須精確指定。
    assert not is_node_canary_eligible("train", "node-canary-2", canary_tag="node-canary")
    assert not is_node_canary_eligible("train", "canary", canary_tag="node-canary")
    assert not is_node_canary_eligible("train", None, canary_tag="node-canary")
    assert not is_node_canary_eligible("train", "", canary_tag="node-canary")


@pytest.mark.parametrize("job_type", ["coding", "sync", "setup"])
def test_non_ordinary_job_types_are_never_eligible(job_type):
    """roadmap 明文限制在 ordinary jobs；Codex Runner 遷移排到 C4。"""
    assert job_type not in ORDINARY_JOB_TYPES
    assert not is_node_canary_eligible(job_type, "node-canary", canary_tag="node-canary")


@pytest.mark.parametrize("job_type", sorted(ORDINARY_JOB_TYPES))
def test_ordinary_job_types_are_eligible_when_tagged(job_type):
    assert is_node_canary_eligible(job_type, "node-canary", canary_tag="node-canary")


def test_lease_refuses_ineligible_job_before_creating_any_attempt(tmp_path):
    """不合格的 job 連 attempt 都不會被建立。"""
    db = Database(str(tmp_path / "t.db"))
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(command="x", type="train", require_tag="production")

    result = lease_job_for_node(
        db,
        node=node,
        job_id=job_id,
        command="x",
        canary_tag=CANARY_TAG,
        job_type="train",
        require_tag="production",
    )
    assert result.attempt is None
    assert "not node-canary eligible" in result.reason
    assert db.list_node_attempts(job_id=job_id) == []


def test_lease_refuses_coding_job_even_when_tagged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(command="x", type="coding", require_tag=CANARY_TAG)

    result = lease_job_for_node(
        db, node=node, job_id=job_id, command="x",
        canary_tag=CANARY_TAG, job_type="coding", require_tag=CANARY_TAG,
    )
    assert result.attempt is None
    assert db.list_node_attempts(job_id=job_id) == []


def test_lease_without_eligibility_arguments_is_fail_closed(tmp_path):
    """呼叫端沒傳資格資訊時一律不合格——不猜。"""
    db = Database(str(tmp_path / "t.db"))
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(command="x", type="train", require_tag=CANARY_TAG)

    result = lease_job_for_node(db, node=node, job_id=job_id, command="x")
    assert result.attempt is None
    assert db.list_node_attempts(job_id=job_id) == []


# ---------------------------------------------------------------------------
# 7. 憑證換發（roadmap Phase 3 "rotation"）
# ---------------------------------------------------------------------------


from app.node_registry import (  # noqa: E402
    NodeAuthError,
    authenticate_node,
    rotate_node_credential,
)


def test_rotation_keeps_identity_and_invalidates_the_old_secret(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    original = enroll_node(db, server_name="w1")

    rotated = rotate_node_credential(db, original.node.id)

    #: 同一個 node 身分。
    assert rotated.node.id == original.node.id
    #: 新憑證可用。
    assert authenticate_node(db, rotated.raw_token).id == original.node.id
    #: 舊憑證立即失效。
    with pytest.raises(NodeAuthError):
        authenticate_node(db, original.raw_token)


def test_rotation_preserves_attempt_ownership(tmp_path):
    """與「撤銷後重新登錄」的關鍵差別：進行中的 attempt 不會變成沒有主人。"""
    db = Database(str(tmp_path / "t.db"))
    original = enroll_node(db, server_name="w1")
    job_id = db.insert_job(command="x", type="train", require_tag=CANARY_TAG)
    result = _eligible_lease(db, original.node, job_id, "x")
    db.ack_node_attempt(result.attempt.id, original.node.id)

    rotated = rotate_node_credential(db, original.node.id)

    row = db.get_node_attempt(result.attempt.id)
    assert row.node_id == rotated.node.id
    assert row.acked_at is not None


def test_revoked_node_cannot_be_rotated(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    original = enroll_node(db, server_name="w1")
    db.revoke_node(original.node.id)

    assert rotate_node_credential(db, original.node.id) is None


def test_rotating_unknown_node_returns_none(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    assert rotate_node_credential(db, "11111111-1111-4111-8111-111111111111") is None


def test_rotation_does_not_affect_other_nodes(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    first = enroll_node(db, server_name="w1")
    second = enroll_node(db, server_name="w2")

    rotate_node_credential(db, first.node.id)

    assert authenticate_node(db, second.raw_token).id == second.node.id


# ---------------------------------------------------------------------------
# 8. 版本化套件與 service definition（roadmap Phase 3）
# ---------------------------------------------------------------------------


def test_agent_package_is_versioned():
    import agent

    assert isinstance(agent.__version__, str) and agent.__version__
    #: 協議面清單要涵蓋 control plane 實際提供的六個端點。
    assert set(agent.SUPPORTED_PROTOCOL_OPERATIONS) == {
        "poll", "ack", "heartbeat", "terminal", "stop-ack", "artifacts",
    }


def test_agent_client_reports_the_package_version_by_default():
    from agent import __version__
    from agent.client import NodeAgentClient

    calls = []

    def _transport(method, path, json=None, headers=None):
        calls.append(json)
        return 200, {"ok": True}

    NodeAgentClient(_transport, node_token="dcn_x.y").heartbeat()
    assert calls[0]["agent_version"] == __version__


def test_service_definition_is_a_non_root_user_unit():
    """INV-NODE-1：agent 以非 root 執行，且不開任何入站 listener。"""
    from pathlib import Path

    unit = (Path(__file__).parents[1] / "agent" / "dispatch-node-agent.service").read_text()
    #: 只檢查**實際指令**，不檢查註解文字——註解裡合理地會提到 root
    #: （說明 enable-linger 需要 sudo、以及 agent 本身不是 root）。
    directives = [
        line.strip()
        for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    body = "\n".join(directives)

    #: systemd **user** unit 不得宣告 User=/Group=（會直接啟動失敗），
    #: 更不能把身分指成 root。
    assert not any(d.startswith(("User=", "Group=")) for d in directives)
    #: 沒有任何指令把身分或路徑指向 root（`Description=... non-root ...`
    #: 是描述文字，不是身分指派，所以比對的是**賦值右側**）。
    values = [d.split("=", 1)[1].lower() for d in directives if "=" in d]
    assert not any("root" in value.replace("non-root", "") for value in values)
    #: 縱深防禦與出站限制。
    assert "NoNewPrivileges=yes" in body
    assert "ProtectSystem=strict" in body
    assert "RestrictAddressFamilies=" in body
    assert "WantedBy=default.target" in body
    #: 憑證只從 0600 環境檔讀入，不寫在 unit 裡。
    assert any(d.startswith("EnvironmentFile=") for d in directives)
    #: 實際指令裡不得出現任何憑證值。
    assert "dcn_" not in body
