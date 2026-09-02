---
name: frontend-architecture
description: Use for UI work in the Studio SPA (studio/, React+TypeScript+Vite, built to static/studio/) or static/login.html: pages/components, forms/modals, rendering, polling/streaming, approvals, diffs, session UI, states, or accessibility.
---

# Frontend Architecture

The browser is a projection of server truth, not an authoritative state machine.

## Read only what applies

- `references/ui-contract.md` — surface, trust, capability, and architecture rules.
- `references/ui-states.md` — required loading/empty/error/stale/unknown/disconnected/partial/blocked states.
- `docs/DECISIONS.md` / `docs/CAPABILITY_LEDGER.md` when UI capability or rollout semantics matter.

## Hard boundary

- The Studio is the single UI surface (DG-STUDIO-UI v1): TypeScript + build in `studio/`, build output gitignored, no external URL resources; `static/login.html` stays dependency-free.
- Never invent backend endpoints/session states or treat browser state as durable truth.
- Never render secrets/internal paths; preserve safe text rendering and accessibility.
- Verify backend contract + rollout before exposing a capability.

## Validation

Run focused Vitest suites (`npm test --prefix studio`), `npm run build --prefix studio` when the build contract changed, `scripts/frontend_smoke.py`, and applicable `ui-states.md` cases only. Never start production services or contact live endpoints.
