"""階段 15 Phase B（PLAN.md P.2.1 節）：kind=git_init 核准流——把一個尚未
受 git 管理的 project_instance 就地初始化成 git repo。

涵蓋：
- `build_default_gitignore()`：預設項齊全、extra 附加（純函式，不驗證
  pattern 字元集——那是 `request_git_init_approval()` 的責任）。
- `request_git_init_approval()`：instance 不存在／已是 git repo／
  extra_ignores 含非法字元 → 400（`InvalidGitInitRequestError`）；payload
  含 gitignore 全文。
- `approve()` 的 git_init 分支：成功路徑完整指令序列；size guard 超標 →
  rejected + rm -rf .git + note 含 MB 數；雙重防線（approve 時已是 git →
  rejected）；project_instances git 狀態更新；稽核。
- 永不自動核准（kind=any 也不行）。
- `POST /projects/{name}/git-init-request` 端點（400/404 對應）。
- 前端 smoke。
"""

from __future__ import annotations

import asyncio

import pytest

from app.approvals import (
    DEFAULT_GITIGNORE_DIRS,
    DEFAULT_GITIGNORE_FILE_PATTERNS,
    GIT_INIT_SIZE_GUARD_KB,
    InvalidGitInitRequestError,
    approve,
    build_default_gitignore,
    maybe_auto_approve,
    request_git_init_approval,
)
from app.audit import read_audit
from app.db import VALID_APPROVAL_KINDS


def _setup_project(db, server="server-a", path="/data/proj1"):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server=server, path=path)


# ---------------------------------------------------------------------------
# build_default_gitignore()：純函式
# ---------------------------------------------------------------------------


def test_build_default_gitignore_contains_all_defaults():
    text = build_default_gitignore()
    for entry in DEFAULT_GITIGNORE_DIRS:
        assert entry in text
    for entry in DEFAULT_GITIGNORE_FILE_PATTERNS:
        assert entry in text
    # 第一行是說明用途的中文註解。
    assert text.startswith("#")


def test_build_default_gitignore_no_extra_ignores_by_default():
    text = build_default_gitignore()
    assert "使用者指定" not in text


def test_build_default_gitignore_appends_extra_ignores():
    text = build_default_gitignore(["foo/", "*.custom"])
    assert "foo/" in text
    assert "*.custom" in text
    # 仍然保留全部預設項目。
    assert "checkpoints/" in text
    assert "*.pt" in text


def test_git_init_kind_registered_in_valid_kinds():
    assert "git_init" in VALID_APPROVAL_KINDS


# ---------------------------------------------------------------------------
# request_git_init_approval()：建立請求時的驗證
# ---------------------------------------------------------------------------


class GitInitFakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class GitInitFakeSSH:
    """依關鍵字回傳預先設定好的結果，記錄呼叫過的每一條指令（模式同
    tests/test_apply_patch.py 的 ApplyPatchFakeSSH）。"""

    def __init__(
        self,
        *,
        already_git: bool = False,
        count_objects_output: str = "size: 10\nsize-pack: 5\n",
        head: str = "abc123def4567890",
        branch: str = "main",
    ):
        self.calls: list[str] = []
        self.already_git = already_git
        self.count_objects_output = count_objects_output
        self.head = head
        self.branch = branch

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if "test -d" in command and "GIT_OK" in command:
            return GitInitFakeCommandResult(stdout="GIT_OK\n" if self.already_git else "")
        if "count-objects -v" in command:
            return GitInitFakeCommandResult(stdout=self.count_objects_output)
        if command.rstrip().endswith("rev-parse HEAD"):
            return GitInitFakeCommandResult(stdout=f"{self.head}\n")
        if "branch --show-current" in command:
            return GitInitFakeCommandResult(stdout=f"{self.branch}\n")
        return GitInitFakeCommandResult()


def test_request_git_init_approval_creates_pending(db, audit_path):
    _setup_project(db)
    ssh = GitInitFakeSSH(already_git=False)
    approval = asyncio.run(
        request_git_init_approval(
            db, "proj1", "server-a", ["foo/"], ssh_run=ssh, audit_path=audit_path
        )
    )
    assert approval.kind == "git_init"
    assert approval.status == "pending"
    assert approval.payload["project"] == "proj1"
    assert approval.payload["server"] == "server-a"
    assert approval.payload["extra_ignores"] == ["foo/"]
    assert "foo/" in approval.payload["gitignore"]
    assert "checkpoints/" in approval.payload["gitignore"]

    records = read_audit(audit_path)
    assert any(r["action"] == "approval_requested" for r in records)


def test_request_git_init_approval_no_instance_rejected(db, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    ssh = GitInitFakeSSH(already_git=False)
    with pytest.raises(InvalidGitInitRequestError):
        asyncio.run(
            request_git_init_approval(
                db, "proj1", "server-a", ssh_run=ssh, audit_path=audit_path
            )
        )
    assert db.list_approvals() == []
    # instance 不存在時不該發生任何 SSH 呼叫（resolve 在 SSH 檢查之前失敗）。
    assert ssh.calls == []


def test_request_git_init_approval_wrong_server_rejected(db, audit_path):
    _setup_project(db, server="server-a")
    ssh = GitInitFakeSSH(already_git=False)
    with pytest.raises(InvalidGitInitRequestError):
        asyncio.run(
            request_git_init_approval(
                db, "proj1", "server-b", ssh_run=ssh, audit_path=audit_path
            )
        )
    assert db.list_approvals() == []


def test_request_git_init_approval_already_git_rejected(db, audit_path):
    _setup_project(db)
    ssh = GitInitFakeSSH(already_git=True)
    with pytest.raises(InvalidGitInitRequestError):
        asyncio.run(
            request_git_init_approval(
                db, "proj1", "server-a", ssh_run=ssh, audit_path=audit_path
            )
        )
    assert db.list_approvals() == []


def test_request_git_init_approval_bad_extra_ignore_pattern_rejected(db, audit_path):
    _setup_project(db)
    ssh = GitInitFakeSSH(already_git=False)
    with pytest.raises(InvalidGitInitRequestError):
        asyncio.run(
            request_git_init_approval(
                db, "proj1", "server-a", ["foo; rm -rf /"], ssh_run=ssh, audit_path=audit_path
            )
        )
    assert db.list_approvals() == []
    # 字元集驗證在 SSH 檢查之前，不該發生任何 SSH 呼叫。
    assert ssh.calls == []


def test_request_git_init_approval_bad_extra_ignore_empty_string_rejected(db, audit_path):
    _setup_project(db)
    ssh = GitInitFakeSSH(already_git=False)
    with pytest.raises(InvalidGitInitRequestError):
        asyncio.run(
            request_git_init_approval(
                db, "proj1", "server-a", [""], ssh_run=ssh, audit_path=audit_path
            )
        )
    assert db.list_approvals() == []


# ---------------------------------------------------------------------------
# approve()：git_init 分支——全部 SSH
# ---------------------------------------------------------------------------


def test_approve_git_init_success_full_command_sequence(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    request_ssh = GitInitFakeSSH(already_git=False)
    approval = asyncio.run(
        request_git_init_approval(
            db, "proj1", "server-a", ssh_run=request_ssh, audit_path=audit_path
        )
    )

    approve_ssh = GitInitFakeSSH(
        already_git=False,
        count_objects_output="size: 10\nsize-pack: 5\n",
        head="deadbeef1234567890",
        branch="main",
    )
    result = asyncio.run(approve(db, approval.id, ssh_run=approve_ssh, audit_path=audit_path))

    approved = result["approval"]
    assert approved.status == "approved"
    assert "deadbeef" in approved.note

    calls = approve_ssh.calls
    joined = "\n".join(calls)
    assert "test -d" in joined and "GIT_OK" in joined
    assert "printf" in joined and ".gitignore" in joined
    assert "git -C" in joined and " init" in joined
    assert "add -A" in joined
    assert "count-objects -v" in joined
    assert "commit -m" in joined
    assert "user.name='dispatch-center'" in joined
    assert "user.email='dispatch@local'" in joined
    assert "rev-parse HEAD" in joined
    assert "branch --show-current" in joined
    assert f"approval #{approval.id}" in joined
    assert not any("rm -rf" in c for c in calls)

    # 指令順序：check -> gitignore -> init -> add -A -> count-objects ->
    # commit -> rev-parse HEAD。
    idx_check = next(i for i, c in enumerate(calls) if "GIT_OK" in c)
    idx_gitignore = next(i for i, c in enumerate(calls) if "printf" in c)
    idx_init = next(i for i, c in enumerate(calls) if c.rstrip().endswith(" init"))
    idx_add = next(i for i, c in enumerate(calls) if "add -A" in c)
    idx_count = next(i for i, c in enumerate(calls) if "count-objects -v" in c)
    idx_commit = next(i for i, c in enumerate(calls) if "commit -m" in c)
    idx_head = next(i for i, c in enumerate(calls) if c.rstrip().endswith("rev-parse HEAD"))
    assert idx_check < idx_gitignore < idx_init < idx_add < idx_count < idx_commit < idx_head

    # project_instances 的 git 狀態被更新。
    instances = db.list_project_instances("proj1")
    assert len(instances) == 1
    assert instances[0].git_branch == "main"
    assert instances[0].git_commit == "deadbeef1234567890"

    records = read_audit(audit_path)
    git_init_events = [r for r in records if r["action"] == "git_init"]
    assert len(git_init_events) == 1
    event = git_init_events[0]
    assert event["result"] == "ok"
    assert event["params"]["project"] == "proj1"
    assert event["params"]["server"] == "server-a"
    assert event["params"]["head"] == "deadbeef1234567890"
    assert event["params"]["staged_kb"] == 15


def test_approve_git_init_double_defense_already_git_rejected(db, audit_path):
    """P.2.1 雙重防線：request 建立時不是 git repo，但 approve 當下重查
    （狀態可能在等待期間變化）發現已經是 git repo 了 -> rejected，不做
    任何寫入。"""
    _setup_project(db)
    request_ssh = GitInitFakeSSH(already_git=False)
    approval = asyncio.run(
        request_git_init_approval(
            db, "proj1", "server-a", ssh_run=request_ssh, audit_path=audit_path
        )
    )

    approve_ssh = GitInitFakeSSH(already_git=True)
    result = asyncio.run(approve(db, approval.id, ssh_run=approve_ssh, audit_path=audit_path))
    assert result["approval"].status == "rejected"
    assert "git" in result["approval"].note

    assert not any("printf" in c for c in approve_ssh.calls)
    assert not any(c.rstrip().endswith(" init") for c in approve_ssh.calls)
    assert not any("commit" in c for c in approve_ssh.calls)

    instances = db.list_project_instances("proj1")
    assert instances[0].git_commit is None

    records = read_audit(audit_path)
    git_init_events = [r for r in records if r["action"] == "git_init"]
    assert len(git_init_events) == 1
    assert git_init_events[0]["result"] == "rejected"


def test_approve_git_init_size_guard_exceeds_rejected(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    request_ssh = GitInitFakeSSH(already_git=False)
    approval = asyncio.run(
        request_git_init_approval(
            db, "proj1", "server-a", ssh_run=request_ssh, audit_path=audit_path
        )
    )

    # size + size-pack 超過 GIT_INIT_SIZE_GUARD_KB（512000 KB = 500MB）。
    over_kb = GIT_INIT_SIZE_GUARD_KB + 1000
    approve_ssh = GitInitFakeSSH(
        already_git=False, count_objects_output=f"size: {over_kb}\nsize-pack: 0\n"
    )
    result = asyncio.run(approve(db, approval.id, ssh_run=approve_ssh, audit_path=audit_path))

    assert result["approval"].status == "rejected"
    note = result["approval"].note
    assert "MB" in note
    assert "extra_ignores" in note

    assert any("rm -rf" in c and ".git" in c for c in approve_ssh.calls)
    # 注意：`.gitignore` 的預設說明註解本身含有「commit」字樣（見
    # build_default_gitignore()），因此這裡精確比對真正的 commit 指令
    # （`commit -m`），不是子字串「commit」——避免誤判 printf 寫入
    # .gitignore 那一步為「有 commit」。
    assert not any("commit -m" in c for c in approve_ssh.calls)
    assert not any("rev-parse HEAD" in c for c in approve_ssh.calls)

    instances = db.list_project_instances("proj1")
    assert instances[0].git_commit is None

    records = read_audit(audit_path)
    git_init_events = [r for r in records if r["action"] == "git_init"]
    assert len(git_init_events) == 1
    assert git_init_events[0]["result"] == "rejected"
    assert git_init_events[0]["params"]["staged_kb"] == over_kb


def test_approve_git_init_requires_ssh_run(db, audit_path):
    _setup_project(db)
    request_ssh = GitInitFakeSSH(already_git=False)
    approval = asyncio.run(
        request_git_init_approval(
            db, "proj1", "server-a", ssh_run=request_ssh, audit_path=audit_path
        )
    )
    with pytest.raises(ValueError):
        asyncio.run(approve(db, approval.id, ssh_run=None, audit_path=audit_path))


# ---------------------------------------------------------------------------
# git_init 永不進自動核准（K.3 白名單只認 enqueue/stop）
# ---------------------------------------------------------------------------


def test_git_init_never_auto_approved_even_with_any_rule(db, audit_path):
    _setup_project(db)
    request_ssh = GitInitFakeSSH(already_git=False)
    approval = asyncio.run(
        request_git_init_approval(
            db, "proj1", "server-a", ssh_run=request_ssh, audit_path=audit_path
        )
    )
    rules = [{"source": "any", "kind": "any"}]

    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=rules, audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# main.py：POST /projects/{name}/git-init-request
# ---------------------------------------------------------------------------


def test_git_init_request_endpoint_creates_pending_approval(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)

    resp = client.post(
        "/projects/proj1/git-init-request",
        json={"server": "server-a", "extra_ignores": ["foo/"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "git_init"
    assert body["status"] == "pending"
    assert body["payload"]["extra_ignores"] == ["foo/"]


def test_git_init_request_endpoint_project_not_found_404(api_client):
    client, main_module = api_client
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)
    resp = client.post(
        "/projects/nope/git-init-request", json={"server": "server-a"}
    )
    assert resp.status_code == 404


def test_git_init_request_endpoint_rejects_already_git(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=True)

    resp = client.post(
        "/projects/proj1/git-init-request", json={"server": "server-a"}
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_git_init_request_endpoint_rejects_bad_extra_ignore(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)

    resp = client.post(
        "/projects/proj1/git-init-request",
        json={"server": "server-a", "extra_ignores": ["bad pattern with spaces"]},
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_git_init_full_flow_via_api_approve_endpoint(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)

    create_resp = client.post(
        "/projects/proj1/git-init-request", json={"server": "server-a"}
    )
    approval_id = create_resp.json()["id"]

    main_module.app_state.ssh_run = GitInitFakeSSH(already_git=False)
    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["approval"]["status"] == "approved"

    instances = db.list_project_instances("proj1")
    assert instances[0].git_commit is not None


# ---------------------------------------------------------------------------
# 前端 smoke：GET / 渲染 git_init 卡片與矩陣按鈕的關鍵字
# ---------------------------------------------------------------------------


def test_index_page_renders_git_init_kind(api_client):
    client, _main = api_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "git_init" in resp.text
    assert "git-init-request" in resp.text
    assert "hub-sync" in resp.text
    assert "git 化" in resp.text
