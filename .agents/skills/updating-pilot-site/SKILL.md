---
name: updating-pilot-site
description: Use only when the user explicitly asks Codex to deploy, rollback, or restart the Dispatch Center pilot site. Never trigger from ordinary coding, review, merge, or UI work.
---

# Updating the Pilot Site

This is an explicit operational workflow, not part of normal development.

## Preconditions

Normal deployment requires:

1. explicit user deployment intent;
2. an exact target commit SHA;
3. release-gate PASS for that same SHA;
4. source HEAD still equals the verified SHA;
5. no deploy step creates/amends/merges/pushes commits.

If exact verification evidence is missing, stop and request the release gate.

## Deployment contract

Use the repository's documented pilot deployment commands for the exact verified
SHA. Preserve runtime data and credentials. Build/sync Studio assets only when
the verified release requires it. Restart only the intended pilot service and
perform the documented health check.

## Rollback

Rollback only to an explicitly identified known-good commit. Never invent the
rollback target.

## Hard boundaries

- Never deploy as a side effect of coding/testing/review/merge.
- Never edit code directly in the runtime worktree.
- Never delete/rewrite runtime DB, audit, results, server configuration, or credentials.
- Never claim deployment/restart safety from memory if current runtime evidence
  contradicts it.

After deployment report deployed SHA, health result, build/dependency actions,
and rollback risk.
