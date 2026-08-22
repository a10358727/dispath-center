# System Architecture(穩定參考,非現況快照)

本檔只記載經 repository 驗證、相對穩定的架構事實。**不記載**:當期審計發現、
優先級排序、階段完成度快照、暫時性 TODO、測試數量、已知 bug、短期優化建議
——那些屬於 snapshot,應放在 skills 之外(例如 `docs/audits/`),不是永久知識。

Development Plane / Compute Plane 的分工、Development Agent(Codex、Claude Code
與未來 provider)邊界,以及「已實作 vs 尚未實作」
的對照,見同目錄的 `development-platform.md`。能力現況(implemented/default-enabled/
deployed/production-ready)以 `docs/CAPABILITY_LEDGER.md` 為準,本檔不重複宣稱。

## 1. System architecture

集中式架構:Server A(本機)跑一個 FastAPI 單體(`app/main.py` + `dispatch_center/`
套件的 API v2 router 邊界),對 `servers.yaml` 列出的工作機做 SSH 探測與派工。
SSH 後端不依賴工作機上任何本系統常駐程式——遠端依賴只有 `tmux`/`bash`
(GPU 機另需 `nvidia-smi`)。工作機上「可以」另外存在經 `INV-NODE-*` 管理的
Node Agent,但 SSH 後端的行為永遠不得假設它存在(`INV-SSH-1`,2026-07-19 DG-C 修訂);
Node Agent 目前預設關閉、未在任何真機啟用。另有一個可選的獨立行程
`app/mcp_bridge.py`(MCP bridge,供 ChatGPT connector),只透過 HTTP 呼叫調度中心。

系統的功能面分成兩個 plane:**Development Plane**(專案匯入/隔離工作區/改碼/
測試/diff/ProjectVersion)與 **Compute Plane**(Dataset/ExecutionPlan/Approval/
Scheduler/執行後端/Run/Results),兩者唯一的交會點是經人工核准的 promotion。
詳見 `development-platform.md`。

兩個常駐迴圈(FastAPI lifespan 啟動):
- **monitor loop**(預設 20s):對每台機器跑一條合成探測指令(nvidia-smi + loadavg + df),
  更新 in-memory `server_states`。
- **scheduler loop**(預設 10s):reconcile running 任務 → 卡死偵測 → blocked 標記 →
  對空閒機器挑任務派發 → 派發 `_local` sync 任務。

## 2. Component responsibilities

本表只列**穩定、跨切片仍成立**的模組職責;`app/` 目前有 80+ 模組,
未列出的多半屬於 default-off 的 Product v2 / Node / audit 子系統,現況以
`docs/CAPABILITY_LEDGER.md` 與程式本身為準。

| 模組 | 職責 |
|---|---|
| `app/main.py` | FastAPI app、AppState(loops、in-memory 狀態、背景 task 追蹤)、legacy REST/WS 端點、auth middleware |
| `dispatch_center/api/` | API v2 router 邊界、錯誤/分頁/idempotency/request-id 原語(預設關閉的 `API_V2_ENABLED` 閘門後) |
| `app/config.py` | 讀 `servers.yaml` 與 `.env`;`ServerConfig`/`AppConfig`;全部功能開關 |
| `app/db.py` | SQLite schema、遷移、全部 CRUD;`VALID_APPROVAL_KINDS` |
| `app/migrations.py` | 版本化 additive migration |
| `app/sshpool.py` | asyncssh 封裝:金鑰認證、逾時、每機序列化、全域併發上限、SFTP 寫檔 |
| `app/localrun.py` | `_local`(Server A 本地)執行層,與 sshpool 介面同形 |
| `app/monitor.py` | 探測指令建構與純函式解析(nvidia-smi/loadavg/df)、`is_idle` 判定 |
| `app/jobqueue.py` | 任務 CRUD、狀態機、依賴、哨兵協議(build_* 指令家族、reconcile) |
| `app/scheduler.py` | `pick_job()` 純函式 policy 與 `scheduler_tick()` 迴圈 |
| `app/execution_*.py` | attempt-driven 執行:contract/plan/backend/dispatch/launch 仲裁、outbox(rollout flag 後) |
| `app/security.py` | `is_dangerous()` 危險指令黑名單(防呆,不防惡意) |
| `app/approvals.py` | 核准流:`request_*` 建卡、`approve()` 各 kind 落地、`maybe_auto_approve()` |
| `app/autoapprove.py` | `auto_approve.yaml` 確定性規則引擎 |
| `app/auto_placement.py` | policy-scoped placement 提案與決策(`INV-APPROVAL-4b`,預設關閉) |
| `app/audit.py` / `app/audit_store.py` / `app/audit_adoption.py` | legacy `audit.jsonl` append-only + 版本化 hash-chained durable 事件帳本與採用目錄 |
| `app/authentication.py` / `app/oidc.py` / `app/identity.py` | credential/session、OIDC handshake、identity binding |
| `app/authorization*.py` / `app/project_roles.py` | 授權目錄、shadow 觀測、fail-closed enforcement、多角色 RBAC(預設 `off`) |
| `app/datasets.py` / `app/dataset_*.py` | 資料集註冊、manifest、sync、快照、資產、別名、分享、發布 |
| `app/inventory.py` | 唯讀 SSH 掃描候選專案(封閉指令集、秘密過濾、禁止路徑) |
| `app/project_instances.py` / `app/project_bootstrap.py` / `app/project_environments.py` | 專案實例狀態、Project bootstrap v2、Environment revision |
| `app/run_templates.py` / `app/product_run*.py` | Run Template v2 與 Product Run 投影 |
| `app/server_config.py` / `app/server_publication.py` | servers.yaml 驗證/原子寫入/備份/熱重載、發布協議、test-ssh |
| `app/activity.py` | 專案執行近況的唯讀 SSH 探測 |
| `app/results.py` / `app/jobfinish.py` | 任務結束 hook:拉結果、寄信、coding run 回填 |
| `app/stall.py` | 卡死偵測純函式(只標旗標) |
| `app/mailer.py` | SMTP 通知(未設定即跳過) |
| `app/coding_agents.py` / `app/engineering_tasks.py` / `app/codex_app_server.py` | Development Agent 的 allowlisted provider registry(現況只有 Codex provider)、任務合約、bounded app-server adapter(預設關閉) |
| `app/engineering_path_policy.py` / `app/engineering_validation.py` | 改碼路徑政策與結果驗證 |
| `app/code_promotion.py` | 本地 bundle 驗證 → 不可執行 ProjectVersion → hub 發布(不推 GitHub) |
| `app/github_publication.py` | GitHub 發布**介面 + fake only**;無 adapter、無路由、無憑證 |
| `app/hub.py` | 中央 bare-repo hub 同步與部署輔助 |
| `app/node_*.py` + `agent/` | Node Agent 協議/註冊/常駐(`INV-NODE-*`,全部預設關閉、未在真機啟用) |
| `app/llm.py` / `app/llm_local.py` / `app/agent_runtime.py` / `app/agent_tools.py` / `app/chat.py` | 選配 LLM 層:意圖分類、JSON tool loop、工具白名單 |
| `app/mcp_bridge.py` | 獨立行程 MCP bridge(ChatGPT),純 HTTP client |
| `app/records.py` | 實驗紀錄與時間軸合併 |
| `static/` | 依賴自由的 vanilla JS 前端:legacy 操作介面(`index.html`/`ui.js`/`ui.css`)與預設關閉的 Product Workspace(`workspace.html`/`workspace.js`/`workspace.css`) |

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

以上是**現行預設路徑**。另有兩條 rollout flag 後、預設關閉的路徑:
attempt-driven SSH 執行(不可變 attempt 身分 + prepare/launch/collect operation +
ambiguous launch 仲裁,`INV-STATE-2`)與 ExecutionPlan v2(把 code revision/
environment/template/dataset/resource 釘成不可變執行意圖,核准後最多具現化一個 Job)。
它們不改上面的哨兵語意,只在其上加不可變身分與證據。

## 4. Trust boundaries

- **人 vs 系統**:批准權只屬於通過認證的人(`X-Auth-Token`、OIDC server-side
  session,或明確啟用的 service bearer);`source` 標記僅記來源通道,不是權限層。
  預設設定下 token 持有者即有完整核准權;`AUTHORIZATION_MODE`(預設 `off`)、
  多角色 RBAC 與 `ALLOW_HIGH_RISK_SELF_APPROVAL`(預設 `false`,只放寬
  requester/decider 分離、不放寬角色或 scope)是**已實作但預設關閉**的收緊層,
  不得當成現行部署的既成保護。
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
| `jobqueue.db`(SQLite) | jobs、approvals、projects、datasets、dataset_cache、project_candidates、project_instances、project_versions、coding_runs、experiment_records,以及 attempt/execution-plan/audit-event/identity/RBAC/dataset-governance 等版本化 migration 加入的表 | 持久真相;schema 演進走 SCHEMA+`_*_COLUMN_MIGRATIONS` 雙軌與版本化 additive migration |
| 工作機 `agent_jobs/{id}/` | cmd.sh/run.sh/job.log/exit_code 哨兵檔 | 任務執行的分散式真相;重啟後 reconcile 的依據 |
| in-memory `server_states`/`server_configs`(AppState) | 監控讀數、機器設定快取 | 可拋棄;重啟歸零由 monitor/`servers.yaml` 重建 |
| `servers.yaml` | 機器設定唯一來源 | 修改必經 backup→原子寫→熱重載 |
| `audit.jsonl` | legacy 稽核事件流 | append-only,程式永不改寫;durable `audit_events` 帳本與其並行,採用狀態是部分的 |

## 6. Approval flow

`approvals` 表統一承載所有 kind(見 `VALID_APPROVAL_KINDS`)。狀態機:
pending → approved / rejected(終態)。三種核准路徑,全部落到同一個 `approve()`:
1. 人工:`POST /approve/{id}`。
2. web 一步生效:`source="web"` 且 `WEB_DIRECT_EXECUTE=true`(提案者=批准者,
   同請求內建卡+核准,`approved_by: "web-direct"`)。
3. 自動核准規則:`auto_approve.yaml` 命中(**kind 白名單只有 enqueue/stop**,
   `approved_by: "auto-rule-N"`)。
另有一條**獨立於** `maybe_auto_approve()` 的 policy-scoped 決策路徑,只適用
`kind=auto_placement`,且須同時滿足 `INV-APPROVAL-4b` 的六項條件(預設 kill
switch 關閉)。任何其他 kind 都沒有自動路徑;promotion 依 DG-CODE-PROMOTE-v1
P-1 永遠只能由人核准。
不論路徑:危險指令在建卡當下已被擋;`approve()` 落地前重驗當下狀態。

## 7. LLM and MCP permission boundaries

- 工具分兩類:唯讀(查狀態/任務/專案/資料卡…)與 `request_*`(只建 pending approval)。
- 工具註冊表本身就是安全邊界:`app/agent_tools.py` 的 `TOOLS` 查表分派(不用
  getattr),不得 import `sshpool`/`localrun`/`subprocess`;需要 SSH 的唯讀工具
  經注入的 `ctx.ssh_run` callable。
- runtime 防護:工具步數上限、結果截斷、併發 semaphore、JSON 解析修正機會。
- prompt injection 的血本封頂設計:最壞情況只是一張待審卡片。
- Development Agent(不分 provider——Codex、Claude Code 或未來 adapter)同屬
  這條邊界的外側:它是 Development Plane 的協作者,在系統建立的隔離 worktree
  內改碼、經既有核准與執行基礎設施跑受控驗證,永遠拿不到憑證、拿不到 shell
  工具、也不能核准自己的請求;provider 選擇(手動或 Auto)永遠不是權限提升
  (見 `development-platform.md` §5)。

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

- 不防惡意繞過:危險指令黑名單是防呆(防手滑),預設設定下 token 持有者本就有
  完整權限。
- 不做搶佔/遷移:任務派出後不移機、不搶佔;停止必經核准。
- 不做 GPU 槽位切分(現行語意:一任務佔一台機;`gpus_needed` 欄位為未來預留)。
- 不做工作機互聯:跨機傳遞一律 Server A 中轉(bundle)。
- 不做 LLM/Development Agent 自主執行:agent(不分 provider)永遠停在「提案」;
  批准通道與提案通道結構性分離,
  且永無 approve/reject/shell/exec/run_command 工具。
- 不把執行通道開放成 general-purpose remote shell:Development Plane 是這層的
  消費者,不是擴充者。
- 前端不引框架、不引 build step:`static/` 維持依賴自由的 vanilla JS。

現況說明(不是 non-goal,但常被誤讀):per-user 身分與多租戶治理(OIDC identity、
多角色 RBAC、service account/token、Project membership)**已實作但預設關閉**,
且沒有部署證據。不要因為程式存在就宣稱它是現行保護,也不要因為預設關閉就把它
當成「不做」。
