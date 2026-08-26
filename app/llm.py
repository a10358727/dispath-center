"""LLM 隔離層（鐵律第 1 條的實作核心）。

`import anthropic` 包在 try/except 裡：沒裝這個套件、或沒有設定
`ANTHROPIC_API_KEY`，`is_llm_available()` 回傳 False，整個系統照常運作。
**其他模組一律只透過這個模組的窄介面使用 LLM**（`is_llm_available()`／
`classify_intent()`／`diagnose_job_failure()`／`summarize_mail_body()`），
任何入口在 LLM 不可用時都要有明確降級行為，絕不能讓例外炸到呼叫端。

LLM 只用在「理解使用者的自然語言」（聊天 intent 分類）與「失敗診斷」／
「信件摘要」這種輔助性質的地方——**不進排程迴圈，也不代產狀態數據**：
`classify_intent()` 只回傳一個 intent 分類 JSON，實際的伺服器/任務狀態
一律由呼叫端（`app/chat.py`）用確定性程式從 DB/AppState 組出來。

所有對外函式都支援注入假的 `client`（duck-typed：只要有一個 async
`client.messages.create(...)` 方法、回傳物件有 `.content`，每個 block 有
`.type`／`.text`／`.name`／`.input`），測試絕不真的呼叫 Anthropic API。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from app.config import AppConfig

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 是否安裝這個套件本身不影響測試覆蓋率的判斷
    import anthropic
except Exception:  # noqa: BLE001 - 沒裝這個套件也要能跑（鐵律第 1 條）
    anthropic = None  # type: ignore[assignment]

#: 預設模型；`.env` 的 LLM_MODEL 可覆蓋（見 app/config.py）。
DEFAULT_MODEL = "claude-sonnet-5"

FALLBACK_HELP_REPLY = (
    "目前是規則式理解，支援三種用法：\n"
    "1. 傳「狀態」查看伺服器狀態\n"
    "2. 傳「任務」查看任務佇列\n"
    "3. 傳「跑 <指令>」建立派工請求（仍需核准才會入列）"
)


class LLMError(Exception):
    """LLM 呼叫失敗：例外、逾時、輸出解析不了等。呼叫端應該捕捉這個例外
    並依情境降級（聊天走規則式／信件不加摘要／診斷回 502），絕不能讓它
    往外炸、影響系統其他部分運作。"""


class LLMUnavailableError(LLMError):
    """anthropic 套件未安裝，或 ANTHROPIC_API_KEY 未設定。"""


def is_anthropic_package_installed() -> bool:
    """DG-ASSISTANT-CLAUDE-TURN v1 C2：`GET /api/v2/ai-providers/status` 的
    `anthropic.package_installed` 欄位——只回報套件是否 import 成功，不含任何
    金鑰狀態（那是 `config.anthropic_api_key` 的事，見 `is_llm_available()`）。
    """
    return anthropic is not None


def is_llm_available(config: AppConfig) -> bool:
    """anthropic 套件 import 成功，且 `ANTHROPIC_API_KEY`（`config.anthropic_api_key`）
    有設定才回傳 True。這是整個 LLM 隔離層唯一的「開關」判斷式，其他模組
    要判斷 LLM 能不能用，一律呼叫這個函式，不自己重複判斷條件。"""
    return anthropic is not None and bool(config.anthropic_api_key)


def get_model(config: AppConfig) -> str:
    return config.llm_model or DEFAULT_MODEL


def build_client(config: AppConfig) -> Any:
    """建立真正的 `anthropic.AsyncAnthropic` client。只有 `is_llm_available()`
    為 True 時才應該呼叫；呼叫端（`app.main.AppState`）在啟動時建立一次、
    之後重複使用。測試一律注入假 client，不會呼叫這個函式，也不會觸發
    真正的網路連線。"""
    if anthropic is None:
        raise LLMUnavailableError("anthropic 套件未安裝")
    if not config.anthropic_api_key:
        raise LLMUnavailableError("未設定 ANTHROPIC_API_KEY")
    return anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)


def _extract_tool_input(response: Any, tool_name: str) -> dict:
    content = getattr(response, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            data = getattr(block, "input", None)
            if not isinstance(data, dict):
                raise ValueError(f"tool_use({tool_name}) 的 input 不是物件: {data!r}")
            return dict(data)
    raise ValueError(f"回應中找不到 tool_use({tool_name}) 區塊")


def _extract_text(response: Any) -> str:
    content = getattr(response, "content", None) or []
    texts = [
        getattr(block, "text", "") for block in content if getattr(block, "type", None) == "text"
    ]
    return "\n".join(t for t in texts if t).strip()


# ---------------------------------------------------------------------------
# 規則式後備（無 key 時三種用法要能動；純函式，可測）
# ---------------------------------------------------------------------------


def parse_intent_fallback(text: str) -> dict:
    """沒有 LLM（或 LLM 呼叫失敗降級）時的規則式後備解析：純函式，可測。

    - 含「狀態」→ status
    - 含「任務」→ jobs
    - 「跑 <指令>」開頭 → enqueue（指令為其餘部分）；後面沒有指令內容則
      當成一般聊天，回固定用法說明
    - 其他 → chat（固定回覆說明三種用法）
    """
    stripped = (text or "").strip()
    empty_intent = {
        "intent": "chat",
        "command": None,
        "project": None,
        "pin_server": None,
        "require_tag": None,
        "reply": FALLBACK_HELP_REPLY,
    }
    if not stripped:
        return empty_intent
    if "狀態" in stripped:
        return {
            "intent": "status",
            "command": None,
            "project": None,
            "pin_server": None,
            "require_tag": None,
            "reply": None,
        }
    if "任務" in stripped:
        return {
            "intent": "jobs",
            "command": None,
            "project": None,
            "pin_server": None,
            "require_tag": None,
            "reply": None,
        }
    if stripped.startswith("跑"):
        command = stripped[1:].strip()
        if command:
            return {
                "intent": "enqueue",
                "command": command,
                "project": None,
                "pin_server": None,
                "require_tag": None,
                "reply": None,
            }
    return empty_intent


# ---------------------------------------------------------------------------
# 聊天 intent 分類（tool-use 強制結構化輸出）
# ---------------------------------------------------------------------------

INTENT_TOOL_NAME = "report_intent"

INTENT_TOOL = {
    "name": INTENT_TOOL_NAME,
    "description": (
        "回報使用者這句話的意圖，結構化輸出，供伺服器端確定性程式接手處理。"
        "不要自己編造伺服器或任務的狀態數據，那些一律由伺服器端另外提供。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["status", "jobs", "enqueue", "chat"],
                "description": (
                    "status=詢問伺服器狀態；jobs=詢問任務佇列；"
                    "enqueue=要求派工/執行指令；chat=其他一般對話"
                ),
            },
            "command": {
                "type": ["string", "null"],
                "description": "intent=enqueue 時要執行的完整 shell 指令",
            },
            "project": {"type": ["string", "null"], "description": "相關的專案名稱（若有提到）"},
            "pin_server": {"type": ["string", "null"], "description": "指定要在哪台機器執行（若有提到）"},
            "require_tag": {"type": ["string", "null"], "description": "要求機器帶有的標籤，例如 gpu"},
            "reply": {
                "type": ["string", "null"],
                "description": "intent=chat 時要回覆使用者的文字",
            },
        },
        "required": ["intent"],
    },
}

_CHAT_SYSTEM_PROMPT = (
    "你是 AI 訓練調度中心的助理。你唯一的工作是透過 report_intent 這個工具回報"
    "使用者這句話的意圖，讓伺服器端接手做確定性處理——你不會、也不能直接執行任何"
    "指令或修改任何系統狀態，也不要在回應裡編造伺服器或任務的即時數據"
    "（那些會由伺服器端另外組好提供給使用者，不需要你產生）。"
    "intent=enqueue 時，盡量把使用者想執行的完整指令填進 command；"
    "intent=chat 時，用 reply 欄位簡短回覆（繁體中文）。"
)


async def classify_intent(
    text: str,
    context_summary: str,
    config: AppConfig,
    client: Any = None,
) -> dict:
    """把使用者訊息＋目前狀態摘要餵給 LLM，用 tool-use 強制輸出結構化
    intent JSON：`{"intent": ..., "command": ..., "project": ..., "pin_server":
    ..., "require_tag": ..., "reply": ...}`。

    LLM 只產生這個分類結果，**資料一律由伺服器端確定性程式組**（鐵律第 1
    條）——這個函式完全不碰 DB／AppState，`context_summary` 由呼叫端
    （`app/chat.py`）事先組好文字傳進來。

    失敗（未設定/未安裝、API 例外、逾時、輸出解析不了、intent 不合法）一律
    丟 `LLMError`，呼叫端應該 catch 之後改用 `parse_intent_fallback()`。
    """
    if client is None:
        if not is_llm_available(config):
            raise LLMUnavailableError("LLM 不可用")
        client = build_client(config)

    messages = [
        {
            "role": "user",
            "content": f"目前狀態摘要：\n{context_summary}\n\n使用者訊息：{text}",
        }
    ]
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=get_model(config),
                max_tokens=1024,
                system=_CHAT_SYSTEM_PROMPT,
                messages=messages,
                tools=[INTENT_TOOL],
                tool_choice={"type": "tool", "name": INTENT_TOOL_NAME},
            ),
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - 任何 SDK 例外/逾時都視為 LLM 失敗
        raise LLMError(f"呼叫 LLM 失敗: {exc}") from exc

    try:
        data = _extract_tool_input(response, INTENT_TOOL_NAME)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"LLM 輸出無法解析: {exc}") from exc

    intent = data.get("intent")
    if intent not in ("status", "jobs", "enqueue", "chat"):
        raise LLMError(f"LLM 回傳不合法的 intent: {intent!r}")

    return {
        "intent": intent,
        "command": data.get("command"),
        "project": data.get("project"),
        "pin_server": data.get("pin_server"),
        "require_tag": data.get("require_tag"),
        "reply": data.get("reply"),
    }


# ---------------------------------------------------------------------------
# 失敗診斷
# ---------------------------------------------------------------------------

#: 公開常數：`app/llm_local.py` 的 `diagnose_job_failure_local()` 重用同一份
#: 中文 system prompt，避免兩處各自維護一份容易漂移的措辭。
DIAGNOSE_SYSTEM_PROMPT = (
    "你是 AI 訓練調度中心的失敗診斷助理。根據任務指令、退出碼、log 尾段，"
    "以及（若有提供）專案目錄結構，用繁體中文說明可能的失敗原因，並提出修改"
    "建議，修改建議請用 diff 格式（--- / +++ / @@ 或至少清楚的 -/+ 前綴）呈現。"
    "你只能提出建議，**絕對不會、也不能**執行任何指令、修改任何檔案或重跑任務"
    "——那些動作只能由使用者自己在核准之後手動操作。"
)
#: 舊名沿用（模組內部使用），避免大範圍改動。
_DIAGNOSE_SYSTEM_PROMPT = DIAGNOSE_SYSTEM_PROMPT


async def diagnose_job_failure(
    *,
    command: str,
    exit_code: Optional[int],
    log_tail: Optional[str],
    project_structure: Optional[str] = None,
    config: AppConfig,
    client: Any = None,
    max_retries: int = 2,
) -> str:
    """failed 任務的診斷：只輸出說明文字＋diff 建議，**只顯示，絕不執行、
    不改碼、不重跑**（鐵律＋實作指令 5.8）。

    呼叫失敗（例外/逾時/解析失敗/空白輸出）最多再重試 `max_retries` 次
    （預設 2，總共最多嘗試 3 次），仍失敗才丟 `LLMError`，呼叫端
    （`POST /jobs/{id}/diagnose`）應該轉成 502。
    """
    if client is None:
        if not is_llm_available(config):
            raise LLMUnavailableError("未設定 ANTHROPIC_API_KEY，診斷不可用")
        client = build_client(config)

    prompt_parts = [
        f"指令：{command}",
        f"退出碼：{exit_code if exit_code is not None else '未知'}",
        "log 尾段：",
        log_tail or "（無 log）",
    ]
    if project_structure:
        prompt_parts += ["", "專案目錄結構：", project_structure]
    user_content = "\n".join(prompt_parts)

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=get_model(config),
                    max_tokens=1500,
                    system=_DIAGNOSE_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_content}],
                ),
                timeout=60,
            )
            text = _extract_text(response)
            if not text:
                raise ValueError("LLM 回傳空白內容")
            return text
        except Exception as exc:  # noqa: BLE001 - 記下來，繼續重試或最後往上丟
            last_error = exc
            logger.warning("失敗診斷呼叫 LLM 失敗（第 %d/%d 次）：%s", attempt + 1, max_retries + 1, exc)

    raise LLMError(f"診斷呼叫 LLM 失敗（已重試 {max_retries} 次）：{last_error}")


# ---------------------------------------------------------------------------
# 信件摘要
# ---------------------------------------------------------------------------

#: 公開常數：`app/llm_local.py` 的 `summarize_mail_body_local()` 重用。
SUMMARY_SYSTEM_PROMPT = (
    "你是 AI 訓練調度中心的通知信摘要助理。根據任務結束通知信內容，用不超過"
    "三行的繁體中文摘要重點；若任務失敗，摘要要包含可能原因。只輸出摘要文字"
    "本身，不要加任何額外說明、前綴或客套話。"
)
_SUMMARY_SYSTEM_PROMPT = SUMMARY_SYSTEM_PROMPT


async def summarize_mail_body(body: str, config: AppConfig, client: Any = None) -> Optional[str]:
    """信件摘要（≤3 行）。**任何失敗都回傳 None，絕不丟例外**——呼叫端
    （`app/jobfinish.py`）應該在拿不到摘要時照常寄原信，不因為摘要失敗而
    不寄信。"""
    if client is None:
        if not is_llm_available(config):
            return None
        try:
            client = build_client(config)
        except LLMError:
            return None
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=get_model(config),
                max_tokens=300,
                system=_SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": body}],
            ),
            timeout=20,
        )
        text = _extract_text(response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("信件摘要呼叫 LLM 失敗：%s", exc)
        return None
    return text or None


# ---------------------------------------------------------------------------
# Agent tool loop 的 Anthropic 後端（DG-CONVERSATION-V1 CV-2a）
# ---------------------------------------------------------------------------


def _extract_anthropic_usage(response: Any) -> Optional[dict]:
    """Packet D3 (usage accounting): defensively pull `{"input_tokens":..,
    "output_tokens":..}` off the Anthropic SDK response's `.usage` object
    (`anthropic.types.Usage`, duck-typed here so a fake test response with
    plain attributes/dict works identically). `None` on any missing/
    non-int shape — never raises, never affects `agent_chat_completion()`'s
    text-only return contract."""

    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


async def agent_chat_completion(
    messages: list[dict],
    config: AppConfig,
    client: Any = None,
    timeout: float = 60,
    record_usage: Optional[Callable[[dict], None]] = None,
) -> str:
    """`app.agent_runtime.run_agent()` 既有 JSON tool loop 的 Anthropic 版
    completion 後端，介面刻意對齊 `app.llm_local.chat_completion()`
    （`messages: [{"role": "system"|"user"|"assistant", "content": str}]`
    進、純文字回傳）——tool loop 本身（步數上限、修正一次的容錯、工具白名單
    校驗）完全不變，只是把「下一步要吐什麼」這一次呼叫換成 Anthropic API，
    不改 `app.agent_tools.TOOLS`、不改任何工具的參數形狀（CV-2a 契約）。

    Anthropic API 的 `system` 是獨立參數、不是 messages 陣列裡的一個角色，
    這裡把 `messages` 裡所有 `role == "system"` 的內容合併成一段（`run_agent()`
    目前只會放一則），其餘 user/assistant 訊息原樣轉送。任何失敗（未設定/
    未安裝、API 例外、逾時、空白輸出）一律丟 `LLMError`，跟 `LLMLocalError`
    在 `run_agent()` 裡是同一種「明確終止、回覆降級系統訊息」處理方式。

    `record_usage`（packet D3，選填）：成功時若能從 `response.usage` 解出
    `{"input_tokens", "output_tokens"}`，呼叫這個 callback 一次——**完全不
    改這個函式本身「回傳純文字」的既有契約**（呼叫端仍是 `await
    agent_chat_completion(messages, config, client=...)`，不需要知道
    usage 這件事），`app.conversations.run_conversation_turn()` 用
    `functools.partial(agent_chat_completion, record_usage=...)` 綁定一個
    已經跟 db 綁好、絕不拋例外的 recorder（見 `app.usage_recording`）。
    任何例外（含 `record_usage` 本身壞掉）都不能影響這一輪對話的結果，
    所以呼叫點額外包一層防禦性 try/except。"""
    if client is None:
        if not is_llm_available(config):
            raise LLMUnavailableError("未設定 ANTHROPIC_API_KEY，agent 對話不可用")
        client = build_client(config)

    system_parts = [
        m.get("content") or "" for m in messages if m.get("role") == "system"
    ]
    system_prompt = "\n\n".join(part for part in system_parts if part)
    conversation = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]

    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=get_model(config),
                max_tokens=1500,
                system=system_prompt,
                messages=conversation,
            ),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 任何 SDK 例外/逾時都視為呼叫失敗
        raise LLMError(f"呼叫 LLM 失敗: {exc}") from exc

    text = _extract_text(response)
    if not text:
        raise LLMError("LLM 回傳空白內容")

    if record_usage is not None:
        try:
            usage = _extract_anthropic_usage(response)
            if usage is not None:
                record_usage(usage)
        except Exception as exc:  # noqa: BLE001 - usage 記錄絕不能影響這一輪對話
            logger.warning("記錄 agent_chat_completion usage 失敗: %s", exc)

    return text
