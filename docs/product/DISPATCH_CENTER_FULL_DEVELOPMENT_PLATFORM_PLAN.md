# Dispatch Center 完整開發平台計畫書
## Single-User Web AI/ML Engineering & Experiment Platform

**文件類型：產品藍圖 / 系統總體計畫**  
**目標模式：單一使用者、網站完成主要工作**  
**核心 AI：Codex**  
**核心執行：多伺服器、固定 Git Revision、Experiment / Run**

---

# 1. 產品定位

Dispatch Center 最終應是一套：

> **Single-User Web AI/ML Engineering Control Center**

使用者從一個網站即可完成：

- 建立新 Project
- 從既有 Server 匯入 Project
- 掃描與整理舊 Project
- 建立 / 連接 GitHub Repository
- 統一 Project 規範
- 連接自己的 Codex
- 與 Codex 長期對話
- 讓 Codex 到指定 Server 查看 / 修改 Project
- 執行測試、分析錯誤、執行受控命令
- Review Diff
- Commit / 建立 ProjectVersion
- 把固定版本同步到多台 Server
- 建立 Experiment
- 建 Parameter Matrix
- 多台 GPU Server 跑不同參數
- 集中查看 Runs / Metrics / Logs / Artifacts
- 比較不同 Run
- 讓 Codex 分析結果
- 讓 Codex提議下一輪 Experiment
- 使用者確認後繼續優化

只有 credential、sudo、OAuth、2FA 等本人授權才需要中斷自動流程。

---

# 2. 核心產品承諾

> **使用者只需要打開 Dispatch Center 網站，就能完成從程式開發到模型實驗的完整循環。**

日常不需要：

```text
ssh gpu-01
ssh gpu-02
tmux
scp
rsync
手動比對 commit
手動複製 dataset
手動找 log
手動整理 experiment
```

---

# 3. 完整工作流

```text
Login
  ↓
Connect Codex
  ↓
Projects
  ├─ New Project
  └─ Import from Server
          ↓
    Project Normalize
          ↓
        GitHub
          ↓
    ProjectVersion
      ┌───┴────┐
      ↓        ↓
 AI Engineer  Experiment
      ↓        ↓
    Codex   Parameter Matrix
      ↓        ↓
 Modify/Test  Preview
      ↓        ↓
   Commit   Auto Placement
      └───────┬───────┐
              ↓       ↓
           Server A Server B ...
              ↓
       Metrics / Artifacts
              ↓
            Compare
              ↓
          Ask Codex
              ↓
      Proposed Next Round
```

---

# 4. 產品資訊架構

主導航：

```text
My Workspace
Projects
Servers
AI
Approvals
Operations
Settings
```

Project：

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

---

# 5. Project 是產品中心

Project 應統一代表：

- Git repository
- ProjectVersion
- Environment
- Run Template
- Dataset bindings
- Project Instances
- AI conversations
- Experiments
- Runs
- Artifacts

Server 只是執行資源，不是 Project source of truth。

---

# 6. Dispatch Project Contract

推薦 Project 結構：

```text
my-project/
├── .git/
├── .gitignore
├── README.md
├── dispatch.yaml
├── src/
├── scripts/
├── configs/
├── tests/
└── .dispatch/
    └── metadata.json
```

所有正式 Project 至少要有可解析的 `dispatch.yaml`。

---

# 7. dispatch.yaml

```yaml
version: 1

project:
  name: llama-finetune

environment:
  type: python
  python: "3.12"
  lockfile: uv.lock

datasets:
  training:
    required: true

runs:
  train:
    entrypoint:
      - python
      - scripts/train.py

    parameters:
      learning_rate:
        type: number
        default: 0.0001

      batch_size:
        type: integer
        default: 32

    resources:
      gpu: 1
      cpu: 8
      memory_gb: 32

    outputs:
      - results/metrics.json
      - outputs/model.pt
```

---

# 8. New Project

網站：

```text
Projects
[ New Project ]
```

可選：

- Empty Project
- Template Project
- Existing GitHub Repository

建立後直接得到：

```text
Project
ProjectVersion
Default Environment
Default Run Template
```

---

# 9. Import from Server

流程：

```text
Select Server
→ Read-only Scan
→ Project Candidates
→ Choose Candidate
→ Analyze
→ Normalize Preview
→ Apply
→ GitHub
→ Register Project
```

---

# 10. Project Scan 安全規則

只讀：

- Git metadata
- README small preview
- dependency files
- project markers
- size statistics
- embedded dataset statistics

禁止讀：

- `.env`
- SSH keys
- private keys
- secrets
- credentials

---

# 11. Project Normalize

檢查：

```text
Git?
Remote?
README?
.gitignore?
Dependency lock?
dispatch.yaml?
Entrypoint?
Dataset mixed with code?
Outputs mixed with code?
Secrets detected?
```

輸出：

```text
Normalization Report
```

---

# 12. Codex-assisted Normalize

Codex協助：

- 找 entrypoint
- 找 CLI parameters
- 建 `.gitignore`
- 補 README
- 建 `dispatch.yaml`
- dependency lock建議
- 區分 code / dataset / result / checkpoint
- 建 smoke test
- 建 Run Template
- 建 output declarations

Codex在 staging / isolated workspace 修改，不直接亂改正式 Project。

---

# 13. GitHub 規則

| 類型 | GitHub |
|---|---|
| Source Code | Yes |
| Config | Yes |
| Tests | Yes |
| Docs | Yes |
| Dataset | No |
| Checkpoint | No |
| Model Weights | No |
| Results | No |
| `.env` | Never |
| Credentials | Never |
| SSH Keys | Never |

---

# 14. GitHub 是 Code Source of Truth

```text
GitHub
  ↓
Git Commit
  ↓
ProjectVersion
```

Server上的資料夾不能成為版本真相來源。

---

# 15. ProjectVersion

保存：

```text
project_id
git_commit
source_branch
promotion_state
promotion_approval
bundle_digest
created_at
```

用途：

- Run pinning
- Server sync
- rollback
- experiment reproducibility
- artifact provenance

---

# 16. Project Instance

代表某台 Server 上的 Project checkout。

```text
gpu-01  abc123 clean
gpu-02  abc123 clean
gpu-03  991abc diverged
```

狀態：

```text
missing
syncing
available
diverged
dirty
busy
blocked
unknown
```

---

# 17. Instance Update

流程：

```text
Promoted ProjectVersion
→ verify target
→ verify clean checkout
→ verify no active work
→ Preview
→ Confirm
→ Update exact revision
→ Verify HEAD
```

禁止：

- auto reset dirty checkout
- auto stash
- auto merge
- silent overwrite

---

# 18. Codex Account Connection

Settings：

```text
Codex
[Connect]
[Disconnect]
```

Dispatch identity 與 Codex identity 分離。

---

# 19. AI Conversation

第一版可先提供：

```text
Project Main Conversation
```

後續再加多 Conversation。

網站保存自己的 conversation history，不把 provider thread 當唯一資料來源。

---

# 20. Conversation Domain

```text
AIConversation
├── Messages
├── CodexSession
├── RemoteWorkspace
├── RemoteOperations
├── GitCheckpoint
└── Run References
```

---

# 21. Codex Remote Operation

使用者：

```text
去 gpu-03 看一下 loader 為什麼 OOM。
```

流程：

```text
Conversation
→ Codex intent
→ Dispatch Remote Controller
→ gpu-03
→ approved workspace
→ read / command / edit
→ result
→ same Conversation
```

---

# 22. Codex 不直接拿 SSH Key

錯誤：

```text
Codex → private key → ssh server
```

正確：

```text
Codex
→ Dispatch Tool
→ Policy
→ Server Registry
→ Credential Reference
→ SSH / Runner
→ Server
```

---

# 23. Codex Tool Set

```text
list_servers
get_server_status
open_project_workspace
get_project_status
read_file
search_code
list_files
apply_patch
create_file
delete_file
run_command
run_tests
git_status
git_diff
git_log
git_commit
create_run
stop_run
get_run
get_run_logs
get_run_artifacts
create_experiment
compare_runs
```

---

# 24. Remote Workspace

```text
ProjectVersion
→ isolated worktree
→ Codex edits
→ Tests
→ Diff
→ Commit
```

Worktree path只能由 Dispatch建立。

---

# 25. Command Policy

## Safe

```text
git status
git diff
pytest
ruff
mypy
node --check
nvidia-smi
approved smoke test
read logs
```

## Confirm

```text
pip install
uv add
npm install
large model download
large GPU run
delete files
Git push
dataset mutation
```

## Credential Required

```text
sudo
OAuth
2FA
private registry login
secret rotation
```

Credential永遠不進 Codex prompt。

---

# 26. Commit Workflow

```text
Codex modifies
→ Tests
→ View Diff
→ Commit
→ ProjectVersion candidate
```

正式 Run不允許使用 dirty worktree。

---

# 27. Run Template

定義：

- entrypoint
- parameter schema
- resources
- dataset inputs
- artifact outputs
- metrics output

---

# 28. Environment

固定：

- Python
- dependencies
- CUDA compatibility
- venv / container
- required capabilities

Run不能依賴「Server碰巧裝了什麼」。

---

# 29. Dataset

使用 logical binding：

```text
training
validation
test
```

建立 Run 時 resolve 到 immutable version。

---

# 30. Experiment

Experiment 是：

> 同一 Code / Dataset / Environment / Template 下，一組不同 Parameters 的 Runs。

固定：

```text
ProjectVersion
EnvironmentRevision
RunTemplateRevision
DatasetVersions
BaseResources
```

---

# 31. Parameter Matrix

```text
learning_rate
0.001
0.0005
0.0001

batch_size
16
32

seed
42
```

Preview：

```text
3 × 2 × 1 = 6 Runs
```

---

# 32. Experiment Guard

Start 前顯示：

```text
Total Runs
Estimated GPU Hours
Estimated Storage
Expected Servers
```

---

# 33. Run Contract

每個 Run固定：

```text
code_revision
environment_revision
dataset_versions
run_template_revision
parameters
resource_request
```

---

# 34. Placement

預設：

```text
Auto
```

使用者只描述：

```text
GPU 1
VRAM >= 24GB
CPU 8
RAM 32GB
```

Dispatch選 Server。

Advanced才允許：

```text
Specific Server
Server Tag
GPU Type
```

---

# 35. Execution

第一版繼續沿用成熟 SSH backend。

```text
ExecutionPlan
→ Attempt
→ SSH
→ Collect
```

Node Agent不作為第一版 blocker。

---

# 36. Result & Metrics

Run完成收：

```text
status
metrics
logs
artifacts
resource usage
server identity
timestamps
```

推薦標準：

```json
{
  "loss": 0.31,
  "accuracy": 0.934,
  "val_loss": 0.37
}
```

---

# 37. Experiment Dashboard

| Run | Server | LR | Batch | Status | Accuracy | Loss |
|---|---|---:|---:|---|---:|---:|
| #201 | gpu-01 | 0.001 | 16 | Done | 91.2 | 0.42 |
| #202 | gpu-02 | 0.001 | 32 | Done | 91.8 | 0.39 |
| #203 | gpu-03 | 0.0005 | 16 | Done | 93.1 | 0.31 |

支援：

```text
Compare
Clone
Re-run
Promote Artifact
Ask Codex
```

---

# 38. Codex Experiment Analysis

Codex可讀：

- Experiment context
- parameters
- metrics
- failed run summary
- logs
- artifact metadata
- Project code

回傳：

```text
Recommendation
Parameter Region
Reason
Proposed Next Experiment
```

---

# 39. Optimization Loop

第一版：

```text
Codex suggests
→ User Preview
→ User Confirm
→ Experiment
```

不要一開始做無限制 autonomous loop。

未來才加：

```text
max_runs
max_gpu_hours
max_rounds
```

---

# 40. Trusted Single User Mode

保留：

- OIDC
- authorization
- approvals
- immutable payload
- durable audit

但允許 trusted user self-approval，降低單人摩擦。

---

# 41. Approval UX

顯示實際操作：

```text
Codex wants to install flash-attn.

Reason:
Required by implementation.

[Approve]
[Reject]
```

不要顯示大量內部 domain jargon。

---

# 42. Website Daily UX

使用者主要只看到：

```text
Projects
AI Engineer
Experiments
Runs
Datasets
Artifacts
Servers
```

ExecutionPlan / Attempt / Outbox / Fencing收進 Advanced。

---

# 43. 第一版暫不做

- 多使用者複雜 UX
- Claude Code
- Multi-Agent
- A2A
- autonomous agent swarm
- Kubernetes
- full browser IDE
- arbitrary web terminal
- root shell
- Node Agent production rollout

---

# 44. Milestone 1 — Project Onboarding

```text
New Project
Import from Server
Normalize
GitHub
ProjectVersion
Project Instance
```

---

# 45. Milestone 2 — Web Remote Codex

```text
Connect Codex
Conversation
Remote Server
Read Code
Modify Worktree
Run Tests
View Diff
Commit
```

---

# 46. Milestone 3 — Experiment

```text
New Experiment
Parameter Matrix
Preview
N Runs
Multi-server placement
Results
Compare
```

---

# 47. Milestone 4 — AI Optimization

```text
Experiment
→ Ask Codex
→ Suggested Next Parameters
→ Preview
→ Run Next Experiment
```

---

# 48. Definition of Done

使用者可以完全從網站完成：

```text
Import Project
→ Normalize
→ GitHub
→ Codex修改
→ Commit
→ Multi-server Experiment
→ Compare
→ Optimize
```

不需要 SSH。

---

# 49. 真實成功標準

1. 一個真實舊 Project 從 Server被網站匯入。
2. Dataset / secret不誤上傳。
3. 建立 GitHub repository。
4. Codex在網站內修改 Project。
5. Codex透過受控工具在遠端跑測試。
6. 使用者在網站看 Diff並 Commit。
7. 同一 revision建立至少6個參數 Run。
8. Runs分散到至少3台 Server。
9. 結果集中回 Experiment。
10. Codex分析並提出下一輪。

---

# 50. 最終產品一句話

> **Dispatch Center 是一個讓單一 AI/ML 開發者只靠瀏覽器與 Codex，就能管理 Project、遠端 Server、程式開發、Git 版本、多 GPU 實驗與模型優化的完整工程平台。**
