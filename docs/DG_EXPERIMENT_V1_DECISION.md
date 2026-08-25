# DG-EXPERIMENT-V1 — `experiment_create_v2`（一 matrix 一 approval）決策閘

> Status: **awaiting ruling**
> 本文件是 review packet，不是核准紀錄。裁定後記入 `docs/DECISIONS.md`。
> 已裁定方向（`docs/DECISIONS.md` 2026-08-23 產品裁定、second-pass Part C）：
> Experiment 採**一 matrix 一 approval**——未來 `experiment_create_v2` kind，
> 不可變 payload 列全數 N 個 resolved run specs + Guard 上限，一次決定
> 原子 materialize N 個 plans/Jobs；且（C6）**必須是自帶契約的新 kind，
> 不是放寬既有 materialization 上界**。本 packet 把該方向落成可實作的
> 具名裁定。

## 1. 現況（證據）

- ExecutionPlan v2 已實作（default-off，`RUN_EXPERIENCE_V2_ENABLED` 鏈）：
  一 request → 一 approval + 一 immutable spec；approve →「一 plan 一
  Job」被三重釘死——schema `execution_plans.job_id UNIQUE`
  （`app/execution_attempt_schema.py:409`）、條件 UPDATE + rowcount 檢查
  （`app/execution_plan_v2_store.py:1884-1888`）、重放短路。C6 要求這些
  上界一條不放寬。
- `execution_plan_v2` 是 transaction-only kind（13 個之一，
  `app/db.py:390-406`；pinned test `tests/test_audit_adoption_gate.py:48`），
  只走 `POST /api/v2/approvals/{id}/decisions`。
- v2 resource requirements **強制 `exclusive_worker=true`**
  （`app/project_bootstrap.py:582`），且 resolve 時只要目標 server 有任何
  queued/running job 就拒絕（`_worker_is_exclusive`，
  `app/execution_plan_v2_store.py:594-613`）——N 個 run 排同一台目前
  **連建立都不可能**，這是 Experiment 必須裁定的接縫。
- 排程語意：一台 worker 同時最多一個 ordinary job
  （`app/monitor.py:107-137`）；N 個 queued job 自然一機一件分散
  （`app/scheduler.py:544-595`）。本 packet 不動排程。
- Run Template v2 已有 typed parameter schema（≤32 參數、五型別、
  canonical number 規則）與確定性 argv 編譯
  （`app/run_templates.py:318-365`）。
- `experiment_records` 是既有「低風險筆記」明文例外（INV-APPROVAL-1），
  純 metadata、無 FK、與 plans 零關聯——**不重用、不改動**。
- metrics-v1 已落地並在 pilot 驗證（DG-METRICS-CONTRACT，2026-08-24）；
  compare 現為嚴格雙邊（`runs_v2.py:477-524`）。

## 2. A 案正文

### EX-1 新 kind `experiment_create_v2`（transaction-only）

- Request（純函式驗證，`execution_plan_v2.py` 慣例）輸入：project、
  template/dataset/environment selection（**全 experiment 共用一組**，
  與單 run 相同的 selection 形狀）、**parameter matrix**（axes：
  `[{name, values[]}]`，每值過 `validate_value()`；固定 overrides 另計）、
  target 清單（1..M 台，明選）、guard 宣告。
- Resolver 對 matrix 做確定性展開（axes 依名稱排序、值序保留、
  cross product），對**每一個組合完整跑既有單 run resolver**
  （`_resolve_template`/`_resolve_datasets`/target 驗證），產出 N 份
  完整 `ExecutionPlanV2Spec`（同 project_version/environment/template/
  dataset bindings digest，僅 `parameter_values` 不同）；target 以
  round-robin 依序指派（確定性、可解釋、記錄於 spec）。
- Approval payload（不可變）：experiment contract version、project、
  matrix 原文、guard、**全數 N 個 plan digest 清單**與總數；任一組合
  resolve 失敗＝整個 request 失敗（fail-closed，不建 approval）。
- 永不自動核准（INV-APPROVAL-4 硬編碼天然排除；不動白名單、不建
  policy-scoped 機制——那是 DG-OPTIMIZATION-QUOTA 的事）。

### EX-2 Approve＝原子 materialize N（all-or-nothing）

- 單一 DB transaction：重驗（INV-APPROVAL-3：對**每個** plan 重跑既有
  dataset/instance/policy 重驗）→ 建 N 個 `execution_plans` 列 + N 個
  Jobs + N 筆 durable audit。任一項失敗＝整筆 decision 拒絕、零列落地
  （不存在部分 materialize 的中間態）。
- **一 plan 一 Job 上界一條不動**：experiment 是新的容器層，每個成員
  plan 仍走既有三重釘死；不修改任何既有 UNIQUE/guard。

### EX-3 Exclusivity 語意（batch-aware，最小修訂）

`_worker_is_exclusive` 對**外部**工作維持原判（任何非本 experiment 的
queued/running job／active attempt → 拒絕），對**同一 experiment 內**
指派到同一 server 的成員互不視為衝突——它們天然由「一機一件」排程
串行執行。這是 exclusivity 檢查的 batch-aware 擴充（同 transaction 內
判定），不是放寬：外部干擾的隔離語意不變。

### EX-4 儲存（additive migration v14）

- `experiments(id, project_id, approval_id UNIQUE, matrix_json,
  guard_json, run_count, created_at)`。
- `experiment_plan_members(experiment_id, plan_id UNIQUE)`——
  **不動 `execution_plans` schema／triggers**，用 membership 表建關聯。
- 既有 `experiment_records`（筆記）完全不動、不混用。

### EX-5 Guard（確定性硬上限）

- `MAX_EXPERIMENT_RUNS = 32`（與 v2 各 32 上限同族）；matrix 展開超過
  即 request 時拒絕。
- payload/matrix byte 上限比照 `execution_plan_v2.py:29-34` 慣例。
- guard 宣告中的 est. GPU hours／storage 為**展示性宣告**（進 payload、
  進核准卡，供人審），V1 不做資源推估引擎；Total Runs 與 target 清單
  是硬性項。

### EX-6 讀取面（M3 dashboard 最小核心）

- `GET /api/v2/experiments`（project scope）＋
  `GET /api/v2/experiments/{id}`：成員 run 投影（重用 product run store
  + `metrics_status` + metrics 摘要）——Run × Params × Status × Metrics
  表格資料源。
- Compare 維持既有雙邊；N-way compare 延後。

### EX-7 旗標與邊界

- `EXPERIMENT_V2_ENABLED=false` 預設關閉；依賴鏈要求
  `RUN_EXPERIENCE_V2_ENABLED`（沿用 `app/settings/model.py:95-108`
  模式）。
- LLM/agent 工具**零擴張**：V1 不給任何 agent「建 experiment 卡」
  工具（INV-LLM-1 上限之內都不開，另案裁定）。
- 計數閘依文件化流程更新：`TRANSACTION_ONLY_APPROVAL_KINDS` 13→14
  pinned set、routes/openapi/authorization、durable audit catalog。

## 3. 選項

- **A（推薦）**：如 §2——新 kind + 原子 all-or-nothing + batch-aware
  exclusivity + membership 表 + 32 上限 + 最小 dashboard 投影，
  default-off。
- **B — UI 迴圈建 N 張單 run 卡**：無新 kind，但 N 次點擊、無原子性、
  無 experiment 容器、且同 server 的組合仍被 exclusivity 擋死——
  與已裁定方向（一 matrix 一 approval）直接矛盾。不推薦。
- **C — 延後**：M3 Dashboard 與 M4 迴圈繼續無 Experiment 層。

## 4. A 案分項確認（可個別改裁）

EX-1 kind 與 request 契約／EX-2 原子 materialize／EX-3 batch-aware
exclusivity／EX-4 membership 表／EX-5 guard 上限（32）／EX-6 讀取面／
EX-7 旗標。**BLOCKED 條件**：任何既有 materialization 上界需要放寬、
需要動排程語意（一機一件）、或 exclusivity 無法以 batch-aware 方式
fail-closed 實現。

## 5. 裁定模板

```text
DG-EXPERIMENT-V1：A 核准（EX-1…EX-7）/ 局部修改（註明 EX-n）/ B / C
```
