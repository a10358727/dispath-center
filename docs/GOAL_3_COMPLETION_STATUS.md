# Goal 3 完成狀態（2026-07-25 結算）

> 這份文件回答一個問題：`docs/GOAL_3_FUTURE_WORK_PLAN.md` 還剩什麼？
>
> 結論：**所有能由開發工作完成的部分都已完成**。剩下的每一項都卡在
> 「真實機器與時間窗」或「需要你具名裁定」，不是還沒寫的程式碼。下面
> 逐項列出卡在什麼、以及要解除需要什麼。
>
> 權威裁定紀錄仍是 `docs/DECISIONS.md`；實作與驗證細節見
> `docs/CURRENT_STATE.md` §0.16–§0.24。

## 一、已完成（程式碼已落地並驗證）

| 切片 | 內容 | 驗證 |
|---|---|---|
| A1 | 唯讀沙箱 preflight 端點 | §0.17，21 tests |
| B1–B3 | 空伺服器 bootstrap（approval kind、capability check、helper 改薄） | §0.16 |
| B4 | 新機 dataset 預熱提案（DG-B4） | §0.22，35 tests |
| C0 | `INV-SSH-1` 修訂＋`INV-NODE-1…6`（DG-C） | 已寫入 invariants.md |
| C1 | ExecutionBackend seam（SSH 零行為變更） | §0.20，golden tests |
| C2 | Node Agent 協議、憑證、工作機端套件 | §0.23，125 tests |
| C3（程式碼） | per-node 通道路由＋SSH 重複派發防護＋`NodeExecutionBackend` | §0.24，40 tests |
| C3（stop-request） | 已核准的停止請求經 poll／心跳送達 agent＋送達回執 | §0.25，18 tests |
| C3（artifact-metadata） | agent 回報產出檔案的路徑/大小/digest（**不傳內容**） | §0.26，20 tests |
| D-1 | 多 Codex Runner pool | §0.18，12 tests |

**A2–A4 已於 2026-07-25 依你的裁定從計畫移除**（沙箱強制路線撤回，
`docs/DECISIONS.md` 同日條目）。

所有新功能預設關閉；未開旗標時系統行為與 Goal 3 開始前逐位元相同。

## 二、卡在真實環境與時間窗（我做不到，不是沒寫）

### C3 canary — 閘門 G4

roadmap 定的門檻：**≥100 個非正式任務、≥2 台節點、連續 7 天**，期間零重複
啟動、零假斷線失敗、零遺失終態，外加一次實地 rollback 演練。

- 程式碼已就緒（防護與逐台回退都已實作並測過）。
- 需要：第二台可用機器、在其上部署 agent、然後**等七天**。
- 這是操作行為與時間累積，無法用開發工作或測試替代。

**C4（逐台提升為主通道、Codex Runner 遷移）依賴 C3 通過**，因此連帶未開始。

> **2026-07-25 兩次修正**（都是我自己讀錯 roadmap，不是真的被阻塞）：
>
> 1. 先前寫「C4 還需要 stop-request 協議」——`docs/CODEX_ROADMAP_PROPOSAL.md`
>    Phase 3 的 deliverables 明列 stop-request 屬於**協議切片**（C2/C3）。
>    已補上：`NodeExecutionBackend.stop()` 真的會記錄已核准的停止請求，
>    agent 透過 poll 或心跳兩條路徑取回（§0.25）。
> 2. 先前寫「artifact 上傳需要儲存政策裁定所以不做」——roadmap 要的是
>    **artifact-metadata**（路徑/大小/digest），不是檔案位元組。不傳內容
>    就沒有儲存位置/配額/保留政策要決定。已補上（§0.26）。
>
> **roadmap Phase 3 的協議 deliverables 至此全部實作完畢。**
> 仍未實作的是**真正的結果回收**（搬移檔案位元組），`collect()` 維持
> `NotImplementedError`——那個確實需要儲存決策，屬 C4。

### D-2 — Codex app-server 正式接線

- 2026-07-16 D1 裁定已明載：正式啟用需要**另一次獨立的 canary/rollback
  簽核**，不與該次決策綁定。
- 另外需要對**真實 Codex CLI 版本**做協議 pin；該 CLI 在主機重建後遺失
  （文件記載版本 0.144.4），需要你重新安裝才能核對。

**D-3（`engineering_command` approval kind）依賴 D-2**：2026-07-16 D3 裁定
寫明這個 kind 的 payload 要繫結 app-server 的 command-approval callback，
所以在 adapter 接上之前定義它等於臆測 payload 形狀。

## 三、卡在你的裁定（governance 要求，不是我保守）

### D-4 — GitHub 發布正式啟用

2026-07-16 D6 裁定原文：正式操作啟用（GitHub App 註冊、repo allowlist、
installation scope、egress policy、短期 token broker）**「尚未核准，需要另外
具名的操作設定決策」**。

要解除需要你決定：App 註冊方式、允許的 repo 清單、token broker 的存放方式。
在那之前實作等於替你決定安全邊界。

### Phase E — 資源語意演進

計畫文件自己寫明：**「本文件不提案具體設計」**，且任何此類變更需要
獨立的架構決策文件與裁定，並且必須建立在 Goal 2 的持久化容量資料之上。

CLAUDE.md 也明列：GPU slot 配置、單機多 job、搶佔/遷移、配額屬於排程架構
變更，需要明確設計核准。目前單 job/單機是明文語意。

## 四、要往下走，你可以做的三件事

1. **推進 C3**：準備第二台機器 → `NODE_AGENT_V1_ENABLED=true` → 兩台各自
   `POST /nodes/enroll-request` 並核准 → 憑證填進 agent →
   `execution_backend: node` → 開始累積 100 個任務/7 天。
   （提醒：canary 期間隨時可把欄位改回 `ssh` 回退，不需要資料遷移。）
2. **推進 D-2**：重裝 Codex CLI（版本對照文件記載的 0.144.4），我就能做
   協議版本 pin；正式啟用仍需你另外簽核 canary。
3. **裁定 D-4**：給出 GitHub App 的操作設定決策，我再實作三個 kind 與發布
   流程。

## 五、不建議現在做的事

- **不要**在 C3 canary 通過前把正式任務的機器改成 `execution_backend: node`
  ——那等於跳過驗證門檻。
- **不要**因為 `NodeExecutionBackend` 已存在就以為 node 通道可用；它的
  stop/collect 還沒實作，會直接拋例外。
- Phase E 的任何「順手先做一點」都不成立：它會改變排程架構語意。
