> archived: 2026-08-25（自 `docs/` 移入;2026-08-06 狀態快照,非 current authority）
> superseded_by: `docs/IMPLEMENTATION_PROGRESS.md` 與 `docs/CAPABILITY_LEDGER.md`

# 測試與驗證報告

更新：2026-08-06

這份報告描述目前 repository 內可重現的證據，不把本機 fake 測試當成伺服器
部署、canary 或 production-ready 證據。

## 已完成的本機驗證

執行環境：repository `.venv`、SQLite temporary fixtures、外部網路封鎖。

```text
DISPATCH_TEST_NETWORK=deny .venv/bin/python3 -m pytest -q
.venv/bin/python3 -m compileall -q app agent
git diff --check
```

完整 offline suite 已取得 **3582 passed，916.58s** 的 release gate
結果，沒有 test failure。這次結果涵蓋 public server publication、native code
promotion、ExecutionPlan/approval 原子建立與 Run lineage、generic
attempt terminal→collect、linked Node attempt lifecycle、exactly-four durable
completion operations、strict canary evaluators，以及安全 staged restore。
局部整合驗證包括：

- `python scripts/check_requirements_lock.py` → PASS。
- `bash .claude/skills/release-gate/scripts/static_checks.sh` → PASS（所有
  invariant/dependency pinning checks）。
- `git diff --check`、`python -m compileall -q app agent` → PASS。

- public server publication/auth/UI focused group：350 passed，1 個既有
  Starlette TestClient deprecation warning。
- real Git code-promotion/config/auth/UI focused group：323 passed，1 warning。
- ExecutionPlan/attempt foundation group：69 passed，1 warning。
- Phase 6 role/fencing/metrics/backup/config/auth focused group：95 passed，
  1 warning；含 execution foundation/dispatch 與文件 gate 的擴大組為
  149 passed，1 warning。
- Node protocol/daemon/lease/credential/canary focused group：281 passed；
  包含 linked generic/protocol lease、ack/terminal 原子收斂、stale heartbeat
  僅設 unknown、四種 completion operation 的 claim fencing/restart recovery，
  以及 Phase 5 evaluator 的 fail-closed 證據驗證。
- hardened canary/health/backup/application focused group：139 passed；包含
  WP-2D v2 8 小時窗與 drill manifest、missing DB、same-second backup、
  malicious archive、schema/write readiness。
- D-5 attempt filesystem preflight focused group：144 passed，1 warning；完整
  3338-test suite 亦通過。覆蓋固定唯讀命令、local/NFS/FUSE/unknown 分類、
  active revision/key drift CAS、舊 DB 的 additive versioned-trigger migration、
  每個新 revision 重置證據、scheduler 篩選與 DB claim transaction 雙層
  fail-closed；revision evidence 與 bounded durable preflight event 的
  transaction rollback、actor attribution、敏感資料排除亦有回歸測試。
- Engineering Task/coding-run cleanup focused group：308 passed；覆蓋
  cleanup intent/outcome、remote response-loss unknown、durable append
  rollback、path-free durable parameters 與 retry no-replay。
- Node lifecycle／audit adoption／actor attribution／recovery focused group：207 passed；
  包含 approval-gated Node enrollment/rotation/retirement/revocation 的
  node／attempt projection、`node_enrolled`/`node_rotated`/`node_drained`/
  `node_retired`/`node_revoked`/`approval_decided` 原子 rollback，以及 closed
  dynamic audit-writer inventory。
- Legacy scheduler dispatch focused tests now cover SSH/local-sync claim and
  requeue events plus durable-append rollback before remote dispatch.
- Scheduler stalled-state focused coverage now asserts `stalled`/`cleared`
  durable transitions, quiet repeated observations, callback ordering, and
  rollback before the legacy `stall_suspect` summary.
- ExecutionPlan materialization focused coverage now asserts the immutable plan,
  pending approval, `approval_created`, and bounded
  `execution_plan_materialized` event share one transaction; an injected event
  append failure rolls both rows back. The full offline count above includes
  this materialization and rollback coverage.
- Audit export alert coverage now verifies that backlog is reported as
  `attention`, while an exhausted dead-letter row produces the shared
  `alert.active=true`/`critical`/`dead_letter_present` signal in both the
  read-only status command and `/operations/metrics`; no rows are replayed by
  the observation path.
- Unbound coding-run result focused coverage now asserts the bounded
  `coding_run_result_recorded` event, actor/approval correlation, and rollback
  when the durable append fails; the compatibility `coding_finished` summary
  remains present.
- Node client/daemon fake transport 覆蓋：無 job selector、duplicate poll/ack、
  current-attempt restart recovery、ack-before-launch、ack response-loss journal
  與 exact-payload first-launch resume、bounded backoff/jitter、explicit
  process-group stop receipt、child terminal 與 terminal report retry。
- `DISPATCH_CONTROL_PLANE_URL=http://127.0.0.1:8000 DISPATCH_NODE_TOKEN=dummy
  DISPATCH_NODE_WORKDIR=<temporary> python -m agent --check` → PASS（只做本機
  設定/匯入/權限檢查，不連線；實機仍需 canary）。
- ExecutionPlan path 覆蓋：pending approval 不可 dispatch、approve-time
  revalidation、plan/job/target digest pin、plan operation authorization。
  Plan 與 pending approval 是同一 transaction，注入第二段 insert failure
  會兩者一起 rollback；`request_approval_id` 之後不可改寫。Run detail 回傳
  approval、Job、attempt、operation、event、Node artifact metadata 的 durable
  lineage，缺資料時保持空集合／`null`，不猜成 failed。
- Code promotion path 覆蓋：native task/base/run/artifact linkage、exact bundle
  digest、真 `git bundle verify`、isolated staging、bare Hub ref publication、
  publish failure 的 non-runnable retry、same-commit no-op、retire 不刪證據；
  flag 預設關閉，不推 GitHub、不清 worktree。
- Attempt path 覆蓋：attempt claim 與第一個 prepare intent 同一 transaction、
  active-attempt 排除 legacy scheduler、response-loss 維持 unknown、terminal
  collect operation 與 finished hook 接線、SSH fallback 預設不變。實機
  forced-response-loss 發現並修正「attempt 已恢復但 launch operation 永久
  uncertain」缺口；相符 companion/receipt/tmux/sentinel 證據現在只收斂同一
  operation、永不重送 launch，錯誤或缺少 token 仍保持 uncertain。另涵蓋舊版
  terminal attempt 的 evidence-only 升級恢復，不重複 terminal hook。後續實機
  restart 又發現已 running attempt 的正面 running 證據被誤送成 self-transition；
  修正後同狀態觀測只刷新 fenced freshness，unknown liveness 才以 CAS 恢復
  known，並有兩個直接回歸測試。兩次失敗候選皆保留單一 attempt 並正常終止，
  但依 canary contract 均不算通過。
- Strict Node terminal path 覆蓋：同一 transaction 更新 generic attempt、
  protocol attempt 與 Job，並 exactly-once 建立 `dependency_refresh`、
  `result_collection`、`notification`、`owner_projection`；重複 terminal 不重複
  建立，worker restart 與 stale claim 依 fencing 接續。
- Backup/restore path 覆蓋：缺少 DB、checksum/manifest 損壞、路徑穿越、
  absolute member、symlink/device/duplicate member 都在 displacement 前拒絕；
  整份備份先安全解壓到 staging，成功後才允許 operator restore。
- WP-3A snapshot path 覆蓋：candidate digest、human approval、atomic
  `building`/`published` evidence、content-addressed shards、source drift
  abort、flag-off refusal、concurrent winner 與 interrupted-build resume；
  `tests/test_dataset_snapshot.py` 25 tests 全部通過。

- 本次 Node crash-window focused run：`155 passed，1 warning`。本地 journal
  記錄 attempt identity/digest、ack request、materialized command、launch
  intent、terminal evidence 與 artifact-report state；log tail 有 16 KiB
  上限，artifact 只接受明確的相對路徑／size／SHA-256 manifest。任何
  response-loss/損壞/不一致都採 unknown + no relaunch，artifact metadata
  暫時送不出去則獨立重試。

## 建議的開發者測試順序

```bash
python scripts/check_requirements_lock.py
DISPATCH_TEST_NETWORK=deny .venv/bin/python3 -m pytest -q \
  tests/test_server_config_api.py tests/test_server_publication.py \
  tests/test_code_promotion.py
DISPATCH_TEST_NETWORK=deny .venv/bin/python3 -m pytest -q \
  tests/test_execution_plan_api.py \
  tests/test_execution_attempt_dispatch.py \
  tests/test_scheduler.py tests/test_jobqueue.py
DISPATCH_TEST_NETWORK=deny .venv/bin/python3 -m pytest -q \
  tests/test_node_agent.py tests/test_node_agent_daemon.py \
  tests/test_node_v2_lease.py tests/test_node_staged_rotation.py \
  tests/test_node_canary_report.py
DISPATCH_TEST_NETWORK=deny .venv/bin/python3 -m pytest -q \
  tests/test_health_endpoints.py tests/test_backup_restore_scripts.py \
  tests/test_restore_drill.py
DISPATCH_TEST_NETWORK=deny .venv/bin/python3 -m pytest -q
bash .claude/skills/release-gate/scripts/static_checks.sh
```

## 實機測試與驗收方式

程式測試全綠後，依序執行下列三個互不取代的驗收：

1. **WP-2D SSH canary**：完全依
   `docs/WP_2D_CANARY_RUNBOOK.md`，在一台 non-production SSH worker
   執行至少 20 jobs/8 小時，以及 response-loss、主控重啟、rollback 三個
   drill。結束後先做 SQLite online backup，再執行：

   ```bash
   .venv/bin/python3 scripts/canary_report.py \
     --db /evidence/wp2d/jobqueue.db \
     --since <start-Z> --through <close-Z> \
     --server <non-production-server> \
     --evidence /evidence/wp2d/wp2d-canary.json
   ```

2. **Phase 5 Node canary**：先填完並核准
   `docs/DG_NODE_CANARY_DECISION.md`，再依
   `docs/PHASE5_NODE_CANARY_RUNBOOK.md` 執行兩節點、至少 100 個
   acknowledged workloads、連續七天與九項 drill。針對 stable DB backup：

   ```bash
   .venv/bin/python3 scripts/node_canary_report.py \
     --db /evidence/node/jobqueue.db \
     --since <start-Z> --through <close-Z> \
     --require-tag <unique-canary-tag> \
     --evidence /evidence/node/node-canary.json
   ```

3. **Phase 6 operations drill**：依
   `docs/PHASE6_OPERATIONS_RUNBOOK.md` 在 temporary/non-production
   環境測 leader takeover；建立一份真正離開 Server A 的 backup，使用
   `scripts/restore_drill.py` 驗證 integrity、row counts、Git refs 與 result
   sample；若提供 signed checkpoint、獨立 key 與 MANIFEST，亦會把 audit
   anchor boundary／manifest binding 納入 PASS gate。`deploy/restore.sh` 只用於
   服務停止且有 operator 核准的真實還原，不拿來做日常 smoke test。

兩個 canary evaluator 的 `0/1/2` 分別代表 pass、證據顯示 fail、證據本身
不可用。只有 exit 0 且 evidence manifest/候選 commit 被保存，才能更新
capability ledger；不能人工解讀後略過失敗列。

### `worker_5090_117` 最小 smoke（不宣稱 canary）

只有在操作者確認它是 non-production、沒有其他人的工作且允許短暫測試時，
才把 `worker_5090_117` 當候選。若目的只是「確認能不能跑，然後先停在這裡」，
建議分成兩層：

1. **Level A — 安全資格檢查**：部署本候選版本，在 Worker 管理頁按
   「建立受管 Revision」。它會建立 `server_update` pending approval（等價 API
   body 是 `{"name":"worker_5090_117","updates":{}}`）；到核准頁確認完整設定
   後人工核准，再按「檢查 Attempt FS」。必須看到 `eligible` 與具名 local filesystem；
   `unknown` 或 `ineligible_non_local_fs` 都停止。這一步只跑固定唯讀 SSH
   `stat -f`，四個 attempt rollout flags 全部保持 `false`。
2. **Level B — 單 job smoke（可選）**：先做 SQLite online backup，只在隔離的
   non-production control-plane process 同時開啟 runbook §1 的四個 flags，
   確認 `/execution-control/status` 的 `is_leader=true`，再送一個可安全重跑、
   無重要副作用且 pin 到 `worker_5090_117` 的短 job。只有 Job terminal、
   attempt terminal 且唯一 `collect` operation delivered 才算 smoke pass。
   完成後先關 `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED`，確認沒有 in-flight
   attempt，再關閉其餘三個 flags。

Level A/單一 Level B 只證明接線與基本執行可用，**不會**關閉
`RB-LAUNCH-001`，也不能把 `canary-proven` 或 `production-ready` 改成 yes；
正式證據仍是 20 jobs/8 小時及三項 drill（`ssh-canary-evidence-v2`）。

## 尚不能由本機測試代替的證據

1. WP-2D v2：非 production SSH worker，至少 20 jobs/8 小時，包含一次強制
   response-loss、control-plane restart 與 rollback。
2. DG-NODE-CANARY：至少一台實機 Node 先完成 `python -m agent --check`、
   protocol/current-attempt/terminal 流程與 staged rotation 演練。
3. Phase 5：兩節點、100 jobs、連續 7 天，並驗證 agent restart、網路中斷、
   control-plane restart 及逐台退回 SSH。
4. DG-OPS-SLO：裁定 RPO/RTO、backup restore、SLO/error budget 與 production
   ready 所需的正式證據。

在上述項目完成前，以下旗標應維持 false：

```text
EXECUTION_ATTEMPT_SHADOW_ENABLED          # 可依需要只做觀測
EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED
EXECUTION_ATTEMPT_RECONCILE_EXISTING
EXECUTION_OUTBOX_WORKER_ENABLED
EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED
NODE_PROTOCOL_DRAIN_ENABLED
NODE_NEW_ASSIGNMENT_ENABLED
DATASET_SNAPSHOT_V1_ENABLED
DATASET_SNAPSHOT_PUBLISH_ENABLED
CODE_PROMOTION_V1_ENABLED
```

### 測試邊界

- 沒有連 production server、SSH worker、OIDC provider 或外部 credential。
- 沒有修改 `main` branch，也沒有把 unknown/unreachable 推論成 failed。
- 真實 canary 必須依照 `docs/WP_2D_CANARY_RUNBOOK.md` 與待裁定的
  `DG-OPS-SLO` 執行，不能用通過 pytest 取代。
