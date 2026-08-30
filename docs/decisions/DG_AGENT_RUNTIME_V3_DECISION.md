# DG-AGENT-RUNTIME-V3 v1 ＋ DG-STUDIO-UI v1 — 決策 packet

> 狀態：**已核准（2026-08-30；紀錄於 `docs/DECISIONS.md`）**。本檔是核准當下的計畫全文（provenance）；
> 憲章 `docs/PLATFORM_CHARTER.md` §2.3／§4／§6／§7 已依「憲章衝突核對」一節改寫。實作進度以程式碼、tests 與
> `docs/CAPABILITY_LEDGER.md` 為準。

---


## Context

使用者的新方向（2026-08-30 規劃問答裁定，共八題）：

1. **不再用 `claude -p` 當 agent 執行引擎，改用 Claude Agent SDK**，目標是「在網頁上做到跟 Claude Code CLI 一樣完整」。
2. **Agent 跑在每台 runner／worker 上的 agent 服務（A2A 風格）**，Server A 只做編排。
3. **認證先用現有 Claude 訂閱**（`claude setup-token` 的 OAuth token；官方文件確認 Agent SDK 支援 Pro/Max 訂閱 token，
   限制只有不能開 Remote Control／claude.ai connectors，與本案無關）。
4. **前端改掉「不引框架、不 build」非目標，用更人性化的介面改寫**。
5. 痛點（全選）：面板太多找不到入口；核准卡要跳頁、流程被打斷；看不到 agent 即時在做什麼；建實驗、挑伺服器太繁瑣。
6. **Bash 完全 CLI 同步**：工作區內任何指令都可以在你即時允許後執行（我已說明這與「不開 general-purpose shell」衝突最大，使用者仍選此項——依此執行，並把「人逐條允許」寫成硬規則）。
7. **新增 INV-AGENT-1／INV-AGENT-2** 兩條不變式。
8. **A2A 只在內部（Server A ⇄ runner）用其語意，對外 Agent Card 留到選配階段**。
9. **Phase 1 就退役舊的 tmux＋`claude -p` 機制**（engineering task、AgentSession V1、助手回合）。

目標領域：AI 模型、MCU、FPGA 開發；集中管理與調度；多台伺服器跑不同參數。

**官方文件確認的 SDK 事實**（code.claude.com/docs/en/agent-sdk）：SDK 內建 Claude Code 二進位（不需另裝 CLI）；工具只作用在
**執行 SDK 那台機器的本機檔案系統**（沒有 remote-cwd）；`ClaudeSDKClient` 多回合、可中斷；`include_partial_messages` 逐 token
串流；`can_use_tool` 權限回呼（allow／deny／改寫 input）；`permission_mode`、`allowed_tools`／`disallowed_tools`、`mcp_servers`
（stdio 或 in-process）、`hooks`、`resume`／`fork_session`、`max_turns`、`max_budget_usd`、`setting_sources=[]`（不讀使用者設定）。
**A2A 沒有官方支援**（Linux Foundation 協定：Agent Card＋task／message／artifact＋串流，可自行實作）。

**現況（實查）**：所有 Development Agent 回合都是 SSH 到 runner 跑 `claude -p`／`codex exec`（tmux＋哨兵，transcript 只在
runner，2 秒輪詢 tail）；Server A 本機沒有跑 agent 的路徑；UI 是 7,387 行 vanilla `workspace.js`＋12 分頁，**沒有實驗矩陣 UI**
（`experiments_v2` API 前端沒接）；核准只有一個「核准區」頁面。剛完成的 DG-ASSISTANT-TOOLS v1（per-turn token、bridge stdio）
在 v3 直接重用為 SDK session 的 MCP 工具層。

## 需要的裁定（核准本計畫＝裁定；Phase 0 記入 DECISIONS.md 並改憲章）

| # | 裁定 | 性質 |
|---|---|---|
| R1 | **DG-AGENT-RUNTIME-V3**：新的 validation mechanism——runner 上的 `dispatch-agent` 服務以 Claude Agent SDK 承載 AgentSession；工作區在 runner 本機；runner **只出站**連 Server A（不開入站埠）；通道用 A2A 的 task／message／artifact／串流語意（`input-required`＝權限提示） | 新 mechanism，具名裁定 |
| R2 | **工作區權限提示 ≠ 平台核准卡**（INV-AGENT-2）：檔案工具限工作區；Bash 的驗證 allowlist 直接跑；**其餘任何指令都彈即時提示，由 session 擁有者逐條允許後才跑**（可「本 session 一律允許此模式」）；提示與決定存 DB、稽核、逾時＝拒絕；平台級動作（run／experiment／promote／伺服器）仍只能經 MCP `request_*` 建核准卡；agent 永無 approve 工具 | 明文界定；INV-APPROVAL-1／4 不變 |
| R3 | **runner agent 登錄**：新 approval kind `agent_runner_enroll`／`agent_runner_revoke`（比照 `node_enroll`：人核准、可個別撤銷的 credential，只存在 runner） | 新 kind |
| R4 | **憲章 §2.3 非目標修訂**：前端改為「Studio 用 TypeScript＋框架＋build，產物不進 git」；「不做 A2A」改為「內部 A2A 語意，對外選配」；「不開 general-purpose shell」改為「平台工具集永無 shell；工作區 Bash 由人逐條允許」 | 非目標變更 |
| R5 | **Claude 認證在 runner**（`CLAUDE_CODE_OAUTH_TOKEN`）；Server A 永不持有 | 部署姿態 |
| R6 | **舊機制 Phase 1 退役**：`app/agent_session_turns.py` tmux 回合、`app/assistant_turns.py`、`app/coding_agents.py` 的 `codex-exec-v1`／`claude-code-v1` job-backed 回合一併退役；**Codex provider 隨之退役**（registry 留歷史註記；日後要接回需在 runner agent 上另案實作） | 範圍調整——請注意 Codex 退役 |
| R7 | 案二 **DG-AGENT-SESSION-V2** 由 v3 取代：證據／PROJECT.md 變成工作區 context 檔＋MCP 即時查詢；建卡工具、記憶（SDK `resume`）、UI 併入 Phase 1／2 | 範圍調整 |

## 目標架構

```text
Browser（Studio SPA）──HTTPS/WS──▶ Server A（FastAPI：真相、核准、稽核、排程；agent gateway）
                                      ▲  出站 WebSocket（runner 發起；A2A JSON-RPC 語意：message/stream、tasks/cancel、
                                      │  TaskStatusUpdate／TaskArtifactUpdate；input-required＝權限提示）
   runner 106 / … ────────────────────┘  dispatch-agent：Claude Agent SDK（ClaudeSDKClient）、本機 worktree、can_use_tool、
                                         MCP dispatch bridge（stdio，session token）、Agent Card（GPU／工具鏈／板子）
```

- **dispatch-agent（新 Python 套件 `dispatch_agent/`，獨立 wheel，比照 `agent/`）**：非 root；登錄 credential 對 Server A 建出站
  WS（重連、心跳）；每個 session 一個 `ClaudeSDKClient`（`cwd`＝worktree、`setting_sources=[]`、`permission_mode="default"`、
  `allowed_tools`＝工作區檔案工具＋驗證 allowlist、`mcp_servers={"dispatch": stdio bridge}`、`max_turns`、`include_partial_messages`）；
  **`can_use_tool`**：工作區內檔案工具允許；allowlist Bash 允許；其餘 → 上送 `input-required` 給 Server A → 使用者在 Studio 決定 →
  allow／deny 回來；SDK 子行程 env 只含 `HOME/PATH/LANG/CLAUDE_CODE_OAUTH_TOKEN`（登錄 credential 永不進 env）；事件（text
  delta、tool_use、tool_result、permission、result 含 cost）即時上送；Agent Card 由封閉探測產生。無工作區的「平台助手」session
  ＝空目錄 cwd、`allowed_tools=[]`、只掛 MCP（取代 `claude -p` 助手回合）。
- **Server A gateway**（`app/agent_gateway.py`＋`dispatch_center/api/routers/studio_v2.py`）：`agent_runners` 註冊表；
  `agent_sessions` 加 `provider_id='claude-agent-sdk'`、`runner_id`、`cost_usd`；**`agent_session_events`**（append-only，SQLite
  為真相）；`agent_permission_requests`（pending／allowed／denied／expired、decider、稽核）；瀏覽器 WS 轉播；cancel／interrupt；
  MCP token 改 session 期、每回合輪換。
- **不變**：ExecutionPlan／experiment／promotion／approval／scheduler／SSH 派工；GPU 與硬體工作永遠經 Compute Plane（INV-PLANE-2）。

## 新不變式草案（Phase 0 寫進憲章 §6，測試釘住）

**INV-AGENT-1 runner agent 身分與連線** — Statement：`dispatch-agent` 是非 root 常駐服務，只發起出站已驗證連線到 Server A，
工作機不開任何入站控制埠；每台一組可個別撤銷的登錄 credential（`agent_runner_enroll` 核准後一次性顯示、只存 runner）；心跳
過期＝`unknown` 不是 failed（INV-SSH-7 同構）；SDK 子行程環境只含必要變數，登錄 credential 與平台 token 永不進 SDK env；Claude
憑證（`CLAUDE_CODE_OAUTH_TOKEN`）只在 runner；agent 只能建工作區在 dispatch-agent 設定的 root 之下。Forbidden：入站 listener、
共享 credential、Server A 持有 Claude 憑證、模型可讀登錄 credential。Verification：`tests/test_dispatch_agent_*.py`、gateway 拒絕
未登錄 runner 的測試、env 白名單測試。

**INV-AGENT-2 工作區權限提示** — Statement：Development Agent 的 Bash 只在工作區 cwd 內啟動；驗證 allowlist 內直接執行；
**其餘每一條指令都必須經 session 擁有者在瀏覽器即時允許才執行**（提示顯示完整指令；「本 session 一律允許」只對相同指令模式有效、
session 關閉即失效）；提示與決定持久化於 `agent_permission_requests` 並稽核；逾時＝拒絕；提示永遠不能決定任何 `approvals` 卡；
平台級動作只能經 MCP `request_*` 建卡；`TOOLS`／MCP 工具集永無 approve／shell。**殘餘風險（明寫）**：被允許的指令以 runner
使用者身分執行，能做到該使用者能做的事——安全靠人逐條看，不靠黑名單。Forbidden：自動允許非 allowlist 指令；把提示做成核准卡的
替代品；agent 自行改 allowlist。Verification：`can_use_tool` 決策矩陣測試、逾時拒絕測試、`test_agent_tools` forbidden_names。

## 憲章衝突核對（稽核）與改寫

| # | 憲章條文（行） | 衝突 | 改寫提案（Phase 0） |
|---|---|---|---|
| C1 | §2.3 L90「不做 multi-agent swarm／A2A」 | v3 用 A2A 語意 | 「不做開放式 swarm；A2A 語意用於 Server A ⇄ runner agent（只出站）與選配的對外 Agent Card；外部 task 只能成為提案卡」 |
| C2 | §2.3 L91「前端不引框架、不引 build step」 | Studio | 「Studio 用 TypeScript＋框架＋build；產物不進 git、CI 與部署時建置；不載入外部 URL」 |
| C3 | §4.4 L220「工作機連不回 Server A」 | runner agent 出站 | 「工作機可持登錄 credential 出站連回 Server A（Node Agent、runner agent 都只出站）；互不相通；跨機資料仍 Server A 中轉」 |
| C4 | §4.5 L232「執行通道只有兩條」 | 新 Development 驗證通道 | 「Compute 執行通道兩條不變；Development 驗證通道＝runner agent（SDK，工作區內），不是 Compute 執行」 |
| C5 | INV-PLANE-2 Enforcement L284「無 Bash／shell 工具給 agent；runner turn `--allowedTools`」 | SDK Bash 人逐條允許 | 「SDK `allowed_tools`＋`can_use_tool` 封閉；Bash 依 INV-AGENT-2（allowlist 直接、其餘人逐條允許）；仍不得從工作區啟動訓練／部署／燒錄」 |
| C6 | §2.3 L86–87、§4.3 L193「永無 shell 工具」「不開 general-purpose shell」 | 使用者選完全 CLI 同步 | 「**平台工具集**永無 approve／shell；Development Agent 的工作區 Bash 由人逐條允許（INV-AGENT-2）——不是平台自動開放的 remote shell，而是人在迴圈的工作區 shell」 |
| C7 | §4.4 L222 只綁私網 | 不衝突 | 補註 runner gateway 掛同一私網綁定、runner 經既有 Funnel HTTPS 連入 |
| C8 | INV-NODE-1 只講 Node Agent | 第二種常駐服務 | 新增 INV-AGENT-1 |
| C9 | §4.3「不得取得 credential」 | SDK env 需 Claude token | INV-AGENT-1 env 白名單＋殘餘風險明寫；`~/.claude`、credential 檔在工作區外且 0600 |
| C10 | §4.3 L193「worktree 只能由 dispatch 端建立」 | 不衝突 | 「dispatch 端」明寫含 dispatch-agent 服務；模型永不自選路徑 |
| C11 | §7 登錄的 DG-AGENT-SESSION-V1 D2、DG-ASSISTANT-CLAUDE-TURN、DG-CLAUDE-ADAPTER C-1…C-6、DG-AGENT-SESSION-V2；INV-PLANE-2 Verification 引用的 tmux pins | Phase 1 退役 | 狀態改 superseded by DG-AGENT-RUNTIME-V3；Verification 改引 runner agent 測試；退役的 pinned tests 依 U8 方式逐一盤點等價保護 |
| C12 | INV-STATE-1 未含「browser／WS／記憶體不是 Development Plane 真相」 | 補漏 | Statement 加 `agent_session_events`／`agent_permission_requests` 為持久真相；runner transcript 為分散式證據 |
| C13 | INV-APPROVAL-1／4 | 提示不是 approval | INV-AGENT-2 明寫；auto-approve 白名單不動 |

## Studio 介面

技術：**React 18＋TypeScript＋Vite＋Tailwind＋shadcn/ui**，`studio/` 目錄；build 到 `static/studio/`（gitignore）；FastAPI 掛
`/studio`；舊 Workspace 留在 `/` 直到 Phase 3 切換。diff 用 `react-diff-view`；TanStack Query＋單一 WS 事件流；Vitest＋Playwright
煙霧（對 TestClient app）。

```text
左側欄（5 個入口）：專案 ｜ 實驗與 Run ｜ 伺服器與硬體 ｜ 核准匣（badge）｜ 設定
專案頁 ＝ Session 優先三欄：
  左：專案／session 清單、版本、workspace 檔案樹
  中：Agent 對話——逐 token 串流；工具卡（展開 input／output）；diff 內嵌；權限提示內嵌（允許／拒絕／本 session 一律允許）；
      composer 支援 slash（/run /experiment /promote /diff /tests /servers）與 ⌘K
  右：Changes ｜ Runs（即時）｜ Metrics ｜ 本 session 建的卡
實驗與 Run：矩陣編輯器（加軸→填值→預覽 N run）→ 伺服器多選晶片（即時容量：GPU 空閒／tags／板子）→ 一張卡→就地決定
      → run × server 即時格（狀態、metrics、log 尾巴、比較）
核准：每張卡在它出現的地方直接決定；核准匣只是總覽
伺服器與硬體：每台一張卡（在線、GPU、agent 已連線、工具鏈、板子、預檢一鍵）
```

## 分階段（每階段拆 packet；全綠→commit→部署 pilot；階段末更新帳本）

### Phase 0 — 裁定與文件
DECISIONS.md 記 R1–R7；憲章：§2.3／§4.3／§4.4／§4.5 依 C1–C10 改寫、§6 新增 INV-AGENT-1／2、INV-PLANE-2／INV-STATE-1 修訂、
§7 登錄 DG-AGENT-RUNTIME-V3 與 DG-STUDIO-UI、C11 狀態更新；ROADMAP M2 改寫；
`docs/decisions/DG_AGENT_RUNTIME_V3_DECISION.md`（本計畫）；`docs/runbooks/RUNNER_AGENT_SETUP.md`。

### Phase 1a — runner agent＋gateway＋Studio 骨架（垂直切片）
- `dispatch_agent/`：config、出站 WS client、Agent Card 探測、`SessionHost`（`ClaudeSDKClient` 包裝：start／message／
  interrupt／close、事件序列化）、`permissions.py`（INV-AGENT-2 決策矩陣，純函式）、`workspace.py`（mirror＋worktree，本機
  subprocess allowlist；沿用 `app/agent_session_turns.py` 的純函式命令 builder 與 secret-grep／bundle 邏輯）、`--check`。
  測試：假 SDK client（duck-typed 事件序列）、假 WS。
- Server A：`agent_runner_enroll`／`revoke` kinds；migration 19（`agent_runners`、`agent_session_events`、
  `agent_permission_requests`、`agent_sessions` 新欄位）；gateway（runner WS、事件持久化、session 狀態機、逾時）；REST／WS
  給瀏覽器；MCP token 改 session 期；`authorization_catalog` 分類；openapi 快照。
- Studio 骨架：Vite 專案、`/studio` 掛載、OIDC cookie 登入、專案頁中欄（串流對話、工具卡、權限提示、diff）、核准卡內嵌決定；
  `scripts/frontend_smoke.py` 擴到 build 產物；CI 加 `npm ci && npm run build`＋Vitest；`updating-pilot-site` 加 build。
- pilot：106 裝 `dispatch-agent` wheel＋`claude setup-token`；登錄核准；expdemo 走完一回合（改檔→diff→跑 pytest（allowlist）→
  一條非 allowlist 指令→提示→允許→執行→checkpoint→promote）。

### Phase 1b — 舊機制退役（R6）
engineering task 改為「帶指令的 SDK session」（沿 `engineering_tasks` 紀錄與 promote 鏈）；助手回合改平台助手 session；移除
`app/agent_session_turns.py` 的 tmux 路徑、`app/assistant_turns.py`、`coding_agents.py` exec providers（Codex 退役、registry 留註記）；
逐一盤點退役 pinned tests 的等價保護（U8 模式）；帳本、憲章 §7、README 同步。

### Phase 2 — Studio 專案頁完整（左／右欄、slash、⌘K、`resume`、成本、多 session）。
### Phase 3 — 實驗與 Run UX（矩陣編輯器接 `experiment_create_v2`、伺服器晶片、run × server 格、內嵌核准；「三張卡」收成一張執行設定卡）
→ 其餘面板遷入 → `/` 切到 Studio → 舊 Workspace 退役。
### Phase 4 — 硬體軌（DG-HARDWARE-EXECUTION，另案裁定）：Agent Card 加板子／工具鏈、燒錄卡。
### Phase 5（選配）— 對外 A2A：Server A 代 runner 發佈 Agent Card；外部 task 只能成為提案卡。

## 驗證（Phase 1 完成的定義）
1. `tests/test_dispatch_agent_*.py`（假 SDK／假 WS：決策矩陣、env 白名單、事件序列化、重連）、`tests/test_agent_gateway.py`、
   `tests/test_studio_v2_api.py`、`test_authorization_coverage`（路由數更新）、openapi 快照、Vitest、`static_checks.sh`。
2. pilot 端到端：`dispatch-agent --check` → 登錄核准 → Studio 開 session →「把 README 加一段」→ 串流、Edit 工具卡、diff →
   「跑 pytest」直接跑（allowlist）→「pip install rich」→ 提示 → 允許 → 執行 → 拒絕一次看 agent 收到拒絕 →「幫我建一個實驗」→
   MCP `request_experiment` 建卡、卡內嵌可決定；agent 永遠不能自己核准。
3. 恢復：Server A 重啟後事件仍在、runner 重連續接；runner 離線 → session `unknown`（不判失敗）。

## 明確不做（v3 Phase 1–3）
Server A 本機跑 agent；runner 開入站埠；自動允許非 allowlist 指令；agent 自核准；multi-agent swarm；build 產物進 git；硬體實作
（另案）；外部 A2A 進站（Phase 5 選配）；Codex 重新接入（另案）。
