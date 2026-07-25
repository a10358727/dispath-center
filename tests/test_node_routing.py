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
    lease_job_for_node(db, node=node, job_id=job_id, command="python train.py")

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert ssh.calls == []


def test_acked_node_attempt_blocks_ssh_dispatch_indefinitely(tmp_path):
    """已 ack ⇒ 可能已經動手 ⇒ 就算失聯也不得從 SSH 重跑（INV-NODE-4）。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    result = lease_job_for_node(db, node=node, job_id=job_id, command="python train.py")
    db.ack_node_attempt(result.attempt.id, node.id)

    ssh = _FakeSSH()
    _tick(db, configs, states, ssh, node_agent_enabled=True)

    assert db.get_job(job_id).status == "queued"
    assert ssh.calls == []


def test_terminal_node_attempt_releases_the_job_for_ssh(tmp_path):
    """attempt 收斂成終態之後，這個 job 又可以正常被派了。"""
    db, configs, states, job_id = _setup(tmp_path, backend="ssh")
    node = enroll_node(db, server_name="other").node
    result = lease_job_for_node(db, node=node, job_id=job_id, command="python train.py")
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
    lease_job_for_node(db, node=node, job_id=job_id, command="python train.py")
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
    job = db.get_job(db.insert_job(command="x", type="train"))
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
    job_id = db.insert_job(command="x", type="train")
    result = lease_job_for_node(db, node=node, job_id=job_id, command="x")
    db.update_node_attempt(
        result.attempt.id, status=status, exit_code=exit_code, log_tail="tail"
    )

    outcome = asyncio.run(_backend_with(db).inspect("w1", job_id))
    assert outcome.status == expected


def test_node_backend_inspect_never_reports_failed_for_silence(tmp_path):
    """INV-NODE-4：agent 失聯 ⇒ running（未知），永遠不是 failed。"""
    db = Database(str(tmp_path / "t.db"))
    node = enroll_node(db, server_name="w1").node
    job_id = db.insert_job(command="x", type="train")
    result = lease_job_for_node(db, node=node, job_id=job_id, command="x")
    db.ack_node_attempt(result.attempt.id, node.id)
    #: 之後完全沒有心跳、沒有終態。

    outcome = asyncio.run(_backend_with(db).inspect("w1", job_id))
    assert outcome.status == "running"


@pytest.mark.parametrize("method", ["stop", "collect"])
def test_node_backend_refuses_to_pretend_for_unimplemented_c4_actions(tmp_path, method):
    """誠實 fail：停止請求與結果上傳是 C4，這裡不假裝做得到。"""
    db = Database(str(tmp_path / "t.db"))
    backend = _backend_with(db)
    with pytest.raises(NotImplementedError):
        if method == "stop":
            asyncio.run(backend.stop("w1", 1))
        else:
            asyncio.run(
                backend.collect(
                    1, ServerConfig(name="w1", host="h", user="u", key="k"), ".", 60.0
                )
            )
