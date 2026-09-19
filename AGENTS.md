# Dispatch Center — Coding Agent Rules

Dispatch Center is an **Agent-native Engineering Platform**.  
Current V0.1 target: `docs/product/V0_1_PRODUCT_ARCHITECTURE.md`  
Current work plan: `docs/product/V0_1_IMPLEMENTATION_PLAN.md`

## Truth order

When sources disagree:

1. `docs/PLATFORM_CHARTER.md` invariants + `docs/DECISIONS.md`
2. current code + tests
3. `docs/CAPABILITY_LEDGER.md`
4. `docs/product/V0_1_PRODUCT_ARCHITECTURE.md`
5. `docs/product/ROADMAP.md`
6. historical/archive documents

## Before coding

Read `docs/README.md`, the current work packet in
`V0_1_IMPLEMENTATION_PLAN.md`, relevant source/tests, and only the Charter /
Decision / Ledger sections needed for that task.

Do not broadly load archive/history unless provenance is required.

## Agent roles

A **coding agent** may use the local development shell, edit repository files,
run tests, lint, typecheck, builds, and inspect git state.

The product's **Development Agent** is different. Its runtime boundaries remain
governed by the platform and must not be weakened.

Coding agents must never contact production workers, use real credentials, or
mutate runtime `jobqueue.db`, `audit.jsonl`, or production `servers.yaml`.

## Protected boundaries

Stop the affected work and request a named decision before changing an
`INV-*` or introducing a new capability class, approval kind, lifecycle state,
provider, validation mechanism, authorization semantic, or high-risk action.

Roadmaps, TODOs, issues, and product plans are not approval.

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
- Preserve existing API, persistence, authorization, audit, and failure semantics.
- Do not create parallel models when an existing v2 model can be reused.
- Avoid substantial new feature logic in `app/main.py`, `app/db.py`, and
  `app/approvals.py` when a current `dispatch_center/*` seam exists.
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

Mark `DONE` only when implementation, required validation, acceptance criteria,
documentation, and plan evidence are complete.

Do not silently weaken acceptance criteria. Record newly discovered work as a
new packet instead of expanding scope without review.

## Validation

Run affected checks first. After failure, rerun the failing check first. Expand
only when shared/protected behavior changed. Run the repository-required full
gate before commit.

Never weaken a boundary test to make a change pass.

## Completion report

Report:

- Status / work packet
- Modified files
- Behavior changed
- Validation
- Acceptance criteria
- Plan update
- Remaining risks
- Next recommended packet

Claude-specific routing lives in `CLAUDE.md` and `.claude/skills/`.
