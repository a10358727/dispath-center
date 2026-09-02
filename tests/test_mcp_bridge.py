"""app/mcp_bridge.py：MCP Bridge（PLAN.md J 節，階段 9）。

全部用 `httpx.MockTransport`（bridge -> 調度中心）與 `httpx.ASGITransport`
（測試 client -> bridge 本身）驅動，絕不依賴真實網路、真實調度中心或真實
ChatGPT。跟既有 test_llm_local.py 一樣，測試函式維持同步（`def test_x():`
內部用 `asyncio.run()`），因為專案沒有設定 pytest-asyncio 的
auto/strict marker，避免額外引入設定檔。

涵蓋範圍：
- `load_bridge_config()`：`MCP_BRIDGE_PATH_SECRET` 未設 -> SystemExit；
  有設定時各項預設值/覆蓋值正確讀入。
- 路徑機密 gate：正確路徑通、錯誤路徑 404、根路徑 404。
- 選配 bearer gate：帶錯 token -> 401；帶對 -> 通；沒帶（路徑對）-> 通。
- 10 個唯讀工具：mock 調度中心回應 -> 斷言輸出形狀；limit/lines/n 的
  clamp；調度中心 500/連線失敗/非 JSON -> 回傳錯誤文字而不拋例外。
- 截斷：超長回應被截斷並註明省略字數。
- MCP 協議層：至少各打通一次 tools/list 與 tools/call。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.audit import read_audit
from app.authentication import LEGACY_ADMIN_ACTOR_ID
from app.mcp_bridge import (
    _MAX_RESULT_CHARS,
    MCP_TOOL_ACTIONS,
    BridgeConfig,
    _clamp_int,
    _truncate,
    create_app,
    load_bridge_config,
)

SECRET = "unit-test-path-secret-0123456789abcdef"


def make_config(**overrides: Any) -> BridgeConfig:
    base = dict(
        dispatch_base_url="http://127.0.0.1:8888",
        auth_token=None,
        port=8890,
        path_secret=SECRET,
        bridge_token=None,
    )
    base.update(overrides)
    return BridgeConfig(**base)


# ---------------------------------------------------------------------------
# 驅動 ASGI lifespan 事件的最小測試工具（見 docstring 內詳細說明：
# httpx.ASGITransport 不會自動送 lifespan 事件，但 FastMCP 的 streamable-http
# session manager 需要靠 lifespan startup 啟動內部 task group）
# ---------------------------------------------------------------------------


class _LifespanRunner:
    def __init__(self, app: Callable) -> None:
        self.app = app
        self._startup_complete = anyio.Event()
        self._shutdown_complete = anyio.Event()
        self._please_shutdown = anyio.Event()
        self._tg: Any = None

    async def _run(self) -> None:
        async def receive() -> dict:
            if not self._startup_complete.is_set():
                return {"type": "lifespan.startup"}
            await self._please_shutdown.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message: dict) -> None:
            if message["type"] == "lifespan.startup.complete":
                self._startup_complete.set()
            elif message["type"] == "lifespan.shutdown.complete":
                self._shutdown_complete.set()

        await self.app({"type": "lifespan"}, receive, send)

    async def __aenter__(self) -> "_LifespanRunner":
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._tg.start_soon(self._run)
        await self._startup_complete.wait()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        self._please_shutdown.set()
        await self._shutdown_complete.wait()
        await self._tg.__aexit__(*exc_info)


async def _with_session(
    config: BridgeConfig,
    dispatch_handler: Callable[[httpx.Request], httpx.Response],
    coro_fn: Callable[[ClientSession], Any],
    *,
    extra_headers: Optional[dict] = None,
):
    """建立一個完整的 MCP session（含 lifespan、mock 過的調度中心
    client），呼叫 `coro_fn(session)` 並回傳其結果；用完保證清理。"""
    dispatch_client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch_handler))
    app = create_app(config, http_client=dispatch_client)

    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url=f"http://127.0.0.1:{config.port}",
        headers=extra_headers or None,
    )

    try:
        async with _LifespanRunner(app):
            url = f"http://127.0.0.1:{config.port}{config.mcp_path}"
            async with streamable_http_client(url=url, http_client=test_http_client) as (
                read,
                write,
                _get_session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await coro_fn(session)
    finally:
        await test_http_client.aclose()
        await dispatch_client.aclose()


async def _call_tool(
    config: BridgeConfig,
    dispatch_handler: Callable[[httpx.Request], httpx.Response],
    tool_name: str,
    args: Optional[dict] = None,
    *,
    extra_headers: Optional[dict] = None,
):
    async def fn(session: ClientSession):
        return await session.call_tool(tool_name, args or {})

    return await _with_session(config, dispatch_handler, fn, extra_headers=extra_headers)


def _tool_text(result) -> str:
    assert result.content, "tool 回傳內容不應該是空的"
    assert result.content[0].type == "text"
    return result.content[0].text


# ---------------------------------------------------------------------------
# load_bridge_config()
# ---------------------------------------------------------------------------


def test_load_bridge_config_missing_path_secret_raises_systemexit(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_BRIDGE_PATH_SECRET", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        load_bridge_config(dotenv_path=tmp_path / "does-not-exist.env")
    assert "MCP_BRIDGE_PATH_SECRET" in str(exc_info.value)


def test_load_bridge_config_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPATCH_BASE_URL", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_PORT", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DISPATCH_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_PATH_SECRET", "abc123")
    config = load_bridge_config(dotenv_path=tmp_path / "does-not-exist.env")
    assert config.dispatch_base_url == "http://127.0.0.1:8888"
    assert config.port == 8890
    assert config.path_secret == "abc123"
    assert config.bridge_token is None
    assert config.auth_token is None
    assert config.dispatch_service_token is None
    assert config.mcp_path == "/mcp-abc123"


def test_load_bridge_config_reads_dotenv_file(monkeypatch, tmp_path):
    for key in (
        "DISPATCH_BASE_URL",
        "MCP_BRIDGE_PORT",
        "MCP_BRIDGE_PATH_SECRET",
        "MCP_BRIDGE_TOKEN",
        "AUTH_TOKEN",
        "DISPATCH_SERVICE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment line, ignored",
                "",
                "DISPATCH_BASE_URL=http://127.0.0.1:9999",
                "MCP_BRIDGE_PORT=1234",
                "MCP_BRIDGE_PATH_SECRET=from-dotenv-secret",
                "MCP_BRIDGE_TOKEN=shh",
                "AUTH_TOKEN=dispatch-token",
                "DISPATCH_SERVICE_TOKEN=dcs_service-token",
            ]
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(dotenv_path=env_file)
    assert config.dispatch_base_url == "http://127.0.0.1:9999"
    assert config.port == 1234
    assert config.path_secret == "from-dotenv-secret"
    assert config.bridge_token == "shh"
    assert config.auth_token == "dispatch-token"
    assert config.dispatch_service_token == "dcs_service-token"


def test_load_bridge_config_real_env_overrides_dotenv(monkeypatch, tmp_path):
    """既有 app/config.py 的 `_load_dotenv()` 慣例：真正的環境變數優先於
    .env 檔內容——這裡複製過來的版本必須維持一樣的行為。"""
    env_file = tmp_path / ".env"
    env_file.write_text("MCP_BRIDGE_PATH_SECRET=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("MCP_BRIDGE_PATH_SECRET", "from-real-env")
    config = load_bridge_config(dotenv_path=env_file)
    assert config.path_secret == "from-real-env"


# ---------------------------------------------------------------------------
# 兩層認證 gate（HTTP 層，不需要跑完整 MCP 協議握手）
# ---------------------------------------------------------------------------


def test_root_path_returns_404():
    config = make_config()
    app = create_app(config)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8890") as client:
            resp = await client.get("/")
            assert resp.status_code == 404

    asyncio.run(run())


def test_wrong_path_secret_returns_404_not_401():
    config = make_config()
    app = create_app(config)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8890") as client:
            resp = await client.post("/mcp-totally-wrong-secret", json={})
            assert resp.status_code == 404

    asyncio.run(run())


def test_wrong_bearer_token_returns_401():
    config = make_config(bridge_token="right-token")
    app = create_app(config)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8890") as client:
            resp = await client.post(
                config.mcp_path,
                headers={"Authorization": "Bearer wrong-token"},
                json={},
            )
            assert resp.status_code == 401

    asyncio.run(run())


def test_missing_bearer_passes_when_path_secret_correct():
    """MCP_BRIDGE_TOKEN 沒設定時，即使不帶 Authorization header，只要路徑
    機密對就放行（相容 ChatGPT connector 的 no-auth 模式）。用
    tools/list 打通當作「有放行到底層 MCP 處理」的證明。"""
    config = make_config(bridge_token=None)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    async def run():
        return await _with_session(config, handler, fn)

    tools = asyncio.run(run())
    assert len(tools.tools) == len(MCP_TOOL_ACTIONS)


def test_correct_bearer_passes():
    config = make_config(bridge_token="right-token")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    async def run():
        return await _with_session(
            config, handler, fn, extra_headers={"Authorization": "Bearer right-token"}
        )

    tools = asyncio.run(run())
    assert len(tools.tools) == len(MCP_TOOL_ACTIONS)


# ---------------------------------------------------------------------------
# tools/list：沒有 approve/reject 工具（鐵律，見模組 docstring）；PLAN.md
# J.2 節新增的兩個寫入工具 annotations 正確
# ---------------------------------------------------------------------------

READ_ONLY_TOOL_NAMES = [
    "get_servers",
    "list_jobs",
    "get_job",
    "get_job_log",
    "list_approvals",
    "list_events",
    "list_projects",
    "list_datasets",
    "list_project_candidates",
    "get_project_candidate",
    "get_project_activity",
    # 階段 12（PLAN.md M.1 節）：兩個唯讀讀檔工具。
    "list_project_files",
    "read_project_file",
    # 階段 13（PLAN.md N.6 節，Codex Worker v2）：三個唯讀工具。
    "get_codex_runner_status",
    "list_coding_runs",
    "get_coding_run",
    # 階段 15 Phase A（PLAN.md P.1.3 節）：一個唯讀工具。
    "get_projects_matrix",
    # 階段 16（PLAN.md Q.3 節，資料卡）：一個唯讀工具。
    "get_dataset_card",
    # 專案詳情頁計畫第 4 節：一個唯讀工具（三源時間軸合併）。
    "get_project_timeline",
]

WRITE_TOOL_NAMES = [
    "request_enqueue_job",
    "request_stop_job",
    # 階段 12（PLAN.md M.2 節）：只建 pending approval，絕不直接套用。
    "request_apply_patch",
    # 階段 13（PLAN.md N.1 節）：只建 pending approval，絕不直接派工。
    "request_coding_task",
    # 專案詳情頁計畫第 4 節：**不建立 approval，直接寫入**（annotations
    # 數值跟其餘寫入工具一樣是 readOnlyHint=False/destructiveHint=False，
    # 但語意是「已經寫入」而不是「等待核准」，見
    # `tests/test_mcp_bridge.py` 下方 add_experiment_record/
    # update_project_doc 專屬測試對這個差異的斷言，以及
    # `app/mcp_bridge.py` 模組層註解的完整說明）。
    "add_experiment_record",
    "update_project_doc",
]


def test_tools_list_has_exactly_twentyfive_tools_no_approve_reject():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    tools = asyncio.run(_with_session(config, handler, fn))
    names = sorted(t.name for t in tools.tools)
    assert names == sorted(READ_ONLY_TOOL_NAMES + WRITE_TOOL_NAMES)
    assert not any("approve" in n.lower() or "reject" in n.lower() for n in names)


def test_read_only_tools_have_readonlyhint_true():
    """PLAN.md J.2 節：既有 10 個唯讀工具全部補標 `readOnlyHint=True`。"""
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    tools = asyncio.run(_with_session(config, handler, fn))
    by_name = {t.name: t for t in tools.tools}
    for name in READ_ONLY_TOOL_NAMES:
        annotations = by_name[name].annotations
        assert annotations is not None, f"{name} 缺少 annotations"
        assert annotations.readOnlyHint is True, f"{name} 應該是 readOnlyHint=True"


def test_write_tools_have_non_readonly_non_destructive_hints():
    """PLAN.md J.2 節：兩個新的寫入工具標 `readOnlyHint=False,
    destructiveHint=False`——建立 pending approval 本身不具破壞性，但也不是
    唯讀查詢，讓 ChatGPT 端對這兩個工具跳原生確認框。"""
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    tools = asyncio.run(_with_session(config, handler, fn))
    by_name = {t.name: t for t in tools.tools}
    for name in WRITE_TOOL_NAMES:
        annotations = by_name[name].annotations
        assert annotations is not None, f"{name} 缺少 annotations"
        assert annotations.readOnlyHint is False, f"{name} 應該是 readOnlyHint=False"
        assert annotations.destructiveHint is False, f"{name} 應該是 destructiveHint=False"


# ---------------------------------------------------------------------------
# 各工具：輸出形狀
# ---------------------------------------------------------------------------


def test_get_servers_shape():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json=[{"name": "a1", "online": True}])

    result = asyncio.run(_call_tool(config, handler, "get_servers"))
    assert captured["path"] == "/servers"
    assert json.loads(_tool_text(result)) == [{"name": "a1", "online": True}]


def test_get_job_shape_and_path():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": 42, "status": "running"})

    result = asyncio.run(_call_tool(config, handler, "get_job", {"job_id": 42}))
    assert captured["path"] == "/jobs/42"
    assert json.loads(_tool_text(result)) == {"id": 42, "status": "running"}


def test_get_job_log_default_lines_and_clamp():
    config = make_config()
    seen_lines = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_lines.append(request.url.params.get("lines"))
        return httpx.Response(200, json={"job_id": 1, "log_tail": "hello"})

    # 沒給 lines -> 預設 40
    asyncio.run(_call_tool(config, handler, "get_job_log", {"job_id": 1}))
    assert seen_lines[-1] == "40"

    # 超過上限 80 -> clamp 到 80
    asyncio.run(_call_tool(config, handler, "get_job_log", {"job_id": 1, "lines": 999}))
    assert seen_lines[-1] == "80"

    # 合法值原樣通過
    asyncio.run(_call_tool(config, handler, "get_job_log", {"job_id": 1, "lines": 10}))
    assert seen_lines[-1] == "10"


def test_list_jobs_default_limit_and_recent_slice():
    config = make_config()
    jobs = [{"id": i, "status": "done"} for i in range(1, 26)]  # 25 筆，ASC

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jobs)

    result = asyncio.run(_call_tool(config, handler, "list_jobs"))
    data = json.loads(_tool_text(result))
    assert len(data) == 20
    assert [d["id"] for d in data] == list(range(6, 26))


def test_list_jobs_limit_clamped_to_20():
    config = make_config()
    jobs = [{"id": i} for i in range(1, 31)]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jobs)

    result = asyncio.run(_call_tool(config, handler, "list_jobs", {"limit": 999}))
    data = json.loads(_tool_text(result))
    assert len(data) == 20
    assert [d["id"] for d in data] == list(range(11, 31))


def test_list_jobs_small_limit_and_status_forwarded():
    config = make_config()
    jobs = [{"id": i} for i in range(1, 11)]
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["status"] = request.url.params.get("status")
        return httpx.Response(200, json=jobs)

    result = asyncio.run(_call_tool(config, handler, "list_jobs", {"limit": 3, "status": "running"}))
    data = json.loads(_tool_text(result))
    assert len(data) == 3
    assert [d["id"] for d in data] == [8, 9, 10]
    assert captured["status"] == "running"


def test_list_jobs_project_forwarded_and_combinable_with_status():
    """階段 11（PLAN.md L.3）：`project` 選填參數轉呼叫 `GET /jobs?project=`，
    可與 `status` 並用；都省略時完全不帶這兩個 query 參數。"""
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["status"] = request.url.params.get("status")
        captured["project"] = request.url.params.get("project")
        return httpx.Response(200, json=[{"id": 1, "project": "proj-a"}])

    asyncio.run(
        _call_tool(
            config, handler, "list_jobs", {"status": "running", "project": "proj-a"}
        )
    )
    assert captured["status"] == "running"
    assert captured["project"] == "proj-a"

    asyncio.run(_call_tool(config, handler, "list_jobs"))
    assert captured["status"] is None
    assert captured["project"] is None


def test_list_approvals_status_forwarded_and_omitted():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["status"] = request.url.params.get("status")
        return httpx.Response(200, json=[{"id": 1, "status": "pending"}])

    asyncio.run(_call_tool(config, handler, "list_approvals", {"status": "pending"}))
    assert captured["status"] == "pending"

    asyncio.run(_call_tool(config, handler, "list_approvals"))
    assert captured["status"] is None


def test_list_events_default_and_clamp():
    config = make_config()
    seen_n = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_n.append(request.url.params.get("n"))
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "list_events"))
    assert seen_n[-1] == "50"

    asyncio.run(_call_tool(config, handler, "list_events", {"n": 500}))
    assert seen_n[-1] == "50"

    asyncio.run(_call_tool(config, handler, "list_events", {"n": 5}))
    assert seen_n[-1] == "5"


def test_list_projects_shape():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/projects"
        return httpx.Response(200, json=[{"name": "proj-a"}])

    result = asyncio.run(_call_tool(config, handler, "list_projects"))
    assert json.loads(_tool_text(result)) == [{"name": "proj-a"}]


def test_list_datasets_shape():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datasets"
        return httpx.Response(200, json=[{"name": "ds-a", "version": "v1"}])

    result = asyncio.run(_call_tool(config, handler, "list_datasets"))
    assert json.loads(_tool_text(result)) == [{"name": "ds-a", "version": "v1"}]


def test_list_project_candidates_params_forwarded():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["server"] = request.url.params.get("server")
        captured["status"] = request.url.params.get("status")
        return httpx.Response(200, json=[{"id": "c1"}])

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "list_project_candidates",
            {"server": "gpu1", "status": "pending"},
        )
    )
    assert captured["path"] == "/inventory/candidates"
    assert captured["server"] == "gpu1"
    assert captured["status"] == "pending"
    assert json.loads(_tool_text(result)) == [{"id": "c1"}]


def test_list_project_candidates_no_params():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.params)
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "list_project_candidates"))
    assert captured["query"] == ""


def test_get_project_candidate_shape_and_path():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "cand-1", "server": "gpu1"})

    result = asyncio.run(
        _call_tool(config, handler, "get_project_candidate", {"candidate_id": "cand-1"})
    )
    assert captured["path"] == "/inventory/candidates/cand-1"
    assert json.loads(_tool_text(result)) == {"id": "cand-1", "server": "gpu1"}


def test_get_project_activity_shape_and_path():
    """階段 11（PLAN.md L 節）：`get_project_activity` 原樣轉呼叫
    `GET /projects/{name}/activity`，標 `readOnlyHint=True`（見
    `READ_ONLY_TOOL_NAMES`），輸出截斷同其他工具（超長內容會被
    `_truncate()` 截斷並註明省略字數，這裡只驗證正常大小回應原樣轉述）。"""
    config = make_config()
    captured = {}
    fake_activity = {
        "project": {"name": "proj-a"},
        "instances": [{"server": "gpu1", "path": "/data/proj-a"}],
        "server_states": {"gpu1": {"online": True, "gpu_util_max": 10.0, "disk_avail_bytes": 999}},
        "recent_jobs": [{"id": 1, "status": "done", "exit_code": 0, "duration_seconds": 30}],
        "latest_job_log_tail": "training done\n",
        "activity": [
            {
                "instance_id": "abc123",
                "server": "gpu1",
                "path": "/data/proj-a",
                "recent_files": [{"path": "train.log", "mtime_epoch": 1700000000.0}],
                "log_tails": {"train.log": "epoch 1 loss=0.5\n"},
            }
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json=fake_activity)

    result = asyncio.run(_call_tool(config, handler, "get_project_activity", {"project_name": "proj-a"}))
    assert captured["path"] == "/projects/proj-a/activity"
    assert json.loads(_tool_text(result)) == fake_activity


def test_get_project_activity_not_found_returns_error_text():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "專案 nope 不存在"})

    result = asyncio.run(_call_tool(config, handler, "get_project_activity", {"project_name": "nope"}))
    text = _tool_text(result)
    assert "ERROR" in text
    assert "404" in text


# ---------------------------------------------------------------------------
# 階段 12（PLAN.md M.1 節）：list_project_files／read_project_file
# ---------------------------------------------------------------------------


def test_list_project_files_shape_and_params_forwarded():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["server"] = request.url.params.get("server")
        captured["subdir"] = request.url.params.get("subdir")
        return httpx.Response(200, json={"server": "gpu1", "path": "/data/proj-a", "files": ["train.py"]})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "list_project_files",
            {"project_name": "proj-a", "server": "gpu1", "subdir": "scripts"},
        )
    )
    assert captured["path"] == "/projects/proj-a/files"
    assert captured["server"] == "gpu1"
    assert captured["subdir"] == "scripts"
    assert json.loads(_tool_text(result))["files"] == ["train.py"]


def test_list_project_files_no_optional_params():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.params)
        return httpx.Response(200, json={"files": []})

    asyncio.run(_call_tool(config, handler, "list_project_files", {"project_name": "proj-a"}))
    assert captured["query"] == ""


def test_read_project_file_shape_and_path_param():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["file_path"] = request.url.params.get("path")
        captured["server"] = request.url.params.get("server")
        return httpx.Response(200, json={"content": "print('hi')\n"})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "read_project_file",
            {"project_name": "proj-a", "file_path": "train.py", "server": "gpu1"},
        )
    )
    assert captured["path"] == "/projects/proj-a/file"
    assert captured["file_path"] == "train.py"
    assert captured["server"] == "gpu1"
    assert json.loads(_tool_text(result))["content"] == "print('hi')\n"


def test_read_project_file_rejected_path_returns_error_text():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "檔案路徑不可包含 .. 片段"})

    result = asyncio.run(
        _call_tool(
            config, handler, "read_project_file", {"project_name": "proj-a", "file_path": "../x"}
        )
    )
    text = _tool_text(result)
    assert "REQUEST REJECTED" in text
    assert ".." in text


# ---------------------------------------------------------------------------
# PLAN.md J.2 節：兩個「只建 pending approval」的寫入工具
# ---------------------------------------------------------------------------


def test_request_enqueue_job_success_posts_to_dispatch_with_auth_header():
    config = make_config(auth_token="dispatch-secret")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["header"] = request.headers.get("x-auth-token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": 5,
                "kind": "enqueue",
                "status": "pending",
                "payload": captured["body"],
                "created_at": "2026-07-09T00:00:00",
                "decided_at": None,
                "note": None,
            },
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "request_enqueue_job",
            {
                "command": "python train.py",
                "type": "train",
                "project": "proj-a",
                "pin_server": "gpu1",
                "require_tag": "gpu",
                "priority": "high",
            },
        )
    )
    assert captured["path"] == "/dispatch"
    assert captured["method"] == "POST"
    assert captured["header"] == "dispatch-secret"
    assert captured["body"] == {
        "command": "python train.py",
        "source": "chatgpt",
        "type": "train",
        "project": "proj-a",
        "pin_server": "gpu1",
        "require_tag": "gpu",
        "priority": "high",
    }

    text = _tool_text(result)
    assert "PENDING APPROVAL" in text
    data = json.loads(text)
    assert data["approval"]["id"] == 5
    assert data["approval"]["status"] == "pending"


def test_request_enqueue_job_omits_optional_fields_when_not_given():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"id": 1, "kind": "enqueue", "status": "pending", "payload": {}}
        )

    asyncio.run(_call_tool(config, handler, "request_enqueue_job", {"command": "echo hi"}))
    assert captured["body"] == {"command": "echo hi", "source": "chatgpt"}


def test_request_enqueue_job_dangerous_command_400_relayed_verbatim():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"detail": "指令被拒絕：偵測到危險指令 rm -rf /"}
        )

    result = asyncio.run(
        _call_tool(config, handler, "request_enqueue_job", {"command": "rm -rf /"})
    )
    assert result.isError is False
    text = _tool_text(result)
    assert "指令被拒絕" in text
    assert "rm -rf /" in text
    # 危險指令一律連 approval 都不建立，回傳裡不該出現任何 approval id/kind。
    assert "PENDING APPROVAL" not in text


def test_request_stop_job_success_posts_to_correct_path():
    config = make_config(auth_token="dispatch-secret")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["header"] = request.headers.get("x-auth-token")
        return httpx.Response(
            200, json={"id": 9, "kind": "stop", "status": "pending", "payload": {"job_id": 42}}
        )

    result = asyncio.run(_call_tool(config, handler, "request_stop_job", {"job_id": 42}))
    assert captured["path"] == "/jobs/42/stop"
    assert captured["method"] == "POST"
    assert captured["header"] == "dispatch-secret"
    text = _tool_text(result)
    assert "PENDING APPROVAL" in text
    data = json.loads(text)
    assert data["approval"]["id"] == 9


def test_request_stop_job_not_found_404_relayed():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "job 999 not found"})

    result = asyncio.run(_call_tool(config, handler, "request_stop_job", {"job_id": 999}))
    assert result.isError is False
    text = _tool_text(result)
    assert "job 999 not found" in text
    assert "PENDING APPROVAL" not in text


def test_request_stop_job_not_running_400_relayed():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"detail": "job 7 is not running (status=done)，無法停止"}
        )

    result = asyncio.run(_call_tool(config, handler, "request_stop_job", {"job_id": 7}))
    assert result.isError is False
    text = _tool_text(result)
    assert "not running" in text
    assert "PENDING APPROVAL" not in text


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.5）：source="chatgpt" 標記＋auto_approved 如實轉述
# ---------------------------------------------------------------------------


def test_request_enqueue_job_posts_source_chatgpt():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"id": 1, "kind": "enqueue", "status": "pending", "payload": {}}
        )

    asyncio.run(_call_tool(config, handler, "request_enqueue_job", {"command": "echo hi"}))
    assert captured["body"]["source"] == "chatgpt"


def test_request_stop_job_posts_source_chatgpt():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"id": 9, "kind": "stop", "status": "pending", "payload": {"job_id": 42}}
        )

    asyncio.run(_call_tool(config, handler, "request_stop_job", {"job_id": 42}))
    assert captured["body"] == {"source": "chatgpt"}


def test_request_enqueue_job_auto_approved_response_says_queued_not_running():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "auto_approved": True,
                "approval": {"id": 5, "kind": "enqueue", "status": "approved"},
                "job": {"id": 12, "status": "queued", "command": "ls -la"},
            },
        )

    result = asyncio.run(
        _call_tool(config, handler, "request_enqueue_job", {"command": "ls -la"})
    )
    assert result.isError is False
    text = _tool_text(result)
    assert "AUTO-APPROVED" in text
    assert "PENDING APPROVAL" not in text
    data = json.loads(text)
    assert data["job"]["id"] == 12
    assert data["approval"]["id"] == 5


def test_request_enqueue_job_pending_response_unchanged_when_no_auto_approved_key():
    """調度中心沒有回 auto_approved 欄位（既有行為／規則沒命中）時，這個
    工具的回應形狀完全跟階段 10 之前一樣——既有測試套件的假設不用改。"""
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": 1, "kind": "enqueue", "status": "pending", "payload": {}}
        )

    result = asyncio.run(
        _call_tool(config, handler, "request_enqueue_job", {"command": "echo hi"})
    )
    text = _tool_text(result)
    assert "PENDING APPROVAL" in text
    assert "AUTO-APPROVED" not in text


def test_request_stop_job_auto_approved_response_keeps_nonterminal_status():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "auto_approved": True,
                "approval": {"id": 8, "kind": "stop", "status": "approved"},
                "job": {"id": 42, "status": "running"},
            },
        )

    result = asyncio.run(_call_tool(config, handler, "request_stop_job", {"job_id": 42}))
    assert result.isError is False
    text = _tool_text(result)
    assert "AUTO-APPROVED" in text
    assert "PENDING APPROVAL" not in text
    data = json.loads(text)
    assert data["job"]["status"] == "running"


def test_write_tool_connection_failure_returns_error_text_not_exception():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = asyncio.run(
        _call_tool(config, handler, "request_enqueue_job", {"command": "echo hi"})
    )
    assert result.isError is False
    text = _tool_text(result)
    assert text.startswith("ERROR:")


# ---------------------------------------------------------------------------
# 階段 12（PLAN.md M.2 節）：request_apply_patch——只建 pending approval，
# 永遠不會 auto_approved（apply_patch 不在 maybe_auto_approve() 的 kind
# 白名單裡，所以調度中心這個端點永遠只會回傳單純的 approval dict，不會有
# `auto_approved` 欄位；這裡的 handler 不需要、也沒有處理該欄位的分支）。
# ---------------------------------------------------------------------------


def test_request_apply_patch_posts_diff_and_returns_pending_status():
    config = make_config(auth_token="dispatch-secret")
    captured = {}
    diff_text = "--- a/train.py\n+++ b/train.py\n@@ -1 +1 @@\n-old\n+new\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["header"] = request.headers.get("x-auth-token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": 9,
                "kind": "apply_patch",
                "status": "pending",
                "payload": captured["body"],
                "created_at": "2026-07-09T00:00:00",
                "decided_at": None,
                "note": None,
            },
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "request_apply_patch",
            {
                "project": "proj-a",
                "server": "gpu1",
                "diff": diff_text,
                "description": "fix off-by-one",
            },
        )
    )
    assert captured["path"] == "/projects/proj-a/apply-patch-request"
    assert captured["method"] == "POST"
    assert captured["header"] == "dispatch-secret"
    assert captured["body"] == {
        "server": "gpu1",
        "diff": diff_text,
        "description": "fix off-by-one",
    }
    text = _tool_text(result)
    assert "PENDING APPROVAL" in text
    assert "never be auto-approved" in text
    data = json.loads(text)
    assert data["approval"]["status"] == "pending"


def test_request_apply_patch_omits_description_when_not_given():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "status": "pending"})

    asyncio.run(
        _call_tool(
            config,
            handler,
            "request_apply_patch",
            {"project": "proj-a", "server": "gpu1", "diff": "diff --git a/x b/x\n"},
        )
    )
    assert "description" not in captured["body"]


def test_request_apply_patch_rejected_diff_400_relayed_verbatim():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "diff 內容不像 unified diff 格式"})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "request_apply_patch",
            {"project": "proj-a", "server": "gpu1", "diff": "not a diff"},
        )
    )
    text = _tool_text(result)
    assert "REQUEST REJECTED" in text
    assert "unified diff" in text


# ---------------------------------------------------------------------------
# 階段 13（PLAN.md N.1 節）：request_coding_task——只建 pending approval，
# 永遠不會 auto_approved（coding_task 不在 maybe_auto_approve() 的 kind
# 白名單裡，理由同 request_apply_patch）。
# ---------------------------------------------------------------------------


def test_request_coding_task_posts_instruction_and_returns_pending_status():
    config = make_config(auth_token="dispatch-secret")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["header"] = request.headers.get("x-auth-token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": 9,
                "kind": "coding_task",
                "status": "pending",
                "payload": captured["body"],
                "created_at": "2026-07-10T00:00:00",
                "decided_at": None,
                "note": None,
            },
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "request_coding_task",
            {
                "project": "proj-a",
                "instruction": "add a --dry-run flag to train.py",
                "base_branch": "main",
            },
        )
    )
    assert captured["path"] == "/projects/proj-a/coding-task-request"
    assert captured["method"] == "POST"
    assert captured["header"] == "dispatch-secret"
    #: v2（PLAN.md N.2）：不再讓呼叫端選 Codex 執行機器，POST body 不帶
    #: `server`——Runner 固定由調度中心的 .env 決定。
    assert captured["body"] == {
        "instruction": "add a --dry-run flag to train.py",
        "base_branch": "main",
    }
    text = _tool_text(result)
    assert "PENDING APPROVAL" in text
    assert "never be auto-approved" in text
    data = json.loads(text)
    assert data["approval"]["status"] == "pending"


def test_request_coding_task_omits_base_branch_when_not_given():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "status": "pending"})

    asyncio.run(
        _call_tool(
            config,
            handler,
            "request_coding_task",
            {"project": "proj-a", "instruction": "fix the bug"},
        )
    )
    assert "base_branch" not in captured["body"]
    assert "server" not in captured["body"]


def test_request_coding_task_rejected_instruction_400_relayed_verbatim():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "instruction 不可為空"})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "request_coding_task",
            {"project": "proj-a", "instruction": ""},
        )
    )
    text = _tool_text(result)
    assert "REQUEST REJECTED" in text
    assert "instruction 不可為空" in text


def test_request_coding_task_forwards_validation_target():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "status": "pending"})

    asyncio.run(
        _call_tool(
            config,
            handler,
            "request_coding_task",
            {
                "project": "proj-a",
                "instruction": "fix the bug",
                "validation_target": "server-a",
            },
        )
    )
    assert captured["body"] == {
        "instruction": "fix the bug",
        "validation_target": "server-a",
    }


def test_request_enqueue_job_forwards_source_coding_run_id():
    """PLAN.md N.7：request_enqueue_job 加選填 source_coding_run_id 透傳
    給 POST /dispatch；沒帶時完全不出現在 body（向下相容既有呼叫）。"""
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "status": "pending"})

    asyncio.run(
        _call_tool(
            config,
            handler,
            "request_enqueue_job",
            {"command": "echo hi", "source_coding_run_id": 7},
        )
    )
    assert captured["body"]["source_coding_run_id"] == 7

    captured.clear()
    asyncio.run(_call_tool(config, handler, "request_enqueue_job", {"command": "echo hi"}))
    assert "source_coding_run_id" not in captured["body"]


# ---------------------------------------------------------------------------
# 階段 13（PLAN.md N.6 節，Codex Worker v2）：三個新唯讀工具。
# ---------------------------------------------------------------------------


def test_get_codex_runner_status_maps_to_status_endpoint():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"configured": False})

    result = asyncio.run(_call_tool(config, handler, "get_codex_runner_status"))
    assert captured["path"] == "/codex-runner/status"
    data = json.loads(_tool_text(result))
    assert data == {"configured": False}


def test_list_coding_runs_default_limit_and_params_forwarded():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"id": 1, "status": "done"}])

    result = asyncio.run(
        _call_tool(
            config, handler, "list_coding_runs", {"status": "done", "project": "proj-a"}
        )
    )
    assert captured["path"] == "/coding-runs"
    assert captured["params"] == {"status": "done", "project": "proj-a", "limit": "20"}
    data = json.loads(_tool_text(result))
    assert data == [{"id": 1, "status": "done"}]


def test_list_coding_runs_limit_clamped_to_20():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "list_coding_runs", {"limit": 999}))
    assert captured["params"]["limit"] == "20"


def test_list_coding_runs_no_params_only_limit_sent():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "list_coding_runs"))
    assert captured["params"] == {"limit": "20"}


def test_get_coding_run_maps_to_detail_endpoint():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={"id": 5, "status": "done", "final_message": "done.", "diff_patch": "diff"},
        )

    result = asyncio.run(_call_tool(config, handler, "get_coding_run", {"coding_run_id": 5}))
    assert captured["path"] == "/coding-runs/5"
    data = json.loads(_tool_text(result))
    assert data["id"] == 5
    assert data["final_message"] == "done."


def test_get_coding_run_not_found_returns_error_string():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "coding_run 99 不存在"})

    result = asyncio.run(_call_tool(config, handler, "get_coding_run", {"coding_run_id": 99}))
    text = _tool_text(result)
    assert text.startswith("ERROR:")


# ---------------------------------------------------------------------------
# 階段 15 Phase A（PLAN.md P.1.3 節）：get_projects_matrix，原樣轉述
# GET /projects/matrix 的回應。
# ---------------------------------------------------------------------------


def test_get_projects_matrix_maps_to_matrix_endpoint():
    config = make_config()
    captured = {}
    fake_matrix = {
        "servers": ["server-b", "server-c", "pro6000"],
        "projects": [
            {
                "name": "proj-a",
                "repo_or_path": "/data/proj-a",
                "instances": {
                    "server-c": {
                        "path": "/data/proj-a",
                        "git_remote": None,
                        "git_branch": "main",
                        "git_commit": "abc123",
                        "dirty": False,
                    }
                },
            }
        ],
        "pending_candidates": {"server-b": 0, "server-c": 27, "pro6000": 0},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json=fake_matrix)

    result = asyncio.run(_call_tool(config, handler, "get_projects_matrix"))
    assert captured["path"] == "/projects/matrix"
    data = json.loads(_tool_text(result))
    assert data == fake_matrix


# ---------------------------------------------------------------------------
# 階段 16（PLAN.md Q.3 節，資料卡）：get_dataset_card 一對一映射
# GET /datasets/{name}/{version}/card
# ---------------------------------------------------------------------------


def test_get_dataset_card_maps_to_card_endpoint_with_card():
    config = make_config()
    captured = {}
    fake_card_response = {
        "name": "defect",
        "version": "v1",
        "card": {
            "description": "缺陷偵測資料集第一版",
            "method": "人工標註",
            "derived_from": None,
            "counts_custom": {"train": 800, "val": 200},
            "created_at": "2026-07-11T00:00:00",
            "updated_at": "2026-07-11T00:00:00",
        },
        "auto_facts": {
            "file_count": 1000,
            "total_size_bytes": 1024000,
            "size_bytes": 1024000,
            "created_at": "2026-07-11T00:00:00",
            "cached_on": ["server-a"],
        },
        "rendered": "# defect@v1\n\n## 描述\n缺陷偵測資料集第一版\n",
        "note": None,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json=fake_card_response)

    result = asyncio.run(
        _call_tool(config, handler, "get_dataset_card", {"name": "defect", "version": "v1"})
    )
    assert captured["path"] == "/datasets/defect/v1/card"
    data = json.loads(_tool_text(result))
    assert data == fake_card_response


def test_get_dataset_card_relays_null_card_and_note_for_legacy_dataset():
    """階段 16 之前建立的舊資料集沒有卡：工具原樣轉述 `card: null` 與
    `note`，不重做任何判斷或腦補（PLAN.md Q.3：「agent 必須照實說沒有
    紀錄，不得腦補」，工具描述也明講這一點）。"""
    config = make_config()
    fake_response = {
        "name": "legacy",
        "version": "v1",
        "card": None,
        "auto_facts": {
            "file_count": 3,
            "total_size_bytes": 100,
            "size_bytes": 100,
            "created_at": "2020-01-01T00:00:00",
            "cached_on": [],
        },
        "rendered": "# legacy@v1\n\n## 描述\n此版本未登記資料卡（建立於資料卡制度之前）...",
        "note": "此版本未登記資料卡（建立於資料卡制度之前），製作方式與用途無紀錄——可事後補登。",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fake_response)

    result = asyncio.run(
        _call_tool(config, handler, "get_dataset_card", {"name": "legacy", "version": "v1"})
    )
    data = json.loads(_tool_text(result))
    assert data["card"] is None
    assert "未登記資料卡" in data["note"]


def test_get_dataset_card_not_found_returns_error_text():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "資料集 nope@v1 不存在"})

    result = asyncio.run(
        _call_tool(config, handler, "get_dataset_card", {"name": "nope", "version": "v1"})
    )
    text = _tool_text(result)
    assert text.startswith("ERROR:")


def test_get_dataset_card_tool_description_warns_against_guessing():
    """PLAN.md Q.3：工具描述要明示『不得推測』，測試釘住這個安全提示不會
    被之後的重構不小心刪掉。"""
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    tools = asyncio.run(_with_session(config, handler, fn))
    tool = next(t for t in tools.tools if t.name == "get_dataset_card")
    assert "not guess" in tool.description.lower() or "do not guess" in tool.description.lower()


# ---------------------------------------------------------------------------
# 專案詳情頁計畫第 4 節：get_project_timeline（唯讀）＋
# add_experiment_record／update_project_doc（**不建 approval，直接寫入**）
# ---------------------------------------------------------------------------


def test_get_project_timeline_maps_to_timeline_endpoint_with_params():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [{"type": "record", "kind": "note", "ts": "2026-07-11T00:00:00"}],
                "next_before_ts": "2026-07-11T00:00:00",
                "has_more": False,
            },
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "get_project_timeline",
            {
                "project_name": "proj-a",
                "q": "train.py",
                "limit": 5,
                "before_ts": "2026-07-10T00:00:00",
                "kinds": ["note", "job"],
            },
        )
    )
    assert captured["path"] == "/projects/proj-a/timeline"
    assert captured["params"]["limit"] == "5"
    assert captured["params"]["q"] == "train.py"
    assert captured["params"]["before_ts"] == "2026-07-10T00:00:00"
    assert captured["params"]["kinds"] == "note,job"

    text = _tool_text(result)
    data = json.loads(text)
    assert data["has_more"] is False
    assert data["items"][0]["kind"] == "note"


def test_get_project_timeline_limit_clamped_and_defaults_omit_optional_params():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "next_before_ts": None, "has_more": False})

    asyncio.run(
        _call_tool(config, handler, "get_project_timeline", {"project_name": "proj-a", "limit": 999})
    )
    assert captured["params"] == {"limit": "50"}


def test_get_project_timeline_not_found_returns_error_text():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "專案 nope 不存在"})

    result = asyncio.run(
        _call_tool(config, handler, "get_project_timeline", {"project_name": "nope"})
    )
    text = _tool_text(result)
    assert text.startswith("ERROR:")


def test_add_experiment_record_posts_to_records_endpoint_with_agent_chatgpt_author():
    config = make_config(auth_token="dispatch-secret")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["header"] = request.headers.get("x-auth-token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": 1,
                "project": "proj-a",
                "kind": "note",
                "title": "來自 ChatGPT",
                "content": "觀察到 loss 下降",
                "author": "agent:chatgpt",
                "created_at": "2026-07-11T00:00:00",
            },
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "add_experiment_record",
            {
                "project_name": "proj-a",
                "content": "觀察到 loss 下降",
                "kind": "observation",
                "title": "來自 ChatGPT",
            },
        )
    )
    assert captured["path"] == "/projects/proj-a/records"
    assert captured["method"] == "POST"
    assert captured["header"] == "dispatch-secret"
    # author 一律固定帶 agent:chatgpt，呼叫端無法覆蓋（工具簽名裡根本沒有
    # author 參數）。
    assert captured["body"] == {
        "content": "觀察到 loss 下降",
        "author": "agent:chatgpt",
        "kind": "observation",
        "title": "來自 ChatGPT",
    }

    text = _tool_text(result)
    data = json.loads(text)
    assert data["author"] == "agent:chatgpt"
    # 這個工具沒有 approval 概念，回應裡不該出現 PENDING APPROVAL 字樣。
    assert "PENDING APPROVAL" not in text


def test_add_experiment_record_omits_optional_fields_when_not_given():
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1})

    asyncio.run(
        _call_tool(
            config, handler, "add_experiment_record", {"project_name": "proj-a", "content": "x"}
        )
    )
    assert captured["body"] == {"content": "x", "author": "agent:chatgpt"}


def test_add_experiment_record_includes_job_id_and_coding_run_id_when_given():
    """修正 2（Fable 最終審查）：`add_experiment_record` 代理工具透傳
    `job_id`／`coding_run_id` 弱關聯到 HTTP body。"""
    config = make_config()
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": 1,
                "project": "proj-a",
                "kind": "note",
                "title": None,
                "content": "x",
                "author": "agent:chatgpt",
                "job_id": 7,
                "coding_run_id": 3,
                "created_at": "2026-07-11T00:00:00",
            },
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "add_experiment_record",
            {"project_name": "proj-a", "content": "x", "job_id": 7, "coding_run_id": 3},
        )
    )
    assert captured["body"] == {
        "content": "x",
        "author": "agent:chatgpt",
        "job_id": 7,
        "coding_run_id": 3,
    }
    text = _tool_text(result)
    data = json.loads(text)
    assert data["job_id"] == 7
    assert data["coding_run_id"] == 3


def test_add_experiment_record_invalid_kind_400_relayed():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "kind 必須是 [...] 其中之一"})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "add_experiment_record",
            {"project_name": "proj-a", "content": "x", "kind": "bogus"},
        )
    )
    text = _tool_text(result)
    assert "REQUEST REJECTED" in text
    assert "kind" in text


def test_add_experiment_record_tool_is_not_marked_read_only():
    """annotations 一定是 readOnlyHint=False（這個工具真的會寫資料），跟
    request_enqueue_job 等一樣不是唯讀，但跟它們不同的是**沒有**
    approval 這一層——見下面 update_project_doc 的對應測試與模組層註解。"""
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    tools = asyncio.run(_with_session(config, handler, fn))
    tool = next(t for t in tools.tools if t.name == "add_experiment_record")
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False


def test_update_project_doc_patches_project_endpoint():
    config = make_config(auth_token="dispatch-secret")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["header"] = request.headers.get("x-auth-token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"name": "proj-a", "goal": "把準確率衝到 95%", "optimization_notes": None, "progress": None},
        )

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "update_project_doc",
            {"project_name": "proj-a", "field": "goal", "content": "把準確率衝到 95%"},
        )
    )
    assert captured["path"] == "/projects/proj-a"
    assert captured["method"] == "PATCH"
    assert captured["header"] == "dispatch-secret"
    assert captured["body"] == {"goal": "把準確率衝到 95%"}

    text = _tool_text(result)
    data = json.loads(text)
    assert data["goal"] == "把準確率衝到 95%"


def test_update_project_doc_rejects_field_outside_whitelist_without_calling_dispatch():
    """`field` 不在白名單裡 -> 這個工具直接回錯誤文字，**根本不打**調度
    中心（防止呼叫端用這個工具亂猜欄位名）。"""
    config = make_config()
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "update_project_doc",
            {"project_name": "proj-a", "field": "repo_or_path", "content": "/etc/passwd"},
        )
    )
    text = _tool_text(result)
    assert text.startswith("ERROR:")
    assert "field" in text
    assert called is False


def test_update_project_doc_not_found_404_relayed():
    """PATCH 沿用 `_dispatch_post_raw()` 既有的 4xx 慣例（同
    `request_stop_job` 對 404 的既有測試）：整個 400-499 範圍都轉述
    `detail` 文字，不是只有 400。"""
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "專案 nope 不存在"})

    result = asyncio.run(
        _call_tool(
            config,
            handler,
            "update_project_doc",
            {"project_name": "nope", "field": "goal", "content": "x"},
        )
    )
    text = _tool_text(result)
    assert "專案 nope 不存在" in text


def test_update_project_doc_tool_is_not_marked_read_only():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def fn(session: ClientSession):
        return await session.list_tools()

    tools = asyncio.run(_with_session(config, handler, fn))
    tool = next(t for t in tools.tools if t.name == "update_project_doc")
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False


# ---------------------------------------------------------------------------
# 錯誤處理：連不上/非 2xx/非 JSON -> 明確錯誤文字，不拋例外炸掉 session
# ---------------------------------------------------------------------------


def test_dispatch_center_500_returns_error_text_not_exception():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal boom")

    result = asyncio.run(_call_tool(config, handler, "get_servers"))
    assert result.isError is False
    text = _tool_text(result)
    assert text.startswith("ERROR:")
    assert "500" in text


def test_dispatch_center_connection_failure_returns_error_text():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = asyncio.run(_call_tool(config, handler, "get_servers"))
    assert result.isError is False
    text = _tool_text(result)
    assert text.startswith("ERROR:")


def test_dispatch_center_non_json_response_returns_error_text():
    config = make_config()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all", headers={"content-type": "text/plain"})

    result = asyncio.run(_call_tool(config, handler, "get_servers"))
    assert result.isError is False
    text = _tool_text(result)
    assert text.startswith("ERROR:")
    assert "JSON" in text


def test_x_auth_token_header_forwarded_when_configured():
    config = make_config(auth_token="dispatch-secret")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("x-auth-token")
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "get_servers"))
    assert captured["header"] == "dispatch-secret"


def test_x_auth_token_header_absent_when_not_configured():
    config = make_config(auth_token=None)
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("x-auth-token")
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "get_servers"))
    assert captured["header"] is None


def test_mcp_enqueue_reaches_dispatch_with_actor_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "dispatch.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "dispatch-secret")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    config = make_config(auth_token="dispatch-secret")
    with TestClient(main_module.app) as dispatch_client:

        async def handler(request: httpx.Request) -> httpx.Response:
            response = dispatch_client.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"content-type": response.headers.get("content-type", "")},
            )

        result = asyncio.run(
            _call_tool(
                config,
                handler,
                "request_enqueue_job",
                {"command": "echo mcp-attributed"},
            )
        )

        assert "mcp-attributed" in _tool_text(result)
        approval = main_module.app_state.db.list_approvals()[-1]
        assert approval.requester_actor_id == LEGACY_ADMIN_ACTOR_ID
        record = read_audit(main_module.app_state.config.audit_path)[-1]
        assert record["actor"] == {
            "id": LEGACY_ADMIN_ACTOR_ID,
            "kind": "legacy",
            "authentication": "legacy_shared_token",
        }


@pytest.mark.parametrize("authorization_mode", ["off", "shadow"])
def test_mcp_dispatch_result_and_approval_are_unchanged_in_shadow(
    tmp_path, monkeypatch, authorization_mode
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / f"{authorization_mode}.db"))
    monkeypatch.setenv(
        "AUDIT_PATH", str(tmp_path / f"{authorization_mode}-audit.jsonl")
    )
    monkeypatch.setenv("AUTHORIZATION_MODE", authorization_mode)
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")

    import app.main as main_module

    with TestClient(main_module.app) as dispatch_client:

        async def handler(request: httpx.Request) -> httpx.Response:
            response = dispatch_client.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"content-type": response.headers.get("content-type", "")},
            )

        result = asyncio.run(
            _call_tool(
                make_config(),
                handler,
                "request_enqueue_job",
                {"command": "echo mcp-shadow-compatible"},
            )
        )

        tool_body = json.loads(_tool_text(result))
        assert tool_body["approval"]["kind"] == "enqueue"
        assert tool_body["approval"]["status"] == "pending"
        approval = main_module.app_state.db.list_approvals()[-1]
        assert approval.payload["command"] == "echo mcp-shadow-compatible"
        assert approval.status == "pending"
        denials = [
            record
            for record in read_audit(main_module.app_state.config.audit_path)
            if record["action"] == "authorization_shadow_denied"
        ]
        assert bool(denials) is (authorization_mode == "shadow")
        if denials:
            assert denials[-1]["params"]["route"] == "POST /dispatch"
            assert denials[-1]["params"]["principal_kind"] == "anonymous"


def test_dispatch_service_token_bearer_and_legacy_fallback_are_both_forwarded():
    config = make_config(
        auth_token="dispatch-secret",
        dispatch_service_token="dcs_test-service-token",
    )
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["legacy"] = request.headers.get("x-auth-token")
        return httpx.Response(200, json=[])

    asyncio.run(_call_tool(config, handler, "get_servers"))

    assert captured == {
        "authorization": "Bearer dcs_test-service-token",
        "legacy": "dispatch-secret",
    }


def test_dispatch_service_token_is_not_confused_with_inbound_bridge_token():
    config = make_config(
        bridge_token="connector-ingress",
        dispatch_service_token="dcs_dispatch-egress",
    )
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    asyncio.run(
        _call_tool(
            config,
            handler,
            "get_servers",
            extra_headers={"Authorization": "Bearer connector-ingress"},
        )
    )

    assert captured["authorization"] == "Bearer dcs_dispatch-egress"


def test_bridge_config_repr_omits_all_credentials():
    config = make_config(
        auth_token="legacy-secret",
        path_secret="path-secret",
        bridge_token="connector-secret",
        dispatch_service_token="service-secret",
    )

    rendered = repr(config)
    for secret in (
        "legacy-secret",
        "path-secret",
        "connector-secret",
        "service-secret",
    ):
        assert secret not in rendered


# ---------------------------------------------------------------------------
# 截斷（比照 app/agent_tools.py／app/agent_runtime.py 的 4000 字元原則）
# ---------------------------------------------------------------------------


def test_truncate_helper_short_text_untouched():
    assert _truncate("short") == "short"


def test_truncate_helper_long_text_is_capped_with_note():
    text = "x" * (_MAX_RESULT_CHARS + 500)
    truncated = _truncate(text)
    assert truncated.startswith("x" * 100)
    assert "已截斷" in truncated
    assert "500" in truncated
    assert len(truncated) > _MAX_RESULT_CHARS  # 註記文字本身也會佔字元數
    assert len(truncated) < len(text)


def test_oversized_dispatch_response_gets_truncated_end_to_end():
    config = make_config()
    huge_list = [{"id": i, "note": "x" * 50} for i in range(500)]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=huge_list)

    result = asyncio.run(_call_tool(config, handler, "get_servers"))
    text = _tool_text(result)
    assert "已截斷" in text
    # 不是合法 JSON（截斷點通常切在字串中間），這正是預期行為——
    # 截斷的目的是限制回饋給模型的字元數，不保證仍是合法 JSON。
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


# ---------------------------------------------------------------------------
# _clamp_int()：小工具本身
# ---------------------------------------------------------------------------


def test_clamp_int_within_range():
    assert _clamp_int(10, default=5, lo=1, hi=20) == 10


def test_clamp_int_above_hi_clamped():
    assert _clamp_int(999, default=5, lo=1, hi=20) == 20


def test_clamp_int_below_lo_clamped():
    assert _clamp_int(-5, default=5, lo=1, hi=20) == 1


def test_clamp_int_none_uses_default():
    assert _clamp_int(None, default=5, lo=1, hi=20) == 5


def test_clamp_int_invalid_value_uses_default():
    assert _clamp_int("not-a-number", default=5, lo=1, hi=20) == 5


# ---------------------------------------------------------------------------
# DG-ASSISTANT-TOOLS v1: runner-hosted stdio mode (per-turn token, call cap, log)
# ---------------------------------------------------------------------------


def _stdio_config(tmp_path, *, max_calls=None, source=None):
    from app.mcp_bridge import load_stdio_bridge_config

    (tmp_path / "token").write_text("dat_11111111-2222-4333-8444-555555555555.secretsecret\n", encoding="utf-8")
    payload = {"dispatch_base_url": "http://127.0.0.1:8000/", "token_file": "token"}
    if max_calls is not None:
        payload["max_calls"] = max_calls
    if source is not None:
        payload["source"] = source
    (tmp_path / "tools.json").write_text(json.dumps(payload), encoding="utf-8")
    return load_stdio_bridge_config(tmp_path / "tools.json")


def test_stdio_config_reads_token_from_sibling_file_and_applies_defaults(tmp_path):
    config = _stdio_config(tmp_path)
    assert config.dispatch_base_url == "http://127.0.0.1:8000"
    assert config.auth_token == "dat_11111111-2222-4333-8444-555555555555.secretsecret"
    assert config.dispatch_service_token is None and config.bridge_token is None
    assert config.max_calls == 8
    assert config.calls_log == tmp_path / "tool_calls.jsonl"
    assert config.request_source == "assistant"
    assert "secretsecret" not in repr(config)
    from app.mcp_bridge import _dispatch_headers

    assert _dispatch_headers(config) == {"X-Auth-Token": config.auth_token}


def test_stdio_config_fails_closed_on_missing_token_or_bad_url(tmp_path):
    from app.mcp_bridge import load_stdio_bridge_config

    (tmp_path / "tools.json").write_text(json.dumps({"dispatch_base_url": "http://x", "token_file": "token"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_stdio_bridge_config(tmp_path / "tools.json")
    (tmp_path / "token").write_text("   \n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_stdio_bridge_config(tmp_path / "tools.json")
    (tmp_path / "token").write_text("dat_x.y", encoding="utf-8")
    (tmp_path / "tools.json").write_text(json.dumps({"dispatch_base_url": "ftp://x", "token_file": "token"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_stdio_bridge_config(tmp_path / "tools.json")


@pytest.mark.anyio
async def test_per_turn_call_cap_rejects_extra_calls_and_logs_every_call(tmp_path):
    config = _stdio_config(tmp_path, max_calls=1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Auth-Token"] == config.auth_token
        return httpx.Response(200, json=[])

    async def two_calls(session: ClientSession):
        first = _tool_text(await session.call_tool("get_servers", {}))
        second = _tool_text(await session.call_tool("get_servers", {}))
        return first, second

    first, second = await _with_session(config, handler, two_calls)
    assert "REQUEST REJECTED" not in first
    assert second.startswith("REQUEST REJECTED")
    assert "上限（1）" in second

    lines = [json.loads(line) for line in (tmp_path / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(entry["tool"], entry["status"]) for entry in lines] == [
        ("get_servers", "ok"),
        ("get_servers", "rejected_cap"),
    ]
    assert all("token" not in json.dumps(entry) for entry in lines)


@pytest.mark.anyio
async def test_stdio_request_source_and_approval_id_are_logged(tmp_path):
    config = _stdio_config(tmp_path, max_calls=8, source="assistant")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        # The real POST /dispatch returns the approval dict itself.
        return httpx.Response(200, json={"id": 42, "kind": "enqueue", "status": "pending", "command": "nvidia-smi"})

    text = _tool_text(await _call_tool(config, handler, "request_enqueue_job", {"command": "nvidia-smi"}))
    assert '"id": 42' in text or '"id":42' in text
    assert seen[0]["source"] == "assistant"
    entry = json.loads((tmp_path / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["tool"] == "request_enqueue_job" and entry["status"] == "ok" and entry["approval_id"] == 42


def test_stdio_argv_parsing_requires_config_path():
    from pathlib import Path

    from app.mcp_bridge import _stdio_config_path

    assert _stdio_config_path([]) is None
    assert _stdio_config_path(["--stdio", "--config", "/tmp/x.json"]) == Path("/tmp/x.json")
    with pytest.raises(SystemExit):
        _stdio_config_path(["--stdio"])
