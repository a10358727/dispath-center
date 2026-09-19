---
name: state-reconciliation
description: Use for SQLite persistence, migrations, lifecycle state, AppState caches, scheduler/monitor/background loops, durable worker evidence, ambiguity handling, or reconciliation/recovery behavior.
---

# State Reconciliation

This skill owns generic durable-state, lifecycle, ambiguity, and recovery
semantics.

## Rules

- Persistent state and reviewed worker evidence own truth; caches are disposable.
- Persist before remote side effects where required by current contracts.
- Ambiguous/unreachable/missing evidence stays unknown rather than invented
  success/failure.
- Recovery must converge after restart, timeout, or interrupted I/O.
- Do not invent lifecycle states, transitions, terminal revival, or reconcile
  loops without a named decision.
- Schema changes use the reviewed additive migration path with tests.
- Recheck stale state after awaits.
- AppState is not the sole home of state that must survive restart.

Use `ssh-dispatch-safety` as a secondary skill when SSH/tmux/sentinel mechanics
also change.

## Validation

Run focused DB/migration/job/scheduler/background/reconciliation tests. Never
open runtime DBs or mutate runtime files.
