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
- **AI 協作開發**：與 Development Agent（runner 上以 Claude Agent SDK 承載的
  Claude Code session）長期對話，讓它在 dispatch 建立的隔離 worktree 讀檔、改碼、
  跑 test/lint，產出可審查的 diff——**它永遠不能自己核准或執行任何東西**。
- **安全派工**：每個會改變系統狀態的動作（派工、停止、匯入專案、promote 版本、
  刪除機器…）都是一張核准卡，由人決定；危險指令在建立請求當下就被拒絕。
- **可收斂的執行**：SSH + tmux + exit_code 哨兵協議派工；Server A 重啟、網路斷線、
  worker 失聯都能由 reconciliation 收斂，不會誤判任務狀態（連不上 ≠ 失敗）。
- **有根據的結果**：任務結束自動回收 results；workload 寫 `results/{job_id}/metrics.json`
  （metrics-v1 契約）即被解析入庫，缺檔是 unknown、不是失敗；所有結論都能追溯到證據。
- **硬體工程軌（進行中）**：synthesis／build／flash／HIL 驗證都被定義為 Compute Plane
  的受治理執行——永不從 agent 工作區發起、實體動作永不自動核准。契約已由
  `DG-HARDWARE-EXECUTION` v1（2026-08-31）裁定，P1（裝置資源模型＋presence 探測）已落地，
  P2–P4（build 模板、`hardware_action_v2` 燒錄、Hardware 分頁）依序進行。

## Architecture｜架構一覽

```text
Browser（Studio SPA：studio/ React＋TypeScript，build 到 static/studio/）
  │  OIDC 認證、專案 Session 對話、核准卡就地決定、實驗矩陣、稽核事件
Server A ── FastAPI 單體 + SQLite（jobqueue.db 是唯一持久真相）
  │        scheduler / monitor / reconcile 背景迴圈
  │        approvals：所有 material 寫入的唯一閘門
  ├─ SSH/SFTP ──→ Worker 1..N（tmux + exit_code 哨兵；一機一件；可附掛硬體板）
  ├─ WS（出站）←─ Runner 的 dispatch-agent（Claude Agent SDK session；隔離 worktree、
  │               逐條 Bash 權限提示；INV-AGENT-1/2）
  ├─ rsync ←──── results/{job_id}/ 回收 + metrics 解析
  └─ LLM 選配層（Anthropic API / MCP bridge）
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

- **Personal pilot 運行中**：單人全程瀏覽器 import → agent 改碼 → promote → 派工 → 結果回收 → metrics / experiment。
- **Studio 是唯一主介面**：Development Agent 以 runner 上的 `dispatch-agent` + Claude Agent SDK 承載；舊 Workspace 與舊 `claude -p` / Codex exec 路徑已退役。
- **正式 Compute worker 仍以 SSH/SFTP/tmux 為主**；Node Agent 保持 gated / test-only，真機啟用仍需對應 canary 裁定與證據。
- **硬體工程軌已落地 P1–P4**：裝置資源、映像、`hardware_action_v2`、receipt / known-good 與 Studio Hardware surface 均已有實作；實際啟用與 canary 狀態仍以 Ledger 為準。
- capability 的 `Implemented / Default / Pilot / Canary` **一律以 [`docs/CAPABILITY_LEDGER.md`](docs/CAPABILITY_LEDGER.md) 為準**，README 不再重複維護完整狀態表。

## Documentation｜文件入口

完整文件導航、真相順序、目錄分類與維護規則都集中在：

> **[`docs/README.md`](docs/README.md) — Documentation Home**

最常用的三份 canonical 文件：

| 文件 | 回答什麼 |
|---|---|
| [`docs/PLATFORM_CHARTER.md`](docs/PLATFORM_CHARTER.md) | 產品定位、架構、安全邊界、`INV-*` |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 使用者最後正式裁定了什麼 |
| [`docs/CAPABILITY_LEDGER.md`](docs/CAPABILITY_LEDGER.md) | capability 現在真正做到哪裡 |

產品方向看 [`docs/product/ROADMAP.md`](docs/product/ROADMAP.md)；實際操作看 [`docs/runbooks/`](docs/runbooks/)；歷史資料看 [`docs/archive/`](docs/archive/)。
