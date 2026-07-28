# DG-NODE-V2 — Server-selected leases, recovery, and per-node retirement

> Date: 2026-07-28
>
> Contract revision: `DG-NODE-V2-v1`
>
> Status: **approved on 2026-07-28 for WP-4A/4C implementation. No flag
> changes, no node is enrolled, `NODE_AGENT_V1_ENABLED` stays off, and
> real-machine activation still requires `DG-NODE-CANARY`.**
>
> Authoritative decision record: `docs/DECISIONS.md`
>
> Approved reviewed-draft SHA-256:
> `870ba081453d467fd5112dcc5d039e1b971d324e114d45e3415ad3ea87133a2c`
> (verifiable in commit `3d91686`; this status block was appended afterwards)
>
> Required approval phrase:
> `DG-NODE-V2 v1：核准本文件的 recommended contract`

Required before WP-4A in `docs/NEXT_IMPLEMENTATION_PLAN.md` §9. Phase 4 has
supplied a runnable daemon (`python -m agent`), so the remaining blocker is
authorization, not code: nothing here decides whether an agent *can* run, only
whether it may be given work.

---

## 1. The defect this gate fixes

Under v1 the **agent** supplies the `job_id` it wants when it polls. That
inverts the trust relationship: the control plane's job is to decide what runs
where, and an agent that names its own work can — accidentally or otherwise —
ask for work it was never eligible for.

v2 makes the lease **server-selected**: the agent asks "is there work for me",
and the control plane answers with work *it* chose, using the same eligibility
rules the SSH path uses. The agent never names a job.

Second defect: a Node terminal today only closes `node_attempts`. It does not
converge the canonical Job, so a Job can sit `running` forever while its node
attempt is finished. v2 requires Node terminals to converge the Job through the
same projection the SSH path uses.

---

## 2. Recommended decision

1. **Server-selected lease.** `POST /node-agent/poll` takes no `job_id`. The
   control plane picks using existing eligibility (enabled, online, tags,
   pinning, dataset availability) and its own scheduling policy.
2. **One active attempt per node, enforced by the database**, not by agent
   good behavior. A second poll returns the same attempt with `reused=true`.
3. **Current-attempt recovery.** A restarting agent asks what it currently
   owns rather than inferring it. The control plane answers from its own
   records; the agent never decides that an acknowledged attempt is
   abandonable.
4. **Node terminals converge the canonical Job**, via the same projection
   guards WP-2C added for SSH — a terminal state must match the attempt's own
   recorded state, and nothing may invent one.
5. **Staged credential rotation.** A node accepts a new token while the old
   one still works for a bounded window, so rotation is not an outage.
6. **Retirement and revocation are different operations.** Routine retirement
   drains first: no new leases, existing work finishes. Emergency revocation
   is immediate, even with work in flight — but revoking a credential does
   **not** mark that work failed, and does **not** authorize SSH relaunch.
7. **Per-node, always.** Enabling one node never enables another, and
   `execution_backend: ssh` on a server is a complete rollback needing no data
   migration (`INV-NODE-6`).

---

## 3. What must not change

- `INV-NODE-1…6` remain canonical. In particular: non-root, outbound-only, no
  inbound control port, and no agent may launch work it has not acknowledged.
- SSH stays every worker's compatibility and emergency channel forever.
- `maybe_auto_approve()` remains exactly `enqueue|stop`.
- An unreachable node never means a failed job.
- A node cannot be its own approver for anything.

---

## 4. Sub-decisions requiring an explicit ruling

| # | Question | Recommended | Consequence if rejected |
|---|---|---|---|
| N-1 | Does the agent keep supplying `job_id` on poll? | **No — server-selected only.** The control plane decides what runs where; an agent naming its own work inverts that. | Keeping it leaves a worker able to request work it was never scheduled for. |
| N-2 | What does a restarting agent do about an acknowledged attempt with no live process? | **Report it and stop.** The control plane decides; the agent never relaunches and never declares it dead. | Either alternative — relaunch or self-declared failure — is the duplicate-execution or false-terminal failure the whole design forbids. |
| N-3 | Emergency revocation with work in flight | **Revoke immediately; the attempt becomes `unknown`, not `failed`, and SSH relaunch is not authorized.** | Marking it failed fabricates a terminal state nobody observed; authorizing relaunch risks running the workload twice. |
| N-4 | Credential rotation | **Staged: both tokens valid for a bounded overlap.** | Hard cutover turns every rotation into an outage and pressures operators into skipping rotations. |
| N-5 | When may a node take real work? | **Only after `DG-NODE-CANARY` passes on real machines.** This gate unlocks implementation, not production. | Conflating the two would put unproven execution on real workloads. |

---

## 5. Acceptance after approval

- An agent cannot obtain work by naming a job id; the field is gone and a
  payload carrying one is rejected.
- Two concurrent polls from one node yield one attempt, proven by the database
  constraint rather than by call ordering.
- A restarted agent with an acknowledged, process-less attempt produces
  `unknown` and zero relaunches.
- A Node terminal converges the canonical Job exactly as the SSH path does,
  including refusing a terminal the attempt does not carry.
- Rotation succeeds with zero failed polls during the overlap window.
- Retirement drains; revocation is immediate and marks nothing failed.
- Disabling one node leaves every other node untouched, and reverting a server
  to `execution_backend: ssh` needs no migration.

---

## 6. Decision

- [x] **Approve recommended contract** — adopt §2 and §4's recommendations
  (N-1…N-5) and authorize WP-4A/4C implementation with `NODE_AGENT_V1_ENABLED`
  still off and no node enrolled.
- [ ] Approve with changes: ______________________________________________
- [ ] Reject — Node stays at protocol primitives plus an unusable daemon, and
  every worker stays on SSH indefinitely.

Approving unlocks implementation only. Real-machine activation additionally
requires `DG-NODE-CANARY`.
