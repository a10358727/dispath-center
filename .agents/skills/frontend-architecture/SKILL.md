---
name: frontend-architecture
description: Use for Dispatch Center Studio UI work: React/TypeScript rendering, interaction, client state, accessibility, and frontend data contracts. Not for backend or Development Agent authority.
---

# Frontend Architecture

The browser is a projection of server truth, not an authoritative state machine.

## Read

- only the relevant section(s) of `docs/product/V0_1_UX_PLAN.md` for the current work packet; do not load the whole plan by default
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

When optional external skills are installed:
- use `frontend-design` only when visual/design direction is part of the task, not for routine UI wiring;
- for `vercel-react-best-practices`, read only the relevant rule files after its index; do not load its compiled `AGENTS.md` unless the task explicitly requests a full React performance audit.

## Validation

Run focused Studio tests, build when required, frontend smoke checks, and affected
backend contract tests. Never contact live production endpoints.
