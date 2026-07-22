# Goal 3 — 平台完整化未來工作（提案，尚未核准、尚未排程）

> Status: 部分核准、分階段啟動中。2026-07-18 應使用者要求，把 Goal 2 明確
> 排除在外的三大塊（D2 沙箱、空伺服器 provisioning／Node Agent、多 Codex
> Runner 與互動式 agent）與其餘懸置項整理成有依賴順序的路線。本文件**不是
> 核准紀錄**：裁定以 `docs/DECISIONS.md` 為權威來源。2026-07-19 使用者裁定
> 啟動 Phase B／A（至 A1）／D-1／C（至 C0 草稿），並具名核准 DG-B；2026-07-19
> DG-C 核准 INV-SSH-1 修訂與 INV-NODE-* 新增（已寫入 invariants.md），G3 閘門
> 清除。2026-07-22 完成 C1（ExecutionBackend seam，零行為變更，golden tests
> 驗證）。DG-A 與各 canary 簽核仍待各自閘門（見 DECISIONS.md 2026-07-19 條目與
> 核准計畫檔 `iterative-wiggling-firefly.md` 的 G1–G4）；Phase A 的 A2 卡在真實
> Runner 的 cgroup CPU 委派與 ext4 project quota 兩項主機前提未完成。Goal 2
> 閉環已於 2026-07-18 試運轉驗證（提案→人工核准→自動派工全通）。

## 0. 全景：與既有計畫的關係

| 計畫 | 範圍 | 狀態 |
|---|---|---|
| Goal 1 | 身分／授權 shadow／OIDC | 完成（validated，未部署） |
| AI Engineering D1–D6 | Engineering Task 決策閘門 | 可作為者已完成（見 `docs/DECISIONS.md`） |
| Goal 2 | 閒置偵測→政策→自動放置閉環 | DG-1/DG-2 已核准，實作中/待實作 |
| **Goal 3（本文件）** | 沙箱強制、空機開通、執行通道演進、多 Runner | 提案 |

## Phase A — D2 落地：Runner 沙箱與資源強制

**解決什麼**：post-agent Git finalization 目前在 Codex sandbox 外以 Runner OS
user 執行（`docs/CURRENT_STATE.md` §0.9 列為 High 殘餘風險）；資源上限
（8 CPU／16 GiB／50 GiB disk／時間配額）只有已核准的數字，沒有強制機制。
這是把 `ENGINEERING_TASK_BACKEND_V1` 開上正式 Runner 前的最後一道硬閘。

**前提（外部，非程式碼）**：
1. 操作者在真實非 root Runner 服務帳號上確認：cgroup v2 可寫委派
   （systemd user scope 或等效）、ext4 project quota 或等效磁碟配額可用、
   bubblewrap 可建 no-network namespace。開發 shell 已證明無法替代這一步。
2. 操作者對照 Server A 實際硬體規格，最終確認 2026-07-16 核准的數字基準。

**切片**：
- A1 — 唯讀 preflight 探測腳本＋`GET /codex-runner/sandbox-preflight` 報告
  （只檢查、不啟用；FakeSSH 測試）。
- A2 — finalization 沙箱包裝器：bwrap no-network＋cgroup scope＋quota，
  包住 post-agent 的全部 worktree-aware Git 步驟；沙箱不可用即 fail-closed
  拒收結果（不降級為警告）。
- A3 — 把
  `ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION` 第二鑰匙
  改造成「preflight 通過」條件並退場（原設計即預告此路徑）。
- A4 — Runner canary：真實 Runner 上跑受控任務集（成功／超時／超額／
  沙箱故障），rollback 演練後才准正式啟用。

**決策點 DG-A**：核准 preflight 標準與最終資源數字；核准 canary 通過後的
啟用條件。

## Phase B — 空伺服器 provisioning（無常駐 agent 版本）

**解決什麼**：目前空機器要人工裝好 bash/tmux/rsync/nvidia-smi/Python 環境
才能 onboard。目標：平台可以「對空的伺服器下指令」完成開通——但**不需要**
先動 `INV-SSH-1`（不裝常駐 agent，仍走一次性 SSH）。

**切片**：
- B1 — 新 approval kind `server_bootstrap`：payload 綁定審閱過的固定
  bootstrap 腳本版本＋SHA-256、目標 host/user/key、要安裝的元件集
  （tmux/rsync/使用者級 Python env；不含 GPU 驅動——驅動屬人工前提）。
  腳本經 SFTP 送達執行（沿用不做 shell interpolation 的既有路徑），
  非 root、只裝使用者層工具、冪等可重跑。
- B2 — bootstrap 後自動跑既有 read-only capability check，通過才允許
  `server_add` 流程繼續；失敗留下可讀報告，不半開通。
- B3 — `onboard-worker.sh` 改為薄包裝：金鑰配置後直接引導到平台的
  bootstrap request，而不是要人自己準備機器。
- B4 —（可選）dataset 預熱：對新機自動建立 `sync` 提案（直接複用 Goal 2
  的 `auto_placement` 提案機制，不另造通道）。

**決策點 DG-B**：核准 bootstrap 腳本內容清單與「非 root、使用者層、冪等」
邊界；GPU 驅動/系統套件明確列為不做。

## Phase C — Node Agent（需 `INV-SSH-1` 修訂）

**解決什麼**：SSH 輪詢模式的天花板——斷線語意、擴充性、雙向事件。這是
roadmap（`docs/CODEX_ROADMAP_PROPOSAL.md` Phase 2–4）最大的一塊，照抄該
文件的階段設計，此處只記依賴與邊界：

- C0 —（前提）正式修訂 `INV-SSH-1`：既有 `INV-SSH-*` 繼續管 SSH 後端，
  新 `INV-NODE-*` 定義 agent 身分、lease、非插值、reconciliation、rollback。
  **這是保護不變量修訂，需單獨具名裁定，不與任何實作綁定核准。**
- C1 — ExecutionBackend seam：把現行 SSH 呼叫面收攏成 prepare/launch/
  inspect/stop/collect/cleanup 合約，SSH 實作零行為變更（golden tests）。
- C2 — Agent 協議與套件：outbound HTTPS 輪詢、node credential、lease/
  acknowledgement、心跳；全 fake 測試。
- C3 — 普通 job canary（roadmap 的量化門檻：≥100 jobs／≥2 nodes／7 天
  零重複啟動零假失敗），SSH 隨時可回退。
- C4 — 按節點逐台提升為主通道；Codex Runner 遷移放最後（C4 通過才動）。

**決策點 DG-C**：C0 的不變量修訂文字本身。

## Phase D — 多 Codex Runner 與互動式 agent 執行

**解決什麼**：單一 `CODEX_RUNNER_SERVER` 是 Engineering Task 吞吐與可用性
瓶頸；一次性 `codex exec` 無法中途對話、無法逐指令核准。

**切片**：
- D-1 — Runner pool：`codex_runner_servers` 集合＋每 Runner 併發=1 的
  確定性選擇（沿用 pick_job 風格），Runner 維持既有 reservation 語意；
  單 Runner 配置完全相容。
- D-2 — D1 app-server 正式接線前置：協議對真實 Codex CLI 版本 pin、
  `CodexAppServerProvider` 接進 turn 生命週期（`CONTROLLED_CODING_RUNNER_V1`
  仍為閘門）；**啟用需 2026-07-16 D1 裁定預告的另一次獨立 canary/rollback
  簽核**。
- D-3 — `engineering_command` approval kind：app-server command-approval
  callback 對接核准流（payload 綁指令 bytes/digest/cwd/policy，2026-07-16
  D3 裁定已預告此 kind 隨 D1 adapter 一起定義）。
- D-4 — `engineering_task_finalize`／`_promote`／`_pr` 三 kind＋D6 正式
  GitHub App 啟用（App 註冊、repo allowlist、installation-token broker——
  D6 裁定明載需另外具名的操作設定決策）。

**決策點 DG-D**：D-2 的啟用 canary 簽核；D-4 的 GitHub 操作設定。

## Phase E — 資源語意演進（最後、獨立）

單 job/單機是現行明文語意（CLAUDE.md：改動即排程架構變更，需明確設計核
准）。在 Goal 2 的容量資料與 Phase C 的 agent 觀測成熟後，才值得評估：
GPU slot 配置、單機多 job、搶佔/遷移、配額。**本文件不提案具體設計**，僅
記錄：任何此類變更需要獨立的架構決策文件與裁定，且必須建立在持久化容量
資料（Goal 2 Slice 1–2）之上。

## 依賴與建議順序

```
Goal 2 S1–S5 穩定
   ├─→ Phase A（獨立；只需硬體確認，建議最先——它擋著 backend 正式啟用）
   ├─→ Phase B（獨立；不動不變量，價值/風險比高）
   ├─→ Phase C（C0 不變量裁定 → C1 seam → C2–C4；最長）
   │      └─→ Phase D 的 Runner 遷移部分依賴 C4
   ├─→ Phase D（D-1 Runner pool 可先行；D-2/D-3 綁 app-server；D-4 綁 D6 操作決策）
   └─→ Phase E（最後，依賴 Goal 2 容量資料）
```

## 全計畫固定邊界（沿用）

自動核准白名單恰好 `enqueue|stop`；additive schema；DB-before-side-effect；
unreachable≠failed；SFTP 非插值路徑；開發/測試不碰真實 worker/憑證/runtime
檔案；`AUTHORIZATION_MODE` 維持 `off|shadow`（授權強制若要做，是獨立於本
計畫的保護不變量決策）；SSH 後端永久保留為相容/緊急通道，不因 Node Agent
上線而移除。
