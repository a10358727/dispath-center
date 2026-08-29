# Frontend contract

Canonical UI rulings live in `docs/DECISIONS.md`; backend capability truth comes from current code/tests and rollout status from `docs/CAPABILITY_LEDGER.md`.

## Surface

- `static/workspace.html`, `workspace.js`, `workspace-features.js`, and `workspace.css` are the single reviewed UI surface.
- Keep vanilla JS and the current dependency-free architecture unless a named decision changes it.
- DOM/event wiring belongs in `workspace.js`; pure/business logic belongs in `workspace-features.js` via `window.WorkspaceUI`.
- Do not recreate retired/parallel UI surfaces.

## State and trust

- Server responses are authoritative; browser/WebSocket/in-memory state is provisional/cache only.
- Reconnect/reload must rebuild from server truth; mutations preserve reviewed idempotency/version semantics.
- Default-off/missing capabilities render as unavailable, not fabricated/broken success.
- Never render secrets/internal paths; server text uses safe text-node rendering.
- Preserve accessibility, keyboard/focus behavior, semantic controls, and non-color status meaning.

## Capability verification

Before building UI against a backend capability, verify the endpoint/schema from current code/tests and rollout state from the capability ledger. Do not invent endpoints or lifecycle state machines.

## Validation

Use `ui-states.md` for state coverage. Run focused frontend smoke/JS syntax/affected-flow tests only; never install frontend dependencies or contact live endpoints.
