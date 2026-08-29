# Dispatch Center — Claude Development Guide

## Product mission

Build a project-centered AI workload control plane: discover and register
projects, datasets, and worker servers; decide which eligible server runs each
job; prepare code and data, dispatch safely; monitor, recover, and collect
outputs; let LLM users inspect the system and propose actions in natural
language; analyze results from grounded evidence. The primary object is a
project and its lifecycle: prepare → schedule → run → observe → collect →
analyze → improve or rerun.

The accepted direction is to grow into an AI/ML development platform where a
project's *code* lifecycle (import → isolate → edit → test → review → promote)
is first-class. That direction is recorded in
`docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`. A plan is
not an implementation and never authorizes an invariant change.

Priority order: 1. safety and authorization; 2. scheduling and state
correctness; 3. recovery and observability; 4. useful automation; 5. UX.
Prefer deterministic code for validation, authorization, scheduling, state
transitions, and execution; use an LLM for language understanding,
explanation, diagnosis, and proposing plans — never as the source of runtime
truth. LLM integrations are optional adapters: when any of them is
unavailable, monitoring, approvals, scheduling, execution, reconciliation,
and result collection must continue to work.

## Truth order

When documents disagree: canonical invariants
(`.claude/skills/dispatcher-domain/references/invariants.md`) and
`docs/DECISIONS.md` → current code and tests → `docs/CAPABILITY_LEDGER.md`
(`implemented` ≠ `default-enabled` ≠ `deployed` ≠ `production-ready`) → the
full development platform plan (future direction only) → `PLAN.md` and
historical roadmap text.

Changing an invariant requires explicit user approval; existing behavior is
not permission to weaken one. Keep `README.md` in sync with user-visible
behavior. Load only the relevant skill and reference sections.

## Development Plane and Compute Plane

Know which plane you are in before you change anything.

**Development Plane** — "what code should exist?" Project Onboarding ·
Development Agents (Codex, Claude Code, future providers) · isolated
workspace/worktree · code edit, test and review · ProjectVersion. Output: a
reviewable, promotable revision. It never decides what runs.

**Compute Plane** — "what should run, where, with what data?" Dataset ·
ExecutionPlan · Approval · Scheduler · SSH / Node backend · Run ·
Results / Artifacts. Output: an executed, observable, collectable Run.

The planes meet at exactly one point: a **promoted ProjectVersion**.
Development Plane output enters the Compute Plane only through human-approved
promotion — never by mutating a live project instance, never from a dirty
worktree, never because an agent reported success.

**A Development Agent is a Development Plane collaborator, not an unrestricted
SSH or root agent** — identically for every provider, and agent/provider
selection (manual or Auto) is never a privilege escalation. An agent may edit
files in its dispatch-created isolated workspace, run controlled development
validation (test/lint/typecheck/build) through a dispatch-controlled
validation path, produce a diff, and create a pending approval — nothing
more. Development validation is not Compute execution: training/GPU/worker
workloads always re-enter the Compute Plane via ExecutionPlan and approval.
Being able to run a test inside a workspace is not permission to run commands
on a worker. Full model and boundary:
`.claude/skills/dispatcher-domain/references/development-platform.md` and the
`development-agent-safety` skill. Do not assume a platform feature exists
because the product plan describes it.

## Skill routing table

Start at `dispatcher-domain` when the owner is unclear; otherwise use the most
specific skill whose trigger matches. Load more than one when a change
genuinely spans them — a development-agent change that adds an approval kind
needs both `development-agent-safety` and `approval-boundary`.

| Work | Skill |
|---|---|
| General architecture, cross-plane design, unclear ownership | `dispatcher-domain` |
| Project onboarding, import, discovery, scan, candidates, instances | `project-onboarding` |
| Codex, Claude Code, any coding/development agent, agent selection, workspace, worktree, engineering task, code session, promotion | `development-agent-safety` |
| Approvals, auth/authorization, LLM/MCP/agent tools, any mutating agent path | `approval-boundary` |
| SSH/SFTP/rsync/tmux, worker execution, remote command builders | `ssh-dispatch-safety` |
| SQLite schema, scheduler, reconciliation, AppState, background loops | `state-reconciliation` |
| Web UI in `static/` | `frontend-architecture` |
| Release verification | `release-gate` (manual `/release-gate` only, read-only) |

Loading a skill does not authorize spawning an agent.

## Non-negotiable engineering boundaries

- All state-changing entry points use the approval workflow unless a canonical
  invariant explicitly documents an exception.
- LLM/MCP control-plane tools may query state and propose/request pending
  approvals only — they have no execution authority of any kind.
- A Development Agent (any provider) has the same absence of approval and
  Compute authority, but is additionally allowed — under the
  `development-agent-safety` boundary — to read/edit code inside its
  dispatch-created isolated workspace, run bounded development validation
  there, and produce a reviewable diff.
- No agent of either kind ever decides its own request, and no agent ever
  receives an approve/reject/shell/exec/run_command tool.
- Development Plane output reaches execution only as a human-promoted
  ProjectVersion; promotion is never auto-approved, never publishes to GitHub,
  and never runs from a dirty worktree.
- The execution layer is never opened into a general-purpose remote shell for
  a Development Plane feature's convenience.
- User command text reaches workers through the existing SFTP script path,
  never shell interpolation.
- Scheduling stays deterministic and explainable from persisted data and
  explicit policy; current semantics are one ordinary job per worker machine —
  GPU slots, preemption, migration, multi-tenancy, and quota scheduling do not
  exist merely because a field does, and adding any of them requires explicit
  design approval.
- Persistent state lives in SQLite or worker sentinel files; AppState
  dictionaries are disposable caches. Database state changes precede remote
  side effects, and every crash window must converge through reconciliation.
- Unreachable workers do not imply failed jobs.
- Audit lifecycle actions without letting audit failure corrupt job state.
- Result analysis is grounded in retrieved evidence; missing outputs are
  unknown, not success or failure — rules in
  `.claude/skills/dispatcher-domain/references/result-analysis.md`.
- Keep services private by default; never expose credentials; disclose server
  topology only through authenticated, explicitly intended interfaces.
- Development and tests must not contact real workers, use real credentials,
  mutate runtime `jobqueue.db`/`audit.jsonl`/`servers.yaml`, or start
  production services.

## Development workflow

Before changing code, first classify the task rather than automatically
spawning a planner. If the user-visible outcome, bounded scope, affected
subsystem, acceptance criteria, and protected behavior are already clear, a
routine implementation may go directly to `sonnet-coder`; a trivial local
change may remain in the main session. Use `fable-planner` only when material
requirement, architecture, invariant, migration/compatibility, approval/auth,
or recovery questions remain unresolved.

When planning is required: state the user outcome and affected lifecycle
stage; inspect only the relevant source, tests, skill, and exact invariant or
decision sections; separate current from desired behavior; define the smallest
coherent slice with observable acceptance criteria; and produce exact or
narrowly scoped validation targets for the coder.

During implementation: smallest coherent vertical slice; reuse existing
interfaces and deterministic policy functions; keep remote-command builders
pure and testable; ship production code with its tests; no speculative
abstractions or unrelated refactors; never weaken a boundary test to admit new
behavior; preserve unrelated user changes in the working tree.

## Validation policy

Development validation is progressive but **not cumulative by default**.
Avoid repeatedly running broader suites after every bounded code slice.

1. **Focused validation** — every coder task starts with the exact pytest
   nodes/files or focused checks named in its packet. Use fail-fast for the
   first attempt when useful; after a fix rerun the failing node first.
2. **Subsystem validation** — expand only when a shared interface,
   persistence/schema behavior, authorization/state-machine behavior, or
   another cross-file contract changed, or when the packet explicitly requires
   it.
3. **Invariant/static validation** — run only the focused checks relevant to
   affected protected boundaries during normal implementation.
4. **Full repository suite** — belongs to CI/release verification. A normal
   coder task must not run it unless its packet explicitly contains
   `FULL_SUITE_REQUIRED`.

Tests use FakeSSH, temporary databases/files, injected fakes, and the existing
network/isolation guards — never real infrastructure. Do not remove test
isolation merely to improve speed. Optimize which tests run and how often
before considering parallel execution.

Done means: the outcome works across the relevant lifecycle; authorization and
failure behavior are explicit; state recovers after restart or interrupted
I/O when relevant; decisions and conclusions are explainable from evidence;
the required focused validation passed; affected docs match user-visible
behavior; and no invariant silently changed. CI/release owns complete-suite
regression evidence.

## Session and model routing

Use the cheapest reasoning path that safely resolves the task. Model capability
never changes authority, approval, or safety boundaries.

- **Main session — classify first.** Do not automatically chain planner → coder
  → planner for every change. Trivial, explicit local changes may stay in the
  main session. Bounded implementation with settled requirements may go
  directly to `sonnet-coder`.
- **Fable / `fable-planner` — think and plan when needed.** Use Fable high
  effort for material requirement clarification, cross-subsystem architecture,
  invariant mapping, tradeoffs, migration/compatibility decisions, complex
  recovery/state behavior, and decomposition that cannot be safely inferred by
  the coder. If the main session itself is already Fable and has sufficient
  context, it owns this planning work directly instead of spawning a duplicate
  planner. Planner output is a compact implementation packet, not a broad
  repository report.
- **Sonnet / `sonnet-coder` — default bounded implementation.** Use Sonnet
  medium effort for routine and moderately complex production code, tests,
  targeted debugging, and local validation after scope and acceptance criteria
  are settled. Sonnet is the normal implementation path, not a lower-confidence
  fallback.
- **Opus / `opus-coder` — escalation only.** Use Opus high effort only when
  Fable explicitly recommends it because implementation itself remains
  unusually reasoning-heavy, or when Sonnet returns BLOCKED with concrete
  root-cause evidence. Typical cases are cross-subsystem concurrency/crash
  recovery, reconciliation/state-machine changes, or security-sensitive
  multi-layer changes. Do not use Opus merely because a task is large,
  important, or deserves the strongest model.
- **Review proportional to risk.** Routine coder completion does not require a
  second Fable planning pass. The main session should inspect the compact coder
  result/diff and only return to Fable for architecture/invariant-sensitive
  review, unresolved risk, or the next genuinely complex slice.
- At most one coder owns a bounded implementation task at a time. Do not run
  Sonnet and Opus competitively on the same worktree, and do not automatically
  chain implementation agents without a settled scope.
- `dispatcher-system-auditor` remains explicit-audit-only and is not part of
  the normal coding loop.
- Keep per-agent model selection in each `.claude/agents/*.md` frontmatter;
  avoid a global subagent-model override that would collapse Fable/Sonnet/Opus
  routing into one model.
- Prefer compact subagent completion reports. Successful subagents should
  return status, changed files/summary, validation evidence, and remaining
  risk; expand only for BLOCKED/FAILED cases.
- Read only what the task needs; prefer targeted inspection over broad audits
  or repeated rereads. Ask before actions that consume substantial quota or
  touch real infrastructure.
