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
        if "command -v claude" in command:
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


def test_free_text_chat_records_assistant_usage(claude_client):
    """Packet D3: a successful runner-hosted turn records one
    `assistant_usage` row (`channel="runner_claude"`) with the server the
    turn actually ran on."""

    client, main_module = claude_client
    fake = FakeAssistantWsSSH(
        reply_json=(
            '{"type":"result","result":"哈囉","is_error":false,'
            '"usage":{"input_tokens":7,"output_tokens":3}}'
        )
    )
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        ws.receive_json()

    summary = main_module.app_state.db.get_assistant_usage_summary(7)
    assert summary["totals"] == {"turns": 1, "input_tokens": 7, "output_tokens": 3}
    breakdown = summary["breakdown"][0]
    assert breakdown["channel"] == "runner_claude"


def test_free_text_chat_uses_configured_assistant_model(claude_client):
    """Packet D2: `ASSISTANT_CLAUDE_MODEL` reaches the Runner shell script
    as `--model '<value>'`."""

    client, main_module = claude_client
    main_module.app_state.config.assistant_claude_model = "opus"
    fake = FakeAssistantWsSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        ws.receive_json()

    run_sh = next(v for k, v in fake.written.items() if k.endswith("run.sh"))
    assert "--model opus" in run_sh


@pytest.mark.parametrize(
    "fake_kwargs, expected_system_fragment",
    [
        (
            {"raise_on_turn_command": ConnectionError("no route to host")},
            # Packet D1: degraded messages now name the selected pool
            # member ("Runner server-a 連不上") -- assert the server-agnostic
            # suffix so this test doesn't hardcode the fixture's runner name.
            "連不上",
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
# Packet D1: pool-wide selection — a chat turn actually runs on the same
# server GET /api/v2/ai-providers/status would report as assistant_brain.server.
# ---------------------------------------------------------------------------


def test_pool_fails_closed_to_second_ready_server(claude_client, monkeypatch):
    client, main_module = claude_client
    from app.monitor import ServerState

    main_module.app_state.config.codex_runner_servers = ("server-a", "server-b")
    main_module.app_state.server_states["server-b"] = ServerState(
        name="server-b", online=True
    )

    class PerServerFakeSSH(FakeAssistantWsSSH):
        async def run(self, server, command, timeout):
            if "command -v claude" in command:
                self.calls.append(command)
                if server == "server-a":
                    return FakeCommandResult("claude-code 1.5.0\nCLAUDE_AUTH_NO\n")
                return FakeCommandResult("claude-code 1.5.0\nCLAUDE_AUTH_OK\n")
            return await super().run(server, command, timeout)

    fake = PerServerFakeSSH()
    _install(main_module, fake)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好，介紹一下自己"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"

    # INV-SSH-2/3 style check: the turn actually launched against server-b
    # (the pool's second, ready member), never a server outside the pool.
    assert any("assistant_chat" in c for c in fake.calls)


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


# ---------------------------------------------------------------------------
# DG-ASSISTANT-TOOLS v1: per-turn token + bridge tools on the runner-claude path
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_tools_client(tmp_path, monkeypatch):
    """`claude_client` plus ASSISTANT_TOOLS_V1_ENABLED, a dispatch base URL and
    AUTH_TOKEN (the WS user must be a real actor for a token to be issued)."""

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
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "server-a")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", "~/codex_workspaces")
    monkeypatch.setenv("CODEX_NETWORK_ACCESS", "false")
    monkeypatch.setenv("ASSISTANT_TOOLS_V1_ENABLED", "true")
    monkeypatch.setenv("ASSISTANT_TOOLS_DISPATCH_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("ASSISTANT_TOOLS_MAX_CALLS", "5")

    import app.main as main_module

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)

    with TestClient(main_module.app) as client:
        main_module.app_state.server_states["server-a"] = ServerState(name="server-a", online=True)
        yield client, main_module


class FakeAssistantToolsSSH(FakeAssistantWsSSH):
    def __init__(self, *, calls_log_text: str = "", tools_reason_text: str = "", **kwargs):
        super().__init__(**kwargs)
        self.calls_log_text = calls_log_text
        self.tools_reason_text = tools_reason_text

    async def run(self, server, command, timeout):
        if "tool_calls.jsonl" in command:
            self.calls.append(command)
            return FakeCommandResult(self.calls_log_text)
        if "tools_reason.txt" in command:
            self.calls.append(command)
            return FakeCommandResult(self.tools_reason_text)
        return await super().run(server, command, timeout)


def _chat_with_tools(client, fake, main_module, text="幫我看看伺服器"):
    _install(main_module, fake)
    frames = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "secret-token"})
        ws.send_json({"type": "chat", "text": text})
        while True:
            msg = ws.receive_json()
            frames.append(msg)
            if msg["type"] == "reply":
                break
    return frames


def test_tools_turn_issues_and_revokes_a_token_and_renders_tool_frames(claude_tools_client):
    client, main_module = claude_tools_client
    approval = client.post(
        "/dispatch",
        json={"command": "nvidia-smi", "source": "api"},
        headers={"X-Auth-Token": "secret-token"},
    ).json()
    approval_id = approval["id"]
    fake = FakeAssistantToolsSSH(
        calls_log_text=(
            '{"tool":"get_servers","status":"ok"}\n'
            f'{{"tool":"request_enqueue_job","status":"ok","approval_id":{approval_id}}}\n'
            '{"tool":"get_job","status":"error"}\n'
            '{"tool":"list_jobs","status":"rejected_cap"}\n'
        )
    )

    frames = _chat_with_tools(client, fake, main_module)
    assert [f["type"] for f in frames] == ["tool_note", "approval_card", "tool_note", "tool_note", "reply"]
    assert frames[0]["text"] == "查詢：get_servers"
    assert frames[1]["approval"]["id"] == approval_id and frames[1]["approval"]["kind"] == "enqueue"
    assert "auto_approved" not in frames[1]
    assert frames[2]["text"] == "工具 get_job 失敗"
    assert "上限" in frames[3]["text"]
    assert frames[4]["text"] == "哈囉，我是助手"

    # the four tool files were shipped over SFTP, the bearer only in `token`
    names = [path.rsplit("/", 1)[1] for path in fake.written]
    assert names[:4] == ["bridge.py", "tools.json", "token", "mcp.json"]
    bridge_path = next(p for p in fake.written if p.endswith("bridge.py"))
    assert "def load_stdio_bridge_config" in fake.written[bridge_path]
    token_path = next(p for p in fake.written if p.endswith("/token"))
    raw_token = fake.written[token_path].strip()
    assert raw_token.startswith("dat_")
    for path, content in fake.written.items():
        if not path.endswith("/token"):
            assert raw_token not in content
    assert not any(raw_token in c for c in fake.calls)
    run_sh = next(v for k, v in fake.written.items() if k.endswith("run.sh"))
    assert "--strict-mcp-config" in run_sh and "--allowedTools mcp__dispatch__" in run_sh
    tools_cfg = next(v for k, v in fake.written.items() if k.endswith("tools.json"))
    assert '"max_calls": 5' in tools_cfg

    # issued for the speaking (legacy) actor and revoked once the turn ended
    db = main_module.app_state.db
    rows = db._conn.execute(
        "SELECT actor_id, revoked_at, turn_ref FROM assistant_turn_tokens"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_id"] == "00000000-0000-0000-0000-000000000001"
    assert rows[0]["revoked_at"] is not None
    assert rows[0]["turn_ref"].endswith(":1")


def test_tools_turn_reports_unavailable_tools_and_keeps_cards_when_degraded(claude_tools_client):
    client, main_module = claude_tools_client
    fake = FakeAssistantToolsSSH(
        exit_code=124,
        tools_reason_text="TOOLS_UNAVAILABLE: python packages mcp/httpx missing on runner",
        calls_log_text='{"tool":"request_stop_job","status":"ok","approval_id":999}\n',
    )
    frames = _chat_with_tools(client, fake, main_module)
    assert [f["type"] for f in frames] == ["system", "tool_note", "system", "reply"]
    assert "平台工具不可用（python packages mcp/httpx missing on runner）" in frames[0]["text"]
    assert frames[1]["text"] == "已建立核准請求 #999（request_stop_job）"
    assert "回應逾時" in frames[2]["text"]
    rows = main_module.app_state.db._conn.execute(
        "SELECT revoked_at FROM assistant_turn_tokens"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["revoked_at"] is not None


def test_tools_turn_is_plain_when_flag_off(claude_client):
    client, main_module = claude_client
    fake = FakeAssistantToolsSSH()
    _install(main_module, fake)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"
    names = {path.rsplit("/", 1)[1] for path in fake.written}
    assert names == {"prompt.txt", "run.sh"}
    assert main_module.app_state.db._conn.execute(
        "SELECT COUNT(*) FROM assistant_turn_tokens"
    ).fetchone()[0] == 0
