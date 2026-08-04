"""MCP Bridge（PLAN.md J 節，階段 9）：ChatGPT custom connector 串接的獨立
行程，`python -m app.mcp_bridge` 啟動。

架構（PLAN.md J 開頭）：

    ChatGPT custom connector (HTTPS)
        -> Cloudflare Tunnel (outbound-only，Server A 不開 inbound port)
        -> 這個 MCP Bridge（Server A 本機，127.0.0.1:MCP_BRIDGE_PORT）
        -> 既有調度中心 REST API（127.0.0.1:8888 + X-Auth-Token）

安全模型：核准永遠留在網頁那條私有通道；這條 ChatGPT 鏈路上**沒有、也
永遠不會有** approve/reject 工具（提案通道與批准通道分離）。第一階段
（唯讀，已由使用者實際用 ChatGPT 驗證過）帳號被盜的最大血本＝看得到狀態。
第二階段（PLAN.md J.2 節，使用者已核可，本檔已實作）加了兩個**只會建立
pending approval、絕不直接執行任何東西**的寫入工具
（`request_enqueue_job`／`request_stop_job`），血本因此變成「看得到狀態
＋建得了待核准請求」——核准這個動作永遠、只能在網頁介面上按。

**鐵律（本模組專屬，違反＝實作錯誤）**：
1. 本檔不得 `import app.*`（config／db／agent_tools 等一律不 import）。
   只透過 httpx 呼叫調度中心的 REST API。這樣 bridge 掛掉、有洞、被换
   到別台機器跑，都不會影響調度中心本體——這是刻意的隔離設計，不是
   偷懶：bridge 是「独立行程」而不是「同一個 FastAPI app 多掛幾個路由」。
2. **永遠不會有 approve/reject 工具，也不會有直接執行/直接停止的工具**
   ——`request_enqueue_job`／`request_stop_job` 只是原樣轉呼叫調度中心
   既有的 `POST /dispatch`／`POST /jobs/{id}/stop`（帶
   `"source": "chatgpt"`，見下），兩個端點本來就只建立 `approvals` 表的
   記錄，bridge 不重做危險指令攔截等任何判斷，全部沿用調度中心既有邏輯。
   **階段 10（PLAN.md K 節）更新**：這兩個請求現在有可能被操作者「預先」
   在調度中心設定的確定性規則（`auto_approve.yaml`，見 README）自動核准
   ——依然不是這條 ChatGPT 鏈路自己批准自己（bridge／模型沒有、也永遠不
   會有 approve 工具），而是操作者透過網頁/設定檔預先寫好的規則在替他自己
   按核准；沒有規則檔或沒命中規則時，行為跟以前完全一樣——仍然是
   `POST /approve/{id}`（網頁核准）才會發生。調度中心的回應會標明
   `auto_approved`，這兩個工具會如實轉述給模型（見下方 docstring）。
   PLAN.md J.2 節明確排除的工具（rerun、候選專案 import/ignore、伺服器
   管理、inventory scan）**這階段不加**。任何人想在這個檔案加
   approve_*/reject_* 工具，或加 J.2 節明確排除的工具，本身就是違反
   PLAN.md J/J.2 節定案的架構，請先找 Fable 覆核，不要直接動手。
3. 只綁 `127.0.0.1`（鐵律第 4 條），絕不 0.0.0.0；對外一律透過
   cloudflared 隧道（見 README「ChatGPT 串接（MCP Bridge）」章節）。

為什麼自己重寫一份 `.env` parser（而不是 `from app.config import
load_dotenv`）：鐵律 1 要求本檔零 `app.*` 依賴，所以下面的
`_load_dotenv()` 是 `app/config.py` 同名函式的**複製品**（邏輯完全一致：
只認 KEY=VALUE、忽略空行/`#`註解、不覆蓋已存在的環境變數），不是共用同一
份程式碼。之後若 `app/config.py` 的 parser 改邏輯，這裡要記得手動同步。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - 選配依賴沒裝時的明確錯誤
    raise SystemExit(
        "MCP Bridge 需要 `mcp` 套件（官方 Python SDK）但目前環境沒裝。\n"
        "請先執行：pip install 'dispatch-center[mcp]'\n"
        "（開發環境則安裝 requirements-dev.lock。）調度中心本體不受影響"
        "——這是獨立行程的選配依賴。"
    ) from exc

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


# ---------------------------------------------------------------------------
# .env 極簡 parser（複製自 app/config.py，見上方模組 docstring 說明為什麼是
# 複製而不是 import）
# ---------------------------------------------------------------------------


def _load_dotenv(path: str | Path = ".env") -> None:
    """極簡 .env 載入：只解析 KEY=VALUE，忽略空行與 # 開頭註解。

    不會覆蓋已經存在的環境變數（shell/systemd 設的優先）。
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


@dataclass
class BridgeConfig:
    dispatch_base_url: str
    auth_token: Optional[str] = field(repr=False)
    port: int
    path_secret: str = field(repr=False)
    bridge_token: Optional[str] = field(repr=False)
    # Outbound dispatch-center credential.  This is deliberately distinct
    # from `bridge_token`, which protects inbound connector requests.
    dispatch_service_token: Optional[str] = field(default=None, repr=False)

    @property
    def mcp_path(self) -> str:
        return f"/mcp-{self.path_secret}"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_bridge_config(dotenv_path: str | Path = ".env") -> BridgeConfig:
    """讀取 .env + 環境變數並組出 `BridgeConfig`。

    `MCP_BRIDGE_PATH_SECRET` 沒設定 -> 直接 `SystemExit`（明確教使用者怎麼
    生一把，不是含糊的 KeyError）。這是唯一會讓 bridge 拒絕啟動的檢查。
    """
    _load_dotenv(dotenv_path)

    path_secret = (os.environ.get("MCP_BRIDGE_PATH_SECRET") or "").strip()
    if not path_secret:
        raise SystemExit(
            "MCP_BRIDGE_PATH_SECRET 未設定，MCP Bridge 拒絕啟動。\n"
            "這把機密是 ChatGPT 連進來的唯一防線之一（掛在 URL 路徑裡），\n"
            "請先產生一把再啟動：\n"
            "  openssl rand -hex 24\n"
            "把輸出貼進 .env（專案根目錄）：\n"
            "  MCP_BRIDGE_PATH_SECRET=<剛剛的輸出>\n"
            "存檔後重新執行：python -m app.mcp_bridge"
        )

    return BridgeConfig(
        dispatch_base_url=os.environ.get("DISPATCH_BASE_URL", "http://127.0.0.1:8888"),
        auth_token=(os.environ.get("AUTH_TOKEN") or None),
        port=_int_env("MCP_BRIDGE_PORT", 8890),
        path_secret=path_secret,
        bridge_token=(os.environ.get("MCP_BRIDGE_TOKEN") or None),
        dispatch_service_token=(os.environ.get("DISPATCH_SERVICE_TOKEN") or None),
    )


# ---------------------------------------------------------------------------
# 小工具：int clamp（比照 app/agent_tools.py 的 `_clamp_int` 原則，這裡自己
# 重寫一份，不 import app.agent_tools——理由同模組 docstring 的鐵律 1）
# ---------------------------------------------------------------------------


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


#: 工具結果統一截斷字元數上限，比照 app/agent_tools.py 與
#: AGENT_TOOL_RESULT_MAX_CHARS 的 4000 字元原則（這裡寫死常數，bridge 是
#: 獨立行程，沒有共用的 AppConfig 可以讀）。
_MAX_RESULT_CHARS = 4000


def _dispatch_headers(config: BridgeConfig) -> dict[str, str]:
    """Return outbound auth headers without ever formatting them for logs."""

    headers: dict[str, str] = {}
    if config.dispatch_service_token:
        headers["Authorization"] = f"Bearer {config.dispatch_service_token}"
    if config.auth_token:
        # Keep the legacy header even when a service token is configured so a
        # rollout can fall back without changing bridge request behavior.
        headers["X-Auth-Token"] = config.auth_token
    return headers


def _truncate(text: str, max_chars: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n…（已截斷，省略後面 {omitted} 字元）"


def _to_json_text(data: Any) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except TypeError:
        text = str(data)
    return _truncate(text)


# ---------------------------------------------------------------------------
# 呼叫調度中心（httpx）——所有工具唯讀，連不上/非 2xx 一律回傳錯誤文字，
# 絕不讓例外往上炸掉整個 MCP session（PLAN.md J：「調度中心連不上/回非
# 2xx -> 工具回傳明確錯誤文字」）。
# ---------------------------------------------------------------------------


async def _dispatch_get_raw(
    config: BridgeConfig,
    path: str,
    params: Optional[dict] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[Any, Optional[str]]:
    """對調度中心發一個 GET，回傳 `(data, error_text)`：成功時
    `(解析後的 JSON, None)`，失敗時 `(None, 明確的錯誤說明文字)`——絕不
    丟例外，呼叫端（`_dispatch_get` 或需要在序列化前先處理資料的工具，如
    `list_jobs`）自行決定要不要再加工。

    `client` 可注入（測試用 `httpx.AsyncClient(transport=httpx.MockTransport(...))`）；
    沒有注入時這裡自己開一個短命的 `httpx.AsyncClient`，用完即關（比照
    app/llm_local.py 的 owns_client 模式）。

    HTTP 400 走跟 `_dispatch_post_raw()` 4xx 相同的「明確拒絕」格式（轉述
    `detail`），跟連線失敗/404/5xx 的「ERROR: ...」格式區分開；404（查無
    資源）與其他非 2xx 仍維持 ERROR 格式不動。
    """
    url = f"{config.dispatch_base_url}{path}"
    headers = _dispatch_headers(config)

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http_client.get(url, params=params, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        return None, f"ERROR: 無法連線到調度中心（{url}）：{exc}"
    except Exception as exc:  # noqa: BLE001 - 防禦性：絕不讓工具丟例外炸掉 MCP session
        return None, f"ERROR: 呼叫調度中心時發生未預期錯誤（{url}）：{exc}"
    finally:
        if owns_client:
            await http_client.aclose()

    if resp.status_code == 400:
        # 調度中心回 400 = 對請求參數的明確拒絕（路徑穿越、秘密檔名等），
        # 比照 _dispatch_post_raw() 的 4xx 慣例轉述 detail，讓模型分得出
        # 「被拒絕」跟「連不上/壞掉」（404/5xx 仍走下面的 ERROR 格式，
        # 404 是查無資源不是拒絕）。
        detail: Optional[str] = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail")
        except ValueError:
            detail = None
        if detail is None:
            detail = resp.text
            if len(detail) > 500:
                detail = detail[:500] + "…"
        return None, f"REQUEST REJECTED by dispatch center (HTTP {resp.status_code}): {detail}"

    if resp.status_code < 200 or resp.status_code >= 300:
        body = resp.text
        if len(body) > 500:
            body = body[:500] + "…"
        return None, f"ERROR: 調度中心回傳 HTTP {resp.status_code}（{path}）：{body}"

    try:
        data = resp.json()
    except ValueError:
        return None, f"ERROR: 調度中心回傳的內容不是合法 JSON（{path}）"

    return data, None


async def _dispatch_get(
    config: BridgeConfig,
    path: str,
    params: Optional[dict] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """`_dispatch_get_raw()` 的文字版：直接回傳截斷後的 JSON 字串或錯誤
    文字，給不需要在序列化前加工資料的工具直接用。"""
    data, error = await _dispatch_get_raw(config, path, params, client=client)
    if error is not None:
        return error
    return _to_json_text(data)


async def _dispatch_post_raw(
    config: BridgeConfig,
    path: str,
    json_body: dict,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[Any, Optional[str]]:
    """PLAN.md J.2 節的兩個寫入工具專用：對調度中心發一個 POST，回傳
    `(data, error_text)`。

    跟 `_dispatch_get_raw()` 的差異在 4xx 的處理：調度中心的既有端點
    （`POST /dispatch`／`POST /jobs/{id}/stop`）在危險指令被拒、job 不存在
    /不是 running 狀態時回 4xx，body 是 `{"detail": "..."}`——J.2 節要求
    「原樣轉述 detail 文字，不拋例外」，所以這裡把 4xx 也走 `(None,
    error_text)` 這條路，但 error_text 直接帶調度中心給的 detail 訊息（沒
    有 detail 欄位時退回原始 body 文字），跟連線失敗/5xx 的「ERROR: ...」
    格式區分開，讓模型看得出「這是調度中心明確拒絕的理由」而不是「連不上
    /未預期的錯誤」。
    """
    url = f"{config.dispatch_base_url}{path}"
    headers = _dispatch_headers(config)

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http_client.post(url, json=json_body, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        return None, f"ERROR: 無法連線到調度中心（{url}）：{exc}"
    except Exception as exc:  # noqa: BLE001 - 防禦性：絕不讓工具丟例外炸掉 MCP session
        return None, f"ERROR: 呼叫調度中心時發生未預期錯誤（{url}）：{exc}"
    finally:
        if owns_client:
            await http_client.aclose()

    if 400 <= resp.status_code < 500:
        detail: Optional[str] = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail")
        except ValueError:
            detail = None
        if detail is None:
            detail = resp.text
            if len(detail) > 500:
                detail = detail[:500] + "…"
        return None, (
            f"REQUEST REJECTED by dispatch center (HTTP {resp.status_code}): {detail}"
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        body_text = resp.text
        if len(body_text) > 500:
            body_text = body_text[:500] + "…"
        return None, f"ERROR: 調度中心回傳 HTTP {resp.status_code}（{path}）：{body_text}"

    try:
        data = resp.json()
    except ValueError:
        return None, f"ERROR: 調度中心回傳的內容不是合法 JSON（{path}）"

    return data, None


async def _dispatch_patch_raw(
    config: BridgeConfig,
    path: str,
    json_body: dict,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[Any, Optional[str]]:
    """專案詳情頁計畫第 4 節（`update_project_doc` 代理工具）專用：對調度
    中心發一個 PATCH，行為跟 `_dispatch_post_raw()` 完全一致（4xx 轉述
    `detail`、5xx/連線失敗一律 `ERROR: ...`），只差在 HTTP method——目前
    唯一用得到的呼叫端是 `PATCH /projects/{name}`，其餘既有代理工具都是
    GET／POST，這裡沒有共用既有 `_dispatch_post_raw()` 是因為 `httpx.
    AsyncClient` 的 `.post()`/`.patch()` 是不同方法，沒有更省事的共用寫法。
    """
    url = f"{config.dispatch_base_url}{path}"
    headers = _dispatch_headers(config)

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http_client.patch(url, json=json_body, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        return None, f"ERROR: 無法連線到調度中心（{url}）：{exc}"
    except Exception as exc:  # noqa: BLE001 - 防禦性：絕不讓工具丟例外炸掉 MCP session
        return None, f"ERROR: 呼叫調度中心時發生未預期錯誤（{url}）：{exc}"
    finally:
        if owns_client:
            await http_client.aclose()

    if 400 <= resp.status_code < 500:
        detail: Optional[str] = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail")
        except ValueError:
            detail = None
        if detail is None:
            detail = resp.text
            if len(detail) > 500:
                detail = detail[:500] + "…"
        return None, (
            f"REQUEST REJECTED by dispatch center (HTTP {resp.status_code}): {detail}"
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        body_text = resp.text
        if len(body_text) > 500:
            body_text = body_text[:500] + "…"
        return None, f"ERROR: 調度中心回傳 HTTP {resp.status_code}（{path}）：{body_text}"

    try:
        data = resp.json()
    except ValueError:
        return None, f"ERROR: 調度中心回傳的內容不是合法 JSON（{path}）"

    return data, None


# ---------------------------------------------------------------------------
# 建立 FastMCP 實例與 25 個工具（19 個唯讀，含階段 11 新增的
# get_project_activity、PLAN.md J.2 節的 2 個「只建 pending approval」寫入
# 工具，以及階段 12（PLAN.md M 節）新增的 2 個唯讀讀檔工具
# list_project_files/read_project_file ＋ 1 個「只建 pending approval」的
# request_apply_patch 寫入工具，以及階段 13（PLAN.md N 節，Codex Worker
# v2）新增的 1 個「只建 pending approval」的 request_coding_task 寫入工具
# ＋ 3 個唯讀工具 get_codex_runner_status/list_coding_runs/
# get_coding_run，以及階段 15 Phase A（PLAN.md P.1.3 節）新增的 1 個唯讀
# 工具 get_projects_matrix，以及階段 16（PLAN.md Q.3 節，資料卡）新增的
# 1 個唯讀工具 get_dataset_card，以及專案詳情頁計畫第 4 節新增的 1 個唯讀
# 工具 get_project_timeline ＋ 2 個「直接寫入、不建 approval」的寫入工具
# add_experiment_record/update_project_doc）。全部一對一映射調度中心既有
# 端點，工具描述用英文寫給 ChatGPT 的模型看。
#
# 階段 12（PLAN.md M 節）安全模型延伸：`request_apply_patch` 一樣**只建立
# pending approval，絕不直接套用任何改動**——核准當下才會真的
# `git apply`／commit（在一個新的 git branch 上，永不 push，見
# `app/approvals.py` 的 `approve()` 的 apply_patch 分支與 README「AI 改碼
# 層次一」小節）。這個 kind **不在** `app.autoapprove` 的自動核准白名單裡
# （只認 enqueue/stop），所以永遠會是一張需要人工在網頁上逐一看過 diff 才
# 能核准的卡片，不會被使用者自己的自動核准規則繞過。
#
# 階段 13（PLAN.md N 節，2026-07-10 使用者裁定改版為 Codex Worker v2：
# Central Codex Runner）安全模型延伸：`request_coding_task` 同理**只建立
# pending approval，絕不直接派工**——核准當下才會真的建立一個
# `coding_runs` 記錄＋enqueue 一個 `type="coding"` 任務跑 `codex exec`（永遠
# 在唯一一台由 `.env` `CODEX_RUNNER_SERVER` 指定的機器上、獨立 git
# worktree、永不 push，見 `app/approvals.py` 的 `approve()` 的 coding_task
# 分支與 README「Codex Worker」小節）。這個 kind 同樣**不在**自動核准白
# 名單裡，永遠需要人工核准 instruction 才會真的執行；v2 起這個工具**不再
# 讓呼叫端選 Codex 執行機器**（沒有 `server` 參數，POST body 也不帶
# `server`——Runner 固定由調度中心的 `.env` 決定，見 PLAN.md N.2）。三個
# 新的唯讀工具（`get_codex_runner_status`/`list_coding_runs`/
# `get_coding_run`）純粹查詢，一對一映射對應的唯讀端點，不受這條鐵律
# 影響（本來就沒有寫入能力）。
#
# 專案詳情頁計畫第 4 節安全模型延伸：`add_experiment_record`／
# `update_project_doc` 是這條 ChatGPT 鏈路上**第一次**出現的「不建立
# approval、直接執行」寫入工具——跟上面 request_enqueue_job/
# request_stop_job/request_apply_patch/request_coding_task 的既有模式都
# 不同。這是刻意的、已在 `app/agent_tools.py` 模組 docstring 正式登記的
# 鐵律例外（同一套理由，見那邊的完整說明）：兩者都只會寫入純文字（Markdown
# 筆記或專案的目標/方法/進度欄位），不會派工、不會碰任何機器、可逆（能
# 改能刪），血本上限跟既有唯讀工具（例如 `get_project_activity` 可能連帶
# SSH 讀到訓練 log 內容）比起來反而更小——**這兩個工具依然不是
# approve/reject 工具**（鐵律第 2 條的核心精神：核准永遠留在網頁那條私有
# 通道，這裡完全不受影響，`enqueue`/`stop` 等有「執行力」的動作一律仍然
# 只能建立 approval）。呼叫端（調度中心 `POST /projects/{name}/records`）
# 把這個代理工具寫入的紀錄 `author` 標記為 `"agent:chatgpt"`（區分於本地
# vLLM 版工具固定寫的 `"agent"`，見 `app/agent_tools.py` 的
# `_tool_add_experiment_record()`），方便事後追查是哪一條鏈路補的紀錄。
# ---------------------------------------------------------------------------


# Goal 1 / Slice 2: metadata-only catalog.  Keep literal strings here rather
# than importing app.authorization: INV-LLM-4 requires this bridge to remain an
# isolated HTTP client process with no app.* imports.
MCP_TOOL_ACTIONS: dict[str, str] = {
    "get_servers": "platform.view",
    "list_jobs": "project.view",
    "get_job": "project.view",
    "get_job_log": "project.view",
    "list_approvals": "approval.view",
    "list_events": "audit.view",
    "list_projects": "project.view",
    "list_datasets": "project.view",
    "get_dataset_card": "project.view",
    "list_project_candidates": "platform.view",
    "get_project_candidate": "platform.view",
    "get_project_activity": "project.view",
    "list_project_files": "project.view",
    "read_project_file": "project.view",
    "request_enqueue_job": "project.operate",
    "request_stop_job": "project.operate",
    "request_apply_patch": "project.operate",
    "request_coding_task": "project.operate",
    "get_codex_runner_status": "platform.view",
    "list_coding_runs": "project.view",
    "get_coding_run": "project.view",
    "get_projects_matrix": "platform.view",
    "get_project_timeline": "project.view",
    "add_experiment_record": "project.operate",
    "update_project_doc": "project.admin",
}


def _build_mcp(config: BridgeConfig, *, http_client: Optional[httpx.AsyncClient] = None) -> FastMCP:
    mcp = FastMCP(
        name="dispatch-center-bridge",
        host="127.0.0.1",
        port=config.port,
        streamable_http_path=config.mcp_path,
        # 唯讀查詢型工具，不需要跨請求的伺服器端 session 狀態；stateless_http
        # 也讓 cloudflared quick tunnel 重連後行為更單純（不用維持
        # Mcp-Session-Id 也能繼續運作）。json_response=True 讓回應走一般
        # JSON（不強制 SSE 串流），跟 ChatGPT connector 的相容性較好。
        stateless_http=True,
        json_response=True,
    )

    async def _get(path: str, params: Optional[dict] = None) -> str:
        return await _dispatch_get(config, path, params, client=http_client)

    #: PLAN.md J.2 節：既有 10 個唯讀工具全部補標 `readOnlyHint=True`；
    #: 兩個新的寫入工具（下方）標 `readOnlyHint=False, destructiveHint=False`
    #: ——建立 pending approval 本身不具破壞性，但也不是唯讀查詢，讓 ChatGPT
    #: 端對這兩個工具跳原生確認框（多一層免費防線）。
    read_only = ToolAnnotations(readOnlyHint=True)
    creates_approval = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
    #: 專案詳情頁計畫第 4 節：`add_experiment_record`/`update_project_doc`
    #: 直接寫入（不建 approval），annotation 數值跟 `creates_approval` 一樣
    #: （readOnlyHint=False, destructiveHint=False——寫入但可逆、非破壞
    #: 性），刻意另外取一個名字避免誤讀成「這兩個工具也只是建立
    #: pending approval」（它們不是，見上方模組層註解的完整說明）。
    direct_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

    @mcp.tool(
        description=(
            "List all configured servers (Server A + GPU worker machines) with "
            "their current live status: online/offline, GPU count and per-GPU "
            "utilization/memory, load average, whether the machine is enabled "
            "for scheduling, and its tags. No arguments. Read-only, maps to "
            "GET /servers on the dispatch center."
        ),
        annotations=read_only,
    )
    async def get_servers() -> str:
        return await _get("/servers")

    @mcp.tool(
        description=(
            "List training/eval/sync jobs known to the dispatch center, most "
            "recent last. Optional `status` filters to one of: queued, "
            "running, done, failed, cancelled, stopped (omit to get all "
            "statuses). Optional `project` filters to jobs belonging to one "
            "registered project (can be combined with `status`). Optional "
            "`limit` caps how many of the most recent jobs are returned "
            "(default 20, hard max 20). Each item is a compact summary (id, "
            "type, project, truncated command, status, server, priority, "
            "created_at) — use get_job for full detail on one job. "
            "Read-only, maps to GET /jobs (limit applied client-side in the "
            "bridge, then truncated to the most recent N)."
        ),
        annotations=read_only,
    )
    async def list_jobs(
        status: Optional[str] = None,
        limit: Optional[int] = None,
        project: Optional[str] = None,
    ) -> str:
        n = _clamp_int(limit, default=20, lo=1, hi=20)
        params: dict = {}
        if status:
            params["status"] = status
        if project:
            params["project"] = project
        data, error = await _dispatch_get_raw(
            config, "/jobs", params or None, client=http_client
        )
        if error is not None:
            return error
        # GET /jobs 沒有 limit 參數（見 app/main.py），這裡在 bridge 端做
        # client-side 截取：db.list_jobs() 是 `ORDER BY id ASC`，取尾端 N
        # 筆就是「最近的 N 筆」。
        if isinstance(data, list) and len(data) > n:
            data = data[-n:]
        return _to_json_text(data)

    @mcp.tool(
        description=(
            "Get full detail for a single job by its integer id: command, "
            "project, dependency/GPU requirements, status, assigned server, "
            "priority, timestamps (created/started/finished), exit_code, "
            "dataset info. Read-only, maps to GET /jobs/{job_id}. Returns an "
            "error string if the job does not exist."
        ),
        annotations=read_only,
    )
    async def get_job(job_id: int) -> str:
        return await _get(f"/jobs/{job_id}")

    @mcp.tool(
        description=(
            "Get the tail of a job's log output. `lines` controls how many "
            "lines to fetch (default 40, hard max 80). For a currently "
            "running job this is a best-effort live tail via SSH on the "
            "dispatch center side; for finished jobs it's the last saved "
            "log_tail. Read-only, maps to GET /jobs/{job_id}/log."
        ),
        annotations=read_only,
    )
    async def get_job_log(job_id: int, lines: Optional[int] = None) -> str:
        n = _clamp_int(lines, default=40, lo=1, hi=80)
        return await _get(f"/jobs/{job_id}/log", {"lines": n})

    @mcp.tool(
        description=(
            "List pending-approval requests (and optionally other statuses) "
            "waiting on the web UI: enqueue new jobs, stop a running job, "
            "server add/update/disable, project import/ignore, inventory "
            "scans, etc. Optional `status` filters (e.g. pending, approved, "
            "rejected); omit to get all. IMPORTANT: this tool only lets you "
            "SEE approval requests — there is no tool anywhere in this "
            "connector to approve or reject them; that action must be done "
            "by a human on the web UI. Read-only, maps to GET /approvals."
        ),
        annotations=read_only,
    )
    async def list_approvals(status: Optional[str] = None) -> str:
        params = {"status": status} if status else None
        return await _get("/approvals", params)

    @mcp.tool(
        description=(
            "List the most recent audit log events (job lifecycle, approval "
            "decisions, server config changes, etc.), newest first. `n` caps "
            "how many events to return (default 50, hard max 50). Read-only, "
            "maps to GET /events (audit.jsonl tail)."
        ),
        annotations=read_only,
    )
    async def list_events(n: Optional[int] = None) -> str:
        count = _clamp_int(n, default=50, lo=1, hi=50)
        return await _get("/events", {"n": count})

    @mcp.tool(
        description=(
            "List all registered projects (name, repo_or_path, default "
            "dataset requirement, default_command, require_tag, setup_cmd). "
            "No arguments. Read-only, maps to GET /projects."
        ),
        annotations=read_only,
    )
    async def list_projects() -> str:
        return await _get("/projects")

    @mcp.tool(
        description=(
            "List all registered datasets (name, version, size_bytes, "
            "source_path, created_at, file_count). No arguments. Read-only, "
            "maps to GET /datasets."
        ),
        annotations=read_only,
    )
    async def list_datasets() -> str:
        return await _get("/datasets")

    # -----------------------------------------------------------------------
    # 階段 16（PLAN.md Q.3 節）：一個唯讀工具，一對一映射
    # GET /datasets/{name}/{version}/card。
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Get the dataset card for one specific dataset version: what it "
            "is, how it was made (preprocessing/generation method, source "
            "data), what it was derived from (if any, forming a lineage "
            "chain), custom counts (e.g. train/val/test split sizes, class "
            "counts) the operator recorded, PLUS automatically-computed "
            "facts (file count, total size in bytes, registration "
            "timestamp, and which machines currently have this version "
            "cached) that are always available regardless of whether a "
            "card was recorded. Use this to answer questions like 'how was "
            "this dataset made', 'how many files/how big is it', or 'what "
            "version was this derived from'. IMPORTANT: `card` can be "
            "`null` — datasets registered before the dataset-card system "
            "existed have no card, and this is NOT an error: the response "
            "still includes the automatic facts, plus a `note` field and a "
            "`rendered` (markdown) field that both explicitly state no "
            "card was recorded and the method/purpose is undocumented. "
            "When `card` is `null` you MUST relay that plainly ('no card "
            "recorded, method/purpose undocumented') — do NOT guess or "
            "make up how the dataset might have been produced. Read-only, "
            "maps to GET /datasets/{name}/{version}/card. Returns an error "
            "string if the dataset does not exist."
        ),
        annotations=read_only,
    )
    async def get_dataset_card(name: str, version: str) -> str:
        return await _get(f"/datasets/{name}/{version}/card")

    @mcp.tool(
        description=(
            "List project inventory candidates discovered by scanning "
            "servers' project_roots (directories that look like training "
            "projects but are not yet registered). Optional `server` filters "
            "by machine name; optional `status` filters by candidate status "
            "(e.g. pending, imported, ignored). Read-only, maps to GET "
            "/inventory/candidates."
        ),
        annotations=read_only,
    )
    async def list_project_candidates(
        server: Optional[str] = None, status: Optional[str] = None
    ) -> str:
        params: dict = {}
        if server:
            params["server"] = server
        if status:
            params["status"] = status
        return await _get("/inventory/candidates", params or None)

    @mcp.tool(
        description=(
            "Get full detail for a single project inventory candidate by its "
            "id string (path, server, detected dataset info, status, "
            "discovered_at). Read-only, maps to GET "
            "/inventory/candidates/{candidate_id}. Returns an error string "
            "if the candidate does not exist."
        ),
        annotations=read_only,
    )
    async def get_project_candidate(candidate_id: str) -> str:
        return await _get(f"/inventory/candidates/{candidate_id}")

    @mcp.tool(
        description=(
            "Get the full recent-activity snapshot for one registered "
            "project: its metadata, all known instances (server/path/git "
            "info), the live GPU/disk status of each machine hosting it, "
            "the 10 most recent jobs (status/exit_code/duration) plus the "
            "latest job's saved log tail, AND — for each currently-online "
            "instance — a read-only SSH probe of the actual machine: a "
            "list of files modified in the last 3 days (path + mtime, "
            "secret files filtered out by name, capped at 50) and the tail "
            "(up to 8KB each, at most 3 files, newest mtime first) of the "
            "most recently modified *.log/*.out/*train*.txt files. "
            "IMPORTANT: this means the response CAN CONTAIN THE CONTENTS "
            "OF TRAINING LOGS, including ones started manually outside "
            "this dispatch center — if such a log happens to print "
            "secrets, credentials, or sensitive data samples, that content "
            "is included verbatim and will flow to OpenAI as part of this "
            "tool's response (the dispatch center can filter out secret "
            "*files* by name, e.g. .env/*.pem/id_rsa*, but cannot filter "
            "content a user chose to print inside a regular log file). "
            "Offline machines are skipped (noted as `skipped: \"offline\"`), "
            "never probed. If the project has no registered instances yet, "
            "the `activity` field is a short explanatory message instead "
            "of a list. The 3-day/3-file/8KB limits are fixed and cannot "
            "be overridden by arguments. Read-only, always executes "
            "immediately with no approval needed (same as get_job_log), "
            "maps to GET /projects/{project_name}/activity. Returns an "
            "error string if the project does not exist."
        ),
        annotations=read_only,
    )
    async def get_project_activity(project_name: str) -> str:
        return await _get(f"/projects/{project_name}/activity")

    # -----------------------------------------------------------------------
    # PLAN.md M.1 節（階段 12）：two read-only file-browsing tools. Both map
    # to dispatch-center endpoints that execute immediately (no approval),
    # scoped strictly to a project's already-registered instance path(s) —
    # they cannot browse or read anything outside that registered path.
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "List files under a registered project's instance directory on "
            "one of its machines (relative paths only, secret-looking "
            "filenames such as .env/*.pem/id_rsa*/credentials* are filtered "
            "out by name, capped at 200 entries, at most 4 directory levels "
            "deep). Read-only, always executes immediately (no approval "
            "needed). `server` is optional — if the project is registered "
            "on exactly one machine it is auto-selected, otherwise you must "
            "specify which machine. `subdir` is optional, a path relative "
            "to the project's registered root to list a subdirectory "
            "instead of the whole thing — must not start with `/` or "
            "contain a `..` path segment (rejected outright, no SSH "
            "attempted). Maps to GET /projects/{project_name}/files. "
            "Returns an error string if the project does not exist or has "
            "no registered instance matching the given server."
        ),
        annotations=read_only,
    )
    async def list_project_files(
        project_name: str, server: Optional[str] = None, subdir: Optional[str] = None
    ) -> str:
        params: dict = {}
        if server:
            params["server"] = server
        if subdir:
            params["subdir"] = subdir
        return await _get(f"/projects/{project_name}/files", params or None)

    @mcp.tool(
        description=(
            "Read the content of a single file (first 64KB) inside a "
            "registered project's instance directory on one of its "
            "machines. `file_path` MUST be a path relative to the "
            "project's registered root — absolute paths, any `..` path "
            "segment, or secret-looking filenames (.env/*.pem/id_rsa*/"
            "credentials* etc.) are rejected outright with no SSH attempt "
            "made at all. `server` is optional, same auto-select/must-"
            "specify rule as list_project_files. Read-only, always "
            "executes immediately (no approval needed). Maps to GET "
            "/projects/{project_name}/file. Returns an error string if the "
            "project/instance does not exist or the path is rejected."
        ),
        annotations=read_only,
    )
    async def read_project_file(
        project_name: str, file_path: str, server: Optional[str] = None
    ) -> str:
        params: dict = {"path": file_path}
        if server:
            params["server"] = server
        return await _get(f"/projects/{project_name}/file", params)

    # -----------------------------------------------------------------------
    # PLAN.md J.2 節：兩個「只建 pending approval」的寫入工具。兩者都原樣
    # 轉呼叫調度中心既有端點，不重做危險指令攔截／approval 建立／sync 計畫
    # 等任何判斷——全部沿用調度中心既有邏輯（見模組 docstring 鐵律 2）。
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Request that a new job be enqueued on the dispatch center. "
            "IMPORTANT — this does NOT run anything itself: it creates an "
            "approval request on the dispatch center, tagged as coming from "
            "this ChatGPT connector. In most cases this will be a PENDING "
            "approval request that a human must review and click Approve "
            "on the web UI before the job is actually queued or run on any "
            "machine — after calling this tool you must NOT tell the user "
            "the job is running, queued, or scheduled unless the response "
            "says otherwise (see below), only that a pending approval "
            "request has been created for a human to review. However, the "
            "operator may have pre-defined deterministic auto-approval "
            "rules (configured in advance on the dispatch center, not by "
            "this connector or by you) that match this request; the "
            "response will clearly indicate `auto_approved: true` in that "
            "case, meaning the request has already been approved by one of "
            "those pre-defined rules and the job has been queued — you may "
            "tell the user it has been queued, but still may not claim it "
            "is already running (queueing and running are different "
            "things; check job status separately if needed). Dangerous/"
            "blacklisted commands (e.g. `rm -rf`, fork bombs, etc.) are "
            "rejected outright by the dispatch center at this step — no "
            "approval request is created for those (auto-approval rules "
            "cannot override this), and this tool returns the rejection "
            "reason as plain text instead. Arguments: `command` (required, "
            "the shell command to run); `type` (one of adhoc/train/sync/"
            "setup, default adhoc); `project` (registered project name, "
            "optional); `pin_server` (force a specific machine name, "
            "optional); `require_tag` (only match servers with this tag, "
            "optional); `priority` (one of low/normal/high, default "
            "normal); `source_coding_run_id` (optional integer — PLAN.md "
            "N.7, Codex Worker v2 downstream bundle flow: reference a "
            "completed coding_run's changes.bundle as this job's starting "
            "point. Only valid when the coding_run is status=done with a "
            "result_commit and a locally-recovered changes.bundle, and "
            "`pin_server` is set to a machine that has a registered "
            "instance of the same project; the dispatch center validates "
            "all of this upfront and rejects with a plain error if not — "
            "no approval is created in that case). Maps to POST /dispatch "
            "on the dispatch center — the same endpoint the web UI itself "
            "uses, so all existing safety checks (dangerous-command "
            "blacklist, dataset-requirement validation, automatic "
            "sync/setup planning for train jobs) apply exactly as they do "
            "there."
        ),
        annotations=creates_approval,
    )
    async def request_enqueue_job(
        command: str,
        type: Optional[str] = None,
        project: Optional[str] = None,
        pin_server: Optional[str] = None,
        require_tag: Optional[str] = None,
        priority: Optional[str] = None,
        source_coding_run_id: Optional[int] = None,
    ) -> str:
        body: dict = {"command": command, "source": "chatgpt"}
        if type is not None:
            body["type"] = type
        if project is not None:
            body["project"] = project
        if pin_server is not None:
            body["pin_server"] = pin_server
        if require_tag is not None:
            body["require_tag"] = require_tag
        if priority is not None:
            body["priority"] = priority
        if source_coding_run_id is not None:
            body["source_coding_run_id"] = source_coding_run_id
        data, error = await _dispatch_post_raw(config, "/dispatch", body, client=http_client)
        if error is not None:
            return error
        if isinstance(data, dict) and data.get("auto_approved"):
            return _to_json_text(
                {
                    "status": "AUTO-APPROVED — a pre-defined deterministic rule set up "
                    "by the operator matched this request, so it has already been "
                    "queued. This does NOT mean it is running yet (that depends on "
                    "the scheduler/queue) — do not claim it is running.",
                    "approval": data.get("approval"),
                    "job": data.get("job"),
                }
            )
        return _to_json_text(
            {
                "status": "PENDING APPROVAL — a human must approve this on the web UI "
                "before the job is queued or run. It is NOT running yet.",
                "approval": data,
            }
        )

    @mcp.tool(
        description=(
            "Request that a currently running job be stopped. IMPORTANT — "
            "this does NOT stop anything itself: it creates a stop-approval "
            "request on the dispatch center, tagged as coming from this "
            "ChatGPT connector. In most cases this will be a PENDING "
            "approval request that a human must review and click Approve "
            "on the web UI before the job is actually killed on the worker "
            "machine — after calling this tool you must NOT tell the user "
            "the job has been stopped or cancelled unless the response "
            "says otherwise (see below), only that a pending stop-approval "
            "request has been created for a human to review. However, the "
            "operator may have pre-defined deterministic auto-approval "
            "rules (configured in advance on the dispatch center, not by "
            "this connector or by you) that match this request; the "
            "response will clearly indicate `auto_approved: true` in that "
            "case, meaning the stop has already been approved by one of "
            "those pre-defined rules and executed. Only works for jobs "
            "currently in `running` status; if the job does not exist or "
            "is not running, the dispatch center rejects the request and "
            "this tool returns that rejection reason as plain text instead "
            "of creating anything. Argument: `job_id` (required, integer). "
            "Maps to POST /jobs/{job_id}/stop on the dispatch center — the "
            "same endpoint the web UI itself uses."
        ),
        annotations=creates_approval,
    )
    async def request_stop_job(job_id: int) -> str:
        data, error = await _dispatch_post_raw(
            config, f"/jobs/{job_id}/stop", {"source": "chatgpt"}, client=http_client
        )
        if error is not None:
            return error
        if isinstance(data, dict) and data.get("auto_approved"):
            return _to_json_text(
                {
                    "status": "AUTO-APPROVED — a pre-defined deterministic rule set up "
                    "by the operator matched this request, so the stop has already "
                    "been carried out.",
                    "approval": data.get("approval"),
                    "job": data.get("job"),
                }
            )
        return _to_json_text(
            {
                "status": "PENDING APPROVAL — a human must approve this on the web UI "
                "before the job is stopped. It has NOT been stopped yet.",
                "approval": data,
            }
        )

    # -----------------------------------------------------------------------
    # PLAN.md M.2 節（階段 12）：request_apply_patch — creates a pending
    # apply_patch approval, never applies anything itself. Unlike
    # request_enqueue_job/request_stop_job, this kind can NEVER be
    # auto-approved by the operator's auto_approve.yaml rules (that engine
    # only recognizes kind=enqueue/stop) — every diff always waits for a
    # human to read it and click Approve on the web UI, no exceptions.
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Request that a unified diff be applied to a registered "
            "project's git repository on one of its machines. IMPORTANT — "
            "this does NOT apply anything itself: it only creates a "
            "PENDING approval request on the dispatch center containing "
            "the full diff text; a human must read the diff and click "
            "Approve on the web UI before anything happens. This request "
            "can NEVER be auto-approved by any pre-configured rule (unlike "
            "request_enqueue_job/request_stop_job) — it always requires "
            "an explicit human review of the diff content, with no "
            "exceptions. If/when approved, the dispatch center runs a "
            "strictly controlled, read-after-check sequence entirely over "
            "SSH: verify the target path is a git repository (otherwise "
            "reject, no changes made); record the current branch; write "
            "the diff to a file on the machine; run `git apply --check` "
            "first (if that fails — including conflicts with uncommitted "
            "local changes — the request is rejected and NO changes are "
            "made at all, the failure reason is recorded); only if the "
            "check succeeds does it check out a NEW branch named "
            "`ai-patch-<approval_id>`, apply the diff, and commit it there "
            "under a fixed bot identity. The original branch is left "
            "untouched and nothing is ever pushed anywhere — reverting "
            "just means checking the original branch back out. Arguments: "
            "`project` (required, registered project name); `server` "
            "(required, exact machine name — this project must already "
            "have a registered instance path on that machine); `diff` "
            "(required, the full unified diff text, e.g. `git diff` "
            "output — max 100KB, must look like a real unified diff with "
            "`--- `/`+++ ` or `diff --git` headers); `description` "
            "(optional, short human-readable summary shown on the "
            "approval card and used in the commit message). The dispatch "
            "center validates upfront (rejecting outright with no "
            "approval created, before any human review) that every file "
            "path touched by the diff is relative (no leading `/`), has "
            "no `..` path segments, and is not a secret-looking filename "
            "(.env/*.pem/id_rsa*/credentials* etc.) — same rules as "
            "read_project_file. Maps to POST "
            "/projects/{project}/apply-patch-request."
        ),
        annotations=creates_approval,
    )
    async def request_apply_patch(
        project: str, server: str, diff: str, description: Optional[str] = None
    ) -> str:
        body: dict = {"server": server, "diff": diff}
        if description is not None:
            body["description"] = description
        data, error = await _dispatch_post_raw(
            config, f"/projects/{project}/apply-patch-request", body, client=http_client
        )
        if error is not None:
            return error
        return _to_json_text(
            {
                "status": "PENDING APPROVAL — a human must read the full diff and "
                "approve it on the web UI before anything is applied. This kind of "
                "request can never be auto-approved. Nothing has been changed yet.",
                "approval": data,
            }
        )

    # -----------------------------------------------------------------------
    # PLAN.md N.1 節（階段 13）：request_coding_task — creates a pending
    # coding_task approval, never dispatches anything itself. Like
    # request_apply_patch, this kind can NEVER be auto-approved by the
    # operator's auto_approve.yaml rules (that engine only recognizes
    # kind=enqueue/stop) — every instruction always waits for a human to
    # read it and click Approve on the web UI, no exceptions.
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Request that an AI coding agent (OpenAI Codex CLI, running "
            "non-interactively as `codex exec` on a single, operator-"
            "designated Central Codex Runner machine — PLAN.md N section, "
            "Codex Worker v2) carry out a natural-language coding task "
            "against a registered project. IMPORTANT — this does NOT run "
            "anything itself: it only creates a PENDING approval request "
            "on the dispatch center containing the full instruction text; "
            "a human must read the instruction and click Approve on the "
            "web UI before anything happens. This request can NEVER be "
            "auto-approved by any pre-configured rule (unlike "
            "request_enqueue_job/request_stop_job) — it always requires an "
            "explicit human review of the instruction content, with no "
            "exceptions. There is deliberately NO way to choose which "
            "machine runs Codex — the dispatch center always uses whatever "
            "single machine the operator configured as the Codex Runner "
            "(`CODEX_RUNNER_SERVER`); if that is not configured, this tool "
            "returns a plain error and creates nothing. If/when approved, "
            "the dispatch center: resolves the project's source repository "
            "on the Runner (an existing registered instance there, or a "
            "bare mirror cloned from the project's registered git remote — "
            "never an arbitrary URL); creates an independent git worktree "
            "on a NEW branch named `ai-task-<approval_id>` (the original "
            "instance's branch/working tree is never touched); runs `codex "
            "exec` there with a writable workspace sandbox (network access "
            "is an operator-controlled global setting, not something this "
            "request can turn on); commits whatever changes remain "
            "afterwards under a fixed bot identity (skipped if nothing "
            "changed, recorded as `no_changes`); and packages the result as "
            "a git bundle plus a diff — nothing is ever pushed to any "
            "external remote, and the coding agent never modifies a "
            "project's live/production working directory directly. Use "
            "get_job_log on the returned job id to watch progress, or "
            "get_coding_run/list_coding_runs once it finishes for "
            "structured results (base/result commit, test outcome, diff "
            "summary). Arguments: `project` (required, registered project "
            "name); `instruction` (required, the full natural-language "
            "task description for the agent — max 4000 characters); "
            "`base_branch` (optional, git branch name to start the work "
            "branch from — only `[A-Za-z0-9._/-]` characters allowed; "
            "defaults to the repository's current HEAD); "
            "`validation_target` (optional, an enabled machine name where "
            "a later smoke-test/training job might run against this "
            "result — purely informational metadata, does NOT affect "
            "where Codex itself runs). The dispatch center validates "
            "upfront (rejecting outright with no approval created, before "
            "any human review) that the instruction is non-empty and "
            "within the length limit, that `base_branch` (if given) "
            "matches the allowed character set, that `validation_target` "
            "(if given) is an enabled machine, and that the project has a "
            "usable repository source on the Runner (an existing instance "
            "or a registered git remote) — otherwise it is rejected with a "
            "clear message telling the operator to import the project onto "
            "the Runner or register a git remote first. Maps to POST "
            "/projects/{project}/coding-task-request."
        ),
        annotations=creates_approval,
    )
    async def request_coding_task(
        project: str,
        instruction: str,
        base_branch: Optional[str] = None,
        validation_target: Optional[str] = None,
    ) -> str:
        body: dict = {"instruction": instruction}
        if base_branch is not None:
            body["base_branch"] = base_branch
        if validation_target is not None:
            body["validation_target"] = validation_target
        data, error = await _dispatch_post_raw(
            config, f"/projects/{project}/coding-task-request", body, client=http_client
        )
        if error is not None:
            return error
        return _to_json_text(
            {
                "status": "PENDING APPROVAL — a human must read the full instruction "
                "and approve it on the web UI before the coding agent runs. This kind "
                "of request can never be auto-approved. Nothing has been changed yet.",
                "approval": data,
            }
        )

    # -----------------------------------------------------------------------
    # PLAN.md N.6 節（階段 13）：三個唯讀工具，一對一映射 Codex Worker v2 的
    # 唯讀端點——不透露任何憑證/token/email（見各自 description）。
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Get the live status of the Central Codex Runner (PLAN.md N "
            "section, Codex Worker v2) — the single machine designated to "
            "run OpenAI Codex CLI for coding tasks. If the operator has not "
            "configured a Codex Runner at all, returns `{\"configured\": "
            "false}` and nothing else (the coding-task feature is entirely "
            "disabled in that case). Otherwise returns: `server` (machine "
            "name); `online` (live reachability, from the same monitoring "
            "loop as get_servers); `codex_installed`/`codex_version` "
            "(whether the `codex` CLI is present and its version string, "
            "from a lightweight read-only SSH probe cached for ~30 "
            "seconds); `authenticated` (whether `codex login status` "
            "reports a logged-in session — the raw output of that command, "
            "any account email, and any token/credential material are "
            "NEVER included here or anywhere else in this API, only this "
            "boolean); `auth_mode` (\"chatgpt\" or \"api_key\"); `busy`/"
            "`running_job_id` (whether a coding job is currently running "
            "on the Runner, and which one); `max_concurrency` (how many "
            "coding jobs may run at once — chatgpt auth mode is always "
            "forced to 1). No arguments. Read-only, maps to GET "
            "/codex-runner/status."
        ),
        annotations=read_only,
    )
    async def get_codex_runner_status() -> str:
        return await _get("/codex-runner/status")

    @mcp.tool(
        description=(
            "List Codex coding-agent runs (PLAN.md N section, Codex Worker "
            "v2), most recent first. Optional `status` filters to one of: "
            "queued, running, done, failed, no_changes, secret_violation, "
            "path_policy_violation. "
            "Optional `project` filters to one registered project name. "
            "`limit` caps how many to return (default 20, hard max 20). "
            "Each item is a compact summary: id, approval_id, job_id, "
            "project, runner_server, instruction, base/result branch and "
            "commit, status, test outcome, timestamps, `has_bundle` "
            "(whether a changes.bundle was produced), and error_message if "
            "any. IMPORTANT: internal filesystem paths (worktree/bundle "
            "locations on the Runner) are never included — use "
            "get_coding_run for the full instruction text plus the final "
            "agent message and diff. Read-only, maps to GET /coding-runs."
        ),
        annotations=read_only,
    )
    async def list_coding_runs(
        status: Optional[str] = None, project: Optional[str] = None, limit: Optional[int] = None
    ) -> str:
        n = _clamp_int(limit, default=20, lo=1, hi=20)
        params: dict = {"limit": n}
        if status:
            params["status"] = status
        if project:
            params["project"] = project
        return await _get("/coding-runs", params)

    @mcp.tool(
        description=(
            "Get full detail for a single Codex coding-agent run by its "
            "integer id (PLAN.md N section, Codex Worker v2): same summary "
            "fields as list_coding_runs, PLUS `final_message` (the full "
            "text Codex produced as its final reply, if the run got far "
            "enough to produce one) and `diff_patch` (the full unified "
            "diff between base_commit and result_commit, if any changes "
            "were made) — both read from locally-recovered result files "
            "and truncated at 64KB if larger; both are `null` if not yet "
            "available (e.g. run still in progress) or the run made no "
            "changes. IMPORTANT: internal filesystem paths (worktree/"
            "bundle locations on the Runner) are never included. Read-only, "
            "maps to GET /coding-runs/{coding_run_id}. Returns an error "
            "string if the id does not exist."
        ),
        annotations=read_only,
    )
    async def get_coding_run(coding_run_id: int) -> str:
        return await _get(f"/coding-runs/{coding_run_id}")

    # -----------------------------------------------------------------------
    # 階段 15 Phase A（PLAN.md P.1.3 節）：一個唯讀工具，一對一映射
    # GET /projects/matrix。
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Get a projects x servers matrix view: every configured server "
            "(including disabled ones), every registered project, and for "
            "each project which of those servers currently has a known "
            "instance of it (path, git_remote, git_branch, git_commit, "
            "dirty — a snapshot from the last inventory scan or import, NOT "
            "a live SSH probe; use get_project_activity for live status). "
            "Also returns `pending_candidates`, a count of not-yet-decided "
            "inventory candidates per server, useful for spotting machines "
            "with unreviewed projects. No arguments. Read-only, purely "
            "reads from the dispatch center's database (no SSH), maps to "
            "GET /projects/matrix."
        ),
        annotations=read_only,
    )
    async def get_projects_matrix() -> str:
        return await _get("/projects/matrix")

    # -----------------------------------------------------------------------
    # 專案詳情頁計畫第 4 節：一個唯讀工具（get_project_timeline）＋兩個
    # 「直接寫入、不建 approval」的寫入工具（add_experiment_record/
    # update_project_doc）。安全模型延伸見上方模組層註解的完整說明。
    # -----------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Get the experiment timeline for one registered project: a "
            "merged, newest-first view of manually-added notes/"
            "observations/conclusions/decisions (experiment_records), "
            "training/eval jobs (jobs), and Codex coding-agent runs "
            "(coding_runs) — merged at query time from three separate "
            "tables, so historical jobs/coding_runs that existed before "
            "this feature show up automatically. Optional `q` does a "
            "case-insensitive substring search across all three sources "
            "(record title/content, job command, coding-run instruction/"
            "result_branch/error_message). Optional `kinds` (a list of "
            "strings) filters to specific item types: note, observation, "
            "conclusion, decision (experiment_records kinds), and/or "
            "\"job\", \"coding_run\" (whole source types) — omit for no "
            "filtering. `limit` caps how many items to return (default 20, "
            "hard max 50). `before_ts` is a pagination cursor: pass the "
            "`next_before_ts` value from a previous response to fetch "
            "older items; the response also includes `has_more` (whether "
            "there are more items beyond this page). Read-only, maps to "
            "GET /projects/{project_name}/timeline. Returns an error "
            "string if the project does not exist."
        ),
        annotations=read_only,
    )
    async def get_project_timeline(
        project_name: str,
        q: Optional[str] = None,
        limit: Optional[int] = None,
        before_ts: Optional[str] = None,
        kinds: Optional[list[str]] = None,
    ) -> str:
        n = _clamp_int(limit, default=20, lo=1, hi=50)
        params: dict = {"limit": n}
        if q:
            params["q"] = q
        if before_ts:
            params["before_ts"] = before_ts
        if kinds:
            params["kinds"] = ",".join(kinds)
        return await _get(f"/projects/{project_name}/timeline", params)

    @mcp.tool(
        description=(
            "Add a manual note/observation/conclusion/decision (markdown "
            "text) to a registered project's experiment timeline. "
            "IMPORTANT: unlike request_enqueue_job/request_stop_job/"
            "request_apply_patch/request_coding_task above, this tool does "
            "NOT create a pending approval — it writes directly and takes "
            "effect immediately. This is a deliberate, narrow exception to "
            "the 'writes always go through human approval' rule used "
            "elsewhere in this connector: it only ever writes a plain-text "
            "database row (never runs anything, never touches any "
            "machine), and it is fully reversible (a human can edit or "
            "delete it later from the web UI). It can NEVER be used to run "
            "commands, dispatch jobs, or approve/reject anything — there is "
            "still no approve/reject capability anywhere in this connector. "
            "Every record created through this tool is tagged with "
            "author=\"agent:chatgpt\" so operators can always tell it apart "
            "from records a human typed themselves. Arguments: "
            "`project_name` (required, registered project name); `content` "
            "(required, the markdown text of the note); `kind` (optional, "
            "one of note/observation/conclusion/decision, default note); "
            "`title` (optional, short title); `job_id` (optional, integer, "
            "attach this note to an existing dispatch job — a weak "
            "reference, not validated to exist); `coding_run_id` (optional, "
            "integer, attach this note to an existing Codex coding run — "
            "same weak-reference semantics). Maps to POST "
            "/projects/{project_name}/records. Returns an error string if "
            "the project does not exist or kind is invalid."
        ),
        annotations=direct_write,
    )
    async def add_experiment_record(
        project_name: str,
        content: str,
        kind: Optional[str] = None,
        title: Optional[str] = None,
        job_id: Optional[int] = None,
        coding_run_id: Optional[int] = None,
    ) -> str:
        body: dict = {"content": content, "author": "agent:chatgpt"}
        if kind is not None:
            body["kind"] = kind
        if title is not None:
            body["title"] = title
        if job_id is not None:
            body["job_id"] = job_id
        if coding_run_id is not None:
            body["coding_run_id"] = coding_run_id
        data, error = await _dispatch_post_raw(
            config, f"/projects/{project_name}/records", body, client=http_client
        )
        if error is not None:
            return error
        return _to_json_text(data)

    @mcp.tool(
        description=(
            "Update one of a registered project's free-text markdown "
            "documentation fields: its goal, its optimization method/notes, "
            "or its current progress. The given `content` REPLACES the "
            "entire field (not an append/merge). IMPORTANT: like "
            "add_experiment_record above, this tool does NOT create a "
            "pending approval — it writes directly and takes effect "
            "immediately. This is a deliberate, narrow exception to the "
            "'writes always go through human approval' rule used elsewhere "
            "in this connector: it only ever overwrites a plain-text "
            "database field (never runs anything, never touches any "
            "machine), and it is fully reversible (a human can edit it "
            "back on the web UI, and past values remain visible in the "
            "audit log via list_events). Arguments: `project_name` "
            "(required, registered project name); `field` (required, one "
            "of exactly: goal, optimization_notes, progress — no other "
            "field name is accepted, this tool cannot touch the project's "
            "name/path/dataset settings or anything else); `content` "
            "(required, the new full markdown text for that field). Maps "
            "to PATCH /projects/{project_name}. Returns an error string if "
            "the project does not exist or `field` is not one of the three "
            "allowed names."
        ),
        annotations=direct_write,
    )
    async def update_project_doc(project_name: str, field: str, content: str) -> str:
        if field not in ("goal", "optimization_notes", "progress"):
            return (
                "ERROR: field must be one of: goal, optimization_notes, progress "
                f"(got: {field!r})"
            )
        body = {field: content}
        data, error = await _dispatch_patch_raw(
            config, f"/projects/{project_name}", body, client=http_client
        )
        if error is not None:
            return error
        return _to_json_text(data)

    return mcp


# ---------------------------------------------------------------------------
# 兩層認證的第二層：選配 bearer token（第一層「路徑機密」不需要額外程式碼——
# FastMCP 的 streamable_http_app() 只在 `config.mcp_path` 這一條路由上掛
# handler，Starlette 對任何其他路徑本來就會回 404，這正是 PLAN.md J 要的
# 「路徑機密錯誤 -> 404（不是 401，不透露端點存在）」，不需要我們自己寫
# 額外的比對程式碼、也不會不小心把它寫成 401。）
#
# 這裡只需要處理第二層：`Authorization: Bearer ...` 有帶的話必須等於
# MCP_BRIDGE_TOKEN；沒帶且路徑對 -> 放行（相容 ChatGPT connector 的
# no-auth 模式）。
#
# 為什麼不用 FastMCP/mcp SDK 內建的 auth（token_verifier / OAuth Resource
# Server）：那一整套是為了完整的 OAuth 2.1 flow 設計的（protected resource
# metadata、WWW-Authenticate 挑戰、client registration...），一旦設定
# token_verifier，SDK 會要求「每個請求都必須通過驗證」，沒有「沒帶
# Authorization header 就放行」這個選項——不符合 PLAN.md J 訂的「選配
# bearer、沒帶且路徑對就放行」語意。手寫這層極簡 middleware 比硬套用/繞過
# SDK 的 OAuth 機制更直接、更容易稽核。
# ---------------------------------------------------------------------------


class _OptionalBearerGate:
    """ASGI middleware：`bridge_token` 有設定時，若 request 帶了
    Authorization header 就必須完全匹配 `Bearer {token}`，否則 401；沒帶
    Authorization header 則直接放行（呼叫端已經先通過路徑機密這一關，見
    上方模組層註解）。`bridge_token` 未設定（None）時完全不檢查。
    """

    def __init__(self, app: ASGIApp, bridge_token: Optional[str]):
        self.app = app
        self.bridge_token = bridge_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.bridge_token:
            await self.app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or []
        supplied: Optional[str] = None
        for key, value in raw_headers:
            if key.lower() == b"authorization":
                supplied = value.decode("latin-1")
                break

        if supplied is not None:
            expected = f"Bearer {self.bridge_token}"
            if supplied != expected:
                response = JSONResponse(
                    {"error": "invalid or missing bearer token"}, status_code=401
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def create_app(config: BridgeConfig, *, http_client: Optional[httpx.AsyncClient] = None) -> ASGIApp:
    """組出完整的 ASGI app：FastMCP 的 streamable-http app（已經只掛在
    `config.mcp_path` 這一條路由上）外面再包一層選配 bearer 檢查。

    `http_client` 只給測試用（注入 `httpx.MockTransport`），正式啟動時
    一律留 `None`，每個工具呼叫各自開短命 client。
    """
    fastmcp = _build_mcp(config, http_client=http_client)
    inner_app = fastmcp.streamable_http_app()
    return _OptionalBearerGate(inner_app, config.bridge_token)


# ---------------------------------------------------------------------------
# 進入點：python -m app.mcp_bridge
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_bridge_config()
    app = create_app(config)

    import uvicorn

    print(
        f"MCP Bridge 啟動：綁定 127.0.0.1:{config.port}，"
        f"端點路徑 /mcp-***（機密已隱藏，長度 {len(config.path_secret)}）",
        file=sys.stderr,
    )
    uvicorn.run(app, host="127.0.0.1", port=config.port, log_level="info")


if __name__ == "__main__":
    main()
