---
name: frontend-architecture
description: Rules for dispatch-center UI work in static/. Use for HTML/CSS/JS, tabs, panels, forms, modals, rendering, polling, WebSocket UI, loading/error states, accessibility, or frontend review.
---

# Frontend Architecture

- Inspect the current DOM, render function, and existing module before editing.
- Keep vanilla JS; do not add frameworks, bundlers, dependencies, or auth-exempt routes.
- Preserve DOM IDs and API behavior unless the user approves a compatibility change.
- Put substantial new logic in the owning `static/js` feature module; do not grow large inline scripts.
- Route requests through the shared API client and polling through one cancellable, visibility-aware mechanism.
- Cover loading, empty, error, success, and stale states where applicable.
- Preserve keyboard access, focus behavior, semantic buttons, and text—not color alone—for status.
- Add targeted smoke tests and verify the affected UI flow. Stop for cross-feature architecture or backend API changes.

Never install frontend dependencies, start production services, or contact live endpoints.
