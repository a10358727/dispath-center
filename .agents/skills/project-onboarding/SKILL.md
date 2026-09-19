---
name: project-onboarding
description: Use for Project discovery, import, registration, project instances, bootstrap, normalize/git-init flows, inventory scans, hub sync, or project-instance updates. Do not use for Compute/Server onboarding.
---

# Project Onboarding

This skill owns Project discovery/import/instance behavior, not workload
execution or Compute target management.

## Read

- relevant `PLATFORM_CHARTER.md` invariants
- current code/tests
- `DECISIONS.md` / Capability Ledger only when semantics or rollout matter

## Rules

- Discovery is read-only, closed-shape, and secret-filtered.
- Registration/mutation uses reviewed request → approval → apply paths.
- Revalidate stale/path state at approval time.
- Never mutate/reset/stash/merge a checkout merely to make onboarding succeed.
- Unreachable is unknown, not missing/clean.
- Server folders are Project instances, not version truth.
- Do not invent provider-specific Project semantics.

If the task changes SSH/probe command mechanics, also use
`ssh-dispatch-safety`. If approval mechanics change, use
`approval-boundary`.

## Validation

Run focused inventory/project-instance/bootstrap/approval tests only. Never
contact real workers or credentials.
