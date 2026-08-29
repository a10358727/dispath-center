---
name: frontend-architecture
description: Use for UI work in static/: HTML/CSS/JS, workspace panels, forms/modals, rendering, polling/streaming, approvals, diffs, test/results views, agent-session UI, states, or accessibility.
---

# Frontend Architecture

The browser is a projection of server truth, not an authoritative state machine.

## Read only what applies

- `references/ui-contract.md` — surface, trust, capability, and architecture rules.
- `references/ui-states.md` — required loading/empty/error/stale/unknown/disconnected/partial/blocked states.
- `docs/DECISIONS.md` / `docs/CAPABILITY_LEDGER.md` when UI capability or rollout semantics matter.

## Hard boundary

- Keep the reviewed single dependency-free workspace surface unless a named decision changes it.
- Never invent backend endpoints/session states or treat browser state as durable truth.
- Never render secrets/internal paths; preserve safe text rendering and accessibility.
- Verify backend contract + rollout before exposing a capability.

## Validation

Run focused frontend smoke/JS syntax/affected-flow tests and applicable `ui-states.md` cases only. Never install frontend dependencies, start production services, or contact live endpoints.
