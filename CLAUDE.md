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
- Production/pilot deployment or restart is an explicit operational action, never a side effect of coding/testing work; use only the reviewed manual operational skill when the user explicitly requests deployment.

## Truth order

When sources disagree:

1. `docs/PLATFORM_CHARTER.md` (§6 invariants) + `docs/DECISIONS.md`
2. current code + tests
3. `docs/CAPABILITY_LEDGER.md`
4. current product targets: `docs/product/V0_1_PRODUCT_ARCHITECTURE.md` and, for user-visible work, `docs/product/V0_1_UX_PLAN.md`
5. current sequencing/progress: `docs/product/V0_1_IMPLEMENTATION_PLAN.md`
6. long-term direction: `docs/product/ROADMAP.md`
7. archive / historical documents

Product targets and plans never override governance or implementation truth. Invariant changes require explicit user approval. Load only the references needed for the current task.

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
| Work-packet branch/checkpoint/rebase/PR sequencing | `work-packet-delivery` |
| Pre-release verification | `release-gate` |
| Pilot deploy / rollback / service restart | `updating-pilot-site` (explicit manual invocation only) |

Loading a skill grants no additional authority.

### Skill composition / precedence

Choose the primary skill by **what domain operation is changing**. Add a second
skill only for a mechanism or protected boundary that the change actually
crosses.

- UI rendering/interaction → `frontend-architecture` is primary; add
  `development-agent-safety` only when Agent authority/session semantics change.
- Project discovery/import → `project-onboarding` is primary; add
  `ssh-dispatch-safety` only when the SSH/probe mechanism changes.
- Approval/auth semantics → `approval-boundary` owns the approval mechanism;
  domain skills state only their domain-specific consequences.
- Durable/recovery semantics → `state-reconciliation` owns generic state rules;
  `ssh-dispatch-safety` owns SSH-specific transport/launch/sentinel behavior.
- Work-packet Git sequencing → `work-packet-delivery`; domain skills still own the packet's implementation semantics.
- Deployment is never inferred from a code/UI task. `release-gate` verifies;
  `updating-pilot-site` deploys only after explicit invocation.

When two skills appear to disagree, Charter/Decisions and current code/tests win;
do not combine the least restrictive interpretation.

## Development

- Prefer the smallest coherent, independently reviewable change.
- If scope, behavior, acceptance criteria, and protected boundaries are clear, implement directly; use planning only for unresolved architecture, requirements, invariants, compatibility, authorization, or recovery behavior.
- Read only relevant source/tests/skill refs; avoid broad repository sweeps, speculative abstractions, drive-by refactors, and repeated rereads.
- Preserve existing API, persistence, authorization, and failure semantics unless explicitly approved otherwise.

## Work-packet delivery

For V0.1 packet execution, use `work-packet-delivery` in the main session. One
packet is one independently reviewable branch/PR by default. Start only from the
latest merged `main`, keep Implementation Plan status aligned with Git ancestry,
and never start the next packet on an unmerged sibling branch.

Implementation subagents do not own Git publication. They perform bounded code
changes and targeted validation; the main session owns packet checkpointing,
required full-gate validation, rebase/publish handoff, and plan-state updates.

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

Use the cheapest safe path. Project settings default the main session to **Sonnet /
medium** for routing, synthesis, and ordinary local work. Subagents override effort/model
only where the task justifies it.

- direct Read/Grep → one or two obvious local lookups;
- `Explore` → Haiku, read-only, for broader file/symbol discovery that would clutter the main context;
- `sonnet-coder` → Sonnet / high, default bounded implementation;
- `opus-reasoner` → Opus / medium, default hard reasoning and independent review;
- `opus-coder` → Opus / high, only for implementation that Opus reasoning explicitly escalates or a verified Sonnet attempt cannot complete;
- `fable-planner` → Fable / high, exceptional frontier planning only after an Opus reasoning gap or explicit user request;
- `dispatcher-system-auditor` → Opus / medium, explicit audit only.

Routing rules:

1. Clear, local, bounded implementation → `sonnet-coder` directly. Do not pay a
   planning-model hop by default.
2. Ambiguous architecture, hard debugging, cross-subsystem behavior, or protected
   boundary questions → `opus-reasoner` first, then one bounded coder packet.
3. Authorization, approval, audit, SSH, scheduler/reconcile, migration, lifecycle, or
   release/deploy changes → coder implementation followed by independent
   `opus-reasoner` review before publication.
4. Use `Explore` only when isolated exploration saves main-context space; for a simple
   lookup, work directly.
5. Do not run multiple coders competitively or against overlapping files. Independent
   read-only investigations may run in parallel when useful.
6. Escalate to Fable because of a demonstrated reasoning gap, not because a task is
   important or large.

Prefer compact subagent results: evidence, decision/changes, validation, and risk.
