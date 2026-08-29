# DG-AMBIGUOUS-LAUNCH — Definite pre-launch failure, atomic remote claim, receipt and replay

> Date: 2026-07-27
>
> Contract revision: `DG-AMBIGUOUS-LAUNCH-v1`
>
> Status: **approved on 2026-07-27 for WP-2B/2C implementation and for the
> `INV-STATE-2` amendment in §3.2. No flag activation, production migration,
> worker contact or canary is authorized.**
>
> Authoritative decision record: `docs/DECISIONS.md`
>
> Approved reviewed-draft SHA-256:
> `d38f2dea83ef3badaa302cdd0769e255847a3f71d1fa8bfbbc76656ed9c9bf08`
>
> This status block was appended after the ruling, so the current file digest
> differs from the approved digest above by exactly this block.
>
> WP-2D duration update (2026-08-01):
> `docs/decisions/DG_WP2D_CANARY_V2_DECISION.md` supersedes only the 24-hour duration in
> §9 with an 8-hour minimum. This v1 text remains intact as historical evidence;
> all non-duration exit criteria still apply.
>
> Required approval phrase:
> `DG-AMBIGUOUS-LAUNCH v1：核准本文件的 recommended contract`

This document is the exact review contract required between WP-2A and WP-2B in
`docs/archive/NEXT_IMPLEMENTATION_PLAN.md` §7. It resolves release blocker
`RB-LAUNCH-001` (`docs/CAPABILITY_LEDGER.md`). Unlike `DG-EXEC-ATTEMPT-v1`,
this gate **does amend a canonical invariant** (`INV-STATE-2`), so §3 must be
ruled on as text, not as intent.

Document body is English to match `docs/decisions/DG_EXEC_ATTEMPT_DECISION.md`. The
proposed invariant text in §3.2 is Chinese because it is copied verbatim into
`.claude/skills/dispatcher-domain/references/invariants.md`, which is Chinese.

---

## 1. Recommended decision

Approve a default-off, evidence-based launch protocol with these fixed
properties:

1. `INV-STATE-2` keeps DB-before-side-effect ordering, but its unconditional
   `dispatch failure → queued` clause is replaced by a **definite pre-launch
   failure** clause. Ambiguity never reverts.
2. Ambiguity is resolved by **arbitration, not by observation**. To abandon a
   launch the controller must itself win the same atomic remote claim. Absence
   of evidence is never treated as absence of launch.
3. The remote launcher wins or loses a single POSIX-atomic `mkdir` claim
   directory before it may create a tmux session. There is no read-then-launch
   step anywhere in the path.
4. A launch receipt is published by temp-file + atomic rename, binds
   `attempt_id`, `fencing_token`, tmux session and remote `boot_id`, and is the
   only artifact that proves *which* attempt started.
5. Terminal truth stays sentinel-only and becomes attempt-scoped: the exit-code
   sentinel lives inside the attempt directory, so a stale attempt's sentinel
   can never be mistaken for the current one.
6. Replay uses the same attempt and the same idempotency key against the same
   target. A second attempt is created only after an attempt reaches
   `abandoned_before_launch`, or after the host proves the workload is dead.
7. Legacy SSH remains the production path. Everything here is additive and
   flag-gated off, and legacy exact-string golden fixtures stay byte-identical.

---

## 2. Existing behavior and the exact defect

### 2.1 Evidence

`app/scheduler.py:538-566` marks the Job `running`, calls `dispatch_job()`,
and on **any** exception executes:

```python
db.update_job(job.id, status="queued", server=None, started_at=None)
```

`dispatch_job()` (`app/scheduler.py:297-308`) performs `backend.prepare()` then
`backend.launch()`. `backend.launch()` issues
`tmux new-session -d -s job_{id} 'bash …/run.sh'`
(`app/jobqueue.py:542-544`). A timeout or connection loss **after** the remote
`tmux` has already forked therefore reverts a genuinely running Job to
`queued`, and the next tick may dispatch the same workload to a different
server. The in-code comment at `app/scheduler.py:529-537` correctly analyzes
the crash window but does not cover the response-lost window.

`app/jobqueue.py:574-608` (`reconcile_job`) partially compensates: it
double-checks `exit_code` after seeing `tmux GONE`. It cannot compensate for
the revert above, because after the revert the Job no longer has a `server` to
reconcile against.

### 2.2 Behavior that must not change

- `jobs.status` stays `queued | running | blocked | done | failed | cancelled`
  (`INV-STATE-4`). This gate adds no Job state and no `running → cancelled`;
  that remains `DG-JOB-STATE`.
- `INV-SSH-1…9` remain canonical: no agent dependency, remote dependencies
  capped at `tmux`/`bash`/`nvidia-smi`, all commands from pure `build_*`
  functions, user command bytes only via SFTP file landing, rsync initiated
  from Server A.
- `maybe_auto_approve()` stays limited to `enqueue|stop`.
- Legacy `agent_jobs/{job_id}/…` paths, command strings and golden fixtures are
  unchanged and keep serving legacy attempts forever.
- Unreachable/stale/unknown never yields `failed`, a new target, or a backend
  fallback.
- `DG-EXEC-ATTEMPT-v1` schema, immutability rules and authorization classes are
  inherited unchanged; this gate only adds columns and remote protocol.

---

## 3. Canonical invariant amendment

### 3.1 Current text (`invariants.md` §`INV-STATE-2`)

> - **Statement**：「DB 寫入 + 遠端副作用」的順序一律 DB 在前(先標 running
>   再 SSH 派發),崩潰窗口留下的中間態必須能被 reconcile 收斂;派發失敗顯式
>   revert 回 queued。
> - **Scope**：`app/scheduler.py` 派發路徑;任何新的「落地+副作用」代碼。
> - **Forbidden**：先做副作用再寫 DB(崩潰會導致雙重派發);留下 reconcile
>   無法辨識的中間態。
> - **Verification**：`app/scheduler.py:286` 起的註解記載了完整推理;
>   `tests/test_scheduler.py` 派發失敗 revert 測試。

The defect is the clause 「派發失敗顯式 revert 回 queued」: it does not
distinguish a failure that provably happened *before* the workload could start
from a failure whose outcome is unknown.

### 3.2 Proposed replacement text (verbatim)

> ### INV-STATE-2 先寫 DB、再做遠端副作用;僅 definite pre-launch failure 可退回
>
> - **Statement**:「DB 寫入 + 遠端副作用」的順序一律 DB 在前(先標 running
>   再派發),崩潰窗口留下的中間態必須能被 reconcile 收斂。派發過程失敗時,
>   **只有 definite pre-launch failure 才可以把 Job 從 running 退回 queued**。
>   definite pre-launch failure 的定義是:存在 transport 或遠端 arbitration
>   證據,足以證明 workload 不可能已經在目標機上啟動。其判定只有兩種來源:
>   (a) transport 層證明 launch request 未被送出或未被接受;
>   (b) 控制端自己以原子操作贏得該 attempt 的 remote launch claim,使
>   launcher 永遠不可能再啟動。
> - **Statement(續)**:launch response timeout、連線中斷、或任何無法歸類的
>   例外,一律視為 **ambiguous**,不是失敗。此時 Job 保持 running,attempt
>   保持原 target 與原 backend、`liveness=unknown`,只能以相同 attempt 與相同
>   idempotency key 對同一台目標機重試查證。
> - **Scope**:`app/scheduler.py` 派發路徑、`ExecutionBackend` 的
>   prepare/launch/inspect、reconciler,以及任何新的「落地+副作用」代碼。
> - **Forbidden**:先做副作用再寫 DB;把 timeout/連線中斷/未知例外當成派發
>   失敗而 revert;以「沒看到 tmux/sentinel」作為未啟動的證明;在 ambiguous
>   狀態下建立第二個 attempt 或改派其他伺服器;留下 reconcile 無法辨識的
>   中間態。
> - **Verification**:`app/scheduler.py` 派發路徑的 definite/ambiguous 分類與
>   `tests/test_scheduler.py`、`tests/test_execution_launch_arbitration.py`
>   的 crash matrix;legacy 路徑的既有 revert 測試改為明確標示 legacy 語意。

### 3.3 Migration of meaning

- Legacy (non-attempt) dispatch keeps today's revert behavior, because it has
  no claim/receipt evidence to arbitrate with. It is explicitly labelled as the
  known-defective legacy path in code and tests, not as compliant behavior.
- The revised invariant binds the attempt-driven path from the moment
  `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED` can be turned on.
- Approving §3.2 does **not** authorize enabling that flag; §9 governs rollout.

---

## 4. Definite pre-launch failure boundary

### 4.1 Classification is an allowlist, and the default is ambiguous

| Class | Condition | Attempt outcome | Job |
|---|---|---|---|
| A — definite, transport | Name resolution failure, TCP connect refused/unreachable, TLS/host-key rejection, SSH auth failure — i.e. no channel was ever opened | `abandoned_before_launch` | `running → queued` |
| A — definite, pre-launch stage | `prepare` failed and no `launch` operation ever had `effect_started_at` set | `abandoned_before_launch` | `running → queued` |
| A — definite, arbitration | Controller won the claim per §5.3 | `abandoned_before_launch` | `running → queued` |
| B — ambiguous | Launch write/exec timeout, connection lost after the channel opened, channel opened with no response, SFTP succeeded but launch response missing | attempt stays, `liveness=unknown` | stays `running` |
| B — ambiguous (default) | **Any** error category not explicitly listed in Class A | attempt stays, `liveness=unknown` | stays `running` |

Rules:

1. Class A membership is a closed, enumerated set in code. An unrecognized
   exception type is Class B. Adding a Class A category is a code change with
   its own test, never a runtime configuration.
2. Class A transport evidence is only admissible while
   `execution_operations.effect_started_at IS NULL` for the launch operation.
   Once the controller has begun the launch effect, only §5.3 arbitration can
   produce a definite verdict.
3. `abandoned_before_launch` is already a valid attempt state under
   `DG-EXEC-ATTEMPT-v1` and is the only state that permits a new attempt for
   the same Job.

### 4.2 New persisted evidence

Every classification writes an `execution_attempt_events` row with
`reason_code` from a closed set:

```text
prelaunch_transport_refused | prelaunch_auth_rejected | prelaunch_hostkey_rejected
prelaunch_prepare_failed    | prelaunch_claim_won_by_controller
ambiguous_launch_timeout    | ambiguous_connection_lost | ambiguous_unclassified
```

No reason code may be inferred at read time; the classification is persisted at
the moment of the decision, with the sanitized error detail already redacted.

---

## 5. Atomic remote claim protocol

### 5.1 Layout

```text
agent_jobs/{job_id}/                        # legacy, unchanged
agent_jobs/{job_id}/attempts/{attempt_id}/  # attempt-scoped, created by prepare
  cmd.sh          # user command bytes, SFTP only
  run.sh          # versioned wrapper, SFTP only
  launch.sh       # versioned launcher, SFTP only
  claim/          # the atomic claim directory — mkdir is the arbitration
    attempt_id
    fencing_token
    boot_id
    abandoned     # present only if the controller won
  receipt.json    # published by atomic rename
  job.log
  exit_code       # attempt-scoped terminal sentinel
```

Only a validated integer `job_id` and an `attempt_id` matching
`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` are ever
interpolated into a command string. `fencing_token` and all user bytes reach
the worker exclusively as SFTP file content (`INV-SSH-4`).

**Precondition:** `mkdir` atomicity requires a local filesystem. A worker whose
`agent_jobs` path is on NFS/CIFS/FUSE is ineligible for the attempt-driven
backend; a preflight check records this per `server_config_revision` and fails
closed rather than silently downgrading.

### 5.2 Launcher (SFTP'd bytes, versioned, exact golden)

```bash
#!/bin/bash
# dispatch-center attempt launcher, contract version v2
set -u
D="$(dirname "$0")"
if ! mkdir "$D/claim" 2>/dev/null; then
  echo LAUNCH_CLAIM_TAKEN
  exit 0
fi
printf '%s\n' "$ATTEMPT_ID"    > "$D/claim/attempt_id"
printf '%s\n' "$FENCING_TOKEN" > "$D/claim/fencing_token"
cat /proc/sys/kernel/random/boot_id > "$D/claim/boot_id" 2>/dev/null || true
tmux new-session -d -s "$SESSION" "bash $D/run.sh"
printf '%s' "$RECEIPT_JSON" > "$D/receipt.json.tmp"
mv -f "$D/receipt.json.tmp" "$D/receipt.json"
echo LAUNCH_CLAIMED
```

- `mkdir` without `-p` is the entire arbitration. A loser exits 0 without
  launching and without writing anything.
- The launcher is invoked detached (`setsid`, stdio to `/dev/null`) so a
  dropped controller connection cannot `SIGHUP` it mid-sequence.
- `run.sh` keeps sentinel duty and must make an approved stop produce a real
  numeric sentinel rather than a controller-fabricated one:

```bash
trap 'c=$?; printf "%s" "$c" > "$D/exit_code.tmp"; mv -f "$D/exit_code.tmp" "$D/exit_code"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP
bash "$D/cmd.sh" > "$D/job.log" 2>&1
```

### 5.3 Controller arbitration (the abandon path)

To declare a definite pre-launch failure after the launch effect started, the
controller must **win the same claim**:

```text
if mkdir {attempt_dir}/claim 2>/dev/null; then \
printf abandoned_by_controller > {attempt_dir}/claim/abandoned; echo CLAIM_WON; \
else echo CLAIM_TAKEN; fi
```

- `CLAIM_WON` → the launcher can never win afterwards, so the workload provably
  never started → `abandoned_before_launch`, Job `running → queued`.
- `CLAIM_TAKEN` → someone launched, or the controller previously abandoned.
  Read `claim/abandoned`, `receipt.json`, tmux and sentinel to decide; never
  requeue on this branch.
- SSH failure while arbitrating is itself ambiguous: nothing is written and the
  attempt stays `unknown`.

This is why §1.2 says *arbitration, not observation*: there is no settle-window
race, because absence of a claim is never the verdict — winning it is.

### 5.4 Receipt

`receipt.json` is canonical JSON (`app/execution_contract.canonical_json`)
containing `attempt_id`, `fencing_token`, `session`, `boot_id`,
`launcher_contract_version`, `started_at`. Its SHA-256 is persisted on the
attempt at first successful read.

The controller trusts tmux or a sentinel **only after** the companion
`claim/attempt_id` and `claim/fencing_token` match the attempt row. A file
whose companion token mismatches is stale evidence and is ignored, never
deleted.

---

## 6. Resolution procedure for an unknown attempt

Executed by the reconciler, in order, always against the attempt's pinned
`server_config_revision_id`:

1. Read `claim/attempt_id` + `claim/fencing_token`. Mismatch or unreadable →
   remain `unknown`, no state change.
2. `claim/abandoned` present → `abandoned_before_launch` (already decided).
3. `exit_code` present and companion token matches → `done`/`failed` by exit
   code. This is the only workload terminal evidence (`INV-SSH-9`).
4. tmux session for this attempt exists → `running`, `liveness=known`.
   `receipt.json` may be absent; that only means the launcher was interrupted
   between `tmux` and the rename, which is not evidence of non-launch.
5. `receipt.json` present, no tmux, no sentinel, remote `boot_id` **equals** the
   receipt's → the workload started and vanished without a sentinel → remain
   `unknown` (see D-2).
6. Remote `boot_id` **differs** from the receipt's → the host rebooted, so the
   workload is provably dead → attempt `failed`
   (`host_rebooted_before_terminal`), Job `running → queued` for a new attempt
   (see D-3).
7. Claim exists, no receipt, no tmux, no sentinel, `boot_id` unchanged →
   remain `unknown`. Do not arbitrate: the claim is already taken.
8. Target unreachable at any step → no state change at all.

Replay always reuses the same launch operation and its
`idempotency_key = launch:{attempt_id}:{fencing_token}`
(already `UNIQUE` in `execution_operations`). A second launch operation for the
same attempt is a bug, not a retry.

---

## 7. Additive schema delta

Additive only, per `INV-STATE-3`; legacy rows stay `NULL` and are never
backfilled.

`execution_attempts`:

```sql
remote_claim_state TEXT
  CHECK (remote_claim_state IS NULL OR remote_claim_state IN
    ('unclaimed', 'launcher_claimed', 'controller_abandoned', 'claim_unknown')),
launch_receipt_sha256 TEXT,
remote_boot_id TEXT,
launcher_contract_version TEXT,
prelaunch_verdict TEXT
  CHECK (prelaunch_verdict IS NULL OR prelaunch_verdict IN
    ('definite_not_launched', 'ambiguous'))
```

`execution_operations`:

```sql
transmission_state TEXT
  CHECK (transmission_state IS NULL OR transmission_state IN
    ('not_transmitted', 'transmitted', 'unknown'))
```

`server_config_revisions`:

```sql
attempt_backend_preflight TEXT
  CHECK (attempt_backend_preflight IS NULL OR attempt_backend_preflight IN
    ('eligible', 'ineligible_non_local_fs', 'unknown'))
```

No table is dropped, no CHECK is widened, and no existing column changes
meaning. `jobs` is untouched.

---

## 8. Command purity and golden fixtures

- Legacy `build_mkdir_command`, `build_launch_command`,
  `build_check_exit_code_command`, `build_tmux_check_command`,
  `build_log_tail_command` keep byte-identical outputs and existing golden
  tests.
- New builders (`build_attempt_prepare_command`, `build_attempt_claim_command`,
  `build_attempt_abandon_command`, `build_attempt_inspect_command`) are pure
  functions with their own versioned exact-string golden fixtures, keyed by
  `launcher_contract_version`.
- Changing any launcher/wrapper byte requires a new
  `launcher_contract_version` and a new golden fixture. Old versions stay in
  the tree so in-flight attempts remain interpretable after a control-plane
  upgrade.

---

## 9. Feature flags and rollout

Inherits the `DG-EXEC-ATTEMPT-v1` flags and adds one:

```text
EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=false
```

- Enabling it requires `NEW_CLAIMS`, `RECONCILE_EXISTING` and
  `OUTBOX_WORKER` all true; an invalid combination fails configuration
  validation before any background loop starts.
- `NEW_CLAIMS=true` additionally requires `RB-SERVER-001` resolved: a target
  whose `server_config_revisions.assignment_eligibility='legacy_observed'`
  stays ineligible for generic claims regardless of this gate.
- Rollback order is fixed: stop new attempt assignment first; keep
  `RECONCILE_EXISTING` and the outbox worker on so in-flight attempts drain;
  only then re-enable legacy SSH assignment, and only for Jobs with no active
  generic attempt and no dual-read legacy Node ownership.
- An `unknown` attempt is never requeued by the legacy scheduler during
  rollback.
- WP-2D canary: one designated non-production worker, ≥20 jobs / 24 hours,
  including one forced response-loss and one control-plane restart. Exit
  criteria: zero duplicate launches, zero false failures, zero lost terminals,
  100% collection success, zero unresolved unknowns at the end of the window.

---

## 10. Invariant compatibility

- `INV-STATE-1`: durable truth stays in SQLite; remote files are evidence.
- `INV-STATE-2`: **amended by §3.2** — the only invariant text this gate
  changes.
- `INV-STATE-3`: §7 is additive and migration-tested.
- `INV-STATE-4`: Job state set and transitions unchanged. `running → queued`
  already exists; no `running → cancelled` is introduced.
- `INV-SSH-1…9`: dependencies still capped at `bash`/`tmux`; user bytes still
  SFTP-only; terminal truth still sentinel-only, now attempt-scoped.
- `INV-NODE-*`: untouched; Node assignment stays blocked by `DG-NODE-V2`.
- `INV-APPROVAL-*`: launch/prepare/collect keep `authorization_class=execution`
  bound to the original execution contract; arbitration and inspection are
  `inspect` class and carry no mutation authority.
- `INV-AUDIT-*`: every classification and arbitration writes an attempt event;
  audit failure never rewrites attempt or Job state.

Still deferred and **not** unlocked here: `DG-JOB-STATE`,
`DG-ATTEMPT-RECOVERY`, `DG-NODE-V2`, `DG-DATASET-SNAPSHOT`, `DG-CODE-PROMOTE`,
`DG-AUTHZ-ENFORCE`, `DG-SSH-HOSTKEY`, `DG-GPU-SCHED`.

---

## 11. Sub-decisions requiring an explicit ruling

| # | Question | Recommended | Consequence if rejected |
|---|---|---|---|
| D-1 | Abandonment by controller-won claim (§5.3) vs. observation plus a settle window | **Controller-won claim.** It removes the race entirely; observation can never prove a `mkdir` will not happen one millisecond later. | Any observation window is a duplicate-launch risk that cannot be closed by testing. |
| D-2 | Receipt present, no tmux, no sentinel, same `boot_id` (§6.5) | **Remain `unknown` indefinitely; require human resolution.** Legacy would have requeued this. | Auto-requeue here re-runs workloads killed by OOM/manual `tmux kill-session`; auto-fail marks live-but-unobservable work as failed. |
| D-3 | Reboot detected, claim exists, no sentinel (§6.6) | **Attempt `failed`, Job `running → queued` for a new attempt.** Reboot is positive proof the workload is dead, so requeue is duplicate-safe. | Without it, every host reboot leaves permanently stuck `unknown` attempts needing manual clearing. |
| D-4 | Per-attempt tmux session name `job_{job_id}_{attempt_short8}` instead of `job_{job_id}` | **Per-attempt naming for the new path; legacy naming unchanged.** A reused name makes a stale session indistinguishable from the current attempt. | Stop/inspect must disambiguate by other means, weakening §6.4. |
| D-5 | Non-local `agent_jobs` filesystem (§5.1) | **Fail closed: mark the revision ineligible for the attempt backend and keep it on legacy SSH.** | `mkdir` atomicity assumptions silently break on NFS, which is exactly the duplicate-launch failure this gate exists to prevent. |

---

## 12. WP-2B/2C acceptance after approval

Crash matrix (`tests/test_execution_launch_arbitration.py`, FakeSSH only), each
asserting zero duplicate launch and no false terminal:

1. Crash before / after the claim transaction commit.
2. Crash before / after the outbox operation claim.
3. `prepare` interrupted midway.
4. Crash before the remote claim; crash after claim, before tmux.
5. tmux created but receipt rename not yet done.
6. Two launchers racing the same claim directory; stale/mismatched token.
7. tmux created but launch response lost (the `RB-LAUNCH-001` case).
8. Control-plane restart at each of the above points.
9. Worker unreachable, then reconnecting with each possible remote state.
10. Sentinel written before the DB converged.
11. Controller arbitration racing a launcher that is mid-sequence.
12. Host reboot between launch and reconcile (D-3 path).
13. Stop delivery failure and success; trap-produced sentinel is numeric and
    token-matching.
14. `collect` interrupted and retried; collection failure does not alter
    workload status.

Plus: legacy exact-string goldens unchanged; new versioned goldens added;
fresh + representative legacy migration; approval/jobqueue/scheduler/static
invariant gate and full suite green; a rollback drill proving new Jobs return
to legacy SSH while `unknown` attempts stay owned by the new path; and a
recorded statement that no production network, credential or runtime file was
touched.

---

## 13. Decision

- [x] **Approve recommended contract** — adopt §3.2 as the new `INV-STATE-2`
  text, the §4 classification, the §5 claim/receipt protocol, the §6 resolution
  procedure, the §7 additive schema and the §11 recommendations (D-1…D-5), and
  authorize WP-2B/2C implementation with all flags default-off.
- [ ] Approve with changes: ______________________________________________
      (list the sub-decisions to overturn; §3.2 text edits must be given
      verbatim)
- [ ] Reject — keep legacy SSH, leave `RB-LAUNCH-001` open and unfixed, and do
  not start WP-2B/2C.

Approving unlocks implementation only. It does not authorize enabling
`EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED`, running a production migration,
contacting a production worker, or starting the WP-2D canary; those need the
rollout sign-off in §9 recorded separately in `docs/DECISIONS.md`.
