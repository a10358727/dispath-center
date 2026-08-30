# WP-2D Canary Runbook — closing RB-LAUNCH-001

> Status: **procedure ready, not executed.** Running this requires a real
> non-production SSH worker and an 8-hour window, so it cannot be completed
> from a short development smoke session.
>
> Authority: `docs/decisions/DG_WP2D_CANARY_V2_DECISION.md` changes only the original
> `DG-AMBIGUOUS-LAUNCH-v1` 24-hour duration to 8 hours. All other exit criteria
> remain unchanged. This runbook only tells you how to produce that evidence.

`RB-LAUNCH-001` closes when this window passes. It does **not** close because
WP-2B/2C merged: the code is correct under test, but nothing here has ever run
against a real worker.

---

## 0. Preconditions

Do not start until all of these hold:

- [ ] A **designated non-production** SSH worker exists. Never run this against
      a machine carrying real work — the drill deliberately kills connections
      mid-launch.
- [ ] `RB-SERVER-001` is resolved, or the target has an approved pinned
      revision. A `legacy_observed` target is ineligible for generic claims and
      the canary cannot start. For an existing legacy entry, use the Worker UI
      action **建立受管 Revision** (an exact `server_update` request with
      `updates={}`), review it in Approvals and approve it. This fresh decision
      creates revision 1; migration never backfills approval evidence.
- [ ] Run the revision-scoped D-5 preflight after that exact revision is
      active. The fixed read-only command checks `agent_jobs` (or its future
      parent when absent), records the observation on the revision and must
      return `status=eligible`:

      ```bash
      curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
        http://127.0.0.1:8000/server-config/<server-name>/attempt-preflight
      ```

      NFS/CIFS/FUSE records `ineligible_non_local_fs`; transport failure,
      malformed output or an unclassified filesystem records `unknown`.
      Both remain on legacy SSH. A new server revision resets this evidence
      to NULL and requires another preflight.
- [ ] A backup of the control-plane database exists
      (`python scripts/sqlite_online_backup.py`).
- [ ] You have recorded the window start timestamp in UTC ISO-8601. Every
      report below is scoped by it.
- [ ] Copy `docs/examples/wp2d_canary_evidence.example.json` to the durable
      evidence directory and fill the exact candidate commit, server and drill
      references. No placeholder may remain at evaluation time.

## 1. Enable the path

```bash
EXECUTION_ATTEMPT_RECONCILE_EXISTING=true
EXECUTION_OUTBOX_WORKER_ENABLED=true
EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true
EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=true
```

All four are required together — configuration validation refuses the launch
flag without new-claim ownership, and refuses new claims without reconcile and
outbox, because a launch path with nothing responsible for reconciling would
strand work.

The scheduler and the DB claim transaction independently require the active
SSH revision's `attempt_backend_preflight=eligible`. Do not work around a
refusal by editing SQLite; the preflight endpoint is the evidence-producing
path.

Confirm the process actually took ownership before submitting anything:

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/execution-control/status
```

Expect `"is_leader": true` and a non-null fencing epoch. If it reports
`implemented=false` for the remote workers, stop — the build is older than
WP-2C.

### 1.1 Create an attempt-eligible canary request

Do **not** use the ordinary Project **派工** button for this canary. That route
intentionally preserves the legacy enqueue contract; its Job has no immutable
execution approval tuple and the attempt owner must refuse it with
`approval_missing`.

Create one pending, pinned request from the control-plane checkout instead:

```bash
python scripts/request_wp2d_canary.py \
  --db jobqueue.db \
  --audit audit.jsonl \
  --server <exact-non-production-server-name> \
  --acknowledge-non-production \
  --command 'echo wp2d-start && hostname && sleep 30 && echo wp2d-done'
```

The tool performs no SSH, creates no Job and never approves its own request.
It fails closed unless the exact active revision is approved, SSH-backed and
has current revision-scoped D-5 evidence with `status=eligible`. Review the
returned approval ID in **Approvals** and approve it manually. Web-direct,
service-token and auto-rule decisions are rejected for this canary seam.

After approval, confirm the resulting Job has non-null
`execution_approval_id`, `approved_payload_sha256`,
`execution_contract_version=enqueue-execution-v1`,
`execution_contract_role=main` and `approved_command_sha256`. Then confirm one
`execution_attempts` row exists for it. Only that first persisted attempt starts
the evidence window; time spent on rejected/legacy requests does not count.

## 2. Run the window

- Submit **at least 20 pinned canary jobs** with the operator tool above over
  **at least 8 hours**. Use a mix of short and long workloads; at least one
  must outlive a control-plane restart. Each returned pending approval must be
  reviewed and manually approved.
- Do not submit production work.

## 3. Required drills

These are not optional. The window is not evidence without them, because they
exercise precisely the paths that ordinary success never touches.

### 3.1 Forced response loss (the RB-LAUNCH-001 scenario)

While a job is launching, sever the control plane's connection to the worker
after the launcher has started but before the response returns. A normal
launcher response may complete in under 100 ms, so do not try to insert the
rule manually after clicking Approve. Copy the bounded watcher to the exact
non-production worker first:

```bash
# From the control plane:
scp scripts/wp2d_response_loss_worker.sh <worker>:/tmp/

# In an interactive SSH session on that worker. Use the Job id which the
# pending approval will materialize, and wait for ARMED before approving it.
sudo bash /tmp/wp2d_response_loss_worker.sh \
  "${SSH_CLIENT%% *}" <next-job-id> "$HOME"
```

The helper waits up to 10 minutes for the exact Job's attempt claim, then
blocks only worker SSH responses to that control-plane IPv4 address for 35
seconds. It installs both an EXIT trap and a delayed best-effort cleanup. Wait
for `RESTORED` before inspecting the result. If `ARMED` was not printed, do not
approve the request.

**Expected:** the Job stays `running`; the attempt shows `liveness=unknown`;
no second attempt appears; after connectivity returns, reconcile resolves it
from the receipt/sentinel.

**Failure signal:** the Job returns to `queued`, or a second attempt exists for
that Job. Either one means the ambiguous-launch fix is not actually in effect —
stop the canary.

### 3.2 Control-plane restart

Restart the control plane while at least one job is running.

**Expected:** after restart, the running attempt is still owned, reconciles
from remote evidence, and converges normally. The lease is taken over by
expiry/fencing, not by deleting it.

### 3.3 Rollback drill

Set `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=false` while attempts are in flight.

**Expected:** new queued Jobs go back to the legacy SSH path; existing attempts
keep draining through the attempt path; no `unknown` attempt is re-dispatched
by the legacy scheduler.

**Failure signal:** an uncertain attempt gets requeued by the legacy path. That
is a double-dispatch and fails the canary.

## 4. Collect the verdict

At the end of the window, take an SQLite online backup. Evaluate that stable
copy rather than a live WAL database:

```bash
python scripts/sqlite_online_backup.py jobqueue.db /evidence/wp2d/jobqueue.db
python scripts/canary_report.py \
  --db /evidence/wp2d/jobqueue.db \
  --since <window-start-utc> \
  --through <window-close-utc-at-least-8h-later> \
  --server <exact-non-production-server-name> \
  --evidence /evidence/wp2d/wp2d-canary.json
```

The script is read-only (it opens the database immutable) and computes the §9
criteria from persisted evidence. In addition to the operational counters, it
joins every scoped attempt to its immutable canary approval and fails closed if
an approval is missing, malformed, uses another contract/purpose or names a
different candidate commit than the manifest:

| Criterion | Threshold |
|---|---|
| attempts in window | ≥ 20 |
| observed window | ≥ 8 hours |
| duplicate launches | 0 |
| jobs with >1 active attempt | 0 |
| false failures | 0 |
| lost terminals | 0 |
| non-terminal attempts at close | 0 |
| result collection | exactly one delivered collect per terminal attempt |
| unresolved unknown attempts | 0 |
| uncertain operations | 0 |
| required drill manifest | all three exact entries pass within the window |

Exit code 0 = pass, 1 = fail, 2 = the evidence itself is unreadable.

## 5. On pass

1. Record the report output and the three drill outcomes in
   `docs/archive/IMPLEMENTATION_PROGRESS.md`.
2. Update `docs/CAPABILITY_LEDGER.md`: `attempt_driven_ssh` may move
   `deployed=yes` and `canary-proven=yes`. `production-ready` still depends on
   the remaining blockers.
3. Move `RB-LAUNCH-001` to the resolved-blockers table with the report as
   evidence.

## 6. On fail

Do not rerun the window hoping for a cleaner result. A failing line means an
attempt reached a state the contract says is impossible, so the contract or the
implementation is wrong. Investigate the specific attempt's
`execution_attempt_events` rows — every classification and transition is
recorded there with its reason code — and fix the cause before restarting.

Roll back with `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=false`. Leave
`RECONCILE_EXISTING` and the outbox worker on, so in-flight attempts still
drain and keep an owner.
