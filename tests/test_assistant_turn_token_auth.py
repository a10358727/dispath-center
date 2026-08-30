"""DG-ASSISTANT-TOOLS v1 T-2/T-3 (packet P1a): HTTP/WS behaviour of per-turn tokens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.assistant_tokens import issue_assistant_turn_token
from app.audit import read_audit
from app.authentication import LEGACY_ADMIN_ACTOR_ID


def _client(tmp_path, monkeypatch, *, enabled: bool):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("ASSISTANT_TOOLS_V1_ENABLED", "true" if enabled else "false")
    import app.main as main_module

    return TestClient(main_module.app), main_module


def _issue(main_module):
    state = main_module.app_state
    return issue_assistant_turn_token(
        state.db,
        actor_id=LEGACY_ADMIN_ACTOR_ID,
        turn_ref="sess:1",
        ttl_sec=150,
        audit_path=state.config.audit_path,
    )


def test_turn_token_is_accepted_only_on_mcp_tool_routes(tmp_path, monkeypatch):
    client, main_module = _client(tmp_path, monkeypatch, enabled=True)
    with client:
        issued = _issue(main_module)
        headers = {"X-Auth-Token": issued.raw_token}

        assert client.get("/servers", headers=headers).status_code == 200
        assert client.get("/jobs", headers=headers).status_code == 200

        denied = client.get("/auth/me", headers=headers)
        assert denied.status_code == 403
        assert denied.json() == {"detail": "assistant turn token not allowed for this route"}
        assert client.post("/approve/1", headers=headers).status_code == 403
        assert client.post("/reject/1", headers=headers).status_code == 403

        audit = read_audit(main_module.app_state.config.audit_path)
        denials = [row for row in audit if row["action"] == "assistant_turn_token_denied"]
        assert len(denials) == 3
        assert denials[0]["params"]["token_id"] == issued.token_id
        assert denials[0]["params"]["path"] == "/auth/me"
        assert issued.raw_token not in open(main_module.app_state.config.audit_path, encoding="utf-8").read()

        # The shared token keeps its full historical reach.
        assert client.get("/auth/me", headers={"X-Auth-Token": "secret-token"}).status_code == 200


def test_turn_token_is_rejected_when_the_flag_is_off(tmp_path, monkeypatch):
    client, main_module = _client(tmp_path, monkeypatch, enabled=False)
    with client:
        issued = _issue(main_module)
        response = client.get("/servers", headers={"X-Auth-Token": issued.raw_token})
        assert response.status_code == 401


def test_turn_token_cannot_open_the_chat_websocket(tmp_path, monkeypatch):
    client, main_module = _client(tmp_path, monkeypatch, enabled=True)
    with client:
        issued = _issue(main_module)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "auth", "token": issued.raw_token})
                ws.receive_json()
