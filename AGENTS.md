# Dispatch Center — Coding Agent Rules

Dispatch Center is an **Agent-native Engineering Platform**.

Current target: `docs/product/V0_1_PRODUCT_ARCHITECTURE.md`  
Current UX: `docs/product/V0_1_UX_PLAN.md`  
Current plan: `docs/product/V0_1_IMPLEMENTATION_PLAN.md`

## Truth order

1. `docs/PLATFORM_CHARTER.md` invariants + `docs/DECISIONS.md`
2. current code + tests
3. `docs/CAPABILITY_LEDGER.md`
4. `docs/product/V0_1_PRODUCT_ARCHITECTURE.md` + `docs/product/V0_1_UX_PLAN.md`
5. `docs/product/V0_1_IMPLEMENTATION_PLAN.md` for current sequencing/progress
6. `docs/product/ROADMAP.md`
7. archive / historical documents

## Before coding

Read `docs/README.md`, the active work packet in the implementation plan,
relevant source/tests, and only the Charter / Decision / Ledger sections needed
for that task. For user-visible work, also read `V0_1_UX_PLAN.md`.

Do not broadly load historical documents unless provenance is required.

## Codex skills

Repo-local Codex workflows live in `.agents/skills/` and require no separate
installation. Use the most specific matching skill; combine skills only when a
task truly crosses a mechanism/protected boundary. Skill loading never grants
additional authority.

## Codex subagent routing

Project-scoped Codex profiles live in `.codex/agents/`. The main Codex thread is
a GPT-6 Luna / medium orchestrator for routing, synthesis, and lightweight repo
inspection. Do not spend a stronger model on work that is already well-scoped.

- `sol_reasoner` — GPT-6 Sol / medium / read-only. Use for ambiguous requirements,
  architecture, hard debugging, cross-cutting reasoning, governance checks, and
  independent review of high-risk changes.
- `web_researcher` — GPT-6 Sol / medium / read-only. Use only when the answer depends
  on current external documentation, library/provider behavior, standards, releases,
  or other version-sensitive web evidence. It must prefer primary sources and report
  when live search is unavailable.
- `luna_coder` — GPT-6 Luna / high / workspace-write. Use for bounded implementation
  after required behavior and scope are settled.
- Generic spawned workers default to GPT-6 Luna / medium for focused lookup, inspection,
  summarization, and other low-risk support work.

Routing rules:

1. Clear, local, low-risk implementation → send directly to `luna_coder`; do not
   automatically call Sol first.
2. Ambiguous architecture, hard debugging, cross-domain behavior, or protected-boundary
   questions → `sol_reasoner` first, then one bounded packet to `luna_coder`.
3. Current external facts are required → `web_researcher`; combine with
   `sol_reasoner` only when independent reasoning is also materially useful.
4. Authorization, approval, audit, SSH, scheduler/reconcile, migration, lifecycle, and
   release/deploy changes → implementation by `luna_coder`, then independent
   read-only review by `sol_reasoner` before publication.
5. Read-only reasoning/research may run in parallel when independent. Code-writing is
   single-writer: do not run overlapping implementation agents.
6. If `luna_coder` finds ambiguity, missing external facts, scope expansion, or a
   protected decision boundary, stop coding and return to the parent / appropriate
   read-only agent.
7. Escalate the main session to Astra only for genuinely exceptional end-to-end work
   that Sol/Luna cannot resolve efficiently; Astra is not the routine parent model.

## Agent roles

A coding agent may use the local development shell, edit repository files, run
tests/lint/typecheck/builds, and inspect git state.

The product Development Agent is different; its runtime security boundaries
remain governed by the platform.

Never contact production workers, use real credentials, or mutate runtime
`jobqueue.db`, `audit.jsonl`, or production `servers.yaml`.

## Protected boundaries

Stop the affected work and request a named decision before changing an
`INV-*` or introducing a new capability class, approval kind, lifecycle state,
provider, validation mechanism, authorization semantic, or high-risk action.

Plans, issues, TODOs, and roadmaps are not approval.

## Engineering rules

- Project is the product center; Server is an execution resource.
- Development Plane and Compute Plane remain separate.
- Only promoted ProjectVersion enters governed Compute execution.
- AI proposes; platform governs and executes; humans decide.
- SSH remains the V0.1 compute backend.
- Unreachable does not mean failed.
- Result-collection failure does not rewrite execution truth.
- Reuse first. Bridge second. Refactor last.
- Prefer the smallest coherent, independently reviewable change.
- Preserve API, persistence, authorization, audit, and failure semantics.
- Do not create parallel models when an existing v2 model can be reused.
- Prefer current `dispatch_center/*` seams for new logic when practical.
- Avoid substantial new feature logic in `app/main.py`, `app/db.py`, and
  `app/approvals.py` unless integration requires it.
- Do not perform unrelated cleanup or broad rewrites.

## V0.1 scope guard

Unless explicitly authorized, do not add Kubernetes execution, Redis/NATS,
microservices, PostgreSQL migration, S3/MinIO, GPU slot scheduling, autonomous
paid-compute loops, production Node Agent rollout, or a Codex runtime provider.

A simpler UI is never permission to bypass backend approval, authorization,
audit, promotion, or execution governance.

## Plan maintenance

`V0_1_PRODUCT_ARCHITECTURE.md` defines the target.  
`V0_1_IMPLEMENTATION_PLAN.md` tracks execution.

A planned work packet is not complete until the implementation plan is updated
in the same change.

Use only: `PLANNED`, `READY`, `IN_PROGRESS`, `BLOCKED`, `DONE`,
`DEFERRED`, `SUPERSEDED`.

Mark `DONE` only after implementation, required validation, acceptance
criteria, documentation, and plan evidence are complete.

Do not weaken acceptance criteria to claim completion. Add newly discovered work
as a separate packet instead of silently expanding scope.

## Validation

Run affected checks first. After failure, rerun the failing check first. Expand
only when shared/protected behavior changed. Run the repository-required full
gate before commit.

Never weaken a boundary test to make a change pass.

## Completion report

Report: status/work packet, modified files, behavior changed, validation,
acceptance criteria, plan update, remaining risks, and next recommended packet.

Codex repo skills live in `.agents/skills/`. Claude-specific routing lives in `CLAUDE.md` and `.claude/skills/`.
