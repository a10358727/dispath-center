"""WS /ws：聊天端點認證與訊息流（實作指令 5.7）。

現有 `auth_middleware` 只攔 HTTP，WebSocket 不經過它，因此認證邏輯另外
在 `_ws_authenticate()` 處理：AUTH_TOKEN 有設時，連線後第一則訊息必須是
`{"type":"auth","token":"..."}`，否則 close(code=1008)。用
`fastapi.testclient.TestClient` 的 `websocket_connect()`，不需要真的起
一個 server。
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.audit import read_audit
from app.authentication import LEGACY_ADMIN_ACTOR_ID
from app.identity import ActorType, generate_service_token, generate_session_token


@pytest.fixture
def ws_auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


# ---------------------------------------------------------------------------
# 認證：AUTH_TOKEN 未設定 -> 不驗證
# ---------------------------------------------------------------------------


def test_ws_no_auth_token_configured_skips_auth(api_client):
    client, _main = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "狀態"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"


def test_ws_shadow_denial_preserves_frames_and_appends_after_disconnect(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "shadow-ws.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "shadow-ws-audit.jsonl"))
    monkeypatch.setenv("AUTHORIZATION_MODE", "shadow")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    audit_path = str(tmp_path / "shadow-ws-audit.jsonl")
    with TestClient(main_module.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "chat", "text": "狀態"})
            message = ws.receive_json()
            assert message["type"] == "reply"
            # Connection-level evidence is deferred, so in-connection behavior
            # is identical to mode off.
            assert read_audit(main_module.app_state.config.audit_path) == []

    records = read_audit(audit_path)
    assert len(records) == 1
    assert records[0]["action"] == "authorization_shadow_denied"
    assert records[0]["params"]["route"] == "WEBSOCKET /ws"
    assert records[0]["params"]["action"] == "identity.self.view"
    assert records[0]["params"]["principal_kind"] == "anonymous"


# ---------------------------------------------------------------------------
# 認證：AUTH_TOKEN 有設定
# ---------------------------------------------------------------------------


def test_ws_missing_auth_first_message_closes_connection(ws_auth_client):
    client, _main = ws_auth_client
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            # 第一則訊息不是 auth，應該被直接關閉連線
            ws.send_json({"type": "chat", "text": "狀態"})
            ws.receive_json()
    assert exc_info.value.code == 1008


def test_ws_wrong_token_closes_connection(ws_auth_client):
    client, _main = ws_auth_client
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "wrong-token"})
            ws.receive_json()
    assert exc_info.value.code == 1008


def test_ws_correct_token_allows_chat(ws_auth_client):
    client, _main = ws_auth_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "secret-token"})
        ws.send_json({"type": "chat", "text": "任務"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"
        assert "任務" in msg["text"]


def test_ws_legacy_enqueue_persists_requester_and_safe_actor(ws_auth_client):
    client, main_module = ws_auth_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "secret-token"})
        ws.send_json({"type": "chat", "text": "跑 echo ws-attributed"})
        message = ws.receive_json()

    assert message["type"] == "approval_card"
    approval = main_module.app_state.db.get_approval(message["approval"]["id"])
    assert approval.requester_actor_id == LEGACY_ADMIN_ACTOR_ID
    record = read_audit(main_module.app_state.config.audit_path)[-1]
    assert record["actor"] == {
        "id": LEGACY_ADMIN_ACTOR_ID,
        "kind": "legacy",
        "authentication": "legacy_shared_token",
    }


def test_ws_valid_session_does_not_consume_first_chat_and_propagates_context(
    ws_auth_client, monkeypatch
):
    client, main_module = ws_auth_client
    actor = main_module.app_state.db.insert_actor(
        actor_type=ActorType.HUMAN,
        display_name="WS session user",
    )
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)
    captured = {}

    async def fake_handle_chat_text(text, **kwargs):
        captured["text"] = text
        captured["context"] = kwargs["request_context"]
        return [{"type": "reply", "text": "session-ok"}]

    monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)

    with client.websocket_connect("/ws") as ws:
        # No auth envelope: this is the first message and must reach chat.
        ws.send_json({"type": "chat", "text": "first-chat"})
        assert ws.receive_json() == {"type": "reply", "text": "session-ok"}

    assert captured["text"] == "first-chat"
    assert captured["context"].actor_id == actor.id
    assert captured["context"].authentication_method == "session"


def test_ws_invalid_session_still_uses_exact_legacy_first_message_protocol(
    ws_auth_client
):
    client, _main = ws_auth_client
    client.cookies.set("dispatch_session", "malformed-session")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "secret-token"})
        ws.send_json({"type": "chat", "text": "任務"})
        assert ws.receive_json()["type"] == "reply"


@pytest.mark.parametrize("token_prefix", ["", "Bearer "])
def test_ws_enabled_service_token_uses_existing_first_message_shape(
    tmp_path, monkeypatch, token_prefix
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "legacy-fallback")
    monkeypatch.setenv("SERVICE_TOKEN_AUTH_ENABLED", "true")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        actor = main_module.app_state.db.insert_actor(
            actor_type=ActorType.SERVICE,
            display_name="WS service",
        )
        main_module.app_state.db.insert_service_account(
            actor_id=actor.id,
            name="ws-service",
        )
        issued = generate_service_token()
        main_module.app_state.db.insert_service_account_token(
            token_id=issued.id,
            service_account_actor_id=actor.id,
            secret_hash=issued.secret_hash,
            scopes=["identity.self.view"],
            expires_at="2099-01-01T00:00:00+00:00",
        )
        captured = {}

        async def fake_handle_chat_text(text, **kwargs):
            captured["context"] = kwargs["request_context"]
            return [{"type": "reply", "text": "service-ok"}]

        monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
        with client.websocket_connect("/ws") as ws:
            ws.send_json(
                {"type": "auth", "token": token_prefix + issued.raw_token}
            )
            ws.send_json({"type": "chat", "text": "status"})
            assert ws.receive_json() == {"type": "reply", "text": "service-ok"}

    assert captured["context"].actor_id == actor.id
    assert captured["context"].authentication_method == "service_token"


@pytest.mark.parametrize("token_prefix", ["", "Bearer "])
def test_ws_first_frame_service_token_revocation_stops_next_chat(
    tmp_path, monkeypatch, token_prefix
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "legacy-fallback")
    monkeypatch.setenv("SERVICE_TOKEN_AUTH_ENABLED", "true")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        actor = main_module.app_state.db.insert_actor(
            actor_type=ActorType.SERVICE,
            display_name="WS revocable service",
        )
        main_module.app_state.db.insert_service_account(
            actor_id=actor.id,
            name="ws-revocable-service",
        )
        issued = generate_service_token()
        main_module.app_state.db.insert_service_account_token(
            token_id=issued.id,
            service_account_actor_id=actor.id,
            secret_hash=issued.secret_hash,
            scopes=["identity.self.view"],
            expires_at="2099-01-01T00:00:00+00:00",
        )
        handled = []

        async def fake_handle_chat_text(text, **kwargs):
            handled.append(text)
            return [{"type": "reply", "text": "service-ok"}]

        monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws") as ws:
                ws.send_json(
                    {"type": "auth", "token": token_prefix + issued.raw_token}
                )
                ws.send_json({"type": "chat", "text": "before-revocation"})
                assert ws.receive_json() == {"type": "reply", "text": "service-ok"}
                assert main_module.app_state.db.revoke_service_account_token(
                    issued.id
                )
                ws.send_json({"type": "chat", "text": "after-revocation"})
                ws.receive_json()

    assert exc_info.value.code == 1008
    assert handled == ["before-revocation"]


def test_ws_authorization_header_service_revocation_never_downgrades_to_anonymous(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("SERVICE_TOKEN_AUTH_ENABLED", "true")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        actor = main_module.app_state.db.insert_actor(
            actor_type=ActorType.SERVICE,
            display_name="WS header service",
        )
        main_module.app_state.db.insert_service_account(
            actor_id=actor.id,
            name="ws-header-service",
        )
        issued = generate_service_token()
        main_module.app_state.db.insert_service_account_token(
            token_id=issued.id,
            service_account_actor_id=actor.id,
            secret_hash=issued.secret_hash,
            scopes=["identity.self.view"],
            expires_at="2099-01-01T00:00:00+00:00",
        )
        handled = []

        async def fake_handle_chat_text(text, **kwargs):
            handled.append(text)
            return [{"type": "reply", "text": "service-header-ok"}]

        monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws",
                headers={"Authorization": f"Bearer {issued.raw_token}"},
            ) as ws:
                ws.send_json({"type": "chat", "text": "before-revocation"})
                assert ws.receive_json() == {
                    "type": "reply",
                    "text": "service-header-ok",
                }
                assert main_module.app_state.db.revoke_service_account_token(
                    issued.id
                )
                ws.send_json({"type": "chat", "text": "after-revocation"})
                ws.receive_json()

    assert exc_info.value.code == 1008
    assert handled == ["before-revocation"]


def test_ws_legacy_first_frame_token_rotation_stops_next_chat(
    ws_auth_client, monkeypatch
):
    client, main_module = ws_auth_client
    handled = []

    async def fake_handle_chat_text(text, **kwargs):
        handled.append(text)
        return [{"type": "reply", "text": "legacy-ok"}]

    monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "secret-token"})
            ws.send_json({"type": "chat", "text": "before-rotation"})
            assert ws.receive_json() == {"type": "reply", "text": "legacy-ok"}
            main_module.app_state.config.auth_token = "rotated-token"
            ws.send_json({"type": "chat", "text": "after-rotation"})
            ws.receive_json()

    assert exc_info.value.code == 1008
    assert handled == ["before-rotation"]


# ---------------------------------------------------------------------------
# 訊息流：status / jobs / enqueue（走核准，不直接入列）/ 危險指令 / 一般聊天
# ---------------------------------------------------------------------------


def test_ws_status_reply(api_client):
    client, _main = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "現在狀態如何？"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"


def test_ws_enqueue_creates_approval_card_not_a_job(api_client):
    client, main_module = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "跑 echo hi"})
        msg = ws.receive_json()
        assert msg["type"] == "approval_card"
        assert msg["approval"]["payload"]["command"] == "echo hi"

    assert main_module.app_state.db.list_jobs() == []


def test_ws_dangerous_command_rejected(api_client):
    client, main_module = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "跑 rm -rf /tmp/x"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"
        assert "拒絕" in msg["text"]

    assert main_module.app_state.db.list_approvals() == []


def test_ws_non_chat_message_type_is_ignored(api_client):
    """收到非 chat 類型的訊息（目前只定義了 chat）應該被忽略，不影響連線；
    緊接著送一則合法的 chat 訊息應該正常得到回覆。"""
    client, _main = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping"})
        ws.send_json({"type": "chat", "text": "狀態"})
        msg = ws.receive_json()
        assert msg["type"] == "reply"


def test_ws_invalid_json_gets_error_reply_and_connection_stays_open(api_client):
    client, _main = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not json at all")
        msg = ws.receive_json()
        assert msg["type"] == "reply"
        assert "格式" in msg["text"]

        # 連線還活著，繼續送合法訊息應該正常回覆
        ws.send_json({"type": "chat", "text": "任務"})
        msg2 = ws.receive_json()
        assert msg2["type"] == "reply"


# ---------------------------------------------------------------------------
# 階段 7 追加：對話記憶（範圍＝這條 WS 連線）——mock app.main.run_agent，
# 斷言第二輪呼叫收到的 history 裡有第一輪的 user/assistant 內容。
# ---------------------------------------------------------------------------


def test_ws_vllm_history_threads_across_turns(api_client, monkeypatch):
    client, main_module = api_client
    monkeypatch.setattr(main_module, "is_vllm_available", lambda config: True)

    calls: list[dict] = []

    async def fake_run_agent(text, **kwargs):
        # `history` 是呼叫端（main.py）持有的同一個 list 物件參照，呼叫後
        # 還會繼續被 append——這裡要存「呼叫當下」的快照，不是參照本身，
        # 否則事後檢查會看到整個連線跑完之後的最終狀態（同
        # tests/test_agent_runtime.py 的 FakeVllmClient 用 deepcopy 的理由）。
        calls.append({"text": text, "history": copy.deepcopy(kwargs.get("history"))})
        return [{"type": "reply", "text": f"回覆：{text}"}]

    monkeypatch.setattr(main_module, "run_agent", fake_run_agent)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "第一句"})
        msg1 = ws.receive_json()
        assert msg1 == {"type": "reply", "text": "回覆：第一句"}

        ws.send_json({"type": "chat", "text": "第二句"})
        msg2 = ws.receive_json()
        assert msg2 == {"type": "reply", "text": "回覆：第二句"}

    assert calls[0]["history"] == []
    assert calls[1]["history"] == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "回覆：第一句"},
    ]


def test_ws_vllm_history_includes_approval_card_summary(api_client, monkeypatch):
    client, main_module = api_client
    monkeypatch.setattr(main_module, "is_vllm_available", lambda config: True)

    calls: list = []
    state = {"turn": 0}

    async def fake_run_agent(text, **kwargs):
        calls.append(copy.deepcopy(kwargs.get("history")))
        state["turn"] += 1
        if state["turn"] == 1:
            return [
                {"type": "tool_note", "text": "建立派工核准請求"},
                {
                    "type": "approval_card",
                    "approval": {"id": 7, "kind": "enqueue", "status": "pending"},
                },
            ]
        return [{"type": "reply", "text": "第二輪回覆"}]

    monkeypatch.setattr(main_module, "run_agent", fake_run_agent)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "跑 echo hi"})
        note = ws.receive_json()
        assert note["type"] == "tool_note"
        card = ws.receive_json()
        assert card["type"] == "approval_card"

        ws.send_json({"type": "chat", "text": "後續問題"})
        reply = ws.receive_json()
        assert reply == {"type": "reply", "text": "第二輪回覆"}

    second_call_history = calls[1]
    assistant_turn = next(t for t in second_call_history if t["role"] == "assistant")
    assert "#7" in assistant_turn["content"]
    assert "enqueue" in assistant_turn["content"]
    user_turn = next(t for t in second_call_history if t["role"] == "user")
    assert user_turn["content"] == "跑 echo hi"


def test_ws_history_not_maintained_when_vllm_unavailable(api_client):
    """走規則式路徑（vLLM 不可用）時不維護 history：既有規則式聊天行為
    不受影響（這裡只驗證不會炸掉，兩輪都正常回覆）。"""
    client, _main = api_client
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "狀態"})
        msg1 = ws.receive_json()
        assert msg1["type"] == "reply"

        ws.send_json({"type": "chat", "text": "狀態"})
        msg2 = ws.receive_json()
        assert msg2["type"] == "reply"
