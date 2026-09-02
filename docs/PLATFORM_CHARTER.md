# Dispatch Center Platform Charter｜平台憲章

> 版本：v1（2026-08-30，DG-PLATFORM-CHARTER v1）
> 地位：**唯一治理文件**——產品定位、範圍、架構模型、全部不變式（`INV-*`）、裁定登錄與文件地圖都在這裡。
> 它取代原本分散在 dispatcher-domain skill 的 invariants／development-platform 參考檔
> 與各產品計畫書裡的治理內容；`docs/DECISIONS.md` 仍是逐條、
> append-only 的裁定時間紀錄（provenance）。

---

## 0. Status & Amendment｜地位與修訂程序

**真相順序（Truth order）**——文件彼此衝突時依此裁決：

1. 本憲章 §6 不變式 ＋ `docs/DECISIONS.md` 具名裁定（兩者同為安全真相；較新的具名裁定優先，且憲章必須在同一 PR 內跟上）
2. 現行程式碼與 `tests/`（實作真相）
3. `docs/CAPABILITY_LEDGER.md`（能力現況：`Implemented` ≠ `Default` ≠ `Pilot` ≠ `Canary`；production-ready 只能由具名裁定宣告）
4. `docs/product/ROADMAP.md`（方向，不是實作）
5. `docs/archive/`（歷史證據，不是現況）

**修訂規則**：

- 修改任何 `INV-*` 的語意、新增或刪除不變式 ＝ 架構決策，**必須由使用者明文裁定**。程序：在 `docs/DECISIONS.md`
  追加條目（含使用者原文或明確選項）→ 修訂本憲章 §6 → 更新 §7 登錄 → 以測試釘住（INV-TEST-2）。不得在功能實作中順手更動。
- 新增一類能力（新 approval kind、新生命週期狀態、新 provider、新 validation mechanism、新外部發布通道、
  新硬體工作類型）＝ 產品／架構決策，需要具名裁定（`DG-*`）；沒有裁定的能力不得實作、不得預先發明語意。
- 本憲章**不記載**能力現況、進度、測試數量、模組清單：現況看帳本；模組職責看
  `.claude/skills/dispatcher-domain/references/architecture.md`；存不存在看程式碼。
- 憲章條文引用測試時，引用檔名與測試函式名，不引用行號。

---

## 1. Positioning｜產品定位

使用者裁定（2026-08-30，原文）：

> Dispatch Center 是一個 **Agent-native Engineering Platform**，目標是在瀏覽器中提供一個完整的
> **AI Engineering Workspace**，讓 AI Engineer 能夠理解工程專案、讀寫程式碼、執行測試、訓練模型，並進一步完成
> FPGA synthesis、bitstream 產生、MCU build/flash 與硬體驗證等工作；平台本身則負責權限與核准、環境隔離、
> 運算與硬體資源配置、跨伺服器執行、版本與 Artifact 管理、Evidence 蒐集、狀態恢復與完整 Audit，
> 使 AI 不只是提供建議，而是能在受控、可追蹤、可驗證的工程流程中，持續從需求、開發、測試、執行、結果分析到
> 下一輪改善，形成一個完整的 AI 驅動工程迭代閉環。

拆開來讀：

| 角色 | 做什麼 |
|---|---|
| **AI Engineer（Development Agent 與助手）** | 理解專案、讀寫程式碼、跑測試、提出實驗與下一輪建議、根據證據分析結果 |
| **平台（Dispatch Center）** | 權限與核准、環境隔離、運算與硬體資源配置、跨伺服器執行、版本與 Artifact 管理、Evidence 蒐集、狀態恢復、完整 Audit |
| **人（owner / reviewer）** | 決定：核准或拒絕每一個 material 動作；promote 版本；裁定新能力 |

一句話：**AI 負責思考與提案，平台負責治理與執行，人負責決定。** AI 永遠不是執行真相、核准權或直接執行通道
（`CLAUDE.md` 全域規則第 1 條）。

「閉環」的形狀（每一個箭頭都是受治理的轉換，不是 agent 自行接續）：

```text
需求 → 開發（隔離 workspace 改碼、驗證、diff）→ review → promote（ProjectVersion）
     → 執行（ExecutionPlan → approval → 派工）→ 觀測 → 收集（results / metrics / artifacts）
     → 分析（grounded evidence）→ 下一輪提案（新 workspace 工作或新 ExecutionPlan，仍經核准）
```

---

## 2. Scope & Non-goals｜範圍與非目標

### 2.1 工程領域（Engineering domains）

| 領域 | 現況 | 進入條件 |
|---|---|---|
| 軟體工程／ML 訓練與實驗 | 現行主體（onboarding、Development Agent、promotion、ExecutionPlan、experiment、metrics-v1） | 依帳本逐能力啟用 |
| **硬體工程軌**：FPGA synthesis／bitstream、MCU build／flash、硬體驗證（HIL） | **已裁定、實作中**（DG-HARDWARE-EXECUTION v1，2026-08-31；§7.1） | 契約：裝置為 worker 附掛資源（servers.yaml `devices:`）、`action_class` 分級、實體動作走 `hardware_action_v2`、映像內容定址、`hardware-receipt-v1`；邊界不變（§4.2、INV-PLANE-2） |

### 2.2 使用者模型（User model）

小團隊使用、單人審核（PROD-1）：正式體驗可由具權限的人直接核准（含自己提出的請求，
`ALLOW_HIGH_RISK_SELF_APPROVAL` 姿態，DG-SELF-APPROVAL-OPTION-v1）。**核准閘門本身永遠存在**：人仍要按核准；
任何 agent 永遠不能核准任何請求（含自己的）。

### 2.3 明文非目標（Explicit non-goals）

- 不防惡意繞過：危險指令黑名單是防呆（防手滑）；預設設定下 token 持有者本就有完整權限。
- 不做搶佔／遷移：任務派出後不移機、不搶佔；停止必經核准（INV-SSH-9）。
- 不做 GPU 槽位切分、多租戶配額排程（現行語意：一任務佔一台機；改變需 `DG-GPU-SCHED`）。
- 不做工作機互聯：跨機傳遞一律 Server A 中轉（bundle）。
- 不做 LLM／Development Agent 自主執行：agent（不分 provider）永遠停在「提案」；**平台工具集**（`TOOLS`／MCP）永無 approve／reject／shell／exec／run_command 工具。
- 不把 Compute 執行通道開放成 general-purpose remote shell：Development Plane 是執行層的消費者，不是擴充者。Development Agent 在自己
  工作區內的 Bash 由 session 擁有者**逐條即時允許**（INV-AGENT-2）——那是人在迴圈的工作區 shell，不是平台自動開放的遠端 shell。
- 不做 full browser IDE、任意 web terminal、root shell。
- 不做 agent 無上限自主優化：限額式迴圈需要新的具名裁定（`DG-OPTIMIZATION-QUOTA`），裁定前每輪人工確認。
- 不做開放式 multi-agent swarm、Kubernetes、大型多租戶或複雜多人協作 UX。A2A 的 task／message／artifact 語意只用於
  Server A ⇄ runner agent 通道（runner 只出站）；對外發佈 Agent Card 是選配階段，且外部 agent 的 task 永遠只能成為提案卡（INV-LLM-1）。
- 前端：Studio 以 TypeScript＋框架＋build 開發（DG-STUDIO-UI v1）；build 產物不進 git，由 CI 與部署步驟建置；頁面不得載入任何
  外部 URL 資源。舊 `static/workspace.*` vanilla 介面在 Studio 功能齊全前並存。
- Auto provider selection 引擎延後（Manual + per-Project 預設 provider 是產品要求；DG-PRODUCT-PLAN-CORRECTIONS v1）。

---

## 3. Core Objects & Lifecycle｜核心物件與生命週期

**Project 是產品中心**：它統一代表 Git repository、ProjectVersion、Environment、Run Template、Dataset bindings、
Project Instances、AI conversations／AgentSessions、Experiments、Runs、Artifacts。**Server 只是執行資源，不是真相來源。**
術語定義見 `.claude/skills/dispatcher-domain/references/glossary.md`。

兩條生命週期，交會於一點：

```text
程式碼生命週期（Development Plane）
  import / create → normalize → isolate（workspace / worktree）→ edit → validate（test / lint / typecheck / build）
  → review（diff）→ checkpoint → promote ──┐
                                            ▼ promoted ProjectVersion（唯一交會點）
執行生命週期（Compute Plane）
  prepare（ExecutionPlan：version + environment + template + dataset + resource）→ approve → schedule
  → run（dispatch、哨兵協議）→ observe（monitor / reconcile）→ collect（results / metrics / artifacts）
  → analyze（grounded evidence）→ improve / rerun（提案 → 核准）
```

分析結論一律附證據，缺失的輸出是 **unknown**、不是成功或失敗；建議要經核准才能變成 rerun 或任何 mutation
（規則：`.claude/skills/dispatcher-domain/references/result-analysis.md`）。

---

## 4. Architecture Model｜架構模型

### 4.1 兩個 Plane 與唯一交會點（Two planes, one junction）

原本的心智模型只有一條線：approval → schedule → dispatch → collect。開發平台加入了一整類「還沒有要執行任何
workload」的工作：匯入舊專案、整理專案結構、在隔離工作區改碼、跑測試、看 diff、決定要不要把這份程式碼變成正式版本。
兩者失敗模式不同——Compute Plane 的風險是**跑錯東西**（錯的 code／dataset／機器／資源／硬體），
Development Plane 的風險是**改錯東西**（動到正式 checkout、外洩 secret、把未審閱的程式碼變成可執行版本、
把 agent 當 shell 用）。分 plane 就是讓「agent 可以在工作區跑 pytest」不可能被誤讀成「agent 可以在工作機跑任意指令」。

```text
Development Plane                          Compute Plane
「應該存在什麼程式碼？」                      「要跑什麼、跑在哪、用什麼資料／硬體、結果在哪？」

Project Onboarding                         Dataset
Development Agent（Claude Code / Codex / …）  ExecutionPlan（不可變執行意圖）
isolated workspace / worktree              Approval（所有 material 寫入的唯一落地閘門）
edit / validate / diff / review            Scheduler（確定性資格過濾 + priority / FIFO）
ProjectVersion                             SSH / Node backend（唯二受控執行通道）
        │                                  Run → Results / Metrics / Artifacts
        └──── promotion approval（人工）──────────▲
```

- Development Plane 的**輸出只有一種形式**：一份可審閱、可 promote 的 revision。它不決定「要不要跑」、「跑在哪」、「用哪份資料」。
- **Development artifact 進入 Compute execution lifecycle 的唯一正式 transition 是 promoted ProjectVersion**（INV-PLANE-1）。
  共用底層 SSH／Job 基礎設施（例如某 provider 的驗證以 approved Job 承載）只是共用基礎設施，不代表未 promoted 的程式碼
  已進入 Compute workload lifecycle。
- Promotion 規則承 DG-CODE-PROMOTE-v1：永不自動核准（P-1）；同 commit 重複 promote 是 no-op（P-2）；沒有 promotion
  approval 的版本是 `legacy_observed`，不得支撐 reproducible run（P-3）；只寫本地 hub、不推 GitHub（P-4）；promotion 後保留
  worktree（P-5）。

### 4.2 硬體動作屬於 Compute Plane（Hardware actions are Compute Plane executions）

定位把 FPGA／MCU／硬體驗證納入目標範圍。憲章先定邊界、後定契約：

- synthesis／bitstream 產生、firmware build、flash／program、電源控制、HIL 測試——**全部是 Compute Plane 的受治理執行**：
  只能從 promoted ProjectVersion 經 ExecutionPlan → approval → 執行層到達目標機器與其附掛硬體（INV-PLANE-2）。
- 其中「實體動作」（flash／program／erase／power）永不自動核准、永不從 agent workspace 發起、永不由 agent 工具直接觸發。
- 硬體是附掛在某台 worker 的**資源**，由平台配置；agent 只能描述需求（「需要一片 Artix-7 板」），不能指定連線方式或指令。
- 資源模型、工作類型、artifact 類型（bitstream／firmware image）、證據契約、排程語意、危險動作黑名單延伸——
  已由 **DG-HARDWARE-EXECUTION v1** 裁定（2026-08-31，§7.1；細節見 `decisions/DG_HARDWARE_EXECUTION_DRAFT.md`）。

### 4.3 Development Agent 模型與邊界（Development Agent model & boundary）

Development Plane 的協作者是 **Development Agent**——受控的改碼代理。它不是單一產品：「Codex」不是抽象層的名字，
只是第一個 provider；Claude／Claude Code 是最終主力 provider（PROD-7）。現有 provider 以 reviewed allowlist registry
（`app/coding_agents.py`）與其測試為準。自 DG-AGENT-RUNTIME-V3（2026-08-30）起，主力承載方式是 runner 上的
**dispatch-agent 服務以 Claude Agent SDK 執行 session**（INV-AGENT-*）；Codex exec provider 於 Phase 1b 退役（registry 留歷史註記）。

```text
DevelopmentAgent   ── 抽象角色：在隔離工作區改碼、驗證、提案
   │ 由一個 provider 實現
AgentProvider      ── provider-specific adapter（registry 為準；CLI 細節只能存在這裡）
AgentSession       ── 一次與某 provider 的工作階段（語意以 code/tests 與具名裁定為準）
```

分層規則：provider 只能從 registry 以 id 選取，永不接受任意 executable；核心 domain 物件（Project、Workspace、
ProjectVersion、ExecutionPlan、Approval）不得依賴特定 provider；所有 provider 受**同一套**邊界，
**選 provider（Manual 或 Auto）永遠不是權限提升**；Auto selection 必須確定性、可解釋、記錄 selected provider、
失效 fail closed、永不 silent fallback 到權限更大的 provider。

Development Agent **可以**（在既有受控通道內）：

- 在系統建立的隔離 workspace／worktree 中讀檔、改檔；
- 經 bounded、dispatch-controlled validation path 跑 project-local 驗證（test／lint／typecheck／build）；
- 產生 reviewable diff、說明、建議、下一步提案；
- 建立 pending approval。

Development Agent **不得**：

- 繞過 authorization／approval／ExecutionPlan／Dataset permission／promotion rule／SSH boundary；
- 核准自己的請求（提案通道與批准通道結構性分離，INV-LLM-1／INV-LLM-2）；
- 取得 SSH 私鑰、credential 或直接開連線（INV-LLM-3）；任意讀寫 runtime DB／audit／server config；
- 自行 deploy、promote、push external origin、動正式 project instance；
- 把 Compute 執行通道當成 general-purpose shell（工作區內的 Bash 依 INV-AGENT-2 由人逐條允許）；自行決定工作區位置
  （worktree 只能由 dispatch 端——Server A 或 dispatch-agent 服務——在設定的 root 下建立）；
- 讓分析建議自動變成動作。

兩種「執行」的結構（對每一個 provider 同樣成立）：

```text
Development validation:   DevelopmentAgent → runner agent（SDK：allowed_tools＋can_use_tool，INV-AGENT-2）→ isolated workspace
Compute workload:         promoted ProjectVersion → ExecutionPlan → approval → dispatch → worker（＋附掛硬體）
```

一個 provider 用哪條 validation mechanism 承載是 implementation fact，不是對其他 provider 的架構要求；每一條 mechanism
都必須能追溯到 `docs/DECISIONS.md` 的具名裁定，新的 mechanism 需要新的裁定，且不得弱化 approval、audit、isolation
或 SSH boundary。能力流向永遠是 `DevelopmentAgent → dispatch tool → policy / validation → approval → 執行層 → server`；
禁止的流向是 `agent → credential/SSH → server`、`agent → 執行層 → server`、`agent → approve() → 副作用`。

平台助手（聊天）是同一條邊界的另一個消費者：能力上限＝唯讀查詢＋建立 pending approval（INV-LLM-*）。DG-ASSISTANT-TOOLS v1
讓它經 MCP bridge 使用平台工具（per-turn token、路由白名單）；Phase 1b 起助手改為 runner agent 上無工作區的 SDK session
（`allowed_tools=[]`、只掛 MCP），上限不變。

### 4.4 信任邊界（Trust boundaries）

- **人 vs 系統**：批准權只屬於通過認證的人（`X-Auth-Token`、OIDC server-side session，或明確啟用的 service bearer）；
  `source` 標記只記來源通道，不是權限層。預設設定下 token 持有者即有完整核准權；`AUTHORIZATION_MODE`（預設 `off`）、
  多角色 RBAC 與 `ALLOW_HIGH_RISK_SELF_APPROVAL` 是**已實作但預設關閉**的收緊層，不得當成現行部署的既成保護。
- **平台 vs LLM／agent**：LLM（Anthropic API／vLLM／runner Claude／ChatGPT connector）與 Development Agent 都在邊界外，
  能力上限＝唯讀查詢＋建立 pending approval＋（Development Agent）在隔離工作區改碼與受控驗證。
- **平台 vs MCP bridge**：行程隔離，bridge 只是 HTTP client（路徑機密＋選配 bearer 兩層認證，錯誤一律 404）。
- **Server A vs 工作機**：Server A 持金鑰可登入工作機；工作機互不相通；工作機上的常駐服務（Node Agent、dispatch-agent）
  只能持各自可撤銷的登錄 credential **出站**連回 Server A（INV-NODE-1、INV-AGENT-1），工作機永不開入站控制埠；跨機資料仍一律
  bundle、Server A 中轉。
- **服務綁定**：只綁私網（預設 `127.0.0.1`，絕不 `0.0.0.0`）；對外靠 Cloudflare Tunnel／Tailscale（outbound-only）；runner gateway 的
  WebSocket 掛在同一個私網綁定上，runner 經既有 Funnel／Tailscale HTTPS 連入。憑證永不進 DB、稽核、diff、transcript 或 prompt。

### 4.5 執行通道與核准流（Execution channels & approval flow）——摘要，規則以 §6 為準

- `approvals` 表統一承載所有 kind（`VALID_APPROVAL_KINDS`，`app/db.py`）；狀態機 pending → approved／rejected（終態）。
- 三種核准路徑全部落到同一個 `approve()`：人工 `POST /approve/{id}`；web 一步生效（`WEB_DIRECT_EXECUTE`，只限 enqueue／stop）；
  `auto_approve.yaml` 規則（kind 白名單只有 enqueue／stop，INV-APPROVAL-4）。另有獨立的 policy-scoped 路徑只適用
  `auto_placement`（INV-APPROVAL-4b）。任何其他 kind 都沒有自動路徑；promotion 永遠只能由人核准。
- 危險指令在建卡當下已被擋（INV-APPROVAL-2）；`approve()` 落地前重驗當下狀態（INV-APPROVAL-3）。
- **Compute** 執行通道只有兩條：agentless SSH／SFTP／tmux（INV-SSH-*，永久保留）與 Node Agent（INV-NODE-*，逐台啟用、隨時回退）。
  使用者指令原文只經 SFTP 落地；成敗只看哨兵 `exit_code`；連不上＝跳過不判定。
- **Development 驗證通道**另列：runner 上的 dispatch-agent（Claude Agent SDK，工作區內；INV-AGENT-*）——它不是 Compute 執行，
  訓練／部署／燒錄永遠不從那裡啟動（INV-PLANE-2）。

---

## 5. Platform Responsibilities｜平台責任

定位陳述的八項責任，對應現行落實位置（**現況一律以帳本與程式碼為準**；這裡只說「在哪裡」，不說「已完成」）：

| # | 責任 | 現行落實 | 治理條文 |
|---|---|---|---|
| 1 | 權限與核准（Authorization & approval） | `app/approvals.py`（`request_*`／`approve()`／`maybe_auto_approve()`）、`app/db.py` `VALID_APPROVAL_KINDS`、`app/autoapprove.py`、`app/authentication.py`／`app/oidc.py`／`app/identity.py`、`app/authorization*.py`＋`app/project_roles.py`（RBAC，預設 off） | INV-APPROVAL-*、INV-LLM-* |
| 2 | 環境隔離（Isolation） | dispatch 建立的 git worktree 與 AgentSession workspace（由 runner 上的 `dispatch_agent/` 服務建立；Phase 1b 起 tmux 過渡機制已退役）、SDK 子行程 env 白名單與 cwd 圈禁、Environment typed revisions（`app/project_environments.py`） | INV-PLANE-*、INV-AGENT-* |
| 3 | 運算與硬體資源配置（Compute & hardware allocation） | `app/scheduler.py` `pick_job()`（pin／tags／資料引力／priority／FIFO）、`app/monitor.py`、`app/auto_placement.py`（預設 off）、`ServerConfig.tags`＋`server_tag_present` 預檢；硬體資源模型已裁定（devices: 附掛資源，P1 實作中） | INV-APPROVAL-4b、DG-HARDWARE-EXECUTION v1 |
| 4 | 跨伺服器執行（Cross-server execution） | `app/sshpool.py`、`app/localrun.py`、`app/jobqueue.py` `build_*`＋哨兵協議、`app/execution_*.py`（attempt-driven，rollout flag）、`app/node_*.py`＋`agent/`（test-only） | INV-SSH-*、INV-NODE-*、INV-STATE-2 |
| 5 | 版本與 Artifact 管理（Versions & artifacts） | `project_versions`＋`app/code_promotion.py`＋`app/hub.py`；Run Template／Environment／Defaults typed immutable revisions；dataset snapshot／assets／alias；artifact 目前分散在 `engineering_task_artifacts`／`execution_attempt_artifacts`／`node_attempt_artifacts` 三張表（統一是硬體軌前置，見 ROADMAP） | INV-PLANE-1、DG-CODE-PROMOTE、DG-RUN-TEMPLATE-V2、DG-DATASET-* |
| 6 | Evidence 蒐集（Evidence collection） | `app/results.py`＋`app/jobfinish.py`（rsync `results/{job_id}/`）、`app/metrics_v1.py`（`run_metrics`／`run_metrics_collection`）、Product Run 投影（`app/product_runs.py`）、`docs/evidence/` | INV-SSH-6、DG-METRICS-CONTRACT、result-analysis 規則 |
| 7 | 狀態恢復（Recovery） | `jobqueue.db` 為唯一持久真相；scheduler reconcile（哨兵三分支）；project instance reconcile；launch 仲裁（`app/execution_launch.py`）；audit export outbox | INV-STATE-* |
| 8 | 完整 Audit（Audit） | `app/audit.py`（`audit.jsonl` append-only）＋`app/audit_store.py`／`app/audit_adoption.py`（hash-chained `audit_events`、trigger 拒絕 UPDATE/DELETE、at-least-once 匯出）；契約見 `docs/reference/AUDIT_LEDGER.md` | INV-AUDIT-* |

---

## 6. Invariants｜不變式（唯一正典）

本節是 dispatch-center 全部架構不變量的**單一真相源**。其他 skill 一律以 ID 引用，不得複製內容。
每條格式：**Statement**（不變量本身）／ **Scope**（適用範圍）／ **Enforcement**（現行落實機制）／
**Forbidden**（明確禁止的行為）／ **Verification**（對應測試或驗證方法）。

### INV-PLANE-*（平面邊界；2026-08-30 DG-PLATFORM-CHARTER v1 新增，編碼既有裁定）

#### INV-PLANE-1 Development Plane 產出只經人工 promotion 進入 Compute Plane
- **Statement**：Development Plane 的產出（workspace／worktree 中的程式碼、agent 的 diff、checkpoint bundle）進入 Compute
  execution lifecycle 的**唯一**正式 transition 是經人工核准 promotion 產生的 ProjectVersion。ExecutionPlan 只能引用
  promoted ProjectVersion；未 promote 的程式碼永遠進不了執行面。承 DG-CODE-PROMOTE-v1：P-1 promotion 永不自動核准；
  P-2 同 commit 重複 promote 為 no-op；P-3 無 promotion approval 的 `project_versions` 列是 `legacy_observed`，不得支撐
  reproducible run；P-4 promotion 只寫本地 hub、不推 GitHub；P-5 promotion 後保留 worktree。
- **Scope**：`app/code_promotion.py`、`agent_session_checkpoint`／`engineering_task_promote` 核准分支、ExecutionPlan v2 的
  version 解析、任何新的「程式碼 → 可執行版本」路徑。
- **Enforcement**：`engineering_task_promote` 只由人核准（manual-human 檢查）；approve 時重驗 bundle bytes、`git bundle verify`、
  建立不可執行 ProjectVersion、發布本地 hub ref；ExecutionPlan resolver 只接受 promoted version。
- **Forbidden**：直接改動正式 project instance 作為執行來源；讓 dirty worktree 成為 Run 的來源；讓「agent 說它改好了」本身
  變成可執行版本；任何自動 promotion 路徑；promotion 附帶 push external origin。
- **Verification**：`tests/test_code_promotion.py`、`tests/test_agent_session_checkpoint.py`、`tests/test_execution_plan_v2_api.py`。

#### INV-PLANE-2 Development validation 不是 Compute execution
- **Statement**：Development Agent（任一 provider）在隔離 workspace 內經 dispatch-controlled validation path 跑
  test／lint／typecheck／build，是 Development Plane 的驗證，**不是**Compute execution。training／GPU／worker workload、
  deployment、以及硬體動作（synthesis／bitstream、firmware build、flash／program、erase、power、HIL test）永遠不從
  workspace 啟動：它們只能以 promoted ProjectVersion → ExecutionPlan → approval → 執行層 → worker（＋附掛硬體）的鏈進入。
  其中實體動作（flash／program／erase／power）永不自動核准、永不由 agent 工具直接觸發。
- **Scope**：`dispatch_agent/`（runner agent 的 `can_use_tool`）、`app/engineering_validation.py`、過渡期的
  `app/agent_session_bundle.py`（checkpoint／bundle 供應鏈）、任何 validation mechanism、任何未來的硬體工作類型。
- **Enforcement**：validation path 由 SDK `allowed_tools`＋`can_use_tool` 封閉：檔案工具限工作區；Bash 依 INV-AGENT-2
  （驗證 allowlist 直接執行，其餘由 session 擁有者逐條即時允許）；
  Compute 工作只由 `approve()` 落地的 Job／attempt 派發；硬體工作類型在 DG-HARDWARE-EXECUTION 裁定前不存在。
- **Forbidden**：把「在工作區跑指令」延伸成「在工作機跑指令」；為 Development Plane 的便利開第二條繞過 approval 的執行路徑；
  在 validation path 內執行訓練、部署、燒錄、電源或任何對 workspace 以外資源有副作用的動作；agent 工具集出現任何
  直接觸發硬體動作的工具。
- **Verification**：`tests/test_dispatch_agent_permissions.py`、`tests/test_agent_session_bundle.py`／
  `tests/test_agent_session_checkpoint.py`、過渡期 `tests/test_claude_code_agent.py`（1b-2 退役）、`tests/test_agent_tools.py::test_no_approve_or_reject_or_shell_tools_registered`。

### INV-APPROVAL-*（核准流）

#### INV-APPROVAL-1 所有 material 寫入動作經核准流
- **Statement**：每個會改變系統 material state 的操作（派工、停止、改機器設定、匯入專案、改碼、部署、project membership、
  service-account／token lifecycle…）對應一個 approval kind，列入 `app/db.py` 的 `VALID_APPROVAL_KINDS`，由 `app/approvals.py`
  的 `request_*` 建立 pending approval、`approve()` 分支落地。以下封閉列舉是 authentication bookkeeping，不是 approval-gated
  material mutation：OIDC login-flow 建立與原子單次消耗、server-side session 建立、logout 撤銷該次送出的 session、以及嚴格以
  `(issuer, subject)` 建立的 OIDC identity binding。新 identity binding 建立 actor 時，platform-admin 只能由明確設定、精確比對的
  OIDC subject allowlist 決定；不得在後續登入提升既有 actor。email 只可作描述性 metadata；project membership、service-account 與
  service-token lifecycle 仍須經核准。
- **Scope**：所有 material mutating API 端點、agent 工具、MCP 工具；以及上述封閉列舉的 authentication-bookkeeping seams。
- **Enforcement**：`db.insert_approval()` 驗證 kind 白名單；`approve()` 是 material mutation 的唯一落地入口。OIDC bookkeeping
  只能使用 identity／session／login-flow 的窄 DB 介面；service-account／token／membership 仍使用既有 approval kinds。
- **明文例外表**（新例外需使用者裁定；表格取代舊版散文列舉，語意不變）：

  | 例外 | 性質 | 裁定來源 |
  |---|---|---|
  | `test-ssh` 等唯讀探測 | 無 material 寫入 | 既有明文例外 |
  | `experiment_records` 筆記 | 低風險筆記 | 既有明文例外 |
  | 冪等 `hub_sync` | 冪等、可重試、不改版本真相 | 既有明文例外 |
  | `server_add`／`server_update`／`server_disable`（含重新啟用）直接執行：`validate_server_config()` 驗證先行、備份、完整稽核；`server_delete` 仍經核准 | 使用者裁定 | DG-INFRA-DIRECT-ACTIONS v1（2026-08-26） |
  | Anthropic API key 的 UI 設定／清除：平台管理員、遮罩輸入、原子寫入 `.env`、值永不回傳／入 DB／入稽核 | 使用者裁定 | DG-ASSISTANT-CLAUDE-TURN v1（2026-08-26） |
  | `hardware_images.known_good` 由平台管理員在 UI 直接標記（或 `hil_test` 決定時勾選）：純標記、不觸發任何執行；回退燒錄本身仍是 `hardware_action_v2` 卡 | 低風險筆記（比照 `experiment_records`） | DG-HARDWARE-EXECUTION v1（2026-08-31） |

- **Forbidden**：新增其他「直接執行」的 material 寫入端點；把 authentication-bookkeeping 例外擴大到 membership、service-account
  或 service-token；以 email 查找、合併或識別 actor；first-login-wins 管理員；登入時修改既有 actor 的 material 權限；繞過
  `request_*`／`approve()` 直接呼叫執行層。
- **Verification**：`tests/test_approvals.py`、`tests/test_inventory_api.py`（「核准後才真的寫入」系列）、`tests/test_identity.py`、
  `tests/test_identity_api.py`、`tests/test_service_tokens.py`、`tests/test_oidc.py`。

#### INV-APPROVAL-2 危險／不合法請求在建立當下拒絕
- **Statement**：命中 `app/security.py` `is_dangerous()` 黑名單或驗證不過的請求，在**建立核准請求當下**就回 400 並寫稽核，
  不建立 approval、不給核准機會。
- **Scope**：所有 enqueue 路徑（REST、聊天、agent、MCP）、server_add／update 驗證。
- **Enforcement**：`app/jobqueue.py` `enqueue_job()` 先呼叫 `is_dangerous()`；`request_server_add_approval()` 先呼叫
  `validate_server_config()`。
- **Forbidden**：把驗證推遲到核准時才做（核准時**重驗**是加項，不是替代）；讓自動核准規則放行黑名單指令。
- **Verification**：`tests/test_security.py`、`tests/test_approvals.py` 危險指令不建 approval 系列。

#### INV-APPROVAL-3 核准當下重新驗證狀態
- **Statement**：`approve()` 各分支在執行前重查目標的當下狀態（job 還是 running 嗎、candidate 還是 pending 嗎、server 有沒有
  running job），建立請求時的檢查不足恃——等待期間狀態可能已變。
- **Scope**：`app/approvals.py` 所有 `approve()` 分支；新增 kind 必須比照。
- **Enforcement**：既有分支的「核准前重查」模式（stop／server_disable／git_init 等先例）。
- **Forbidden**：新 kind 的 approve 分支直接信任 payload 裡建立時的快照。
- **Verification**：`tests/test_approvals.py` 對應 kind 的過期狀態測試。

#### INV-APPROVAL-4 自動核准白名單只有 enqueue／stop
- **Statement**：`maybe_auto_approve()` 的 kind 閘門（`app/approvals.py`：`if approval.kind not in ("enqueue", "stop")`）永遠只認
  這兩個 kind；其他 kind（git_init、project_deploy、server_* 等）天然排除、永不自動核准。
- **Scope**：`app/approvals.py`、`app/autoapprove.py`、`auto_approve.yaml` 規則引擎。
- **Enforcement**：kind 閘門硬編碼；規則欄位只有 source／kind／command_regex／project／pin_server 五個。
- **Forbidden**：擴大 kind 白名單；新增規則欄位；`WEB_DIRECT_EXECUTE` 的一步生效擴及 web enqueue／stop 以外的 kind；把
  INV-APPROVAL-4b 的 policy-scoped 機制實作成 `maybe_auto_approve()` 的 kind 或規則。
- **Verification**：`tests/test_autoapprove.py`；release-gate `static_checks.sh` 釘住閘門那一行。
- **註記（DG-SINGLE-OPERATOR-CONFIRM v1，2026-09-02）**：Studio 的「確認並執行」單鍵是同一位登入者對剛建立的卡
  立即做出的**人工** v2 decision（digest 綁定、完整稽核、封閉 kind 清單），不是自動核准，不經 `maybe_auto_approve()`，
  不改本條白名單；promotion、刪除類、伺服器底層、runner／node／service、硬體實體動作永不提供單鍵。

#### INV-APPROVAL-4b Policy-scoped 自動決策只限 auto_placement（2026-07-18 DG-2 裁定新增）
- **Statement**：`kind=auto_placement` 的 pending approval 可以由**獨立於 `maybe_auto_approve()` 的** policy-scoped 機制自動核准，
  且只在同時滿足以下全部條件時：(1) `AUTO_PLACEMENT_KILL_SWITCH` 明確設定為停用狀態以外的值（預設值必須是「自動執行停用」）；
  (2) 提案引用的 dispatch policy 目前 head 仍為 `approved` 且 revision 與 payload 一致；(3) 目標伺服器在該 policy 的
  `allowed_servers` 精確清單內且 enabled；(4) 該 policy 的 `valid_until` 未過期；(5) 自動核准後的活躍 placement 數不超過
  `max_concurrent_placements`；(6) 指令重推導後的 SHA-256 與 payload 一致且通過 `is_dangerous()` 複檢。自動決策必須逐件寫入稽核，
  `decision_mechanism` 記為 `policy-{policy_id}-r{revision}`，決策者不是人也不得偽稱為人。
- **Scope**：`app/approvals.py` 的 auto_placement 決策路徑、`app/main.py` 的 auto placement 迴圈、`app/config.py` 的 kill switch。
- **Enforcement**：kill switch 預設停用；任一條件不滿足時提案**留在 pending**（不自動 reject、不降級執行）；policy archive 立即使其
  所有後續自動決策失效。
- **Forbidden**：把 auto_placement 加進 `maybe_auto_approve()` 白名單；對 enqueue／stop／auto_placement 以外任何 kind 建立
  policy-scoped 自動決策；繞過 (2)–(6) 任何一項 revalidation；自動核准一個 payload 與當下重推導結果不一致的提案。
- **Verification**：`tests/test_auto_placement.py` 的 Slice 5 測試群（範圍內自動／超界留 pending／archive 即停／kill switch 即停／
  稽核完整性）。
- **Provenance**：使用者 2026-07-18 於 `docs/DECISIONS.md` 裁定 DG-2 核准（`docs/archive/GOAL_2_AUTOMATED_DISPATCH_PLAN.md` §3）。

#### INV-APPROVAL-5 認證涵蓋所有新端點
- **Statement**：`AUTH_TOKEN` 有設定或 `OIDC_ENABLED=true` 時，只有 `GET /`、`GET /auth/login`、`GET /auth/callback` 與 `/static/*`
  前綴不要求既有 credential。`GET /auth/login` 與 `GET /auth/callback` 只供 OIDC Authorization Code + PKCE handshake；
  `GET /auth/me`、`POST /auth/logout` 與所有其他 application API 仍須由有效 server-side session、明確啟用的 service bearer、
  或相容的 `X-Auth-Token` 通過認證。WS `/ws` 保留有效 session／service credential 或連線後首則 auth 訊息的相容協議。
- **Scope**：`app/main.py` auth middleware；每個新端點。
- **Enforcement**：middleware 是預設涵蓋（不豁免＝受保護），豁免以 `_AUTH_EXEMPT_ROUTES` 的 HTTP method + exact path 封閉列舉；
  `/static/*` 是唯一豁免前綴；新端點零設定即受保護。
- **Forbidden**：新增任何其他豁免 method／path 或豁免前綴；豁免 `GET /auth/me` 或 `POST /auth/logout`；把 credential 放進
  query string；把 OIDC handshake exemption 擴成一般 `/auth/*` exemption。
- **Verification**：`tests/test_auth.py`、`tests/test_oidc.py`、`tests/test_authorization_coverage.py`；release-gate 釘住完整
  method／path 豁免集合。

### INV-SSH-*（SSH 執行邊界）

#### INV-SSH-1 SSH 後端無 agent、依賴封頂（2026-07-19 DG-C 修訂）
- **Statement**：SSH 執行後端不依賴工作機上任何本系統常駐程式；遠端依賴只有 `tmux`、`bash`、`nvidia-smi`（GPU 機），一切互動經
  asyncssh（`app/sshpool.py`）。rsync 由 Server A 端發起。工作機上「可以」另外存在經 `INV-NODE-*` 管理的 Node Agent，但
  **SSH 後端的行為與依賴永遠不得假設它存在**；SSH 後端永久保留為每台工作機的相容／緊急通道，不因 Node Agent 上線而移除或弱化。
- **Scope**：所有產生遠端指令的模組。
- **Enforcement**：架構慣例；`servers.yaml.example` 與 README 明文；ExecutionBackend seam（C1）以 golden tests 證明 SSH 路徑
  指令字串逐字不變。
- **Forbidden**：SSH 後端的遠端指令引入其他工具依賴（python、jq、curl…）；SSH 後端假設／呼叫任何 agent 端點；以 Node Agent
  存在為由刪除或跳過任何 INV-SSH-2…9 的保證。
- **Verification**：code review；新遠端指令的配對測試斷言完整指令字串；C1 golden tests。

#### INV-SSH-2 使用者指令原文只經 SFTP 落地
- **Statement**：使用者任務指令（`job.command`）寫入 `agent_jobs/{id}/cmd.sh` 一律走 SFTP（`ssh_write_file`），不得插值進任何
  shell 指令字串——這是免除 shell 跳脫問題的結構性解法。
- **Scope**：`app/scheduler.py` `dispatch_job()`、`app/jobqueue.py` build_* 家族。
- **Enforcement**：`build_run_sh_content()`／`build_launch_command()` 只插值 int 型 `job_id`。
- **Forbidden**：任何 f-string／`%` 把 `job.command` 或其他自由文字拼進 `ssh_run` 的指令。
- **Verification**：`tests/test_jobqueue.py`、`tests/test_scheduler.py`（FakeSSH 斷言指令字串）。

#### INV-SSH-3 遠端指令由純函式建構、值先驗證或 quote
- **Statement**：每條遠端指令字串由可單測的純函式 `build_*` 產生；插值進去的值要嘛先過字元集驗證（`validate_name_component` 類），
  要嘛 `shlex.quote()`；路徑用 home 相對（SFTP 不展開 `~`，見 `app/jobqueue.py` 模組註解）。
- **Scope**：`app/jobqueue.py`、`app/datasets.py`、`app/inventory.py`、`app/activity.py`、`app/results.py`、`app/hub.py`、
  `app/approvals.py` 內組指令處。
- **Forbidden**：未驗證／未 quote 的使用者輸入進指令；在呼叫點內聯拼指令（繞過純函式）。
- **Verification**：各 build_* 的單元測試；`tests/test_inventory.py` 秘密檔過濾系列。

#### INV-SSH-4 唯讀探測是封閉指令集
- **Statement**：inventory 掃描、activity 探測、`test-ssh` 只跑各自明文列舉的固定唯讀指令；秘密檔名／目錄過濾在**組指令的 Python 層**
  完成，不依賴遠端執行時攔截。
- **Scope**：`app/inventory.py`、`app/activity.py`、`app/server_config.py` `test_ssh_connection()`。
- **Forbidden**：接受任意路徑／任意指令參數；新增指令而不同步補白名單說明與測試。
- **Verification**：`tests/test_inventory.py`、`tests/test_server_config_api.py`（mock 斷言只跑固定指令）。

#### INV-SSH-5 逾時明確、長工不佔連線
- **Statement**：每次 `ssh_run` 帶明確 timeout（連線 10s、指令預設 30s，`app/config.py`）；長時間工作一律丟進 tmux detached session
  立即返回，絕不在 SSH 通道上等它完成。
- **Scope**：所有 `ssh_run`／`local_run` 呼叫點。
- **Forbidden**：無 timeout 的遠端呼叫；用 SSH 同步等待訓練／同步類長工。
- **Verification**：code review + FakeSSH 測試的 timeout 參數斷言。

#### INV-SSH-6 成敗判定只看哨兵 exit_code
- **Statement**：任務終態判定唯一依據是 `agent_jobs/{id}/exit_code` 檔案；不解析 log 內容、不以 tmux session 存活與否為準
  （tmux 只供人工 attach 觀看）。
- **Scope**：`app/jobqueue.py` `reconcile_job()` 與所有消費它的地方。
- **Forbidden**：從 log 文字猜成敗；把 tmux GONE 直接當 failed。
- **Verification**：`tests/test_jobqueue.py` reconcile 三分支測試。

#### INV-SSH-7 連不上＝跳過，不下判定
- **Statement**：SSH 例外（`SSHUnreachableError` 等）一律視為「本輪跳過、狀態不動」，unreachable 永不等於 failed；離線機上的
  running 任務保持 running，等機器回線後再 reconcile。
- **Scope**：`app/scheduler.py`、`app/jobqueue.py`、`app/monitor.py`。
- **Forbidden**：把連線失敗落地成任務失敗；對離線機的任務做任何狀態變更。
- **Verification**：`tests/test_scheduler.py` 離線機分支測試。

#### INV-SSH-8 host-key 政策是已記錄的取捨
- **Statement**：`app/sshpool.py` 的 `known_hosts=None` 是在 Tailscale 私網前提下的明文取捨；變更 host-key 驗證政策是架構決策
  （`DG-SSH-HOSTKEY`）。
- **Scope**：`app/sshpool.py`。
- **Forbidden**：在不相關的改動中順手「修好」它；反之，新增的 SSH 呼叫路徑也不得引入與現行不一致的政策。
- **Verification**：code review（此層無單元測試）。

#### INV-SSH-9 取消運行中任務必經核准
- **Statement**：停止 running 任務的唯一路徑是 kind=stop approval 核准後 `tmux kill-session -t job_{id}`；kill 之後終態仍由哨兵協議判定。
- **Scope**：`app/approvals.py` stop 分支、所有停止入口。
- **Forbidden**：任何入口（前端、agent、MCP、排程器）直接 kill；排程器自動殺「疑似卡死」任務（stall 只標旗標，INV-STATE-4）。
- **Verification**：`tests/test_approvals.py` stop 流程測試。

### INV-NODE-*（Node Agent 執行邊界；2026-07-19 DG-C 核准）

> 依 `docs/DECISIONS.md` 2026-07-19 DG-C 裁定寫入（草稿：`docs/decisions/DG_C_INVARIANT_REVISION_DRAFT.md`）。
> 這些不變量自裁定日起生效。實作現況：Node daemon 與 v2 協議**已實作但 test-only／default-off**，沒有任何 node 被啟用，
> 實機啟用以 `DG-NODE-CANARY` 為前提——現況一律以帳本為準。它們不影響既有 SSH 後端的任何行為。

#### INV-NODE-1 Agent 身分與出站單向連線
- **Statement**：Node Agent 是**非 root** 的小型常駐服務，只發起**出站**已驗證 HTTPS 輪詢；工作機不得開放任何入站控制埠。每個 agent
  持有一組可個別撤銷的 node credential，control plane 對每個請求驗證 node 身分；credential 洩漏的處置是撤銷該 node，不影響其他 node。
- **Forbidden**：入站 listener、共享 credential、以 IP／hostname 取代 credential 驗證、agent 以 root 執行。
- **Verification**：agent 套件測試（全 fake、不碰真機）；authorization catalog 覆蓋 node 端點。

#### INV-NODE-2 Lease／acknowledgement 執行語意
- **Statement**：一次 attempt 只能被一個 agent lease；agent 必須先原子性 acknowledge 並把 attempt 身分持久化到本機，才能啟動任何副作用。
  control plane 在 lease 未過期且未收到終態前，**不得**把同一 attempt 再派給任何通道（含 SSH）。
- **Forbidden**：無 lease 的執行、ack 前產生副作用、lease 期內重複派發。
- **Verification**：協議層 fake 測試覆蓋 lease 競態、重複 ack、過期 reclaim。

#### INV-NODE-3 指令位元組非插值落地
- **Statement**：核准的指令位元組由 agent 寫入檔案後，以只含已驗證識別字的 launcher 啟動——與 `INV-SSH-2`／`INV-SSH-3` 同構：
  自由文字永不拼進任何 shell 字串，digest 與核准 payload 綁定。
- **Forbidden**：agent 端任何形式的指令字串插值；執行未經核准 digest 比對的位元組。
- **Verification**：launcher 純函式測試；digest 比對測試。

#### INV-NODE-4 心跳過期＝unknown，不是 failed
- **Statement**：心跳過期、agent 連不上、輪詢中斷一律判 `unknown`，不得推斷任務失敗（與 `INV-SSH-7` 同構）。狀態收斂唯一依據是
  agent 回報的持久化終態或（SSH 相容通道的）哨兵檔案。
- **Forbidden**：以心跳缺席把 running 任務標 failed；以 unknown 觸發自動重派。
- **Verification**：reconciliation 測試覆蓋 agent 消失／重啟／回歸各情境。

#### INV-NODE-5 重啟不重複、狀態可收斂
- **Statement**：control plane 或 agent 任一方重啟後，已 acknowledge 的 attempt 不得被重複啟動；雙方各自以持久化紀錄（DB／本機 attempt 檔）
  收斂，收斂規則必須可測（fake 時序測試）。
- **Forbidden**：以記憶體狀態判斷 attempt 歸屬；重啟後自動重跑未確認終態的 attempt。
- **Verification**：雙側重啟矩陣的 fake 測試（Phase 5 量化門檻：≥100 jobs／≥2 nodes／7 天零重複啟動零假失敗）。

#### INV-NODE-6 逐台提升、隨時回退
- **Statement**：Node Agent 以**每台工作機**為單位明確啟用；未啟用的機器完全走 SSH 後端。任何一台可在不影響其他機器的情況下回退到 SSH；
  Codex Runner 的遷移放在所有普通 worker 之後（C4 通過才動）。
- **Forbidden**：全域一刀切開關；移除 SSH 後端程式碼；讓回退需要資料遷移。
- **Verification**：per-node 開關測試；回退演練紀錄。

### INV-AGENT-*（runner agent；2026-08-30 DG-AGENT-RUNTIME-V3 新增）

> runner 上的 `dispatch-agent` 服務是第二種工作機常駐服務（第一種是 Node Agent）。它承載 Development Agent 的 SDK session，
> 不是 Compute 執行通道。實作自 Phase 1a 起；在那之前這兩條約束已生效，過渡期的 tmux 回合不得弱化它們。

#### INV-AGENT-1 runner agent 身分與連線
- **Statement**：`dispatch-agent` 是**非 root** 的常駐服務，只發起**出站**已驗證連線到 Server A；工作機不開任何入站控制埠。每台
  runner 一組可個別撤銷的登錄 credential（`agent_runner_enroll` 核准後一次性顯示、只存 runner；`agent_runner_revoke` 撤銷）。心跳
  過期、斷線＝session `unknown`，不是 failed（INV-SSH-7 同構）。SDK 子行程環境只含必要變數（`HOME`／`PATH`／`LANG`／Claude 憑證），
  登錄 credential 與任何平台 token 永不進 SDK env。Claude 憑證（`CLAUDE_CODE_OAUTH_TOKEN`）只在 runner；Server A 永不持有。
  工作區只能建在 dispatch-agent 設定的 root 之下，由服務（不是模型）建立。
- **Scope**：`dispatch_agent/` 套件、Server A 的 runner gateway、`agent_runners` 登錄表。
- **Forbidden**：入站 listener；共享或硬編碼 credential；Server A 持有 Claude 憑證；模型可讀登錄 credential；以 IP／hostname
  取代 credential 驗證；以斷線推斷失敗；agent 以 root 執行。
- **Verification**：`tests/test_dispatch_agent_*.py`（env 白名單、登錄拒絕、重連＝unknown）、`tests/test_agent_gateway.py`。

#### INV-AGENT-2 工作區權限提示
- **Statement**：Development Agent 的 Bash 只在工作區 cwd 內啟動；驗證 allowlist 內的指令直接執行；**其餘每一條指令都必須經
  session 擁有者在瀏覽器即時允許才執行**（提示顯示完整指令；「本 session 一律允許」只對相同指令模式有效、session 關閉即失效）。
  提示與決定持久化於 `agent_permission_requests` 並稽核；逾時＝拒絕。提示永遠不能決定任何 `approvals` 卡；平台級動作
  （run／experiment／promote／伺服器）只能經 MCP `request_*` 建卡；`TOOLS`／MCP 工具集永無 approve／shell（INV-LLM-2）。
  **殘餘風險（明寫）**：被允許的指令以 runner 使用者身分執行，能做到該使用者能做的事——安全靠人逐條看，不靠黑名單。
- **Scope**：`dispatch_agent/permissions.py`（`can_use_tool` 決策）、Server A 的權限提示路由與 Studio 提示元件。
- **Forbidden**：自動允許非 allowlist 指令；把提示做成核准卡的替代品或反之；agent 自行修改 allowlist；提示不留紀錄；
  從工作區啟動訓練／部署／燒錄（INV-PLANE-2）。
- **Verification**：`tests/test_dispatch_agent_permissions.py`（決策矩陣：工作區內允許／allowlist 直接／其餘提示／逾時拒絕）、
  `tests/test_agent_tools.py::test_no_approve_or_reject_or_shell_tools_registered`。

### INV-STATE-*（狀態一致性）

#### INV-STATE-1 持久真相 vs 可拋棄快取的所有權
- **Statement**：任務／專案／資料集／核准的持久真相 = `jobqueue.db` + 工作機上的哨兵檔；`server_states`／`server_configs`
  （`app/main.py` AppState 的 in-memory dict）是可拋棄的執行期快取，重啟歸零、由 monitor loop 與 `servers.yaml` 重建。
  AgentSession 的事件、權限提示與決定的持久真相在 SQLite（`agent_session_events`、`agent_permission_requests`，Phase 1a 起）；
  runner 上的 transcript／worktree 是分散式證據；browser、WebSocket 與記憶體中的 session 狀態永遠不是真相。
- **Scope**：所有新功能的狀態設計。
- **Forbidden**：把「只存在 in-memory」的資料當持久真相；把需要跨重啟存活的狀態只放 `server_states`。
- **Verification**：重啟恢復測試（`tests/test_scheduler.py` reconcile 系列）。

#### INV-STATE-2 先寫 DB、再做遠端副作用；僅 definite pre-launch failure 可退回
> 2026-07-27 依 `DG-AMBIGUOUS-LAUNCH-v1` 修訂（`docs/decisions/DG_AMBIGUOUS_LAUNCH_DECISION.md` §3.2）。修訂前的條文把
> 「派發失敗顯式 revert 回 queued」寫成無條件規則，無法區分「證明沒啟動」與「不知道有沒有啟動」。

- **Statement**：「DB 寫入 + 遠端副作用」的順序一律 DB 在前（先標 running 再派發），崩潰窗口留下的中間態必須能被 reconcile 收斂。
  派發過程失敗時，**只有 definite pre-launch failure 才可以把 Job 從 running 退回 queued**。definite pre-launch failure 的定義是：
  存在 transport 或遠端 arbitration 證據，足以證明 workload 不可能已經在目標機上啟動。其判定只有兩種來源：(a) transport 層證明
  launch request 未被送出或未被接受；(b) 控制端自己以原子操作贏得該 attempt 的 remote launch claim，使 launcher 永遠不可能再啟動。
- **Statement（續）**：launch response timeout、連線中斷、或任何無法歸類的例外，一律視為 **ambiguous**，不是失敗。此時 Job 保持
  running，attempt 保持原 target 與原 backend、`liveness=unknown`，只能以相同 attempt 與相同 idempotency key 對同一台目標機重試查證。
- **Scope**：`app/scheduler.py` 派發路徑、`ExecutionBackend` 的 prepare／launch／inspect、reconciler，以及任何新的「落地+副作用」代碼。
- **Forbidden**：先做副作用再寫 DB；把 timeout／連線中斷／未知例外當成派發失敗而 revert；以「沒看到 tmux／sentinel」作為未啟動的證明；
  在 ambiguous 狀態下建立第二個 attempt 或改派其他伺服器；留下 reconcile 無法辨識的中間態。
- **Verification**：`app/execution_launch.py` 的 definite／ambiguous 分類與仲裁函式；`tests/test_execution_launch_arbitration.py` 的
  crash matrix。
- **Legacy exception（尚未收斂）**：`app/scheduler.py` 的 legacy 派發路徑目前仍對任何 `dispatch_job()` 例外無條件 revert，因為它沒有
  claim／receipt 可供仲裁。這是已登記的釋出阻擋項 `RB-LAUNCH-001`，由 attempt-driven 路徑（rollout flag）接上後收斂；在那之前它是
  **已知缺陷，不是合規行為**，不得引用它作為新程式碼的先例。

#### INV-STATE-3 schema 演進走雙軌遷移
- **Statement**：新欄位必須同時進 `SCHEMA` 常數（新 DB）與對應的 `_*_COLUMN_MIGRATIONS`（既有 DB 的 `ALTER TABLE ADD COLUMN`），
  並補遷移測試；版本化 additive migration 依 `app/migrations.py`；不刪欄、不改既有欄位語意。
- **Scope**：`app/db.py`、`app/migrations.py`。
- **Forbidden**：只改 SCHEMA 不補遷移；破壞性 schema 變更。
- **Verification**：`tests/test_db_migration.py`、`tests/test_migrations.py`。

#### INV-STATE-4 任務狀態機封閉
- **Statement**：合法轉移限於 queued→running→done／failed、queued→blocked／cancelled、running→queued（requeue，tmux 中斷）、
  blocked 依 refresh 邏輯；done／failed／cancelled 是終態不復活。stall 偵測只設 `stalled_suspect` 旗標，不改 status。
  新狀態值或新轉移需 `DG-JOB-STATE` 類的具名裁定。
- **Scope**：`app/jobqueue.py`、`app/scheduler.py`、`app/approvals.py`。
- **Forbidden**：引入新狀態值或新轉移而未經使用者裁定；讓監控／偵測邏輯改動 status。
- **Verification**：`tests/test_jobqueue.py` 狀態機測試、`tests/test_stall.py`。

#### INV-STATE-5 servers.yaml 變更三步曲
- **Statement**：`servers.yaml` 的程式化修改一律 `backup_servers_yaml()` → `write_servers_yaml_atomically()`（暫存檔+`os.replace`）→
  in-memory 熱替換（`reload_server_config_if_supported()`）；不留「要重啟才生效」的半套。
- **Scope**：`app/server_config.py`、`app/approvals.py` server_* 分支、DG-INFRA-DIRECT-ACTIONS 的直接執行路徑。
- **Forbidden**：直接 `open(..., "w")` 寫 servers.yaml；跳過 backup；改了檔案不同步 in-memory。
- **Verification**：`tests/test_server_config.py` atomic write／backup 測試。

#### INV-STATE-6 背景工作不卡排程輪
- **Statement**：可能耗時的任務結束 hook（拉結果／寄信／metrics 解析）以同步回呼把工作丟進 AppState 追蹤的 `asyncio.create_task()`，
  排程輪本身不 await 它們；單行程 asyncio 下，跨 `await` 點的共享狀態操作要重查最新狀態。
- **Scope**：`app/main.py` AppState、`app/jobfinish.py`、任何新 hook。
- **Forbidden**：在 scheduler／monitor loop 內 await 長工；裸 `create_task` 不經 AppState 追蹤；在迭代舊快照後直接落地寫入而不重查。
- **Verification**：`tests/test_main_background_hooks.py`。

### INV-AUDIT-*（稽核）

#### INV-AUDIT-1 audit.jsonl 是 append-only
- **Statement**：`audit.jsonl` 只透過 `app/audit.py` `append_audit()` 以 `"a"` 模式追加；程式永不改寫、刪除、輪替它。durable
  `audit_events` 帳本與其並行：以 SQLite trigger 拒絕 UPDATE／DELETE、hash-chained、匯出至 JSONL 是 at-least-once
  （契約：`docs/reference/AUDIT_LEDGER.md`）。
- **Scope**：全部程式碼。
- **Forbidden**：以 `"w"` 模式開啟；任何就地編輯／truncate；測試以外的代碼刪檔；回填舊 JSONL 進 `audit_events`。
- **Verification**：release-gate `static_checks.sh` 釘住 open 模式；`tests/test_durable_audit.py`；code review。

#### INV-AUDIT-2 每個動作進稽核
- **Statement**：核准生命週期（建立／核准／拒絕／自動核准，含 `approved_by` 標記）與任務生命週期（enqueue／dispatch／done／failed／
  requeue／cancel／stop）每一步都寫一筆 `{ts, action, params, result}`；新 kind／新動作比照，且動作必須能對應到 `app/audit_adoption.py`
  的 typed catalog（CI 的 adoption gate 拒絕未分類的寫入者）。
- **Scope**：所有 `request_*`、`approve()` 分支、scheduler 落地點；新 kind／新動作比照。
- **Forbidden**：新增的寫入動作沒有對應 audit action；稽核寫入失敗導致主流程中斷（稽核是 best-effort 追加；durable 事件與其擁有的
  transaction 同 commit）；憑證或指令原文進入稽核。
- **Verification**：各測試檔對 audit 內容的斷言（`tests/test_api.py` 等）；`scripts/audit_adoption_gate.py`。

### INV-LLM-*（LLM／MCP／助手權限邊界）

#### INV-LLM-1 LLM 通道只能建 pending approval
- **Statement**：所有 LLM 入口（WS `/ws`、`POST /agent/chat`、MCP bridge、runner Claude turn）的寫入能力上限 = 呼叫既有
  `request_*_approval()` 建立 pending approval；最壞情況（prompt injection 全成功）的血本封頂是「一張待審卡片」。
- **Scope**：`app/agent_tools.py`、`app/agent_runtime.py`、`app/chat.py`、`app/mcp_bridge.py`。
- **Forbidden**：LLM 工具直接呼叫 `approve()`、`enqueue_job()`、執行層或檔案寫入（`experiment_records` 筆記類是既有明文例外）。
- **Verification**：`tests/test_agent_tools.py`、`tests/test_mcp_bridge.py`（含 prompt injection 測試）。

#### INV-LLM-2 永遠沒有 approve／reject／自由 shell 工具
- **Statement**：agent 工具表（`TOOLS`）與 MCP bridge 工具集永不包含 `approve`／`approve_approval`／`reject`／`reject_approval`／`shell`／
  `exec`／`run_command`；提案通道與批准通道結構性分離。
- **Scope**：`app/agent_tools.py`、`app/mcp_bridge.py`、任何未來暴露給 LLM 的工具集。
- **Forbidden**：以任何名義（除錯、管理員模式、「有 token 保護」）新增這類工具。
- **Verification**：`tests/test_agent_tools.py::test_no_approve_or_reject_or_shell_tools_registered`；release-gate 靜態複驗。

#### INV-LLM-3 不存在 LLM→SSH 直通路徑
- **Statement**：`app/agent_tools.py` 不得 import `app.sshpool`、`app.localrun`、`subprocess`；工具需要的 SSH 能力（如 `test_server_ssh`）
  只能經呼叫端注入的 `ctx.ssh_run` callable，且僅限封閉唯讀指令集（INV-SSH-4）。
- **Scope**：`app/agent_tools.py` 及其被 import 的路徑。
- **Forbidden**：直接 import 執行層；把注入的 callable 用於白名單之外的指令。
- **Verification**：`tests/test_agent_tools.py::test_agent_tools_does_not_import_ssh_or_subprocess_modules`；release-gate 靜態複驗。

#### INV-LLM-4 MCP bridge 是行程隔離的 HTTP client
- **Statement**：`app/mcp_bridge.py` 是獨立行程，不 import 任何 `app.*` 模組，只透過 HTTP（httpx + `X-Auth-Token`）呼叫平台 REST API；
  bridge 掛掉或被攻破不影響平台本體。
- **Scope**：`app/mcp_bridge.py`。
- **Forbidden**：`from app.xxx import ...`；bridge 直接讀 `jobqueue.db`／`servers.yaml`；bridge 重做平台已有的判斷（危險指令等——
  原樣轉述 400 即可）。
- **Verification**：`tests/test_mcp_bridge.py`；release-gate 靜態複驗 import。

#### INV-LLM-5 LLM 層可選、缺席不影響本體
- **Statement**：anthropic key／套件、vLLM 設定、mcp 套件、runner 上的 Claude CLI 任一缺席時，對應功能明確降級（規則式後備／503／跳過，
  並顯示中文原因），排程、核准、執行、reconcile、結果收集完全不受影響。
- **Scope**：`app/llm.py`、`app/llm_local.py`、`app/mcp_bridge.py` 的 import 守護與開關判斷。
- **Forbidden**：核心路徑對選配套件產生硬依賴；降級分支拋未處理例外。
- **Verification**：`tests/test_llm.py`／`tests/test_llm_local.py`／`tests/test_chat.py`
  （開發環境本身沒裝 anthropic，全綠即證明）。

### INV-TEST-*（測試邊界）

#### INV-TEST-1 測試不需要真實環境
- **Statement**：測試一律用 FakeSSH／假 `local_run`／假 `send_mail`／duck-typed 假 LLM client 與 `fastapi.testclient.TestClient`，
  不需要真 SSH、真 GPU、真 SMTP、真 API key、真網路。唯一例外：`tests/test_localrun.py` 用本機 `bash`／`tmux`（它測的就是本機
  subprocess 層）。
- **Scope**：`tests/` 全部。
- **Forbidden**：新測試連真機／真服務；測試依賴 `.env` 裡的真實憑證；測試寫入 repo 根目錄的 `jobqueue.db`／`audit.jsonl`（用 tmp fixture）。
- **Verification**：`tests/conftest.py` 的 fixture 設計；CI 無網路即可全綠。

#### INV-TEST-2 邊界不變量有測試釘住，不得鬆綁
- **Statement**：INV-LLM-2／3、INV-PLANE-2 等安全邊界由明確測試釘住（`tests/test_agent_tools.py` 的 forbidden_modules／forbidden_names、
  allowedTools／confinement pins 等）；修改功能時可以擴充這些測試，不得刪除或弱化其斷言。文件治理由 `tests/test_document_authority.py`
  釘住（歷史文件標明取代來源、裁定紀錄不改寫歷史、live 文件連結存在）。
  釐清（DG-CONSOLIDATION-v1 C-1，2026-09-02）：結構簿記 pin（schema-version 字面值、route／工具數量、CI 步驟順序、
  openapi hash、coverage 白名單、帳本逐列字面值）不屬於本條保護範圍，可重寫為單一來源斷言或刪除；本條保護的是安全邊界斷言。
- **Scope**：所有標註「測試釘住」的不變量。
- **Forbidden**：為了讓新代碼過關而放寬釘住斷言；刪除釘住測試。
- **Verification**：release-gate 檢查測試檔案存續；code review。

---

## 7. Decision Register｜裁定登錄

完整條文在 `docs/DECISIONS.md`（依日期分段，append-only）；決策 packet 在 `docs/decisions/`。本表只做索引與狀態。
狀態：**active**＝現行有效；**superseded**＝被後續裁定取代；**closed**＝一次性事件已完成；**pending**＝尚未裁定。

### 7.1 已裁定（Ruled）

| 日期 | 裁定 | 一行摘要 | 狀態 | Packet |
|---|---|---|---|---|
| 2026-07-16 | AI Engineering D1–D7 | D1 app-server adapter bounded；D2 runner 隔離附條件；D3 指令／task-lifecycle 核准拆分；D4 secret-reference keep disabled；D5 Run Profile v1；D6 GitHub 發布 interface+fake；D7 授權本輪不涉及；瀏覽器視覺證據 disposable install | active | `decisions/AI_ENGINEERING_DECISION_GATE.md` |
| 2026-07-17 | D5／D6 解鎖實作 | 前提滿足，開放實作（仍不啟用） | closed | 同上 |
| 2026-07-18 | DG-1／DG-2（Goal 2） | 政策驅動自動放置提案核准；policy-scoped 自動決策 → INV-APPROVAL-4b | active | `archive/GOAL_2_AUTOMATED_DISPATCH_PLAN.md` |
| 2026-07-19 | Goal 3 啟動範圍與 DG-B | Goal 3 範圍核准 | closed | `archive/GOAL_3_FUTURE_WORK_PLAN.md` |
| 2026-07-19 | DG-C | INV-SSH-1 修訂；INV-NODE-1…6 核准 | active | `decisions/DG_C_INVARIANT_REVISION_DRAFT.md` |
| 2026-07-25 | Phase A 範圍緊縮 | A2–A4 移除、A1 保留；DG-A 不再需要裁定 | closed | — |
| 2026-07-25 | DG-B4 | 新機 dataset 預熱，先落地後啟用 | active | `decisions/DG_B4_DATASET_PREWARM_DRAFT.md` |
| 2026-07-27 | DG-EXEC-ATTEMPT-v1 | attempt／operation／outbox recommended contract | active | `decisions/DG_EXEC_ATTEMPT_DECISION.md` |
| 2026-07-27 | DG-AMBIGUOUS-LAUNCH-v1＋DG-EXEC-ATTEMPT-v1.1 | definite／ambiguous launch 仲裁 → INV-STATE-2 修訂 | active | `decisions/DG_AMBIGUOUS_LAUNCH_DECISION.md` |
| 2026-07-27 | WP-2D canary 延後 | 跳過 24h 窗 | superseded（DG-WP2D-CANARY-v2） | — |
| 2026-07-27 | DG-DATASET-SNAPSHOT-v1 | 不可變 dataset snapshot 契約 | active | `decisions/DG_DATASET_SNAPSHOT_DECISION.md` |
| 2026-07-28 | DG-CODE-PROMOTE-v1 | P-1…P-5 promotion 規則 | active（→ INV-PLANE-1） | `decisions/DG_CODE_PROMOTE_DECISION.md` |
| 2026-07-28 | DG-NODE-V2-v1 | Node v2 lease／終態／完成操作契約 | active | `decisions/DG_NODE_V2_DECISION.md` |
| 2026-08-01 | DG-WP2D-CANARY-v2 | 8 小時 canary 窗與實機啟動；候選 `d73a38e` 通過（`docs/evidence/WP2D_V2_20260802_D73A38E.md`） | active | `decisions/DG_WP2D_CANARY_V2_DECISION.md` |
| 2026-08-07 | DG-PRODUCT-V2-BASELINE-v1 | Product v2 執行計畫為核准實作計畫；授權／RBAC／API v2／bootstrap／dataset sharing 五項 | active（計畫檔已歸檔） | `archive/PRODUCT_V2_EXECUTION_PLAN.md` |
| 2026-08-07 | DG-API-V2-FOUNDATION-v1 | `/api/v2` root、APIError／request-id、cursor、Migration v5 | active | — |
| 2026-08-07 | DG-PRODUCT-RBAC-V2-v1 | Migration v6 多角色 RBAC、two-person 角色變更 | active | — |
| 2026-08-07 | DG-PROJECT-BOOTSTRAP-V2-v1 | Migration v7、typed bootstrap、Project Wizard | active | — |
| 2026-08-07 | DG-PROJECT-ENVIRONMENTS-V1 | `host-environment-v1` 不可變 revisions | active | — |
| 2026-08-07 | DG-RUN-TEMPLATE-V2 | `run-template-spec-v2`、shell-free argv、canonical decimal | active | — |
| 2026-08-09 | DG-DATASET-ASSETS-V2-v1 | Migration v8 六表 dataset governance | active | — |
| 2026-08-09 | DG-DATASET-SHARING-V2-v1 | offer／accept／grant／revoke | active | — |
| 2026-08-09 | DG-DATASET-PUBLISH-V2-v1 | local-path／Run-output 發布為 snapshot | active | — |
| 2026-08-10 | DG-EXECUTION-PLAN-V2-v1 | Migration v9、preview／submit、一 plan 一 Job | active | — |
| 2026-08-10 | DG-PRODUCT-RUN-EXPERIENCE-V2-v1 | 單一 canonical Product Run 投影、Clone／Compare／Stop／Artifacts | active | — |
| 2026-08-16 | DG-SELF-APPROVAL-OPTION-v1 | `ALLOW_HIGH_RISK_SELF_APPROVAL` default-off 部署政策 | active | — |
| 2026-08-23 | 產品最終完成品釐清 PROD-1…7 | 使用者模型、typed revisions 為真相、GitHub 為紀錄、對話＋task 並存、限額迴圈、Node 為主力、Claude 為主力 provider | active；定位陳述由 DG-PLATFORM-CHARTER v1 取代 | `product/ROADMAP.md` |
| 2026-08-23 | DG-PERSONAL-PILOT-v1 D1–D4 | 單人 pilot：現行安全姿態、正常 promote 流程、最小 results 讀取、legacy-first | active | `archive/PERSONAL_PILOT_PLAN.md` |
| 2026-08-24 | DG-CLAUDE-ADAPTER v1 C-1…C-6 | `claude-code-v1` 為第二 provider（＋2026-08-26 實機修正） | superseded by DG-AGENT-RUNTIME-V3（Phase 1b 已完成 2026-08-31：job-backed 回合與 provider 執行類別移除） | `decisions/DG_CLAUDE_ADAPTER_DECISION.md` |
| 2026-08-24 | DG-CONVERSATION-V1 CV-1…CV-6 | 每 Project AI conversation（2a）；2b 由 DG-AGENT-SESSION-V1 取代 | active／2b superseded | `decisions/DG_CONVERSATION_V1_DECISION.md` |
| 2026-08-24 | DG-AGENT-SESSION-V1 D1–D6、E-1…E-3 | Hybrid web-hosted Claude Code runtime：`agent_session_open`、per-turn 通道、D3 confinement、D4 零平台工具 | active（D1 kind、checkpoint 供應鏈保留；D2 tmux 通道 Phase 1b 已移除 2026-08-31） | `archive/AGENT_SESSION_V1_PLAN.md` |
| 2026-08-24 | DG-AGENT-SESSION-CHECKPOINT | `agent_session_checkpoint` kind；promotion 仍為第二道人工閘 | active | `decisions/DG_AGENT_SESSION_CHECKPOINT_DECISION.md` |
| 2026-08-24 | DG-METRICS-CONTRACT v1 | metrics-v1 檔案契約、`run_metrics` | active | `decisions/DG_METRICS_CONTRACT_DECISION.md` |
| 2026-08-24 | DG-PRODUCT-PLAN-CORRECTIONS v1 | Manual selection 為完成品要求、metrics-v1 一級契約、Workspace 唯一主介面 | active | `archive/FULL_PLATFORM_SECOND_PASS_PLAN.md` |
| 2026-08-25 | DG-EXPERIMENT-V1 EX-1…EX-7 | `experiment_create_v2`：一 matrix 一 approval | active | `decisions/DG_EXPERIMENT_V1_DECISION.md` |
| 2026-08-25 | DG-PERSONAL-PILOT-v1 D1 clarification | pilot 證據可立 `deployed=yes`（personal-pilot only） | active | — |
| 2026-08-25 | DG-UI-UNIFICATION v1 U1–U8 | 單一中文 Workspace，legacy UI 退役 | closed（已完成） | — |
| 2026-08-26 | DG-INFRA-DIRECT-ACTIONS v1 | server add／update／disable 直接執行；delete 仍核准 | active（INV-APPROVAL-1 例外） | — |
| 2026-08-26 | DG-ASSISTANT-CLAUDE-TURN v1 | runner 零工具 `claude -p` 聊天回合；API key UI 直接執行例外 | superseded by DG-AGENT-RUNTIME-V3（Phase 1b 已完成 2026-08-31：runner `claude -p` 助手腦移除，`/ws` 降為 vLLM→規則式；API key 例外保留） | — |
| 2026-08-26 | DG-DEV-OPERATOR-DIRECT v1＋排除條款 | 開發階段 dev-operator 直接決定建立型／唯讀型測試卡；排除刪除、底層、P-1 | active（開發階段限定） | — |
| 2026-08-30 | **DG-PLATFORM-CHARTER v1** | 定位改為 Agent-native Engineering Platform；本憲章成立；INV-PLANE-1／2 新增；文件重整；硬體佔位 | active | 本檔 |
| 2026-08-30 | DG-ASSISTANT-TOOLS v1＋DG-AGENT-SESSION-V2 | 助手取得既有平台工具集（per-turn 短效 token、授權＝發話者、只掛 runner-claude 腦）；session 內證據物化、建卡工具（新增 `request_run`／`request_experiment`）、`PROJECT.md`、checkpoint 記憶；順序案一 → S-1/S-3 → S-2/S-4/S-5；pilot 旗標預設開 | 案一 active（token／bridge 重用為 SDK 的 MCP 層）；案二 superseded by DG-AGENT-RUNTIME-V3 | `decisions/DG_ASSISTANT_TOOLS_AND_AGENT_SESSION_V2_DRAFT.md` |
| 2026-08-30 | **DG-AGENT-RUNTIME-V3 v1** | Development Agent 改由 runner 上的 dispatch-agent（Claude Agent SDK）承載：只出站、A2A 語意通道、工作區權限提示（人逐條允許）、`agent_runner_enroll`／`revoke` kinds、INV-AGENT-1／2 新增、舊 tmux／`claude -p` 機制與 Codex provider Phase 1b 退役 | active（Phase 1a–1b＋Phase 2 完成 2026-08-31；runner 106 上線） | `decisions/DG_AGENT_RUNTIME_V3_DECISION.md` |
| 2026-08-30 | **DG-STUDIO-UI v1** | 新 Studio 介面（React＋TypeScript＋Vite，build 不進 git）：session 優先三欄、內嵌權限提示與核准、實驗矩陣與伺服器晶片；舊 Workspace 並存至 Phase 3 | active（Phase 1–3 完成 2026-08-31：`GET /` 已切 Studio、舊 Workspace 退役） | 同上 |
| 2026-08-31 | **DG-HARDWARE-EXECUTION v1** | 硬體工程軌契約：devices: 附掛資源（presence 封閉探測）、`action_class` 分級、`compute`/`build` 沿 `execution_plan_v2`、實體動作走新 kind `hardware_action_v2`（永不自動核准、matrix 拒絕、dev-operator 排除）、`hardware_images` 內容定址（≤256 MiB）、`hardware-receipt-v1`、known-good 例外；工具鏈三類全收（在機：ESP32）；順序 P1–P4 | active（P1 實作中） | `decisions/DG_HARDWARE_EXECUTION_DRAFT.md` |
| 2026-09-02 | **DG-CONSOLIDATION-v1** | 整頓計畫六條款：簿記 pin 可重寫（INV-TEST-2 釐清）、程式預設改 pilot 姿態（安全姿態旗標除外）、execution 鏈維持關（RB-LAUNCH-001）、帳本欄位改制（`deployed`→`Pilot`）、四項退役面刪除、補充紀錄模板＋CHANGELOG 退役 | active | — |
| 2026-09-02 | **DG-SINGLE-OPERATOR-CONFIRM v1** | 單人姿態下 Studio「確認並執行」單鍵：封閉 kind 清單、人工 v2 decision（digest 綁定、完整稽核）；promote／刪除／伺服器底層／runner·node·service／硬體實體動作永不適用；INV-APPROVAL-4 加註、白名單不變 | active（實作於整頓 U7） | — |

### 7.2 保留閘名（Named gates without a draft）——動到對應範圍前必須先裁定

| 閘 | 守的是什麼 |
|---|---|
| `DG-GITHUB-PUBLISH` | 真實 GitHub 發布 adapter／憑證／egress（現行 interface + fake only） |
| `DG-GPU-SCHED` | GPU 槽位切分、多任務同機、配額排程（現行一機一件） |
| `DG-JOB-STATE` | 任務狀態機新狀態值或新轉移（INV-STATE-4） |
| `DG-ATTEMPT-RECOVERY` | attempt 層自動回復／重派語意 |
| `DG-AUTHZ-ENFORCE` | 授權 enforcement 在真實環境的啟用（personal pilot 現以 enforce 姿態運行——DG-CONSOLIDATION-v1 C-4 如實記錄，personal-pilot only；本閘適用於任何非 pilot 環境） |
| `DG-SSH-HOSTKEY` | host-key 驗證政策變更（INV-SSH-8） |
| `DG-OPTIMIZATION-QUOTA` | 限額式自動優化迴圈（PROD-5） |

### 7.3 待裁定（Pending）

| 閘 | 狀態 | 內容 |
|---|---|---|
| `DG-NODE-CANARY` | draft，未核准 | Node Agent 實機逐台啟用的證據門檻（`decisions/DG_NODE_CANARY_DECISION.md`；runbook：`runbooks/PHASE5_NODE_CANARY_RUNBOOK.md`） |
| `DG-OPS-SLO` | draft，未核准 | RPO／RTO／retention 數值與營運 SLO（`decisions/DG_OPS_SLO_DECISION.md`） |

---

## 8. Capability Status & Evidence｜能力現況與證據

本憲章不維護 implemented／not-implemented 對照表（那種快照必然過期）。在依賴、擴充或否定任何平台能力之前，一律照這個順序查證：

1. **實作真相**：current code + tests——找到實際的 module、route、schema、`VALID_APPROVAL_KINDS` 條目與對應測試；找不到就是不存在。
2. **核准真相**：`docs/DECISIONS.md`——能力必須能追溯到一條具名裁定（§7），裁定同時界定它的 bounded scope 與明文 non-goals；
   沒有具名裁定的能力不得實作、不得預先發明語意。
3. **能力現況**：`docs/CAPABILITY_LEDGER.md`——`Implemented` ≠ `Default` ≠ `Pilot` ≠ `Canary`（DG-CONSOLIDATION-v1 C-4 欄位）；
   personal-pilot 證據只能立 `Pilot=on`，永不立 `Canary`；production-ready 不是欄位，只能由具名裁定宣告。
4. **路線圖只是方向**：`docs/product/ROADMAP.md` 提到 ≠ 已核准 ≠ 已實作；照路線圖直接寫程式就是本節要防止的失敗。

證據規則（結果分析與 canary 同適用）：保留原始 log 與 artifact；每個結論指向 job／run、artifact 路徑、metric 或 log；區分觀測事實、
計算值與 LLM 推論；缺失或不可讀的輸出是 unknown；建議需核准才能 rerun 或 mutation；結果收集失敗與任務執行狀態分開記錄。
歷史證據檔在 `docs/evidence/`（commit-pinned），格式範例在 `docs/examples/`。

---

## 9. Document Map｜文件地圖

| 路徑 | 內容 | 權威性 |
|---|---|---|
| `docs/PLATFORM_CHARTER.md` | 本憲章：定位、範圍、架構、不變式、裁定登錄 | 安全真相（與 DECISIONS.md 並列） |
| `docs/DECISIONS.md` | 逐條、append-only 裁定時間紀錄 | 安全真相（provenance） |
| `docs/decisions/` | 決策 packet（DG_*.md）、草稿、2026-07-16 審閱包 | provenance，不是權威文字 |
| `docs/CAPABILITY_LEDGER.md` | 每項能力的 Ruling／Implemented／Default／Pilot／Canary＋一行證據 | 能力現況 |
| `docs/product/ROADMAP.md` | 產品路線圖與最終完成品形狀（含硬體工程軌） | 方向 |
| `docs/reference/` | 技術參考：SETTINGS、MIGRATIONS、PACKAGING、API_ROUTING、REPOSITORIES、AUDIT_LEDGER、COVERAGE／TYPECHECK baseline | 實作說明（以程式碼為準） |
| `docs/runbooks/` | 操作程序：Node canary、operations、WP-2D canary、OIDC 非 production 驗收、runner agent 安裝 | 程序 |
| `docs/evidence/`、`docs/examples/` | commit-pinned 證據與格式範例 | 證據 |
| `docs/archive/` | 歷史計畫／進度／狀態快照（附 `superseded_by:`） | 歷史，不是現況 |
| `README.md` | 使用者面定位、快速開始、文件地圖 | 使用者可見行為需與其同步 |
| `CLAUDE.md`＋`.claude/skills/` | AI 協作開發規範、skill 路由、實作層參考（architecture／glossary／result-analysis） | 開發規範 |
| `AGENTS.md`、`CONTRIBUTING.md`、`SECURITY.md` | 非 Claude agent 入口、貢獻規則、安全政策 | 規範（`CHANGELOG.md` 於 DG-CONSOLIDATION-v1 C-6 退役；變更以裁定紀錄＋帳本為準） |
