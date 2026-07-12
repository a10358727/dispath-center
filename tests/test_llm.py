"""app/llm.py：LLM 隔離層（PLAN.md F 節）。

全部用假 client（duck-typed，不需要真的裝 anthropic 套件），絕不真的呼叫
Anthropic API。`is_llm_available()`／規則式後備是純函式，其餘用
`asyncio.run()` 直接呼叫 async 函式（沿用專案既有測試風格，見
tests/test_jobfinish.py）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import AppConfig
from app.llm import (
    LLMError,
    LLMUnavailableError,
    classify_intent,
    diagnose_job_failure,
    is_llm_available,
    parse_intent_fallback,
    summarize_mail_body,
)


def make_config(**overrides) -> AppConfig:
    base = dict(servers=[], anthropic_api_key="sk-test-key")
    base.update(overrides)
    return AppConfig(**base)


# ---------------------------------------------------------------------------
# 假 anthropic client：模擬 tool_use / text 回應形狀
# ---------------------------------------------------------------------------


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
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self._responder(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responder):
        self.messages = FakeMessages(responder)


# ---------------------------------------------------------------------------
# is_llm_available()
# ---------------------------------------------------------------------------


def test_is_llm_available_false_when_no_api_key():
    config = make_config(anthropic_api_key=None)
    assert is_llm_available(config) is False


def test_is_llm_available_false_when_anthropic_not_importable(monkeypatch):
    import app.llm as llm_module

    monkeypatch.setattr(llm_module, "anthropic", None)
    config = make_config(anthropic_api_key="sk-test-key")
    assert is_llm_available(config) is False


def test_is_llm_available_true_when_package_and_key_present(monkeypatch):
    import app.llm as llm_module

    monkeypatch.setattr(llm_module, "anthropic", object())
    config = make_config(anthropic_api_key="sk-test-key")
    assert is_llm_available(config) is True


# ---------------------------------------------------------------------------
# 規則式後備：parse_intent_fallback()
# ---------------------------------------------------------------------------


def test_fallback_status_intent():
    result = parse_intent_fallback("目前狀態如何？")
    assert result["intent"] == "status"


def test_fallback_jobs_intent():
    result = parse_intent_fallback("看一下任務")
    assert result["intent"] == "jobs"


def test_fallback_enqueue_intent_strips_prefix():
    result = parse_intent_fallback("跑 echo hi")
    assert result["intent"] == "enqueue"
    assert result["command"] == "echo hi"


def test_fallback_enqueue_without_command_falls_back_to_chat():
    result = parse_intent_fallback("跑")
    assert result["intent"] == "chat"


def test_fallback_other_text_is_chat_with_help_reply():
    result = parse_intent_fallback("你好啊")
    assert result["intent"] == "chat"
    assert "狀態" in result["reply"]
    assert "任務" in result["reply"]
    assert "跑" in result["reply"]


def test_fallback_empty_text_is_chat():
    result = parse_intent_fallback("   ")
    assert result["intent"] == "chat"


# ---------------------------------------------------------------------------
# classify_intent()：tool-use 強制結構化輸出
# ---------------------------------------------------------------------------


def test_classify_intent_parses_tool_use_response():
    def responder(kwargs):
        assert kwargs["tool_choice"] == {"type": "tool", "name": "report_intent"}
        return FakeResponse(
            [
                FakeBlock(
                    "tool_use",
                    name="report_intent",
                    input={
                        "intent": "enqueue",
                        "command": "python train.py",
                        "project": "resnet50",
                        "pin_server": None,
                        "require_tag": "gpu",
                        "reply": None,
                    },
                )
            ]
        )

    client = FakeClient(responder)
    config = make_config()
    result = asyncio.run(classify_intent("幫我用 resnet50 訓練", "狀態摘要", config, client=client))
    assert result["intent"] == "enqueue"
    assert result["command"] == "python train.py"
    assert result["project"] == "resnet50"
    assert result["require_tag"] == "gpu"


def test_classify_intent_no_client_and_unavailable_raises():
    config = make_config(anthropic_api_key=None)
    with pytest.raises(LLMUnavailableError):
        asyncio.run(classify_intent("狀態", "摘要", config, client=None))


def test_classify_intent_sdk_exception_raises_llm_error():
    def responder(kwargs):
        return RuntimeError("boom")

    client = FakeClient(responder)
    config = make_config()
    with pytest.raises(LLMError):
        asyncio.run(classify_intent("狀態", "摘要", config, client=client))


def test_classify_intent_missing_tool_use_block_raises_llm_error():
    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="我不會用工具")])

    client = FakeClient(responder)
    config = make_config()
    with pytest.raises(LLMError):
        asyncio.run(classify_intent("狀態", "摘要", config, client=client))


def test_classify_intent_invalid_intent_value_raises_llm_error():
    def responder(kwargs):
        return FakeResponse(
            [FakeBlock("tool_use", name="report_intent", input={"intent": "not_a_real_intent"})]
        )

    client = FakeClient(responder)
    config = make_config()
    with pytest.raises(LLMError):
        asyncio.run(classify_intent("狀態", "摘要", config, client=client))


# ---------------------------------------------------------------------------
# diagnose_job_failure()：重試上限 2
# ---------------------------------------------------------------------------


def test_diagnose_job_failure_returns_text_on_success():
    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="可能是缺少套件，建議：\n--- a\n+++ b")])

    client = FakeClient(responder)
    config = make_config()
    result = asyncio.run(
        diagnose_job_failure(
            command="python train.py",
            exit_code=1,
            log_tail="ModuleNotFoundError: no module named foo",
            project_structure=None,
            config=config,
            client=client,
        )
    )
    assert "缺少套件" in result


def test_diagnose_job_failure_retries_then_succeeds():
    attempts = {"n": 0}

    def responder(kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient failure")
        return FakeResponse([FakeBlock("text", text="第三次成功的診斷")])

    client = FakeClient(responder)
    config = make_config()
    result = asyncio.run(
        diagnose_job_failure(
            command="x",
            exit_code=1,
            log_tail="log",
            config=config,
            client=client,
            max_retries=2,
        )
    )
    assert attempts["n"] == 3
    assert result == "第三次成功的診斷"


def test_diagnose_job_failure_exhausts_retries_raises_llm_error():
    def responder(kwargs):
        raise RuntimeError("always fails")

    client = FakeClient(responder)
    config = make_config()
    with pytest.raises(LLMError):
        asyncio.run(
            diagnose_job_failure(
                command="x", exit_code=1, log_tail="log", config=config, client=client, max_retries=2
            )
        )
    assert len(client.messages.calls) == 3  # 初次 + 最多重試 2 次


def test_diagnose_job_failure_no_key_raises_unavailable():
    config = make_config(anthropic_api_key=None)
    with pytest.raises(LLMUnavailableError):
        asyncio.run(
            diagnose_job_failure(command="x", exit_code=1, log_tail="log", config=config, client=None)
        )


# ---------------------------------------------------------------------------
# summarize_mail_body()：任何失敗都回傳 None，絕不丟例外
# ---------------------------------------------------------------------------


def test_summarize_mail_body_returns_summary_on_success():
    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="任務失敗，可能是缺少套件。")])

    client = FakeClient(responder)
    config = make_config()
    result = asyncio.run(summarize_mail_body("完整信件內容...", config, client=client))
    assert result == "任務失敗，可能是缺少套件。"


def test_summarize_mail_body_returns_none_on_sdk_failure():
    def responder(kwargs):
        return RuntimeError("boom")

    client = FakeClient(responder)
    config = make_config()
    result = asyncio.run(summarize_mail_body("body", config, client=client))
    assert result is None


def test_summarize_mail_body_returns_none_when_unavailable():
    config = make_config(anthropic_api_key=None)
    result = asyncio.run(summarize_mail_body("body", config, client=None))
    assert result is None


def test_summarize_mail_body_returns_none_on_empty_text():
    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="   ")])

    client = FakeClient(responder)
    config = make_config()
    result = asyncio.run(summarize_mail_body("body", config, client=client))
    assert result is None
