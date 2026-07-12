---
name: dispatcher-domain
description: Core dispatch-center architecture context. Use for changes or questions involving app/, tests/, static/index.html, jobs, approvals, datasets, scheduler, workers, or Codex Runner.
---

# Dispatcher Domain

- Read `references/architecture.md` only when architecture or data flow matters.
- Read the relevant INV sections in `references/invariants.md` before changing protected behavior; do not load the whole file when a targeted section is enough.
- Read `references/glossary.md` only for unfamiliar project terms.
- Treat `PLAN.md` as newer than the original implementation brief; verify both against current code.
- Changing an invariant requires explicit user approval.
- Use the specialized approval, SSH, state, frontend, or release skill only when its trigger matches.
