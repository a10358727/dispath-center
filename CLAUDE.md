# Dispatch Center — Claude Guide

## Mission

Dispatch Center is an **Agent-native Engineering Platform**: a project-centered workspace where AI agents can inspect/edit code, validate changes, propose engineering work, execute through governed platform paths, analyze evidence, and iterate.

The **Project** is the primary object. AI agents reason, edit, propose, and analyze; the platform owns authorization, approval, durable state, resource/execution control, artifacts, evidence, recovery, and audit.

For the detailed Development/Compute plane model and capability-verification method, read `docs/PLATFORM_CHARTER.md` §4 and §8 only when the task needs them.

## Global rules

- Never use an LLM/agent as runtime truth, approval authority, or a direct execution channel.
- Development Agents work only in dispatch-created isolated workspaces and never gain unrestricted SSH, shell, credentials, or production-state access.
- Material mutations and execution follow the platform's approval/authorization paths; never create a shortcut for an agent or UI.
- Development validation is not Compute execution: training, worker execution, deployment, flashing, or hardware actions must use governed execution paths.
- Preserve unrelated user changes; never weaken an invariant or boundary test to make a change pass.
- Tests/dev work never contact real workers, use real credentials, mutate runtime `jobqueue.db`/`audit.jsonl`/`servers.yaml`, or start production services.

## Truth order

When sources disagree:

1. `docs/PLATFORM_CHARTER.md` (§6 invariants) + `docs/DECISIONS.md`
2. current code + tests
3. `docs/CAPABILITY_LEDGER.md`
4. product plans / roadmap documents

Future plans are direction, not implementation truth. Invariant changes require explicit user approval. Load only the references needed for the current task.

## Skill routing

Use the most specific matching skill; combine skills only when a change truly crosses boundaries.

| Work | Skill |
|---|---|
| Architecture / unclear ownership / cross-plane design | `dispatcher-domain` |
| Project discovery / import / instances | `project-onboarding` |
| Coding agents / workspaces / sessions / promotion | `development-agent-safety` |
| Approval / auth / mutating agent paths | `approval-boundary` |
| SSH / worker execution / remote commands | `ssh-dispatch-safety` |
| SQLite / scheduler / reconciliation / background loops | `state-reconciliation` |
| Studio SPA in `studio/`（+ `static/login.html`） | `frontend-architecture` |
| Pre-release verification | `release-gate` |

Loading a skill grants no additional authority.

## Development

- Prefer the smallest coherent, independently reviewable change.
- If scope, behavior, acceptance criteria, and protected boundaries are clear, implement directly; use planning only for unresolved architecture, requirements, invariants, compatibility, authorization, or recovery behavior.
- Read only relevant source/tests/skill refs; avoid broad repository sweeps, speculative abstractions, drive-by refactors, and repeated rereads.
- Preserve existing API, persistence, authorization, and failure semantics unless explicitly approved otherwise.

## Validation

Validation is targeted, not cumulative:

1. run affected tests/checks first;
2. after a failure, rerun the failing test first;
3. expand only when shared behavior or a protected boundary changed;
4. run the full suite (`make test`, parallel) before every commit — a commit
   is a pilot deploy candidate; within a packet, iterate on targeted tests only.

## Documentation checklist (DG-CONSOLIDATION-v1 C-6)

What a change must touch — nothing more:

| Change | DECISIONS.md | Charter | Ledger | Elsewhere |
|---|---|---|---|---|
| New capability class or `INV-*` change | full `DG-*` entry (user's words) | §6 if INV, §7 row | row | pin test |
| Packet inside an existing ruling | 10-line 補充紀錄 (template in DECISIONS.md) | none | one-line row edit | none |
| Bug fix / refactor / tests / CI / docs | none | none | none | commit message |
| Flag default change | 補充紀錄 citing C-2 | none | `Default` column | `SETTINGS.md`, `.env.example` |
| Migration | one 補充紀錄 line | none | none | `MIGRATIONS.md`, `EXPECTED_MIGRATIONS` |

## Agent routing

Use the cheapest safe path:

- main session → trivial/local work;
- `sonnet-coder` → default bounded implementation;
- `fable-planner` → unresolved architecture/reasoning;
- `opus-coder` → exceptional complexity or verified Sonnet BLOCKED;
- `dispatcher-system-auditor` → explicit audit only.

Do not automatically chain agents or run multiple coders competitively. Prefer compact subagent results: status, changes, validation, risk.
