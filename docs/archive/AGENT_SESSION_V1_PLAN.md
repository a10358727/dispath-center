# AgentSession V1 實作計畫（Web 版 Claude Code 開發體驗）

> archived: 2026-08-30 · superseded_by: `docs/PLATFORM_CHARTER.md`（定位／架構／不變式）、`docs/CAPABILITY_LEDGER.md`（能力現況）、`docs/DECISIONS.md`（裁定）· 本檔為歷史證據，不是現況。

**文件類型：實作計畫（bounded implementation packets P1–P4）**
**日期：2026-08-24**
**授權來源：`docs/DECISIONS.md` → DG-AGENT-SESSION-V1（D1–D6 + E-1…E-3）**
**部署模型：non-production personal pilot（承 DG-PERSONAL-PILOT-v1 D1 姿態）**

> 本文件是計畫，不是實作，也不授權任何 invariant 變更。安全真相在
> canonical invariants 與 `docs/DECISIONS.md`。V1 旗標
> `AGENT_SESSION_V1_ENABLED=false` 預設關閉。

---

## 1. 產品目標

把 Claude Code CLI 的 development-agent 體驗搬進 Dispatch Center Web UI。
使用者在瀏覽器中：開 project → 開 Development Agent session → 與 Claude
持續對話 → Claude 自行 read/grep/edit workspace、跑 bounded validation →
web 即時顯示 tool calls／輸出／diff → 迭代修改 → review diff →
promote ProjectVersion。**不是 chatbot，是 Web 版 Claude Code。**

**最終演進目標（非 V1 範圍，架構必須承接）**：
Develop → Validate → Promote → Run → Collect Evidence →
Analyze with Skills → Recommend Optimization → Develop Next Iteration。

## 2. 架構

```
Browser（AI Engineer 分頁）
  │ 訊息／輪詢 transcript
Server A ── agent_sessions（SQLite 真相）+ ai_conversation_messages（沿用 2a）
  │ 純函式組 turn script → SFTP → 每 turn 一個 tmux + exit_code sentinel
Runner ── claude -p（Pro/Max 訂閱登入；--resume CLI session）
  ├─ 工作目錄 = ai-session-{id} worktree（dispatch 建立、隔離）
  ├─ dev-local 工具：Read/Grep/Glob/Edit/Write/git status·diff/
  │   bounded validation（Bash allowlist）── 全部限定 workspace
  ├─ 零 platform 工具、零 credential；network 依既有慣例
  └─ stream-json transcript → 檔案 → Server A live tail → web 渲染
  │
使用者：review diff → checkpoint（Server A 跑既有 path-policy+bundle）
  → engineering_task_promote 人工核准 → ProjectVersion（機制零變更）
```

Session model：**persistent conversation（2a 表）+ persistent workspace
（worktree 跨 turn 存續）+ per-turn Claude process**（不做常駐行程、
不自製 resume 協議；CLI `--resume` 失效即 fail-closed 降級並回報）。

## 3. 權限邊界

**Development-local（Claude 可直接用，限 session worktree 內）**：
Read/Grep/Glob/Edit/Write；git status/diff/log/add/commit（僅該
worktree/branch）；Bash 僅 validation allowlist；workspace venv 裝依賴。
**Deny**：workspace 外路徑、git push/remote、任意 Bash、sudo、
`~/.ssh`／`~/.claude` 憑證區、（預設）網路。

**Server-A-mediated（Claude 永遠沒有）**：approve/reject；promotion
（checkpoint→bundle→核准全在 Server A，Claude 零參與）；
ExecutionPlan/Run/dispatch；server config／dataset permission／deploy；
任意 SSH；platform credentials。V1 連「建 pending 卡」工具都沒有
（比 INV-LLM-1 上限更緊；開放屬另案裁定）。

**演進護欄（E-1…E-3，已裁定）**：Skill 只以檔案物化進 workspace、永不
改 launcher 權限（Skill ≠ permission）；Run evidence 未來一律由 Server A
物化成唯讀檔案進 workspace，runner 永不持 platform 憑證、永不 SSH 至
Compute node；`agent_sessions` 保持 task-neutral（V1 即帶 `provider_id`）。

## 4. 裁定摘要（權威文字在 DECISIONS.md）

D1 `agent_session_open` approval kind（一次核准=workspace+有界 turns，
永不自動核准）；D2 per-turn tmux+sentinel 通道（具名核准的新 validation
mechanism；timeout、SFTP 非插值、unreachable=降級）；D3 confinement 以
pinned CLI 設定實現、驗證不了即 BLOCKED（殘餘風險僅限 personal pilot
接受）；D4 V1 零 platform 工具；D5 turn 10 分鐘／session 200 turns／
閒置 7 天自動 close；D6 沿用 AIConversation 持久層。

## 5. 實作切片（順序執行，一次一個 coder）

| Packet | 範圍 | 驗收重點 |
|---|---|---|
| **P1** Session domain | Migration v11 `agent_sessions`（含 provider_id、one-active-per-project 約束）；`agent_session_open` request/approve（approve 零遠端副作用——workspace 由 P2 turn script 建立，coding-task 先例）；lazy 7 天過期；close endpoint；旗標；3 條路由+catalog+KIND_LABEL | migration 雙軌；request 驗證群；approve 原子建 session；INV-APPROVAL-3 過期重驗；永不自動核准釘住；計數閘 |
| **P2** Turn channel | 純函式 turn script（pinned `claude -p` + confinement 設定 + `--resume` + prompt SFTP/stdin + stream-json 落檔 + 環境憑證剝除 + 首 turn 建 worktree）；每 turn tmux `agent_turn_{sid}_{n}` + sentinel + 10 分鐘 timeout；POST messages（200 上限、串行）；sentinel 收斂→assistant message 入 conversation；GET transcript（live tail 先例）；unreachable 降級 | 指令字串+confinement 釘住；哨兵三分支；timeout/unreachable/上限/resume 失效降級；auth+catalog+計數閘 |
| **P3** Checkpoint/promote glue | GET session diff（重用既有 diff builders）；checkpoint→Server A 跑既有 path-policy 雙檢+bundle→接既有 `engineering_task_promote` 流 | path-policy fail-closed；bundle verify；promotion 零行為變更 |
| **P4** Web UX | AI Engineer 分頁升級：開 session（建卡→核准）、訊息流、tool calls/輸出即時渲染（輪詢）、diff/validation 檢視、checkpoint/promote/close 按鈕、degraded 全態；2a 聊天保留 | 前端靜態斷言；全態覆蓋；JS syntax+smoke；全套綠 |

每個 packet：Fable review diff → 全套測試單次乾淨綠 → commit（附測試
證據）→ 下一個 packet；全部完成後依 `updating-pilot-site` skill 部署。

**共同邊界**：不改 invariant/釘住測試；`agent_session_open` 外零新
kind；`TOOLS` 表不動；auto-approve 白名單不動；測試全 fake。
**BLOCKED 條件**：需要第二個新 kind、invariant 變更、confinement 無法
以 pinned 版本落實。

## 6. 明文延後（不得因此擴大 V1）

structured Run Evidence tools、metrics-v1、analysis Skills、experiment
comparison、optimization loop、agent-generated Run proposal、automatic
iteration、真 WebSocket push streaming、turn 中互動式指令核准、
多 session/專案、Codex session 對等版、worktree retention 政策。
以上未來能力**不得要求重做 AgentSession 核心架構**（§3 護欄保證）。

## 7. Rollback

關閉 `AGENT_SESSION_V1_ENABLED` 即隱藏全部介面；session 資料與
worktree 保留為證據；turn 無長駐行程可壞，哨兵收斂；promotion 機制
未被修改，不受影響。
