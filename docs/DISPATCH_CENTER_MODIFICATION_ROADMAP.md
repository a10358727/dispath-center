# Dispatch Center 專案修改方向與優化建議

> 審核基準：GitHub PR #21，分支 `codex/wp2d-canary-7ecbec6`。
> 目前狀態：本機架構重構基礎與本輪 P0/P1 code slices 已完成並通過離線回歸；尚未合併 `main`，亦尚未具備正式 Node rollout、production deployment、外部 audit anchor 或完整 domain audit migration 證據。
> 本文件用途：作為後續修正、拆分 PR、測試與驗收清單。

## 1. 現況摘要

目前已建立：

- Control Plane 與 Node Agent 的獨立 packaging。
- Typed settings 與設定驗證。
- API router boundary。
- Authorization enforce mode。
- Versioned SQLite migrations。
- Repository／Unit of Work seam。
- Durable audit ledger 與 export outbox。
- Node protocol 2.0 與 read-only probe。
- Worker process role。
- Workload isolation contract builder。
- Ruff、mypy、coverage、wheel smoke、frontend smoke 等 CI gate。
- 完整 offline suite 與 quickstart cleanup 重跑證據。

2026-08-06 remote evidence：上一個遠端驗證 commit `068ff50` 已推送；本輪
feature-flag metadata/readiness slice 位於 `b41dcb7`，audit-export worker
slice 位於 `d55638f`，copy-drain evidence 文件位於 `b4de453`。receipt-CAS
recovery slice 位於 `9113464`，其後的 audit-worker success-freshness/readiness
切片與 OpenAPI contract 修正已收斂至 `f0bf0c7` 的 exact candidate
`f0bf0c725ce5e7055293710954d21db5816fd7aa`。該候選的 push run
`31064358200` 與 pull-request run `31064360956` 均成功；PR #21 目前為
Ready for review，merge state 為 `CLEAN`。目前尚無 reviewer decision，故不宣稱
已核准或可合併。
CI 現在也會在 package smoke gate 後發佈保留 14 天的
`dispatch-wheels-<commit-sha>` review artifact；它只提供固定 wheel 給後續
non-production canary 安裝，不能替代「已安裝」或 canary 證據。
本輪 `4e2ff0d818652d8ef67e85ad1d973b585ec7bb03` 另加入 execution outbox
read-only evidence gate；其 push run `31066516682` 與 pull-request run
`31066518699` 均完成完整 suite 並成功，對應 review wheel artifact
`8954019952` 保留至 `2026-08-20T02:46:14Z`。這些是本地／CI evidence，不代表
117 host 已安裝 wheel 或已完成 canary。
其後的 coverage-classification candidate
`c6aece6399b277159140419298743b31e4655b1f` 之 push run `31068045167` 與
pull-request run `31068048570` 亦均成功；review wheel artifact
`8954569767` 保留至 `2026-08-20T03:18:33Z`。這仍是 evidence projection，不能
替代 117 host 安裝、canary 或 reviewer approval。
WP-2D evaluator 另會將窗口內每個 attempt 綁回 immutable canary approval，
確認 contract/purpose、payload digest、候選 commit 與 manifest 完全一致；這
是 evidence quality gate，不是新的 canary 執行結果。

另有一份歷史 WP-2D v2 SSH canary 在候選 `d73a38e`、`worker_5090_117`
上通過完整八小時 evaluator（20/20 terminal、三項 drill 全過）；摘要見
`docs/evidence/WP2D_V2_20260802_D73A38E.md`。因目前分支在該候選後仍有
execution-path commits，這份證據不會把最新 commit 標成 canary-proven，需
對最新 exact candidate 重新跑窗口。

目前仍不能宣稱：

- Node workload 已在正式 canary／production host 強制使用隔離 unit。
- 所有 domain mutation 都使用 durable audit。
- Audit chain 具備 off-host tamper evidence。
- Node backend 已通過正式 canary。
- PR #21 已達到適合一次性合併的審查大小。

本機已確認：systemd launcher 已接入 Agent runtime，`--check` 與真實
user-systemd smoke 可驗證 workload/control evidence 分離、credential
allowlist、cgroup stop、restart recovery 與 resource properties；這些證據
仍不等同於正式 worker rollout。


## 2. 已確認完成的修正

### 2.1 Durable Audit 基礎

目前已有：

- `audit_events` append-only table。
- DB trigger 禁止 `UPDATE`／`DELETE`。
- Event sequence、previous hash、versioned hash contract。
- Canonical JSON hash。
- Event 與 export operation 1:1。
- Export lease、retry、dead-letter、manual replay。
- JSONL at-least-once export。
- `event_id + event_sha256` 去重。
- Bounded audit parameter size 與 nesting。
- Secret-like 欄位拒絕規則。
- API 顯示 `durable_db`／`legacy_jsonl` 與 evidence durability。
- Backup／restore 時驗證 audit hash chain。

### 2.2 Quickstart Cleanup

先前四個 cleanup timeout 已完成 idle-host 驗證：

- 完整 suite 連續兩次通過。
- Quickstart 連續十輪通過。
- 未發現 orphan process。

此項不再視為目前的 merge blocker。

### 2.3 Authorization Enforcement

目前已支援 `off`、`shadow`、`enforce`。

Enforce mode 具備：

- fail-closed route catalog。
- project／global scope resolution。
- service token scope enforcement。
- collection row filtering。
- high-risk approval separation of duties。
- unmapped route rejection。

### 2.4 Packaging、Migration、Process Role

目前已有：

- Control Plane wheel。
- Node Agent wheel。
- `dispatch-api`、`dispatch-scheduler`、`dispatch-worker`。
- SQLite migration ledger 與 process lock。
- DB backup／restore／check CLI。
- Worker non-HTTP entry point。
- Uvicorn 使用已載入 FastAPI app object，避免二次 import 造成 state 分裂。


# 3. 合併前必須修正

## P0-1：Audit Export 不得提前 Dead-letter Active Lease（本機已完成）

### 問題

目前 claim 流程會把下列 row 直接轉成 `dead_letter`：

```sql
attempt_count >= max_attempts
AND state IN ('pending', 'failed', 'processing')
```

若某個 exporter 正在處理最後一次嘗試，而且 lease 尚未到期，另一個 exporter 呼叫 claim 時可能把 active processing operation 提前轉成 dead-letter。

可能結果：

```text
Worker A 正在輸出 JSONL
Worker B 將 operation 標成 dead_letter
Worker A 寫入成功，但 complete CAS 失敗
JSONL 有資料，DB 卻顯示 dead_letter
```

### 修改方向

```sql
WHERE attempt_count >= ?
  AND (
      state IN ('pending', 'failed')
      OR (
          state = 'processing'
          AND claim_expires_at IS NOT NULL
          AND claim_expires_at <= ?
      )
  )
```

### 必加測試

- Final attempt 正在 processing 且 lease 未到期。
- 第二個 claimer 不得改成 dead-letter。
- Lease 到期後才可轉入 dead-letter。
- 原 owner 在 lease 有效期間完成 export 應成功。
- 已寫 JSONL 但 DB complete 失敗時仍可 deduplicate。

### 驗收標準

- Active lease 永遠不被其他 claimer 改寫。
- State transition 有完整 CAS predicate。
- 兩個 SQLite connection 的 race test 通過。

另提供 `scripts/audit_export_status.py` 作為 read-only 的 outbox evidence
gate；`--require-clear` 只在明確要求 backlog/dead-letter 為零時回傳失敗，
不會把一次查詢誤當成 production alert 或 SLO readiness。

目前實作已加入 active-lease CAS 條件與雙 connection race regression；本機
offline suite 已通過。遠端 CI／production exporter backlog 仍屬外部 gate。

---

## P0-2：Node Workload Isolation 必須接入真實 Runtime（本機 code slice 已完成）

### 問題

目前已有 isolation contract、strict environment allowlist、`PrivateUsers`、`ProtectProc`、read-only control evidence、resource policy 與 `KillMode=control-group`；Agent daemon 的 launch path 已依設定選擇 launcher，且 strict systemd 模式在 canary／production 會 fail closed。正式 worker rollout 與 canary 證據仍未完成。

```text
isolation builder exists
```

不等於：

```text
all Node workloads are isolated
```

### 建議設定

```env
DISPATCH_NODE_ISOLATION_MODE=direct
```

允許值：

```text
direct
systemd
```

規則：

- `direct`：開發與 rollback。
- `systemd`：正式 isolated transient attempt unit。
- Canary／production 必須要求 `systemd`。
- 設定為 `systemd` 但 host 不支援時 fail closed。
- 不可靜默 fallback 到 `direct`。

### Runtime 接線

```text
poll
→ verify protocol
→ acknowledge
→ persist command
→ select isolation backend
→ build transient attempt unit
→ persist isolation manifest
→ start isolated supervisor
→ monitor/report terminal
```

建議建立：

```python
class WorkloadLauncher(Protocol):
    def launch(self, request: LaunchRequest) -> LaunchReceipt:
        ...
```

實作：

```python
class DirectSupervisorLauncher:
    ...

class SystemdTransientLauncher:
    ...
```

### Control Evidence 目錄

```text
attempt/
  workload/
    cmd.sh
    stdout.log
    stderr.log
    artifacts.json

control/
  supervisor.json
  terminal.json
  isolation.json
  launch-receipt.json
```

要求：

- workload 可寫 `workload/`。
- workload 不可寫 `control/`。
- Agent／Supervisor 才可寫 `control/`。
- `control/` 不作為 workload cwd。
- Terminal evidence 不可由 workload 覆寫。

### `--check` 應驗證

- user systemd 可用。
- `systemd-run --user` 可建立 transient unit。
- user namespace／`PrivateUsers` 可用。
- cgroup backend 可用。
- `ProtectProc`／`ProcSubset` 支援。
- control evidence path 與 workload path 不重疊。
- workdir 可建立與 fsync。
- Agent 非 root。
- Agent package、supervisor、launcher imports 可用。

### 真實 Linux 整合測試

- workload 看不到 `DISPATCH_NODE_TOKEN`。
- workload 讀不到 credential env file。
- workload 無法寫 `terminal.json`。
- workload 無法 signal／ptrace Agent。
- stop 會終止整個 workload cgroup。
- Agent restart 後可恢復 supervisor evidence。
- isolated attempt 完成後 unit 可被 collect。
- resource limit 生效。

本機已通過 4 項 user-systemd integration tests（不支援 systemd 的 host
會 skip-safe）；這不是正式 Node canary。


## P1-1：Migration Checksum 必須反映 Migration 內容（本機已完成）

### 問題

目前 checksum 只基於 version、name、module 與 qualified function name。修改 migration function 內容但保留名稱時，checksum 不變。

### 建議方案

將 migration checksum 改為明確固定 digest：

```python
Migration(
    version=2,
    name="durable_audit_export_outbox",
    checksum="reviewed-sha256",
    apply=apply_migration_2,
)
```

Digest 來源可為：

- checked-in SQL file bytes；
- canonical migration manifest；
- migration Python artifact 的固定內容。

建議結構：

```text
app/migrations/
  0001_legacy_schema.py
  0002_durable_audit.sql
  0003_audit_hash_contract.py
```

### 必加測試

- 同 version／name，但 checksum 不同時拒絕。
- 修改 migration artifact 後舊 DB status fail closed。
- Unknown migration version 拒絕。
- Migration ledger gap 拒絕。

---

## P1-2：Migration Ledger 不可有版本缺口（本機已完成）

以下 ledger 應視為損壞：

```text
1, 3
```

不能在 version 3 後補套 version 2。

`status()` 應驗證：

```python
applied_versions == list(range(1, current_version + 1))
```

遇到 gap 應直接 fail closed，readiness 也必須失敗。

---

## P1-3：Node Protocol 缺 Header 時應有明確策略（本機已完成）

### 問題

目前明確不相容版本會回 `426`，但缺少 version header 時仍可能被當成 current version。

### 建議設定

```env
NODE_PROTOCOL_ALLOW_MISSING_VERSION=false
```

預設為 `false`。過渡期需要相容時才開啟，且必須有移除日期。

### 建議 HTTP 行為

缺少 header：

```http
HTTP/1.1 426 Upgrade Required
X-Node-Protocol-Version: 2.0
```

```json
{
  "detail": "node protocol version is required",
  "protocol_version": "2.0"
}
```

不相容版本同樣回 `426`。

### 必加測試

- 缺 header 且 compatibility off → 426。
- 缺 header 且 compatibility on → 接受並記錄 deprecated usage。
- `1.0` → 426。
- `2.0` → 正常。
- Response 一律帶 current protocol header。
- Agent probe 驗證 response version 與 capabilities。

---

## P1-4：Audit Adoption Catalog 必須覆蓋全部 Mutation（目前為部分遷移）

### 目標資料模型

```python
@dataclass(frozen=True)
class AuditAdoptionEntry:
    mutation: str
    emitted_actions: tuple[str, ...]
    durability: Literal["durable", "legacy"]
    owner: str
    migration_issue: str | None
    target_slice: str | None
```

### 必須納入

- Identity：service account、service token、membership、session security。
- Approval：create、approve、reject。
- Server／Node：add、update、disable、delete、enroll、rotate、drain、revoke、retire。
- Execution：Job materialization、run、attempt、launch resolution、stop、terminal、artifact、result。
- Project／Dataset／Engineering：project mutation、snapshot publish、engineering promotion。

### CI Gate

- 每個 mutation route 必須有 catalog entry。
- Durable action 不可直接呼叫 legacy `append_audit()`。
- 新 mutation 沒登錄時 CI fail。
- Legacy entry 必須有 owner 與 migration issue。
- Durable entry 必須有 transaction rollback test。

### 建議遷移順序

1. Approval decision。
2. Identity／service token。
3. Server／Node lifecycle。
4. Run／stop／terminal／result。
5. Project／dataset／engineering。

目前已完成 approval decision、identity、Node lifecycle、execution
attempt/run、project create/update/delete、dataset registry/card/cache、dataset
sync verification、snapshot publish/abort，以及 Engineering Task
create/update、result、promotion 的 transactional slices；pinned server
publication 的 add/update/disable/delete 也會在 publication transaction
內留下 prepare/transition/outcome durable events；四個 server route 的
approval decision 亦由 `approval_decided` durable evidence 覆蓋。approval
create/decide/approve/reject 現在都由 durable catalog entry 覆蓋，沒有額外的
`approve` compatibility decision writer；既有 request route 的
  `approval_requested` JSONL 摘要則由 `approval.compatibility` 明確列管。catalog 與 CI gate 仍會
列出 unpinned server fallback（不建立 revision，但以
`server_legacy_mutation_intent`／outcome 留 durable evidence；摘要由
`server.compatibility` 保存）、未提供的
archive endpoint，以及舊版 unbound `coding_task` wrapper 的 compatibility
摘要（其 Job materialization 本身已由 transaction-bound durable event 覆蓋）；generic
reject、native Engineering Task retry/discard 已由 transaction-bound durable
  events 覆蓋；pinned canary、worker validation、execution-plan materialization
  也不再重複寫 approval/plan JSONL。ExecutionPlan request 現在把 immutable
  plan row、pending approval 與 bounded `execution_plan_materialized` event
  綁在同一 transaction；`plan_run_request` 只保留 compatibility summary。
  未遷移項目不可視為 durable。

普通 enqueue（包含 setup/sync/bundle 多工作流）以及 standalone compatibility
enqueue 現在都由
`execution_job_materialized` 與 `approval_decided`、整張 Job graph 共用同一
個 UoW；其 payload 仍是明確標記的 legacy-unpinned evidence。standalone
wrapper 的 legacy JSONL compatibility line 仍由 `execution.compatibility`
保留；policy-scoped auto-placement decision 已在同一 UoW 內寫入
`auto_placement_policy_decision` durable event，不能追溯宣稱為 pinned
durable execution。

其中 pinned publication、legacy server intent/outcome、bound coding-run result
與 legacy unbound coding-run result projection 分別以
`server.publication`、`server.legacy_observed`、`engineering_task.result` 的
durable slices 記錄；unbound collector 現在以
`coding_run_result_recorded` 將 CodingRun row 與 durable evidence 綁在同一
transaction。這不會把同一 family 的 unpinned/unbound compatibility path 的
revision/materialization 追溯標成 durable，只表示這個結果 projection 的
bounded evidence 已落入 durable ledger。

Run Profile 與 Dispatch Policy 的 create/update/archive approval 現在也已
落入同一個 revision-plus-decision UoW：immutable revision insert、bounded
`*_revision_created` durable event、以及 typed `approval_decided` 會一起
commit；durable append 失敗時兩者一起 rollback。既有 route-specific JSONL
摘要仍由 `run_profile.compatibility`／`dispatch_policy.compatibility` 明確
標示，尚未宣稱 legacy sink 已退役。

專案詳情頁的 experiment record create/update/delete 也已遷移：HTTP 與 Agent
tool 共用 `Database` 的 transaction-bound UoW，三種 mutation 分別寫入
`experiment_record_created`／`experiment_record_updated`／
`experiment_record_deleted` durable event；event 只保存 bounded identity、
author/kind、changed fields 與 link presence，不複製 Markdown 內容或標題。
audit append fault injection 會讓資料列與事件一起 rollback。

Identity project-membership grant/update/remove approval 也已收斂到同一個
membership decision UoW：membership row、`membership_granted`／
`membership_revoked` 與 `approval_decided` 同時 commit，identity event 只帶
bounded project/actor/role 與 approval correlation。same-role／already-absent
仍是 idempotent，audit append fault 會讓 membership 保持原狀且 approval 留在
pending；直接 CRUD 路徑維持相容。

Service-account create 與 service-token issue/revoke approval 也已收斂到
identity decision UoW：actor/account 或 token/revocation、bounded
`service_account_created`／`service_token_created`／`service_token_revoked`
以及 `approval_decided` 同時 commit。事件只帶 actor/token identity、scope
count 與 approval correlation，不複製 bearer token、secret hash、label 或
scope 內容；disabled service account 仍不能核發新 token，already-revoked
仍保持 idempotent。audit append fault 會讓 mutation rollback 且 approval
留在 pending，直接 CRUD 行為維持相容。

Dataset cache reconcile 也已改為單一 UoW：同一台機器一次 `ls` observation
的所有 cache add/remove row、row-level durable events 與 count-only
`dataset_cache_reconciled` event 同時 commit。原本的 `cache_reconcile`
JSONL 摘要保留為 default-on、可由 `LEGACY_AUDIT_JSONL_ENABLED` 關閉的
compatibility projection；empty observation 不產生事件，audit append fault
會讓整輪 cache map 與事件一起 rollback。

Project instance 的唯讀 reconcile 也已補上 row/event transaction boundary：
state 或 git snapshot 真正改變時，`project_instance_reconciled` durable event
與 row 同時 commit；event 只帶 project/server identity、from/to state 與
observed flag，不帶 path、command output 或 exception。重複 observation 不
產生無界 event stream，offline/unknown 不會被誤判成 missing；既有
`instance_reconcile` JSONL 摘要仍保留為 compatibility evidence。

Project candidate upsert/status 與 project-instance create/upsert 現在也各自
使用 immediate transaction 發出 bounded lifecycle events；unchanged scan
保持安靜，path、README、embedded-data、command 與 remote content 不會進入
durable params。核准的 `import_project` 與 `ignore_project_candidate` 決策則
把 candidate/project/instance mutation、lifecycle events 與
`approval_decided` 放在同一個 rollback boundary；既有 JSONL 摘要仍是
compatibility evidence。

`inventory_scan` 的 SSH read-only observation 完成後，所有候選 upsert 與
approval decision 現在以單一 batch UoW 落地；audit append failure 會讓整批
候選與 approval 一起 rollback。`ignore_nested_candidates` 也在同一個 UoW
重新檢查 candidate 狀態並保留已處理項目的 skip race 語意；既有
`candidates_ignore_nested` JSONL 摘要仍保留為 compatibility evidence。

Canonical `ProjectVersion` 建立也已補上 immutable row/event boundary：新
`(project, git_commit)` identity 會在同一個 immediate transaction 寫入
`project_version_created`，重複 hub-sync/deploy 不會改寫既有 ref/source/
metadata 或重複發 event；deploy path 會帶 approval correlation，metadata
內容不進 durable params。

`git_init` 的遠端 mutation 也已補上 intent/outcome boundary：第一個 SSH
寫入前先以 approval payload digest 寫入 immutable
`project_git_init_intent`，成功或 size-guard compensation 將 instance git
projection、`project_git_init_outcome` 與 `approval_decided` 放在同一個
UoW；SSH response loss 只記 `unknown` 並保留 pending，後續唯讀 HEAD/branch
reconcile 才能收斂，不能盲目重播遠端 mutation。既有 `git_init` JSONL line
仍是 compatibility summary。

`project_deploy` 也在 local bundle／rsync／clone 前寫入
`project_deploy_intent`；成功時新 instance、canonical ProjectVersion、
`project_deploy_outcome` 與 `approval_decided` 同一個 UoW，已知命令失敗仍
保留原有 rejected/cleanup 指引並留下 bounded outcome。SSH response loss
維持 pending，下一次只用唯讀 HEAD/branch reconcile，不重播 bundle/clone；
destination path 與 raw command/bundle content 不進 durable params。

`apply_patch` 的 branch mutation 也已補上同樣的 boundary：在 checkout 前
寫入 `project_apply_patch_intent`，固定 approval payload digest、instance、
原 branch 與產生的 branch；成功時 `project_apply_patch_outcome` 與
`approval_decided` 同一個 UoW。SSH response loss 或 durable append failure
維持 pending 並留下 bounded `unknown`，下一次只讀 pinned branch head，絕不
重播 checkout／apply／commit；既有含完整 diff 的 JSONL 仍只是相容摘要。

直接執行的 `hub_sync` 也已在 worker bundle／rsync／local fetch 前寫入
`project_hub_sync_intent`；成功時 immutable ProjectVersion 與
`project_hub_sync_outcome` 同一個 UoW，已知命令失敗記 `failed`，response
loss 記 `unknown`。重用同一 operation id 可從 unknown 收斂到 applied，且不
重複建立同一 `(project, git_commit)` version；既有 `hub_sync` JSONL 保持為
compatibility summary。

`server_bootstrap` approval 也已在固定腳本 upload／execution 前寫入
`server_bootstrap_intent`；報告 row、`server_bootstrap_outcome` 與
`approval_decided` 同一個 UoW。SSH/write response loss 記 `unknown` 並保持
pending，retry 不會重新上傳或執行腳本，需人工 recovery；durable params 不含
key、report content 或 capability output，既有 JSONL 仍是 compatibility summary。

D-5 的 `server_attempt_backend_preflight` 也已收斂：exact active approved
revision 的 observation 欄位與 `server_attempt_backend_preflight_recorded`
durable event 同一個 UoW，append failure 會 rollback evidence。事件只帶
server/revision identity、status、bounded reason、filesystem type 與 contract
version；remote stdout、key path、exception text 不進 durable params。既有
`server_attempt_backend_preflight` JSONL 仍是明確的 compatibility summary。

Engineering Task 的 `coding_cleanup` remote compensation 也已補上
intent/outcome boundary：Runner task directory deletion 前寫入
`engineering_task_cleanup_intent`，成功時本地 `worktree_path` projection 與
`engineering_task_cleanup_outcome(applied)` 同一個 UoW；response loss 或
durable finalization failure 記 `unknown`、保留 local projection 並禁止自動
重播。workspace/source path 不進 durable params，既有 `coding_cleanup` JSONL
仍是 compatibility summary。

Approval-gated `node_enroll`、`node_rotate`、`node_retire` 與 `node_revoke` 也已
補齊 approval transaction boundary：一次性 credential 只在 approval layer
產生；enroll 的 node row、`node_enrolled`、approval note/status 與
`approval_decided`，rotation 的 legacy/staged credential projection 與
`node_rotated`，retirement 的 drain/retired projection 與 `node_drained`／
`node_retired`，以及 revoke 的 security recovery hold、affected-attempt
liveness、`node_revoked` 與 `approval_decided`，各自在同一個 SQLite UoW。任一
durable append failure 都會讓 approval 留在 pending 且 rollback node/attempt
projection；staged rotation 仍由獨立 activation UoW 完成 primary cutover，
`replace_pending` 保留 response-loss recovery 語義。raw token、secret digest、
activation nonce 與 credential material 不進 durable params，直接
`insert_node()` 與非 approval lifecycle 的相容 primitive 仍維持原行為。

Execution 的 legacy queued-Job cancel 也已接入專用的 status-CAS UoW：status
transition 與 bounded `execution_job_cancelled` event 同一 transaction，command
不進 ledger；audit append 失敗會保留 queued 狀態。Engineering Task owner
Job 仍由既有安全檢查拒絕通用 cancel，沒有放寬受保護流程。
保留的 SSH scheduler fallback 也已把 queued→running claim 與 dispatch exception
回 queued 的狀態轉換接入 `execution_job_dispatched`／
`execution_job_dispatch_requeued` immediate UoW；local sync 使用同一 bounded
contract，pre-dispatch disk rejection 沿用 terminal event。既有
`dispatch`／`dispatch_failed` JSONL 只作 compatibility summary，durable params
不含 command、path、exception 或 remote output；append failure 會在遠端副作用前
rollback claim，SSH fallback 與 response-loss 行為仍保留。
Scheduler stalled detection 的 `stalled_suspect` flag transition 也已接入同一
Job UoW，寫入 bounded `execution_job_stall_state_recorded`（含 stalled/cleared
reason）；重複觀測不重複寫 event，append failure 會 rollback log-size/flag，且
不會先觸發通知或 JSONL summary。
Legacy unbound coding-run result collection likewise commits the `coding_runs`
status projection with `coding_run_result_recorded`; an append failure leaves the
previous result unchanged. The older `coding_finished` line remains a
compatibility summary and is not treated as the authoritative mutation record.
Generic reconcile 的 terminal done/failed、interrupted requeue，以及依賴
blocked 狀態也已透過同一個 optional Job UoW hook 發出 bounded durable events；
log tail、command 與 dependency raw payload 不會進 ledger。只有 approved stop
仍在競態中阻擋 requeue 時，保留 `requeue_blocked` compatibility summary。

本輪又補上 adoption inventory：CI 會掃描 `app/` 與 `dispatch_center/` 中每一個
literal `append_audit()` action，未列入 typed catalog 的新 action 直接 fail；目前
55 個 literal action 均已由 70 個 catalog entries 明確列管，其中 16 個是保留
相容性的 legacy/inventory entries。另已把 closed dynamic writer 一併展開驗證：
`approval.kind` 使用 `VALID_APPROVAL_KINDS`、server `action_name` 的四個
mutation kind，以及 authorization shadow 的兩個 evidence action 都必須在同一
catalog 中有 owner/issue；未知的 open dynamic expression 仍不會被猜測或誤標成
durable。這只是把既有 JSONL 邊界完整列出，不把 operational/read-only summary
或尚未遷移的 mutation 誤宣稱為 durable。
`audit_coverage()` 另提供 `required_durable_actions`、
`required_legacy_actions` 與 `required_missing`，把 P1-4 required mutation
family 的結果與完整 catalog 分開呈現；目前沒有 required missing，唯一列在
required legacy 的是明確保留的 `engineering_task.compatibility` 摘要。這是
evidence 分類，不代表 partial adoption 已完成或 legacy JSONL 已退休。

---

## P1-5：Artifact Metadata 改為 Immutable Insert-or-Verify（本機已完成）

### 問題

相同 artifact identity 不應被後續 upsert 改寫 digest 或 size。

### 修改方向

```text
不存在
→ INSERT

存在且完全一致
→ idempotent success

存在但 digest／size／kind 衝突
→ reject
```

建議約束：

```sql
UNIQUE(attempt_id, artifact_path)
```

不應使用一般 `ON CONFLICT DO UPDATE` 覆寫 digest。

Artifact 最好只在 terminal evidence 確認後 finalize。若需要執行中 metadata，可分成：

```text
reported
verified
finalized
```

---

## P1-6：Audit Chain 需要 External Anchor（local verifier 已完成；external gate 未完成）

### 問題

SQLite 內部 hash chain 只能證明內部一致性；具有完整 DB 寫權限者仍能修改事件並重算整條 chain。

### 修改方向

定期產生 checkpoint：

```json
{
  "contract_version": "audit-checkpoint-v1",
  "database_id": "...",
  "first_sequence": 1,
  "last_sequence": 5000,
  "last_event_sha256": "...",
  "created_at": "...",
  "application_version": "...",
  "schema_version": 3
}
```

Checkpoint 應：

- 寫入 off-host immutable storage。
- 使用獨立 signing key。
- 與 backup manifest 綁定。
- 不與 DB 共用相同寫入權限。

Restore drill 應驗證 internal chain、external checkpoint、signature 與 backup freshness。

目前已提供本機簽名 checkpoint、backup-manifest binding、
`restore-verify --audit-anchor` verifier，以及可選 signed-anchor gate 的
`scripts/restore_drill.py`；尚未替 production provision off-host immutable
storage、獨立金鑰輪替或 freshness schedule。

Audit export 的本機 evidence surface 也已收斂：DB telemetry、
`scripts/audit_export_status.py` 與 `/operations/metrics` 共用同一 projection；
任何 backlog 顯示 `attention`，retry ceiling 後的 dead-letter 會顯示
`alert.active=true`、`severity=critical`、`dead_letter_present`。這仍只是
可機讀訊號，不代表已接上 production notifier、alert owner 或外部 immutable
anchor，因此 Production Gate 的 dead-letter／external 項目維持未完成。


# 4. 架構優化方向

## 4.1 將 Repository／UoW 變成真正 Transaction Boundary

目前 repository 多數仍代理原本 `Database` facade。短期可接受，但長期應走：

```text
Route／CLI
→ Application Service
→ Unit of Work
→ Repository
→ Domain State Transition
→ Durable Audit／Outbox
→ Commit
```

建議新增：

```text
dispatch_center/application/
  approvals/
  executions/
  nodes/
  projects/
  datasets/
  identities/
```

新功能不得再直接擴大 `Database` facade。每個 use case 應由 application service 擁有 transaction boundary。

## 4.2 逐步縮小 `app/main.py`

建議結構：

```text
dispatch_center/
  api/
    routers/
    schemas/
    dependencies/
    presenters/
  application/
    commands/
    queries/
    services/
  domain/
    execution/
    approval/
    node/
    project/
  infrastructure/
    db/
    audit/
    ssh/
    node/
```

Router 只負責解析 request、取得 identity、呼叫 service、轉換 response，不直接控制 transaction、SQL 或 remote side effect。

## 4.3 Worker Loop 依責任拆分

```text
dispatch-worker
  execution outbox
  audit export outbox
  reconciliation
  terminal projection
  result recovery
```

每個 loop 應有：

- durable lease。
- fencing token。
- max batch。
- retry policy。
- backpressure。
- readiness signal。
- last-success timestamp。
- pending／processing／dead-letter metrics。

2026-08-06 本機 slice：`AUDIT_EXPORT_WORKER_ENABLED=true` 且
`PROCESS_ROLE=all|worker` 時，`AppState` 會啟動受監督的 audit-export loop；
它重用 `audit_export_operations` 的 durable lease/CAS、批次上限與既有
retry/dead-letter contract，並把 loop error、last tick 與 last-success 納入
readiness telemetry；audit exporter 連續無成功 delivery 或存在 dead-letter
時會 fail closed，單純 pending backlog 仍只顯示 attention。
預設仍為 false，手動 `dispatch db audit-export` 不變；這只提供可回滾的本機
delivery worker，不把 runtime backlog、external anchor 或 production alert
誤宣稱為已完成。

同日 read-only SQLite online backup evidence：目前 runtime copy 的 20 筆
`pending` export operations 由一輪 worker drain 全部轉成 `exported`，
`failed=0`、`dead_letter=0`、`--require-clear` 通過；原始 runtime DB 沒有被
修改，因此 Definition of Done 的 production backlog gate 仍維持未勾選。

同日 execution outbox/ownership loop 也補上 success freshness：只有實際持有
durable scheduler lease 的 process，在 outbox delivery 與 completion recovery
一輪都正常完成後才更新 `last_success`；失敗迭代只增加 error telemetry，不會
把 heartbeat 當成成功。`/readyz` 對 current owner 要求三個 cadence 內有成功
迭代；非 leader 仍可 serve read/approval，pending/uncertain outbox 只標記
`attention`，不把未知遠端結果改判成 Job failure。這個切片的 focused
health/background/execution coverage 為 75 passed，仍不代表正式 outbox
rollout 或 production SLO 已啟用。

另提供 `scripts/execution_outbox_status.py` 作為同一 projection 的 read-only
證據 gate：它同時列出 `execution_operations` 與
`execution_completion_operations` 的 state／operation 分布、未收斂 backlog、
uncertain 數量、processing／uncertain age 與 terminal failed 計數；`--require-clear`
只在 pending／processing／uncertain 的本地工作仍存在時失敗。它不 claim lease、
不執行 SSH／Node side effect，也不把 terminal failed、未知遠端狀態或一次性
查詢誤宣稱成 production alert／SLO readiness。

## 4.4 Feature Flag Metadata

每個 flag 應有：

- default。
- dependency。
- incompatible combinations。
- owner。
- rollout state。
- retirement date。
- deprecated alias。
- startup validation。
- readiness exposure。

2026-08-06 本機 slice：`app.settings.FeatureFlagSpec` 已提供
`rollout_state`、`incompatible_with` 與 roadmap-facing `retirement_date`；
registry import gate 會拒絕重複 key、空 rollout state、自我衝突與未知衝突
參照，`Settings.feature_report()` 會以 non-secret read-only 形式暴露這些欄位。
未經核准的退場日期維持 `null`，且既有 flag default／startup 行為不變。這個
slice 不代表外部 readiness、canary 或 production rollout 已完成。


# 5. 測試與 CI 建議

## 5.1 必須新增的測試

### Audit Export

- active final lease 不被提前 dead-letter。
- expired final lease 才進 dead-letter。
- append success／receipt failure duplicate recovery。
- two-worker race。
- filesystem full。
- permission denied。
- partial JSONL tail。
- manual replay audit。

### Migration

- migration content checksum drift。
- version gap。
- duplicate version。
- unknown future version。
- partial DDL rollback。
- concurrent process migration。
- backup restore with old schema。
- restore with corrupt audit chain。

### Node Protocol

- missing header。
- incompatible version。
- probe capabilities mismatch。
- response version mismatch。
- deprecated missing-header telemetry。

### Node Isolation

- real systemd transient unit。
- environment secret isolation。
- control evidence immutability。
- cgroup stop。
- Agent restart recovery。
- unit cleanup。
- resource limit。
- unsupported host fail closed。

### Audit Adoption

- route mutation coverage。
- durable action cannot call legacy writer。
- legacy entry requires issue／owner。
- rollback leaves no audit event。
- successful mutation produces exactly one event。

## 5.2 GitHub CI 必須成為 Required Check

建議 required checks：

```text
lint
typecheck
unit-core
integration
migration
security-contract
package-smoke
frontend-smoke
full-offline-suite
```

Branch protection：

- 所有 required checks 綠燈。
- PR 非 draft。
- 至少一位 reviewer approval。
- Security-sensitive path 觸發 CODEOWNERS review。
- Branch 必須 up-to-date。
- 禁止直接 push `main`。
- 禁止 merge 未解決 review thread。

## 5.3 Coverage

目前總 coverage 約 39%，建議優先提高 critical state machine 的 branch coverage：

- execution transitions。
- node lease／ack／terminal。
- authorization enforcement。
- migration failure。
- audit outbox。
- recovery／rollback。
- isolation backend selection。

建議目標：

```text
overall line coverage >= 40%
critical modules branch coverage >= 80%
```


# 6. PR 拆分建議

PR #21 範圍過大，建議拆成：

## PR-A：Packaging／Settings／CI

- pyproject
- requirements split
- wheel boundary
- CLI entry points
- typed settings
- CI lint／type／coverage
- CODEOWNERS／PR template

## PR-B：API Router／Authorization

- router registry
- schemas
- request ID
- API error boundary
- authorization enforce
- row filtering

## PR-C：Migration／Repository／UoW

- migration runner
- DB CLI
- migration checksum
- ledger gap check
- repository protocols
- Unit of Work

## PR-D：Durable Audit

- audit schema
- hash contract
- export outbox
- adoption catalog
- API evidence quality
- active lease dead-letter 修正
- audit tests

## PR-E：Node Protocol

- protocol header
- 426 handling
- probe
- client/server contract
- missing-header policy

## PR-F：Node Isolation／Worker

- isolation backend interface
- systemd transient runtime 接線
- control evidence split
- worker role
- real Linux integration evidence

## PR-G：Frontend Smoke

- frontend smoke script
- JavaScript syntax gate
- 不包含 backend/security behavior change


# 7. 建議實作順序

## 第一階段：讓 PR 可安全審查

1. 拆分 PR #21。
2. 保留已完成的 active processing dead-letter、migration checksum/ledger
   與 missing-header 修正之 regression evidence。
3. 將 GitHub CI 設為 required 並取得 reviewer。

## 第二階段：完成 Node 隔離

1. 在指定 non-production host 安裝固定版本 Node wheel。
2. 以 `systemd` 模式執行 read-only probe 與 isolated smoke。
3. 完成 restart／response-loss／rollback drill。
4. 取得正式 Node canary evidence；SSH backend 仍保留 fallback。

## 第三階段：正式 Canary

1. 選定 non-production worker。
2. 安裝固定版本 wheel。
3. 保存 package digest。
4. 執行 read-only probe。
5. 執行 isolated single-job smoke。
6. 執行正式 Node canary。
7. response-loss drill。
8. Agent restart drill。
9. Control Plane restart drill。
10. rollback drill。
11. 確認 SSH 保持 fallback。
12. 逐步啟用 rollout flag。

## 第四階段：全面 Audit Migration

1. Approval。
2. Identity。
3. Server／Node lifecycle。
4. Run／stop／terminal。
5. Project／dataset／engineering。
6. External checkpoint。
7. Segment retention。
8. Legacy JSONL 退場計畫。已新增 default-on 的
   `LEGACY_AUDIT_JSONL_ENABLED` typed switch；它只可關閉已具 durable
   materialization／intent-outcome event 的 standalone enqueue／unbound
   coding-task／unpinned server 相容摘要，
   不影響 durable DB event 或 reject/error evidence。真正退場仍須先完成
   external export、retention、restore evidence 並取得 operator approval，
   不能以本機測試取代。


# 8. Definition of Done

## PR Merge Gate

- [ ] PR 已拆分成單一可審查責任。
- [x] PR 非 draft（PR #21 已 Ready for review）。
- [x] Required CI 全部通過（exact candidate `f0bf0c7` 的 push run
      `31064358200` 與 pull-request run `31064360956` 的 `python-tests` 均成功）。
- [ ] 有 reviewer approval。
- [ ] CODEOWNERS 規則生效。
- [ ] 無未解決 review comments。
- [x] 所有 migration 有 immutable checksum。
- [x] Migration ledger gap fail closed。
- [x] Audit export active lease 不會提前 dead-letter。
- [x] Missing Node protocol header 行為已固定。
- [x] 文件沒有把 local evidence 寫成 production evidence。

## Node Enable Gate

- [ ] Node Agent wheel 已安裝。
- [x] `--check` 通過（本機 user-systemd host）。
- [x] `--probe` contract 已有本機測試。
- [x] isolation mode 選擇與 strict systemd fail-closed 已接線。
- [ ] direct fallback 在 canary host 關閉。
- [x] workload 讀不到 token（本機 integration evidence）。
- [x] workload 無法修改 control evidence（本機 integration evidence）。
- [ ] workload 無法 signal Agent（需指定 canary host evidence）。
- [x] stop 可終止整個 cgroup（本機 integration evidence）。
- [x] restart recovery 通過（本機 integration evidence）。
- [ ] 正式 canary 通過。
- [ ] rollback drill 通過。

## Durable Audit Production Gate

- [ ] 所有高風險 mutation 已 durable（目前仍有明確 legacy entries）。
- [x] Adoption catalog 對 required mutation families 有明確 entry。
- [x] 每筆 legacy entry 有 owner／issue。
- [ ] Audit export outbox 無 pending backlog。
- [ ] Dead-letter 有 alert。
- [ ] External checkpoint 已啟用（local signed verifier 已完成）。
- [ ] Restore drill 驗證 external anchor。
- [ ] Retention policy 已核准。


# 9. 建議目前狀態文字

> 本機架構基礎與 P0/P1 code slices 已完成，offline regression suite 目前穩定；PR #21 已 Ready for review 且遠端 required CI 通過。Durable audit 仍為部分遷移；Node workload isolation 已接入 Agent runtime 並有本機 user-systemd evidence，但尚需指定 worker 的 rollout、canary 與 rollback 證據。目前不宣稱 production deployment、外部 audit anchor 或正式 Node canary。合併前仍需拆分目前過大的 PR、CODEOWNERS／review comments 核對與 reviewer approval。

# 10. 最終優先級

## 立即修正

1. 將 remaining legacy mutation families 逐一移入同一 transaction boundary。
2. 拆分 PR #21 並取得遠端 required checks／reviewer evidence。

## Node Canary 前

1. 指定 non-production worker 並安裝固定 wheel。
2. 執行 systemd probe、isolated smoke、restart 與 response-loss drill。
3. 完成 rollback drill；維持 SSH fallback。

## Production 前

1. Full-domain durable audit。
2. External audit anchor 與獨立金鑰管理。
3. 正式 Node canary。
4. Rollback drill 與 operational alert／SLO／dead-letter runbook。
