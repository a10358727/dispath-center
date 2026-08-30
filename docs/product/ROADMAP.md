# Dispatch Center Roadmap｜產品路線圖

**文件類型：產品路線圖／最終完成品形狀（方向，不是實作真相）**
**修訂：2026-08-23（依六項產品釐清裁定改版；原 2026-08 版全文重寫）**
**修訂：2026-08-24（DG-PRODUCT-PLAN-CORRECTIONS v1：Manual selection 為完成品要求、
metrics-v1 一級契約、Product Workspace 唯一主介面；插入 M0 Personal Pilot）**
**修訂：2026-08-25（狀態標記更新；能力現況一律以 `docs/CAPABILITY_LEDGER.md` 與 code/tests 為準）**
**修訂：2026-08-30（DG-PLATFORM-CHARTER v1：改名 ROADMAP；定位、兩平面模型與
Development Agent 邊界移入 `docs/PLATFORM_CHARTER.md`；新增硬體工程軌 M5）**
**使用者模型：小團隊、單人審核**
**核心 AI：Development Agent（主力 Claude／Claude Code；provider 現況以 `app/coding_agents.py` registry 為準）**
**核心執行：多伺服器、固定 Git Revision、Experiment／Run；硬體板為未來執行資源**

> 本文件只描述**最終完成品的形狀與里程碑**。它不是能力現況（看 `docs/CAPABILITY_LEDGER.md`）、
> 不是安全真相（看 `docs/PLATFORM_CHARTER.md` §6 與 `docs/DECISIONS.md`），也不授權任何
> invariant 變更或新能力——路線圖提到 ≠ 已核准 ≠ 已實作。文中狀態標記：**[已實作]** =
> 現行程式已有（多為 default-off）、**[已核准契約]** = 已具名裁定形狀但未完成／未啟用、
> **[未來目標]** = 尚無裁定或實作，動工前需要具名決策。

---

# 1. Positioning｜定位

定位、範圍與非目標的權威文字在 `docs/PLATFORM_CHARTER.md` §1–§2：**Agent-native
Engineering Platform**——瀏覽器中的完整 AI Engineering Workspace，AI 思考與提案、平台治理
與執行、人決定。本文件只回答「最終做到什麼樣子、分幾步」。

# 2. User model｜使用者模型（PROD-1）

**小團隊使用、單人審核。** 平台建立在已實作的多 actor 基礎上（OIDC identity、多角色 RBAC、
service account）[已實作，default-off]；正式體驗不採 two-person review：具決定權限的人可直接
核准，含自己提出的請求（`ALLOW_HIGH_RISK_SELF_APPROVAL=true` 姿態，DG-SELF-APPROVAL-OPTION-v1）
[已實作]。approval 閘門本身永遠存在（憲章 §2.2）。

# 3. Planes & boundary｜兩個 Plane 與邊界

兩平面模型、唯一交會點（promoted ProjectVersion）、硬體動作歸屬、Development Agent 的
可為／不可為，全部以 `docs/PLATFORM_CHARTER.md` §4 與 INV-PLANE-1／INV-PLANE-2 為準；
本文件不重述。

# 4. Target workflow｜完整工作流（最終態）

```text
Login
  ↓
Connect Agent Provider（Claude / Codex / …）
  ↓
Projects ── New Project / Import from Server
  ↓
Project Normalize → GitHub 紀錄庫
  ↓
AI Engineer（長期對話）⇄ isolated workspace（edit / validate / diff）
  ↓
Commit → promotion approval → ProjectVersion
  ↓
Experiment（Parameter Matrix）→ Auto Placement → Server A/B/C…
  │                                  └─ 硬體軌：synthesis / build → flash / program → HIL test（M5）
  ↓
Metrics / Artifacts（含 bitstream / firmware image）→ Compare → Ask Agent
  ↓
Proposed Next Round → 使用者核准額度 → 額度內自動迭代
```

# 5. Information architecture｜產品資訊架構

主導航：`My Workspace / Projects / Servers / AI / Approvals / Operations / Settings`

Project 內：`Overview / AI Engineer / Code / Experiments / Runs / Datasets / Artifacts / Hardware / Settings`
（`Hardware` 為 M5 新增的分頁，[未來目標]）

日常主要畫面只有 Projects、AI Engineer、Experiments、Runs、Datasets、Artifacts、Servers；
ExecutionPlan／Attempt／Outbox／Fencing 等內部機制收進 Advanced。[未來目標；現行為單一 v2 Workspace]

**Studio（DG-STUDIO-UI v1，2026-08-30）取代 Product Workspace 成為最終唯一主介面**：React＋TypeScript＋Vite，session 優先三欄，
權限提示與核准卡就地決定，實驗矩陣＋伺服器晶片；舊 Workspace 並存至 Phase 3。原「Product Workspace 是最終唯一主介面」
（DG-PRODUCT-PLAN-CORRECTIONS v1）自此由 Studio 承接。現況
（DG-UI-UNIFICATION v1 U1–U8 完成）：legacy UI 已退役並刪除，Workspace 為唯一介面，涵蓋
engineering task 介面（AI 工程精靈、任務詳情、coding runs、Development Session 工作台）。

# 6. Project is the center｜Project 是產品中心

Project 統一代表：Git repository、ProjectVersion、Environment、Run Template、Dataset bindings、
Project Instances、AI conversations、Experiments、Runs、Artifacts（未來含硬體 artifact）。
**Server 只是執行資源，不是 Project 的 source of truth**（憲章 §3）。

# 7. Execution spec interface｜執行規格介面（PROD-2）

**正式真相是 DB 中的 typed immutable revisions**：

- Run Template v2（`run-template-spec-v2`：entrypoint、parameter schema、resources、dataset inputs、
  outputs、metrics）[已核准契約，已實作 default-off]
- Environment revisions（`host-environment-v1`）[已核准契約，已實作 default-off]
- 修改一律經 approval 產生新 revision；Run 釘精確 revision。

`dispatch.yaml` **降級為匯入／匯出格式**[未來目標]：匯入 Project 時可從 repo 內的 `dispatch.yaml`
解析出 Run Template／Environment／Dataset binding **草稿**，仍經 approval 才落地為 revision；匯出時可把
現行 revisions 寫成 `dispatch.yaml` 供攜出；repo 檔案永遠不是執行真相，衝突以 DB revision 為準。

# 8. Project onboarding

## 8.1 New Project

Empty／Template／Existing GitHub Repository 三種來源；建立後即得 Project + 初始 ProjectVersion +
Default Environment + Default Run Template。[部分已實作：Project bootstrap v2 default-off]

## 8.2 Import from Server [已實作骨幹]

```text
Select Server → Read-only Scan → Candidates → Analyze
→ Normalize Preview → Apply → GitHub 紀錄庫 → Register Project
```

掃描安全規則（INV-SSH-4）：只讀 Git metadata、README 預覽、dependency 檔、project markers、大小統計；
**禁止讀** `.env`、SSH keys、private keys、secrets、credentials。

## 8.3 Normalize [未來目標]

檢查 Git／Remote／README／.gitignore／lockfile／dispatch.yaml／entrypoint／dataset 與 code 混放／secrets，
輸出 Normalization Report。Agent 可協助補齊（在 isolated workspace 內，不直接改正式 Project）。
Onboarding 完成的產物是 **Workspace Ready／Agent Ready** 能力，不綁定任何特定 provider。

# 9. GitHub role｜GitHub 角色（PROD-3）

**開發過程持續推 GitHub 作為紀錄；本地 hub 同時保有最新版，並繼續作為執行面的 code source。**

- 開發中的 commit／ProjectVersion 會同步 push 到 GitHub 留紀錄 [未來目標：gated on `DG-GITHUB-PUBLISH`，
  現行僅 interface+fake（D6）]；
- Run 永遠釘本地 promoted ProjectVersion，不依賴 GitHub 可用性；
- promotion 動作本身不自動 push（P-4 不變）；GitHub 同步是獨立、可重試的紀錄動作；
- GitHub 內容規則：Source／Config／Tests／Docs = Yes；Dataset／Checkpoint／Weights／Results／bitstream／
  firmware image = No；`.env`／Credentials／SSH Keys = **Never**。

# 10. ProjectVersion & Instance

- ProjectVersion：`project_id、git_commit、source_branch、promotion_state、promotion_approval、bundle_digest、
  created_at`；用於 Run pinning、server sync、rollback、reproducibility、artifact provenance。[已實作]
- Project Instance：某 Server 上的 checkout，狀態 `missing/syncing/available/diverged/dirty/busy/blocked/unknown`
  （`diverged` = 乾淨 checkout 但 commit ≠ hub HEAD，是可派工狀態）。[已實作]
- Instance Update：verify target → verify clean → verify no active work → Preview → Confirm → update exact
  revision → verify HEAD；禁止 auto reset／stash／merge／silent overwrite。[已實作，default-off]

# 11. Development Agent（provider-neutral；PROD-7）

模型與邊界以憲章 §4.3 為準。路線圖層面的要求：

- **承載方式（DG-AGENT-RUNTIME-V3，2026-08-30）**：每台 runner 的 `dispatch-agent` 服務以 **Claude Agent SDK** 執行 session
  （只出站連 Server A、A2A 語意通道、工作區權限提示由人逐條允許；INV-AGENT-1／2）[Phase 1a 實作中]。舊的 tmux＋`claude -p`
  回合（`claude-code-v1`、AgentSession V1 D2、助手回合）與 **Codex exec provider 於 Phase 1b 退役**（重新接入另案）。
- **Provider**：Claude 為主力；future providers 只能經 runner agent 的 reviewed registry 加入。
- **Selection**：完成品要求 **Manual**（使用者明選）+ per-Project 預設 provider；**Auto** 引擎為可選延伸、
  非 Definition of Done [Manual 已實作（C-3）；Auto 延後，動工前需具名裁定]。
- **互動形態**：每個 Project 有持續的 AI Conversation（第一版單一 main conversation）；網站保存自己的對話歷史
  [已實作：AIConversation（DG-CONVERSATION-V1）與 AgentSession + checkpoint 核准鏈（DG-AGENT-SESSION-V1／
  CHECKPOINT）]。對話中的**每一個**改碼、驗證、執行動作仍是獨立的受控 task + approval；對話本身沒有執行權。
- **能力目錄**（以 dispatch tool 形式落地，永遠沒有 `run_command` 式自由 shell）：

  | 形式 | 例子 |
  |---|---|
  | 唯讀 dispatch tool | list_servers、get_server_status、read_file、search_code、git_status/diff/log、get_run/logs/artifacts、compare_runs |
  | request-approval dispatch tool（只建 pending approval） | apply_patch/create_file/delete_file（=diff 核准）、git_commit（=commit/promotion 流程）、create_run/stop_run、create_experiment、（M5）request_hardware_job |
  | dispatch-controlled validation path | run_tests 與受控驗證命令 |

  session 內平台工具與證據物化：待 DG-ASSISTANT-TOOLS v1／DG-AGENT-SESSION-V2 裁定 [草稿]。
  credential-required 操作（sudo／OAuth／2FA）一律回到本人。

# 12. Dataset

logical binding（training／validation／test）→ 建 Run 時 resolve 到 immutable snapshot／version。
[已核准契約，已實作 default-off：dataset snapshot、assets v2、alias、sharing、publish]

# 13. Experiment & Run

- Experiment = 同一 ProjectVersion／Environment／Template／Dataset versions 下的一組不同 Parameters 的 Runs。
  [已核准契約：DG-EXPERIMENT-V1（EX-1…EX-7）；`experiment_create_v2` 已在 pilot 跑通 4-run matrix，
  現況以 ledger 為準]
- Parameter Matrix + Preview（`3 × 2 × 1 = 6 Runs`）+ Experiment Guard（Total Runs／Est. GPU Hours／
  Est. Storage／Expected Servers）。[已核准契約；GPU hours／storage 為展示性宣告（EX-5）]
- Run Contract：每個 Run 釘 `code_revision、environment_revision、dataset_versions、run_template_revision、
  parameters、resource_request`。[已核准契約：ExecutionPlan v2，已實作 default-off]
- Placement 預設 Auto（使用者只描述資源需求，Dispatch 選 Server；Advanced 才指定 Server／Tag／GPU type）。
  [部分已實作：auto_placement policy-scoped 機制，INV-APPROVAL-4b，default-off]

# 14. Execution backend｜執行後端（PROD-6）

**最終品的工作機主力是 Node Agent；SSH 永久保留為相容／緊急通道。**

- Node Agent：非 root、出站單向、lease／ack、durable 終態、逐台啟用、隨時回退（INV-NODE-*）。
  [已實作 test-only；實機啟用 gated on DG-NODE-CANARY]
- SSH backend 依 INV-SSH-1 永不移除、永不弱化；第一版與過渡期繼續以 SSH 為預設。[已實作，現行預設]
- Ambiguous launch、attempt、outbox 語意依 DG-EXEC-ATTEMPT／DG-AMBIGUOUS-LAUNCH。[已核准契約，rollout default-off]

# 15. Results / Metrics / Analysis

Run 完成收 status、logs、artifacts、server identity、timestamps。**metrics-v1 是一級產品契約**
[已實作：DG-METRICS-CONTRACT v1]：workload 寫 bounded typed `results/{job_id}/metrics.json`（扁平 object、
≤64 KiB、≤256 keys、拒絕 float），job-finish 收集後由 Server A 純函式解析入庫（`run_metrics` + 四態
collection status）；missing = unknown；invalid 永不影響任務終態（INV-SSH-6 不變）。

Experiment Dashboard：Run × Server × Params × Status × Metrics 表格，支援 Compare／Clone／Re-run／
Promote Artifact／Ask Agent。[未來目標；Product Run Experience v2 已實作 default-off 的 Run 卡／detail／compare]

分析規則（missing = unknown、結論附證據、建議需核准）承憲章 §8。

# 16. Optimization loop｜優化迴圈（PROD-5）

最終品支援**限額式自動迴圈**：

```text
Agent 提出下一輪 → 使用者一次核准一個額度（max_rounds / max_runs / max_gpu_hours / …）
→ 額度內：自動建立並執行下一輪 Experiment
→ 超額、異常、或指標停滯 → 停止，回到人工確認
```

[未來目標：需要對 approval 機制的新具名裁定 `DG-OPTIMIZATION-QUOTA`（類 INV-APPROVAL-4b 的
policy-scoped 模式）；在該裁定前，現行真相是每輪人工確認 suggest → preview → confirm。]

# 17. Approval UX

顯示實際操作與理由，單人即可決定：

```text
Agent 想安裝 flash-attn。
理由：實作需要。
[Approve] [Reject]
```

不顯示內部 domain jargon；requester 與 decider 都留稽核。核准卡自動更新（不需刷新頁面）[已實作]。

# 18. Hardware engineering track｜硬體工程軌（M5，[未來目標]）

定位把 FPGA synthesis／bitstream、MCU build／flash 與硬體驗證納入目標範圍；邊界已由憲章 §4.2 與
INV-PLANE-2 固定（硬體動作＝Compute Plane 受治理執行；實體動作永不從 workspace 發起、永不自動核准）。
動工前必須先裁定 **DG-HARDWARE-EXECUTION**（草稿：`docs/decisions/DG_HARDWARE_EXECUTION_DRAFT.md`，建議契約 H-1…H-6），至少決定：

| # | 待決項 | 選項空間（不預設答案） |
|---|---|---|
| 1 | **資源模型**：FPGA／MCU 板、programmer／探針、電源控制如何登記為附掛在 worker 的資源 | `ServerConfig.tags` 延伸 vs 新 `hardware_devices` 表；可用性探測必須是封閉唯讀指令（INV-SSH-4 同構） |
| 2 | **工作類型**：synthesis／bitstream、firmware build、flash／program、HIL test 作為 ExecutionPlan run kinds | 純建置（無實體副作用）與實體動作（flash／program／erase／power）分級；後者的 approval 永不自動、永不由 agent 觸發 |
| 3 | **Artifact**：bitstream／firmware image／測試報告成為一級 artifact | digest + provenance 綁 ProjectVersion + ExecutionPlan；前置：`engineering_task_artifacts`／`execution_attempt_artifacts`／`node_attempt_artifacts` 三張表統一 |
| 4 | **證據**：build log、programming receipt、HIL 結果 | metrics-v1 延伸 vs 新契約；missing = unknown |
| 5 | **排程**：一機一件是否延伸為一板一件 | 板為 worker 子資源 vs 獨立可排程單位（涉及 `DG-GPU-SCHED` 同類語意） |
| 6 | **安全**：實體動作的 dangerous 黑名單延伸與回退 | erase／power 指令封閉列舉；re-flash 已知良好映像的回退程序 |

工具鏈（Vivado／Quartus／yosys／openocd／platformio…）屬 workload 內容與 Environment revision，不是平台
依賴；平台永不因硬體軌放寬 INV-SSH-1 的遠端依賴封頂。

# 19. Not in the final product｜最終完成品暫不包含

- 大型多租戶／複雜多人協作 UX（小團隊單人審核為準）
- multi-agent swarm／A2A
- Kubernetes
- full browser IDE、arbitrary web terminal、root shell
- agent 無上限自主優化（只做限額式迴圈）
- Auto provider selection 引擎（延後為可選延伸，非完成品要求）
- Codex 重新接入（Phase 1b 退役後另案）；對外 A2A 進站（Phase 5 選配）

# 20. Milestones｜里程碑

| M | 內容 | 現況（導覽用；權威在 ledger） |
|---|---|---|
| M0 Personal Pilot | 單人全程瀏覽器 import → agent → promote → run → results | 運行中（DG-PERSONAL-PILOT-v1）；experiment matrix 已跑通 |
| M1 Project Onboarding | Import／Scan／Candidates／Instance／Bootstrap | 骨幹已實作（部分 default-off） |
| M2 Studio＋runner agent（v3） | dispatch-agent（Claude Agent SDK）、串流對話、工具卡、權限提示、diff、checkpoint→promote、Studio 專案頁 | DG-AGENT-RUNTIME-V3／DG-STUDIO-UI 已裁定（2026-08-30）；Phase 1a 進行中；AgentSession V1 為過渡期後備至 Phase 1b |
| M3 Experiment | Matrix、Guard、multi-server placement、Dashboard、Compare | experiment_create_v2 已落地；Dashboard 為未來目標 |
| M4 AI Optimization | Ask Agent → 建議 → 限額式自動迴圈 | 未實作；需 DG-OPTIMIZATION-QUOTA |
| **M5 Hardware Engineering** | 硬體資源登記 → synthesis／build job → flash／program（人核）→ HIL test → 硬體 artifact 與證據 | **零實作、零裁定**；需 DG-HARDWARE-EXECUTION |

# 21. Definition of Done｜最終完成品

小團隊成員可以完全從網站完成：

```text
Import Project → Normalize → GitHub 紀錄 → Agent（Claude 主力）修改
→ Commit → promote ProjectVersion → Multi-server Experiment（GPU；M5 後含硬體板）
→ Compare → 核准額度 → 自動優化迭代
```

過程不需要 SSH；所有 material 寫入經 approval；所有結論可追溯證據；任何 provider 都跨不出
Development Plane boundary；Run metrics 一律經 metrics-v1 契約結構化收集與解析（missing = unknown）；
日常操作只使用單一 Product Workspace 介面完成；硬體實體動作永遠有人按下核准。
