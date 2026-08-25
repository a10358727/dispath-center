---
name: state-reconciliation
description: Protect Compute Plane state consistency across SQLite, AppState caches, and worker sentinel files. Use for schema/status changes, reconcile/recovery, scheduler/monitor loops, background hooks, hot reload, or server_states access.
---

# State Reconciliation (Compute Plane)

This skill owns **Compute Plane** state: jobs, attempts, runs, datasets,
approvals, server config, and the reconciliation that converges them after a
crash or an interrupted connection. Development Plane objects persist under the
same storage rules but do not get new lifecycles invented here.

Read the relevant `INV-STATE-*`, `INV-SSH-6/7`, and `INV-AUDIT-1` sections in
`../dispatcher-domain/references/invariants.md`.

## For each change check

- Persistent truth lives in SQLite and worker sentinel files; AppState
  (`server_states`/`server_configs`) is a disposable cache rebuilt after restart.
- DB writes precede remote side effects, and every crash point must converge
  through reconciliation. Ambiguous launch outcomes stay running with
  `liveness=unknown`; they are never reverted and never re-targeted.
- Schema additions update both `SCHEMA` and the column migrations, with
  migration tests. No column removal, no silent semantic change.
- Do not invent status values or transitions, and do not revive terminal jobs,
  without an explicit user decision. Detection logic (stall) sets flags only.
- Recheck stale state after every `await`; track background tasks in AppState
  and keep long work out of the scheduler/monitor loops.
- Preserve server config backup → atomic write → in-memory hot reload.
- Unreachable, stale heartbeat, and missing evidence all map to *unknown* —
  never to failure or success.

## Plane scope

- Reconciliation converges **execution** state from durable evidence:
  sentinel files, attempt records, approvals, and collected results.
- Development Plane objects (project candidates, instances, workspaces,
  coding runs, ProjectVersions) persist under `INV-STATE-1`/`INV-STATE-3` like
  everything else, and their observed remote state follows the same
  unknown-not-failed rule.
- **Do not invent lifecycle semantics for Development Plane objects** (agent
  sessions, workspaces, coding runs — any provider). Their statuses,
  transitions, and reconcile behavior come only from existing code + tests
  and named rulings in `docs/DECISIONS.md`; adding a status value, a
  transition, or a reconcile loop that no ruling defines is exactly the
  "invent semantics" failure this skill prevents. If a task seems to need
  one, stop and report it.

Use targeted DB/job/scheduler/background tests. Never open the real
`jobqueue.db`, contact SSH, or mutate runtime files.
