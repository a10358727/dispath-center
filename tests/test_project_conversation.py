"""DG-CONVERSATION-V1 CV-2a：每 project 一個 main AI conversation。

`app.conversations.run_conversation_turn()` 是 domain 層直接測（假 Anthropic
client，見 `FakeClient`/`FakeMessages`/`FakeResponse`/`FakeTextBlock`，型狀
沿用 `tests/test_chat.py` 的假 client 慣例，只是 block type 改成
`agent_runtime`／`_extract_text()` 認得的 `"text"`，不是 `chat.py` 專用的
`tool_use`）；`GET`/`POST /projects/{name}/conversation*` 走
`api_client`（`tests/conftest.py`），旗標與 LLM client 一律用
`main_module.app_state.config.*`/`main_module.app_state.llm_client` 事後指定
（同 `tests/test_run_profiles.py` 的旗標切換慣例）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from app.config import AppConfig
from app.conversations import CONVERSATION_HISTORY_MESSAGES, run_conversation_turn
from app.db import Database

ROOT = Path(__file__).parents[1]


def make_config(**overrides) -> AppConfig:
    base = dict(servers=[])
    base.update(overrides)
    return AppConfig(**base)


# ---------------------------------------------------------------------------
# 假 Anthropic client：`agent_chat_completion()`（app/llm.py）唯讀
# `client.messages.create(...)` 回傳物件的 `.content`，每個 block 要有
# `.type == "text"` 與 `.text`（`app.llm._extract_text()`）。
# ---------------------------------------------------------------------------


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeMessages:
    """回應用一個固定佇列消耗——每次 `create()` 呼叫吐出佇列的下一個項目，
    對應 tool loop 每一步一次呼叫模型。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages.create() 被呼叫次數超過預期回應數")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def final_response(reply: str) -> FakeResponse:
    return FakeResponse([FakeTextBlock(json.dumps({"action": "final", "reply": reply}))])


def tool_response(tool: str, args: dict) -> FakeResponse:
    return FakeResponse(
        [FakeTextBlock(json.dumps({"action": "tool", "tool": tool, "args": args}))]
    )


# ---------------------------------------------------------------------------
# Domain 層：run_conversation_turn()
# ---------------------------------------------------------------------------


def test_turn_persists_user_and_assistant_messages_and_returns_ok(db, audit_path):
    db.insert_project("proj1", "https://example.invalid/p.git")
    client = FakeClient([final_response("目前沒有任何 pending 任務。")])

    result = asyncio.run(
        run_conversation_turn(
            db,
            project_name="proj1",
            text="有沒有正在跑的任務？",
            config=make_config(),
            server_states={},
            audit_path=audit_path,
            llm_client=client,
        )
    )

    assert result.status == "ok"
    assert result.user_message.role == "user"
    assert result.user_message.content == "有沒有正在跑的任務？"
    assert result.assistant_message.role == "assistant"
    assert result.assistant_message.content == "目前沒有任何 pending 任務。"
    assert result.assistant_message.refs is None

    conversation = db.get_or_create_project_conversation("proj1")
    assert conversation.id == result.conversation.id
    stored = db.list_conversation_messages(conversation.id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert [m.content for m in stored] == [
        "有沒有正在跑的任務？",
        "目前沒有任何 pending 任務。",
    ]


def test_new_database_instance_reproduces_full_history(db, audit_path, tmp_path):
    db_path = tmp_path / "conv.db"
    database = Database(str(db_path))
    try:
        database.insert_project("proj1", "https://example.invalid/p.git")
        client = FakeClient([final_response("第一輪回覆")])
        asyncio.run(
            run_conversation_turn(
                database,
                project_name="proj1",
                text="第一輪訊息",
                config=make_config(),
                server_states={},
                audit_path=audit_path,
                llm_client=client,
            )
        )
    finally:
        database.close()

    reopened = Database(str(db_path))
    try:
        conversation = reopened.get_or_create_project_conversation("proj1")
        messages = reopened.list_conversation_messages(conversation.id)
        assert [m.content for m in messages] == ["第一輪訊息", "第一輪回覆"]
    finally:
        reopened.close()


def test_get_or_create_project_conversation_is_one_per_project(db):
    db.insert_project("proj1", "https://example.invalid/p.git")
    first = db.get_or_create_project_conversation("proj1")
    second = db.get_or_create_project_conversation("proj1")
    assert first.id == second.id
    assert first.project_id == second.project_id


def test_turn_captures_approval_ref_from_request_enqueue_job_tool(db, audit_path):
    db.insert_project("proj1", "https://example.invalid/p.git")
    client = FakeClient(
        [
            tool_response(
                "request_enqueue_job",
                {"command": "python train.py", "project": "proj1"},
            ),
            final_response("已建立一筆待核准的派工請求。"),
        ]
    )

    result = asyncio.run(
        run_conversation_turn(
            db,
            project_name="proj1",
            text="幫我跑 python train.py",
            config=make_config(),
            server_states={},
            audit_path=audit_path,
            llm_client=client,
        )
    )

    assert result.status == "ok"
    approvals = db.list_approvals()
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert result.assistant_message.refs == {"approval_id": approvals[0].id}
    # 動作本身仍走既有 request_* 建卡，不直接入列（鐵律第 2 條）。
    assert db.list_jobs() == []
    # tool_note + approval_card + reply 三則 runtime 訊息都保留給呼叫端。
    assert [m["type"] for m in result.runtime_messages] == [
        "tool_note",
        "approval_card",
        "reply",
    ]


def test_turn_without_any_tool_call_has_no_refs(db, audit_path):
    db.insert_project("proj1", "https://example.invalid/p.git")
    client = FakeClient([final_response("哈囉，我可以幫你查狀態或建立派工請求。")])

    result = asyncio.run(
        run_conversation_turn(
            db,
            project_name="proj1",
            text="嗨",
            config=make_config(),
            server_states={},
            audit_path=audit_path,
            llm_client=client,
        )
    )

    assert result.assistant_message.refs is None


def test_turn_llm_unavailable_is_degraded_and_persists_nothing(db, audit_path):
    db.insert_project("proj1", "https://example.invalid/p.git")

    result = asyncio.run(
        run_conversation_turn(
            db,
            project_name="proj1",
            text="有沒有正在跑的任務？",
            config=make_config(),  # anthropic_api_key 未設定
            server_states={},
            audit_path=audit_path,
            llm_client=None,
        )
    )

    assert result.status == "llm_unavailable"
    assert result.conversation is None
    assert result.user_message is None
    assert result.assistant_message is None
    # 沒有任何 conversation 被建立（不因為降級而腦補一個空對話）。
    assert db._conn.execute("SELECT COUNT(*) FROM ai_conversations").fetchone()[0] == 0


def test_turn_history_is_bounded_by_conversation_history_messages(db, audit_path):
    assert CONVERSATION_HISTORY_MESSAGES > 0
    db.insert_project("proj1", "https://example.invalid/p.git")
    conversation = db.get_or_create_project_conversation("proj1")
    for i in range(CONVERSATION_HISTORY_MESSAGES + 4):
        db.append_conversation_message(
            conversation.id, role="user" if i % 2 == 0 else "assistant", content=f"m{i}"
        )

    captured = {}

    async def fake_run_agent(text, *, history, **kwargs):
        captured["history_len"] = len(history)
        return [{"type": "reply", "text": "ok"}]

    import app.conversations as conversations_module

    original = conversations_module.run_agent
    conversations_module.run_agent = fake_run_agent
    try:
        asyncio.run(
            run_conversation_turn(
                db,
                project_name="proj1",
                text="another message",
                config=make_config(),
                server_states={},
                audit_path=audit_path,
                llm_client=FakeClient([]),
            )
        )
    finally:
        conversations_module.run_agent = original

    assert captured["history_len"] <= CONVERSATION_HISTORY_MESSAGES


# ---------------------------------------------------------------------------
# HTTP 層：GET/POST /projects/{name}/conversation*
# ---------------------------------------------------------------------------


def test_conversation_routes_404_when_flag_disabled(api_client):
    client, _main = api_client

    get_resp = client.get("/projects/proj1/conversation")
    assert get_resp.status_code == 404
    assert get_resp.json() == {"detail": "Project conversation is disabled"}

    post_resp = client.post(
        "/projects/proj1/conversation/messages", json={"content": "hi"}
    )
    assert post_resp.status_code == 404
    assert post_resp.json() == {"detail": "Project conversation is disabled"}


def test_get_conversation_endpoint_unknown_project_404(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True

    resp = client.get("/projects/does-not-exist/conversation")
    assert resp.status_code == 404


def test_get_conversation_endpoint_returns_empty_history_for_new_conversation(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    resp = client.get("/projects/proj1/conversation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation"]["project_id"]
    assert body["messages"] == []


def test_post_message_full_turn_persists_and_is_visible_on_get(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")
    main_module.app_state.llm_client = FakeClient(
        [final_response("目前沒有任何 pending 任務。")]
    )

    post_resp = client.post(
        "/projects/proj1/conversation/messages",
        json={"content": "有沒有正在跑的任務？"},
    )
    assert post_resp.status_code == 200
    body = post_resp.json()
    assert body["status"] == "ok"
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == "目前沒有任何 pending 任務。"
    assert body["user_message"]["content"] == "有沒有正在跑的任務？"

    get_resp = client.get("/projects/proj1/conversation")
    assert get_resp.status_code == 200
    messages = get_resp.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_post_message_llm_unavailable_returns_typed_degraded_response(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")
    main_module.app_state.llm_client = None
    main_module.app_state.config.anthropic_api_key = None

    resp = client.post(
        "/projects/proj1/conversation/messages", json={"content": "hi"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "llm_unavailable"
    assert "ANTHROPIC_API_KEY" in body["detail"]

    get_resp = client.get("/projects/proj1/conversation")
    assert get_resp.json()["messages"] == []


def test_post_message_rejects_empty_content(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    resp = client.post(
        "/projects/proj1/conversation/messages", json={"content": "   "}
    )
    assert resp.status_code == 400


def test_post_message_rejects_content_over_64kib(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    oversized = "x" * (Database.AI_CONVERSATION_MESSAGE_MAX_BYTES + 1)
    resp = client.post(
        "/projects/proj1/conversation/messages", json={"content": oversized}
    )
    assert resp.status_code == 400


def test_post_message_unknown_project_404(api_client):
    client, main_module = api_client
    main_module.app_state.config.project_conversation_v1_enabled = True

    resp = client.post(
        "/projects/does-not-exist/conversation/messages", json={"content": "hi"}
    )
    assert resp.status_code == 404


def test_conversation_routes_require_auth_token_when_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("PROJECT_CONVERSATION_V1_ENABLED", "true")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/projects/proj1/conversation").status_code == 401
        assert (
            client.post(
                "/projects/proj1/conversation/messages", json={"content": "hi"}
            ).status_code
            == 401
        )

        headers = {"X-Auth-Token": "secret-token"}
        main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")
        resp = client.get("/projects/proj1/conversation", headers=headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Frontend 靜態斷言（tests/test_project_workspace_ui.py 風格 -> DG-UI-
# UNIFICATION v1 U6b/U8 起 tests/test_identity_workspace_v2.py 風格）：AI
# Engineer section 存在、textContent-only 渲染、旗標關閉時整段隱藏。
#
# U8: legacy `static/index.html`/`ui.js` are deleted. The equivalent surface
# is the `legacy-ai-conversation-*` block ported into the v2 Workspace's
# legacy project-detail「AI Engineer」sub-tab (`data-legacy-detail-panel=
# "ai-engineer"`, U6b -- see `static/workspace.html`/`static/workspace.js`
# module comments crediting this exact CV-2a port).
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_ai_engineer_section_markup_is_present_inside_ai_engineering_pane():
    html = _read(ROOT / "static" / "workspace.html")
    start = html.index('data-legacy-detail-panel="ai-engineer"')
    end = html.index("</div>", html.rindex('id="legacy-ai-conversation-status"', start))
    pane = html[start:end]

    assert 'id="legacy-ai-conversation-section"' in pane
    assert "AI 工程助理" in pane
    assert 'id="legacy-ai-conversation-messages"' in pane
    assert 'id="legacy-ai-conversation-input"' in pane
    assert 'id="legacy-ai-conversation-send-btn"' in pane
    assert 'id="legacy-ai-conversation-form"' in pane
    # Hidden by default until the GET probe confirms the flag is on (CV-6).
    section_tag = re.search(
        r'<section\b[^>]*\bid="legacy-ai-conversation-section"[^>]*>', pane
    )
    assert section_tag is not None
    assert "hidden" in section_tag.group(0)


def test_ai_engineer_panel_functions_exist_and_are_exported():
    js = _read(ROOT / "static" / "workspace.js")
    for name in (
        "resetAIConversationPanel",
        "loadAIConversationPanel",
        "submitAIConversationMessage",
        "aiConversationRenderMessages",
    ):
        assert re.search(rf"\bfunction\s+{name}\s*\(", js), f"missing {name}"

    render_start = js.index("function aiConversationRenderMessages")
    render_end = js.index("\n  }\n", render_start)
    render_body = js[render_start:render_end]
    # Every message field that can carry server/LLM content must be rendered
    # via textContent/`node()`, never innerHTML (untrusted content boundary).
    assert "node(\"p\", message.content)" in render_body
    assert ".innerHTML" not in render_body


def test_ai_engineer_wired_into_open_project_detail_reset_and_load():
    js = _read(ROOT / "static" / "workspace.js")
    assert "resetAIConversationPanel();" in js
    assert "loadAIConversationPanel(projectName);" in js
