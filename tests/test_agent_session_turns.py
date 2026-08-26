"""DG-AGENT-SESSION-V1 P2 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P2): the
per-turn Claude Code execution channel.

Pure builder tests (golden script pin, path/command shape, validated-
identifier rejection, argv-flag-smuggling guard) plus orchestration tests
against a `FakeAgentSessionSSH` (no real SSH/subprocess anywhere, INV-SSH-2/3
confirmed by asserting the hostile user message never appears in the written
`run.sh`) covering the sentinel three-branch settle (done/failed/
interrupted), the local lazy timeout fallback, unreachable-Runner degrade,
and the DB-enforced 409s (concurrent turn, turn-count limit). Route-level
tests (`api_client`, mirrors `tests/test_agent_sessions.py`'s convention)
cover the flag-off 404 and the end-to-end launch+settle round trip through
`POST .../messages` and `GET .../transcript`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional

import pytest

from app.agent_session_turns import (
    AGENT_SESSION_MESSAGE_MAX_BYTES,
    AGENT_SESSION_TURN_TIMEOUT_GRACE_SEC,
    InvalidAgentSessionTurnInputError,
    build_check_exit_code_command,
    build_dispatch_paths,
    build_launch_command,
    build_mkdir_command,
    build_read_file_command,
    build_tmux_check_command,
    build_transcript_tail_command,
    build_turn_script,
    converge_agent_session_turn,
    launch_agent_session_turn,
    tmux_session_name,
    turn_dir,
)
from app.db import (
    AgentSessionNotActiveError,
    AgentSessionTurnConflictError,
    AgentSessionTurnLimitError,
    Database,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"
BRANCH = f"ai-session-{SESSION_ID}"
COMMIT = "a" * 40


def _project_with_version(db, name="proj1", repo="https://example.invalid/proj1.git"):
    db.insert_project(name, repo)
    return db.get_or_create_project_version(name, COMMIT, git_ref="main")


def _open_active_session(db, *, max_turns=200, turn_timeout_sec=600):
    version = _project_with_version(db)
    conv = db.get_or_create_project_conversation("proj1")
    approval_id = db.insert_approval(
        kind="agent_session_open", payload={}, requester_actor_id=None
    )
    session = db.apply_agent_session_open_decision(
        approval_id=approval_id,
        project_id=version.project_id,
        conversation_id=conv.id,
        provider_id="claude-code",
        workspace_branch=BRANCH,
        base_version_id=version.id,
        max_turns=max_turns,
        turn_timeout_sec=turn_timeout_sec,
    )
    project = db.get_project("proj1")
    return session, project


@dataclass
class FakeCommandResult:
    stdout: str = ""


class FakeAgentSessionSSH:
    """Duck-typed `ssh_run`/`ssh_write_file` fake (no network, no subprocess
    — same convention as `tests/test_coding_task.py`'s `CodingFakeCommandResult`
    fixtures). Responds to the exact command shapes
    `app.agent_session_turns` builds."""

    def __init__(self):
        self.calls: list[str] = []
        self.written_files: dict[str, str] = {}
        self.exit_code: Optional[int] = None
        self.tmux_exists = True
        self.final_message = ""
        self.cli_session_id = ""
        self.raise_on_run: Optional[BaseException] = None

    async def run(self, server, command, timeout):
        self.calls.append(command)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        if command.startswith("cat ") and "exit_code" in command:
            return FakeCommandResult(
                stdout=str(self.exit_code) if self.exit_code is not None else ""
            )
        if "tmux has-session" in command:
            return FakeCommandResult(stdout="EXISTS" if self.tmux_exists else "GONE")
        if "final_message.txt" in command:
            return FakeCommandResult(stdout=self.final_message)
        if "cli_session_id.txt" in command:
            return FakeCommandResult(stdout=self.cli_session_id)
        return FakeCommandResult(stdout="")

    async def write_file(self, server, path, content):
        self.written_files[path] = content


# ---------------------------------------------------------------------------
# Pure builders: paths, commands, validated-identifier rejection
# ---------------------------------------------------------------------------


def test_turn_dir_and_tmux_session_name():
    assert (
        turn_dir("codex_workspaces", SESSION_ID, 3)
        == f"codex_workspaces/agent_sessions/{SESSION_ID}/turns/3"
    )
    assert tmux_session_name(SESSION_ID, 3) == f"agent_turn_{SESSION_ID[:8]}_3"


def test_build_dispatch_paths_shape():
    paths = build_dispatch_paths("codex_workspaces", "proj1", SESSION_ID, 1)
    assert paths.session_dir == f"codex_workspaces/agent_sessions/{SESSION_ID}"
    assert paths.repo_dir == f"{paths.session_dir}/repo"
    assert paths.mirror_path == "codex_workspaces/mirrors/proj1.git"
    assert paths.turn_dir == f"{paths.session_dir}/turns/1"
    assert paths.prompt_file == f"{paths.turn_dir}/prompt.txt"
    assert paths.transcript_file == f"{paths.turn_dir}/transcript.jsonl"
    assert paths.exit_code_file == f"{paths.turn_dir}/exit_code"
    assert paths.final_message_file == f"{paths.turn_dir}/final_message.txt"
    assert paths.cli_session_id_file == f"{paths.turn_dir}/cli_session_id.txt"


@pytest.mark.parametrize(
    "session_id, turn_no, branch, commit",
    [
        ("not-a-uuid", 1, BRANCH, COMMIT),
        (SESSION_ID, 0, BRANCH, COMMIT),
        (SESSION_ID, -1, BRANCH, COMMIT),
        (SESSION_ID, 1, "not-the-session-branch", COMMIT),
        (SESSION_ID, 1, BRANCH, "not-a-commit"),
    ],
)
def test_build_turn_script_rejects_unvalidated_identifiers(session_id, turn_no, branch, commit):
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_turn_script(
            session_id=session_id,
            turn_no=turn_no,
            workspace_rel="codex_workspaces",
            project="proj1",
            project_source_url="https://example.invalid/proj1.git",
            workspace_branch=branch,
            base_commit=commit,
            prior_cli_session_id=None,
            turn_timeout_sec=600,
        )


def test_build_turn_script_rejects_invalid_bash_allowlist_entry():
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_turn_script(
            session_id=SESSION_ID,
            turn_no=1,
            workspace_rel="codex_workspaces",
            project="proj1",
            project_source_url="https://example.invalid/proj1.git",
            workspace_branch=BRANCH,
            base_commit=COMMIT,
            prior_cli_session_id=None,
            turn_timeout_sec=600,
            bash_allowlist=("pytest; rm -rf /",),
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("project", "-evil-project"),
        ("project_source_url", "--upload-pack=touch /tmp/pwned;"),
    ],
)
def test_build_turn_script_rejects_leading_dash_argv_smuggling(field, value):
    """Security hardening: `shlex.quote()` alone does not stop a value
    starting with `-` from being parsed as a flag by the program it is
    handed to (`git clone --mirror <this>` here) — `project`/
    `project_source_url` are free-text DB columns with no format
    restriction, so this must be rejected before script assembly, not just
    shell-quoted."""

    kwargs = dict(
        session_id=SESSION_ID,
        turn_no=1,
        workspace_rel="codex_workspaces",
        project="proj1",
        project_source_url="https://example.invalid/proj1.git",
        workspace_branch=BRANCH,
        base_commit=COMMIT,
        prior_cli_session_id=None,
        turn_timeout_sec=600,
    )
    kwargs[field] = value
    with pytest.raises(InvalidAgentSessionTurnInputError, match="argv flag smuggling"):
        build_turn_script(**kwargs)


def test_build_turn_script_is_syntactically_valid_bash(tmp_path):
    script = build_turn_script(
        session_id=SESSION_ID,
        turn_no=1,
        workspace_rel="codex_workspaces",
        project="proj1",
        project_source_url="https://example.invalid/proj1.git",
        workspace_branch=BRANCH,
        base_commit=COMMIT,
        prior_cli_session_id=None,
        turn_timeout_sec=600,
    )
    script_path = tmp_path / "run.sh"
    script_path.write_text(script)
    subprocess.run(["bash", "-n", str(script_path)], check=True)


def test_build_turn_script_with_resume_and_bash_allowlist_is_syntactically_valid(tmp_path):
    prior_cli_session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    script = build_turn_script(
        session_id=SESSION_ID,
        turn_no=2,
        workspace_rel="codex_workspaces",
        project="proj1",
        project_source_url="https://example.invalid/proj1.git",
        workspace_branch=BRANCH,
        base_commit=COMMIT,
        prior_cli_session_id=prior_cli_session_id,
        turn_timeout_sec=600,
        bash_allowlist=("pytest -q", "git diff"),
        network_access=True,
    )
    assert f"--resume {prior_cli_session_id}" in script
    assert "Bash(pytest -q:*)" in script
    assert "Bash(git diff:*)" in script
    script_path = tmp_path / "run.sh"
    script_path.write_text(script)
    subprocess.run(["bash", "-n", str(script_path)], check=True)


def test_build_turn_script_uses_end_of_options_marker_for_externally_influenceable_git_args():
    """Pins the argv-flag-smuggling fix: every git subcommand that receives
    `project_source_url` (repo clone URL) or the worktree destination as a
    bare positional argument must terminate option parsing with `--` first,
    even though `project`/`project_source_url` are additionally rejected
    outright when they start with `-` (defense in depth, see
    `test_build_turn_script_rejects_leading_dash_argv_smuggling`)."""

    script = build_turn_script(
        session_id=SESSION_ID,
        turn_no=1,
        workspace_rel="codex_workspaces",
        project="proj1",
        project_source_url="https://example.invalid/proj1.git",
        workspace_branch=BRANCH,
        base_commit=COMMIT,
        prior_cli_session_id=None,
        turn_timeout_sec=600,
    )
    assert (
        "git clone --mirror -- https://example.invalid/proj1.git "
        "codex_workspaces/mirrors/proj1.git" in script
    )
    assert (
        f"git --git-dir=codex_workspaces/mirrors/proj1.git worktree add "
        f"-b {BRANCH} -- codex_workspaces/agent_sessions/{SESSION_ID}/repo "
        f"{COMMIT}" in script
    )


def test_build_turn_script_golden_string_pin():
    """Pins the exact turn-1 script byte-for-byte (D3: any accidental drift
    in the confinement flags/preflight/sentinel shape must fail loudly)."""

    script = build_turn_script(
        session_id=SESSION_ID,
        turn_no=1,
        workspace_rel="codex_workspaces",
        project="proj1",
        project_source_url="https://example.invalid/proj1.git",
        workspace_branch=BRANCH,
        base_commit=COMMIT,
        prior_cli_session_id=None,
        turn_timeout_sec=600,
    )
    assert script == EXPECTED_GOLDEN_TURN_SCRIPT


def test_build_turn_script_never_contains_resume_flag_on_first_turn():
    script = build_turn_script(
        session_id=SESSION_ID,
        turn_no=1,
        workspace_rel="codex_workspaces",
        project="proj1",
        project_source_url="https://example.invalid/proj1.git",
        workspace_branch=BRANCH,
        base_commit=COMMIT,
        prior_cli_session_id=None,
        turn_timeout_sec=600,
    )
    assert "--resume" not in script


def test_build_transcript_tail_command_uses_byte_offset_and_bound():
    command = build_transcript_tail_command(
        "codex_workspaces", "proj1", SESSION_ID, 1, offset=100, max_bytes=2048
    )
    assert command == (
        f"tail -c +101 codex_workspaces/agent_sessions/{SESSION_ID}/turns/1/transcript.jsonl "
        "2>/dev/null | head -c 2048"
    )


def test_build_launch_and_tmux_check_and_mkdir_commands_are_shell_quoted():
    mkdir_cmd = build_mkdir_command("codex_workspaces", "proj1", SESSION_ID, 1)
    assert mkdir_cmd == (
        f"mkdir -p codex_workspaces/agent_sessions/{SESSION_ID}/turns/1"
    )
    launch_cmd = build_launch_command(SESSION_ID, 1, "some/run.sh")
    assert launch_cmd == (
        f"tmux new-session -d -s agent_turn_{SESSION_ID[:8]}_1 'bash some/run.sh'"
    )
    tmux_cmd = build_tmux_check_command(SESSION_ID, 1)
    assert tmux_cmd == (
        f"tmux has-session -t agent_turn_{SESSION_ID[:8]}_1 2>/dev/null && echo EXISTS || echo GONE"
    )


# ---------------------------------------------------------------------------
# Orchestration: launch + lazy settle (FakeAgentSessionSSH, no network)
# ---------------------------------------------------------------------------


async def _launch(db, session, project, ssh, content="please fix the bug"):
    return await launch_agent_session_turn(
        db,
        session=session,
        project=project,
        content=content,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
        ssh_write_file=ssh.write_file,
    )


@pytest.mark.asyncio
async def test_launch_never_puts_user_message_in_the_written_script(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    hostile = "hi'; rm -rf ~; echo pwned #\n$(whoami)"
    result = await _launch(db, session, project, ssh, content=hostile)

    run_sh = [v for k, v in ssh.written_files.items() if k.endswith("run.sh")]
    assert len(run_sh) == 1
    assert hostile not in run_sh[0]

    prompt_files = [v for k, v in ssh.written_files.items() if k.endswith("prompt.txt")]
    assert prompt_files == [hostile]
    assert result.turn_no == 1


@pytest.mark.asyncio
async def test_launch_persists_user_message_and_claims_turn(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh, content="hello claude")

    assert result.user_message.role == "user"
    assert result.user_message.content == "hello claude"
    refreshed = db.get_agent_session(session.id)
    assert refreshed.active_turn_no == 1
    assert refreshed.turn_count == 0  # only settle() increments turn_count


@pytest.mark.asyncio
async def test_second_launch_while_a_turn_is_running_raises_conflict(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    await _launch(db, session, project, ssh)

    with pytest.raises(AgentSessionTurnConflictError):
        await _launch(db, session, project, ssh)


@pytest.mark.asyncio
async def test_launch_raises_limit_error_once_max_turns_reached(db):
    session, project = _open_active_session(db, max_turns=1)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.exit_code = 0
    await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    with pytest.raises(AgentSessionTurnLimitError):
        await _launch(db, session, project, ssh)


@pytest.mark.asyncio
async def test_launch_raises_not_active_when_session_is_closed(db):
    session, project = _open_active_session(db)
    db.close_agent_session(session.id)
    ssh = FakeAgentSessionSSH()
    with pytest.raises(AgentSessionNotActiveError):
        await _launch(db, session, project, ssh)


@pytest.mark.asyncio
async def test_settle_branch_done_persists_assistant_message_and_cli_session_id(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.exit_code = 0
    ssh.final_message = "the fix is ready"
    ssh.cli_session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    assert status.status == "done"
    assert status.exit_code == 0
    assert status.assistant_message.content == "the fix is ready"
    refreshed = db.get_agent_session(session.id)
    assert refreshed.active_turn_no is None
    assert refreshed.turn_count == 1
    assert refreshed.cli_session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.mark.asyncio
async def test_settle_branch_nonzero_exit_persists_failed_turn_message(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.exit_code = 1

    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    assert status.status == "failed"
    assert status.exit_code == 1
    assert "1" in status.assistant_message.content
    refreshed = db.get_agent_session(session.id)
    assert refreshed.active_turn_no is None
    assert refreshed.turn_count == 1


@pytest.mark.asyncio
async def test_settle_branch_tmux_gone_without_exit_code_is_interrupted_not_silent(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.exit_code = None
    ssh.tmux_exists = False

    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    assert status.status == "interrupted"
    assert status.exit_code is None
    assert status.assistant_message is not None
    refreshed = db.get_agent_session(session.id)
    assert refreshed.active_turn_no is None
    assert refreshed.turn_count == 1  # an attempted turn still counts (D5)


@pytest.mark.asyncio
async def test_settle_still_running_when_tmux_alive_and_no_exit_code(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.exit_code = None
    ssh.tmux_exists = True

    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    assert status.status == "running"
    refreshed = db.get_agent_session(session.id)
    assert refreshed.active_turn_no == result.turn_no
    assert refreshed.turn_count == 0


@pytest.mark.asyncio
async def test_settle_local_timeout_fallback_marks_turn_interrupted(db):
    session, project = _open_active_session(db, turn_timeout_sec=1)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)

    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_sessions SET active_turn_started_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", session.id),
        )

    calls_before_converge = len(ssh.calls)
    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    assert status.status == "interrupted"
    assert "timed out" in status.detail
    # no SSH round trip was even attempted for a locally-detected timeout
    assert len(ssh.calls) == calls_before_converge
    refreshed = db.get_agent_session(session.id)
    assert refreshed.active_turn_no is None
    assert refreshed.turn_count == 1


@pytest.mark.asyncio
async def test_settle_unreachable_runner_leaves_session_untouched(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.raise_on_run = ConnectionError("no route to host")

    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    assert status.status == "unreachable"
    refreshed = db.get_agent_session(session.id)
    assert refreshed.status == "active"
    assert refreshed.active_turn_no == result.turn_no  # untouched, self-heals via timeout
    assert refreshed.turn_count == 0


@pytest.mark.asyncio
async def test_converge_reports_settled_for_an_already_settled_turn(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()
    result = await _launch(db, session, project, ssh)
    ssh.exit_code = 0
    await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )

    status = await converge_agent_session_turn(
        db,
        session=db.get_agent_session(session.id),
        project_name="proj1",
        turn_no=result.turn_no,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert status.status == "settled"


@pytest.mark.asyncio
async def test_converge_reports_not_running_for_a_turn_number_never_claimed(db):
    session, project = _open_active_session(db)
    ssh = FakeAgentSessionSSH()

    status = await converge_agent_session_turn(
        db,
        session=session,
        project_name="proj1",
        turn_no=1,
        workspace_rel="codex_workspaces",
        runner_server="runner-a",
        ssh_run=ssh.run,
    )
    assert status.status == "not_running"


# ---------------------------------------------------------------------------
# Route level (api_client, tests/conftest.py — mirrors
# tests/test_agent_sessions.py's convention)
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


def test_message_and_transcript_routes_404_when_flag_disabled(api_client):
    client, _main = api_client
    assert (
        client.post(
            "/agent-sessions/some-id/messages", json={"content": "hi"}
        ).status_code
        == 404
    )
    assert (
        client.get("/agent-sessions/some-id/transcript?turn=1").status_code == 404
    )


def _open_active_session_via_routes(client, main_module):
    """Same round trip as `tests/test_agent_sessions.py`'s
    `test_open_request_and_list_and_close_round_trip`, returning the new
    session id."""

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


def test_message_then_transcript_route_round_trip(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}

    session_id = _open_active_session_via_routes(client, main_module)

    ssh = FakeAgentSessionSSH()
    main_module.app_state.ssh_run = ssh.run
    main_module.app_state.ssh_write_file = ssh.write_file

    msg_resp = client.post(
        f"/agent-sessions/{session_id}/messages", json={"content": "hello claude"}
    )
    assert msg_resp.status_code == 200
    body = msg_resp.json()
    assert body["status"] == "launched"
    assert body["turn_no"] == 1

    ssh.exit_code = 0
    ssh.final_message = "done!"
    transcript_resp = client.get(
        f"/agent-sessions/{session_id}/transcript?turn=1&offset=0"
    )
    assert transcript_resp.status_code == 200
    transcript_body = transcript_resp.json()
    assert transcript_body["status"] == "done"
    assert transcript_body["message"]["content"] == "done!"

    # A second turn while none is running succeeds; a concurrent third
    # attempt on top of it is a 409.
    second_resp = client.post(
        f"/agent-sessions/{session_id}/messages", json={"content": "one more thing"}
    )
    assert second_resp.status_code == 200
    assert second_resp.json()["turn_no"] == 2

    third_resp = client.post(
        f"/agent-sessions/{session_id}/messages", json={"content": "and another"}
    )
    assert third_resp.status_code == 409


def test_message_route_rejects_oversized_content(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_routes(client, main_module)

    oversized = "x" * (AGENT_SESSION_MESSAGE_MAX_BYTES + 1)
    resp = client.post(
        f"/agent-sessions/{session_id}/messages", json={"content": oversized}
    )
    assert resp.status_code == 400


def test_message_route_unreachable_runner_returns_degraded_200_and_stays_active(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_routes(client, main_module)

    async def broken_run(server, command, timeout):
        raise ConnectionError("no route to host")

    main_module.app_state.ssh_run = broken_run

    async def broken_write(server, path, content):
        raise ConnectionError("no route to host")

    main_module.app_state.ssh_write_file = broken_write

    resp = client.post(
        f"/agent-sessions/{session_id}/messages", json={"content": "hello"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "unreachable"
    session = main_module.app_state.db.get_agent_session(session_id)
    assert session.status == "active"


EXPECTED_GOLDEN_TURN_SCRIPT = r"""set -u
TURN_DIR="codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1"
log() { echo "[agent_session_turn] $*"; }
export R_ERROR=""
fail() { export R_ERROR="$1"; log "FAIL: $1"; exit 1; }

main() {
  mkdir -p "$TURN_DIR" || { log "cannot create TURN_DIR"; exit 1; }
  EXTENDED_PATH="$HOME/.local/bin:$HOME/bin:$PATH"
  export PATH="$EXTENDED_PATH"
  command -v claude >/dev/null 2>&1 || fail 'claude CLI not installed; install and log in on the AgentSession Runner'
  R_CLI_VERSION="$(claude --version 2>/dev/null | head -1)"
  CLI_VERSION_NUM="$(printf '%s\n' "$R_CLI_VERSION" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  [ -n "$CLI_VERSION_NUM" ] || fail 'claude CLI version could not be parsed'
  awk -v v="$CLI_VERSION_NUM" 'BEGIN{split(v,a,".");n=a[1]*1000000+a[2]*1000+a[3]; if (n>=1000000 && n<3000000) exit 0; exit 1}' || fail 'claude CLI version outside the reviewed compatible range'
  claude auth status >/dev/null 2>&1 || fail 'claude is not logged in on this Runner'
  [ "$(id -u)" != "0" ] || fail 'refusing to run claude as root'
  git --version >/dev/null 2>&1 || fail 'git not found'
  [ -s codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/prompt.txt ] || fail 'prompt.txt missing (should have been SFTP-written before launch)'

  if [ -d codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/repo ]; then
    git -C codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/repo rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail 'existing session workspace is not a git worktree'
    R_CUR_BRANCH="$(git -C codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/repo rev-parse --abbrev-ref HEAD)"
    [ "$R_CUR_BRANCH" = ai-session-11111111-2222-3333-4444-555555555555 ] || fail 'existing session workspace is on an unexpected branch'
  else
    if [ -d codex_workspaces/mirrors/proj1.git ]; then
      git --git-dir=codex_workspaces/mirrors/proj1.git remote update --prune || fail 'mirror update failed'
    else
      git clone --mirror -- https://example.invalid/proj1.git codex_workspaces/mirrors/proj1.git || fail 'mirror clone failed'
    fi
    git --git-dir=codex_workspaces/mirrors/proj1.git cat-file -e aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa^{commit} || fail 'mirror does not contain the session base commit'
    git --git-dir=codex_workspaces/mirrors/proj1.git worktree add -b ai-session-11111111-2222-3333-4444-555555555555 -- codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/repo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa || fail 'session worktree creation failed'
  fi

  # confinement: cwd IS the workspace — Claude Code scopes file tools to
  # the working-directory tree, so running from $HOME would expose the
  # whole home (including ~/.ssh, ~/.claude) to Read/Edit. The subshell
  # cds into the worktree; prompt/transcript paths get an explicit
  # "$HOME"/ prefix because every path in this script is home-relative
  # (SFTP does not expand ~, see app/jobqueue.py module docstring).
  # Bash limited to the validated allowlist (no network-capable tool granted); no
  # platform/approval tool exists in this CLI's tool set at all (D4).
  [ -d codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/repo ] || fail 'workspace 目錄不存在'
  ( cd codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/repo && exec env -i HOME="$HOME" PATH="$EXTENDED_PATH" USER="$USER" LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb \
    timeout 600s claude -p \
    --add-dir . \
    --output-format stream-json --verbose \
    --permission-mode acceptEdits \
    --allowedTools 'Read Edit Write Grep Glob' \
    < "$HOME"/codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/prompt.txt > "$HOME"/codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/transcript.jsonl )
  R_CLAUDE_EXIT=$?

  python3 - codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/transcript.jsonl codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/final_message.txt codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/cli_session_id.txt <<'PYEOF'
import json, os, sys
transcript, final_message_file, cli_session_id_file = sys.argv[1:4]
result_text = None
cli_session_id = None
assistant_fragments = []
try:
    with open(transcript) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            sid = event.get('session_id')
            if isinstance(sid, str) and sid:
                cli_session_id = sid
            if event.get('type') == 'result' and isinstance(event.get('result'), str):
                result_text = event['result']
            if event.get('type') == 'assistant':
                message = event.get('message') or {}
                for block in message.get('content') or []:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        text = block.get('text')
                        if isinstance(text, str):
                            assistant_fragments.append(text)
except FileNotFoundError:
    pass
if result_text is None:
    result_text = '\n'.join(assistant_fragments)
with open(final_message_file, 'w') as fh:
    fh.write(result_text or '')
with open(cli_session_id_file, 'w') as fh:
    fh.write(cli_session_id or '')
PYEOF
  exit "$R_CLAUDE_EXIT"
}
main 2>&1 | tee codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/turn.log
EXIT_CODE="${PIPESTATUS[0]}"
echo "$EXIT_CODE" > codex_workspaces/agent_sessions/11111111-2222-3333-4444-555555555555/turns/1/exit_code
exit "$EXIT_CODE"
"""
