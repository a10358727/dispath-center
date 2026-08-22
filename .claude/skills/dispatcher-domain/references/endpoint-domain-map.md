# Endpoint / Table ↔ 新 Domain Model 對照(Phase 0 盤點)

> **狀態:已被取代的歷史快照(2026-07-12),只作為當時盤點的證據保存。**
> 本檔的判定欄與端點清單不再反映現況:此後已加入 API v2 router 邊界
> (`dispatch_center/api/`)、Product v2 的 Project/Environment/Run Template/
> Dataset/ExecutionPlan/Run 端點、identity 與 RBAC 端點、node 端點等。
> 需要現況時請直接讀 `app/main.py` 與 `dispatch_center/api/routers/`;
> 需要結構模型時讀 `development-platform.md`;需要能力狀態讀
> `docs/CAPABILITY_LEDGER.md`。**不要依本檔規劃或實作。**

> 依 PLAN.md Phase 0 要求盤點,2026-07-12。標示:
> **keep** = 行為不變(可能加欄位/參數);**refactor** = 併入新 domain model,
> 過渡期雙軌相容;**deprecate** = 有替代後移除(先出 deprecation 訊息)。
> 本檔是盤點快照,實作以 PLAN.md 與 invariants 為準,完成對應切片後更新。

## DB tables(app/db.py)

| Table | 判定 | 對應新 model / 動作 |
|---|---|---|
| `jobs` | refactor | 切片 8~9 過渡為 JobSpec/Run/Attempt;舊列保留 command 事實欄,缺 version/snapshot 標 `legacy_unpinned`(§12) |
| `approvals` | keep | 加 plan hash(切片 9);Phase 7 加 requester/approver/expiry |
| `projects` | refactor | 切片 1:加 `id` UUID(additive)、name 降為 slug;之後加 state/hub 欄位(§6.1) |
| `project_instances` | refactor | 切片 1~2:加 `state`(available/missing/dirty/diverged/unknown)與 project_id FK |
| `project_candidates` | keep | 切片 3:status 增加「已連結既有 Project」路徑 |
| `datasets` | refactor | 切片 7:過渡到 DatasetSnapshot;舊列登記 legacy snapshot、hash 標 unknown(§12) |
| `dataset_cache` | refactor | 切片 7:過渡為 DatasetReplica state machine(missing/transferring/verifying/ready) |
| `coding_runs` | keep | 切片 8 併入 Run domain 前不動 |
| `experiment_records` | keep | Phase 5 experiment timeline 的基礎 |

## API endpoints(app/main.py)

### Project 生命週期
| Endpoint | 判定 | 備註 |
|---|---|---|
| `POST /projects` | refactor | 切片 1 起回傳/接受 UUID;name 過渡雙軌 |
| `GET /projects`、`/projects/matrix`、`/{name}/instances`、`/{name}/activity`、`/{name}/detail`、`/{name}/timeline`、`PATCH /{name}` | keep | 加 id 查詢支援;URL 的 `{name}` 長期 deprecate 改 `{id}`(過渡期兩者都收) |
| `GET /{name}/files`、`GET /{name}/file` | keep | 唯讀瀏覽 |
| `POST /{name}/records`、`PATCH`/`DELETE records/{id}` | keep | timeline 人工紀錄(低風險明文例外) |
| `POST /{name}/git-init-request`、`/apply-patch-request`、`/coding-task-request`、`/deploy-request`、`DELETE /{name}` | keep | approval 入口;deploy 於切片 4 起固定 ProjectVersion commit |
| `POST /{name}/hub-sync` | keep | 冪等例外已明文;切片 4 產出 ProjectVersion |

### Inventory
| Endpoint | 判定 | 備註 |
|---|---|---|
| `POST /inventory/scan`、`GET /candidates`、`GET /candidates/{id}`、`POST manual`、`ignore-request`、`ignore-nested-request` | keep | |
| `POST /candidates/{id}/import-request` | refactor | 切片 3:增加「連結既有 Project」模式,不再只能新建 |

### Dataset
| Endpoint | 判定 | 備註 |
|---|---|---|
| `POST /datasets` | refactor | 切片 7 後由 snapshot builder 取代主路徑;過渡期保留 |
| `GET /datasets`、`GET/PATCH card` | keep | |

### Job / Approval / 執行
| Endpoint | 判定 | 備註 |
|---|---|---|
| `POST /jobs` | refactor | 切片 8~9:JobSpec 優先、raw command 相容(§8.1) |
| `GET /jobs`、`/{id}`、`/{id}/log`、`POST cancel/stop`、`POST /{id}/diagnose` | keep | |
| `POST /dispatch` | keep | 手動觸發排程輪 |
| `GET /approvals`、`POST /approve/{id}`、`/reject/{id}` | keep | 切片 9 加 plan-hash 重驗 |

### 其他
| Endpoint | 判定 | 備註 |
|---|---|---|
| `GET /`、`/servers`、`/events`、`/audit`、`server-config/*`、`codex-runner/status`、`coding-runs*` | keep | |
| `POST /agent/chat`、`GET /agent/tools`、`POST /agent/cmd`、`WS /ws` | keep | Phase 6 增加 project-centric tools,不改權限邊界 |

**目前沒有立即 deprecate 的端點**;唯一的長期棄用是 name-based project URL
(等 UUID 雙軌驗證完成,§12)。
