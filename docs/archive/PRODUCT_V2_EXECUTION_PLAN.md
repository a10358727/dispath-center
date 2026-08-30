# Dispatch Center Product v2 可執行計畫

> archived: 2026-08-30 · superseded_by: `docs/PLATFORM_CHARTER.md`（定位／架構／不變式）、`docs/CAPABILITY_LEDGER.md`（能力現況）、`docs/DECISIONS.md`（裁定）· 本檔為歷史證據，不是現況。

> 文件狀態：已完成產品與交付決策，供後續工作包實作、審核與驗收使用。
>
> 建立日期：2026-08-06
>
> 基準分支：`codex/product-v2-plan`
>
> 基準 commit：`d0aac3d1494a120f7365fbbb0319b679c0c5c3a5`
>
> 第一個交付環境：117 非 production SSH Pilot

---

## 1. 文件目的

本文件將
`docs/DISPATCH_CENTER_CURRENT_SYSTEM_AND_IMPROVEMENT_PLAN.md`
中的產品願景，整理成可以直接拆工作包、建立 PR、測試、部署及驗收的執行計畫。

原文件繼續作為 Product Brief 與 UX roadmap；本文件才是 Product v2
的實作與交付依據。

本計畫不取代既有安全正典。發生衝突時依下列順序處理：

1. `.claude/skills/dispatcher-domain/references/invariants.md`
2. `docs/DECISIONS.md`
3. 目前程式碼與邊界測試
4. `docs/CAPABILITY_LEDGER.md`
5. 本執行計畫
6. 其他歷史 roadmap／status 文件

若實作需要改變 protected invariant，該工作包必須停止，先提出具名
Decision Gate，不得在功能 PR 中順便改變語意。

---

## 2. 執行摘要

產品方向可行：

> 將 Dispatch Center 從遠端執行工程控制台，改造成以 Project、Run、
> Dataset 為中心的內部 AI／GPU 工作台。

但現有 Product Brief 不能直接照表實作，因為它混合了：

- 已完成的本機程式能力。
- 預設關閉、尚未部署的能力。
- 尚未通過 Canary 的能力。
- 真正需要新增的產品功能。
- Production 前的外部治理與營運 gate。

如果不先重新基準化，會重建 OIDC、Run Profile、ExecutionPlan、
Dataset Snapshot 等既有能力，形成兩套 API、資料表與狀態真相。

Product v2 採取以下原則：

- 重用既有 canonical domain model。
- 新產品介面使用 additive `/api/v2`。
- 舊 API 保留有限 rollback 期，不立即破壞既有 client。
- 第一個里程碑在 117 使用 SSH backend。
- Node rollout 與 production readiness 是後續獨立 gate。
- 所有 approved execution payload 保持不可變。
- Unknown／unreachable 不推定為 failed。
- SSH 永久保留為支援與緊急回退通道。

---

## 3. 現況重新基準化

| 能力 | 目前真實狀態 | Product v2 動作 |
| --- | --- | --- |
| OIDC Authorization Code + PKCE | 已本機實作、預設關閉、未部署 | 啟用、接真實非 production IdP、驗證 rollback |
| Server-side browser session | 已實作 absolute TTL、logout revocation | 沿用；第一版不擴張其他 session revoke |
| Authorization enforce | 已實作、預設 `off` | 117 Pilot 啟用 `enforce` |
| Project Workspace | 已有七個 pane 與 supporting APIs | 整合成 v2 Project-centered experience |
| Project membership | 已有單一 `admin/operator/viewer` | Additive 演進成可複數五角色 |
| Run Profile | 已有 immutable revisions | 作為 Run Template canonical model |
| ExecutionPlan | 已有 preview、request、approval binding、lineage | 延伸 v2 typed contract，不另建第二套 Run 真相 |
| Dataset registry | 既有、不可宣稱 reproducible | 保留 legacy label，不供 v2 reproducible Run 使用 |
| Dataset Snapshot | 已有 content-addressed publish、預設關閉 | 作為 Dataset Version canonical model |
| Dataset alias／sharing／usage | 尚未實作 | Product v2 新增 |
| Node isolation | 本機程式與 integration evidence 已有 | 第一個 Pilot 不啟用；後續正式 Canary |
| Audit race／migration checksum／ledger gap | 已完成 | 不重做，只保留 regression gate |
| External audit anchor | 只有本機 signer/verifier | Production gate 新增 off-host anchor |

狀態聲明固定使用：

- `implemented`：程式或操作工具存在。
- `enabled`：目標環境已開啟該功能。
- `deployed`：有 exact artifact／commit 的部署證據。
- `canary-proven`：通過核准的真實環境與時間窗 gate。
- `production-ready`：部署、Canary、rollback、營運與安全 gate 全部完成。

本機測試通過不等於 deployed、canary-proven 或 production-ready。

---

## 4. 已核准的產品決策

### 4.1 文件與分支

- 原文件保留為 Product Brief。
- 本文件是 Product v2 執行計畫。
- 從基準 commit 建立 `codex/product-v2-plan`。
- 不再擴大 PR #21。
- 後續每個工作包使用獨立小型分支與 PR。
- 不直接修改 main。

### 4.2 第一個交付里程碑

第一版不是單純登入 demo，而是包含 Dataset 的完整垂直 Pilot：

```text
OIDC Login
→ My Workspace
→ Project Bootstrap
→ Environment / Run Template / Defaults
→ Dataset Publish / Alias / Sharing
→ Run Preview
→ Approval
→ SSH execution on 117
→ Log / Artifact metadata / Result
→ Run output published as Dataset
→ Lineage / Usage / Compare
```

第一個 Pilot 明確不啟用 Node backend。

### 4.3 API 策略

- 新產品介面使用 `/api/v2`。
- v2 UI 只呼叫 v2 API。
- 舊 API 不再增加產品功能。
- 舊 API 透過 compatibility flag 保留一個穩定 release 作 rollback。
- 通過 client inventory、至少一個穩定 release，以及連續 30 天零呼叫後，
  才能另開 breaking-change PR 移除。

### 4.4 五角色與複數指派

同一使用者在同一 Project 可以持有複數角色，權限取聯集。

| 角色 | 可執行能力 |
| --- | --- |
| Owner | 專案資料、成員、分享接受／撤銷、project approval |
| Operator | Environment、Run Template、Defaults、Run、Clone、Stop |
| Reviewer | 查看與決定 project-scoped approval |
| Dataset Manager | Dataset publish、alias、lineage、share offer |
| Viewer | Project、Run、Dataset、Artifact、Activity 唯讀 |

共同規則：

- Owner 與 Reviewer 均可決定 project-scoped approval。
- `enqueue`、`stop` 保留現有非 high-risk 語意。
- 其他新增 approval kind 一律 high-risk。預設要求請求與決定者分離；小型可信任
  團隊可用 `ALLOW_HIGH_RISK_SELF_APPROVAL=true` 明確允許具既有決定權限的人類
  核准自己的請求。此開關不取消 approval、immutable payload、RBAC、service
  actor 禁令或 durable audit。
- Service actor 不可取得 Owner／Reviewer，也不可決定 approval。
- 每個 Project 至少保留一名 Owner。
- 每個 Project 至少有兩名不同的人具 Owner／Reviewer 能力，避免高風險流程鎖死。
- Platform Admin 是否受 high-risk self-decision 限制，由同一部署開關決定。

Legacy migration：

| 舊角色 | v2 角色 |
| --- | --- |
| `admin` | Owner + Operator + Reviewer + Dataset Manager |
| `operator` | Operator |
| `viewer` | Viewer |

Migration 必須標示 `legacy_membership` provenance；沒有原始 approval ID
就維持 `NULL`，不得偽造歷史。

### 4.5 OIDC

- 117 Pilot 使用單一非 production OIDC IdP。
- Identity key 僅為 `(issuer, subject)`。
- Email 只作 metadata。
- OIDC group 只作觀測資料，不在登入時改變 membership 或 platform admin。
- 正常 Pilot 使用 `AUTHORIZATION_MODE=enforce`。
- 正常狀態停用 legacy shared token。
- IdP 故障時才依 runbook 暫時啟用相容 token／shadow rollback。
- Service token 與 Node credential 不受 browser login 改動影響。

第一版只承諾：

- 現有 secure／HttpOnly／SameSite session cookie。
- Absolute session lifetime。
- Current-session logout 立即撤銷。

第一版不包含：

- OIDC group 自動同步角色。
- Device Flow。
- 其他 session 遠端撤銷。
- Inactivity timeout。
- 以 email 合併或識別帳號。

### 4.6 Project Wizard

Project Wizard 產生單一 immutable `project_bootstrap_v2` approval。

Approval payload 固定：

- Project UUID、name／slug。
- Repo 或 local path reference。
- 初始角色 assignments。
- 第一個 Environment revision。
- 第一個 Run Template revision。
- 第一個 Project Defaults revision。
- 可選的既有 Dataset grants／aliases。

核准後，所有本機 DB 資源在同一 transaction 建立。任何一步或 durable
audit append 失敗時整體 rollback。

Git init、remote deploy、server bootstrap 等遠端副作用仍使用既有獨立
approval，不併入 bootstrap transaction。

### 4.7 Dataset 分享

- 每個 Dataset asset 有一個 owning Project。
- 分享使用雙邊兩階段核准。
- 來源 Project 先核准 share offer。
- 目標 Project 再核准 accept。
- 兩次核准都完成後才建立 project grant。
- Share offer 固定 exact published snapshot IDs。
- 未來新增 snapshot 不會自動分享。
- Alias 是 project-scoped immutable revision。
- 目標 Project 可以替已取得 grant 的 snapshot 建自己的 alias。
- 撤銷阻止新 Run，不改寫歷史 Run。
- 已核准或執行中的 Job 不會因 grant 撤銷而被暗中改派或改成 failed。

### 4.8 Environment 與 Resource

117 SSH Pilot 的 Environment Revision 是 host environment contract：

- 固定 setup command。
- Required server tags。
- `project_checkout` working-directory policy。
- 非秘密 env allowlist。
- Secret reference 名稱，不保存 secret value。
- Closed-set typed preflight。

Preflight 不接受任意 shell command，只支援：

- Required executable present。
- Required non-secret env／secret reference present。
- Project-relative required path present。
- Server tags 與資源證據符合。

第一版不建立 Container 或 Conda，也不宣稱 process hostile isolation。

Resource v1 只做整台 worker eligibility：

- Required tags。
- Minimum GPU count／GPU memory。
- Minimum available RAM。
- Minimum available disk。
- `exclusive_worker=true`。

Unknown 或 stale observation 會使新 preview 無法宣稱 ready，但不會把既有
Job 判成 failed。

---

## 5. Canonical Product v2 模型

| Product v2 名稱 | Canonical model |
| --- | --- |
| User | `actors` |
| OIDC Identity | `oidc_identities` |
| Browser Session | `actor_sessions` |
| Project | 既有 Project UUID，加 v2 adapter |
| Run Template | `run_profiles` immutable revision + typed spec |
| Environment | 新增 immutable environment revision |
| Project Defaults | 新增 immutable default revision |
| Dataset Version | 既有 published `dataset_snapshots` |
| Run | `execution_plan → approval → job → attempt` lineage |
| Product Run State | 從既有 durable state 投影 |

禁止建立以下平行真相：

- 不另建第二套 `users`／`browser_sessions`。
- 不以新 `runs` table 取代 ExecutionPlan／Job／Attempt。
- 不以新 `dataset_versions` table 取代 DatasetSnapshot。
- 不把 Run Template 實作成與 Run Profile 無關的第二套 revision head。

---

## 6. Run Template v2 Contract

Run Template v2 以現有 Run Profile revision 為 identity，新增一對一 typed spec。

Spec 包含：

- `contract_version`
- `environment_revision_id`
- `argv_template`
- `parameter_schema`
- `resource_requirements`
- `output_declarations`
- `spec_digest`

參數第一版只支援：

- string
- integer
- number
- boolean
- enum

每個欄位必須有長度、範圍或 allowlist 限制。第一版不接受 nested object、
arbitrary JSON 或 secret value。

`argv_template` 僅包含 literal 與 parameter reference。Server 使用確定性
compiler 產生最終 command bytes；不得直接把使用者字串插入 shell。

Output declaration：

- 只能是 result root 下的相對路徑或 bounded glob。
- 禁止 absolute path。
- 禁止 `..`。
- 固定 output name、kind、path pattern 與 required／optional。
- Run output 發布成 Dataset 時必須引用此 declaration。

既有沒有 typed spec 的 Run Profile 標記為 `legacy_raw_command`：

- 可由 legacy adapter 讀取。
- 不可用於 Product v2 One-click Run。
- 不自動生成 parameter schema、environment 或 output definition。

---

## 7. ExecutionPlan v2 Contract

Product Run identity 沿用 `execution_plans.id`。

v2 companion contract 固定：

- Project UUID。
- Exact ProjectVersion。
- Exact Run Profile revision。
- Run Profile spec digest。
- Exact Environment revision。
- Canonical typed parameter values。
- Exact Dataset Snapshot IDs 或 explicit `dataset_none`。
- Project-scoped alias 在 preview／submit 時解析出的 exact snapshot。
- Exact Dispatch Policy revision或 explicit server-config revision。
- Exact target server-config revision。
- Backend：第一個 Pilot 固定 `ssh`。
- Compiled command SHA-256。
- Resource requirements。
- Output declarations digest。
- Overall plan digest。

流程：

1. Preview 解析 defaults、alias、revision 與 eligibility。
2. Preview 完成 target selection。
3. Preview 回傳完整 canonical body 與 plan digest，不建立資料。
4. Submit 必須帶 `expected_plan_digest`。
5. Server 重新推導所有內容。
6. 不一致時回 409，且不建立 ExecutionPlan 或 approval。
7. 一致時原子建立 ExecutionPlan v2 與 pending enqueue approval。
8. 核准時再次查詢所有 exact revision、snapshot grant、target 與 command digest。
9. 重驗成功才建立至多一個 target-pinned Job。

核准後不得：

- 重新解析 alias 成另一個 snapshot。
- 重新選擇 server。
- 改變 backend。
- 使用 template／environment 的新 head。
- 修改 compiled command。

---

## 8. Product Run State

不修改 canonical `jobs.status` 封閉集合。

| Product state | Durable evidence |
| --- | --- |
| `draft` | 只存在於 client／preview response，不持久化 |
| `awaiting_approval` | ExecutionPlan approval pending |
| `rejected` | ExecutionPlan approval rejected |
| `queued` | Job queued，沒有 active preparation |
| `preparing` | Attempt 正在 claim／prepare／launch |
| `running` | Canonical Job／Attempt running |
| `stopping` | Stop approval／stop intent 尚未取得 terminal evidence |
| `succeeded` | Canonical Job `done` |
| `failed` | Canonical Job `failed`，且有合法 terminal evidence |
| `cancelled` | Canonical Job `cancelled` |
| `blocked` | Canonical Job `blocked` |
| `needs_attention` | Unknown、stalled、collection 問題或其他非 terminal attention |

`needs_attention` 是 presentation state，不能用來自動重派、改成 failed
或覆寫 canonical Job。

Clone 只建立新的 preview；使用者確認後才建立新 request。

Compare 是唯讀，顯示：

- ProjectVersion 差異。
- Template／Environment revision 差異。
- Parameters 差異。
- Dataset snapshot 差異。
- Resource／target 差異。
- Terminal result 與 Artifact metadata 差異。

---

## 9. Dataset v2 Contract

### 9.1 Dataset asset

Dataset asset 是 Project-centered 管理外殼；實際不可變版本仍是
`dataset_snapshots`。

新 Dataset 必須有：

- Asset UUID。
- Owning Project UUID。
- Name／description。
- Data Card。
- Published snapshot links。
- Project grants。
- Project-scoped aliases。
- Lineage edges。

Existing legacy registry 或 snapshot 不會自動取得 owner。它們維持
`legacy_unscoped`，需要高風險 adoption approval 才能成為 v2 asset。

### 9.2 Publish

第一版支援兩種 source：

1. Server A 本機路徑。
2. 已完成且 result collection 成功的 Run output。

Publish preview 是唯讀掃描，回傳：

- Source identity。
- File count／total bytes。
- Per-file manifest digest。
- Store revision。
- Shard policy。
- Expected snapshot identity。

Publish request 必須帶 expected preview digest。Server 重新掃描不一致時
回 409，不建立 approval。

Material publish 重用現有 Dataset Snapshot builder：

- Human approval。
- Approve-time source re-verification。
- Content-addressed shards。
- Staging verification。
- Atomic publish。
- Published row／manifest／shard mapping 不可修改。

### 9.3 Alias

- Alias 是 `(project_id, asset_id, alias_name)` 下的 immutable revisions。
- 每次移動 alias 都新增 revision。
- Alias mutation 是 high-risk approval。
- Run submit 後只保留 resolved snapshot ID，不保留可漂移的 alias target。

### 9.4 Lineage 與 usage

Run output publish 時寫入 immutable lineage edge：

- Producing ExecutionPlan ID。
- Input Snapshot IDs。
- Output Snapshot ID。
- Output declaration。
- Publish approval ID。

Lineage graph 必須拒絕 cycle。

Usage 不建立可手動更新的第二套統計表；由下列 canonical references 查詢：

- ExecutionPlan v2 dataset bindings。
- Project Default revisions。
- Active project grants／aliases。

### 9.5 分享

來源流程：

```text
Dataset Manager 建立 share-offer request
→ Owner／Reviewer 核准
→ 產生 immutable offer
```

目標流程：

```text
Target Owner 建立 accept request
→ 另一位 Target Owner／Reviewer 核准
→ 建立 exact snapshot grant
```

Offer／accept payload 必須包含：

- Asset ID。
- Source／target Project ID。
- Exact Snapshot IDs。
- Offer digest。
- Expiry。

Target alias 只能指向 grant 中仍可供新 Run 使用的 snapshot。

---

## 10. Public API v2

### 10.1 共通規則

- Base path：`/api/v2`
- Project 使用 UUID。
- Mutation request 必須帶 `Idempotency-Key`。
- 同 key／同 actor／同 route／同 payload 回傳原結果。
- 同 key 不同 payload 回 `409 idempotency_key_reused`。
- Revision mutation 帶 `expected_revision`。
- Preview 完全唯讀。
- Pending request 回 HTTP 202。
- 建立 read model resource 回 HTTP 201。
- 危險或不合法輸入回 400，且不建立 approval。
- Authentication failure 回 401。
- Authorization failure 回 403；需要隱藏存在性的資源回 404。
- Revision、digest、state conflict 回 409。
- Schema validation 回 422。
- List 使用 bounded cursor pagination，預設 50、上限 100。

Error envelope 沿用既有契約：

```json
{
  "error": {
    "code": "plan_digest_mismatch",
    "message": "Execution plan verification failed",
    "request_id": "<server-generated UUID>",
    "details": {}
  }
}
```

### 10.2 Identity／Workspace

```text
GET  /api/v2/me
GET  /api/v2/me/sessions
GET  /api/v2/workspace
```

`/api/v2/workspace` 回傳：

- Caller roles。
- Scoped Projects。
- Recent Runs。
- Pending approvals caller 可查看／決定的摘要。
- Recent Dataset assets。
- Capability／feature state。

### 10.3 Project

```text
POST /api/v2/projects/bootstrap-previews
POST /api/v2/projects/bootstrap-requests
GET  /api/v2/projects/{project_id}/workspace
GET  /api/v2/projects/{project_id}/roles
POST /api/v2/projects/{project_id}/role-change-requests
GET  /api/v2/projects/{project_id}/environments
POST /api/v2/projects/{project_id}/environment-change-requests
GET  /api/v2/projects/{project_id}/run-templates
POST /api/v2/projects/{project_id}/run-template-change-requests
GET  /api/v2/projects/{project_id}/defaults
POST /api/v2/projects/{project_id}/default-change-requests
```

### 10.4 Run

```text
POST /api/v2/projects/{project_id}/run-previews
POST /api/v2/projects/{project_id}/run-requests
GET  /api/v2/runs/{plan_id}
POST /api/v2/runs/{plan_id}/clone-previews
GET  /api/v2/runs/compare
POST /api/v2/runs/{plan_id}/stop-requests
GET  /api/v2/runs/{plan_id}/artifacts
```

### 10.5 Dataset

```text
GET  /api/v2/projects/{project_id}/datasets
POST /api/v2/projects/{project_id}/dataset-publish-previews
POST /api/v2/projects/{project_id}/dataset-publish-requests
GET  /api/v2/dataset-assets/{asset_id}
GET  /api/v2/dataset-assets/{asset_id}/lineage
GET  /api/v2/dataset-assets/{asset_id}/usage
GET  /api/v2/dataset-assets/{asset_id}/storage
POST /api/v2/dataset-assets/{asset_id}/alias-change-requests
POST /api/v2/dataset-assets/{asset_id}/share-offer-requests
POST /api/v2/dataset-share-offers/{offer_id}/accept-requests
POST /api/v2/dataset-grants/{grant_id}/revoke-requests
```

### 10.6 Approval

```text
GET  /api/v2/approvals
GET  /api/v2/approvals/{approval_id}
POST /api/v2/approvals/{approval_id}/decisions
```

Decision body：

```json
{
  "decision": "approve",
  "note": "reviewed"
}
```

Server 必須重新判斷 scope、role、self-decision、payload digest 與當下狀態；
不得信任 UI 顯示的角色或 approval metadata。

---

## 11. Additive Migration Plan

目前基準 schema version 為 4。Product v2 預留以下順序：

### Migration v5 — API Idempotency

新增：

- `api_idempotency_keys`

固定：

- Actor ID。
- Route key。
- Hashed idempotency key。
- Request SHA-256。
- Result resource type／ID。
- Created／expires timestamps。

### Migration v6 — Multi-role RBAC

新增：

- `project_role_bindings`

固定：

- Project／actor／role identity。
- Grant provenance。
- Grant approval ID。
- Revocation approval ID。
- Granted／revoked timestamps。
- Partial unique active binding。

Migration 依既有 role 做確定性映射，但不偽造 actor、approval 或時間。

### Migration v7 — Project Experience

新增：

- `project_environments`
- `environment_revisions`
- `run_profile_specs`
- `project_default_revisions`

Environment、spec、defaults 使用 immutable revision；existing Run Profile
沒有 spec 時維持 legacy。

### Migration v8 — Dataset Governance

新增：

- `dataset_assets`
- `dataset_asset_snapshots`
- `dataset_share_offers`
- `project_dataset_grants`
- `dataset_alias_revisions`
- `dataset_lineage_edges`

Existing Snapshot 不自動分配 owner 或 asset。

### Migration v9 — ExecutionPlan v2

新增：

- `execution_plan_v2_specs`

以 `execution_plan_id` 一對一固定 Product v2 contract，不改寫既有
ExecutionPlan v1 rows。

### Migration 共通 Gate

每個 migration 必須驗證：

- Fresh DB。
- 代表性 schema v4 upgrade。
- Content checksum。
- Ledger gap fail closed。
- 兩個 connection 競爭。
- 失敗 transaction rollback。
- Reopen idempotency。
- 舊版本資料保持誠實的 legacy／unknown。
- 新版程式關閉 feature flag 時仍可讀舊資料。

不提供 destructive down migration。Rollback 關閉新 feature，不刪除
additive table、published snapshot、approval 或 execution evidence。

---

## 12. 工作包與 PR 順序

每個 PR 只對應一個工作包，不跨包順手重構。

### PR-00 — Plan and Decision Baseline

交付：

- 本執行計畫。
- Product Brief 連結與 current-state 修正。
- Capability Ledger authorization 狀態修正。
- RBAC、API cutover、Dataset sharing、Project bootstrap decision record。

驗收：

- 文件 authority order 一致。
- 不把 local evidence 寫成 deployed／production。
- `git diff --check` 與文件／static gates 通過。

### PR-01 — API v2 Foundation

交付：

- v2 router boundary。
- APIError、request ID。
- Cursor pagination。
- Idempotency middleware／repository。
- `API_V2_ENABLED=false`。

非目標：

- 不加入任何產品 resource。
- 不改舊 API response。

### PR-02 — Multi-role RBAC

交付：

- Migration v6。
- 五角色 action matrix。
- Multi-role RequestContext。
- Legacy role migration。
- v1 adapter 也使用 action matrix，不把 Reviewer 誤映射成 legacy admin。
- Role request／approve flows。

驗收：

- Cross-project fail closed。
- Union permissions 正確。
- Service actor restrictions。
- Last Owner／approval-capable pair protection。
- High-risk self-decision。

### PR-03 — OIDC and My Workspace

交付：

- `/api/v2/me`、sessions read view、workspace。
- OIDC v2 UI shell。
- Role-aware navigation。
- Feature／capability status。
- Legacy token 不進 browser storage。

驗收：

- 真實非 production IdP login／logout。
- Disabled actor 不取得新 session。
- OIDC failure 不降級 anonymous。
- Enforce mode cross-project denial。
- `OIDC_ENABLED=false` rollback。

### PR-04 — Project Bootstrap

交付：

- Bootstrap preview。
- `project_bootstrap_v2` approval。
- 原子 Project／roles／initial resources materialization。
- Project Workspace read model。

驗收：

- Name／UUID conflict revalidation。
- Injected durable-audit failure 整體 rollback。
- Requester 不可自批。
- 遠端 Git／deploy 不在 bootstrap transaction。

### PR-05 — Environment Revisions

交付：

- Migration v7 environment 部分。
- Host Environment immutable revision。
- Typed preflight。
- Secret reference metadata only。
- Create／update／archive approval。

驗收：

- Dangerous setup request-time reject。
- Approve-time revalidation。
- Missing／unknown host evidence 誠實顯示。
- Secret value 不進 API、DB、audit 或 log。

### PR-06 — Run Template Specs and Defaults

交付：

- Run Profile typed spec。
- Structured argv compiler。
- Parameter／resource／output schema。
- Immutable Project Default revision。
- Legacy Run Profile 誠實分類。

驗收：

- Shell injection／invalid type 拒絕。
- Compiler deterministic golden tests。
- Stale template／environment head 409。
- Existing raw profiles 不自動補造 spec。

### PR-07 — Dataset Assets, Aliases and Lineage

交付：

- Migration v8 core tables。
- Project-owned asset。
- Immutable alias revisions。
- Lineage／usage read models。
- Legacy snapshot adoption request。

驗收：

- Published snapshot 不可修改。
- Alias movement 不改歷史 Run。
- Lineage cycle 拒絕。
- Legacy unscoped 不洩漏到普通 Project。

### PR-08 — Dataset Sharing

交付：

- Source share offer。
- Target accept。
- Exact snapshot grant。
- Revoke／unlink。
- Cross-project scope filtering。

驗收：

- 單邊核准不建立 grant。
- Offer digest／expiry revalidation。
- 新 snapshot 不自動加入舊 grant。
- 撤銷阻止新 Run，不改寫歷史與 active Job。

### PR-09 — Dataset Publish Wizard

交付：

- Local-path preview／request。
- Run-output preview／request。
- Existing snapshot builder integration。
- Data Card。
- Project alias creation after publish。

驗收：

- Source drift。
- Manifest／shard digest。
- Run result collection incomplete 拒絕。
- Output path escape 拒絕。
- File publish interrupted resume。

### PR-10 — ExecutionPlan v2

交付：

- Migration v9。
- Run preview／submit。
- Alias resolution。
- Resource eligibility。
- Target pinning。
- Plan digest。
- Approval materialization。

驗收：

- Preview 零寫入。
- Expected digest mismatch 零持久化。
- Duplicate submit／approve 不建立第二個 Plan／Job。
- Approve-time stale revision／grant／target 拒絕。
- Target 不在核准後漂移。

### PR-11 — Product Run Experience

交付：

- Run state projection。
- Run detail／timeline。
- Clone preview。
- Compare。
- Stop request。
- Artifact metadata。
- Project Run UI。

驗收：

- Unknown／unreachable 不映射 failed。
- Stopping 不直接改 Job terminal status。
- Clone 不直接執行。
- Compare 不修改任何資料。
- Empty／legacy／partial lineage 正確呈現。

### PR-12 — 117 SSH Pilot

交付：

- Exact release candidate manifest。
- 117 migration／deployment evidence。
- OIDC／RBAC／Project／Dataset／Run E2E。
- 20 jobs／8 hours。
- Response-loss、restart、rollback drills。

### PR-13 — Legacy API Retirement

前置 Gate：

- 至少一個穩定 v2 release。
- 已知 client inventory 全部遷移。
- 連續 30 天 legacy route 零呼叫。
- Rollback artifact 已保存。
- Code Owner 核准 breaking change。

未達 Gate 前只保留 adapter，不移除 supported behavior。

---

## 13. 117 Pilot 計畫

### 13.1 Entry Gate

- PR #21 基準已凍結並取得獨立審核。
- Product v2 所有 PR CI 全綠。
- Exact control-plane／agent wheel 已建置。
- Release manifest 記錄：
  - Git commit。
  - Wheel SHA-256。
  - Package version。
  - Schema version。
  - CI run。
  - Feature flag matrix。
- Runtime DB 已備份並驗證 manifest。
- 117 是明確 non-production worker。
- Server-config revision 與 SSH preflight 已核准。
- OIDC non-production issuer、client、callback、TLS/proxy 已準備。
- 至少兩個 Platform Admin 測試帳號。
- 每個測試 Project 至少兩位 approval-capable human。

### 13.2 Pilot Flag Matrix

正常 Pilot：

```text
OIDC_ENABLED=true
AUTHORIZATION_MODE=enforce
ALLOW_HIGH_RISK_SELF_APPROVAL=false  # trusted-team Pilot 可明確設為 true
LEGACY_SHARED_TOKEN_ENABLED=false
API_V2_ENABLED=true
PRODUCT_RBAC_V2_ENABLED=true
PROJECT_BOOTSTRAP_V2_ENABLED=true
PROJECT_ENVIRONMENTS_V1_ENABLED=true
RUN_TEMPLATE_V2_ENABLED=true
DATASET_SNAPSHOT_V1_ENABLED=true
DATASET_SNAPSHOT_PUBLISH_ENABLED=true
DATASET_WORKSPACE_V2_ENABLED=true
RUN_EXPERIENCE_V2_ENABLED=true
LEGACY_API_COMPAT_ENABLED=false
NODE_AGENT_V1_ENABLED=false
```

Execution attempt SSH flags依當時 approved candidate/runbook 啟用；禁止同時開啟
未經 Canary 的 Node new assignment。

### 13.3 Functional Scenarios

Pilot 必須完成：

1. OIDC 登入／登出。
2. 建立 Project A 與 Project B。
3. 指派並驗證五角色組合。
4. Project A 以本機 path publish Dataset Snapshot。
5. 建立 Project A alias。
6. Project A 建立 share offer。
7. Project B 核准 accept。
8. Project B 建立自己的 alias。
9. Project B 以 shared snapshot 建立 One-click Run。
10. Preview 顯示 exact code／template／environment／dataset／target。
11. 核准後以 SSH 在 117 執行。
12. 查看 Log、Artifact metadata、terminal result。
13. 將成功 Run output publish 為 Project B Dataset。
14. 查看 input→Run→output lineage 與 usage。
15. Clone Run 並修改參數。
16. Compare 兩個 Run。
17. 建立並核准 Stop request。
18. 撤銷 Dataset share，確認只阻止新 Run。

### 13.4 Execution Reliability Window

- 至少 20 個 ordinary jobs。
- 持續至少 8 小時。
- 至少一次 launch 後 response-loss。
- 至少一次 control-plane restart。
- 至少一次 rollback drill。

通過標準：

- Duplicate launch = 0。
- False failure = 0。
- Lost terminal evidence = 0。
- Conflicting terminal evidence = 0。
- Result collection success = 100%。
- Cross-project data leak = 0。
- 結束時 unresolved unknown = 0。
- Approved payload drift = 0。

任何不符合均視為 Pilot fail，不得把部分成功宣稱為 canary-proven。

### 13.5 Rollback

Product rollback：

1. 停止建立新 v2 mutation request。
2. `LEGACY_API_COMPAT_ENABLED=true`。
3. 回退到上一版 static UI artifact。
4. 保留所有 additive schema 與 evidence。

OIDC rollback：

1. 先保留現有有效 session。
2. IdP 無法恢復時，依 runbook 暫時開 legacy token。
3. 必要時將 authorization 回到 shadow。
4. 此降級必須有操作者、開始時間、原因及結束時間 evidence。

Execution rollback：

1. 停止 new attempt claims。
2. Existing attempt reconcile／outbox 保持啟用直到 drain。
3. Owner-free 新工作回 legacy SSH。
4. 不把 active attempt 改派。

Dataset rollback：

1. 停止新的 publish。
2. Published snapshot、blob、manifest、alias revision、grant 保留。
3. 不自動刪除 staging 或 evidence。

---

## 14. 後續 Node 與 Production Lane

Node 不屬於第一個 Product v2 Pilot。

Product v2 SSH 流程穩定後，才依既有 Node Decision Gate 執行：

1. 單一 Node，至少 24 小時／10 jobs。
2. 兩個 Nodes，至少 100 acknowledged jobs／7 天。
3. Control-plane restart。
4. Agent restart。
5. Network loss。
6. Credential rotation。
7. Drain／retire。
8. Emergency revoke。
9. Per-node SSH rollback。

任何 duplicate launch、false failure、lost/conflicting terminal、
cross-target effect 或 unresolved unknown 都使 Node Canary 失敗。

Production-ready 另需完成：

- Full-domain durable audit adoption。
- Audit export backlog／dead-letter 處置。
- Off-host immutable external anchor。
- Independent signing key owner。
- External notifier／alert owner。
- Approved audit／result retention。
- Backup／restore drill with external anchor。
- Approved RPO／RTO／SLO。
- Signed release manifest。
- Production rollout／abort／rollback owner。

在上述 gate 完成前，只能宣稱 non-production Pilot。

---

## 15. 每個 PR 的共同 Definition of Done

每個 PR 必須包含：

- 使用者可觀察 outcome。
- Scope 與 non-goals。
- 影響的 API、schema、state、reason code、feature flag。
- 保持不變的 protected invariants。
- Request-time validation。
- Approve-time revalidation。
- Immutable payload／revision tests。
- Fresh DB migration。
- Representative schema v4 upgrade。
- Injected transaction／audit failure rollback。
- Authentication／authorization／cross-project tests。
- Idempotency／retry／restart tests。
- Unknown／unreachable handling。
- Frontend empty／loading／error／disconnected states。
- Rollout／abort／rollback。
- Capability Ledger evidence update。
- Exact test command 與真實結果。
- Implementer、independent reviewer、deployment／rollback owner。

共同驗證：

```bash
python -m ruff check app agent dispatch_center scripts tests
python -m mypy app agent dispatch_center scripts
python scripts/coverage_gate.py
bash .claude/skills/release-gate/scripts/static_checks.sh
python scripts/audit_adoption_gate.py
python scripts/frontend_smoke.py
node --check static/ui.js
DISPATCH_TEST_NETWORK=deny python -m pytest -q
git diff --check
```

CI 必須在 exact candidate SHA 上通過。Focused tests 不可取代 full suite。

---

## 16. 第一版明確 Non-goals

- 不重寫 FastAPI／SQLite application。
- 不改成 Kubernetes／microservices。
- 不遷移 PostgreSQL。
- 不宣稱 hostile multi-tenant isolation。
- 不啟用 Node backend。
- 不移除 SSH。
- 不做 S3／object storage。
- 不做 browser Dataset upload。
- 不做 Dataset GC／自動 retention deletion。
- 不做 Container／Conda 自動建置。
- 不做 GPU sharing、MIG slot、quota、preemption 或 multi-job worker。
- 不做 OIDC group 自動授權。
- 不做 Device Flow。
- 不做 remote session revoke。
- 不做 arbitrary artifact byte upload。
- 不做 Codex 自動核准、auto promotion 或 auto merge。

---

## 17. 計畫完成標準

Product v2 第一里程碑完成需同時滿足：

- 使用者不需 browser token 即可登入。
- 五角色在 enforce mode 下實際限制後端操作。
- Project Wizard 可建立完整、可使用的 Project。
- Run Template 不要求使用者輸入完整 shell command。
- Environment、Template、Dataset、target 均固定 exact revision。
- Dataset 可由本機 path 或成功 Run output 發布。
- Alias、lineage、usage 與雙邊 sharing 可追蹤。
- One-click Run 在 approval 前顯示完整實際執行契約。
- 核准後 payload、Dataset、target 與 command 不漂移。
- 117 SSH Pilot 通過 20 jobs／8 小時與全部 drill。
- 所有新功能可由 feature flag 回退。
- Existing SSH work 不因 Product v2 rollback 失去 owner。
- 沒有 production-ready 的過度宣稱。

完成上述條件後，Product v2 可標示：

```text
implemented=yes
deployed=yes（117 non-production）
canary-proven=yes（SSH Product v2 Pilot）
production-ready=no
```

Node 與 Production Gate 完成後，才可重新評估 `production-ready`。
