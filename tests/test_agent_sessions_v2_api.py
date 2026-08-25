"""HTTP-surface tests for the `/api/v2/legacy-projects/{name}/conversation*` /
`/api/v2/legacy-projects/{name}/agent-session*` / `/api/v2/agent-sessions/
{session_id}/*` wrappers (DG-UI-UNIFICATION v1, U6b).

Thin wrappers around the exact legacy `/projects/{name}/conversation*`
(DG-CONVERSATION-V1 CV-2a) and `/projects/{name}/agent-sessions*` /
`/agent-sessions/{session_id}/*` (DG-AGENT-SESSION-V1 P1-P4 / DG-AGENT-
SESSION-CHECKPOINT) surfaces in `dispatch_center/api/routers/
projects_legacy_v2.py`. Parity is asserted against the legacy endpoints
(modeled on `tests/test_projects_legacy_v2_api.py`/`tests/test_engineering_v2_api.py`);
flag-off 404 is asserted on BOTH surfaces (legacy and v2), matching every
other U6b acceptance criterion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from app.config import ServerConfig
from app.db import Database


COMMIT = "a" * 40


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


# ---------------------------------------------------------------------------
# Fake Anthropic client -- mirrors `tests/test_project_conversation.py`'s
# `FakeClient`/`FakeMessages`/`FakeResponse`/`FakeTextBlock`.
# ---------------------------------------------------------------------------


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def final_response(reply: str) -> FakeResponse:
    return FakeResponse([FakeTextBlock(json.dumps({"action": "final", "reply": reply}))])


# ---------------------------------------------------------------------------
# Fake AgentSession SSH -- mirrors `tests/test_agent_session_turns.py`'s
# `FakeAgentSessionSSH`/`FakeCommandResult`.
# ---------------------------------------------------------------------------


@dataclass
class FakeCommandResult:
    stdout: str = ""


class FakeAgentSessionSSH:
    def __init__(self):
        self.calls: list[str] = []
        self.exit_code: Optional[int] = None
        self.tmux_exists = True
        self.final_message = ""
        self.cli_session_id = ""

    async def run(self, server, command, timeout):
        self.calls.append(command)
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
        pass


class FakeAgentSessionDiffSSH:
    def __init__(self):
        self.diff_stdout = ""
        self.status_stdout = ""

    async def run(self, server, command, timeout):
        if "diff" in command:
            return FakeCommandResult(stdout=self.diff_stdout)
        return FakeCommandResult(stdout=self.status_stdout)


def _server_config(name="runner-a"):
    return ServerConfig(
        name=name,
        host="10.0.0.9",
        user="train",
        key="~/.ssh/id_rsa",
        port=22,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Flag gating: both surfaces 404 when the backing flag is off.
# ---------------------------------------------------------------------------


def test_conversation_flag_off_404_on_both_surfaces(api_client):
    client, main_module = api_client
    _enable_v2(main_module)

    assert client.get("/projects/proj1/conversation").status_code == 404
    assert client.get("/api/v2/legacy-projects/proj1/conversation").status_code == 404
    assert (
        client.post(
            "/projects/proj1/conversation/messages", json={"content": "hi"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v2/legacy-projects/proj1/conversation/messages", json={"content": "hi"}
        ).status_code
        == 404
    )


def test_agent_session_flag_off_404_on_both_surfaces(api_client):
    client, main_module = api_client
    _enable_v2(main_module)

    assert client.get("/projects/proj1/agent-sessions").status_code == 404
    assert client.get("/api/v2/legacy-projects/proj1/agent-sessions").status_code == 404
    assert (
        client.post(
            "/projects/proj1/agent-sessions/open-request",
            json={"base_version_id": "whatever"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v2/legacy-projects/proj1/agent-session-open-requests",
            json={"base_version_id": "whatever"},
        ).status_code
        == 404
    )
    assert client.post("/agent-sessions/some-id/close").status_code == 404
    assert client.post("/api/v2/agent-sessions/some-id/close").status_code == 404
    assert (
        client.post("/agent-sessions/some-id/messages", json={"content": "hi"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v2/agent-sessions/some-id/messages", json={"content": "hi"}
        ).status_code
        == 404
    )
    assert client.get("/agent-sessions/some-id/transcript?turn=1").status_code == 404
    assert client.get("/api/v2/agent-sessions/some-id/transcript?turn=1").status_code == 404
    assert client.get("/agent-sessions/some-id/diff").status_code == 404
    assert client.get("/api/v2/agent-sessions/some-id/diff").status_code == 404
    assert client.post("/agent-sessions/some-id/checkpoint-request").status_code == 404
    assert (
        client.post("/api/v2/agent-sessions/some-id/checkpoint-requests").status_code == 404
    )


# ---------------------------------------------------------------------------
# Conversation (CV-2a) parity.
# ---------------------------------------------------------------------------


def test_conversation_get_is_byte_identical_to_legacy(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    legacy = client.get("/projects/proj1/conversation").json()
    v2 = client.get("/api/v2/legacy-projects/proj1/conversation").json()
    assert v2 == legacy
    assert v2["messages"] == []


def test_conversation_post_message_ok_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")
    main_module.app_state.llm_client = FakeClient([final_response("目前沒有任何 pending 任務。")])

    resp = client.post(
        "/api/v2/legacy-projects/proj1/conversation/messages",
        json={"content": "有沒有正在跑的任務？"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["message"]["content"] == "目前沒有任何 pending 任務。"

    get_resp = client.get("/api/v2/legacy-projects/proj1/conversation")
    messages = get_resp.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_conversation_post_message_llm_unavailable_degraded(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")
    main_module.app_state.llm_client = None
    main_module.app_state.config.anthropic_api_key = None

    resp = client.post(
        "/api/v2/legacy-projects/proj1/conversation/messages", json={"content": "hi"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "llm_unavailable"
    assert "ANTHROPIC_API_KEY" in body["detail"]


def test_conversation_post_message_rejects_empty_and_oversized(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    empty_resp = client.post(
        "/api/v2/legacy-projects/proj1/conversation/messages", json={"content": "   "}
    )
    assert empty_resp.status_code == 400

    oversized = "x" * (Database.AI_CONVERSATION_MESSAGE_MAX_BYTES + 1)
    oversized_resp = client.post(
        "/api/v2/legacy-projects/proj1/conversation/messages",
        json={"content": oversized},
    )
    assert oversized_resp.status_code == 400


def test_conversation_get_unknown_project_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.project_conversation_v1_enabled = True
    resp = client.get("/api/v2/legacy-projects/does-not-exist/conversation")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AgentSession list / open-request / close parity.
# ---------------------------------------------------------------------------


def test_agent_sessions_list_parity(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    legacy = client.get("/projects/proj1/agent-sessions").json()
    v2 = client.get("/api/v2/legacy-projects/proj1/agent-sessions").json()
    assert v2 == legacy
    assert v2 == {"current": None, "recent": []}


def test_agent_session_open_request_creates_pending_agent_session_open_approval(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/p.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")

    resp = client.post(
        "/api/v2/legacy-projects/proj1/agent-session-open-requests",
        json={"base_version_id": version.id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "agent_session_open"
    assert body["status"] == "pending"


def test_agent_session_open_request_unknown_project_400(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"

    resp = client.post(
        "/api/v2/legacy-projects/does-not-exist/agent-session-open-requests",
        json={"base_version_id": "whatever"},
    )
    assert resp.status_code == 400


def _open_active_session_via_v2_open_request(client, main_module):
    """Open via the v2 request wrapper, approve via the legacy generic
    engine (same `POST /approve/{id}` -- U1's shared decision engine)."""

    db = main_module.app_state.db
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", COMMIT, git_ref="main")
    open_resp = client.post(
        "/api/v2/legacy-projects/proj1/agent-session-open-requests",
        json={"base_version_id": version.id},
    )
    approval_id = open_resp.json()["id"]
    approve_resp = client.post(f"/approve/{approval_id}")
    return approve_resp.json()["agent_session"]["id"]


def test_agent_session_close_parity_and_list_reflects_it(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    session_id = _open_active_session_via_v2_open_request(client, main_module)

    v2_list_before = client.get("/api/v2/legacy-projects/proj1/agent-sessions").json()
    assert v2_list_before["current"]["id"] == session_id
    assert v2_list_before["current"]["status"] == "active"

    close_resp = client.post(f"/api/v2/agent-sessions/{session_id}/close")
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "closed"

    v2_list_after = client.get("/api/v2/legacy-projects/proj1/agent-sessions").json()
    assert v2_list_after["current"] is None
    assert v2_list_after["recent"][0]["id"] == session_id
    assert v2_list_after["recent"][0]["status"] == "closed"


def test_agent_session_close_unknown_session_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True

    resp = client.post("/api/v2/agent-sessions/does-not-exist/close")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# messages / transcript round trip (P2), diff (P3), checkpoint-request.
# ---------------------------------------------------------------------------


def test_message_then_transcript_round_trip_via_v2(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_v2_open_request(client, main_module)

    ssh = FakeAgentSessionSSH()
    main_module.app_state.ssh_run = ssh.run
    main_module.app_state.ssh_write_file = ssh.write_file

    msg_resp = client.post(
        f"/api/v2/agent-sessions/{session_id}/messages", json={"content": "hello claude"}
    )
    assert msg_resp.status_code == 200
    body = msg_resp.json()
    assert body["status"] == "launched"
    assert body["turn_no"] == 1

    ssh.exit_code = 0
    ssh.final_message = "done!"
    transcript_resp = client.get(
        f"/api/v2/agent-sessions/{session_id}/transcript?turn=1&offset=0"
    )
    assert transcript_resp.status_code == 200
    transcript_body = transcript_resp.json()
    assert transcript_body["status"] == "done"
    assert transcript_body["message"]["content"] == "done!"


def test_message_route_rejects_oversized_content(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_v2_open_request(client, main_module)

    oversized = "x" * 65537
    resp = client.post(
        f"/api/v2/agent-sessions/{session_id}/messages", json={"content": oversized}
    )
    assert resp.status_code == 400


def test_message_route_unreachable_runner_returns_degraded_200_and_stays_active(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_v2_open_request(client, main_module)

    async def broken_run(server, command, timeout):
        raise ConnectionError("no route to host")

    async def broken_write(server, path, content):
        raise ConnectionError("no route to host")

    main_module.app_state.ssh_run = broken_run
    main_module.app_state.ssh_write_file = broken_write

    resp = client.post(
        f"/api/v2/agent-sessions/{session_id}/messages", json={"content": "hello"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "unreachable"
    session = main_module.app_state.db.get_agent_session(session_id)
    assert session.status == "active"


def test_diff_route_parity_happy_path(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_v2_open_request(client, main_module)

    ssh = FakeAgentSessionDiffSSH()
    ssh.diff_stdout = "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n--- a/foo.py\n+x\n"
    ssh.status_stdout = "?? untracked.py\n"
    main_module.app_state.ssh_run = ssh.run

    resp = client.get(f"/api/v2/agent-sessions/{session_id}/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["status"] == "available"
    assert body["patch"] == ssh.diff_stdout
    assert body["untracked_files"] == ["untracked.py"]
    assert body["max_chars"] == 65536


def test_diff_route_unreachable_degrades_200(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_v2_open_request(client, main_module)

    async def broken_run(server, command, timeout):
        raise ConnectionError("no route to host")

    main_module.app_state.ssh_run = broken_run

    resp = client.get(f"/api/v2/agent-sessions/{session_id}/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["status"] == "unreachable"


def test_checkpoint_request_creates_pending_agent_session_checkpoint_approval(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True
    main_module.app_state.config.codex_runner_server = "runner-a"
    main_module.app_state.server_configs = {"runner-a": _server_config()}
    session_id = _open_active_session_via_v2_open_request(client, main_module)
    # A real turn must complete before a checkpoint is requestable.
    main_module.app_state.db.increment_agent_session_turn_count(session_id)

    resp = client.post(f"/api/v2/agent-sessions/{session_id}/checkpoint-requests")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "agent_session_checkpoint"
    assert body["status"] == "pending"
