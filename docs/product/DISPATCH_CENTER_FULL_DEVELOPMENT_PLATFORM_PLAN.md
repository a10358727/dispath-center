# Dispatch Center 完整開發平台計畫書
## Small-Team Web AI/ML Engineering & Experiment Platform

**文件類型：產品藍圖 / 最終完成品定義**
**修訂：2026-08-23（依六項產品釐清裁定改版；原 2026-08 版全文重寫）**
**使用者模型：小團隊、單人審核**
**核心 AI：Development Agent（最終主力：Claude / Claude Code;現行已實作 provider：Codex）**
**核心執行：多伺服器、固定 Git Revision、Experiment / Run**

> 本文件只定義**最終完成品**與其邊界。它不是能力現況（看
> `docs/CAPABILITY_LEDGER.md`）、不是安全真相（看 canonical invariants 與
> `docs/DECISIONS.md`）、也不授權任何 invariant 變更。文中狀態標記:
> **[已實作]** = 現行程式已有(多為 default-off)、**[已核准契約]** = 已具名
> 裁定形狀但未完成/未啟用、**[未來目標]** = 尚無裁定或實作,動工前需要
> 具名決策。

---

# 1. 產品定位

Dispatch Center 的最終完成品是:

> **Small-Team Web AI/ML Engineering Control Center**

一個小團隊(或單人)從一個網站完成:

- 建立 / 從既有 Server 匯入 Project
- 整理與標準化舊 Project
- 與 Development Agent 長期對話、讓它在隔離工作區改碼與驗證
- Review Diff、Commit、promote 成 ProjectVersion
- 把固定版本同步到多台 Server
- 建立 Experiment 與 Parameter Matrix,多 GPU Server 分散執行
- 集中查看 Runs / Metrics / Logs / Artifacts、比較 Run
- 讓 Agent 分析結果並提出下一輪,在核准的額度內自動迭代

日常不需要 ssh / tmux / scp / rsync / 手動比對 commit / 手動找 log。
只有 credential、sudo、OAuth、2FA 等本人授權需要中斷自動流程。

# 2. 使用者模型(裁定 ①)

**小團隊使用、單人審核。**

- 平台建立在已實作的多 actor 基礎上(OIDC identity、多角色 RBAC、
  service account)[已實作,default-off]。
- 正式體驗**不採用 two-person review**:具決定權限的人可以直接核准,
  包含核准自己提出的請求——即 `ALLOW_HIGH_RISK_SELF_APPROVAL=true` 的
  部署姿態(DG-SELF-APPROVAL-OPTION-v1)[已實作]。
- approval 閘門本身永遠存在:人仍要按核准;任何 agent 永遠不能核准
  任何請求(含自己的)。這是 invariant,不隨產品目標改變。

# 3. 兩個 Plane 與唯一交會點

```text
Development Plane                     Compute Plane
「應該存在什麼程式碼?」               「要跑什麼、跑在哪、用什麼資料?」

Project Onboarding                    Dataset
Development Agent(Claude/Codex/…)     ExecutionPlan
isolated workspace/worktree           Approval
edit / validate / diff / review       Scheduler
ProjectVersion                        SSH / Node backend
                                      Run → Results / Artifacts
        │                                     ▲
        └── promotion approval(人工)──────────┘
```

**Development artifact 進入 Compute execution lifecycle 的唯一正式
transition 是 promoted ProjectVersion。** 共用底層 SSH/Job 基礎設施不代表
未 promoted 的程式碼已進入 Compute workload lifecycle。

Promotion 規則承 DG-CODE-PROMOTE-v1(P-1…P-5)[已實作,default-off]:
永不自動核准;同 commit 重複 promote 是 no-op;無 promotion approval 的
版本是 `legacy_observed` 不得支撐 reproducible run;promotion 只寫本地
hub、不推 GitHub;promotion 後保留 worktree。

完整 plane 模型見
`.claude/skills/dispatcher-domain/references/development-platform.md`。

# 4. 完整工作流(最終態)

```text
Login
  ↓
Connect Agent Provider(Claude / Codex / …)
  ↓
Projects ── New Project / Import from Server
  ↓
Project Normalize → GitHub 紀錄庫
  ↓
AI Engineer(長期對話)⇄ isolated workspace(edit / validate / diff)
  ↓
Commit → promotion approval → ProjectVersion
  ↓
Experiment(Parameter Matrix)→ Auto Placement → Server A/B/C…
  ↓
Metrics / Artifacts → Compare → Ask Agent
  ↓
Proposed Next Round → 使用者核准額度 → 額度內自動迭代
```

# 5. 產品資訊架構

主導航:

```text
My Workspace / Projects / Servers / AI / Approvals / Operations / Settings
```

Project 內:

```text
Overview / AI Engineer / Code / Experiments / Runs / Datasets / Artifacts / Settings
```

日常主要畫面只有 Projects、AI Engineer、Experiments、Runs、Datasets、
Artifacts、Servers;ExecutionPlan / Attempt / Outbox / Fencing 等內部
機制收進 Advanced。[未來目標;現行為 legacy UI + default-off Product
Workspace]

# 6. Project 是產品中心

Project 統一代表:Git repository、ProjectVersion、Environment、
Run Template、Dataset bindings、Project Instances、AI conversations、
Experiments、Runs、Artifacts。**Server 只是執行資源,不是 Project 的
source of truth。**

# 7. 執行規格介面(裁定 ②)

**正式真相是 DB 中的 typed immutable revisions**:

- Run Template v2(`run-template-spec-v2`:entrypoint、parameter schema、
  resources、dataset inputs、outputs、metrics)[已核准契約,已實作
  default-off]
- Environment revisions(`host-environment-v1`)[已核准契約,已實作
  default-off]
- 修改一律經 approval 產生新 revision;Run 釘精確 revision。

`dispatch.yaml` **降級為匯入/匯出格式**[未來目標]:

- 匯入 Project 時可從 repo 內的 `dispatch.yaml` 解析出 Run Template /
  Environment / Dataset binding **草稿**,仍經 approval 才落地為 revision;
- 匯出時可把現行 revisions 寫成 `dispatch.yaml` 供攜出;
- repo 檔案永遠不是執行真相,兩者衝突以 DB revision 為準。

# 8. Project Onboarding

## 8.1 New Project

Empty / Template / Existing GitHub Repository 三種來源;建立後即得
Project + 初始 ProjectVersion + Default Environment + Default Run
Template。[部分已實作:Project bootstrap v2 default-off]

## 8.2 Import from Server [已實作骨幹]

```text
Select Server → Read-only Scan → Candidates → Analyze
→ Normalize Preview → Apply → GitHub 紀錄庫 → Register Project
```

掃描安全規則(與 INV-SSH-4 一致):只讀 Git metadata、README 預覽、
dependency 檔、project markers、大小統計;**禁止讀** `.env`、SSH keys、
private keys、secrets、credentials。

## 8.3 Normalize [未來目標]

檢查 Git / Remote / README / .gitignore / lockfile / dispatch.yaml /
entrypoint / dataset 與 code 混放 / secrets,輸出 Normalization Report。
Agent 可協助補齊(在 isolated workspace 內,不直接改正式 Project)。

Onboarding 完成的產物是 **Workspace Ready / Agent Ready** 能力,
不綁定任何特定 provider。

# 9. GitHub 角色(裁定 ③)

**開發過程持續推 GitHub 作為紀錄;本地 hub 同時保有最新版,並繼續作為
執行面的 code source。**

- 開發中的 commit / ProjectVersion 會同步 push 到 GitHub 留紀錄
  [未來目標:gated on `DG-GITHUB-PUBLISH`,現行僅 interface+fake(D6)];
- Run 永遠釘本地 promoted ProjectVersion,不依賴 GitHub 可用性;
- promotion 動作本身不自動 push(P-4 不變);GitHub 同步是獨立、可重試
  的紀錄動作;
- GitHub 內容規則:Source/Config/Tests/Docs = Yes;Dataset/Checkpoint/
  Weights/Results = No;`.env`/Credentials/SSH Keys = **Never**。

# 10. ProjectVersion 與 Instance

- ProjectVersion:`project_id、git_commit、source_branch、promotion_state、
  promotion_approval、bundle_digest、created_at`;用於 Run pinning、
  server sync、rollback、reproducibility、artifact provenance。[已實作]
- Project Instance:某 Server 上的 checkout,狀態
  `missing/syncing/available/diverged/dirty/busy/blocked/unknown`。[已實作]
- Instance Update:verify target → verify clean → verify no active work →
  Preview → Confirm → update exact revision → verify HEAD;禁止 auto
  reset/stash/merge/silent overwrite。[已實作,default-off]

# 11. Development Agent(provider-neutral;裁定 ④ 與 Claude 主力方向)

## 11.1 Provider 模型

```text
DevelopmentAgent(抽象角色)
├── Claude / Claude Code   ← 最終完成品的主力 provider[未來目標,無 adapter]
├── Codex                  ← 現行唯一已實作 provider[已實作]
└── future providers
```

- Provider 只能從 reviewed allowlist registry 以 id 選取,永不接受任意
  executable;provider CLI 細節只存在於 adapter,不進核心 domain model。
- **選 provider 永遠不是權限提升**:所有 provider 受同一套 Development
  Plane safety boundary。
- Selection:**Manual**(使用者明選)或 **Auto**(依 configured/available/
  capability/project requirement/policy/session requirement 決定;
  確定性、可解釋、記錄 selected provider、失效 fail closed、永不 silent
  fallback 到權限更大的 provider)。[未來目標:selection 引擎不存在]

## 11.2 互動形態:長期對話 + task 並存

- 每個 Project 有持續的 AI Conversation(第一版單一 main conversation);
  網站保存自己的對話歷史,不把 provider thread 當唯一資料來源。
  [未來目標:AIConversation / AgentSession domain 不存在]
- 對話中的**每一個**改碼、驗證、執行動作仍是獨立的受控 task + approval;
  對話本身沒有任何執行權。

```text
AIConversation
├── Messages
├── AgentSession(provider-scoped)
├── RemoteWorkspace(dispatch 建立)
├── Controlled Tasks(每個都有 approval)
├── GitCheckpoint
└── Run References
```

## 11.3 能力與邊界

Development Agent 可以:在 dispatch 建立的 isolated workspace/worktree
讀檔改檔;經 **bounded dispatch-controlled validation path** 跑
test/lint/typecheck/build(現行 Codex 以 approved Job + SSH/tmux/sentinel
承載——這是 implementation fact,不是對未來 provider 的架構要求);產生
reviewable diff;建立 pending approval。

Agent 永遠不得:核准任何請求;取得 credential/SSH key/直接執行 handle;
獲得 shell/exec/run_command 工具;繞過 authorization / approval /
ExecutionPlan / Dataset permission / promotion / SSH boundary;自行選
工作區位置;push external origin;動正式 instance;讓分析建議自動變成
動作。能力流向永遠是
`Agent → dispatch tool → policy → approval → 執行層 → server`。

## 11.4 Agent 能力目錄(以 dispatch tool 形式實現)

原版 §23 的工具清單**不是** raw tool 名單,而是最終品的能力目錄,每項
只能以下列三種受控形式落地:

| 形式 | 例子 |
|---|---|
| 唯讀 dispatch tool | list_servers、get_server_status、read_file、search_code、git_status/diff/log、get_run/logs/artifacts、compare_runs |
| request-approval dispatch tool(只建 pending approval) | apply_patch/create_file/delete_file(=diff 核准)、git_commit(=commit/promotion 流程)、create_run/stop_run、create_experiment |
| dispatch-controlled validation path | run_tests 與受控驗證命令 |

**永遠不存在** `run_command` 式的自由 shell 工具(INV-LLM-2)。原版
Safe/Confirm/Credential Command Policy 由「封閉唯讀集 + approval +
credential 永不進 prompt」取代;credential-required 操作(sudo/OAuth/2FA)
一律回到本人。

# 12. Dataset

logical binding(training/validation/test)→ 建 Run 時 resolve 到
immutable snapshot/version。[已核准契約,已實作 default-off:
dataset snapshot、assets v2、alias、sharing、publish]

# 13. Experiment 與 Run

- Experiment = 同一 ProjectVersion / Environment / Template / Dataset
  versions 下的一組不同 Parameters 的 Runs。[未來目標]
- Parameter Matrix + Preview(`3 × 2 × 1 = 6 Runs`)+ Experiment Guard
  (Total Runs / Est. GPU Hours / Est. Storage / Expected Servers)。
  [未來目標]
- Run Contract:每個 Run 釘 `code_revision、environment_revision、
  dataset_versions、run_template_revision、parameters、resource_request`。
  [已核准契約:ExecutionPlan v2,已實作 default-off]
- Placement 預設 Auto(使用者只描述資源需求,Dispatch 選 Server;
  Advanced 才指定 Server/Tag/GPU type)。[部分已實作:auto_placement
  policy-scoped 機制,INV-APPROVAL-4b,default-off]

# 14. 執行後端(裁定 ⑥)

**最終品的工作機主力是 Node Agent;SSH 永久保留為相容/緊急通道。**

- Node Agent:非 root、出站單向、lease/ack、durable 終態、逐台啟用、
  隨時回退(INV-NODE-*)。[已實作 test-only;實機啟用 gated on
  DG-NODE-CANARY]
- SSH backend 依 INV-SSH-1 永不移除、永不弱化;第一版與過渡期繼續以
  SSH 為預設。[已實作,現行預設]
- Ambiguous launch、attempt、outbox 語意依 DG-EXEC-ATTEMPT /
  DG-AMBIGUOUS-LAUNCH。[已核准契約,rollout default-off]

# 15. Result / Metrics / 分析

Run 完成收 status、metrics(建議 `metrics.json` 標準鍵)、logs、
artifacts、resource usage、server identity、timestamps。[部分已實作]

Experiment Dashboard:Run × Server × Params × Status × Metrics 表格,
支援 Compare / Clone / Re-run / Promote Artifact / Ask Agent。[未來目標;
Product Run Experience v2 已實作 default-off 的 Run 卡/detail/compare]

分析規則(missing = unknown、結論附證據、建議需核准)承
`dispatcher-domain/references/result-analysis.md`。

# 16. 優化迴圈(裁定 ⑤)

最終品支援**限額式自動迴圈**:

```text
Agent 提出下一輪 → 使用者一次核准一個額度
  (max_rounds / max_runs / max_gpu_hours / …)
→ 額度內:自動建立並執行下一輪 Experiment
→ 超額、異常、或指標停滯 → 停止,回到人工確認
```

[未來目標:需要對 approval 機制的新具名裁定(類 INV-APPROVAL-4b 的
policy-scoped 模式);在該裁定前,現行真相是每輪人工確認
suggest → preview → confirm。]

# 17. Approval UX

顯示實際操作與理由,單人即可決定(裁定 ①):

```text
Agent 想安裝 flash-attn。
理由:實作需要。
[Approve] [Reject]
```

不顯示內部 domain jargon;requester 與 decider 都留稽核。

# 18. 最終完成品暫不包含

- 大型多租戶 / 複雜多人協作 UX(小團隊單人審核為準)
- multi-agent swarm / A2A
- Kubernetes
- full browser IDE、arbitrary web terminal、root shell
- agent 無上限自主優化(只做限額式迴圈)

(原版「暫不做 Claude Code」已移除——Claude 是最終主力方向。)

# 19. Milestones(重新基準化)

| M | 內容 | 現況 |
|---|---|---|
| M1 Project Onboarding | Import/Scan/Candidates/Instance/Bootstrap | 骨幹已實作(部分 default-off) |
| M2 Web Development Agent | Conversation、AgentSession、provider connection(Claude 優先)、workspace UI、diff/commit | 未實作;現行僅 task 式 Codex 路徑 |
| M3 Experiment | Matrix、Guard、multi-server placement、Dashboard、Compare | ExecutionPlan/Run 契約已核准;Experiment 層未實作 |
| M4 AI Optimization | Ask Agent → 建議 → 限額式自動迴圈 | 未實作;需新裁定 |

# 20. Definition of Done(最終品)

小團隊成員可以完全從網站完成:

```text
Import Project → Normalize → GitHub 紀錄 → Agent(Claude 主力)修改
→ Commit → promote ProjectVersion → Multi-server Experiment
→ Compare → 核准額度 → 自動優化迭代
```

過程不需要 SSH;所有 material 寫入經 approval;所有結論可追溯證據;
任何 provider 都跨不出 Development Plane boundary。

# 21. 一句話

> **Dispatch Center 是一個讓小團隊只靠瀏覽器與 Development Agent
> (以 Claude 為主力),就能管理 Project、遠端 Server、程式開發、
> Git 版本、多 GPU 實驗與模型優化的完整工程平台。**
