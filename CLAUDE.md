# Dispatch Center — Claude Development Guide

## Product mission

Build a project-centered AI workload control plane that can:

1. discover and register projects, datasets, and worker servers;
2. decide which eligible server should run each job;
3. prepare required code and data, then dispatch work safely;
4. monitor execution, recover state after failures, and collect outputs;
5. let GPT/LLM users inspect the system and propose actions in natural language;
6. analyze logs, metrics, artifacts, and experiment history using grounded evidence.

The product is not merely a remote command launcher. The primary object is a
project and its lifecycle: prepare → schedule → run → observe → collect → analyze
→ improve or rerun.

The accepted direction is for this control plane to grow into an AI/ML
development platform, where a project's *code* lifecycle (import → isolate →
edit → test → review → promote) is a first-class part of the system rather than
something the user does over SSH beforehand. That direction is recorded in
`docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md`. A plan is not
an implementation and never authorizes an invariant change.

## Product principles

Priority order:

1. Safety and authorization
2. Scheduling and state correctness
3. Recovery and observability
4. Useful automation
5. User experience and convenience

Prefer deterministic code for validation, authorization, scheduling, state
transitions, and execution. Use an LLM for language understanding, explanation,
diagnosis, summarization, and proposing plans—not as the source of runtime truth.

## GPT/LLM control-plane contract

GPT may:

- query real server, project, dataset, job, approval, log, and result data;
- translate natural language into structured, validated requests;
- create pending approval requests for state-changing actions;
- explain scheduling decisions and blocked jobs;
- analyze results and suggest a next experiment or patch.

GPT must never:

- approve or reject its own request;
- execute arbitrary shell, SSH, deployment, or database operations directly;
- bypass request validation, approval, audit, or scheduler policy;
- present guessed server state, metrics, artifacts, or experiment conclusions as facts;
- automatically act on an analysis recommendation without a new approved action.

LLM integrations are optional adapters. When GPT, Anthropic, vLLM, or MCP is
unavailable, monitoring, approvals, scheduling, execution, reconciliation, and
result collection must continue to work.

## Development Plane and Compute Plane

The system has two planes with different failure modes. Know which one you are
in before you change anything.

**Development Plane** — "what code should exist?"
Project Onboarding (discovery, scan, import, registration) · Development Agents
(Codex, Claude Code, future providers) · isolated workspace/worktree · code
edit, test and review · ProjectVersion.
Its output is a reviewable, promotable revision. It never decides what runs.

**Compute Plane** — "what should run, where, with what data?"
Dataset · ExecutionPlan · Approval · Scheduler · SSH / Node backend · Run ·
Results / Artifacts. Its output is an executed, observable, collectable Run.

The planes meet at exactly one point: a **promoted ProjectVersion**. Development
Plane output enters the Compute Plane only through promotion — never by mutating
a live project instance, never from a dirty worktree, and never because an agent
reported success. Promotion is human-approved by design and is never automated.

**A Development Agent is a Development Plane collaborator, not an unrestricted
SSH or root agent.** This holds identically for every provider — Codex (the
current implemented provider), Claude Code, and any future coding agent. The
user may pick a provider manually, or an Auto mode may select one from
configured availability, capability, project requirement, and policy — but
selection must be deterministic and recorded, and **agent selection is never a
privilege escalation**. Any Development Agent may read and edit files inside a
dispatch-created isolated workspace, run controlled project-local validation
through the existing approved execution path, produce a diff, and create a
pending approval. It must never bypass authorization, approval, ExecutionPlan,
Dataset permission, promotion rules, or the SSH boundary; must never approve
or reject any request, including its own; must never hold a credential or a
direct execution handle; and must never be given a general-purpose shell. The
only valid direction of capability is
`Development Agent → dispatch tool → policy → approval → execution layer → server`.

Development Plane convenience never extends Compute Plane authority: being able
to run a test inside a workspace is not permission to run commands on a worker.

`.claude/skills/dispatcher-domain/references/development-platform.md` holds the
full plane model, the Development Agent / AgentProvider model and boundary, and
the implemented-vs-planned map. Do not assume a platform feature exists because
the product plan describes it.

## Scheduling model

Scheduling must remain explainable and deterministic. Every dispatch decision
should be derivable from persisted job data, current worker observations, project
requirements, dataset availability, and an explicit policy.

For each job, preserve this conceptual flow:

1. Validate the request and create an approval when state will change.
2. Persist the approved job and its dependencies.
3. Filter eligible workers by enabled/online state, pinning, tags, project type,
   dataset availability, and supported resource requirements.
4. Select deterministically by the documented priority/FIFO policy.
5. Persist `running` before creating the remote side effect.
6. Dispatch through the controlled SSH/local execution layer.
7. Reconcile from sentinel state after restarts or interrupted connections.
8. Collect results and record lifecycle audit events.

Current resource semantics are one ordinary job per worker machine. GPU slot
allocation, preemption, migration, multi-tenancy, and quota scheduling are not
implemented merely because a field such as `gpus_needed` exists. Adding any of
these changes the scheduling architecture and requires explicit design approval.

## Result and experiment intelligence

Result analysis must be grounded in retrieved evidence. Prefer structured
artifacts and recorded metadata over free-form log interpretation.

When developing analysis features:

- preserve raw logs and artifacts; derived summaries must not replace source data;
- identify every conclusion's job/run, artifact path, metric, or log evidence;
- distinguish observed facts, calculated values, and LLM inference;
- represent missing or unreadable outputs as unknown, not success or failure;
- support comparison across runs by project, dataset/version, command/config,
  worker, code revision, timestamps, status, and recorded metrics;
- make recommendations reviewable and require approval before rerun or mutation;
- keep result collection failure separate from the job's execution status.

## Canonical project context

- `.claude/skills/dispatcher-domain/references/invariants.md` is the canonical
  source for protected behavior. Changing an invariant requires explicit user
  approval.
- `.claude/skills/dispatcher-domain/references/architecture.md` contains stable
  architecture and trust boundaries.
- `.claude/skills/dispatcher-domain/references/development-platform.md` holds the
  Development/Compute Plane model and the implemented-vs-planned map.
- `docs/DECISIONS.md` is the authoritative record of which options the user has
  actually ruled on; it ranks with the invariants as safety truth.
- `docs/CAPABILITY_LEDGER.md` is the current capability reference. `implemented`
  is not `default-enabled`, and neither is `deployed` or `production-ready`.
- `docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md` is future
  product direction only.
- `PLAN.md` records newer accepted product and architecture decisions; verify it
  against current code and tests before implementation.
- `README.md` and `使用說明書.md` describe user-visible operation and must be
  updated when behavior or setup changes.
- Current code and tests describe implemented behavior, but existing behavior is
  not permission to weaken a canonical invariant.

Truth order when documents disagree: canonical invariants and `docs/DECISIONS.md`
→ current code and tests → `docs/CAPABILITY_LEDGER.md` → the full development
platform plan → `PLAN.md` and historical roadmap text.

Load only the relevant project skill and reference sections.

## Skill routing table

Start at `dispatcher-domain` when the owner is unclear; otherwise use the most
specific skill whose trigger matches. Load more than one when a change genuinely
spans them — a development-agent change that adds an approval kind needs both
`development-agent-safety` and `approval-boundary`.

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
- LLM, MCP, and Development Agent tools (any provider) may query state or
  create pending approvals only; no agent ever decides its own request, and no
  agent ever receives an approve/reject/shell/exec/run_command tool.
- Development Plane output reaches execution only as a human-promoted
  ProjectVersion; promotion is never auto-approved, never publishes to GitHub,
  and never runs from a dirty worktree.
- The execution layer is never opened into a general-purpose remote shell for a
  Development Plane feature's convenience.
- User command text reaches workers through the existing SFTP script path, never
  shell interpolation.
- Persistent state lives in SQLite or worker sentinel files; AppState dictionaries
  are disposable caches.
- Database state changes precede remote side effects, and every crash window must
  converge through reconciliation.
- Unreachable workers do not imply failed jobs.
- Audit lifecycle actions without allowing audit failure to corrupt job state.
- Keep services private by default. Never expose credentials, and disclose server
  topology only through authenticated, explicitly intended interfaces.
- Development and tests must not contact real workers, use real credentials, mutate
  runtime `jobqueue.db`/`audit.jsonl`/`servers.yaml`, or start production services.

## Development workflow

Before changing code:

1. State the user outcome and the affected lifecycle stage.
2. Inspect the relevant source, tests, skill, and invariant sections.
3. Separate current behavior from desired behavior.
4. Define a bounded implementation slice and observable acceptance criteria.
5. Identify approval, SSH, state, migration, and compatibility risks.

During implementation:

- make the smallest coherent vertical slice;
- reuse existing interfaces and deterministic policy functions;
- keep remote-command builders pure and directly testable;
- add production code and its tests together;
- avoid speculative abstractions and unrelated refactors;
- never weaken a boundary test to accommodate new behavior;
- preserve unrelated user changes in the working tree.

Validate from narrowest to broadest: focused unit tests, related subsystem tests,
static invariant checks, then the wider suite when justified. Tests use FakeSSH,
temporary databases/files, injected clients, and other isolated interfaces.

## Definition of done

A feature is complete only when:

- the user-visible outcome works across the full relevant lifecycle;
- authorization and failure behavior are explicit;
- state remains recoverable after restart or interrupted I/O;
- scheduling decisions and analysis conclusions are explainable from evidence;
- focused tests cover success, rejection, unavailable dependencies, and stale state;
- affected documentation and examples match the implemented behavior;
- no invariant was silently changed.

## Main-session and agent routing

- Handle normal analysis, architecture, coding, debugging, testing, review, and
  documentation in the main session.
- The main session owns product decisions, architecture, scope, acceptance criteria,
  and risk acceptance.
- Delegate to `sonnet-coder` only after defining a bounded implementation task with
  explicit files/subsystem and expected tests. The user may request it directly.
- Use `dispatcher-system-auditor` only when the user explicitly requests a system,
  architecture, security, or production-readiness audit.
- Loading a skill does not authorize spawning an agent. Do not automatically chain
  agents; both configured agents run in the foreground.

## Cost and context discipline

- Read only the files and reference sections needed for the current task.
- Prefer targeted inspection and focused tests before repository-wide work.
- Do not run broad audits, repeated rereads, or final-review agents automatically.
- Ask before actions expected to consume substantial model quota or affect real
  infrastructure.
