---
name: project-onboarding
description: Rules for bringing a project into dispatch-center. Use for inventory scan, project candidates, import/link, git-init, hub sync, project instances, instance update, project bootstrap v2, normalize, or any project discovery/registration work.
---

# Project Onboarding (Development Plane)

Onboarding turns "a directory on some server" into "a project this system may
reason about". It never runs a workload and never decides where anything
executes.

## Required reads

- `../dispatcher-domain/references/development-platform.md` §2/§6 — plane
  model and how to verify whether a capability exists. Verify current
  onboarding capabilities from code + tests (`app/inventory.py`,
  `project_instances`, `app/hub.py`, the onboarding entries in
  `VALID_APPROVAL_KINDS`) and rollout status from
  `docs/CAPABILITY_LEDGER.md`; a capability with no named ruling in
  `docs/DECISIONS.md` (e.g. a `dispatch.yaml` contract or Normalize report)
  is a product decision, not something to build. GitHub publication stays
  interface + fake per ruling D6 — wiring a real adapter/credential/route
  needs a new named ruling.
- Relevant `INV-APPROVAL-*`, `INV-SSH-3/4/7`, `INV-STATE-1/3` sections in
  `../dispatcher-domain/references/invariants.md`.

## Hard rules

- **Discovery is read-only**: closed command set, secret-file/path filtering
  in the Python command-building layer (`INV-SSH-4`); never accept arbitrary
  paths or commands. `.env`, keys, credentials, tokens are excluded at build
  time and never reach DB, API, audit, or an LLM prompt.
- **Registration is a material mutation**: import, link, ignore, git-init,
  deploy, instance update all go `request_*` → pending approval → `approve()`.
  Forbidden-path checks run at request time and again before the actual
  scan/mutation. Revalidate candidate/instance state at approval time
  (`INV-APPROVAL-3`). No onboarding kind ever joins the `enqueue|stop`
  auto-approval allowlist (`INV-APPROVAL-4`).
- **Never mutate a checkout to make onboarding succeed**: no auto reset,
  stash, merge, or silent overwrite — a dirty/diverged/busy instance is
  reported and the operation stops.
- **Instance state is observed, not assumed**: unreachable = unknown, never
  missing/clean (`INV-SSH-7`). Server folders are instances, not version
  truth; code truth flows hub/ProjectVersion → server.
- **Provider-neutral outcome**: completed onboarding yields capabilities —
  **Workspace Ready** (a dispatch-created isolated workspace/worktree is
  possible from a known revision) and **Agent Ready** (a Development Agent
  session can be requested, provider chosen at session time from the
  configured allowlist). Never hardcode "Codex Ready" or any
  provider-specific readiness into states, schemas, UI, or docs; provider
  concerns belong to `development-agent-safety`.
- Persistence follows `INV-STATE-1/3`: survives-restart state never lives
  only in AppState; new columns update both `SCHEMA` and migrations with tests.

## Validation

Normally: `pytest tests/test_inventory.py tests/test_project_instances.py
tests/test_project_instance_update_v2.py tests/test_project_bootstrap_v2.py
tests/test_approvals.py -q`

Cover: successful registration, dangerous/forbidden-path rejection,
secret-file exclusion, stale-candidate revalidation, unreachable-server
behavior.

Never contact real servers, read real keys, or mutate runtime
`jobqueue.db`/`audit.jsonl`/`servers.yaml`.
