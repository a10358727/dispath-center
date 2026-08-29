# Contributing to Dispatch Center

Dispatch Center is a safety-sensitive control plane. Changes must preserve the
approval, execution, recovery, and evidence boundaries documented in
`CLAUDE.md`, `PLAN.md`, and
`docs/PLATFORM_CHARTER.md`.

## Development setup

Use Python 3.10 or newer and keep the environment local to the checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
```

Do not point development or tests at production databases, credentials,
workers, service units, or runtime paths. The test suite is designed to use
temporary SQLite databases and fake external interfaces.

## Before changing code

1. Read `CLAUDE.md`, `PLAN.md`, the applicable decision records under `docs/`,
   and the canonical invariants.
2. State the user-visible outcome and the lifecycle stage being changed.
3. Identify authorization, approval, migration, remote-side-effect, rollback,
   and compatibility risks.
4. Keep the SSH backend intact until a tested replacement and rollback path
   exist.

Changing a canonical invariant requires explicit user approval. Unknown or
unreachable remote state must remain unknown; it is not automatically failure.

## Quality checks

Run the narrowest relevant tests first, then the repository gates:

```bash
.venv/bin/python -m ruff check app agent dispatch_center scripts tests
.venv/bin/python -m mypy app agent dispatch_center scripts
.venv/bin/python scripts/coverage_gate.py
.venv/bin/python -m pytest -q
bash .claude/skills/release-gate/scripts/static_checks.sh
git diff --check
```

Tests must not use real network services or mutate repository runtime files.
Every behavior change needs tests for success, rejection, unavailable
dependencies, and stale or interrupted state where applicable.

## Pull requests

Keep pull requests cohesive and reviewable. Separate security, migration,
agent, frontend, and deployment changes when they can be reviewed
independently. Complete the pull request template, including the invariant,
migration, rollback, security, testing, and deployment-evidence sections.

Do not push directly to `main`. At least one reviewer should approve a change;
changes to security-sensitive paths additionally require the owners listed in
`.github/CODEOWNERS`.

## Releases

User-visible changes go in the `Unreleased` section of `CHANGELOG.md`. A
release must identify the control-plane, node-agent, protocol, API, and
database-schema compatibility it supports. Passing local tests is not evidence
that a feature is deployed, canary-proven, or production-ready.
