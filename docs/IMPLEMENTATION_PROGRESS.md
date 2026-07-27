# Implementation Progress

> 對應計畫：`docs/NEXT_IMPLEMENTATION_PLAN.md`
>
> 規則：只有具備程式、測試及驗證證據的項目才標 `completed`；尚缺證據的
> 項目維持 `in_progress`，不以「已建立檔案」冒充完成。

## 2026-07-27

### WP-0A-1 — TestClient / shutdown baseline

**Status:** `completed`

**Outcome**

- 分離出 Codex filesystem sandbox 的 AnyIO blocking-portal 限制：空白
  FastAPI TestClient 在沙箱內會停在進入點，但在一般本機環境能正常進出。
- 找到並修正真正的 application shutdown race：lifespan 取消
  `monitor_loop` 時，executor thread 仍在操作 SQLite，主執行緒卻已關閉同一
  connection，曾造成 Python segmentation fault。
- `AppState` 現在追蹤並 shield blocking executor futures，shutdown 會先排空
  它們再關閉共用資源；`Database.close()` 同時取得 connection lock 作最後
  防線。

**Changed**

- `app/main.py`
- `app/db.py`
- `tests/test_main_background_hooks.py`
- `scripts/testclient_smoke.py`

**Evidence**

- `timeout 20s .venv/bin/python scripts/testclient_smoke.py`
  → `TestClient lifecycle smoke: PASS`
- shutdown regression
  `test_stop_background_tasks_drains_blocking_db_call_before_close`
  → PASS
- 修正後完整測試不再於約 4% 發生 SQLite segmentation fault。

**Safety**

- 全部使用 in-process FastAPI、temporary SQLite 與本機 thread。
- 未連 production server、SSH、OIDC provider、LLM 或外部服務。

### WP-0A-2 — Deterministic offline pytest configuration

**Status:** `completed`

**Outcome**

- 新增 `pyproject.toml`，固定 test discovery、pytest-asyncio mode/loop scope
  與 faulthandler timeout。
- repository suite 預設拒絕 external socket/DNS，只允許 Unix/loopback；
  CI 再明確設定 `DISPATCH_TEST_NETWORK=deny`。
- network guard 的 boundary tests 不需要真的建立 AF_INET socket，因此在更
  嚴格的 filesystem sandbox 也能執行。
- server-update approval 測試改用 `tmp_path` 內 mode `0600` 的假 SSH key，
  不再依賴開發者家目錄，且沒有放寬 production key validation。

**Changed**

- `pyproject.toml`
- `tests/conftest.py`
- `tests/test_test_environment.py`
- `tests/test_agent_tools.py`
- `README.md`

**Evidence**

- `DISPATCH_TEST_NETWORK=deny ... pytest ...`
  → 62 passed
- `python -m py_compile ...` → PASS
- `git diff --check` → PASS

### WP-0A-3 — Clean current-environment suite baseline

**Status:** `completed`

**Outcome**

- 修正 server reload cache maintenance 意外擴大的 API response；移除已刪節點
  的 in-memory state 行為保留，公開 response 恢復既有 `{"ok": true}`。
- subprocess timeout 現在會在 kill 後排空 stdout/stderr transports，避免
  cancelled `communicate()` 在 event loop 關閉後才由 GC 清理。
- pytest parametrization 改為先 materialize cases，消除 pytest 10 相容性
  警告。
- 以三個明確、不重疊的 test-file groups 跑完 repository 目前全部 2892 tests，
  避免單次工具 300 秒上限把部分通過誤報成完整綠燈。

**Evidence**

- Group 1: 1226 passed
- Group 2: 828 passed
- Group 3: 838 passed
- Total: **2892 passed, 0 failed**
- `bash .claude/skills/release-gate/scripts/static_checks.sh`
  → PASS
- `python -m pip check` → no broken requirements
- `tests/test_requirements_lock.py tests/test_localrun.py
  tests/test_authorization.py tests/test_test_environment.py`
  → **161 passed**
- 加入 lock-sync tests 後完整 test discovery
  → **2896 tests collected**

**Historical warning status**

- Current local environment emits Starlette's TestClient/httpx deprecation
  warning because that既有環境尚未安裝新 lock 中的 `httpx2`。
- 原先的 pytest parametrization 與 asyncio subprocess finalizer warnings
  已由上述聚焦測試確認修正；exact-lock 環境的完整 suite 為 0 warning。
  Exact-lock CI 仍是 locked dependency combination 的最終依據。

### WP-0A-4 — Exact dependency lock and CI

**Status:** `completed`

**Implemented**

- `requirements.txt` remains the top-level dependency manifest.
- Added `httpx2>=2.9,<3.0` as Starlette 1.3+ TestClient's test backend while
  retaining `httpx` for application HTTP clients.
- Regenerated the Python 3.10 SHA-256 `requirements.lock`; the current lock
  includes exact `httpx2`, `httpcore2`, and `truststore` pins.
- Added `scripts/check_requirements_lock.py` plus boundary tests. CI now fails
  when a direct manifest dependency is missing, not exact-pinned, or outside
  its declared version constraint.
- Added bounded TestClient smoke and GitHub Actions flow:
  exact hash install → manifest/lock sync → smoke → collect → static gate →
  migration/core → complete suite.
- CI clears credential/provider settings and runs the suite with external
  network denied.
- Release dependency drift remains fail-closed: only the exact approved
  Authlib and httpx2 specs may differ from the current Git baseline.

**Evidence**

- `python scripts/check_requirements_lock.py`
  → `PASS (11 direct requirements)`
- `pip-sync --dry-run requirements.lock`
  → lock parsed successfully and produced a complete exact install plan
- static invariant gate → PASS
- CI workflow YAML parse → PASS
- `git diff --check` → PASS
- Fresh Python `3.10.12` venv:
  `/tmp/dispatch-center-wp0a-final-rdCbk3/venv`
- Lock SHA-256:
  `d74395fac8cdec0c332939d1736213bdb60123661414d63b762ba0fb372aa167`
- `pip install --require-hashes -r requirements.lock` → PASS
- `python -m pip check` → `No broken requirements found`
- Locked versions include FastAPI `0.140.0`, Starlette `1.3.1`,
  httpx `0.28.1`, and httpx2 `2.9.1`.
- `timeout 20s ... scripts/testclient_smoke.py`
  → `TestClient lifecycle smoke: PASS`
- Locked test discovery → `2896 tests collected`
- Locked complete suite → **2896 passed in 426.92s**, zero failures.

**Gate**

- WP-0A is complete. Its fresh-lock, smoke, static, migration/core and complete
  suite evidence is green, so WP-0B may now begin.

### WP-0B — Release gate and capability truth

**Status:** `completed`

**Outcome**

- CI 現在固定執行 exact hash install、manifest/lock sync、bounded TestClient
  smoke、Node primitive-only smoke、完整 collect、static invariant、
  migration/core、full suite 與 checkout pollution gate。
- CI 的 DB/audit/server config/auto-approve/home paths 全部指向
  `${{ runner.temp }}`，provider/credential settings 清空，external
  socket/DNS fail closed。
- `deploy/backup.sh` 改用 Python stdlib SQLite online backup，不再依賴系統
  `sqlite3` CLI；backup/restore round trip、拒絕未輸入明確 `yes`、displaced
  recovery 都只使用 temporary fixtures。
- 新增 capability ledger，嚴格區分 `implemented / test-only /
  default-enabled / deployed / canary-proven / production-ready`；沒有實地
  證據的欄位保持 `unknown/no`。
- Node 現況明確改為 protocol/client/runner primitives；因
  `agent/__main__.py` 不存在，service unit 只標為不可安裝的 template，
  沒有冒充 runnable daemon 或 canary-ready backend。
- 舊 roadmap/status 文件加入 `superseded_by` 與權威順序，不刪除歷史。
- 已用 characterization tests 固定三個 release blockers：
  stop kill unreachable 仍產生 false terminal、legacy dataset endpoint
  直接寫入且沒有 approval、server mutation 尚無 generic attempt/config
  revision guard。這些測試記錄缺陷，不把缺陷宣稱為正確 invariant。

**Changed**

- `.github/workflows/ci.yml`
- `.claude/skills/release-gate/scripts/static_checks.sh`
- `deploy/backup.sh`
- `scripts/sqlite_online_backup.py`
- `scripts/node_primitives_smoke.py`
- `docs/CAPABILITY_LEDGER.md`
- historical roadmap/status headers and Node service template
- WP-0B release/capability/authority/backup/Node/drift tests

**Evidence**

- Fresh locked Python 3.10 environment:
  `/tmp/dispatch-center-wp0a-clean-v2`。
- `pip install --require-hashes -r requirements.lock` → PASS。
- `python -m pip check` → `No broken requirements found.`。
- `python scripts/check_requirements_lock.py`
  → `PASS (11 direct requirements)`。
- backup/Node/capability/document/test-environment focused gate → 17 passed。
- stop/dataset/server characterization suites → 131 passed。
- migration/core suites → 88 passed。
- CI workflow contract → 3 passed。
- full discovery → 2909 tests collected。
- `DISPATCH_TEST_NETWORK=deny python -m pytest -q`
  → **2909 passed, 0 failed in 427.17s**。
- `bash .claude/skills/release-gate/scripts/static_checks.sh` → PASS。
- `git diff --check` and shell syntax checks → PASS。

**Safety / boundary**

- 未停止、重新載入或操作既有執行中服務；未連 production worker、OIDC、
  LLM 或外部服務，未讀 production credential。
- backup/restore test 只操作 pytest temporary directories；沒有對現存
  runtime DB 做備份、還原或 migration。
- GitHub-hosted CI 尚待 commit/push 後產生第一次 remote run；本紀錄只宣稱
  workflow contract 與等價本機 gate 通過。

**Gate**

- Phase 0 / WP-0A / WP-0B 本機實作與驗證完成。
- `DG-EXEC-ATTEMPT-v1` 已於 2026-07-27 具名核准；下一步為 WP-1A additive
  schema 與 pure domain tests。仍不切換 scheduler、不接觸 production
  SSH/Node backend。

### DG-EXEC-ATTEMPT — Decision-contract preparation

**Preparation status:** `completed`

**Decision status:** `approved 2026-07-27`

**Outcome**

- 新增 `docs/DG_EXEC_ATTEMPT_DECISION.md`，把 Phase 1 前置決策從 roadmap
  摘要展開為可逐條裁定的 exact contract。
- 固定 attempt/operation/Job projection、partial unique active constraint、
  immutable payload/target/authorization、outbox `effect_started_at` crash
  boundary、append-only events、shadow-only Phase 1 與 leader fencing。
- legacy approval/server/Node rows維持 honest legacy，不補造 digest、revision
  或 attempt；新 generic path 不設 nullable legacy authorization 例外。
- 明列 server delete/repoint/rotation dual-read guard、routine rotation 與
  emergency revoke 的不同語意。
- 確認本 gate 不修改 `jobs.status`、canonical invariants、SSH command/
  sentinel contract 或 production routing；ambiguous launch 仍留給
  `DG-AMBIGUOUS-LAUNCH`。
- 實作可行性複核補齊：
  - multi-Job approval 的 stable role/command digest mapping；
  - `servers.yaml` 與 SQLite 間的 prepared/publication mutation journal 及
    每一個 crash window；
  - reload 失敗時 only-exact-digest compensation，以及 digest drift 的
    `recovery_hold`；
  - 沒有 generic attempt 的 legacy running Job stop-intent；
  - Node protocol row linkage、non-cascading FK/immutable triggers。
- 第二次一致性複核再修正：
  - `uncertain` operation 只可依 read-only authoritative evidence 收斂，
    不會重送 material effect；
  - legacy SSH stop intent 與 generic `operation=stop` 分開持有，不雙寫成
    兩個真相來源；
  - credential revision 只用 `realpath/stat` 的版本化檔案參照，不開啟或
    hash 私鑰內容；
  - approval ID 與 payload digest 以不可變 tuple 綁定，避免把自增 ID
    嵌入自己的 hash 所造成的循環；
  - 所有 attempt 相關 runtime flags 預設關閉，已有 active ownership 時
    關閉 reconciler 會 fail closed。
- 同步修正 `docs/NEXT_IMPLEMENTATION_PLAN.md` 的 Phase 1 摘要，使其不再
  保留「generic attempt 可有 nullable legacy authorization」、私鑰內容 hash、
  approval ID 嵌入自身 digest 等已被 exact contract 收斂掉的舊描述。
- 新增 `tests/test_exec_attempt_decision_gate.py` 並由 static release gate
  釘住。核准前的 characterization 已證明新 schema、feature flags 與
  `attempt_cleanup` authority 沒有提前進入 runtime。

**Evidence**

- decision-gate characterization → 4 passed。
- decision/capability/document focused set → 10 passed。
- full discovery（含 decision-gate tests）→ 2913 tests collected。
- locked Python 3.10 complete suite
  → **2913 passed, 0 failed in 371.58s**。
- `DG-EXEC-ATTEMPT-v1` review document SHA-256
  → `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`。
- static invariant gate → PASS。
- `git diff --check` → PASS。

**Gate**

- 使用者於 2026-07-27 明確回覆
  `DG-EXEC-ATTEMPT：核准本文件的 recommended contract`。
- 此裁定已以 reviewed-draft SHA-256
  `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`
  綁定 `DG-EXEC-ATTEMPT-v1` 並寫入 `docs/DECISIONS.md`。
- WP-1A additive schema、窄版 DB/domain APIs 與 pure tests 已解除阻擋；
  production migration/cutover、scheduler 切換、遠端連線及後續 named gates
  均未獲授權。

### WP-1A — Attempt/outbox additive foundation

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-1A-0 decision authorization`：`completed`
   - 凍結核准當下 reviewed draft SHA-256。
   - 將具名裁定寫入 `docs/DECISIONS.md`，並把 decision document 改為
     approved。
   - 明列只解鎖 additive schema、窄版 API 與 pure tests；所有 execution
     flags 維持預設關閉。
2. `2026-07-27` — `WP-1A-1 additive schema and migration`：`completed`
   - 新增 `server_config_revisions`、`server_config_mutations`、
     `execution_attempts`、`execution_operations`、
     `execution_attempt_events`、`execution_shadow_observations`、
     `legacy_job_stop_intents` 與 `scheduler_leases`。
   - 對 `jobs`、`approvals`、`node_attempts` 採 additive nullable migration；
     representative legacy rows 維持 NULL，未補造 approval、digest、revision
     或 attempt。
   - 每個新 Database connection 在 schema initialization 前啟用並驗證
     `PRAGMA foreign_keys=ON`；新 FK 全部 `ON DELETE RESTRICT`，沒有 cascade。
   - 建立 active-attempt、active-revision、unresolved-mutation、legacy-stop
     partial unique indexes，以及 pinned approval/Job、target/attempt/operation
     identity、append-only event/shadow 的 DB triggers。
   - 新增 canonical JSON/command digest 驗證；float、NaN、非字串 key、
     command base64/digest 不一致均 fail closed。
   - 核准文件的 creation-event requirement 原先缺少成功 reason code；依該
     文件允許的「新增 code 必須有文件＋測試」規則，補入窄義
     `contract_validated`。它不表示 remote effect 已開始或完成。
   - WP-0B「新表不存在」characterization 已替換為 WP-1A schema-present、
     WP-1B dual-read guard 尚未接線的誠實契約。

   **Evidence**

   - fresh/legacy migration、immutability、race/outbox/config focused set
     → **111 passed**。
   - DB/API/approval/Node/server-config/background related set
     → **441 passed**。
   - static invariant gate → PASS。
   - `git diff --check`、Python compile → PASS。
   - 全部測試仍待 `WP-1A-3` 執行；此處不以 focused tests 取代 full gate。
3. `2026-07-27` — `WP-1A-2 transaction/CAS domain foundation`：`completed`
   - 新增 `BEGIN IMMEDIATE` material transaction boundary、SQLite-time
     scheduler lease acquire/renew/failover 與 live owner/epoch fencing。
   - attempt creation 會在同一 transaction 重驗 pinned Job/approval/command、
     active approved target revision、legacy Node owner、leader lease，建立
     attempt 與 append-only event；SSH 只建立 `dispatching` DB intent，
     沒有呼叫遠端。
   - Node generic path 只允許在同一 transaction 建立並連結新的
     `node_attempts.execution_attempt_id`；legacy Node rows維持 NULL，沒有
     retrofit。
   - attempt state/liveness 及 operation result 全採 expected-state CAS；
     unknown/unreachable 不改 Job terminal state，done/failed 只接受 matching
     terminal evidence。
   - outbox claim 只會重取 `effect_started_at IS NULL` 的過期 processing
     operation；effect boundary 之後過期會進 `uncertain`，不再送 material
     effect。
   - operation authorization class 重驗 actual approval kind/digest；
     execution approval 不可被 stop 重用。`inspect` 的 backend-specific fixed
     read-only allowlist 尚未由後續 gate/version pin 定義，因此 WP-1A API
     fail closed，不接受任意 shell payload。
   - server-config publication 以單一 transaction 建立 approval
     materialization marker、mutation intent 與 prepared revision；只有 exact
     before/after YAML digest 可 rollback/resume，第三 digest 進
     `recovery_hold`，final activation 同 transaction 更新 revision、journal
     與 approval。
   - 修正 leader failover fencing 語意：attempt 上的 epoch 是 immutable
     creation evidence；每次後續 mutation 另驗 current live lease，因此舊
     leader 被 fence、新 leader仍能 reconcile，而不改寫 creation epoch。
   - 補入 `remote_state_observed` reason code，僅代表 matching positive
     acknowledgement/receipt/sentinel/agent evidence；不由 reachability loss
     推導。
   - 四個 rollout flags 全部預設 `false`；`NEW_CLAIMS=true` 缺
     reconciler/outbox 時 config fail closed。有 durable active ownership
     而 `RECONCILE_EXISTING=false` 時，AppState 在任何 SSH/provider/background
     建立前拒絕啟動。

   **Evidence**

   - foundation/decision/config focused set → **83 passed**。
   - migration/server/Node/background related set + static gate
     → **396 passed**，static PASS。
   - two-connection active-attempt race、raw partial-unique bypass、
     stale/new leader failover、pre/post-effect claim expiry、server YAML
     drift/compensation 與 terminal evidence projection 均有專項測試。
   - 未建立 public API/UI/LLM/MCP mutation route，未接 scheduler/outbox
     background loop，未進行任何 SSH/Node/production DB 操作。
4. `2026-07-27` — `WP-1A-3 release evidence and handoff`：`completed`
   - full discovery → **2933 tests collected**。
   - 第一輪 full suite → **2932 passed, 1 failed**；唯一失敗是 WP-0B
     capability-ledger characterization 仍要求
     `execution_attempt_outbox.implemented=no`，與已完成的 WP-1A
     test-only foundation 不符。
   - 更新 ledger test 後，精確釘住 `implemented=yes`、`test-only=yes`，
     且 default/deployed/canary/production-ready 全為 `no`；其他未來
     cutover capability 仍為 `implemented=no`。
   - 第二輪 locked Python 3.10 full suite
     → **2933 passed, 0 failed in 492.89s**。
   - final static invariant gate、requirements lock sync、TestClient lifecycle
     smoke、`git diff --check` → 全部 PASS。
   - `tests/test_execution_attempt_foundation.py` 已加入 static
     `INV-TEST-2` pinning 清單。
   - 核准當下 reviewed-draft SHA-256 保持
     `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`；
     加入核准狀態與兩個依文件規則擴充且有測試的 reason/fencing clarification
     後，current implementation-contract document SHA-256 為
     `09aa99ebcc80e5b7884121aae3d4c290a0f9259cc8b605d66976c69f6a1e88dd`。

**WP-1A outcome**

- additive DB/domain foundation 完成，legacy SSH scheduler 行為未切換。
- 四個 flags 全為 default-off；沒有 production migration/deployment/canary
  證據，capability 正確標為 test-only、production-ready=no。
- 未操作現有 runtime DB、未連 SSH/Node/OIDC/LLM 或任何 production service，
  未讀 production credential。
- 下一工作包為 WP-1B；不可把 WP-1A schema/API pass 誤稱為 runnable
  attempt-driven execution。

**Next**

- `WP-1B`：pinned approval-to-Job publication、generic/legacy dual-read server
  mutation guard、shadow parity。所有 runtime flags 仍須維持關閉。

### WP-1B — Pinned publication, ownership guards and shadow parity

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-1B-1 atomic pinned Job publication`：`completed`
   - 新增 internal `materialize_pinned_execution_jobs()`；同一
     `BEGIN IMMEDIATE` transaction 重新驗證 pending approval、canonical
     payload/version/digest，寫 one-way materialization actor/mechanism，
     topological materialize 全部 role Job，最後才將 approval 設 approved。
   - `job_specs` 可依 stable role 指向 dependency role；publication 會解析
     dependency graph，產生實際 `depends_on` Job IDs，但 approved payload
     不嵌入 DB-generated ID。
   - 每個 Job 在 INSERT 同時固定 approval tuple、contract role、command
     digest 與 dispatch constraints；新增 partial unique role index，禁止同
     approval 重複 role。
   - 任何第二筆 Job 的 shape/constraint 錯誤會 rollback 第一筆 INSERT、
     materialization marker 與 actor attribution，不留下 partial chain。
   - 兩個 Database connections 同時 materialize 同一 approval 時恰好一個
     成功，另一個 fail closed；最後只有一條完整 Job graph。
   - 這是 internal DB/domain API；既有 public enqueue/approve、legacy SSH
     scheduler 與所有 runtime flags 均未變更。

   **Evidence**

   - foundation + representative legacy migration + decision gate
     → **58 passed**。
   - multi-Job reverse-order dependency、mid-publication rollback、
     two-connection race 與 unique role 均有專項測試。
2. `2026-07-27` — `WP-1B-2 dual-read server mutation guard`：`completed`
   - 新增 `get_server_execution_blockers()`，同時讀取 active generic
     attempt、pending/processing/uncertain operation、unlinked 且未過期/
     acknowledged/running 的 legacy Node attempt、enrolled non-revoked Node
     與 legacy running Job。
   - `server_delete` 在核准當下重驗上述 durable owners；任一存在即將
     approval 設 rejected，不改 `servers.yaml`、不清 in-memory config。
   - `server_update` 對 host/user/port/key/root sets/backend 的實際 repoint
     或 routine credential rotation 套用同一 guard；note/tags 等非 target
     欄位不被過度阻擋。Disable 仍保留設定與 revision 可解析性，沿用既有
     running-Job guard。
   - 任一 unresolved `server_config_mutations`
     `intent|yaml_applied|recovery_hold` 會全域阻擋既有 add/update/disable/
     delete approval 落地，legacy HTTP flow 不得繞過 recovery journal。
   - terminal workload 後仍 pending 的 collect operation 會繼續阻擋 target
     delete/repoint；已撤銷 Node 上 acknowledged legacy attempt 也不因撤權
     被誤當成可安全刪除。
   - 所有 rejection 只寫 DB decision/audit 與 blocker counts，不執行 SSH、
     不讀 credential bytes。

   **Evidence**

   - server-config API + foundation + attribution + approvals
     → **107 passed**。
   - enrolled Node delete/repoint、display-only update、unresolved global
     journal bypass、terminal-attempt pending operation、revoked-node legacy
     attempt 均有專項測試。
3. `2026-07-27` — `WP-1B-3 default-off shadow parity`：`completed`
   - 新增獨立 `execution_attempt_shadow_loop()`；只有
     `EXECUTION_ATTEMPT_SHADOW_ENABLED=true` 才執行，rollback/default-off
     時連 shadow DB method 都不呼叫。
   - 每輪只讀既有 Job、pinned approval、active approved target revision
     與 unresolved publication journal，且只 append
     `execution_shadow_observations`；不建立/更新 Job、attempt、operation、
     event、Node attempt 或 scheduler lease。
   - pinned contract/command digest/target revision 符合時記
     `eligible_*`；honest legacy Job、缺 approval/revision 或 digest 不符時
     記 `ineligible_*` 與 closed reason code，不補造歷史 approval、revision
     或 digest。
   - 同一 Job 狀態未改變時不重複產生觀測；狀態改變才記下一筆 before/after
     evidence。shadow 表的 UPDATE/DELETE 由 DB trigger fail closed。
   - loop 不接收 SSH/Node callback，也不呼叫 legacy scheduler；blocking DB
     call 仍納入 AppState shutdown drain，避免關閉 SQLite 的競態。

   **Evidence**

   - shadow/foundation/background/config/decision focused set
     → **101 passed**。
   - API/approval/server-config/Node/document expanded set
     → **179 passed**。
   - canonical tables 逐表 before/after snapshot、legacy ineligible evidence、
     missing target、append-only、zero attempt/outbox/lease、zero SSH 與
     default-off rollback 均有專項測試。
   - static invariant gate、requirements lock sync、Python compile、
     `git diff --check` → PASS。
4. `2026-07-27` — `WP-1B-4 release evidence and handoff`：`completed`
   - locked Python 3.10 full discovery → **2947 tests collected**。
   - external-network-denied full suite
     → **2947 passed, 0 failed in 449.41s**。
   - final static invariant gate、requirements lock sync、TestClient lifecycle
     smoke、Python compile、`git diff --check` → 全部 PASS。
   - 唯一 warning 是既知 Starlette TestClient/httpx deprecation；未影響測試
     結果，亦不是本工作包引入的 runtime failure。
   - 全程未連 SSH/Node/OIDC/LLM/production service，未讀 production
     credential，未修改現有 runtime DB；所有 execution rollout flags
     維持預設 `false`。

**WP-1B outcome**

- 完整 pinned multi-Job publication、server dual-read mutation guard 與
  default-off shadow parity 已完成並具 release evidence。
- shadow 只提供 append-only 測量；沒有 scheduler cutover、new claim、
  outbox worker 或 production attempt ownership。
- legacy SSH 仍是既有支援 backend；generic claim 仍須等待 pinned
  server-config public publication/recovery surface 及後續具名 cutover gate。

**Next**

- `WP-1C`：修正 legacy SSH stop false-terminal defect；approved stop 只保存
  durable intent，kill failure/unreachable 不得把 Job 標成 terminal，並防止
  stop 後被舊 scheduler 自動 requeue。這是 release blocker
  `RB-STOP-001`，不等同於啟用 generic execution。

### WP-1C — Legacy stop invariant correction

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-1C-1 pinned stop intent and delivery boundary`：
   `completed`
   - `request_stop_approval()` 改為在 INSERT 時建立 canonical
     `stop-intent-v1` payload digest；payload/version/timestamp 由既有 pinned
     approval trigger 保持 immutable。
   - 既有 pending unpinned stop approval 不補造 digest，核准時在任何遠端
     effect 前 fail closed，要求 reject/re-create。
   - 新增 legacy stop intent domain methods：在遠端前建立 `requested`，
     一次性跨越 delivery boundary 成為 `delivery_uncertain`，positive kill
     response 才成為 `delivered`。
   - crash/retry 若已跨 effect boundary 不重送 kill；已記 delivered 則只完成
     pending approval。raw remote exception 不寫入 stop-intent evidence，
     新表只存固定 category 與 sanitized detail。
   - generic attempt Job 不得建立 competing legacy intent；必須走後續
     generic stop operation。
2. `2026-07-27` — `WP-1C-2 canonical Job/reconcile correction`：
   `completed`
   - kill 成功或失敗都不再寫 `running → cancelled`/`finished_at`；成功表示
     delivered，失敗/timeout/unreachable 表示 delivery uncertain，Job 均
     保持 running。
   - unresolved stop intent 在同一 SQLite `BEGIN IMMEDIATE` 邊界阻止
     `running → queued`，並在 legacy scheduler 與 Node/SSH dispatch
     eligibility 兩層 fail closed，避免 tmux gone 後重新啟動。
   - matching done/failed reconcile 寫 Job terminal 時，同一 DB transaction
     把 intent 轉 `terminal_observed`；finish hook 仍只觸發一次。
   - unresolved intent 也納入 server delete/repoint ownership blocker；routine
     target mutation 不得讓 recovery 失去原 server identity。
   - 更新 REST/web-direct/auto-rule、Engineering Task/validation 與使用說明
     的舊 `cancelled` 契約；queued cancel 的既有 `cancelled` 行為不變。

   **Evidence**

   - approvals/API/scheduler/Node/server-config/Engineering/foundation focused
     set → **367 passed**。
   - pinned immutability、unreachable、definite pre-effect refusal、
     crash-no-replay、requeue/dispatch block、terminal convergence 與
     unpinned legacy refusal均有專項測試。
3. `2026-07-27` — `WP-1C-3 release evidence and handoff`：`completed`
   - locked Python 3.10 full discovery → **2951 tests collected**。
   - 第一輪 full gate 在舊 Agent tool assertion 發現仍期待
     `status=cancelled`；以 `-x` 精確重現為 1 failed / 182 passed，更新
     Agent/MCP fake response 與契約測試後，相關 set → **320 passed**。
   - 從第 1 項重跑 external-network-denied full suite
     → **2951 passed, 0 failed in 500.47s**。
   - final static invariant gate、requirements lock sync、TestClient lifecycle
     smoke、Python compile、`git diff --check` → 全部 PASS。
   - 唯一 warning 是既知 Starlette TestClient/httpx deprecation；未進行
     SSH/Node/production DB/credential 操作，所有 generic execution flags
     仍為預設 `false`。

**WP-1C outcome**

- `RB-STOP-001` 已解除：approved stop 不再製造 false terminal，unknown/
  unreachable 也不會 requeue 或重派；只有 matching terminal evidence
  收斂 done/failed。
- queued Job 的既有 user cancel 仍可寫 `cancelled`；本工作包沒有新增狀態或
  核准 `running → cancelled`，因此不需要也沒有改動 `DG-JOB-STATE`。
- legacy SSH backend 保留；本修正不是 generic attempt cutover，也未解決
  ambiguous launch。

**Next**

- `WP-2A`：minimum scheduler leader/fencing 與 outbox telemetry；只做
  internal/default-off ownership/observability。`WP-2B/2C` remote atomic
  launch/receipt/cutover 仍受 `DG-AMBIGUOUS-LAUNCH` 阻擋。

### WP-2A — Minimum scheduler ownership and execution telemetry

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-2A-1 process ownership loop`：`completed`
   - `AppState` 新增 process-opaque owner UUID 與獨立 ownership loop；只有
     `NEW_CLAIMS`、`RECONCILE_EXISTING` 或 `OUTBOX_WORKER` 任一明確設定時，
     才讀寫既有 `scheduler_leases`。
   - lease acquire/renew 沿用 WP-1A 的 SQLite-time `BEGIN IMMEDIATE`
     transaction 與 fencing epoch；同 DB 的第二個 process 只能保持
     non-leader，無權建立 attempt、claim operation 或寫 transition。
   - lease duration 是 scheduler interval 的三倍（最低 5 秒）；shutdown
     不刪 durable lease，由 expiry/fencing 接手，避免把 process exit 誤當
     ownership transfer evidence。
   - default configuration 完全不建立 lease；loop 不接收 SSH/Node callback，
     未新增 remote launch/reconcile/outbox effect。
2. `2026-07-27` — `WP-2A-2 evidence-backed telemetry`：`completed`
   - 新增 read-only `Database.get_execution_control_plane_telemetry()`；lease
     liveness、queue/unknown/uncertain age 全以 SQLite clock 計算，不混用
     process wall clock。
   - telemetry 包含 attempt by state/backend、active unknown age、outbox
     backlog/uncertain age、operation/collection state、Job queue depth/age，
     以及只計既有 `claim_conflict` event 的 recorded duplicate-prevention
     lower bound。
   - `GET /execution-control/status` 已登錄為 authenticated platform-view
     read surface；回傳 current-process leader 判定與 rollout flags，但不回
     credential、remote log 或任意 command。
   - remote reconciler/outbox/new-claim worker 尚未實作時，status 明確回
     `implemented=false`、`reconciler_last_success_at=null`；不把 lease
     renewal 冒充 remote reconciliation success。
   - 本包重用既有 tables，沒有 schema migration，也沒有 fabrication/
     backfill historical attempt、revision、digest 或 metrics。
3. `2026-07-27` — `WP-2A-3 release evidence and handoff`：`completed`
   - foundation/background/API/config/authorization focused regression
     → **178 passed**。
   - 專項測試涵蓋 default-off 零 lease write、雙 process 單一 leader、
     loser fail closed/零 remote call、durable telemetry aggregate、空資料
     `null` 語意與 read route 零 mutation。
   - external-network-denied full suite
     → **2955 passed, 0 failed in 502.05s**。
   - document/capability/decision/CI/UI/authorization handoff set
     → **56 passed**。
   - static invariant gate、requirements lock sync、Python compile 與
     `git diff --check` → PASS。
   - 唯一 warning 仍是既知 Starlette TestClient/httpx deprecation；未連
     SSH/Node/OIDC/LLM/production DB，所有 execution flags 維持預設
     `false`。

**WP-2A outcome**

- minimum single-leader/fencing ownership 與 canary 前所需的 durable
  execution telemetry 已具 runtime surface 及 local regression evidence。
- 這不等於 generic scheduler/outbox 已可執行：status 會誠實顯示三個 remote
  worker 尚未實作，`execution_attempt_outbox` 仍非 deployed、非
  canary-proven、非 production-ready。
- `RB-LAUNCH-001` 未解除；沒有 atomic remote claim、receipt/token validation
  或 response-loss recovery，legacy SSH 仍是唯一支援執行路徑。

**Next**

- `WP-2B/2C` 仍受 `DG-AMBIGUOUS-LAUNCH` 阻擋。下一個可安全進行的工作是先
  提交該 gate 的 exact `INV-STATE-2` 修訂、definite-prelaunch boundary、
  atomic claim/receipt 與 replay contract 供核准；核准前不得實作或啟用
  remote attempt path。

### DG-AMBIGUOUS-LAUNCH / DG-EXEC-ATTEMPT-v1.1 — Gate rulings

**Status:** `approved (documentation only; no code changed)`

**Task log**

1. `2026-07-27` — `Defect re-verification before drafting`：`completed`
   - `app/scheduler.py:538-566` 確認為無條件 revert：`dispatch_job()` 的任何
     例外都執行 `db.update_job(job.id, status="queued", server=None,
     started_at=None)`。
   - `dispatch_job()`（`app/scheduler.py:297-308`）先 `prepare()` 後
     `launch()`，而 `launch()` 送出的是
     `tmux new-session -d -s job_{id} 'bash …/run.sh'`
     （`app/jobqueue.py:542-544`），故 tmux 已 fork 但 response lost 時仍會
     revert。`app/scheduler.py:529-537` 的既有註解只涵蓋 crash window。
   - `app/jobqueue.py:574-608` 的 double-check 無法補償此路徑，因為 revert
     之後 Job 已無 `server` 可供 reconcile。
2. `2026-07-27` — `DG-AMBIGUOUS-LAUNCH-v1 draft`：`completed`
   - 新增 `docs/DG_AMBIGUOUS_LAUNCH_DECISION.md`（493 行），內容涵蓋
     `INV-STATE-2` 逐字修訂、definite/ambiguous 分類、atomic remote claim /
     receipt / trap sentinel 協議、unknown 解析程序、additive schema delta、
     flags/rollout、crash matrix 與五項子裁定。
   - 核心設計為 **arbitration, not observation**：控制端必須自己贏得同一個
     `mkdir` claim 才能宣告 `abandoned_before_launch`，藉此消除
     settle-window 競態。
3. `2026-07-27` — `DG-EXEC-ATTEMPT-v1.1 addendum draft`：`completed`
   - 於 `docs/DG_EXEC_ATTEMPT_DECISION.md` 末尾附加 `Addendum v1.1`，
     §1–12 的已核准 v1 文字逐字未動，並在 addendum 檔頭記錄附加前 digest
     `09aa99eb…` 以保留證據鏈。
4. `2026-07-27` — `使用者裁定與記錄`：`completed`
   - 兩份草稿經使用者審閱後核准，具名裁定與 reviewed-draft SHA-256
     （`d38f2dea…`、`3a8eb432…`）記錄於 `docs/DECISIONS.md`。
   - `DG-AMBIGUOUS-LAUNCH-v1` 的 D-1…D-5 五項子裁定全部採用建議值。
   - 同步更新 `docs/NEXT_IMPLEMENTATION_PLAN.md` 檔頭狀態、§12 gate 表、
     §14 WP-2B/2C 阻擋狀態與 §16 下一切片，以及
     `docs/CAPABILITY_LEDGER.md` 的 `RB-LAUNCH-001`／`RB-SERVER-001` 兩列。

**Outcome**

- 兩個 gate 已核准，WP-2B/2C 與 `RB-SERVER-001` 的實作獲得授權。
- **沒有任何程式碼、schema、flag 或 canonical invariant 被修改。**
  `RB-LAUNCH-001` 仍然開啟：`app/scheduler.py:557` 的缺陷未動，legacy SSH
  仍是唯一支援的執行路徑。gate 核准不是修復。
- `INV-STATE-2` 的條文**刻意尚未寫入**
  `.claude/skills/dispatcher-domain/references/invariants.md`：其
  Verification 欄引用的 `tests/test_execution_launch_arbitration.py` 尚不
  存在，條文與測試必須在 WP-2B 同一個工作包內一起落地。

**Next**

- `WP-2B`：依 gate §3.2/§4/§5/§7/§12 實作 additive schema、pure builder、
  versioned launcher golden 與 FakeSSH crash matrix，並在同一包內 enact
  `INV-STATE-2` 新條文。不接 scheduler dispatch 路徑，flags 全部維持關閉。

### WP-2B — SSH launch arbitration primitives

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-2B-1 canonical INV-STATE-2 amendment`：`completed`
   - 依 `DG-AMBIGUOUS-LAUNCH-v1` §3.2 逐字改寫
     `.claude/skills/dispatcher-domain/references/invariants.md` 的
     `INV-STATE-2`：無條件 revert 條款改為
     **只有 definite pre-launch failure 可 `running → queued`**。
   - 新增 **Legacy exception** 欄位，明文記載 `app/scheduler.py` 的 legacy
     派發路徑仍無條件 revert，屬已登記缺陷 `RB-LAUNCH-001`，由 WP-2C 收斂；
     不得被引用為新程式碼的先例。條文與其 Verification 引用的測試檔在同一個
     工作包內落地。
2. `2026-07-27` — `WP-2B-2 pure launch primitives`：`completed`
   - 新增 `app/execution_launch.py`：attempt-scoped 路徑、per-attempt tmux
     session 命名、prepare/launch/abandon/inspect 純 builder、versioned
     launcher/wrapper bytes（`LAUNCHER_CONTRACT_VERSION = "v2"`）。
   - `mkdir "$D/claim"`（無 `-p`）是唯一仲裁原語；prepare 用 `mkdir -p`
     故無法仲裁；receipt 以 temp file + atomic rename 發布；wrapper 以 bash
     trap 產生真正的數字 sentinel，controller 不偽造終態。
   - 只有經驗證的整數 job id 與 UUID attempt id 會進入指令字串；fencing
     token 與使用者 command bytes 僅以 SFTP 檔案內容落地（`INV-SSH-4`）。
   - inspect 在 tmux 探測前後各讀一次 sentinel，關掉「工作剛好在探測間隙
     完成」被誤讀為消失的競態。
3. `2026-07-27` — `WP-2B-3 classification and resolution`：`completed`
   - definite/ambiguous 分類為封閉 allowlist，**預設 ambiguous**；未列舉的
     例外型別一律 ambiguous。transport 證據在 `effect_started` 之後不可採信
     （gate §4.1 rule 2），此後只有仲裁能產生 definite verdict。
   - `resolve_attempt_observation()` 實作 gate §6 解析順序；唯二可 requeue
     的情形是 controller 贏得 claim，以及 boot_id 變更（重開機為工作已死的
     正面證據）。「沒看到 tmux/sentinel」永遠不構成 requeue。
4. `2026-07-27` — `WP-2B-4 additive schema delta`：`completed`
   - `execution_attempts` 增 `remote_claim_state`、`launch_receipt_sha256`、
     `remote_boot_id`、`launcher_contract_version`、`prelaunch_verdict`；
     `execution_operations` 增 `transmission_state`；
     `server_config_revisions` 增 `attempt_backend_preflight`。
   - 新 DB 由 CREATE TABLE 的 CHECK 約束值域；`ALTER TABLE ADD COLUMN`
     無法帶 CHECK，因此另加 trigger 讓既有 DB 得到相同強度，並加上
     settled claim 單向、receipt/boot_id 證據不可改寫、
     `not_transmitted` 在 `effect_started_at` 之後不可宣稱等守衛。
   - legacy row 全部維持 `NULL`，不回填、不捏造觀測。
5. `2026-07-27` — `WP-2B-5 release evidence`：`completed`
   - 新增 `tests/test_execution_launch_arbitration.py`（54 tests），涵蓋
     gate §12 crash matrix：claim/tmux/receipt 三個崩潰點、雙 launcher 競爭、
     controller 仲裁對上進行中 launcher、response lost（`RB-LAUNCH-001`
     情境）、token 不符、跨 attempt 證據、重開機、探測間隙完成、unreachable。
   - 另以真實 `bash` 在隔離 scratchpad 執行 launcher bytes 驗證：第二個
     launcher 回 `LAUNCH_CLAIM_TAKEN` 且零 session；controller 先贏時
     launcher 全程未建立 tmux；trap 寫出 exit code 3；receipt key 為
     canonical 排序。未接觸任何 production worker 或 runtime 檔案。
   - 相關子系統 → **207 passed**；static invariant gate → **PASS**
     （新測試檔已納入 INV-TEST-2 pinning 清單）；
     external-network-denied full suite → **3009 passed, 0 failed in 569.91s**。

**WP-2B outcome**

- `INV-STATE-2` 的新語意已成為 canonical 條文，並有可執行的 crash matrix
  作為 Verification。
- 仲裁原語、分類與解析皆為純函式，可直接被 WP-2C 的 dispatch 路徑取用。
- **`RB-LAUNCH-001` 仍未解除**：`app/scheduler.py` 尚未改接 attempt-driven
  路徑，legacy 無條件 revert 一行未動。本包刻意不接 dispatch，並以
  `test_scheduler_does_not_import_the_new_launch_module_yet` 靜態鎖住此邊界。
- 所有 execution flags 維持預設 `false`；未新增 public/LLM/MCP route。

**Next**

- `WP-2C`：把 dispatch/reconcile/stop/collect 改由 attempt 驅動，接上本包的
  仲裁原語，解除 `RB-LAUNCH-001`；仍不啟用 production flag，canary 屬 WP-2D。

### WP-2C — Attempt-driven SSH dispatch

**Status:** `completed（code-complete；canary 未做，RB-LAUNCH-001 尚未關閉）`

**Task log**

1. `2026-07-27` — `WP-2C-1 attempt-driven lifecycle`：`completed`
   - 新增 `app/execution_dispatch.py`：`dispatch_job_via_attempt()` 依
     gate §7.2 順序執行——先在單一 DB transaction 建立 attempt、固定
     server/backend/revision 並把 Job `queued → running`，commit 前不做任何
     mkdir/SFTP/SSH；prepare 與 launch 皆為 durable outbox operation，各自在
     material call 之前寫 `effect_started_at`。
   - launch 的 `idempotency_key` 為 `launch:{attempt_id}:{fencing_token}`，
     replay 重用同一列，不建立第二個 operation。
   - `reconcile_attempt()` 只讀，狀態變更一律由 WP-2B 的
     `resolve_attempt_observation()` 推導；`arbitrate_unknown_attempt()` 讓
     控制端去搶同一個 claim，贏了才可 requeue。
2. `2026-07-27` — `WP-2C-2 canonical Job convergence`：`completed`
   - `Database.requeue_job_after_abandoned_attempt()` 只接受
     `abandoned_before_launch` 且 `liveness=known` 的 attempt；
     `apply_attempt_resolution_to_job()` 的 done/failed 必須與 attempt 的
     terminal state 相符，queued 投影必須來自 failed（重開機證據）。
     投影無法憑空產生 attempt 本身沒有的終態。
3. `2026-07-27` — `WP-2C-3 guard reconciliation`：`completed`
   - **WP-1A 的 abandon 守衛過寬**：原本只要「任何 operation 已開始 effect」
     就拒絕 abandon。收窄為只看 `launch` operation，且允許
     `transmission_state='not_transmitted'`——prepare 開始後失敗
     （SSH 連線被拒、launcher 從未被呼叫）正是 INV-STATE-2 允許 requeue 的
     definite pre-launch failure。
   - **WP-2B 的 trigger 與仲裁機制矛盾**：原 trigger 禁止「effect 已開始還
     宣稱 not_transmitted」，但控制端仲裁的全部意義就是在 effect 開始之後
     才建立「未傳輸」的證明。移除該條件，改由
     `transition_execution_attempt` 的 abandon 守衛把關，並在 schema 註記
     為何這條規則不能是靜態 trigger。
   - `liveness unknown` 的合法 reason code 增列 `effect_outcome_unknown`
     （launch response 遺失時主機可能完全可達，未知的是 effect 是否生效）；
     `unknown → known` 增列 `pre_effect_definite_failure`，否則會與
     abandon 守衛要求的 reason code 互斥。
   - `insert_execution_operation` 的 `inspect` 由 WP-1A 的一律拒絕，改為
     只接受 WP-2B 已釘住的 `command_kind` allowlist；任意 payload 仍然
     fail closed。
4. `2026-07-27` — `WP-2C-4 rollout gating`：`completed`
   - 新增 `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED`（預設 `false`），啟用時
     必須同時 `EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true`，否則在 background
     loop 啟動前於設定驗證階段就失敗。
   - `AttemptLaunchContext.owns()` 保守判定：flag 開啟、本 process 目前持有
     leader lease、且該台機器有 approved pinned revision，三者皆成立才由
     attempt path 接管；任何不確定一律留在 legacy SSH 路徑。
   - scheduler 的 legacy 無條件 revert 分支保留且逐字不變，只在 attempt path
     未接管該機器時執行；就地補上註解說明它是 `RB-LAUNCH-001`，不得作為
     新程式碼先例。
5. `2026-07-27` — `WP-2C-5 release evidence`：`completed`
   - 新增 `tests/test_execution_attempt_dispatch.py`（18 tests）。關鍵案例
     `test_response_lost_after_tmux_keeps_the_job_running` 直接重現
     `RB-LAUNCH-001`：launch 逾時後 Job 維持 `running`、attempt
     `liveness=unknown`，不再退回 queue。
   - 另涵蓋：遠端呼叫前 DB intent 已 commit、ambiguous 不建立第二 attempt、
     definite transport failure 才 requeue、DB 層拒絕從 ambiguous requeue、
     仲裁贏/輸/不可達、terminal sentinel 收斂、缺證據不 requeue、
     flag/leader/revision 三重 gating、inspect allowlist。
   - 修正測試污染：原用 `asyncio.get_event_loop()`，在其他測試關閉 loop 後
     會 `RuntimeError`（單獨跑全過、全套跑掛 14 個），改用 `asyncio.run()`。
   - 相關子系統 → **298 passed**；static invariant gate → **PASS**；
     external-network-denied full suite → **3027 passed, 0 failed in 580.88s**。

**WP-2C outcome**

- `RB-LAUNCH-001` 的缺陷行為在 attempt path 已修正並有直接對應的測試。
- **但 blocker 尚未關閉**：flag 預設關閉，production 仍走 legacy 無條件
  revert 分支；沒有任何部署或 canary 證據。ledger 因此記為
  「fixed in code, not yet proven in operation」，`deployed`/`canary-proven`
  維持 `no`。
- 沒有新增 public/LLM/MCP mutation route；`jobs.status` 狀態集合未變；
  legacy SSH builder 與 golden fixtures 逐字不變。

**Next**

- `WP-2D`：非 production worker 上 ≥20 jobs / 24 小時 canary，含一次強制
  response-loss 與一次 control-plane restart，以及 rollback drill。通過後才
  能關閉 `RB-LAUNCH-001` 並考慮啟用 flag。

### Phase 2 completion — backend convergence and WP-2D readiness

**Status:** `code-complete；WP-2D canary 未執行，RB-LAUNCH-001 仍未關閉`

**Task log**

1. `2026-07-27` — `stop/collect 收進 attempt outbox（gate §7.3）`：`completed`
   - 新增 `build_attempt_stop_command()`：只殺**該 attempt** 的 session。
     legacy 的 `tmux kill-session -t job_{id}` 會連同一個 Job 的其他 attempt
     一起殺掉，per-attempt session 命名是這裡安全性的來源。
   - `stop_attempt()` 綁 `kind=stop` approval（DB 以 `authorization_class`
     強制，原 execution approval 無法冒充），且**送達不是終態證據**：成功只
     代表訊號送出，attempt 仍等 wrapper trap 寫出的數字 sentinel 才收斂；
     送達失敗則完全不動 attempt。
   - `collect_attempt()` 為獨立 outbox operation，失敗只記在該 operation，
     不回寫 workload 狀態——exit 0 的 Job 即使拉不到 artifact 仍是 `done`。
2. `2026-07-27` — `發現並修正 stop 契約互斥`：`completed`
   - WP-1A 的 attempt stop 授權要求 approval payload 帶 `attempt_id`，但
     WP-1C 把 stop 契約釘成**恰好** `{job_id, source}` 兩個鍵，兩個驗證器
     互斥，attempt-scoped stop 自始無法通過任何一條路徑。
   - 改為接受兩種形狀：legacy 兩鍵（走 legacy SSH），以及三鍵帶
     `attempt_id`（attempt 路徑）。若不釘 attempt，某個 attempt 的 stop
     核准就能拿去殺同一 Job 的後續 attempt。
3. `2026-07-27` — `versioned exact goldens（gate §8）`：`completed`
   - `_GOLDEN_V2` 逐字釘住 prepare/launch/abandon/stop/collect 五條指令與
     launcher/wrapper 檔頭；測試同時斷言 `LAUNCHER_CONTRACT_VERSION == "v2"`，
     改版必須新增 fixture 並保留舊版，讓升級後仍能解讀在途 attempt。
4. `2026-07-27` — `WP-2D 工具與手冊`：`completed`
   - 新增 `scripts/canary_report.py`：以 immutable 模式唯讀開啟 DB，從持久化
     證據計算 gate §9 的八條 pass/fail，exit 0/1/2。判定來自資料，不是操作者
     對那個觀察窗的印象。
   - 新增 `tests/test_canary_report.py`（6 tests）：乾淨窗通過、job 數不足、
     unresolved unknown、重複 launcher claim 皆正確 FAIL；DB 不可讀回 exit 2；
     並斷言腳本執行前後 DB bytes 完全相同。
   - 新增 `docs/WP_2D_CANARY_RUNBOOK.md`：前置條件（含 `agent_jobs` 必須在
     本機檔案系統的 `stat -f` 驗證）、四個 flag 的啟用順序、三項必做演練
     （強制 response-loss、control-plane restart、rollback drill）與各自的
     失敗信號、判定與後續處置。
   - full suite → **3040 passed, 0 failed in 574.80s**；static gate → PASS。

**Outcome**

- Phase 2 的**程式碼**部分完成：六個 operation 全部經 attempt outbox，
  stop/collect 語意正確，golden 已版本化。
- **Phase 2 本身尚未完成**：WP-2D 需要真實非 production worker 與 24 小時
  觀察窗，無法在開發階段執行。`RB-LAUNCH-001` 維持開啟，
  `attempt_driven_ssh` 的 `deployed`/`canary-proven` 維持 `no`。

**Next**

- 由操作者依 `docs/WP_2D_CANARY_RUNBOOK.md` 執行 canary，再以
  `scripts/canary_report.py` 產生判定。通過才可關閉 `RB-LAUNCH-001`。
