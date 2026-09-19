# Dispatch Center V0.1 Implementation Plan

> **Purpose:** execution plan and progress ledger for
> `V0_1_PRODUCT_ARCHITECTURE.md`.  
> **Status:** active planning document  
> **Last planned:** 2026-09-19  
>
> This document tracks implementation. It does not authorize protected
> architecture changes. Charter + named Decisions remain authoritative.

---

# 1. Operating Rule

The plan is updated as part of the same change that completes a work packet.

Status vocabulary:

- `PLANNED` — defined but not yet ready to start.
- `READY` — prerequisites are satisfied; may be selected next.
- `IN_PROGRESS` — currently being implemented.
- `BLOCKED` — cannot continue without a dependency or decision.
- `DONE` — implementation, validation, acceptance, docs, and evidence complete.
- `DEFERRED` — intentionally outside current V0.1 execution.
- `SUPERSEDED` — replaced by another packet; history remains visible.

A packet may not be marked `DONE` merely because code exists.

---

# 2. V0.1 Outcome

The implementation must converge on this verified loop:

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
SSH custom-port rental GPU
  ↓
Logs / Metrics / Artifacts
  ↓
AI grounded analysis
  ↓
Human presses Continue
  ↓
Next controlled iteration
```

The architecture target is complete only when the end-to-end acceptance scenario
in `V0_1_PRODUCT_ARCHITECTURE.md` passes.

---

# 3. Program Board

| WP | Work packet | Status | Depends on | Primary result |
|---|---|---|---|---|
| WP0 | Repository architecture mapping | READY | — | verified implementation map + gap/decision audit |
| WP1 | Compute information architecture | PLANNED | WP0 | user-facing Compute surface and terminology |
| WP2 | Guided SSH compute onboarding | PLANNED | WP0, WP1 | add custom-port rental GPU through Studio |
| WP3 | Compute readiness projection | PLANNED | WP2 | clear Ready / Not Ready preflight |
| WP4 | SSH host identity decision/implementation | PLANNED | WP0 | fingerprint behavior, only after ruling if required |
| WP5 | Typed Agent → Run application seam | PLANNED | WP0 | agent can propose existing governed Run |
| WP6 | Development Agent tool integration | PLANNED | WP5 | typed Run tool exposed without execution authority |
| WP7 | Session inline Ready-to-Run card | PLANNED | WP5, WP6 | run proposal appears inside engineering context |
| WP8 | Inline Run monitoring | PLANNED | WP7 | state, metrics, logs, stop in Project context |
| WP9 | Result evidence access for Agent | PLANNED | WP0 | bounded Run/metrics/artifact/log tools |
| WP10 | Result analysis + Continue workflow | PLANNED | WP8, WP9 | grounded analysis and human-controlled next round |
| WP11 | UX navigation consolidation | PLANNED | WP7–WP10 | Projects / Compute / Activity normal UX |
| WP12 | End-to-end V0.1 acceptance | PLANNED | WP2–WP11 | exact product loop demonstrated and documented |
| WP13 | Documentation / capability closeout | PLANNED | WP12 | canonical status/evidence updates required by rules |

Only WP0 is initially `READY`. WP0 must verify this sequencing against current
code before later packets are promoted to `READY`.

---

# 4. WP0 — Repository Architecture Mapping

**Status:** READY

## Goal

Map the V0.1 target to the repository's current implementation before changing
runtime code.

## Required inspection

Verify the current implementation for:

- Project workspace and Project APIs;
- Agent Session / runner gateway / messages / permission flow;
- checkpoint and promotion;
- ProjectVersion;
- Environment and Run Template revisions;
- RunComposer / preview / submit / confirmation;
- ExecutionPlan v2;
- approval / authorization;
- scheduler / monitor / reconcile;
- ExecutionBackend and SSH backend;
- ServerConfig / custom port / SSHPool;
- server readiness / target readiness / project instance update;
- result collection / rsync;
- metrics parser/store;
- artifacts;
- MCP / assistant / dispatch-agent tools;
- Studio project/session/run/server components;
- relevant tests and feature flags.

## Deliverables

Update this plan with:

1. exact module/API/test mapping;
2. `already exists / partial / missing / legacy-only`;
3. reusable seams;
4. protected decision boundaries;
5. revised dependencies/order for WP1–WP13;
6. recommended first implementation packet.

## Acceptance

- [ ] Mapping is based on code/tests, not documentation assumptions.
- [ ] Every V0.1 core flow has an identified current seam or documented gap.
- [ ] Decision-gate audit is complete.
- [ ] No runtime code changed.
- [ ] Next packet is explicitly promoted to `READY`.

## Evidence

Pending.

---

# 5. WP1 — Compute Information Architecture

**Status:** PLANNED

## Goal

Introduce a clear user-facing **Compute** concept without replacing the existing
Server domain model.

## Scope

- define normal vs Advanced Compute information;
- surface rental GPU / owned server / hardware host in a coherent list;
- preserve existing server identifiers and APIs unless WP0 proves a small adapter
  is needed;
- establish terminology reused by onboarding and Run target selection.

## Reuse first

Expected current seams:

- Servers page;
- ServerConfig;
- server status / readiness APIs;
- target candidate UI.

## Acceptance

- [ ] User-facing terminology is Compute-oriented.
- [ ] Existing server identity/history semantics are unchanged.
- [ ] Advanced details remain accessible.
- [ ] No Server → ComputeTarget domain migration is introduced.

## Evidence

Pending.

---

# 6. WP2 — Guided SSH Compute Onboarding

**Status:** PLANNED

## Goal

Allow a user to add a rented GPU or owned server through a guided Studio flow.

## Required fields

- unique name;
- host;
- custom SSH port;
- user;
- credential/key reference;
- tags / basic classification where already supported.

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

- credentials are never exposed to the Development Agent;
- reuse current server configuration publication/audit behavior;
- one rental instance receives one unique Server identity;
- releasing a rental initially means disabling the target, not deleting history.

## Acceptance

- [ ] Custom SSH port survives create/edit/test/use path.
- [ ] Existing server config governance remains intact.
- [ ] User can distinguish rental GPU from other targets without a new provider subsystem.
- [ ] Error messages identify connection/configuration problems clearly.

## Evidence

Pending.

---

# 7. WP3 — Compute Readiness Projection

**Status:** PLANNED

## Goal

Make “SSH works” insufficient; project a clear execution readiness result using
existing safe probes wherever possible.

## Desired checks

Base runtime, subject to current invariant limits:

- SSH connectivity;
- expected shell/runtime dependency;
- tmux;
- git where required by project deployment;
- rsync transfer capability;
- writable required path;
- disk availability.

GPU target:

- GPU presence;
- model/count;
- VRAM;
- utilization where existing monitor data supports it.

## Output

```text
READY
NOT READY
BLOCKED
UNKNOWN
```

with human-readable reasons.

These are product projections and must not silently introduce a canonical
lifecycle state.

## Decision audit

If a desired probe is a new validation mechanism or violates SSH dependency
invariants, split it into a decision-gated follow-up rather than expanding this
packet.

## Acceptance

- [ ] Normal Run target selection excludes clearly ineligible targets.
- [ ] Readiness reasons are visible.
- [ ] Existing monitoring and target-readiness logic is reused.
- [ ] No unrestricted arbitrary remote probing is introduced.

## Evidence

Pending.

---

# 8. WP4 — SSH Host Identity

**Status:** PLANNED

## Goal

Define safe host identity behavior for Internet-facing rental SSH endpoints.

## Desired product behavior

First connection:

```text
show fingerprint
→ human trusts
→ fingerprint pinned
```

Later mismatch:

```text
BLOCKED: SSH host identity changed
```

## Gate

WP0 must determine whether this is covered by an existing ruling. If it creates a
new validation/security mechanism, this packet becomes `BLOCKED` until the named
decision is approved.

## Acceptance

To be defined after the decision audit.

## Evidence

Pending.

---

# 9. WP5 — Typed Agent → Run Application Seam

**Status:** PLANNED

## Goal

Connect the Development Plane to the existing typed Run flow without creating a
parallel execution model.

## Agent intent

Conceptually:

```text
project
promoted project_version
run_template / revision selection
environment selection
parameters
dataset bindings where required
manual target or resource intent
```

## Platform responsibility

The control plane resolves:

- immutable revisions;
- target/server revision;
- datasets;
- canonical spec;
- digest;
- authorization;
- approval/confirmation;
- Job materialization.

## Constraints

Do not:

- directly create a Job from the Agent tool;
- SSH from the Agent;
- use raw adhoc command as the normal typed path;
- introduce AgentRun / AgentExecutionPlan parallel models unless proven necessary.

## Acceptance

- [ ] Promoted ProjectVersion is mandatory.
- [ ] Existing Run preview/resolution is reused.
- [ ] Existing ExecutionPlan/approval semantics are preserved.
- [ ] No Job exists before the existing governed confirmation/materialization point.
- [ ] Request is idempotent/retry-safe according to existing application semantics.

## Evidence

Pending.

---

# 10. WP6 — Development Agent Tool Integration

**Status:** PLANNED

## Goal

Expose WP5 through the existing dispatch-agent platform-tool boundary.

## Desired tool

Use repository naming conventions; likely conceptually:

```text
request_run
```

## Constraints

Tool may propose/request only.

It must not expose:

- approve/reject;
- SSH credentials;
- raw SSH;
- raw platform shell;
- direct Job creation.

## Acceptance

- [ ] Tool is available to the current Claude Agent SDK session path.
- [ ] Request produces the same governed object/path as normal Studio Run creation.
- [ ] Agent receives safe, useful response data for rendering/explanation.
- [ ] Approval/execution authority remains outside the Agent.

## Evidence

Pending.

---

# 11. WP7 — Session Inline Ready-to-Run Card

**Status:** PLANNED

## Goal

Present the typed Run proposal inside the Project/AI workflow.

## Card information

Normal view should prioritize:

- purpose/title;
- version;
- compute target;
- important parameters;
- estimated/runtime metadata only where grounded;
- readiness;
- primary `Run` action.

Advanced details may expose underlying identifiers.

## UX rule

One primary engineering decision should normally map to one primary CTA, while
the backend still performs all required approval/governance steps.

## Acceptance

- [ ] User does not need to manually reconstruct the Run in RunComposer.
- [ ] Card is backed by durable/governed server state, not only model text.
- [ ] Required approval cannot be bypassed.
- [ ] Existing Runs view still works as history/advanced surface.

## Evidence

Pending.

---

# 12. WP8 — Inline Run Monitoring

**Status:** PLANNED

## Goal

Keep the user in Project context after pressing Run.

## Normal projection

- queued/running/terminal;
- target;
- elapsed runtime;
- latest useful metrics where available;
- bounded log tail;
- stop action;
- result-collection progress as a projection.

## Constraints

Do not add canonical Job states merely to obtain friendlier UI labels.

Do not collapse:

- transport unknown;
- execution failure;
- collection failure.

## Acceptance

- [ ] Project context reflects the same durable Run truth as the Runs view.
- [ ] Disconnect does not become false failure.
- [ ] Stop uses the existing governed stop path.
- [ ] Raw logs/details remain available in Advanced view.

## Evidence

Pending.

---

# 13. WP9 — Result Evidence Access for Agent

**Status:** PLANNED

## Goal

Give the Development Agent bounded access to real Run evidence.

## Reuse audit

Before adding tools, inspect existing tool catalog for equivalents to:

- Run detail;
- metrics;
- artifacts;
- bounded logs;
- compare runs.

## Constraints

- no unbounded log injection;
- no credentials;
- no direct result-directory shell;
- missing evidence is reported as unknown, not invented.

## Acceptance

- [ ] Agent can retrieve the evidence needed for common result analysis.
- [ ] Responses are bounded and structured.
- [ ] Artifact metadata/provenance remains platform-owned.
- [ ] Existing evidence tools are reused rather than duplicated where possible.

## Evidence

Pending.

---

# 14. WP10 — Result Analysis and Continue

**Status:** PLANNED

## Goal

Complete the human-controlled engineering loop.

## Flow

```text
Run evidence available
→ Agent grounded analysis
→ recommendation
→ STOP
→ user presses Continue
→ next controlled Agent turn
```

The next turn should receive/reference the relevant Run identity and retrieve
evidence through governed tools.

## Constraints

No automatic repeated paid-compute loop.

## Acceptance

- [ ] Analysis can identify the Run evidence used.
- [ ] Continue is a human action.
- [ ] Next iteration stays in the same Project context.
- [ ] No automatic Run is started merely because the Agent recommends one.

## Evidence

Pending.

---

# 15. WP11 — UX Navigation Consolidation

**Status:** PLANNED

## Goal

Make the normal product mental model:

```text
Projects
Compute
Activity
Settings
```

and the Project loop:

```text
Ask AI
Review
Run
Result
Continue
```

## Scope

- simplify navigation/labels;
- keep advanced execution/governance surfaces accessible;
- move system-object terminology out of the normal path where safe;
- avoid a full Studio rewrite.

## Acceptance

- [ ] Core V0.1 loop can be completed primarily from Project context.
- [ ] Normal user need not understand ExecutionPlan/Attempt/digest IDs.
- [ ] Compute management is discoverable.
- [ ] Activity provides understandable operational history.
- [ ] Advanced/debugging capability is not lost.

## Evidence

Pending.

---

# 16. WP12 — End-to-End Acceptance

**Status:** PLANNED

## Goal

Demonstrate the exact V0.1 scenario against an approved test/pilot environment.

## Required scenario

1. add a custom-port rental GPU target;
2. readiness says Ready;
3. open a real Project;
4. ask Agent for a bounded code/config change;
5. validate;
6. review and promote;
7. Agent proposes typed Run;
8. human presses Run;
9. existing SSH/tmux path executes;
10. useful progress/logs are visible;
11. results are collected;
12. metrics are parsed;
13. Agent reads evidence;
14. Agent analyzes goal result;
15. human presses Continue;
16. next controlled engineering iteration starts.

## Acceptance

- [ ] All 16 steps pass.
- [ ] Required audit/approval records exist.
- [ ] No credential appears in prompt/transcript/audit/UI payload.
- [ ] Failure semantics remain correct.
- [ ] Evidence is recorded in the repository only where existing governance requires it.

## Evidence

Pending.

---

# 17. WP13 — Documentation and Capability Closeout

**Status:** PLANNED

## Goal

Make repository truth match the finished implementation.

## Required actions

Apply the repository documentation checklist, including only what the actual
changes require:

- Decision supplement/full ruling where applicable;
- Capability Ledger row updates;
- settings/migration/reference updates where applicable;
- runbook/evidence links where applicable;
- this plan's final status;
- architecture document only if the target itself materially changed.

## Acceptance

- [ ] Every completed packet is `DONE` with evidence.
- [ ] No stale `IN_PROGRESS`/false capability claim remains.
- [ ] Ledger matches code/tests.
- [ ] V0.1 end-to-end Definition of Done is rechecked independently of packet count.

## Evidence

Pending.

---

# 18. Deferred Backlog

These are intentionally outside the V0.1 execution plan unless a later decision
moves them in:

| Item | Status | Reason |
|---|---|---|
| Kubernetes execution backend | DEFERRED | SSH endpoint is sufficient |
| Provider API rental/provision/release | DEFERRED | manual rental first |
| Rental billing/cost accounting | DEFERRED | V0.2 target |
| GPU slot scheduler | DEFERRED | one target = one scheduling unit |
| S3/MinIO artifact store | DEFERRED | local/rsync sufficient for V0.1 |
| Production Node Agent rollout | DEFERRED | existing canary gate remains |
| Codex runtime provider | DEFERRED | Claude Agent SDK is V0.1 runtime |
| Multi-agent autonomous engineering | DEFERRED | user-confirmed loop first |
| PostgreSQL / HA | DEFERRED | single control plane / SQLite first |
| Microservice split | DEFERRED | no V0.1 need |
| FPGA full product loop | DEFERRED | post-V0.1 expansion |

---

# 19. Plan Update Template

When a packet changes status, edit its section and append a short record here.

```text
## YYYY-MM-DD — WPx <title>

Status: DONE | BLOCKED | ...

Implemented:
- ...

Validation:
- command: PASS/FAIL

Acceptance:
- ...

Plan changes:
- WPx → DONE
- WPy → READY
- added/changed dependency: ...

Remaining risk:
- ...
```

Do not erase earlier completion/blocker records merely to make the plan look
cleaner.

---

# 20. Change Log

No implementation packets completed yet. WP0 is the first executable packet.
