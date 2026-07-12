"""本地 vLLM 隔離層（PLAN.md H 節）。

跟 `app/llm.py` 同一種降級哲學，但這裡是「本地」模型：用 `httpx`（不用
`openai` SDK）呼叫 OpenAI-compatible 的 `POST {VLLM_BASE_URL}/chat/completions`。
`VLLM_BASE_URL`／`VLLM_MODEL` 兩者都有設定，`is_vllm_available()` 才回傳
True——這是這個模組唯一的「開關」判斷式，其他模組要判斷本地模型能不能用，
一律呼叫這個函式。

所有對外函式都支援注入假的 `httpx.AsyncClient`（或任何有 async
`.post(url, json=..., headers=...)`/`.get(url, headers=...)` 方法、回傳
物件有 `.raise_for_status()` 與 `.json()` 的 duck-typed 物件），測試一律
用 `httpx.MockTransport` 或假 client，絕不真的連線任何 vLLM server。

任何 HTTP 錯誤／逾時／回應格式異常一律轉成 `LLMLocalError`，絕不讓原始
例外外洩到呼叫端（跟 `app.llm.LLMError` 的角色一致）。

`VLLM_API_KEY`（`config.vllm_api_key`）有設定時，每次請求都會加上
`Authorization: Bearer {key}` header（見 `_auth_headers()`）——這一欄位純
選填，vLLM 用 `--api-key` 啟動時才需要，**不影響** `is_vllm_available()`
的判斷（沒有 `--api-key` 的部署一樣合法）。header 是在「每次呼叫」時算，
不是綁在共用 client 的建構參數上，所以 `app.main.AppState` 啟動時建立的
那個共用 `httpx.AsyncClient` 不需要因為 key 而特別處理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import AppConfig
from app.llm import DIAGNOSE_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60


class LLMLocalError(Exception):
    """本地 vLLM 呼叫失敗：例外、逾時、輸出格式異常等。呼叫端應該捕捉這個
    例外並依情境降級，絕不能讓它往外炸、影響系統其他部分運作。"""


def is_vllm_available(config: AppConfig) -> bool:
    """`VLLM_BASE_URL`／`VLLM_MODEL`（`config.vllm_base_url`/`config.vllm_model`）
    都有設定才回傳 True。這是本地 vLLM 選配層唯一的「開關」判斷式。"""
    if not config.vllm_base_url or not config.vllm_model:
        return False

    # 防止 VLLM_BASE_URL 誤指回調度中心自己的 FastAPI port。否則 agent
    # 請求會打到自己的 /v1 路徑、失敗後重試，形成看不見的無效流量。
    parsed = urlparse(config.vllm_base_url)
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        try:
            target_port = parsed.port
        except ValueError:
            return False
        if target_port == config.api_port:
            return False
    return True


def _chat_completions_url(config: AppConfig) -> str:
    return f"{(config.vllm_base_url or '').rstrip('/')}/chat/completions"


def _models_url(config: AppConfig) -> str:
    return f"{(config.vllm_base_url or '').rstrip('/')}/models"


def _auth_headers(config: AppConfig) -> dict[str, str]:
    """`VLLM_API_KEY` 有設定時回傳 `{"Authorization": "Bearer ..."}`，沒設
    定回傳空 dict。**每次 request 都重新算一次、加在請求上**，不是綁在
    client 建構時——這樣共用的 `httpx.AsyncClient`（`app.main.AppState` 啟動
    時建立一次、重複使用）不用因為 key 改變就重建，測試注入的假 client
    也吃得到這個 header（假 client 通常不會去讀 client 建構參數）。"""
    if config.vllm_api_key:
        return {"Authorization": f"Bearer {config.vllm_api_key}"}
    return {}


async def chat_completion(
    messages: list[dict],
    config: AppConfig,
    client: Optional[Any] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """呼叫本地 vLLM 的 OpenAI-compatible `/chat/completions`，回傳 assistant
    的文字內容。

    `client` 可注入（測試用 `httpx.AsyncClient(transport=httpx.MockTransport(...))`
    或任意假物件）；沒有注入時這裡自己開一個短命的 `httpx.AsyncClient`，用完
    即關閉，不留下連線。

    任何失敗（未設定、HTTP 錯誤、逾時、JSON 解析失敗、回應形狀不對、空白
    內容）一律丟 `LLMLocalError`。
    """
    if not is_vllm_available(config):
        raise LLMLocalError("VLLM_BASE_URL/VLLM_MODEL 未設定，本地模型不可用")

    payload = {
        "model": config.vllm_model,
        "messages": messages,
    }
    url = _chat_completions_url(config)
    headers = _auth_headers(config)

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()
    try:
        try:
            response = await asyncio.wait_for(
                http_client.post(url, json=payload, headers=headers), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - 任何連線/逾時例外都視為呼叫失敗
            raise LLMLocalError(f"呼叫本地模型失敗: {exc}") from exc
    finally:
        if owns_client:
            await http_client.aclose()

    try:
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - httpx.HTTPStatusError 等
        raise LLMLocalError(f"本地模型回傳錯誤狀態碼: {exc}") from exc

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - JSON 解析失敗、形狀不對
        raise LLMLocalError(f"本地模型回應格式異常: {exc}") from exc

    if not isinstance(content, str) or not content.strip():
        raise LLMLocalError("本地模型回傳空白內容")

    return content


async def health_check(config: AppConfig, client: Optional[Any] = None, timeout: float = 10) -> dict:
    """`GET {VLLM_BASE_URL}/models`，供 `app.agent_tools` 的 `vllm_health`
    工具與健康檢查使用。回傳 `{"ok": bool, "detail": ...}`，**不丟例外**——
    這是給工具/健檢用的唯讀查詢，失敗也只是回報結果，不需要中斷任何流程。
    """
    if not is_vllm_available(config):
        return {"ok": False, "detail": "VLLM_BASE_URL/VLLM_MODEL 未設定"}

    url = _models_url(config)
    headers = _auth_headers(config)
    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()
    try:
        try:
            response = await asyncio.wait_for(
                http_client.get(url, headers=headers), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"呼叫失敗: {exc}"}
    finally:
        if owns_client:
            await http_client.aclose()

    try:
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"回應異常: {exc}"}

    return {"ok": True, "detail": data}


# ---------------------------------------------------------------------------
# 失敗診斷（介面對齊 app.llm.diagnose_job_failure）
# ---------------------------------------------------------------------------


async def diagnose_job_failure_local(
    *,
    command: str,
    exit_code: Optional[int],
    log_tail: Optional[str],
    project_structure: Optional[str] = None,
    config: AppConfig,
    client: Optional[Any] = None,
    max_retries: int = 2,
) -> str:
    """failed 任務的診斷（本地模型版本），介面與重試上限（預設 2，總共最多
    嘗試 3 次）都對齊 `app.llm.diagnose_job_failure()`。仍失敗丟
    `LLMLocalError`，呼叫端應該把它視同 `LLMError` 處理（例如仍然嘗試
    anthropic 或回 502/503）。"""
    if not is_vllm_available(config):
        raise LLMLocalError("VLLM_BASE_URL/VLLM_MODEL 未設定，診斷不可用")

    prompt_parts = [
        f"指令：{command}",
        f"退出碼：{exit_code if exit_code is not None else '未知'}",
        "log 尾段：",
        log_tail or "（無 log）",
    ]
    if project_structure:
        prompt_parts += ["", "專案目錄結構：", project_structure]
    user_content = "\n".join(prompt_parts)

    messages = [
        {"role": "system", "content": DIAGNOSE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return await chat_completion(messages, config, client=client, timeout=60)
        except LLMLocalError as exc:  # noqa: BLE001 - 記下來，繼續重試或最後往上丟
            last_error = exc
            logger.warning(
                "本地模型失敗診斷呼叫失敗（第 %d/%d 次）：%s", attempt + 1, max_retries + 1, exc
            )

    raise LLMLocalError(f"本地模型診斷呼叫失敗（已重試 {max_retries} 次）：{last_error}")


# ---------------------------------------------------------------------------
# 信件摘要（介面對齊 app.llm.summarize_mail_body）
# ---------------------------------------------------------------------------


async def summarize_mail_body_local(
    body: str, config: AppConfig, client: Optional[Any] = None
) -> Optional[str]:
    """信件摘要（本地模型版本，≤3 行）。**任何失敗都回傳 None，絕不丟例外**
    ——呼叫端應該在拿不到摘要時照常寄原信。"""
    if not is_vllm_available(config):
        return None

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]
    try:
        text = await chat_completion(messages, config, client=client, timeout=20)
    except LLMLocalError as exc:
        logger.warning("本地模型信件摘要呼叫失敗：%s", exc)
        return None
    return text or None
