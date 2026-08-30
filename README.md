# Dispatch Center

> **Agent-native Engineering Platform**——在瀏覽器裡提供一個完整的 **AI Engineering
> Workspace**：AI Engineer 理解工程專案、讀寫程式碼、執行測試、訓練模型，並往
> FPGA synthesis／bitstream、MCU build／flash 與硬體驗證延伸；平台負責權限與核准、
> 環境隔離、運算與硬體資源配置、跨伺服器執行、版本與 Artifact 管理、Evidence 蒐集、
> 狀態恢復與完整 Audit。**AI 負責思考與提案，平台負責治理與執行，人負責決定。**

## What it does｜這個平台做什麼

日常訓練模型與做硬體的痛點：ssh 到每台機器看誰有空、scp 資料、tmux 開任務、
手動找 log、忘記哪個結果對應哪個 commit、燒錄板子前不確定是哪個版本。
Dispatch Center 把這整條工作流搬進一個網站，而且每一步都留下可追蹤、可驗證的證據：

```text
匯入/建立 Project → AI Engineer 在隔離工作區改碼、跑測試 → Review Diff
  → Commit → promote 成 ProjectVersion → 派工到多台 Server（GPU；未來含 FPGA/MCU 板）
  → 集中看 Runs / Metrics / Logs / Artifacts → 有根據的分析 → 下一輪提案（仍經核准）
```

- **專案中心**：Project 統一代表 repo、版本、環境、資料集綁定、對話、實驗與結果；
  Server 只是執行資源。
- **AI 協作開發**：與 Development Agent（Claude Code、Codex）長期對話，讓它在
  dispatch 建立的隔離 worktree 讀檔、改碼、跑 test/lint，產出可審查的 diff——
  **它永遠不能自己核准或執行任何東西**。
- **安全派工**：每個會改變系統狀態的動作（派工、停止、匯入專案、promote 版本、
  刪除機器…）都是一張核准卡，由人決定；危險指令在建立請求當下就被拒絕。
- **可收斂的執行**：SSH + tmux + exit_code 哨兵協議派工；Server A 重啟、網路斷線、
  worker 失聯都能由 reconciliation 收斂，不會誤判任務狀態（連不上 ≠ 失敗）。
- **有根據的結果**：任務結束自動回收 results；workload 寫 `results/{job_id}/metrics.json`
  （metrics-v1 契約）即被解析入庫，缺檔是 unknown、不是失敗；所有結論都能追溯到證據。
- **硬體工程軌（路線圖）**：synthesis／build／flash／HIL 驗證都被定義為 Compute Plane
  的受治理執行——永不從 agent 工作區發起、實體動作永不自動核准；資源模型與工作類型
  待 `DG-HARDWARE-EXECUTION` 裁定，目前**零實作**。

## Architecture｜架構一覽

```text
Browser（單一中文 Workspace：static/workspace.html + workspace.js／workspace-features.js）
  │  OIDC 認證、核准卡、AI 工程分頁（AgentSession）、平台助手
Server A ── FastAPI 單體 + SQLite（jobqueue.db 是唯一持久真相）
  │        scheduler / monitor / reconcile 背景迴圈
  │        approvals：所有 material 寫入的唯一閘門
  ├─ SSH/SFTP ──→ Worker 1..N（tmux + exit_code 哨兵；一機一件；未來附掛硬體）
  ├─ SSH/SFTP ──→ Runner（Development Agent 的隔離 worktree；助手的零工具 claude 回合）
  ├─ rsync ←──── results/{job_id}/ 回收 + metrics 解析
  └─ LLM 選配層（runner Claude / Anthropic API / 本地 vLLM / MCP bridge）
       ——只能查詢與「建 pending 卡」，零執行權，缺席不影響本體
```

兩個 Plane、一個交會點：**Development Plane**（該存在什麼程式碼：onboarding、agent
改碼、diff、ProjectVersion）與 **Compute Plane**（跑什麼、在哪跑、用什麼資料／硬體：
ExecutionPlan、排程、Run、結果）只在「人工核准 promote 的 ProjectVersion」交會——
未 promote 的程式碼永遠進不了執行面；在工作區跑測試永遠不等於在工作機跑指令。

## Safety model｜安全模型（不變式摘要）

1. 所有 material 寫入走核准流；自動核准白名單只有 `enqueue`/`stop`。
2. 任何 agent（LLM 工具、MCP、Development Agent）永遠沒有 approve/reject/自由 shell
   工具，永遠不能核准自己的請求。
3. Development Plane 的產出只經人工 promotion 進入執行面；訓練、部署、燒錄、電源等
   永不從工作區發起。
4. 使用者指令原文只經 SFTP 落地，永不進 shell 字串插值；任務成敗只看哨兵 exit_code；
   worker 連不上＝跳過不判定。
5. 憑證永不進 DB、稽核、diff、transcript 或 prompt。

完整不變式（`INV-*`）、架構模型與裁定登錄見 `docs/PLATFORM_CHARTER.md`；逐條裁定紀錄見
`docs/DECISIONS.md`。

## Quick start｜快速開始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp .env.example .env          # 設定 servers.yaml、認證、旗標
.venv/bin/python -m pytest tests/ -q     # 全套離線測試（無真機需求）
.venv/bin/uvicorn app.main:app --port 8000
```

worker 機只需要 SSH 帳號與 tmux，不安裝任何 agent。實際部署／換版
流程見 `.claude/skills/updating-pilot-site/SKILL.md`（systemd user
unit + Tailscale）。

> **Node Agent 現況揭露**（DG-NODE-V2）：`agent/__main__.py` 與
> systemd user-unit template 已存在，`python -m agent --check` 可離線
> 驗證機器設定與非 root 身分——但 **daemon 可執行不等於 Node 可用**：
> `NODE_AGENT_V1_ENABLED` 已棄用、兩個替代旗標維持關閉，實機逐台啟用
> 必須先取得 `DG-NODE-CANARY` 證據；受支援的 production worker 仍是
> agentless SSH/SFTP/tmux（INV-SSH-1）。

## Status｜目前狀態

- **Personal pilot 運行中**：單人全程瀏覽器 import → agent 改碼 → promote → 派工
  → 結果回收 → metrics → experiment matrix 已跑通。
- **單一中文 Workspace**：`GET /` 一律回傳 `static/workspace.html`；`API_V2_ENABLED`
  關閉時改回內嵌提示頁，不再有第二套介面。
- 大量 v2 能力（typed revisions、ExecutionPlan v2、dataset governance、RBAC、
  Node Agent…）已實作但 **default-off**，逐步啟用中——能力現況以
  `docs/CAPABILITY_LEDGER.md` 為準（`implemented` ≠ `enabled` ≠ `deployed` ≠
  `production-ready`）。
- **硬體工程軌**：定位與邊界已定，資源模型／工作類型／artifact／證據尚待
  `DG-HARDWARE-EXECUTION` 裁定，無任何實作。
- **v3 方向已裁定（2026-08-30）**：Development Agent 改由每台 runner 的 `dispatch-agent` 服務以 Claude Agent SDK 承載
  （只出站、權限提示由人逐條允許），新 Studio 介面（TypeScript）開發中；舊 tmux／`claude -p` 機制與 Codex provider 將於
  Phase 1b 退役。詳見 `docs/decisions/DG_AGENT_RUNTIME_V3_DECISION.md`。

## Documents｜文件地圖（真相順序）

| 順位 | 文件 | 內容 |
|---|---|---|
| 1 | `docs/PLATFORM_CHARTER.md` + `docs/DECISIONS.md` | 安全真相：定位、架構、不變式、裁定登錄；逐條裁定紀錄 |
| 2 | 程式碼與 `tests/` | 實作真相（4,500+ 離線測試） |
| 3 | `docs/CAPABILITY_LEDGER.md` | 能力現況帳本 |
| 4 | `docs/product/ROADMAP.md` | 產品路線圖（未來方向，含硬體工程軌） |
| — | `docs/decisions/` | 裁定 packet（provenance） |
| — | `docs/reference/`、`docs/runbooks/` | 技術參考（設定、遷移、封裝、API、稽核帳本）與操作程序 |
| — | `docs/archive/` | 歷史計畫／進度／驗收手冊（含原階段 1–11 逐步驗收手冊） |
| — | `CLAUDE.md` + `.claude/skills/` | AI 協作開發規範 |
