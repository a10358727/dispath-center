---
name: state-reconciliation
description: Protect durable state and reconciliation across SQLite, AppState caches, worker evidence, scheduler/monitor loops, migrations, and background hooks.
---

# State Reconciliation

Durable evidence owns state; caches are disposable. Recovery must converge after restart, timeout, or interrupted I/O.

## Read only what applies

- `references/state-contract.md` — durable-state, lifecycle, migration, and recovery checklist.
- Relevant `INV-STATE-*`, `INV-SSH-6/7`, `INV-AUDIT-1` in `docs/PLATFORM_CHARTER.md` §6.
- `docs/DECISIONS.md` when lifecycle/status/reconciliation semantics would change.

## Hard boundary

- Persist before remote side effects; ambiguous/unreachable/missing evidence stays unknown rather than being invented as success/failure.
- Do not invent status values, transitions, reconciliation loops, or terminal revival.
- Schema changes use reviewed additive migration paths with tests.
- AppState is never the only home of state that must survive restart.

## Validation

Run focused DB/migration/job/scheduler/background/reconciliation tests only. Never open runtime DBs, contact SSH, or mutate runtime files.
