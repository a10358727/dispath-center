"""DG-ASSISTANT-CLAUDE-TURN v1 C1: WS `/ws` brain-priority routing.

Covers the three-tier deterministic priority (`_assistant_brain_mode()` /
`_claude_assistant_channel_ready()` in `app/main.py`): runner-hosted Claude
first (deterministic intents bypass the LLM entirely, only a genuine
free-text "chat" spends a bounded `claude -p` turn), then the byte-identical
existing vLLM branch, then the existing rule-based fallback. Every degraded
Claude-turn outcome (unreachable/not_logged_in/timeout/failed) must surface
an explicit `system` note and still answer with the rule-based fallback
reply — the turn is never silently swallowed.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.monitor import ServerState


@pytest.fixture
def claude_client(tmp_path, monkeypatch):
    """Mirrors `tests/test_coding_task.py`'s `codex_client` fixture: a clean
    `TestClient` with `CODEX_RUNNER_SERVER` pointed at a `servers.yaml` entry
    that exists and is enabled, monitor loop stubbed out so tests fully
    control `server_states`/`ssh_run`."""

    servers_yaml_path = tmp_path / "servers.yaml"
    servers_yaml_path.write_text(
        "servers:\n"
        "  - name: server-a\n"
        "    host: 10.0.0.1\n"
        "    user: train\n"
        "    key: ~/.ssh/id_rsa\n"
        "    enabled: true\n"
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(servers_yaml_path))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "server-a")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", "~/codex_workspaces")
    monkeypatch.setenv("CODEX_NETWORK_ACCESS", "false")

    import app.main as main_module

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)

    with TestClient(main_module.app) as client:
        client_module = main_module
        client_module.app_state.server_states["server-a"] = ServerState(
            name="server-a", online=True
        )
        yield client, client_module


class FakeCommandResult:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


class FakeAssistantWsSSH:
    """Answers both the claude probe (`command -v claude ...`) and every
    `app.assistant_turns` turn command against one in-memory fake, so a
    single WS integration test can drive the whole path (no real SSH)."""

    def __init__(
        self,
        *,
        probe_output: str = "claude-code 1.5.0\nCLAUDE_AUTH_OK\n",
        exit_code=0,
        reply_json: str = '{"type":"result","result":"哈囉，我是助手","is_error":false}',
        reason_text: str = "",
        tmux_exists: bool = True,
        raise_on_turn_command: BaseException | None = None,
    ):
        self.calls: list[str] = []
        self.written: dict[str, str] = {}
        self.probe_output = probe_output
        self.exit_code = exit_code
        self.reply_json = reply_json
        self.reason_text = reason_text
        self.tmux_exists = tmux_exists
        self.raise_on_turn_command = raise_on_turn_command

    async def run(self, server, command, timeout):
        self.calls.append(command)
        if command.startswith("command -v claude"):
            return FakeCommandResult(self.probe_output)
        if self.raise_on_turn_command is not None and "assistant_chat" in command:
            raise self.raise_on_turn_command
        if command.startswith("cat ") and "exit_code" in command:
            return FakeCommandResult(
                str(self.exit_code) if self.exit_code is not None else ""
            )
        if "tmux has-session" in command:
            return FakeCommandResult("EXISTS" if self.tmux_exists else "GONE")
        if "reply.json" in command:
            return FakeCommandResult(self.reply_json)
        if "reason.txt" in command:
            return FakeCommandResult(self.reason_text)
        return FakeCommandResult("")

    async def write_file(self, server, path, content):
        self.written[path] = content


def _install(main_module, fake: FakeAssistantWsSSH) -> None:
    main_module.app_state.ssh_run = fake.run
    main_module.app_state.ssh_write_file = fake.write_file


# ---------------------------------------------------------------------------
# Deterministic intents bypass the claude turn entirely
# ---------------------------------------------------------------------------


def test_status_intent_bypasses_claude_turn(claude_client):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "狀態"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"

    assert not any("assistant_chat" in c for c in fake.calls)


def test_jobs_intent_bypasses_claude_turn(claude_client):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "任務"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"

    assert not any("assistant_chat" in c for c in fake.calls)


def test_enqueue_intent_bypasses_claude_turn_and_creates_approval(claude_client):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "跑 echo hi"})
        msg = ws.receive_json()
        assert msg["type"] == "approval_card"
        assert msg["approval"]["payload"]["command"] == "echo hi"
        assert msg["approval"]["payload"]["source"] == "vllm"

    assert not any("assistant_chat" in c for c in fake.calls)


# ---------------------------------------------------------------------------
# Free-text chat -> one bounded claude turn
# ---------------------------------------------------------------------------


def test_free_text_chat_uses_claude_turn_ok(claude_client):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好，介紹一下自己"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"
        assert msg["text"] == "哈囉，我是助手"

    assert any("assistant_chat" in c for c in fake.calls)
    # INV-SSH-2/3: the user's message reaches the prompt file, never the
    # shell command strings this fake recorded.
    assert not any("你好，介紹一下自己" in c for c in fake.calls)
    prompt_path = next(k for k in fake.written if k.endswith(".txt") and "prompt" in k)
    assert "你好，介紹一下自己" in fake.written[prompt_path]


@pytest.mark.parametrize(
    "fake_kwargs, expected_system_fragment",
    [
        (
            {"raise_on_turn_command": ConnectionError("no route to host")},
            "Runner 連不上",
        ),
        (
            {"exit_code": 1, "reason_text": "NOT_LOGGED_IN: claude is not logged in"},
            "未登入 Claude",
        ),
        ({"exit_code": 124}, "回應逾時"),
        (
            {"exit_code": 1, "reason_text": "NOT_INSTALLED: claude CLI not installed"},
            "執行失敗",
        ),
    ],
)
def test_free_text_chat_degraded_claude_turn_falls_back_to_rule_based_reply(
    claude_client, fake_kwargs, expected_system_fragment
):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH(**fake_kwargs)
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好，介紹一下自己"})
        system_msg = ws.receive_json()
        reply_msg = ws.receive_json()

    assert system_msg["type"] == "system"
    assert expected_system_fragment in system_msg["text"]
    assert reply_msg["type"] == "reply"
    assert "規則式理解" in reply_msg["text"]


def test_claude_turn_conversation_history_round_trips(claude_client):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        ws.receive_json()
        ws.send_json({"type": "chat", "text": "還在嗎"})
        ws.receive_json()

    prompt_paths = [k for k in fake.written if k.endswith(".txt") and "prompt" in k]
    assert len(prompt_paths) == 2
    second_prompt = sorted(prompt_paths)[-1]
    assert "你好" in fake.written[second_prompt]
    assert "還在嗎" in fake.written[second_prompt]


# ---------------------------------------------------------------------------
# Not ready (offline / not authenticated) falls through to vLLM-or-rule-based
# exactly like before this packet (no CODEX_RUNNER_SERVER regression path is
# already covered by tests/test_ws.py's existing suite).
# ---------------------------------------------------------------------------


def test_claude_not_authenticated_falls_back_to_existing_rule_based_path(
    claude_client,
):
    client, main_module = claude_client
    fake = FakeAssistantWsSSH(probe_output="claude-code 1.5.0\nCLAUDE_AUTH_NO\n")
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"

    assert not any("assistant_chat" in c for c in fake.calls)
