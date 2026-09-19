---
name: frontend-architecture
description: Use for Studio SPA or static login UI work: React/TypeScript pages and components, forms, streaming/polling, approvals, diffs, AI session UI, Context usage, Compute cards, Run/Result cards, states, accessibility, or frontend data contracts.
---

# Frontend Architecture

The browser is a projection of server truth, not an authoritative state machine.

## Read

- `docs/product/V0_1_UX_PLAN.md` for V0.1 user-visible interaction targets
- current backend endpoint/schema tests before using a capability
- Capability Ledger when rollout/default matters

## Ownership

This skill owns rendering, interaction, client state, accessibility, and UI
projection. It does not own Development Agent authority/session semantics.

## Rules

- Studio remains the reviewed React + TypeScript surface.
- Reuse the current design/component system; do not introduce another UI framework.
- Never invent backend endpoints or lifecycle states.
- Reload/reconnect rebuilds from server truth.
- Loading, empty, error, stale, unknown, disconnected, partial, and blocked
  states remain distinguishable when applicable.
- Never render secrets/internal credential values.
- Status meaning must not rely on color alone.
- UI simplification never bypasses approval/security/governance.

Use `development-agent-safety` when Agent authority/session semantics also change.

## Validation

Run focused Studio tests, build when required, frontend smoke checks, and affected
backend contract tests. Never contact live production endpoints.
