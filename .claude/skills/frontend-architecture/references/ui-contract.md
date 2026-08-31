# Frontend contract

Canonical UI rulings live in `docs/DECISIONS.md`; backend capability truth comes from current code/tests and rollout status from `docs/CAPABILITY_LEDGER.md`.

## Surface

- The Studio SPA (`studio/` — React + TypeScript + Vite + Tailwind, TanStack Query) is the single reviewed UI surface (DG-STUDIO-UI v1). It builds to the gitignored `static/studio/`; `GET /` serves it once signed in.
- `static/login.html` is the only other page: script-free, storage-free, OIDC entry only.
- Structure: `studio/src/api/` (typed client + TanStack hooks), `features/`, `pages/`. Server calls go through the shared `api()` helper to `/api/v2/*`; no hard-coded remote origins (smoke gate pins this).
- Do not recreate retired/parallel UI surfaces — legacy `index.html`/`ui.js` and the v2 Workspace `workspace.*` are deleted, and `scripts/frontend_smoke.py` pins their absence.

## State and trust

- Server responses are authoritative; browser/WebSocket/in-memory state is provisional/cache only.
- Reconnect/reload must rebuild from server truth; mutations preserve reviewed idempotency/version semantics.
- Default-off/missing capabilities render as unavailable, not fabricated/broken success.
- Never render secrets/internal paths; server text uses safe text-node rendering.
- Preserve accessibility, keyboard/focus behavior, semantic controls, and non-color status meaning.

## Capability verification

Before building UI against a backend capability, verify the endpoint/schema from current code/tests and rollout state from the capability ledger. Do not invent endpoints or lifecycle state machines.

## Validation

Use `ui-states.md` for state coverage. Run focused checks: `npm run build` / `npm test` in `studio/`, `python scripts/frontend_smoke.py`, and affected backend-flow tests only; never contact live endpoints.
