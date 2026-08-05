"""app/agent_tools.py：白名單工具註冊表——agent 的安全邊界（PLAN.md H 節）。

重點：
- 靜態斷言本模組不 import app.sshpool/app.localrun/subprocess（鐵律）。
- 唯讀工具的資料組裝正確（重用既有純函式，不碰即時 SSH）。
- 寫入工具全部只建立 approval，危險指令沿用既有拒絕行為。
- request_rerun_job 只複製派工參數欄位，不複製執行期欄位。
"""

from __future__ import annotations

import ast
import asyncio

import pytest

from app.agent_tools import TOOLS, AgentContext, dispatch_tool
from app.audit import read_audit
from app.config import AppConfig
from app.identity import Actor, ActorType, RequestContext
from app.monitor import GpuReading, ServerState


def make_config(**overrides) -> AppConfig:
    base = dict(servers=[])
    base.update(overrides)
    return AppConfig(**base)


def make_ctx(
    db,
    server_states=None,
    config=None,
    audit_path="audit.jsonl",
    server_configs=None,
    ssh_run=None,
    ssh_run_direct=None,
    request_context=None,
) -> AgentContext:
    return AgentContext(
        db=db,
        server_states=server_states or {},
        config=config or make_config(),
        audit_path=audit_path,
        server_configs=server_configs,
        ssh_run=ssh_run,
        ssh_run_direct=ssh_run_direct,
        request_context=request_context,
    )


# ---------------------------------------------------------------------------
# 鐵律：agent_tools 不得碰 SSH / 本地執行 / subprocess
# ---------------------------------------------------------------------------


def test_agent_tools_does_not_import_ssh_or_subprocess_modules():
    """靜態掃描（`ast`，不是字串比對，避免 docstring 裡提到這些名字誤判）：
    `app/agent_tools.py` 的原始碼裡不能有任何 import app.sshpool／
    app.localrun／subprocess 的陳述式，模組自己的 namespace 也不應該掛著
    這些模組物件、也不能出現 `os.system(...)` 呼叫。"""
    import app.agent_tools as agent_tools_module

    source = open(agent_tools_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)

    forbidden_modules = {"app.sshpool", "app.localrun", "subprocess", "sshpool", "localrun"}
    hit = imported_modules & forbidden_modules
    assert not hit, f"agent_tools.py 不應該 import 這些模組：{hit}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "system":
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                pytest.fail("agent_tools.py 不應該呼叫 os.system(...)")

    for attr_name in ("sshpool", "localrun", "subprocess"):
        assert not hasattr(agent_tools_module, attr_name)


def test_no_approve_or_reject_or_shell_tools_registered():
    forbidden_names = {"approve", "approve_approval", "reject", "reject_approval", "shell", "exec", "run_command"}
    assert forbidden_names.isdisjoint(TOOLS.keys())


# ---------------------------------------------------------------------------
# 唯讀工具
# ---------------------------------------------------------------------------


def test_status_tool_reuses_chat_build_status_reply(db):
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.3)}
    ctx = make_ctx(db, server_states=server_states)
    result = asyncio.run(dispatch_tool("status", {}, ctx))
    assert "server-a" in result


def test_servers_tool_returns_dict_list_without_key_paths(db):
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.3)}
    ctx = make_ctx(db, server_states=server_states)
    result = asyncio.run(dispatch_tool("servers", {}, ctx))
    assert isinstance(result, list)
    assert result[0]["name"] == "server-a"
    assert "key" not in result[0] and "key_path" not in result[0]


def test_jobs_tool_limit_capped_at_20(db):
    for i in range(25):
        db.insert_job(command=f"echo {i}")
    ctx = make_ctx(db)
    result = asyncio.run(dispatch_tool("jobs", {"limit": 999}, ctx))
    assert len(result) == 20


def test_jobs_tool_filters_by_status(db):
    db.insert_job(command="echo 1", status="queued")
    id2 = db.insert_job(command="echo 2", status="queued")
    db.update_job(id2, status="running")
    ctx = make_ctx(db)
    result = asyncio.run(dispatch_tool("jobs", {"status": "running"}, ctx))
    assert len(result) == 1
    assert result[0]["status"] == "running"


def test_job_detail_tool_not_found(db):
    ctx = make_ctx(db)
    result = asyncio.run(dispatch_tool("job_detail", {"job_id": 999}, ctx))
    assert "error" in result


def test_job_detail_tool_found(db):
    job_id = db.insert_job(command="python train.py")
    ctx = make_ctx(db)
    result = asyncio.run(dispatch_tool("job_detail", {"job_id": job_id}, ctx))
    assert result["id"] == job_id
    assert result["command"] == "python train.py"


def test_job_log_tool_only_reads_stored_log_tail_no_live_ssh(db):
    job_id = db.insert_job(command="python train.py")
    db.update_job(job_id, log_tail="\n".join(f"line{i}" for i in range(100)))
    ctx = make_ctx(db)
    result = asyncio.run(dispatch_tool("job_log", {"job_id": job_id, "lines": 200}, ctx))
    # lines 上限 80，即使要求 200 行也只回 80 行
    assert len(result["log_tail"].splitlines()) == 80
    assert result["log_tail"].splitlines()[-1] == "line99"


def test_engineering_owned_jobs_are_redacted_and_cannot_be_generically_rerun(
    db, audit_path
):
    secret = "synthetic-agent-tool-secret-123456789"
    private_path = "/home/runner/private/task-42"
    job_id = db.insert_job(
        command=f"cd {private_path} && AUTHORIZATION='Bearer {secret}' codex exec",
        type="coding",
        project="demo",
        engineering_task_id="8da8c173-f0f5-4e0b-b67b-3aad07155182",
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )
    db.update_job(
        job_id,
        log_tail=f"Authorization: Bearer {secret}\nworking at {private_path}\n",
    )
    ctx = make_ctx(db, audit_path=audit_path)

    jobs = asyncio.run(dispatch_tool("jobs", {}, ctx))
    detail = asyncio.run(dispatch_tool("job_detail", {"job_id": job_id}, ctx))
    log = asyncio.run(dispatch_tool("job_log", {"job_id": job_id}, ctx))
    encoded = repr((jobs, detail, log))

    assert "Run Codex agent in an isolated worktree" in encoded
    assert secret not in encoded
    assert private_path not in encoded
    assert detail["engineering_task_id"] == "8da8c173-f0f5-4e0b-b67b-3aad07155182"

    rerun = asyncio.run(
        dispatch_tool("request_rerun_job", {"job_id": job_id}, ctx)
    )
    assert "安全重試流程" in rerun["error"]
    assert db.list_approvals() == []


def test_approvals_tool_lists_by_status(db, audit_path):
    from app.approvals import request_enqueue_approval

    request_enqueue_approval(db, command="echo hi", audit_path=audit_path)
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("approvals", {"status": "pending"}, ctx))
    assert len(result) == 1
    assert result[0]["status"] == "pending"


def test_events_tool_reads_audit_tail(db, audit_path):
    from app.audit import append_audit

    for i in range(5):
        append_audit("test_action", {"i": i}, path=audit_path)
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("events", {"n": 3}, ctx))
    assert len(result) == 3
    # 新到舊
    assert result[0]["params"]["i"] == 4


def test_shadow_events_tool_returns_baseline_before_deferred_denial(db, audit_path):
    from app.audit import append_audit

    append_audit("baseline", {"kept": True}, path=audit_path)
    ctx = make_ctx(
        db,
        config=make_config(authorization_mode="shadow"),
        audit_path=audit_path,
    )

    result = asyncio.run(dispatch_tool("events", {"n": 20}, ctx))

    assert [record["action"] for record in result] == ["baseline"]
    records = read_audit(audit_path)
    assert [record["action"] for record in records] == [
        "baseline",
        "authorization_shadow_denied",
    ]
    assert records[-1]["params"]["tool"] == "events"
    assert records[-1]["params"]["action"] == "audit.view"


def test_shadow_tool_denial_does_not_block_approval_creation(db, audit_path):
    ctx = make_ctx(
        db,
        config=make_config(authorization_mode="shadow"),
        audit_path=audit_path,
    )

    result = asyncio.run(
        dispatch_tool(
            "request_enqueue_job",
            {"command": "echo tool-shadow-compatible"},
            ctx,
        )
    )

    approval = db.get_approval(result["approval"]["id"])
    assert approval is not None
    assert approval.status == "pending"
    shadow = [
        record
        for record in read_audit(audit_path)
        if record["action"] == "authorization_shadow_denied"
    ]
    assert len(shadow) == 1
    assert shadow[0]["params"]["tool"] == "request_enqueue_job"
    assert shadow[0]["params"]["action"] == "project.operate"


def test_gpu_tool_summarizes_readings(db):
    state = ServerState(
        name="gpu-box",
        online=True,
        gpus=[GpuReading(util_percent=42.0, mem_used_mb=1000, mem_total_mb=8000)],
    )
    ctx = make_ctx(db, server_states={"gpu-box": state})
    result = asyncio.run(dispatch_tool("gpu", {}, ctx))
    assert result[0]["name"] == "gpu-box"
    assert result[0]["gpus"][0]["util_percent"] == 42.0


def test_vllm_health_tool_not_configured(db):
    ctx = make_ctx(db, config=make_config(vllm_base_url=None, vllm_model=None))
    result = asyncio.run(dispatch_tool("vllm_health", {}, ctx))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# 階段 16（PLAN.md Q.3 節，資料卡）：get_dataset_card 唯讀工具
# ---------------------------------------------------------------------------


def test_get_dataset_card_tool_with_card(db):
    db.insert_dataset(
        name="defect",
        version="v1",
        size_bytes=100,
        source_path="/data/defect",
        manifest={"file_count": 2, "total_size": 100, "files": []},
        card={
            "description": "缺陷偵測資料集第一版",
            "method": "人工標註",
            "derived_from": None,
            "counts_custom": {"train": 80, "val": 20},
            "created_at": "2026-07-11T00:00:00",
            "updated_at": "2026-07-11T00:00:00",
        },
    )
    db.upsert_dataset_cache("server-a", "defect", "v1")
    ctx = make_ctx(db)
    result = asyncio.run(
        dispatch_tool("get_dataset_card", {"name": "defect", "version": "v1"}, ctx)
    )
    assert result["card"]["description"] == "缺陷偵測資料集第一版"
    assert result["note"] is None
    assert result["auto_facts"]["cached_on"] == ["server-a"]
    assert "缺陷偵測資料集第一版" in result["rendered"]


def test_get_dataset_card_tool_without_card_returns_null_and_note(db):
    """階段 16 之前建立的舊版本沒有卡：唯讀工具不報錯、不回空，如實標示
    未登記資料卡（PLAN.md Q.3：agent 必須照實說沒有紀錄，不得腦補）。"""
    db.insert_dataset(
        name="legacy",
        version="v1",
        size_bytes=50,
        source_path="/data/legacy",
        manifest={"file_count": 1, "total_size": 50, "files": []},
    )
    ctx = make_ctx(db)
    result = asyncio.run(
        dispatch_tool("get_dataset_card", {"name": "legacy", "version": "v1"}, ctx)
    )
    assert result["card"] is None
    assert result["note"] is not None
    assert "未登記資料卡" in result["note"]
    assert "未登記資料卡" in result["rendered"]


def test_get_dataset_card_tool_not_found(db):
    ctx = make_ctx(db)
    result = asyncio.run(
        dispatch_tool("get_dataset_card", {"name": "nope", "version": "v1"}, ctx)
    )
    assert "error" in result


def test_get_dataset_card_tool_missing_args(db):
    ctx = make_ctx(db)
    result = asyncio.run(dispatch_tool("get_dataset_card", {"name": "defect"}, ctx))
    assert "error" in result


def test_get_dataset_card_tool_description_warns_against_guessing():
    """釘住工具描述含「不得推測」字樣（PLAN.md Q.3），避免之後重構時被
    悄悄拿掉這個安全提示。"""
    assert "不得推測" in TOOLS["get_dataset_card"].description or (
        "腦補" in TOOLS["get_dataset_card"].description
    )


# ---------------------------------------------------------------------------
# 寫入工具：只建立 approval，絕不直接入列/停止
# ---------------------------------------------------------------------------


def test_request_enqueue_job_creates_approval_not_a_job(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool("request_enqueue_job", {"command": "python train.py"}, ctx)
    )
    assert "approval" in result
    assert result["approval"]["kind"] == "enqueue"
    assert result["approval"]["status"] == "pending"
    assert db.list_jobs() == []


def test_request_enqueue_job_attributes_local_agent_principal(db, audit_path):
    request_context = RequestContext(
        actor=Actor(
            id="33333333-3333-3333-3333-333333333333",
            actor_type=ActorType.SERVICE,
            display_name="Local agent service",
        ),
        authentication_method="service_token",
    )
    ctx = make_ctx(db, audit_path=audit_path)
    ctx.request_context = request_context

    result = asyncio.run(
        dispatch_tool("request_enqueue_job", {"command": "echo attributed"}, ctx)
    )

    approval = db.get_approval(result["approval"]["id"])
    assert approval.requester_actor_id == request_context.actor_id
    record = read_audit(audit_path)[-1]
    assert record["actor"] == {
        "id": request_context.actor_id,
        "kind": "service",
        "authentication": "service_token",
    }


def test_request_enqueue_job_dangerous_command_rejected_no_approval(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool("request_enqueue_job", {"command": "rm -rf /tmp/x"}, ctx)
    )
    assert result.get("rejected") is True
    assert "拒絕" in result["reason"]
    assert db.list_approvals() == []


def test_request_enqueue_job_missing_command(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_enqueue_job", {}, ctx))
    assert "error" in result
    assert db.list_approvals() == []


def test_request_stop_job_creates_approval(db, audit_path):
    job_id = db.insert_job(command="sleep 100")
    db.update_job(job_id, status="running", server="server-a")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_stop_job", {"job_id": job_id}, ctx))
    assert "approval" in result
    assert result["approval"]["kind"] == "stop"
    # job 狀態不變，要核准才會真的停止
    assert db.get_job(job_id).status == "running"


def test_request_stop_job_not_running_returns_error(db, audit_path):
    job_id = db.insert_job(command="sleep 100")  # queued
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_stop_job", {"job_id": job_id}, ctx))
    assert "error" in result
    assert db.list_approvals() == []


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.1/K.3）：source="vllm" 標記＋自動核准規則諮詢
# ---------------------------------------------------------------------------


def test_request_enqueue_job_records_source_vllm(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_enqueue_job", {"command": "echo hi"}, ctx))
    assert result["approval"]["payload"]["source"] == "vllm"
    assert "auto_approved" not in result


def test_request_enqueue_job_matching_rule_auto_approves(db, audit_path, tmp_path):
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: vllm\n    command_regex: '^echo '\n", encoding="utf-8")
    ctx = make_ctx(db, audit_path=audit_path, config=make_config(auto_approve_rules_path=str(rules_path)))

    result = asyncio.run(dispatch_tool("request_enqueue_job", {"command": "echo hi"}, ctx))
    assert result["auto_approved"] is True
    assert result["approval"]["status"] == "approved"
    jobs = db.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].command == "echo hi"


def test_request_enqueue_job_non_matching_rule_stays_pending(db, audit_path, tmp_path):
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: web\n", encoding="utf-8")
    ctx = make_ctx(db, audit_path=audit_path, config=make_config(auto_approve_rules_path=str(rules_path)))

    result = asyncio.run(dispatch_tool("request_enqueue_job", {"command": "echo hi"}, ctx))
    assert "auto_approved" not in result
    assert result["approval"]["status"] == "pending"
    assert db.list_jobs() == []


def test_request_stop_job_records_source_vllm(db, audit_path):
    job_id = db.insert_job(command="sleep 100")
    db.update_job(job_id, status="running", server="server-a")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_stop_job", {"job_id": job_id}, ctx))
    assert result["approval"]["payload"]["source"] == "vllm"
    assert "auto_approved" not in result


def test_request_stop_job_matching_rule_without_ssh_run_stays_pending(db, audit_path, tmp_path):
    """stop 的自動核准需要 ctx.ssh_run；agent_tools 的 AgentContext 沒有給
    （呼叫端沒注入）時保持 pending，不報錯（PLAN.md K.3）。"""
    job_id = db.insert_job(command="sleep 100")
    db.update_job(job_id, status="running", server="server-a")
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: vllm\n    kind: stop\n", encoding="utf-8")
    ctx = make_ctx(
        db,
        audit_path=audit_path,
        config=make_config(auto_approve_rules_path=str(rules_path)),
        ssh_run=None,
    )

    result = asyncio.run(dispatch_tool("request_stop_job", {"job_id": job_id}, ctx))
    assert "auto_approved" not in result
    assert result["approval"]["status"] == "pending"
    assert db.get_job(job_id).status == "running"


def test_request_stop_job_matching_rule_with_ssh_run_executes(db, audit_path, tmp_path):
    job_id = db.insert_job(command="sleep 100")
    db.update_job(job_id, status="running", server="server-a")
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: vllm\n    kind: stop\n", encoding="utf-8")

    class FakeSSH:
        def __init__(self):
            self.calls = []

        async def __call__(self, server_name, command, timeout):
            self.calls.append(command)

            class R:
                stdout = "stopped\n"

            return R()

    fake_ssh = FakeSSH()
    ctx = make_ctx(
        db,
        audit_path=audit_path,
        config=make_config(auto_approve_rules_path=str(rules_path)),
        ssh_run=fake_ssh,
    )

    result = asyncio.run(dispatch_tool("request_stop_job", {"job_id": job_id}, ctx))
    assert result["auto_approved"] is True
    assert db.get_job(job_id).status == "running"
    assert db.get_legacy_job_stop_intent(job_id=job_id)["state"] == "delivered"
    assert any("tmux kill-session" in c for c in fake_ssh.calls)


def test_request_rerun_job_copies_dispatch_fields_only(db, audit_path):
    db.insert_project(name="demo", repo_or_path="/repo/demo")
    original_id = db.insert_job(
        command="python train.py --lr 0.1",
        type="train",
        project="demo",
        require_tag="gpu",
        pin_server="server-a",
        priority="low",
        depends_on=[1, 2],
        gpus_needed=2,
    )
    db.update_job(
        original_id,
        status="failed",
        server="server-a",
        started_at="2024-01-01T00:00:00",
        finished_at="2024-01-01T01:00:00",
        exit_code=1,
    )

    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_rerun_job", {"job_id": original_id}, ctx))
    assert "approval" in result
    payload = result["approval"]["payload"]

    # 複製的欄位
    assert payload["command"] == "python train.py --lr 0.1"
    assert payload["type"] == "train"
    assert payload["project"] == "demo"
    assert payload["require_tag"] == "gpu"
    assert payload["pin_server"] == "server-a"
    assert payload["priority"] == "low"

    # 不複製的執行期／依賴期欄位：新的 approval payload 裡 depends_on 應為空，
    # 不應該帶著舊任務的 server/started_at/exit_code 等執行期狀態
    assert payload["depends_on"] == []
    assert "server" not in payload
    assert "started_at" not in payload
    assert "exit_code" not in payload

    assert db.list_jobs() == [db.get_job(original_id)]  # 沒有新增任何 job


def test_request_rerun_job_not_found(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_rerun_job", {"job_id": 999}, ctx))
    assert "error" in result


def test_request_rerun_job_dangerous_command_rejected(db, audit_path):
    original_id = db.insert_job(command="rm -rf /tmp/x")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_rerun_job", {"job_id": original_id}, ctx))
    assert result.get("rejected") is True
    assert db.list_approvals() == []


# ---------------------------------------------------------------------------
# 階段 8 第二批：Web Server Management 工具
# ---------------------------------------------------------------------------


def _make_server_config(name="server-x", **overrides):
    from app.config import ServerConfig

    base = dict(name=name, host="10.0.0.5", user="train", key="~/.ssh/id_rsa")
    base.update(overrides)
    return ServerConfig(**base)


def test_list_server_configs_tool_hides_key_content(db):
    cfg = _make_server_config()
    ctx = make_ctx(db, server_configs={"server-x": cfg})
    result = asyncio.run(dispatch_tool("list_server_configs", {}, ctx))
    assert len(result) == 1
    assert result[0]["key"] == "~/.ssh/id_rsa"
    assert "key_path" not in result[0]


def test_get_server_config_tool_not_found(db):
    ctx = make_ctx(db, server_configs={})
    result = asyncio.run(dispatch_tool("get_server_config", {"name": "nope"}, ctx))
    assert "error" in result


def test_get_server_config_tool_found(db):
    cfg = _make_server_config(host="10.0.0.9")
    ctx = make_ctx(db, server_configs={"server-x": cfg})
    result = asyncio.run(dispatch_tool("get_server_config", {"name": "server-x"}, ctx))
    assert result["host"] == "10.0.0.9"


def test_test_server_ssh_tool_without_ssh_run_direct_returns_error(db):
    ctx = make_ctx(db, ssh_run_direct=None)
    result = asyncio.run(
        dispatch_tool(
            "test_server_ssh",
            {"payload": {"name": "server-x", "host": "10.0.0.5", "user": "train", "key": "~/.ssh/id_rsa"}},
            ctx,
        )
    )
    assert "error" in result


def test_test_server_ssh_tool_legacy_ssh_run_alone_is_not_enough(db):
    """釘住 bug 修正：就算（legacy）`ctx.ssh_run` 有設，沒有 `ssh_run_direct`
    的話工具仍然要回錯誤——不能悄悄退回按名字查表的舊行為。"""
    async def fake_legacy_ssh_run(server_name, command, timeout):
        raise AssertionError("不應該呼叫 legacy ssh_run（按名字查表）")

    ctx = make_ctx(db, ssh_run=fake_legacy_ssh_run, ssh_run_direct=None)
    result = asyncio.run(
        dispatch_tool(
            "test_server_ssh",
            {"payload": {"name": "server-x", "host": "10.0.0.5", "user": "train", "key": "~/.ssh/id_rsa"}},
            ctx,
        )
    )
    assert "error" in result


def test_test_server_ssh_tool_runs_with_injected_ssh_run_direct(db):
    """bug 修正的釘住測試：注入的 `ssh_run_direct` 收到的第一個參數必須是
    `payload` 組出來的 `ServerConfig`（不是按名字查表），即使 name 是
    `"server-x"` 而不存在於任何 server_configs 裡也能測試成功。"""
    class FakeResult:
        def __init__(self, stdout):
            self.stdout = stdout

    calls = []
    seen_configs = []

    async def fake_ssh_run_direct(server_cfg, command, timeout):
        calls.append(command)
        seen_configs.append(server_cfg)
        return FakeResult("ok\n")

    ctx = make_ctx(db, ssh_run_direct=fake_ssh_run_direct)
    result = asyncio.run(
        dispatch_tool(
            "test_server_ssh",
            {"payload": {"name": "server-x", "host": "10.0.0.5", "user": "train", "key": "~/.ssh/id_rsa"}},
            ctx,
        )
    )
    assert result["ok"] is True
    assert len(calls) == 4  # hostname/whoami/tmux/gpu，沒有 project_roots/dataset_roots
    assert all(cfg.host == "10.0.0.5" for cfg in seen_configs)


def test_request_add_server_tool_creates_approval(db, tmp_path, audit_path):
    key_path = tmp_path / ".ssh" / "id_test"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("fake\n")
    key_path.chmod(0o600)
    config = make_config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ctx = make_ctx(db, config=config, audit_path=audit_path)
    payload = {
        "payload": {
            "name": "server-x",
            "host": "10.0.0.5",
            "user": "train",
            "key": str(key_path),
        }
    }
    result = asyncio.run(dispatch_tool("request_add_server", payload, ctx))
    assert "approval" in result
    assert result["approval"]["kind"] == "server_add"
    assert result["approval"]["status"] == "pending"


def test_request_add_server_tool_invalid_config_rejected(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    payload = {"payload": {"name": "bad name!", "host": "10.0.0.5", "user": "train", "key": "~/.ssh/id_rsa"}}
    result = asyncio.run(dispatch_tool("request_add_server", payload, ctx))
    assert result.get("rejected") is True
    assert db.list_approvals() == []


def test_request_update_server_tool_creates_approval(db, tmp_path, audit_path):
    # Mutation validation must not depend on a developer's real ~/.ssh key.
    key_path = tmp_path / ".ssh" / "id_test"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("fake\n")
    key_path.chmod(0o600)
    cfg = _make_server_config(key=str(key_path))
    config = make_config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ctx = make_ctx(
        db,
        config=config,
        server_configs={"server-x": cfg},
        audit_path=audit_path,
    )
    result = asyncio.run(
        dispatch_tool(
            "request_update_server",
            {"name": "server-x", "updates": {"host": "10.0.0.10"}},
            ctx,
        )
    )
    assert "approval" in result
    assert result["approval"]["kind"] == "server_update"


def test_request_update_server_tool_rename_rejected(db, audit_path):
    cfg = _make_server_config()
    ctx = make_ctx(db, server_configs={"server-x": cfg}, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "request_update_server",
            {"name": "server-x", "updates": {"name": "server-y"}},
            ctx,
        )
    )
    assert "error" in result
    assert db.list_approvals() == []


def test_request_disable_server_tool_creates_approval(db, audit_path):
    cfg = _make_server_config()
    ctx = make_ctx(db, server_configs={"server-x": cfg}, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_disable_server", {"name": "server-x"}, ctx))
    assert "approval" in result
    assert result["approval"]["kind"] == "server_disable"


def test_request_disable_server_tool_not_found(db, audit_path):
    ctx = make_ctx(db, server_configs={}, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("request_disable_server", {"name": "nope"}, ctx))
    assert "error" in result


def test_no_request_delete_server_tool_registered():
    """PLAN.md I.6：沒有 request_delete_server 工具——LLM 不能刪除
    server，連建立刪除請求的權限都沒有，只有網頁介面可以發起
    delete-request。"""
    assert "request_delete_server" not in TOOLS


# ---------------------------------------------------------------------------
# 專案詳情頁計畫（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、
# 目標／方法／進度管理」）第 4 節：search_experiment_timeline（唯讀）＋
# add_experiment_record／update_project_doc（**已正式登記的鐵律例外**：
# 不建立 approval，直接寫入，見 app/agent_tools.py 模組 docstring）。
# ---------------------------------------------------------------------------


def test_search_experiment_timeline_tool_returns_merged_items(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "手動筆記")
    db.insert_job(command="python train.py", project="proj1")
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool("search_experiment_timeline", {"project_name": "proj1"}, ctx)
    )
    assert "items" in result
    types = {item["type"] for item in result["items"]}
    assert types == {"record", "job"}


def test_search_experiment_timeline_tool_not_found(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool("search_experiment_timeline", {"project_name": "nope"}, ctx)
    )
    assert "error" in result


def test_search_experiment_timeline_tool_missing_project_name(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(dispatch_tool("search_experiment_timeline", {}, ctx))
    assert "error" in result


def test_search_experiment_timeline_tool_limit_clamped(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    for i in range(5):
        db.insert_experiment_record("proj1", f"note {i}")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "search_experiment_timeline", {"project_name": "proj1", "limit": 2}, ctx
        )
    )
    assert len(result["items"]) == 2
    assert result["has_more"] is True


def test_search_experiment_timeline_tool_kinds_filter(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a", kind="note")
    db.insert_job(command="echo hi", project="proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "search_experiment_timeline",
            {"project_name": "proj1", "kinds": ["job"]},
            ctx,
        )
    )
    assert len(result["items"]) == 1
    assert result["items"][0]["type"] == "job"


def test_add_experiment_record_tool_forces_agent_author_no_approval(db, audit_path):
    """鐵律例外的核心斷言：**強制 `author="agent"`**、**不建立 approval**
    （跟 `request_enqueue_job`／`request_stop_job` 等其餘寫入工具完全不同
    的行為），直接寫進 `experiment_records` 表。"""
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "agent 觀察到的現象", "kind": "observation"},
            ctx,
        )
    )
    assert "approval" not in result
    assert result["author"] == "agent"
    assert result["kind"] == "observation"

    records = db.list_experiment_records("proj1")
    assert len(records) == 1
    assert records[0].author == "agent"
    assert records[0].content == "agent 觀察到的現象"
    # 沒有任何 approval 被建立（跟鐵律第 2 條的其餘寫入工具行為不同，是
    # 已登記的刻意例外）。
    assert db.list_approvals() == []


def test_add_experiment_record_tool_ignores_caller_supplied_author(db, audit_path):
    """呼叫端就算故意在 args 塞一個 `author` 鍵，也不會被工具讀取／採用
    ——handler 完全不讀這個鍵。"""
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "x", "author": "user"},
            ctx,
        )
    )
    assert result["author"] == "agent"


def test_add_experiment_record_tool_default_kind_is_note(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool("add_experiment_record", {"project_name": "proj1", "content": "x"}, ctx)
    )
    assert result["kind"] == "note"


def test_add_experiment_record_tool_not_found(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record", {"project_name": "nope", "content": "x"}, ctx
        )
    )
    assert "error" in result


def test_add_experiment_record_tool_missing_content(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool("add_experiment_record", {"project_name": "proj1"}, ctx)
    )
    assert "error" in result
    assert db.list_experiment_records("proj1") == []


def test_add_experiment_record_tool_invalid_kind(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "x", "kind": "bogus"},
            ctx,
        )
    )
    assert "error" in result
    assert db.list_experiment_records("proj1") == []


def test_add_experiment_record_tool_writes_audit(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    asyncio.run(
        dispatch_tool("add_experiment_record", {"project_name": "proj1", "content": "x"}, ctx)
    )
    events = db.list_durable_audit_events(limit=10)
    created = [e for e in events if e["action"] == "experiment_record_created"]
    assert len(created) == 1
    assert created[0]["params"]["author"] == "agent"
    assert created[0]["resource_type"] == "experiment_record"


def test_add_experiment_record_tool_accepts_job_id_link(db, audit_path):
    """修正 2（Fable 最終審查）：`add_experiment_record` 工具開放
    `job_id` 弱關聯——不驗證所指 job 是否存在。"""
    db.insert_project("proj1", "/repo/proj1")
    job_id = db.insert_job(command="python train.py", project="proj1")
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "補充說明", "job_id": job_id},
            ctx,
        )
    )
    assert result["job_id"] == job_id
    assert result["coding_run_id"] is None

    record = db.list_experiment_records("proj1")[0]
    assert record.job_id == job_id


def test_add_experiment_record_tool_accepts_coding_run_id_link(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-c", instruction="fix bug"
    )
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "補充說明", "coding_run_id": run_id},
            ctx,
        )
    )
    assert result["coding_run_id"] == run_id
    assert result["job_id"] is None


def test_add_experiment_record_tool_invalid_job_id(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "x", "job_id": "abc"},
            ctx,
        )
    )
    assert "error" in result
    assert db.list_experiment_records("proj1") == []


def test_add_experiment_record_tool_invalid_coding_run_id(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "add_experiment_record",
            {"project_name": "proj1", "content": "x", "coding_run_id": "abc"},
            ctx,
        )
    )
    assert "error" in result
    assert db.list_experiment_records("proj1") == []


def test_update_project_doc_tool_updates_whitelisted_field_no_approval(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool(
            "update_project_doc",
            {"project_name": "proj1", "field": "goal", "content": "把準確率衝到 95%"},
            ctx,
        )
    )
    assert "approval" not in result
    assert result["project"]["goal"] == "把準確率衝到 95%"
    assert db.get_project("proj1").goal == "把準確率衝到 95%"
    assert db.list_approvals() == []


def test_update_project_doc_tool_rejects_field_outside_whitelist(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)

    result = asyncio.run(
        dispatch_tool(
            "update_project_doc",
            {"project_name": "proj1", "field": "repo_or_path", "content": "/etc/passwd"},
            ctx,
        )
    )
    assert "error" in result
    assert db.get_project("proj1").repo_or_path == "/repo/proj1"


def test_update_project_doc_tool_not_found(db, audit_path):
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "update_project_doc",
            {"project_name": "nope", "field": "goal", "content": "x"},
            ctx,
        )
    )
    assert "error" in result


def test_update_project_doc_tool_missing_content(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool(
            "update_project_doc", {"project_name": "proj1", "field": "goal"}, ctx
        )
    )
    assert "error" in result


def test_update_project_doc_tool_writes_audit(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    ctx = make_ctx(db, audit_path=audit_path)
    asyncio.run(
        dispatch_tool(
            "update_project_doc",
            {"project_name": "proj1", "field": "progress", "content": "第 3 輪訓練中"},
            ctx,
        )
    )
    events = db.list_durable_audit_events(limit=10)
    updated = [e for e in events if e["action"] == "project_updated"]
    assert len(updated) == 1
    assert updated[0]["params"]["field_names"] == ["progress"]
    assert updated[0]["params"]["field_count"] == 1


def test_project_summary_includes_new_doc_fields(db, audit_path):
    db.insert_project("proj1", "/repo/proj1")
    db.update_project("proj1", goal="目標文字" * 100)  # 超過 300 字，確認截斷
    ctx = make_ctx(db, audit_path=audit_path)
    result = asyncio.run(
        dispatch_tool("get_project_profile", {"project_name": "proj1"}, ctx)
    )
    summary = result["project"]
    assert "goal" in summary and "optimization_notes" in summary and "progress" in summary
    assert len(summary["goal"]) <= 301
    assert summary["goal"].endswith("…")


# ---------------------------------------------------------------------------
# dispatch_tool()：只查表，不用 getattr/eval
# ---------------------------------------------------------------------------


def test_dispatch_tool_unknown_name_raises_keyerror(db):
    ctx = make_ctx(db)
    with pytest.raises(KeyError):
        asyncio.run(dispatch_tool("approve_approval", {}, ctx))
