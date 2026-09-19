---
name: release-gate
description: Manual exact-commit pre-release verification for dispatch-center. Invoke only with /release-gate before pilot/production deployment; never auto-trigger.
disable-model-invocation: true
context: fork
allowed-tools: Bash(pytest *), Bash(python3 -m pytest *), Bash(bash .claude/skills/release-gate/scripts/static_checks.sh), Bash(git diff *), Bash(git rev-parse *), Bash(git status *), Read, Grep, Glob
---

# Release Gate

Read-only verification of the exact commit intended for deployment.

## Preconditions

1. Resolve `VERIFIED_SHA=$(git rev-parse HEAD)`.
2. Require a clean working tree. If tracked/untracked development changes could
   affect the tested result, report `PRECONDITION FAILED` and never PASS.
3. Never create a commit or modify the repository from this skill.

## Verification

1. Run `bash .claude/skills/release-gate/scripts/static_checks.sh`.
2. Run `pytest -q`.
3. Run `git diff --name-status HEAD -- tests/`; deleted tests fail the gate.
4. Re-read `git rev-parse HEAD`. If HEAD changed during verification, FAIL.
5. Report static results, test counts, test/dependency changes, warnings, final
   PASS/FAIL, and the exact `VERIFIED_SHA`.

A PASS applies only to that exact commit SHA. It is not transferable to a later
commit.

Never install dependencies, contact network/SSH, start/restart services, mutate
runtime data, or perform Git writes. Deployment is a separate explicit
operational action.
