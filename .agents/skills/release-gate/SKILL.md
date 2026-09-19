---
name: release-gate
description: Use only when the user explicitly asks for pre-release verification of an exact Dispatch Center commit before pilot/production deployment. Do not auto-run as part of ordinary implementation.
---

# Release Gate

Read-only verification of the exact commit intended for deployment.

## Preconditions

- Resolve the exact HEAD SHA.
- Require a clean source state; if local changes could affect the tested result,
  report PRECONDITION FAILED.
- Never create/amend a commit.

## Verification

1. Run the repository static checks.
2. Run the required full test suite.
3. Check that tests were not deleted to make the change pass.
4. Re-read HEAD; if it changed during verification, FAIL.
5. Report PASS/FAIL and `VERIFIED_SHA`.

PASS applies only to that exact SHA.

Never install dependencies, contact workers/SSH, restart services, mutate runtime
data, or perform Git writes.
