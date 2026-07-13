"""POST /agent/chat、GET /agent/tools、POST /agent/cmd、WS /ws 切換
（PLAN.md H 節）。

`api_client`（tests/conftest.py）明確清掉 VLLM_BASE_URL/VLLM_MODEL，天然
測「vLLM 未設定」那一路；這裡另外提供 `vllm_client`/`vllm_auth_client`
fixture，設定假的 VLLM_BASE_URL/VLLM_MODEL 讓 `is_vllm_available()` 為
True，但一律 monkeypatch 掉 `app.main.run_agent`，絕不真的連線任何 vLLM
server。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent_tools import TOOLS


@pytest.fixture
def vllm_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("VLLM_MODEL", "qwen3-coder-30b")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


@pytest.fixture
def vllm_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("VLLM_MODEL", "qwen3-coder-30b")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


# ---------------------------------------------------------------------------
# GET /agent/tools：工具白名單清單，跟 vLLM 有沒有設定無關
# ---------------------------------------------------------------------------


def test_agent_tools_endpoint_matches_tools_registry(api_client):
    client, _main = api_client
    resp = client.get("/agent/tools")
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body}
    assert names == set(TOOLS.keys())
    for entry in body:
        assert "description" in entry
        assert "args" in entry


def test_agent_tools_endpoint_has_no_approve_or_shell_tool(api_client):
    client, _main = api_client
    names = {t["name"] for t in client.get("/agent/tools").json()}
    assert "approve" not in names
    assert "reject" not in names
    assert "shell" not in names


# ---------------------------------------------------------------------------
# POST /agent/chat：vLLM 未設定 → 503；有設定 → mock runtime
# ---------------------------------------------------------------------------


def test_agent_chat_503_when_vllm_not_configured(api_client):
    client, _main = api_client
    resp = client.post("/agent/chat", json={"text": "狀態"})
    assert resp.status_code == 503
    assert "VLLM_BASE_URL" in resp.json()["detail"]


def test_agent_chat_calls_run_agent_and_returns_messages(vllm_client, monkeypatch):
    client, main_module = vllm_client
    captured = {}

    async def fake_run_agent(text, **kwargs):
        captured["text"] = text
        captured["kwargs_keys"] = set(kwargs.keys())
        captured["context"] = kwargs["request_context"]
        return [{"type": "reply", "text": f"回覆：{text}"}]

    monkeypatch.setattr(main_module, "run_agent", fake_run_agent)

    resp = client.post("/agent/chat", json={"text": "現在狀態如何？"})
    assert resp.status_code == 200
    assert resp.json() == {"messages": [{"type": "reply", "text": "回覆：現在狀態如何？"}]}
    assert captured["text"] == "現在狀態如何？"
    assert {"db", "server_states", "config", "audit_path", "http_client"} <= captured["kwargs_keys"]
    assert captured["kwargs_keys"] >= {"request_context"}
    assert captured["context"].actor is None
    assert captured["context"].authentication_method == "anonymous"


def test_agent_chat_propagates_legacy_request_context(vllm_auth_client, monkeypatch):
    client, main_module = vllm_auth_client
    captured = {}

    async def fake_run_agent(text, **kwargs):
        captured["context"] = kwargs["request_context"]
        return [{"type": "reply", "text": "ok"}]

    monkeypatch.setattr(main_module, "run_agent", fake_run_agent)
    resp = client.post(
        "/agent/chat",
        json={"text": "hi"},
        headers={"X-Auth-Token": "secret-token"},
    )

    assert resp.status_code == 200
    assert captured["context"].authentication_method == "legacy_shared_token"
    assert captured["context"].actor_type.value == "legacy"


# ---------------------------------------------------------------------------
# POST /agent/cmd：enum 白名單（7 個合法值），非法值/自由文字一律 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd", ["status", "servers", "jobs", "approvals", "events", "gpu", "vllm"]
)
def test_agent_cmd_whitelist_values_return_200(api_client, cmd):
    client, _main = api_client
    resp = client.post("/agent/cmd", json={"cmd": cmd})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cmd"] == cmd
    assert "result" in body


def test_agent_cmd_rejects_illegal_value(api_client):
    client, _main = api_client
    resp = client.post("/agent/cmd", json={"cmd": "docker"})
    assert resp.status_code == 400


def test_agent_cmd_rejects_free_text(api_client):
    client, _main = api_client
    resp = client.post("/agent/cmd", json={"cmd": "rm -rf / 順便查一下狀態"})
    assert resp.status_code == 400


def test_agent_cmd_does_not_go_through_llm(api_client, monkeypatch):
    """/agent/cmd 直接呼叫工具 handler，不經過 run_agent／chat_completion。"""
    client, main_module = api_client

    async def boom(*args, **kwargs):
        raise AssertionError("/agent/cmd 不應該呼叫 run_agent")

    monkeypatch.setattr(main_module, "run_agent", boom)

    resp = client.post("/agent/cmd", json={"cmd": "status"})
    assert resp.status_code == 200


def test_agent_cmd_propagates_request_context(vllm_auth_client, monkeypatch):
    client, main_module = vllm_auth_client
    captured = {}
    original_dispatch = main_module.dispatch_tool

    async def capture_dispatch(name, args, ctx):
        captured["context"] = ctx.request_context
        return await original_dispatch(name, args, ctx)

    monkeypatch.setattr(main_module, "dispatch_tool", capture_dispatch)
    resp = client.post(
        "/agent/cmd",
        json={"cmd": "status"},
        headers={"X-Auth-Token": "secret-token"},
    )

    assert resp.status_code == 200
    assert captured["context"].authentication_method == "legacy_shared_token"


def test_agent_cmd_vllm_value_reports_not_configured(api_client):
    client, _main = api_client
    resp = client.post("/agent/cmd", json={"cmd": "vllm"})
    assert resp.status_code == 200
    assert resp.json()["result"]["ok"] is False


# ---------------------------------------------------------------------------
# auth middleware 自動涵蓋 /agent/*
# ---------------------------------------------------------------------------


def test_agent_endpoints_require_auth_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/agent/tools").status_code == 401
        assert client.post("/agent/chat", json={"text": "hi"}).status_code == 401
        assert client.post("/agent/cmd", json={"cmd": "status"}).status_code == 401

        headers = {"X-Auth-Token": "secret-token"}
        assert client.get("/agent/tools", headers=headers).status_code == 200
        assert client.post("/agent/cmd", json={"cmd": "status"}, headers=headers).status_code == 200
        # /agent/chat 沒設定 vLLM，帶對 token 之後應該是 503（不是 401）
        resp = client.post("/agent/chat", json={"text": "hi"}, headers=headers)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# WS /ws：vLLM 可用時改走 agent runtime；不可用時沿用既有規則式/anthropic
# ---------------------------------------------------------------------------


def test_ws_uses_agent_runtime_when_vllm_available(vllm_client, monkeypatch):
    client, main_module = vllm_client

    async def fake_run_agent(text, **kwargs):
        return [{"type": "reply", "text": f"agent-runtime:{text}"}]

    monkeypatch.setattr(main_module, "run_agent", fake_run_agent)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "狀態"})
        msg = ws.receive_json()
        assert msg == {"type": "reply", "text": "agent-runtime:狀態"}


def test_ws_falls_back_to_handle_chat_text_when_vllm_not_available(api_client):
    """api_client fixture 沒有設定 VLLM_BASE_URL——這是既有 289 條測試的
    假設本身，這裡再明確驗證一次：走的是規則式後備，不是 agent runtime。"""
    client, _main = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "狀態"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"
        assert "伺服器" in msg["text"] or "沒有設定任何伺服器" in msg["text"]
