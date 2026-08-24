# Dispatch Center 完整平台計畫（第二次獨立審視版）

**文件類型：獨立第二次審視（audit）+ 決策閘 packet + 分階段 roadmap**
**日期：2026-08-23**
**關係：本文件不取代
`docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`
（最終完成品定義）；它記錄第二次獨立審視的結論、待裁定的決策閘、
以及從 personal pilot 到最終品的階段路徑。**

> 計畫不是實作，永不授權 invariant 變更。真相順序：canonical
> invariants + `docs/DECISIONS.md` → code/tests →
> `docs/CAPABILITY_LEDGER.md` → 產品計畫。文中狀態標記：
> **[已裁定]**（DECISIONS.md 有紀錄）、**[待裁定]**（packet 已備、
> 未核准）、**[提案]**（本審視的建議，尚無 packet）。

---

# Part A — 審視總結論

**最終品方向 coherent。** 兩 plane 模型、promotion 唯一交會點、
approval 骨幹、provider-neutral agent 抽象在對抗式審讀下成立；
invariants、決策紀錄、code 三者高度一致。主要風險不是設計缺陷，而是：

1. **Activation debt**：repo 內實際上有兩個產品——default-on 的
   legacy 產品（實際在跑）與 default-off、無部署證據、UI 幾乎未覆蓋
   的 Product v2 產品（最終品定義所在）。約 30 個 feature flags 全關。
2. **Metrics 地基缺失**：results 路徑只有 rsync 收集 + test exit
   code，**不存在任何 metrics 解析**；原計畫書 §15 的「部分已實作」
   高估現況。M3 dashboard 與 M4 優化迴圈都站在這塊缺失的地基上。
3. 若干接縫尚未定義（對話×workspace、Experiment 核准粒度、
   promotion 唯一入口的範圍）。

# Part B — 獨立最終品定義（與原計畫書的三處分歧）

維持原計畫書的定位與 PROD-1…PROD-7 裁定，另提出三處修正
**[提案；待 DG-PRODUCT-PLAN-CORRECTIONS 裁定]**：

1. **Auto provider selection 降級**：完成品要求 Manual +
   per-Project 預設 provider；Auto 引擎為可選延伸，非 Definition of
   Done。「selection 永不是權限提升／永不 silent fallback」約束保留。
2. **Metrics 是一級產品契約**（`metrics-v1`）：workload 寫 bounded
   typed `metrics.json`，job-finish 收集後 Server A 解析入庫；
   missing = unknown；invalid 永不影響任務終態（INV-SSH-6 不變）。
3. **Product Workspace（v2 UI）是最終唯一主介面**；legacy UI 為相容
   過渡產物，退場條件沿用 API v2 cutover 裁定。

# Part C — 獨立目標架構

- **控制面**：既有 FastAPI 單體 + 已實作的 v2 spine（API v2、RBAC v2、
  typed immutable revisions、dataset governance、ExecutionPlan v2、
  Product Run projection）activation 後即為目標架構，無需新發明。
- **Development Plane**：
  - 對話層 **[已裁定方向]**：`AIConversation` 為 Server A domain
    object（SQLite），智能走既有 LLM tool-loop 邊界（Anthropic API +
    唯讀/request-approval 工具；INV-LLM-1/2/3 不變，血本封頂維持
    「一張待審卡」）。對話提案 controlled task，本身零執行權。
    不採 runner 上常駐互動 session（那需要新 validation mechanism
    裁定與新的 session reconciliation invariants）。
  - 任務層：既有 engineering task 生命週期承載全部改碼；provider 為
    Job-backed adapter（`codex-exec-v1` 現行；`claude-code-v1` 依
    DG-CLAUDE-ADAPTER packet，headless 一次性 turn、同 runner 慣例、
    default-off）。
  - Promotion：不變（bundle verify → 不可執行 ProjectVersion →
    本地 hub；永不自動核准、不推 GitHub）。
- **Compute Plane**：ExecutionPlan v2 為唯一 Run 契約。其上
  **[已裁定方向]** Experiment 採一 matrix 一 approval：未來
  `experiment_create_v2` kind，不可變 payload 列全數 N 個 resolved
  run specs + Guard 上限，一次決定原子 materialize N 個 plans/Jobs。
  PROD-5 限額迴圈之後以 policy-scoped 自動決策（INV-APPROVAL-4b
  類比）組合於其上，另需具名裁定。
- **執行後端**：SSH sentinel 為現行預設；attempt-driven 路徑與
  Node Agent 各依既有證據閘（WP-2D 重跑、DG-NODE-CANARY）晉級。
- **Results**：`metrics-v1` 契約（見 Part E Gate 3）。
- **UI**：Product Workspace 長成計畫書 §5 導航，只呼叫 v2 API。

# Part D — 架構關切（audit findings）

- **C1 Activation debt**（中心問題）：見 Part A；由 Stage 0/Stage 3
  路徑處理。
- **C2 Metrics 缺失**：唯一實質性的現況誤述；由 DG-METRICS-CONTRACT
  處理。
- **C3 Workspace × 對話接縫**：現行 per-task 單回合 worktree vs
  計畫書 RemoteWorkspace 跨任務存續；`engineering_command` 決策
  現況「記錄但無法送回 live session」。DG-CONVERSATION-V1 須明確
  裁定 task-chaining 語意。
- **C4 Provider 抽象樣本數 = 1**：介面誠實 fail-closed，但形狀繞
  Codex 而生；`claude-code-v1` slice 同時是抽象層是否真 neutral 的
  最便宜檢驗。
- **C5 Promotion 唯一入口**：只有 terminal engineering task 能
  promote；人工/外部 commit 無路徑成為 ProjectVersion。需明文裁定
  「這是規則」或「這是缺口」。
- **C6 一 plan 一 Job vs matrix**：`experiment_create_v2` 必須寫成
  自帶契約的新 kind，不是放寬既有 materialization 上界。
- **C7 單體壓力**：`app/main.py` ~10.9k 行 / ~386 routes、
  `app/db.py` ~31.8k 行、12 個空 router stub；vanilla-JS 無框架
  約束將被 M2 串流對話 UI 真正考驗，值得一次誠實重審。
- **C8 Onboarding 分岔**：legacy import 產出的專案沒有 v2 typed
  物件；「legacy 專案 graduation」是缺失的收斂路徑。
- **C9 已接受風險保持可見**：unsandboxed finalization（2026-07-25
  長期接受）、`known_hosts=None`、黑名單防呆不防惡意。

# Part E — 決策閘現況

| Gate | 狀態 | 摘要 |
|---|---|---|
| **DG-PERSONAL-PILOT-v1（D1–D4）** | **[已裁定]** 2026-08-23 | Single-user legacy-first pilot；實作計畫 `docs/product/PERSONAL_PILOT_PLAN.md` |
| **DG-CLAUDE-ADAPTER v1** | **[已裁定]** 2026-08-24 | `claude-code-v1` bounded implementation 已核准並實作（default-off，`CLAUDE_CODE_AGENT_V1=false`）；packet：`docs/DG_CLAUDE_ADAPTER_DECISION.md` |
| **DG-METRICS-CONTRACT v1** | **[已裁定]** 2026-08-24 — A 核准 | `metrics-v1` 檔案契約（packet：`docs/DG_METRICS_CONTRACT_DECISION.md`；本文件 §E-3 為其草案骨架）；實作 default-off |
| **DG-PRODUCT-PLAN-CORRECTIONS v1** | **[已裁定]** 2026-08-24 | Part B 三處修正記入 DECISIONS.md，E1–E8 已套用至原計畫書 |
| **DG-PILOT-ACTIVATION（v2 全鏈啟用）** | **[提案；已被 pilot 重定範圍]** | 原「117 pilot 啟用 v2 全鏈」建議由 DG-PERSONAL-PILOT-v1 的 legacy-first 取代；v2 activation 成為 Stage 3 的獨立未來閘 |
| DG-CONVERSATION-V1 | **[已裁定]** 2026-08-24 | CV-2a 已實作；後由 DG-AGENT-SESSION-V1（同日）承接 web 開發體驗（AgentSession V1 已實作 default-off） |
| DG-EXPERIMENT-V1 / DG-OPTIMIZATION-QUOTA / DG-GITHUB-PUBLISH / DG-NODE-CANARY | **[未起草／既有 blocked]** | 各自於對應 Stage 前起草 |

## E-3 DG-METRICS-CONTRACT v1 草案骨架（延後，不阻塞 pilot）

- 契約：`results/{job_id}/metrics.json`，單一扁平 JSON object；
  ≤64 KiB、≤256 keys、key 字元集封閉（`[a-z0-9_.]`，1–128 字）；
  值為 JSON integer / bool / bounded string / canonical decimal
  string（沿用 Run Template v2 數字規則；拒絕 JSON float，與既有
  `canonical_json()` 一致）。
- 解析點：既有 job-finish hook 收集後、Server A 本地純函式解析；
  **零新遠端指令**（INV-SSH-4 不動）。
- 儲存：additive migration `run_metrics` 表 + collection evidence 上
  的 `metrics_status`（collected/missing/invalid/oversize），永不寫
  在 `jobs` 上。
- 語意（承 `result-analysis.md`）：missing = unknown；invalid 有
  reason；**metrics 永不影響任務狀態機／reconciliation／scheduling**
  ——哨兵 exit_code 仍是唯一終態來源（INV-SSH-6 明文不動）。
- 已否決替代案：log scraping（違反 grounded-evidence 規則）、
  worker push endpoint（顛倒單向信任邊界）。

## E-4 原計畫書修訂清單（E1–E8；待 DG-PRODUCT-PLAN-CORRECTIONS 裁定後套用）

對 `docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`：

- **E1** 檔頭：加註 2026-08-23 v2 修訂行（記錄四項修正）。
- **E2** §5：Product Workspace 定為最終唯一主介面；legacy UI 過渡
  產物、退場沿用 API v2 cutover 條件；現況註記 Workspace 尚未涵蓋
  engineering task 介面。
- **E3** §11.1：Selection 改為「Manual 為完成品要求；Auto 為可選
  延伸 [延後]」，保留全部安全約束。
- **E4** §15 第一段：修正為「rsync 收集與 v2 artifact metadata 存在；
  **metrics 解析目前完全不存在**」，並新增 metrics-v1 一級契約段
  [未來目標：gated on DG-METRICS-CONTRACT]。
- **E5** §15 Dashboard 段：加註前置於 DG-METRICS-CONTRACT。
- **E6** §18：新增「Auto provider selection 引擎（延後為可選）」。
- **E7** §19：插入 M0 列（現為 Personal Pilot，Stage 0）；M3 列加註
  metrics 前置。
- **E8** §20 DoD：加入 metrics-v1 契約與單一 Workspace 介面兩句。

# Part F — 分階段 roadmap

```text
Stage 0  Personal Pilot（已裁定，進行中）
         legacy-first：三個 v1 feature flags + Codex runner
         configuration + server_update / hub-sync / 首次 promotion
         activation steps + N1 Run Request UI + N2 Results 唯讀檢視
         → 單人全程瀏覽器跑通 import → agent → promote → plan_run
           → Run → results
         驗收：docs/product/PERSONAL_PILOT_PLAN.md §6 T3

Stage 1  Claude adapter（待 DG-CLAUDE-ADAPTER 裁定）
         claude-code-v1 bounded implementation（default-off）
         → PROD-7 主力 provider 落地；同時檢驗 provider 抽象

Stage 2  Metrics 契約（待 DG-METRICS-CONTRACT 裁定）
         metrics-v1 解析 + 儲存 + Run detail/compare 投影
         → M3/M4 的資料地基

Stage 3  Product v2 activation（獨立未來閘）
         OIDC/RBAC/enforce + v2 spine 逐鏈啟用 + Workspace 補齊
         engineering 介面；≥2 HUMAN Owner/Reviewer 治理規則在此
         階段才生效（單人部署屆時需另行裁定或補足人數）
         → pilot 架構收斂到最終架構

Stage 4  M2 對話（DG-CONVERSATION-V1）→ M3 Experiment
         （DG-EXPERIMENT-V1）→ M4 限額優化迴圈
         （DG-OPTIMIZATION-QUOTA）；GitHub 紀錄同步
         （DG-GITHUB-PUBLISH）與 Node Agent（DG-NODE-CANARY）
         依各自證據閘平行推進
```

每個 Stage 開工前的規則不變：具名裁定 → bounded packet →
sonnet-coder 實作 → Fable 對照驗收；`implemented` 永不被推導為
`enabled`/`deployed`/`canary-proven`/`production-ready`。
