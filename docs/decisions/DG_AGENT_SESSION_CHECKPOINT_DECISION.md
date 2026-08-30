# DG-AGENT-SESSION-CHECKPOINT — session 成果進入 promotion 流的決策閘

> Status: **approved on 2026-08-24 — Option A（新增 agent_session_checkpoint kind）**（權威紀錄見 `docs/DECISIONS.md` 同日條目，含語意定義與 UX 附帶裁定）
> 本文件是 review packet，不是核准紀錄。裁定後記入 `docs/DECISIONS.md`。
> 背景：DG-AGENT-SESSION-V1（2026-08-24）P3 實作時，coder 依 BLOCKED
> 邊界正確停工——promotion 機制（`resolve_promotion_candidate()`，
> `app/code_promotion.py:69`）只接受背靠真實 approval 的
> `engineering_tasks` 列（schema 釘 `approval_id INTEGER NOT NULL
> UNIQUE`）；任何橋接都需要新 kind 或偽造 approval，後者絕對禁止。

## 1. 問題

使用者在 web 按「checkpoint」後，Server A 需要把 session worktree 的
修改變成可被既有 promotion 流消費的 artifact。這是 material mutation
（staging push + DB 列建立），且橋接列的 schema 要求真實 approval。

## 2. 選項

- **A（推薦）— 新 kind `agent_session_checkpoint`**：
  checkpoint-request → pending 卡 → 人核准 → approve 分支執行
  Server-A checkpoint 管線（commit worktree → 既有 path-policy 雙檢 →
  bundle 建立與 `git bundle verify` → staging push）→ 以**本核准**作為
  bridge `engineering_tasks` 列的 `approval_id`（誠實 provenance，
  metadata 標注 session 來源）→ 之後走**完全不變**的
  `engineering_task_promote` 二次核准（DG-CODE-PROMOTE P-1…P-5 不動）。
  kind 永不自動核准（enqueue|stop 白名單不動）。
- **B — 改造 promotion 契約**直接消費 session 產物：需重開
  DG-CODE-PROMOTE、動最敏感機制、slice 大。不推薦。
- **C — 延後**：V1 無 promotion 出口，目標 lifecycle 斷尾。不推薦。

## 3. A 案實作範圍（裁定後交 sonnet-coder）

`agent_session_checkpoint` 進 `VALID_APPROVAL_KINDS`；request 驗證
（session active、無 turn 進行中、至少一 turn 完成、無 pending 重複）；
approve 時重驗（INV-APPROVAL-3）→ 純函式 checkpoint script（tmux+sentinel
或有界 ssh_run；git positional 全部 `--` marker + 前導 `-` 拒絕，P2 慣例）
→ bundle 驗證 → bridge 列（terminal、session provenance）；audit 進
`agent_session.compatibility` family；P4 預留的 checkpoint 按鈕接上；
計數閘依既定流程；測試含 path-policy fail-closed、promotion 全流程
fake、永不自動核准釘住。

## 4. 裁定模板

```text
DG-AGENT-SESSION-CHECKPOINT：A 核准 / B / C（延後）
```
