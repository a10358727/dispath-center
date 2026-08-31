"""階段 12（PLAN.md M 節）：AI 改碼層次一——讀檔＋diff 核准卡。

涵蓋 M.4 測試清單：
- 路徑穿越拒絕（`../`、絕對路徑、diff 內路徑三處：`--- a/`／`+++ b/`／
  `read_instance_file` 的 `rel_file`——後者在 tests/test_activity.py）。
- 秘密檔拒絕（diff 目標；讀檔的秘密檔測試同樣在 tests/test_activity.py）。
- diff 大小/格式驗證。
- 非 git repo 拒絕（`approve()` 的 apply_patch 分支）。
- `git apply --check` 失敗不留任何改動（不會有 checkout/apply --index/
  commit 指令被送出）。
- 成功路徑的 git 指令序列斷言（mock ssh_run／ssh_write_file 記錄呼叫）。
- apply_patch 不被 `maybe_auto_approve()` 自動核准（kind 白名單只有
  enqueue/stop，就算規則寫 kind="any" 也不行）。
- MCP bridge 三個新工具（list_project_files／read_project_file／
  request_apply_patch）在 tests/test_mcp_bridge.py。
- vLLM `app/agent_tools.py` 沒有 apply_patch 相關工具（靜態掃描）。
- 前端 smoke（`GET /` 含 apply_patch 渲染關鍵字）。
"""

from __future__ import annotations

import ast
import asyncio

import pytest

from app.approvals import (
    InvalidApplyPatchRequestError,
    approve,
    maybe_auto_approve,
    request_apply_patch_approval,
)
from app.audit import read_audit
from app.db import VALID_APPROVAL_KINDS
from app.identity import ActorType, generate_session_token


def _login(client, main_module):
    """Login-first root (`GET /`) now requires an authenticated context to
    serve the Studio shell (DG-STUDIO-UI v1 P3-4); these front-end smoke tests only pin static
    markup, so a throwaway human actor + session is the simplest fix
    (mirrors `tests/test_identity_workspace_v2.py::_session_for`)."""

    actor = main_module.app_state.db.insert_actor(
        actor_type=ActorType.HUMAN, display_name="Workspace Smoke Test"
    )
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)


# ---------------------------------------------------------------------------
# request_apply_patch_approval：建立請求時的驗證
# ---------------------------------------------------------------------------

VALID_DIFF = (
    "diff --git a/train.py b/train.py\n"
    "--- a/train.py\n"
    "+++ b/train.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old line\n"
    "+new line\n"
)


def _setup_project(db, server="server-a", path="/data/proj1"):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server=server, path=path)


def test_apply_patch_kind_registered_in_valid_kinds():
    assert "apply_patch" in VALID_APPROVAL_KINDS


def test_request_apply_patch_approval_creates_pending(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, description="fix bug", audit_path=audit_path
    )
    assert approval.kind == "apply_patch"
    assert approval.status == "pending"
    assert approval.payload["project"] == "proj1"
    assert approval.payload["server"] == "server-a"
    assert approval.payload["diff"] == VALID_DIFF
    assert approval.payload["description"] == "fix bug"

    records = read_audit(audit_path)
    assert any(r["action"] == "approval_requested" for r in records)


def test_request_apply_patch_approval_no_instance_rejected(db, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(
            db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
        )
    assert db.list_approvals() == []


def test_request_apply_patch_approval_wrong_server_rejected(db, audit_path):
    _setup_project(db, server="server-a")
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(
            db, "proj1", "server-b", VALID_DIFF, audit_path=audit_path
        )
    assert db.list_approvals() == []


def test_request_apply_patch_approval_empty_diff_rejected(db, audit_path):
    _setup_project(db)
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(db, "proj1", "server-a", "", audit_path=audit_path)
    assert db.list_approvals() == []


def test_request_apply_patch_approval_too_large_diff_rejected(db, audit_path):
    _setup_project(db)
    huge_diff = VALID_DIFF + ("x" * (100 * 1024 + 1))
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(db, "proj1", "server-a", huge_diff, audit_path=audit_path)
    assert db.list_approvals() == []


def test_request_apply_patch_approval_not_diff_like_rejected(db, audit_path):
    _setup_project(db)
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(
            db, "proj1", "server-a", "please just fix the bug for me", audit_path=audit_path
        )
    assert db.list_approvals() == []


def test_request_apply_patch_approval_rejects_dotdot_target_path(db, audit_path):
    _setup_project(db)
    diff = (
        "--- a/../../etc/passwd\n"
        "+++ b/../../etc/passwd\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(db, "proj1", "server-a", diff, audit_path=audit_path)
    assert db.list_approvals() == []


def test_request_apply_patch_approval_rejects_absolute_target_path(db, audit_path):
    _setup_project(db)
    diff = (
        "--- a//etc/passwd\n"
        "+++ b//etc/passwd\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(db, "proj1", "server-a", diff, audit_path=audit_path)
    assert db.list_approvals() == []


def test_request_apply_patch_approval_rejects_secret_target_path(db, audit_path):
    _setup_project(db)
    diff = (
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    with pytest.raises(InvalidApplyPatchRequestError):
        request_apply_patch_approval(db, "proj1", "server-a", diff, audit_path=audit_path)
    assert db.list_approvals() == []


def test_request_apply_patch_approval_allows_dev_null_targets(db, audit_path):
    """新增檔案的 diff：`--- /dev/null`——不是路徑穿越，應該放行。"""
    _setup_project(db)
    diff = (
        "diff --git a/new_file.py b/new_file.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new_file.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('hi')\n"
    )
    approval = request_apply_patch_approval(db, "proj1", "server-a", diff, audit_path=audit_path)
    assert approval.status == "pending"


# ---------------------------------------------------------------------------
# approve()：apply_patch 分支——全部 SSH
# ---------------------------------------------------------------------------


class ApplyPatchFakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class ApplyPatchFakeSSH:
    """依關鍵字回傳預先設定好的結果，記錄呼叫過的每一條指令。"""

    def __init__(
        self,
        *,
        is_work_tree: bool = True,
        branch: str = "main",
        check_ok: bool = True,
        check_stderr: str = "patch does not apply",
        existing_branches: "set[str] | None" = None,
    ):
        self.calls: list[str] = []
        self.is_work_tree = is_work_tree
        self.branch = branch
        self.check_ok = check_ok
        self.check_stderr = check_stderr
        self.existing_branches = existing_branches or set()

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if "rev-parse --is-inside-work-tree" in command:
            return ApplyPatchFakeCommandResult(stdout="true\n" if self.is_work_tree else "")
        if "branch --show-current" in command:
            return ApplyPatchFakeCommandResult(stdout=f"{self.branch}\n")
        if "apply --check" in command:
            if self.check_ok:
                return ApplyPatchFakeCommandResult(exit_status=0)
            return ApplyPatchFakeCommandResult(stderr=self.check_stderr, exit_status=1)
        if "rev-parse --verify --quiet" in command:
            # 真實 git 是精確比對 ref 名，子字串比對會把 ai-patch-1 誤判成
            # ai-patch-1-2 的存在證據
            ref = command.rsplit("refs/heads/", 1)[1].strip()
            if ref in self.existing_branches:
                return ApplyPatchFakeCommandResult(stdout=f"{ref}\n")
            return ApplyPatchFakeCommandResult(stdout="")
        return ApplyPatchFakeCommandResult()


class ApplyPatchRaisingSSH(ApplyPatchFakeSSH):
    def __init__(self, *, raise_on: str, branch_heads: "dict[str, str] | None" = None, **kwargs):
        super().__init__(**kwargs)
        self.raise_on = raise_on
        self.branch_heads = branch_heads or {}

    async def __call__(self, server, command, timeout):
        if self.raise_on in command:
            self.calls.append(command)
            raise OSError("simulated apply_patch response loss")
        if "rev-parse --verify --quiet refs/heads/" in command:
            branch = command.rsplit("refs/heads/", 1)[1].strip()
            if branch in self.branch_heads:
                self.calls.append(command)
                return ApplyPatchFakeCommandResult(stdout=f"{self.branch_heads[branch]}\n")
        return await super().__call__(server, command, timeout)


class RecordingWriteFile:
    def __init__(self):
        self.writes: list[tuple[str, str, str]] = []

    async def __call__(self, server, path, content):
        self.writes.append((server, path, content))


class FakeAppState:
    def __init__(self, ssh_write_file):
        self.ssh_write_file = ssh_write_file


def test_approve_apply_patch_not_git_repo_rejected_no_changes(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    ssh = ApplyPatchFakeSSH(is_work_tree=False)
    write_file = RecordingWriteFile()
    app_state = FakeAppState(write_file)

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )
    assert result["approval"].status == "rejected"
    assert "git" in result["approval"].note
    assert write_file.writes == []
    assert not any("checkout -b" in c for c in ssh.calls)
    assert not any("commit" in c for c in ssh.calls)

    records = read_audit(audit_path)
    apply_patch_events = [r for r in records if r["action"] == "apply_patch"]
    assert len(apply_patch_events) == 1
    assert apply_patch_events[0]["result"] == "rejected"


def test_approve_apply_patch_check_failure_leaves_no_changes(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    ssh = ApplyPatchFakeSSH(check_ok=False, check_stderr="error: patch failed: train.py:1")
    write_file = RecordingWriteFile()
    app_state = FakeAppState(write_file)

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )
    assert result["approval"].status == "rejected"
    assert "patch failed" in result["approval"].note

    # diff 檔案已經寫過（第 3 步），但接下來完全沒有 checkout/apply --index/
    # commit——「不留任何改動」指的是不動這個 git repo 本身，agent_jobs 底下
    # 的暫存 diff 檔不算「改動」（同既有 cmd.sh/run.sh 的既有慣例）。
    assert len(write_file.writes) == 1
    assert not any("checkout -b" in c for c in ssh.calls)
    assert not any("apply --index" in c for c in ssh.calls)
    assert not any("commit" in c for c in ssh.calls)

    records = read_audit(audit_path)
    apply_patch_events = [r for r in records if r["action"] == "apply_patch"]
    assert len(apply_patch_events) == 1
    assert apply_patch_events[0]["result"] == "rejected"
    assert "patch failed" in apply_patch_events[0]["params"]["reason"]


def test_approve_apply_patch_success_full_command_sequence(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, description="fix off-by-one", audit_path=audit_path
    )
    ssh = ApplyPatchFakeSSH(branch="main")
    write_file = RecordingWriteFile()
    app_state = FakeAppState(write_file)

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )

    approved = result["approval"]
    assert approved.status == "approved"
    assert f"ai-patch-{approval.id}" in approved.note
    assert "git checkout main" in approved.note

    # diff 寫檔：路徑慣例同 agent_jobs/{id}/... （相對 home，見
    # app/jobqueue.py 的 AGENT_JOBS_DIR）。
    assert len(write_file.writes) == 1
    written_server, written_path, written_content = write_file.writes[0]
    assert written_server == "server-a"
    assert written_path == f"agent_jobs/patch_{approval.id}.diff"
    assert written_content == VALID_DIFF

    joined_calls = "\n".join(ssh.calls)
    assert "rev-parse --is-inside-work-tree" in joined_calls
    assert "branch --show-current" in joined_calls
    assert "mkdir -p agent_jobs" in joined_calls
    assert "apply --check" in joined_calls
    assert f"checkout -b ai-patch-{approval.id}" in joined_calls
    assert "apply --index" in joined_calls
    assert "commit -m" in joined_calls
    assert "user.name='dispatch-center'" in joined_calls
    assert "user.email='dispatch@local'" in joined_calls
    assert f"AI patch #{approval.id}: fix off-by-one" in joined_calls

    # 指令順序：is-inside-work-tree 在 branch --show-current 之前，
    # apply --check 在 checkout -b 之前，checkout -b 在 apply --index 之前，
    # apply --index 在 commit 之前。
    idx_worktree = next(i for i, c in enumerate(ssh.calls) if "is-inside-work-tree" in c)
    idx_branch = next(i for i, c in enumerate(ssh.calls) if "branch --show-current" in c)
    idx_check = next(i for i, c in enumerate(ssh.calls) if "apply --check" in c)
    idx_checkout = next(i for i, c in enumerate(ssh.calls) if "checkout -b" in c)
    idx_apply_index = next(i for i, c in enumerate(ssh.calls) if "apply --index" in c)
    idx_commit = next(i for i, c in enumerate(ssh.calls) if "commit -m" in c)
    assert idx_worktree < idx_branch < idx_check < idx_checkout < idx_apply_index < idx_commit

    records = read_audit(audit_path)
    apply_patch_events = [r for r in records if r["action"] == "apply_patch"]
    assert len(apply_patch_events) == 1
    event = apply_patch_events[0]
    assert event["params"]["project"] == "proj1"
    assert event["params"]["server"] == "server-a"
    assert event["params"]["original_branch"] == "main"
    assert event["params"]["new_branch"] == f"ai-patch-{approval.id}"
    assert event["params"]["diff"] == VALID_DIFF

    durable = db.list_durable_audit_events(limit=100)
    intent = next(e for e in durable if e["action"] == "project_apply_patch_intent")
    outcome = next(
        e
        for e in durable
        if e["action"] == "project_apply_patch_outcome" and e["result"] == "applied"
    )
    assert intent["approval_id"] == approval.id
    assert outcome["approval_id"] == approval.id
    assert intent["resource_id"] == outcome["resource_id"]
    assert intent["params"]["new_branch"] == f"ai-patch-{approval.id}"
    assert len(intent["params"]["payload_sha256"]) == 64
    assert "diff" not in intent["params"]
    assert "diff" not in outcome["params"]


def test_approve_apply_patch_remote_unknown_does_not_replay_and_can_reconcile(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    write_file = RecordingWriteFile()
    app_state = FakeAppState(write_file)

    first_ssh = ApplyPatchRaisingSSH(raise_on="commit -m")
    with pytest.raises(OSError):
        asyncio.run(
            approve(db, approval.id, ssh_run=first_ssh, audit_path=audit_path, app_state=app_state)
        )
    assert db.get_approval(approval.id).status == "pending"
    assert any(
        event["action"] == "project_apply_patch_outcome"
        and event["result"] == "unknown"
        for event in db.list_durable_audit_events(limit=100)
    )

    branch = f"ai-patch-{approval.id}"
    second_ssh = ApplyPatchRaisingSSH(
        raise_on="never-match",
        branch_heads={branch: "reconciled1234567890"},
    )
    second = asyncio.run(
        approve(db, approval.id, ssh_run=second_ssh, audit_path=audit_path, app_state=app_state)
    )
    assert second["approval"].status == "approved"
    assert any("refs/heads/" in command for command in second_ssh.calls)
    assert not any("checkout -b" in command for command in second_ssh.calls)
    assert not any("apply --index" in command for command in second_ssh.calls)
    assert not any("commit -m" in command for command in second_ssh.calls)
    outcomes = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "project_apply_patch_outcome"
    ]
    assert {event["result"] for event in outcomes} == {"unknown", "applied"}
    assert sum(
        event["action"] == "project_apply_patch_intent"
        for event in db.list_durable_audit_events(limit=100)
    ) == 1


def test_approve_apply_patch_outcome_audit_failure_keeps_pending(monkeypatch, db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_applied(cursor, **kwargs):
        if (
            kwargs.get("action") == "project_apply_patch_outcome"
            and kwargs.get("result") == "applied"
        ):
            raise RuntimeError("injected outcome append failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_applied)
    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=ApplyPatchFakeSSH(),
            audit_path=audit_path,
            app_state=FakeAppState(RecordingWriteFile()),
        )
    )
    assert result["approval"].status == "pending"
    events = db.list_durable_audit_events(limit=100)
    assert not any(
        event["action"] == "project_apply_patch_outcome"
        and event["result"] == "applied"
        for event in events
    )
    assert any(
        event["action"] == "project_apply_patch_outcome"
        and event["result"] == "unknown"
        for event in events
    )


def test_approve_apply_patch_branch_name_collision_gets_suffix(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    ssh = ApplyPatchFakeSSH(existing_branches={f"ai-patch-{approval.id}"})
    write_file = RecordingWriteFile()
    app_state = FakeAppState(write_file)

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )
    assert result["approval"].status == "approved"
    base_branch = f"ai-patch-{approval.id}"
    assert f"{base_branch}-2" in result["approval"].note
    assert any(c.rstrip().endswith(f"checkout -b {base_branch}-2") for c in ssh.calls)
    # 沒有任何一條指令是「用原本（未加序號）的名字」checkout -b（會跟
    # `.endswith(base_branch)` 混淆的是 `{base_branch}-2` 本身，用
    # `rstrip().endswith(f"checkout -b {base_branch}")`（不帶 `-2`）精確排除）。
    assert not any(c.rstrip().endswith(f"checkout -b {base_branch}") for c in ssh.calls)


def test_approve_apply_patch_requires_ssh_run(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    app_state = FakeAppState(RecordingWriteFile())
    with pytest.raises(ValueError):
        asyncio.run(approve(db, approval.id, ssh_run=None, audit_path=audit_path, app_state=app_state))


def test_approve_apply_patch_requires_app_state_with_ssh_write_file(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    ssh = ApplyPatchFakeSSH()
    with pytest.raises(ValueError):
        asyncio.run(approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=None))


# ---------------------------------------------------------------------------
# apply_patch 永不進自動核准（K.3 白名單只認 enqueue/stop）
# ---------------------------------------------------------------------------


def test_apply_patch_never_auto_approved_even_with_any_rule(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    rules = [{"source": "any", "kind": "any"}]

    result = asyncio.run(
        maybe_auto_approve(
            db, approval, source="chatgpt", rules=rules, audit_path=audit_path
        )
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


def test_apply_patch_never_auto_approved_without_rules(db, audit_path):
    _setup_project(db)
    approval = request_apply_patch_approval(
        db, "proj1", "server-a", VALID_DIFF, audit_path=audit_path
    )
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=[], audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# vLLM app/agent_tools.py 沒有 apply_patch 相關的任何工具（鐵律：PLAN.md
# M.2 裁定，7B 模型寫 diff 品質不可靠，只加在 MCP bridge）
# ---------------------------------------------------------------------------


def test_agent_tools_has_no_apply_patch_tool():
    from app.agent_tools import TOOLS

    forbidden = {"request_apply_patch", "apply_patch"}
    assert forbidden.isdisjoint(TOOLS.keys())


def test_agent_tools_module_does_not_import_request_apply_patch_approval():
    import app.agent_tools as agent_tools_module

    source = open(agent_tools_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)

    assert "request_apply_patch_approval" not in imported_names


# ---------------------------------------------------------------------------
# main.py：POST /projects/{name}/apply-patch-request
# ---------------------------------------------------------------------------


def test_apply_patch_request_endpoint_creates_pending_approval(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.post(
        "/projects/proj1/apply-patch-request",
        json={"server": "server-a", "diff": VALID_DIFF, "description": "fix bug"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "apply_patch"
    assert body["status"] == "pending"
    assert body["payload"]["diff"] == VALID_DIFF


def test_apply_patch_request_endpoint_rejects_invalid_diff(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.post(
        "/projects/proj1/apply-patch-request",
        json={"server": "server-a", "diff": "not a diff at all"},
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_apply_patch_request_endpoint_rejects_unknown_server(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.post(
        "/projects/proj1/apply-patch-request",
        json={"server": "server-b", "diff": VALID_DIFF},
    )
    assert resp.status_code == 400


def test_apply_patch_full_flow_via_api_approve_endpoint(api_client):
    """建立請求 -> POST /approve/{id} -> 真的走 apply_patch 分支（app_state
    注入假 ssh_run/ssh_write_file）。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    ssh = ApplyPatchFakeSSH(branch="main")
    write_file = RecordingWriteFile()
    main_module.app_state.ssh_run = ssh
    main_module.app_state.ssh_write_file = write_file

    create_resp = client.post(
        "/projects/proj1/apply-patch-request",
        json={"server": "server-a", "diff": VALID_DIFF, "description": "fix bug"},
    )
    approval_id = create_resp.json()["id"]

    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["approval"]["status"] == "approved"
    assert f"ai-patch-{approval_id}" in body["approval"]["note"]
    assert len(write_file.writes) == 1
    assert any("commit -m" in c for c in ssh.calls)


# ---------------------------------------------------------------------------
# 前端 smoke：GET / 渲染 apply_patch 卡片的關鍵字
# ---------------------------------------------------------------------------


