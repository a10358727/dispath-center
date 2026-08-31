# AI Engineering Task 決策紀錄

> Status: approval record. 本文件記錄使用者對 `docs/AI_ENGINEERING_DECISION_GATE.md`
> 所列決策的實際裁定。決策文件本身仍是唯讀的審閱包，不因這份紀錄而改寫；
> 這份紀錄才是「哪些選項被核准」的權威來源。

> 2026-08-30 起（DG-PLATFORM-CHARTER v1）：不變式正典在 `docs/PLATFORM_CHARTER.md` §6、裁定索引在 §7；
> 決策 packet 移至 `docs/decisions/`、歷史計畫移至 `docs/archive/`。本檔歷史段落引用的舊路徑保留原文不改。

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

## 2026-07-17 追加：D5/D6 解鎖實作（本文件原記「下一輪」的兩項）

D5/D6 原本記載的前提「待 D3/D1 的核准/adapter 模式驗證後再開始」已在
2026-07-17 滿足（D1 首切片完成並通過完整測試，見 `docs/CURRENT_STATE.md`
§0.11；D3 兩個 kind 已在 2026-07-16 commit `a092229` 完成）。使用者接續下達
「根據 Goal 1 / AI Engineering 十個切片的計畫來實施」的指示，本輪據此把
D5/D6 從「下一輪」推進為已實作，範圍與裁定文字完全一致，**不**擴大裁定
本身：

| 決策 | 本輪動作 |
|---|---|
| D5 | additive `run_profiles` 不可變 revision schema + `run_profile_create`/`_update`/`_archive` 三個 approval-gated kind，`RUN_PROFILE_V1_ENABLED` 預設關閉；**未接線**進 enqueue/job 派工（選用某個 Run Profile 實際影響派工是後續切片） |
| D6 | `app/github_publication.py`：`GitHubPublicationProvider` 介面 + 不可變、無憑證欄位的 request/result；**沒有**任何 production adapter、沒有任何模組 import 它、沒有 API 路由；fake provider 只存在於測試 |

詳見 `docs/CURRENT_STATE.md` §0.13。

## 明確不做（本輪與 2026-07-17 追加皆同）

- D2 的任何實作或啟用前提放寬；
- D1/D6 的正式操作啟用（真實 app-server provider、真實 GitHub App 註冊/
  installation token broker）；
- `engineering_task_finalize`/`_promote`/`_pr` 三種 approval kind；
- Run Profile 接線進 enqueue/派工邏輯（v1 只做 schema + 生命週期核准）；
- 修改 `.claude/skills/dispatcher-domain/references/invariants.md` 中的
  任何不變量；
- 讓 `engineering_task_retry`/`_discard`/`run_profile_*` 的自動核准清單納入
  `enqueue|stop` 以外的 kind。

## 決策日期：2026-07-18（Goal 2 自動調度決策點）

使用者對 `docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md` §3 的兩個決策點裁定如下：

```text
DG-1 政策驅動自動放置提案（Slice 4）: 核准
DG-2 政策範圍內自動執行（Slice 5）: 核准
```

裁定範圍與附帶條件（依計畫原文，不擴大）：

- **DG-1**：scheduler 可依 approved dispatch policy 自動建立 `auto_placement`
  **pending approval**（提案）；執行前仍必有人批。`auto_placement` 永不加入
  `enqueue|stop` 自動核准白名單。
- **DG-2**：對明確有界的 approved policy，`auto_placement` 提案可在政策精確
  範圍（伺服器集合、`max_concurrent_placements`、`valid_until`、指令 SHA）內
  由獨立的 policy-scoped auto-decision 機制自動核准，逐件稽核，附全域
  `AUTO_PLACEMENT_KILL_SWITCH`（預設停用自動執行）。動工 Slice 5 前仍須把
  對應不變量修訂文字寫入
  `.claude/skills/dispatcher-domain/references/invariants.md` 並在本文件記錄
  修訂內容——本裁定授權該修訂，但修訂本身以實作時的正式文字為準。
  **修訂已於 2026-07-18 完成**：invariants.md 新增 `INV-APPROVAL-4b`
  （policy-scoped 自動決策只限 `auto_placement`，六項硬條件、kill switch
  預設停用、條件不滿足留 pending、`decision_mechanism=policy-{id}-r{rev}`），
  並在既有 `INV-APPROVAL-4` 的 Forbidden 清單加註「不得把 4b 機制實作成
  `maybe_auto_approve()` 的 kind 或規則」。`INV-APPROVAL-4` 本文與
  `enqueue|stop` 白名單完全未動。
- Goal 2 Slices 1–3 依計畫本就不需決策點，隨時可動工。
- 本裁定**不**涉及 D2 沙箱、provisioning/Node Agent（`INV-SSH-1`）、多
  Codex Runner——這些屬 Goal 3 未來工作，見
  `docs/GOAL_3_FUTURE_WORK_PLAN.md`，各自需要另外的具名裁定。

## 決策日期：2026-07-19（Goal 3 啟動範圍與 DG-B）

使用者對 `docs/GOAL_3_FUTURE_WORK_PLAN.md` 裁定如下（核准計畫檔：
`/home/formosa/.claude/plans/iterative-wiggling-firefly.md`）：

```text
Goal 3 啟動範圍: Phase B、Phase A、Phase D-1、Phase C 全部啟動，
                 另把 Goal 2 延後的前端 UI 排在最前面
DG-B bootstrap 腳本內容: 核准預設清單
DG-A / DG-C / canary 簽核: 尚未裁定（各自的閘門到時另行具名裁定）
```

- **DG-B（具名核准）**：bootstrap 腳本為審閱過的固定版本（SHA-256 pin），
  非 root、只裝使用者層工具、冪等可重跑。允許元件：tmux、rsync、git、
  使用者級 Python（venv/pip）。**明確排除**：GPU 驅動、任何系統套件
  （sudo/apt 層）——這些維持人工前提。腳本經既有 SFTP 非插值路徑遞送。
- **Phase A**：本輪只實作 A1 唯讀 preflight；A2–A4 停在 G2 閘門
  （操作者重裝 codex CLI、真實 Runner 跑 preflight、據以裁定 DG-A
  最終資源數字）之後。
- **Phase D**：本輪只實作 D-1 Runner pool（單 Runner 設定完全相容）；
  D-2/D-3/D-4 依原計畫各自綁 canary 簽核與 GitHub 操作決策，未裁定。
- **Phase C**：本輪只到 C0 草稿——`INV-SSH-1` 修訂／`INV-NODE-*` 文字
  先草擬，**寫入 invariants.md 前需 DG-C 具名裁定**（G3 閘門）；C1 seam
  之後才動。
- **實作方式**：本輪由主 session（Fable）直接實作，不派 sonnet-coder。

## 決策日期：2026-07-19（DG-C：INV-SSH-1 修訂與 INV-NODE-* 核准）

使用者裁定 **DG-C 核准**，文字以
`docs/DG_C_INVARIANT_REVISION_DRAFT.md` 的草稿版本為準（G3 閘門解除）。
已依裁定寫入 `.claude/skills/dispatcher-domain/references/invariants.md`：

- `INV-SSH-1` 修訂為「SSH 後端無 agent、依賴封頂」——SSH 後端永不假設
  agent 存在、永久保留為相容/緊急通道；`INV-SSH-2…9` 一字未動。
- 新增 `INV-NODE-1…6`（出站單向身分、lease/ack 先於副作用、非插值落地、
  心跳過期＝unknown、重啟不重複、逐台提升隨時回退）。實作尚未存在；
  這些不變量約束之後的 C1–C4 切片，不影響既有 SSH 行為。

本裁定解鎖 Phase C 的 **C1（ExecutionBackend seam，SSH 零行為變更＋
golden tests）**；C2–C4 依 Goal 3 計畫的量化門檻逐步推進，canary（C3）
仍需屆時的操作簽核（G4）。

## 決策日期：2026-07-25（Phase A 範圍緊縮：A2–A4 移除，DG-A 不再需要裁定）

使用者裁定：

```text
Phase A（Runner 沙箱強制）: 移除 A2–A4，保留 A1（唯讀 preflight）
理由: 不再走沙箱強制路線；接受 post-agent finalization 長期在 Runner OS
      使用者身分下執行，不做 bwrap/cgroup/quota 強制隔離
```

- **A1（唯讀 preflight，已上線）**：維持現狀。`GET /codex-runner/sandbox-preflight`
  純粹回報現況、不強制任何事，繼續保留作為診斷工具。
- **A2（finalization 沙箱包裝器）、A3（第二把鑰匙退場）、A4（canary）**：
  自 `docs/GOAL_3_FUTURE_WORK_PLAN.md` 移除，不再規劃實作。原本卡住這三項
  的兩個主機前提（cgroup CPU 使用者委派、ext4 project quota）不再需要處理。
- **`ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION`**：從「暫時
  接受風險、待 preflight 通過後退場」的過渡鑰匙，改為**長期維持**的狀態——
  不會被 A3 原本規劃的「preflight 通過」條件取代。`docs/CURRENT_STATE.md`
  §0.9 記錄的 High 殘留風險（post-agent Git finalization 在 Runner OS 使用者
  身分下執行、無 CPU/記憶體/磁碟/network namespace 隔離）因此從「待 A2 修復」
  變成使用者知情下的**長期接受**，不是被無聲移除。
- **DG-A（原訂之後要裁定的 preflight 標準與最終資源數字）**：因 A2 不再實作
  而不再需要裁定，從決策待辦中移除。

本裁定不影響 Phase B/C/D 的既有進度或裁定；`INV-SSH-*`/`INV-NODE-*` 等既有
不變量不變。

## 決策日期：2026-07-25（DG-B4：新機 dataset 預熱核准實作）

使用者裁定 **DG-B4 核准**，設計以 `docs/DG_B4_DATASET_PREWARM_DRAFT.md`
的草案為準，依「先落地、後啟用」模式實作（預設 flags 全關，同 Phase B／
A1／D-1）：

```text
DG-B4 dataset 預熱: 核准草案設計，開始實作
預設狀態: DATASET_PREWARM_V1_ENABLED=false 且 DATASET_PREWARM_KILL_SWITCH=true
```

- **觸發**：新背景迴圈 `dataset_prewarm_loop()`（比照 `auto_placement_loop()`）
  找出 `enabled=true` 且 `dataset_cache` 為空的機器。
- **選取**：data gravity——挑被最多台其他 enabled 機器快取的資料集；同分
  取 `size_bytes` 較小者；再同分取 (name, version) 字典序（確定性）。
- **每輪每台機器最多 1 筆提案**；`dataset_prewarm` 為第 33 個 approval
  kind，**永遠不在** `maybe_auto_approve()` 的 `enqueue|stop` 白名單。
- **核准後**走既有 `dataset_remote_dir()`／`build_sync_script()`／
  `enqueue_job(type="sync")` 路徑，指令字串與手動派工附帶的 sync 任務
  逐位元一致（golden test 驗證），不另造通道。
- **煞車**：`DATASET_PREWARM_V1_ENABLED`（預設 false）與
  `DATASET_PREWARM_KILL_SWITCH`（預設 true＝停用）雙保險，兩者都要撥開
  才會提案；`DATASET_PREWARM_COOLDOWN_SEC`（預設 3600）對
  (server, dataset, version) 三元組去重與冷卻。

**與草案的一處偏離（已一併核准）**：草案原寫「提案時用
`app.datasets.check_disk_space()` 確認空間」。實作改為提案時用 monitor
已探測到的 `ServerState.disk_avail_bytes` 做便宜預檢（fail-closed，缺值
＝不提案），**提案這一輪完全不 SSH**——比照 `build_dispatch_plan()`
「純 DB 查詢（不 SSH）」的既有慣例，避免背景提案迴圈對外連線。權威的
df 檢查維持不變，仍由 `app/scheduler.py` 在派工當下對每個 sync job 執行
`check_disk_space()`（同一個 `SPACE_SAFETY_FACTOR`）。

## 追蹤

實作進度與驗證方式見 `docs/CURRENT_STATE.md` §0.11–§0.13（本次工作階段）。
`/home/formosa/.claude/plans/groovy-tinkering-lake.md` 是舊工作階段的核准
計畫檔，該次伺服器重建後已不存在，2026-07-17 起的追蹤改以本文件與
`docs/CURRENT_STATE.md` 的日期化 addendum 為準。

## 決策日期：2026-07-27（DG-EXEC-ATTEMPT-v1：recommended contract 核准）

使用者具名裁定：

```text
DG-EXEC-ATTEMPT：核准本文件的 recommended contract
```

本句明確指向 `docs/DG_EXEC_ATTEMPT_DECISION.md` 的
`DG-EXEC-ATTEMPT-v1`。使用者核准當下的 reviewed-draft SHA-256 為：

```text
5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d
```

裁定為 **Approve recommended contract**，只解鎖：

- WP-1A additive attempt/outbox/event/server-config-revision schema 與
  migration；
- 窄版 DB/domain transaction、CAS、operation authorization API；
- pure migration/transition/concurrency/immutability tests；
- 文件列出的 feature flags，但所有 flags 必須維持預設 `false`。

本裁定**不核准** production DB migration、scheduler/SSH/Node cutover、
production flag 啟用、遠端連線、新 public/LLM/MCP mutation route，亦不修改
`jobs.status` 或任何 canonical invariant。`DG-AMBIGUOUS-LAUNCH`、
`DG-JOB-STATE`、`DG-ATTEMPT-RECOVERY`、`DG-NODE-V2` 等後續 gate 仍各自
維持 blocked。

## 決策日期：2026-07-27（DG-AMBIGUOUS-LAUNCH-v1 與 DG-EXEC-ATTEMPT-v1.1）

使用者於本次工作階段審閱兩份草稿後裁定：

```text
我審核通過
```

本句在當次對話中明確指向同一則訊息所呈交的兩份文件，因此記錄為兩項獨立
裁定：

### 裁定一：`DG-AMBIGUOUS-LAUNCH-v1` — **Approve recommended contract**

指向 `docs/DG_AMBIGUOUS_LAUNCH_DECISION.md`。使用者審閱當下的 reviewed-draft
SHA-256 為：

```text
d38f2dea83ef3badaa302cdd0769e255847a3f71d1fa8bfbbc76656ed9c9bf08
```

裁定內容包含該文件 §13 第一個選項的完整範圍：

- §3.2 的 `INV-STATE-2` 逐字替換文字，即**只有 definite pre-launch failure
  才可 `running → queued`**；timeout／連線中斷／未分類例外一律 ambiguous，
  Job 保持 running、attempt 保持原 target 與 `liveness=unknown`。
  這是本次唯一被修訂的 canonical invariant。
- §4 的 definite/ambiguous 分類（allowlist，預設 ambiguous）與封閉 reason
  code 集合。
- §5 的 atomic remote claim／launcher／receipt／trap sentinel 協議。
- §6 的 unknown attempt 解析程序與 same-attempt／same-idempotency-key replay。
- §7 的 additive schema delta。
- §11 的五項子裁定 **D-1…D-5 全部採用建議值**：控制端搶 claim 仲裁；
  receipt 在但 tmux/sentinel 皆無且 boot_id 未變時**永久保持 unknown、需人工
  處置**；偵測到重開機時 attempt `failed` 且 Job 退回 queued 重派；新路徑採
  per-attempt tmux session 命名；`agent_jobs` 非本機檔案系統時 fail closed。

本裁定**只解鎖 WP-2B/2C 的實作**（additive schema、pure builder、FakeSSH
crash matrix、versioned golden fixtures），以及據此更新
`.claude/skills/dispatcher-domain/references/invariants.md` 的 `INV-STATE-2`
條文。

本裁定**不核准**：啟用 `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED` 或任何既有
execution flag、production DB migration、連線 production worker、WP-2D canary
啟動。上述 rollout 需依該文件 §9 另外具名簽核。

`DG-JOB-STATE`、`DG-ATTEMPT-RECOVERY`、`DG-NODE-V2`、`DG-DATASET-SNAPSHOT`、
`DG-CODE-PROMOTE`、`DG-AUTHZ-ENFORCE`、`DG-SSH-HOSTKEY`、`DG-GPU-SCHED`
仍各自維持 blocked。

### 裁定二：`DG-EXEC-ATTEMPT-v1.1` addendum — **Approve addendum**

指向 `docs/DG_EXEC_ATTEMPT_DECISION.md` 的 `Addendum v1.1`。使用者審閱當下
的 reviewed-draft SHA-256 為：

```text
3a8eb432962796bfa10e25ccc6acada19a07f42a14233248409ee48f6d9d1807
```

解鎖 `RB-SERVER-001` 的實作：既有 server add/update/disable/delete approval
的**執行面**改走已核准的 pinned publication protocol（`intent →
yaml_applied → activated`），並新增單一 authenticated operator recovery
surface 供讀取 mutation journal 與處置 `recovery_hold`。

不新增 approval kind、不新增 public/LLM/MCP mutation route、不變更
`VALID_APPROVAL_KINDS` 或 auto-approve allowlist；legacy 目標不得由 migration
升級為 `approved`，只能重新核准。`EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED`
在本 addendum 的工作期間全程維持 `false`。

### 證據鏈附註

上述兩個 SHA-256 是使用者實際審閱的 bytes。裁定後兩份文件的檔頭狀態列被
更新為 approved，因此檔案 digest 已改變；stamp 後的 digest 分別記錄於各該
文件的檔頭，兩者的轉換原因即本節。此後對這兩份文件的任何內容修改都必須
另提新的 contract revision 與新的具名裁定，不得沿用本節的 digest。

## 決策日期：2026-07-27（WP-2D canary 延後執行）

使用者裁定：

```text
我想要跳過這步驟！指定的非 production worker、≥20 jobs / ≥24 小時、
一次強制 response-loss、一次 control-plane restart、一次 rollback drill
直接進入下一步
```

**裁定內容**：WP-2D 的實機 canary 延後，不阻擋後續工作包開工。

**本裁定不改變的事實**（延後執行 ≠ 已通過）：

- `RB-LAUNCH-001` 維持**開啟**。缺陷在程式碼層面已修正並有 crash matrix，
  但沒有任何實機證據。
- `docs/CAPABILITY_LEDGER.md` 的 `attempt_driven_ssh` 維持
  `deployed=no`、`canary-proven=no`。不得因為程式碼合併而改成 `yes`。
- `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED` 維持預設 `false`。**在 canary 完成
  前於 production 啟用該 flag，等同執行一條未經實機驗證的派工路徑**；若要
  啟用，需另外具名裁定並承擔該風險。
- `docs/WP_2D_CANARY_RUNBOOK.md` 與 `scripts/canary_report.py` 保留，隨時
  可執行。

延後的是「取得證據的活動」，不是「證據本身」。任何文件都不得據此宣稱
canary 已通過。

## 決策日期：2026-08-01（DG-WP2D-CANARY-v2：8 小時窗與實機啟動）

使用者明文裁定：

```text
測試玩了!可以跑!接下來正式測試吧!幫我改規章8小時就好
```

**裁定內容**：核准 `docs/DG_WP2D_CANARY_V2_DECISION.md`。WP-2D
觀察窗由原始 v1 的 24 小時改為 **8 小時**，new evidence contract
為 `ssh-canary-evidence-v2`。此改動只縮短時間門檻；至少 20 個 terminal
workloads、forced response-loss、control-plane restart、rollback-to-legacy
三項 drill，以及零 duplicate／false failure／lost terminal／unresolved
unknown／uncertain operation 與 100% collection 全部保留。

本裁定同時核准以已選定的非 production SSH worker
`worker_5090_117` 開始受限 WP-2D v2 canary，但必須先固定精確
candidate commit、完成 SQLite online backup、確認 active approved revision
的 preflight 仍為 `eligible`，並驗證 execution-control leader/fencing。本裁定
不授權 production worker、Phase 5 Node canary，也不改動 `DG-OPS-SLO`
的 RPO 24h。

原始 `DG-AMBIGUOUS-LAUNCH-v1` 與 2026-07-27 延後裁定保留為歷史
證據，不回寫成當時就是 8 小時。任何 v2 通過結果只能宣稱
「8-hour WP-2D v2 canary」，不得宣稱滿足原 v1 的 24 小時窗。

## 決策日期：2026-07-27（DG-DATASET-SNAPSHOT-v1：recommended contract 核准）

使用者具名裁定：

```text
同意！幫我開下去跑！
```

本句明確指向 `docs/DG_DATASET_SNAPSHOT_DECISION.md` 的
`DG-DATASET-SNAPSHOT-v1`。使用者審閱當下的 reviewed-draft SHA-256 為：

```text
da53f51d8274091bff6f82da80ebb7ae7477094e3160a14807a4e4684037b5b7
```

該 digest 對應 commit `b5f2627` 中的檔案內容，可驗證。

裁定為 **Approve recommended contract**，包含 §11 的五項子裁定
**D-1…D-5 全部採用建議值**：

- **D-1**：`POST /datasets` **不**轉成 request/approve flow，改以
  `reproducible=false` 明確標記；要求 reproducible 的 run 使用 legacy
  dataset 時在 request 時即拒絕，不得靜默降級。
- **D-2**：對每個 byte 計算 SHA-256，並設明確上限
  `DATASET_SNAPSHOT_MAX_BYTES`（預設 200 GiB），超過即拒絕建置，
  **不抽樣、不以 mtime 替代**。
- **D-3**：v1 只從 Server A 本地路徑取 bytes；只存在於 worker 上的資料集
  暫不支援 snapshot。
- **D-4**：snapshot 的身分是 `manifest_digest`（每檔內容 digest），
  shard digest 只是儲存證據。
- **D-5**：legacy dataset 用於 reproducible run 時在 request 時拒絕，
  reason code `dataset_not_reproducible`。

**解鎖範圍**：WP-3A 的 additive schema 與 migration、local ArtifactStore、
deterministic manifest/shard builder、content-addressed atomic publish、
`dataset_snapshot_build` approval kind 與其 pinned contract、相關純函式與
fixture 測試。

**不核准**：啟用 `DATASET_SNAPSHOT_V1_ENABLED` 或
`DATASET_SNAPSHOT_PUBLISH_ENABLED`、發布任何真實資料集、S3/object-store
adapter、garbage collection 或任何 retention 刪除、dataset ACL、跨站複寫。

`DG-CODE-PROMOTE`、`DG-NODE-V2`、`DG-AUTHZ-ENFORCE` 等後續 gate 仍各自
維持 blocked。

## 決策日期：2026-07-28（DG-CODE-PROMOTE-v1：recommended contract 核准）

使用者具名裁定：

```text
DG-CODE-PROMOTE v1：核准本文件的 recommended contract
```

指向 `docs/DG_CODE_PROMOTE_DECISION.md`。使用者審閱當下的 reviewed-draft
SHA-256 為：

```text
510d4075d6011dc84b3fc4c913665dc8a3853bfc72f721033a6c65d14fb25baf
```

該 digest 對應 commit `4abd84e` 的檔案內容，可驗證。

裁定為 **Approve recommended contract**，包含 §5 的五項子裁定
**P-1…P-5 全部採用建議值**：

- **P-1**：promotion **永遠不得自動核准**——不透過 `maybe_auto_approve()`，
  也不透過 `INV-APPROVAL-4b` 的 policy-scoped 機制。任何自動路徑都會讓系統
  執行沒有人看過的程式碼。
- **P-2**：同一個 commit 重複 promote 是 no-op，回傳既有版本；一個 commit
  對應兩個版本會讓「這次 run 用的是哪個版本」無法回答。
- **P-3**：沒有 promotion approval 的 `project_versions` 列是
  `legacy_observed`，不得支撐 reproducible run。
- **P-4**：promotion 只寫 hub，**不推 GitHub**；`DG-GITHUB-PUBLISH` 維持
  獨立且未核准。
- **P-5**：promotion 後保留 worktree；清理屬另行核准的 retention 操作。

**解鎖範圍**：WP-3C 的 `engineering_task_promote` approval kind 與
`code-promotion-v1` pinned contract、`project_versions` 的 additive promotion
provenance 欄位與遷移、approve-time bundle digest 重驗、hub 發布順序、
以及 planner 端只接受已 promote 版本的閘門。

**一併授權**：更新
`tests/test_engineering_command.py::test_all_three_d4_kinds_are_deliberately_absent`。
該測試原本把三個 kind 釘為刻意不存在，理由是「沒有任何型別或流程定義其語意，
定義它們等於臆造語意」，並明文要求「要做的話必須先有具名裁定」。本裁定即為
該具名裁定，因此 `engineering_task_promote` 的釘選遷移為「存在且永不自動
核准」；`engineering_task_pr` 與 `engineering_task_finalize` **維持刻意不存在**。

**不核准**：在運行中的部署啟用 promotion、任何 GitHub adapter、worktree 刪除、
以及 `DG-GITHUB-PUBLISH`／`DG-NODE-V2`／`DG-OPS-SLO`（各自維持 blocked）。

## 決策日期：2026-07-28（DG-NODE-V2-v1：recommended contract 核准）

使用者具名裁定：

```text
DG-NODE-V2 v1：核准本文件的 recommended contract
```

指向 `docs/DG_NODE_V2_DECISION.md`。使用者審閱當下的 reviewed-draft SHA-256：

```text
870ba081453d467fd5112dcc5d039e1b971d324e114d45e3415ad3ea87133a2c
```

對應 commit `3d91686` 的檔案內容，可驗證。

裁定為 **Approve recommended contract**，§4 的五項子裁定
**N-1…N-5 全部採用建議值**：

- **N-1**：`POST /node-agent/poll` **不再接受 agent 提供的 `job_id`**。
  控制平面用與 SSH 路徑相同的資格規則自己挑選。agent 自報工作顛倒了信任
  關係——決定什麼在哪裡跑是控制平面的職責。
- **N-2**：重啟後遇到「已 ack 但無存活行程」的 attempt，agent **回報後停手**，
  由控制平面決定。agent 既不重啟（重複執行），也不自行判定失敗（偽造終態）。
- **N-3**：緊急撤權**立即生效**，即使仍有工作在跑。但撤權**不代表該工作
  failed**，`unknown` 才是誠實的狀態，且**不授權 SSH 重跑**。
- **N-4**：憑證輪替為 **staged**——新舊 token 在有界的重疊窗口內都有效。
  硬切換會讓每次輪替變成停機，實務上會導致操作者乾脆不輪替。
- **N-5**：node 只有在 `DG-NODE-CANARY` 於實機通過後才能接真實工作。
  本 gate 解鎖的是實作，不是可用性。

**額外要求（本裁定一併確認）**：Node 的 terminal 必須以與 SSH 路徑相同的
投影守衛**收斂 canonical Job**——終態必須與 attempt 自身記錄的狀態相符，
不得憑空產生。現況只關閉 `node_attempts`，Job 可能永遠停在 `running`。

**解鎖範圍**：WP-4A/4C 的實作——server-selected lease、每 node 單一 active
attempt 的 DB 強制、current-attempt recovery、Node terminal 收斂 Job、
staged credential rotation、退役 drain 與緊急撤權分流。

**不核准**：啟用 `NODE_AGENT_V1_ENABLED`、登記任何 node、實機啟用
（另需 `DG-NODE-CANARY`）、以及 `DG-OPS-SLO`（維持 blocked）。

## 決策日期：2026-08-07（DG-PRODUCT-V2-BASELINE-v1）

使用者將根目錄 `PLAN.md` 指定為已核准的實作計畫，並要求依其工作包持續
實作與驗收。核准當下，`PLAN.md` 與
`docs/DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md` 是同一份 Product v2
執行契約，兩者的 SHA-256 均為：

```text
0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

本裁定保留 2026-07-16 D7 的原文與當時範圍。D7 所述「本輪不涉及」仍是
當時的歷史事實；本裁定只取代其對目前僅支援 `off|shadow`、以及 enforce
仍待另一項決策的現況判斷。本節就是該項後續獨立決策，不得倒推為
2026-07-16 已取得 enforce 實作、部署或啟用授權。

### Authorization enforcement

- 接受目前程式與測試所證明的實作狀態：`AUTHORIZATION_MODE` 支援
  `off|shadow|enforce`，預設仍為 `off`。
- 核准依 Product v2 entry gate 在 117 **非 production** Pilot 使用
  `enforce`。
- 本裁定不表示目前已啟用、已部署、已通過 Canary 或可在 production
  啟用。這些狀態只能由對應環境與時間窗證據推進。

### Multi-role RBAC v2

- 同一使用者在同一 Project 可持有 Owner、Operator、Reviewer、
  Dataset Manager、Viewer 中的複數角色，權限取聯集。
- Owner 與 Reviewer 都可決定 project-scoped approval；service actor
  不可取得這兩個角色，也不可決定 approval。
- 每個 Project 至少保留一名 Owner，且至少有兩名不同的人具
  Owner／Reviewer 能力。high-risk requester 不可自批，Platform Admin
  亦不例外。
- Legacy `admin` 確定性映射為 Owner + Operator + Reviewer +
  Dataset Manager，`operator` 映射為 Operator，`viewer` 映射為 Viewer。
  Migration 必須記錄 `legacy_membership` provenance；缺少原始 approval ID
  時維持 `NULL`，不得偽造歷史。

### API v2 cutover

- 新產品介面使用 additive `/api/v2`，v2 UI 只呼叫 v2 API；舊 API
  不再承接新的產品功能。
- Legacy API 由 compatibility flag 保留至少一個穩定 release 作 rollback。
- 只有在 client inventory 全部遷移、至少一個穩定 v2 release、連續 30 天
  legacy route 零呼叫且 rollback artifact 已保存後，才可另開需 Code Owner
  核准的 breaking-change PR；本裁定不直接授權移除舊 API。

### Project bootstrap

- Project Wizard 產生單一 immutable `project_bootstrap_v2` approval，
  payload 固定 Project identity、初始角色、第一個 Environment、
  Run Template、Project Defaults，以及可選的既有 Dataset grant／alias。
- 核准後，所有本機 DB 資源與 durable audit 在同一 transaction 建立；
  任一步失敗即整體 rollback。
- Git init、remote deploy、server bootstrap 等遠端副作用仍使用既有的
  獨立 approval，不得併入 bootstrap transaction。

### Dataset sharing

- 分享採雙邊兩階段核准：來源 Project 先核准 immutable share offer，
  目標 Project 再核准 accept；單邊核准不得建立 grant。
- Offer 與 grant 只包含 exact published snapshot IDs，未來 snapshot
  不會自動加入。
- Alias 是 project-scoped immutable revision；目標 Project 只能對仍有效的
  granted snapshot 建立自己的 alias。
- 撤銷只阻止新的 Run，不改寫歷史 Run，也不將已核准、執行中或既有 Job
  改派或推定為 failed。

### 邊界

本裁定解鎖 Product v2 各工作包的本機、additive、default-off 實作，不修改
canonical invariants，也不授權 production migration、production rollout、
Node Pilot、破壞性 schema 變更或 legacy API removal。`implemented` 不得被
推導為 `enabled`、`deployed`、`canary-proven` 或 `production-ready`。

## 決策日期：2026-08-07（DG-API-V2-FOUNDATION-v1）

依使用者授權的 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-01 的 public
API、schema、安全與相容性邊界回傳 `SOL_DECISION`，採用以下最小合約：

- `/api/v2` root router 永遠註冊但目前不含 probe 或產品 endpoint；
  `API_V2_ENABLED` 預設 `false`，只控制未來 v2 route 的 runtime
  availability。OpenAPI 不因環境旗標改變，legacy routes、response body 與
  router registry 保持不變。
- Cursor 採 version 1、無 padding base64url canonical JSON，綁 actor、
  stable route、排序契約與 normalized filters；預設 limit 50、上限 100。
  Cursor 不是 authorization capability，每頁仍須重新套用目前 actor scope。
- HTTP idempotency 綁 canonical durable actor、uppercase method + stable
  FastAPI route template、exact visible-ASCII key 的 SHA-256，以及 validated
  normalized request 的 canonical JSON SHA-256。匿名 mutation 不成立，
  同 scope/key 不同 request digest 回 `409 idempotency_key_reused`。
- TTL 固定 24 小時；expired row 在同 transaction 刪除後可重用。只保存完成
  resource identity，不保存 raw key、pending state、response body、header、
  status code或 transient failure。
- Mutation callback、本機 canonical resource、durable audit 與完成的
  idempotency row 共用一個 `BEGIN IMMEDIATE` transaction；rollback 不留
  orphan row。外部網路、SSH 或檔案副作用不放入該 transaction。
- Migration v5 是 `api_idempotency_keys` 的唯一 schema owner，採 source
  checksum pin、actor `ON DELETE RESTRICT`、composite primary key 與 expiry
  index；不 backfill 或偽造歷史 HTTP idempotency evidence。

本裁定只解鎖 default-off 的本機 foundation。它不新增任何 Product v2
resource、不啟用部署、不構成 Canary 或 production-ready 證據。

## 決策日期：2026-08-07（DG-PRODUCT-RBAC-V2-v1）

依 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-02 的 schema、授權、安全與
legacy 相容邊界回傳 `SOL_DECISION`。本節細化
`DG-PRODUCT-V2-BASELINE-v1` 的 Multi-role RBAC 裁定，不擴張 production
rollout 授權。

- Migration v6 新增獨立 `project_role_bindings`。有效 legacy membership
  只做確定性映射；初始 binding 的 `grant_provenance` 是
  `legacy_membership`、`grant_approval_id` 是 `NULL`。
  `project_memberships.updated_at` 逐字存入 `granted_at`，只代表 legacy row
  在最後觀測時間呈現該角色，不代表原始授權或人工核准時間。
- Product v2 角色是 Owner、Operator、Reviewer、Dataset Manager、Viewer，
  沒有 hierarchy；同一 actor 的權限取角色 action set 聯集。Owner 不自動
  取得 Operator，Reviewer 不成為 legacy admin。
- Project ready 必須有至少一名 enabled HUMAN Owner，並有至少兩名不同的
  enabled HUMAN Owner／Reviewer。Service／legacy actor 不得持有 Owner 或
  Reviewer，也不得決定 approval。
- `needs_role_repair` 只接受對 enabled HUMAN 單調增加 Owner／Reviewer，
  不接受 removal。外部損壞造成的非人類 Owner／Reviewer 持續 fail closed；
  PR-02 不提供無證據的刪除或自動修復 API。
- `project_role_change` 是 immutable high-risk approval。Requester 必須是
  HUMAN Owner 或 Platform Admin；decider 必須是另一名 enabled HUMAN
  Owner／Reviewer 或 Platform Admin。Approve-time 重新驗證 requester、
  decider、target、digest 與 resulting invariants；binding delta、approval
  decision、durable audit 與 HTTP idempotency completion 同一 transaction。
- Public v1 membership request 在 Product RBAC v2 啟用時以
  `409 legacy_membership_write_disabled` 關閉。只有成功的 approved v1
  membership decision 可用真實 approval ID／decision timestamp 同步
  `legacy_membership` bindings；direct DB helpers 不推導 provenance。
  Product v2 role change 永不反向寫入 legacy membership。
- Role-decision endpoint 對不存在、非 role-change、無法解析 scope，或
  caller 不屬於該 Project 的 approval 統一回 generic `404 not_found`，
  不洩漏 project、approval kind、payload 或 authorization reason。同 Project
  的角色不足、service denial 與 high-risk 自批仍回可操作的 403。
- `PRODUCT_RBAC_V2_ENABLED` 與 `API_V2_ENABLED` 都預設 `false`；前者依賴
  後者。Migration 不受 runtime flag 控制，rollback 關旗標且保留 additive
  schema、approval 與 audit evidence。

本裁定不授權 destructive migration、direct-mutator provenance 推導、
corrupt-binding repair API、legacy API removal、部署、Canary 或
production-ready 宣稱。

## 決策日期：2026-08-10（DG-PRODUCT-RUN-EXPERIENCE-V2-v1）

PR-11 的 Product Run identity、state projection、Clone/Compare、Stop、Artifact
與 Workspace 相容性由唯讀 `sol_advisor` 裁決為
`SOL_DECISION: APPROVE_OPTION_1_SINGLE_CANONICAL_PRODUCT_RUN_PROJECTION`：

- Product Run 唯一 canonical identity 是 `execution_plans.id`。不新增 Migration
  v10、Run/read-model/stop/artifact table、Product state 欄位或新的 approval kind。
  v2 lineage 共用 ExecutionPlan verifier；帶 v2 marker 但 verification 失敗時，
  authorized caller 得 409、foreign caller 得 opaque 404，且不得 fallback legacy。
- 五條固定 route 是 detail、`clone-previews`、static `runs/compare`、
  `stop-requests` 與 `artifacts`。全部使用 `RUN_EXPERIENCE_V2_ENABLED`、no-store、
  authorization catalog/shadow/enforcement；read 需要 `project.view`，Clone/Stop
  需要 `project.operate`。Compare 先獨立解析並授權兩側，任一不可見整體 404。
- State 是 Job/attempt/evidence 的 closed projection。Pending/rejected approval、
  queued/preparing/running、stopping、terminal、blocked/cancelled 依固定優先序投影；
  terminal evidence 勝過 stopping。Unknown liveness、stalled、recovery hold、
  uncertain operation 或不一致 terminal evidence 成為 `needs_attention`，永不由
  absence/unreachable 推導 `failed`。Done 且 collection 尚待處理仍為
  `succeeded` 加 reason；collection failed/unknown 才 needs-attention。
- Detail timeline 是 bounded deterministic safe projection，只包含 closed event
  kind、ID/digest/timestamp/state/liveness/reason。不得回 approval/operation payload、
  evidence、command、argv、log、host、credential、checkout 或 storage path。
- Clone 固定 source exact ProjectVersion、Run Profile、Environment、resolved
  snapshots、server revision、完整 stored parameter base，再套 bounded overrides；
  alias 不跟 head，policy 不重選。它只回 preview，零 Plan/approval/Job/audit/
  idempotency write。Compare 只回 fixed typed dimensions；parameters 僅 keys/digest/
  changed keys，target 不回 server/host，並保留 unknown/truncated。
- Stop 沿用 `kind=stop`、`stop-intent-v1` 與 server-fixed
  `{job_id,source=product_v2,attempt_id}`。Request、approve、reject 各自把 decision、
  durable audit、idempotency completion，及 approve 時唯一 pending stop outbox 放在
  同一 `BEGIN IMMEDIATE` transaction；HTTP 不做 SSH delivery、不改 Job terminal。
  同 active attempt 跨 key semantic-dedup pending 或完整 approved intent；同 key
  replay 在 authorization 後先於 current-state revalidation。Rejected、terminal、
  partial/conflicting materialization 對新 key 回 409。Approve-time stale target
  原子標 rejected `stop_target_stale` 且不建 outbox；recovery hold 保持 pending。
- Stop 保持 `high_risk=false`；同一 HUMAN 同時是 Operator 與 Owner/Reviewer時可
  self-decision，enabled HUMAN Owner/Reviewer 或 Platform Admin可決定，Service
  actor不可決定。不把全域 `stop` 加入 transaction-only set，也不改 PR-10
  `execution_plan_v2` high-risk 契約。
- Artifact 只讀授權 lineage 中 exact node attempts，read-time 重驗相對 path、
  kind、size、digest、timestamp。只回 metadata；不得回 host/root/storage/absolute
  path/URL/content。空、invalid、partial 或 untrusted evidence 保持 unknown，不宣稱
  collection complete。Legacy 只接受 exact name→唯一 canonical UUID 與可信 Job
  FK；Legacy Clone/Stop 回 409。
- Workspace gate-on `recent_runs` 只用 ExecutionPlan projection，card identity 是
  plan UUID；不得把 standalone Job 冒充 Product Run。UI 只呼叫 v2 API，所有
  relative paths 使用文字節點，Stop 只顯示 stopping/approval state。Gate-off
  保留有限 legacy adapter。

Runtime rollback 只關閉 `RUN_EXPERIENCE_V2_ENABLED`，保留 Migration v9、plans、
approvals、Jobs、attempts、stop outbox、idempotency與audit evidence；不得取消或
改寫已核准 Job。本裁定不授權 117 deployment/Pilot、legacy retirement、Canary
或 production-ready 宣稱。

## 2026-08-09 — DG-DATASET-ASSETS-V2-v1

狀態：accepted for local implementation

裁定：

- 採用單一 Migration v8，一次建立 `dataset_assets`、
  `dataset_asset_snapshots`、`dataset_share_offers`、
  `project_dataset_grants`、`dataset_alias_revisions` 與
  `dataset_lineage_edges`；不 backfill legacy registry/snapshot ownership。
- Snapshot ID 是最長 255 UTF-8 bytes 的 canonical opaque text，不假設 UUID。
  Asset、approval、alias revision、lineage edge 等新 identity 使用 canonical
  UUID；所有 Project／asset／snapshot／offer／grant 關係由 exact 或 composite
  foreign key 固定。
- `dataset_asset_adoption_v2` 只允許 enabled HUMAN Platform Admin request，並在
  request/decision 重驗 Project readiness、published manifest digest、unlinked
  snapshot 與 asset name/UUID conflict；different enabled HUMAN Owner／Reviewer
  或 Platform Admin 核准後，asset、exact snapshot link、approval decision、
  durable audit 與 idempotency evidence 原子提交。不得建立 empty asset。
- `dataset_alias_change_v2` 只允許 same-Project owned exact snapshot；create 必須
  no-head/revision 0，move 必須綁 exact head UUID/revision/digest。每次核准新增
  immutable N+1 revision，不修改 predecessor 或已 resolved Run。Alias identity
  固定為 `(project_id, asset_id, alias_name)`；不同 asset 的同名 alias head、
  revision 與 CAS 完全隔離。
- Lineage edge 只連結兩個 published exact asset snapshots，且 producing
  ExecutionPlan 必須固定 input。Reachability check 與 insert 在同一個
  `BEGIN IMMEDIATE` transaction；self edge、既有 path 的 reciprocal edge 與
  concurrent cycle 都 fail closed。同一 `input_snapshot_id → output_snapshot_id`
  最多一列 provenance，不因 plan 或 declaration 不同而重複。所有
  graph/page/read response 有 hard bound。
- Asset、link、offer、alias、lineage rows immutable。Grant 只允許 trigger-guarded
  `NULL→revocation approval/time` 單向 transition；禁止 delete、unrevoke 或其他
  update。Grant UUID 獨立，unique boundary 是 `(offer_id, snapshot_id)`，不阻止
  future approved regrant。
- PR-07 Usage 只把 canonical active alias heads 標成 `available`。ExecutionPlan
  v2 bindings 與 Project Defaults 在 PR-10 前、active grants 在 PR-08 前都明確回
  `unavailable` 且不查詢或洩漏 reserved/legacy rows，不得回假 zero。
  Storage read model 不回 legacy dataset name/version、source/manifest/descriptor
  path 或 credential。普通 Project API 只列 `dataset_assets`，不列
  `legacy_unscoped` rows。
- Data Card 的 canonical JSON contract 在 request validation 即執行 16 KiB
  aggregate bound，避免建立永遠無法 materialize 的 approval。
- `DATASET_ASSETS_V2_ENABLED=false` 為精確預設，依賴 API v2 與 Product RBAC。
  關閉時隱藏 Dataset routes、兩種 approval 的 list/detail/decision 與 Workspace
  projection，但保留 Migration v8、immutable resources、approval、idempotency
  與 durable audit evidence。

本裁定預留 share-offer snapshot set 的 bounded sorted unique canonical text array
與 digest，供 PR-08 在同一 transaction 驗 exact membership；不提前開放 sharing、
publish、ExecutionPlan v2、legacy removal、production deployment 或 Pilot 宣稱。

## 決策日期：2026-08-07（DG-PROJECT-BOOTSTRAP-V2-v1）

依 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-04 的 schema、approval、
安全、原子性及 PR-05／PR-06 ownership 邊界回傳 `SOL_DECISION`：

- Migration v7 一次建立唯一 canonical `project_environments`、
  `environment_revisions`、`run_profile_specs` 與
  `project_default_revisions`。PR-05／PR-06 只延伸這些表的行為，不建立臨時表
  或平行真相。既有 Project／raw Run Profile 不 backfill 或偽造 typed evidence。
- Preview 可以先產生所有 resource UUID，必須回完整 canonical payload 與
  SHA-256 digest，且零持久化。Request 提交 exact payload／expected digest，
  需要 `Idempotency-Key`，只建立 immutable pending approval；approval 不另產生
  materialization identity。
- Approval payload 固定 Project UUID/name/credential-free source reference、初始
  roles、Environment logical/revision IDs、Run Profile typed spec、Defaults exact
  refs，以及 canonical empty Dataset arrays。非空 Dataset grant／alias 在
  PR-07／PR-08 前以 `409 capability_unavailable` 阻擋。
- Requester 與 decider 都必須是 enabled HUMAN Platform Admin，且不得同一人。
  初始角色另須有 enabled HUMAN Owner，以及兩名不同的 enabled HUMAN
  Owner／Reviewer；service／legacy actor 不得取得 Owner／Reviewer。
- Approve-time 重新驗證 schema、payload digest、actor／角色、references 與所有
  UUID／name conflicts。Project、role bindings、Environment revision 1、既有 Run
  Profile identity + typed spec、Defaults revision 1、approval decision、durable
  audit 與 HTTP idempotency completion 必須在同一 transaction；任一步或 audit
  append 失敗，全部 rollback 且 approval 保持 pending。
- Bootstrap 只建立本機 canonical DB 資源，不啟用 execution。Typed profile 在
  PR-06 deterministic compiler 完成前不得走 legacy raw-command path。Git init、
  filesystem creation、remote deploy、server bootstrap、SSH 與 network side
  effects 一律不屬於此 transaction。
- Environment preflight、argv、parameter、resource、output 與 Defaults 使用
  closed typed validators；不接受 arbitrary JSON、nested value 或 secret value。
  危險 setup 在 request-time 回 400，approval-time 仍重新套用目前安全政策。
- Project Workspace 只接受 `project.view`，不存在與未授權皆回 opaque 404；
  response 不含 source/setup、secret reference names、default values 或 output
  paths。`PROJECT_BOOTSTRAP_V2_ENABLED` 預設關閉且依賴 API v2／Product RBAC；
  rollback 關旗標並保留 additive data 與 evidence。

本裁定只授權本機、additive、default-off 實作，不授權 production migration、
部署、117 Pilot、Node rollout 或 production-ready 宣稱。

PR-04 最終唯讀審查另固定以下 hardening，屬同一決策的實作約束：

- `/api/v2/approvals` 只回授權過濾後的 bounded safe summaries；
  `/api/v2/approvals/{approval_id}` 才對 authorized reviewer 回 verified
  canonical payload、contract version 與 digest。Project Wizard 必須成功載入
  detail 並由 reviewer 明確確認後，才啟用 approve。
- Typed Run Profile 的 logical `(project_id, name)` lineage 不得由 legacy raw
  update／archive 建立 successor；request-time 與 approve-time 都重新檢查。
- Migration v7 以 composite foreign keys 固定 Run Profile、Environment、Project、
  spec digest 與 Defaults 的 exact same-Project references。
- PR-07／PR-08 尚未裁決前，Dataset placeholder element 為 opaque；OpenAPI 不先
  宣告 UUID／alias semantics。任何非空輸入只回 blocking preview，request 409
  且零寫入。

## 決策日期：2026-08-07（DG-PROJECT-ENVIRONMENTS-V1）

依 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-05 的 public API、approval、
安全、readiness evidence 與 Migration v7 ownership 回傳
`SOL_DECISION: APPROVE_OPTION_1_WITH_FLAG_CORRECTION`，並對 validation secret
reflection 另回 `APPROVE_OPTION_1_WITH_HARDENING`：

- `environment_change_v2` 是唯一 standalone high-risk kind。Canonical payload
  固定 `environment-change-v2`、operation、Project UUID、expected revision／head
  與完整 `host-environment-v1` target revision；approval 欄位保存 canonical JSON
  與 SHA-256，不建立遞迴 payload digest。Create 由 server 產生 logical／revision
  UUID；update 是完整 replacement；archive 不接受 replacement。
- Name 不可變。每次核准只 append exact N+1 revision；archive 複製前一版完整
  contract 成為 terminal `archived` successor，不刪除或重寫歷史。Create／update
  在 request-time 與 approve-time 都套用目前 dangerous-setup 與 secret-free
  政策；archive 即使政策後來漂移仍可執行。
- GET 需要 `project.view`；request 需要 `project.operate`。Owner 不自動取得
  Operator。Requester 可為 HUMAN 或具有 exact scope 的 service Operator；
  Platform Admin 可 override。Approve-time requester 必須仍 enabled、非 legacy、
  仍具 Operator 且 Project RBAC ready。
- Decider 必須是不同的 enabled HUMAN Owner、Reviewer 或 Platform Admin；service
  永不決定。Approve 重新驗證 immutable bytes、scope、actors、safe setup 與 exact
  current head。Reject 仍驗 bytes／scope／decider，但刻意不要求 current head、
  current requester 或目前 setup policy，讓 stale／unsafe pending request 能被關閉。
- Request、decision、revision、durable audit 與 completed idempotency identity 在
  同一 transaction。Decision idempotency 額外綁 approval payload digest；legacy
  generic approve／reject 不得處理此 kind。Audit 只含 bounded IDs、operation、
  status 與 digests，不含 setup、raw Environment、secret、server identity 或 YAML。
- Readiness 是 head-only、bounded、`no-store`、純讀衍生值，不執行 probe 或寫入。
  Required tags 必須同時存在於同一候選。候選只接受 active／approved
  `server-config-v1` revision，並驗 creating approval status、canonical payload、
  exact canonical YAML bytes、normalized target、credential reference 與 digests；
  legacy/runtime-only server 不冒充 evidence。
- Observation 必須晚於 activation、不在未來，且 freshness 為
  `max(60, 3 * MONITOR_INTERVAL_SEC)`。Missing／malformed／future／stale evidence
  是 `unknown`；fresh offline／probe-failed 是 `not_ready`；有任何 ready 候選才
  roll up 為 `ready`。Executable、env、secret-ref、path 等尚無 typed host evidence
  的 check 保持 `unknown`。Response 不暴露 server name、host、user、key、YAML
  或 credential。
- `PROJECT_ENVIRONMENTS_V1_ENABLED=false` 是精確 default；它依賴 API v2 與
  Product RBAC，但不依賴 bootstrap flag。關閉時隱藏 Environment routes 與其
  approval list/detail/decision/workspace entry，保留 additive data 與 evidence。
  Migration v7 source/checksum不變；不新增 migration、table 或 production
  dependency。
- Workspace 對 Environment approval 只從 verified detail 進入決策，approve 前
  必須 explicit confirm。List 僅回 safe summary，不能從 summary 直接核准。
- Environment router 專用 `APIRoute` 捕捉 `RequestValidationError`，只回有限的
  固定 type／message 與白名單 location；raw `input`、`msg`、`ctx`、`url`、body、
  discriminator 值及未知 field name 全部丟棄，`raise ... from None` 且不記錄。
  其他 routes 的既有 FastAPI 422 行為不變，OpenAPI discriminated union 保留。

本裁定只授權本機、additive、default-off 實作。它不授權 production migration、
部署、117 Pilot、遠端 setup 執行、secret resolution、Container／Conda 建置、
Node rollout 或 production-ready 宣稱。

## 決策日期：2026-08-07（DG-RUN-TEMPLATE-V2）

依 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-06 的 public API、typed
contract、canonical number、approval、安全、legacy compatibility 與
Migration v7 ownership 回傳
`SOL_DECISION: APPROVE_OPTION_1_WITH_CANONICAL_DECIMALS_AND_EXPLICIT_LEGACY_ADOPTION`：

- Standalone high-risk kinds 固定為 `run_template_change_v2`／
  `run-template-change-v2` 與 `project_defaults_change_v2`／
  `project-defaults-change-v2`；resource contracts 維持
  `run-template-spec-v2` 與 `project-defaults-v1`，Migration v7 source、checksum
  與 schema 不變。
- Template create 固定 revision 0／空 head，由 server 產生 UUID 與 revision 1；
  legacy row 只能以 exact approved head 明確 `adopt`；typed update 綁 exact head、
  revision 與 spec digest；archive 不接受 replacement，複製完整前版 spec 成
  terminal N+1。所有 typed successor 的 raw command/setup/tag 欄位為 `NULL`，
  name 不可變且歷史 row 不改寫。
- Create／adopt／update 在 request 與 approve 都綁 current approved same-Project
  Environment head，且 Environment required tags 必須是 Template required tags
  子集合。Environment drift 使 pending proposal stale；archive 只重驗 exact
  typed Template head，因此 Environment 漂移或 archive 後仍可關閉 lineage。
- Defaults 只有 project-global create／update immutable history。Create 要求沒有
  head；update 綁 exact head UUID／revision／digest。Target 必須完整綁 current
  approved typed Template head/spec digest 與 current approved Environment head，
  並重驗所有 parameter values；read model 顯示 exact-reference staleness，不修改
  歷史。
- 既有 `canonical_json()` 不修改且持續拒絕 float。Integer 用 JSON integer，bool
  不可冒充 integer；fractional number 使用最長 128-byte、無 exponent／plus／
  leading-zero／trailing-fraction-zero 的 canonical decimal string。Range comparison
  只使用標準庫 `Decimal`；request model 不接受 JSON float 或型別 coercion，既有
  integer-only bootstrap digest bytes 不變。
- Pure compiler 只輸出 `tuple[str, ...]`、canonical JSON array UTF-8 bytes 與其
  SHA-256。Literal／present parameter 各是一個 argv element，missing optional
  省略；每 element 上限 4096 UTF-8 bytes、整體上限 65536 bytes，control／NUL
  拒絕。Printable shell metacharacter 保持單一 inert element；compiler 不呼叫
  shell、不 join command、不做 I/O。
- 無 spec 的既有 lineage 是 `legacy_raw_command` 且 Product one-click eligibility
  為 false；lineage 曾有 typed spec 但 head 無 spec 時是 `contract_invalid`，不得
  fallback。Legacy adoption 必須明確且 stale race fail closed；typed lineage 不得
  走 legacy mutation 或 execution path，真正執行仍等待 PR-10。
- GET 需要 `project.view`；request 需要 `project.operate`。Requester 可為 enabled
  HUMAN 或具 exact scope 的 service Operator；decider 必須是不同的 enabled HUMAN
  Owner／Reviewer 或 Platform Admin。Reject 驗 immutable bytes、scope 與 decider，
  但不要求 requester/head 仍 current。Request、decision、revision、bounded durable
  audit 與 idempotency completion 各自原子；decision identity 綁 payload digest。
- Heads／Defaults history 使用 default 50／max 100 的 bounded cursor pagination；
  validation 是 router-scoped non-reflecting 422。Approval 先載入 verified detail 並
  explicit confirm 才可 approve。`RUN_TEMPLATE_V2_ENABLED=false` 精確預設，依賴
  API v2、Product RBAC 與 Host Environments；關閉時隱藏 routes、approvals、
  decisions、Workspace 與 UI projection，保留所有 additive evidence。

本裁定不新增 production dependency，不啟用 execution／SSH／shell／Job，不修改
Migration v7，也不授權部署、117 Pilot、legacy API retirement 或
production-ready 宣稱。

## 決策日期：2026-08-09（DG-DATASET-SHARING-V2-v1）

依 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-08 的雙邊授權、public API、
offer/grant contract、expiry、撤回、跨 Project scope 與 Migration ownership 回傳
`SOL_DECISION: APPROVE_OPTION_1_WITH_HARDENING`：

- `DATASET_SHARING_V2_ENABLED=false` 是獨立 default-off package，依賴 API v2、
  Product RBAC 與 Dataset Assets。三種 transaction-only high-risk kind 固定為
  `dataset_share_offer_v2`、`dataset_share_accept_v2`、
  `dataset_grant_revoke_v2`；generic decision／auto-approve 不得處理。
- Offer 綁 source/target Project、source-owned asset、sorted unique exact snapshot
  set、asset/snapshot-set/offer digests 與 UTC microsecond `Z` expiry。Snapshot set
  為 1–100 個 bounded opaque text，TTL 為 5 分鐘至 30 天。Source Dataset
  Manager（HUMAN 或 scoped SERVICE）可 request；不同的 enabled HUMAN source
  Owner/Reviewer 才可 decide。Platform Admin 沒有雙邊 Project role bypass。
  Offer approval 只建立 immutable offer，絕不建立 grant。
- Accept request 必須由 target HUMAN Owner 提交完整且逐 byte 等價的 offer
  envelope；不同的 target HUMAN Owner/Reviewer 決定。Request 與 approve 都重驗
  offer provenance/digest/expiry、兩 Project readiness、source ownership、published
  exact links 及 actor state。所有 grant UUID 由 server 預產，decision 一次建立
  全部 exact grants；任一步或 durable audit 失敗全部 rollback，approval 保持
  pending。
- 同一 offer 只能接受一次；同一 target/asset/snapshot 不得同時有兩個 active
  grants。舊 grant 全部撤回後只可用新 offer regrant。Offer expiry 只限制 accept
  window，已建立 grant 不因 offer 到期自動失效；asset 未來 snapshot 永不擴張
  舊 offer/grant。
- `revoke` 由 source Dataset Manager request、不同 source HUMAN Owner/Reviewer
  decide；`unlink` 由 target HUMAN Owner request、不同 target HUMAN
  Owner/Reviewer decide。兩者共用 Migration v8 的 monotonic active-to-revoked
  transition，語意保存在 immutable payload/audit。只要求撤回方 RBAC-ready，
  counterparty governance 失效不得阻止撤回。Offer、alias history、historical
  ExecutionPlan/Job 與 active Job 均不刪除或改寫。
- Canonical eligibility 對 exact `(project_id, asset_id, snapshot_id)` 只回
  `owned_published`、verified `active_grant` 或 fail-closed unavailable。Target
  aliases 沿用 PR-07 CAS，但 request/decision/resolver 每次都重驗 exact grant；
  pending alias 若 grant 先失效不得 materialize，既有 history 保留。
- Cross-Project list 去重 owned/shared assets；asset-scoped detail/lineage/usage/
  storage 對 target 必須有 explicit `project_id`，只投影 verified exact snapshots
  與該 Project aliases。Lineage 兩端都需 eligibility；scope 截斷必須明示。
  Malformed provenance、revocation、不同 target 或最後一個 grant 失效都回 opaque
  absence，不洩漏 source/其他 target/未授權 snapshot 或 path。
- Feature off 隱藏 sharing routes、三種 approval、target alias/read/eligibility 與
  Workspace sharing projection；owner-only PR-07 行為維持，usage 明示
  `dataset_sharing_v2_disabled`。資料與 evidence 不刪除。

本裁定重用既有 Migration v8，禁止修改其 source/checksum 或新增 Migration v9；
也不授權 publish wizard、ExecutionPlan v2、default Dataset bindings、legacy plan/
Job mutation、部署、117 Pilot、legacy retirement 或 production-ready 宣稱。

## 決策日期：2026-08-09（DG-DATASET-PUBLISH-V2-v1）

依 Sol–Luna workflow，唯讀 `sol_advisor` 對 PR-09 的 public API、source
trust、Run evidence、approval/materialization transaction、filesystem crash model、
alias/lineage sequencing 與 Migration ownership 回傳
`SOL_DECISION: APPROVE_OPTION_1_SINGLE_PINNED_DATASET_PUBLISH_V2`：

- `DATASET_PUBLISH_V2_ENABLED=false` 是獨立 default-off package，依賴 API v2、
  Product RBAC、Dataset Assets、Dataset Snapshot build 與 publish。固定 public
  routes 為 project-scoped publish previews/requests；feature off 時 opaque 404，
  Workspace capability、Wizard、approval list/detail/decision 同步隱藏。
- 唯一 mutation contract 是 strict canonical transaction-only
  `dataset_publish_v2`。Dataset Manager 或 Platform Admin 可 request；不同的
  enabled HUMAN Owner/Reviewer 或 Platform Admin 才可 approve。禁止 direct/
  auto approval，也不得串接 legacy snapshot/adoption/alias approval。第一版只能
  建立全新的 Project-owned asset，payload 預配 snapshot、asset、optional initial
  alias revision 1 及 optional Run-lineage UUID。
- Preview 完全唯讀；request 使用同一 secure scanner 重掃。Expected preview
  digest 不一致回 409，且必須在 idempotency binding/UoW 前結束，因此 approval、
  idempotency、snapshot、audit materialization 皆為零。Preview 固定 source identity、
  candidate/manifest digest、file/byte count、store revision、shard policy、max bytes、
  完整 Data Card、asset metadata 與 optional alias。
- Local source 只接受 operator 明列的 `DATASET_PUBLISH_LOCAL_ROOTS`；空清單停用
  local mode，不得借用 compute roots。Preview/request/approve/build/resume 每階段
  均重建 canonical containment；禁止 broad/symlink roots、`..`、prefix collision、
  symlink traversal、special file、source/store overlap。Scanner 與 builder 使用
  dirfd、`O_NOFOLLOW`、`fstat` identity checks，public API/error/audit 不得出現
  absolute path。
- Run-output source 必須綁 exact Project、Plan、done/exit-0 Job、exact successful
  Node attempt、typed Run Profile spec/output declaration，以及 canonical delivered
  `result_collection` payload/output evidence。Payload 必須精確綁 attempt server 與
  server-config revision；output 必須是 required/collected/path-available true。
  Source 只能由 managed `LOCAL_HOME_DIR/results/{job_id}` bounded scan 找到唯一
  kind-matching path。Legacy SSH/directory existence 不算 evidence；沒有 exact
  governed input asset snapshot 的 Run 不可 publish 或偽造 lineage。
- Approve 前先重掃，drift 時 approval 保持 pending且不建 building row。其後在
  `BEGIN IMMEDIATE` 內重驗 immutable payload、兩人角色、Project readiness、Run
  DB evidence、name/UUID/alias/lineage conflicts；approval decision、durable audit
  與唯一 building snapshot 同 transaction commit。同名 concurrent approvals 只有
  一個可取得 reservation。
- Filesystem blobs 先以 content address 安全 publication，不宣稱與 SQLite 原子。
  最終單一 transaction 驗 exact approved/building/builder evidence，寫 shards、
  publish snapshot、建 asset/link、optional initial alias、optional exact lineage 與
  bounded audit；任一步失敗全部 rollback，snapshot 保持 building。Store/build
  interruption與 response loss 使用同一 approval、snapshot/asset/alias/lineage IDs
  專用 resume/dedup；精確完成 replay 不建重複 row。
- 不支援 existing-asset publish、後續 alias move、多重 output match、弱 completion
  evidence、`dataset_none` 假 lineage或 legacy `datasets` row。重用現有 Migration
  v8，checksum `bcfaadfa86db2d8f3d8f79102cddc5ca7c2f3f8023762ca150f8175d64e18b81`
  不變；PR-09 不新增／占用 Migration v9，v9 繼續由 PR-10 專屬。

本裁定不新增 production dependency、不啟用 feature、不授權部署、117 Pilot、
legacy retirement、Canary 或 production-ready 宣稱；本機通過測試只證明
implemented/local evidence。

## 決策日期：2026-08-10（DG-EXECUTION-PLAN-V2-v1）

依 Sol–Luna workflow，PR-10 的 public API、Migration v9、approval/Job/Attempt
schema、安全、Dataset usage 與跨 Project denial 由唯讀 `sol_advisor` 裁決；
後續補充決策分別為
`SOL_DECISION: APPROVE_OPTION_1_CLOSED_V2_VERSION_MAPPING`、
`SOL_DECISION: APPROVE_OPTION_1_AVAILABLE_EMPTY_PROJECT_DEFAULTS_V1` 與
`SOL_DECISION: APPROVE_OPTION_1_OPAQUE_RUN_PROJECT_DENIALS`，以及
`SOL_DECISION: APPROVE_OPTION_1_VERIFIED_EXECUTION_PLAN_REVIEW`：

- `RUN_EXPERIENCE_V2_ENABLED=false` 是 default-off package，依賴 API v2、
  Product RBAC、Host Environments、Run Templates 與 Dataset Assets。固定 mutation
  surface 為 Project-scoped preview/request 及既有 Product approval endpoint 的
  verified `execution_plan_v2` branch；不改 v1 routes、raw typed-template legacy
  eligibility 或 SSH backend 預設。
- Preview/submit 共用同一 resolver，固定 promoted ProjectVersion、current typed
  Template 或 exact current Defaults、current Environment、canonical parameter
  values、explicit Dataset none 或 1–32 resolved bindings、eligible SSH target
  revision、clean exact checkout、fresh resource evidence、argv/outputs/resources
  digest 與 generated Job command digest。Preview 零寫入且只回 review-safe spec；
  host/user/key/path/setup/command bytes不回傳。
- Alias 在 preview/submit 解析為 exact snapshot 並保存 historical alias revision
  UUID/digest；approve驗該歷史 revision，不重新跟隨 current head。Shared binding
  固定 exact grant/offer/accept provenance並重驗 active state。Dispatch Policy固定
  exact head digest及 deterministic chosen target。Template、Defaults、Environment、
  grant、policy、target、checkout、resource或command drift都不建 Job。
- Migration v9 專屬 immutable one-to-one `execution_plan_v2_specs` companion，保存
  canonical spec及全部 exact-reference/digest evidence。Insert trigger綁 legacy plan
  projection、same-Project typed rows、active target與 pending immutable approval；
  update/delete拒絕。Migration與 trigger replacement/ledger row同 transaction，失敗
  rollback後可重試，且不 backfill任何 plan、Job、approval或attempt。
- Approval payload contract固定為 `execution-plan-v2-approval-v1`；Job與Attempt
  semantic contract固定為 `execution-plan-v2`。`jobs_execution_pin_insert_guard`
  只對 exact kind加這兩個 exact version開放封閉例外；所有 generic/v1 contracts
  保持 payload/Job version equality。Attempt與 prepare/launch/collect authorization
  驗 exact plan/companion/approval/Job/target/command linkage；同 server不同 revision
  仍拒絕，未授權 operation不借用 execution approval。
- Requester必須是 enabled HUMAN/scoped SERVICE Operator；decider必須是不同的
  enabled HUMAN Owner/Reviewer。Request、approval、plan、companion、Job、durable
  audit及 idempotency completion各自在其UoW原子；audit injection或 digest mismatch
  不留下 partial row。Duplicate submit/decide replay既有 identity，不建立第二個
  plan或Job。
- Dataset usage只讀 verified `execution_plan_v2_specs` resolved bindings，限定 exact
  scope Project、asset及目前可見 snapshot集合，bounded deterministic limit-plus-one，
  不查 legacy `execution_plans.dataset_snapshot_id`，也不重解 alias head。
  `project-defaults-v1` 的封閉 contract只有 Environment、Run Profile與 parameter
  values，無 Dataset binding；因此 `project_defaults` 是 exhaustive
  `available/items=[]/truncated=false`，不得由 Plan參照或 parameter名稱反推。
- Run preview/request在 middleware與router兩層均把 cross-Project及 missing
  membership denial映成 opaque 404；同 Project role insufficient仍403，anonymous
  仍401。只加入兩個 exact method/path tuple，不改 authorization matrix或 catalog。
- `GET /api/v2/approvals/{approval_id}` 必須在 opaque authorization 與既有
  envelope verification 後，對 `execution_plan_v2` 另外驗 plan、companion 與 typed
  spec，並回傳 `review={execution_plan_id, plan_digest, contract}`；`contract` 精確是
  submit 時 immutable `ExecutionPlanV2Spec`。既有 approval `payload` 仍只有四欄，
  list 與其他 approval detail shape 不變。Verifier 失敗對已授權 caller 回 409，
  foreign caller仍404；不得序列化 raw plan/companion、command、argv、observation、
  normalized target、credential、host/user/key、checkout path或setup command。
- Submit idempotency identity 必須先於 current-state resolver replay 判定；首次
  request只在同一 UoW callback內重解與驗 expected digest，失敗整筆 rollback。
  已完成的同 actor/key/body則直接 replay immutable identity，即使其 target已忙、
  observation過期或head已變，不得把合法重送改成409。
- Runtime verifier逐欄把plan、companion split evidence、canonical JSON/digest、
  argv hash、self-contained submit observation provenance/digest、requester與
  timestamps綁回 typed spec及approval；detail與approve共用此 verifier。來源
  `server_observations` row依retention正常prune不會使歷史detail失效。Migration v9
  table存在但ledger仍為v8不算ready。
- `execution-plan-v2` Job只允許 exact-revision generic attempt path。Attempt launch
  gate/leader/revision map未就緒時保持queued；map指向不同revision時attempt拒絕且
  不得fallback到legacy SSH。Legacy reconcile、stall probe與dispatch均不得碰v2
  Job；dispatch JSONL只記approved command digest、approval ID與contract version，
  不記raw command/path/setup。

Runtime rollback先關閉 `RUN_EXPERIENCE_V2_ENABLED`，保留 Migration v9、plan、
approval、Job、attempt、idempotency與audit evidence；不取消或改寫已核准 Job。
本裁定不授權 deployment、117 Pilot、legacy retirement、Canary 或
production-ready 宣稱。

## 決策日期：2026-08-16（DG-SELF-APPROVAL-OPTION-v1）

為提高小型可信任團隊的開發效率，新增 default-off 部署政策
`ALLOW_HIGH_RISK_SELF_APPROVAL`：

- `false` 保留原本 high-risk requester／decider 分離規則。
- `true` 允許原本就具決定權限的 enabled HUMAN（含 Platform Admin、Owner、
  Reviewer）決定自己提出的 high-risk approval。
- 不放寬角色或 Project scope，不允許 Service actor 決定，不取消 approval、
  immutable payload/digest、approve-time revalidation、idempotency 或 durable audit。
- 每個決定仍保存 requester、decision actor、mechanism、時間與 note；關閉開關即可
  回復雙人分離，既有歷史不重寫。

## 決策日期：2026-08-23（產品最終完成品釐清與 Claude 主力方向）

使用者於 Claude Code session 中逐題裁定最終完成品定義，正式文本已寫入
`docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`
（PR #36 重寫版）。本條目為權威裁定紀錄；均為**產品方向裁定**，不變更任何
canonical invariant，也不啟用任何 default-off 能力。

```text
PROD-1 使用者模型：小團隊、單人審核（ALLOW_HIGH_RISK_SELF_APPROVAL 姿態），
       不做 two-person review；approval 閘門與「agent 永不自核」不變。
PROD-2 執行規格介面：DB typed immutable revisions（Run Template v2 /
       Environment revisions）為正式真相；dispatch.yaml 降為匯入/匯出格式，
       匯入產生的草稿仍經 approval 落地。
PROD-3 GitHub 角色：開發過程推 GitHub 作為紀錄；本地 hub 保有最新版並
       維持為執行面 code source。實作前提為 DG-GITHUB-PUBLISH 核准；
       promotion 不自動 push（DG-CODE-PROMOTE P-4 不變）。
PROD-4 Agent 互動形態：每 Project 長期對話 + 受控 task 並存；第一版單一
       main conversation；對話本身無執行權。
PROD-5 優化迴圈上限：限額式自動迴圈（一次核准一個額度，額度內自動迭代、
       超額即停）；實作前需要對 approval 機制的新具名裁定，裁定前維持
       每輪人工確認。
PROD-6 執行後端：Node Agent 為最終主力，SSH 依 INV-SSH-1 永久保留為
       相容/緊急通道；實機啟用仍以 DG-NODE-CANARY 為前提。
PROD-7 Development Agent 主力 provider：最終完成品以 Claude（Claude Code）
       為主力；Codex 為現行已實作 provider 與備選。provider-neutral
       架構與「selection 永不是權限提升」不變；本裁定不代表 Claude Code
       adapter 已存在或已核准實作（見 DG-CLAUDE-ADAPTER）。
```

一併裁定的文件處置：`docs/NEXT_IMPLEMENTATION_PLAN.md` 維持保留（其內容
被 `tests/test_exec_attempt_decision_gate.py` 釘住），**未來與該測試一起
退役**——屆時把釘住斷言改釘 `docs/DG_EXEC_ATTEMPT_DECISION.md` 後刪檔，
屬邊界測試變更，需屆時單獨裁定後執行。

同日背景（非本條裁定範圍，先前已各自成立）：Development/Compute Plane
模型與 provider-neutral Development Agent 架構已寫入 `CLAUDE.md` 與
`.claude/skills/dispatcher-domain/references/development-platform.md`
（PR #31–#33）；過時文件清理（PR #34）。

## 決策日期：2026-08-23（DG-PERSONAL-PILOT-v1：single-user usable pilot）

使用者裁定以 legacy 表面 + v1 接縫建立單人可用 pilot，四項裁定如下。
均為部署／產品方向裁定，不變更任何 canonical invariant，不產生任何
production-readiness、deployment（pilot 環境以外）或 canary 證據。
實作計畫見 `docs/product/PERSONAL_PILOT_PLAN.md`；完整 second-pass
審視與分階段 roadmap 見
`docs/product/FULL_PLATFORM_SECOND_PASS_PLAN.md`。

### D1 — Pilot 安全姿態：維持現行預設（shared token + authorization off）

沿用 shared `X-Auth-Token` + `AUTHORIZATION_MODE=off`（兩者皆為現行
預設，非放寬）。本姿態**僅限**：single-user、non-production personal
pilot、private/trusted network。明文約束：

- 本裁定**不是**永久取消 authorization/RBAC 的產品決策；Product v2 的
  RBAC/enforce 目標不變，其 activation 仍需屆時的獨立裁定。
- Pilot 期間的任何運行紀錄**不得**作為 production-readiness 證據。
- `docs/CAPABILITY_LEDGER.md` 不因本 pilot 升級任何
  `deployed`/`canary-proven`/`production-ready` 欄位。
  （2026-08-25 修訂：本點由同日「DG-PERSONAL-PILOT-v1 D1 clarification」
  節取代——pilot 的直接 runtime 證據可支撐 `deployed=yes`，evidence 必須
  明標 personal-pilot-only；`canary-proven`/`production-ready` 仍永不因
  pilot 升級。）

### D2 — 第一個 promoted ProjectVersion：走正常流程，不加捷徑

第一個 promoted ProjectVersion 必須經正常 engineering task →
human review（diff）→ `engineering_task_promote` approval 產生。
不新增任何 promotion shortcut、不新增「promote 既有 commit」路徑。
`require_reproducible=false` 維持既有 API 行為，不做 UI、不推薦使用。
DG-CODE-PROMOTE-v1 P-1…P-5 全部不變。

### D3 — Results 存取：最小唯讀 list + download

新增 job-scoped 唯讀 results 列表與單檔下載：path-safe（嚴格限定
`results/{job_id}/` 之內，拒絕 traversal 與 symlink escape）、
bounded（列表筆數與 inline 預覽大小有上限）、authenticated（不進
auth 豁免清單，INV-APPROVAL-5 預設涵蓋）。**不做** metrics
parsing、schema、migration；`metrics.json` 僅以原文顯示。
`metrics-v1` 契約名稱保留給未來 DG-METRICS-CONTRACT。

### D4 — Pilot 表面：Legacy-first

Pilot 以現有 legacy UI 跑通完整 workflow。Product v2 仍是 final
target architecture；本 pilot 不構成 v2 activation，v2 各 feature
flags 維持關閉。Pilot 架構 ≠ 最終架構，兩者的收斂另案裁定。

### 一併確認的用語規範

Pilot 文件一律區分三類：**feature flags**（預設關閉的布林開關，如
`ENGINEERING_TASK_BACKEND_V1`）、**configuration**（設定值，如
`CODEX_RUNNER_SERVER`）、**activation steps**（經 approval 或操作在
運行系統上執行的動作，如 `server_update` 核准鑄出 approved+active
server config revision、runner 機安裝登入 codex CLI）。

同場session的先行架構方向選擇（均為方向裁定，實作各自另案）：
M2 對話腦採 control-plane orchestrator（沿用既有 LLM tool-loop 邊界，
不採 runner 上常駐互動 session）；Experiment 採一 matrix 一 approval
（未來 `experiment_create_v2`，實作前需具名裁定）。DG-CLAUDE-ADAPTER、
DG-METRICS-CONTRACT、DG-PRODUCT-PLAN-CORRECTIONS 維持**未裁定**
（DG-CLAUDE-ADAPTER 於 2026-08-24 另行裁定，見下節）。

## 決策日期：2026-08-24（DG-CLAUDE-ADAPTER v1：approve bounded implementation）

使用者具名核准 `docs/DG_CLAUDE_ADAPTER_DECISION.md` 的 bounded
implementation。本裁定**只允許實作** `claude-code-v1` 作為第二個
Development Agent provider；不代表立即啟用、不代表 production-ready，
也不包含長期對話、Auto selection、新 approval kind 或任何 invariant
修改。六項裁定點全部採建議值：

- **C-1 准入形態：approved-gated**——`claude-code-v1` 進入正式 approved
  provider registry，受 `CLAUDE_CODE_AGENT_V1` feature flag 控制；
  flag off 時不可選、不可使用。
- **C-2 執行承載：Job-backed runner**——沿用現有 Codex 的 runner／
  isolated worktree／Job-backed execution 模式；Claude Code 使用
  headless one-shot turn；instruction 必須透過 file + stdin 傳遞，
  不得插入 shell command string（INV-SSH-2）；沿用既有 sentinel
  terminal-state contract（INV-SSH-6）；**不建立新的
  validation/execution mechanism**。
- **C-3 Provider selection：approve explicit `agent_provider_id`**——
  Engineering Task request 可明確指定 provider id；只接受 approved
  registry 中且目前 enabled 的 provider；預設仍為 `codex`；本 slice
  不實作 Auto selection；不允許 silent fallback 到其他 provider。
- **C-4 Feature flag：`CLAUDE_CODE_AGENT_V1=false`**——預設關閉；
  裁定與 merge 不得自動啟用；啟用屬後續 deployment/operator action。
- **C-5 CLI version/protocol drift：fail closed**——pin 已驗證的
  Claude Code CLI 版本或相容範圍；capability/version probe 不符即
  拒絕執行；輸出無法解析即 task failure 並保留 evidence；永不
  fallback 到 Codex 或其他 provider。
- **C-6 Credential boundary**——Claude Code login/auth state 只存在
  runner；credential/API key/token 不得進入 instruction、prompt、DB、
  audit event、diff、artifact；Server A 不持有 Claude Code runner
  credential。

**一併納入 bounded implementation scope**：

1. Web UI provider selector——Engineering Task 建立介面加入 provider
   選擇；只顯示目前 enabled + approved providers；只有一個 provider
   時可隱藏 selector；預設 codex；不含 Auto selection。
2. Verification——full test suite 維持 green；更新既有 test-count
   gates（依各自文件化流程）；forbidden names／forbidden imports／
   pinned boundary assertions 原樣通過；flag off 時 Claude provider
   完全不可用且 Codex 行為零改變；flag on 時以 fake runner／fake
   protocol 完成 request → approval → isolated worktree → Claude
   one-shot turn → validation → diff → bundle 全流程。

**Non-goals（明文不做）**：persistent AgentSession；resume／interactive
Claude session；Auto provider selection；provider fallback；新 approval
kind；auto-approval policy 變更；任何 invariant 修改；啟用
`CLAUDE_CODE_AGENT_V1`；production deployment。

## 決策日期：2026-08-24（DG-CONVERSATION-V1：approve bounded implementation）

使用者具名裁定「整體核准，CV-2 選先2a後2b」，指向
`docs/DG_CONVERSATION_V1_DECISION.md`。裁定內容：

- **CV-1 approve**：additive migration 新增 `ai_conversations`（每
  Project 唯一 main）與 `ai_conversation_messages`；SQLite 是唯一真相，
  WS 只是傳輸；訊息大小與載入筆數有上限；retention 另案。
- **CV-2 staged：先 2a、後 2b**——本切片實作 **2a**（Anthropic API 直連，
  沿用既有 LLM tool loop，工具集零擴張，INV-LLM-1/2/3 一字不動）；
  **2b**（Pro/Max 訂閱承載：runner headless `claude -p` turn + MCP bridge
  工具）為已核准的後續方向，其輕量 chat-turn 通道屬新 validation
  mechanism，實作前仍以屆時的 bounded packet 確認設計，不得先於 2a
  完成動工。兩案憑證/登入態永不進 DB、audit、diff。
- **CV-3 approve**：conversation 釘死單一 project；查詢與 `request_*`
  提案預設以該 project 為 scope；task/run 參照持久化於訊息。
- **CV-4 approve**：每個動作各自成卡各自核准；對話永不自動連鎖下一步。
- **CV-5 approve**：Project 詳情頁「AI Engineer」分頁，v1 非串流；
  全域 chat 分頁保留。
- **CV-6 approve**：`PROJECT_CONVERSATION_V1_ENABLED=false` 預設關閉。

不新增 approval kind、不改 auto-approval、不改任何 invariant；
Non-goals 依 packet §1。本裁定不啟用旗標，啟用屬部署動作。

## 決策日期：2026-08-24（DG-AGENT-SESSION-V1：Hybrid Web-hosted Claude Code Runtime）

使用者核准 Hybrid Web-hosted Claude Code Runtime 的 V1 方向，取代
DG-CONVERSATION-V1 §6a 的 completion-backend 草案與原 CV-2b MCP 草圖
（兩者標記 superseded）。目標：把 Claude Code 的 development-agent 體驗
搬進 Web UI——persistent AgentSession + persistent isolated workspace +
per-turn Claude process，dev-local 工具限定 workspace，platform 權限
全部留在 Server A + human approval。

六項裁定：

- **D1 APPROVE**：新 approval kind `agent_session_open`——一次核准 =
  建立 session workspace + 授權該 session 內的有界 turns；永不自動核准；
  關閉/過期即失效。
- **D2 APPROVE**：per-turn 執行通道為具名核准的新 validation mechanism
  ——每 turn 一個有界 tmux session + exit_code sentinel（INV-SSH-6 同構）、
  明確 timeout、prompt 走 SFTP 永不進 shell 字串（INV-SSH-2/3）、
  runner 不可達 = 降級不判錯（INV-SSH-7）；不進 job queue（pinned runner）。
- **D3 APPROVE（僅限 non-production personal pilot）**：confinement 以
  pinned Claude Code CLI 版本 + pinned 設定實現（檔案工具限 workspace、
  Bash 僅 validation allowlist、其餘 deny），實作時驗證、無法確保即
  BLOCKED；runner OS-user 層級殘餘風險與 2026-07-25 accepted
  unsandboxed finalization 同一姿態。
- **D4 APPROVE**：V1 零 platform 工具（比 INV-LLM-1 上限更緊）；
  未來開放「建 pending 卡」屬另案具名裁定。
- **D5 APPROVE**：turn timeout 10 分鐘、每 session 上限 200 turns、
  閒置 7 天自動 close。
- **D6 APPROVE**：沿用 CV-2a 的 AIConversation 持久層；session 以 FK
  綁 conversation。

**Target requirement（架構約束，非 V1 範圍）**：AgentSession 不是
coding-only。最終 lifecycle：Develop → Validate → Promote → Run →
Collect Evidence → Analyze with Skills → Recommend Optimization →
Develop Next Iteration。為此 V1 一併裁定三條演進護欄：

- **E-1 Skill ≠ permission**：Skill 只以檔案物化進 session workspace
  （knowledge/workflow/reasoning），永不改變 launcher 的工具/權限設定。
- **E-2 Evidence 經 Server A**：Run evidence（status/plan/version/
  metrics/logs/artifacts/dataset/environment/比較）未來一律由 Server A
  的受控介面物化成唯讀檔案進 workspace；runner 永不持 platform 憑證、
  永不 SSH 至 Compute node 自取證據。
- **E-3 Task-neutral 核心**：`agent_sessions` schema 與狀態機不含
  coding 專用語意；V1 即帶 `provider_id` 欄（預設 claude-code）。

**V1 scope**：persistent AgentSession、persistent isolated workspace、
per-turn Claude Code process、dev-local tools、transcript（stream-json
落檔 + live tail）、diff/validation、checkpoint、既有 promotion flow。
**明文延後**：structured Run Evidence tools、metrics-v1、analysis
Skills、experiment comparison、optimization loop、agent-generated Run
proposal、automatic iteration——且這些未來能力不得要求重做 AgentSession
核心架構（本節護欄即為此而立）。

Claude 永不可：self-approve、self-promote、direct dispatch Compute、
arbitrary SSH、deploy、改 protected server configuration、改 dataset
permission、存取 platform credentials。不變更任何 canonical invariant；
`agent_session_open` 之外不新增 kind；旗標 default off。

## 決策日期：2026-08-24（DG-AGENT-SESSION-CHECKPOINT：A 核准）

使用者具名裁定選項 A：新增 approval kind **`agent_session_checkpoint`**
（指向 `docs/DG_AGENT_SESSION_CHECKPOINT_DECISION.md`）。理由：既有
DG-CODE-PROMOTE／ProjectVersion promotion contract 優先保持不變；session
成果必須有真實、可追溯的 approval provenance；保持 Development Session →
Checkpoint → Promote → ProjectVersion → Run 完整閉環；不為減少一次點擊
改造敏感 promotion 機制。

**語意定義（裁定原文）**：

- `agent_session_checkpoint` approval＝使用者確認目前 Session workspace
  的修改可以被封裝、驗證成 promotion candidate。
- `engineering_task_promote` approval＝使用者確認該 candidate 正式成為
  ProjectVersion。
- **兩者不可合併、不可自動核准**（enqueue|stop 白名單不動；
  INV-APPROVAL-4／4b 不動；DG-CODE-PROMOTE P-1…P-5 一字不改）。

**UX 附帶裁定**：checkpoint approval 完成後，Session UI 直接顯示
Promote action（同頁完成兩段核准的請求端，決策端仍在核准頁），
避免切頁；此為介面便利，不改變任何核准語意。

**實作約束**：最小實作；重用既有 path-policy 雙檢、bundle 驗證、
promotion pipeline；不新增自動 promotion；bridge 列以本核准為
`approval_id`（誠實 provenance，metadata 標注 session 來源）。

## 決策日期：2026-08-24（DG-METRICS-CONTRACT v1：A 核准）

使用者具名裁定選項 A（指向 `docs/DG_METRICS_CONTRACT_DECISION.md`）：
核准 **metrics-v1 檔案契約**的 bounded implementation。

- **契約**：workload 寫 `results/{job_id}/metrics.json`——單一扁平
  JSON object；≤64 KiB；≤256 keys；key 字元集 `[a-z0-9_.]`（1–128）；
  值僅 JSON integer / bool / bounded string（≤4096 bytes）/ canonical
  decimal string（沿用 `canonical_decimal()` 規則）；**拒絕 JSON
  float**。檔案經既有 rsync 收集一併回收，零新遠端指令
  （INV-SSH-4 不動）。
- **解析點**：job-finish hook 於 `pull_job_results` 成功後、Server A
  本地純函式解析；engineering-task recovery 路徑同樣收斂。解析
  失敗/超限/缺檔永不影響 job 狀態機、reconciliation、scheduling——
  哨兵 exit_code 仍是唯一終態來源（INV-SSH-6 明文不動）。
- **儲存**：additive migration v13——`run_metrics(job_id, key,
  value_type, value_text, recorded_at)` UNIQUE(job_id,key) +
  `run_metrics_collection(job_id UNIQUE, status, reason,
  source_sha256, collected_at)`，`status ∈
  collected|missing|invalid|oversize`；永不寫在 `jobs` 上。
- **語意**：missing = unknown；invalid 必附 reason；重收斂以
  `source_sha256` 判斷、整批替換冪等。
- **讀取面**：唯讀 `GET /jobs/{job_id}/metrics`（沿用 results API
  auth 慣例）+ Product Run projection 之 `metrics_status`/摘要；
  Dashboard/Compare UI 屬 M3 另案。
- **旗標**：`METRICS_V1_ENABLED=false` 預設關閉；off 時零解析、
  零新表寫入、endpoint 不可用，現行為零改變。

**明文否決**：log scraping；worker push endpoint；metrics 影響任務
終態或排程；新 approval kind；歷史 results backfill（延後）。
不改任何 canonical invariant；本裁定不啟用旗標，啟用屬部署動作。

## 決策日期：2026-08-24（DG-PRODUCT-PLAN-CORRECTIONS v1：核准）

使用者核准第二次獨立審視（`docs/product/FULL_PLATFORM_SECOND_PASS_PLAN.md`
Part B）的三處修正記入正式產品定義，並依 §E-4 編輯清單 E1–E8 修訂
`docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`：

1. **Auto provider selection 降級**：完成品要求 Manual + per-Project
   預設 provider；Auto 引擎為可選延伸，非 Definition of Done。
   「selection 永不是權限提升／永不 silent fallback」約束保留。
2. **metrics-v1 是一級產品契約**（同日 DG-METRICS-CONTRACT v1 已
   裁定其內容）；並更正原 §15「部分已實作」的現況誤述——metrics
   解析在裁定當日之前完全不存在。
3. **Product Workspace（v2 UI）是最終唯一主介面**；legacy UI 為
   相容過渡產物，退場條件沿用 API v2 cutover 裁定。

純文件裁定：不改程式行為、不改 invariant、不啟用任何旗標。

## 決策日期：2026-08-25（DG-EXPERIMENT-V1：A 核准，EX-1…EX-7）

使用者具名裁定選項 A（指向 `docs/DG_EXPERIMENT_V1_DECISION.md`）：核准
**`experiment_create_v2`** 的 bounded implementation（一 matrix 一
approval，承 2026-08-23 產品裁定與 second-pass C6）。

- **EX-1 新 kind `experiment_create_v2`（transaction-only）**：request
  以純函式驗證 matrix（axes 展開、每值過 `validate_value()`）並對每個
  組合完整跑既有單 run resolver，產出 N 份完整 `ExecutionPlanV2Spec`
  （同 version/environment/template/dataset digest，僅 parameter_values
  不同）；target 清單明選、round-robin 確定性指派；payload 不可變、
  列全數 N 個 plan digest；任一組合 resolve 失敗＝request 失敗。
  永不自動核准（INV-APPROVAL-4 天然排除，白名單不動）。
- **EX-2 原子 materialize（all-or-nothing）**：單一 transaction 內對
  每個 plan 重驗（INV-APPROVAL-3）→ N plans + N Jobs + durable audit；
  任一失敗＝整筆拒絕、零列落地。**「一 plan 一 Job」既有三重上界
  一條不動**（C6）。
- **EX-3 batch-aware exclusivity**：`_worker_is_exclusive` 對外部
  queued/running/attempt 維持原判；同一 experiment 內指派同 server 的
  成員互不視為衝突（由既有一機一件排程天然串行）。不動排程語意。
- **EX-4 儲存**：additive migration——`experiments`（approval_id
  UNIQUE）+ `experiment_plan_members`（plan_id UNIQUE）membership 表；
  不動 `execution_plans` schema/triggers；`experiment_records` 筆記
  例外完全不混用。
- **EX-5 Guard**：`MAX_EXPERIMENT_RUNS = 32` 硬上限；byte 上限比照
  `execution_plan_v2.py` 慣例；est. GPU hours/storage 為展示性宣告，
  V1 無資源推估引擎。
- **EX-6 讀取面**：`GET /api/v2/experiments`（project scope）+
  `GET /api/v2/experiments/{id}` 成員投影（product run store +
  `metrics_status`/metrics 摘要）；compare 維持雙邊，N-way 延後。
- **EX-7 旗標**：`EXPERIMENT_V2_ENABLED=false` 預設關閉，依賴
  `RUN_EXPERIENCE_V2_ENABLED` 鏈；LLM/agent 工具零擴張（V1 不給任何
  agent 建 experiment 卡的工具，另案裁定）。

**明文延後**：optimization loop／限額自動迭代（DG-OPTIMIZATION-QUOTA）、
auto placement 整合、N-way compare、agent-generated experiment、
Dashboard 完整 UI。**BLOCKED 條件**：需放寬任何既有 materialization
上界、需動一機一件排程語意、或 batch-aware exclusivity 無法 fail-closed
實現。不改任何 canonical invariant；本裁定不啟用旗標。

## 決策日期：2026-08-25（DG-PERSONAL-PILOT-v1 D1 clarification：pilot deployed 證據）

使用者裁定，解決 DG-PERSONAL-PILOT-v1 D1 第三點與 `docs/CAPABILITY_LEDGER.md`
`deployed` 欄位語意的衝突（該衝突於 2026-08-25 補齊 AI-engineering capability
rows 時被指出：`metrics_v1` 等 row 已依 pilot 證據記 `deployed=yes`，而 D1
原文寫「不因本 pilot 升級任何 `deployed`/`canary-proven`/`production-ready`
欄位」）。裁定原文：

> Personal pilot evidence may establish `deployed=yes` when the repository
> contains direct evidence that the capability is installed and enabled in the
> pilot runtime. Such evidence must be explicitly scoped as personal-pilot-only
> and must not establish `canary-proven` or `production-ready`.

即：D1 第三點修訂為只禁止 pilot 證據升級 `canary-proven` 與
`production-ready`；`deployed=yes` 可由 pilot 的直接 runtime 證據支撐，但
evidence 欄必須明標 `personal-pilot deployment only`。D1 其餘兩點（pilot
安全姿態的適用範圍限制、pilot 運行紀錄不得作為 production-readiness 證據）
一字不變。

範圍：純文件裁定——既有 capability rows 的 `deployed=yes` 維持；不改
runtime code、feature flags 或任何 canonical invariant。

## 決策日期：2026-08-25（DG-UI-UNIFICATION v1：核准）

使用者核准 UI 統一計畫（plan：`~/.claude/plans/compiled-prancing-salamander.md`，
本紀錄為權威摘要）：把 legacy（`static/index.html`+`ui.js`）與 Product v2
Workspace 整合為**單一中文 Workspace surface**。

- **範圍：一次全搬**——全部 legacy 功能面板遷入 Workspace（packet
  U1–U8 順序實作，每包全綠 commit＋部署 pilot 交測）；全部完成後
  移除 legacy 三檔與其 pinned 測試（等價保護先由新測試承接）。
  過渡期間**不留**舊介面逃生口。
- **JSON 呈現**：每種核准卡/預覽以中文摘要列必要欄位；完整 payload
  一律保留在預設收合的「查看原始內容」（審核證據不丟）。
- **決定通道統一**：`POST /api/v2/approvals/{id}/decisions` fan-out
  新增 generic legacy-kind 分支，內部呼叫與 legacy `/approve|/reject`
  完全相同的 `approvals_module.approve()/reject()` 引擎，沿用
  compatibility-snapshot digest 驗證（enqueue 先例）。**語意零變更**：
  同引擎、同授權（Action.APPROVAL_DECIDE）、同稽核；
  `ONE_TIME_SECRET_APPROVAL_KINDS` 在 v2 明確拒絕（比照 legacy 停用
  語意）。無新 approval kind；auto-approve 白名單、INV-APPROVAL-5
  豁免集合、reviewed-allowlist 前端架構全部不動。
- **API 策略（A-lite）**：缺 v2 端點者新增 thin `/api/v2` wrapper
  （同 domain 函式、進 authorization catalog）；legacy API 端點退場
  於 U8 另議。Chat 沿用既有 `/ws`（auth 協議不變、pinned 字面路徑）。
- **前端架構**：新增第二個 IIFE 檔 `workspace-features.js`
  （`frontend_smoke` 的 script 計數 pin 依文件化流程 1→2）；
  無框架、無 build step、no-innerHTML/no-storage/Idempotency 慣例
  全部保留並延伸；全介面繁體中文。

本裁定不改任何 canonical invariant。

### 完成紀錄（2026-08-26，U8 收尾）

U1–U8 全部落地：總覽整併（worker 健康卡／活動與稽核合併 feed／管理入口）、
`GET /` 固定回傳 `static/workspace.html`（`API_V2_ENABLED` 關閉時回內嵌中文
提示頁，不再回退 legacy）、legacy `static/index.html`／`ui.js`／`ui.css`
三檔與其 5 個 pinned 測試檔已刪除。刪除前逐一盤點每個 legacy pin 的等價
保護，缺口（download-safety、provider selector、AI conversation、matrix
pending-candidates 等）已先補進 `tests/test_identity_workspace_v2.py`／
`tests/test_project_conversation.py`／`tests/test_claude_code_agent.py`
等既有測試檔，保護未出現空窗。`scripts/frontend_smoke.py`、CI
`node --check`、`scripts/check_wheel_boundaries.py` 均已改指向
`workspace.*` 資產。本次未改任何 canonical invariant。

## 決策日期：2026-08-26（DG-INFRA-DIRECT-ACTIONS v1：核准）

使用者具名裁定（原文：「基礎設施的新增／更新／停用／刪除一律先建立
核准卡，這件事除了刪除要核准卡其他的不用！」）：

- **`server_add`／`server_update`／`server_disable`（含重新啟用）改為
  直接執行的 web 動作**：`validate_server_config()` 驗證先行（不合法
  即拒、零寫入）、寫入 servers.yaml 前備份、完整稽核——比照 hub_sync
  既有 direct-execute 例外模式。此為 INV-APPROVAL-1 明文例外列舉的
  使用者裁定擴充，invariants.md 同步更新。
- **`server_delete` 維持核准卡**（基礎設施唯一保留的核准動作）。
- kinds 保留於 `VALID_APPROVAL_KINDS` 供既有 pending 卡相容決定；
  auto-approve 白名單（enqueue|stop）與 INV-APPROVAL-4/4b 完全不動。
- 附帶修正：generic compatibility decision 的失敗訊息帶出原因
  （如快照過期），不再只回「could not be applied」。

背景：#155/#157 兩張 server_update 卡釘同一份 yaml 快照，#157 生效後
#155 因 INV-APPROVAL-3 過期重驗被正確拒絕——本裁定同時消除此類
同文件競態。

## 決策日期：2026-08-26（DG-ASSISTANT-CLAUDE-TURN v1：核准）

使用者核准助手大腦改造計畫（plan 檔為權威細節，本紀錄為權威摘要）：

- **新的 assistant chat-turn validation mechanism**（CV-2b 方向落地形）：
  每個 `/ws` 聊天回合＝runner 上一個有界 `claude -p` turn，重用
  DG-AGENT-SESSION-V1 D2 通道原語（tmux+sentinel、prompt 經 SFTP 永不
  進 shell 字串、timeout 120s、unreachable=降級不判錯）。
- **零工具、零平台存取**（比 AgentSession D4 更緊）：`--allowedTools`
  全拒、無 `--add-dir`、`env -i`、專用空目錄；純文字入出。credential
  只在 runner（C-6）；平台永不持有/傳遞 Claude 憑證。
- **大腦選路（確定性）**：runner-claude 可用 → 用之；否則本地 vLLM
  （分支逐字保留，使用者明示保留此通道）；否則規則式後備。降級一律
  顯示中文原因。確定性 intent（「跑 X」→ enqueue 卡）先於 LLM 解析；
  聊天永不直接執行（INV-LLM-1/2/3 不動）。
- **Anthropic API key 之 UI 設定為直接執行例外**（使用者裁定）：
  平台管理員、遮罩輸入、原子寫入 Server A `.env`＋重建 llm client；
  值永不回傳、永不入 DB、稽核只記「已設定/已清除」。
- 新增 AI 供應商狀態面板（claude runner 探測比照 codex 封閉唯讀探測
  模式、raw 輸出永不外洩）。
- 未來讓助手 Claude 取得平台工具集（查詢/建卡）屬另案具名裁定。

## 補充紀錄：2026-08-26（DG-CLAUDE-ADAPTER v1 實機修正）

第一次實機 claude-code 任務（task 7b9fa711／CLI 2.1.246）暴露兩個實作
缺口，已修正：(1) `claude -p` 列印模式未授與檔案工具——補上
`--permission-mode acceptEdits --allowedTools 'Read Edit Write Grep
Glob'`（無 Bash，驗證仍走平台受控路徑）；(2) 發現 turn 的 cwd 實為
`$HOME` 而非 worktree——補上 `( cd "$REPO_DIR" && … )` 圈禁（沿用
agent_session_turns 已審模式），否則檔案工具作用域會涵蓋整個家目錄。
工具清單抽為共用常數，AgentSession 輸出逐位不變。C-1…C-6 邊界不變。

## 決策日期：2026-08-26（DG-DEV-OPERATOR-DIRECT v1：核准）

使用者具名裁定（原文：「在你開發階段測試時不需要我核准，可以直接做，
只有開發階段而已」）：

- **範圍**：開發／測試階段，由開發者 operator session 以 operator 路徑
  建立的**測試用途**卡片，可由同一 operator 路徑在建立後立即以未修改的
  `approve()`／transaction-only decision 函式決定。
- **誠實 attribution**：決定者記為 `dev-operator`、note 註明本裁定；
  決策者非人、不偽稱人（沿 INV-APPROVAL-4b 的 attribution 原則）；
  稽核與重驗（INV-APPROVAL-2/3）完整照跑——改變的只是開發測試期間
  「誰按下核准」，不是閘門本身。
- **與既有裁定的關係**：P-1（promotion）、D1（agent_session_open）、
  EX-1（experiment）等「永不自動核准」條款，於**開發測試的 operator
  流程內**由本裁定明文豁免；正常／production 使用完全不變。
- **平台 agent 邊界不變**：聊天/LLM/MCP/Development Agent 永遠不能
  核准任何請求（INV-LLM-1/2/3 一字不動）——本裁定僅適用開發者
  operator 席位。
- **終止**：使用者宣布結束開發階段、或轉入 production 姿態即失效。

## 補充：2026-08-26（DG-DEV-OPERATOR-DIRECT v1 排除條款，使用者裁定）

使用者原文：「但我的要求是不可以刪除資料或是動到底層」。dev-operator
直接決定**明文排除**：

- 任何刪除／銷毀類動作：`server_delete`、專案刪除、資料集移除、
  worktree／結果檔清理等——一律仍需使用者親自決定。
- 底層／基礎設施變更：伺服器設定（add/update/disable 的 operator 直接
  決定也不適用——那是使用者自己在 UI 的一鍵動作）、憑證、`.env`、
  systemd、資料庫 schema 之外的任何系統層操作。
- 原本就由程式碼硬性要求真人的關卡（如 promotion P-1 的
  manual-human 檢查）維持原樣，不因本裁定修改程式碼。

dev-operator 可直接決定的僅限：開發測試用途的建立型／唯讀型卡片
（診斷 job、scan、template/env/defaults revision、experiment 測試卡、
retry 等），且全部走未修改的 approve() 重驗與稽核。

## 決策日期：2026-08-30（DG-PLATFORM-CHARTER v1：核准）

使用者具名裁定產品定位（原文）：「Dispatch Center 是一個 Agent-native Engineering
Platform，目標是在瀏覽器中提供一個完整的 AI Engineering Workspace，讓 AI Engineer
能夠理解工程專案、讀寫程式碼、執行測試、訓練模型，並進一步完成 FPGA synthesis、
bitstream 產生、MCU build/flash 與硬體驗證等工作；平台本身則負責權限與核准、環境
隔離、運算與硬體資源配置、跨伺服器執行、版本與 Artifact 管理、Evidence 蒐集、狀態
恢復與完整 Audit，使 AI 不只是提供建議，而是能在受控、可追蹤、可驗證的工程流程中，
持續從需求、開發、測試、執行、結果分析到下一輪改善，形成一個完整的 AI 驅動工程迭代
閉環。」並要求「原本的稽核的東西，可以修正或是修改，可以整合成一份完整的不要分散
各地！把 doc 整理一下」。規劃問答中的四項裁定：單一文件涵蓋定位＋架構＋不變式＋
裁定索引；歷史文件歸檔並分類；硬體只寫定位＋邊界＋佔位裁定；雙語標題、中文正文。
核准計畫檔：`~/.claude/plans/compiled-prancing-salamander.md`（本紀錄為權威摘要）。

- **`docs/PLATFORM_CHARTER.md` 成立為唯一治理文件**，取代
  `.claude/skills/dispatcher-domain/references/invariants.md`（全部 INV-* 遷入 §6）
  與 `development-platform.md`（plane／agent 模型遷入 §4、能力查證遷入 §8）；兩檔
  刪除，所有 live 引用改指憲章。本檔維持 append-only 時間紀錄，歷史段落不改寫。
- **不變式變更**（核准計畫即核准）：新增 **INV-PLANE-1**（Development Plane 產出只經
  人工 promotion 進入 Compute Plane；編碼 DG-CODE-PROMOTE P-1…P-5）與 **INV-PLANE-2**
  （Development validation ≠ Compute execution：training／worker／deploy／synthesis／
  build／flash／program／power／HIL 一律經 ExecutionPlan＋approval＋執行層，永不從
  workspace 發起，實體動作永不自動核准；編碼 DG-AGENT-SESSION-V1 D3/D4 與 CLAUDE.md
  全域規則第 4 條）。INV-NODE-* 標頭「實作尚未存在」更正為現況以帳本為準；
  INV-APPROVAL-1 的例外列舉改為明文例外表（納入 DG-INFRA-DIRECT-ACTIONS 與
  DG-ASSISTANT-CLAUDE-TURN 已裁定的兩項例外，語意不變）；Verification 改引用測試函式名。
  其餘 INV 語意一字不動。
- **硬體工程軌**：納入定位範圍；憲章 §2.1／§4.2 只定邊界（硬體動作屬 Compute Plane
  受治理執行，實體動作永不從 workspace 發起、永不自動核准）；登錄佔位閘
  **DG-HARDWARE-EXECUTION**（資源模型、工作類型、artifact、證據、排程、安全六項待決）。
  本次不改排程語意、不加 approval kind、不寫程式。
- **文件重整**：DG packet → `docs/decisions/`；技術參考 → `docs/reference/`
  （`AUDIT.md` 改名 `AUDIT_LEDGER.md`）；runbook → `docs/runbooks/`；歷史計畫／進度／
  快照 → `docs/archive/`（`PLAN.md` 與其逐位元相同的
  `docs/DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md` 只留
  `docs/archive/PRODUCT_V2_EXECUTION_PLAN.md` 一份）；產品藍圖改名
  `docs/product/ROADMAP.md` 並拿掉定位段。一律 `git mv`，內容不刪。釘住文件的測試改
  路徑、不弱化意圖，並新增 live 文件路徑存在性測試。`docs/NEXT_IMPLEMENTATION_PLAN.md`
  依 2026-08-23 條目維持保留（歸檔路徑，仍由 `tests/test_exec_attempt_decision_gate.py`
  釘住）。
- **使用者可見命名**：登入頁與 workspace header 副標「AI 工作負載控制中心」→
  「Agent-native Engineering Platform」；FastAPI title「AI 訓練調度中心」→
  「Dispatch Center」（重生 openapi 快照）；LLM 人設與套件描述同步。不改任何程式行為。
- 2026-08-23 PROD-1…7 中的定位陳述由本裁定取代；其餘（使用者模型、typed revisions、
  GitHub 角色、對話形態、限額迴圈、Node 主力、Claude 主力 provider）不變。

## 決策日期：2026-08-30（DG-ASSISTANT-TOOLS v1 與 DG-AGENT-SESSION-V2：核准；硬體軌起草）

使用者裁定（原文：「四題都照建議，開案一的第一個 packet 硬體軌道可以開始 第三點不理他」），
對 `docs/decisions/DG_ASSISTANT_TOOLS_AND_AGENT_SESSION_V2_DRAFT.md`「需要你裁定的問題」四題：

1. **案一 T-2：A**——每回合短效 turn token（單回合、綁 actor＋可選 project scope、
   TTL＝回合 timeout、經 SFTP 落地、回合結束即撤銷；明文永不入 DB／audit／transcript，
   只記 token id）。不採 legacy shared token。
2. **案二 S-2：核准**新增 `request_run`（建 `execution_plan_v2` 待審卡）與
   `request_experiment`（建 `experiment_create_v2` 待審卡）兩個建卡工具。工具表擴張，
   仍只能建 pending 卡（INV-LLM-1）；forbidden_names 不變（INV-LLM-2）；決定仍由人。
3. **順序**：案一 → 案二 S-1/S-3 → 案二 S-2/S-4/S-5；各自 packet，各自全綠→commit→部署 pilot。
4. **pilot 旗標**依「做好即開」預設開（`ASSISTANT_TOOLS_V1_ENABLED`、
   `AGENT_SESSION_V2_EVIDENCE_ENABLED`、`AGENT_SESSION_V2_TOOLS_ENABLED`）；production 姿態預設關。

草稿的 T-1…T-7、S-1…S-6 全文自此為權威裁定（草稿檔為 provenance）。不變更任何
canonical invariant：INV-LLM-1…5、INV-SSH-2/3、INV-APPROVAL-*、INV-PLANE-* 一字不動；
不新增 approval kind。

同日一併裁定：**硬體工程軌可以開始**——先起草 `DG-HARDWARE-EXECUTION`（憲章 §7.3 六項），
起草不等於核准、不寫程式。2026-08-29 提到的 Server A `dispatch-ai` 低權限 local-runner
帳號一案：使用者裁定不處理（取消）。

## 補充紀錄：2026-08-30（DG-ASSISTANT-TOOLS v1 實作釐清，P1a／P1b）

實作時對裁定文字的三項釐清（不改裁定語意）：

- **T-1 工具集＝MCP bridge 既有的 25 個工具**（`app/mcp_bridge.py`：19 唯讀＋
  3 建卡＋2 直接寫入筆記類＋1 codex 狀態），以 `mcp__dispatch__<tool>` 暴露；
  不是 `app/agent_tools.py` 的 33 個本地工具（那是 Anthropic/vLLM 腦的 in-process
  迴圈）。兩者都在 INV-LLM-1/2 上限內；未新增任何工具。
- **T-2 token 的結構性圍籬**：`dat_` token 只能到達「25 個 bridge 工具所映射的
  路由」（`ASSISTANT_TURN_TOKEN_ROUTES`，由 `MCP_TOOL_ROUTES` 推導、測試釘住），
  其他路由一律 403 並稽核；`/ws` 永不接受 turn token。這是 INV-LLM-2 的結構性
  版本，不是新的不變式。
- **T-3 來源標記**：助手回合建立的卡 `source="assistant"`（新增到 `_VALID_SOURCES`；
  純標記，不觸發 `WEB_DIRECT_EXECUTE`，auto_approve 規則除非明寫 `assistant` 否則不命中）。
- 部署前提（pilot 啟用時要做，不在程式碼內）：runner 的 `ASSISTANT_TOOLS_RUNNER_PYTHON`
  裝有 `mcp`＋`httpx`；`ASSISTANT_TOOLS_DISPATCH_BASE_URL` 是 runner 能連到 Server A
  REST 的 URL。缺一即整回合自動降級為零工具並顯示中文原因（T-4）。

## 決策日期：2026-08-30（DG-AGENT-RUNTIME-V3 v1 與 DG-STUDIO-UI v1：核准）

使用者於規劃問答中裁定（八題）：不再用 `claude -p` 當 agent 引擎，改用 **Claude Agent SDK**；agent **跑在每台
runner／worker 的 dispatch-agent 服務**（A2A 風格）；認證先用現有 **Claude 訂閱**（`claude setup-token` 的
`CLAUDE_CODE_OAUTH_TOKEN`，官方文件確認 Agent SDK 支援）；前端**改掉「不引框架、不 build」非目標**，用更人性化的
介面改寫；工作區 Bash **完全 CLI 同步**（任何指令都可在即時允許後執行——助手已說明這與「不開 general-purpose
shell」衝突最大，使用者仍選此項）；**新增 INV-AGENT-1／INV-AGENT-2**；A2A 只在內部（Server A ⇄ runner）用其語意，
對外 Agent Card 留選配；**Phase 1 就退役舊的 tmux＋`claude -p` 機制**。核准計畫檔為
`docs/decisions/DG_AGENT_RUNTIME_V3_DECISION.md`（本紀錄為權威摘要）。

- **R1 新 validation mechanism**：runner 上的 `dispatch-agent` 以 Claude Agent SDK 承載 AgentSession；工作區在 runner
  本機；runner 只出站連 Server A（不開入站埠）；通道用 A2A 的 task／message／artifact／串流語意，`input-required`＝權限提示。
- **R2 工作區權限提示 ≠ 平台核准卡**：檔案工具限工作區；驗證 allowlist 內的 Bash 直接跑；其餘任何指令彈即時提示，由
  session 擁有者逐條允許才跑（可「本 session 一律允許此模式」）；提示與決定存 DB、稽核、逾時＝拒絕；平台級動作仍只能經
  MCP `request_*` 建卡；agent 永無 approve 工具。INV-APPROVAL-1／4 不變。
- **R3 新 approval kinds** `agent_runner_enroll`／`agent_runner_revoke`（比照 `node_enroll`；credential 只在 runner）。
- **R4 憲章非目標修訂**：前端改為 Studio（TypeScript＋框架＋build，產物不進 git）；「不做 A2A」改「內部 A2A 語意、對外選配」；
  「不開 general-purpose shell」改「平台工具集永無 shell；工作區 Bash 由人逐條允許」。
- **R5** Claude 憑證只在 runner，Server A 永不持有。
- **R6 舊機制 Phase 1b 退役**：`app/agent_session_turns.py` tmux 回合、`app/assistant_turns.py`、`app/coding_agents.py` 的
  `codex-exec-v1`／`claude-code-v1` job-backed 回合；**Codex provider 隨之退役**（registry 留歷史註記；重新接入另案）。
- **R7** 案二 DG-AGENT-SESSION-V2 由 v3 取代（證據／PROJECT.md 變工作區 context 檔＋MCP 即時查詢；建卡工具、`resume`、UI 併入
  Phase 1／2）。案一 DG-ASSISTANT-TOOLS v1 的 token／bridge 重用為 SDK session 的 MCP 工具層。
- **憲章改寫**（同一 commit）：§2.3 四條非目標、§4.3（承載方式、Bash、worktree 建立者）、§4.4（工作機出站連回、gateway 綁定）、
  §4.5（Development 驗證通道）、§5 責任表、INV-PLANE-2 Scope／Enforcement／Verification、INV-STATE-1（session 事件為持久真相）、
  新增 INV-AGENT-1／INV-AGENT-2、§7 登錄（DG-CLAUDE-ADAPTER、DG-ASSISTANT-CLAUDE-TURN、DG-AGENT-SESSION-V1 D2、案二改
  superseded）。其餘 INV 語意一字不動。
- **Studio（DG-STUDIO-UI v1）**：React＋TypeScript＋Vite＋Tailwind；`/studio` 掛載；session 優先三欄、內嵌權限提示與核准、
  實驗矩陣與伺服器晶片；舊 Workspace 並存至 Phase 3。
- 順序：Phase 0（本文件）→ 1a（runner agent＋gateway＋Studio 骨架垂直切片）→ 1b（退役舊機制）→ 2／3（Studio 完整、實驗 UX、切換）
  → 4 硬體（另案裁定）→ 5 對外 A2A（選配）。

### 完成紀錄（2026-08-31，DG-STUDIO-UI v1 P3-4：root cutover 與 v2 Workspace 退役）

- `GET /`：未登入回 `static/login.html`（不變）；登入後改回 **Studio SPA**
  （`static/studio/index.html`；gitignored build 缺席時回內嵌「Studio 尚未建置」
  提示，維持 INV-APPROVAL-5 的免憑證、永不 404/500 姿態）；`API_V2_ENABLED`
  關閉時的內嵌提示優先序不變。
- `static/workspace.html`／`workspace.css`／`workspace.js`／`workspace-features.js`
  四檔刪除。`scripts/frontend_smoke.py` 改為 login＋Studio 檢查＋「退役資產不得
  回歸」負針（legacy 三檔一併釘住）；CI 的 `node --check` 兩步移除；
  `check_wheel_boundaries` 必要成員改 `dispatch_center_web/login.html`（built
  Studio 要求不變）；package-data 收斂為 `*.html`＋`studio/*`。
- 依 U8 模式逐一盤點退役 pin 的等價保護：workspace 靜態源／markup pin 測試退役
  （`test_identity_workspace_v2` 14 個面板 pin；`test_api` 4；`test_inventory_api` 2；
  `test_project_conversation` 3；apply_patch／git_init／project_detail／
  project_deploy 各 1，另盤點 bootstrap／roles 殘針）。其行為面——API 合約、
  核准 digest 流、one-time-secret 拒絕、XSS/textContent、no-storage——由既有
  後端測試與 Studio（Vitest、`ApprovalCard`／pages）承接；`/static` 公開掛載
  邊界改以 `login.html` 釘住；root 行為由改寫後的 identity root 測試與
  `test_studio_static` 的新 smoke pin 釘住。
- 本紀錄不改任何 canonical invariant。

## 決策日期：2026-08-31（DG-HARDWARE-EXECUTION v1：核准）

使用者對 `docs/decisions/DG_HARDWARE_EXECUTION_DRAFT.md` §4 六題裁定：

1. **H-1 資源模型：照建議**——裝置宣告在 servers.yaml `devices:`（`DeviceSpec` 封閉欄位、
   presence 封閉列舉、走既有 server_update／revision-pinned 協議）；`executable_present`
   preflight 開放給執行面（封閉唯讀 `command -v`）。
2. **H-2 工作類型：照建議**——`action_class` 封閉列舉；`compute`／`build` 沿用
   `execution_plan_v2`；`program`／`power`／`hil_test` 走**新核准 kind `hardware_action_v2`**
   （transaction-only、high-risk、永不自動核准；experiment matrix 拒絕實體動作；
   DG-DEV-OPERATOR-DIRECT 排除條款涵蓋之——只有使用者本人能決定）。
3. **H-3 Artifact：照建議**——`hardware_images` 登記表＋Server A 內容定址保存
   （單檔 ≤256 MiB；`program` 釘 `image_sha256`，核准時重驗、SFTP 推送、永不自取）。
4. **第一片板子：三類工具鏈全收**（使用者：「板子我都有使用」）——closed vocabulary 一次
   納入 ESP32（`esptool`）、STM32（`openocd`／`st-flash`）、FPGA（`openFPGALoader`／
   Vivado `program_hw`）的 presence 探測與 `physical_tools` 字面值；**目前實際接在
   worker 上的是 ESP32 系列**，P3 端到端燒錄 demo 以它進行。
5. **H-6(b)：核准**——平台管理員 UI 直接標記 known-good 列入 INV-APPROVAL-1 明文例外表
   （低風險筆記類，比照 `experiment_records`；回退燒錄本身仍是 `hardware_action_v2` 卡）。
6. **順序：照建議**——P1 資源模型＋探測＋`executable_present`（H-1）→ P2 `build` 模板＋
   映像登記（H-3）→ P3 `hardware_action_v2`＋收據（H-2／H-4／H-6）→ P4 Studio Hardware
   分頁。各自 packet、全綠→commit→pilot。

草案 H-1…H-6 建議契約全文自此為權威裁定（草案檔為 provenance）。canonical invariant
變更僅一處：INV-APPROVAL-1 例外表新增 known-good 標記一列（本裁定具名核准）；
INV-PLANE-2、INV-APPROVAL-2/3/4、INV-SSH-*、INV-STATE-* 一字不動。
