# Development Platform Model(Development Plane / Compute Plane)

本檔記載 dispatch-center 從「AI workload control plane」演進為
**AI/ML Development Platform** 的穩定結構模型:兩個 plane 的職責、它們唯一
的交會點,以及 Development Agent(Codex、Claude Code 與未來 provider)的
權限邊界。

**本檔不是不變量、不是核准,也不記載能力現況。** 安全真相在
`invariants.md` 與 `docs/DECISIONS.md`;實作真相在 current code + tests;
能力現況在 `docs/CAPABILITY_LEDGER.md`;產品方向在
`docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`。
本檔的作用是讓開發者知道**一個東西屬於哪一邊、受哪條邊界約束**;
一個能力現在是否存在,依 §6 的方法查證,不要依本檔任何句子推斷。

---

## 1. 為什麼要分 plane

原本的心智模型只有一條線:approval → schedule → dispatch → collect。
開發平台加入了一整類「還沒有要執行任何 workload」的工作:匯入舊專案、
整理專案結構、在隔離工作區改碼、跑測試、看 diff、決定要不要把這份程式碼
變成正式版本。這些工作的失敗模式與執行工作完全不同:

- Compute Plane 的風險是**跑錯東西**(錯的 code/dataset/機器/資源)。
- Development Plane 的風險是**改錯東西**(動到正式 checkout、外洩 secret、
  把未審閱的程式碼變成可執行版本、把 agent 當成 shell 用)。

把兩者混在同一組規則裡,會讓「agent 可以在工作區跑 pytest」被誤讀成
「agent 可以在工作機跑任意指令」。分 plane 就是為了讓這個誤讀不可能發生。

---

## 2. Development Plane

**問題**:應該存在什麼程式碼?

| 物件 | 意義 |
|---|---|
| Project Onboarding | 發現、掃描、匯入、登記一個專案(含既有 server 上的舊專案) |
| Isolated workspace / worktree | 由系統建立、與正式 checkout 隔離的改碼工作區 |
| Code edit / test / review | 在工作區內讀檔、改檔、跑受控驗證、產生 diff 供人審閱 |
| ProjectVersion | 經過人工核准、不可變、可被 Run 引用的程式碼版本 |

**輸出只有一種形式**:一份**可審閱、可 promote 的 revision**。
Development Plane 不決定「要不要跑」、「跑在哪台」、「用哪份資料」。

## 3. Compute Plane

**問題**:要跑什麼、跑在哪、用什麼資料、結果在哪?

| 物件 | 意義 |
|---|---|
| Dataset | 資料集註冊/快照/別名/分享/權限 |
| ExecutionPlan | 把 code revision + environment + template + dataset + resource 釘成一份不可變執行意圖 |
| Approval | 所有 material 寫入的唯一落地閘門 |
| Scheduler | 確定性資格過濾 + priority/FIFO 選機 |
| SSH / Node backend | 唯二受控執行通道 |
| Run | 一次實際執行的生命週期與狀態 |
| Results / Artifacts | 產出、指標、log 的收集與呈現 |

Compute Plane 的狀態機、reconciliation、哨兵協議規則見 `state-reconciliation`
與 `INV-STATE-*`/`INV-SSH-*`。

## 4. 唯一交會點:promotion

```text
Development Plane                    Compute Plane
  workspace/worktree
        │
     diff + tests
        │
   human review
        │
   [ promotion approval ]   ←── 唯一的門
        │
   ProjectVersion  ────────────→  ExecutionPlan  →  Run
```

精確地說:**Development artifact 進入 Compute execution lifecycle 的唯一
正式 transition 是 promoted ProjectVersion。** 共用底層 SSH/Job
infrastructure(例如現行 Codex validation 以 approved Job 承載)只是共用
基礎設施,不代表未 promoted 的程式碼已進入 Compute workload lifecycle——
validation 的產物仍然只是 Development artifact,必須經 promotion 才能被
ExecutionPlan 引用。

規則:

- Development Plane 的產出進入 Compute Plane **只能**經過 promotion 產生的
  ProjectVersion;不得直接改動正式 project instance、不得讓 dirty worktree
  成為 Run 的來源、不得讓「agent 說它改好了」本身變成可執行版本。
- Promotion **永遠不得自動核准**(DG-CODE-PROMOTE-v1 P-1:任何自動路徑都會讓
  系統執行沒有人看過的程式碼)。
- 同一 commit 重複 promote 是 no-op(P-2);沒有 promotion approval 的
  `project_versions` 列是 `legacy_observed`,不得支撐 reproducible run(P-3)。
- Promotion 只寫本地 hub,**不推 GitHub**(P-4);GitHub 發布是獨立且尚未核准
  的決策。
- Promotion 後保留 worktree(P-5);清理是另行核准的操作。

## 5. Development Agent 模型與邊界

Development Plane 的協作者是 **Development Agent**——一個受控的改碼代理。
它不是單一產品:**「Codex」不是這個抽象層的名字**,它只是第一個 provider。
目前有哪些 provider,以 reviewed allowlist registry(`app/coding_agents.py`)
與其測試為準。

### 5a. Conceptual model

```text
DevelopmentAgent          ── 抽象角色:在隔離工作區改碼、驗證、提案
   │ 由一個 provider 實現
AgentProvider             ── provider-specific adapter(registry 為準)
AgentSession              ── 一次與某 provider 的工作階段(語意以
                              code/tests 與具名裁定為準,如 DG-AGENT-SESSION-V1)
```

分層規則:

- **AgentProvider 是唯一允許 provider-specific 細節存在的地方**(CLI 呼叫
  形狀、協議、版本 pin、auth mode)。Project、Workspace、ProjectVersion、
  ExecutionPlan、Approval 等核心 domain 物件不得依賴特定 provider,也不得
  把 provider CLI 細節寫進 schema 或共用合約。
- Provider 只能從 reviewed allowlist registry(`app/coding_agents.py`)以
  provider id 選取,永遠不能由請求提供 executable 或任意指令。
- 所有 provider 受**同一套** Development Plane safety boundary 約束;
  換 provider 永遠不是權限提升。

### 5b. Agent selection

依 DG-PRODUCT-PLAN-CORRECTIONS v1:**Manual + per-Project 預設 provider
是產品要求;Auto 引擎是可選延伸,不屬 Definition of Done**。現況(哪些
selection 能力已實作)以 code/tests 與 `docs/DECISIONS.md` 具名裁定為準。

- **Manual**:使用者明確選 provider(只能選 approved registry 中且目前
  enabled 的 provider id)。
- **Auto**:系統依 configured/installed、availability、supported
  capability、project requirement、policy、session requirement 決定。

Auto selection 必須:

- 確定性、可解釋——同樣輸入選同一個 provider,理由可由記錄資料推導;
- 記錄 selected provider(persist 到 session/run 記錄);
- 不因 provider 不同提升 authorization,不繞過 approval / ExecutionPlan /
  Dataset permission / SSH boundary;
- 永不 silently fallback 到另一個 provider,更不得 fallback 到有效權限
  更大的 provider——provider 失效或 drift 一律 fail closed 並回報。

### 5c. 邊界(對每一個 provider 同樣成立)

Development Agent 可以(在既有受控通道內):

- 在系統建立的隔離 workspace/worktree 中讀檔、改檔;
- 跑受控的 project-local 驗證(test/lint/typecheck/build),經由 bounded、
  dispatch-controlled validation path;
- 產生 reviewable diff、說明、建議、下一步提案;
- 建立 pending approval。

Development Agent 不得:

- 繞過 authorization / approval / ExecutionPlan / Dataset permission /
  promotion rule / SSH boundary;
- 核准自己的請求(提案通道與批准通道結構性分離,`INV-LLM-1`/`INV-LLM-2`);
- 取得 SSH 私鑰或直接開連線(`INV-LLM-3`:agent 工具層不得 import 執行層);
- 任意讀寫 runtime DB / audit / server config;
- 取得 unrestricted secret access;
- 自行 deploy 或 promote——那是另外的人工核准動作;
- 把 dispatch/執行通道當成 general-purpose shell;
- 自行決定工作區位置——worktree 路徑只能由 dispatch 端建立;
- 自行 push external origin、動正式 project instance、或讓分析建議自動變成動作。

兩種「執行」的抽象結構(對每一個 provider 同樣成立):

```text
Development validation:
  DevelopmentAgent
  → bounded dispatch-controlled validation path
  → isolated workspace

Compute workload:
  promoted ProjectVersion
  → ExecutionPlan
  → approval
  → dispatch
  → worker
```

一個 provider 用哪條 validation mechanism 承載是 **implementation fact,
不是對其他 provider 的架構要求**;每一條 mechanism 都必須能追溯到
`docs/DECISIONS.md` 的具名裁定,任何新的 validation mechanism 都需要新的
具名裁定,且不得弱化 approval、audit、isolation 或 SSH boundary。真正的
training/GPU/worker workload 永遠不從 workspace 啟動:它只能走上面的
Compute workload 鏈。

正確的能力流向永遠是:

```text
DevelopmentAgent → Dispatch tool → policy/validation → approval → 執行層 → server
```

錯誤(禁止)的流向:

```text
agent → credential/SSH → server
agent → 執行層 → server
agent → approve() → 副作用
```

## 6. 一個能力「存不存在」怎麼查證

本檔不維護 implemented / not-implemented 對照表(那種快照必然過期)。
在依賴、擴充或否定任何平台能力之前,一律照這個順序查證:

1. **實作真相**:current code + tests——找到實際的 module、route、
   schema、`VALID_APPROVAL_KINDS` 條目與對應測試;找不到就是不存在。
2. **核准真相**:`docs/DECISIONS.md`——能力必須能追溯到一條具名裁定
   (DG-*),裁定同時界定它的 bounded scope 與明文 non-goals;沒有具名
   裁定的能力不得實作、不得預先發明語意(狀態值、轉移、表、registry
   條目)。
3. **能力現況**:`docs/CAPABILITY_LEDGER.md`——`implemented` ≠
   `default-enabled` ≠ `deployed` ≠ `production-ready`;`unknown` 不是
   `yes`。已實作但 default-off 的能力,不得在文件或行為上宣稱是現況。
4. **產品計畫書只是方向**:計畫書提到 ≠ 已核准 ≠ 已實作;照計畫書
   直接寫程式就是本節要防止的失敗。

新增一類能力(新 approval kind、新 lifecycle、新 provider、新 validation
mechanism、新外部發布通道)= 產品/架構決策,需要明文具名裁定。

## 7. 平台工作的紅線

- 不得為了對齊計畫書而改動 `invariants.md` 的語意。
- 不得因為某個 default-off 能力「已實作」就在文件或行為上宣稱它是現況。
- 不得把 Development Plane 的便利性(在工作區跑指令)延伸成 Compute Plane
  的權限(在工作機跑指令)。
- 不得建立第二條繞過 approval 的寫入路徑,即使它只服務開發流程。
- 不得讓 browser/WebSocket/in-memory session 成為任何 Development Plane
  物件的唯一真相來源。
