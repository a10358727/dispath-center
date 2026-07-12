---
name: state-reconciliation
description: Protect state consistency across SQLite, AppState caches, and worker sentinel files. Use for schema/status changes, reconcile/recovery, scheduler/monitor loops, background hooks, hot reload, or server_states access.
---

# State Reconciliation

Read the relevant `INV-STATE-*`, `INV-SSH-6/7`, and `INV-AUDIT-1` sections in `../dispatcher-domain/references/invariants.md`.

For each change check:

- Persistent state belongs in SQLite/sentinel files; AppState is disposable cache.
- DB writes precede remote side effects; every crash point must converge after restart.
- Schema additions update both `SCHEMA` and migrations with migration tests.
- Do not invent status values/transitions or revive terminal jobs without user approval.
- Recheck stale state after `await`; track background tasks and keep long work out of scheduler/monitor loops.
- Preserve server config backup → atomic write → in-memory reload.

Use targeted DB/job/scheduler/background tests. Never open the real `jobqueue.db`, contact SSH, or mutate runtime files.
