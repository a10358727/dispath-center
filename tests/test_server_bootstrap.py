"""Goal 3 Phase B server bootstrap tests（docs/GOAL_3_FUTURE_WORK_PLAN.md；
DG-B 核准見 docs/DECISIONS.md 2026-07-19）。

覆蓋：固定腳本與 SHA pin 的 DG-B 邊界（非 root、無 sudo/apt、無 GPU
驅動）、純函式（元件驗證/指令組裝/報告解析/capability 判定）、
request→approve 生命週期（FakeSSH pool，絕不碰真實機器）、server_add
的 bootstrap-report 閘（含「功能關閉時逐位元相容」）、auto-approval
白名單排除、API 路由預設隱藏。
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from app.approvals import (
    InvalidServerBootstrapRequestError,
    InvalidServerConfigError,
    ServerBootstrapDisabledError,
    approve,
    maybe_auto_approve,
    request_server_add_approval,
    request_server_bootstrap_approval,
)
from app.config import ServerConfig
from app.identity import ActorType, RequestContext
from app.provisioning import (
    BOOTSTRAP_ALLOWED_COMPONENTS,
    BOOTSTRAP_REMOTE_PATH,
    BOOTSTRAP_SCRIPT,
    BOOTSTRAP_SCRIPT_VERSION,
    bootstrap_script_sha256,
    build_bootstrap_command,
    evaluate_capabilities,
    parse_bootstrap_report,
    run_server_bootstrap,
    validate_bootstrap_components,
)
from app.sshpool import SSHUnreachableError


_GOOD_REPORT_LINE = (
    'BOOTSTRAP_REPORT:{"tmux":"present","rsync":"present",'
    '"git":"present","python-venv":"ready"}'
)


def _human_context(db, name: str = "Ada") -> RequestContext:
    actor = db.insert_actor(
        actor_type=ActorType.HUMAN, display_name=name, platform_admin=True
    )
    return RequestContext(actor=actor, authentication_method="session")


def _config(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(server_bootstrap_v1_enabled=enabled)


def _payload(**overrides) -> dict:
    payload = {
        "host": "10.0.0.9",
        "username": "worker",
        "port": 22,
        "key": "~/.ssh/dispatch_worker",
        "components": ["tmux", "rsync", "git", "python-venv"],
        "gpu": False,
    }
    payload.update(overrides)
    return payload


class FakeSSHPool:
    """FakeSSH：以子字串比對回覆固定輸出；記錄所有指令與 SFTP 寫入。"""

    def __init__(self, outputs=None, unreachable_on=None):
        self.outputs = outputs or {}
        self.unreachable_on = unreachable_on
        self.commands: list[str] = []
        self.written: list[tuple[str, str]] = []

    async def run(self, server_cfg, command, timeout):
        if self.unreachable_on is not None and self.unreachable_on in command:
            raise SSHUnreachableError(f"SSH 到 {server_cfg.name} 失敗")
        self.commands.append(command)
        for key, output in self.outputs.items():
            if key in command:
                if isinstance(output, Exception):
                    raise output
                return SimpleNamespace(exit_status=0, stdout=output, stderr="")
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def write_file(self, server_cfg, path, content):
        self.written.append((path, content))


class RaisingSSHPool(FakeSSHPool):
    def __init__(self, *, raise_on: str, **kwargs):
        super().__init__(**kwargs)
        self.raise_on = raise_on

    async def run(self, server_cfg, command, timeout):
        if self.raise_on in command:
            self.commands.append(command)
            raise SSHUnreachableError("simulated bootstrap response loss")
        return await super().run(server_cfg, command, timeout)


def _success_outputs() -> dict:
    return {
        BOOTSTRAP_REMOTE_PATH: _GOOD_REPORT_LINE + "\n",
        "command -v bash": "/usr/bin/bash\n",
        "command -v tmux": "/usr/bin/tmux\n",
        "command -v rsync": "/usr/bin/rsync\n",
        "command -v git": "/usr/bin/git\n",
        "command -v python3": "/usr/bin/python3\n",
        "command -v nvidia-smi": "/usr/bin/nvidia-smi\n",
    }


def _app_state(pool, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(config=_config(enabled), ssh_pool=pool)


def _approve(db, approval_id, audit_path, context, *, app_state):
    return asyncio.run(
        approve(
            db,
            approval_id,
            app_state=app_state,
            audit_path=audit_path,
            request_context=context,
        )
    )


# ---------------------------------------------------------------------------
# DG-B 腳本邊界
# ---------------------------------------------------------------------------


def test_bootstrap_script_never_escalates_or_installs_system_packages():
    executable_lines = "\n".join(
        line
        for line in BOOTSTRAP_SCRIPT.splitlines()
        if not line.strip().startswith("#")
    )
    for forbidden in ("sudo", "apt", "yum", "dnf", "pacman", "nvidia"):
        assert forbidden not in executable_lines, forbidden
    # 冪等性：venv 已存在時直接視為 ready，不重建。
    assert '.dispatch-center/venv' in BOOTSTRAP_SCRIPT
    assert "umask 077" in BOOTSTRAP_SCRIPT


def test_bootstrap_script_sha256_pins_the_reviewed_constant():
    assert bootstrap_script_sha256() == hashlib.sha256(
        BOOTSTRAP_SCRIPT.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------


def test_validate_components_orders_dedupes_and_rejects_unknown():
    assert validate_bootstrap_components(["git", "tmux", "git"]) == ["tmux", "git"]
    assert validate_bootstrap_components(list(BOOTSTRAP_ALLOWED_COMPONENTS)) == list(
        BOOTSTRAP_ALLOWED_COMPONENTS
    )
    for bad in ([], None, "tmux", ["curl"], ["tmux", 3], ["TMUX"]):
        with pytest.raises(ValueError):
            validate_bootstrap_components(bad)


def test_build_bootstrap_command_is_deterministic_and_uses_pinned_path():
    command = build_bootstrap_command(["python-venv", "tmux"])
    assert command == f"bash '{BOOTSTRAP_REMOTE_PATH}' tmux python-venv"
    assert command == build_bootstrap_command(["tmux", "python-venv"])


def test_parse_bootstrap_report_takes_last_line_and_fails_closed():
    stdout = 'noise\nBOOTSTRAP_REPORT:{"tmux":"missing"}\n' + _GOOD_REPORT_LINE + "\n"
    assert parse_bootstrap_report(stdout) == {
        "tmux": "present",
        "rsync": "present",
        "git": "present",
        "python-venv": "ready",
    }
    assert parse_bootstrap_report("") is None
    assert parse_bootstrap_report("BOOTSTRAP_REPORT:{broken") is None
    assert parse_bootstrap_report('BOOTSTRAP_REPORT:{"a": 1}') is None
    assert parse_bootstrap_report("no report at all") is None


def test_evaluate_capabilities_unknown_is_missing():
    ok, missing = evaluate_capabilities(
        {"bash": "/bin/bash", "tmux": "/usr/bin/tmux", "rsync": "/usr/bin/rsync"},
        gpu=False,
    )
    assert ok and missing == []
    ok, missing = evaluate_capabilities(
        {"bash": "/bin/bash", "tmux": None, "rsync": ""}, gpu=False
    )
    assert not ok and missing == ["tmux", "rsync"]
    ok, missing = evaluate_capabilities(
        {"bash": "/bin/bash", "tmux": "/usr/bin/tmux", "rsync": "/usr/bin/rsync"},
        gpu=True,
    )
    assert not ok and missing == ["nvidia-smi"]


# ---------------------------------------------------------------------------
# run_server_bootstrap（FakeSSH）
# ---------------------------------------------------------------------------


def _server_cfg() -> ServerConfig:
    return ServerConfig(
        name="bootstrap:10.0.0.9:22",
        host="10.0.0.9",
        user="worker",
        key="~/.ssh/dispatch_worker",
    )


def test_run_server_bootstrap_success_uploads_pinned_script_then_checks():
    pool = FakeSSHPool(outputs=_success_outputs())
    report = asyncio.run(
        run_server_bootstrap(
            _server_cfg(),
            ssh_run_direct=pool.run,
            write_file_direct=pool.write_file,
            components=list(BOOTSTRAP_ALLOWED_COMPONENTS),
        )
    )
    assert report["passed"] is True
    assert report["errors"] == []
    assert report["script_version"] == BOOTSTRAP_SCRIPT_VERSION
    assert report["script_sha256"] == bootstrap_script_sha256()
    assert report["components"]["python-venv"] == "ready"
    assert pool.written == [(BOOTSTRAP_REMOTE_PATH, BOOTSTRAP_SCRIPT)]
    assert pool.commands[0].startswith("mkdir -p '.dispatch-center'")
    assert pool.commands[1] == build_bootstrap_command(list(BOOTSTRAP_ALLOWED_COMPONENTS))


def test_run_server_bootstrap_missing_component_fails_closed():
    outputs = _success_outputs()
    outputs[BOOTSTRAP_REMOTE_PATH] = (
        'BOOTSTRAP_REPORT:{"tmux":"missing","rsync":"present",'
        '"git":"present","python-venv":"ready"}\n'
    )
    pool = FakeSSHPool(outputs=outputs)
    report = asyncio.run(
        run_server_bootstrap(
            _server_cfg(),
            ssh_run_direct=pool.run,
            write_file_direct=pool.write_file,
            components=list(BOOTSTRAP_ALLOWED_COMPONENTS),
        )
    )
    assert report["passed"] is False
    assert any("tmux" in error for error in report["errors"])


def test_run_server_bootstrap_unparseable_report_fails_closed():
    outputs = _success_outputs()
    outputs[BOOTSTRAP_REMOTE_PATH] = "garbage output\n"
    pool = FakeSSHPool(outputs=outputs)
    report = asyncio.run(
        run_server_bootstrap(
            _server_cfg(),
            ssh_run_direct=pool.run,
            write_file_direct=pool.write_file,
            components=["tmux"],
        )
    )
    assert report["passed"] is False
    assert report["components"] == {"tmux": "unknown"}
    assert any("BOOTSTRAP_REPORT" in error for error in report["errors"])


def test_run_server_bootstrap_capability_check_failure_is_recorded_not_fatal():
    outputs = _success_outputs()
    outputs["command -v rsync"] = RuntimeError("check exploded")
    pool = FakeSSHPool(outputs=outputs)
    report = asyncio.run(
        run_server_bootstrap(
            _server_cfg(),
            ssh_run_direct=pool.run,
            write_file_direct=pool.write_file,
            components=["tmux"],
        )
    )
    assert report["passed"] is False
    assert report["capabilities"]["rsync"] is None
    assert any("rsync" in error for error in report["errors"])


def test_run_server_bootstrap_unreachable_propagates():
    pool = FakeSSHPool(outputs=_success_outputs(), unreachable_on="mkdir -p")
    with pytest.raises(SSHUnreachableError):
        asyncio.run(
            run_server_bootstrap(
                _server_cfg(),
                ssh_run_direct=pool.run,
                write_file_direct=pool.write_file,
                components=["tmux"],
            )
        )


# ---------------------------------------------------------------------------
# request_server_bootstrap_approval
# ---------------------------------------------------------------------------


def test_request_requires_the_rollback_switch(db, audit_path):
    with pytest.raises(ServerBootstrapDisabledError):
        request_server_bootstrap_approval(
            db, _payload(), _config(enabled=False), audit_path=audit_path
        )


def test_request_creates_pending_approval_with_script_pin(db, audit_path):
    context = _human_context(db)
    approval = request_server_bootstrap_approval(
        db,
        _payload(components=["git", "tmux", "git"]),
        _config(),
        audit_path=audit_path,
        request_context=context,
    )
    assert approval.kind == "server_bootstrap"
    assert approval.status == "pending"
    assert approval.payload["host"] == "10.0.0.9"
    assert approval.payload["username"] == "worker"
    assert approval.payload["components"] == ["tmux", "git"]
    assert approval.payload["script_version"] == BOOTSTRAP_SCRIPT_VERSION
    assert approval.payload["script_sha256"] == bootstrap_script_sha256()


def test_request_accepts_legacy_user_alias(db, audit_path):
    payload = _payload()
    payload["user"] = payload.pop("username")
    approval = request_server_bootstrap_approval(
        db, payload, _config(), audit_path=audit_path
    )
    assert approval.payload["username"] == "worker"


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "bad host!"},
        {"host": ""},
        {"username": "Root User"},
        {"username": "root"},
        {"port": 0},
        {"port": "not-a-port"},
        {"key": "/etc/passwd"},
        {"key": "~/.ssh/../escape"},
        {"key": "-----BEGIN OPENSSH PRIVATE KEY-----"},
        {"components": []},
        {"components": ["curl"]},
    ],
)
def test_request_rejects_malformed_payloads(db, audit_path, overrides):
    with pytest.raises(InvalidServerBootstrapRequestError):
        request_server_bootstrap_approval(
            db, _payload(**overrides), _config(), audit_path=audit_path
        )


def test_request_dedupes_same_pending_target(db, audit_path):
    request_server_bootstrap_approval(db, _payload(), _config(), audit_path=audit_path)
    with pytest.raises(InvalidServerBootstrapRequestError):
        request_server_bootstrap_approval(
            db, _payload(), _config(), audit_path=audit_path
        )
    # 不同目標不受影響。
    request_server_bootstrap_approval(
        db, _payload(host="10.0.0.10"), _config(), audit_path=audit_path
    )


# ---------------------------------------------------------------------------
# approve 分支
# ---------------------------------------------------------------------------


def _pending_bootstrap(db, audit_path, **overrides):
    return request_server_bootstrap_approval(
        db, _payload(**overrides), _config(), audit_path=audit_path
    )


def test_approve_requires_the_rollback_switch_and_stays_pending(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    with pytest.raises(ServerBootstrapDisabledError):
        _approve(
            db, approval.id, audit_path, context,
            app_state=_app_state(FakeSSHPool(), enabled=False),
        )
    assert db.get_approval(approval.id).status == "pending"


def test_approve_runs_bootstrap_and_records_passing_report(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    pool = FakeSSHPool(outputs=_success_outputs())

    result = _approve(db, approval.id, audit_path, context, app_state=_app_state(pool))

    decided = db.get_approval(approval.id)
    assert decided.status == "approved"
    assert "通過" in decided.note
    assert result["report"]["passed"] is True
    latest = db.latest_server_bootstrap_report(
        host="10.0.0.9", username="worker", port=22
    )
    assert latest is not None
    assert latest.passed is True
    assert latest.approval_id == approval.id
    assert latest.components == ["tmux", "rsync", "git", "python-venv"]
    durable = db.list_durable_audit_events(limit=100)
    intent = next(e for e in durable if e["action"] == "server_bootstrap_intent")
    outcome = next(
        e
        for e in durable
        if e["action"] == "server_bootstrap_outcome" and e["result"] == "applied"
    )
    assert intent["approval_id"] == approval.id
    assert outcome["approval_id"] == approval.id
    assert len(intent["params"]["payload_sha256"]) == 64
    assert "key" not in intent["params"]
    assert outcome["params"]["report_id"] == latest.id


def test_approve_records_failing_report_as_approved_but_not_passed(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    outputs = _success_outputs()
    outputs[BOOTSTRAP_REMOTE_PATH] = (
        'BOOTSTRAP_REPORT:{"tmux":"missing","rsync":"present",'
        '"git":"present","python-venv":"ready"}\n'
    )
    outputs["command -v tmux"] = "\n"
    pool = FakeSSHPool(outputs=outputs)

    _approve(db, approval.id, audit_path, context, app_state=_app_state(pool))

    decided = db.get_approval(approval.id)
    assert decided.status == "approved"
    assert "未通過" in decided.note
    latest = db.latest_server_bootstrap_report(
        host="10.0.0.9", username="worker", port=22
    )
    assert latest.passed is False


def test_approve_rejects_when_script_changed_since_request(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    tampered = dict(approval.payload)
    tampered["script_sha256"] = "0" * 64
    db.update_approval(approval.id, payload=tampered)
    context = _human_context(db)

    result = _approve(
        db, approval.id, audit_path, context, app_state=_app_state(FakeSSHPool())
    )

    assert result["approval"].status == "rejected"
    assert "改版" in result["approval"].note
    assert db.latest_server_bootstrap_report(
        host="10.0.0.9", username="worker", port=22
    ) is None


def test_approve_unreachable_leaves_approval_pending(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    pool = FakeSSHPool(outputs=_success_outputs(), unreachable_on="mkdir -p")

    with pytest.raises(SSHUnreachableError):
        _approve(db, approval.id, audit_path, context, app_state=_app_state(pool))

    assert db.get_approval(approval.id).status == "pending"
    assert db.latest_server_bootstrap_report(
        host="10.0.0.9", username="worker", port=22
    ) is None
    assert any(
        event["action"] == "server_bootstrap_outcome"
        and event["result"] == "unknown"
        for event in db.list_durable_audit_events(limit=100)
    )


def test_approve_bootstrap_unknown_retry_never_replays_script(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    first_pool = RaisingSSHPool(
        raise_on=f"bash '{BOOTSTRAP_REMOTE_PATH}'",
        outputs=_success_outputs(),
    )
    with pytest.raises(SSHUnreachableError):
        _approve(db, approval.id, audit_path, context, app_state=_app_state(first_pool))
    assert db.get_approval(approval.id).status == "pending"

    second_pool = FakeSSHPool(outputs=_success_outputs())
    second = _approve(
        db, approval.id, audit_path, context, app_state=_app_state(second_pool)
    )
    assert second["approval"].status == "pending"
    assert second_pool.commands == []
    assert second_pool.written == []


def test_approve_bootstrap_outcome_append_failure_rolls_back_report(
    monkeypatch, db, audit_path
):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    original = db.append_durable_audit_event_in_transaction

    def fail_applied(cursor, **kwargs):
        if (
            kwargs.get("action") == "server_bootstrap_outcome"
            and kwargs.get("result") == "applied"
        ):
            raise RuntimeError("injected bootstrap outcome append failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_applied)
    result = _approve(
        db,
        approval.id,
        audit_path,
        context,
        app_state=_app_state(FakeSSHPool(outputs=_success_outputs())),
    )
    assert result["approval"].status == "pending"
    assert db.latest_server_bootstrap_report(
        host="10.0.0.9", username="worker", port=22
    ) is None
    events = db.list_durable_audit_events(limit=100)
    assert not any(
        event["action"] == "server_bootstrap_outcome"
        and event["result"] == "applied"
        for event in events
    )
    assert any(
        event["action"] == "server_bootstrap_outcome"
        and event["result"] == "unknown"
        for event in events
    )


def test_approve_without_ssh_pool_raises_value_error(db, audit_path):
    approval = _pending_bootstrap(db, audit_path)
    context = _human_context(db)
    app_state = SimpleNamespace(config=_config(), ssh_pool=None)
    with pytest.raises(ValueError):
        _approve(db, approval.id, audit_path, context, app_state=app_state)
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# server_add 的 bootstrap-report 閘（B2）
# ---------------------------------------------------------------------------


def _server_add_setup(tmp_path, *, bootstrap_enabled: bool):
    """`validate_server_config()` 會驗證 key 檔案存在與允許目錄，因此用
    真 `AppConfig` ＋ tmp 目錄下的假 key 檔（內容無所謂，不是真金鑰）。"""
    from app.config import AppConfig

    key_path = tmp_path / "dispatch_worker"
    key_path.write_text("not-a-real-key")
    payload = {
        "name": "worker-new",
        "host": "10.0.0.9",
        "user": "worker",
        "key": str(key_path),
        "port": 22,
    }
    config = AppConfig(
        servers=[],
        ssh_key_allowed_dirs=[str(tmp_path)],
        server_bootstrap_v1_enabled=bootstrap_enabled,
    )
    return payload, config


def _insert_report(db, *, passed: bool):
    db.insert_server_bootstrap_report(
        host="10.0.0.9",
        username="worker",
        port=22,
        components=["tmux"],
        script_version=BOOTSTRAP_SCRIPT_VERSION,
        script_sha256=bootstrap_script_sha256(),
        passed=passed,
        report={"passed": passed, "errors": [] if passed else ["元件未就緒：tmux"]},
    )


def test_server_add_blocked_by_latest_failing_report(db, audit_path, tmp_path):
    _insert_report(db, passed=False)
    payload, config = _server_add_setup(tmp_path, bootstrap_enabled=True)
    with pytest.raises(InvalidServerConfigError) as excinfo:
        request_server_add_approval(db, payload, config, audit_path=audit_path)
    assert "bootstrap" in str(excinfo.value)


def test_server_add_allowed_when_latest_report_passes(db, audit_path, tmp_path):
    _insert_report(db, passed=False)
    _insert_report(db, passed=True)  # 之後成功的一筆覆蓋失敗
    payload, config = _server_add_setup(tmp_path, bootstrap_enabled=True)
    approval = request_server_add_approval(db, payload, config, audit_path=audit_path)
    assert approval.kind == "server_add"
    assert approval.status == "pending"


def test_server_add_allowed_without_any_report(db, audit_path, tmp_path):
    payload, config = _server_add_setup(tmp_path, bootstrap_enabled=True)
    approval = request_server_add_approval(db, payload, config, audit_path=audit_path)
    assert approval.status == "pending"


def test_server_add_gate_inactive_when_flag_disabled(db, audit_path, tmp_path):
    _insert_report(db, passed=False)
    payload, config = _server_add_setup(tmp_path, bootstrap_enabled=False)
    approval = request_server_add_approval(db, payload, config, audit_path=audit_path)
    assert approval.status == "pending"


# ---------------------------------------------------------------------------
# 自動核准排除與 API 隱藏
# ---------------------------------------------------------------------------


def test_server_bootstrap_is_never_auto_approved():
    result = asyncio.run(
        maybe_auto_approve(
            SimpleNamespace(),
            SimpleNamespace(kind="server_bootstrap", id=1),
            source="web",
            rules=[{"kind": "any", "action": "approve"}],
        )
    )
    assert result is None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/servers/bootstrap-reports", None),
        (
            "post",
            "/servers/bootstrap-request",
            {
                "host": "10.0.0.9",
                "username": "worker",
                "key": "~/.ssh/dispatch_worker",
                "components": ["tmux"],
            },
        ),
    ],
)
@pytest.mark.usefixtures("legacy_posture")
def test_bootstrap_routes_are_hidden_by_default(api_client, method, path, body):
    client, _ = api_client
    response = (
        getattr(client, method)(path, json=body)
        if body is not None
        else getattr(client, method)(path)
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "server bootstrap is disabled"}
