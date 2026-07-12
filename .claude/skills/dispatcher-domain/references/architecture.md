# System Architecture(穩定參考,非現況快照)

本檔只記載經 repository 驗證、相對穩定的架構事實。**不記載**:當期審計發現、
優先級排序、階段完成度快照、暫時性 TODO、測試數量、已知 bug、短期優化建議
——那些屬於 snapshot,應放在 skills 之外(例如 `docs/audits/`),不是永久知識。

## 1. System architecture

集中式架構:Server A(本機)跑一個 FastAPI 單體(`app/main.py`),對 `servers.yaml`
列出的工作機做**無 agent** 的 SSH 探測與派工。工作機上只需要 `tmux`/`bash`
(GPU 機另需 `nvidia-smi`),沒有任何本系統的常駐程式。另有一個可選的獨立行程
`app/mcp_bridge.py`(MCP bridge,供 ChatGPT connector),只透過 HTTP 呼叫調度中心。

兩個常駐迴圈(FastAPI lifespan 啟動):
- **monitor loop**(預設 20s):對每台機器跑一條合成探測指令(nvidia-smi + loadavg + df),
  更新 in-memory `server_states`。
- **scheduler loop**(預設 10s):reconcile running 任務 → 卡死偵測 → blocked 標記 →
  對空閒機器挑任務派發 → 派發 `_local` sync 任務。

## 2. Component responsibilities

| 模組 | 職責 |
|---|---|
| `app/main.py` | FastAPI app、AppState(loops、in-memory 狀態、背景 task 追蹤)、全部 REST/WS 端點、auth middleware |
| `app/config.py` | 讀 `servers.yaml` 與 `.env`;`ServerConfig`/`AppConfig` |
| `app/db.py` | SQLite schema、遷移、全部 CRUD;`VALID_APPROVAL_KINDS` |
| `app/sshpool.py` | asyncssh 封裝:金鑰認證、逾時、每機序列化、全域併發上限、SFTP 寫檔 |
| `app/localrun.py` | `_local`(Server A 本地)執行層,與 sshpool 介面同形 |
| `app/monitor.py` | 探測指令建構與純函式解析(nvidia-smi/loadavg/df)、`is_idle` 判定 |
| `app/jobqueue.py` | 任務 CRUD、狀態機、依賴、哨兵協議(build_* 指令家族、reconcile) |
| `app/scheduler.py` | `pick_job()` 純函式 policy 與 `scheduler_tick()` 迴圈 |
| `app/security.py` | `is_dangerous()` 危險指令黑名單(防呆,不防惡意) |
| `app/approvals.py` | 核准流:`request_*` 建卡、`approve()` 各 kind 落地、`maybe_auto_approve()` |
| `app/autoapprove.py` | `auto_approve.yaml` 確定性規則引擎 |
| `app/audit.py` | `audit.jsonl` append-only 稽核 |
| `app/datasets.py` | 資料集註冊、manifest、sync 計畫、資料引力、空間檢查、資料卡 |
| `app/inventory.py` | 唯讀 SSH 掃描候選專案(封閉指令集、秘密過濾、禁止路徑) |
| `app/server_config.py` | servers.yaml 驗證/原子寫入/備份/熱重載、test-ssh |
| `app/activity.py` | 專案執行近況的唯讀 SSH 探測 |
| `app/results.py` / `app/jobfinish.py` | 任務結束 hook:拉結果、寄信、coding run 回填 |
| `app/stall.py` | 卡死偵測純函式(只標旗標) |
| `app/mailer.py` | SMTP 通知(未設定即跳過) |
| `app/llm.py` / `app/llm_local.py` / `app/agent_runtime.py` / `app/agent_tools.py` / `app/chat.py` | 選配 LLM 層:意圖分類、JSON tool loop、工具白名單 |
| `app/mcp_bridge.py` | 獨立行程 MCP bridge(ChatGPT),純 HTTP client |
| `app/records.py` | 實驗紀錄與時間軸合併 |
| `app/hub.py` | 中央 bare-repo hub 同步與部署輔助 |
| `static/index.html` | 單檔 vanilla JS 前端(輪詢) |

## 3. Data flow(一個訓練任務的生命週期)

1. 入口(Web/API/agent/MCP)→ `request_enqueue_approval()`:危險指令當場 400;
   `type=train`+`project`+`pin_server` 時同場算好 sync/setup 計畫附在 payload。
2. 核准(人工 `POST /approve/{id}`、web 一步生效、或 auto_approve 規則)→
   `enqueue_job()` 入列,自動建立依賴的 sync/setup 任務。
3. sync 任務 pin 到 `_local` 在 Server A 執行(rsync 推出),完成後 manifest 驗證
   通過才登記 `dataset_cache`。
4. scheduler 對空閒機器 `pick_job()`(pin/tag/資料引力資格過濾/priority/FIFO)→
   **先標 running** → SSH 派發(mkdir + SFTP 寫 cmd.sh/run.sh + tmux 起 session)。
5. 每輪 reconcile 依哨兵協議判定(exit_code 檔在→done/failed;tmux 在→running;
   都不在→requeue),終態觸發背景 hook(拉 `results/{id}/`、寄信、寫稽核)。

## 4. Trust boundaries

- **人 vs 系統**:批准權只在持有 AUTH_TOKEN 的人(網頁/API);`source` 標記僅記
  來源通道,不是權限層(token 持有者本來就有完整核准權)。
- **調度中心 vs LLM**:LLM(anthropic/vLLM/ChatGPT)在邊界外,能力上限=唯讀查詢
  +建立 pending approval;沒有 approve 工具、沒有 shell 工具。
- **調度中心 vs MCP bridge**:行程隔離,bridge 只是 HTTP client(路徑機密+選配
  bearer 兩層認證,錯誤一律 404)。
- **Server A vs 工作機**:單向信任——Server A 持金鑰可登入工作機;工作機互不
  相通、也連不回 Server A(跨機傳遞一律 bundle、Server A 中轉)。
- **服務綁定**:只綁私網(預設 `127.0.0.1`,絕不 `0.0.0.0`);對外靠 Cloudflare
  Tunnel(outbound-only)。

## 5. Persistence and runtime-state ownership

| 存放區 | 內容 | 所有權/生命週期 |
|---|---|---|
| `jobqueue.db`(SQLite) | jobs、approvals、projects、datasets、dataset_cache、project_candidates、project_instances、coding_runs、experiment_records | 持久真相;schema 演進走 SCHEMA+`_*_COLUMN_MIGRATIONS` 雙軌 |
| 工作機 `agent_jobs/{id}/` | cmd.sh/run.sh/job.log/exit_code 哨兵檔 | 任務執行的分散式真相;重啟後 reconcile 的依據 |
| in-memory `server_states`/`server_configs`(AppState) | 監控讀數、機器設定快取 | 可拋棄;重啟歸零由 monitor/`servers.yaml` 重建 |
| `servers.yaml` | 機器設定唯一來源 | 修改必經 backup→原子寫→熱重載 |
| `audit.jsonl` | 稽核事件流 | append-only,程式永不改寫 |

## 6. Approval flow

`approvals` 表統一承載所有 kind(見 `VALID_APPROVAL_KINDS`)。狀態機:
pending → approved / rejected(終態)。三種核准路徑,全部落到同一個 `approve()`:
1. 人工:`POST /approve/{id}`。
2. web 一步生效:`source="web"` 且 `WEB_DIRECT_EXECUTE=true`(提案者=批准者,
   同請求內建卡+核准,`approved_by: "web-direct"`)。
3. 自動核准規則:`auto_approve.yaml` 命中(**kind 白名單只有 enqueue/stop**,
   `approved_by: "auto-rule-N"`)。
不論路徑:危險指令在建卡當下已被擋;`approve()` 落地前重驗當下狀態。

## 7. LLM and MCP permission boundaries

- 工具分兩類:唯讀(查狀態/任務/專案/資料卡…)與 `request_*`(只建 pending approval)。
- 工具註冊表本身就是安全邊界:`app/agent_tools.py` 的 `TOOLS` 查表分派(不用
  getattr),不得 import `sshpool`/`localrun`/`subprocess`;需要 SSH 的唯讀工具
  經注入的 `ctx.ssh_run` callable。
- runtime 防護:工具步數上限、結果截斷、併發 semaphore、JSON 解析修正機會。
- prompt injection 的血本封頂設計:最壞情況只是一張待審卡片。

## 8. SSH execution boundary

- 全部遠端執行收斂在 `app/sshpool.py`(SSH/SFTP)與 `app/localrun.py`(`_local`);
  上層模組只依賴注入的 `ssh_run`/`ssh_write_file` callable 形狀。
- 指令由純函式 `build_*` 建構;使用者指令原文只經 SFTP 落地成 cmd.sh。
- 唯讀探測(inventory/activity/test-ssh)是封閉指令集,秘密過濾在 Python 層。
- 逾時明確、每機序列化、全域併發上限;連不上=本輪跳過不下判定。

## 9. Testing boundary

- 全部測試跑在假介面上:FakeSSH、假 `local_run`、假 `send_mail`、duck-typed 假
  LLM client、`TestClient`;不需要真機/真 GPU/真 SMTP/真 key/真網路。
- 唯一例外 `tests/test_localrun.py`:用本機 `bash`/`tmux`(被測物就是本機 subprocess 層)。
- 安全邊界由測試釘住(forbidden imports/forbidden tool names 等),功能改動不得
  弱化這些斷言。

## 10. Explicit non-goals(明文記錄的「不做」)

- 不防惡意繞過:危險指令黑名單是防呆(防手滑),token 持有者本就有完整權限。
- 不做多租戶:單一共享 token,無 per-user 身分與配額。
- 不做搶佔/遷移:任務派出後不移機、不搶佔;停止必經核准。
- 不做 GPU 槽位切分(現行語意:一任務佔一台機;`gpus_needed` 欄位為未來預留)。
- 不做工作機互聯:跨機傳遞一律 Server A 中轉(bundle)。
- 不做 LLM 自主執行:LLM 永遠停在「提案」;批准通道與提案通道結構性分離。
- 前端不引框架:`static/index.html` 維持單檔 vanilla JS。
