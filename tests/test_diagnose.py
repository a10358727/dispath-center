"""POST /jobs/{id}/diagnose（實作指令 5.8／PLAN.md F 節）。

只顯示診斷說明與修改建議（diff），絕不執行、不改碼、不重跑；沒有設定
ANTHROPIC_API_KEY 時回 503；任務不是 failed 狀態回 400；診斷成功/失敗都要
寫稽核 `diagnose`。用 `api_client` fixture（tests/conftest.py），LLM 走假
client，絕不真的呼叫 Anthropic API。
"""

from __future__ import annotations


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


def _create_failed_job(client, main_module, command="python train.py", project=None):
    body = {"command": command}
    if project:
        body["project"] = project
        body["type"] = "adhoc"  # 避免觸發階段 3 的 sync/setup 計畫，這裡不需要
    approval_id = client.post("/dispatch", json=body).json()["id"]
    client.post(f"/approve/{approval_id}")
    job_id = client.get("/jobs").json()[0]["id"]
    main_module.app_state.db.update_job(
        job_id, status="failed", exit_code=1, log_tail="Traceback...\nModuleNotFoundError\n"
    )
    return job_id


def _enable_fake_llm(main_module, monkeypatch, responder):
    """把 app_state 標成「LLM 可用」，並注入假 client，不需要真的裝
    anthropic 套件也不需要真的呼叫 API。"""
    import app.llm as llm_module

    monkeypatch.setattr(llm_module, "anthropic", object())
    main_module.app_state.config.anthropic_api_key = "sk-test-key"
    client = FakeClient(responder)
    main_module.app_state.llm_client = client
    return client


# ---------------------------------------------------------------------------
# 前置檢查：404 / 400 / 503
# ---------------------------------------------------------------------------


def test_diagnose_missing_job_404(api_client):
    client, _main = api_client
    resp = client.post("/jobs/9999/diagnose")
    assert resp.status_code == 404


def test_diagnose_non_failed_job_400(api_client):
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 5"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job_id = client.get("/jobs").json()[0]["id"]

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 400


def test_diagnose_no_api_key_returns_503(api_client):
    """`api_client` fixture 沒有設定 ANTHROPIC_API_KEY（也沒裝 anthropic 套
    件），is_llm_available() 恆為 False——這是「沒有 key 時前四階段功能完全
    不受影響」的驗收核心之一：診斷入口明確降級，不會炸。"""
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 成功／失敗路徑（假 LLM client）
# ---------------------------------------------------------------------------


def test_diagnose_success_returns_diagnosis_and_writes_audit(api_client, monkeypatch):
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    def responder(kwargs):
        assert "ModuleNotFoundError" in kwargs["messages"][0]["content"]
        return FakeResponse([FakeBlock("text", text="缺少套件，建議：\n-import foo\n+import bar")])

    _enable_fake_llm(main_module, monkeypatch, responder)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert "缺少套件" in body["diagnosis"]

    events = client.get("/events").json()
    diag_events = [e for e in events if e["action"] == "diagnose"]
    assert len(diag_events) == 1
    assert diag_events[0]["params"]["job_id"] == job_id
    assert diag_events[0]["params"]["success"] is True
    assert "缺少套件" in diag_events[0]["params"]["diagnosis"]


def test_diagnose_llm_failure_returns_502_and_writes_failed_audit(api_client, monkeypatch):
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    def responder(kwargs):
        return RuntimeError("simulated LLM outage")

    _enable_fake_llm(main_module, monkeypatch, responder)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 502

    events = client.get("/events").json()
    diag_events = [e for e in events if e["action"] == "diagnose"]
    assert len(diag_events) == 1
    assert diag_events[0]["params"]["success"] is False
    assert diag_events[0]["result"] == "failed"


def test_diagnose_no_project_skips_ssh_project_structure_probe(api_client, monkeypatch):
    """沒有掛專案的任務不應該嘗試 SSH 抓專案結構。"""
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module, project=None)

    async def ssh_should_not_be_called(server_name, command, timeout):
        raise AssertionError("不應該呼叫 ssh_run（任務沒有掛專案）")

    main_module.app_state.ssh_run = ssh_should_not_be_called

    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="診斷內容")])

    _enable_fake_llm(main_module, monkeypatch, responder)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 階段 7 後備：anthropic 不可用、本地 vLLM 可用 → 改用
# app.llm_local.diagnose_job_failure_local()（PLAN.md H 節）
# ---------------------------------------------------------------------------


def test_diagnose_falls_back_to_local_vllm_when_anthropic_unavailable(api_client, monkeypatch):
    """`api_client` 本來就沒有設定 ANTHROPIC_API_KEY——這裡另外把
    vllm_base_url/vllm_model 標成「可用」，驗證 503 分支改走
    `diagnose_job_failure_local()`，不再直接回 503。"""
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    main_module.app_state.config.vllm_base_url = "http://127.0.0.1:8001/v1"
    main_module.app_state.config.vllm_model = "qwen3-coder-30b"

    calls = []

    async def fake_diagnose_local(**kwargs):
        calls.append(kwargs)
        assert kwargs["command"] == "python train.py"
        return "本地模型診斷內容"

    monkeypatch.setattr(main_module, "diagnose_job_failure_local", fake_diagnose_local)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 200
    assert resp.json()["diagnosis"] == "本地模型診斷內容"
    assert len(calls) == 1

    events = client.get("/events").json()
    diag_events = [e for e in events if e["action"] == "diagnose"]
    assert len(diag_events) == 1
    assert diag_events[0]["params"]["success"] is True


def test_diagnose_local_vllm_failure_returns_502(api_client, monkeypatch):
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    main_module.app_state.config.vllm_base_url = "http://127.0.0.1:8001/v1"
    main_module.app_state.config.vllm_model = "qwen3-coder-30b"

    async def fake_diagnose_local(**kwargs):
        raise main_module.LLMLocalError("本地模型連不上")

    monkeypatch.setattr(main_module, "diagnose_job_failure_local", fake_diagnose_local)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 502

    events = client.get("/events").json()
    diag_events = [e for e in events if e["action"] == "diagnose"]
    assert len(diag_events) == 1
    assert diag_events[0]["params"]["success"] is False


def test_diagnose_still_503_when_neither_anthropic_nor_vllm_available(api_client):
    """兩者都沒設定（`api_client` fixture 的天然狀態）時，維持既有 503 行為。"""
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 503
    assert "VLLM_BASE_URL" in resp.json()["detail"]


def test_diagnose_prefers_anthropic_over_vllm_when_both_available(api_client, monkeypatch):
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module)

    main_module.app_state.config.vllm_base_url = "http://127.0.0.1:8001/v1"
    main_module.app_state.config.vllm_model = "qwen3-coder-30b"

    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="anthropic 診斷內容")])

    _enable_fake_llm(main_module, monkeypatch, responder)

    async def local_should_not_be_called(**kwargs):
        raise AssertionError("anthropic 可用時不應該呼叫本地 vLLM 診斷")

    monkeypatch.setattr(main_module, "diagnose_job_failure_local", local_should_not_be_called)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 200
    assert resp.json()["diagnosis"] == "anthropic 診斷內容"


def test_diagnose_ssh_probe_failure_does_not_block_diagnosis(api_client, monkeypatch):
    """有掛專案、目標機在線，但 SSH 抓專案結構失敗（best effort）不影響診
    斷本身能不能跑完。"""
    client, main_module = api_client
    job_id = _create_failed_job(client, main_module, project=None)
    main_module.app_state.db.update_job(job_id, project="demo", server="server-a")
    main_module.app_state.server_states["server-a"] = main_module.ServerState(
        name="server-a", online=True
    )

    async def ssh_fails(server_name, command, timeout):
        raise ConnectionError("unreachable")

    main_module.app_state.ssh_run = ssh_fails

    def responder(kwargs):
        return FakeResponse([FakeBlock("text", text="診斷內容")])

    _enable_fake_llm(main_module, monkeypatch, responder)

    resp = client.post(f"/jobs/{job_id}/diagnose")
    assert resp.status_code == 200
