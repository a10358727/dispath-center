"""階段 15 Phase C（PLAN.md P.3 節）：kind=project_deploy 核准流——把中央
hub（`app.hub`）目前的某個 ref 部署成另一台機器上一個全新的
`project_instance`。**來源固定是 Server A hub**，不支援從任意 instance
commit 直接部署（要部署先 `hub-sync`）。

涵蓋：
- `request_project_deploy_approval()`：target 不存在/disabled、target 已有
  instance、hub 不存在、ref 不合法/解析不到、dest_path 不合法（相對路徑／
  `..`／禁止根／已存在非空）、無 project_roots 且未給 dest → 400；成功
  payload shape（dest 預設計算、ref 預設 HEAD branch）。
- `approve()` 的 project_deploy 分支：成功路徑完整指令序列；instance 寫入
  （branch/commit）；稽核；失敗路徑（clone 失敗 → rejected，note 含清理
  指引，不 rm）；雙重防線（approve 時 target 已有 instance → rejected）。
- 永不自動核准（kind=any 也不行）。
- `POST /projects/{name}/deploy-request` 端點（404/400 對應）。
- 前端 smoke。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.approvals import approve, maybe_auto_approve
from app.audit import read_audit
from app.config import AppConfig, ServerConfig
from app.db import VALID_APPROVAL_KINDS
from app.hub import (
    InvalidProjectDeployRequestError,
    build_deploy_push_command,
    hub_repo_path,
    local_deploy_bundle_path,
    request_project_deploy_approval,
)
from app.identity import Actor, ActorType, RequestContext, generate_session_token


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


def _setup_project(db, server="server-a", path="/data/proj1"):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server=server, path=path)


def _make_target_server(name="server-b", project_roots=None, enabled=True, port=22) -> ServerConfig:
    return ServerConfig(
        name=name,
        host="10.0.0.6",
        user="train",
        key="~/.ssh/id_rsa",
        port=port,
        project_roots=list(project_roots or []),
        enabled=enabled,
    )


def _make_config(tmp_path, servers) -> AppConfig:
    return AppConfig(servers=servers, local_home_dir=str(tmp_path))


def _make_hub(tmp_path, project="proj1"):
    repo_dir = Path(hub_repo_path(project, str(tmp_path)))
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


class DeployFakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class DeployFakeSSH:
    """target 機器的假 SSH——同時服務 request 階段的唯讀確認（test -e／
    ls -A）與 approve 階段的實際執行（mkdir/clone/remote remove/rev-parse）。
    """

    def __init__(
        self,
        *,
        dest_exists: bool = False,
        dest_empty: bool = True,
        mkdir_ok: bool = True,
        clone_ok: bool = True,
        remote_remove_ok: bool = True,
        head: str = "deadbeef1234567890",
    ):
        self.calls: list[str] = []
        self.dest_exists = dest_exists
        self.dest_empty = dest_empty
        self.mkdir_ok = mkdir_ok
        self.clone_ok = clone_ok
        self.remote_remove_ok = remote_remove_ok
        self.head = head

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if "test -e" in command and "NOT_EXIST" in command:
            stdout = "" if self.dest_exists else "NOT_EXIST\n"
            return DeployFakeCommandResult(stdout=stdout)
        if command.startswith("ls -A"):
            stdout = "" if self.dest_empty else "somefile\n"
            return DeployFakeCommandResult(stdout=stdout)
        if command.startswith("mkdir -p"):
            ok = self.mkdir_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "mkdir failed"
            )
        if "git clone -b" in command:
            ok = self.clone_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "clone failed"
            )
        if "remote remove origin" in command:
            ok = self.remote_remove_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "remote remove failed"
            )
        if command.rstrip().endswith("rev-parse HEAD"):
            return DeployFakeCommandResult(stdout=f"{self.head}\n")
        return DeployFakeCommandResult()


class DeployRaisingSSH(DeployFakeSSH):
    def __init__(self, *, raise_on: str, **kwargs):
        super().__init__(**kwargs)
        self.raise_on = raise_on

    async def __call__(self, server, command, timeout):
        if self.raise_on in command:
            self.calls.append(command)
            raise OSError("simulated deploy response loss")
        return await super().__call__(server, command, timeout)


class DeployFakeLocalRun:
    """Server A 本地的假 local_run——服務 request 階段的 ref 解析與 hub_head
    讀取，以及 approve 階段的 bundle create/verify＋rsync 推送。"""

    def __init__(
        self,
        *,
        symbolic_ref: str = "main",
        abbrev_ref: str = "",
        verify_ref_ok: bool = True,
        hub_head: str = "cafef00d1234567890",
        create_ok: bool = True,
        verify_ok: bool = True,
        push_ok: bool = True,
    ):
        self.calls: list[str] = []
        self.symbolic_ref = symbolic_ref
        self.abbrev_ref = abbrev_ref
        self.verify_ref_ok = verify_ref_ok
        self.hub_head = hub_head
        self.create_ok = create_ok
        self.verify_ok = verify_ok
        self.push_ok = push_ok

    async def __call__(self, command, timeout):
        self.calls.append(command)
        if "symbolic-ref --short HEAD" in command:
            ok = bool(self.symbolic_ref)
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stdout=f"{self.symbolic_ref}\n" if ok else ""
            )
        if "rev-parse --abbrev-ref HEAD" in command:
            ok = bool(self.abbrev_ref)
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stdout=f"{self.abbrev_ref}\n" if ok else ""
            )
        if "rev-parse --verify" in command:
            ok = self.verify_ref_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "unknown revision"
            )
        if command.rstrip().endswith("rev-parse HEAD"):
            return DeployFakeCommandResult(stdout=f"{self.hub_head}\n")
        if "bundle create" in command:
            ok = self.create_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "bundle create failed"
            )
        if "bundle verify" in command:
            ok = self.verify_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "bundle verify failed"
            )
        if "rsync" in command:
            ok = self.push_ok
            return DeployFakeCommandResult(
                exit_status=0 if ok else 1, stderr="" if ok else "rsync failed"
            )
        return DeployFakeCommandResult()


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------


def test_local_deploy_bundle_path_uses_local_home_dir():
    assert local_deploy_bundle_path(7, "/srv/dispatch") == "/srv/dispatch/hub_bundles/deploy_7.bundle"


def test_build_deploy_push_command_contains_expected_pieces():
    target = _make_target_server()
    cmd = build_deploy_push_command(3, "proj1", target, "/srv/dispatch")
    assert "mkdir -p deploy_bundles" in cmd
    assert "rsync -a -e" in cmd
    assert "/srv/dispatch/hub_bundles/deploy_3.bundle" in cmd
    assert "train@10.0.0.6:deploy_bundles/3.bundle" in cmd
    assert "-p 22" not in cmd


def test_build_deploy_push_command_non_default_port_appends_dash_p():
    target = _make_target_server(port=32221)
    cmd = build_deploy_push_command(3, "proj1", target, "/srv/dispatch")
    assert "-p 32221" in cmd


def test_project_deploy_kind_registered_in_valid_kinds():
    assert "project_deploy" in VALID_APPROVAL_KINDS


# ---------------------------------------------------------------------------
# request_project_deploy_approval()：建立請求時的驗證
# ---------------------------------------------------------------------------


def test_request_deploy_target_not_found_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    config = _make_config(tmp_path, [])
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b",
                config=config, server_configs={}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_target_disabled_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"], enabled=False)
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_target_already_has_instance_rejected(db, audit_path, tmp_path):
    _setup_project(db, server="server-a")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_hub_not_exists_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    # 刻意不建立 hub 目錄。
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError) as excinfo:
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert "hub 同步" in str(excinfo.value)
    assert db.list_approvals() == []
    # hub 不存在時不該發生任何 local_run/ssh 呼叫。
    assert local_run.calls == []
    assert ssh.calls == []


def test_request_deploy_ref_bad_chars_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b", ref="bad ref!",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_ref_unresolvable_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun(verify_ref_ok=False)
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b", ref="feature-x",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_head_branch_unresolvable_rejected(db, audit_path, tmp_path):
    """ref 沒提供時預設用 hub HEAD 的 branch 名：symbolic-ref／
    rev-parse --abbrev-ref 都解不出（例如 hub 剛 init --bare 完還沒
    fetch）→ 400，提示先 hub 同步。"""
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun(symbolic_ref="", abbrev_ref="")
    with pytest.raises(InvalidProjectDeployRequestError) as excinfo:
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert "hub 同步" in str(excinfo.value)
    assert db.list_approvals() == []


def test_request_deploy_dest_relative_path_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b", dest_path="relative/path",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_dest_dotdot_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b", dest_path="/data/../etc/passwd",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_dest_forbidden_root_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b", dest_path="/etc",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_dest_already_exists_nonempty_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH(dest_exists=True, dest_empty=False)
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b", dest_path="/home/bgab141/Howard/proj1",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_dest_exists_empty_allowed(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH(dest_exists=True, dest_empty=True)
    local_run = DeployFakeLocalRun()
    approval = asyncio.run(
        request_project_deploy_approval(
            db, "proj1", "server-b", dest_path="/home/bgab141/Howard/proj1",
            config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
            audit_path=audit_path,
        )
    )
    assert approval.status == "pending"


def test_request_deploy_no_project_roots_no_dest_rejected(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=[])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    with pytest.raises(InvalidProjectDeployRequestError):
        asyncio.run(
            request_project_deploy_approval(
                db, "proj1", "server-b",
                config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
                audit_path=audit_path,
            )
        )
    assert db.list_approvals() == []


def test_request_deploy_success_default_dest_and_ref(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun(symbolic_ref="main", hub_head="cafef00d1234567890")
    request_context = RequestContext(
        actor=Actor(
            id="deploy-requester",
            actor_type=ActorType.SERVICE,
            display_name="deploy automation",
            email="must-not-appear@example.invalid",
        ),
        authentication_method="service_token",
        service_token_id="must-not-appear-token-row",
        service_scopes=frozenset({"must-not-appear.scope"}),
    )

    approval = asyncio.run(
        request_project_deploy_approval(
            db, "proj1", "server-b",
            config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
            audit_path=audit_path,
            request_context=request_context,
        )
    )
    assert approval.kind == "project_deploy"
    assert approval.status == "pending"
    assert approval.payload["project"] == "proj1"
    assert approval.payload["target_server"] == "server-b"
    assert approval.payload["dest_path"] == "/home/bgab141/Howard/proj1"
    assert approval.payload["ref"] == "main"
    assert approval.payload["hub_head"] == "cafef00d1234567890"
    assert approval.requester_actor_id == "deploy-requester"

    records = read_audit(audit_path)
    requested = next(r for r in records if r["action"] == "approval_requested")
    assert requested["actor"] == {
        "id": "deploy-requester",
        "kind": "service",
        "authentication": "service_token",
    }
    serialized = Path(audit_path).read_text(encoding="utf-8")
    assert "must-not-appear@example.invalid" not in serialized
    assert "must-not-appear-token-row" not in serialized
    assert "must-not-appear.scope" not in serialized


def test_request_deploy_explicit_ref_and_dest(db, audit_path, tmp_path):
    _setup_project(db)
    _make_hub(tmp_path)
    target = _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun(verify_ref_ok=True)

    approval = asyncio.run(
        request_project_deploy_approval(
            db, "proj1", "server-b", dest_path="/home/bgab141/custom", ref="feature-x",
            config=config, server_configs={"server-b": target}, ssh_run=ssh, local_run=local_run,
            audit_path=audit_path,
        )
    )
    assert approval.payload["dest_path"] == "/home/bgab141/custom"
    assert approval.payload["ref"] == "feature-x"
    assert any("rev-parse --verify" in c and "refs/heads/feature-x" in c for c in local_run.calls)


# ---------------------------------------------------------------------------
# approve()：project_deploy 分支
# ---------------------------------------------------------------------------


class _FakeAppState:
    def __init__(self, config):
        self.config = config


def _create_pending_deploy_approval(db, audit_path, tmp_path, *, target=None, dest_path=None, ref=None):
    _setup_project(db)
    _make_hub(tmp_path)
    target = target or _make_target_server(project_roots=["/home/bgab141/Howard"])
    config = _make_config(tmp_path, [target])
    ssh = DeployFakeSSH()
    local_run = DeployFakeLocalRun()
    approval = asyncio.run(
        request_project_deploy_approval(
            db, "proj1", target.name, dest_path=dest_path, ref=ref,
            config=config, server_configs={target.name: target}, ssh_run=ssh, local_run=local_run,
            audit_path=audit_path,
        )
    )
    return approval, target, config


def test_approve_project_deploy_success_full_command_sequence(db, audit_path, tmp_path):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)

    approve_ssh = DeployFakeSSH(head="deadbeef1234567890")
    approve_local_run = DeployFakeLocalRun()

    result = asyncio.run(
        approve(
            db, approval.id,
            ssh_run=approve_ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=approve_local_run,
        )
    )

    approved = result["approval"]
    assert approved.status == "approved"
    assert "deadbeef" in approved.note
    assert target.name in approved.note

    local_calls = approve_local_run.calls
    assert any("bundle create" in c and "--git-dir=" in c for c in local_calls)
    assert any("bundle verify" in c and "--git-dir=" in c for c in local_calls)
    assert any("rsync" in c and "-p 22" not in c for c in local_calls)

    idx_create = next(i for i, c in enumerate(local_calls) if "bundle create" in c)
    idx_verify = next(i for i, c in enumerate(local_calls) if "bundle verify" in c)
    idx_push = next(i for i, c in enumerate(local_calls) if "rsync" in c)
    assert idx_create < idx_verify < idx_push

    ssh_calls = approve_ssh.calls
    assert any(c.startswith("mkdir -p") for c in ssh_calls)
    assert any("git clone -b" in c and "main" in c for c in ssh_calls)
    assert any("remote remove origin" in c for c in ssh_calls)
    assert any(c.rstrip().endswith("rev-parse HEAD") for c in ssh_calls)

    idx_mkdir = next(i for i, c in enumerate(ssh_calls) if c.startswith("mkdir -p"))
    idx_clone = next(i for i, c in enumerate(ssh_calls) if "git clone -b" in c)
    idx_remove = next(i for i, c in enumerate(ssh_calls) if "remote remove origin" in c)
    idx_head = next(i for i, c in enumerate(ssh_calls) if c.rstrip().endswith("rev-parse HEAD"))
    assert idx_mkdir < idx_clone < idx_remove < idx_head

    instances = db.list_project_instances("proj1")
    target_instance = next(i for i in instances if i.server == target.name)
    assert target_instance.git_branch == "main"
    assert target_instance.git_commit == "deadbeef1234567890"
    assert target_instance.path == "/home/bgab141/Howard/proj1"

    records = read_audit(audit_path)
    deploy_events = [r for r in records if r["action"] == "project_deploy"]
    assert len(deploy_events) == 1
    event = deploy_events[0]
    assert event["result"] == "ok"
    assert event["params"]["project"] == "proj1"
    assert event["params"]["target_server"] == target.name
    assert event["params"]["dest_path"] == "/home/bgab141/Howard/proj1"
    assert event["params"]["ref"] == "main"
    assert event["params"]["head"] == "deadbeef1234567890"

    durable = db.list_durable_audit_events(limit=100)
    intent = next(e for e in durable if e["action"] == "project_deploy_intent")
    outcome = next(
        e
        for e in durable
        if e["action"] == "project_deploy_outcome" and e["result"] == "applied"
    )
    assert intent["approval_id"] == approval.id
    assert outcome["approval_id"] == approval.id
    assert intent["resource_id"] == outcome["resource_id"]
    assert len(intent["params"]["payload_sha256"]) == 64
    assert len(intent["params"]["dest_path_sha256"]) == 64
    assert "dest_path" not in intent["params"]
    assert "dest_path" not in outcome["params"]
    assert any(
        event["action"] == "project_instance_created"
        and event["approval_id"] == approval.id
        for event in durable
    )
    assert any(
        event["action"] == "approval_decided"
        and event["approval_id"] == approval.id
        and event["result"] == "approved"
        for event in durable
    )


def test_project_deploy_outcome_audit_failure_rolls_back_local_projection(
    monkeypatch, db, audit_path, tmp_path
):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)
    original = db.append_durable_audit_event_in_transaction

    def fail_applied(cursor, **kwargs):
        if kwargs.get("action") == "project_deploy_outcome" and kwargs.get("result") == "applied":
            raise RuntimeError("injected deploy outcome append failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_applied)
    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=DeployFakeSSH(head="deadbeef1234567890"),
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=DeployFakeLocalRun(),
        )
    )
    assert result["approval"].status == "pending"
    assert not any(i.server == target.name for i in db.list_project_instances("proj1"))
    assert db.list_project_versions("proj1") == []
    events = db.list_durable_audit_events(limit=100)
    assert any(e["action"] == "project_deploy_intent" for e in events)
    assert any(
        e["action"] == "project_deploy_outcome" and e["result"] == "unknown"
        for e in events
    )
    assert not any(
        e["action"] == "project_deploy_outcome" and e["result"] == "applied"
        for e in events
    )


def test_project_deploy_response_loss_does_not_replay_and_reconciles(
    db, audit_path, tmp_path
):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)
    with pytest.raises(OSError, match="response loss"):
        asyncio.run(
            approve(
                db,
                approval.id,
                ssh_run=DeployRaisingSSH(raise_on="git clone -b"),
                audit_path=audit_path,
                server_configs={target.name: target},
                app_state=app_state,
                local_run=DeployFakeLocalRun(),
            )
        )
    assert db.get_approval(approval.id).status == "pending"
    assert not any(i.server == target.name for i in db.list_project_instances("proj1"))

    reconcile_ssh = DeployFakeSSH(
        dest_exists=True,
        head="reconciled-deploy-commit",
    )
    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=reconcile_ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=DeployFakeLocalRun(),
        )
    )
    assert result["approval"].status == "approved"
    assert not any("git clone -b" in command for command in reconcile_ssh.calls)
    instance = next(
        i for i in db.list_project_instances("proj1") if i.server == target.name
    )
    assert instance.git_commit == "reconciled-deploy-commit"
    outcomes = [
        e
        for e in db.list_durable_audit_events(limit=100)
        if e["action"] == "project_deploy_outcome"
    ]
    assert {e["result"] for e in outcomes} == {"unknown", "applied"}


def test_approve_project_deploy_creates_project_version(db, audit_path, tmp_path):
    """PLAN.md 2026-07-11 版 §14 切片 4:部署成功後,以目標機記錄的完整
    commit 登記一筆 ProjectVersion(`git_ref`＝部署的 ref;沒有來源
    instance,`source_instance_id` 是 None——方向是 hub → 新 instance)。"""
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)

    result = asyncio.run(
        approve(
            db, approval.id,
            ssh_run=DeployFakeSSH(head="deadbeef1234567890"),
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=DeployFakeLocalRun(),
        )
    )
    assert result["approval"].status == "approved"

    versions = db.list_project_versions("proj1")
    assert len(versions) == 1
    assert versions[0].git_commit == "deadbeef1234567890"
    assert versions[0].git_ref == "main"
    assert versions[0].source_instance_id is None


def test_approve_project_deploy_reuses_project_version_from_prior_hub_sync(db, audit_path, tmp_path):
    """部署的 commit 若已經被(較早的)hub_sync 登記過同一筆 ProjectVersion
    ——不應該重複建立第二筆。"""
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    existing = db.get_or_create_project_version(
        "proj1", "deadbeef1234567890", git_ref="main", source_instance_id="prior-instance"
    )
    app_state = _FakeAppState(config)

    asyncio.run(
        approve(
            db, approval.id,
            ssh_run=DeployFakeSSH(head="deadbeef1234567890"),
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=DeployFakeLocalRun(),
        )
    )

    versions = db.list_project_versions("proj1")
    assert len(versions) == 1
    assert versions[0].id == existing.id
    assert versions[0].source_instance_id == "prior-instance"  # 沒有被覆寫


def test_approve_project_deploy_non_default_port_rsync_includes_dash_p(db, audit_path, tmp_path):
    target = _make_target_server(project_roots=["/home/bgab141/Howard"], port=32221)
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path, target=target)
    app_state = _FakeAppState(config)

    approve_ssh = DeployFakeSSH()
    approve_local_run = DeployFakeLocalRun()
    asyncio.run(
        approve(
            db, approval.id,
            ssh_run=approve_ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=approve_local_run,
        )
    )
    assert any("-p 32221" in c for c in approve_local_run.calls)


def test_approve_project_deploy_clone_failure_rejected_no_cleanup(db, audit_path, tmp_path):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)

    approve_ssh = DeployFakeSSH(clone_ok=False)
    approve_local_run = DeployFakeLocalRun()

    result = asyncio.run(
        approve(
            db, approval.id,
            ssh_run=approve_ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=approve_local_run,
        )
    )
    approved = result["approval"]
    assert approved.status == "rejected"
    assert "clone" in approved.note.lower() or "步驟 3" in approved.note
    assert "手動移除" in approved.note

    # 失敗不自動 rm 任何東西。
    assert not any("rm -rf" in c for c in approve_ssh.calls)
    assert not any("rm -rf" in c for c in approve_local_run.calls)

    # clone 失敗，不該繼續往下走（沒有 remove origin / rev-parse HEAD）。
    assert not any("remote remove origin" in c for c in approve_ssh.calls)
    assert not any(c.rstrip().endswith("rev-parse HEAD") for c in approve_ssh.calls)

    # 沒有新建任何 instance。
    instances = db.list_project_instances("proj1")
    assert not any(i.server == target.name for i in instances)

    records = read_audit(audit_path)
    deploy_events = [r for r in records if r["action"] == "project_deploy"]
    assert len(deploy_events) == 1
    assert deploy_events[0]["result"] == "rejected"


def test_approve_project_deploy_bundle_create_failure_no_cleanup_hint(db, audit_path, tmp_path):
    """步驟 1（建立 bundle）失敗時完全沒動任何檔案，note 不用附清理指引。"""
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)

    approve_ssh = DeployFakeSSH()
    approve_local_run = DeployFakeLocalRun(create_ok=False)

    result = asyncio.run(
        approve(
            db, approval.id,
            ssh_run=approve_ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=approve_local_run,
        )
    )
    assert result["approval"].status == "rejected"
    assert approve_ssh.calls == []
    assert not any("bundle verify" in c for c in approve_local_run.calls)
    assert not any("rsync" in c for c in approve_local_run.calls)


def test_approve_project_deploy_double_defense_existing_instance_rejected(db, audit_path, tmp_path):
    """P.3 雙重防線：request 建立時 target 尚無 instance，但 approve 當下
    重查（等待期間可能有人手動部署了）發現已經有了 -> rejected，不做任何
    寫入，也不會發起任何本地/遠端指令。"""
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    db.insert_project_instance(project_name="proj1", server=target.name, path="/somewhere/else")
    app_state = _FakeAppState(config)

    approve_ssh = DeployFakeSSH()
    approve_local_run = DeployFakeLocalRun()

    result = asyncio.run(
        approve(
            db, approval.id,
            ssh_run=approve_ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=app_state,
            local_run=approve_local_run,
        )
    )
    assert result["approval"].status == "rejected"
    assert approve_ssh.calls == []
    assert approve_local_run.calls == []

    records = read_audit(audit_path)
    deploy_events = [r for r in records if r["action"] == "project_deploy"]
    assert len(deploy_events) == 1
    assert deploy_events[0]["result"] == "rejected"


def test_approve_project_deploy_requires_local_run(db, audit_path, tmp_path):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)
    with pytest.raises(ValueError):
        asyncio.run(
            approve(
                db, approval.id,
                ssh_run=DeployFakeSSH(),
                audit_path=audit_path,
                server_configs={target.name: target},
                app_state=app_state,
                local_run=None,
            )
        )


def test_approve_project_deploy_requires_ssh_run(db, audit_path, tmp_path):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    app_state = _FakeAppState(config)
    with pytest.raises(ValueError):
        asyncio.run(
            approve(
                db, approval.id,
                ssh_run=None,
                audit_path=audit_path,
                server_configs={target.name: target},
                app_state=app_state,
                local_run=DeployFakeLocalRun(),
            )
        )


# ---------------------------------------------------------------------------
# project_deploy 永不進自動核准（K.3 白名單只認 enqueue/stop）
# ---------------------------------------------------------------------------


def test_project_deploy_never_auto_approved_even_with_any_rule(db, audit_path, tmp_path):
    approval, target, config = _create_pending_deploy_approval(db, audit_path, tmp_path)
    rules = [{"source": "any", "kind": "any"}]

    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=rules, audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# main.py：POST /projects/{name}/deploy-request
# ---------------------------------------------------------------------------


def _configure_target_server(main_module, tmp_path, *, project_roots=None, port=22):
    server_cfg = ServerConfig(
        name="server-b", host="10.0.0.6", user="train", key="~/.ssh/id_rsa",
        port=port, project_roots=list(project_roots or ["/home/bgab141/Howard"]),
    )
    main_module.app_state.server_configs = {"server-b": server_cfg}
    main_module.app_state.config.servers = [server_cfg]
    main_module.app_state.config.local_home_dir = str(tmp_path)
    return server_cfg


def test_project_deploy_request_endpoint_creates_pending_approval(api_client, tmp_path):
    client, main_module = api_client
    db = main_module.app_state.db
    _setup_project(db)
    _make_hub(tmp_path)
    _configure_target_server(main_module, tmp_path)
    main_module.app_state.ssh_run = DeployFakeSSH()
    main_module.local_run = DeployFakeLocalRun()

    resp = client.post(
        "/projects/proj1/deploy-request",
        json={"target_server": "server-b"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "project_deploy"
    assert body["status"] == "pending"
    assert body["payload"]["target_server"] == "server-b"
    assert body["payload"]["dest_path"] == "/home/bgab141/Howard/proj1"


def test_project_deploy_request_endpoint_project_not_found_404(api_client, tmp_path):
    client, main_module = api_client
    _configure_target_server(main_module, tmp_path)
    main_module.app_state.ssh_run = DeployFakeSSH()
    main_module.local_run = DeployFakeLocalRun()

    resp = client.post(
        "/projects/nope/deploy-request",
        json={"target_server": "server-b"},
    )
    assert resp.status_code == 404


def test_project_deploy_request_endpoint_rejects_unknown_target(api_client, tmp_path):
    client, main_module = api_client
    db = main_module.app_state.db
    _setup_project(db)
    _make_hub(tmp_path)
    _configure_target_server(main_module, tmp_path)
    main_module.app_state.ssh_run = DeployFakeSSH()
    main_module.local_run = DeployFakeLocalRun()

    resp = client.post(
        "/projects/proj1/deploy-request",
        json={"target_server": "server-does-not-exist"},
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_project_deploy_full_flow_via_api_approve_endpoint(api_client, tmp_path):
    client, main_module = api_client
    db = main_module.app_state.db
    _setup_project(db)
    _make_hub(tmp_path)
    _configure_target_server(main_module, tmp_path)
    main_module.app_state.ssh_run = DeployFakeSSH()
    main_module.local_run = DeployFakeLocalRun()

    create_resp = client.post(
        "/projects/proj1/deploy-request", json={"target_server": "server-b"}
    )
    assert create_resp.status_code == 200
    approval_id = create_resp.json()["id"]

    main_module.app_state.ssh_run = DeployFakeSSH()
    main_module.local_run = DeployFakeLocalRun()
    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["approval"]["status"] == "approved"

    instances = db.list_project_instances("proj1")
    assert any(i.server == "server-b" and i.git_commit is not None for i in instances)


# ---------------------------------------------------------------------------
# 前端 smoke：GET / 渲染 project_deploy 卡片與矩陣按鈕的關鍵字
# ---------------------------------------------------------------------------


