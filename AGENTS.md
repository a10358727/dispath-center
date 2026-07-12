# Codex repository instructions

This repository is being transitioned from Claude Code to Codex.

Before making architectural or behavioral changes, read:

- CLAUDE.md, if present
- PLAN.md
- documentation under docs/
- .claude/skills/dispatcher-domain/references/invariants.md, if present
- existing migrations and tests

Core rules:

- Preserve existing supported behavior.
- Do not rewrite the application from scratch.
- Work only in the current Git worktree.
- Do not connect to production servers.
- Do not access production credentials.
- Do not modify main directly.
- Keep the SSH execution backend until a tested replacement and rollback path exist.
- Unknown or unreachable remote state is not automatically failure.
- Do not fabricate missing revisions, dataset hashes, or historical data.
- Approved execution payloads must remain immutable.
- Prefer additive database migrations.
- Run relevant tests after changes.
- Stop and report when a protected invariant must change.
