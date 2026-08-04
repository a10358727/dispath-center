# Dispatch Center 專案架構整理與改造計畫

> 適用儲存庫：`a10358727/dispath-center`  
> 檢查基準：`main` commit `7ecbec6881765ff948eaa5633fd80c785d949915`  
> 文件日期：2026-08-03  
> 文件用途：作為後續程式改造、Issue 拆分、Pull Request 驗收與部署決策的共同規格。

> **Local implementation update (2026-08-04):** PR-09 protocol version/header,
> canonical capability contract, authenticated `agent --probe`, PR-10's
> opt-in systemd isolation/resource-policy builders, PR-11's additive
> `dispatch-worker` role, and PR-12's dependency-free frontend smoke/build
> gate are implemented and locally tested. Deployment, separate-UID/runtime
> evidence, real worker/node canary, and production rollout remain explicit
> gates; Node assignment and SSH defaults are unchanged.

---

## 1. 結論摘要

目前專案已具備相當多實際功能，包括排程、SSH 執行、核准流程、專案與資料集管理、Node Agent、AI Engineering Task、OIDC、備份還原、健康檢查及大量測試。

目前的主要問題不是「功能不夠」，而是：

1. 安全邊界尚未完整落實。
2. 應用程式、資料庫、設定和前端過度集中。
3. 歷史相容路徑與階段式功能持續累積，缺少明確退場機制。
4. 套件、部署、資料庫 migration、CI/CD 和發布流程還不像可長期維護的正式產品。
5. 單元測試很多，但真實部署、真實 worker、真實重啟與權限隔離證據仍不足。
6. 使用者操作仍偏向管理內部物件，而不是完成一條清楚的「建立並執行工作」流程。

建議不要重寫整個專案。應採用「保留功能、逐步建立邊界」的方式，依序完成：

- 安全與授權；
- 專案套件化；
- API／Service／Repository 分層；
- 正式 migration；
- 背景工作與 outbox；
- Node Agent 隔離；
- 前端工作流程；
- CI/CD、發布及文件治理。

---

## 2. 現有優點

改造時應保留以下特性，不應因重構而退化：

- 高風險操作多數已走人工核准。
- 大量功能使用 feature flag，預設關閉。
- SSH 仍是穩定且可回退的執行後端。
- SQLite 寫入已有交易與鎖定意識。
- Node attempt 已加入原子 claim、唯一約束、durable supervisor 和 PID identity。
- Execution Plan、Project Version、Dataset Snapshot 已朝不可變資源設計。
- CI 使用鎖定依賴並執行完整測試。
- 備份、還原、health、readiness、canary 文件已開始形成。
- 文件對「已實作、已部署、已證明」之間的差異有一定程度的區分。
- 預設只綁定 loopback／私網位址。

重構原則是把這些能力整理成更清楚的模組，而不是把安全機制移除以換取簡化。

---

## 3. 目前問題清單

### 3.1 P0：正式開放多人使用前必須處理

#### P0-1：授權目前只有 Shadow Mode，沒有真正 Enforcement

目前 `app/authorization.py` 保持純 policy evaluator；截至 PR-05，
`AppConfig` 已支援 `off`、`shadow` 與 `enforce`。`off`／`shadow` 仍保留
相容與觀測語意，`enforce` 由獨立 adapter 對已登記 route-action 契約做
fail-closed 檢查。完整 hostile multi-tenant isolation 仍需後續部署與 worker
隔離證據，不能只由這個 mode 宣稱完成。

風險：

- 已登入不代表只能操作自己有權限的 Project。
- 介面可能顯示 would-deny，但後端仍執行操作。
- OIDC、Project Membership、Service Scope 容易讓使用者誤以為 RBAC 已正式生效。
- 一旦控制平面被多人存取，這會成為實際越權問題。

要求：

- 新增 `AUTHORIZATION_MODE=enforce`。
- 所有 route 必須對應一個封閉的 `Action`。
- 所有資源必須先解析成 Project 或 Global scope。
- 無法解析資源歸屬時 fail closed。
- 高風險核准必須實施 separation of duties。
- list endpoint 必須做資料列層級過濾，不只是按鈕隱藏。
- service account 必須檢查 scope。
- legacy shared token 不得在 enforce 模式下自動取得全域管理權。

驗收：

- Viewer 無法執行操作。
- Operator 無法管理成員或平台設定。
- Project Admin 無法管理其他 Project。
- 非 Platform Admin 無法管理 Server、Node、Identity。
- 提案者不能核准自己提出的高風險操作。
- 所有 would-deny 測試轉為實際 403 測試。

---

#### P0-2：Node Agent 控制層與工作負載使用同一個 Unix 使用者

目前 Agent、Supervisor 和工作負載由同一 UID 執行。即使 Supervisor 從子行程環境移除 Node token，工作負載仍可能讀取同一使用者可讀的 environment file、Agent journal 或 supervisor evidence。

風險：

- 工作負載讀取 Node credential。
- 工作負載偽裝成 Agent 呼叫 Node API。
- 工作負載修改 `supervisor.json`、`terminal.json` 或 attempt journal。
- 工作負載 signal、ptrace 或干擾 Agent。
- AI 產生的程式碼獲得控制面憑證。

目標架構：

```text
dispatch-agent user / namespace
├── Node credential
├── Agent daemon
├── control journal
└── supervisor evidence

dispatch-workload user / container
├── read-only command
├── project / dataset mounts
└── writable outputs
```

要求：

- Agent 與 workload 使用不同 UID、user namespace 或 rootless container。
- 工作負載不能讀 Agent credential 與 control journal。
- Supervisor evidence 放在 workload 不可寫的位置。
- 工作負載環境使用 allowlist，不複製整個 `os.environ`。
- 每個 attempt 使用獨立 cgroup 或 systemd transient unit。
- 設定 CPU、RAM、PID、GPU、磁碟、timeout 和 network policy。
- 完成前 `NODE_NEW_ASSIGNMENT_ENABLED` 維持 false。

---

#### P0-3：缺少真實 Node Canary 與隔離證據

程式合併不等於部署可用。目前仍缺少真實工作機證據。

最低 canary：

- 兩台非正式 worker。
- 至少 100 個普通測試工作。
- Agent restart while workload running。
- Control Plane restart。
- 網路中斷與 response loss。
- credential rotate／drain／revoke。
- stop escalation。
- PID reuse 模擬。
- disk full／journal corruption。
- 零重複啟動。
- 每個 attempt 最終都有 terminal 或明確 unknown。
- 回退到 SSH 不需要資料 migration。

輸出必須是可驗證 evidence artifact，不只是 PR 描述。

---

#### P0-4：Audit 仍是 Best-Effort，不是持久安全證據

目前 audit 寫入 JSONL，失敗時會記 log 和 process-local metric，但 domain action 仍成功；重啟後 failure counter 會消失。

要求：

- 建立 `audit_events` DB table。
- domain mutation 和 audit event 在同一 transaction。
- JSONL 改為 export sink，而不是唯一真相。
- 加入 event hash、request ID、actor、resource、approval、result。
- 建立 audit export outbox。
- audit 查詢改用 DB pagination。
- 定義 retention、rotation、backup、restore 驗證。

---

### 3.2 P1：維護性與正確性的重要問題

#### P1-1：`app/main.py` 是過大的 Composition Root 與 Route Monolith

目前大量內容集中在 `app/main.py`：

- FastAPI models；
- middleware；
- route；
- AppState；
- background loop；
- health／metrics；
- project、dataset、engineering、node、approval 等多個 domain 的序列化與流程。

結果：

- 修改一個功能容易碰到不相關區域。
- route 測試需要載入整個應用。
- dependency graph 不清楚。
- transaction、authorization、audit 容易在不同 route 中以不同方式處理。
- merge conflict 與大型 PR 風險增加。

應拆成：

```text
src/dispatch_center/api/
├── app.py
├── dependencies.py
├── middleware/
├── errors.py
├── schemas/
└── routers/
    ├── projects.py
    ├── runs.py
    ├── approvals.py
    ├── datasets.py
    ├── servers.py
    ├── nodes.py
    ├── engineering.py
    ├── identities.py
    └── operations.py
```

`api/app.py` 只負責：

- 建立 FastAPI；
- 註冊 middleware；
- 註冊 router；
- lifespan；
- exception handler；
- dependency container。

---

#### P1-2：`app/db.py` 同時負責 Schema、Migration、Model、Repository 與 Domain Logic

目前 DB 層包含：

- schema；
- additive ALTER migration；
- dataclass／row mapping；
- 所有 CRUD；
- transaction；
- state transition；
- approval、job、node、dataset、engineering 等不同 domain。

要求拆成：

```text
src/dispatch_center/infrastructure/db/
├── connection.py
├── unit_of_work.py
├── migrations/
├── models/
└── repositories/
    ├── jobs.py
    ├── attempts.py
    ├── approvals.py
    ├── projects.py
    ├── datasets.py
    ├── nodes.py
    ├── identities.py
    └── audit.py
```

規則：

- route 不可直接呼叫 SQL repository。
- application service 決定 use case。
- repository 只負責 persistence。
- domain transition 不可散落在任意 `update_*()`。
- transaction 由 Unit of Work 管理。
- 需要同時更新多張表的流程必須使用同一 UoW。

---

#### P1-3：沒有正式、具版本的資料庫 Migration 系統

目前 migration 主要在啟動時檢查欄位並執行 `ALTER TABLE`。這對早期專案可用，但功能增加後會出現：

- 無清楚 schema version。
- migration 順序隱藏在 Python 初始化。
- 無法在部署前 dry-run。
- 無 downgrade 或 rollback 計畫。
- 啟動服務時才發現 migration 失敗。
- 多 process 同時啟動可能競爭 migration。
- 大表重建與資料轉換難以管理。

建議使用 Alembic，或建立等價的版本化 migration runner。

最低要求：

```text
migrations/
├── 0001_initial.py
├── 0002_node_attempt_constraints.py
├── 0003_audit_outbox.py
└── ...
```

CLI：

```bash
dispatch db current
dispatch db upgrade
dispatch db check
dispatch db backup
dispatch db restore-verify
```

部署流程必須是：

```text
backup
→ migration preflight
→ migration
→ application rollout
→ smoke test
→ rollback decision
```

---

#### P1-4：設定物件過大，Feature Flag 與 Secret 混在一起

目前 `AppConfig` 同時承載：

- HTTP；
- DB；
- auth；
- OIDC；
- SSH；
- scheduler；
- Node；
- LLM；
- engineering；
- backup；
- dataset；
- frontend 行為。

建議拆成：

```python
class HttpSettings(...)
class DatabaseSettings(...)
class AuthSettings(...)
class OIDCSettings(...)
class SSHSettings(...)
class SchedulerSettings(...)
class NodeSettings(...)
class EngineeringSettings(...)
class DatasetSettings(...)
class ObservabilitySettings(...)
```

並由：

```python
class Settings:
    http: HttpSettings
    database: DatabaseSettings
    auth: AuthSettings
    ...
```

統一組合。

要求：

- 使用 `pydantic-settings` 或等價 typed settings。
- Secret 使用 `SecretStr` 或 secret reference。
- startup 顯示非秘密設定摘要。
- 每個 feature flag 有 owner、預設值、相依條件、退場日期。
- 舊 compatibility flag 設定 deprecation warning。
- 禁止同一能力有多個含糊 source of truth。

---

#### P1-5：Runtime、Optional 與 Development Dependencies 混在一起

目前 `requirements.txt` 同時包含：

- 核心服務 runtime；
- Anthropic；
- MCP；
- 測試工具。

影響：

- Control Plane 安裝不需要的套件。
- Node Agent 無法獨立安裝。
- attack surface 增加。
- 依賴升級與 lock 變得困難。
- 無標準 wheel／package metadata。

建議改用單一 `pyproject.toml`：

```toml
[project]
name = "dispatch-center"
version = "0.x.y"
requires-python = ">=3.10"

[project.optional-dependencies]
llm = [...]
mcp = [...]
dev = [...]
test = [...]
postgres = [...]

[project.scripts]
dispatch-api = "dispatch_center.cli:api"
dispatch-scheduler = "dispatch_center.cli:scheduler"
dispatch = "dispatch_center.cli:main"
dispatch-node-agent = "dispatch_node_agent.cli:main"
```

Node Agent 最好成為獨立 package：

```text
packages/
├── dispatch-center/
├── dispatch-node-agent/
└── dispatch-protocol/
```

---

#### P1-6：Node Protocol 沒有獨立版本協商與 Canonical Schema

目前 agent version 同時承擔 package version 和協議相容性的責任，且 client／server 各自實作 model 與 digest。

要求：

- 新增明確 protocol version。
- request header：

```text
X-Dispatch-Protocol-Version: 2
X-Dispatch-Agent-Version: 1.0.0
```

- 建立 protocol package 或 JSON Schema。
- contract test 直接啟動真實 FastAPI app，使用真實 client。
- 不相容時回明確錯誤，不依賴偶然的 422。
- 舊 `poll(job_id=None)` 設定移除期限。
- 建立 compatibility matrix。

---

#### P1-7：Artifact Metadata 可以被後續回報覆寫

目前相同 `(attempt_id, relative_path)` 可透過 upsert 改變 digest。

應改為：

- 第一次：insert。
- 同內容重送：idempotent success。
- 不同 digest／size：409 `artifact_conflict`。
- terminal 後 immutable。
- 執行中的暫時觀測放另一張表。
- Artifact metadata 不等於已完成 result collection，兩者狀態需分離。

---

#### P1-8：背景迴圈雖可分角色，但仍和 Web 應用共用過多狀態

`PROCESS_ROLE` 已開始區分 `api`／`scheduler`，方向正確，但長期應形成獨立入口：

```text
dispatch-api
dispatch-scheduler
dispatch-worker
dispatch-maintenance
```

建議：

- API process 不建立 scheduler state。
- Scheduler 不承接一般使用者流量。
- completion operation、notification、result collection 使用 durable queue／outbox。
- 所有 background task 都有 lease、heartbeat、retry、dead-letter。
- readiness 依 role 檢查不同 dependency。
- 不要只依賴記憶體 task list 判斷工作是否健康。

---

#### P1-9：API 缺少穩定版本、統一錯誤格式與 Use-Case 邊界

建議 API 統一為：

```text
/api/v1/projects
/api/v1/runs
/api/v1/approvals
/api/v1/servers
/api/v1/nodes
/api/v1/operations
```

統一錯誤：

```json
{
  "error": {
    "code": "plan_digest_mismatch",
    "message": "Execution plan verification failed",
    "request_id": "...",
    "details": {}
  }
}
```

要求：

- 不把 raw exception 直接回傳給 client。
- 錯誤碼與顯示文字分離。
- 中文 UI 文案不要成為程式判斷依據。
- route 只做 parse、authorize、call service、serialize。
- OpenAPI tag 與 operation ID 穩定。
- mutation 支援 idempotency key。
- list endpoint 一律 pagination。
- 長時間操作改成 operation resource，不阻塞 HTTP request。

---

#### P1-10：部署仍依賴手動修改 systemd Template

目前 Control Plane 和 Node Agent systemd unit 都需要操作者手改路徑或自行確保 module 可 import。

建議：

- 建立 wheel。
- 建立版本化 release artifact。
- systemd unit 由安裝器產生，不手改 repository template。
- 使用固定安裝路徑，例如 `/opt/dispatch-center/releases/<version>`。
- 使用 symlink 切換 current release。
- 設定放 `/etc/dispatch-center/`。
- state 放 `/var/lib/dispatch-center/`。
- log 進 journald 或 `/var/log/dispatch-center/`。
- secret 不放 repo checkout。
- 建立 install、upgrade、rollback、uninstall 命令。
- Agent 每個 attempt 使用 transient unit，避免 `KillMode=process` 留下無管理 child。

---

### 3.3 P2：專案成熟度與團隊流程問題

#### P2-1：CI 只有單一 Python 3.10 測試工作

目前 CI 的優點是鎖依賴、完整測試、migration suite 和 static invariant；但仍缺少：

- Python 3.11／3.12 matrix。
- formatter／linter。
- type checking。
- coverage threshold。
- dependency vulnerability scan。
- secret scan。
- build wheel 驗證。
- migration dry-run。
- API schema diff。
- frontend lint／build。
- systemd unit verification。
- minimal install smoke test。

建議 CI 工作：

```text
lint
typecheck
unit
integration-sqlite
contract-node
migration
frontend
package-build
security-scan
e2e-smoke
release-gate
```

工具可採：

- Ruff；
- Pyright 或 mypy；
- pytest-cov；
- pip-audit；
- detect-secrets／gitleaks；
- bandit 僅作輔助，不取代人工 review；
- `python -m build`；
- `twine check`；
- `systemd-analyze verify`。

---

#### P2-2：缺少正式發布與版本治理

應加入：

- `CHANGELOG.md`；
- SemVer；
- Git tag；
- release notes；
- compatibility matrix；
- migration notes；
- deprecation policy；
- support window；
- release artifact checksum／SBOM。

版本應分開：

```text
Control Plane version
Node Agent version
Protocol version
Database schema version
API version
```

---

#### P2-3：PR 範圍與 Review 治理不足

近期 PR #20 修改 96 個檔案、增加超過 20,000 行，且沒有 reviewer discussion。即使測試通過，仍難以人工確認所有 invariant。

建議：

- `main` branch protection。
- 至少一位 reviewer。
- CODEOWNERS。
- 安全敏感路徑要求專門 review。
- 禁止直接 push main。
- PR size warning。
- migration、agent、security、frontend 分開 PR。
- PR template 必填：
  - 問題；
  - invariant；
  - rollback；
  - migration；
  - security impact；
  - test evidence；
  - deployment evidence；
  - 文件更新。

---

#### P2-4：文件以階段編號累積，容易漂移

目前程式和文件大量使用「階段 1～16」「Goal」「Slice」「WP」等歷史語彙。這對開發過程有價值，但不適合作為長期產品結構。

建議文件分層：

```text
docs/
├── architecture/
│   ├── overview.md
│   ├── execution-model.md
│   ├── security-model.md
│   └── data-model.md
├── adr/
├── api/
├── operator/
├── developer/
├── runbooks/
└── archive/
    └── historical-phase-plans/
```

規則：

- README 只保留快速開始和架構概覽。
- 歷史 phase 文件移到 archive。
- 每個重要決策用 ADR。
- Capability Ledger 由機器可讀檔產生。
- 文件狀態必須區分：
  - designed；
  - implemented；
  - enabled；
  - deployed；
  - canary-proven；
  - production-ready。

---

#### P2-5：前端仍偏向單頁大檔案與內部物件操作

建議先不急著全面改 React。第一步可將現有 JS 拆成：

```text
web/
├── api/
├── pages/
├── components/
├── state/
├── forms/
└── main.js
```

成熟後可轉 Vite + TypeScript。

主要 UX 應從內部物件改成任務流程：

```text
選 Project
→ 選 Version
→ 選 Run Profile
→ 選 Dataset Snapshot
→ 選資源需求
→ Preview
→ Request Approval
→ 追蹤
→ 結果與 Lineage
```

管理頁仍可保留底層物件，但不是日常主要入口。

---

#### P2-6：專案名稱拼字與對外名稱需要統一

Repository 名稱目前是 `dispath-center`，程式概念則是 dispatch center。

不建議立即改 repository，避免破壞 clone URL、systemd path 和文件連結；但應先統一：

- Python package：`dispatch_center`
- CLI：`dispatch`
- 發布名稱：`dispatch-center`
- Node package：`dispatch-node-agent`

Repository rename 可在建立相容 redirect 和部署 migration 後單獨處理。

---

## 4. 建議的目標架構

### 4.1 高階元件

```text
                        ┌────────────────────┐
                        │   Web / CLI / MCP  │
                        └─────────┬──────────┘
                                  │ HTTPS
                        ┌─────────▼──────────┐
                        │   Control API      │
                        │ AuthN + AuthZ      │
                        │ Request / Query    │
                        └─────────┬──────────┘
                                  │ Application Services
              ┌───────────────────┼───────────────────┐
              │                   │                   │
    ┌─────────▼────────┐ ┌────────▼─────────┐ ┌──────▼──────────┐
    │ Project / Run    │ │ Approval Service │ │ Platform Service │
    │ Services         │ │                  │ │ Server / Node    │
    └─────────┬────────┘ └────────┬─────────┘ └──────┬──────────┘
              │                   │                   │
              └───────────────────┼───────────────────┘
                                  │ Unit of Work
                        ┌─────────▼──────────┐
                        │ SQLite/PostgreSQL  │
                        │ + Durable Outbox   │
                        └─────────┬──────────┘
                                  │ claims / operations
                        ┌─────────▼──────────┐
                        │ Scheduler / Worker │
                        └──────┬───────┬─────┘
                               │       │
                         SSH backend   Node protocol
                               │       │
                        ┌──────▼──┐ ┌──▼────────────────┐
                        │ Worker  │ │ Isolated Node Agent│
                        └─────────┘ └───────────────────┘
```

---

### 4.2 建議目錄

```text
dispath-center/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── SECURITY.md
├── CONTRIBUTING.md
├── LICENSE
├── .github/
│   ├── workflows/
│   ├── CODEOWNERS
│   └── pull_request_template.md
├── packages/
│   ├── control-plane/
│   │   └── src/dispatch_center/
│   │       ├── api/
│   │       ├── application/
│   │       ├── domain/
│   │       ├── infrastructure/
│   │       ├── workers/
│   │       ├── settings/
│   │       └── cli/
│   ├── node-agent/
│   │   └── src/dispatch_node_agent/
│   └── protocol/
│       └── src/dispatch_protocol/
├── web/
│   ├── src/
│   ├── tests/
│   └── package.json
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── e2e/
│   └── fixtures/
├── deploy/
│   ├── systemd/
│   ├── install/
│   └── upgrade/
├── docs/
│   ├── architecture/
│   ├── adr/
│   ├── operator/
│   ├── developer/
│   ├── runbooks/
│   └── archive/
└── scripts/
```

單一 repository 仍可保留；重點是 package boundary，不必拆成多個 Git repository。

---

## 5. 分層規則

### Domain Layer

包含：

- Entity；
- Value Object；
- State Machine；
- Policy；
- Domain Error。

不得 import：

- FastAPI；
- sqlite3；
- asyncssh；
- HTTP client；
- filesystem adapter。

### Application Layer

包含 use case：

- RequestRun；
- ApproveRun；
- StopRun；
- RegisterServer；
- EnrollNode；
- PublishDatasetSnapshot；
- PromoteEngineeringResult。

可以依賴：

- Domain；
- Repository protocol；
- UnitOfWork protocol；
- Event publisher protocol。

不得直接依賴具體 SQLite／SSH。

### Infrastructure Layer

實作：

- SQLite repository；
- migration；
- SSH adapter；
- Node transport；
- audit sink；
- email；
- filesystem；
- Git hub store。

### API Layer

只做：

1. 驗證輸入；
2. authentication；
3. authorization；
4. 呼叫 application service；
5. 轉換 response；
6. 統一例外。

### Worker Layer

負責：

- scheduler；
- operation outbox；
- completion outbox；
- result collection；
- notification；
- reconciliation；
- maintenance。

---

## 6. 資料模型與交易策略

### 6.1 單一執行真相

最終目標：

```text
execution_attempts = 所有 SSH／Node 執行的唯一狀態機
node_attempts      = Node protocol receipt 與 wire metadata
jobs               = 使用者可見 aggregate projection
```

避免長期同時維護兩套 attempt 狀態機。

### 6.2 Outbox

應建立：

```text
outbox_operations
audit_events
completion_operations
notifications
```

流程：

```text
Domain transaction
├── 更新 aggregate
├── 新增 audit event
└── 新增 outbox operation
COMMIT

Worker
→ claim outbox
→ 執行外部副作用
→ 寫 receipt
→ retry / unknown / dead-letter
```

外部副作用不能藏在 route transaction 中。

### 6.3 SQLite 與 PostgreSQL

目前規模小，可繼續使用 SQLite，但先定義升級條件：

- scheduler process > 1；
- sustained write contention；
- API replicas > 1；
- database > 指定容量；
- active jobs／attempts 超過門檻；
- audit／event 查詢成為主要負載；
- 需要 row-level locking 或較高可用性。

在達到門檻前，不需要為了「看起來正式」立即導入 PostgreSQL。

Repository abstraction 必須讓未來替換可行，但不可為抽象而抽象。

---

## 7. 建議拆分的 Pull Requests

### PR-01：Project Hygiene

內容：

- 新增 `CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`。
- 新增 PR template、CODEOWNERS。
- 加 Ruff、Pyright／mypy、pytest-cov。
- 不改 runtime behavior。

驗收：

- CI 新增 lint／typecheck。
- main branch 要求 review。
- 文件說明開發、測試、發布流程。

---

### PR-02：Packaging

內容：

- 建立完整 `pyproject.toml`。
- Control Plane 與 Node Agent 可 build wheel。
- Runtime／optional／dev dependencies 分組。
- 加 console scripts。

驗收：

```bash
python -m build
pip install dist/*.whl
dispatch --help
dispatch-api --help
dispatch-node-agent --check
```

---

### PR-03：Settings Decomposition

內容：

- 拆分 typed settings。
- 保留舊環境變數。
- 新增 deprecation warning。
- startup config report 不包含秘密。

驗收：

- 所有舊設定測試通過。
- 不同 feature 的驗證互不影響。
- secret 不出現在 repr／log。

---

### PR-04：API Router Extraction

內容：

- 從 `main.py` 移出 routers 和 schemas。
- 先不改 use case。
- 建立統一 errors 與 request ID。

驗收：

- API contract 不變。
- OpenAPI snapshot 通過。
- `main.py` 僅保留 composition root。

---

### PR-05：Authorization Enforcement

內容：

- 加 `enforce` mode。
- route-action catalog。
- list filtering。
- separation of duties。
- legacy token policy。

驗收：

- 完整 401／403／404 matrix。
- Cross-project 測試。
- Service scope 測試。
- Shadow 與 enforce decision parity。

---

### PR-06：Versioned DB Migration

內容：

- schema version。
- migration CLI。
- 將現有 startup ALTER 搬成版本 migration。
- migration lock。
- backup preflight。

驗收：

- 從歷史 fixture 升級到最新。
- repeated upgrade idempotent。
- failure 不會半套用。
- app 啟動不再偷偷執行大型 migration。

---

### PR-07：Repository + Unit of Work

內容：

- 先搬 Node／Execution domain。
- 建立 repository interfaces。
- 建立 SQLite UoW。
- 保留舊 facade 作 compatibility adapter。

驗收：

- Node claim、terminal、artifact、stop 全由 UoW 驗證。
- route 不直接執行 DB state transition。
- transaction fault injection 通過。

---

### PR-08：Durable Audit + Outbox

內容：

- `audit_events`。
- audit 與 mutation 同 transaction。
- JSONL exporter。
- pagination。
- retry／dead-letter。

驗收：

- disk full 不遺失 DB audit。
- exporter 可重送。
- backup／restore 保留 hash chain。
- `/events` 不再讀完整檔案。

---

### PR-09：Node Protocol Package

內容：

- protocol version。
- canonical schema。
- client/server contract test。
- 移除無效果的 `job_id` compatibility。
- `agent --probe`。

驗收：

- 不相容版本明確拒絕。
- 真實 client 對 in-process FastAPI 測試。
- request payload boundary 完整。

---

### PR-10：Workload Isolation

內容：

- Agent／workload 分離 UID 或 rootless container。
- transient systemd attempt unit。
- credential namespace。
- read-only control evidence。
- resource policy。

驗收：

- workload 無法讀 Node token。
- workload 無法修改 terminal evidence。
- workload 無法 signal Agent。
- Agent restart 不終止 workload。
- workload terminal 可恢復。
- stop 只作用於正確 attempt。

---

### PR-11：Worker Process Split

內容：

- `dispatch-scheduler`。
- `dispatch-worker`。
- durable outbox claim。
- role-based readiness。

驗收：

- API process 無 scheduler loop。
- scheduler 重啟不遺失 operation。
- 兩個 contender 只有 leader 執行。
- operation 可觀測、可重試。

---

### PR-12：Frontend Workflow

內容：

- 將 JS 拆模組。
- 建立 Run Wizard。
- Approval Inbox。
- Project timeline。
- capability-based buttons。

驗收：

- 不必輸入內部 ID。
- 一條完整 run 流程可在 UI 完成。
- 403 顯示具體原因。
- 前端 build／lint／smoke test 進 CI。

---

## 8. 建議執行順序

```text
PR-01 Project Hygiene
  ↓
PR-02 Packaging
  ↓
PR-03 Settings
  ↓
PR-04 API Extraction
  ↓
PR-05 Authorization Enforcement
  ↓
PR-06 Migration
  ↓
PR-07 Repository + UoW
  ↓
PR-08 Audit + Outbox
  ↓
PR-09 Protocol
  ↓
PR-10 Workload Isolation
  ↓
真實 Node Canary
  ↓
PR-11 Worker Split
  ↓
PR-12 Frontend Workflow
```

Node assignment 必須等 PR-09、PR-10 和 canary 完成。

---

## 9. 每個 PR 的 Definition of Done

每個 PR 必須回答：

- [ ] 問題是什麼？
- [ ] 哪個 invariant 被建立或維持？
- [ ] 是否有 DB migration？
- [ ] 是否有 configuration change？
- [ ] 是否有 security impact？
- [ ] 是否有 backward compatibility？
- [ ] rollback 方法是什麼？
- [ ] unit tests 是否完成？
- [ ] integration tests 是否完成？
- [ ] failure injection 是否完成？
- [ ] 文件是否更新？
- [ ] capability ledger 是否更新？
- [ ] deployment evidence 是否需要？
- [ ] 是否需要 canary？
- [ ] 是否刪除已到期的 compatibility path？

禁止只用「完整測試通過」取代以上說明。

---

## 10. 不建議的做法

- 不要一次重寫整個系統。
- 不要同一個 PR 同時改 DB、Agent、前端、部署和文件。
- 不要在授權 enforcement 完成前宣稱 OIDC／RBAC 已可多人使用。
- 不要在 workload isolation 和 canary 完成前開啟 Node assignment。
- 不要直接把 SQLite 換成 PostgreSQL，卻保留原本的單體 DB API。
- 不要為了拆檔而拆檔；每個模組必須有清楚責任。
- 不要讓 route 直接處理外部副作用。
- 不要用黑名單判斷任意 shell 指令是否安全。
- 不要把 feature flag 當成永久架構。
- 不要讓 legacy compatibility 永久存在而沒有移除日期。
- 不要將測試數量當成部署證據。
- 不要將 PR body 的聲明當成 canary evidence。

---

## 11. 可立即建立的 Issues

### Security

- [ ] Implement authorization enforcement mode.
- [~] Separate Node Agent credentials from workload UID (local contract/builder;
  separate-UID or rootless runtime evidence still required).
- [~] Protect supervisor evidence from workload writes (read-only transient
  unit mount builder; installation/runtime evidence still required).
- [x] Replace workload inherited environment with allowlist (strict opt-in
  systemd template mode; direct compatibility mode remains for rollback).
- [~] Add transient systemd unit per attempt (local argv/resource contract;
  real user-systemd verification still required).
- [ ] Make audit durable and transactional.

### Architecture

- [ ] Extract FastAPI routers from `app/main.py`.
- [ ] Split `app/db.py` into repositories and UoW.
- [ ] Introduce versioned migration runner.
- [ ] Split settings by bounded context.
- [ ] Define canonical execution attempt state machine.
- [ ] Define compatibility removal plan.

### Packaging and Operations

- [ ] Build Control Plane wheel.
- [x] Build Node Agent wheel.
- [ ] Add install／upgrade／rollback commands.
- [~] Verify systemd units in CI (static contract/local builder; no installed
  worker unit evidence).
- [ ] Add `dispatch doctor`.
- [x] Add `dispatch-node-agent --probe`.

### CI and Governance

- [x] Add Ruff.
- [x] Add type checking.
- [x] Add coverage threshold.
- [ ] Add dependency／secret scans.
- [ ] Add Python version matrix.
- [ ] Add CODEOWNERS and branch protection.
- [ ] Add PR size warning.
- [ ] Add release tags and changelog.

### UX

- [x] Add Run Wizard.
- [x] Add global Approval Inbox.
- [~] Replace raw IDs with searchable selectors (existing progressive workspace;
  remaining legacy tabs are intentionally retained).
- [x] Add execution timeline and correlation IDs.
- [x] Add capability-aware disabled states.
- [ ] Move legacy token UI to advanced／break-glass settings.

---

## 12. 目標狀態

完成以上改造後，專案應具備以下形狀：

- Control Plane、Scheduler、Worker、Node Agent 有獨立入口。
- FastAPI 只處理 API，不承擔整個 domain。
- Application service 表達使用案例。
- Domain state machine 不依賴 SQLite 或 FastAPI。
- Repository 與 Unit of Work 管理 persistence。
- 所有外部副作用都有 durable operation。
- Audit 是 transaction 內的持久證據。
- OIDC 與 RBAC 會實際阻止越權。
- Node Agent credential 與 workload 完全隔離。
- DB migration 可預覽、可執行、可驗證。
- Control Plane 與 Agent 可安裝為版本化 wheel。
- CI 同時驗證格式、型別、測試、migration、package、安全和前端。
- PR 小而可審查。
- UI 以「完成一次執行」為核心，而不是要求使用者理解所有內部 ID。
- 文件能清楚區分設計、實作、部署、canary 與 production-ready。

---

## 13. 本次檢查依據

主要參考檔案：

- `app/main.py`
- `app/db.py`
- `app/config.py`
- `app/authorization.py`
- `app/audit.py`
- `app/execution_attempt_schema.py`
- `app/node_registry.py`
- `agent/__main__.py`
- `agent/client.py`
- `agent/runner.py`
- `agent/supervisor.py`
- `agent/dispatch-node-agent.service`
- `deploy/dispatch-center.service`
- `.github/workflows/ci.yml`
- `pyproject.toml`
- `requirements.txt`
- `requirements.lock`
- `tests/test_db_migration.py`
- `tests/test_node_safety_hardening.py`
- PR #20 與 merge commit `7ecbec6881765ff948eaa5633fd80c785d949915`

---

## 14. 第一個實作里程碑

第一個里程碑不應是「再增加一個功能」，而應是：

> 一位經 OIDC 登入且具 Project Operator 權限的使用者，可以透過穩定 API／UI，建立一個不可變 Run Request；另一位具核准權限的使用者核准後，由 SSH backend 安全執行；所有狀態、audit、result 和 lineage 可追蹤；越權操作會被後端實際拒絕；整個流程可從版本化 package 部署並透過正式 migration 升級。

這條 Golden Path 穩定後，再正式啟用 Node Agent、進階自動排程和更高程度的 AI 自動化。
