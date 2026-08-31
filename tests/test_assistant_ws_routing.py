"""Phase 1b: the `/ws` chat assistant no longer runs runner-hosted `claude -p`
turns — Studio SDK sessions are the Claude surface. These tests pin the
surviving two-tier brain (local vLLM when configured, else the rule-based
fallback) and that the panel projection can never claim `runner_claude`."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.monitor import ServerState


@pytest.fixture
def ws_client(tmp_path, monkeypatch):
    servers_yaml_path = tmp_path / "servers.yaml"
    servers_yaml_path.write_text(
        "servers:\n  - name: server-a\n    host: 10.0.0.1\n    user: train\n    key: ~/.ssh/id_rsa\n    enabled: true\n"
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(servers_yaml_path))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    import app.main as main_module

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)
    with TestClient(main_module.app) as client:
        main_module.app_state.server_states["server-a"] = ServerState(name="server-a", online=True)
        yield client, main_module


def test_brain_mode_never_reports_runner_claude_even_with_a_ready_pool():
    import app.main as main_module

    ready_pool = [{"server": "server-a", "available": True, "authenticated": True, "version": "2.0.0"}]
    mode, server, reason = main_module._assistant_brain_mode(ready_pool, False)
    assert mode == "rule_based" and server is None and "已退役" in reason and "Studio" in reason
    mode, server, reason = main_module._assistant_brain_mode(ready_pool, True)
    assert mode == "vllm" and server is None and "已退役" in reason


def test_free_text_without_vllm_gets_the_rule_based_reply(ws_client):
    client, main_module = ws_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "嗨，你好"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "reply":
                break
    assert frames[-1]["type"] == "reply" and frames[-1]["text"]
    # no runner probe, no degradation note about a claude turn
    assert not any("claude" in str(frame).lower() for frame in frames)


def test_free_text_with_vllm_uses_the_agent_and_keeps_history(ws_client, monkeypatch):
    client, main_module = ws_client
    seen = {}

    async def fake_run_agent(text, **kwargs):
        seen["text"] = text
        seen["history"] = list(kwargs.get("history") or [])
        return [{"type": "reply", "text": "vLLM 回覆"}]

    monkeypatch.setattr(main_module, "run_agent", fake_run_agent)
    monkeypatch.setattr(main_module, "is_vllm_available", lambda config: True)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "第一句"})
        while ws.receive_json().get("type") != "reply":
            pass
        ws.send_json({"type": "chat", "text": "第二句"})
        while ws.receive_json().get("type") != "reply":
            pass
    assert seen["text"] == "第二句"
    assert {"role": "user", "content": "第一句"} in seen["history"]
    assert any(entry["role"] == "assistant" and "vLLM 回覆" in entry["content"] for entry in seen["history"])
