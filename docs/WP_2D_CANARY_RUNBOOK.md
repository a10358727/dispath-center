# WP-2D Canary Runbook — closing RB-LAUNCH-001

> Status: **procedure ready, not executed.** Running this requires a real
> non-production SSH worker and a 24-hour window, so it cannot be done from a
> development session.
>
> Authority: `docs/DG_AMBIGUOUS_LAUNCH_DECISION.md` §9 fixes the exit criteria.
> This runbook only tells you how to produce that evidence.

`RB-LAUNCH-001` closes when this window passes. It does **not** close because
WP-2B/2C merged: the code is correct under test, but nothing here has ever run
against a real worker.

---

## 0. Preconditions

Do not start until all of these hold:

- [ ] A **designated non-production** SSH worker exists. Never run this against
      a machine carrying real work — the drill deliberately kills connections
      mid-launch.
- [ ] That worker's `agent_jobs` path is on a **local filesystem**. The atomic
      `mkdir` claim is not reliable on NFS/CIFS/FUSE, which is exactly the
      duplicate-launch failure this whole gate exists to prevent (gate D-5).
      Verify: `stat -f -c %T <agent_jobs path>` should not report `nfs`.
- [ ] `RB-SERVER-001` is resolved, or the target has an approved pinned
      revision. A `legacy_observed` target is ineligible for generic claims and
      the canary cannot start.
- [ ] A backup of the control-plane database exists
      (`python scripts/sqlite_online_backup.py`).
- [ ] You have recorded the window start timestamp in UTC ISO-8601. Every
      report below is scoped by it.

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

Confirm the process actually took ownership before submitting anything:

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/execution-control/status
```

Expect `"is_leader": true` and a non-null fencing epoch. If it reports
`implemented=false` for the remote workers, stop — the build is older than
WP-2C.

## 2. Run the window

- Submit **at least 20 jobs** over **at least 24 hours**. Use a mix of short
  and long workloads; at least one must outlive a control-plane restart.
- Do not submit production work.

## 3. Required drills

These are not optional. The window is not evidence without them, because they
exercise precisely the paths that ordinary success never touches.

### 3.1 Forced response loss (the RB-LAUNCH-001 scenario)

While a job is launching, sever the control plane's connection to the worker
after the launcher has started but before the response returns:

```bash
# on the control plane, during a launch
sudo iptables -A OUTPUT -d <worker-ip> -p tcp --dport 22 -j DROP
sleep 60
sudo iptables -D OUTPUT -d <worker-ip> -p tcp --dport 22 -j DROP
```

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

```bash
python scripts/canary_report.py --db jobqueue.db --since <window-start-utc>
```

The script is read-only (it opens the database immutable) and computes the §9
criteria from persisted evidence:

| Criterion | Threshold |
|---|---|
| attempts in window | ≥ 20 |
| duplicate launches | 0 |
| jobs with >1 active attempt | 0 |
| false failures | 0 |
| lost terminals | 0 |
| non-terminal attempts at close | 0 |
| result collection | 100% |
| unresolved unknown attempts | 0 |

Exit code 0 = pass, 1 = fail, 2 = the evidence itself is unreadable.

## 5. On pass

1. Record the report output and the three drill outcomes in
   `docs/IMPLEMENTATION_PROGRESS.md`.
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
