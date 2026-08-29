---
name: project-onboarding
description: Use for project discovery/import/registration, inventory scans, candidates, instances, bootstrap, normalize, git-init, hub sync, or instance updates.
---

# Project Onboarding

Onboarding turns discovered project locations into governed Project/instance state. It does not run workloads or decide execution placement.

## Read only what applies

- `references/onboarding-contract.md` — discovery, mutation, instance, and provider-neutrality checklist.
- `../dispatcher-domain/references/development-platform.md` — plane/project model and capability verification.
- Relevant `INV-APPROVAL-*`, `INV-SSH-3/4/7`, `INV-STATE-1/3` in `../dispatcher-domain/references/invariants.md`.
- `docs/DECISIONS.md` / `docs/CAPABILITY_LEDGER.md` for capability semantics/rollout.

## Hard boundary

- Discovery is read-only, closed-shape, and secret-filtered.
- Registration/mutation uses reviewed approval paths and approval-time revalidation.
- Never modify a checkout to make onboarding succeed; unreachable state remains unknown.
- Keep onboarding/provider readiness domain-neutral; do not invent contracts or lifecycle semantics from product plans.

## Validation

Run only focused inventory/project-instance/bootstrap/approval tests for the touched path. Never contact real servers/keys or runtime data.
