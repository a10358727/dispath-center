"""DG-AGENT-SESSION-V1 P3 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P3):
checkpoint/promote glue.

**Scope actually implemented in this slice: step 1 only (`GET
.../agent-sessions/{session_id}/diff`)** — a read-only remote diff of the
session's persistent worktree, pure command builders plus a
`FakeAgentSessionSSH`-driven orchestration test and route-level round trip.

**Step 2 (`POST .../checkpoint-request` → bundle → existing
`engineering_task_promote` flow) is BLOCKED, not implemented here.** Evidence
(see the coder completion report for the full writeup): `resolve_promotion_
candidate()` (`app/code_promotion.py`) only ever reads a real `engineering_
tasks` row, and that table's schema pins `approval_id INTEGER NOT NULL
UNIQUE` (`app/db.py`, `CREATE TABLE engineering_tasks`) — every Engineering
Task the promotion machinery can ever resolve is backed by a genuine
`approvals` row. `app/approvals.py` repeatedly and explicitly documents that
the `engineering_task_request`/`coding_task` approval family **is never
auto-approved by `maybe_auto_approve()`** (module docstring, several sites).
Bridging a session checkpoint into that machinery without either (a) a new
approval kind, or (b) minting/auto-approving an existing-kind approval row
that never went through its real human-reviewed request flow, is not
possible as an additive change — both (a) and (b) are the exact conditions
the P3 packet names as BLOCKED triggers ("a new approval kind seems
needed"). No checkpoint route, no bridge row, and no migration were added;
nothing here creates or auto-approves any approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.agent_session_turns import (
    AGENT_SESSION_DIFF_MAX_BYTES,
    AGENT_SESSION_NO_WORKSPACE_MARKER,
    InvalidAgentSessionTurnInputError,
    build_diff_command,
    build_status_command,
    collect_agent_session_diff,
    session_repo_dir,
)

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
    `tests/test_agent_session_turns.py`'s `FakeAgentSessionSSH`)."""

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


def test_build_diff_command_golden_string():
    repo = f"codex_workspaces/agent_sessions/{SESSION_ID}/repo"
    cmd = build_diff_command("codex_workspaces", SESSION_ID, COMMIT)
    assert cmd == (
        f"[ -d {repo} ] || {{ echo {AGENT_SESSION_NO_WORKSPACE_MARKER}; exit 0; }}; "
        f"git -C {repo} --no-pager diff --no-ext-diff --no-textconv {COMMIT} -- "
        f"2>/dev/null | head -c {AGENT_SESSION_DIFF_MAX_BYTES}"
    )


def test_build_status_command_golden_string():
    repo = f"codex_workspaces/agent_sessions/{SESSION_ID}/repo"
    cmd = build_status_command("codex_workspaces", SESSION_ID)
    assert cmd == (
        f"[ -d {repo} ] || {{ echo {AGENT_SESSION_NO_WORKSPACE_MARKER}; exit 0; }}; "
        f"git -C {repo} status --porcelain --untracked-files=all -- "
        f"2>/dev/null | head -c {AGENT_SESSION_DIFF_MAX_BYTES}"
    )


def test_build_diff_command_rejects_invalid_session_id():
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_diff_command("codex_workspaces", "not-a-uuid", COMMIT)


def test_build_diff_command_rejects_invalid_commit():
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_diff_command("codex_workspaces", SESSION_ID, "not-a-commit")


def test_build_diff_command_rejects_leading_dash_commit():
    # A commit-shaped value can never start with '-' (hex-only regex), but
    # confirm the validator actually rejects a flag-smuggling attempt rather
    # than merely happening not to match the happy path.
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_diff_command("codex_workspaces", SESSION_ID, "-evil-flag")


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


@pytest.mark.asyncio
async def test_collect_agent_session_diff_available_with_dirty_and_untracked(db):
    session = _open_session(db)
    ssh = FakeAgentSessionDiffSSH()
    ssh.diff_stdout = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "+added line\n"
        "-removed line\n"
    )
    ssh.status_stdout = " M foo.py\n?? new_file.py\n"

    result = await collect_agent_session_diff(
        db,
        session=session,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert result.status == "available"
    assert result.available is True
    assert result.patch == ssh.diff_stdout
    assert result.withheld is False
    assert list(result.dirty_files) == ["foo.py"]
    assert list(result.untracked_files) == ["new_file.py"]
    assert "1 個檔案變更" in result.summary
    # Golden command shape reused end-to-end: base_commit resolved from the
    # session's base ProjectVersion, not caller-supplied.
    assert any(COMMIT in call for call in ssh.calls)


@pytest.mark.asyncio
async def test_collect_agent_session_diff_no_workspace_before_first_turn(db):
    session = _open_session(db)
    ssh = FakeAgentSessionDiffSSH()
    ssh.diff_stdout = AGENT_SESSION_NO_WORKSPACE_MARKER

    result = await collect_agent_session_diff(
        db,
        session=session,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert result.status == "no_workspace"
    assert result.available is False
    # Only the diff probe ran — status is never queried once "no workspace"
    # is already known from the diff probe.
    assert len(ssh.calls) == 1


@pytest.mark.asyncio
async def test_collect_agent_session_diff_unreachable_degrades_INV_SSH_7(db):
    session = _open_session(db)
    ssh = FakeAgentSessionDiffSSH()
    ssh.raise_on_run = ConnectionError("no route to host")

    result = await collect_agent_session_diff(
        db,
        session=session,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert result.status == "unreachable"
    assert result.available is False
    # Session itself is left completely untouched by a read-only probe.
    reloaded = db.get_agent_session(session.id)
    assert reloaded.status == "active"


@pytest.mark.asyncio
async def test_collect_agent_session_diff_unreachable_on_second_probe(db):
    session = _open_session(db)
    ssh = FakeAgentSessionDiffSSH()
    ssh.diff_stdout = "diff --git a/x b/x\n"
    ssh.raise_after_n_calls = 1  # diff probe succeeds, status probe fails

    result = await collect_agent_session_diff(
        db,
        session=session,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert result.status == "unreachable"
    assert result.available is False


@pytest.mark.asyncio
async def test_collect_agent_session_diff_withholds_private_key_content(db):
    session = _open_session(db)
    ssh = FakeAgentSessionDiffSSH()
    ssh.diff_stdout = (
        "diff --git a/secret.pem b/secret.pem\n"
        "+-----BEGIN RSA PRIVATE KEY-----\n"
        "+abc123\n"
        "+-----END RSA PRIVATE KEY-----\n"
    )
    ssh.status_stdout = " M secret.pem\n"

    result = await collect_agent_session_diff(
        db,
        session=session,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert result.status == "withheld"
    assert result.available is False
    assert result.withheld is True
    assert result.patch is None


@pytest.mark.asyncio
async def test_collect_agent_session_diff_requires_base_version(db):
    session = _open_session(db)
    broken_session = session.__class__(
        **{**session.__dict__, "base_version_id": None}
    )
    ssh = FakeAgentSessionDiffSSH()
    with pytest.raises(InvalidAgentSessionTurnInputError):
        await collect_agent_session_diff(
            db,
            session=broken_session,
            workspace_rel="codex_workspaces",
            runner_server="runner-a",
            ssh_run=ssh.run,
        )


# ---------------------------------------------------------------------------
# Route level (api_client, tests/conftest.py — mirrors
# tests/test_agent_session_turns.py's convention)
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


def test_diff_route_404_when_flag_disabled(api_client):
    client, _main = api_client
    assert client.get("/agent-sessions/some-id/diff").status_code == 404


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


def test_diff_route_happy_path(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_routes(client, main_module)

    ssh = FakeAgentSessionDiffSSH()
    ssh.diff_stdout = (
        "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n--- a/foo.py\n+x\n"
    )
    ssh.status_stdout = "?? untracked.py\n"
    main_module.app_state.ssh_run = ssh.run

    resp = client.get(f"/agent-sessions/{session_id}/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["status"] == "available"
    assert body["patch"] == ssh.diff_stdout
    assert body["untracked_files"] == ["untracked.py"]
    assert body["max_chars"] == 65536


def test_diff_route_unreachable_runner_degrades_200(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_routes(client, main_module)

    async def broken_run(server, command, timeout):
        raise ConnectionError("no route to host")

    main_module.app_state.ssh_run = broken_run

    resp = client.get(f"/agent-sessions/{session_id}/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["status"] == "unreachable"


def test_diff_route_404_for_unknown_session(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    resp = client.get("/agent-sessions/does-not-exist/diff")
    assert resp.status_code == 404
