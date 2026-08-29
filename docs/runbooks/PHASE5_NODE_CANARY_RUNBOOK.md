# Phase 5 Node Canary Runbook

> Status: procedure, evidence schema and evaluator are implemented; **not
> executed**. This document is not deployment or canary evidence.
>
> Scope: two designated non-production ordinary workers, at least 100
> acknowledged workloads and seven consecutive days.
>
> Required authorization: approve `DG-NODE-CANARY` before enrolling or enabling
> a real Node. Emergency-revoke convergence also remains bounded by
> `DG-ATTEMPT-RECOVERY`; this runbook cannot silently authorize abandon,
> relaunch, SSH stop or a fabricated terminal.

## 1. What may be prepared before the gate

The following work is safe to complete before any real activation:

- install the agent package and disabled user service on non-production hosts;
- run `python -m agent --check` with a temporary non-production configuration;
- verify both workers are non-root, outbound-only and have no new listening
  port;
- verify the existing SSH execution path independently on both workers;
- create off-host copies of control-plane data and record rollback owners;
- review this runbook and the evidence template.

Do not enroll a Node, expose a credential, set a server to
`execution_backend: node`, or enable new assignment until `DG-NODE-CANARY`
defines the exact machines, owner and window.

## 2. Preconditions

- [ ] Phase 0–4 local release gates pass on the exact candidate revision.
- [ ] WP-2D has closed `RB-LAUNCH-001`, or `DG-NODE-CANARY` explicitly records
      why the Node canary may start while SSH attempt cutover remains disabled.
- [ ] Two ordinary non-production workers are named and have independently
      tested SSH fallback.
- [ ] Each worker has one server binding, one Node identity, an independent
      credential and an exact agent version.
- [ ] `GET /readyz`, `GET /operations/metrics` and
      `GET /nodes/operations` are available to the canary owner.
- [ ] The response-loss and network drills have a bounded blast radius and an
      operator who can immediately stop new assignment.
- [ ] Emergency-revoke recovery is explicitly authorized. A credential revoke
      may be tested with active work, but that work cannot be abandoned or
      relaunched without `DG-ATTEMPT-RECOVERY`.

Record a UTC `since` timestamp ending in `Z`. Use one unique `require_tag`
(for example `node-canary-20260801`) for this window; do not reuse a production
tag.

## 3. Split flags and staged activation

Use the split controls; keep the legacy aggregate alias off:

```text
NODE_AGENT_V1_ENABLED=false
NODE_PROTOCOL_DRAIN_ENABLED=true
NODE_NEW_ASSIGNMENT_ENABLED=false
NODE_CANARY_REQUIRE_TAG=node-canary-20260801
```

With new assignment still off:

1. Create and approve one `node_enroll` request per worker.
2. Transfer each one-time token through the approved secret-delivery channel.
   Never place it in argv, shell history, logs, audit or the evidence JSON.
3. Start each non-root agent and verify heartbeat/current-attempt operations.
4. Exercise `node_rotate` in staged mode. The agent receives the new token plus
   activation nonce through protected environment/file input and calls
   `/node-agent/activate`; only then does the old primary enter bounded grace.
5. Force one activation response loss. Retry the same activation and verify
   the durable response is idempotent rather than issuing another credential.

The evidence JSON records only `credential_id`, never a raw token, digest or
nonce.

## 4. Progressive rollout

### 4.1 One Node / 24 hours

Set only the first canary server to `execution_backend: node`, then enable:

```text
NODE_NEW_ASSIGNMENT_ENABLED=true
```

Submit and approve ten short `adhoc` or `train` Jobs with the exact canary tag.
Over at least 24 hours, run:

- control-plane restart while an acknowledged Job is running;
- agent restart before and after acknowledge;
- bounded network interruption spanning heartbeat/poll response loss.

Expected throughout:

- duplicate poll returns the same ownership;
- unreachable/response-lost is unknown, never failed;
- restart never relaunches an acknowledged or launch-intent attempt;
- terminal retry converges Node attempt, generic attempt and canonical Job.

### 4.2 Two Nodes / 100 Jobs / seven days

Add the second canary server. Across the full window:

- at least 100 workloads must be acknowledged, not merely leased;
- both Node identities must acknowledge work;
- only the exact canary tag is eligible;
- coding/setup/sync and the Codex Runner remain on SSH;
- no active/unknown attempt may remain when the window closes.

### 4.3 Per-node rollback

For one server, stop Node new assignment (use the approved
`node_retire:start_drain` path or switch only that server's new-work backend
after verifying its publication contract). Existing Node attempts remain
Node-owned and the protocol stays enabled. New eligible work may use SSH only
when no generic/legacy active owner exists.

After active=0, either resume assignment through approval or approve
`complete_retirement`. Never stop the daemon or retire its credential while it
still owns work.

### 4.4 Emergency security revoke

Use a dedicated canary attempt:

1. Approve a single-node `node_revoke` while that Node has active ownership.
2. Verify its primary, grace, pending and activation-receipt credentials all
   immediately return 401; the other Node remains healthy.
3. Verify the generic attempt keeps its backend, target and state but changes
   to `liveness=unknown` with
   `recovery_hold_reason=security_credential_revoked`.
4. Verify the Job is not failed/requeued and material outbox work is frozen;
   only the pinned read-only inspect operation is admissible.
5. Converge only from exact matching terminal/sentinel evidence or the
   separately approved `DG-ATTEMPT-RECOVERY` procedure. If neither exists,
   the canary remains failed/open; do not clear the hold manually.

## 5. Evidence manifest

Copy
`docs/examples/node_canary_evidence.example.json` to an off-repository evidence
directory. Replace every placeholder. Each drill entry has exactly:

- `passed`: must be literal `true`;
- `observed_at`: UTC within the declared window;
- `evidence_ref`: a durable report/audit/ticket reference.

Do not add free-form secrets. The evaluator rejects missing drills, mismatched
Node IDs, a production environment label or a window mismatch.

## 6. Produce the verdict

After all attempts have converged, take an online SQLite backup and run the
report against that stable copy:

```bash
python scripts/node_canary_report.py \
  --db /path/to/backup/jobqueue.db \
  --since 2026-08-01T00:00:00Z \
  --through 2026-08-08T00:00:00Z \
  --require-tag node-canary-20260801 \
  --evidence /path/to/evidence/node-canary-20260801.json
```

Exit codes:

- `0`: every database and drill criterion passed;
- `1`: usable evidence proves at least one criterion failed;
- `2`: database, timestamps or evidence format are unusable.

The report requires:

- seven consecutive days, 100 acknowledged workloads and two observed Nodes;
- zero duplicate/cross-Node launch, false disconnect failure, lost/conflicting
  terminal, cross-target/pin violation, collection projection failure, active
  attempt and unresolved active unknown;
- every acknowledged workload to be terminal, with exactly one delivered
  `dependency_refresh`, `result_collection`, `notification` and
  `owner_projection` operation for each terminal generic attempt;
- durable staged-activation and security-revoke event evidence;
- all nine required drills.

`--json` emits a machine-readable report and keeps the same exit semantics.

## 7. Promotion and rollback

A PASS is necessary but does not change flags or the capability ledger by
itself. Attach the report, evidence manifest digest, candidate commit, exact
configuration and owners to the `DG-NODE-CANARY` decision record.

On any failure:

1. set `NODE_NEW_ASSIGNMENT_ENABLED=false`;
2. keep `NODE_PROTOCOL_DRAIN_ENABLED=true`;
3. leave active Node attempts owned and preserve all evidence;
4. return only owner-free new work to SSH;
5. investigate before opening a new seven-day window.

Do not mark `deployed`, `canary-proven` or `production-ready` from local pytest
results or from an incomplete window.
