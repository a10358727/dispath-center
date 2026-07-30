# DG-NODE-CANARY — Real-machine Node rollout authorization

> Date: 2026-07-30
>
> Contract revision: `DG-NODE-CANARY-v1`
>
> Status: **draft for review — not approved and not yet approvable.** Complete
> the exact machines, tag, dates and owners in §2 before ruling.
>
> Authoritative decision record after ruling: `docs/DECISIONS.md`
>
> Reviewed-draft SHA-256: `<computed only after §2 is complete>`
>
> Required approval phrase after all placeholders are resolved:
> `DG-NODE-CANARY v1：核准本文件的 recommended contract`

This gate authorizes a bounded non-production experiment. It does not authorize
production workloads, global Node routing, automatic recovery of unknown work,
authorization enforcement or removal of SSH.

## 1. Recommended contract

1. Run the exact procedure in `docs/PHASE5_NODE_CANARY_RUNBOOK.md`.
2. Use two named non-production ordinary workers. Each retains tested SSH
   fallback, an independent Node identity/credential and a non-root agent.
3. Only `adhoc|train` Jobs carrying one exact unique canary tag are eligible.
   Coding/setup/sync and the Codex Runner stay on SSH.
4. Roll out one Node for at least 24 hours/10 Jobs, then two Nodes for at least
   100 acknowledged Jobs over seven consecutive days.
5. Run every required restart, network, staged-rotation, rollback and
   emergency-revoke drill. Preserve existing ownership and never infer failure
   from unreachable state.
6. `NODE_PROTOCOL_DRAIN_ENABLED` remains on whenever active ownership exists.
   `NODE_NEW_ASSIGNMENT_ENABLED` is the first rollback switch.
7. Promotion requires exit code 0 from
   `scripts/node_canary_report.py` against a stable online backup and the exact
   `node-canary-evidence-v1` manifest. Local tests do not satisfy this gate.

## 2. Exact rollout record — must be completed before approval

| Field | Required value |
|---|---|
| Candidate commit | `<exact Git commit>` |
| Node A / server binding | `<node id>` / `<server name>` |
| Node B / server binding | `<node id>` / `<server name>` |
| Exact agent version | `<version or artifact digest>` |
| Canary tag | `<unique non-production require_tag>` |
| UTC start / earliest close | `<...Z>` / `<at least seven days later ...Z>` |
| Rollout owner | `<person>` |
| Rollback owner | `<person>` |
| Evidence directory | `<off-repository durable path/reference>` |
| Emergency-revoke recovery authority | `<approved DG-ATTEMPT-RECOVERY reference or exact pre-authorized drill>` |

No placeholder may remain when the approval phrase is accepted. Node IDs may be
filled after enrollment only if enrollment itself is separately authorized as
part of this gate record; raw credentials never enter this document.

## 3. Pass/fail and immediate rollback

The canary fails on any duplicate launch, false disconnect failure, lost or
conflicting terminal, cross-target/pin violation, collection-driven execution
failure, unresolved active unknown, missing drill or evidence-format error.

On failure:

1. set `NODE_NEW_ASSIGNMENT_ENABLED=false`;
2. keep `NODE_PROTOCOL_DRAIN_ENABLED=true`;
3. preserve active ownership, DB/audit/agent journals and remote sentinels;
4. route only owner-free new work back to SSH;
5. require a new full window after correction.

Emergency revoke is not a shortcut to rollback. It immediately invalidates one
Node credential and creates an unknown security hold; it never authorizes
failed/requeue/SSH relaunch.

## 4. What approval changes

Approval authorizes only the exact §2 canary. It does not automatically edit
configuration, enroll a Node, start a service or update the capability ledger.
Those actions remain explicit operator steps with their existing approval and
publication records.

After a recorded PASS, `node_protocol_v2` and `node_daemon` may gain
`deployed=yes`/`canary-proven=yes` only when the deployment and report evidence
are committed to `docs/IMPLEMENTATION_PROGRESS.md`. `production-ready` still
depends on `DG-OPS-SLO` and the remaining operational/security gates.

## 5. Decision

- [ ] **Approve recommended contract** after §2 has no placeholders.
- [ ] Approve with changes: ______________________________________________
- [ ] Reject — all real Node enrollment/new assignment remains prohibited and
      ordinary workers continue on SSH.
