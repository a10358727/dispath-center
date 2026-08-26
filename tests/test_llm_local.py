"""app/llm_local.py：本地 vLLM 隔離層（PLAN.md H 節）。

全部用 `httpx.MockTransport` 或假 client（duck-typed），絕不真的連線任何
vLLM server。跟 `tests/test_llm.py`（anthropic 版本）對齊的測試涵蓋範圍：
成功／HTTP 錯誤／逾時／回應格式異常／未設定時的降級。
"""

from __future__ import annotations

import asyncio
import json as json_module

import httpx
import pytest

from app.config import AppConfig
from app.llm_local import (
    LLMLocalError,
    chat_completion,
    diagnose_job_failure_local,
    health_check,
    is_vllm_available,
    summarize_mail_body_local,
)


def make_config(**overrides) -> AppConfig:
    base = dict(
        servers=[],
        vllm_base_url="http://127.0.0.1:8001/v1",
        vllm_model="qwen3-coder-30b",
    )
    base.update(overrides)
    return AppConfig(**base)


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# is_vllm_available()：唯一的開關判斷式
# ---------------------------------------------------------------------------


def test_is_vllm_available_true_when_both_set():
    assert is_vllm_available(make_config()) is True


def test_is_vllm_available_false_when_base_url_missing():
    assert is_vllm_available(make_config(vllm_base_url=None)) is False


def test_is_vllm_available_false_when_model_missing():
    assert is_vllm_available(make_config(vllm_model=None)) is False


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_is_vllm_available_false_when_pointing_to_own_api(base_url):
    assert is_vllm_available(make_config(vllm_base_url=base_url, api_port=8000)) is False


def test_is_vllm_available_ignores_api_key():
    """`vllm_api_key` 純粹是「要不要送 Authorization header」的旗標，
    不列入 `is_vllm_available()` 的判斷——沒有 `--api-key` 的 vLLM 部署
    也是合法的。"""
    assert is_vllm_available(make_config(vllm_api_key=None)) is True
    assert is_vllm_available(make_config(vllm_api_key="local-dev-key")) is True


# ---------------------------------------------------------------------------
# chat_completion()：成功／HTTP 錯誤／逾時／格式異常／未設定
# ---------------------------------------------------------------------------


def test_chat_completion_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json_module.loads(request.content)
        assert body["model"] == "qwen3-coder-30b"
        assert body["messages"][0]["content"] == "hi"
        return httpx.Response(200, json={"choices": [{"message": {"content": "哈囉"}}]})

    client = make_client(handler)
    config = make_config()
    try:
        text = asyncio.run(
            chat_completion([{"role": "user", "content": "hi"}], config, client=client)
        )
    finally:
        asyncio.run(client.aclose())
    assert text == "哈囉"


def test_chat_completion_records_usage_when_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "哈囉"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    client = make_client(handler)
    config = make_config()
    recorded = []
    try:
        text = asyncio.run(
            chat_completion(
                [{"role": "user", "content": "hi"}],
                config,
                client=client,
                record_usage=recorded.append,
            )
        )
    finally:
        asyncio.run(client.aclose())
    assert text == "哈囉"
    assert recorded == [{"input_tokens": 9, "output_tokens": 4}]


def test_chat_completion_no_usage_in_response_does_not_call_recorder():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "哈囉"}}]})

    client = make_client(handler)
    config = make_config()
    recorded = []
    try:
        asyncio.run(
            chat_completion(
                [{"role": "user", "content": "hi"}],
                config,
                client=client,
                record_usage=recorded.append,
            )
        )
    finally:
        asyncio.run(client.aclose())
    assert recorded == []


def test_chat_completion_broken_recorder_does_not_affect_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "哈囉"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    def broken_recorder(_usage):
        raise RuntimeError("boom")

    client = make_client(handler)
    config = make_config()
    try:
        text = asyncio.run(
            chat_completion(
                [{"role": "user", "content": "hi"}],
                config,
                client=client,
                record_usage=broken_recorder,
            )
        )
    finally:
        asyncio.run(client.aclose())
    assert text == "哈囉"


def test_chat_completion_sends_bearer_header_when_api_key_configured():
    """vLLM 用 `--api-key` 啟動時，`VLLM_API_KEY` 有設定就要在每個請求上
    帶 `Authorization: Bearer <key>`（不是綁在 client 建構參數上）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer local-dev-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "哈囉"}}]})

    client = make_client(handler)
    config = make_config(vllm_api_key="local-dev-key")
    try:
        text = asyncio.run(
            chat_completion([{"role": "user", "content": "hi"}], config, client=client)
        )
    finally:
        asyncio.run(client.aclose())
    assert text == "哈囉"


def test_chat_completion_no_authorization_header_when_api_key_not_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "哈囉"}}]})

    client = make_client(handler)
    config = make_config(vllm_api_key=None)
    try:
        asyncio.run(chat_completion([{"role": "user", "content": "hi"}], config, client=client))
    finally:
        asyncio.run(client.aclose())


def test_chat_completion_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = make_client(handler)
    config = make_config()
    try:
        with pytest.raises(LLMLocalError):
            asyncio.run(
                chat_completion([{"role": "user", "content": "hi"}], config, client=client)
            )
    finally:
        asyncio.run(client.aclose())


def test_chat_completion_timeout_wraps_as_llmlocalerror():
    class TimeoutClient:
        async def post(self, url, json=None, headers=None):
            raise httpx.TimeoutException("timed out")

    config = make_config()
    with pytest.raises(LLMLocalError):
        asyncio.run(
            chat_completion(
                [{"role": "user", "content": "hi"}], config, client=TimeoutClient()
            )
        )


def test_chat_completion_malformed_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = make_client(handler)
    config = make_config()
    try:
        with pytest.raises(LLMLocalError):
            asyncio.run(
                chat_completion([{"role": "user", "content": "hi"}], config, client=client)
            )
    finally:
        asyncio.run(client.aclose())


def test_chat_completion_empty_content_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    client = make_client(handler)
    config = make_config()
    try:
        with pytest.raises(LLMLocalError):
            asyncio.run(
                chat_completion([{"role": "user", "content": "hi"}], config, client=client)
            )
    finally:
        asyncio.run(client.aclose())


def test_chat_completion_not_configured_raises_without_network_call():
    config = make_config(vllm_base_url=None)
    with pytest.raises(LLMLocalError):
        asyncio.run(chat_completion([{"role": "user", "content": "hi"}], config))


# ---------------------------------------------------------------------------
# health_check()：不丟例外，只回報結果
# ---------------------------------------------------------------------------


def test_health_check_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen3-coder-30b"}]})

    client = make_client(handler)
    config = make_config()
    try:
        result = asyncio.run(health_check(config, client=client))
    finally:
        asyncio.run(client.aclose())
    assert result["ok"] is True


def test_health_check_not_configured():
    config = make_config(vllm_base_url=None)
    result = asyncio.run(health_check(config))
    assert result["ok"] is False


def test_health_check_http_error_does_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = make_client(handler)
    config = make_config()
    try:
        result = asyncio.run(health_check(config, client=client))
    finally:
        asyncio.run(client.aclose())
    assert result["ok"] is False


def test_health_check_sends_bearer_header_when_api_key_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer local-dev-key"
        return httpx.Response(200, json={"data": []})

    client = make_client(handler)
    config = make_config(vllm_api_key="local-dev-key")
    try:
        result = asyncio.run(health_check(config, client=client))
    finally:
        asyncio.run(client.aclose())
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# diagnose_job_failure_local()：重試上限 2（總共最多 3 次）
# ---------------------------------------------------------------------------


def test_diagnose_local_success_includes_log_tail_in_prompt():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json_module.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        assert "ModuleNotFoundError" in body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "診斷內容"}}]})

    client = make_client(handler)
    config = make_config()
    try:
        text = asyncio.run(
            diagnose_job_failure_local(
                command="python x.py",
                exit_code=1,
                log_tail="Traceback...\nModuleNotFoundError\n",
                config=config,
                client=client,
            )
        )
    finally:
        asyncio.run(client.aclose())
    assert text == "診斷內容"


def test_diagnose_local_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    client = make_client(handler)
    config = make_config()
    try:
        with pytest.raises(LLMLocalError):
            asyncio.run(
                diagnose_job_failure_local(
                    command="x",
                    exit_code=1,
                    log_tail=None,
                    config=config,
                    client=client,
                    max_retries=2,
                )
            )
    finally:
        asyncio.run(client.aclose())
    assert calls["n"] == 3  # 第一次 + 重試 2 次


def test_diagnose_local_not_configured_raises():
    config = make_config(vllm_base_url=None)
    with pytest.raises(LLMLocalError):
        asyncio.run(
            diagnose_job_failure_local(
                command="x", exit_code=1, log_tail=None, config=config
            )
        )


# ---------------------------------------------------------------------------
# summarize_mail_body_local()：任何失敗都回 None，絕不丟例外
# ---------------------------------------------------------------------------


def test_summarize_local_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "摘要"}}]})

    client = make_client(handler)
    config = make_config()
    try:
        result = asyncio.run(summarize_mail_body_local("信件內容", config, client=client))
    finally:
        asyncio.run(client.aclose())
    assert result == "摘要"


def test_summarize_local_failure_returns_none_not_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = make_client(handler)
    config = make_config()
    try:
        result = asyncio.run(summarize_mail_body_local("信件內容", config, client=client))
    finally:
        asyncio.run(client.aclose())
    assert result is None


def test_summarize_local_not_configured_returns_none():
    config = make_config(vllm_base_url=None)
    result = asyncio.run(summarize_mail_body_local("信件內容", config))
    assert result is None
