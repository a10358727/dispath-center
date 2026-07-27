# Next Implementation Plan — Reliable Codex-to-Compute Control Plane

> 日期：2026-07-27
>
> 狀態：**Phase 0、Phase 1（WP-1A/1B/1C）與 Phase 2 的 WP-2A/2B 已完成；
> `DG-EXEC-ATTEMPT-v1`、`DG-AMBIGUOUS-LAUNCH-v1` 與 `DG-EXEC-ATTEMPT-v1.1`
> 均已於 2026-07-27 核准；下一個 execution package 為 WP-2C（唯一能解除
> `RB-LAUNCH-001` 的工作包），未啟用任何 production flag**
>
> 目的：把目前已存在的 SSH 主控、Codex Runner、Project/Job 基礎與
> Node Agent protocol primitives，收斂成一條可重現、可復原、可逐台回退的
> 「Codex 改碼 → 固定版本 → 主控派工 → 運算伺服器執行 → 回收結果」閉環。
>
> 2026-07-27 第二次架構檢查已納入：operation-level authorization、
> immutable server-config revision、atomic SSH launch claim、真正 immutable
> DatasetSnapshot publish、Node 重啟取回、staged credential rotation，以及
> 「例行退役」與「緊急安全撤權」的分流。
>
> 實作完成證據與未完成 gate 持續記錄於
> `docs/IMPLEMENTATION_PROGRESS.md`；不得只修改本計畫的勾選狀態來宣稱完成。

本文件不取代 canonical invariants、`PLAN.md` 或既有決策紀錄。發生衝突時，
優先順序仍是：

1. `.claude/skills/dispatcher-domain/references/invariants.md`
2. `docs/DECISIONS.md`
3. 已實作程式與邊界測試
4. 本文件

---

## 1. 執行摘要

目前系統不是空殼：

- 單一 FastAPI/SQLite 主控、approval、audit、monitor、scheduler、SSH/SFTP/
  tmux/sentinel、result rsync 已形成可用的內部 MVP。
- Codex Runner 可用單回合 `codex exec` 在隔離 worktree 修改程式並產生
  Git commit/bundle。
- Project、ProjectVersion、Run Profile、Dataset、Engineering Task 與
  Node protocol 都已有部分資料模型。

但目前不能宣稱完整閉環：

- SSH launch 回應不確定時仍可能 requeue，存在重複執行窗口。
- 沒有跨 SSH/Node 的 durable execution-attempt 真相。
- Node Agent 沒有可執行 daemon，`python -m agent` 目前無法啟動。
- Node poll 由 agent 提供任意 `job_id`，不是主控選下一份合法工作。
- Node terminal 只完成 `node_attempts`，不會收斂 canonical Job lifecycle。
- 普通 run 尚未統一固定 ProjectVersion、Run Profile revision、Dataset snapshot
  與完整 ExecutionPlan。
- Python 3.10 exact-lock、TestClient、offline CI、backup/restore smoke 與
  capability truth baseline 已由 WP-0A/0B 建立；GitHub-hosted CI 尚待
  commit/push 後取得第一次 remote run evidence。

因此後續固定順序為：

```text
可信測試基線
  → Durable ExecutionAttempt / outbox（shadow）
  → SSH production cutover + 不確定性修正
  → Immutable Project/Codex ExecutionPlan（先走 SSH）
  → Node Agent v2 dark launch
  → 兩節點實地 canary
  → Production operations / authorization enforcement
```

在 Phase 5 canary 通過前：

- production worker 一律維持 SSH；
- `NODE_AGENT_V1_ENABLED` 維持關閉；
- Codex Runner 不遷移到 Node；
- 不增加 GPU sharing、preemption、multi-job-per-node 或 quota 語意。

---

## 2. 最終使用者成果與 Definition of Done

使用者從單一 Project 頁或 API 完成以下流程：

1. 選擇或要求 Codex 修改一個已知 ProjectVersion。
2. Codex Runner 產生可檢查的 diff 與 immutable commit/bundle。
3. 人類核准 promotion；Codex/LLM 不得自行核准。
4. 選擇 exact Run Profile revision、Dataset snapshot 與 resource request。
5. 主控產生 immutable JobSpec/ExecutionPlan，顯示 target、backend 與 staging。
6. 核准時重驗 plan；payload/digest 不一致或過期時拒絕，絕不偷換內容。
7. Scheduler 以 durable attempt 將工作交給 SSH 或已通過 canary 的 Node。
8. 任一 control-plane/worker 重啟、SSH timeout 或 agent 失聯，都不會重複啟動；
   unknown 保持 unknown，直到證據或人工處置使狀態收斂。
9. log、terminal status、artifact metadata 與實際 result collection 可追到
   ProjectVersion、Dataset snapshot、Plan、Job、Attempt 與 worker。
10. 每台 Node 可單獨退回 SSH，既有 attempt 仍由原 backend 收斂。

可先交付的核心產品里程碑是 **Phase 3 完成**：即使 Node 尚未提升，使用者也能
透過既有 SSH 通道完成可追溯的「Codex 改碼 → 人工核准 → 多伺服器執行 →
結果回收」。

整體計畫完成門檻：

- 同一 approved payload 永遠不產生兩個並行副作用。
- 完整 full suite、static invariant gate、migration suite 在乾淨環境全綠。
- legacy SSH exact-string golden fixtures 保留；attempt-driven SSH 另建版本化
  exact fixtures，兩者都維持 `INV-SSH-1…9` 的安全與終態語意。
- Node 通過至少 2 nodes、100 jobs、連續 7 天的實地 gate。
- 完成一次 control-plane restart、agent restart、網路中斷及逐台 SSH rollback
  演練。
- 完成 restore drill，並記錄實際 RPO/RTO。

---

## 3. 固定目標架構

```text
Web / API / LLM / MCP
          │
          ▼
Identity / Authorization / Approval Gateway
          │
          ▼
ProjectVersion + DatasetSnapshot + RunProfile
          │
          ▼
Immutable JobSpec / ExecutionPlan
          │
          ▼
Job + Durable ExecutionAttempt + Operation Outbox
          │
          ▼
Single Scheduler/Reconciler Leader
       ┌──┴──────────────────────┐
       │                         │
SSHExecutionBackend       NodeExecutionBackend
現行相容/緊急通道         逐台啟用、agent 出站 pull
       │                         │
       └────── Compute Workers ──┘
                    │
          Logs / Artifacts / Results
                    │
             Project Run Timeline

Codex Runner Pool（先維持 SSH）
          │
          └── immutable commit/bundle ──> ProjectVersion
```

架構決定：

- `jobs` 保留現行封閉狀態值；transport 的 `dispatching/unknown/leased` 等狀態
  只存在新的 `execution_attempts`。
- `execution_attempts` 是 durable execution identity；`execution_operations`
  是 prepare/launch/inspect/stop/collect/cleanup 的 durable outbox。兩者都以
  SQLite 為真相，不另建記憶體 queue。
- 每個會產生 material side effect 的 operation 都固定自己的 authorization
  approval/contract digest；原 execution approval 不能被拿來冒充後續 stop
  approval。
- backend 在 attempt 建立時固定；attempt 未終態前不得漂移或 fallback。
- attempt 同時固定 immutable `server_config_revision` 與 target identity digest；
  新 assignment 才讀 current servers.yaml，既有 attempt 不受後續 config
  update/delete 漂移。
- SSH 永久保留；Node 是 per-server 提升，不是全域切換。
- 第一版仍可維持單一 Server A 與 SQLite；先建立 single-leader/fencing，
  不在本計畫內直接改成微服務、PostgreSQL 或 Kubernetes。
- 第一版仍維持一台 ordinary worker 同時只執行一個 Job；`unknown` 屬於
  attempt 的觀測狀態，不新增 `jobs.status=unknown`。
- rollout flag 必須分開控制「停止建立新 attempt」與「允許既有 attempt
  heartbeat/terminal/drain」；回退不能讓 active work 失去 owner。
- Node 的例行退役必須先 drain；credential 洩漏等安全事件則可立即撤銷單一
  node，即使仍有 active attempt。緊急撤權不代表 attempt failed，也不授權
  SSH relaunch。
- Codex Runner 與 compute worker 保持角色分離，普通 Node canary 通過後才討論
  Runner 遷移。
- Authorization 仍為 off/shadow 時，只能作 private/internal controlled rollout；
  Phase 3 MVP 不等於可開放多人 production。

---

## 4. 全計畫保護邊界

除第 5 節明列、等待對應 phase 修正的 legacy defects 外，每個新增或切換後的
行為都必須符合：

- 所有 material mutation 仍走 approval；LLM/MCP 只能建立 pending request。
- durable outbox 不能擴大 approval 權限：`prepare/launch/collect` 只能在原
  approved execution contract 明列的範圍內執行；`stop` 必須綁
  `kind=stop`；會刪除遠端資料的 `cleanup` 必須綁獨立 cleanup/retention
  approval。純 `inspect` 才可不帶 mutation approval。
- Approved command/plan bytes 與 digest 永遠不可修改；重試建立新 attempt，
  不改寫原 approval。
- 新 execution approval 必須保存 canonical payload SHA-256；現有可任意更新
  approval payload 的通用 DB 介面，必須在接上正式 ExecutionPlan 前封住。
- 危險指令 request-time 拒絕、approve-time 重驗。
- DB 先記 intent/attempt，再允許任何遠端副作用。
- unreachable、heartbeat stale、launch response lost 均不得自動判 failed；
  新 attempt path 也不得因 response lost 自動重派。
- 使用者 command bytes 仍以檔案落地，不插值進 launcher shell string。
- SSH backend 不得依賴 agent，且現有 SFTP/tmux/sentinel/timeout 行為保留。
- Node Agent 非 root、只出站 authenticated HTTPS、不開入站控制埠。
- 一個 active attempt 只能屬於一個 backend 與一個 server/node。
- server disable 只禁止新 assignment；active attempt 仍以 immutable config
  revision reconcile。server update/delete 不得讓 active generic/legacy
  attempt 或已登記 Node 失去可解析的 target identity。
- stop request、kill delivery ack 或連線失敗都不是 terminal evidence；
  在既有 Job state invariant 下，running Job 只能由 sentinel/agent terminal
  收斂為 done/failed。若要支援 `running → cancelled`，必須先通過
  `DG-JOB-STATE`。
- result collection failure 與 workload execution status 分離。
- schema 一律 additive；舊 row 缺 revision/hash 時明確標 legacy/unknown，
  不得補造歷史事實。
- tests 不碰 production DB、servers.yaml、audit、credential、worker 或網路。

以下事項不在 implementation slice 中順手改動：

- Job state value/transition 集合
- `maybe_auto_approve()` 的 `enqueue|stop` 白名單
- SSH host-key policy
- GPU slot allocation、single-node 多 job、preemption、quota
- authorization `enforce`
- Codex app-server production activation
- GitHub publication production adapter

需要時必須先走第 12 節的 named decision gate。

---

## 5. Phase 0 — Reproducible Baseline and Capability Truth

### 5.1 目標

建立下一輪所有實作可相信的測試與現況基線。Phase 0 不改 production behavior。

### 5.2 實作切片

#### Slice 0A — Dependency/test baseline

- 以 Python 3.10 建立全新 disposable venv，不沿用已搬移且 shebang 失效的 `.venv`。
- 保留 top-level dependency manifest，另產生 exact transitive runtime/dev lock。
- 固定一組可正常進出 FastAPI `TestClient` 的 FastAPI/Starlette/http client/
  AnyIO/pytest 組合。
- 增加 `pyproject.toml` 或等價 pytest 設定，固定 asyncio/test discovery 行為。
- 記錄 Python 與 dependency versions，不以目前機器 site-packages 作為基線。

#### Slice 0B — Automated release gate

- 新增 CI：clean install → collect → static checks → migration/core suites →
  full `pytest -q`。
- CI 禁止網路與真實 credential；所有 runtime paths 指向 temp directory。
- 預留 Node package smoke target；Phase 0 只驗現有 primitives 可 import，
  不把尚不存在的 `python -m agent` 當成已完成。Phase 4 才將
  `python -m agent --check` 轉為必過 gate。
- 加入 backup/restore script smoke test，資料全部使用 temp fixture。

#### Slice 0C — Capability ledger

- 新增單一 capability ledger，欄位固定為：
  `implemented / test-only / default-enabled / deployed / canary-proven /
  production-ready`。
- 更正過期文件標頭與矛盾敘述；舊草稿加 `superseded_by`，不刪歷史。
- Node 現況必須標為 protocol primitives，而非 runnable/canary-ready backend。

#### Slice 0D — Known invariant drift

- 把現行 stop 路徑的已知缺陷列為 release blocker：remote kill 失敗或 unreachable
  時仍可能將 running Job 標為 cancelled，與 `INV-SSH-9` /
  `INV-STATE-4` 衝突（從 `app/approvals.py` 的 stop approval 分支開始修）。
- 補 characterization/regression tests，固定「stop request 不等於 terminal」；
  實際行為修正在 Phase 1 完成。
- 將目前 `POST /datasets` 直接 `insert_dataset()` 列為待裁定的 material
  mutation drift；它不在 `INV-APPROVAL-1` 的封閉例外中。Phase 0 先建立
  characterization 與 capability ledger 記錄，Phase 3 的 immutable snapshot
  publish 不得沿用這條直接寫入路徑。
- 為現行 server disable/delete 建立 characterization：目前 running Job 與
  queued pin 的保護不等於未來 generic/legacy attempt 與 Node binding 保護。
  Phase 1 server-config revision/ownership guard 完成前，不得宣稱刪除操作對
  attempt-driven backend 安全。
- 權威文件順序固定為 canonical invariants → `DECISIONS.md` → 程式與測試證據
  → roadmap/status 文件；不能讓舊文件的 `code ready` 覆蓋實際證據。

### 5.3 驗收

- 在空環境單一命令可重建 venv 並跑完整 suite。
- 最小 FastAPI `TestClient` smoke 不 hang。
- 全套測試乾淨結束，不接受「大量 pass + 1 個已知 failure」作 release green。
- static invariant gate PASS。
- CI 每次 commit 自動執行。
- `git status` 不出現 runtime DB/audit/config 污染。

### 5.4 Rollback

只移除 CI/lock/test setup；不涉及 runtime schema 或 worker。

---

## 6. Phase 1 — Durable ExecutionAttempt / Outbox（Shadow）

### 6.1 目標

先建立跨重啟、跨 backend 的 execution identity 與副作用意圖；attempt/outbox
以 shadow mode 驗證，不立即替換現行 SSH scheduler。第 6.5 節 stop fix 是
獨立的 canonical defect correction，不算 shadow parity。

### 6.2 Additive data model

`execution_attempts`：

```text
id                       UUID / opaque attempt id
job_id                   canonical Job id
attempt_number           per-job monotonic integer
backend                  ssh | node
server_name              exact selected target
server_config_revision_id immutable normalized target/config reference
target_identity_sha256   backend/host/port/user/root-sets/key-ref identity digest
execution_approval_id    NOT NULL for every new generic attempt
approved_payload_sha256  NOT NULL for every new generic attempt
execution_contract_version NOT NULL for every new generic attempt
state                    leased | dispatching | running
                         | done | failed
                         | expired | abandoned_before_launch
liveness                 known | unknown
fencing_token            opaque ownership token
recovery_hold_reason     nullable; security_credential_revoked /
                         manual_recovery_review
created_at / lease_expires_at
dispatch_intent_at / acknowledged_at / last_observed_at
stop_requested_at / stop_acknowledged_at
terminal_at / exit_code
last_error_category / sanitized_error_detail
```

shadow parity 不寫入上述 canonical active rows；另用 append-only
`execution_shadow_observations`，避免測量本身取得 ownership 或阻擋舊 scheduler。

`execution_operations`（durable outbox）：

```text
id / attempt_id
operation                prepare | launch | inspect | stop | collect | cleanup
idempotency_key
authorization_approval_id nullable only for inspect
authorization_class      execution | inspect | stop | cleanup
authorized_contract_sha256
payload_json / payload_sha256
state                    pending | processing | delivered | uncertain | failed
claim_owner / claim_fencing_epoch / claim_expires_at / retry_at
effect_started_at
created_at / updated_at / last_error_category
```

精確 decision contract 採更安全的窄化：legacy execution 不補造 generic
attempt/outbox，所以 material operation 不存在 nullable legacy authorization
例外；只有 pure read-only `inspect` 的 authorization reference/digest 可為
NULL。既有 legacy SSH/Node 繼續走原路徑，直到用新 pinned contract 明確建立。

operation authorization 規則：

- `prepare/launch/collect` 可引用原 execution approval，但只有在 immutable
  execution contract 明列該 operation、target、paths 與 artifact scope 時。
- `stop` 一律引用已核准的 `kind=stop` approval 與該 stop payload digest；
  enqueue/execution approval 不具停止權限。
- 會刪除 remote bytes、workspace、receipt 或 artifact 的 `cleanup` 使用獨立
  approval kind/retention contract；不得以「attempt 已 terminal」推導刪除權限。
- `inspect` 必須是 pure read-only，才可沒有 mutation approval。
- worker claim 後、任何副作用前，必須重新驗 approval 狀態、kind、attempt、
  target 與 contract digest；不符即 fail closed，不得更新原 operation payload。

新增 additive `server_config_revisions`：

```text
id / server_name / revision
normalized_target_json   backend / host / port / user /
                         project_roots / dataset_roots
credential_ref_json      version id / real path / stat-only file identity
target_identity_sha256
assignment_eligibility / publication_state
created_by_approval_id / created_at / activated_at / retired_at
```

- revision immutable，只保存必要的非秘密 target identity 與受管 credential
  version reference，不把 private key/token bytes 複製進 DB、audit 或 payload。
- 初始 file provider 只可用 `realpath/stat` 固定版本化檔案 reference；建立
  revision 時不得開啟或 hash 私鑰內容。exact working directory 由 execution
  contract/operation payload 固定，且必須落在 revision 的 root set 內。
- material server config 核准後建立新 revision；不得就地改寫舊 revision。
- 新 attempt 從當下 effective config 選 revision；既有 attempt 永遠解析自己
  固定的 revision，不回頭讀 current servers.yaml。
- 例行 key/config rotation 必須保留 active revision 所需的 credential reference
  到 active=0；安全事件可立即停用 credential，但 attempt 只設
  `liveness=unknown` 與 `recovery_hold_reason=security_revoke`，不新增 Job/
  Attempt state、不改 target、不 fallback、不判 failed。

另加 `server_config_mutations` publication journal，把 approval、before/after
YAML digest、prepared revision 與 `intent → yaml_applied → activated`（或 exact
rollback/recovery hold）固定下來。YAML atomic replace 與 in-memory reload
成功後，最後一個 DB transaction 才同時切 active revision、完成 journal 與
approval；unresolved journal 期間 legacy/generic scheduler 都不得對該 server
新派工。

另加 append-only `execution_attempt_events`，保存狀態 CAS、遠端 receipt 與
reconcile evidence。它是稽核證據，不取代 canonical audit。

資料庫約束：

- `UNIQUE(job_id, attempt_number)`。
- partial unique index 保證每個 Job 最多一個 active attempt；不能只靠 Python
  的 read-then-write。
- attempt 建立後，`job_id/backend/server_name/server_config_revision_id/
  target identity/execution approval/payload digest/fencing_token` immutable。
- outbox `idempotency_key` 唯一；worker claim 使用有期限的 DB lease。
- material operation 的 authorization reference/kind/contract digest 建立後
  immutable；資料庫 CHECK/foreign key 加上 service-layer revalidation。
- `node_attempts` 保留為 protocol-specific extension，新增 nullable
  `execution_attempt_id`；既有列不猜測 backfill。
- eligibility/rollback 使用 dual-read ownership guard：沒有 generic parent
  的 legacy 未過期 `leased`，以及 `acked|running` Node row，仍阻擋 SSH 與
  新 attempt。只有來源事實完整時才可連結。
- `jobs` 增加 nullable execution approval/digest/version/role/command-digest
  linkage，不改 status 值域；legacy 列維持 NULL。
- server disable 只影響新 assignment；server delete、host/user/port/work-root
  repoint、backend 改動與 credential retirement 必須 dual-read generic active
  attempts、未連結的 legacy Node lease/ack，以及 enrolled Node binding。若會
  破壞既有 attempt 的 target resolution 就拒絕或延後；不能只查 Job status。

Job/Attempt 對應固定為：

| Attempt | Canonical Job |
|---|---|
| leased、尚未 ack/dispatch intent | queued |
| dispatching / running，無論 liveness known/unknown | running |
| done | done |
| failed | failed |
| expired（未 ack）/ abandoned_before_launch（可證明未啟動 workload） | queued |

`jobs.status=cancelled` 只保留既有的 `queued → cancelled`。attempt 不以
`cancelled` 逃避 terminal evidence；重試必須建立新 attempt。
`leased` 供 Node 的 pre-ack 階段；SSH 在第一個遠端 prepare 前即以
dispatching attempt 將 Job 標 running，以符合 `INV-STATE-2`。

### 6.3 Immutable execution payload

- 新 execution-bearing approval 在建立時保存 canonical payload bytes/JSON 與
  SHA-256；attempt 引用該 digest，不從可變 `jobs` 欄位重新拼出命令。
- digest 至少涵蓋每個 stable Job role 的 command bytes、允許的 operations、
  project/version、dataset/profile、resource 與 placement constraints。exact
  target/revision 只能在核准範圍內於 attempt 建立時選定，再由 attempt
  immutable 固定。
- DB 自增 approval ID 不嵌入自己的 payload hash；所有 downstream truth 以
  `(approval_id, payload_sha256, payload_contract_version)` 不可變 tuple 綁定，
  避免循環 insert/hash 相依。
- approve-time revalidation 只能接受或拒絕 stale request，不能重寫 payload。
  內容要變時，建立新 approval/spec。
- 現有通用 approval update 路徑必須限制 material payload 更新；歷史 row 保留
  原 bytes，新 hash 欄位可為 NULL 並標 `legacy_unpinned`，不得補造。

### 6.4 Shadow workflow

1. 舊 scheduler 仍執行 SSH；shadow writer 記錄它預期會建立的
   attempt/operation transitions。
2. 每次 tick 對照 Job、selected server、command digest、status transition。
3. shadow writer 不呼叫遠端、不影響 eligibility、不取得 production ownership。
4. migration 同時覆蓋 fresh DB 與代表性 legacy DB。
5. parity 穩定後，才提出 Phase 2 production cutover。

### 6.5 Stop invariant correction

這是 Node cutover 前的 release blocker，且不等待 `DG-JOB-STATE`：

- approved stop 只保存 stop intent 並呼叫 backend stop。
- kill 失敗、timeout 或 target unreachable 時，Job 保持 running；attempt
  liveness 可為 unknown，不能標 cancelled/failed。
- kill 成功仍等待 SSH sentinel 或日後 Node terminal report 收斂 done/failed。
- persisted stop intent 會阻擋「tmux gone + no sentinel」的自動 requeue，
  避免剛停止的 workload 被 scheduler 再啟動；Phase 2 再讓 versioned wrapper
  對 approved stop 可靠產生 matching terminal sentinel。
- finish hook/audit 只在第一個有效 terminal CAS 觸發一次。
- 若產品需要 `running → cancelled`，先通過 `DG-JOB-STATE`；本修正不偷偷新增。

### 6.6 Acceptance / rollback

- 兩個 DB connection、scheduler thread/process 同時競爭時只有一個 active
  attempt。
- transaction commit 前/後 crash 均可從 DB 重建 ownership。
- payload/digest tamper 在接觸 worker 前 fail closed。
- stop operation 引用 execution approval、cleanup operation 缺授權、approval
  kind/digest/target 不符，均為零 remote side effect。
- active attempt 存在時 server delete/repoint/credential routine retirement
  fail closed；server disable 後既有 attempt 仍可由原 revision reconcile。
- stop request、kill failure、unreachable 測試都不產生假 terminal。
- 除已知違反 invariant 的 stop terminal 行為外，shadow observations 與現行
  SSH lifecycle 逐筆 parity；full/static/migration suite 全綠。
- rollback 關閉 shadow/new-claim flag 即可；schema/evidence 不刪除。
- stop correction 永久保留；rollback 不得恢復已知違反
  `INV-SSH-9`/`INV-STATE-4` 的行為。
- legacy scheduler 從本階段起永久檢查 generic active attempt，避免未來回退時
  重派可能已產生副作用的工作；同時 dual-read 尚未連結的 legacy Node attempt。

---

## 7. Phase 2 — SSH Production Cutover and Ambiguous Launch

### 7.1 目標

讓目前真正可用的 SSH backend 改由 attempt/outbox 驅動，消除「tmux 已啟動，
但 SSH response lost 後 Job 被放回 queued」的雙跑窗口。

前置條件：Phase 1 全綠。開始 remote receipt/cutover（WP-2B/2C）前，
`DG-AMBIGUOUS-LAUNCH` 必須提交並核准 canonical `INV-STATE-2` 的精確文字
修訂，不能只用實作註解「澄清」。最低語意固定為：

- 只有 transport/launcher evidence 能證明 request 未被接受、workload 不可能
  已啟動的 definite pre-launch failure，才可 abandon attempt 並
  `running → queued`。
- request 可能已到達 target 但 response lost/timeout，不屬於 definite dispatch
  failure；Attempt 保持原 target/backend、liveness unknown，Job 保持 running。
- 只能用同一 attempt/idempotency key 對同一 target reconcile/replay；禁止建立
  第二 attempt 或改派。

### 7.2 Attempt-driven SSH flow

1. scheduler 在同一 DB transaction 重驗 eligibility、固定 server/backend、
   建立 dispatching attempt + prepare outbox，並將 Job `queued → running`；
   commit 前不做任何 mkdir/SFTP/SSH。
2. outbox worker 才執行 `prepare`。它雖可冪等重試，仍是遠端副作用，因此
   必須排在上述 DB intent 之後；若可證明 launch 從未被嘗試，prepare 最終
   失敗可標 `abandoned_before_launch` 並將 Job `running → queued`。
3. `launch` 使用 attempt id/fencing token 作 idempotency key，只能送到原
   target。
4. 遠端固定 launcher 在 `agent_jobs/{job_id}` 保存 attempt receipt/marker；
   attempt id/token 也只經 SFTP 檔案落地，launcher command 仍只插值已驗證的
   integer Job id。
5. launcher 在同一 filesystem 先以 bash-only atomic primitive（例如
   `mkdir` claim directory 或 noclobber create）競爭
   `attempt_id + fencing_token` 的 launch claim；禁止 read-then-launch。
   - 只有 claim winner 可建立 tmux。
   - loser 只可讀 matching receipt/evidence，不可再 launch。
   - claim 已存在但 receipt/tmux/sentinel 無法證明狀態時，回 unknown；
     不移除 claim、不盲目重試。
   - receipt 以 temp file + atomic rename 發布，內容綁 attempt/token/session；
     crash 發生在 claim、tmux、receipt 任兩步之間都不得產生第二次 launch。
   versioned fixed wrapper 必須以 bash trap/finalizer 讓 approved stop 也寫出
   token-matching numeric exit sentinel，不能由 controller 偽造。
6. 回應成功後 attempt running；timeout/connection loss 則 liveness unknown、
   operation uncertain，Job 保持 running。
7. reconciler 只能在同一 server 以 attempt receipt、tmux 與 exit-code sentinel
   查證，且在採信 tmux/sentinel 前先驗 companion attempt/fencing token；
   restart 後一律由 DB/outbox 繼續。

terminal 規則：

- exit-code sentinel 是 done/failed 的唯一 workload terminal 證據；每次讀取
  receipt/sentinel 都必須同時驗證 companion attempt/fencing token，舊 attempt
  或 token 不符的檔案一律不採信。
- tmux/receipt 只能證明 launch/running，不能證明成功或失敗。
- target unreachable 不改 Job terminal status。
- 「沒有看到 tmux/sentinel」本身不足以 requeue；只有可證明 launch 從未被
  接受時才可 `abandoned_before_launch`。
- launch uncertain 時可以用相同 attempt/idempotency key 對同一 target
  reconcile/replay，絕不建立第二 attempt 或改派其他 server。

### 7.3 Backend convergence

- `prepare/launch/inspect/stop/collect/cleanup` 全部經 `ExecutionBackend`，
  不再由 scheduler、approval handler、finish hook 直呼 SSH primitives。
- backend 與 connection target 依 persisted attempt 的
  `server_config_revision_id` 選擇，不依後來修改的 servers.yaml 漂移；current
  config 的 enabled/backend 只約束新 assignment。
- legacy SSH exact golden fixtures 保留；attempt-driven launcher 建立版本化
  exact golden。兩者都維持 command 只經 SFTP、無自由文字插值、pure
  `build_*`、bash/tmux 依賴封頂與 sentinel-only terminal。
- collection 是獨立 outbox operation；失敗只標 collection 狀態，不回寫
  workload failed。
- remote job directory/evidence 依 retention policy 處理，不因成功自動刪除。

### 7.4 Minimum scheduler ownership / telemetry

attempt-driven production flag 開啟前：

- 增加 SQLite-backed scheduler leader lease/fencing；同一時間只有一個 owner
  可建立新 attempt。outbox operation 另以自己的短 lease/CAS 防重。
- `role=all` 可維持單 process，但第二個 scheduler owner 必須 fail closed，
  不能因多個 API worker 各自啟動 background loops。
- 提供 canary 所需最小 metrics/health：scheduler/reconciler last-success、
  outbox backlog/uncertain age、attempt by state/backend、queue age、collection
  狀態與 duplicate prevention count。
- Phase 6 再完成 API/scheduler 角色拆分、leader takeover drill 與完整告警，
  不是到 Phase 6 才首次建立 ownership。

### 7.5 Rollout / rollback

- flags 分為 `attempt_new_assignment`、`legacy_ssh_new_assignment` 與
  `attempt_drain_reconcile`。
- 先在一台 designated non-production SSH worker 執行至少 20 jobs/24 小時，
  並完成一次 response-lost/restart 與 rollback drill。
- rollback 先停止新 attempt；既有 attempt 的 reconcile/terminal/collect
  永遠保持開啟，再只對安全的新 Job 開啟 legacy SSH assignment。
- legacy path 僅可處理沒有 generic active attempt、也沒有 dual-read
  legacy Node ownership 的 Job。
- uncertain attempt 不得因 rollback 被 legacy scheduler 重派。
- canary 關閉條件：零 duplicate/false failure/lost terminal、所有 20 jobs
  terminal 收斂、result collection 成功率 100%、unresolved unknown=0。

### 7.6 Acceptance

fake crash matrix 至少涵蓋：

- claim transaction commit 前/後
- outbox claim 前/後
- prepare 中斷
- atomic remote claim 前/後、tmux 成功但 receipt 尚未發布
- duplicate launcher 同時競爭與 stale/mismatched claim
- remote tmux 成功但 response lost
- control-plane restart
- worker unreachable/reconnect
- sentinel 已寫但 DB 尚未收斂
- stop delivery fail/success
- collect 中斷與重試

每個案例必須符合：零 duplicate launch、unreachable 不是假 failure、
active backend 不漂移、terminal hook exactly once、result collection 不污染
execution result。legacy SSH exact golden、新 attempt versioned golden、approval、
jobqueue、scheduler、migration、full suite 必須全綠；rollback drill 必須證明
新 queued Job 可回 legacy SSH，而 active/uncertain attempt 仍由新路徑 drain。

---

## 8. Phase 3 — Immutable Codex-to-Compute Flow（SSH First）

### 8.1 目標

先交付使用者核心價值：Codex 產生可驗證 code artifact，人類核准 exact plan，
再由主控透過成熟 SSH backend 分派到不同運算伺服器。不讓未完成的 Node daemon
阻塞此閉環。

### 8.2 延伸現有 domain model

一般化現有 `engineering_validation_requests` 的 snapshot/hash/bundle 骨架，
不可另造一條互不相容的 Codex 流程。

```text
DatasetSnapshot
  id / dataset / version / state
  manifest_digest / artifact_store_revision / shard_count / total_size
  source_candidate_digest / verification_level
  build_approval_id / published_at / created_at

JobSpec（immutable）
  project_id / base_project_version_id / result_project_version_id
  result_commit / changes_bundle_digest+size
  exact_run_profile_revision / dataset_snapshot_ids
  setup+command bytes / resources / output contract / spec_digest

ExecutionPlan（immutable）
  job_spec_id / target_server / backend / staging_steps
  observed inputs+freshness / approval identity / plan_digest / created_at

Run
  project_id / execution_plan_id / approval_id / job_id / current_attempt_id
```

規則：

- managed Project 固定 exact ProjectVersion/commit。
- Codex 產物固定 `result_commit` 與 verified bundle digest/size。
- Run Profile 引用 exact immutable revision，不讀會漂移的 head。
- verified DatasetSnapshot 或明確 `none` 才可宣稱 reproducible；
  legacy 資料標 `legacy_unverified`，不得假造 hash。
- approval 等待期間 target/profile/dataset/backend/command 任一 material drift，
  原 request stale/reject；不得更新原 plan。
- staging Job 與 downstream compute Job 都引用同一 plan/digest lineage。

### 8.3 Minimum DatasetSnapshot workflow

Phase 3 不只新增 schema，還要完成 `PLAN.md` 定義的最小 local
ArtifactStore、content-addressed bytes 與 atomic publish。只有計算 digest 而
仍引用可被就地改寫的 source directory，不構成 immutable snapshot。

ArtifactStore 第一版固定使用 Server A local filesystem，路徑與介面保留未來
S3-compatible adapter，但本 phase 不接 production object store：

```text
dataset_store/blobs/sha256/<shard-digest>.tar
dataset_store/manifests/<snapshot-id>.jsonl[.zst]
dataset_store/snapshots/<snapshot-id>.json
dataset_store/staging/<build-id>/...
```

publish workflow：

1. 對已登記 Dataset/version 做 read-only candidate inventory，建立排序固定的
   streaming manifest；每個檔案保存 relative path、size 與 content digest，
   或引用已驗證且可取得的 external manifest。只用 file count/total size
   不得取得 `verified`。
2. canonicalize candidate manifest，計算 `source_candidate_digest`。來源不可讀、
   掃描中漂移或不完整時回 `verification_unknown/stale`，不能產生假 hash。
3. 建立 `dataset_snapshot_build`（或 gate 核准的等價 kind）pending approval，
   payload 固定 dataset/version/source identity/candidate digest、ArtifactStore
   revision、shard policy 與 publish target；LLM 只能建 request。
4. approve-time 重新驗 candidate digest。相符後才建立 `building` row，stream
   產生 deterministic tar shards、計算每 shard SHA-256，全部只寫 staging。
   任一 source drift、讀取或 hash failure 都中止且不得 publish。
5. 從 staging 重新驗 manifest、shard digest/size 與 snapshot metadata；以
   content-addressed rename/dedup 發布 blobs，最後原子發布 snapshot descriptor，
   再以 DB CAS `building → published`。`published` 後 descriptor、manifest、
   shard mapping 不可改寫。
6. Job/ExecutionPlan 只綁 `published` snapshot id、manifest digest 與 ArtifactStore
   revision；不得綁 mutable latest/source path。target staging 只傳缺少的 shards，
   在 final path 外解包驗證，再用 marker/atomic rename 發布 replica。
7. staging/approve-time 再驗 target 上的 exact snapshot binding；mismatch 在任何
   workload side effect 前 stale/reject。
8. manifest/shard corruption、missing file、source drift、legacy unknown、
   interrupted build/publish 都有 reason code、可重啟 reconcile 與測試；不完整
   staging 可由另行核准的 retention/cleanup operation 回收。

現行 `POST /datasets` 的 path+size/count manifest 只能作 legacy registry 輸入，
不可直接建立 `published` snapshot。Phase 3 開始前，必須由
`DG-DATASET-SNAPSHOT` 裁定將 registration 改成 request/approve flow，或核准
另一個明文且有界的例外；未裁定時只能使用 `dataset=none` 完成 reproducible
E2E。

Phase 3 的 reproducible E2E 只接受 verified snapshot 或 `dataset=none`；
legacy dataset 仍可依既有政策執行，但 API/UI 必須回
`reproducible=false`，不可通過 reproducible acceptance。

### 8.4 Planner / API

- `POST /projects/{name}/execution-plans/preview`
  - 純讀、無副作用；
  - 回 draft、reason codes、digest、missing prerequisites。
- `POST /projects/{name}/runs/request`
  - 由選定 inputs 重新推導；
  - persist immutable JobSpec/Plan；
  - 建立 pending approval，不執行。
- `GET /runs/{id}`
  - 顯示 Codex run、commit/bundle、plan、approval、Job、Attempts、backend、
    results/evidence。

approve-time 重驗 exact revisions、snapshot、target/server、backend、policy、
command digest 與危險指令；成功後才建立/連結 canonical Job。scheduler 不得
重新規劃 target。

### 8.5 Codex promotion

1. Engineering Task 固定 base ProjectVersion。
2. 現行單回合 `codex exec` 在隔離 worktree 產生 diff、commit/bundle/evidence。
3. 人類檢查後提出 promotion approval。
4. 核准後匯入 central hub，登記新的 immutable ProjectVersion。
5. 新 ProjectVersion 才可進 Planner/Run request。

`engineering_task_promote` 或等價 approval kind 必須先通過
`DG-CODE-PROMOTE`。在此之前只產 bundle，不得 direct hub write。

### 8.6 Acceptance / rollback

- Project 頁完成「Codex 修改 → review → promotion → preview → approval →
  SSH compute → result」fake/integration E2E。
- 覆蓋 Codex/test/secret/bundle corruption/stale profile/target/dataset/backend/
  offline/restart/collect retry。
- local ArtifactStore、content-addressed shards、immutable descriptor、atomic
  publish、manifest revalidation 與 corruption rejection 真正走完；
  `dataset=none` 與 legacy-unverified 顯示語意正確。
- snapshot build/publish 沒有 approval、candidate drift、publish crash、同 digest
  concurrent build、shard corruption 均 fail closed；published bytes/metadata
  不可就地修改。
- 核准畫面 plan hash 與 attempt 實際 payload 完全一致。
- 每個 result 可追溯 Codex run、bundle digest、commit、dataset、profile、
  server/backend、attempt。
- Codex/LLM 無 approve、promotion、direct SSH 能力。
- rollback 關閉新 planner；既有 approved/in-flight plan 仍由原 attempt 收斂，
  immutable rows 不刪不改。legacy raw enqueue 路徑保留但明確標示不可重現。

Phase 3 完成即達成第一個可交付的 Codex-to-compute MVP；後續 Node 只替換
transport，不改 ExecutionPlan 語意。

---

## 9. Phase 4 — Node Agent v2 End-to-End（Dark Launch）

### 9.1 目標

把現有 Node protocol primitives 補成可安裝、可重啟收斂的 outbound daemon，
接上共同 attempt contract；本 phase 不承接 production jobs。

### 9.2 Protocol v2

新增 `POST /node-agent/v2/lease-next`：

- request 不接受 `job_id`、server、command 或 target。
- authenticated node identity 唯一決定綁定 server。
- control plane 使用與 SSH 共用的 pure eligibility policy 選下一個 Job。
- response 固定 attempt id、job id、immutable payload、digest、lease expiry、
  protocol/capability version 與 stop state。
- 若該 node 已有 generic 或未連結 legacy active attempt，`lease-next` 不得建立
  新 lease；回 `active_attempt_exists`，agent 必須先走取回/收斂流程。

新增 `GET /node-agent/v2/attempts/current`：

- 只回 authenticated node 自己擁有的 active attempt，以及 exact payload、
  digest、ack/launch/stop state；不得接受 caller-supplied node/server/job id。
- daemon 每次啟動、斷線重連、ack response lost 時，必須先呼叫 current，再
  決定是否 lease-next。
- control plane 顯示 dispatching，但本機 durable journal 可證明同一 token
  仍為 `not_launched` 時，可繼續該 attempt 的第一次 launch；若 journal 遺失、
  token 不符或無法證明，標 unknown 並禁止 relaunch。
- current/heartbeat/terminal 屬於 existing-attempt protocol；關閉 new
  assignment 時仍保持可用。

既有 `/node-agent/poll` 保留相容 route，但 caller-supplied `job_id` 不得建立
新 lease；最多讓同 node 取回已屬於自己的 legacy attempt。v1 assignment flag
固定 false，新 daemon 只用 2.x protocol。

### 9.3 Atomic eligibility and convergence

lease transaction 必須同時重驗：

- credential active/not revoked，node 綁定 exact server。
- server enabled 且 effective backend=`node`。
- Job queued、dependencies done、無 active attempt。
- node/server 沒有另一個 ordinary active Job；仍是一機一 Job。
- type 僅 `train|adhoc`；coding/setup/sync 排除。
- pin、required tag/canary、dataset/project readiness、capacity 全符合。
- approved payload/digest 與 command safety 重驗一致。

狀態規則：

1. lease 成功：Attempt=leased，Job 仍 queued，worker 尚無副作用。
2. 未 ack lease 過期：Attempt=expired，Job 維持 queued。
3. ack 前 agent 只 fsync attempt identity、payload digest 與 `not_launched`
   狀態。ack transaction 必須 CAS 重驗 lease owner/expiry、Job queued、
   target/backend/digest；成功時將 Attempt dispatching、Job
   `queued → running` 並固定 server/backend。
4. ack 成功後才 materialize command、fsync launch intent，再進冪等 launcher。
5. ack 後失聯：Attempt liveness unknown、Job running；禁止重派/SSH fallback。
6. terminal CAS 同時更新 generic attempt、`node_attempts` 與 Job done/failed；
   first terminal wins。
7. terminal 後 exactly-once 建立 dependency refresh、result collection、
   notification 與 owner projection operations。

### 9.4 Runnable daemon

新增並驗證：

- `agent/__main__.py`、`python -m agent --check`
- daemon/config、正式 verified-HTTPS transport、backoff+jitter
- versioned package/獨立 venv/systemd user unit
- durable local attempt journal 與 process supervisor

daemon 流程：

1. 從受保護設定讀 URL/token/work root；token 不進 argv/log。
2. TLS verification fail closed；沒有 production insecure mode。
3. 啟動/重連先 current，對照 DB ownership 與 durable local journal；只有明確
   沒有 active attempt 才 lease-next。
4. lease-next 後驗 digest，先原子 fsync identity/digest/`not_launched`，不啟動
   也不 materialize executable command。
5. ack 成功，或 ack response lost 後由 current 證明同一 attempt 已 dispatching
   且本機 journal 證明未 launch，才寫 command file、fsync launch intent，再以
   argv list、
   `shell=False`、獨立 process group 進入冪等 launcher。
6. local launcher 以 attempt id 冪等；ack 已 commit 但無法證明 launch 與否時
   unknown，不自行 relaunch。只有 durable journal/launcher 能證明同一 key
   從未啟動時才可繼續第一次 launch。
7. heartbeat 接 stop request；先回 delivery receipt，再 SIGTERM，固定 grace
   後必要時 SIGKILL。
8. stop intent/receipt/termination reason 分開保存；在現行 Job invariant 下，
   實際 exit 仍收斂 done/failed，不把 request 冒充 cancelled。
9. terminal/log tail/artifact metadata 本機持久化重試；重啟後繼續送。

credential lifecycle：

- **例行 rotation** 使用 staged protocol，不做單步「舊 token 立即失效、新 token
  只回一次」：
  1. 核准後建立有期限的 pending credential version；current credential 仍有效。
  2. 新 token/activation nonce 只顯示一次；若交付 response lost，可撤銷 pending
     version 後重開，不影響 current credential。
  3. agent 以新 credential 呼叫 activation/heartbeat，control plane 原子提升
     new primary，舊 credential 進 bounded grace。
  4. 新 credential 已證明可 current/terminal 後才結束 grace；所有 secret/nonce
     不進 argv/log/audit。DB schema 使用 additive primary/pending/grace 欄位。
- **例行退役** 先關 new assignment，active=0 後才停 daemon、retire credential
  與 server binding。
- **緊急安全撤權**（洩漏/節點失陷）不等 drain：核准的 single-node revoke
  立即使該 credential 全路由失效，其他 node 不受影響。該 node 的 active
  attempt 保持原 backend/target，設 `liveness=unknown` 與
  `recovery_hold_reason=security_revoke`，不判 failed、不 SSH relaunch。Server A
  只能用 exact target 上 token-matching sentinel 作 read-only reconcile/collect；
  不得把 inspect 當成 backend fallback。沒有可信 terminal evidence時走
  `DG-ATTEMPT-RECOVERY`；任何 SSH stop/處置仍需該 gate 與獨立 material
  approval。撤權事件與受影響 attempt ids 必須 audit/alert。

Node 暫不主動上傳 result bytes；正式結果仍由 Server A 透過現行 SSH/rsync
回收。

### 9.5 Flags / rollback

- `node_new_assignment_enabled`：只控制新 lease。
- existing-attempt protocol（ack/heartbeat/stop/terminal）在 active attempt
  存在時永遠可用，不與 assignment kill switch 共用；若保留
  `node_protocol_drain_enabled`，DB/config validation 必須在 active>0 時拒絕
  關閉。
- 例行 rollback/retirement 先關 new assignment，protocol drain 保持到
  active=0；active=0 前不得停 daemon、retire credential 或讓 terminal endpoint
  404。
- 上述 drain prohibition 不得阻擋緊急安全撤權；緊急撤權後 protocol route
  對該 credential 立即 401，attempt 設 `liveness=unknown` /
  `recovery_hold_reason=security_revoke` 且仍受 ownership guard 保護。安全撤權
  不是 rollback，也不是 terminal evidence。
- per-server `node → ssh` 只影響沒有 active attempt 的新工作。
- acknowledged/unknown Node attempt 永遠由 Node 收斂，不能用 SSH 重跑。
- 沒有 generic parent 的 legacy live Node attempt 仍由 dual-read guard 擁有，
  不能因 migration/rollback 被重派。
- 若要人工 abandon unknown attempt 並強制重跑，必須先通過
  `DG-ATTEMPT-RECOVERY`。

### 9.6 Acceptance

- `python -m agent --check` 與 systemd user unit 真正可啟動且非 root。
- Phase 2 telemetry 延伸 lease/ack/terminal、heartbeat freshness、agent/protocol
  version、cross-node reject 與 Node unknown age；Phase 5 不等待 Phase 6 才補。
- fake HTTPS E2E：lease → fsync → ack → launch → heartbeat → terminal →
  Job/result convergence。
- wrong pin/server/backend、disabled server、dependency pending、non-queued、
  wrong tag/dataset、cross-node token 均為零 attempt/零副作用。
- 同 node duplicate poll、兩 node、兩 control-plane claimant 競爭時只能有
  一個 active attempt。
- digest mismatch、expired/cross-node ack、heartbeat、terminal 全 fail closed。
- 同內容 terminal retry 冪等；衝突 terminal 不覆寫 first terminal。
- control plane 與 agent 在 lease/fsync/ack/launch/terminal 各 crash window
  重啟時零 duplicate；不確定即 unknown。
- agent 重啟與 ack response lost 必須先取回 owned attempt；local journal
  存在/遺失/損壞/token mismatch 的矩陣均不產生 duplicate launch。
- stop request/receipt 不等於 terminal；v1 explicit-id poll 不能新 claim。
- staged rotation 覆蓋新 token 交付遺失、activation response lost、grace 重啟
  與舊 token expiry；例行 rotation 不得讓 active attempt 失去 terminal 能力。
- 誤設 drain flag、assignment off 仍送 terminal、active>0 例行 retirement
  全部 fail closed 且不遺失 terminal。
- active>0 緊急 revoke 必須立即拒絕該 node 的新舊 token，其他 node 繼續正常；
  受影響 attempt 只設 unknown/security-revoke hold，絕不自動 SSH
  fallback/failed/requeue。
- fresh/legacy DB、SSH golden、full/static/migration/protocol suites 全綠。

---

## 10. Phase 5 — Real Node Canary and Promotion

### 10.1 前置條件

- Phase 0–4 release gates 全綠。
- 至少兩台 non-production ordinary worker；每台 SSH fallback 已實測。
- 每台獨立 credential、agent version、server binding。
- operations view/alerts 至少涵蓋 lease/ack/terminal、duplicate prevention、
  active/unknown age、heartbeat、queue age、result collection 與 reconciler
  freshness。

### 10.2 Progressive rollout

1. 單 node、10 個短 `adhoc` canary jobs、至少 24 小時。
2. 實演 control-plane restart、agent restart、短暫斷網、staged credential
   rotation（含 activation response lost）。
3. 第二台 node 加入，執行正式門檻：
   - 至少 100 個 designated non-production ordinary jobs；
   - 至少 2 nodes；
   - 連續 7 天。
4. 實演停止新 Node assignment，將其中一台的新工作退回 SSH；active Node
   attempt 必須保持 Node-owned。
5. 在獨立 canary attempt 實演單 node 緊急 security revoke：credential 立即
   失效、其他 node 不受影響、active attempt 不 failed/requeue/fallback，並依
   matching evidence 或預先核准的 recovery drill 收斂。

### 10.3 Pass/fail

以下必須全部為零：

- duplicate workload launch
- false disconnect failure
- lost terminal result
- cross-server/pin violation
- terminal 後 Job/owner projection 未收斂
- result collection 被誤當 execution failure

promotion 時 unresolved active unknown attempt 必須為零；只有「已有說明」仍
不算收斂。若無法取得 terminal evidence，就等待或另走
`DG-ATTEMPT-RECOVERY`，不能帶著 unknown 通過 canary。任一失敗即停止
promotion、關閉該 node 新 assignment、保留證據。

### 10.4 Promotion order

- 先逐台提升 ordinary `train|adhoc`。
- coding/setup/sync 與中央 Codex Runner 持續 SSH。
- 接著只讓 Codex 產物的 downstream compute 走 Node。
- Codex Runner transport 不屬於本 phase 的完成條件；只能在所有 ordinary
  worker 通過後另立計畫/canary。`codex exec → app-server` 又是另一個獨立
  gate，不能混入 Node transport promotion。

### 10.5 Rollback

1. 關閉該 node 的 new assignment，existing-attempt protocol 保持開啟。
2. 只讓沒有 generic/legacy active ownership 的新 Job 回 SSH。
3. acknowledged/running/unknown Node attempt 保持 Node-owned，直到 terminal
   或經 `DG-ATTEMPT-RECOVERY` 處置。
4. 例行 rollback/retirement 在 active=0 後才停止 agent 或 retire credential；
   schema、audit、attempt evidence 不回滾。安全事件的緊急 revoke 可在
   active>0 立即執行，但不得觸發 SSH relaunch 或假 terminal。
5. rollback drill 必須證明其他 node 不受影響、新工作可走 SSH、in-flight
   沒有 duplicate/lost terminal。

---

## 11. Phase 6 — Production Control Plane Operations

### 11.1 Production topology / leader takeover

- 延伸 Phase 2 的 minimum leader lease/fencing，將 API serving 與 scheduler
  ownership 明確分開。
- 非 leader 可服務 read/API/approval，但不能 dispatch。
- 接手前確認舊 lease 過期；active attempts 只 reconcile，不重建。
- 完成 leader crash/takeover、clock skew/lease expiry、split-brain 防護演練。
- 第一版可保留 `role=all` 單 process；多副本部署仍須明確拓撲與 runbook。

### 11.2 Health / observability

- liveness：process/event loop。
- readiness：DB/schema、leader、background-loop freshness、writable state paths、
  required config。
- metrics：queue depth/age、tick/errors、outbox/attempt by state/backend、
  unknown age、node heartbeat、collection、DB/disk capacity、backup age。
- background supervisor：loop 非預期退出立即告警，不能讓 `/` 繼續假綠。

### 11.3 Backup / restore

- 自動化 SQLite online backup、audit/config/hub/result metadata 備份。
- manifest 含 checksum，副本離開單一 Server A。
- 至少季度 restore drill；記錄 row counts、Git refs、artifact/result sampling。
- 初始建議 RPO 24 小時、RTO 4 小時，正式上線前由 `DG-OPS-SLO` 裁定。
- 建立 retention/GC/capacity policy，避免 DB、audit、results 填滿系統碟。

### 11.4 Security gates

- Authorization `enforce` 前，route/WS/local/MCP catalog 與所有 approval-kind
  resource resolver 必須完整；hidden 404、forbidden 403、collections/indirect
  data filtering、service scope、separation-of-duties、break-glass/rollback
  全有證據。先 shadow review，再獨立切 enforce。
- Node routes 只接受 node credential，且只能操作自己擁有的 attempt。
- SSH host-key policy 依 `INV-SSH-8` 另案裁定；未裁定前不順手改現況，
  Node HTTPS 仍必須驗 TLS。
- application config 自身需對 API bind/private exposure fail closed，
  不只依賴 shell helper。
- Codex Runner hard isolation 另開 resource/sandbox decision。

---

## 12. Named Decision Gates

| Gate | 必須裁定的內容 | 未裁定時行為 |
|---|---|---|
| [DG-EXEC-ATTEMPT](DG_EXEC_ATTEMPT_DECISION.md)（2026-07-27 已核准 v1） | attempt/outbox schema、operation-level authorization、immutable server-config revision、active 唯一性、payload immutability、server mutation guard、minimum leader ownership | 維持現行 SSH，不啟用 attempt-driven path |
| [DG-AMBIGUOUS-LAUNCH](DG_AMBIGUOUS_LAUNCH_DECISION.md)（2026-07-27 已核准 v1） | `INV-STATE-2` 精確修訂文字、definite pre-launch failure 邊界、atomic remote claim、response lost、receipt、replay/recovery | 新 attempt path 不啟用；legacy SSH 風險維持且不得宣稱已修正 |
| DG-JOB-STATE | 是否新增 Job 狀態/轉移，尤其 `running → cancelled` | 保持封閉狀態機；stop 後由 terminal 收斂 done/failed |
| DG-ATTEMPT-RECOVERY | 是否允許 abandon acknowledged/unknown attempt 強制重跑 | 禁止重派，只能持續 reconcile/人工查證 |
| DG-DATASET-SNAPSHOT | snapshot build approval kind、local ArtifactStore revision、content-addressed shards、atomic publish、legacy dataset registration 邊界 | reproducible run 只允許 `dataset=none`；legacy dataset 不得標 verified |
| DG-CODE-PROMOTE | promotion approval kind、hub 寫入與 rollback | 只產 bundle，不 promotion |
| DG-NODE-V2 | server-selected lease、current-attempt recovery、protocol/drain flags、staged rotation、例行退役與緊急撤權、v1 retirement | Node new assignment 全關 |
| DG-NODE-CANARY | 兩台機器、tag、時間窗、metrics 與 rollback owner | 不進實機 |
| DG-AUTHZ-ENFORCE | roles、404/403、scope、SoD、break-glass/rollback | 保持 off/shadow |
| DG-SSH-HOSTKEY | known-hosts 來源、rotation、錯誤/回退 | 保持 canonical 現況 |
| DG-GPU-SCHED | GPU/MIG slots、多 job、quota、preemption/fairness | 一機一 ordinary Job |
| DG-CODEX-APP-SERVER | exact CLI/protocol pin、canary、rollback | 使用單回合 `codex exec` |
| DG-GITHUB-PUBLISH | GitHub App、repo allowlist、token broker、egress | interface/fake only |
| DG-OPS-SLO | RPO/RTO、retention、alert owner | 不宣稱 production-ready |

任何 gate 只解鎖該列。修改 canonical invariant 時，另提交清楚的舊/新語意、
migration、tests、rollout 與 rollback，不得藏在功能 PR。

---

## 13. Release Gate（每個 Slice）

每個 slice 必須交付：

1. 使用者可觀察成果與未完成聲明。
2. 變更的 schema/API/state/reason codes/feature flags。
3. 涉及與保持不變的 invariants。
4. fresh DB + representative legacy migration evidence。
5. success、invalid、stale、unreachable、restart、retry、idempotency、rollback。
6. focused、related subsystem、static checks、full suite 結果。
7. 無 production network/credential/runtime file 接觸的聲明。
8. rollout/rollback 與 active-attempt ownership。
9. capability ledger 更新。

若 full suite 因環境無法執行，slice 不算完成；不得用 focused pass 取代。

---

## 14. 建議工作包與交接順序

Blocking edges：

| 開始前 | 必須已完成 |
|---|---|
| WP-1A | WP-0A/0B + `DG-EXEC-ATTEMPT` |
| WP-2B | Phase 1 + `DG-AMBIGUOUS-LAUNCH` |
| WP-3A | Phase 2 + `DG-DATASET-SNAPSHOT` |
| WP-3C / Phase 3 promotion E2E | WP-3A/3B + `DG-CODE-PROMOTE` |
| WP-4A | Phase 3 + `DG-NODE-V2` |
| WP-5 | Phase 4 + `DG-NODE-CANARY` 操作簽核 |
| authorization/host-key/GPU/app-server/ops activation | 各自同名 gate |

每個工作包獨立 branch/PR，不跨包順手重構：

1. **WP-0A**：dependency lock、TestClient baseline、CI。
2. **WP-0B**：Slice 0B–0D：CI、capability/invariant-drift ledger、stop/dataset/
   server-mutation characterization、文件矛盾、backup/restore smoke。
3. **WP-1A**：attempt/outbox/event/server-config-revision schema、
   operation authorization、migration、pure transition tests。
4. **WP-1B**：payload hash/immutability、active-attempt/server-mutation guard、
   shadow parity。
5. **WP-1C**：stop invariant correction 與 regression tests。
6. **WP-2A（2026-07-27 completed）**：minimum scheduler
   leader/fencing、outbox telemetry。
7. **WP-2B（2026-07-27 completed）**：SSH atomic launch claim、attempt
   receipt/token validation、idempotent launcher、crash matrix、versioned
   golden tests。原語完成但刻意未接 dispatch。
8. **WP-2C（下一個工作包）**：attempt-driven SSH
   dispatch/reconcile/stop/collect，解除 `RB-LAUNCH-001`。
9. **WP-2D**：20-job/24-hour non-production SSH canary 與 rollback drill。
10. **WP-3A**：local ArtifactStore、approved DatasetSnapshot builder、
    content-addressed shard verifier/atomic publish +
    JobSpec/ExecutionPlan/Run schema。
11. **WP-3B**：preview/request/approval binding、planner-driven SSH execution。
12. **WP-3C**：Codex promotion 與 Project page E2E。
13. **WP-4A**：Node v2 server-selected atomic lease、current-attempt recovery、
    convergence。
14. **WP-4B**：runnable daemon、HTTPS transport、local supervisor、staged
    credential rotation 與 emergency-revoke handling。
15. **WP-4C**：stop/terminal retry/result hooks/telemetry、dark-launch E2E。
16. **WP-5**：兩節點/100-job/7-day canary 與 rollback drill。
17. **WP-6A**：production role split、leader takeover、health/alerts/backup。
18. **WP-6B**：authorization/SSH/operations 各自 gate 後實作。

同一時間只開一個 execution/state-machine 工作包。UI、文件或純讀 observability
可平行，但不能與另一個 state transition 變更混在同一 PR。

---

## 15. 明確延後

在上述閉環與 production gate 完成前，不做：

- Kubernetes/microservice rewrite
- PostgreSQL migration
- hostile multi-tenant isolation
- GPU sharing/bin packing/preemption
- 自動 Codex repair loop 或自動 promotion
- Codex 自己核准 request
- agent-initiated artifact byte upload
- SSH backend 移除
- GitHub 自動 merge/deploy

這些不是永久否決；只是不能先於 execution correctness、immutable plan 與實機
canary。

---

## 16. 下一個立即可執行的切片

Phase 0、Phase 1、WP-2A 與 **WP-2B 已完成**（`INV-STATE-2` 新條文已生效、
`app/execution_launch.py` 仲裁原語與 54 項 crash matrix 全綠）。下一步固定為
**WP-2C**，這也是唯一能解除 `RB-LAUNCH-001` 的工作包：

1. 讓 scheduler 在同一 DB transaction 重驗 eligibility、固定 server/backend、
   建立 dispatching attempt 與 prepare outbox，並將 Job `queued → running`；
   commit 前不做任何 mkdir/SFTP/SSH。
2. 把 `app/scheduler.py` 現行的無條件 revert 換成 WP-2B 的分類：definite
   pre-launch failure 才 revert，ambiguous 一律保持 running 並記
   `liveness=unknown`。同時移除 WP-2B 的邊界測試
   `test_scheduler_does_not_import_the_new_launch_module_yet`，並在該處說明
   邊界為何解除。
3. reconcile/stop/collect 全部改由 `ExecutionBackend` 依 persisted attempt 的
   `server_config_revision_id` 取得目標，不讀後來修改的 servers.yaml。
4. collection 獨立為 outbox operation；失敗只標 collection 狀態，不回寫
   workload failed。
5. 補上 attempt-driven 的 versioned exact golden，legacy golden 逐字不變；
   rollback drill 必須證明新 queued Job 可回 legacy SSH，而 uncertain
   attempt 仍由新路徑 drain。
6. 全程維持所有 flags 預設關閉；實機 canary 屬 WP-2D，不在本包。

---

## 17. 給下一位實作者的 handoff checklist

- 目前工作 branch：`codex/goal-1-oidc`；不得直接修改 main。
- 動工前重新讀 `CLAUDE.md`、`PLAN.md`、canonical invariants、
  `docs/DECISIONS.md`、相關 migration/tests。
- 目前工作樹已有不屬於本計畫文件的修改：
  `app/approvals.py`、`app/server_config.py`、`static/index.html`、
  `tests/test_server_config_api.py`、`tests/test_supporting_surfaces_ui.py`，
  以及 runtime `server.log`；先確認歸屬，不覆寫、不清除。
- 每次只認領一個 WP；先寫 decision/contract 與 failing boundary tests，
  再做 additive implementation。
- 只用 fake/local fixtures；不讀 production credential、不連 production worker、
  不以 production runtime DB 作 migration 測試。
- 若實作需要改 protected invariant，立即停止該 WP，提出 named decision，
  不以「順便修正」或測試更新掩蓋語意變更。
