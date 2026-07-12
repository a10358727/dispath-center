"""階段 11：專案執行近況（`get_project_activity`，PLAN.md L 節）。

涵蓋 L.5 測試清單：
- find/tail 指令組裝（含 `-prune`／秘密過濾／上限）
- parse（秘密檔案濾掉）
- log 候選挑選（mtime 排序、上限 3、非 log 檔不選、秘密雙保險過濾）
- `probe_instance()`：SSH 失敗回 `{"error": ...}` 不拋例外
- `db.list_jobs()` 的 `project` 過濾（單獨與跟 `status` 併用）
- `GET /projects/{name}/activity`：404／正常形狀／無 instance 明確訊息／
  離線跳過／稽核 `project_activity`
- `GET /jobs?project=` 過濾
- agent 工具 `get_project_activity`／`jobs` 的 `project` 選填參數

MCP bridge 對應的 `get_project_activity`／`list_jobs(project=...)` 測試在
`tests/test_mcp_bridge.py`（沿用該檔案既有的 mock 調度中心風格），這裡不
重複。
"""

from __future__ import annotations

import asyncio

import pytest

from app.activity import (
    LOG_TAIL_BYTES,
    MAX_LIST_FILES,
    MAX_LOG_FILES,
    MAX_READ_FILE_BYTES,
    MAX_RECENT_FILES,
    RECENT_DAYS,
    ProjectInstanceResolutionError,
    build_list_files_command,
    build_log_tail_commands,
    build_read_file_command,
    build_recent_files_command,
    list_instance_files,
    parse_list_files_output,
    parse_recent_files_output,
    probe_instance,
    read_instance_file,
    resolve_project_instance,
    validate_rel_path,
)
from app.agent_tools import AgentContext, dispatch_tool
from app.config import AppConfig, ServerConfig
from app.inventory import DEFAULT_EXCLUDE_NAMES
from app.monitor import GpuReading, ServerState


# ---------------------------------------------------------------------------
# build_recent_files_command / parse_recent_files_output
# ---------------------------------------------------------------------------


def test_build_recent_files_command_includes_prune_and_head_limit():
    cmd = build_recent_files_command("/data/projects/proj1", DEFAULT_EXCLUDE_NAMES)
    assert "-prune" in cmd
    assert f"head -{MAX_RECENT_FILES}" in cmd
    for name in DEFAULT_EXCLUDE_NAMES:
        assert f"-name {name}" in cmd or f"-name '{name}'" in cmd


def test_build_recent_files_command_no_prune_when_exclude_names_empty():
    cmd = build_recent_files_command("/data/projects/proj1", [])
    assert "-prune" not in cmd


def test_build_recent_files_command_uses_default_days_and_custom_days():
    cmd_default = build_recent_files_command("/data/projects/proj1", [])
    assert f"-mtime -{RECENT_DAYS}" in cmd_default
    cmd_custom = build_recent_files_command("/data/projects/proj1", [], days=7)
    assert "-mtime -7" in cmd_custom


def test_build_recent_files_command_never_reads_file_contents():
    cmd = build_recent_files_command("/data/projects/proj1", DEFAULT_EXCLUDE_NAMES)
    assert " cat " not in cmd
    assert "tail" not in cmd
    assert "-printf" in cmd
    assert "sort -rn" in cmd


def test_parse_recent_files_output_parses_mtime_and_path():
    text = "1700000000.5 train.py\n1700000100.0 subdir/model.py\n"
    parsed = parse_recent_files_output(text)
    assert parsed == [
        {"path": "train.py", "mtime_epoch": 1700000000.5},
        {"path": "subdir/model.py", "mtime_epoch": 1700000100.0},
    ]


def test_parse_recent_files_output_handles_empty_input():
    assert parse_recent_files_output("") == []
    assert parse_recent_files_output("   \n  \n") == []


@pytest.mark.parametrize(
    "filename",
    [".env", "id_rsa", "id_ed25519", "server.pem", "private.key", "secrets.yaml", "credentials.json"],
)
def test_parse_recent_files_output_filters_out_secret_files(filename):
    """秘密檔即使出現在 find 輸出裡也會在 parse 這一層就被濾掉（第一道
    保險）。"""
    text = f"1700000000.0 {filename}\n1700000000.0 train.py\n"
    parsed = parse_recent_files_output(text)
    paths = [p["path"] for p in parsed]
    assert filename not in paths
    assert "train.py" in paths


def test_parse_recent_files_output_skips_malformed_lines():
    text = "not-a-valid-line\n1700000000.0 ok.py\n"
    parsed = parse_recent_files_output(text)
    assert parsed == [{"path": "ok.py", "mtime_epoch": 1700000000.0}]


# ---------------------------------------------------------------------------
# build_log_tail_commands：候選挑選（mtime 排序、上限 3、非 log 不選、秘密
# 雙保險過濾）
# ---------------------------------------------------------------------------


def test_build_log_tail_commands_selects_log_out_and_train_txt_only():
    candidates = [
        {"path": "train.log", "mtime_epoch": 100.0},
        {"path": "run.out", "mtime_epoch": 90.0},
        {"path": "train_progress.txt", "mtime_epoch": 80.0},
        {"path": "notes.txt", "mtime_epoch": 200.0},  # 不含 "train"，不選
        {"path": "model.py", "mtime_epoch": 300.0},  # 不是 log 副檔名，不選
    ]
    commands = build_log_tail_commands("/data/projects/proj1", candidates)
    assert set(commands.keys()) == {"train.log", "run.out", "train_progress.txt"}
    for cmd in commands.values():
        assert f"tail -c {LOG_TAIL_BYTES}" in cmd
        assert "|| true" in cmd


def test_build_log_tail_commands_caps_at_max_log_files_newest_mtime_first():
    candidates = [
        {"path": f"epoch{i}_train.log", "mtime_epoch": float(i)} for i in range(10)
    ]
    commands = build_log_tail_commands("/data/projects/proj1", candidates)
    assert len(commands) == MAX_LOG_FILES
    # 最新 mtime（9,8,7）優先被選中。
    assert set(commands.keys()) == {"epoch9_train.log", "epoch8_train.log", "epoch7_train.log"}


def test_build_log_tail_commands_double_filters_secret_files():
    """即使呼叫端不小心傳了未經 `parse_recent_files_output()` 過濾的候選
    （例如秘密檔案剛好符合 `*.log`），這裡仍然要擋下來（雙保險）。"""
    candidates = [
        {"path": "credentials.log", "mtime_epoch": 100.0},
        {"path": "train.log", "mtime_epoch": 50.0},
    ]
    commands = build_log_tail_commands("/data/projects/proj1", candidates)
    assert "credentials.log" not in commands
    assert "train.log" in commands


def test_build_log_tail_commands_uses_quoted_full_path():
    candidates = [{"path": "sub dir/train.log", "mtime_epoch": 1.0}]
    commands = build_log_tail_commands("/data/projects/proj1", candidates)
    cmd = commands["sub dir/train.log"]
    assert "/data/projects/proj1/" in cmd


def test_build_log_tail_commands_empty_candidates_returns_empty_dict():
    assert build_log_tail_commands("/data/projects/proj1", []) == {}


# ---------------------------------------------------------------------------
# probe_instance：async 主流程，注入假 ssh_run
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


class RecordingFakeSSH:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        for predicate, stdout in self.responses:
            if predicate(command):
                return FakeResult(stdout)
        return FakeResult("")


def test_probe_instance_assembles_recent_files_and_log_tails():
    ssh = RecordingFakeSSH(
        [
            (
                lambda c: c.startswith("find"),
                "1700000200.0 train.log\n1700000100.0 README.md\n",
            ),
            (lambda c: c.startswith("tail"), "epoch 3 loss=0.1\n"),
        ]
    )
    result = asyncio.run(probe_instance(ssh, "server-a", "/data/projects/proj1", []))
    assert "error" not in result
    assert {"path": "train.log", "mtime_epoch": 1700000200.0} in result["recent_files"]
    assert result["log_tails"] == {"train.log": "epoch 3 loss=0.1\n"}


def test_probe_instance_ssh_failure_on_find_returns_error_not_exception():
    async def failing_ssh(server_name, command, timeout):
        raise ConnectionError("simulated ssh failure")

    result = asyncio.run(probe_instance(failing_ssh, "server-a", "/data/projects/proj1"))
    assert "error" in result
    assert "server-a" in result["error"]


def test_probe_instance_no_log_candidates_returns_empty_log_tails():
    ssh = RecordingFakeSSH(
        [(lambda c: c.startswith("find"), "1700000000.0 README.md\n")]
    )
    result = asyncio.run(probe_instance(ssh, "server-a", "/data/projects/proj1"))
    assert result["log_tails"] == {}
    assert result["recent_files"] == [{"path": "README.md", "mtime_epoch": 1700000000.0}]


def test_probe_instance_uses_default_exclude_names_when_omitted():
    captured_cmd = {}

    async def ssh(server_name, command, timeout):
        captured_cmd["cmd"] = command
        return FakeResult("")

    asyncio.run(probe_instance(ssh, "server-a", "/data/projects/proj1"))
    assert "-prune" in captured_cmd["cmd"]


# ---------------------------------------------------------------------------
# db.list_jobs()：project 過濾
# ---------------------------------------------------------------------------


def test_db_list_jobs_filters_by_project(db):
    db.insert_job(command="echo a", project="proj-a")
    db.insert_job(command="echo b", project="proj-b")
    db.insert_job(command="echo c", project="proj-a")

    all_jobs = db.list_jobs()
    assert len(all_jobs) == 3

    proj_a_jobs = db.list_jobs(project="proj-a")
    assert len(proj_a_jobs) == 2
    assert all(j.project == "proj-a" for j in proj_a_jobs)


def test_db_list_jobs_project_combined_with_status(db):
    id1 = db.insert_job(command="echo a", project="proj-a", status="queued")
    id2 = db.insert_job(command="echo b", project="proj-a", status="queued")
    db.insert_job(command="echo c", project="proj-b", status="queued")
    db.update_job(id2, status="running")

    result = db.list_jobs(status="running", project="proj-a")
    assert len(result) == 1
    assert result[0].id == id2

    result_no_match = db.list_jobs(status="running", project="proj-b")
    assert result_no_match == []
    assert id1  # sanity: id1 untouched, still queued proj-a not selected above


def test_db_list_jobs_no_filters_matches_existing_behavior(db):
    db.insert_job(command="echo a")
    assert len(db.list_jobs()) == 1


# ---------------------------------------------------------------------------
# GET /jobs?project=
# ---------------------------------------------------------------------------


def test_get_jobs_endpoint_filters_by_project(api_client):
    client, main_module = api_client
    main_module.app_state.db.insert_job(command="echo a", project="proj-a")
    main_module.app_state.db.insert_job(command="echo b", project="proj-b")

    resp = client.get("/jobs?project=proj-a")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["project"] == "proj-a"


def test_get_jobs_endpoint_project_and_status_combined(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    id1 = db.insert_job(command="echo a", project="proj-a", status="queued")
    db.insert_job(command="echo b", project="proj-a", status="queued")
    db.update_job(id1, status="running")

    resp = client.get("/jobs?project=proj-a&status=running")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == id1


# ---------------------------------------------------------------------------
# GET /projects/{name}/activity
# ---------------------------------------------------------------------------


class ActivityFakeSSH:
    """依指令內容回傳固定假輸出：一個 recent file（train.log）＋其 tail 內容。
    記錄呼叫過的指令方便斷言稽核/探測有沒有真的發生。"""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if command.startswith("find"):
            return FakeResult("1700000000.0 train.log\n")
        if command.startswith("tail"):
            return FakeResult("epoch 1 loss=1.0\n")
        return FakeResult("")


def test_get_project_activity_404_for_unknown_project(api_client):
    client, _main = api_client
    resp = client.get("/projects/nope/activity")
    assert resp.status_code == 404


def test_get_project_activity_no_instances_returns_explicit_message_not_404(api_client):
    client, main_module = api_client
    main_module.app_state.db.insert_project("proj1", "https://github.com/x/proj1.git")

    resp = client.get("/projects/proj1/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["instances"] == []
    assert isinstance(body["activity"], str)
    assert "沒有已登記" in body["activity"]


def test_get_project_activity_skips_offline_instances(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.server_states["server-a"] = ServerState(name="server-a", online=False)
    ssh = ActivityFakeSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["activity"]) == 1
    assert body["activity"][0]["skipped"] == "offline"
    assert ssh.calls == []


def test_get_project_activity_probes_online_instances_and_writes_audit(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.server_states["server-a"] = ServerState(
        name="server-a",
        online=True,
        gpus=[GpuReading(util_percent=42.0, mem_used_mb=100.0, mem_total_mb=1000.0)],
        disk_avail_bytes=123456,
    )
    main_module.app_state.server_configs["server-a"] = ServerConfig(
        name="server-a", host="10.0.0.1", user="train", key="~/.ssh/id_rsa"
    )
    ssh = ActivityFakeSSH()
    main_module.app_state.ssh_run = ssh
    job_id = db.insert_job(command="python train.py", project="proj1", status="done")
    db.update_job(job_id, log_tail="training finished\n", exit_code=0)

    resp = client.get("/projects/proj1/activity")
    assert resp.status_code == 200
    body = resp.json()

    assert body["project"]["name"] == "proj1"
    assert len(body["instances"]) == 1
    assert body["server_states"]["server-a"]["online"] is True
    assert body["server_states"]["server-a"]["gpu_util_max"] == 42.0
    assert body["server_states"]["server-a"]["disk_avail_bytes"] == 123456
    assert len(body["recent_jobs"]) == 1
    assert body["recent_jobs"][0]["exit_code"] == 0
    assert body["latest_job_log_tail"] == "training finished\n"

    activity = body["activity"]
    assert len(activity) == 1
    assert activity[0]["server"] == "server-a"
    assert activity[0]["recent_files"] == [{"path": "train.log", "mtime_epoch": 1700000000.0}]
    assert activity[0]["log_tails"] == {"train.log": "epoch 1 loss=1.0\n"}
    assert any(c.startswith("find") for c in ssh.calls)
    assert any(c.startswith("tail") for c in ssh.calls)

    events = client.get("/events").json()
    audit_actions = [e["action"] for e in events]
    assert "project_activity" in audit_actions
    activity_event = next(e for e in events if e["action"] == "project_activity")
    assert activity_event["params"]["project"] == "proj1"
    assert activity_event["params"]["probed"] == ["server-a"]


def test_get_project_activity_offline_instance_not_counted_in_audit_probed(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.server_states["server-a"] = ServerState(name="server-a", online=False)

    client.get("/projects/proj1/activity")
    events = client.get("/events").json()
    activity_event = next(e for e in events if e["action"] == "project_activity")
    assert activity_event["params"]["probed"] == []


# ---------------------------------------------------------------------------
# agent 工具：get_project_activity／jobs 的 project 選填參數
# ---------------------------------------------------------------------------


def _make_ctx(db, **overrides) -> AgentContext:
    base = dict(db=db, server_states={}, config=AppConfig(servers=[]))
    base.update(overrides)
    return AgentContext(**base)


def test_agent_tool_jobs_filters_by_project(db):
    db.insert_job(command="echo a", project="proj-a")
    db.insert_job(command="echo b", project="proj-b")
    ctx = _make_ctx(db)
    result = asyncio.run(dispatch_tool("jobs", {"project": "proj-a"}, ctx))
    assert len(result) == 1
    assert result[0]["project"] == "proj-a"


def test_agent_tool_get_project_activity_unknown_project_returns_error(db):
    ctx = _make_ctx(db)
    result = asyncio.run(dispatch_tool("get_project_activity", {"project_name": "nope"}, ctx))
    assert "error" in result


def test_agent_tool_get_project_activity_missing_project_name_returns_error(db):
    ctx = _make_ctx(db)
    result = asyncio.run(dispatch_tool("get_project_activity", {}, ctx))
    assert "error" in result


def test_agent_tool_get_project_activity_no_instances_message(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    ctx = _make_ctx(db)
    result = asyncio.run(dispatch_tool("get_project_activity", {"project_name": "proj1"}, ctx))
    assert result["instances"] == []
    assert isinstance(result["activity"], str)


def test_agent_tool_get_project_activity_skips_offline_instance(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ctx = _make_ctx(
        db,
        server_states={"server-a": ServerState(name="server-a", online=False)},
        ssh_run=ActivityFakeSSH(),
    )
    result = asyncio.run(dispatch_tool("get_project_activity", {"project_name": "proj1"}, ctx))
    assert result["activity"][0]["skipped"] == "offline"


def test_agent_tool_get_project_activity_no_ssh_run_treats_as_offline(db):
    """沒有注入 `ctx.ssh_run`（`None`）時，即使機器 `online=True` 也視為
    離線處理（不拋例外，不嘗試呼叫 `None`）。"""
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ctx = _make_ctx(
        db, server_states={"server-a": ServerState(name="server-a", online=True)}, ssh_run=None
    )
    result = asyncio.run(dispatch_tool("get_project_activity", {"project_name": "proj1"}, ctx))
    assert result["activity"][0]["skipped"] == "offline"


def test_agent_tool_get_project_activity_probes_online_instance(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = ActivityFakeSSH()
    ctx = _make_ctx(
        db,
        server_states={"server-a": ServerState(name="server-a", online=True)},
        ssh_run=ssh,
        server_configs={
            "server-a": ServerConfig(name="server-a", host="10.0.0.1", user="t", key="~/.ssh/id_rsa")
        },
    )
    result = asyncio.run(dispatch_tool("get_project_activity", {"project_name": "proj1"}, ctx))
    assert result["activity"][0]["log_tails"] == {"train.log": "epoch 1 loss=1.0\n"}
    assert ssh.calls


# ---------------------------------------------------------------------------
# PLAN.md M.1（階段 12）：讀檔工具——build_list_files_command／
# parse_list_files_output／build_read_file_command／validate_rel_path
# ---------------------------------------------------------------------------


def test_build_list_files_command_includes_prune_maxdepth_and_head_limit():
    cmd = build_list_files_command("/data/projects/proj1", DEFAULT_EXCLUDE_NAMES)
    assert "-prune" in cmd
    assert "-maxdepth 4" in cmd
    assert f"head -{MAX_LIST_FILES}" in cmd
    assert "-type f" in cmd


def test_build_list_files_command_no_prune_when_exclude_names_empty():
    cmd = build_list_files_command("/data/projects/proj1", [])
    assert "-prune" not in cmd


def test_build_list_files_command_never_reads_file_contents():
    cmd = build_list_files_command("/data/projects/proj1", [])
    assert " cat " not in cmd
    assert "tail" not in cmd
    assert "-printf" in cmd


def test_parse_list_files_output_returns_relative_paths():
    text = "train.py\nsubdir/model.py\n"
    assert parse_list_files_output(text) == ["train.py", "subdir/model.py"]


def test_parse_list_files_output_handles_empty_input():
    assert parse_list_files_output("") == []
    assert parse_list_files_output("  \n \n") == []


@pytest.mark.parametrize(
    "filename",
    [".env", "id_rsa", "id_ed25519", "server.pem", "private.key", "secrets.yaml", "credentials.json"],
)
def test_parse_list_files_output_filters_out_secret_files(filename):
    text = f"{filename}\ntrain.py\n"
    paths = parse_list_files_output(text)
    assert filename not in paths
    assert "train.py" in paths


def test_build_read_file_command_uses_head_c_and_read_limit():
    cmd = build_read_file_command("/data/projects/proj1", "train.py")
    assert f"head -c {MAX_READ_FILE_BYTES}" in cmd
    assert "/data/projects/proj1/train.py" in cmd
    assert " cat " not in cmd


def test_build_read_file_command_never_writes():
    cmd = build_read_file_command("/data/projects/proj1", "train.py")
    # stderr 導向 /dev/null 不算寫檔（模組慣例），排除後不得再有任何重導向
    assert ">" not in cmd.replace("2>/dev/null", "")


def test_validate_rel_path_accepts_normal_relative_path():
    assert validate_rel_path("train.py") is None
    assert validate_rel_path("subdir/model.py") is None


def test_validate_rel_path_rejects_empty():
    assert validate_rel_path("") is not None
    assert validate_rel_path("   ") is not None


def test_validate_rel_path_rejects_absolute_path():
    assert validate_rel_path("/etc/passwd") is not None


@pytest.mark.parametrize(
    "rel_path",
    ["../secret.txt", "a/../../etc/passwd", "a/b/../../../x", ".."],
)
def test_validate_rel_path_rejects_dotdot_segments(rel_path):
    assert validate_rel_path(rel_path) is not None


def test_validate_rel_path_does_not_false_positive_on_dots_in_filename():
    """檔名剛好含兩個點但不是路徑穿越片段（例如 `a..b`）不該被誤判。"""
    assert validate_rel_path("a..b/file.py") is None
    assert validate_rel_path("weird..name.txt") is None


@pytest.mark.parametrize(
    "filename",
    [".env", "id_rsa", "id_ed25519", "server.pem", "private.key", "secrets.yaml", "credentials.json"],
)
def test_validate_rel_path_rejects_secret_filenames(filename):
    assert validate_rel_path(filename) is not None
    assert validate_rel_path(f"subdir/{filename}") is not None


# ---------------------------------------------------------------------------
# list_instance_files／read_instance_file：async 主函式
# ---------------------------------------------------------------------------


def test_list_instance_files_returns_parsed_files():
    ssh = RecordingFakeSSH([(lambda c: c.startswith("find"), "train.py\nmodel.py\n")])
    result = asyncio.run(list_instance_files(ssh, "server-a", "/data/proj1", []))
    assert result == {"files": ["train.py", "model.py"]}


def test_list_instance_files_ssh_failure_returns_error_not_exception():
    async def failing_ssh(server_name, command, timeout):
        raise ConnectionError("simulated ssh failure")

    result = asyncio.run(list_instance_files(failing_ssh, "server-a", "/data/proj1"))
    assert "error" in result
    assert "server-a" in result["error"]


def test_read_instance_file_returns_content():
    ssh = RecordingFakeSSH([(lambda c: c.startswith("head"), "print('hello')\n")])
    result = asyncio.run(read_instance_file(ssh, "server-a", "/data/proj1", "train.py"))
    assert result == {"content": "print('hello')\n"}


def test_read_instance_file_ssh_failure_returns_error_not_exception():
    async def failing_ssh(server_name, command, timeout):
        raise ConnectionError("simulated ssh failure")

    result = asyncio.run(read_instance_file(failing_ssh, "server-a", "/data/proj1", "train.py"))
    assert "error" in result


def test_read_instance_file_rejects_path_traversal_without_any_ssh_call():
    ssh = RecordingFakeSSH([])
    result = asyncio.run(read_instance_file(ssh, "server-a", "/data/proj1", "../secret.txt"))
    assert "error" in result
    assert ssh.calls == []


def test_read_instance_file_rejects_absolute_path_without_any_ssh_call():
    ssh = RecordingFakeSSH([])
    result = asyncio.run(read_instance_file(ssh, "server-a", "/data/proj1", "/etc/passwd"))
    assert "error" in result
    assert ssh.calls == []


def test_read_instance_file_rejects_secret_filename_without_any_ssh_call():
    ssh = RecordingFakeSSH([])
    result = asyncio.run(read_instance_file(ssh, "server-a", "/data/proj1", ".env"))
    assert "error" in result
    assert ssh.calls == []


# ---------------------------------------------------------------------------
# resolve_project_instance：依 project(+server) 找唯一 instance
# ---------------------------------------------------------------------------


def test_resolve_project_instance_no_instances_raises(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    with pytest.raises(ProjectInstanceResolutionError):
        resolve_project_instance(db, "proj1", None)


def test_resolve_project_instance_auto_selects_single_instance(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    instance = resolve_project_instance(db, "proj1", None)
    assert instance.server == "server-a"


def test_resolve_project_instance_multiple_without_server_raises(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    with pytest.raises(ProjectInstanceResolutionError):
        resolve_project_instance(db, "proj1", None)


def test_resolve_project_instance_multiple_with_server_selects_match(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1-a")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1-b")
    instance = resolve_project_instance(db, "proj1", "server-b")
    assert instance.path == "/data/proj1-b"


def test_resolve_project_instance_server_not_registered_raises(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    with pytest.raises(ProjectInstanceResolutionError):
        resolve_project_instance(db, "proj1", "server-nope")


# ---------------------------------------------------------------------------
# GET /projects/{name}/files、GET /projects/{name}/file
# ---------------------------------------------------------------------------


class FilesFakeSSH:
    """依指令內容回傳固定假輸出：`find` -> 兩個檔案；`head` -> 檔案內容。"""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if command.startswith("find"):
            return FakeResult("train.py\nmodel.py\n")
        if command.startswith("head"):
            return FakeResult("print('hi')\n")
        return FakeResult("")


def test_get_project_files_endpoint_404_for_unknown_project(api_client):
    client, _main = api_client
    resp = client.get("/projects/nope/files")
    assert resp.status_code == 404


def test_get_project_files_endpoint_400_when_multiple_instances_and_no_server(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1-a")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1-b")

    resp = client.get("/projects/proj1/files")
    assert resp.status_code == 400


def test_get_project_files_endpoint_success(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["server"] == "server-a"
    assert body["files"] == ["train.py", "model.py"]

    events = client.get("/events").json()
    assert any(e["action"] == "project_file_list" for e in events)


def test_get_project_files_endpoint_rejects_subdir_path_traversal(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/files?subdir=../../etc")
    assert resp.status_code == 400
    assert ssh.calls == []


def test_get_project_file_endpoint_success(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/file?path=train.py")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "print('hi')\n"


def test_get_project_file_endpoint_rejects_path_traversal_without_ssh_call(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/file?path=../secret.txt")
    assert resp.status_code == 400
    assert ssh.calls == []


def test_get_project_file_endpoint_rejects_secret_filename(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/file?path=.env")
    assert resp.status_code == 400
    assert ssh.calls == []


def test_get_project_file_endpoint_404_for_unknown_project(api_client):
    client, _main = api_client
    resp = client.get("/projects/nope/file?path=train.py")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# agent 工具：list_project_files／read_project_file
# ---------------------------------------------------------------------------


def test_agent_tool_list_project_files_unknown_project_returns_error(db):
    ctx = _make_ctx(db)
    result = asyncio.run(dispatch_tool("list_project_files", {"project_name": "nope"}, ctx))
    assert "error" in result


def test_agent_tool_list_project_files_success(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    ctx = _make_ctx(db, ssh_run=ssh)
    result = asyncio.run(dispatch_tool("list_project_files", {"project_name": "proj1"}, ctx))
    assert result["files"] == ["train.py", "model.py"]


def test_agent_tool_list_project_files_multiple_instances_requires_server(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1-a")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1-b")
    ctx = _make_ctx(db, ssh_run=FilesFakeSSH())
    result = asyncio.run(dispatch_tool("list_project_files", {"project_name": "proj1"}, ctx))
    assert "error" in result


def test_agent_tool_read_project_file_success(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    ctx = _make_ctx(db, ssh_run=ssh)
    result = asyncio.run(
        dispatch_tool("read_project_file", {"project_name": "proj1", "file_path": "train.py"}, ctx)
    )
    assert result["content"] == "print('hi')\n"


def test_agent_tool_read_project_file_rejects_path_traversal_without_ssh(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = FilesFakeSSH()
    ctx = _make_ctx(db, ssh_run=ssh)
    result = asyncio.run(
        dispatch_tool(
            "read_project_file", {"project_name": "proj1", "file_path": "../secret.txt"}, ctx
        )
    )
    assert "error" in result
    assert ssh.calls == []


def test_agent_tool_read_project_file_missing_file_path_returns_error(db):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ctx = _make_ctx(db, ssh_run=FilesFakeSSH())
    result = asyncio.run(dispatch_tool("read_project_file", {"project_name": "proj1"}, ctx))
    assert "error" in result
