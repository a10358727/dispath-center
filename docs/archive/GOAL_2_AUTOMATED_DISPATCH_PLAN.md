# Goal 2 — 自動資源調度最小閉環（已核准的歷史實作計畫）

> archived: 2026-08-30 · superseded_by: `docs/PLATFORM_CHARTER.md`（定位／架構／不變式）、`docs/CAPABILITY_LEDGER.md`（能力現況）、`docs/DECISIONS.md`（裁定）· 本檔為歷史證據，不是現況。

> Status: 已核准的實作計畫。2026-07-18 依使用者「AI 資源調度平台」的產品
> 目標與當日架構缺口分析撰寫；同日使用者裁定 **DG-1 核准、DG-2 核准**
> （權威紀錄見 `docs/DECISIONS.md` 2026-07-18 條目，含附帶條件）。Slice 5
> 所需的 `INV-APPROVAL-4b` 已寫入 canonical invariants。本文件的後續能力
> 狀態由 `docs/CAPABILITY_LEDGER.md` 記錄。

## 1. 產品目標與現狀對照

使用者的目標：**找出沒在使用的伺服器 → 自動把專案複製過去執行 → 平台內用
Codex 類 agent 改程式 → 可對空伺服器下指令開通**。

| 目標 | 現狀（2026-07-18 稽核，見 `docs/CURRENT_STATE.md`） | 差距 |
|---|---|---|
| 找閒置伺服器 | `app/monitor.py` 每 ~20s 探測 GPU util/loadavg/disk，`is_idle()` fail-closed 判定 | 讀數只在記憶體，無歷史；無 RAM；單 job/單機 |
| 複製專案過去跑 | `project_deploy`（bundle→rsync→clone）與 `setup` job 都存在且穩定 | 全程逐件人工核准；scheduler 刻意不自動合成 sync/setup/deploy |
| Codex 改程式 | Engineering Task 子系統完整（核准制、path policy、secret 掃描） | 單一 Runner、單回合；app-server 互動式尚未接線（D1 裁定範圍外） |
| 空伺服器開通 | `onboard-worker.sh` 只配金鑰＋唯讀檢查 | 無任何 provisioning；Node Agent 未開始且 `INV-SSH-1` 明文禁止常駐 agent |

本計畫聚焦第一、二項的閉環（閒置偵測 → 自動放置提案 → 一次核准後自動執
行）。第三項（多 Runner／互動式 Codex）與第四項（provisioning／Node Agent）
牽涉獨立的不變量修訂與硬體前提，列入 §7 後續工作，不在本計畫範圍。

## 2. 固定邊界（全計畫適用）

- 自動核准白名單維持**恰好 `enqueue|stop`**；本計畫新增的任何 approval kind
  永不加入該白名單。Slice 5 的「政策內自動執行」走獨立的、明確裁定後才存在
  的機制，不是擴大 `maybe_auto_approve()`。
- 所有 schema 變更 additive；不改寫、不刪除既有列；歷史事實缺失標 unknown/
  legacy，不腦補。
- DB-before-side-effect、sentinel reconciliation、unreachable≠failed、
  SFTP 腳本路徑（不做 shell interpolation）全部維持。
- 開發與測試不接觸真實 worker、真實憑證、runtime `jobqueue.db`／
  `audit.jsonl`／`servers.yaml`；一律 FakeSSH／暫存 DB。
- `AUTHORIZATION_MODE` 維持 `off|shadow`；本計畫不含授權強制。
- 每個切片獨立可回滾：關掉該切片的 feature flag 後，前一版程式可直接跑在
  additive schema 上。

## 3. 需要使用者明確裁定的決策點（動工前）

**DG-1 —「政策驅動的自動放置提案」是否核准（Slice 4 前提）。**
現行鐵律（`app/datasets.py` `build_dispatch_plan` docstring）：自動模式不合成
sync/setup/deploy 工作。Slice 4 提議：scheduler 依「已核准的 dispatch policy」
產生**pending approval**（提案，不執行）。這不改變「執行前必有人批」，但改變
「系統不主動提案」的現狀。選項：核准提案模式／維持純手動。

**DG-2 —「一次核准、範圍內自動執行」是否核准（Slice 5 前提）。**
Slice 5 提議：使用者核准一個明確、有界的 dispatch policy 之後，該 policy 精
確範圍內（指定專案、指定伺服器集合、指定指令模板、有效期）的後續放置由系統
自動核准並執行，逐件寫入稽核。這實質上是新的自動核准通道，必須修訂
`.claude/skills/dispatcher-domain/references/invariants.md` 中對應不變量
（INV-APPROVAL-4 相鄰語意）才能動工。選項：核准（附帶 policy 有界性條件）／
只到 Slice 4（提案模式）為止。

沉默或部分回覆不視為核准（比照 decision gate 慣例）。

## 4. 實作切片

### Slice 1 — 容量觀測持久化（無排程行為變更）

**目標**：monitor 的每次探測結果落地 SQLite，成為可查詢的歷史證據。

- Additive 表 `server_observations`：`id, server_name, observed_at, online,
  gpu_count, gpu_util_max, gpu_mem_used_mb, gpu_mem_total_mb, load1,
  disk_avail_bytes, probe_ok`。索引 `(server_name, observed_at)`。
- monitor tick 寫入（best-effort：寫入失敗不影響現有 in-memory
  `server_states` 與排程；比照 audit 失敗不腐蝕 job state 的既有原則）。
- 保留策略：每台保留最近 N 天（config `SERVER_OBSERVATION_RETENTION_DAYS`，
  預設 14），由 monitor 迴圈機會性清理過期列。
- 探測項目**加收 RAM**：probe 指令加 `free -b` 一行，解析 total/available。
  純函式 `parse_free_output()` 進 `app/monitor.py`，與既有 parser 同型。
- 唯讀 API `GET /servers/{name}/observations?hours=24`（分頁上限），UI 暫不做
  圖表（後續切片）。
- **不改** `is_idle()`／`pick_job()` 任何行為。

測試：parser 純函式、寫入/清理/失敗容錯、API shape、monitor tick 在 DB 唯讀
故障下照常更新 in-memory 狀態。回滾：停寫即可，表保留。

### Slice 2 — 確定性閒置摘要服務（唯讀）

**目標**：把「哪台機器有多閒」變成可解釋、可查詢的結論。

- 純函式模組 `app/capacity.py`：輸入某台伺服器一段觀測列，輸出
  `IdleSummary`（樣本數、線上比例、GPU util p50/p95、load p50/p95、
  「連續閒置時長」、資料新鮮度）。缺樣本輸出 unknown，不推測。
- API `GET /servers/idle-summary`（全部伺服器一覽）＋專案頁/基礎設施頁的
  唯讀呈現（沿用現有前端模式，不加輪詢負擔——併入既有 5 秒 refresh 的
  独立 surface，失敗只標記該 surface）。
- **不改**排程決策；這是給人看與給 Slice 4 引用的證據層。

測試：摘要純函式表格測試（含缺值/斷線/單樣本）、API shape。回滾：移除唯讀
路由即可。

### Slice 3 — Dispatch Policy 物件（有物件、無運行時效果）

**目標**：把「什麼專案可以自動放到哪些機器」寫成不可變、核准制的政策物件。
完全比照 Run Profile v1 的模式（additive 不可變 revision＋三個 kind＋rollback
flag），該模式已於 2026-07-17 驗證。

- Additive 表 `dispatch_policies`（不可變 revision）：`id, project_id, name,
  revision, status(approved|archived), allowed_servers(JSON), require_tag,
  run_profile_id(可選，引用既有 Run Profile revision), dataset_required(bool),
  max_concurrent_placements(int, 預設 1), valid_until, approval_id,
  created_by_actor_id, created_at`。
- 新 approval kinds：`dispatch_policy_create` / `_update` / `_archive`；
  request-time 驗證＋approve-time revalidation（含 `based_on_revision` 併發
  防護，直接沿用 run_profile 分支的寫法）。
- Rollback flag `DISPATCH_POLICY_V1_ENABLED=false` 預設關閉；關閉時路由 404、
  approve fail-closed（同 Run Profile 模式）。
- 授權目錄補 route→action 映射；coverage 測試計數更新。
- **本切片政策沒有任何運行時效果**——scheduler 完全不讀它。

測試：比照 `tests/test_run_profiles.py` 全套（生命週期、併發 revalidation、
auto-approve 排除、旗標隱藏、API round-trip）。回滾：關旗標。

### Slice 4 — 政策驅動的放置提案（需 DG-1 核准）

**目標**：scheduler 發現「有 approved policy 的專案 × 符合條件的閒置伺服器」
時，自動**建立 pending approval**，人批了才動。

- 新 approval kind `auto_placement`（不進自動核准白名單）。payload 綁定：
  policy id+revision、目標伺服器、依 policy 展開的完整 job 鏈（setup/sync/
  enqueue 各自的 command 由既有 `build_setup_script`/`build_sync_script`/
  Run Profile 確定性產生並記 SHA-256）。
- scheduler tick 內新增唯讀評估步驟：對每個 approved+未過期 policy，找
  `is_idle()` 且符合 allowed_servers/require_tag/dataset 條件的伺服器；
  已有同 policy 同 server 的 pending/近期 approval 則不重複提案（冪等鍵）。
  提案頻率上限（config，預設每 policy 每小時 1 次）防洪。
- 核准時 revalidation：policy 仍 approved 同 revision、伺服器仍 enabled、
  command SHA 一致，然後走**既有** setup/sync/enqueue 建 job 路徑，不新建
  執行通道。
- UI：核准卡完整揭露政策、目標機、指令摘要與 digest。

測試：提案產生/不重複/頻率上限的純函式與 scheduler tick 測試（FakeSSH）、
核准 revalidation 拒絕過期政策、job 鏈與手動路徑位元組級一致。回滾：關
`DISPATCH_POLICY_V1_ENABLED` 或 archive 政策，pending 提案可整批 reject。

### Slice 5 — 政策範圍內自動執行（需 DG-2 核准＋不變量修訂）

**目標**：對明確有界的政策，`auto_placement` 提案在政策精確範圍內自動核准。

- 前提：使用者明確修訂不變量，定義「policy-scoped auto-decision」為獨立機
  制（不是把 kind 加進 `enqueue|stop` 白名單）。決策紀錄寫入
  `docs/DECISIONS.md`。
- 實作：`auto_placement` 的 decision path 增加
  `decision_mechanism="policy-{id}-r{revision}"`；逐件稽核；政策
  `max_concurrent_placements`、`valid_until`、伺服器集合是硬邊界，超界一律
  留 pending。政策 archive 立即停止自動核准。
- 全域煞車：config `AUTO_PLACEMENT_KILL_SWITCH`（預設 on=停用），操作者一鍵
  回到 Slice 4 提案模式。

測試：範圍內自動/超界留 pending/archive 即停/kill switch 即停/稽核完整性。
回滾：kill switch → 提案模式；不需 schema 變更。

## 5. 驗證與 release gate（每切片）

比照既有慣例：focused 新測試 → 相關子系統（scheduler/monitor/approvals/
authorization coverage）→ 全套 `pytest -q` → static invariant gate →
`git diff --check`。Slice 1–2 另須驗證 monitor 迴圈在 DB 故障注入下不影響
現有排程（state-reconciliation 邊界）。不接觸任何真實 worker。

## 6. 明確不在本計畫範圍

- D2 沙箱/資源強制（硬體前提未解除）；D1/D6 正式啟用；
  `engineering_task_finalize`/`_promote`/`_pr`。
- 多 Codex Runner、互動式 app-server 接線。
- 空伺服器 provisioning 與 Node Agent（`INV-SSH-1` 修訂是獨立決策，建議在
  本計畫閉環穩定後另立 Goal 3）。
- GPU slot/搶佔/遷移/配額排程（`gpus_needed` 欄位存在不構成實作理由，
  CLAUDE.md 明文）。
- 授權強制（`off|shadow` 不變）。

## 7. 建議順序與里程碑

1. Slice 1＋2（純 additive、零排程風險）→ 立即可做，不需 DG 裁定。
2. Slice 3（模式已驗證）→ 立即可做，不需 DG 裁定。
3. DG-1 裁定 → Slice 4。
4. Slice 4 實際運行一段時間、提案品質被人工核准驗證後 → DG-2 裁定 → Slice 5。

里程碑驗收（對應使用者目標）：平台能自動指出「B 機已閒置 6 小時」（S1–2），
操作者建立一次政策（S3），之後系統自動提案（S4）或在範圍內自動把專案部署到
B 機開跑（S5），全程可稽核、可一鍵回滾。
