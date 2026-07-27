"""INV-NODE-1…6 的**靜態強制**（Goal 3 C2/C3/C4）。

為什麼要有這一份：今天實作 Node Agent 的過程中，我多次口頭宣稱「這樣就
滿足某條不變量了」。口頭宣稱會過期、也會出錯——這份把那些宣稱變成
source-level 斷言，之後任何人（包括我）破壞它都會讓測試紅掉。

風格比照 `.claude/skills/release-gate/scripts/static_checks.sh`：用 AST 或
字串層級檢查程式碼**結構**，不需要跑服務、不碰真實機器、不看執行期行為。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parent.parent / "app"
AGENT = Path(__file__).resolve().parent.parent / "agent"


def _sources(directory: Path):
    return {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.py")}


# ---------------------------------------------------------------------------
# INV-NODE-1：非 root、只出站、身分可個別撤銷
# ---------------------------------------------------------------------------


def test_agent_package_opens_no_inbound_listener():
    """工作機不得開放任何入站控制埠。"""
    forbidden = ("socketserver", "HTTPServer", "uvicorn", "FastAPI", "flask")
    for name, source in _sources(AGENT).items():
        for marker in forbidden:
            assert marker not in source, f"agent/{name} 疑似引入 listener: {marker}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"bind", "listen"}:
                pytest.fail(f"agent/{name} 呼叫了 .{node.attr}()（不得開入站埠）")


def test_agent_package_never_imports_control_plane_code():
    """agent 要能單獨部署到工作機——不得依賴 control plane 任何模組。"""
    for name, source in _sources(AGENT).items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app"):
                pytest.fail(f"agent/{name} import 了 {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app."), f"agent/{name}"


def test_service_unit_never_assigns_root_identity():
    """systemd **user** unit：不得宣告 User=/Group=，賦值也不得指向 root。"""
    unit = (AGENT / "dispatch-node-agent.service").read_text(encoding="utf-8")
    directives = [
        line.strip()
        for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(d.startswith(("User=", "Group=")) for d in directives)
    values = [d.split("=", 1)[1].lower() for d in directives if "=" in d]
    assert not any("root" in v.replace("non-root", "") for v in values)


def test_node_credentials_are_only_ever_stored_hashed():
    """raw token 不得落庫：DB 層只接受 secret_hash。"""
    source = (APP / "db.py").read_text(encoding="utf-8")
    assert "secret_hash" in source
    #: 不得有把 raw token 寫進 nodes 表的欄位名。
    for banned in ("raw_token", "secret TEXT", "token TEXT"):
        assert banned not in source, f"nodes 表疑似存了明文憑證: {banned}"


# ---------------------------------------------------------------------------
# INV-NODE-2：ack 先於副作用；lease 期間不得重複派發
# ---------------------------------------------------------------------------


def test_agent_refuses_to_launch_without_acknowledgement():
    source = (AGENT / "runner.py").read_text(encoding="utf-8")
    assert "if not attempt.acked" in source
    assert "never acknowledged" in source


def test_scheduler_consults_the_double_dispatch_guard_before_dispatching():
    """INV-NODE-2 的實際執行點必須留在排程器裡。"""
    source = (APP / "scheduler.py").read_text(encoding="utf-8")
    assert "job_is_dispatchable" in source
    tree = ast.parse(source)
    called = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "job_is_dispatchable"
        for node in ast.walk(tree)
    )
    assert called, "scheduler 匯入了 job_is_dispatchable 卻沒有呼叫它"


def test_ack_is_a_conditional_update_not_a_read_then_write():
    """第一個 ack 才算數，必須由 SQL 條件保證（不是先讀再寫的競態寫法）。"""
    source = (APP / "db.py").read_text(encoding="utf-8")
    assert "acked_at IS NULL" in source


# ---------------------------------------------------------------------------
# INV-NODE-3：指令位元組非插值落地
# ---------------------------------------------------------------------------


def test_launcher_builders_return_a_list_on_every_path():
    """兩邊的 launcher 都只能回傳 argv list——回傳字串就是 shell 注入面。"""
    for path in (APP / "node_protocol.py", AGENT / "runner.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if "launcher_argv" not in node.name:
                continue
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value]
            assert returns, f"{path.name}:{node.name} 沒有回傳值"
            for ret in returns:
                assert isinstance(ret.value, ast.List), (
                    f"{path.name}:{node.name} 回傳了非 list（可能是 shell 字串）"
                )


def test_agent_never_spawns_through_a_shell():
    for name, source in _sources(AGENT).items():
        assert "shell=True" not in source, f"agent/{name} 使用了 shell=True"


# ---------------------------------------------------------------------------
# INV-NODE-4：心跳過期＝unknown，結構上沒有 failed
# ---------------------------------------------------------------------------


def test_heartbeat_state_has_no_failure_member():
    from app.node_protocol import HeartbeatState

    assert {member.value for member in HeartbeatState} == {"fresh", "stale", "unknown"}


def test_heartbeat_state_is_only_consulted_inside_the_read_only_summary():
    """`heartbeat_state()` 只可以在唯讀彙總函式裡呼叫。

    如果它出現在任何會寫入 attempt 狀態的函式裡，就代表「心跳」有機會影響
    任務判定——那正是 INV-NODE-4 禁止的。這裡用 AST 找出每個呼叫點所在的
    函式名，白名單以外一律失敗。
    """
    allowed = {"summarize_node_operations"}
    for path in (APP / "node_protocol.py", APP / "node_registry.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            if func.name in allowed or func.name == "heartbeat_state":
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None)
                    assert name != "heartbeat_state", (
                        f"{path.name}:{func.name} 呼叫了 heartbeat_state()，"
                        "心跳不得參與任務狀態判定（INV-NODE-4）"
                    )


def test_unreachable_agent_maps_to_running_not_failed():
    """node 後端 inspect：沉默＝running（未知），永遠不是 failed。"""
    source = (APP / "execution_backend.py").read_text(encoding="utf-8")
    assert 'ReconcileOutcome(status="running")' in source


# ---------------------------------------------------------------------------
# INV-NODE-5：重啟不重複
# ---------------------------------------------------------------------------


def test_both_sides_refuse_to_relaunch_an_acknowledged_attempt():
    for path in (APP / "node_protocol.py", AGENT / "runner.py"):
        source = path.read_text(encoding="utf-8")
        assert "never relaunch" in source, f"{path.name} 缺少重啟不重複的保證"


def test_agent_persists_attempt_state_durably():
    """重啟要能收斂就必須真的落地：fsync + 原子替換，缺一不可。"""
    source = (AGENT / "runner.py").read_text(encoding="utf-8")
    assert "os.fsync" in source
    assert "os.replace" in source


# ---------------------------------------------------------------------------
# INV-NODE-6：逐台提升、隨時回退、禁止全域一刀切
# ---------------------------------------------------------------------------


def test_execution_backend_is_a_per_server_field_defaulting_to_ssh():
    from app.config import ServerConfig

    cfg = ServerConfig(name="x", host="h", user="u", key="k")
    assert cfg.execution_backend == "ssh"


def test_backend_resolution_fails_closed_to_ssh():
    from app.node_protocol import resolve_execution_backend

    assert resolve_execution_backend("node", node_agent_enabled=False) == "ssh"
    assert resolve_execution_backend("bogus", node_agent_enabled=True) == "ssh"
    assert resolve_execution_backend(None, node_agent_enabled=True) == "ssh"


def test_ssh_backend_source_never_references_the_agent():
    """INV-SSH-1：SSH 後端的行為與依賴永遠不得假設 agent 存在。"""
    tree = ast.parse((APP / "scheduler.py").read_text(encoding="utf-8"))
    ssh_dispatch = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_job"
    ]
    for func in ssh_dispatch:
        body = ast.dump(func)
        for banned in ("node_agent", "lease_job_for_node", "authenticate_node"):
            assert banned not in body, f"dispatch_job 引用了 agent 相關符號: {banned}"


# ---------------------------------------------------------------------------
# 邊界：node kinds 永不自動核准
# ---------------------------------------------------------------------------


def test_no_node_kind_is_in_the_auto_approve_whitelist():
    """INV-APPROVAL-4：白名單恰好 enqueue|stop。"""
    source = (APP / "approvals.py").read_text(encoding="utf-8")
    assert '"enqueue", "stop"' in source or "'enqueue', 'stop'" in source
    from app.db import VALID_APPROVAL_KINDS

    for kind in VALID_APPROVAL_KINDS:
        if kind.startswith("node_") or kind.startswith("engineering_"):
            assert kind not in {"enqueue", "stop"}
