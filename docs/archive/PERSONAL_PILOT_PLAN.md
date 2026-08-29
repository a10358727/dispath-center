# Personal Pilot 實作計畫（第一個計劃書）

> archived: 2026-08-30 · superseded_by: `docs/PLATFORM_CHARTER.md`（定位／架構／不變式）、`docs/CAPABILITY_LEDGER.md`（能力現況）、`docs/DECISIONS.md`（裁定）· 本檔為歷史證據，不是現況。

**文件類型：實作計畫（bounded implementation packet）**
**日期：2026-08-23**
**授權來源：`docs/DECISIONS.md` → DG-PERSONAL-PILOT-v1（D1–D4）**
**部署模型：single-user / non-production personal pilot / private network**

> 本文件是計畫，不是實作，也不授權任何 invariant 變更。安全真相在
> `.claude/skills/dispatcher-domain/references/invariants.md` 與
> `docs/DECISIONS.md`；能力現況在 `docs/CAPABILITY_LEDGER.md`。
> 本 pilot 不產生任何 production-readiness / canary / v2 activation 宣稱。
> 完整 second-pass 審視與長期 roadmap 見
> `docs/product/FULL_PLATFORM_SECOND_PASS_PLAN.md`。
>
> **2026-08-25 進度註記（本文其餘內容維持 2026-08-23 撰寫時快照）**：
> 撰寫後的具名裁定已推進本計畫的多處內容——§5 non-goals 中的
> Claude Code adapter、metrics 解析、AgentSession／長期對話已分別由
> DG-CLAUDE-ADAPTER、DG-METRICS-CONTRACT、DG-CONVERSATION-V1 與
> DG-AGENT-SESSION-V1 裁定並實作，且已在本 pilot 啟用；「不升級
> capability ledger 任何欄位」一句由 2026-08-25 的 DG-PERSONAL-PILOT-v1
> D1 clarification 修訂（pilot 直接證據可支撐 `deployed=yes`，
> 明標 personal-pilot-only；canary／production-ready 永不因 pilot 升級）。
> §6-T0 的「三個 feature flags」與 §8 rollback 因此不完整：pilot 現行
> 啟用旗標與完整關閉清單以 `docs/CAPABILITY_LEDGER.md` 與
> `app/settings/features.py` 為準。

---

## 1. 目標：Usable Pilot 定義

完成後，單一使用者只用瀏覽器（legacy UI）+ 一組 `X-Auth-Token`，可以：

1. **Projects**：匯入既有 server 上的專案（inventory scan → candidate →
   import）或直接註冊；`git_init`（如需）→ `hub-sync` 得到第一個
   ProjectVersion。
2. **AI 工程任務**：建立 engineering task → 核准 → Codex 在 runner 機的
   隔離 worktree 改碼跑驗證 → web 看 diff → promote-request → 核准 →
   promoted ProjectVersion。
3. **建立 Run**（本計畫新增 UI）：選 promoted version + 目標 server
   （approved+active revision）+ `dataset_none` + command → pending
   `plan_run` approval → 核准 → Job 由既有 legacy scheduler/SSH 派工。
4. **看結果**：Job log 即時 tail、失敗一鍵 diagnose、完成後在 web 列出／
   下載 `results/{job_id}/` 檔案（`metrics.json` 原文顯示）。
5. 每一步都留 approval + audit；agent 永遠只能提案。

## 2. 裁定摘要（權威文字在 DECISIONS.md）

- **D1** 安全姿態：維持現行預設 shared `X-Auth-Token` +
  `AUTHORIZATION_MODE=off`；僅限 single-user、non-production、
  private/trusted network；非永久取消 authorization/RBAC 的產品決策。
- **D2** 第一個 promoted ProjectVersion 走正常 engineering task →
  review → promotion，不加捷徑。
- **D3** Results 只做最小唯讀 list + download（path-safe、bounded、
  authenticated）；不做 metrics parsing/schema。
- **D4** Legacy-first；Product v2 仍是 final target architecture，
  pilot 不構成 v2 activation。

## 3. 用語規範

- **Feature flags**：預設關閉的布林開關（`.env`）。
- **Configuration**：設定值（非開關），如 runner 機名。
- **Activation steps**：經 approval 或操作在運行系統上執行的動作。

## 4. 已查證的 code 事實（本計畫的依據）

- `ENGINEERING_TASK_BACKEND_V1`、`CODE_PROMOTION_V1_ENABLED` 等 v1
  feature flags 對 `API_V2_ENABLED`/`PRODUCT_RBAC_V2_ENABLED`/
  `OIDC_ENABLED` **零依賴**（`app/settings/features.py:308-374`）；
  engineering backend 需同開
  `ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION`
  （`app/settings/model.py:387-394`；該風險 2026-07-25 已裁定長期接受）。
- `plan_run`（ExecutionPlan v1）**不受 feature flag 閘控**
  （`app/main.py:8082/8100/8156`）；預設 `require_reproducible=true`
  要求 **promoted** ProjectVersion（`app/db.py:8030-8037`）；可釘
  `dataset_none`（`app/execution_plan.py:185-193`）；不需要 run
  profile；核准後 materialize 成一般 legacy Job 走既有 scheduler/SSH
  （`app/db.py:8335-8471`）。
- **硬要求**：plan 目標必須是 approved+active 的
  `server_config_revision_id`；僅由 `servers.yaml` 觀測到的機器是
  `legacy_observed`，不能當 plan 目標（`app/execution_plan.py:195-199`、
  `app/server_publication.py`）。
- Single-user 可行性：`AUTHORIZATION_MODE=off` + RBAC v2 關閉時，
  legacy kinds（`coding_task`/`engineering_task_promote`/`plan_run`/
  `apply_patch`/`git_init`）**沒有任何 requester≠decider 檢查**
  （`app/main.py:9942-9968`；enforcement 僅在 `enforce` 模式，
  `app/authorization.py:1111-1142`）；shared token 對應預建的
  `legacy-admin` durable actor，requester/decider/audit 都有紀錄。
- Legacy UI 已覆蓋：engineering task 建立/detail/diff/promote 按鈕、
  generic approval 卡（任何 kind 皆可核准）。**缺**：`plan_run`
  request/檢視 UI（`static/` 零覆蓋）與一般 Job 的 results
  list/download（`index.html:782` 明文承認）。
- Engineering task request 需要 `project_version_id`（不必 promoted）
  與含該 commit 的 Server A Hub（先 `hub-sync`）；第一個 ProjectVersion
  由 hub-sync 產生（`app/db.py:24478-24500`）。

## 5. Scope / Non-goals

**Scope**：§1 的完整鏈路，全程瀏覽器（一次性 activation steps 除外）。

**Non-goals（明文不做）**：Product v2 stack 任何 feature flag 開啟；
OIDC／`enforce`；Claude Code adapter（下一階段，DG-CLAUDE-ADAPTER
另裁）；Auto provider selection；Experiment matrix；優化迴圈；metrics
解析／儲存；Node Agent；GitHub 發布；`EXECUTION_ATTEMPT_*` 路徑；
dataset snapshot（pilot 一律 `dataset_none`）；AgentSession／長期對話；
multi-user；任何 refactor（含 `app/main.py` 拆分）；任何 invariant 或
釘住測試的變更。

## 6. Bounded tasks

### T0 — Activation & Configuration（操作者執行，零 code）

**Feature flags**（`.env`，三個，預設皆 false）：

```text
ENGINEERING_TASK_BACKEND_V1=true
ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION=true
CODE_PROMOTION_V1_ENABLED=true
```

**Configuration**：

- `CODEX_RUNNER_SERVER=<runner 機名>`（須為 `servers.yaml` 中 enabled
  的機器；未設則 engineering 功能整體停用）。
- `AUTH_TOKEN` 已設（現況沿用）。

**Activation steps**：

1. Runner 機安裝並登入 `codex` CLI（script preflight 硬檢查
   `codex login status`）。
2. **每台 plan_run 目標機**提交一次 `server_update` approval 並核准
   → 鑄出 approved+active `server_config_revision_id`。
3. Pilot 專案：import/建立 → `git_init`（如需）→ `hub-sync` →
   第一個（未 promoted）ProjectVersion。
4. 依 D2 跑第一個小型真實 engineering task → review → promote。

**Acceptance criteria**：

- 重啟後無 settings validation error；
  `GET /engineering-tasks/capabilities` 顯示 enabled。
- `GET /codex-runner/status` 健康；`GET /codex-runner/sandbox-preflight`
  回報結果。
- `GET /server-config` 對每台目標機顯示 approved/active 的
  `server_config_revision_id`。
- `GET /projects/{name}/versions` 出現 `promotion_state=promoted` 的版本。
- 兩個新開 feature flags 關回後系統回到現行行為（rollback 檢查）。

### T1 — N1：Run Request UI（sonnet-coder；純前端）

**Scope**：`static/index.html` + `static/ui.js`（必要時 `ui.css`）；
零後端變更。專案 detail 頁新增「建立 Run」區塊：

- promoted version 下拉（`GET /projects/{name}/versions`，只列
  `promotion_state=promoted`）；
- 目標下拉（`GET /server-config`，只列有 approved/active
  `server_config_revision_id` 的機器，送出 revision id）；
- command 多行輸入；`dataset_none` 固定為 true（顯示說明文字）；
  `require_reproducible` 維持後端預設 true，不出現在表單；
- 先呼叫既有 preview 端點顯示 blocking reasons，再
  `POST /projects/{name}/runs/request`；
- `KIND_LABEL` 補 `plan_run` 與 `engineering_task_promote` 標籤與
  payload 摘要；Job/approval 檢視處以 `GET /runs/{plan_id}` 顯示
  plan 狀態一行。

**邊界**：遵守 `frontend-architecture` skill（vanilla JS、無框架、
text node 渲染、loading/empty/error 狀態）；不新增端點、不動後端。

**Acceptance criteria**：

1. 各欄位有 loading/empty/error 狀態；無 promoted version 或無
   eligible 目標時顯示明確指引。
2. 送出 → Approvals 出現 `plan_run` pending 卡（有標籤與摘要）→
   核准 → Jobs 列表出現對應 Job。
3. Preview blocking reasons 以可讀文字呈現。
4. 既有 static smoke gate 與 JS syntax check 通過；補對應前端測試；
   全部既有測試綠。

### T2 — N2：Results list/download（sonnet-coder；後端 + 前端）

**Scope**：

- 後端（`app/main.py` 路由 + 純函式輔助，可併入 `app/results.py`）：
  - `GET /jobs/{job_id}/results`：列 `results/{job_id}/` 檔案
    （name/size/mtime），bounded（≤500 筆、深度 ≤5）；目錄不存在回
    空列表 + `collected=false`（missing = unknown，不是錯誤）。
  - `GET /jobs/{job_id}/results/{path}`：單檔串流下載；嚴格 path
    safety——canonical 化後必須位於該 job 目錄內、拒絕 `..`、拒絕
    symlink escape（realpath containment / `O_NOFOLLOW` 慣例）。
  - 兩端點**不進** `_AUTH_EXEMPT_ROUTES`（INV-APPROVAL-5 預設涵蓋）；
    登記 authorization catalog 為 read-only action；純 Server A 本地讀，
    **零新遠端指令**（INV-SSH-4 不受影響）。
- 前端：Job 面板新增 results 檔案列表 + 下載連結；`metrics.json`
  存在時原文顯示（inline 預覽 ≤64 KiB，超過只給下載）。
- `tests/openapi_snapshot.sha256` 依既有流程隨新路由更新。

**Acceptance criteria**：

1. 單元測試：path traversal（`..`、絕對路徑、URL-encoded）與 symlink
   escape 全拒；不存在目錄 → 空列表非 500；超上限截斷且明示 truncated。
2. 認證測試：未帶 credential 被拒；authorization coverage 測試涵蓋
   新路由。
3. 整合測試（fake）：完成 Job 在 UI 顯示檔案列表、可下載；含
   `metrics.json` 時原文呈現。
4. 靜態釘住項（forbidden imports/names、audit open-mode）原樣通過；
   全套件綠。
5. **不含** metrics 解析；無 scheduler/reconcile 模組 import 新程式。

### T3 — End-to-end pilot validation（操作者 + 主 session，零新 code）

**Scope**：T0–T2 完成後，一次從瀏覽器跑完 §1 全 loop；主 session
（Fable）對照本計畫驗收。

**Acceptance criteria**：

1. §1 鏈路在 pilot 機一次走通，全程無 SSH/CLI 介入（T0 一次性
   activation steps 除外）。
2. `audit.jsonl` 與 durable events 對 loop 每個 approval/lifecycle
   動作都有紀錄。
3. 全套離線測試綠；釘住斷言零弱化。
4. 產出 pilot 走查紀錄（日期、步驟、輸出），**明文標注不構成
   production/canary 證據**（D1）。

## 7. 指派與順序

| Task | 執行者 | 順序 |
|---|---|---|
| T0 | 操作者 | 與 T1 可並行 |
| T1 | sonnet-coder | T0 後或並行 |
| T2 | sonnet-coder | **T1 之後**（兩者都改 `ui.js`/`index.html`，同一時間至多一個 coder） |
| T3 | 操作者 + 主 session 驗收 | 最後 |

**BLOCKED 條件（T1/T2 共用）**：發現需要新 approval kind、修改任何
invariant／釘住測試、或 preview/plan API 形狀與本計畫描述不符時，
停工回報，不自行擴權。

## 8. Rollback

關閉 §6-T0 的三個 feature flags 即回到現行行為；T1/T2 的 UI 與唯讀
端點無狀態、無 migration，revert commit 即可。`server_update` 鑄出的
revision 與 approvals/audit 依既有規則保留為歷史證據，不回收。
