# DG-WP2D-CANARY-v2 — Eight-hour SSH attempt canary window

> Date: 2026-08-01
>
> Evidence contract: `ssh-canary-evidence-v2`
>
> Status: **approved by the user's explicit instruction quoted in §7.**
>
> Scope: this decision changes only the WP-2D minimum observation window from
> 24 hours to 8 hours. It does not weaken any other exit criterion.

## 1. Decision

The WP-2D canary defined by `DG-AMBIGUOUS-LAUNCH-v1` now requires one
designated non-production SSH worker, at least 20 terminal workloads, and an
observation window of at least 8 hours.

This v2 decision supersedes only the `24 hours` duration in
`docs/decisions/DG_AMBIGUOUS_LAUNCH_DECISION.md` §9. The v1 document and its original
decision record remain unchanged as historical evidence.

## 2. Unchanged exit criteria

All of the following remain mandatory:

- exactly one designated non-production SSH worker and an immutable candidate
  commit;
- at least 20 terminal SSH attempts inside the evidence window;
- one forced launch-response-loss drill;
- one control-plane restart while an attempt is running;
- one rollback-to-legacy-SSH drill;
- zero duplicate launches and no Job with more than one active attempt;
- zero false failures, lost terminals, unresolved unknown attempts or uncertain
  operations at the cutoff;
- all attempts terminal at the cutoff; and
- exactly one delivered collect operation for every terminal attempt.

The report still fails closed if any evidence field or drill reference is
missing, malformed, outside the window or tied to another server/window.

## 3. Versioning and compatibility

- New evidence must use `contract_version=ssh-canary-evidence-v2`.
- The v2 evaluator requires `window_seconds >= 28800`.
- A v1 manifest is rejected by the v2 evaluator; it is not silently reinterpreted.
- The default minimum remains 20 Jobs and cannot be reduced with documentation.
- The candidate commit must remain an exact 40-character lowercase Git commit.
- The evaluator must bind every scoped attempt to the immutable canary approval
  contract and require one candidate commit matching the evidence manifest.

## 4. Explicitly unaffected gates

This decision does not change:

- `INV-STATE-2`, the atomic remote claim/receipt protocol, or any SSH invariant;
- the Phase 5 Node canary's one-Node 24-hour stage, two-Node 100-Job/7-day stage,
  or `DG-NODE-CANARY` evidence contract;
- the Phase 6 RPO 24-hour target or any `DG-OPS-SLO` requirement;
- the requirement that production remains on the legacy SSH path until the
  canary passes; or
- the rule that local pytest/smoke results are not canary evidence.

## 5. Risk acceptance

Reducing the observation window from 24 hours to 8 hours produces less temporal
stability evidence. The report and capability ledger must identify the result
as an **8-hour WP-2D v2 canary** and must never describe it as satisfying the
original 24-hour v1 window.

The shorter window is accepted only because all fault drills and correctness
criteria remain mandatory and are evaluated from persisted evidence.

## 6. Rollout authorization

The same user instruction authorizes starting this bounded WP-2D canary on
`worker_5090_117`, the already selected non-production SSH worker, after:

1. the candidate code is committed and its exact commit is recorded;
2. an SQLite online backup is taken;
3. its active approved server revision remains preflight `eligible`; and
4. the execution-control leader reports a non-null fencing epoch.

This authorization does not cover any production worker or the Phase 5 Node
canary. On any failure signal, stop new attempt assignment first and keep
reconciliation/outbox ownership enabled until in-flight attempts drain.

## 7. Ruling record

User instruction:

```text
測試玩了!可以跑!接下來正式測試吧!幫我改規章8小時就好
```

Ruling: approve this bounded v2 amendment and the non-production WP-2D canary
start described above. Only the time threshold changes; every other safeguard
and exit criterion remains in force.
