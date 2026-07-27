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
