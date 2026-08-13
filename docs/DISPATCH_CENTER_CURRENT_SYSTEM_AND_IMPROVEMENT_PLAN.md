# Dispatch Center 現有系統盤點與改良計畫

> 文件定位：本文件保留為 Product Brief 與 UX roadmap，用來說明產品願景與
> 使用者體驗，不是目前能力、工作包順序或部署狀態的權威來源。
>
> 實作權威：根目錄 [`PLAN.md`](../PLAN.md)；其 byte-identical 文件鏡像為
> [`DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md`](DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md)。
>
> 現況證據：[`CAPABILITY_LEDGER.md`](CAPABILITY_LEDGER.md)。文件中的
> 「建議功能」不代表已實作、啟用、部署、通過 Canary 或 production-ready。
>
> 權威順序：canonical invariants → [`DECISIONS.md`](DECISIONS.md) →
> 程式碼／邊界測試 → Capability Ledger → Product v2 execution plan →
> 其他歷史 roadmap／status 文件。

---

# 0. 2026-08-07 現況重新基準化

以下只陳述目前 repository evidence；環境狀態仍以 Capability Ledger 與實際
部署／Canary 證據為準。

| 能力 | 本機實作狀態 | 尚未取得的證據／Product v2 動作 |
|---|---|---|
| OIDC Authorization Code + PKCE | 已實作，預設關閉 | 尚未部署；117 Pilot 接非 production IdP |
| Authorization | `off`、`shadow`、`enforce` 均已實作，預設 `off` | 尚未部署或通過 Canary；117 Pilot 才啟用 `enforce` |
| Project Workspace | 七個 pane 與 supporting APIs 已存在 | 整合成 v2 Project-centered experience |
| Run Profile／ExecutionPlan | Immutable revision、preview、request、approval binding 與 lineage 已存在 | 增加 typed v2 companion contract，不建立平行真相 |
| Dataset Snapshot | Content-addressed publish 已實作，預設關閉 | 增加 asset、alias、sharing、lineage 與 usage |
| Audit race／migration checksum／ledger gap | 修正已完成並保留本機 regression gates | 不得把本機通過宣稱為部署證據 |
| Node isolation | Contract、程式與本機 integration evidence 已存在 | 不屬於第一個 Product v2 Pilot；尚無正式 Node Canary |
| External audit anchor | 只有本機 signer／verifier | Off-host anchor 仍是 production gate |

下方舊 phase、PR 編號與 endpoint 範例繼續保留作為產品形成過程與 UX 構想；
實際執行順序、API contract、scope 與驗收一律以 Product v2 execution plan
為準。

---

# 1. 執行摘要

目前的 Dispatch Center 是一套偏向安全治理與遠端執行控制的內部平台，核心能力包括：

- 專案、伺服器與工作節點管理。
- 工作建立、排程與遠端執行。
- SSH 與 Node Agent 兩種執行後端。
- 人工核准與權限控制。
- Execution Plan、Attempt、Lease、Fencing 與 Recovery。
- Audit event、export outbox、migration 與備份驗證。
- Dataset 登錄、Snapshot 與工程任務的初步基礎。

目前系統的優勢是底層控制與安全邊界逐漸完整；主要問題是一般使用者仍需要理解太多技術概念，例如 Token、Server、Node、Job、Backend、Approval、Attempt 與 Dataset Path。

建議的產品方向是：

> 將 Dispatch Center 從「管理遠端執行的工程控制台」，改造成「以專案為中心的內部運算工作台」。

理想使用流程：

```text
使用公司帳號登入
→ 進入我的專案
→ 選擇執行範本
→ 選擇 Dataset 版本
→ 填寫少量參數
→ 系統自動檢查環境與資源
→ 預覽並提交
→ 必要時進入核准
→ 自動選擇執行節點
→ 查看進度、Log、結果與 Artifact
```

---

# 2. 目前正在做的系統是什麼

## 2.1 產品定位

Dispatch Center 是一套：

> 面向內部 CPU／GPU 工作節點的安全派工與專案治理平台。

它的用途不是單純啟動 Shell Command，而是確保工作在以下條件成立時執行：

- 正確的人提出要求。
- 正確的人完成核准。
- 使用者具有專案與資源權限。
- 執行內容與核准內容一致。
- 使用固定的程式碼、Dataset 與環境版本。
- 在正確的 Server 或 Node 上執行。
- 執行失聯時不會被錯誤重派。
- 任務停止、失敗與重試具備明確狀態。
- 完成後有可追溯的 Log、Artifact 與 Audit Evidence。

## 2.2 目前系統的核心流程

```text
使用者／服務建立工作要求
→ Authorization 判斷
→ 必要時建立 Approval
→ 固定 Execution Plan
→ 建立 Execution Attempt
→ 選擇 SSH 或 Node Backend
→ 執行遠端副作用
→ 回報 Heartbeat、Log 與 Terminal Result
→ 收集 Artifact
→ 更新 Job／Run 狀態
→ 寫入 Audit Evidence
```

## 2.3 主要系統元件

### Control Plane

負責：

- API 與 Web UI。
- 使用者身分與權限。
- 專案、Dataset、Server、Node 管理。
- Approval。
- Job／Run／Attempt 狀態。
- Scheduler。
- Worker／Outbox。
- Audit。
- Migration、Backup、Restore 與 Readiness。

### SSH Backend

負責：

- 連線既有工作主機。
- 準備執行目錄。
- 啟動 Supervisor。
- 查詢執行狀態。
- 收集 Log、Terminal Evidence 與 Artifact。
- 停止工作。

SSH 目前仍是重要的既有執行方式與回退路徑。

### Node Agent

安裝在運算節點，由節點主動連出 Control Plane：

- Activation。
- Poll Assignment。
- Acknowledge。
- Heartbeat。
- Launch。
- Stop Receipt。
- Terminal Report。
- Artifact Report。
- Credential Rotation。
- Restart Recovery。

### Worker Process

負責非 HTTP 背景責任，例如：

- Execution outbox。
- Audit export outbox。
- Reconciliation。
- Terminal projection。
- Result recovery。

---

# 3. 目前已具備的能力

## 3.1 工程與部署基礎

目前已建立或正在整合：

- Control Plane 與 Node Agent 的獨立套件邊界。
- Typed Settings 與啟動驗證。
- API、Scheduler、Worker 的 Process Role。
- API Router Boundary。
- Ruff、mypy、compile、coverage 與 package smoke。
- Versioned SQLite Migration。
- Repository／Unit of Work 的架構接縫。
- Backup、Restore Verify 與 DB Check。

## 3.2 權限與核准

目前已有：

- Authorization 的 `off`、`shadow`、`enforce` 模式。
- Project 與 Global Scope。
- Service Token Scope。
- Collection Row Filtering。
- 未登錄 Route 的 Fail-closed 行為。
- 高風險操作的 Separation of Duties。
- Approval create／approve／reject 的基礎流程。

## 3.3 工作執行

目前已有或正在建立：

- Job／Attempt 狀態。
- SSH 執行流程。
- Node Protocol 2.0。
- Lease、Ack、Heartbeat、Stop 與 Terminal Result。
- Worker Role。
- Node Workload Isolation Contract Builder。
- Unknown／Recovery 語意。
- 避免 SSH 與 Node 重複執行同一工作的設計方向。

## 3.4 Audit 與資料完整性

目前已有：

- Durable Audit Event Table。
- Append-only Trigger。
- Event Sequence 與 Previous Hash。
- Versioned Hash Contract。
- Audit Export Outbox。
- Retry、Dead-letter 與 Manual Replay。
- Legacy JSONL 與 Durable DB 的來源標示。
- Backup／Restore 時的 Hash Chain 驗證。

## 3.5 專案與 Dataset 基礎

目前系統已具備或規劃中的資源包括：

- Project。
- Project Instance。
- Server／Node。
- Dataset Registry。
- Dataset Snapshot。
- Dataset Card。
- Engineering Task。
- Coding Run。
- Artifact。

這些能力目前偏向後端資源與管理端點，尚未完整整合成一般使用者容易操作的工作流程。

---

# 4. 目前主要問題

## 4.1 人員登入體驗不正常

OIDC 與 server-side browser session 已有本機實作，但預設關閉且沒有部署
證據；本節描述的是 Pilot 啟用前的一般使用者體驗缺口，不是「尚無 OIDC
程式碼」。

目前使用 Token 登入對工程測試方便，但對一般使用者存在問題：

- 使用者需要手動取得並保存 Token。
- Token 容易被複製、截圖或誤貼。
- 無法自然整合公司帳號、MFA、離職停權與群組管理。
- 瀏覽器端 Token 保存方式容易產生額外風險。
- 人類使用者與 Service／Node Identity 沒有清楚分流。

## 4.2 導覽以技術資源為中心

目前使用者容易先看到：

```text
Server
Node
Job
Approval
Dataset
Token
```

但一般使用者真正想做的是：

```text
找到專案
執行專案
選擇資料
查看結果
重新執行
```

## 4.3 專案資料分散

同一專案相關資訊可能分散在：

- Project。
- Project Instance。
- Server。
- Job。
- Approval。
- Dataset。
- Artifact。
- Audit。

使用者無法在一個頁面回答：

- 專案目前版本是什麼？
- 最近執行是否成功？
- 使用哪個 Dataset？
- 產出的模型在哪裡？
- 誰有權限？
- 哪些機器可執行？

## 4.4 每次執行需要理解太多技術細節

若使用者每次都要處理：

- Shell Command。
- Working Directory。
- Server。
- Backend。
- GPU。
- Environment Variable。
- Dataset Path。
- Approval Payload。

系統雖然安全，但日常操作成本仍高。

## 4.5 Dataset 偏向 Path／Registry，而不是完整資產

Dataset 若只有名稱、版本字串與路徑，會出現：

- `final`、`final2`、`latest-new` 等混亂命名。
- 不知道版本是否可被覆寫。
- 不知道由哪個 Run 產生。
- 不知道哪些專案與模型正在使用。
- 不知道哪些節點已經 Cache。
- 不知道是否可以安全刪除。

## 4.6 底層架構仍有合併前修正

本機 regression baseline 已完成 Audit Export active-lease race、migration
content checksum 與 migration ledger-gap 的修正。其餘項目必須區分
「本機程式存在」與「外部 gate 已完成」：

- Audit Export Active Lease 提前 Dead-letter 的競態：已修正，保留 regression gate。
- Migration Checksum 未真正綁定 Migration 內容：已修正，保留 regression gate。
- Migration Ledger Gap：已修正，保留 fail-closed regression gate。
- Node Protocol 缺少 Version Header 的政策。
- Audit Adoption Catalog 覆蓋不足。
- Artifact Metadata Immutable Insert-or-Verify。
- Node Workload Isolation 已有本機 contract／程式／integration evidence，
  尚未取得正式 Node Canary。
- External Audit Anchor 尚未完成。

---

# 5. 改良後的目標產品

改良後的 Dispatch Center 應以四個核心工作區組成：

```text
我的工作台
Project Workspace
Run Workspace
Dataset Workspace
```

另由管理者使用：

```text
Approval Center
Resource Administration
Node／Server Operations
Audit／System Health
```

## 5.1 目標產品描述

> Dispatch Center 是一套讓團隊使用公司帳號登入，以專案為中心管理程式碼、執行環境、Dataset、運算工作、核准與產出物的內部 AI／GPU 工作平台。

## 5.2 產品設計原則

1. 人員登入與機器 Token 分離。
2. 一般使用者以 Project 和 Run 為中心。
3. 技術底層留給管理者展開查看。
4. 每次 Run 都必須可重現。
5. Dataset Version 不可變。
6. 高風險操作仍需核准。
7. 安全控制不可為了方便而繞過。
8. 常用操作應由 Template 與 Default 自動完成。
9. 使用者應在提交前看懂實際執行內容。
10. 所有關鍵操作可追蹤、可復原、可稽核。

---

# 6. 改良計畫一：正常登入與身分管理

## 6.1 人員登入改用 OIDC／SSO

建議架構：

```text
Identity Provider
→ OIDC Authorization Code Flow
→ Dispatch Center Callback
→ Server-side Session
→ HttpOnly Secure Cookie
```

可整合：

- Microsoft Entra ID。
- Google Workspace。
- Keycloak。
- Authentik。
- 其他支援 OIDC 的公司身分平台。

## 6.2 身分類型分流

| 身分類型 | 認證方式 |
|---|---|
| 一般使用者 | OIDC + Browser Session |
| 管理者 | OIDC + MFA／Passkey |
| CLI 使用者 | Device Flow 或短效 Token |
| CI/CD | Service Account |
| Node Agent | Node Credential |
| Worker | Service／Workload Identity |

核心原則：

```text
Human Login ≠ API Token
```

## 6.3 Session 安全

Session Cookie 應使用：

- `HttpOnly`。
- `Secure`。
- `SameSite=Lax` 或更嚴格設定。
- Inactivity Timeout。
- Absolute Lifetime。
- Server-side Revocation。
- Login 後 Session ID Rotation。
- Logout 後立即失效。

前端不得將登入 Token 寫入：

- `localStorage`。
- `sessionStorage`。
- URL Query。
- HTML Source。

## 6.4 使用者與群組同步

建議支援：

- 首次登入自動建立 User Profile。
- Email／Subject Identifier 綁定。
- Identity Provider Group 對應平台角色。
- 離職或停權後禁止新 Session。
- 管理者可查看登入紀錄與 Session。
- 不允許以 Email 作為唯一不可變 Identity Key。

## 6.5 驗收標準

- 一般使用者不需要手動 Token 登入。
- Service Token 與 Node Credential 不受影響。
- Logout 能撤銷 Session。
- Session 過期後不可繼續使用。
- Authorization 仍以平台內角色與 Scope 決定。
- OIDC 失敗不會退回不安全的匿名模式。

---

# 7. 改良計畫二：我的工作台

登入後首頁建議顯示：

- 我的專案。
- 收藏專案。
- 最近 Run。
- Running／Failed／Needs Attention。
- 待我核准。
- 最近 Dataset。
- 最近 Artifact。
- 系統通知。
- 我的資源使用量。

## 一般使用者導覽

```text
首頁
我的專案
執行紀錄
Datasets
核准中心
```

## Platform Admin 額外導覽

```text
Servers
Nodes
Workers
Service Accounts
Audit
System Health
Settings
```

## 驗收標準

- 一般使用者不會先看到 Token、Node Credential 或 Outbox。
- 使用者能在兩次點擊內進入最近使用的專案。
- 使用者能立即看到失敗、待核准與進行中的工作。
- 導覽項目依角色顯示。

---

# 8. 改良計畫三：Project Workspace

每個專案提供：

```text
/projects/{project_id}
```

建議頁籤：

```text
Overview
Runs
Datasets
Environments
Templates
Artifacts
Members
Activity
Settings
```

## 8.1 Overview

顯示：

- 專案名稱與說明。
- Owner／Team。
- Git Repository。
- 目前預設 Version。
- Default Environment。
- Default Dataset Alias。
- Default Run Template。
- 最近 Run。
- 最近 Artifact。
- 最近錯誤。
- 可用運算資源摘要。

## 8.2 Runs

顯示此專案的：

- Draft。
- Awaiting Approval。
- Queued。
- Preparing。
- Running。
- Succeeded。
- Failed。
- Needs Attention。
- Cancelled。

## 8.3 Datasets

顯示：

- 已連結 Dataset。
- Default Dataset。
- 使用中的 Version。
- Cache／Prewarm 狀態。
- 最近由此專案產生的 Dataset。

## 8.4 Environments

管理：

- Python／Conda Environment。
- Container Image。
- Dependency Lock。
- Setup Command。
- Working Directory。
- Environment Variable Schema。
- Secret Reference。
- GPU／CUDA Requirement。

## 8.5 Templates

管理：

- Training。
- Evaluation。
- Inference。
- Data Preparation。
- Dataset Build。
- Coding Task。
- Custom Command。

## 8.6 Artifacts

集中顯示：

- Model Checkpoint。
- Metrics。
- Evaluation Report。
- Logs。
- Diff／Patch。
- Dataset Manifest。
- Export Bundle。

## 8.7 Members

建議角色：

| 角色 | 能力 |
|---|---|
| Owner | 管理專案、成員與所有設定 |
| Operator | 建立、執行與停止 Run |
| Reviewer | 核准高風險操作 |
| Dataset Manager | 建立與發布 Dataset |
| Viewer | 查看專案、Run 與 Dataset |

## 8.8 Activity

顯示完整時間軸：

```text
Project Created
Member Added
Run Submitted
Approval Decided
Attempt Started
Dataset Version Used
Artifact Published
Run Completed
```

## 8.9 驗收標準

- 專案相關資料不再分散於多個全域頁面。
- 使用者能從 Project Workspace 建立、查看與重跑工作。
- 所有 Default 都有明確 Revision 或 Alias。
- 使用者只看到有權限的專案資料。

---

# 9. 改良計畫四：建立專案 Wizard

## Step 1：基本資料

- 名稱。
- 說明。
- Owner。
- Team。
- Tags。

## Step 2：程式碼來源

選擇：

```text
連接 Git Repository
從既有伺服器路徑匯入
建立空白專案
從組織範本建立
```

## Step 3：執行環境

選擇：

- Python。
- Conda。
- Docker／OCI Image。
- Existing Server Environment。
- Custom Setup。

## Step 4：預設資源

例如：

```text
CPU: 4
RAM: 16 GB
GPU: 1
GPU Memory: 24 GB
Timeout: 8 Hours
```

## Step 5：Dataset

選擇：

- 連結現有 Dataset。
- 建立新 Dataset。
- 稍後設定。

## Step 6：成員與權限

設定：

- Owner。
- Operator。
- Reviewer。
- Viewer。
- Dataset Manager。

## Step 7：系統驗證

自動檢查：

- Repository 可讀。
- Environment 可建立。
- Dataset 可存取。
- 至少一台資源符合需求。
- Secret 未直接寫入設定。
- 使用者具備建立專案權限。

## 建立完成後自動產生

- Project。
- Owner Membership。
- Default Environment。
- Default Resource Profile。
- Default Run Template。
- Initial Activity Event。

---

# 10. 改良計畫五：One-click Run

## 10.1 使用者操作

```text
選擇 Run Template
→ 選 Dataset Version
→ 填寫參數
→ 檢視執行預覽
→ 提交
```

## 10.2 使用者可見欄位

### 執行類型

- Training。
- Evaluation。
- Inference。
- Data Preparation。
- Dataset Build。
- Custom Command。

### 參數

例如：

- Epochs。
- Batch Size。
- Dataset Version。
- Model Version。
- Output Name。
- Priority。

### 資源需求

預設由 Template 帶入：

```text
CPU: 4
RAM: 16 GB
GPU: 1
Timeout: 8 Hours
```

一般使用者預設選擇：

```text
自動選擇執行位置
```

只有管理者或進階使用者才展開：

- Server。
- Node。
- GPU Type。
- Backend。
- Placement Constraint。

## 10.3 系統背後自動完成

```text
解析 Template Revision
→ 固定 Project Version
→ 固定 Dataset Version
→ 固定 Environment Revision
→ 驗證 Parameters
→ 建立 Approval Payload
→ 計算 Command／Payload Digest
→ 選擇資源
→ 建立 Execution Plan
→ 建立 Attempt
→ 使用 SSH 或 Node 執行
```

## 10.4 執行預覽

提交前顯示：

```text
Project: image-classification
Project Version: commit abc123
Template: training-default@rev-8
Dataset: animals@version-12
Environment: cuda-training@rev-7
Resources: 1 × A100, 32 GB RAM
Placement: Automatic
Approval Required: Yes
```

## 10.5 驗收標準

- 一般使用者不需要手動輸入完整 Shell Command。
- 一次提交固定 Project、Dataset、Environment 與 Template Revision。
- 提交前可清楚預覽實際內容。
- 需要 Approval 時不會直接執行。
- 重跑時能顯示與原 Run 的差異。

---

# 11. 改良計畫六：Run Template

範例：

```yaml
name: training-default
type: training

command:
  - python
  - train.py

parameters:
  epochs:
    type: integer
    default: 20
    minimum: 1

  batch_size:
    type: integer
    default: 32
    minimum: 1

  dataset:
    type: dataset_version

resources:
  cpu: 4
  memory_gb: 16
  gpu_count: 1

timeout_minutes: 480

outputs:
  - checkpoints/**
  - metrics.json
```

Template 功能應包含：

- Immutable Revision。
- Draft／Published。
- Project Template。
- Organization Template。
- Parameter Schema。
- Default Value。
- Validation Rule。
- Resource Profile。
- Dataset Input Declaration。
- Artifact Output Declaration。
- Approval Requirement。
- Allowed Environment。
- Allowed Server／Node Tag。

---

# 12. 改良計畫七：Project Environment

Environment 應成為一級資源，包含：

```text
Environment ID
Revision
Python Version
Container Image
Dependency Lock Digest
Setup Command
Working Directory
Environment Variable Schema
Secret References
Required Capabilities
Created By
Validation Status
```

## Environment Validation

提供 `Validate Environment`：

- Python Import Check。
- CUDA Check。
- Container Pull Check。
- Disk Check。
- Repository Checkout Check。
- Dataset Mount Check。
- Dependency Digest Check。
- Read／Write Permission Check。

Run 應引用：

```text
environment_revision_id
```

不能只引用可被直接修改的 Environment 名稱。

---

# 13. 改良計畫八：Dataset Workspace

每個 Dataset 提供：

```text
/datasets/{dataset_id}
```

建議頁籤：

```text
Overview
Versions
Lineage
Usage
Storage
Data Card
Activity
```

## 13.1 Overview

顯示：

- 名稱。
- 說明。
- Owner。
- 最新 Version。
- Default／Production Alias。
- Size。
- File Count。
- Format／Schema。
- Storage Location。
- 使用中的專案。
- Validation Status。

## 13.2 Immutable Versions

每個 Version 包含：

- Dataset Version ID。
- Sequence Number。
- Digest。
- Size。
- File Count。
- Manifest。
- Created By。
- Created At。
- Parent Version。
- Production Method。
- Storage Locations。

已發布 Version 不可覆寫。

## 13.3 Alias

使用 Alias 取代混亂的字串版本：

```text
production → version 12
training-default → version 10
archived → version 6
```

改 Alias 不會修改 Immutable Version。

## 13.4 Lineage

顯示：

```text
raw-images@v1
→ cleaned-images@v2
→ labeled-images@v5
→ train-split@v3
```

記錄：

- Parent Dataset Version。
- 產生它的 Run。
- Project Commit。
- Environment Revision。
- Parameters。
- Transformation Artifact。

## 13.5 Usage

顯示：

- 哪些 Project 使用。
- 哪些 Run 使用。
- 哪些 Model／Artifact 引用。
- 最後使用時間。
- 是否可刪除。
- Retention 狀態。

## 13.6 Storage／Cache

顯示：

```text
Central Storage: Available
worker-01: Cached
worker-02: Not Cached
worker-03: Syncing
```

支援：

- Cache Request。
- Prewarm Proposal。
- Sync Progress。
- Checksum Verification。
- Eviction Policy。

---

# 14. 改良計畫九：建立 Dataset Wizard

## Step 1：資料來源

```text
Server Path
Upload
Object Storage
另一個 Dataset
Run Output
Git LFS／DVC Reference
```

## Step 2：基本資料

- 名稱。
- 說明。
- Owner。
- Tags。
- Format。

## Step 3：掃描

系統自動計算：

- Size。
- File Count。
- Extension Distribution。
- Basic Schema。
- Digest。
- Missing File。
- Duplicate Candidate。
- Invalid Path。

## Step 4：Data Card

填寫：

- 收集方式。
- 處理方式。
- 授權。
- 使用限制。
- 敏感資料。
- Known Issues。
- Recommended Use。
- Counts。

## Step 5：Snapshot

產生 Immutable Manifest。

## Step 6：Publish

必要時建立 Approval，核准後發布 Dataset Version。

---

# 15. 改良計畫十：可重現 Run Contract

每次 Run 必須固定：

```text
Project Version
Environment Revision
Dataset Versions
Run Template Revision
Parameters
Resource Request
Approval Reference
Execution Plan
```

Run 頁面顯示：

```text
Inputs
├── project: recommender@abc123
├── environment: cuda-training@rev-7
└── dataset: user-events@version-18

Outputs
├── model.pt
├── metrics.json
└── evaluation-report.html
```

這使系統可以回答：

- 模型使用哪個 Dataset？
- Dataset 當時是哪一版？
- 使用哪個 Commit？
- 使用什麼參數？
- 使用哪個 Environment？
- 在哪台機器執行？
- 由誰提出與核准？
- 產出哪些 Artifact？

---

# 16. 改良計畫十一：Run 與底層 Job 分層

產品層使用 `Run`，底層保留精確執行模型：

```text
Run
├── Approval
├── Execution Plan
├── Attempt 1
├── Attempt 2
├── Operations
└── Artifacts
```

一般使用者看到：

```text
Training Run #184
```

管理者可以展開：

- Attempt。
- Lease。
- Backend。
- Operation Delivery。
- Recovery State。
- Fencing Token。

## 建議產品狀態

```text
Draft
Awaiting Approval
Queued
Preparing
Running
Stopping
Succeeded
Failed
Needs Attention
Cancelled
```

---

# 17. 改良計畫十二：重新執行與複製

Run 頁面提供：

- 使用相同設定重新執行。
- 複製並修改。
- 只修改 Dataset。
- 只修改參數。
- 使用最新 Project Version。
- 使用最新 Environment Revision。

系統必須顯示差異：

```text
Original Run
Project: abc123
Dataset: version 4
Environment: revision 6

New Run
Project: def456
Dataset: version 5
Environment: revision 7
```

不得將不同 Revision 的執行描述為完全相同重跑。

---

# 18. 底層安全與可靠性改良

產品體驗改良不能取代底層修正。以下項目應同步完成。

## 18.1 Audit Export Lease Race

- Active Processing Lease 不得被另一 Worker 提前 Dead-letter。
- Processing Row 只有 Lease 到期後才可轉移或 Dead-letter。
- 使用兩個 DB Connection 測試 Race。

## 18.2 Migration Content Checksum

- Checksum 必須綁定 Migration Artifact 內容。
- 不可只使用 Function Name。
- 既有 DB 遇到 Drift 時 Fail Closed。

## 18.3 Migration Ledger Gap

合法：

```text
1
1, 2
1, 2, 3
```

不合法：

```text
2
1, 3
1, 2, 4
```

Readiness 與 DB Check 應拒絕不連續 Ledger。

## 18.4 Node Protocol Version

- 真實 Node HTTP Route 必須要求 Protocol Header。
- Missing／Incompatible Version 回 `426`。
- 過渡相容模式必須明確開啟且有退場日期。

## 18.5 Audit Adoption Catalog

- 所有 Mutation 都必須有 Machine-readable Entry。
- Legacy Entry 必須有 Owner 與 Migration Issue。
- 新 Mutation 未登錄時 CI Fail。

## 18.6 Artifact Immutability

採用 Insert-or-Verify：

```text
不存在 → Insert
完全一致 → Idempotent Success
Digest／Size／Kind 衝突 → Reject
```

## 18.7 Node Workload Isolation

- 建立 Direct／Systemd Launcher Interface。
- Canary／Production 使用 Systemd Transient Unit。
- Workload 不能取得 Node Token。
- Workload 不能改寫 Control Evidence。
- Stop 必須終止整個 Cgroup。
- 不支援 Isolation 時 Fail Closed。

## 18.8 External Audit Anchor

- 定期產生 Signed Checkpoint。
- 保存於 Off-host Immutable Storage。
- Restore Drill 驗證 External Anchor。

---

# 19. 建議資料模型新增或調整

## Identity／Session

```text
users
identity_links
browser_sessions
service_accounts
service_tokens
project_memberships
```

## Project Experience

```text
project_defaults
project_environments
environment_revisions
run_templates
run_template_revisions
resource_profiles
```

## Run Experience

```text
runs
run_inputs
run_parameters
run_outputs
run_comparisons
```

現有 Job／Attempt 可保留為底層執行模型。

## Dataset

```text
datasets
dataset_versions
dataset_aliases
dataset_manifests
dataset_lineage
dataset_project_links
dataset_usage
dataset_cache_locations
```

## Activity／Notification

```text
activity_events
notifications
user_favorites
recent_items
```

所有 Schema 變更必須使用 Versioned Migration。

---

# 20. API 與頁面建議

> 本節 endpoint 名稱是早期 Product Brief 範例，不是 public contract。
> Product v2 使用 additive `/api/v2`，精確 route、error、pagination 與
> compatibility contract 以 execution plan §10 為準。

## Authentication

```text
GET  /auth/login
GET  /auth/callback
POST /auth/logout
GET  /api/me
GET  /api/me/sessions
DELETE /api/me/sessions/{session_id}
```

## Project Workspace

```text
GET  /api/projects/{project_id}/workspace
GET  /api/projects/{project_id}/runs
GET  /api/projects/{project_id}/datasets
GET  /api/projects/{project_id}/activity
GET  /api/projects/{project_id}/members
```

## Run

```text
POST /api/projects/{project_id}/runs/preview
POST /api/projects/{project_id}/runs
GET  /api/runs/{run_id}
POST /api/runs/{run_id}/clone
POST /api/runs/{run_id}/stop
GET  /api/runs/{run_id}/artifacts
```

## Run Template

```text
GET  /api/projects/{project_id}/run-templates
POST /api/projects/{project_id}/run-templates
POST /api/run-templates/{template_id}/publish
GET  /api/run-template-revisions/{revision_id}
```

## Dataset

```text
GET  /api/datasets
POST /api/datasets
GET  /api/datasets/{dataset_id}
POST /api/datasets/{dataset_id}/versions/scan
POST /api/datasets/{dataset_id}/versions
POST /api/datasets/{dataset_id}/aliases/{alias}
GET  /api/datasets/{dataset_id}/lineage
GET  /api/datasets/{dataset_id}/usage
GET  /api/datasets/{dataset_id}/storage
```

API 具體名稱可依現有 Router 與 Schema 調整，重點是以 Workspace 與 Use Case 為中心，不要單純暴露資料表 CRUD。

---

# 21. 開發分期

> 本節舊分期保留作為 UX roadmap 歷史。實作不得依此跳過依賴或改變範圍；
> 工作包與 PR 順序以 Product v2 execution plan §12 為準。

## Phase 0：安全基線與 PR 整理

目標：讓目前重構可以安全審查與合併。

工作：

1. 拆分過大的 Draft PR。
2. 修正 Audit Export Lease Race。
3. 修正 Migration Checksum。
4. 增加 Migration Ledger Gap 檢查。
5. 固定 Node Protocol Missing Header 行為。
6. 完成 Audit Adoption Catalog 基礎。
7. 設定 Required CI 與 Reviewer Gate。

完成標準：

- Required CI 全綠。
- PR 非 Draft。
- Reviewer Approval。
- 無未解決安全問題。

## Phase 1：OIDC 登入與首頁

目標：移除一般使用者 Token Login。

工作：

1. OIDC Authorization Code Flow。
2. Server-side Browser Session。
3. Logout／Session Revoke。
4. User Profile 與 Role Mapping。
5. 我的工作台。
6. 依角色顯示導覽。
7. 保留 Service／Node Token。

完成標準：

- 使用者以公司帳號登入。
- Browser 不保存 API Token。
- 所有既有 Authorization Test 維持通過。

## Phase 2：Project Workspace

目標：讓專案成為操作中心。

工作：

1. Project Overview。
2. Project Runs。
3. Project Datasets。
4. Project Members。
5. Project Activity。
6. Project Defaults。
7. 建立專案 Wizard。

完成標準：

- 使用者可在單一 Workspace 完成主要專案操作。
- 專案資料具備完整 Scope Filtering。

## Phase 3：One-click Run

目標：降低每次執行的設定成本。

工作：

1. Run Template／Revision。
2. Parameter Schema。
3. Resource Profile。
4. Environment Revision。
5. Run Preview。
6. Approval Integration。
7. Re-run／Clone／Compare。
8. Product Run State。

完成標準：

- 常用任務不需手動輸入完整 Command。
- Run Contract 固定所有輸入 Revision。

## Phase 4：Dataset Workspace

目標：將 Dataset 從 Path 升級為可治理資產。

工作：

1. Immutable Dataset Version。
2. Manifest 與 Digest。
3. Dataset Alias。
4. Lineage。
5. Usage。
6. Project Link。
7. 建立 Dataset Wizard。
8. Cache／Prewarm Status。
9. Data Card。

完成標準：

- Published Version 不可覆寫。
- 每個 Run 都引用明確 Dataset Version。
- 可從 Dataset 查回所有相關 Run 與 Project。

## Phase 5：Node Runtime Isolation 與 Canary

目標：讓 Node Backend 可進行受控實際部署。

工作：

1. Workload Launcher Interface。
2. Systemd Transient Unit。
3. Workload／Control Evidence 分離。
4. Agent Preflight。
5. 真實 Linux Integration Test。
6. Single-job Smoke。
7. Formal Canary。
8. Restart／Response-loss／Rollback Drill。

完成標準：

- Workload 無法取得 Node Credential。
- Stop 可終止整個 Cgroup。
- SSH 保持有效回退路徑。

## Phase 6：Audit、維運與 Production Gate

目標：支援受控正式上線。

工作：

1. Full-domain Durable Audit Migration。
2. External Audit Anchor。
3. Dead-letter Alert。
4. Backup／Restore Drill。
5. Metrics、Logs、Traces。
6. SLO 與 Runbook。
7. Signed Release Artifact。
8. Production Rollout Gate。

---

# 22. 建議 PR 切片

建議保持小型、可審查、可回退：

```text
PR-01 OIDC Session Foundation
PR-02 User Profile and Role Mapping
PR-03 My Workspace Navigation
PR-04 Project Workspace Read Model
PR-05 Project Creation Wizard
PR-06 Environment Revision
PR-07 Run Template Revision
PR-08 Run Preview and Submit
PR-09 Run Clone and Compare
PR-10 Dataset Immutable Version
PR-11 Dataset Alias and Lineage
PR-12 Dataset Workspace
PR-13 Dataset Cache and Prewarm
PR-14 Node Runtime Isolation
PR-15 Formal Node Canary
PR-16 External Audit Anchor
```

每個 PR 必須包含：

- Scope。
- Non-goals。
- Migration Impact。
- Security Impact。
- Test Commands。
- 真實 Test Result。
- Rollback。
- 文件更新。

---

# 23. 優先級建議

## 立即處理

1. Audit Export Lease Race。
2. Migration Checksum。
3. Migration Ledger Gap。
4. Node Protocol Header Policy。
5. Audit Adoption Catalog。
6. Required CI 與 PR 拆分。

## 最高產品優先級

1. OIDC／SSO 登入。
2. 我的工作台。
3. Project Workspace。
4. Run Template。
5. One-click Run。
6. Immutable Dataset Version。
7. Dataset Workspace。

## Canary 前

1. Artifact Immutability。
2. Node Runtime Isolation。
3. Control Evidence 分離。
4. Agent Preflight。
5. 真實 Linux Integration Test。

## Production 前

1. Full-domain Durable Audit。
2. External Audit Anchor。
3. Formal Node Canary。
4. Backup／Restore Drill。
5. Alert、SLO 與 Runbook。

---

# 24. 產品成效指標

## 登入

- 一般使用者 Token Login 使用率降至 0%。
- 登入成功率。
- Session Error Rate。
- 平均登入時間。

## 專案管理

- 使用者找到專案的平均時間。
- 從登入到進入 Project Workspace 的操作次數。
- 建立專案完成率。
- 專案設定錯誤率。

## 執行體驗

- 從進入專案到提交 Run 的平均時間。
- 使用 Template 建立 Run 的比例。
- 因參數或環境錯誤造成的失敗率。
- Re-run／Clone 使用率。
- Needs Attention 平均處理時間。

## Dataset

- 未綁定明確 Version 的 Run 數量應為 0。
- Published Dataset Version 覆寫事件應為 0。
- Dataset Lineage 完整率。
- Cache Hit Rate。
- 因 Dataset 缺失造成的失敗率。

## 安全與可靠性

- Unauthorized Access 數量。
- Duplicate Execution 數量。
- Audit Dead-letter 數量與處理時間。
- Recovery 成功率。
- Backup Restore Drill 成功率。

---

# 25. Definition of Done

## OIDC Login

- [ ] 人員可以使用公司帳號登入。
- [ ] Browser 使用安全 Session Cookie。
- [ ] Logout 可撤銷 Session。
- [ ] Service／Node Token 保持獨立。
- [ ] Authorization Regression Test 通過。

## Project Workspace

- [ ] Overview、Runs、Datasets、Members、Activity 可用。
- [ ] Project Scope Filtering 完整。
- [ ] Project Default 有明確 Revision／Alias。
- [ ] 一般使用者不需要進入 Server／Node 頁面即可執行。

## One-click Run

- [ ] 可選 Template。
- [ ] 可選 Dataset Version。
- [ ] Parameter Validation 完整。
- [ ] 提交前有 Run Preview。
- [ ] Run 固定 Project／Environment／Dataset／Template Revision。
- [ ] Approval 無法被繞過。

## Dataset Workspace

- [ ] Dataset Version 不可變。
- [ ] Alias 與 Version 分離。
- [ ] Manifest／Digest 可驗證。
- [ ] Lineage 可查詢。
- [ ] Usage 可追蹤。
- [ ] 每個 Run 引用明確 Version。

## Production Readiness

- [ ] Node Runtime Isolation 真正接線。
- [ ] Formal Canary 通過。
- [ ] SSH Rollback Drill 通過。
- [ ] Full-domain Durable Audit 完成。
- [ ] External Audit Anchor 啟用。
- [ ] Required CI、Review、Backup 與 Alert Gate 生效。

---

# 26. 最終理想使用情境

## 一般使用者

```text
使用公司帳號登入
→ 看到我的專案
→ 進入 image-classification
→ 點擊「新增執行」
→ 選 Training Template
→ 選 animals Dataset version 12
→ 將 epochs 改為 50
→ 查看 Project、Dataset、Environment、資源與核准預覽
→ 提交
→ Reviewer 核准
→ 系統自動選擇 GPU Node
→ 查看即時狀態與 Log
→ 完成後查看 Metrics、Model 與 Artifact
```

## Dataset 管理者

```text
登入
→ 進入 Dataset Workspace
→ 從某次 Run Output 建立新版本
→ 系統掃描、計算 Digest 與 Manifest
→ 填寫 Data Card 與變更說明
→ 提交發布核准
→ 發布 version 13
→ 將 production Alias 指向 version 13
→ 新 Run 預設使用 version 13
```

## 專案管理者

```text
登入
→ 建立專案
→ 連接 Git Repository
→ 選擇 Environment Template
→ 指定 Default Dataset
→ 加入成員與 Reviewer
→ 驗證可用資源
→ 發布專案
→ 團隊直接使用 Run Template 執行
```

---

# 27. 最終結論

目前的 Dispatch Center 已具備一套安全派工控制平台所需的大部分底層概念，包括核准、權限、Execution Attempt、SSH／Node Backend、Migration、Audit 與 Recovery。

下一階段不應只繼續增加底層管理端點，而應優先將能力整合成使用者可理解的產品流程。

最重要的改良順序是：

```text
先修正安全與資料一致性基線
→ OIDC 正常登入
→ 我的工作台
→ Project Workspace
→ Run Template 與 One-click Run
→ Immutable Dataset Version 與 Dataset Workspace
→ Node Runtime Isolation 與 Canary
→ 完整 Audit 與 Production Gate
```

完成後，Dispatch Center 可以從：

> 管理遠端工作與節點的工程工具

提升為：

> 團隊每天可使用、以專案為中心、具備正常登入、一鍵執行、Dataset 版本治理、人工核准、可重現結果與完整稽核能力的內部 AI／GPU 運算平台。
