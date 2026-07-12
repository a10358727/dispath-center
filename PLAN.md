# AI Project & Resource Control Platform — 產品與實作計畫

> 狀態：2026-07-11 重新規劃版。本文取代舊的逐階段功能流水帳，作為未來開發
> 的產品方向與落地順序。現有程式與測試代表已實作行為；受保護行為仍以
> `.claude/skills/dispatcher-domain/references/invariants.md` 為正典。計畫涉及
> 不變量改動時，必須在對應實作批次明確核准，不能因本文存在就直接改掉。

## 1. 產品願景

建立一個以「專案」為核心的 AI 工作負載管理平台，讓使用者不必記住每台
伺服器有哪些目錄、資料或環境，也不必手動決定在哪台機器執行。

平台應做到：

1. 自動發現散落在不同伺服器上的專案，集中建立 catalog。
2. 明確知道每個專案在哪些機器有 instance、版本是否一致、是否有未提交修改。
3. 將選定的 code revision、dataset snapshot 與 runtime requirements 組成可重現
   的 JobSpec。
4. 根據資源、資料位置、專案位置、機器健康度與排程政策選擇 worker。
5. 缺少 code 或 dataset 時，先建立明確、可核准、可追蹤的 staging plan。
6. 安全執行、持續監控、斷線/重啟後恢復狀態，最後集中收回結果。
7. 把 run、log、artifact、metrics、結論與下一步留在專案時間軸。
8. 讓 GPT/LLM 能用自然語言查詢、提出派工、解釋排程、分析結果，但不能繞過
   確定性驗證、核准與執行邊界。

成功後，使用者從一個專案頁就能回答：

- 這個專案的中央版本是什麼？哪些機器有副本？哪些副本 dirty/diverged？
- 它需要哪些資料與環境？目前哪台機器最適合跑？
- 為什麼任務還沒執行？缺 code、資料、資源、核准，還是 worker 離線？
- 之前跑過哪些版本與參數？結果、指標與結論是什麼？
- GPT 的建議根據哪個 run、log、artifact 或 metric？

## 2. 核心產品模型

平台不是「遠端 shell 面板」，而是以下生命週期的 control plane：

```text
Discover → Register → Normalize → Version → Plan → Stage → Schedule
→ Execute → Observe → Collect → Analyze → Improve / Rerun
```

主要物件：

```text
Project
├── ProjectVersion（中央 hub 的 immutable commit/ref）
├── ProjectInstance（某台 worker 上的實體目錄）
├── RuntimeProfile（環境、entrypoint、setup、resource defaults）
├── DatasetBinding（需要的 immutable dataset snapshots）
├── JobTemplate / JobSpec
├── Runs / CodingRuns
└── Experiment Records / Results / Metrics

Worker
├── StaticCapabilities（GPU/CPU/RAM/tags/paths/execution backend）
├── ObservedState（online/load/GPU/memory/disk/current reservations）
├── ProjectInstances
├── DatasetReplicas
└── ActiveRuns
```

核心定義：

- **Project** 是中央邏輯身分，使用 UUID；名稱只是顯示與 slug，不再是跨系統
  唯一身分。
- **ProjectVersion** 是可重現的 code revision。正式 run 必須固定 version，不能
  只寫「使用 server-b 上目前那份目錄」。
- **ProjectInstance** 是 Project 在某台機器某個 path 的物化副本，不是另一個
  Project，也不應默認成 source of truth。
- **DatasetSnapshot** 是 immutable 資料版本。rolling dataset 的 latest 只是一個
  pointer，job 核准後永遠固定 snapshot id。
- **JobSpec** 描述要跑什麼；**ExecutionPlan** 描述為了跑起來要在哪台機器做哪些
  code/data/setup/execute 動作。
- **Run** 是 JobSpec 的一次執行，保留實際 code、dataset、worker、resources、
  timestamps、status 與 artifacts。

## 3. 產品邊界與原則

### 3.1 真相來源

| 領域 | 真相來源 |
|---|---|
| 專案身分、metadata、instance 關係 | Control-plane DB |
| 正式 code revision | Server A project hub（bare Git repo） |
| Dataset version | ArtifactStore 中的 immutable snapshot＋manifest |
| Job/approval/run state | DB＋worker sentinel/reconcile |
| Worker 即時資源 | monitor observations（有 freshness，非永久真相） |
| Results | ArtifactStore 原始 artifact＋結構化 result metadata |
| 人的結論與決策 | Project experiment timeline |

### 3.2 安全與可預測性

- 先用確定性程式完成 validation、authorization、planning、scheduling、state
  transition 與 execution；LLM 不負責決定事實。
- 所有 material state change 必須出現在 approval plan，核准後不能偷換 payload。
- GPT/LLM 只能唯讀或建立 request，永遠沒有 approve/reject/自由 shell/直接 SSH。
- DB 先記錄 reservation/running，再做遠端副作用；每個 crash window 都能 reconcile。
- worker 連不上代表 unknown/skip，不代表 failed。
- 不自動覆寫 dirty project instance、不自動合併疑似重複專案、不自動刪 dataset/
  result/artifact。

### 3.3 漸進式架構

- 近期維持單一 Server A control plane、SQLite、agentless SSH worker，先把產品模型
  與工作流做正確。
- 對 storage、execution、identity 建立窄介面，未來才能替換成 object storage、
  container/VM backend、OIDC/PostgreSQL，而不是現在就全面重寫。
- 近期定位是 `single_owner` 或 `trusted_team`。真正 multi-tenant 必須完成 DB、
  storage、compute、network、secrets 與 GPT context 隔離後另行啟用。

### 3.4 目前不追求

- Kubernetes、複雜微服務拆分或 worker-to-worker 傳輸。
- 未經實測就做 GPU sharing、preemption、migration 或 distributed scheduler。
- 把工作資料改成只能由 LMDB/WebDataset 讀取；worker 上仍保留可瀏覽散檔。
- 讓 GPT 自主核准、部署、執行、修改 production 或根據分析自動重跑。

## 4. 現況能力與主要缺口

現有程式已具備可重用的雛形，不從零重寫：

| 領域 | 已存在 | 需要整合/補強 |
|---|---|---|
| Worker | SSH 設定、monitor、server management | 能力模型、freshness、resource reservation |
| Project | inventory candidates、projects、instances、matrix、hub、deploy | UUID 身分、instance reconciliation、canonical revision、dirty/diverged workflow |
| Dataset | registry、card、cache、rsync、data gravity | 小檔 shard、snapshot、hash、atomic publish、transfer state |
| Queue | jobs、dependencies、priority/FIFO、reconcile | JobSpec/ExecutionPlan、reason codes、resource fit、idempotent claim |
| Execution | SSH/SFTP/tmux sentinel、local runner | execution backend abstraction、project/data staging、structured output contract |
| Results | rsync 回收、log、mail、records/timeline | artifact manifest、metrics、比較、可重試回收 |
| AI | chat、vLLM tools、MCP bridge、diagnosis | project-centric tools、grounded citations、plan explanation、identity context |
| Security | shared token、approval、audit、安全 tests | 個別 actor、immutable approval、trusted-team RBAC；多租戶延後 |

目前最大的結構性問題：Project、ProjectInstance、Hub、Dataset、Job、Result 已有
功能，但缺少一條統一的 project lifecycle。後續所有工作先圍繞 Project Control
Plane 收斂，不再新增彼此獨立的功能入口。

## 5. 目標架構

```text
Web UI                 GPT / vLLM                 ChatGPT MCP
  │                         │                          │
  └────────────── API / Identity / Approval Gateway ──┘
                              │
                 ┌────────────┼────────────┐
                 │            │            │
          Project Service  Planning     Query/Analysis
                 │         & Scheduler      Service
                 │            │            │
       Project Catalog     Job/Run DB   Experiment Index
       + Git Hub              │            │
                 └────────────┼────────────┘
                              │
                       Execution Service
                    ┌─────────┴─────────┐
                    │                   │
             SSH/tmux backend     Future sandbox backend
                    │
           Worker project/data/result roots
                    │
                    └──── ArtifactStore ──── Dataset/Result snapshots
```

邏輯上分成五個 plane，第一版仍可在同一 FastAPI process 中：

1. **Project control plane**：專案 catalog、hub、instances、runtime profiles。
2. **Artifact/data plane**：dataset snapshots、code bundles、results、transfers。
3. **Resource/execution plane**：workers、observations、reservations、dispatch、reconcile。
4. **Experiment intelligence plane**：runs、metrics、artifacts、records、comparison。
5. **AI interaction plane**：GPT/MCP/vLLM 的查詢與 request tools。

模組邊界應逐步收斂，避免繼續把全部 endpoint、policy 與 orchestration 堆在
`app/main.py`：

```text
app/projects.py           Project catalog/lifecycle
app/project_instances.py Instance reconcile/deploy status
app/artifact_store.py     Storage interface/backends
app/dataset_snapshots.py Snapshot/shard/replica
app/planner.py            JobSpec → immutable ExecutionPlan
app/resource_model.py     ResourceRequest/Capacity/Fit
app/scheduler.py          Eligibility/scoring/claim
app/execution.py          ExecutionBackend interface
app/run_service.py        Run lifecycle/result collection
app/analysis.py           Grounded result/experiment views
app/identity.py           Future actor context
app/authorization.py      Future centralized policy
```

不是要求一次搬完；每個 phase 只抽出當次需要的邊界並保持 API 相容。

## 6. Project Control Plane 詳細設計

### 6.1 Project catalog

Project 至少保存：

```text
id UUID
slug / display_name
description / goal
state discovered|managed|archived
hub_repo_path
default_branch
default_runtime_profile_id
created_at / updated_at
```

ProjectVersion：

```text
id UUID
project_id
git_commit
git_ref
source_instance_id
created_at
metadata
```

ProjectInstance：

```text
id UUID
project_id
server
path
state available|missing|dirty|diverged|unknown
git_remote / branch / commit
last_seen_at
last_scanned_at
error
```

### 6.2 發現與集中管理流程

```text
掃描設定的 project_roots（唯讀）
→ project_candidates
→ 去除明顯巢狀噪音
→ 人工判斷：新 Project / 連結既有 Project / ignore
→ 建立 Project＋ProjectInstance
→ 非 Git instance：產生 git-init approval
→ 排除 datasets/results/secrets 後建立第一個 commit
→ bundle 經 Server A 拉回中央 hub
→ 建立 ProjectVersion
→ instance 進入 managed
```

辨識規則：

- 有 Git remote＋commit 時，以 normalized remote 與歷史關係提供「可能相同」提示。
- 只有相同 basename/path 不得自動合併。
- 非 Git 專案不能靠內容 hash 自動宣稱相同；由使用者選擇連結哪個 Project。
- import/merge/link 不刪原目錄、不改 working tree；寫入仍走 approval。

### 6.3 中央 hub 與版本規則

- Server A bare hub 是 managed project 的正式 code source。
- worker 的 dirty working tree 不會自動進 hub；使用者先查看 diff，再建立 sync/
  promote approval，產生新 commit/version。
- 正式 run 固定 hub commit；若使用「現場 dirty instance」做臨時實驗，必須標為
  `unreproducible=true` 並在 approval 明顯提示，預設不允許。
- 部署到新 worker 一律由 hub commit 建 bundle；不直接 rsync 整個 project tree。
- instance reconcile 定期唯讀取得 exists/branch/commit/dirty，更新 last_seen；不在
  list API 即時 SSH 全部機器。

### 6.4 Project page

每個 Project 頁集中顯示：

- 目標、說明、中央 branch/commit、hub 狀態。
- instance matrix：server/path/commit/dirty/diverged/last seen。
- runtime profiles、dataset bindings、job templates。
- 執行按鈕：Run、Coding Task、Sync to Hub、Deploy、Compare Runs。
- active/queued runs、最近結果、metrics、experiment timeline。
- 所有動作的 ExecutionPlan/approval，不要求使用者跳到多個無關頁面。

## 7. Dataset / Artifact Plane

### 7.1 大量小檔案策略

正式模型是「工作時散檔、傳輸時分片、版本時 snapshot」：

```text
Source directory
→ streaming manifest
→ deterministic tar shards（預設目標約 2 GiB，可 benchmark 調整）
→ 每 shard SHA-256
→ immutable DatasetSnapshot
→ 只傳目標缺少的 shards
→ staging 解包
→ manifest/marker 驗證
→ atomic publish
→ DatasetReplica=ready
```

禁止：把數百萬檔完整 list JSON 塞進主 DB、直接解包進 final path、只用檔案數＋
總大小作唯一完整性證據、同名版本就地換內容。

ArtifactStore 第一版用 Server A local filesystem，介面預留 S3-compatible backend：

```text
dataset_store/blobs/sha256/<hash>.tar
dataset_store/manifests/<snapshot-id>.jsonl[.zst]
dataset_store/snapshots/<snapshot-id>.json
```

### 7.2 Snapshot / replica / transfer

必要狀態：

```text
DatasetSnapshot: building → published → retired（或 corrupt）
DatasetReplica: missing → transferring → verifying → ready（或 corrupt）
Transfer: queued → running → verifying → done/failed/cancelled
```

- Job 綁 snapshot id，不綁 mutable latest。
- transfer `(snapshot,target)` 同時最多一個 active lease，可在服務重啟後接手。
- final marker 最後寫入，包含 snapshot id、manifest hash、file count、logical bytes。
- scheduler 只認 replica ready＋marker matching。
- 空間預估包含 shard staging、解包 tree、final coexistence 與安全餘量。
- cleanup/eviction 必須檢查 job reference、last used、quota，並走 approval。

### 7.3 統一 artifact

同一 ArtifactStore 最終承載：

- Dataset snapshots/shards。
- Code/deploy/coding bundles。
- Run results、logs、checkpoints、reports。
- Structured metrics 與 result manifest。

原始 artifact 永遠保留為證據；LLM summary、圖表或計算結果只是 derived view。

## 8. Job Planning、Resource Scheduling 與 Execution

### 8.1 JobSpec

```yaml
project_id: <uuid>
project_version_id: <uuid>
job_type: train|evaluate|adhoc|coding
entrypoint: train.py
args: {epochs: 100, batch_size: 32}
dataset_snapshot_ids: [<uuid>]
resources:
  exclusive_node: true
  gpu_count: 1
  gpu_memory_min_mb: 16000
  cpu_cores: 8
  memory_mb: 32768
  scratch_bytes: 200000000000
  timeout_sec: 43200
constraints:
  require_tags: [cuda]
  pin_server: null
outputs:
  include: [metrics.json, checkpoints/**, reports/**]
```

近期保留 raw command 相容，但新 project workflow 優先使用 runtime profile＋entrypoint＋
structured args。自由 shell 不應成為未來多使用者的預設介面。

### 8.2 Planner

Planner 在建立 approval 前產生完整 ExecutionPlan：

```text
chosen worker
required project version/deploy
required dataset transfers
required setup/runtime preparation
resource reservation
run command/spec
estimated transfer bytes / disk requirement
material risks and warnings
```

- approval 固定 plan hash；核准時重驗 worker/resource/project/dataset 狀態。
- 若狀態變化只是不影響副作用的 observation，可繼續；若 target、transfer、code
  version 或 command 改變，原 approval 失效並產生新 plan。
- scheduler 不得在核准後臨時創造未顯示的 sync/deploy/setup 副作用。

### 8.3 Scheduler

Eligibility 順序固定：

1. deployment/workspace/worker-pool boundary。
2. server enabled、online 且 observation 未過期。
3. pin server、job type、special runner restrictions。
4. resource fit（exclusive node 先行；slot scheduling 後續）。
5. required tags/runtime capabilities。
6. project version 可取得或已在計畫中 deploy。
7. dataset replicas ready 或 transfer dependencies 已建立。
8. dependencies、quota/concurrency。

合格後才 scoring：dataset locality → project locality → resource fit/waste → priority →
FIFO/aging。所有 filter/score 都是純函式或可重現 policy，不能依 LLM。

Job 必須有可解釋 reason code：

```text
WAITING_APPROVAL, DEPENDENCY_PENDING, PROJECT_STAGING, DATASET_TRANSFER,
NO_ELIGIBLE_WORKER, WORKER_OFFLINE, RESOURCE_MISMATCH, QUOTA_EXCEEDED,
CODING_RUNNER_OFFLINE, READY_TO_RUN
```

### 8.4 Execution

建立 `ExecutionBackend`：

```text
prepare(plan)
launch(run)
inspect(run)
stop(run)
collect(run)
cleanup(run)
```

第一版 backend 包裝既有 SSH/SFTP/tmux/sentinel；`job.command` 仍只經 SFTP 落地。
未來 container/VM backend 不改 Planner/Run domain model。

狀態與恢復：

- DB reservation/running 先於 remote launch。
- 每個 run 有 attempt id/lease，remote 路徑與 tmux/session 包含 attempt identity。
- exit code sentinel 是 execution 終態證據；unreachable 不改狀態。
- dispatch 部分成功時不能盲目 revert 再派；需依 attempt marker 判斷是否已 launch。
- stop、cleanup、retry 都是顯式狀態轉移並進 audit。

## 9. Result、Experiment 與 GPT Intelligence

### 9.1 Result contract

每個 run 最少產生/保存：

```text
run.json              實際 project/dataset/resources/worker/timestamps/status
logs/                 原始 log
metrics.jsonl|json     結構化 metrics（選配但推薦）
artifacts/             checkpoints/reports/images/其他輸出
result-manifest.json   artifact path/size/hash/type
```

沒有 structured metrics 時平台仍收原始結果，但不得由 LLM 把 log 猜測成正式指標。
結果回收是獨立 transfer，可重試；回收失敗不改 execution success/failure。

### 9.2 Project experiment timeline

時間軸合併：Run、CodingRun、DatasetVersion、人工 observation/conclusion/decision。
人工結論引用 run/artifact/metric，不複製原始真相。支援：

- 依 project、version、dataset、worker、status、時間、tag 搜尋。
- 選 2～N 個 runs 比較參數、環境、metrics 與 artifacts。
- 將 observation 升為 conclusion/decision，但保留作者與時間。
- 下一個 JobSpec 可從 run 複製，所有差異在 approval 前顯示。

### 9.3 GPT 能力

GPT 應優先提供 project-centric tools：

- `get_project_overview`
- `list_project_instances`
- `explain_project_drift`
- `plan_project_run`
- `request_project_run`
- `explain_job_scheduling`
- `compare_project_runs`
- `analyze_run_results`
- `search_experiment_timeline`

回答規則：

- 每項事實引用 project/run/artifact/metric/log id/path。
- 明確分開 observed fact、calculated value、LLM inference。
- 找不到資料時回 unknown/missing，不補故事。
- 建議 rerun、patch、deploy 時只建立新的 request/approval。
- 模型沒有直接 SSH、approve/reject、任意 path、DB 或 ArtifactStore credentials。

## 10. 身分、安全與未來多人使用

近期先把系統做成可信任的 single-owner/trusted-team：

- 個別 actor/service account，token 只存 hash，可 expiry/revoke。
- middleware 產生 RequestContext；`source=web/chatgpt/vllm` 不是身分。
- 集中式 deny-by-default authorization。
- approval 保存 requester、approver、canonical payload hash、risk、expiry。
- trusted-team 預設關閉 web direct execute；高風險禁止 self-approval。
- GPT/MCP 使用 request-only service account scopes。

真正 multi-tenant 不列為近期平台 MVP。只有以下同時完成才啟用：

- PostgreSQL＋application ownership check＋RLS defense in depth。
- workspace-scoped DB/cache/search/conversation/artifact。
- 每 workspace 專屬 worker pool/SSH identity，或通過安全測試的 VM/container。
- filesystem/mount/process/GPU/network/secrets/resource quota 隔離。
- 固定 A/B cross-tenant regression tests 與人工 security review。

在此以前，workspace 欄位只可作 schema readiness，產品不得宣稱 hostile tenant
isolation。

## 11. 重新規劃後的實作 Roadmap

### Phase 0 — Baseline 與 migration 安全網

目的：在不丟現有資料/能力下建立可演進基線。

- 為現行 DB、servers.yaml、hub、dataset/result directories 建 backup/restore 步驟。
- 建立 schema/API snapshot tests 與完整 migration fixture。
- 盤點每個現有 endpoint/table 對應新 domain model，標示 keep/refactor/deprecate。
- 新功能一律 additive migration；不刪舊欄、不把缺失歷史資訊腦補成真實 revision。
- 補 `INV-PROJECT-*`、`INV-SCHED-*`、`INV-DATA-*` 草案，經核准後才實作。

驗收：舊 DB 可在新程式開啟、既有核心 tests 綠、可完整還原；沒有 production SSH。

### Phase 1 — Project Control Plane（最高優先）

目的：先解決「不同機器各有不同專案、沒有集中管理」。

- Project UUID、ProjectVersion、ProjectInstance state/last_seen migration。
- 將現有 inventory/candidates/matrix/detail/hub/deploy 收斂到 project lifecycle service。
- 增加 instance reconcile：available/missing/dirty/diverged/unknown。
- 建立「連結候選到既有 Project」流程，不再只能新建 Project。
- managed project 必須有 hub canonical source；非 Git 專案以核准流程 git 化。
- Project page 完整呈現 hub、instances、版本差異、可執行管理動作。
- 所有部署固定 project version/commit，dirty instance 不被覆寫。

驗收：掃描全部設定 roots 後，使用者能在單一矩陣看到每個 Project 在哪台機器、
哪個 commit、是否 dirty/diverged；可安全匯入、同步 hub、部署到新 worker。

### Phase 2 — Dataset / Artifact Plane

目的：解決大量小檔案與跨機資料版本一致性。

- ArtifactStore local backend。
- Streaming manifest、deterministic tar shards、SHA-256、immutable snapshot。
- Replica/Transfer state machine、lease/resume/progress。
- Staging、安全解包、marker、atomic publish/reconcile。
- Project dataset binding 改綁 snapshot id。
- 舊 dataset 登記為 legacy snapshot：保留已知 count/size，未知 hash 明確標 unknown。

驗收：大量小檔只傳 shards；中斷不重傳已完成 shard；未完整資料不會被派工；
job 可重現使用哪個 snapshot。

### Phase 3 — JobSpec、Planner 與可重現執行

目的：把「一條 command」升級為 project-aware execution contract。

- RuntimeProfile、JobTemplate、JobSpec、ExecutionPlan、Run/Attempt model。
- Planner 在 approval 前固定 project version、dataset snapshots、target 與 staging。
- Approval plan hash/staleness handling。
- ExecutionBackend 包裝現有 SSH/tmux。
- 結構化 reason codes 與 project run UI。

驗收：從 Project 頁建立一次 run，可以看到完整 code/data/setup/resource plan；核准後
執行的 plan 與看到的一致；重啟後 run/attempt 正確 reconcile。

### Phase 4 — Resource Scheduler v2

目的：讓平台真的依資源而不是只靠 tag/整機空閒派工。

- Static capabilities、observed state freshness、ResourceRequest/Reservation。
- 先穩定 exclusive-node scheduling，再以真實需求決定 GPU slot sharing。
- Eligibility/scoring 純函式、atomic claim/lease、transfer-aware scheduling。
- Priority/FIFO＋aging；trusted-team 才加入簡單 quota/fairness。
- Scheduling explanation API/UI/GPT tool。

驗收：每個 queued job 都有 reason；不會超賣資源或重複派發；資料/專案 locality
能降低 staging；離線/過期 observation 不誤派。

### Phase 5 — Results & Experiment Intelligence

目的：把「任務完成」變成可比較、可學習的實驗紀錄。

- Result manifest、可重試 artifact collection、structured metrics convention。
- Project timeline 與 run comparison。
- Artifact preview/read policy、retention 與 approval-based cleanup。
- 結論/決策引用真實 run/metric/artifact。

驗收：可選兩次 runs 比較 code/dataset/args/resources/metrics；artifact 拉取失敗可重試，
不改 job status；所有分析可追到原始證據。

### Phase 6 — GPT Project Command Center

目的：讓 GPT 操作的是 Project/Plan/Run，而不是拼 shell。

- Project-centric read/plan/request/analysis tools。
- Grounded response schema 與 evidence references。
- GPT 解釋 instance drift、dataset staging、scheduler reason、run comparison。
- 所有 mutation 仍只建立 immutable approval request。

驗收：GPT 能回答「把專案 P 的版本 V 用資料 D 跑在合適 GPU」並先回完整 plan；
沒有證據就說不知道；prompt injection 最壞只產生受限待審 request。

### Phase 7 — Trusted-team Identity & Operations

目的：支援多位可信操作員並提升 production readiness，不宣稱多租戶。

- 個別 identity/RBAC、actor-aware audit、token rotation/revocation。
- Backup/restore drill、DB/artifact capacity、scheduler/monitor liveness、alerts。
- API pagination/rate limit/request size/backpressure。
- Security/production readiness gate。

驗收：能回答誰提案、誰核准、執行哪個 plan、產生哪些 artifacts；單一操作員不能
因 UI 漏洞獲得未授權管理能力；故障後可依文件恢復。

### Phase 8 — Multi-tenant（只有明確商業需求才啟動）

獨立 architecture project：PostgreSQL/OIDC/workspace isolation、專屬 worker pool 或
VM/container、secrets/network/storage quota、cross-tenant tests。不得在前面 phase
順手加入半套 multi-tenant 並對外開放。

## 12. Migration 原則

- `projects.name` 舊關係逐步對應新 `project_id`；過渡期雙讀/雙寫由 migration
  adapter 處理，完成驗證後才移除 name foreign references。
- 舊 project instance 保留原 path/server/commit；找不到時標 unknown/missing，不刪。
- 既有 hub repo 經 verify 後掛到 Project；沒有 hub 的 Project 保持 unmanaged。
- 舊 dataset 不偽造 content hash；建立 legacy snapshot 並顯示 verification level。
- 舊 jobs/runs 保留 command 與事實欄；缺 project version/dataset snapshot 時明確
  標 `legacy_unpinned`，仍可查看但不能假裝可重現。
- 所有 migration 支援 fresh DB 與現有 DB；每批有 backup、dry-run、row counts、
  rollback 說明。
- 新舊 API 過渡有 deprecation 訊息與 README 更新，不一次破壞前端/MCP。

## 13. 測試與 Release Gate

每個 vertical slice 至少測：

1. Domain pure functions/state machines。
2. DB fresh/migration/rollback-sensitive path。
3. FakeSSH/local/artifact backend，不碰真機與 runtime data。
4. Approval 建立時 validation＋核准時 revalidation＋payload unchanged。
5. Crash windows、retry/idempotency、unreachable behavior。
6. API shape/auth/404/409/422 與 pagination。
7. GPT/MCP forbidden tools/imports、prompt injection、grounded missing-data response。
8. Frontend smoke 與 project lifecycle critical path。

Phase release 必須提供：

- 變更的 domain model/API/schema。
- 保護的 invariants 與新增 reason/state values。
- targeted tests、related suite、static checks 結果。
- migration/rollback/operational notes。
- 未完成能力與禁止宣稱事項。

## 14. 近期實作切片順序

先不要同時開 Data Plane、Scheduler 與多使用者。最小依賴順序：

1. **Project identity migration**：UUID、legacy name adapter、instance last_seen/state。
2. **Instance reconciliation**：唯讀 scan/probe → available/missing/dirty/diverged。
3. **Candidate linking**：候選可連到既有 Project，避免同專案重複登記。
4. **Canonical version service**：hub commit → ProjectVersion；run/deploy 可固定 revision。
5. **Project overview/page 收斂**：矩陣、hub、instances、runs、datasets、actions 一頁。
6. **ArtifactStore skeleton**：local backend＋hash/staging primitives，不先接 production sync。
7. **DatasetSnapshot builder**：stream manifest＋shards，完成後才替換現行 rsync path。
8. **JobSpec/Planner skeleton**：先產 plan 不執行；與現行 job 建立結果比對。
9. **Planner 驅動 approval/execution**：feature flag 漸進切換。
10. **Scheduler v2 與 result intelligence**：建立在前述穩定 identity/snapshot/plan 上。

近期第一個可驗收里程碑不是「多一個 API」，而是：

> 使用者打開 Project 頁，能可靠看到散落在所有 worker 的 instances、中央版本與
> 差異；選定一個版本與資料後，平台能產生完整、可核准的執行計畫。
