"""DG-AGENT-SESSION-V1 P3 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P3):
checkpoint/promote glue.

**Step 1** (`GET .../agent-sessions/{session_id}/diff`) — a read-only remote
diff of the session's persistent worktree — was implemented first; its pure
command builders plus a `FakeAgentSessionDiffSSH`-driven orchestration test
and route-level round trip are unchanged below.

**Step 2** (`POST .../checkpoint-request` → bundle → existing
`engineering_task_promote` flow) was BLOCKED in that P3 slice: `resolve_
promotion_candidate()` (`app/code_promotion.py`) only ever reads a real
`engineering_tasks` row backed by a genuine, human-reviewed `approvals` row,
and the `engineering_task_request`/`coding_task` approval family is never
auto-approved. The BLOCKED finding was escalated and resolved by an explicit
user ruling: **DG-AGENT-SESSION-CHECKPOINT, Option A, approved 2026-08-24**
(`docs/DECISIONS.md` same-date entry; review packet
`docs/DG_AGENT_SESSION_CHECKPOINT_DECISION.md`) — a new, narrowly-scoped
approval kind `agent_session_checkpoint` whose approve branch runs the
checkpoint pipeline (commit + secret-file gate + `git bundle create`/
`verify` on the Runner, then a local rsync pull + hash) and, only on
success, creates honest bridge `engineering_tasks`/`coding_runs`/
`engineering_task_artifacts` rows with `approval_id` pinned to *that*
checkpoint approval — never a forged or auto-approved one. The completely
unmodified `engineering_task_promote` approval still decides ProjectVersion
promotion (DG-CODE-PROMOTE-v1 P-1..P-5 untouched); the two kinds are never
merged and neither is ever auto-approved.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from app.agent_session_bundle import (
    AGENT_SESSION_NO_WORKSPACE_MARKER,
    InvalidAgentSessionTurnInputError,
    build_checkpoint_bundle_pull_command,
    build_checkpoint_script,
    checkpoint_bundle_remote_path,
    checkpoint_bundle_staging_path,
    checkpoint_commit_message,
    run_agent_session_checkpoint_pipeline,
    session_repo_dir,
)
from app.approvals import (
    InvalidAgentSessionRequestError,
    approve,
    maybe_auto_approve,
    request_agent_session_checkpoint_approval,
)
from app.code_promotion import resolve_promotion_candidate
from app.config import AppConfig, ServerConfig
from app.db import VALID_APPROVAL_KINDS
from app.results import local_result_dir

SESSION_ID = "11111111-2222-3333-4444-555555555555"
COMMIT = "a" * 40


def _project_with_version(db, name="proj1", repo="https://example.invalid/proj1.git"):
    db.insert_project(name, repo)
    return db.get_or_create_project_version(name, COMMIT, git_ref="main")


@dataclass
class FakeCommandResult:
    stdout: str = ""


class FakeAgentSessionDiffSSH:
    """Duck-typed `ssh_run` fake for the diff/status probes only (no
    network, no subprocess — same convention as
    the retired turn suite's `FakeAgentSessionSSH`)."""

    def __init__(self):
        self.calls: list[str] = []
        self.diff_stdout = ""
        self.status_stdout = ""
        self.raise_on_run: Optional[BaseException] = None
        self.raise_after_n_calls: Optional[int] = None

    async def run(self, server, command, timeout):
        self.calls.append(command)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        if (
            self.raise_after_n_calls is not None
            and len(self.calls) > self.raise_after_n_calls
        ):
            raise ConnectionError("no route to host")
        if "--porcelain" in command:
            return FakeCommandResult(stdout=self.status_stdout)
        return FakeCommandResult(stdout=self.diff_stdout)


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_session_repo_dir_shape():
    assert (
        session_repo_dir("codex_workspaces", SESSION_ID)
        == f"codex_workspaces/agent_sessions/{SESSION_ID}/repo"
    )












# ---------------------------------------------------------------------------
# Orchestration (FakeAgentSessionDiffSSH, no network/subprocess)
# ---------------------------------------------------------------------------


def _open_session(db):
    version = _project_with_version(db)
    conv = db.get_or_create_project_conversation("proj1")
    approval_id = db.insert_approval(
        kind="agent_session_open", payload={}, requester_actor_id=None
    )
    return db.apply_agent_session_open_decision(
        approval_id=approval_id,
        project_id=version.project_id,
        conversation_id=conv.id,
        provider_id="claude-code",
        workspace_branch=f"ai-session-{SESSION_ID}",
        base_version_id=version.id,
        max_turns=200,
        turn_timeout_sec=600,
    )














# ---------------------------------------------------------------------------
# Route level (api_client, tests/conftest.py — mirrors
# the retired turn suite's convention)
# ---------------------------------------------------------------------------


def _server_config():
    from app.config import ServerConfig

    return ServerConfig(
        name="runner-a",
        host="10.0.0.9",
        user="train",
        key="~/.ssh/id_rsa",
        port=22,
        enabled=True,
    )




def _open_active_session_via_routes(client, main_module):
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    open_resp = client.post(
        "/projects/proj1/agent-sessions/open-request",
        json={"base_version_id": version.id},
    )
    approval_id = open_resp.json()["id"]
    approve_resp = client.post(f"/approve/{approval_id}")
    return approve_resp.json()["agent_session"]["id"]








# ===========================================================================
# DG-AGENT-SESSION-CHECKPOINT: pure builders
# ===========================================================================


def test_checkpoint_commit_message_and_paths():
    assert checkpoint_commit_message(SESSION_ID) == f"agent-session checkpoint {SESSION_ID}"
    assert checkpoint_bundle_remote_path("codex_workspaces", SESSION_ID) == (
        f"codex_workspaces/agent_sessions/{SESSION_ID}/checkpoint.bundle"
    )
    assert checkpoint_bundle_staging_path("/home/dispatch", 42) == (
        "/home/dispatch/agent_session_checkpoints/42.bundle"
    )
    with pytest.raises(InvalidAgentSessionTurnInputError):
        checkpoint_bundle_staging_path("/home/dispatch", 0)


def test_build_checkpoint_script_golden_shape_and_markers():
    script = build_checkpoint_script(
        session_id=SESSION_ID,
        workspace_rel="codex_workspaces",
        workspace_branch=f"ai-session-{SESSION_ID}",
        base_commit=COMMIT,
    )
    repo = f"codex_workspaces/agent_sessions/{SESSION_ID}/repo"
    bundle = f"codex_workspaces/agent_sessions/{SESSION_ID}/checkpoint.bundle"
    assert f"[ -d {repo} ]" in script
    assert AGENT_SESSION_NO_WORKSPACE_MARKER in script
    assert f"git -C {repo} bundle create {bundle} {COMMIT}..HEAD" in script
    assert f"git -C {repo} bundle verify {bundle}" in script
    assert f"-m 'agent-session checkpoint {SESSION_ID}'" in script
    assert "AGENT_SESSION_CHECKPOINT_STATUS=ok" in script
    assert "AGENT_SESSION_CHECKPOINT_RESULT_COMMIT=$RESULT_COMMIT" in script
    # Defense-in-depth end-of-options marker on the one pathspec-shaped git
    # positional this script builds (same convention as `build_diff_command`).
    assert f"diff --name-only {COMMIT}..HEAD -- " in script
    # The prompt/instruction text never appears in this script at all -- it
    # has none; only system-known identifiers are ever interpolated.
    assert "prompt.txt" not in script


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": "not-a-uuid"},
        {"workspace_branch": "not-the-session-branch"},
        {"base_commit": "not-a-commit"},
        {"base_commit": "-evil-flag"},
    ],
)
def test_build_checkpoint_script_rejects_invalid_input(kwargs):
    base = dict(
        session_id=SESSION_ID,
        workspace_rel="codex_workspaces",
        workspace_branch=f"ai-session-{SESSION_ID}",
        base_commit=COMMIT,
    )
    base.update(kwargs)
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_checkpoint_script(**base)


def test_build_checkpoint_bundle_pull_command_golden_shape():
    server = ServerConfig(name="runner-a", host="10.0.0.9", user="train", key="~/.ssh/id_rsa", port=22, enabled=True)
    cmd = build_checkpoint_bundle_pull_command(
        remote_bundle_path="codex_workspaces/agent_sessions/x/checkpoint.bundle",
        server=server,
        local_staging_path="/home/dispatch/agent_session_checkpoints/7.bundle",
    )
    assert "mkdir -p /home/dispatch/agent_session_checkpoints" in cmd
    assert "rsync -a -e" in cmd
    assert "train@10.0.0.9:codex_workspaces/agent_sessions/x/checkpoint.bundle" in cmd
    assert cmd.endswith("/home/dispatch/agent_session_checkpoints/7.bundle")


# ===========================================================================
# DG-AGENT-SESSION-CHECKPOINT: pipeline orchestration (fake ssh_run/local_run,
# no network/subprocess -- same convention as `FakeAgentSessionDiffSSH`).
# ===========================================================================


@dataclass
class FakeLocalRunResult:
    exit_status: int = 0
    stdout: str = ""


class FakeCheckpointSSH:
    def __init__(self, *, script_stdout: str, raise_on_run: Optional[BaseException] = None):
        self.script_stdout = script_stdout
        self.raise_on_run = raise_on_run
        self.calls: list[str] = []

    async def run(self, server, command, timeout):
        self.calls.append(command)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return FakeLocalRunResult(stdout=self.script_stdout)


def _pipeline_kwargs(db, ssh_run, local_run, tmp_path, approval_id=99):
    session = _open_session(db)
    return dict(
        db=db,
        session=session,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        server_config=ServerConfig(
            name="runner-a", host="10.0.0.9", user="train", key="~/.ssh/id_rsa", port=22, enabled=True
        ),
        local_home_dir=str(tmp_path),
        approval_id=approval_id,
        ssh_run=ssh_run,
        local_run=local_run,
        timeout_sec=300,
    )


@pytest.mark.asyncio
async def test_pipeline_ok_pulls_and_hashes_bundle(db, tmp_path):
    result_commit = "b" * 40
    stdout = (
        f"AGENT_SESSION_CHECKPOINT_RESULT_COMMIT={result_commit}\n"
        "AGENT_SESSION_CHECKPOINT_STATUS=ok\n"
    )
    ssh = FakeCheckpointSSH(script_stdout=stdout)

    async def fake_local_run(command, timeout):
        # Simulate the rsync pull by writing real bytes to the staging path
        # the command references (last shell-quoted token).
        dest = command.rsplit(" ", 1)[-1]
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"fake bundle bytes")
        return FakeLocalRunResult(exit_status=0)

    result = await run_agent_session_checkpoint_pipeline(
        **_pipeline_kwargs(db, ssh.run, fake_local_run, tmp_path)
    )
    assert result.status == "ok"
    assert result.result_commit == result_commit
    assert result.bundle_local_path is not None
    assert Path(result.bundle_local_path).read_bytes() == b"fake bundle bytes"
    assert result.bundle_sha256 == hashlib.sha256(b"fake bundle bytes").hexdigest()
    assert result.bundle_size_bytes == len(b"fake bundle bytes")


@pytest.mark.asyncio
async def test_pipeline_no_workspace(db, tmp_path):
    ssh = FakeCheckpointSSH(script_stdout=AGENT_SESSION_NO_WORKSPACE_MARKER + "\n")

    async def unreachable_local_run(command, timeout):  # never called
        raise AssertionError("local_run must not run for no_workspace")

    result = await run_agent_session_checkpoint_pipeline(
        **_pipeline_kwargs(db, ssh.run, unreachable_local_run, tmp_path)
    )
    assert result.status == "no_workspace"


@pytest.mark.parametrize(
    "marker_status",
    ["no_changes", "branch_mismatch", "secret_violation", "bundle_create_failed"],
)
@pytest.mark.asyncio
async def test_pipeline_non_ok_statuses_never_pull(db, tmp_path, marker_status):
    ssh = FakeCheckpointSSH(script_stdout=f"AGENT_SESSION_CHECKPOINT_STATUS={marker_status}\n")

    async def unreachable_local_run(command, timeout):  # never called
        raise AssertionError(f"local_run must not run for {marker_status}")

    result = await run_agent_session_checkpoint_pipeline(
        **_pipeline_kwargs(db, ssh.run, unreachable_local_run, tmp_path)
    )
    assert result.status == marker_status


@pytest.mark.asyncio
async def test_pipeline_unreachable_ssh_never_rejects_INV_SSH_7(db, tmp_path):
    ssh = FakeCheckpointSSH(script_stdout="", raise_on_run=ConnectionError("no route to host"))

    async def unreachable_local_run(command, timeout):  # never called
        raise AssertionError("local_run must not run when ssh_run is unreachable")

    result = await run_agent_session_checkpoint_pipeline(
        **_pipeline_kwargs(db, ssh.run, unreachable_local_run, tmp_path)
    )
    assert result.status == "unreachable"


@pytest.mark.asyncio
async def test_pipeline_pull_failed_when_local_run_raises(db, tmp_path):
    stdout = f"AGENT_SESSION_CHECKPOINT_RESULT_COMMIT={'c' * 40}\nAGENT_SESSION_CHECKPOINT_STATUS=ok\n"
    ssh = FakeCheckpointSSH(script_stdout=stdout)

    async def broken_local_run(command, timeout):
        raise ConnectionError("no route to host")

    result = await run_agent_session_checkpoint_pipeline(
        **_pipeline_kwargs(db, ssh.run, broken_local_run, tmp_path)
    )
    assert result.status == "unreachable"


@pytest.mark.asyncio
async def test_pipeline_pull_failed_on_nonzero_exit(db, tmp_path):
    stdout = f"AGENT_SESSION_CHECKPOINT_RESULT_COMMIT={'d' * 40}\nAGENT_SESSION_CHECKPOINT_STATUS=ok\n"
    ssh = FakeCheckpointSSH(script_stdout=stdout)

    async def failing_local_run(command, timeout):
        return FakeLocalRunResult(exit_status=1)

    result = await run_agent_session_checkpoint_pipeline(
        **_pipeline_kwargs(db, ssh.run, failing_local_run, tmp_path)
    )
    assert result.status == "pull_failed"


# ===========================================================================
# DG-AGENT-SESSION-CHECKPOINT: kind registration + request-time validation
# ===========================================================================


def _pin_never_auto_approved(db, kind: str) -> None:
    """`maybe_auto_approve()` only ever considers kind in ("enqueue", "stop")
    (module docstring, "只支援 kind 為 enqueue/stop"); every other kind,
    `agent_session_checkpoint` included, short-circuits to `None`
    (pending stays pending) regardless of `rules`/`source` -- this is the
    exact allowlist boundary this kind must never join."""

    approval_id = db.insert_approval(kind=kind, payload={}, requester_actor_id=None)
    approval = db.get_approval(approval_id)
    outcome = asyncio.run(
        maybe_auto_approve(db, approval, source="web", rules=[{"kind": "any"}])
    )
    assert outcome is None
    assert db.get_approval(approval_id).status == "pending"


def test_kind_registered_and_never_in_auto_approve_allowlist(db):
    assert "agent_session_checkpoint" in VALID_APPROVAL_KINDS
    for kind in ("agent_session_checkpoint", "agent_session_open", "engineering_task_promote"):
        _pin_never_auto_approved(db, kind)


def _checkpoint_config(**overrides) -> AppConfig:
    kwargs = dict(
        servers=[],
        agent_session_v1_enabled=True,
        codex_runner_server="runner-a",
    )
    kwargs.update(overrides)
    return AppConfig(**kwargs)


def test_request_checkpoint_flag_off_rejected(db):
    session = _open_session(db)
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_checkpoint_approval(
            db, session.id, config=_checkpoint_config(agent_session_v1_enabled=False)
        )


def test_request_checkpoint_unknown_session_rejected(db):
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_checkpoint_approval(
            db, "does-not-exist", config=_checkpoint_config()
        )


def test_request_checkpoint_inactive_session_rejected(db):
    session = _open_session(db)
    db.close_agent_session(session.id)
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_checkpoint_approval(db, session.id, config=_checkpoint_config())


def test_request_checkpoint_running_turn_rejected(db):
    session = _open_session(db)
    db.begin_agent_session_turn(session.id)
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_checkpoint_approval(db, session.id, config=_checkpoint_config())


def test_request_checkpoint_zero_turns_rejected(db):
    session = _open_session(db)
    assert session.turn_count == 0
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_checkpoint_approval(db, session.id, config=_checkpoint_config())


def test_request_checkpoint_duplicate_pending_rejected(db):
    session = _open_session(db)
    db.increment_agent_session_turn_count(session.id)
    request_agent_session_checkpoint_approval(db, session.id, config=_checkpoint_config())
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_checkpoint_approval(db, session.id, config=_checkpoint_config())


def test_request_checkpoint_happy_path_payload_shape(db):
    session = _open_session(db)
    db.increment_agent_session_turn_count(session.id)
    approval = request_agent_session_checkpoint_approval(db, session.id, config=_checkpoint_config())
    assert approval.kind == "agent_session_checkpoint"
    assert approval.status == "pending"
    assert approval.payload["session_id"] == session.id
    assert approval.payload["workspace_branch"] == session.workspace_branch
    assert approval.payload["runner_server"] == "runner-a"
    assert approval.payload["base_commit"] == COMMIT


def test_request_checkpoint_route_404_when_flag_disabled(api_client):
    client, _main = api_client
    resp = client.post("/agent-sessions/some-id/checkpoint-request")
    assert resp.status_code == 404


def test_request_checkpoint_route_happy_path(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_routes(client, main_module)
    # A real turn must complete before a checkpoint is requestable.
    db = main_module.app_state.db
    db.increment_agent_session_turn_count(session_id)

    resp = client.post(f"/agent-sessions/{session_id}/checkpoint-request")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "agent_session_checkpoint"
    assert body["status"] == "pending"


# ===========================================================================
# DG-AGENT-SESSION-CHECKPOINT: approve() branch, end to end
# ===========================================================================


def _git(*args: str, cwd: Path | None = None, check: bool = True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _real_hub_and_bundle(home: Path, project: str, result_suffix: str):
    """Build a real bare Hub with `base_commit` plus a real, valid
    `base..result` bundle -- same fixture shape as
    `tests/test_code_promotion.py::_seed_native_candidate`, reused here
    because the *promote* step this checkpoint bridges into
    (`verify_bundle_in_staging`) genuinely fetches `base_commit` from the
    Hub and runs `git bundle verify` against real bytes; faking those would
    weaken the exact boundary this test exists to exercise."""

    work = home / f"{project}-work"
    hub = home / "git" / f"{project}.git"
    work.mkdir(parents=True)
    _git("init", cwd=work)
    _git("checkout", "-b", "main", cwd=work)
    _git("config", "user.name", "Checkpoint Test", cwd=work)
    _git("config", "user.email", "checkpoint@example.invalid", cwd=work)
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base_commit = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    hub.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--bare", str(work), str(hub))

    _git("checkout", "-b", "checkpoint-result", cwd=work)
    (work / "README.md").write_text(f"base\n{result_suffix}\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "checkpoint result", cwd=work)
    result_commit = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    bundle_path = home / "runner_bundle" / "checkpoint.bundle"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    _git(
        "bundle", "create", str(bundle_path), "refs/heads/checkpoint-result", f"^{base_commit}",
        cwd=work,
    )
    return work, hub, base_commit, result_commit, bundle_path


class _CheckpointAppState:
    def __init__(self, config, server_configs):
        self.config = config
        self.server_configs = server_configs


def _open_checkpointable_session(db, home: Path, project: str = "demo"):
    work, hub, base_commit, result_commit, bundle_path = _real_hub_and_bundle(
        home, project, "checkpoint change"
    )
    db.insert_project(project, str(work))
    version = db.get_or_create_project_version(project, base_commit, git_ref="refs/heads/main")
    conv = db.get_or_create_project_conversation(project)
    open_approval_id = db.insert_approval(kind="agent_session_open", payload={}, requester_actor_id=None)
    session = db.apply_agent_session_open_decision(
        approval_id=open_approval_id,
        project_id=version.project_id,
        conversation_id=conv.id,
        provider_id="claude-code",
        workspace_branch=f"ai-session-{uuid.uuid4()}",
        base_version_id=version.id,
        max_turns=200,
        turn_timeout_sec=600,
    )
    db.increment_agent_session_turn_count(session.id)
    session = db.get_agent_session(session.id)
    return session, base_commit, result_commit, bundle_path


def _request_checkpoint(db, session, home: Path):
    return request_agent_session_checkpoint_approval(
        db, session.id, config=_checkpoint_config(local_home_dir=str(home))
    )


def _approve_checkpoint(db, home: Path, approval_id: int, *, ssh_run, local_run):
    return asyncio.run(
        approve(
            db,
            approval_id,
            ssh_run=ssh_run,
            local_run=local_run,
            server_configs={
                "runner-a": ServerConfig(
                    name="runner-a", host="10.0.0.9", user="train", key="~/.ssh/id_rsa", port=22, enabled=True
                )
            },
            app_state=_CheckpointAppState(
                _checkpoint_config(local_home_dir=str(home)),
                {"runner-a": ServerConfig(
                    name="runner-a", host="10.0.0.9", user="train", key="~/.ssh/id_rsa", port=22, enabled=True
                )},
            ),
            approved_by="human",
            audit_path="/dev/null",
        )
    )


def _fake_ssh_for_result(result_commit: str):
    stdout = f"AGENT_SESSION_CHECKPOINT_RESULT_COMMIT={result_commit}\nAGENT_SESSION_CHECKPOINT_STATUS=ok\n"

    async def ssh_run(server, command, timeout):
        return FakeLocalRunResult(stdout=stdout)

    return ssh_run


def _fake_pull_from(bundle_path: Path):
    async def local_run(command, timeout):
        dest = command.rsplit(" ", 1)[-1]
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(bundle_path.read_bytes())
        return FakeLocalRunResult(exit_status=0)

    return local_run


def test_approve_stale_session_rejects(db, tmp_path):
    session, base_commit, result_commit, bundle_path = _open_checkpointable_session(db, tmp_path)
    approval = _request_checkpoint(db, session, tmp_path)
    db.close_agent_session(session.id)  # INV-APPROVAL-3: state can change before approve

    result = _approve_checkpoint(
        db,
        tmp_path,
        approval.id,
        ssh_run=_fake_ssh_for_result(result_commit),
        local_run=_fake_pull_from(bundle_path),
    )
    assert result["approval"].status == "rejected"
    assert db.get_engineering_task_by_approval_id(approval.id) is None


def test_approve_secret_violation_rejects_zero_bridge_rows(db, tmp_path):
    session, base_commit, result_commit, bundle_path = _open_checkpointable_session(db, tmp_path)
    approval = _request_checkpoint(db, session, tmp_path)

    async def secret_ssh_run(server, command, timeout):
        return FakeLocalRunResult(stdout="AGENT_SESSION_CHECKPOINT_STATUS=secret_violation\n")

    async def never_pull(command, timeout):
        raise AssertionError("must never pull a bundle on a rejected pipeline")

    result = _approve_checkpoint(
        db, tmp_path, approval.id, ssh_run=secret_ssh_run, local_run=never_pull
    )
    assert result["approval"].status == "rejected"
    assert "secret_violation" in (result["approval"].note or "")
    assert db.get_engineering_task_by_approval_id(approval.id) is None


def test_approve_unreachable_stays_pending_INV_SSH_7(db, tmp_path):
    session, base_commit, result_commit, bundle_path = _open_checkpointable_session(db, tmp_path)
    approval = _request_checkpoint(db, session, tmp_path)

    async def unreachable_ssh_run(server, command, timeout):
        raise ConnectionError("no route to host")

    async def never_pull(command, timeout):
        raise AssertionError("must never pull when the runner is unreachable")

    result = _approve_checkpoint(
        db, tmp_path, approval.id, ssh_run=unreachable_ssh_run, local_run=never_pull
    )
    assert result["approval"].status == "pending"
    assert db.get_engineering_task_by_approval_id(approval.id) is None


def test_approve_happy_path_bridges_into_real_promotion_flow(db, tmp_path):
    from app.approvals import request_engineering_task_promote_approval

    session, base_commit, result_commit, bundle_path = _open_checkpointable_session(db, tmp_path)
    approval = _request_checkpoint(db, session, tmp_path)

    result = _approve_checkpoint(
        db,
        tmp_path,
        approval.id,
        ssh_run=_fake_ssh_for_result(result_commit),
        local_run=_fake_pull_from(bundle_path),
    )
    assert result["approval"].status == "approved"
    task_id = result["bridge_engineering_task_id"]
    assert isinstance(task_id, str) and task_id

    task = db.get_engineering_task_by_approval_id(approval.id)
    assert task is not None
    assert task.id == task_id
    assert task.status == "done"
    assert task.detected_metadata.get("source") == "agent_session_checkpoint"
    assert task.detected_metadata.get("session_id") == session.id

    run = db.get_coding_run(task.coding_run_id)
    assert run is not None
    assert run.status == "done"
    assert run.base_binding == "project_version_pinned"
    assert run.result_commit == result_commit
    expected_bundle_path = str(Path(local_result_dir(run.job_id, str(tmp_path))) / "changes.bundle")
    assert run.bundle_path == expected_bundle_path
    assert Path(run.bundle_path).exists()

    # The exact honest-bridge contract P3a's BLOCKED note named: a real
    # `resolve_promotion_candidate()` call must accept this row.
    config = _checkpoint_config(local_home_dir=str(tmp_path), code_promotion_v1_enabled=True)
    candidate = resolve_promotion_candidate(db, config, task_id)
    assert candidate.result_commit == result_commit
    assert candidate.base_commit == base_commit.lower()

    # Never auto-approved; never merged with `engineering_task_promote`.
    _pin_never_auto_approved(db, "agent_session_checkpoint")

    # Full, completely unmodified promote flow: request -> approve ->
    # ProjectVersion created.
    promote_approval = request_engineering_task_promote_approval(
        db, task_id, config=config, audit_path="/dev/null"
    )
    assert promote_approval.kind == "engineering_task_promote"
    promote_result = asyncio.run(
        approve(
            db,
            promote_approval.id,
            app_state=_CheckpointAppState(config, {}),
            approved_by="human",
            local_run=_promote_local_run,
            audit_path="/dev/null",
        )
    )
    assert promote_result["approval"].status == "approved"
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM project_versions WHERE promotion_approval_id = ?",
            (promote_approval.id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["git_commit"] == result_commit


async def _promote_local_run(command, timeout):
    from app.localrun import local_run as real_local_run

    return await real_local_run(command, timeout)
