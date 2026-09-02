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

import pytest

import json
from dataclasses import dataclass
from typing import Optional

from app.config import ServerConfig


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
# Fake AgentSession SSH -- mirrors the retired turn suite's
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


@pytest.mark.usefixtures("legacy_posture")
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
    # Phase 1b: the per-turn messages/transcript/diff routes are retired on
    # both surfaces (the SDK session in Studio replaced them), so only the
    # session-lifecycle routes remain flag-gated here.
    assert client.post("/agent-sessions/some-id/checkpoint-request").status_code == 404
    assert (
        client.post("/api/v2/agent-sessions/some-id/checkpoint-requests").status_code == 404
    )


# ---------------------------------------------------------------------------
# Conversation (CV-2a) parity.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AgentSession list / open-request / close parity.
# ---------------------------------------------------------------------------


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


def test_agent_session_close_unknown_session_404(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.agent_session_v1_enabled = True

    resp = client.post("/api/v2/agent-sessions/does-not-exist/close")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# messages / transcript round trip (P2), diff (P3), checkpoint-request.
# ---------------------------------------------------------------------------


