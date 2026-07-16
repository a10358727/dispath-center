"""聊天 intent 處理（實作指令 5.7／PLAN.md F 節）。

WS `/ws` 收到使用者訊息之後：

1. 若可以嘗試用 LLM（有注入 client，或 `app.llm.is_llm_available()`），呼叫
   `app.llm.classify_intent()` 把文字轉成結構化 intent；呼叫失敗
   （`LLMError`）一律降級走規則式 `app.llm.parse_intent_fallback()`，並在
   回覆前面附上一則 `system` 訊息註明「LLM 暫不可用，已用規則式理解」。
   本來就沒有設定 LLM（沒裝套件／沒 key）時直接走規則式，不算「呼叫失敗」，
   不會附加這則提示（沒有必要每句話都提醒使用者一件從一開始就是這樣的事）。
2. intent 的後續處理是**確定性**的，跟有沒有 LLM 完全無關：
   - status／jobs：純函式組文字摘要回覆
   - enqueue：呼叫既有 `app.approvals.request_enqueue_approval()`（危險指令
     直接被拒、`type="train"` + `project` 時會帶 sync/setup 計畫與
     warning，全部沿用），回 `approval_card` 訊息；**一律走核准，聊天絕不
     直接入列**（鐵律第 2 條）
   - chat：回 LLM 的 `reply` 文字（規則式後備只會產生固定用法說明）

這個模組刻意跟 WebSocket 物件本身脫鉤（只吃字串、回傳一串 dict），方便不
開真的 WS 連線就能單元測試；`app/main.py` 的 `/ws` endpoint 只負責收發
JSON 與呼叫 `handle_chat_text()`。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.approvals import (
    DangerousCommandError,
    ProjectNotFoundError,
    approval_to_dict,
    maybe_auto_approve,
    request_enqueue_approval,
)
from app.autoapprove import get_rules
from app.config import AppConfig
from app.db import Database
from app.identity import RequestContext
from app.llm import (
    LLMError,
    classify_intent,
    is_llm_available,
    parse_intent_fallback,
)

logger = logging.getLogger(__name__)

_STATUS_LABEL = {
    "queued": "排隊中",
    "running": "執行中",
    "done": "完成",
    "failed": "失敗",
    "blocked": "被擋住",
    "cancelled": "已取消",
}

_LLM_FALLBACK_NOTICE = "LLM 暫不可用，已用規則式理解"


# ---------------------------------------------------------------------------
# 確定性文字摘要（status / jobs / context，跟有沒有 LLM 無關）
# ---------------------------------------------------------------------------


def build_status_reply(server_states: dict) -> str:
    if not server_states:
        return "目前沒有設定任何伺服器（檢查 servers.yaml）。"
    lines = ["目前伺服器狀態："]
    for name in sorted(server_states):
        state = server_states[name]
        if not state.online:
            note = f"（{state.error}）" if state.error else ""
            lines.append(f"- {name}：離線{note}")
            continue
        gpu = f"{state.gpu_util_max:.0f}%" if state.gpu_util_max is not None else "無讀數"
        load1 = state.load1 if state.load1 is not None else "-"
        lines.append(f"- {name}：在線，GPU {gpu}，load1 {load1}")
    return "\n".join(lines)


def build_jobs_reply(db: Database) -> str:
    jobs = db.list_jobs()
    if not jobs:
        return "目前沒有任何任務。"
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    counts_line = "、".join(f"{_STATUS_LABEL.get(k, k)} {v}" for k, v in counts.items())
    lines = [f"任務佇列共 {len(jobs)} 筆（{counts_line}）", "最近幾筆："]
    for job in jobs[-5:]:
        if (
            job.engineering_task_id is not None
            or job.engineering_validation_request_id is not None
        ):
            if job.engineering_validation_request_id is not None:
                command_hint = (
                    "Push verified Engineering Task bundle to approved worker"
                    if job.type == "sync"
                    else "Run approved Engineering Task worker validation"
                )
            else:
                command_hint = {
                    "staging": "Prepare immutable Engineering Task inputs",
                    "coding": "Run Codex agent in an isolated worktree",
                    "validation": "Run approved Engineering Task validation",
                }.get(job.engineering_task_role or "", "Run Engineering Task step")
        else:
            command_hint = job.command[:40] + ("…" if len(job.command) > 40 else "")
        lines.append(
            f"- #{job.id} [{_STATUS_LABEL.get(job.status, job.status)}] "
            f"{job.project or '-'} {command_hint}"
        )
    return "\n".join(lines)


def build_context_summary(db: Database, server_states: dict) -> str:
    """餵給 LLM 看的純文字狀態摘要（伺服器＋任務＋專案），純文字組裝、
    不含任何 LLM 呼叫。LLM 只拿這段文字當背景資訊做 intent 分類，
    **不會**、也不需要自己產生這些數據（鐵律第 1 條）。"""
    parts = [build_status_reply(server_states), "", build_jobs_reply(db)]
    projects = db.list_projects()
    if projects:
        parts.append("")
        parts.append("已註冊專案：" + "、".join(p.name for p in projects))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# intent 解析：有 LLM 就試、失敗就降級；決定要不要附加降級提示
# ---------------------------------------------------------------------------


async def _resolve_intent(
    text: str, db: Database, server_states: dict, config: AppConfig, llm_client: Any
) -> tuple[dict, bool]:
    """回傳 (intent_data, llm_call_failed)。`llm_call_failed` 只在「真的嘗試
    呼叫 LLM 但失敗了」時才是 True——原本就沒設定 LLM 不算失敗，不需要每句
    話都提醒使用者。"""
    should_attempt = llm_client is not None or is_llm_available(config)
    if not should_attempt:
        return parse_intent_fallback(text), False

    context_summary = build_context_summary(db, server_states)
    try:
        data = await classify_intent(text, context_summary, config, client=llm_client)
        return data, False
    except LLMError as exc:
        logger.warning("聊天 LLM 呼叫失敗，降級走規則式：%s", exc)
        return parse_intent_fallback(text), True


# ---------------------------------------------------------------------------
# 主入口：把一句使用者訊息變成一串要送回前端的 ws 訊息（dict，尚未序列化）
# ---------------------------------------------------------------------------


async def handle_chat_text(
    text: str,
    *,
    db: Database,
    server_states: dict,
    config: AppConfig,
    audit_path: str = "audit.jsonl",
    llm_client: Any = None,
    server_configs: Optional[dict] = None,
    request_context: Optional[RequestContext] = None,
) -> list[dict]:
    """處理一句使用者聊天訊息，回傳要依序送給前端的一或多則訊息（dict）。

    訊息形狀：
        {"type": "system", "text": "..."}          —— 降級提示等系統訊息
        {"type": "reply", "text": "..."}            —— 純文字回覆
        {"type": "approval_card", "approval": {...}, "auto_approved": bool}
                                                     —— enqueue 待核准/已自動核准卡片

    `server_configs`（階段 10，PLAN.md K.3）：`app_state.server_configs`，
    只有自動核准命中且 payload 帶 sync_plan 時才用得到（組 rsync 指令要知道
    目標機設定）；`None` 時自動核准仍然照常運作，只是遇到需要 sync 的計畫
    會在 `approve()` 內丟 `ValueError`（理論上不會發生，聊天 enqueue 目前
    不會帶 pin_server 明確指定機器，因此不會產生 sync_plan）。
    """
    intent_data, llm_failed = await _resolve_intent(text, db, server_states, config, llm_client)
    messages: list[dict] = []
    if llm_failed:
        messages.append({"type": "system", "text": _LLM_FALLBACK_NOTICE})

    intent = intent_data.get("intent")

    if intent == "status":
        messages.append({"type": "reply", "text": build_status_reply(server_states)})
    elif intent == "jobs":
        messages.append({"type": "reply", "text": build_jobs_reply(db)})
    elif intent == "enqueue":
        messages.append(
            await _handle_enqueue_intent(
                intent_data,
                db,
                audit_path,
                config=config,
                server_configs=server_configs,
                request_context=request_context,
            )
        )
    else:  # "chat"，或 LLM 回了非預期值（classify_intent 已經驗證過，理論上不會發生）
        reply = intent_data.get("reply") or "我沒有理解這句話，請換個方式說，或試試「狀態」「任務」「跑 <指令>」。"
        messages.append({"type": "reply", "text": reply})

    return messages


async def _handle_enqueue_intent(
    intent_data: dict,
    db: Database,
    audit_path: str,
    *,
    config: Optional[AppConfig] = None,
    server_configs: Optional[dict] = None,
    request_context: Optional[RequestContext] = None,
) -> dict:
    """建立 enqueue 核准請求：**一律走核准，聊天絕不直接入列**（鐵律第 2
    條——這裡指「模型自己」絕不能直接入列；下面的自動核准諮詢是**使用者預
    先寫好的確定性規則**在核准，不是模型，見 `app.autoapprove` 模組
    docstring）。危險指令、找不到專案等錯誤都轉成純文字 reply 訊息，不讓
    例外往上炸掉整個 WS 連線。

    階段 10（PLAN.md K.1/K.3）：`source="vllm"`（這條路徑同時服務 anthropic
    與規則式聊天後備，`source` 名稱沿用 PLAN.md 既有用詞，代表「聊天/agent
    通道」而非字面上限定本地 vLLM）；建立成功後諮詢自動核准規則
    （`config` 為 `None` 時——理論上不會發生，`app/main.py` 一定會傳——就
    跳過諮詢，維持既有 pending 行為）。
    """
    # Slice 3 carries identity to this approval-creation boundary without
    # changing persisted approval fields.  Slice 4 consumes the context.
    _ = request_context
    command = (intent_data.get("command") or "").strip()
    if not command:
        return {
            "type": "reply",
            "text": "請告訴我要執行的完整指令，例如「跑 echo hi」。",
        }

    project = intent_data.get("project")
    job_type = "train" if project else "adhoc"
    try:
        approval = request_enqueue_approval(
            db,
            command=command,
            type=job_type,
            project=project,
            require_tag=intent_data.get("require_tag"),
            pin_server=intent_data.get("pin_server"),
            source="vllm",
            audit_path=audit_path,
            request_context=request_context,
        )
    except DangerousCommandError as exc:
        return {"type": "reply", "text": f"指令被拒絕：{exc}"}
    except ProjectNotFoundError as exc:
        return {"type": "reply", "text": str(exc)}
    except ValueError as exc:
        return {"type": "reply", "text": str(exc)}

    if config is not None:
        rules = get_rules(config.auto_approve_rules_path)
        result = await maybe_auto_approve(
            db,
            approval,
            source="vllm",
            rules=rules,
            server_configs=server_configs,
            audit_path=audit_path,
            request_context=request_context,
        )
        if result is not None:
            return {
                "type": "approval_card",
                "approval": approval_to_dict(result["approval"]),
                "auto_approved": True,
            }

    return {"type": "approval_card", "approval": approval_to_dict(approval)}
