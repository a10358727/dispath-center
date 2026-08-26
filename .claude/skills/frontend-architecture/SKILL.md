---
name: frontend-architecture
description: Rules for dispatch-center UI work in static/. Use for HTML/CSS/JS, tabs, panels, forms, modals, rendering, polling, WebSocket/streaming UI, onboarding or development-agent session UI (Codex, Claude Code), diffs, test results, approval/stale/disconnected states, loading/error states, accessibility, or frontend review.
---

# Frontend Architecture

## Surfaces

- `static/workspace.html` + `static/workspace.js` + `static/workspace-features.js`
  + `static/workspace.css` — the single UI surface (DG-UI-UNIFICATION v1,
  U1–U8, 2026-08-25/26). `GET /` always serves `workspace.html`; every
  workspace read/write goes through an explicit `/api/v2` path allowlist
  gated by `API_V2_ENABLED` (default off) — a clean configuration serves a
  minimal inline Chinese notice instead. `workspace-features.js` is the
  business-logic/pure-function module (loaded first), handed off via
  `window.WorkspaceUI`; `workspace.js` builds DOM nodes and wires events.
- The legacy `static/index.html` + `static/ui.js` + `static/ui.css` surface
  is retired and deleted. Do not recreate it or add a second UI surface;
  every legacy panel has been ported into the Workspace above.

There is no build step, framework, bundler, or `static/js` package directory.
Do not introduce one.

## Required reads

- `references/ui-states.md` — the state-coverage table (loading/empty/error/
  success/stale/unknown/disconnected/partial/blocked) and the domain-specific
  rules for onboarding, approvals, diff review, test results, agent session UI,
  and the future Development Agent selector.

## Hard rules

- **The server is the source of truth.** Browser state, WebSocket state, and
  in-memory session state are never authoritative: every rendered fact traces
  to a server response; optimistic UI is visibly provisional, replaced by the
  next authoritative response, and reverted on failure; reconnect/reload must
  rebuild the full view from the server; mutations carry idempotency/
  expected-version info so a retry cannot double-apply.
- Keep vanilla JS; no frameworks, bundlers, dependencies, or auth-exempt routes.
- Preserve DOM IDs and API behavior unless the user approves a compatibility change.
- New UI logic goes in `workspace.js` (DOM/events) or `workspace-features.js`
  (pure/business logic, exported via `window.WorkspaceUI`); there is no
  legacy surface left to add to.
- Requests go through the shared API client; polling through one cancellable,
  visibility-aware mechanism.
- Default-off capabilities render as unavailable, not broken or fabricated.
- Never render internal filesystem paths, credentials, or secret values; render
  server-supplied text through text nodes.
- Preserve keyboard access, focus behavior, semantic buttons, and text — not
  color alone — for status.
- Before building UI against any backend capability (agent sessions, provider
  selection, conversations, metrics, experiments, …), verify the endpoint
  contract from current code + tests and its flag/rollout status from
  `docs/CAPABILITY_LEDGER.md`. Never build UI against invented endpoints or
  session state machines — if the backend is missing, stop and report.

## Validation

- Inspect the current DOM, render function, and owning file before editing.
- Cover the applicable states from `references/ui-states.md`.
- Add targeted smoke tests and verify the affected flow.
- Stop for cross-feature architecture or backend API changes.
- Never install frontend dependencies, start production services, or contact
  live endpoints.
