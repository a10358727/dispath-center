---
name: release-gate
description: Manual pre-release verification for dispatch-center. Invoke only with /release-gate before production restart; never auto-trigger.
disable-model-invocation: true
context: fork
allowed-tools: Bash(pytest *), Bash(python3 -m pytest *), Bash(bash .claude/skills/release-gate/scripts/static_checks.sh), Bash(git diff *), Bash(git rev-parse *), Bash(git status *), Read, Grep, Glob
---

# Release Gate

Read-only workflow:

1. Require a local Git HEAD; without it report `PRECONDITION FAILED` and never PASS.
2. Run `bash .claude/skills/release-gate/scripts/static_checks.sh`.
3. Run `pytest -q`.
4. Run `git diff --name-status HEAD -- tests/`; deleted tests fail the gate.
5. Report static results, test counts, test/dependency changes, warnings, and final PASS/FAIL.

Never modify files, install dependencies, contact network/SSH, start/restart services, mutate runtime data, or perform Git writes. Restart remains a manual user action.
