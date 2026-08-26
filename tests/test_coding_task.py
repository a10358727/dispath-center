"""階段 13（PLAN.md N 節，Codex Worker v2：Central Codex Runner）：AI 改碼
層次二。v1（每台工作機各裝 Codex CLI、使用者指定 server）已於 2026-07-10
全面廢止，本檔案改寫為 v2 版本，涵蓋 N.12 測試清單：

- kind/type 註冊（`coding_task` in VALID_APPROVAL_KINDS、`coding` in
  VALID_TYPES）。
- `request_coding_task_approval()` v2 驗證：功能停用／legacy server
  相容／project 不存在／instruction 空白或過長／base_branch 格式／
  validation_target 不是已啟用的 server／N.3 三段式 repo 來源判定
  （情況 A/B/C，C 錯誤訊息全文比對）。
- `build_codex_instruction_file()`：guardrail 關鍵字、網路狀態字樣、
  instruction 原文在檔尾。
- `build_coding_task_script()`：`bash -n` 語法通過（多組合）、旗標斷言、
  不含 instruction 內文、secret pattern／bundle／root 檢查等鐵律佐證。
- `approve()` v2：coding_run 建立與回填、job 組裝、instruction.txt 寫入、
  稽核不含 instruction 全文、runner 未設定 rejected。
- v1 遺留 payload 相容（有 server 無 source_kind）。
- coding_task 永不被 `maybe_auto_approve()` 自動核准。
- vLLM `app/agent_tools.py` 沒有 coding_task 相關工具（靜態掃描）。
- 端點：`POST /projects/{name}/coding-task-request`（v2 body）。
- `app.jobfinish.handle_job_finished()` 的 coding_runs 回填（PLAN.md N.6）。
- MCP bridge 的 request_coding_task 測試在 tests/test_mcp_bridge.py（body
  相容性，不受這次改動影響——bridge 只是轉發 HTTP body，見該檔案）。
- 前端 smoke（`GET /` 含 coding_task 渲染關鍵字，static/index.html 本次
  未變動）。
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import replace

import pytest

from app.approvals import (
    CodingRunNotCleanableError,
    CodingRunNotFoundError,
    InvalidCodingTaskRequestError,
    approve,
    build_bundle_checkout_preamble,
    build_codex_instruction_file,
    build_coding_task_script,
    cleanup_coding_run,
    maybe_auto_approve,
    request_coding_task_approval,
    request_enqueue_approval,
    resolve_codex_workspace_rel,
)
from app.audit import read_audit
from app.config import AppConfig, ServerConfig
from app.coding_agents import (
    UnknownCodingAgentProviderError,
    require_coding_agent_provider,
)
from app.engineering_path_policy import _is_protected_secret_basename
from app.db import VALID_APPROVAL_KINDS, VALID_TYPES, Job
from app.jobfinish import _backfill_coding_run, handle_job_finished
from app.results import build_bundle_push_command
from app.identity import ActorType, generate_session_token


def _login(client, main_module):
    """Login-first root (`GET /`) now requires an authenticated context to
    serve `workspace.html`; these front-end smoke tests only pin static
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


def _setup_project(db, server="server-a", path="/data/proj1", repo_or_path=None):
    db.insert_project("proj1", repo_or_path or "/local/only/proj1")
    db.insert_project_instance(project_name="proj1", server=server, path=path)


def _config(**overrides):
    base = dict(
        servers=[],
        codex_runner_server="server-a",
        codex_workspace_root="~/codex_workspaces",
        codex_network_access=False,
        codex_auth_mode="chatgpt",
        codex_max_concurrency=1,
    )
    base.update(overrides)
    return AppConfig(**base)


# ---------------------------------------------------------------------------
# kind/type 註冊
# ---------------------------------------------------------------------------


def test_coding_task_kind_registered_in_valid_kinds():
    assert "coding_task" in VALID_APPROVAL_KINDS


def test_coding_type_registered_in_valid_types():
    assert "coding" in VALID_TYPES


# ---------------------------------------------------------------------------
# request_coding_task_approval() v2：建立請求時的驗證
# ---------------------------------------------------------------------------


def test_request_coding_task_disabled_without_runner(db, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    config = _config(codex_runner_server=None)
    with pytest.raises(InvalidCodingTaskRequestError, match="未設定 CODEX_RUNNER_SERVER"):
        request_coding_task_approval(
            db, "proj1", "fix the bug", config=config, server_enabled={}, audit_path=audit_path
        )
    assert db.list_approvals() == []


def test_request_coding_task_project_not_found_rejected(db, audit_path):
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError):
        request_coding_task_approval(
            db,
            "no-such-project",
            "fix the bug",
            config=config,
            server_enabled={"server-a": True},
            audit_path=audit_path,
        )
    assert db.list_approvals() == []


def test_request_coding_task_empty_instruction_rejected(db, audit_path):
    _setup_project(db)
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError):
        request_coding_task_approval(
            db, "proj1", "   ", config=config, server_enabled={"server-a": True}, audit_path=audit_path
        )
    assert db.list_approvals() == []


def test_request_coding_task_too_long_instruction_rejected(db, audit_path):
    _setup_project(db)
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError):
        request_coding_task_approval(
            db,
            "proj1",
            "x" * 4001,
            config=config,
            server_enabled={"server-a": True},
            audit_path=audit_path,
        )
    assert db.list_approvals() == []


@pytest.mark.parametrize(
    "instruction",
    [
        "Use Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Clone ssh://runner:plain-password@example.invalid/repo.git",
        "Authorization:\u200b Basic dXNlcjpwYXNzd29yZA==",
    ],
)
def test_request_coding_task_rejects_raw_credentials_before_approval(
    db, audit_path, instruction
):
    _setup_project(db)
    with pytest.raises(InvalidCodingTaskRequestError, match="raw credential"):
        request_coding_task_approval(
            db,
            "proj1",
            instruction,
            config=_config(),
            server_enabled={"server-a": True},
            audit_path=audit_path,
        )

    assert db.list_approvals() == []


def test_request_coding_task_exactly_max_chars_allowed(db, audit_path):
    _setup_project(db)
    config = _config()
    approval = request_coding_task_approval(
        db,
        "proj1",
        "x" * 4000,
        config=config,
        server_enabled={"server-a": True},
        audit_path=audit_path,
    )
    assert approval.status == "pending"


def test_request_coding_task_invalid_base_branch_rejected(db, audit_path):
    _setup_project(db)
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError):
        request_coding_task_approval(
            db,
            "proj1",
            "fix the bug",
            base_branch="main; rm x",
            config=config,
            server_enabled={"server-a": True},
            audit_path=audit_path,
        )
    assert db.list_approvals() == []


def test_request_coding_task_validation_target_not_enabled_rejected(db, audit_path):
    _setup_project(db)
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError):
        request_coding_task_approval(
            db,
            "proj1",
            "fix the bug",
            validation_target="server-b",
            config=config,
            server_enabled={"server-a": True, "server-b": False},
            audit_path=audit_path,
        )
    assert db.list_approvals() == []


def test_request_coding_task_validation_target_can_equal_runner(db, audit_path):
    _setup_project(db)
    config = _config()
    approval = request_coding_task_approval(
        db,
        "proj1",
        "fix the bug",
        validation_target="server-a",
        config=config,
        server_enabled={"server-a": True},
        audit_path=audit_path,
    )
    assert approval.payload["validation_target"] == "server-a"


def test_request_coding_task_legacy_server_matches_runner_accepted(db, audit_path):
    _setup_project(db, server="server-a")
    config = _config()
    approval = request_coding_task_approval(
        db,
        "proj1",
        "fix the bug",
        config=config,
        server_enabled={"server-a": True},
        legacy_server="server-a",
        audit_path=audit_path,
    )
    assert approval.payload["legacy_server_param"] is True
    records = read_audit(audit_path)
    requested = [r for r in records if r["action"] == "approval_requested"][0]
    assert requested["params"]["legacy_server_param"] is True


def test_request_coding_task_legacy_server_mismatch_rejected(db, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError, match="CODEX_RUNNER_SERVER"):
        request_coding_task_approval(
            db,
            "proj1",
            "fix the bug",
            config=config,
            server_enabled={"server-a": True, "server-b": True},
            legacy_server="server-b",
            audit_path=audit_path,
        )
    assert db.list_approvals() == []


# ---------------------------------------------------------------------------
# N.3 三段式 repo 來源判定
# ---------------------------------------------------------------------------


def test_request_coding_task_source_kind_instance_when_runner_has_instance(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    assert approval.payload["source_kind"] == "instance"
    assert approval.payload["source"] == "/data/proj1"
    assert approval.payload["runner_server"] == "server-a"


def test_request_coding_task_payload_includes_network_access_false(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config(codex_network_access=False)
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    assert approval.payload["network_access"] is False


def test_request_coding_task_payload_includes_network_access_true(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config(codex_network_access=True)
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    assert approval.payload["network_access"] is True


def test_request_coding_task_source_kind_mirror_when_no_instance_but_git_remote(db, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    assert approval.payload["source_kind"] == "mirror"
    assert approval.payload["source"] == "https://github.com/x/proj1.git"


@pytest.mark.parametrize("remote", ["https://github.com/x/y.git", "http://x/y.git", "git@github.com:x/y.git", "ssh://git@x/y.git"])
def test_request_coding_task_source_kind_mirror_accepts_all_remote_prefixes(db, audit_path, remote):
    db.insert_project("proj1", remote)
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    assert approval.payload["source_kind"] == "mirror"


@pytest.mark.parametrize(
    "remote",
    [
        "https://runner:plain-password@example.invalid/proj1.git",
        "ssh://runner:plain-password@example.invalid/proj1.git",
    ],
)
def test_request_coding_task_rejects_credential_bearing_registered_remote(
    db, audit_path, remote
):
    db.insert_project("proj1", remote)

    with pytest.raises(InvalidCodingTaskRequestError, match="git_remote") as excinfo:
        request_coding_task_approval(
            db,
            "proj1",
            "fix the bug",
            config=_config(),
            server_enabled={"server-a": True},
            audit_path=audit_path,
        )

    assert "plain-password" not in str(excinfo.value)
    assert db.list_approvals() == []


def test_request_coding_task_source_kind_none_rejected_full_message(db, audit_path):
    db.insert_project("proj1", "/local/only/path")
    config = _config()
    with pytest.raises(InvalidCodingTaskRequestError) as excinfo:
        request_coding_task_approval(
            db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
        )
    assert str(excinfo.value) == (
        "此專案沒有 Codex Runner instance，也沒有可用的 git_remote。"
        "請先匯入專案到 Codex Runner 或登記 git_remote。"
    )
    assert db.list_approvals() == []


def test_request_coding_task_instance_on_other_server_falls_back_to_mirror(db, audit_path):
    """Runner（server-a）沒有 instance，但另一台機器（server-b）有——不算
    「Runner 已有 instance」，仍要看有沒有 git remote 可用（情況 B）。"""
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    assert approval.payload["source_kind"] == "mirror"


# ---------------------------------------------------------------------------
# build_codex_instruction_file()
# ---------------------------------------------------------------------------


def test_build_codex_instruction_file_contains_guardrail_keywords():
    text = build_codex_instruction_file("add a --dry-run flag", network_access=False)
    assert "sudo" in text
    assert ".venv" in text
    assert "push" in text


def test_build_codex_instruction_file_network_status_disabled():
    text = build_codex_instruction_file("do the thing", network_access=False)
    assert "停用" in text
    assert "允許" not in text.split("=" * 40)[0] or "網路存取目前【停用】" in text


def test_build_codex_instruction_file_network_status_enabled():
    text = build_codex_instruction_file("do the thing", network_access=True)
    assert "網路存取目前【允許】" in text


def test_build_codex_instruction_file_instruction_at_tail():
    instruction = "add a --dry-run flag to train.py"
    text = build_codex_instruction_file(instruction, network_access=False)
    assert text.rstrip("\n").endswith(instruction)


def test_build_codex_instruction_file_preserves_instruction_verbatim():
    instruction = "do X; then run `echo hi`; don't break anything"
    text = build_codex_instruction_file(instruction, network_access=True)
    assert instruction in text


# ---------------------------------------------------------------------------
# build_coding_task_script()：確定性任務腳本組裝
# ---------------------------------------------------------------------------


def _bash_n_ok(script: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    return result.returncode == 0, result.stderr


_SCRIPT_COMBOS = [
    (1, "codex_workspaces", "proj1", "instance", "/data/proj1", None, False),
    (2, "codex_workspaces", "proj1", "instance", "/data/proj1", "develop", True),
    (3, "codex_workspaces", "proj1", "mirror", "https://github.com/x/proj1.git", None, False),
    (4, "codex_workspaces", "proj1", "mirror", "git@github.com:x/proj1.git", "main", True),
]


@pytest.mark.parametrize("combo", _SCRIPT_COMBOS)
def test_build_coding_task_script_passes_bash_syntax_check(combo):
    script = build_coding_task_script(*combo)
    ok, stderr = _bash_n_ok(script)
    assert ok, stderr


def test_build_coding_task_script_uses_workspace_write_and_never_full_auto():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert "--sandbox workspace-write" in script
    assert "-c approval_policy=never" in script
    assert "--json" in script
    assert "-o " in script
    assert "- < " in script or '- < "$TASK_DIR/instruction.txt"' in script
    assert "--full-auto" not in script
    assert "--ask-for-approval" not in script


def test_build_coding_task_script_does_not_contain_instruction_text():
    dangerous_instruction = "please clean up: '; rm -rf / #"
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    instruction_file = build_codex_instruction_file(dangerous_instruction, network_access=False)
    assert dangerous_instruction in instruction_file
    assert dangerous_instruction not in script


def test_build_coding_task_script_network_false_has_no_network_access_string():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert "network_access" not in script


def test_build_coding_task_script_network_true_has_network_access_flag():
    script = build_coding_task_script(*_SCRIPT_COMBOS[1])
    assert "sandbox_workspace_write.network_access=true" in script


def test_build_coding_task_script_contains_secret_pattern_and_bundle_and_root_check():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert "id_rsa" in script
    assert "id_ed25519" in script
    assert "auth\\.json" in script
    assert "bundle create" in script
    assert "bundle verify" in script
    assert '[ "$(id -u)" != "0" ]' in script
    assert "codex login status" in script
    assert "worktree add" in script
    assert "core.hooksPath=/dev/null" in script
    assert "commit.gpgsign=false" in script
    assert "commit --no-verify" in script
    assert "--no-ext-diff --no-textconv" in script


# 受保護 secret basename 樣式有三份拷貝：app/engineering_path_policy.py 的
# Python 判定、v1 script 模板內的 grep ERE、以及 v2 升級用的 byte-exact 錨。
# 錨若漂移，_upgrade_coding_script_with_final_path_policy 會直接 RuntimeError；
# 這裡另外釘住「Python 判定 vs 生成腳本 grep」的行為一致性。
_SECRET_BASENAME_EXPECTED_HITS = frozenset(
    {
        ".env",
        ".env.production",
        ".ENV.local",
        ".env.example",
        ".envrc",
        "auth.json",
        "server.pem",
        "signing.KEY",
        "keystore.p12",
        "legacy.PFX",
        "secret.json",
        "SECRET.YAML",
        "secrets.toml",
        "credentials-prod.json",
        "id_rsa.pub",
        "ID_ED25519_backup",
    }
)
_SECRET_BASENAME_EXPECTED_MISSES = frozenset(
    {
        ".environment",
        ".envoy.yaml",
        "secretary.py",
        "mysecret.txt",
        "oauth.json",
        "xauth.json",
        "server.pem.bak",
        "monkey",
        "p12",
    }
)


def test_secret_basename_patterns_agree_across_python_and_generated_script():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    patterns = re.findall(r"grep -E -i '([^']+)'", script)
    assert len(patterns) == 1
    names = sorted(_SECRET_BASENAME_EXPECTED_HITS | _SECRET_BASENAME_EXPECTED_MISSES)
    completed = subprocess.run(
        ["grep", "-E", "-i", patterns[0]],
        input="\n".join(names) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    grep_hits = set(completed.stdout.splitlines())
    python_hits = {name for name in names if _is_protected_secret_basename(name)}
    assert grep_hits == python_hits == set(_SECRET_BASENAME_EXPECTED_HITS)


def test_v2_script_replaces_the_shell_secret_gate_with_the_verifier():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    patterns = re.findall(r"grep -E -i '([^']+)'", script)
    assert len(patterns) == 1
    upgraded = build_coding_task_script(
        *_SCRIPT_COMBOS[0],
        path_policy_sha256="a" * 64,
        path_verifier_sha256="b" * 64,
    )
    assert patterns[0] not in upgraded
    assert "path-policy-verifier.py" in upgraded


def test_build_coding_task_script_instance_case_contains_instance_path():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert "SRC=/data/proj1" in script
    assert "clone --mirror" not in script


def test_build_coding_task_script_mirror_case_contains_clone_and_url():
    script = build_coding_task_script(*_SCRIPT_COMBOS[2])
    assert "clone --mirror" in script
    assert "https://github.com/x/proj1.git" in script
    assert "mirrors/proj1.git" in script


def test_build_coding_task_script_branch_name_uses_approval_id():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert 'BR="ai-task-1"' in script


def test_build_coding_task_script_base_branch_present_only_when_given():
    no_base = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert "refs/heads/" not in no_base or "rev-parse HEAD" in no_base
    with_base = build_coding_task_script(*_SCRIPT_COMBOS[1])
    assert "refs/heads/develop" in with_base


def test_build_coding_task_script_rejects_invalid_base_branch():
    with pytest.raises(ValueError):
        build_coding_task_script(1, "codex_workspaces", "proj1", "instance", "/data/proj1", "main; rm x", False)


def test_build_coding_task_script_never_pushes_external_origin():
    for combo in _SCRIPT_COMBOS:
        script = build_coding_task_script(*combo)
        assert "git push" not in script


def test_build_coding_task_script_rejects_unreviewed_provider_identifier():
    with pytest.raises(UnknownCodingAgentProviderError, match="unapproved"):
        build_coding_task_script(
            *_SCRIPT_COMBOS[0], agent_provider_id="arbitrary-agent-executable"
        )


@pytest.mark.parametrize("drift", ["identity", "outputs"])
def test_build_coding_task_script_rejects_provider_launch_contract_drift(
    monkeypatch, drift
):
    reviewed = require_coding_agent_provider("codex")

    class DriftedProvider:
        descriptor = reviewed.descriptor

        def start_turn(self, request):
            launch = reviewed.start_turn(request)
            if drift == "identity":
                return replace(launch, adapter="unreviewed-adapter")
            return replace(
                launch,
                outputs=replace(
                    launch.outputs, checkpoint_file="unexpected-checkpoint.json"
                ),
            )

    monkeypatch.setattr(
        "app.approvals.require_coding_agent_provider",
        lambda _provider_id: DriftedProvider(),
    )
    with pytest.raises(ValueError, match="launch/output contract"):
        build_coding_task_script(*_SCRIPT_COMBOS[0])


# ---------------------------------------------------------------------------
# approve()：coding_task v2 分支
# ---------------------------------------------------------------------------


class CodingFakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class RecordingSSH:
    def __init__(self):
        self.calls: list[tuple[str, str, float]] = []

    async def __call__(self, server, command, timeout):
        self.calls.append((server, command, timeout))
        return CodingFakeCommandResult()


class RaisingCleanupSSH(RecordingSSH):
    def __init__(self, *, fail_on_call: int = 1):
        super().__init__()
        self.fail_on_call = fail_on_call

    async def __call__(self, server, command, timeout):
        result = await super().__call__(server, command, timeout)
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("SECRET-CLEANUP-REMOTE-DETAIL")
        return result


class RecordingWriteFile:
    def __init__(self):
        self.writes: list[tuple[str, str, str]] = []

    async def __call__(self, server, path, content):
        self.writes.append((server, path, content))


class FakeAppState:
    def __init__(self, config, ssh_write_file):
        self.config = config
        self.ssh_write_file = ssh_write_file


def test_approve_coding_task_runner_not_configured_rejected(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    disabled_config = _config(codex_runner_server=None)
    app_state = FakeAppState(disabled_config, RecordingWriteFile())

    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=RecordingSSH(),
            audit_path=audit_path,
            app_state=app_state,
        )
    )
    assert result["approval"].status == "rejected"
    assert "未設定 CODEX_RUNNER_SERVER" in result["approval"].note
    assert db.list_coding_runs() == []


def test_approve_coding_task_creates_coding_run_and_job(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "add a --dry-run flag", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    ssh = RecordingSSH()
    write_file = RecordingWriteFile()
    app_state = FakeAppState(config, write_file)

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )

    approved = result["approval"]
    assert approved.status == "approved"
    job = result["job"]
    run_id = result["coding_run_id"]
    assert job.type == "coding"
    assert job.pin_server == "server-a"
    assert job.project == "proj1"

    expected_script = build_coding_task_script(
        approval.id, "codex_workspaces", "proj1", "instance", "/data/proj1", None, False
    )
    assert job.command == expected_script

    coding_run = db.get_coding_run(run_id)
    assert coding_run.job_id == job.id
    assert coding_run.status == "queued"
    assert coding_run.instruction == "add a --dry-run flag"
    assert coding_run.runner_server == "server-a"

    expected_instruction_file = build_codex_instruction_file("add a --dry-run flag", False)
    assert write_file.writes == [
        ("server-a", f"codex_workspaces/tasks/{approval.id}/instruction.txt", expected_instruction_file)
    ]
    assert any("mkdir -p" in c[1] and f"tasks/{approval.id}" in c[1] for c in ssh.calls)

    assert f"#{job.id}" in approved.note
    assert f"coding_run #{run_id}" in approved.note
    assert f"ai-task-{approval.id}" in approved.note

    records = read_audit(audit_path)
    coding_task_events = [r for r in records if r["action"] == "coding_task"]
    assert len(coding_task_events) == 1
    event = coding_task_events[0]
    assert event["params"]["approval_id"] == approval.id
    assert event["params"]["project"] == "proj1"
    assert event["params"]["runner_server"] == "server-a"
    assert event["params"]["job_id"] == job.id
    assert event["params"]["coding_run_id"] == run_id
    assert event["params"]["source_kind"] == "instance"
    assert "instruction" not in event["params"]
    materialized = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "execution_job_materialized"
        and event["approval_id"] == approval.id
    ]
    assert len(materialized) == 1
    assert materialized[0]["resource_id"] == str(job.id)
    assert materialized[0]["params"]["legacy_unpinned"] is True
    assert materialized[0]["params"]["coding_run_id"] == run_id
    assert not any(
        record["action"] == "enqueue"
        and record.get("params", {}).get("approval_id") == approval.id
        for record in records
    )


def test_approve_coding_task_materialization_audit_failure_rolls_back(
    db, audit_path, monkeypatch
):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config()
    approval = request_coding_task_approval(
        db,
        "proj1",
        "add a --dry-run flag",
        config=config,
        server_enabled={"server-a": True},
        audit_path=audit_path,
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_materialization(*args, **kwargs):
        if kwargs.get("action") == "execution_job_materialized":
            raise RuntimeError("coding task audit append fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        db, "append_durable_audit_event_in_transaction", fail_materialization
    )
    with pytest.raises(RuntimeError, match="coding task audit append fault"):
        asyncio.run(
            approve(
                db,
                approval.id,
                ssh_run=RecordingSSH(),
                audit_path=audit_path,
                app_state=FakeAppState(config, RecordingWriteFile()),
            )
        )

    assert db.get_approval(approval.id).status == "pending"
    runs = db.list_coding_runs()
    assert len(runs) == 1
    assert runs[0].job_id is None
    assert db.list_jobs() == []
    assert not any(
        event["action"] in {"approval_decided", "execution_job_materialized"}
        and event.get("approval_id") == approval.id
        for event in db.list_durable_audit_events(limit=100)
    )


def test_approve_coding_task_with_base_branch_and_validation_target(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config()
    approval = request_coding_task_approval(
        db,
        "proj1",
        "fix the bug",
        base_branch="develop",
        validation_target="server-a",
        config=config,
        server_enabled={"server-a": True},
        audit_path=audit_path,
    )
    ssh = RecordingSSH()
    app_state = FakeAppState(config, RecordingWriteFile())

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )
    coding_run = db.get_coding_run(result["coding_run_id"])
    assert coding_run.base_branch == "develop"
    assert coding_run.validation_target == "server-a"
    assert "refs/heads/develop" in result["job"].command


def test_approve_coding_task_mirror_source(db, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    ssh = RecordingSSH()
    app_state = FakeAppState(config, RecordingWriteFile())

    result = asyncio.run(
        approve(db, approval.id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )
    assert "clone --mirror" in result["job"].command
    assert "https://github.com/x/proj1.git" in result["job"].command


# ---------------------------------------------------------------------------
# v1 遺留 payload 相容
# ---------------------------------------------------------------------------


def test_approve_coding_task_legacy_payload_server_matches_runner_executes_v2(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config()
    legacy_payload = {
        "project": "proj1",
        "server": "server-a",
        "instruction": "fix the bug",
        "base_branch": None,
    }
    approval_id = db.insert_approval(kind="coding_task", payload=legacy_payload)
    ssh = RecordingSSH()
    app_state = FakeAppState(config, RecordingWriteFile())

    result = asyncio.run(
        approve(db, approval_id, ssh_run=ssh, audit_path=audit_path, app_state=app_state)
    )
    assert result["approval"].status == "approved"
    assert result["job"].type == "coding"
    assert result["job"].pin_server == "server-a"
    assert db.get_coding_run(result["coding_run_id"]) is not None


def test_approve_coding_task_legacy_payload_server_mismatch_rejected(db, audit_path):
    _setup_project(db, server="server-b", path="/data/proj1")
    config = _config(codex_runner_server="server-a")
    legacy_payload = {
        "project": "proj1",
        "server": "server-b",
        "instruction": "fix the bug",
        "base_branch": None,
    }
    approval_id = db.insert_approval(kind="coding_task", payload=legacy_payload)
    app_state = FakeAppState(config, RecordingWriteFile())

    result = asyncio.run(
        approve(db, approval_id, ssh_run=RecordingSSH(), audit_path=audit_path, app_state=app_state)
    )
    assert result["approval"].status == "rejected"
    assert "架構已改為 Central Codex Runner" in result["approval"].note
    assert db.list_coding_runs() == []


def test_approve_coding_task_can_retire_legacy_jsonl_summary(db, audit_path):
    _setup_project(db, server="server-a", path="/data/proj1")
    config = _config(legacy_audit_jsonl_enabled=False)
    approval = request_coding_task_approval(
        db,
        "proj1",
        "add a --dry-run flag",
        config=config,
        server_enabled={"server-a": True},
        audit_path=audit_path,
    )
    app_state = FakeAppState(config, RecordingWriteFile())
    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=RecordingSSH(),
            audit_path=audit_path,
            app_state=app_state,
        )
    )

    assert result["approval"].status == "approved"
    job = result["job"]
    assert any(
        event["action"] == "execution_job_materialized"
        and event["resource_id"] == str(job.id)
        for event in db.list_durable_audit_events(limit=100)
    )
    assert not any(record["action"] == "coding_task" for record in read_audit(audit_path))


# ---------------------------------------------------------------------------
# coding_task 永不進自動核准（K.3 白名單只認 enqueue/stop）
# ---------------------------------------------------------------------------


def test_coding_task_never_auto_approved_even_with_any_rule(db, audit_path):
    _setup_project(db)
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    rules = [{"source": "any", "kind": "any"}]

    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=rules, audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


def test_coding_task_never_auto_approved_without_rules(db, audit_path):
    _setup_project(db)
    config = _config()
    approval = request_coding_task_approval(
        db, "proj1", "fix the bug", config=config, server_enabled={"server-a": True}, audit_path=audit_path
    )
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="chatgpt", rules=[], audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# vLLM app/agent_tools.py 沒有 coding_task 相關的任何工具
# ---------------------------------------------------------------------------


def test_agent_tools_has_no_coding_task_tool():
    from app.agent_tools import TOOLS

    forbidden = {"request_coding_task", "coding_task"}
    assert forbidden.isdisjoint(TOOLS.keys())


def test_agent_tools_module_does_not_import_request_coding_task_approval():
    import app.agent_tools as agent_tools_module

    source = open(agent_tools_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)

    assert "request_coding_task_approval" not in imported_names


# ---------------------------------------------------------------------------
# main.py：POST /projects/{name}/coding-task-request（v2 body）
# ---------------------------------------------------------------------------


@pytest.fixture
def codex_client(tmp_path, monkeypatch):
    """跟 `tests/conftest.py` 的 `api_client` 一樣起一個乾淨的
    `TestClient`，但額外把 `CODEX_RUNNER_SERVER` 指到一台 `servers.yaml`
    裡確實存在且 `enabled` 的機器（Codex 功能啟用）。**不改
    `tests/conftest.py`**——`api_client` 預設不設定
    `CODEX_RUNNER_SERVER`（Codex 功能停用）是既有 862 條測試假設的預設
    狀態，這裡另開一個獨立 fixture，不動到那些測試。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    servers_yaml_path = tmp_path / "servers.yaml"
    servers_yaml_path.write_text(
        "servers:\n"
        "  - name: server-a\n"
        "    host: 10.0.0.1\n"
        "    user: train\n"
        "    key: ~/.ssh/id_rsa\n"
        "    enabled: true\n"
    )
    monkeypatch.setenv("SERVERS_YAML_PATH", str(servers_yaml_path))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "server-a")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", "~/codex_workspaces")
    monkeypatch.setenv("CODEX_NETWORK_ACCESS", "false")

    import app.main as main_module

    # This fixture owns a synthetic, unroutable Runner and each status test
    # installs its own deterministic SSH probe.  Leaving the real monitor loop
    # active would race those observations and attempt to probe 10.0.0.1 in the
    # background.  Keep the task cancellable while preventing both effects.
    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        main_module.AppState,
        "monitor_loop",
        isolated_monitor_loop,
    )

    with TestClient(main_module.app) as client:
        yield client, main_module


def test_coding_task_request_endpoint_disabled_by_default(api_client):
    """`api_client`（沒設 CODEX_RUNNER_SERVER）：Codex 功能停用，400。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")

    resp = client.post(
        "/projects/proj1/coding-task-request", json={"instruction": "add a --dry-run flag"}
    )
    assert resp.status_code == 400
    assert "CODEX_RUNNER_SERVER" in resp.json()["detail"]
    assert db.list_approvals() == []


def test_coding_task_request_endpoint_creates_pending_approval(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.post(
        "/projects/proj1/coding-task-request", json={"instruction": "add a --dry-run flag"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "coding_task"
    assert body["status"] == "pending"
    assert body["payload"]["instruction"] == "add a --dry-run flag"
    assert body["payload"]["source_kind"] == "instance"
    assert body["payload"]["runner_server"] == "server-a"


def test_coding_task_request_accepts_structured_wizard_compatibility_payload(codex_client):
    """Slice 1's browser wizard still sends only the three existing fields."""
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    instruction = (
        "AI Engineering Task\n\n"
        "Task objective:\n"
        "- add a deterministic dry-run mode\n\n"
        "Acceptance criteria:\n"
        "- relevant tests pass\n\n"
        "Requested execution behavior:\n"
        "- modify files only in the isolated worktree"
    )

    resp = client.post(
        "/projects/proj1/coding-task-request",
        json={
            "instruction": instruction,
            "base_branch": "main",
            "validation_target": "server-a",
        },
    )

    assert resp.status_code == 200
    payload = resp.json()["payload"]
    assert payload["instruction"] == instruction
    assert payload["base_branch"] == "main"
    assert payload["validation_target"] == "server-a"
    assert "project_version_id" not in payload
    assert "agent_provider_id" not in payload


def test_coding_task_request_endpoint_legacy_server_matches_runner_accepted(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.post(
        "/projects/proj1/coding-task-request",
        json={"server": "server-a", "instruction": "add a --dry-run flag"},
    )
    assert resp.status_code == 200
    assert resp.json()["payload"]["legacy_server_param"] is True


def test_coding_task_request_endpoint_legacy_server_mismatch_rejected(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")

    resp = client.post(
        "/projects/proj1/coding-task-request",
        json={"server": "server-b", "instruction": "add a --dry-run flag"},
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_coding_task_request_endpoint_rejects_empty_instruction(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.post(
        "/projects/proj1/coding-task-request", json={"instruction": "   "}
    )
    assert resp.status_code == 400
    assert db.list_approvals() == []


def test_coding_task_full_flow_via_api_approve_endpoint(codex_client):
    """建立請求 -> POST /approve/{id} -> 真的走 coding_task v2 分支
    （app_state 注入假 ssh_run/ssh_write_file，比照
    test_apply_patch.py 的既有慣例——不打真實 SSH）。"""
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    main_module.app_state.ssh_run = RecordingSSH()
    main_module.app_state.ssh_write_file = RecordingWriteFile()

    create_resp = client.post(
        "/projects/proj1/coding-task-request", json={"instruction": "add a --dry-run flag"}
    )
    approval_id = create_resp.json()["id"]

    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["approval"]["status"] == "approved"
    assert f"ai-task-{approval_id}" in body["approval"]["note"]
    assert body["job"]["type"] == "coding"
    assert body["job"]["pin_server"] == "server-a"

    job = db.get_job(body["job"]["id"])
    assert job is not None
    assert job.status == "queued"

    coding_run = db.get_coding_run_by_job_id(job.id)
    assert coding_run is not None
    assert coding_run.approval_id == approval_id


# ---------------------------------------------------------------------------
# 前端 smoke：GET / 渲染 coding_task 卡片的關鍵字（static/index.html 本批
# 未變動）
# ---------------------------------------------------------------------------


def _read_workspace_asset(name):
    from pathlib import Path

    return (Path(__file__).parents[1] / "static" / name).read_text(encoding="utf-8")


def test_index_page_renders_coding_task_kind(api_client):
    """DG-UI-UNIFICATION v1 U8: see
    `tests/test_git_init.py::test_index_page_renders_git_init_kind` -- the
    legacy inlined-SPA `resp.text` pin moves to the ported `workspace.js`
    source directly (`GET /` itself only pins that the Workspace shell
    loads)."""
    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True
    _login(client, main_module)
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="workspace-navigation"' in resp.text
    assert "coding_task" in _read_workspace_asset("workspace.js")


# ---------------------------------------------------------------------------
# 前端 smoke：PLAN.md N.10（Codex Worker v2 前端，批次 3b）——伺服器頁
# Runner 徽章、建立 coding task 表單、Coding Runs 分頁、後續驗證
# （source_coding_run_id）關鍵字。DG-UI-UNIFICATION v1 U8: `static/ui.js`'s
# inlined-page pin moves to the ported `workspace.js`/`workspace-features.js`
# source directly (same rationale as the git_init/project_deploy smoke tests
# above).
# ---------------------------------------------------------------------------


def test_index_page_renders_codex_runner_status_wiring(api_client):
    assert "codex-runner/status" in _read_workspace_asset("workspace.js")


def test_index_page_loads_versioned_dependency_free_ui_assets(api_client):
    """DG-UI-UNIFICATION v1 U8: `static/ui.css`/`ui.js` are deleted; the
    equivalent versioned, dependency-free asset pair is
    `workspace.css`/`workspace.js` (+ `workspace-features.js`), asserted the
    same way -- versioned query string embedded in the served page, served
    successfully, and containing a known ported symbol."""
    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True
    _login(client, main_module)
    asset_version = "20260826-infra-direct-actions"
    index = client.get("/")
    css = client.get(f"/static/workspace.css?v={asset_version}")
    javascript = client.get(f"/static/workspace.js?v={asset_version}")

    assert index.status_code == 200
    assert f"/static/workspace.css?v={asset_version}" in index.text
    assert f"/static/workspace.js?v={asset_version}" in index.text
    assert css.status_code == 200
    assert "--canvas" in css.text
    assert javascript.status_code == 200
    assert "renderStructuredInstruction" in _read_workspace_asset("workspace-features.js")


def test_index_page_renders_coding_runs_ui(api_client):
    assert "coding-runs" in _read_workspace_asset("workspace.js")


def test_index_page_renders_codex_runner_badge_label(api_client):
    assert "Codex Runner" in _read_workspace_asset("workspace-features.js")


def test_index_page_renders_coding_task_worktree_warning(api_client):
    """DG-UI-UNIFICATION v1 U8: the legacy coding-task creation form's exact
    「不會直接修改正式專案」sentence is not carried over verbatim -- the U6a
    wizard's fixed-permissions summary (`engineering-wizard-fixed-
    permissions`, pinned in
    `tests/test_identity_workspace_v2.py::test_workspace_ai_engineering_panel_is_v2_only_and_instruction_contract_is_pinned`)
    conveys the same isolated-worktree guarantee structurally (修改專案檔案：
    允許·技術強制；worktree cleanup button) instead of restating it as
    prose. This test keeps a bounded equivalent: the worktree isolation
    concept is still named in the ported source."""
    assert "worktree" in _read_workspace_asset("workspace.js")


def test_index_page_renders_source_coding_run_id_wiring(api_client):
    assert "source_coding_run_id" in _read_workspace_asset("workspace.js")


# ---------------------------------------------------------------------------
# app.jobfinish：coding_runs 回填（PLAN.md N.6）
# ---------------------------------------------------------------------------


def _jobfinish_config(tmp_path, **overrides):
    base = dict(
        servers=[],
        local_home_dir=str(tmp_path),
        result_pull_timeout_sec=600,
    )
    base.update(overrides)
    return AppConfig(**base)


def _coding_job(**overrides):
    base = dict(
        id=42,
        type="coding",
        project="proj1",
        command="bash cmd.sh",
        require_tag=None,
        pin_server="server-a",
        status="done",
        server="server-a",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:10:00+00:00",
        exit_code=0,
        log_tail="[coding_task] 完成\n",
    )
    base.update(overrides)
    return Job(**base)


def _server_cfg():
    return ServerConfig(name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa")


class _RecordingLocalRun:
    def __init__(self, calls: list, exit_status: int = 0):
        self.calls = calls
        self.exit_status = exit_status

    async def __call__(self, command, timeout):
        self.calls.append(command)
        from app.sshpool import CommandResult

        return CommandResult(exit_status=self.exit_status, stdout="", stderr="")


async def _noop_send_mail(config, subject, body):
    return True


def _write_result_json(tmp_path, job_id, data):
    result_dir = tmp_path / "results" / str(job_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")
    return result_dir


def test_current_compatibility_wrapper_sanitizes_result_before_database(
    db, tmp_path, audit_path
):
    job = _coding_job(
        command="log '自動 repository validation 已安全跳過：受控 sandbox 尚未啟用'"
    )
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        job_id=job.id,
    )
    raw_secret = "runner-basic-secret"
    _write_result_json(
        tmp_path,
        job.id,
        {
            "status": "failed",
            "base_commit": "a" * 40,
            "result_branch": "ai-task-1",
            "result_commit": "b" * 40,
            "test_command": "python3 -m pytest -q",
            "test_exit_code": 0,
            "codex_version": "codex-cli 0.144.3",
            "error_message": f"Authorization: Basic {raw_secret}",
        },
    )

    _backfill_coding_run(
        job,
        db=db,
        config=_jobfinish_config(tmp_path),
        audit_path=audit_path,
    )

    run = db.get_coding_run(run_id)
    assert run.status == "failed"
    assert run.test_command is None
    assert run.test_exit_code is None
    assert run.error_message == (
        "Coding Runner 回報執行失敗；請查看經過遮罩的任務日誌"
    )
    assert raw_secret not in run.error_message


def test_legacy_coding_run_projection_redacts_runner_and_instruction_credentials(db):
    from app.main import _coding_run_to_dict

    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="Authorization: Basic request-secret",
        test_command="python tool.py --password=runner-secret",
        codex_version="password=version-secret",
        status="failed",
    )
    db.update_coding_run(
        run_id,
        error_message="Authorization: Basic error-secret",
    )

    data = _coding_run_to_dict(db.get_coding_run(run_id))

    serialized = json.dumps(data)
    assert "request-secret" not in serialized
    assert "runner-secret" not in serialized
    assert "version-secret" not in serialized
    assert "error-secret" not in serialized
    assert serialized.count("[REDACTED]") == 4


def test_jobfinish_backfills_coding_run_done(db, tmp_path, audit_path):
    job = _coding_job(status="done")
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", job_id=job.id
    )
    result_dir = _write_result_json(
        tmp_path,
        job.id,
        {
            "status": "done",
            "base_commit": "aaa111",
            "result_branch": "ai-task-1",
            "result_commit": "bbb222",
            "no_changes": False,
            "test_command": "python3 -m pytest -q",
            "test_exit_code": 0,
            "codex_exit": 0,
            "diff_summary": "1 file changed",
            "codex_version": "codex-cli 0.144.1",
            "error_message": None,
        },
    )
    (result_dir / "changes.bundle").write_text("bundle-bytes")
    config = _jobfinish_config(tmp_path)

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=_noop_send_mail,
        )
    )

    coding_run = db.get_coding_run(run_id)
    assert coding_run.status == "done"
    assert coding_run.base_commit == "aaa111"
    assert coding_run.result_commit == "bbb222"
    assert coding_run.test_exit_code == 0
    assert coding_run.codex_version == "codex-cli 0.144.1"
    assert coding_run.bundle_path == str(result_dir / "changes.bundle")
    assert coding_run.started_at == job.started_at
    assert coding_run.finished_at == job.finished_at

    records = read_audit(audit_path)
    finished_events = [r for r in records if r["action"] == "coding_finished"]
    assert len(finished_events) == 1
    assert finished_events[0]["params"]["coding_run_id"] == run_id
    assert finished_events[0]["params"]["result_commit"] == "bbb222"

    durable_events = [
        event
        for event in db.list_durable_audit_events(limit=50)
        if event["action"] == "coding_run_result_recorded"
    ]
    assert len(durable_events) == 1
    assert durable_events[0]["params"] == {
        "coding_run_id": run_id,
        "job_id": job.id,
        "result_commit_present": True,
        "status": "done",
    }
    assert durable_events[0]["resource_type"] == "coding_run"
    assert durable_events[0]["resource_id"] == str(run_id)
    assert durable_events[0]["approval_id"] == 1
    assert durable_events[0]["actor"] == {
        "id": "system",
        "kind": "system",
        "authentication": "system",
    }


def test_unbound_coding_run_result_rolls_back_when_durable_audit_fails(
    db, tmp_path, monkeypatch
):
    job = _coding_job()
    run_id = db.insert_coding_run(
        approval_id=11,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        job_id=job.id,
        status="running",
    )
    _write_result_json(
        tmp_path,
        job.id,
        {
            "status": "done",
            "base_commit": "a" * 40,
            "result_branch": "ai-task-11",
            "result_commit": "b" * 40,
        },
    )

    original_append = db.append_durable_audit_event_in_transaction

    def fail_result_event(cursor, **kwargs):
        if kwargs.get("action") == "coding_run_result_recorded":
            raise RuntimeError("injected durable audit failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_result_event)

    with pytest.raises(RuntimeError, match="injected durable audit failure"):
        _backfill_coding_run(
            job,
            db=db,
            config=_jobfinish_config(tmp_path),
            audit_path=str(tmp_path / "audit.jsonl"),
        )

    coding_run = db.get_coding_run(run_id)
    assert coding_run.status == "running"
    assert coding_run.result_commit is None
    assert db.count_durable_audit_events() == 1  # insert_coding_run only
    assert not any(
        event["action"] == "coding_run_result_recorded"
        for event in db.list_durable_audit_events(limit=50)
    )


def test_jobfinish_backfills_coding_run_no_changes(db, tmp_path, audit_path):
    job = _coding_job(status="done")
    run_id = db.insert_coding_run(
        approval_id=2, project="proj1", runner_server="server-a", instruction="noop", job_id=job.id
    )
    _write_result_json(
        tmp_path,
        job.id,
        {
            "status": "no_changes",
            "base_commit": "aaa111",
            "result_branch": "ai-task-2",
            "result_commit": "aaa111",
            "no_changes": True,
            "error_message": None,
        },
    )
    config = _jobfinish_config(tmp_path)

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=_noop_send_mail,
        )
    )
    assert db.get_coding_run(run_id).status == "no_changes"


def test_jobfinish_backfills_coding_run_secret_violation(db, tmp_path, audit_path):
    job = _coding_job(status="failed", exit_code=1)
    run_id = db.insert_coding_run(
        approval_id=3, project="proj1", runner_server="server-a", instruction="leak secret", job_id=job.id
    )
    _write_result_json(
        tmp_path,
        job.id,
        {
            "status": "secret_violation",
            "base_commit": "aaa111",
            "result_branch": "ai-task-3",
            "result_commit": "ccc333",
            "error_message": "修改了受保護檔案（不產 bundle）：.env",
        },
    )
    config = _jobfinish_config(tmp_path)

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=_noop_send_mail,
        )
    )
    coding_run = db.get_coding_run(run_id)
    assert coding_run.status == "secret_violation"
    assert ".env" in coding_run.error_message
    assert coding_run.bundle_path is None


def test_jobfinish_coding_failed_job_still_pulls_results(db, tmp_path, audit_path):
    """coding 任務標成 failed 時也要嘗試拉結果（result.json/final_message
    往往是唯一的診斷線索），不能因為 job 是 failed 就整個跳過拉結果。"""
    job = _coding_job(status="failed", exit_code=1)
    db.insert_coding_run(
        approval_id=4, project="proj1", runner_server="server-a", instruction="fail case", job_id=job.id
    )
    pull_calls: list[str] = []
    config = _jobfinish_config(tmp_path)

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun(pull_calls),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=_noop_send_mail,
        )
    )
    assert pull_calls != []


def test_jobfinish_missing_result_json_marks_failed(db, tmp_path, audit_path):
    job = _coding_job(status="failed", exit_code=1)
    run_id = db.insert_coding_run(
        approval_id=5, project="proj1", runner_server="server-a", instruction="broken", job_id=job.id
    )
    config = _jobfinish_config(tmp_path)

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=_noop_send_mail,
        )
    )
    coding_run = db.get_coding_run(run_id)
    assert coding_run.status == "failed"
    assert "result.json" in coding_run.error_message


def test_jobfinish_legacy_coding_job_without_coding_run_does_not_crash(db, tmp_path, audit_path):
    """v1 遺留的 coding job：approve() 沒有建過 coding_run——跳過回填，不
    當成錯誤，照常寄信。"""
    job = _coding_job(status="done")
    config = _jobfinish_config(tmp_path)
    mail_calls: list[str] = []

    async def _recording_send_mail(config, subject, body):
        mail_calls.append(subject)
        return True

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=db,
            send_mail=_recording_send_mail,
        )
    )
    assert mail_calls  # 有寄信
    records = read_audit(audit_path)
    assert not any(r["action"] == "coding_finished" for r in records)
    assert any(r["action"] == "job_notified" for r in records)


def test_jobfinish_db_none_skips_backfill_entirely(db, tmp_path, audit_path):
    """`db` 沒傳入（呼叫端還沒接線）：完全跳過回填邏輯，不影響既有寄信
    行為（既有 862 條測試的假設）。"""
    job = _coding_job(status="done")
    config = _jobfinish_config(tmp_path)
    mail_calls: list[str] = []

    async def _recording_send_mail(config, subject, body):
        mail_calls.append(subject)
        return True

    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=_server_cfg(),
            local_run=_RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            send_mail=_recording_send_mail,
        )
    )
    assert mail_calls


# ---------------------------------------------------------------------------
# 批次 3a：build_coding_task_script() 的 GIT_ACCESS -> gitsrc() 硬化
# ---------------------------------------------------------------------------


def test_build_coding_task_script_uses_gitsrc_function_not_git_access_variable():
    script = build_coding_task_script(*_SCRIPT_COMBOS[0])
    assert "gitsrc()" in script
    assert "$GIT_ACCESS" not in script
    assert "GIT_ACCESS=" not in script
    assert "gitsrc rev-parse" in script
    assert "gitsrc worktree add" in script


def test_build_coding_task_script_mirror_gitsrc_uses_git_dir_mirror():
    script = build_coding_task_script(*_SCRIPT_COMBOS[2])
    assert 'gitsrc() { git --git-dir="$MIRROR" "$@"; }' in script


def test_build_coding_task_script_instance_path_with_space_passes_bash_syntax_check():
    """路徑含空白是這次硬化要修的問題：舊的 `$GIT_ACCESS` 無引號展開會把
    空白斷成兩個字，新的 `gitsrc()` 函式內部用 `"$SRC"`（加引號）不會。"""
    script = build_coding_task_script(
        1, "codex_workspaces", "proj1", "instance", "/data/my project/proj1", None, False
    )
    ok, stderr = _bash_n_ok(script)
    assert ok, stderr
    assert "gitsrc" in script


# ---------------------------------------------------------------------------
# GET /codex-runner/status（PLAN.md N.6）
# ---------------------------------------------------------------------------


class FakeCodexProbeSSH:
    def __init__(self, output: str = "codex-cli 0.144.1\nAUTH_OK\n"):
        self.output = output
        self.calls = 0

    async def __call__(self, server, command, timeout):
        self.calls += 1
        return CodingFakeCommandResult(stdout=self.output)


def test_codex_runner_status_unconfigured_returns_configured_false(api_client):
    client, _main = api_client
    resp = client.get("/codex-runner/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_codex_runner_status_configured_full_shape_no_secrets_leaked(codex_client):
    client, main_module = codex_client
    main_module.app_state.server_states["server-a"].online = True
    main_module.app_state.ssh_run = FakeCodexProbeSSH("codex-cli 0.144.1\nAUTH_OK\n")

    resp = client.get("/codex-runner/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "configured": True,
        "server": "server-a",
        "online": True,
        "probe_status": "ok",
        "codex_installed": True,
        "codex_version": "codex-cli 0.144.1",
        "authenticated": True,
        "auth_mode": "chatgpt",
        "busy": False,
        "running_job_id": None,
        "max_concurrency": 1,
    }
    body_text = json.dumps(body)
    assert "token" not in body_text.lower()
    assert "email" not in body_text.lower()
    assert "auth.json" not in body_text


def test_codex_runner_status_not_installed_and_not_authenticated(codex_client):
    client, main_module = codex_client
    main_module.app_state.server_states["server-a"].online = True
    main_module.app_state.ssh_run = FakeCodexProbeSSH("NO_CODEX\nAUTH_NO\n")

    resp = client.get("/codex-runner/status")
    body = resp.json()
    assert body["codex_installed"] is False
    assert body["codex_version"] is None
    assert body["authenticated"] is False


def test_codex_runner_status_offline_skips_probe_returns_default(codex_client):
    client, main_module = codex_client
    main_module.app_state.server_states["server-a"].online = False
    fake = FakeCodexProbeSSH()
    main_module.app_state.ssh_run = fake

    resp = client.get("/codex-runner/status")
    body = resp.json()
    assert body["online"] is False
    assert body["probe_status"] == "offline"
    assert body["codex_installed"] is False
    assert body["authenticated"] is False
    assert fake.calls == 0


def test_codex_runner_status_probe_failure_is_not_misreported_as_not_installed(
    codex_client, caplog,
):
    client, main_module = codex_client
    main_module.app_state.server_states["server-a"].online = True

    async def failed_probe(_server, _command, _timeout):
        raise ConnectionError("synthetic private probe detail")

    main_module.app_state.ssh_run = failed_probe
    response = client.get("/codex-runner/status")

    assert response.status_code == 200
    body = response.json()
    assert body["online"] is True
    assert body["probe_status"] == "probe_failed"
    assert body["codex_installed"] is False
    assert body["authenticated"] is False
    assert "synthetic private probe detail" not in response.text
    assert "synthetic private probe detail" not in caplog.text


def test_codex_runner_status_probe_result_cached_for_30_seconds(codex_client):
    client, main_module = codex_client
    main_module.app_state.server_states["server-a"].online = True
    fake = FakeCodexProbeSSH()
    main_module.app_state.ssh_run = fake

    client.get("/codex-runner/status")
    assert fake.calls == 1
    client.get("/codex-runner/status")
    assert fake.calls == 1  # 30 秒內第二次呼叫用快取，不再 SSH

    main_module.app_state._codex_probe_cache_at -= 31  # 模擬 TTL 過期
    client.get("/codex-runner/status")
    assert fake.calls == 2


def test_codex_runner_status_busy_reflects_running_coding_job(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    main_module.app_state.server_states["server-a"].online = True
    main_module.app_state.ssh_run = FakeCodexProbeSSH()
    job_id = db.insert_job(
        command="bash cmd.sh", type="coding", pin_server="server-a", status="running"
    )
    db.update_job(job_id, server="server-a")

    resp = client.get("/codex-runner/status")
    body = resp.json()
    assert body["busy"] is True
    assert body["running_job_id"] == job_id


# ---------------------------------------------------------------------------
# GET /coding-runs、GET /coding-runs/{id}（PLAN.md N.6）
# ---------------------------------------------------------------------------


def test_list_coding_runs_endpoint_excludes_paths_and_includes_has_bundle(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        worktree_path="~/codex_workspaces/tasks/1",
        bundle_path="/srv/dispatch-center/results/1/changes.bundle",
        status="done",
    )
    resp = client.get("/coding-runs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["id"] == run_id
    assert "worktree_path" not in item
    assert "bundle_path" not in item
    assert item["has_bundle"] is True


def test_list_coding_runs_endpoint_no_bundle_when_bundle_path_empty(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", status="queued"
    )
    resp = client.get("/coding-runs")
    assert resp.json()[0]["has_bundle"] is False


def test_list_coding_runs_endpoint_filters_by_status_and_project(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="a", status="done"
    )
    db.insert_coding_run(
        approval_id=2, project="proj2", runner_server="server-a", instruction="b", status="failed"
    )
    resp = client.get("/coding-runs", params={"status": "done"})
    assert [r["project"] for r in resp.json()] == ["proj1"]
    resp = client.get("/coding-runs", params={"project": "proj2"})
    assert [r["status"] for r in resp.json()] == ["failed"]


def test_get_coding_run_endpoint_404_when_missing(codex_client):
    client, _main = codex_client
    resp = client.get("/coding-runs/999")
    assert resp.status_code == 404


def test_get_coding_run_endpoint_reads_final_message_and_diff(codex_client, tmp_path):
    client, main_module = codex_client
    db = main_module.app_state.db
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        job_id=42,
        status="done",
    )
    result_dir = tmp_path / "results" / "42"
    result_dir.mkdir(parents=True)
    (result_dir / "final_message.txt").write_text("all good", encoding="utf-8")
    (result_dir / "diff.patch").write_text("diff --git a b", encoding="utf-8")

    resp = client.get(f"/coding-runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["final_message"] == "all good"
    assert body["diff_patch"] == "diff --git a b"
    assert "worktree_path" not in body
    assert "bundle_path" not in body


def test_get_coding_run_endpoint_missing_files_return_none(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", job_id=42, status="queued"
    )
    resp = client.get(f"/coding-runs/{run_id}")
    body = resp.json()
    assert body["final_message"] is None
    assert body["diff_patch"] is None


def test_get_coding_run_endpoint_truncates_large_final_message(codex_client, tmp_path):
    client, main_module = codex_client
    db = main_module.app_state.db
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", job_id=42, status="done"
    )
    result_dir = tmp_path / "results" / "42"
    result_dir.mkdir(parents=True)
    (result_dir / "final_message.txt").write_text("x" * 70000, encoding="utf-8")

    resp = client.get(f"/coding-runs/{run_id}")
    body = resp.json()
    assert len(body["final_message"]) <= 65536 + 100
    assert "截斷" in body["final_message"]


# ---------------------------------------------------------------------------
# POST /coding-runs/{id}/cleanup（PLAN.md N.9 鐵律 10/11）
# ---------------------------------------------------------------------------


def test_cleanup_coding_run_404_when_missing(codex_client):
    client, _main = codex_client
    resp = client.post("/coding-runs/999/cleanup")
    assert resp.status_code == 404


def test_cleanup_coding_run_409_when_not_terminal(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", status="running"
    )
    resp = client.post(f"/coding-runs/{run_id}/cleanup")
    assert resp.status_code == 409


def test_cleanup_coding_run_409_when_referenced_by_queued_job(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", status="done"
    )
    db.insert_job(command="echo hi", type="adhoc", source_coding_run_id=run_id)
    resp = client.post(f"/coding-runs/{run_id}/cleanup")
    assert resp.status_code == 409


def test_cleanup_coding_run_success_removes_task_dir_and_prunes_instance_worktree(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    approval_id = db.insert_approval(
        kind="coding_task",
        payload={"project": "proj1", "source_kind": "instance", "source": "/data/proj1"},
    )
    run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        status="done",
        worktree_path=f"~/codex_workspaces/tasks/{approval_id}",
    )
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.post(f"/coding-runs/{run_id}/cleanup")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    rm_calls = [c for c in ssh.calls if c[1].startswith("rm -rf ")]
    assert len(rm_calls) == 1
    assert f"codex_workspaces/tasks/{approval_id}" in rm_calls[0][1]

    prune_calls = [c for c in ssh.calls if "worktree prune" in c[1]]
    assert len(prune_calls) == 1
    assert "/data/proj1" in prune_calls[0][1]

    coding_run = db.get_coding_run(run_id)
    assert coding_run.worktree_path is None

    records = read_audit(main_module.app_state.config.audit_path)
    cleanup_events = [r for r in records if r["action"] == "coding_cleanup"]
    assert len(cleanup_events) == 1
    assert cleanup_events[0]["params"] == {
        "coding_run_id": run_id,
        "approval_id": approval_id,
        "runner_server": "server-a",
    }
    durable = db.list_durable_audit_events(limit=100)
    intent = next(
        event
        for event in durable
        if event["action"] == "engineering_task_cleanup_intent"
    )
    outcome = next(
        event
        for event in durable
        if event["action"] == "engineering_task_cleanup_outcome"
    )
    assert intent["result"] == "intent"
    assert outcome["result"] == "applied"
    assert outcome["params"]["worktree_cleared"] is True
    assert outcome["params"]["prune_instance"] is True
    assert "/data/proj1" not in str(intent)
    assert "codex_workspaces" not in str(outcome)


def test_cleanup_coding_run_response_loss_is_unknown_and_not_replayed(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    approval_id = db.insert_approval(
        kind="coding_task",
        payload={"project": "proj1", "source_kind": "mirror", "source": "https://x/y.git"},
    )
    run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        status="done",
        worktree_path=f"~/codex_workspaces/tasks/{approval_id}",
    )
    first = RaisingCleanupSSH()
    main_module.app_state.ssh_run = first

    response = client.post(f"/coding-runs/{run_id}/cleanup")

    assert response.status_code == 409
    assert "SECRET-CLEANUP-REMOTE-DETAIL" not in response.text
    assert db.get_coding_run(run_id).worktree_path is not None
    durable = db.list_durable_audit_events(limit=100)
    assert [event["result"] for event in durable if event["action"] == "engineering_task_cleanup_outcome"] == ["unknown"]

    retry = RecordingSSH()
    main_module.app_state.ssh_run = retry
    response = client.post(f"/coding-runs/{run_id}/cleanup")

    assert response.status_code == 409
    assert retry.calls == []
    assert not any(
        event["result"] == "applied"
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "engineering_task_cleanup_outcome"
    )


def test_cleanup_coding_run_durable_append_failure_rolls_back_projection(
    codex_client, monkeypatch
):
    client, main_module = codex_client
    db = main_module.app_state.db
    approval_id = db.insert_approval(
        kind="coding_task", payload={"project": "proj1", "source_kind": "mirror"}
    )
    run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        status="failed",
        worktree_path=f"~/codex_workspaces/tasks/{approval_id}",
    )
    original_append = db.append_durable_audit_event_in_transaction

    def fail_applied(cursor, **kwargs):
        if (
            kwargs.get("action") == "engineering_task_cleanup_outcome"
            and kwargs.get("result") == "applied"
        ):
            raise ValueError("injected cleanup outcome append failure")
        return original_append(cursor, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_applied)
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    response = client.post(f"/coding-runs/{run_id}/cleanup")

    assert response.status_code == 409
    assert db.get_coding_run(run_id).worktree_path is not None
    outcomes = [
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "engineering_task_cleanup_outcome"
    ]
    assert [event["result"] for event in outcomes] == ["unknown"]
    assert len(ssh.calls) == 1


def test_cleanup_coding_run_mirror_source_no_worktree_prune(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    approval_id = db.insert_approval(
        kind="coding_task",
        payload={"project": "proj1", "source_kind": "mirror", "source": "https://x/y.git"},
    )
    run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        status="failed",
    )
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.post(f"/coding-runs/{run_id}/cleanup")
    assert resp.status_code == 200
    assert not any("worktree prune" in c[1] for c in ssh.calls)


def test_cleanup_coding_run_direct_call_not_found_and_not_cleanable(db, audit_path):
    with pytest.raises(CodingRunNotFoundError):
        asyncio.run(
            cleanup_coding_run(
                db, 999, ssh_run=RecordingSSH(), config=_config(), audit_path=audit_path
            )
        )

    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", status="queued"
    )
    with pytest.raises(CodingRunNotCleanableError):
        asyncio.run(
            cleanup_coding_run(
                db, run_id, ssh_run=RecordingSSH(), config=_config(), audit_path=audit_path
            )
        )


# ---------------------------------------------------------------------------
# resolve_codex_workspace_rel()（純函式，approve() 的 coding_task 分支與
# cleanup_coding_run() 共用）
# ---------------------------------------------------------------------------


def test_resolve_codex_workspace_rel_strips_tilde_prefix():
    assert resolve_codex_workspace_rel("~/codex_workspaces") == "codex_workspaces"
    assert resolve_codex_workspace_rel("codex_workspaces") == "codex_workspaces"
    assert resolve_codex_workspace_rel("~/a/b/") == "a/b"


def test_resolve_codex_workspace_rel_rejects_absolute_path():
    with pytest.raises(ValueError):
        resolve_codex_workspace_rel("/srv/codex_workspaces")


@pytest.mark.parametrize(
    "unsafe",
    ("", "~/", ".", "..", "../codex", "codex/../other", "codex//tasks", "codex work"),
)
def test_resolve_codex_workspace_rel_rejects_unsafe_relative_paths(unsafe):
    with pytest.raises(ValueError):
        resolve_codex_workspace_rel(unsafe)


# ---------------------------------------------------------------------------
# PLAN.md N.7：changes.bundle 下游流——build_bundle_checkout_preamble()／
# build_bundle_push_command()（純函式）
# ---------------------------------------------------------------------------


def test_build_bundle_checkout_preamble_contains_required_steps_and_no_rm_rf():
    preamble = build_bundle_checkout_preamble(7, "abc123", "/data/proj1")
    assert "bundle verify" in preamble
    assert "'+refs/heads/*:refs/coding-runs/7/*'" in preamble
    assert "cat-file -e" in preamble
    assert "worktree add --detach" in preamble
    assert 'cd "$CR_WT"' in preamble
    assert "rm -rf" not in preamble
    assert "abc123" in preamble
    assert "/data/proj1" in preamble
    assert "coding_bundles/7/changes.bundle" in preamble


def test_build_bundle_checkout_preamble_passes_bash_syntax_check():
    preamble = build_bundle_checkout_preamble(3, "deadbeef", "/data/my project/proj1")
    script = "#!/bin/bash\n" + preamble + "echo user-command-goes-here\n"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_build_bundle_push_command_contains_port_and_key_and_paths():
    target = ServerConfig(
        name="server-b", host="10.0.0.9", user="train", key="~/.ssh/id_rsa", port=2222
    )
    cmd = build_bundle_push_command(100, 7, target, "/srv/dispatch-center")
    assert "-i" in cmd and "id_rsa" in cmd
    assert "-p 2222" in cmd
    assert "coding_bundles/7" in cmd
    assert "/srv/dispatch-center/results/100/changes.bundle" in cmd
    assert "mkdir -p coding_bundles/7" in cmd
    assert "train@10.0.0.9" in cmd


def test_build_bundle_push_command_default_port_omits_dash_p():
    target = ServerConfig(name="server-b", host="10.0.0.9", user="train", key="~/.ssh/id_rsa")
    cmd = build_bundle_push_command(100, 7, target, "/srv/dispatch-center")
    assert "-p 22" not in cmd


# ---------------------------------------------------------------------------
# PLAN.md N.7：request_enqueue_approval() 的 source_coding_run_id 驗證
# ---------------------------------------------------------------------------


def _make_done_coding_run(
    db, tmp_path, *, project="proj1", runner="server-a", job_id=100, result_commit="cccddd"
):
    run_id = db.insert_coding_run(
        approval_id=1,
        project=project,
        runner_server=runner,
        instruction="fix",
        job_id=job_id,
        status="done",
        result_commit=result_commit,
        base_commit="base111",
    )
    result_dir = tmp_path / "results" / str(job_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "changes.bundle").write_bytes(b"bundle-bytes")
    return run_id


def test_request_enqueue_rejects_missing_coding_run(db, audit_path):
    with pytest.raises(ValueError):
        request_enqueue_approval(
            db,
            command="echo hi",
            pin_server="server-b",
            source_coding_run_id=999,
            audit_path=audit_path,
        )
    assert db.list_approvals() == []


def test_request_enqueue_rejects_coding_run_not_done(db, audit_path):
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-a", instruction="fix", status="running"
    )
    with pytest.raises(ValueError):
        request_enqueue_approval(
            db,
            command="echo hi",
            pin_server="server-b",
            source_coding_run_id=run_id,
            audit_path=audit_path,
        )


def test_request_enqueue_rejects_missing_local_bundle(db, tmp_path, audit_path):
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="fix",
        job_id=100,
        status="done",
        result_commit="deadbeef",
    )
    with pytest.raises(ValueError):
        request_enqueue_approval(
            db,
            command="echo hi",
            pin_server="server-b",
            source_coding_run_id=run_id,
            local_home_dir=str(tmp_path),
            audit_path=audit_path,
        )


def test_request_enqueue_rejects_missing_pin_server(db, tmp_path, audit_path):
    run_id = _make_done_coding_run(db, tmp_path)
    with pytest.raises(ValueError):
        request_enqueue_approval(
            db,
            command="echo hi",
            source_coding_run_id=run_id,
            local_home_dir=str(tmp_path),
            audit_path=audit_path,
        )


def test_request_enqueue_rejects_target_without_instance(db, tmp_path, audit_path):
    run_id = _make_done_coding_run(db, tmp_path)
    with pytest.raises(ValueError):
        request_enqueue_approval(
            db,
            command="echo hi",
            pin_server="server-b",
            source_coding_run_id=run_id,
            local_home_dir=str(tmp_path),
            audit_path=audit_path,
        )


def test_request_enqueue_auto_fills_project_from_coding_run(db, tmp_path, audit_path):
    db.insert_project("proj1", "/local/only/proj1")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    run_id = _make_done_coding_run(db, tmp_path)
    approval = request_enqueue_approval(
        db,
        command="echo hi",
        pin_server="server-b",
        source_coding_run_id=run_id,
        local_home_dir=str(tmp_path),
        audit_path=audit_path,
    )
    assert approval.payload["project"] == "proj1"
    assert approval.payload["source_coding_run_id"] == run_id


def test_request_enqueue_rejects_project_mismatch(db, tmp_path, audit_path):
    db.insert_project("proj1", "/local/only/proj1")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    run_id = _make_done_coding_run(db, tmp_path)
    with pytest.raises(ValueError):
        request_enqueue_approval(
            db,
            command="echo hi",
            project="other-proj",
            pin_server="server-b",
            source_coding_run_id=run_id,
            local_home_dir=str(tmp_path),
            audit_path=audit_path,
        )


def test_request_enqueue_without_source_coding_run_id_payload_has_none(db, audit_path):
    """向下相容：完全不帶這個參數的既有呼叫，payload 裡的
    `source_coding_run_id` 是 `None`，不影響既有行為。"""
    approval = request_enqueue_approval(db, command="echo hi", audit_path=audit_path)
    assert approval.payload["source_coding_run_id"] is None


# ---------------------------------------------------------------------------
# PLAN.md N.7：approve() 的 enqueue 分支——source_coding_run_id 落地
# ---------------------------------------------------------------------------


def test_approve_enqueue_with_source_coding_run_id_builds_push_job_and_preamble(db, tmp_path, audit_path):
    db.insert_project("proj1", "/local/only/proj1")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    run_id = _make_done_coding_run(db, tmp_path, job_id=100, result_commit="cccddd")

    server_configs = {
        "server-b": ServerConfig(name="server-b", host="10.0.0.9", user="train", key="~/.ssh/id_rsa"),
    }
    approval = request_enqueue_approval(
        db,
        command="python3 smoke_test.py",
        pin_server="server-b",
        source_coding_run_id=run_id,
        local_home_dir=str(tmp_path),
        audit_path=audit_path,
    )
    config = AppConfig(servers=[], local_home_dir=str(tmp_path))
    app_state = FakeAppState(config, RecordingWriteFile())

    result = asyncio.run(
        approve(
            db,
            approval.id,
            ssh_run=RecordingSSH(),
            audit_path=audit_path,
            server_configs=server_configs,
            app_state=app_state,
        )
    )
    job = result["job"]
    assert job.source_coding_run_id == run_id
    assert len(job.depends_on) == 1
    push_job = db.get_job(job.depends_on[0])
    assert push_job.type == "sync"
    assert push_job.pin_server == "_local"
    assert f"coding_bundles/{run_id}" in push_job.command
    assert "results/100/changes.bundle" in push_job.command

    assert "cccddd" in job.command  # preamble 含 result_commit
    assert "worktree add --detach" in job.command
    assert "rm -rf" not in job.command
    assert job.command.rstrip().endswith("python3 smoke_test.py")

    durable = [
        event
        for event in db.list_durable_audit_events(limit=50)
        if event["action"] == "execution_job_materialized"
        and event["approval_id"] == approval.id
    ]
    assert {event["params"]["job_role"] for event in durable} == {
        "bundle_push",
        "main",
    }
    assert {
        event["resource_id"] for event in durable
    } == {str(push_job.id), str(job.id)}
    assert not any(
        record["action"] in {"approve", "enqueue"}
        for record in read_audit(audit_path)
    )


def test_approve_enqueue_without_source_coding_run_id_unaffected(db, audit_path):
    """既有行為不受影響：沒有 source_coding_run_id 的 enqueue 核准，
    command 原樣、depends_on 不多加任何東西。"""
    approval = request_enqueue_approval(db, command="echo hi", audit_path=audit_path)
    result = asyncio.run(approve(db, approval.id, audit_path=audit_path))
    assert result["job"].command == "echo hi"
    assert result["job"].depends_on == []
    assert result["job"].source_coding_run_id is None


# ---------------------------------------------------------------------------
# approve_endpoint()：coding_run_id 原樣帶出（批次 3a 補線）
# ---------------------------------------------------------------------------


def test_approve_endpoint_returns_coding_run_id_at_top_level(codex_client):
    client, main_module = codex_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    main_module.app_state.ssh_run = RecordingSSH()
    main_module.app_state.ssh_write_file = RecordingWriteFile()

    create_resp = client.post(
        "/projects/proj1/coding-task-request", json={"instruction": "add a --dry-run flag"}
    )
    approval_id = create_resp.json()["id"]

    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert "coding_run_id" in body
    coding_run = db.get_coding_run(body["coding_run_id"])
    assert coding_run is not None
    assert coding_run.approval_id == approval_id


def test_approve_endpoint_enqueue_kind_has_no_coding_run_id_key(api_client):
    """一般 enqueue 核准的回應不帶 `coding_run_id` 這個 key（不是
    `null`——完全不存在，呼叫端用 `"coding_run_id" in body` 判斷）。"""
    client, main_module = api_client
    resp = client.post("/dispatch", json={"command": "echo hi"})
    approval_id = resp.json()["id"]
    approve_resp = client.post(f"/approve/{approval_id}")
    assert "coding_run_id" not in approve_resp.json()


def test_dispatch_endpoint_with_source_coding_run_id_web_direct_executes(api_client, tmp_path):
    """PLAN.md N.7：WEB_DIRECT_EXECUTE／auto_approve 對這種 enqueue 照常
    適用——它是一般任務，不是 coding_task，不受「coding_task 永遠人工核准」
    那條規則限制。這裡走 `source="web"`（`web_direct_execute` 預設開），
    整個請求應該一步生效（`auto_approved: true`），完全比照一般 enqueue
    請求的既有行為。"""
    client, main_module = api_client
    db = main_module.app_state.db
    main_module.app_state.server_configs["server-b"] = ServerConfig(
        name="server-b", host="10.0.0.9", user="train", key="~/.ssh/id_rsa"
    )
    db.insert_project("proj1", "/local/only/proj1")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")
    run_id = _make_done_coding_run(db, tmp_path, job_id=200, result_commit="abc999")

    resp = client.post(
        "/dispatch",
        json={
            "command": "python3 smoke_test.py",
            "pin_server": "server-b",
            "source_coding_run_id": run_id,
            "source": "web",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("auto_approved") is True
    assert body["job"]["source_coding_run_id"] == run_id
    assert "abc999" in body["job"]["command"]
