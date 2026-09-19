---
name: dispatcher-domain
description: Route Dispatch Center architecture/domain work. Use when ownership is unclear, a task spans multiple planes or protected boundaries, or no more specific repo skill clearly owns the change.
---

# Dispatcher Domain

Route first; do not duplicate subsystem rules here.

## Truth order

1. `docs/PLATFORM_CHARTER.md` invariants + `docs/DECISIONS.md`
2. current code + tests
3. `docs/CAPABILITY_LEDGER.md`
4. current V0.1 Product Architecture / UX target
5. `docs/product/V0_1_IMPLEMENTATION_PLAN.md`
6. Roadmap
7. archive/history

## Route

- Project discovery/import/instances → `project-onboarding`
- Development Agent/workspace/session/promotion → `development-agent-safety`
- Approval/auth/mutation gate → `approval-boundary`
- SSH/SFTP/rsync/tmux/remote execution → `ssh-dispatch-safety`
- SQLite/lifecycle/reconciliation/scheduler state → `state-reconciliation`
- Studio UI → `frontend-architecture`
- Pre-release verification → `release-gate`
- Explicit pilot deploy/rollback/restart → `updating-pilot-site`

## Composition

The domain operation chooses the primary skill. Add mechanism/boundary skills only
when that boundary actually changes.

- UI rendering is frontend-owned; Agent authority is Development-Agent-owned.
- Project onboarding is onboarding-owned; SSH command/probe mechanics are SSH-owned.
- Approval mechanics are approval-owned.
- Generic durable/recovery semantics are state-owned; SSH launch/sentinel transport is SSH-owned.
- Release verification and deployment are separate operations.

If guidance conflicts, follow Charter/Decisions and current code/tests. Stop the
protected part if semantics remain unclear.
