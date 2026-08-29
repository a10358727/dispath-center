---
name: dispatcher-domain
description: Route dispatch-center architecture/domain work. Use when ownership is unclear, a change spans planes/boundaries, or no more specific skill obviously owns it.
---

# Dispatcher Domain

Route first; do not duplicate subsystem rules here.

## Decide

1. Which plane/domain owns the change?
2. Which truth source decides current behavior?
3. Which specialized skill owns the boundary?

Truth order: `references/invariants.md` + `docs/DECISIONS.md` → current code/tests → `docs/CAPABILITY_LEDGER.md` → product plans.

## Route

| Work | Skill |
|---|---|
| Project discovery/import/instances | `project-onboarding` |
| Development agents/workspaces/sessions/promotion | `development-agent-safety` |
| Approval/auth/mutating agent paths | `approval-boundary` |
| SSH/worker execution/remote commands | `ssh-dispatch-safety` |
| Persistence/scheduler/reconciliation | `state-reconciliation` |
| UI | `frontend-architecture` |
| Release verification | `release-gate` |

Use several only when the change genuinely crosses boundaries. Loading a skill grants no authority.

## References

Read only the sections needed:

- `references/development-platform.md` — plane model, Development Agent model, capability verification.
- `references/architecture.md` — stable architecture/trust boundaries.
- `references/invariants.md` — canonical `INV-*` rules.
- `references/glossary.md` — vocabulary.
- `references/result-analysis.md` — evidence/result analysis.

If a task needs an invariant change or a capability with no named ruling, stop and report the decision required instead of inventing semantics.
