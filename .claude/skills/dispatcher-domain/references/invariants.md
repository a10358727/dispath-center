# Repository Invariants(唯一正典來源)

本檔是 dispatch-center 全部架構不變量的**單一真相源**。其他 skill(approval-boundary、
ssh-dispatch-safety、state-reconciliation、release-gate)一律以 ID 引用本檔,不得複製內容。
修改任何不變量本身 = 架構決策,必須由使用者明文裁定,不得在實作過程中順手更動。

每條格式:**Statement**(不變量本身)/ **Scope**(適用範圍)/ **Enforcement**(現行落實機制)/
**Forbidden**(明確禁止的行為)/ **Verification**(對應測試或驗證方法)。

---

## INV-APPROVAL-*(核准流)

### INV-APPROVAL-1 所有 material 寫入動作經核准流
- **Statement**:每個會改變系統 material state 的操作(派工、停止、改機器設定、匯入專案、改碼、部署、project membership、service-account/token lifecycle…)對應一個 approval kind,列入 `app/db.py` 的 `VALID_APPROVAL_KINDS`,由 `app/approvals.py` 的 `request_*` 建立 pending approval、`approve()` 分支落地。以下封閉列舉是 authentication bookkeeping,不是 approval-gated material mutation:OIDC login-flow 建立與原子單次消耗、server-side session 建立、logout 撤銷該次送出的 session、以及嚴格以 `(issuer, subject)` 建立的 OIDC identity binding。新 identity binding 建立 actor 時,platform-admin 只能由明確設定、精確比對的 OIDC subject allowlist 決定;不得在後續登入提升既有 actor。email 只可作描述性 metadata;project membership、service-account 與 service-token lifecycle 仍須經核准。
- **Scope**:所有 material mutating API 端點、agent 工具、MCP 工具;以及上述封閉列舉的 authentication-bookkeeping seams。
- **Enforcement**:`db.insert_approval()` 驗證 kind 白名單;`approve()` 是 material mutation 的唯一落地入口。OIDC bookkeeping 只能使用 identity/session/login-flow 的窄 DB 介面;service-account/token/membership 仍使用既有 approval kinds。
- **Forbidden**:新增其他「直接執行」的 material 寫入端點(唯讀探測如 `test-ssh`、低風險筆記如 `experiment_records`、冪等 hub-sync 是既有的明文例外,新例外需使用者裁定);把 authentication-bookkeeping 例外擴大到 membership、service-account 或 service-token;以 email 查找、合併或識別 actor;first-login-wins 管理員;登入時修改既有 actor 的 material 權限;繞過 `request_*`/`approve()` 直接呼叫執行層。
- **Verification**:`tests/test_approvals.py`、`tests/test_inventory_api.py`(「核准後才真的寫入」系列)、`tests/test_identity.py`、`tests/test_identity_api.py`、`tests/test_service_tokens.py`、`tests/test_oidc.py`。

### INV-APPROVAL-2 危險/不合法請求在建立當下拒絕
- **Statement**:命中 `app/security.py` `is_dangerous()` 黑名單或驗證不過的請求,在**建立核准請求當下**就回 400 並寫稽核,不建立 approval、不給核准機會。
- **Scope**:所有 enqueue 路徑(REST、聊天、agent、MCP)、server_add/update 驗證。
- **Enforcement**:`app/jobqueue.py` `enqueue_job()` 先呼叫 `is_dangerous()`;`request_server_add_approval()` 先呼叫 `validate_server_config()`。
- **Forbidden**:把驗證推遲到核准時才做(核准時**重驗**是加項,不是替代);讓自動核准規則放行黑名單指令。
- **Verification**:`tests/test_security.py`、`tests/test_approvals.py` 危險指令不建 approval 系列。

### INV-APPROVAL-3 核准當下重新驗證狀態
- **Statement**:`approve()` 各分支在執行前重查目標的當下狀態(job 還是 running 嗎、candidate 還是 pending 嗎、server 有沒有 running job),建立請求時的檢查不足恃——等待期間狀態可能已變。
- **Scope**:`app/approvals.py` 所有 `approve()` 分支;新增 kind 必須比照。
- **Enforcement**:既有分支的「核准前重查」模式(stop/server_disable/git_init 等先例)。
- **Forbidden**:新 kind 的 approve 分支直接信任 payload 裡建立時的快照。
- **Verification**:`tests/test_approvals.py` 對應 kind 的過期狀態測試。

### INV-APPROVAL-4 自動核准白名單只有 enqueue/stop
- **Statement**:`maybe_auto_approve()` 的 kind 閘門(`app/approvals.py`:`if approval.kind not in ("enqueue", "stop")`)永遠只認這兩個 kind;其他 kind(git_init、project_deploy、server_* 等)天然排除、永不自動核准。
- **Scope**:`app/approvals.py`、`app/autoapprove.py`、`auto_approve.yaml` 規則引擎。
- **Enforcement**:kind 閘門硬編碼;規則欄位只有 source/kind/command_regex/project/pin_server 五個。
- **Forbidden**:擴大 kind 白名單;新增規則欄位;`WEB_DIRECT_EXECUTE` 的一步生效擴及 web enqueue/stop 以外的 kind;把 INV-APPROVAL-4b 的 policy-scoped 機制實作成 `maybe_auto_approve()` 的 kind 或規則。
- **Verification**:`tests/test_autoapprove.py`;release-gate `static_checks.sh` 釘住閘門那一行。

### INV-APPROVAL-4b Policy-scoped 自動決策只限 auto_placement（2026-07-18 DG-2 裁定新增）
- **Statement**:`kind=auto_placement` 的 pending approval 可以由**獨立於 `maybe_auto_approve()` 的** policy-scoped 機制自動核准,且只在同時滿足以下全部條件時:(1) `AUTO_PLACEMENT_KILL_SWITCH` 明確設定為停用狀態以外的值（預設值必須是「自動執行停用」）;(2) 提案引用的 dispatch policy 目前 head 仍為 `approved` 且 revision 與 payload 一致;(3) 目標伺服器在該 policy 的 `allowed_servers` 精確清單內且 enabled;(4) 該 policy 的 `valid_until` 未過期;(5) 自動核准後的活躍 placement 數不超過 `max_concurrent_placements`;(6) 指令重推導後的 SHA-256 與 payload 一致且通過 `is_dangerous()` 複檢。自動決策必須逐件寫入稽核,`decision_mechanism` 記為 `policy-{policy_id}-r{revision}`,決策者不是人也不得偽稱為人。
- **Scope**:`app/approvals.py` 的 auto_placement 決策路徑、`app/main.py` 的 auto placement 迴圈、`app/config.py` 的 kill switch。
- **Enforcement**:kill switch 預設停用;任一條件不滿足時提案**留在 pending**（不自動 reject、不降級執行);policy archive 立即使其所有後續自動決策失效。
- **Forbidden**:把 auto_placement 加進 `maybe_auto_approve()` 白名單;對 enqueue/stop/auto_placement 以外任何 kind 建立 policy-scoped 自動決策;繞過 (2)–(6) 任何一項 revalidation;自動核准一個 payload 與當下重推導結果不一致的提案。
- **Verification**:`tests/test_auto_placement.py` 的 Slice 5 測試群(範圍內自動/超界留 pending/archive 即停/kill switch 即停/稽核完整性)。
- **Provenance**:使用者 2026-07-18 於 `docs/DECISIONS.md` 裁定 DG-2 核准（`docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md` §3),本節即該裁定要求的正式修訂文字。

### INV-APPROVAL-5 認證涵蓋所有新端點
- **Statement**:`AUTH_TOKEN` 有設定或 `OIDC_ENABLED=true` 時,只有 `GET /`、`GET /auth/login`、`GET /auth/callback` 與 `/static/*` 前綴不要求既有 credential。`GET /auth/login` 與 `GET /auth/callback` 只供 OIDC Authorization Code + PKCE handshake;`GET /auth/me`、`POST /auth/logout` 與所有其他 application API 仍須由有效 server-side session、明確啟用的 service bearer、或相容的 `X-Auth-Token` 通過認證。WS `/ws` 保留有效 session/service credential 或連線後首則 auth 訊息的相容協議。
- **Scope**:`app/main.py` auth middleware;每個新端點。
- **Enforcement**:middleware 是預設涵蓋(不豁免=受保護),豁免以 `_AUTH_EXEMPT_ROUTES` 的 HTTP method + exact path 封閉列舉;`/static/*` 是唯一豁免前綴;新端點零設定即受保護。
- **Forbidden**:新增任何其他豁免 method/path 或豁免前綴;豁免 `GET /auth/me` 或 `POST /auth/logout`;把 credential 放進 query string;把 OIDC handshake exemption 擴成一般 `/auth/*` exemption。
- **Verification**:`tests/test_auth.py`、`tests/test_oidc.py`、`tests/test_authorization_coverage.py`;release-gate 釘住完整 method/path 豁免集合。

---

## INV-SSH-*(SSH 執行邊界)

### INV-SSH-1 SSH 後端無 agent、依賴封頂(2026-07-19 DG-C 修訂)
- **Statement**:SSH 執行後端不依賴工作機上任何本系統常駐程式;遠端依賴只有 `tmux`、`bash`、`nvidia-smi`(GPU 機),一切互動經 asyncssh(`app/sshpool.py`)。rsync 由 Server A 端發起。工作機上「可以」另外存在經 `INV-NODE-*` 管理的 Node Agent,但 **SSH 後端的行為與依賴永遠不得假設它存在**;SSH 後端永久保留為每台工作機的相容/緊急通道,不因 Node Agent 上線而移除或弱化。
- **Scope**:所有產生遠端指令的模組。
- **Enforcement**:架構慣例;`servers.yaml.example` 與 README 明文;ExecutionBackend seam(C1)以 golden tests 證明 SSH 路徑指令字串逐字不變。
- **Forbidden**:SSH 後端的遠端指令引入其他工具依賴(python、jq、curl…);SSH 後端假設/呼叫任何 agent 端點;以 Node Agent 存在為由刪除或跳過任何 INV-SSH-2…9 的保證。
- **Verification**:code review;新遠端指令的配對測試斷言完整指令字串;C1 golden tests。

### INV-SSH-2 使用者指令原文只經 SFTP 落地
- **Statement**:使用者任務指令(`job.command`)寫入 `agent_jobs/{id}/cmd.sh` 一律走 SFTP(`ssh_write_file`),不得插值進任何 shell 指令字串——這是免除 shell 跳脫問題的結構性解法。
- **Scope**:`app/scheduler.py` `dispatch_job()`、`app/jobqueue.py` build_* 家族。
- **Enforcement**:`build_run_sh_content()`/`build_launch_command()` 只插值 int 型 `job_id`。
- **Forbidden**:任何 f-string/`%` 把 `job.command` 或其他自由文字拼進 `ssh_run` 的指令。
- **Verification**:`tests/test_jobqueue.py`、`tests/test_scheduler.py`(FakeSSH 斷言指令字串)。

### INV-SSH-3 遠端指令由純函式建構、值先驗證或 quote
- **Statement**:每條遠端指令字串由可單測的純函式 `build_*` 產生;插值進去的值要嘛先過字元集驗證(`validate_name_component` 類),要嘛 `shlex.quote()`;路徑用 home 相對(SFTP 不展開 `~`,見 `app/jobqueue.py` 模組註解)。
- **Scope**:`app/jobqueue.py`、`app/datasets.py`、`app/inventory.py`、`app/activity.py`、`app/results.py`、`app/hub.py`、`app/approvals.py` 內組指令處。
- **Forbidden**:未驗證/未 quote 的使用者輸入進指令;在呼叫點內聯拼指令(繞過純函式)。
- **Verification**:各 build_* 的單元測試;`tests/test_inventory.py` 秘密檔過濾系列。

### INV-SSH-4 唯讀探測是封閉指令集
- **Statement**:inventory 掃描、activity 探測、`test-ssh` 只跑各自明文列舉的固定唯讀指令;秘密檔名/目錄過濾在**組指令的 Python 層**完成,不依賴遠端執行時攔截。
- **Scope**:`app/inventory.py`、`app/activity.py`、`app/server_config.py` `test_ssh_connection()`。
- **Forbidden**:接受任意路徑/任意指令參數;新增指令而不同步補白名單說明與測試。
- **Verification**:`tests/test_inventory.py`、`tests/test_server_config_api.py`(mock 斷言只跑固定指令)。

### INV-SSH-5 逾時明確、長工不佔連線
- **Statement**:每次 `ssh_run` 帶明確 timeout(連線 10s、指令預設 30s,`app/config.py`);長時間工作一律丟進 tmux detached session 立即返回,絕不在 SSH 通道上等它完成。
- **Scope**:所有 `ssh_run`/`local_run` 呼叫點。
- **Forbidden**:無 timeout 的遠端呼叫;用 SSH 同步等待訓練/同步類長工。
- **Verification**:code review + FakeSSH 測試的 timeout 參數斷言。

### INV-SSH-6 成敗判定只看哨兵 exit_code
- **Statement**:任務終態判定唯一依據是 `agent_jobs/{id}/exit_code` 檔案;不解析 log 內容、不以 tmux session 存活與否為準(tmux 只供人工 attach 觀看)。
- **Scope**:`app/jobqueue.py` `reconcile_job()` 與所有消費它的地方。
- **Forbidden**:從 log 文字猜成敗;把 tmux GONE 直接當 failed。
- **Verification**:`tests/test_jobqueue.py` reconcile 三分支測試。

### INV-SSH-7 連不上=跳過,不下判定
- **Statement**:SSH 例外(`SSHUnreachableError` 等)一律視為「本輪跳過、狀態不動」,unreachable 永不等於 failed;離線機上的 running 任務保持 running,等機器回線後再 reconcile。
- **Scope**:`app/scheduler.py`、`app/jobqueue.py`、`app/monitor.py`。
- **Forbidden**:把連線失敗落地成任務失敗;對離線機的任務做任何狀態變更。
- **Verification**:`tests/test_scheduler.py` 離線機分支測試。

### INV-SSH-8 host-key 政策是已記錄的取捨
- **Statement**:`app/sshpool.py` 的 `known_hosts=None` 是在 Tailscale 私網前提下的明文取捨;變更 host-key 驗證政策是架構決策。
- **Scope**:`app/sshpool.py`。
- **Forbidden**:在不相關的改動中順手「修好」它;反之,新增的 SSH 呼叫路徑也不得引入與現行不一致的政策。
- **Verification**:code review(此層無單元測試,PLAN.md 明文)。

### INV-SSH-9 取消運行中任務必經核准
- **Statement**:停止 running 任務的唯一路徑是 kind=stop approval 核准後 `tmux kill-session -t job_{id}`;kill 之後終態仍由哨兵協議判定。
- **Scope**:`app/approvals.py` stop 分支、所有停止入口。
- **Forbidden**:任何入口(前端、agent、MCP、排程器)直接 kill;排程器自動殺「疑似卡死」任務(stall 只標旗標,INV-STATE-4)。
- **Verification**:`tests/test_approvals.py` stop 流程測試。

---

## INV-NODE-*(Node Agent 執行邊界;2026-07-19 DG-C 核准,實作尚未存在)

> 依 `docs/DECISIONS.md` 2026-07-19 DG-C 裁定寫入(草稿:
> `docs/DG_C_INVARIANT_REVISION_DRAFT.md`)。這些不變量在任何 Node Agent
> 實作動工前生效——實作(C1 之後)必須符合;沒有實作時它們不影響
> 既有 SSH 後端的任何行為。

### INV-NODE-1 Agent 身分與出站單向連線
- **Statement**:Node Agent 是**非 root** 的小型常駐服務,只發起**出站**已驗證 HTTPS 輪詢;工作機不得開放任何入站控制埠。每個 agent 持有一組可個別撤銷的 node credential,control plane 對每個請求驗證 node 身分;credential 洩漏的處置是撤銷該 node,不影響其他 node。
- **Forbidden**:入站 listener、共享 credential、以 IP/hostname 取代 credential 驗證、agent 以 root 執行。
- **Verification**:agent 套件測試(全 fake、不碰真機);authorization catalog 覆蓋 node 端點。

### INV-NODE-2 Lease/acknowledgement 執行語意
- **Statement**:一次 attempt 只能被一個 agent lease;agent 必須先原子性 acknowledge 並把 attempt 身分持久化到本機,才能啟動任何副作用。control plane 在 lease 未過期且未收到終態前,**不得**把同一 attempt 再派給任何通道(含 SSH)。
- **Forbidden**:無 lease 的執行、ack 前產生副作用、lease 期內重複派發。
- **Verification**:協議層 fake 測試覆蓋 lease 競態、重複 ack、過期 reclaim。

### INV-NODE-3 指令位元組非插值落地
- **Statement**:核准的指令位元組由 agent 寫入檔案後,以只含已驗證識別字的 launcher 啟動——與 `INV-SSH-2`/`INV-SSH-3` 同構:自由文字永不拼進任何 shell 字串,digest 與核准 payload 綁定。
- **Forbidden**:agent 端任何形式的指令字串插值;執行未經核准 digest 比對的位元組。
- **Verification**:launcher 純函式測試;digest 比對測試。

### INV-NODE-4 心跳過期＝unknown,不是 failed
- **Statement**:心跳過期、agent 連不上、輪詢中斷一律判 `unknown`,不得推斷任務失敗(與 `INV-SSH-7` 同構)。狀態收斂唯一依據是 agent 回報的持久化終態或(SSH 相容通道的)哨兵檔案。
- **Forbidden**:以心跳缺席把 running 任務標 failed;以 unknown 觸發自動重派。
- **Verification**:reconciliation 測試覆蓋 agent 消失/重啟/回歸各情境。

### INV-NODE-5 重啟不重複、狀態可收斂
- **Statement**:control plane 或 agent 任一方重啟後,已 acknowledge 的 attempt 不得被重複啟動;雙方各自以持久化紀錄(DB/本機 attempt 檔)收斂,收斂規則必須可測(fake 時序測試)。
- **Forbidden**:以記憶體狀態判斷 attempt 歸屬;重啟後自動重跑未確認終態的 attempt。
- **Verification**:雙側重啟矩陣的 fake 測試(roadmap Phase 3 量化門檻:≥100 jobs/≥2 nodes/7 天零重複啟動零假失敗)。

### INV-NODE-6 逐台提升、隨時回退
- **Statement**:Node Agent 以**每台工作機**為單位明確啟用;未啟用的機器完全走 SSH 後端。任何一台可在不影響其他機器的情況下回退到 SSH;Codex Runner 的遷移放在所有普通 worker 之後(C4 通過才動)。
- **Forbidden**:全域一刀切開關;移除 SSH 後端程式碼;讓回退需要資料遷移。
- **Verification**:per-node 開關測試;回退演練紀錄。

---

## INV-STATE-*(狀態一致性)

### INV-STATE-1 持久真相 vs 可拋棄快取的所有權
- **Statement**:任務/專案/資料集/核准的持久真相 = `jobqueue.db` + 工作機上的哨兵檔;`server_states`/`server_configs`(`app/main.py` AppState 的 in-memory dict)是可拋棄的執行期快取,重啟歸零、由 monitor loop 與 `servers.yaml` 重建。
- **Scope**:所有新功能的狀態設計。
- **Forbidden**:把「只存在 in-memory」的資料當持久真相;把需要跨重啟存活的狀態只放 `server_states`。
- **Verification**:重啟恢復測試(`tests/test_scheduler.py` reconcile 系列)。

### INV-STATE-2 先寫 DB、再做遠端副作用;僅 definite pre-launch failure 可退回
> 2026-07-27 依 `DG-AMBIGUOUS-LAUNCH-v1` 修訂(`docs/DG_AMBIGUOUS_LAUNCH_DECISION.md` §3.2,裁定記錄於 `docs/DECISIONS.md`)。修訂前的條文把「派發失敗顯式 revert 回 queued」寫成無條件規則,無法區分「證明沒啟動」與「不知道有沒有啟動」。

- **Statement**:「DB 寫入 + 遠端副作用」的順序一律 DB 在前(先標 running 再派發),崩潰窗口留下的中間態必須能被 reconcile 收斂。派發過程失敗時,**只有 definite pre-launch failure 才可以把 Job 從 running 退回 queued**。definite pre-launch failure 的定義是:存在 transport 或遠端 arbitration 證據,足以證明 workload 不可能已經在目標機上啟動。其判定只有兩種來源:(a) transport 層證明 launch request 未被送出或未被接受;(b) 控制端自己以原子操作贏得該 attempt 的 remote launch claim,使 launcher 永遠不可能再啟動。
- **Statement(續)**:launch response timeout、連線中斷、或任何無法歸類的例外,一律視為 **ambiguous**,不是失敗。此時 Job 保持 running,attempt 保持原 target 與原 backend、`liveness=unknown`,只能以相同 attempt 與相同 idempotency key 對同一台目標機重試查證。
- **Scope**:`app/scheduler.py` 派發路徑、`ExecutionBackend` 的 prepare/launch/inspect、reconciler,以及任何新的「落地+副作用」代碼。
- **Forbidden**:先做副作用再寫 DB;把 timeout/連線中斷/未知例外當成派發失敗而 revert;以「沒看到 tmux/sentinel」作為未啟動的證明;在 ambiguous 狀態下建立第二個 attempt 或改派其他伺服器;留下 reconcile 無法辨識的中間態。
- **Verification**:`app/execution_launch.py` 的 definite/ambiguous 分類與仲裁函式;`tests/test_execution_launch_arbitration.py` 的 crash matrix。
- **Legacy exception(尚未收斂)**:`app/scheduler.py` 的 legacy 派發路徑目前仍對任何 `dispatch_job()` 例外無條件 revert,因為它沒有 claim/receipt 可供仲裁。這是已登記的釋出阻擋項 `RB-LAUNCH-001`,由 WP-2C 接上 attempt-driven 路徑後收斂;在那之前它是**已知缺陷,不是合規行為**,不得引用它作為新程式碼的先例。

### INV-STATE-3 schema 演進走雙軌遷移
- **Statement**:新欄位必須同時進 `SCHEMA` 常數(新 DB)與對應的 `_*_COLUMN_MIGRATIONS`(既有 DB 的 `ALTER TABLE ADD COLUMN`),並補遷移測試;不刪欄、不改既有欄位語意。
- **Scope**:`app/db.py`。
- **Forbidden**:只改 SCHEMA 不補遷移;破壞性 schema 變更。
- **Verification**:`tests/test_db_migration.py`。

### INV-STATE-4 任務狀態機封閉
- **Statement**:合法轉移限於 queued→running→done/failed、queued→blocked/cancelled、running→queued(requeue,tmux 中斷)、blocked 依 refresh 邏輯;done/failed/cancelled 是終態不復活。stall 偵測只設 `stalled_suspect` 旗標,不改 status。
- **Scope**:`app/jobqueue.py`、`app/scheduler.py`、`app/approvals.py`。
- **Forbidden**:引入新狀態值或新轉移而未經使用者裁定;讓監控/偵測邏輯改動 status。
- **Verification**:`tests/test_jobqueue.py` 狀態機測試、`tests/test_stall.py`。

### INV-STATE-5 servers.yaml 變更三步曲
- **Statement**:`servers.yaml` 的程式化修改一律 `backup_servers_yaml()` → `write_servers_yaml_atomically()`(暫存檔+`os.replace`)→ in-memory 熱替換(`reload_server_config_if_supported()`);不留「要重啟才生效」的半套。
- **Scope**:`app/server_config.py`、`app/approvals.py` server_* 分支。
- **Forbidden**:直接 `open(..., "w")` 寫 servers.yaml;跳過 backup;改了檔案不同步 in-memory。
- **Verification**:`tests/test_server_config.py` atomic write/backup 測試。

### INV-STATE-6 背景工作不卡排程輪
- **Statement**:可能耗時的任務結束 hook(拉結果/寄信)以同步回呼把工作丟進 AppState 追蹤的 `asyncio.create_task()`,排程輪本身不 await 它們;單行程 asyncio 下,跨 `await` 點的共享狀態操作要重查最新狀態。
- **Scope**:`app/main.py` AppState、`app/jobfinish.py`、任何新 hook。
- **Forbidden**:在 scheduler/monitor loop 內 await 長工;裸 `create_task` 不經 AppState 追蹤;在迭代舊快照後直接落地寫入而不重查。
- **Verification**:`tests/test_main_background_hooks.py`。

---

## INV-AUDIT-*(稽核)

### INV-AUDIT-1 audit.jsonl 是 append-only
- **Statement**:`audit.jsonl` 只透過 `app/audit.py` `append_audit()` 以 `"a"` 模式追加;程式永不改寫、刪除、輪替它。
- **Scope**:全部程式碼。
- **Forbidden**:以 `"w"` 模式開啟;任何就地編輯/truncate;測試以外的代碼刪檔。
- **Verification**:release-gate `static_checks.sh` 釘住 open 模式;code review。

### INV-AUDIT-2 每個動作進稽核
- **Statement**:核准生命週期(建立/核准/拒絕/自動核准,含 `approved_by` 標記)與任務生命週期(enqueue/dispatch/done/failed/requeue/cancel/stop)每一步都寫一筆 `{ts, action, params, result}`。
- **Scope**:所有 `request_*`、`approve()` 分支、scheduler 落地點;新 kind/新動作比照。
- **Forbidden**:新增的寫入動作沒有對應 audit action;稽核寫入失敗導致主流程中斷(稽核是 best-effort 追加)。
- **Verification**:各測試檔對 audit 內容的斷言(`tests/test_api.py` 等)。

---

## INV-LLM-*(LLM / MCP 權限邊界)

### INV-LLM-1 LLM 通道只能建 pending approval
- **Statement**:所有 LLM 入口(WS `/ws`、`POST /agent/chat`、MCP bridge)的寫入能力上限 = 呼叫既有 `request_*_approval()` 建立 pending approval;最壞情況(prompt injection 全成功)的血本封頂是「一張待審卡片」。
- **Scope**:`app/agent_tools.py`、`app/agent_runtime.py`、`app/chat.py`、`app/mcp_bridge.py`。
- **Forbidden**:LLM 工具直接呼叫 `approve()`、`enqueue_job()`、執行層或檔案寫入(`experiment_records` 筆記類是既有明文例外)。
- **Verification**:`tests/test_agent_tools.py`、`tests/test_mcp_bridge.py`(含 prompt injection 測試)。

### INV-LLM-2 永遠沒有 approve/reject/自由 shell 工具
- **Statement**:agent 工具表(`TOOLS`)與 MCP bridge 工具集永不包含 `approve`/`approve_approval`/`reject`/`reject_approval`/`shell`/`exec`/`run_command`;提案通道與批准通道結構性分離。
- **Scope**:`app/agent_tools.py`、`app/mcp_bridge.py`。
- **Forbidden**:以任何名義(除錯、管理員模式、「有 token 保護」)新增這類工具。
- **Verification**:`tests/test_agent_tools.py:86` 的 `forbidden_names` 斷言;release-gate 靜態複驗。

### INV-LLM-3 不存在 LLM→SSH 直通路徑
- **Statement**:`app/agent_tools.py` 不得 import `app.sshpool`、`app.localrun`、`subprocess`;工具需要的 SSH 能力(如 `test_server_ssh`)只能經呼叫端注入的 `ctx.ssh_run` callable,且僅限封閉唯讀指令集(INV-SSH-4)。
- **Scope**:`app/agent_tools.py` 及其被 import 的路徑。
- **Forbidden**:直接 import 執行層;把注入的 callable 用於白名單之外的指令。
- **Verification**:`tests/test_agent_tools.py:72` 的 `forbidden_modules` 斷言;release-gate 靜態複驗。

### INV-LLM-4 MCP bridge 是行程隔離的 HTTP client
- **Statement**:`app/mcp_bridge.py` 是獨立行程,不 import 任何 `app.*` 模組,只透過 HTTP(httpx + `X-Auth-Token`)呼叫調度中心 REST API;bridge 掛掉或被攻破不影響調度中心本體。
- **Scope**:`app/mcp_bridge.py`。
- **Forbidden**:`from app.xxx import ...`;bridge 直接讀 `jobqueue.db`/`servers.yaml`;bridge 重做調度中心已有的判斷(危險指令等——原樣轉述 400 即可)。
- **Verification**:`tests/test_mcp_bridge.py`;release-gate 靜態複驗 import。

### INV-LLM-5 LLM 層可選、缺席不影響本體
- **Statement**:anthropic key/套件、vLLM 設定、mcp 套件任一缺席時,對應功能明確降級(規則式後備/503/跳過),排程核心功能完全不受影響。
- **Scope**:`app/llm.py`、`app/llm_local.py`、`app/mcp_bridge.py` 的 import 守護與開關判斷。
- **Forbidden**:核心路徑對選配套件產生硬依賴;降級分支拋未處理例外。
- **Verification**:`tests/test_llm.py`/`test_llm_local.py`/`test_chat.py`(開發環境本身沒裝 anthropic,全綠即證明)。

---

## INV-TEST-*(測試邊界)

### INV-TEST-1 測試不需要真實環境
- **Statement**:測試一律用 FakeSSH/假 `local_run`/假 `send_mail`/duck-typed 假 LLM client 與 `fastapi.testclient.TestClient`,不需要真 SSH、真 GPU、真 SMTP、真 API key、真網路。唯一例外:`tests/test_localrun.py` 用本機 `bash`/`tmux`(它測的就是本機 subprocess 層)。
- **Scope**:`tests/` 全部。
- **Forbidden**:新測試連真機/真服務;測試依賴 `.env` 裡的真實憑證;測試寫入 repo 根目錄的 `jobqueue.db`/`audit.jsonl`(用 tmp fixture)。
- **Verification**:`tests/conftest.py` 的 fixture 設計;CI 無網路即可全綠。

### INV-TEST-2 邊界不變量有測試釘住,不得鬆綁
- **Statement**:INV-LLM-2/3 等安全邊界由明確測試釘住(`tests/test_agent_tools.py` 的 forbidden_modules/forbidden_names 等);修改功能時可以擴充這些測試,不得刪除或弱化其斷言。
- **Scope**:所有標註「測試釘住」的不變量。
- **Forbidden**:為了讓新代碼過關而放寬釘住斷言;刪除釘住測試。
- **Verification**:release-gate 檢查測試檔案存續;code review。
