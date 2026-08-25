# Dispatch Center

> **小團隊的 Web AI/ML 工程控制中心**——讓一個小團隊（或單人）只靠
> 瀏覽器與 Development Agent，就能管理 Project、遠端 GPU Server、
> 程式開發、Git 版本、多機實驗與結果分析的完整平台。

## 這個專案在做什麼

日常訓練模型的痛點：ssh 到每台機器看誰有空、scp 資料、tmux 開任務、
手動找 log、忘記哪個結果對應哪個 commit。Dispatch Center 把這整條
工作流搬進一個網站：

```
匯入/建立 Project → AI Engineer 對話改碼（隔離工作區）→ Review Diff
  → Commit → promote 成 ProjectVersion → 派工到多台 GPU Server
  → 集中看 Runs / Metrics / Logs / Artifacts → 比較、分析、下一輪
```

- **專案中心**：Project 統一代表 repo、版本、環境、資料集綁定、
  對話、實驗與結果；Server 只是執行資源。
- **AI 協作開發**：與 Development Agent（Claude Code、Codex）長期
  對話，讓它在 dispatch 建立的隔離 worktree 讀檔、改碼、跑
  test/lint，產出可審查的 diff——**它永遠不能自己核准或執行任何東西**。
- **安全派工**：每個會改變系統狀態的動作（派工、停止、改機器設定、
  匯入專案、promote 版本…）都是一張核准卡，由人決定；危險指令在
  建立請求當下就被拒絕。
- **可收斂的執行**：SSH + tmux + exit_code 哨兵協議派工；Server A
  重啟、網路斷線、worker 失聯都能由 reconciliation 收斂，不會誤判
  任務狀態（連不上 ≠ 失敗）。
- **有根據的結果**：任務結束自動回收 results；workload 寫
  `results/{job_id}/metrics.json`（metrics-v1 契約）即被解析入庫，
  缺檔是 unknown、不是失敗；所有結論都能追溯到證據。

## 架構一覽

```
Browser（單頁前端 static/）
  │  OIDC 認證、核准卡、AI Engineer 分頁
Server A ── FastAPI 單體 + SQLite（jobqueue.db 是唯一真相）
  │        scheduler / monitor / reconcile 背景迴圈
  │        approvals：所有 material 寫入的唯一閘門
  ├─ SSH/SFTP ──→ Worker 1..N（tmux + exit_code 哨兵；一機一件）
  ├─ rsync ←──── results/{job_id}/ 回收 + metrics 解析
  └─ LLM 選配層（Anthropic API / 本地 vLLM / MCP bridge）
       ——只能查詢與「建 pending 卡」，零執行權，缺席不影響本體
```

兩個 Plane、一個交會點：**Development Plane**（該存在什麼程式碼：
onboarding、agent 改碼、diff、ProjectVersion）與 **Compute Plane**
（跑什麼、在哪跑、用什麼資料：ExecutionPlan、排程、Run、結果）只在
「人工核准 promote 的 ProjectVersion」交會——未 promote 的程式碼
永遠進不了執行面。

## 安全模型（不變式）

1. 所有 material 寫入走核准流；自動核准白名單只有
   `enqueue`/`stop`。
2. 任何 agent（LLM 工具、MCP、Development Agent）永遠沒有
   approve/reject/自由 shell 工具，永遠不能核准自己的請求。
3. 使用者指令原文只經 SFTP 落地，永不進 shell 字串插值。
4. 任務成敗只看哨兵 exit_code；worker 連不上＝跳過不判定。
5. 憑證永不進 DB、稽核、diff 或 prompt。

完整 canonical invariants 見
`.claude/skills/dispatcher-domain/references/invariants.md`；
具名裁定見 `docs/DECISIONS.md`。

## 快速開始

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

## 目前狀態

- **Personal pilot 運行中**：單人全程瀏覽器 import → agent 改碼 →
  promote → 派工 → 結果回收 → metrics 已跑通。
- 大量 v2 能力（typed revisions、ExecutionPlan v2、dataset
  governance、RBAC、Node Agent…）已實作但 **default-off**，逐步
  啟用中——能力現況以 `docs/CAPABILITY_LEDGER.md` 為準
  （`implemented` ≠ `enabled` ≠ `production-ready`）。
- 最終完成品定義見
  `docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`。

## 文件地圖（真相順序）

| 順位 | 文件 | 內容 |
|---|---|---|
| 1 | `.claude/skills/dispatcher-domain/references/invariants.md` + `docs/DECISIONS.md` | 安全真相：不變式與具名裁定 |
| 2 | 程式碼與 `tests/` | 實作真相（4300+ 離線測試） |
| 3 | `docs/CAPABILITY_LEDGER.md` | 能力現況帳本 |
| 4 | `docs/product/…FULL_DEVELOPMENT_PLATFORM_PLAN.md` | 產品藍圖（未來方向） |
| — | `docs/STAGE_ACCEPTANCE_MANUAL.md` | 原 README：階段 1–11 逐步驗收手冊（已存檔，章節編號不變） |
| — | `CLAUDE.md` + `.claude/skills/` | AI 協作開發規範 |
