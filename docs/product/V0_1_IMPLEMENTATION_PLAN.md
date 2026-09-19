# Dispatch Center V0.1 Implementation Plan

> **Purpose:** single execution plan and progress ledger for
> `V0_1_PRODUCT_ARCHITECTURE.md`.  
> **Status:** active  
> **Last planned:** 2026-09-19  
>
> This plan does not authorize protected architecture changes. Charter and named
> Decisions remain authoritative.

# 1. Plan Rules

Every planned work packet is updated in this file as part of the same change
that completes or blocks it.

Statuses:

- `PLANNED`
- `READY`
- `IN_PROGRESS`
- `BLOCKED`
- `DONE`
- `DEFERRED`
- `SUPERSEDED`

`DONE` requires implementation, required validation, acceptance criteria,
required documentation, and recorded evidence.

Do not weaken acceptance criteria to mark work complete. New work discovered
during implementation becomes a new packet unless it is required for correctness.

# 2. V0.1 Target Loop

```text
Project
  ↓
Ask AI
  ↓
AI edits + validates
  ↓
Review
  ↓
Promoted ProjectVersion
  ↓
AI proposes typed Run
  ↓
Human presses Run
  ↓
Existing governed ExecutionPlan / Scheduler
  ↓
SSH custom-port compute
  ↓
Logs / Metrics / Artifacts
  ↓
AI grounded analysis
  ↓
Human presses Continue
  ↓
Next controlled iteration
```

V0.1 is complete only when the end-to-end acceptance scenario passes.

# 3. Program Board

| WP | Work packet | Status | Depends on | Primary result |
|---|---|---|---|---|
| WP0 | Repository architecture mapping | READY | — | verified map + gap/decision audit |
| WP1 | Compute information architecture | PLANNED | WP0 | clear Compute surface/terminology |
| WP2 | Guided SSH compute onboarding | PLANNED | WP0, WP1 | add custom-port rental GPU |
| WP3 | Compute readiness projection | PLANNED | WP2 | Ready / Not Ready with reasons |
| WP4 | SSH host identity | PLANNED | WP0 | fingerprint contract if authorized |
| WP5 | Typed Agent → Run application seam | PLANNED | WP0 | agent can propose existing governed Run |
| WP6 | Development Agent tool integration | PLANNED | WP5 | typed Run tool without execution authority |
| WP7 | Session inline Ready-to-Run card | PLANNED | WP5, WP6 | Run action inside Project context |
| WP8 | Inline Run monitoring | PLANNED | WP7 | state/metrics/logs/stop in Project context |
| WP9 | Result evidence access for Agent | PLANNED | WP0 | bounded Run evidence tools |
| WP10 | Result analysis + Continue | PLANNED | WP8, WP9 | grounded analysis + human next round |
| WP11 | UX navigation consolidation | PLANNED | WP7–WP10 | Projects / Compute / Activity |
| WP12 | End-to-end acceptance | PLANNED | WP2–WP11 | full V0.1 scenario demonstrated |
| WP13 | Documentation / capability closeout | PLANNED | WP12 | repository truth matches implementation |

Only WP0 starts `READY`. WP0 must verify and may revise later dependencies
before promoting the next packet.

# 4. WP0 — Repository Architecture Mapping

**Status:** READY

## Goal

Map the V0.1 target to current code/tests before runtime changes.

## Inspect

- Project workspace/APIs
- Agent Session, runner gateway, messages, permission flow
- checkpoint / promotion / ProjectVersion
- Environment / Run Template revisions
- RunComposer / preview / submit / confirm
- ExecutionPlan v2
- approval / authorization
- scheduler / monitor / reconcile
- ExecutionBackend / SSH backend
- ServerConfig / custom port / SSHPool
- target readiness / project instance update
- result collection / rsync
- metrics / artifacts
- MCP / assistant / dispatch-agent tools
- Studio project/session/run/server components
- relevant tests / feature flags

## Deliverables

Record in this plan:

1. exact module/API/test mapping;
2. exists / partial / missing / legacy-only;
3. reusable seams;
4. decision boundaries;
5. revised WP ordering/dependencies;
6. first safe implementation packet.

## Acceptance

- [ ] Mapping uses current code/tests.
- [ ] Every V0.1 core flow has a seam or documented gap.
- [ ] Decision-gate audit is complete.
- [ ] No runtime code changed.
- [ ] Next packet is promoted to `READY`.

## Evidence

Pending.

# 5. WP1 — Compute Information Architecture

**Status:** PLANNED

## Goal

Introduce a clear user-facing **Compute** concept without replacing the existing
Server domain model.

## Scope

- normal vs Advanced Compute information;
- rental GPU / owned server / hardware host presentation;
- terminology reused by onboarding and Run target selection;
- existing server identities/history preserved.

## Expected reuse

- Servers page
- ServerConfig
- server status/readiness APIs
- target candidate UI

## Acceptance

- [ ] User-facing terminology is Compute-oriented.
- [ ] Server identity/history semantics are unchanged.
- [ ] Advanced details remain available.
- [ ] No Server → ComputeTarget domain migration.

## Evidence

Pending.

# 6. WP2 — Guided SSH Compute Onboarding

**Status:** PLANNED

## Goal

Add a rented GPU or owned server through a guided Studio flow.

## Required fields

- unique name
- host
- custom SSH port
- user
- credential/key reference
- supported tags/classification

## Flow

```text
Add Compute
→ connection details
→ Test Connection
→ readiness/preflight
→ review
→ Add
```

## Constraints

- credentials never reach the Development Agent;
- reuse current server config publication/audit behavior;
- each rental instance gets a unique Server identity;
- ending a rental disables the target rather than rewriting/deleting history.

## Acceptance

- [ ] Custom port survives create/edit/test/use.
- [ ] Existing server configuration governance remains intact.
- [ ] Rental GPU is understandable without a provider subsystem.
- [ ] Connection/config errors are actionable.

## Evidence

Pending.

# 7. WP3 — Compute Readiness Projection

**Status:** PLANNED

## Goal

Project clear execution readiness using existing safe probes where possible.

## Desired checks

Base:

- SSH connectivity
- backend-required shell/runtime dependency
- tmux
- git where deployment requires it
- rsync capability
- writable expected path
- disk availability

GPU:

- GPU presence
- model/count
- VRAM
- utilization where existing monitor data supports it

## Product states

`READY`, `NOT READY`, `BLOCKED`, `UNKNOWN` are UI/readiness projections,
not new canonical Job lifecycle states.

## Gate

Any desired probe that creates a new validation mechanism or conflicts with SSH
invariants must be split into a decision-gated packet.

## Acceptance

- [ ] Ineligible targets are not offered as normal Run targets.
- [ ] Readiness reasons are visible.
- [ ] Existing monitor/readiness logic is reused.
- [ ] No arbitrary remote probing is introduced.

## Evidence

Pending.

# 8. WP4 — SSH Host Identity

**Status:** PLANNED

## Goal

Define host identity behavior for Internet-facing rental SSH endpoints.

Desired flow:

```text
first connection → show fingerprint → human trust → pin
later mismatch → BLOCKED
```

## Decision gate

WP0 must determine whether an existing ruling covers this. If this is a new
validation/security mechanism, mark WP4 `BLOCKED` and draft the required named
decision before implementation.

## Acceptance

Defined after the decision audit.

## Evidence

Pending.

# 9. WP5 — Typed Agent → Run Application Seam

**Status:** PLANNED

## Goal

Connect the Development Plane to the existing typed Run path without creating a
parallel execution model.

## Agent intent

Conceptually:

- project
- promoted ProjectVersion
- Run Template selection
- Environment selection
- parameters
- dataset bindings where required
- manual target/resource intent

## Platform-owned resolution

- immutable revisions
- target/server revision
- datasets
- canonical execution spec
- digest
- authorization
- approval/confirmation
- Job materialization

## Constraints

Do not:

- directly create a Job from the Agent tool;
- SSH from the Agent;
- make raw adhoc command the normal typed path;
- introduce AgentRun/AgentExecutionPlan models without proof they are necessary.

## Acceptance

- [ ] Promoted ProjectVersion is mandatory.
- [ ] Existing preview/resolution is reused.
- [ ] Existing ExecutionPlan/approval semantics are preserved.
- [ ] No Job exists before the governed materialization point.
- [ ] Retry/idempotency follows existing application semantics.

## Evidence

Pending.

# 10. WP6 — Development Agent Tool Integration

**Status:** PLANNED

## Goal

Expose WP5 through the current dispatch-agent platform-tool boundary.

Likely concept: `request_run`; final name follows repository conventions.

## Constraints

The tool proposes/requests only. It exposes no approve/reject, credential, raw
SSH, raw platform shell, or direct Job creation.

## Acceptance

- [ ] Tool is available to the Claude Agent SDK session path.
- [ ] It produces the same governed path as normal Studio Run creation.
- [ ] Response contains safe data usable by the Agent/UI.
- [ ] Execution authority remains outside the Agent.

## Evidence

Pending.

# 11. WP7 — Session Inline Ready-to-Run Card

**Status:** PLANNED

## Goal

Present the typed Run proposal inside the Project/AI workflow.

## Normal card

- purpose/title
- promoted version
- Compute target
- important parameters
- grounded estimate metadata only if available
- readiness
- primary `Run` action

Advanced details may expose internal identifiers.

## Acceptance

- [ ] User does not rebuild the Run manually in RunComposer.
- [ ] Card is backed by governed platform state, not model text alone.
- [ ] Required approvals cannot be bypassed.
- [ ] Runs view remains available for history/advanced use.

## Evidence

Pending.

# 12. WP8 — Inline Run Monitoring

**Status:** PLANNED

## Goal

Keep the user in Project context after Run starts.

## Normal projection

- queued/running/terminal
- target
- elapsed runtime
- latest useful metrics
- bounded log tail
- stop action
- result-collection progress projection

## Constraints

Do not add canonical Job states only for friendlier labels. Do not collapse
transport unknown, execution failure, and collection failure.

## Acceptance

- [ ] Project context uses the same durable Run truth as Runs view.
- [ ] Disconnect does not become false failure.
- [ ] Stop uses the existing governed path.
- [ ] Raw details remain available in Advanced view.

## Evidence

Pending.

# 13. WP9 — Result Evidence Access for Agent

**Status:** PLANNED

## Goal

Provide bounded access to real Run evidence.

Before adding tools, inspect existing equivalents for:

- Run detail
- metrics
- artifacts
- bounded log tail
- Run comparison

## Constraints

- no unbounded logs
- no credentials
- no direct result-directory shell
- missing evidence remains unknown

## Acceptance

- [ ] Agent can retrieve evidence needed for common result analysis.
- [ ] Responses are structured and bounded.
- [ ] Artifact provenance remains platform-owned.
- [ ] Existing tools are reused where possible.

## Evidence

Pending.

# 14. WP10 — Result Analysis and Continue

**Status:** PLANNED

## Goal

Complete the human-controlled engineering loop.

```text
Run evidence
→ Agent grounded analysis
→ recommendation
→ STOP
→ user presses Continue
→ next controlled Agent turn
```

## Acceptance

- [ ] Analysis can identify the Run evidence used.
- [ ] Continue is a human action.
- [ ] Next iteration stays in the same Project context.
- [ ] Agent recommendation alone cannot start another Run.

## Evidence

Pending.

# 15. WP11 — UX Navigation Consolidation

**Status:** PLANNED

## Goal

Normal product mental model:

```text
Projects
Compute
Activity
Settings
```

Project loop:

```text
Ask AI
Review
Run
Result
Continue
```

## Scope

Simplify navigation and labels; keep advanced execution/governance surfaces
available; do not perform a full Studio rewrite.

## Acceptance

- [ ] Core V0.1 loop completes primarily from Project context.
- [ ] Normal users need not understand ExecutionPlan/Attempt/digest IDs.
- [ ] Compute management is discoverable.
- [ ] Activity provides understandable operational history.
- [ ] Advanced/debug capability is retained.

## Evidence

Pending.

# 16. WP12 — End-to-End Acceptance

**Status:** PLANNED

## Scenario

1. Add a custom-port rental GPU target.
2. Readiness reports Ready.
3. Open a real Project.
4. Ask Agent for a bounded code/config change.
5. Validate.
6. Review and promote.
7. Agent proposes typed Run.
8. Human presses Run.
9. Existing SSH/tmux path executes.
10. Useful progress/logs are visible.
11. Results are collected.
12. Metrics are parsed.
13. Agent reads evidence.
14. Agent analyzes whether the goal was met.
15. Human presses Continue.
16. Next controlled engineering iteration starts.

## Acceptance

- [ ] All 16 steps pass.
- [ ] Required audit/approval records exist.
- [ ] No credential leaks to prompt/transcript/audit/UI payload.
- [ ] Failure semantics remain correct.
- [ ] Required evidence is recorded according to existing governance.

## Evidence

Pending.

# 17. WP13 — Documentation and Capability Closeout

**Status:** PLANNED

## Goal

Make repository truth match the finished implementation.

Apply only documentation required by actual changes:

- Decision supplement/full ruling where needed;
- Capability Ledger rows;
- settings/migration/reference docs where applicable;
- runbook/evidence links where applicable;
- final packet statuses;
- architecture doc only if the target materially changed.

## Acceptance

- [ ] Every completed packet is `DONE` with evidence.
- [ ] No stale progress claim remains.
- [ ] Ledger matches code/tests.
- [ ] V0.1 Definition of Done is independently rechecked.

## Evidence

Pending.

# 18. Deferred Backlog

| Item | Status | Reason |
|---|---|---|
| Kubernetes execution backend | DEFERRED | SSH endpoint is sufficient |
| Provider API rental/provision/release | DEFERRED | manual rental first |
| Rental billing/cost accounting | DEFERRED | post-V0.1 |
| GPU slot scheduler | DEFERRED | one target = one scheduling unit |
| S3/MinIO artifact store | DEFERRED | rsync/local is sufficient |
| Production Node Agent rollout | DEFERRED | canary gate remains |
| Codex runtime provider | DEFERRED | Claude Agent SDK is V0.1 runtime |
| Multi-agent autonomous loop | DEFERRED | human-confirmed loop first |
| PostgreSQL / HA | DEFERRED | SQLite/single control plane first |
| Microservice split | DEFERRED | no V0.1 need |
| FPGA full product loop | DEFERRED | post-V0.1 expansion |

# 19. Plan Update Template

Append one short record when a packet changes materially:

```text
## YYYY-MM-DD — WPx <title>

Status: DONE | BLOCKED | ...

Implemented:
- ...

Validation:
- <command>: PASS/FAIL

Acceptance:
- ...

Plan changes:
- WPx → DONE
- WPy → READY
- dependency/scope adjustment: ...

Remaining risk:
- ...
```

Do not erase earlier completion/blocker records merely to make the plan cleaner.

# 20. Change Log

No implementation packet has been completed yet. WP0 is the first executable
packet.
