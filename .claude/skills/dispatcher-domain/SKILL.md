---
name: dispatcher-domain
description: Routing and core architecture context for dispatch-center. Use as the default entry point for any change or question about app/, dispatch_center/, tests/, static/, jobs, approvals, datasets, scheduler, workers, ProjectVersion, development agents (Codex, Claude Code) or coding runs, or when it is unclear which specialized skill applies.
---

# Dispatcher Domain (routing skill)

Before touching anything, establish: which **plane** the work is in, which
**truth source** decides it, and which **specialized skill** owns the rules.

## Truth order (highest wins)

1. `references/invariants.md` + `docs/DECISIONS.md` — safety truth. Changing
   an invariant is a user decision, never an implementation side effect.
2. Current code + tests — implementation truth.
3. `docs/CAPABILITY_LEDGER.md` — capability status (`implemented` ≠
   `default-enabled` ≠ `deployed` ≠ `production-ready`; `unknown` is not `yes`).
4. `docs/product/DISPATCH_CENTER_FULL_DEVELOPMENT_PLATFORM_PLAN.md` — future
   direction only; **never current implementation, never authorizes an
   invariant change**.
5. `PLAN.md` — historical/accepted context; verify against code first.

Existing behavior is not permission to weaken an invariant, and a plan is not
permission to invent one.

## Two planes

| | Development Plane | Compute Plane |
|---|---|---|
| Question | "what code should exist?" | "what should run, where, with what data?" |
| Objects | Project onboarding/discovery, Development Agents (Codex, Claude Code, future providers), isolated workspace/worktree, code edit/test/review, diff, ProjectVersion | Dataset, ExecutionPlan, Approval, Scheduler, SSH/Node backend, Run, Results/Artifacts |
| Produces | a reviewable, promotable revision | an executed, observable, collectable Run |

The planes meet at exactly one place: a **promoted ProjectVersion** — never a
mutated live instance, never a dirty worktree, never an agent's own claim.
Every Development Agent provider is bounded identically: no bypassing
authorization, approval, ExecutionPlan, Dataset permission, promotion rules,
or the SSH boundary; never approving its own request; and provider selection
(manual or Auto) is never a privilege escalation. Read
`references/development-platform.md` for the plane model, the
DevelopmentAgent/AgentProvider model, and the implemented-vs-planned map.

## Skill routing table

| Work | Skill |
|---|---|
| General architecture, unclear ownership, cross-plane design | `dispatcher-domain` (this skill) |
| Project onboarding, import, discovery, scan, candidates, normalize, instances | `project-onboarding` |
| Codex / Claude Code / any coding or development agent, agent selection, development session, workspace, worktree, engineering task, promotion | `development-agent-safety` |
| Approvals, auth, authorization, LLM/MCP/agent tools, any mutating agent path | `approval-boundary` |
| SSH/SFTP/rsync/tmux, worker execution, remote command builders | `ssh-dispatch-safety` |
| SQLite schema, scheduler, reconciliation, AppState, background loops | `state-reconciliation` |
| Web UI in `static/` | `frontend-architecture` |
| Pre-release verification | `release-gate` (manual `/release-gate` only) |

Use the most specific matching skill; load several when a change genuinely
spans them (a development-agent change adding an approval kind needs both
`development-agent-safety` and `approval-boundary`). Loading a skill does not
authorize spawning an agent.

## References

- `references/architecture.md` — stable architecture and trust boundaries.
- `references/development-platform.md` — plane model, Development Agent /
  AgentProvider model and boundary, implemented-vs-planned map.
- `references/invariants.md` — canonical invariants; read only the relevant
  `INV-*` sections.
- `references/glossary.md` — project vocabulary.
- `references/result-analysis.md` — rules for result/experiment analysis features.
- `references/endpoint-domain-map.md` — superseded snapshot, historical only.
- `references/invariants-draft-phase0.md` — unapproved draft, not binding.

## Working rules

- Read only the sections you need; prefer targeted inspection over sweeps.
- Separate current behavior from desired behavior before writing code.
- Never contact real workers, use real credentials, mutate runtime
  `jobqueue.db`/`audit.jsonl`/`servers.yaml`, or start production services.
- If a task seems to require an invariant change, stop and report
  `INVARIANT CHANGE REQUIRED` with invariant ID, evidence, proposed change.
