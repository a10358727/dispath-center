"""app/chat.py：聊天 intent 處理（實作指令 5.7／PLAN.md F 節）。

不開真的 WS 連線，直接呼叫 `handle_chat_text()`（吃字串、回傳一串 dict）。
LLM 走假 client（見 tests/test_llm.py 的 FakeClient/FakeResponse/FakeBlock）。
"""

from __future__ import annotations

import asyncio

import app.chat as chat_module
from app.audit import read_audit
from app.chat import build_jobs_reply, build_status_reply, handle_chat_text
from app.config import AppConfig
from app.identity import Actor, ActorType, RequestContext
from app.monitor import ServerState


def make_config(**overrides) -> AppConfig:
    base = dict(servers=[])
    base.update(overrides)
    return AppConfig(**base)


class FakeBlock:
    def __init__(self, type_, **kwargs):
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeMessages:
    def __init__(self, responder):
        self._responder = responder

    async def create(self, **kwargs):
        result = self._responder(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responder):
        self.messages = FakeMessages(responder)


def tool_use_response(**input_overrides):
    data = {
        "intent": "chat",
        "command": None,
        "project": None,
        "pin_server": None,
        "require_tag": None,
        "reply": None,
    }
    data.update(input_overrides)
    return FakeResponse([FakeBlock("tool_use", name="report_intent", input=data)])


# ---------------------------------------------------------------------------
# 規則式：無 key 時「狀態」「任務」「跑 <指令>」三種要能動
# ---------------------------------------------------------------------------


def test_no_llm_status_keyword(db, audit_path):
    result = asyncio.run(
        handle_chat_text(
            "現在狀態如何？", db=db, server_states={}, config=make_config(), audit_path=audit_path
        )
    )
    assert len(result) == 1
    assert result[0]["type"] == "reply"
    assert "伺服器" in result[0]["text"] or "沒有設定任何伺服器" in result[0]["text"]


def test_no_llm_jobs_keyword(db, audit_path):
    result = asyncio.run(
        handle_chat_text(
            "任務跑得怎樣", db=db, server_states={}, config=make_config(), audit_path=audit_path
        )
    )
    assert result[0]["type"] == "reply"
    assert "任務" in result[0]["text"]


def test_no_llm_run_command_creates_approval_card(db, audit_path):
    result = asyncio.run(
        handle_chat_text(
            "跑 echo hi", db=db, server_states={}, config=make_config(), audit_path=audit_path
        )
    )
    assert len(result) == 1
    assert result[0]["type"] == "approval_card"
    approval = result[0]["approval"]
    assert approval["kind"] == "enqueue"
    assert approval["status"] == "pending"
    assert approval["payload"]["command"] == "echo hi"
    # 一律走核准，聊天絕不直接入列（鐵律第 2 條）
    assert db.list_jobs() == []


def test_chat_propagates_request_context_to_approval_boundary(
    db, audit_path, monkeypatch
):
    request_context = RequestContext(
        actor=Actor(
            id="11111111-1111-1111-1111-111111111111",
            actor_type=ActorType.HUMAN,
            display_name="Ada",
        ),
        authentication_method="session",
    )
    captured = {}

    async def fake_handle(intent_data, database, path, **kwargs):
        captured["request_context"] = kwargs.get("request_context")
        return {"type": "reply", "text": "captured"}

    monkeypatch.setattr(chat_module, "_handle_enqueue_intent", fake_handle)
    result = asyncio.run(
        handle_chat_text(
            "跑 echo hi",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            request_context=request_context,
        )
    )

    assert result == [{"type": "reply", "text": "captured"}]
    assert captured["request_context"] is request_context


def test_chat_enqueue_persists_requester_and_safe_audit_actor(db, audit_path):
    request_context = RequestContext(
        actor=Actor(
            id="22222222-2222-2222-2222-222222222222",
            actor_type=ActorType.HUMAN,
            display_name="Private display name",
            email="private@example.invalid",
        ),
        authentication_method="session",
    )

    result = asyncio.run(
        handle_chat_text(
            "跑 echo attributed",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            request_context=request_context,
        )
    )

    approval = db.get_approval(result[0]["approval"]["id"])
    assert approval.requester_actor_id == request_context.actor_id
    record = read_audit(audit_path)[-1]
    assert record["action"] == "approval_requested"
    assert record["actor"] == {
        "id": request_context.actor_id,
        "kind": "human",
        "authentication": "session",
    }
    assert "private@example.invalid" not in str(record)


def test_no_llm_dangerous_command_rejected_not_approval(db, audit_path):
    result = asyncio.run(
        handle_chat_text(
            "跑 rm -rf /tmp/x", db=db, server_states={}, config=make_config(), audit_path=audit_path
        )
    )
    assert result[0]["type"] == "reply"
    assert "拒絕" in result[0]["text"]
    assert db.list_approvals() == []


def test_no_llm_other_text_gives_help_reply(db, audit_path):
    result = asyncio.run(
        handle_chat_text(
            "你好嗎", db=db, server_states={}, config=make_config(), audit_path=audit_path
        )
    )
    assert result[0]["type"] == "reply"
    assert "狀態" in result[0]["text"] and "跑" in result[0]["text"]
    # 沒設定 LLM 從一開始就是規則式，不需要附加「LLM 暫不可用」提示
    assert not any(m["type"] == "system" for m in result)


# ---------------------------------------------------------------------------
# 有 LLM（假 client）：intent 分類後走確定性處理
# ---------------------------------------------------------------------------


def test_llm_status_intent_uses_deterministic_summary(db, audit_path):
    client = FakeClient(lambda kwargs: tool_use_response(intent="status"))
    server_states = {"server-a": ServerState(name="server-a", online=True, load1=0.5)}
    result = asyncio.run(
        handle_chat_text(
            "狀態怎樣",
            db=db,
            server_states=server_states,
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "reply"
    assert "server-a" in result[0]["text"]
    assert result[0]["text"] == build_status_reply(server_states)


def test_llm_jobs_intent_uses_deterministic_summary(db, audit_path):
    client = FakeClient(lambda kwargs: tool_use_response(intent="jobs"))
    result = asyncio.run(
        handle_chat_text(
            "任務清單",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["text"] == build_jobs_reply(db)


def test_jobs_summary_hides_engineering_executor_command(db):
    secret = "synthetic-chat-secret-123456789"
    private_path = "/home/runner/private/task-42"
    db.insert_job(
        command=f"cd {private_path} && AUTHORIZATION='Bearer {secret}' codex exec",
        type="coding",
        engineering_task_id="8da8c173-f0f5-4e0b-b67b-3aad07155182",
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )

    reply = build_jobs_reply(db)

    assert "Run Codex agent in an isolated worktree" in reply
    assert secret not in reply
    assert private_path not in reply


def test_llm_enqueue_intent_creates_approval_card(db, audit_path):
    client = FakeClient(
        lambda kwargs: tool_use_response(intent="enqueue", command="python train.py", project=None)
    )
    result = asyncio.run(
        handle_chat_text(
            "幫我跑 python train.py",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "approval_card"
    assert result[0]["approval"]["payload"]["command"] == "python train.py"
    assert db.list_jobs() == []  # 一律走核准


def test_llm_enqueue_dangerous_command_rejected(db, audit_path):
    client = FakeClient(
        lambda kwargs: tool_use_response(intent="enqueue", command="rm -rf /tmp/x")
    )
    result = asyncio.run(
        handle_chat_text(
            "刪掉 /tmp/x",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "reply"
    assert "拒絕" in result[0]["text"]
    assert db.list_approvals() == []


def test_llm_chat_intent_returns_llm_reply(db, audit_path):
    client = FakeClient(
        lambda kwargs: tool_use_response(intent="chat", reply="你好，我可以幫你派工或查狀態。")
    )
    result = asyncio.run(
        handle_chat_text(
            "嗨",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "reply"
    assert result[0]["text"] == "你好，我可以幫你派工或查狀態。"


# ---------------------------------------------------------------------------
# LLM 呼叫失敗 → 降級走規則式，附加提示訊息
# ---------------------------------------------------------------------------


def test_llm_call_failure_falls_back_with_notice(db, audit_path):
    client = FakeClient(lambda kwargs: RuntimeError("simulated failure"))
    result = asyncio.run(
        handle_chat_text(
            "現在狀態如何？",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "system"
    assert "LLM 暫不可用" in result[0]["text"]
    assert result[1]["type"] == "reply"


def test_llm_call_failure_falls_back_to_help_reply_for_unmatched_text(db, audit_path):
    client = FakeClient(lambda kwargs: RuntimeError("simulated failure"))
    result = asyncio.run(
        handle_chat_text(
            "隨便說點什麼",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "system"
    assert result[1]["type"] == "reply"
    assert "狀態" in result[1]["text"]


# ---------------------------------------------------------------------------
# enqueue：指令為空時不應該直接呼叫 request_enqueue_approval
# ---------------------------------------------------------------------------


def test_llm_enqueue_without_command_asks_for_clarification(db, audit_path):
    client = FakeClient(lambda kwargs: tool_use_response(intent="enqueue", command=None))
    result = asyncio.run(
        handle_chat_text(
            "幫我派工",
            db=db,
            server_states={},
            config=make_config(),
            audit_path=audit_path,
            llm_client=client,
        )
    )
    assert result[0]["type"] == "reply"
    assert "指令" in result[0]["text"]
    assert db.list_approvals() == []


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.1/K.3）：source="vllm" 標記＋自動核准規則諮詢
# ---------------------------------------------------------------------------


def test_enqueue_intent_records_source_vllm(db, audit_path):
    result = asyncio.run(
        handle_chat_text(
            "跑 echo hi", db=db, server_states={}, config=make_config(), audit_path=audit_path
        )
    )
    approval = result[0]["approval"]
    assert approval["payload"]["source"] == "vllm"
    assert "auto_approved" not in result[0]


def test_enqueue_intent_no_rules_file_stays_pending(db, audit_path, tmp_path):
    config = make_config(auto_approve_rules_path=str(tmp_path / "auto_approve.yaml"))
    result = asyncio.run(
        handle_chat_text("跑 echo hi", db=db, server_states={}, config=config, audit_path=audit_path)
    )
    assert result[0]["type"] == "approval_card"
    assert "auto_approved" not in result[0]
    assert db.list_jobs() == []


def test_enqueue_intent_matching_rule_auto_approves_and_enqueues(db, audit_path, tmp_path):
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: vllm\n    command_regex: '^echo '\n", encoding="utf-8")
    config = make_config(auto_approve_rules_path=str(rules_path))

    result = asyncio.run(
        handle_chat_text("跑 echo hi", db=db, server_states={}, config=config, audit_path=audit_path)
    )
    assert result[0]["type"] == "approval_card"
    assert result[0]["auto_approved"] is True
    assert result[0]["approval"]["status"] == "approved"

    jobs = db.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].command == "echo hi"


def test_enqueue_intent_non_matching_rule_stays_pending(db, audit_path, tmp_path):
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: web\n", encoding="utf-8")
    config = make_config(auto_approve_rules_path=str(rules_path))

    result = asyncio.run(
        handle_chat_text("跑 echo hi", db=db, server_states={}, config=config, audit_path=audit_path)
    )
    assert result[0]["type"] == "approval_card"
    assert "auto_approved" not in result[0]
    assert db.list_jobs() == []


def test_enqueue_intent_dangerous_command_still_rejected_with_any_rule(db, audit_path, tmp_path):
    """規則寫 `.*`（理論上會命中一切）也救不回危險指令——is_dangerous() 在
    建立核准請求當下就先發生，規則引擎連 approval 都看不到。"""
    rules_path = tmp_path / "auto_approve.yaml"
    rules_path.write_text("rules:\n  - source: any\n    command_regex: '.*'\n", encoding="utf-8")
    config = make_config(auto_approve_rules_path=str(rules_path))

    result = asyncio.run(
        handle_chat_text(
            "跑 rm -rf /tmp/x", db=db, server_states={}, config=config, audit_path=audit_path
        )
    )
    assert result[0]["type"] == "reply"
    assert "拒絕" in result[0]["text"]
    assert db.list_approvals() == []
    assert db.list_jobs() == []
