# System Architecture（實作層穩定參考，非現況快照）

本檔只記載經 repository 驗證、相對穩定的**實作層**架構事實：系統形狀、模組職責、資料流、
持久化所有權、測試邊界。治理內容——產品定位、兩平面模型、Development Agent 邊界、信任邊界、
核准流、不變式——一律在 `docs/PLATFORM_CHARTER.md`（§4 架構模型、§6 不變式），本檔不重複。
能力現況（Implemented/Default/Pilot/Canary）以 `docs/CAPABILITY_LEDGER.md`
為準。**不記載**：當期審計發現、優先級排序、階段完成度快照、暫時性 TODO、測試數量、已知 bug。

## 1. System shape

集中式架構：Server A（本機）跑一個 FastAPI 單體（`app/main.py` + `dispatch_center/`
套件的 API v2 router 邊界），對 `servers.yaml` 列出的工作機做 SSH 探測與派工。
SSH 後端不依賴工作機上任何本系統常駐程式——遠端依賴只有 `tmux`/`bash`
（GPU 機另需 `nvidia-smi`）。工作機上「可以」另外存在經 `INV-NODE-*` 管理的
Node Agent，但 SSH 後端的行為永遠不得假設它存在（`INV-SSH-1`）；Node Agent 的
rollout 狀態以帳本為準。另有可選的獨立行程 `app/mcp_bridge.py`（MCP bridge，供 ChatGPT
connector），只透過 HTTP 呼叫平台 REST API；以及可選的 runner 主機（`.env`
`CODEX_RUNNER_SERVER`），其上的 `dispatch-agent` 服務以 Claude Agent SDK 承載
AgentSession（只出站 WS 連回 Server A，DG-AGENT-RUNTIME-V3）。

兩個常駐迴圈（FastAPI lifespan 啟動）：
- **monitor loop**（預設 20s）：對每台機器跑一條合成探測指令（nvidia-smi + loadavg + df），
  更新 in-memory `server_states`。
- **scheduler loop**（預設 10s）：reconcile running 任務 → 卡死偵測 → blocked 標記 →
  對空閒機器挑任務派發 → 派發 `_local` sync 任務。

其他背景迴圈（project instance reconcile、auto placement、audit export outbox 等）
都掛在 AppState 上，各自的旗標與週期以 `app/config.py` 與 `app/main.py` 為準。

## 2. Component responsibilities

本表只列**穩定、跨切片仍成立**的模組職責；`app/` 目前有 80+ 模組，
未列出的多半屬於 default-off 的 Product v2 / Node / audit 子系統，現況以帳本與程式本身為準。

| 模組 | 職責 |
|---|---|
| `app/main.py` | FastAPI app、AppState（loops、in-memory 狀態、背景 task 追蹤）、legacy REST/WS 端點、auth middleware |
| `dispatch_center/api/` | API v2 router 邊界、錯誤/分頁/idempotency/request-id 原語（`API_V2_ENABLED` 閘門後） |
| `app/config.py` | 讀 `servers.yaml` 與 `.env`；`ServerConfig`/`AppConfig`；全部功能開關 |
| `app/db.py` | SQLite schema、遷移、全部 CRUD；`VALID_APPROVAL_KINDS` |
| `app/migrations.py` | 版本化 additive migration |
| `app/sshpool.py` | asyncssh 封裝：金鑰認證、逾時、每機序列化、全域併發上限、SFTP 寫檔 |
| `app/localrun.py` | `_local`（Server A 本地）執行層，與 sshpool 介面同形 |
| `app/monitor.py` | 探測指令建構與純函式解析（nvidia-smi/loadavg/df）、`is_idle` 判定 |
| `app/jobqueue.py` | 任務 CRUD、狀態機、依賴、哨兵協議（build_* 指令家族、reconcile） |
| `app/scheduler.py` | `pick_job()` 純函式 policy 與 `scheduler_tick()` 迴圈 |
| `app/execution_*.py` | attempt-driven 執行：contract/plan/backend/dispatch/launch 仲裁、outbox（rollout flag 後） |
| `app/execution_plan_v2*.py` / `app/experiment_v2*.py` | ExecutionPlan v2 不可變執行意圖與 experiment matrix（一 matrix 一 approval） |
| `app/security.py` | `is_dangerous()` 危險指令黑名單（防呆，不防惡意） |
| `app/approvals.py` | 核准流：`request_*` 建卡、`approve()` 各 kind 落地、`maybe_auto_approve()` |
| `app/autoapprove.py` | `auto_approve.yaml` 確定性規則引擎 |
| `app/auto_placement.py` | policy-scoped placement 提案與決策（`INV-APPROVAL-4b`，預設關閉） |
| `app/audit.py` / `app/audit_store.py` / `app/audit_adoption.py` | legacy `audit.jsonl` append-only + 版本化 hash-chained durable 事件帳本與採用目錄 |
| `app/authentication.py` / `app/oidc.py` / `app/identity.py` | credential/session、OIDC handshake、identity binding |
| `app/authorization*.py` / `app/project_roles.py` | 授權目錄、shadow 觀測、fail-closed enforcement、多角色 RBAC（預設 `off`） |
| `app/datasets.py` / `app/dataset_*.py` | 資料集註冊、manifest、sync、快照、資產、別名、分享、發布 |
| `app/inventory.py` | 唯讀 SSH 掃描候選專案（封閉指令集、秘密過濾、禁止路徑） |
| `app/project_instances.py` / `app/project_bootstrap.py` / `app/project_environments.py` | 專案實例狀態與 reconcile、Project bootstrap v2、Environment revision |
| `app/run_templates.py` / `app/product_run*.py` | Run Template v2 與 Product Run 投影 |
| `app/server_config.py` / `app/server_publication.py` / `app/server_attempt_preflight.py` | servers.yaml 驗證/原子寫入/備份/熱重載、發布協議、test-ssh、revision-scoped 檔案系統預檢 |
| `app/activity.py` | 專案執行近況的唯讀 SSH 探測 |
| `app/results.py` / `app/jobfinish.py` / `app/metrics_v1.py` | 任務結束 hook：拉結果、寄信、coding run 回填、metrics-v1 解析入庫 |
| `app/stall.py` | 卡死偵測純函式（只標旗標） |
| `app/mailer.py` | SMTP 通知（未設定即跳過） |
| `app/coding_agents.py` / `app/engineering_tasks.py` | 退役 descriptor registry（誠實 retired 快照）與歷史 Engineering Task 紀錄；新入口回誠實退役錯誤（DG-AGENT-RUNTIME-V3 Phase 1b） |
| `app/agent_gateway.py` / `dispatch_agent/` | runner 上的 Claude Agent SDK AgentSession：Server A gateway（runner WS、事件持久化、權限提示、checkpoint）與 runner 端 `dispatch-agent` 服務（出站 WS、SDK host、工作區） |
| `app/engineering_path_policy.py` / `app/engineering_validation.py` | 改碼路徑政策與結果驗證 |
| `app/code_promotion.py` | 本地 bundle 驗證 → 不可執行 ProjectVersion → hub 發布（不推 GitHub） |
| `app/github_publication.py` | GitHub 發布介面（interface + fake only；真實 adapter 需 `DG-GITHUB-PUBLISH`） |
| `app/hub.py` | 中央 bare-repo hub 同步與部署輔助 |
| `app/node_*.py` + `agent/` | Node Agent 協議/註冊/常駐（`INV-NODE-*`；rollout 狀態見帳本） |
| `app/llm.py` / `app/llm_local.py` / `app/agent_runtime.py` / `app/agent_tools.py` / `app/chat.py` | 選配 LLM 層：意圖分類、JSON tool loop、工具白名單、規則式後備 |
| `app/mcp_bridge.py` | 獨立行程 MCP bridge（ChatGPT），純 HTTP client |
| `app/records.py` | 實驗紀錄與時間軸合併 |
| `static/` + `studio/` | `static/login.html`（未登入的唯一頁面）＋ Studio SPA（`studio/`，React+TypeScript+Vite，build 到 gitignored `static/studio/`）；`GET /` 未登入回 login.html、登入後回 Studio index（DG-STUDIO-UI v1 P3-4） |

## 3. Data flow（一個訓練任務的生命週期）

1. 入口（Web/API/agent/MCP）→ `request_enqueue_approval()`：危險指令當場 400；
   `type=train`+`project`+`pin_server` 時同場算好 sync/setup 計畫附在 payload。
2. 核准（人工 `POST /approve/{id}`、web 一步生效、或 auto_approve 規則）→
   `enqueue_job()` 入列，自動建立依賴的 sync/setup 任務。
3. sync 任務 pin 到 `_local` 在 Server A 執行（rsync 推出），完成後 manifest 驗證
   通過才登記 `dataset_cache`。
4. scheduler 對空閒機器 `pick_job()`（pin/tag/資料引力資格過濾/priority/FIFO）→
   **先標 running** → SSH 派發（mkdir + SFTP 寫 cmd.sh/run.sh + tmux 起 session）。
5. 每輪 reconcile 依哨兵協議判定（exit_code 檔在→done/failed；tmux 在→running；
   都不在→requeue），終態觸發背景 hook（拉 `results/{id}/`、解析 `metrics.json`、寄信、寫稽核）。

以上是**現行預設路徑**。另有兩條 rollout-flag 閘門後的路徑：
attempt-driven SSH 執行（不可變 attempt 身分 + prepare/launch/collect operation +
ambiguous launch 仲裁，`INV-STATE-2`）與 ExecutionPlan v2（把 code revision/
environment/template/dataset/resource 釘成不可變執行意圖，核准後最多具現化一個 Job）。
它們不改上面的哨兵語意，只在其上加不可變身分與證據。

## 4. Persistence and runtime-state ownership

| 存放區 | 內容 | 所有權/生命週期 |
|---|---|---|
| `jobqueue.db`（SQLite） | jobs、approvals、projects、datasets、dataset_cache、project_candidates、project_instances、project_versions、coding_runs、experiment_records，以及 attempt/execution-plan/experiment/metrics/audit-event/identity/RBAC/dataset-governance/agent-session 等版本化 migration 加入的表 | 持久真相；schema 演進走 SCHEMA+`_*_COLUMN_MIGRATIONS` 雙軌與版本化 additive migration |
| 工作機 `agent_jobs/{id}/` | cmd.sh/run.sh/job.log/exit_code 哨兵檔 | 任務執行的分散式真相；重啟後 reconcile 的依據 |
| in-memory `server_states`/`server_configs`（AppState） | 監控讀數、機器設定快取 | 可拋棄；重啟歸零由 monitor/`servers.yaml` 重建 |
| `servers.yaml` | 機器設定唯一來源 | 修改必經 backup→原子寫→熱重載 |
| `audit.jsonl` | legacy 稽核事件流 | append-only，程式永不改寫；durable `audit_events` 帳本與其並行，採用狀態是部分的 |

## 5. Testing boundary

- 全部測試跑在假介面上：FakeSSH、假 `local_run`、假 `send_mail`、duck-typed 假
  LLM client、`TestClient`；不需要真機/真 GPU/真 SMTP/真 key/真網路。
- 唯一例外 `tests/test_localrun.py`：用本機 `bash`/`tmux`（被測物就是本機 subprocess 層）。
- 安全邊界由測試釘住（forbidden imports/forbidden tool names、allowedTools/confinement pins 等），
  功能改動不得弱化這些斷言（`INV-TEST-2`）。

## 6. Where the rest lives

| 主題 | 位置 |
|---|---|
| 產品定位、範圍、非目標 | `docs/PLATFORM_CHARTER.md` §1–§2 |
| 兩平面模型、硬體邊界、Development Agent 邊界、信任邊界、核准流摘要 | `docs/PLATFORM_CHARTER.md` §4 |
| 平台八項責任對應的模組 | `docs/PLATFORM_CHARTER.md` §5 |
| 不變式 `INV-*` | `docs/PLATFORM_CHARTER.md` §6 |
| 裁定登錄與待決閘 | `docs/PLATFORM_CHARTER.md` §7、`docs/DECISIONS.md` |
| 能力現況 | `docs/CAPABILITY_LEDGER.md` |
