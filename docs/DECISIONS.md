# AI Engineering Task 決策紀錄

> Status: approval record. 本文件記錄使用者對 `docs/AI_ENGINEERING_DECISION_GATE.md`
> 所列決策的實際裁定。決策文件本身仍是唯讀的審閱包，不因這份紀錄而改寫；
> 這份紀錄才是「哪些選項被核准」的權威來源。

## 決策日期：2026-07-16

使用者採納助手建議的答案，逐項裁定如下（依決策文件 §5 模板格式）：

```text
D1 app-server: bounded implementation only
D2 resources: approve proposed limits（附帶條件，見下）
D3 approval kinds: approve proposed split
D4 secret broker: keep disabled
D5 Run Profiles: approve proposed v1
D6 GitHub: interface+fake only
Browser evidence: authorize disposable install
```

## 逐項裁定與範圍

### D1 — Codex app-server adapter：**bounded implementation only**

核准在 `CONTROLLED_CODING_RUNNER_V1=false`（預設關閉）下實作與假協議測試；
**不核准正式啟用**。實作必須：

- 只註冊一個版本化 allowlisted adapter（如 `codex-app-server-v1`），使用者
  永遠不能提供任意 executable；
- 精確 pin 已審閱的 Codex CLI/app-server 版本與協議能力集，版本或訊息形狀
  drift 時 fail closed；
- 保留 `codex-exec-v1` 作為 legacy adapter，且 app-server turn 一旦可能已
  產生副作用，永不悄悄 fallback 回 exec；
- 測試只用 in-process 假協議 peer，不接觸真實 provider、Runner 或 worker；
- 正式環境啟用需要**另一次獨立的** canary/rollback 簽核，不與本次決策
  綁定。

第一批解鎖實作見 Phase 4（`app/codex_app_server.py` + `CodexAppServerProvider`）。

### D2 — Runner 隔離與 Server A 資源強制：**approve proposed limits（附帶條件）**

核准決策文件提出的預設上限（8 CPU / 16 GiB / 50 GiB task disk / 20 分鐘
每指令 / 45 分鐘+至多 3 輪自動修復 / 2 小時累計執行時間）作為**數字基準**，
但附帶兩個尚未解除的前提：

1. **實機規格尚未核對**——這些數字目前是「accepted-plan defaults」，
   需要操作者對照 Server A 的實際硬體規格與典型任務資源需求核對一次，
   才能視為最終值。
2. **fail-closed 沙箱前提尚未滿足**——2026-07-15 的唯讀能力稽核已確認本
   開發 shell 具備 bubblewrap 0.6.1、非特權 user/network namespace、
   cgroup v2，但**無法**委派可寫 cgroup、無法連上 systemd user/system
   bus、也沒有可用的 ext4 project quota 或 container/FUSE quota
   runtime。因此 `timeout`/`prlimit`/軟性磁碟檢查繼續被拒絕作為聚合
   CPU/RAM/process 與 50 GiB task-disk 保證的替代品。post-agent Git
   finalization 沙箱（decision gate §D2 詳述的核心殘餘風險）同樣尚未
   實作。

**因此 D2 的實作不在本輪範圍**：core.md 明確排除，`[F1]` 只把「不得單開
backend」從文件紀律變成技術閘門（雙鑰匙），沒有解決底層沙箱能力缺口。
D2 實作需要在真實的非 root Runner 服務帳號上通過 reviewed preflight/
canary 後才能進行，且不能由本開發 shell 完成或驗證。

### D3 — 指令與 task-lifecycle 核准：**approve proposed split**

核准新增窄範圍的核准 kind，**自動核准清單維持恰好 `enqueue|stop` 不變**。
第一批解鎖實作（Phase 3）只做兩個已有明確語意、且不依賴 D1 adapter 的
kind：

- `engineering_task_retry`——對同一份不可變合約建立新 attempt（沿用現有
  `codex-exec-v1`，不需要 app-server）；
- `engineering_task_discard`——標記 terminal 任務作廢、withhold 結果
  指標，但不自動刪除工作區（保留既有 cleanup endpoint 作為後續手動
  步驟）。

`engineering_command`（單一指令核准）留待 Phase 4 隨 D1 adapter 一起定義，
因為它的 payload 需要繫結 app-server 的 command-approval callback；
`engineering_task_finalize`/`_promote`/`_pr` 三種留待 D5/D6 相鄰切片
（尚未實作，只有本決策紀錄）。

### D4 — Secret-reference 後端：**keep disabled**

維持現狀關閉。目前 repo 沒有通用 secret broker，raw value 在 task 與 Run
Profile 表單中持續被拒絕。**不核准**任何 broker/provider 或 scope model，
本輪不實作。

### D5 — Run Profile 持久化：**approve proposed v1**

核准加入 additive `run-profile-v1` schema，含不可變 revision 與
approval-gated create/update/archive。既有 `Project.default_command`、
`setup_cmd`、`require_tag` 保持相容欄位，**不自動遷移**成假造的已核准
profile。本輪**未實作**——排入下一輪，待 D3/D1 的核准/adapter 模式驗證
後再開始。

### D6 — GitHub 發布：**interface + fake only**

核准先做 provider 介面與 fake-provider 測試。正式操作啟用（GitHub App
註冊、repo allowlist、installation scope、egress policy、短期
installation-token broker）**尚未核准**，需要另外具名的操作設定決策。
第一次發布動作僅能建立 draft PR，永不 merge/deploy，也永不把憑證交給
coding agent。本輪**未實作**——排入下一輪。

### D7 — 授權強制：本輪不涉及

決策文件本身未對此提出決策請求；`AUTHORIZATION_MODE` 維持 `off|shadow`。
未來若要啟用 403/404 過濾或伺服端強制，是獨立的保護不變量決策，不得與
Engineering Task 工作綁在一起核准。

### 瀏覽器視覺證據：**authorize disposable install**

核准在本開發環境一次性安裝 Playwright + Chromium，產出 1440/1024/768/
375 像素的真實截圖與鍵盤/焦點/overflow 檢查證據，取代目前缺失的視覺
驗收證據缺口。排入 Phase 5。

## 本輪（2026-07-16 這批）實際解鎖的實作範圍

| 決策 | 本輪動作 |
|---|---|
| D1 | Phase 4：`codex-app-server-v1` adapter 骨架 + 假協議測試，**不接線**到現有 turn 生命週期，`CONTROLLED_CODING_RUNNER_V1` 預設關閉 |
| D2 | **不實作**——前提未齊（見上） |
| D3 | Phase 3：`engineering_task_retry` + `engineering_task_discard` 兩個 kind |
| D4 | 不實作（維持關閉） |
| D5 | 不實作（下一輪） |
| D6 | 不實作（下一輪） |
| D7 | 不涉及 |
| 瀏覽器證據 | Phase 5：Playwright 安裝 + 截圖 |

## 明確不做（本輪）

- D2 的任何實作或啟用前提放寬；
- D5 Run Profile persistence、D6 GitHub 介面的程式碼；
- `engineering_task_finalize`/`_promote`/`_pr` 三種 approval kind；
- 修改 `.claude/skills/dispatcher-domain/references/invariants.md` 中的
  任何不變量；
- 讓 `engineering_task_retry`/`_discard` 的自動核准清單納入
  `enqueue|stop` 以外的 kind。

## 追蹤

實作進度與驗證方式見 `/home/formosa/.claude/plans/groovy-tinkering-lake.md`
（本次工作階段的核准計畫）。此文件與該計畫的 Phase 編號一致。
