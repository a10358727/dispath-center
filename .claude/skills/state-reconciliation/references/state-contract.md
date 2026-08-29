# State and reconciliation contract

Canonical semantics live in the relevant `INV-STATE-*`, `INV-SSH-*`, and `INV-AUDIT-*` sections of `../../dispatcher-domain/references/invariants.md` and named decisions.

## Durable truth

- Persistent state lives in SQLite and reviewed worker evidence; AppState caches are disposable/rebuildable.
- DB changes precede remote side effects and every crash window must converge through reconciliation.
- Ambiguous launch outcomes remain unknown/running against the same execution identity; do not silently retarget or revert.
- Unreachable, stale heartbeat, or missing evidence means unknown, never success/failure.

## Schema/lifecycle

- Schema changes are additive and update both schema/migration paths with tests.
- Do not invent status values, transitions, terminal revival, or reconciliation semantics without a named decision.
- Recheck stale state after awaits; keep long work out of scheduler/monitor loops and track background tasks.
- Preserve reviewed server-config backup/atomic-write/hot-reload behavior.

## Development Plane objects

Development objects use the same durable-state principles but their lifecycle semantics come from current code/tests and named decisions, not from this skill.

## Validation

Run focused DB/migration/job/scheduler/background/reconciliation tests for the touched path. Never open runtime DBs, contact SSH, or mutate runtime files.
