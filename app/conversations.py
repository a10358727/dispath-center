"""DG-CONVERSATION-V1 CV-2a：每 project 一個 main conversation 的 domain 層。

一輪對話 = 讀 DB 裡這個 project 的 main conversation 的一段有界歷史 → 組進
`app.agent_runtime.run_agent()` 既有的 JSON tool loop（工具白名單、approval
提案、prompt injection 血本封頂全部原封不動，見該模組 docstring）→ 把這一輪
的 user 訊息與最終 assistant 回覆（含從 `approval_card` 抽出的 refs）寫回
`ai_conversations`/`ai_conversation_messages`（`app/db.py`，SQLite 是唯一真相，
CV-1）。

**INV-LLM-5**：`ANTHROPIC_API_KEY` 未設定（或未裝 anthropic 套件）時，
`run_conversation_turn()` 回傳一個 `status="llm_unavailable"` 的
`ConversationTurnResult`，**絕不拋例外**——呼叫端（`app/main.py` 的路由）
直接把這個狀態轉成一個對使用者友善的訊息，排程/核准等核心功能完全不受
影響。

**refs 限制（CV-3/CV-4，已知、記錄在案的 v1 缺口）**：`app.agent_tools.TOOLS`
目前沒有任何「提案 engineering task／Run」的工具（PLAN.md M.2：
`request_apply_patch` 只在 MCP bridge，不在這張表），只有
`request_enqueue_job`/`request_stop_job`/`request_rerun_job`/
`request_add_server`/`request_update_server`/`request_disable_server`/
`request_import_project_candidate`/`request_ignore_project_candidate`/
`scan_project_inventory` 這幾種既有 `request_*_approval()` 寫入工具。
本模組能清楚產生的參照因此只有這些工具建立的 **approval id**（`run_agent()`
既有的 `approval_card` 抽取機制，見 `app.agent_runtime.run_agent()`）；一輪
裡沒有任何工具建立 approval 時 `refs` 是 `None`，不是解析失敗。"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent_runtime import run_agent
from app.config import AppConfig
from app.db import AIConversation, AIConversationMessage, Database
from app.identity import RequestContext
from app.llm import agent_chat_completion, get_model, is_llm_available
from app.usage_recording import make_usage_recorder

#: 送進 tool loop 前，從 DB 取回的歷史訊息上限（一輪一問一答，這裡取的是
#: 「訊息數」不是「輪數」，跟 `app.db.Database.list_conversation_messages()`
#: 的預設值刻意不同——對話輪次通常比全域聊天短，40 則訊息已經是相當長的
#: 一段歷史）。
CONVERSATION_HISTORY_MESSAGES = 40


def _project_context_text(project_name: str) -> str:
    return (
        f"這個對話固定屬於專案「{project_name}」。查詢或建立核准請求時，"
        "除非使用者的訊息明確指定了另一個專案，一律把這個專案名稱當成"
        "預設的 project/project_name 參數值。"
    )


@dataclass(frozen=True)
class ConversationTurnResult:
    """`run_conversation_turn()` 的回傳形狀，`status` 是唯一的分派依據。"""

    #: "ok" | "llm_unavailable"
    status: str
    conversation: Optional[AIConversation] = None
    user_message: Optional[AIConversationMessage] = None
    assistant_message: Optional[AIConversationMessage] = None
    #: `run_agent()` 這一輪產生的完整訊息序列（tool_note/approval_card 等），
    #: 供呼叫端（路由層）需要時做更豐富的呈現；核心欄位仍是上面兩則持久化
    #: 訊息。
    runtime_messages: tuple[dict, ...] = field(default_factory=tuple)


async def run_conversation_turn(
    db: Database,
    *,
    project_name: str,
    text: str,
    config: AppConfig,
    server_states: dict,
    audit_path: str = "audit.jsonl",
    llm_client: Any = None,
    server_configs: Optional[dict] = None,
    ssh_run: Any = None,
    ssh_run_direct: Any = None,
    request_context: Optional[RequestContext] = None,
) -> ConversationTurnResult:
    """一輪對話：讀歷史 → 呼叫既有 tool loop → 落 DB。

    `llm_client`：duck-typed Anthropic client（`app_state.llm_client`，測試
    注入假 client）。`client is None` 時退回 `is_llm_available(config)` 的
    判斷——兩者任一條件成立才嘗試呼叫，否則直接回 `llm_unavailable`
    （INV-LLM-5，不拋例外）。"""
    if llm_client is None and not is_llm_available(config):
        return ConversationTurnResult(status="llm_unavailable")

    conversation = db.get_or_create_project_conversation(project_name)
    prior = db.list_conversation_messages(
        conversation.id, limit=CONVERSATION_HISTORY_MESSAGES
    )
    history = [{"role": m.role, "content": m.content} for m in prior]

    runtime_messages = await run_agent(
        text,
        db=db,
        server_states=server_states,
        config=config,
        audit_path=audit_path,
        http_client=llm_client,
        server_configs=server_configs,
        ssh_run=ssh_run,
        ssh_run_direct=ssh_run_direct,
        history=history,
        request_context=request_context,
        complete=functools.partial(
            agent_chat_completion,
            record_usage=make_usage_recorder(
                db, channel="api", model=get_model(config)
            ),
        ),
        project_context=_project_context_text(project_name),
    )

    reply_text = ""
    refs: Optional[dict] = None
    for msg in runtime_messages:
        msg_type = msg.get("type")
        if msg_type in ("reply", "system"):
            # "system" 訊息（例如 LLM 呼叫中途失敗的降級說明）也當成這一輪
            # 對使用者可見的回覆內容——run_agent() 在這類情況下會直接終止
            # 整個 loop，這則訊息就是唯一給使用者看的內容。
            reply_text = msg.get("text") or reply_text
        elif msg_type == "approval_card":
            approval = msg.get("approval") or {}
            approval_id = approval.get("id")
            if approval_id is not None:
                refs = {"approval_id": approval_id}

    if not reply_text:
        reply_text = "（沒有產生任何回覆內容）"

    user_message = db.append_conversation_message(
        conversation.id, role="user", content=text, refs=None
    )
    assistant_message = db.append_conversation_message(
        conversation.id, role="assistant", content=reply_text, refs=refs
    )

    return ConversationTurnResult(
        status="ok",
        conversation=conversation,
        user_message=user_message,
        assistant_message=assistant_message,
        runtime_messages=tuple(runtime_messages),
    )


__all__ = [
    "CONVERSATION_HISTORY_MESSAGES",
    "ConversationTurnResult",
    "run_conversation_turn",
]
