# Dispatch Center 現有系統修改計畫書
## 從 Product v2 改造成 Single-User Web AI/ML Engineering Platform

**文件類型：現況改造 / Implementation Roadmap**  
**基線：目前 main 已有 Product v2 One-click Run；existing instance exact update 尚未合併。**  
**改造主軸：Project Onboarding → Web Remote Codex → Experiment → Optimization Loop**

---

# 1. 改造目標

目前 Dispatch Center 已經具備成熟的：

- Server
- SSH execution
- Scheduler
- Approval
- Audit
- Project
- ProjectVersion
- Project Instance
- Dataset
- Environment
- Run Template
- ExecutionPlan v2
- Product Run
- One-click Run
- Artifact metadata
- Engineering Task
- Codex one-shot provider

下一步不重寫底層。

目標是：

> **把現有 Control Plane 改造成一個使用者能完全從網站完成 Project 開發與模型 Experiment 的產品。**

---

# 2. 現況基線

目前主分支已有：

```text
Product v2 Workspace
Project RBAC
OIDC
Project Bootstrap
Environment
Run Template
Dataset Assets
Dataset Publish
ExecutionPlan v2
Product Run
Clone
Compare
Stop
Artifacts
One-click Run
```

One-click Run 已能：

```text
ProjectVersion
Template
SSH target
Parameter overrides
Dataset binding
Preview
Approval
```

這些全部應沿用。

---

# 3. 主要缺口

目前還需要補：

```text
Existing Project Normalize Wizard
GitHub Onboarding
Interactive Codex Login
Persistent Codex Conversation
Remote Codex Operations
Remote Workspace
Codex multi-turn
Command approval callback
Experiment domain
Parameter Matrix
Auto Placement UX
Experiment metrics
Codex experiment analysis
```

---

# 4. 暫停擴張的領域

近期降低優先級：

```text
Multi-user UI
Claude Code
Multi-Agent
A2A
Node production rollout
new scheduler abstractions
new audit abstractions
enterprise workflow complexity
```

不是刪除，只是先不當產品主線。

---

# 5. 第一個必要動作：完成 Existing Instance Update

先完成：

```text
ProjectVersion
→ Existing Project Instance
```

流程：

```text
Preview
→ Approval
→ exact version update
→ verify HEAD
```

這是讓固定版本真正能安全跑在不同 Server 的必要基礎。

---

# 6. Instance Update 不可破壞的 invariant

- exact promoted ProjectVersion
- exact ServerConfig revision
- clean checkout
- no active work
- no arbitrary client path
- no arbitrary command
- no auto reset
- no auto stash
- no auto merge
- response loss 不 blind replay

---

# 7. Phase A — Single User Product Profile

新增 deployment profile：

```text
trusted_single_user
```

推薦：

```text
OIDC enabled
authorization enforce
high-risk self approval allowed
legacy token hidden
```

---

# 8. UI 簡化

單人模式不把以下放主畫面：

```text
Member Management
Reviewer Assignment
Role Matrix
```

底層模型保留。

主導航改成：

```text
Projects
AI
Experiments
Runs
Servers
Settings
```

---

# 9. Project Workspace 重整

建議：

```text
Overview
AI Engineer
Code
Experiments
Runs
Datasets
Artifacts
Settings
```

不要讓一般工作流以：

```text
ExecutionPlan
Attempt
Operation
Fencing
Outbox
```

作為主要概念。

---

# 10. Phase B — Project Onboarding v1

新增：

```text
New Project
Import from Server
Import from GitHub
```

---

# 11. 重用 Existing Inventory

直接使用目前 read-only Inventory。

不要重寫 Scanner。

補 Product UI：

```text
Server
Project Candidate
Git status
Git remote
size
dataset detected
secret warning
```

---

# 12. Project Candidate Detail

新增 read model：

```text
GET /api/v3/project-candidates/{id}
```

包含：

- server
- safe path label / opaque id
- git state
- safe git remote
- dependency files
- marker files
- dataset summary
- warnings

避免不必要 absolute path exposure。

---

# 13. Normalize Preview

新增：

```text
POST /api/v3/project-candidates/{id}/normalize-preview
```

零寫入。

輸出：

```text
Git action
.gitignore proposal
dispatch.yaml proposal
dependency proposal
dataset separation proposal
output separation proposal
GitHub action
warnings
```

---

# 14. Codex Normalize

新增 Engineering Task 類型：

```text
project_normalization
```

第一版可以先用現有 one-shot Codex：

```text
Candidate staging copy
→ Codex analysis
→ structured normalization result
```

不用等 interactive Codex才能開始。

---

# 15. Normalize Apply

新增：

```text
POST /api/v3/project-candidates/{id}/normalize-request
```

只允許 reviewed transformation：

- `.gitignore`
- README
- dispatch.yaml
- approved path moves
- dependency metadata
- Git init

禁止：

- 讀 secret
- 自動上傳 dataset
- 自動上傳 checkpoint
- 自動刪除原 Project

---

# 16. GitHub Onboarding

No repository：

```text
Git Init
→ Initial Commit
→ Create GitHub Repo
→ Push
→ Register Project
```

Existing repository：

```text
Verify Remote
→ Link
→ Preserve History
```

---

# 17. GitHub Credential

新增：

```text
Connect GitHub
```

Credential只以 secure reference 存取。

不能進：

```text
Codex prompt
Engineering Task payload
Run payload
logs
```

---

# 18. 接回 Existing Project Bootstrap

Normalize完成後直接接：

```text
Project Bootstrap
Environment
Run Template
Defaults
```

不要建立第二套 Project model。

---

# 19. Phase C — Codex Connection

新增：

```text
AI Provider Connection
```

第一版只有 Codex。

資料：

```text
ai_connections
id
user_id
provider
credential_ref
status
account_label
last_verified_at
```

---

# 20. Codex Login UI

Settings：

```text
Codex
[Connect]
[Disconnect]
```

流程：

```text
start auth
→ user authorization
→ provider credential storage
→ connected
```

Credential與 Session資料分離。

---

# 21. 保留 Existing Codex Exec

保留：

```text
codex-exec-v1
```

用途：

- one-shot tasks
- batch normalization
- rollback
- compatibility

不要刪除。

---

# 22. 新增 Interactive Provider

新增：

```text
codex-interactive-v1
```

能力：

```text
start_turn=true
resume_turn=true
cancel_turn=true
event_stream=true
command_approval_callback=true
```

---

# 23. Interactive Provider 啟用條件

必須通過：

- pinned Codex version
- account login test
- session start
- resume
- cancel
- event normalization
- approval callback
- credential redaction
- dev-server real canary
- rollback to codex-exec-v1

---

# 24. Phase D — AI Conversation Persistence

新增：

```text
ai_conversations
ai_messages
ai_sessions
ai_events
```

---

# 25. AIConversation

```text
id
project_id
title
state
created_at
updated_at
```

---

# 26. AISession

```text
id
conversation_id
provider
provider_session_id
server_id
project_instance_id
worktree_id
base_revision
state
```

---

# 27. AIMessage

```text
id
conversation_id
role
safe_content
created_at
```

---

# 28. AIEvent

```text
id
session_id
turn_id
type
payload_ref
created_at
```

---

# 29. Normalized Event Types

```text
user_message
assistant_message
command_requested
command_started
command_output
command_finished
file_changed
test_started
test_finished
git_checkpoint
warning
error
```

---

# 30. Phase E — AI Engineer UI

Project新增：

```text
AI Engineer
```

畫面：

```text
Conversation
Current Server
Current Revision
Current Worktree
Files Changed
Tests
Pending Action
```

---

# 31. Remote Server Selector

```text
Current Server: gpu-03
[Change]
```

Codex可以透過 Tool Request切換 Server。

---

# 32. Phase F — Remote Workspace

新增：

```text
coding_workspaces
```

資料：

```text
id
project_id
project_version_id
server_id
worktree_ref
branch
state
owner_conversation_id
created_at
```

---

# 33. Workspace 建立流程

```text
ProjectVersion
→ target Server
→ verify repo
→ create isolated worktree
→ verify HEAD
→ register workspace
```

---

# 34. Workspace 禁止事項

不得：

- 直接拿 production checkout給 AI寫
- 共用 dirty workspace
- AI直接改 canonical Project Instance
- AI讀其他 Project
- client自訂 arbitrary worktree path

---

# 35. Phase G — Remote Engineering Tool Service

新增：

```text
RemoteEngineeringTools
```

第一批 read-only：

```text
list_servers
get_project_instance
list_files
read_file
search_code
git_status
git_diff
git_log
```

---

# 36. 第二批 Mutation Tools

```text
apply_patch
create_file
delete_file
run_command
run_tests
git_commit
```

每個 Tool都綁：

```text
project
server
workspace
conversation
```

---

# 37. Tool 不是 Arbitrary Shell API

Browser不能直接送：

```text
host
path
command
```

任意值。

所有操作經：

- Project scope
- Server registry
- workspace binding
- command policy
- canonical validation

---

# 38. Phase H — Command Policy Engine

新增分類：

```text
safe
confirm
credential_required
forbidden
```

---

# 39. Safe Command

例如：

```text
pytest
ruff
mypy
node --check
nvidia-smi
git status
git diff
approved smoke tests
```

---

# 40. Confirm Command

新增 approval kind：

```text
engineering_command
```

Approval綁：

```text
conversation_id
session_id
server_id
worktree_id
command_digest
cwd_digest
```

不可批准A卻執行B。

---

# 41. Credential Required

Codex只能收到：

```text
credential_required
```

UI提示使用者。

Credential輸入永遠不回傳給 Codex。

---

# 42. Phase I — Conversation → Commit

AI Engineer UI增加：

```text
View Diff
Run Tests
Commit
Discard
```

---

# 43. Git Commit Evidence

保存：

```text
commit
branch
diff artifact
test evidence
conversation reference
```

---

# 44. 接 Existing ProjectVersion Lifecycle

不要新增第二套：

```text
AIRevision
```

AI commit最後接既有：

```text
ProjectVersion
```

---

# 45. Phase J — Project Instance Sync UX

Code頁：

```text
Current Version abc123

gpu-01  Current
gpu-02  Current
gpu-03  Update Available
```

---

# 46. Bulk Sync Preview

新增：

```text
[Update Ready Instances]
```

Preview：

```text
gpu-01 no-op
gpu-02 update
gpu-03 blocked: dirty
```

再確認。

---

# 47. Phase K — Experiment Domain

新增：

```text
experiments
experiment_runs
```

不新增第二套 Run state machine。

---

# 48. Experiment Schema

```text
experiments

id
project_id
name
project_version_id
environment_revision_id
run_template_revision_id
dataset_binding_snapshot
resource_request
state
created_at
```

---

# 49. ExperimentRun Relation

```text
experiment_runs

experiment_id
execution_plan_id
matrix_index
parameter_snapshot
```

繼續使用：

```text
execution_plans.id
```

當 Product Run identity。

---

# 50. Phase L — Parameter Matrix

新增 typed matrix。

只允許 Run Template schema中的參數。

例如：

```text
learning_rate=[0.001,0.0005,0.0001]
batch_size=[16,32]
seed=[42]
```

---

# 51. Matrix Preview API

```text
POST /api/v3/experiments/preview
```

回：

```text
run_count
parameter_combinations
resource_estimate
warnings
```

零寫入。

---

# 52. Experiment Create

```text
POST /api/v3/projects/{id}/experiments
```

建立 immutable context。

---

# 53. Experiment Start

```text
Experiment
→ expand matrix
→ build N ExecutionPlan specs
→ reuse Product Run materialization
```

不要繞過現有 approval / execution。

---

# 54. Aggregate Confirmation

單人 UX可顯示：

```text
Start 12 Runs
```

一次確認。

底層仍保留每個 Run immutable evidence。

---

# 55. Phase M — Auto Placement UX

目前 One-click Run的：

```text
SSH target
```

改成：

```text
Placement
● Auto
○ Specific Server
```

Auto為預設。

---

# 56. Auto Placement Preview

顯示：

```text
6 Runs
3 Ready Servers
3 can start
3 queued
```

這只是 Preview，不取代 start-time revalidation。

---

# 57. Placement Revalidation

執行前重新檢查：

- Server revision
- target identity
- capacity
- Project Instance
- Git revision
- Environment
- Dataset
- health
- active work

---

# 58. Phase N — Metrics Contract

擴 Run Template：

```text
metrics_output
```

例如：

```text
results/metrics.json
```

---

# 59. Metrics Collection

Collect時：

```text
verify declared output
size bound
parse JSON
schema validate
persist structured metrics
link artifact
```

---

# 60. Phase O — Experiment Dashboard

功能：

- progress
- run table
- parameter columns
- metric columns
- sort
- filter
- best run
- failed run summary
- artifact links
- compare selected

---

# 61. 沿用 Existing Run Compare

不要重做 Compare engine。

增加：

```text
Compare Selected Runs
```

即可。

---

# 62. Phase P — Codex Experiment Analysis

新增 read-only tools：

```text
get_experiment_summary
get_run_metrics
get_run_logs
compare_runs
get_artifact_metadata
```

---

# 63. Ask Codex

Experiment頁：

```text
[Ask Codex]
```

Context：

```text
code revision
dataset version
environment
template
parameters
metrics
failed summaries
```

---

# 64. Next Experiment Proposal

Codex產出：

```text
reason
parameter_matrix
expected_run_count
resource_request
```

但不能直接執行。

---

# 65. Proposal Flow

```text
Codex Proposal
→ Preview
→ User Confirm
→ Experiment
```

---

# 66. Phase Q — Overview 收斂

Project Overview：

```text
Current Code
Codex Status
Active Experiment
Running Runs
Server Instance Health
Best Recent Run
Default Dataset
```

---

# 67. Operations 收斂

Operations只保留：

```text
Server Health
Queue
Worker
Audit
Backup
Recovery
```

日常 Project UX不顯示內部執行細節。

---

# 68. API Strategy

保留：

```text
/api/v2
```

既有 Product能力。

新 AI / Experiment可使用：

```text
/api/v3
```

不要為版本號重寫 Product v2。

---

# 69. Feature Flags

```text
SINGLE_USER_PRODUCT_MODE=false
PROJECT_NORMALIZE_V1=false
GITHUB_ONBOARDING_V1=false
AI_CONNECTIONS_V1=false
AI_CONVERSATIONS_V1=false
CODEX_INTERACTIVE_V1=false
REMOTE_ENGINEERING_TOOLS_V1=false
EXPERIMENT_V1=false
EXPERIMENT_MATRIX_V1=false
EXPERIMENT_AUTO_PLACEMENT_V1=false
EXPERIMENT_METRICS_V1=false
CODEX_EXPERIMENT_ANALYSIS_V1=false
```

---

# 70. Migration 原則

全部 additive。

保留：

- old Engineering Task
- old Codex exec
- old Product Run
- old Workspace compatibility
- old SSH backend

---

# 71. 推薦 PR Roadmap

```text
PR-30  Finalize existing Project Instance exact update
PR-31  Single-user product profile + UI simplification
PR-32  Project onboarding candidate UI
PR-33  Normalize preview + Dispatch Project Contract
PR-34  GitHub onboarding
PR-35  AI Connection model + Codex login
PR-36  AI Conversation persistence
PR-37  Codex interactive provider
PR-38  Remote Workspace lifecycle
PR-39  Remote Engineering Tools read-only
PR-40  Remote Engineering mutation tools
PR-41  Command policy + approval UX
PR-42  AI Engineer Project UI
PR-43  Conversation → Commit → ProjectVersion
PR-44  Project Instance bulk sync UX
PR-45  Experiment model
PR-46  Parameter Matrix preview
PR-47  Experiment → existing Product Runs
PR-48  Auto Placement UX
PR-49  Metrics contract / collection
PR-50  Experiment Dashboard
PR-51  Codex Experiment Analysis
PR-52  Next Experiment proposal workflow
```

---

# 72. P0

```text
Instance Update
Project Onboarding
Codex Login
Interactive Conversation
Remote Workspace
Remote Tools
Commit
```

做到這裡就能「不用SSH開發」。

---

# 73. P1

```text
Experiment
Parameter Matrix
Auto Placement
Metrics
Dashboard
```

做到這裡就能「不用手動分配 Server做模型實驗」。

---

# 74. P2

```text
Codex experiment analysis
next experiment proposal
bulk instance sync
resource estimation
```

做到這裡形成 AI-assisted optimization loop。

---

# 75. P3

之後才做：

```text
Claude Code
Multi-AI
bounded autonomous optimization
Node Agent rollout
```

---

# 76. Pilot 1 — Existing Project Import

真實 legacy Project：

```text
Scan
→ Normalize
→ GitHub
→ Project
```

驗證：

- secret不讀
- dataset不誤上傳
- history正確
- ProjectVersion可建立

---

# 77. Pilot 2 — Web Remote Codex

```text
Project
→ AI Engineer
→ Codex
→ gpu-dev
→ modify
→ pytest
→ diff
→ commit
```

全程不用SSH。

---

# 78. Pilot 3 — Multi-Server Experiment

```text
commit abc123
→ 6 parameter combinations
→ 3 servers
→ results
→ compare
```

---

# 79. Pilot 4 — Optimization Loop

```text
Experiment
→ Codex Analysis
→ Next Matrix Proposal
→ Confirm
→ Second Experiment
```

---

# 80. Definition of Done — Project Onboarding

- 能掃既有 Server Project
- secret不讀
- dataset不commit
- Normalize可Preview
- GitHub可建立/連結
- ProjectVersion建立
- Project Instance建立

---

# 81. Definition of Done — Remote Codex

- 網站登入 Codex
- persistent conversation
- 選 Server
- remote workspace
- read/search
- edit
- safe command
- confirm command
- event stream
- diff
- tests
- commit
- credential不進 prompt

---

# 82. Definition of Done — Experiment

- fixed ProjectVersion
- fixed Dataset
- fixed Environment
- typed Parameter Matrix
- preview run count
- reuse Product Runs
- multi-server scheduling
- structured metrics
- compare
- artifacts

---

# 83. Definition of Done — Full Platform

使用者可以直接說：

```text
幫我去 gpu-03 修 OOM。
```

網站完成。

接著：

```text
用這個版本在三台機器跑這幾組參數。
```

網站完成。

最後：

```text
幫我分析結果，下一輪應該怎麼試。
```

Codex完成分析並產生可確認的 Experiment proposal。

---

# 84. 不應破壞的核心 invariant

- approved payload immutable
- unknown != failed
- no duplicate attempt
- no secret exposure
- no arbitrary SSH target
- no arbitrary executable provider
- no dirty worktree正式Run
- Product Run仍使用 ExecutionPlan identity
- uncertain remote effect不 blind retry

---

# 85. 改造後定位

改造完成：

> **Dispatch Center 從 Compute Control Plane 升級成 Web AI/ML Engineering Workbench。**

底層繼續沿用：

```text
Project
Dataset
Environment
ExecutionPlan
SSH
Approval
Audit
Artifacts
```

上層新增：

```text
Project Onboarding
Codex
Remote Engineering
Experiment
Optimization
```

---

# 86. 最重要的施工原則

接下來每一個 PR 都應回答：

> **這個變更是否讓使用者更接近「不用 SSH，只用網站 + Codex 完成開發與實驗」？**

若不是，就不應是近期 P0。
