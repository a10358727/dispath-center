---
name: frontend-architecture
description: Rules for dispatch-center UI work in static/. Use for HTML/CSS/JS, tabs, panels, forms, modals, rendering, polling, WebSocket/streaming UI, onboarding or development-agent session UI (Codex, Claude Code), diffs, test results, approval/stale/disconnected states, loading/error states, accessibility, or frontend review.
---

# Frontend Architecture

## Surfaces

- `static/index.html` + `static/ui.js` + `static/ui.css` — legacy operations
  UI; `index.html` still holds substantial inline script incl. the `/ws` chat
  WebSocket client.
- `static/workspace.html` + `static/workspace.js` + `static/workspace.css` —
  Product v2 Workspace (default-off), driven by an explicit `/api/v2`
  path allowlist.

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
- New Product v2 logic goes in `workspace.js`; legacy-surface logic in `ui.js`,
  not the inline script in `index.html`.
- Requests go through the shared API client; polling through one cancellable,
  visibility-aware mechanism.
- Default-off capabilities render as unavailable, not broken or fabricated.
- Never render internal filesystem paths, credentials, or secret values; render
  server-supplied text through text nodes.
- Preserve keyboard access, focus behavior, semantic buttons, and text — not
  color alone — for status.
- No agent conversation/session backend, provider-selection endpoint, or
  Claude Code adapter exists yet (only Codex is implemented). Do not build UI
  against invented endpoints or session state machines — stop and report.

## Validation

- Inspect the current DOM, render function, and owning file before editing.
- Cover the applicable states from `references/ui-states.md`.
- Add targeted smoke tests and verify the affected flow.
- Stop for cross-feature architecture or backend API changes.
- Never install frontend dependencies, start production services, or contact
  live endpoints.
