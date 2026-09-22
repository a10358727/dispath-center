# Dispatch Center V0.1 UX / Interaction Plan

> **Document type:** product UX / interaction specification
> **Status:** V0.1 implementation target
> **Last planned:** 2026-09-19
>
> This document complements `V0_1_PRODUCT_ARCHITECTURE.md` and is implemented
> through `V0_1_IMPLEMENTATION_PLAN.md`. It does not override Charter invariants,
> named Decisions, current code/tests, or the Capability Ledger.

---

# 1. UX Goal

Dispatch Center should feel like one engineering workspace, not a collection of
administration pages.

Normal user mental model:

```text
Overview
  ↓
Project
  ↓
Ask AI
  ↓
Review
  ↓
Run
  ↓
Result
  ↓
Continue
```

Compute is managed separately:

```text
Compute
  ↓
Add / inspect / disable server
  ↓
Readiness
  ↓
Available to Project Runs
```

Normal users should not need to understand ExecutionPlan, Attempt,
ServerConfigRevision, plan digest, approval IDs, or fencing state. Those remain
available in Advanced views.

# 2. UX Principles

## Project first

Chat, code changes, Run proposals, Run progress, results, and Continue should be
visible in Project context.

## One engineering decision, one primary CTA

```text
Changes        → Save Version
Ready to Run   → Run
Running        → Stop
Result         → Continue
Add Compute    → Add Compute
```

Review, Edit, Logs, Retry, and Advanced are secondary actions.

## Summary first, details second

Default views show what matters to the next decision. Internal IDs, revisions,
digests, raw probes, audit events, and complete logs live in Advanced views.

## UI projections do not redefine backend truth

Friendly states such as Ready, Busy, Needs Attention, Collecting Results, and
Analyzing are projections over existing durable state, not new canonical
lifecycle states.

## Never fake precision

If a value is not authoritative, label it.

- provider context telemetry → exact/provider-reported;
- local context estimate → `Estimated`;
- historical runtime estimate → `Estimated`;
- uncertain remote state → `Unknown`, never fake `Failed`.

# 3. Primary Information Architecture

V0.1 navigation:

```text
Overview
Projects
Compute
Activity

Settings
```

## Overview

Answers: **What needs my attention right now?**

Shows Project attention items, active Runs, Compute health, and recent activity.

## Projects

Answers: **What engineering work am I doing?**

Contains the AI Workspace and the complete Ask → Review → Run → Result →
Continue loop.

## Compute

Answers: **What machines can I use, and are they ready?**

Contains server cards, readiness, onboarding, editing, and disabling.

## Activity

Answers: **What happened recently?**

A human-readable timeline, not a raw audit dump.

# 4. Overview

Overview is the first-glance operational home, not a dense infrastructure
dashboard.

Recommended structure:

```text
Overview

Projects needing attention
────────────────────────────────
YOLO Training
AI has a suggestion
Last Run · mAP 0.791
[ Continue ]

Active Runs
────────────────────────────────
Edge Classifier
Epoch 31 / 50
RTX 4090
[ Open Run ]

Compute
────────────────────────────────
3 Ready · 1 Busy · 1 Needs Attention

Rental RTX 4090    Ready
Lab RTX 3090       Busy
FPGA Lab           Needs Attention

[ View Compute ]

Recent Activity
────────────────────────────────
18:42 Run completed · YOLO Training
18:11 Compute connected · Rental RTX 4090
17:58 Version saved · YOLO Training
```

Attention ordering:

1. blocked / needs attention;
2. waiting for user decision;
3. active Run;
4. AI suggestion / Continue;
5. healthy idle state.

Overview Compute cards show name, status, useful hardware capability, active Run
when busy, and last-seen information when unhealthy. Never show credential
values.

# 5. Project AI Workspace

The AI Workspace is the main product surface.

```text
┌────────────────────────────────────────────────────────────┐
│ YOLO Training                  Context 42% · 84k / 200k    │
├──────────────┬─────────────────────────────────────────────┤
│ Overview     │ AI Engineer                                 │
│ Runs         │                                             │
│ Artifacts    │ You                                         │
│ Settings     │ 幫我使用 dataset-v2 跑 baseline。          │
│              │                                             │
│              │ AI                                          │
│              │ 我檢查了目前 pipeline...                   │
│              │                                             │
│              │ [ structured engineering cards ]            │
├──────────────┴─────────────────────────────────────────────┤
│ Ask AI about this project…                          Send   │
└────────────────────────────────────────────────────────────┘
```

Conversation timeline may contain:

- user text;
- AI text;
- progress/tool state;
- Changes card;
- validation result;
- version-ready card;
- Ready-to-Run card;
- Run progress card;
- Result card;
- AI analysis;
- Continue action.

These are one engineering timeline, not unrelated workflows.

# 6. AI Composer

V0.1 composer supports:

- multiline text;
- Send;
- keyboard send shortcut;
- disabled/loading state while accepting a message;
- retry on send failure;
- visible session/agent unavailable state.

Recommended placeholder:

`Ask AI about this project…`

Input states:

- ready;
- sending;
- waiting for agent;
- streaming;
- permission/action required;
- failed to send;
- session unavailable.

The input should remain visible when an engineering action is pending.

Optional object references such as `@run-182` or `@dataset-v2` may be added only
if existing Agent context architecture supports them cleanly; they must not block
V0.1.

# 7. Context Usage

The user should see how much AI context is currently used.

Default exact display:

```text
Context 42%
84k / 200k
```

Estimated display:

```text
Estimated context 42%
~84k / 200k
```

## Source priority

1. provider / Agent SDK context-window usage telemetry;
2. provider-reported token accounting that can safely derive current usage;
3. repository-owned deterministic estimate;
4. unavailable.

Never present an estimate as provider truth. If the active model's maximum
context window is unknown, do not invent one.

Default guidance:

```text
< 60%       Normal
60–80%      Large
> 80%       Near limit
Unknown     Usage unavailable
```

Thresholds may be adjusted after WP0 inspects real runtime behavior. Status must
not rely on color alone.

## Context drawer

Clicking Context opens:

```text
AI Context

Usage
84k / 200k · 42%

Source
Provider reported

Included
✓ Project / repository context
✓ Recent conversation
✓ Current ProjectVersion
✓ Run #182 evidence

Optional
○ Additional Run history
○ Compute readiness
```

Only show category token counts if the runtime actually exposes them.

Near the context limit, show a warning. Explicit compaction/summarization should
only be added if WP0 confirms the current Agent runtime contract supports it;
do not create a second compaction system by assumption.

# 8. Structured Engineering Cards

## Changes

```text
Changes
────────────────────────
3 files changed
+84 −21

✓ Validation passed

[ Review ]          [ Save Version ]
```

Summary first; detailed diff is secondary.

## Ready to Run

```text
Ready to Run
────────────────────────
Train baseline model

Version
a81fc92

Compute
Rental RTX 4090 · Ready

Parameters
Epochs      50
Batch       32
Dataset     dataset-v2

[ Edit ]                  [ Run ]
```

Only show runtime/cost estimates when grounded.

## Running

```text
Training
────────────────────────
Epoch 34 / 50

Current mAP   0.774
Best mAP      0.781

Compute
RTX 4090 · GPU 92%

Elapsed
01:38:21

[ Logs ]                 [ Stop ]
```

## Result

```text
Run Complete
────────────────────────
Goal
mAP > 0.80

Result
0.791

Target not reached

Artifacts
best.pt
metrics.json

AI Analysis
Validation loss is still decreasing…

[ View Results ]      [ Continue ]
```

Result UI must distinguish execution completion from result-collection
availability.

# 9. Compute

Compute is the user-facing management surface for SSH execution targets. Backend
may continue to call them Servers.

Ready card:

```text
Rental RTX 4090            Ready

RTX 4090 · 24 GB
GPU 2%
Disk 312 GB free
SSH :31827

[ Open ]
```

Busy card:

```text
Lab RTX 3090               Busy

YOLO Training · Run #182
Epoch 34 / 50
GPU 92%

[ View Run ]
```

Needs-attention card:

```text
Rental RTX 4090            Needs Attention

Last check failed
Connection unavailable

[ Check Connection ]
```

## Compute status projections

Recommended:

- Ready
- Busy
- Preparing
- Needs Attention
- Offline
- Unknown
- Disabled

Semantics:

**Ready** — eligible for normal scheduling under current readiness rules.

**Busy** — active Run/reservation according to the existing scheduler model.

**Preparing** — authoritative transient preparation is in progress.

**Needs Attention** — known actionable configuration/readiness problem.

**Offline** — connectivity known unavailable according to current observation
semantics.

**Unknown** — platform cannot safely determine current state.

**Disabled** — administratively disabled and unavailable to normal Runs.

# 10. Add Compute Wizard

Primary action:

`+ Add Compute`

## Step 1 — Type

```text
Rental GPU
My Server
FPGA Host
```

These are UX classifications. They reuse the existing Server domain unless an
approved architecture decision says otherwise.

## Step 2 — Connection

Required V0.1 fields:

```text
Name
Host
Port
User
Credential reference
Supported tags/classification
```

Custom SSH port is first-class. Credential values are never displayed back.

## Step 3 — Test / Preflight

```text
Connection
✓ SSH

Runtime
✓ tmux
✓ git
✓ rsync

GPU
✓ RTX 4090
✓ 24 GB

Storage
✓ 312 GB available

Ready
```

Only show checks that the backend actually performed.

## Failure UX

Avoid generic `Connection failed`.

Prefer:

```text
Cannot connect to gpu.example.com:31827.

Check:
- host
- SSH port
- server state
- network/firewall

[ Edit Connection ] [ Retry ]
```

Dependency failure:

```text
SSH works, but rsync is unavailable.

Dispatch Center needs rsync for result transfer.

[ Retry Check ]
```

Only show installation commands if the target environment can be identified
safely and accurately.

Final action:

`[ Add Compute ]`

Do not create durable Compute merely because Test Connection was clicked unless
existing server-management semantics intentionally require that.

# 11. Compute Detail

Default sections:

- Identity;
- Status;
- Capabilities;
- Resource usage;
- Connection summary;
- Current Run;
- Recent Runs;
- Readiness issues.

Normal information:

- name;
- status;
- host;
- port;
- user;
- credential reference label;
- GPU/model/count/VRAM if known;
- disk;
- last check;
- current Run;
- recent Runs.

Advanced may show server config revision, backend, roots, raw readiness details,
fingerprint when implemented, and audit/config publication details.

Actions:

- Check Connection / Refresh;
- Edit;
- Disable / Enable;
- View current Run.

Rental lifecycle should normally disable rather than erase an identity that is
referenced by durable history.

# 12. Activity

Activity is a product timeline.

```text
18:42 Run completed
      YOLO Training · mAP 0.791

18:11 Compute connected
      Rental RTX 4090

17:58 Version saved
      YOLO Training · 3 files changed

17:32 Run started
      YOLO Training · Rental RTX 4090
```

Entries link to Project, Run, or Compute. Raw audit payloads remain Advanced.

# 13. Empty / Loading / Error States

Every primary surface must intentionally handle non-happy paths.

## Overview

- no Projects;
- no Compute;
- no active work;
- loading;
- partial data unavailable.

Example:

```text
No Compute connected yet.
[ Add Compute ]
```

## AI Workspace

- no conversation;
- agent starting;
- agent unavailable;
- streaming;
- action waiting for user;
- context usage unavailable;
- message send failed.

## Compute

- no targets;
- loading;
- readiness unknown;
- offline;
- disabled;
- invalid configuration;
- partial probe failure.

## Run card

- preparing;
- queued;
- running;
- remote state unknown;
- execution complete;
- collecting results;
- results ready;
- collection failed;
- execution failed;
- stopped.

These are UI descriptions over existing canonical state.

# 14. Terminology

| Internal / backend | Normal UX |
|---|---|
| Server | Compute |
| ServerConfig / revision | Advanced Compute configuration |
| ProjectVersion promotion | Save Version |
| Approval | Confirm / Review when context is obvious |
| ExecutionPlan | Run details |
| Job / Attempt | Run / Advanced execution details |
| plan digest | Advanced details |
| Agent Session | AI Workspace / AI Engineer |
| Artifact | Artifact |
| Metrics | Metrics / Result |

Do not rename backend domain concepts solely for UI vocabulary.

# 15. Security / Privacy UX

Never display credential material.

Allowed:

```text
Credential
SSH Key · rental-gpu-key
```

Not allowed: private key bytes, secrets, provider credentials.

AI context views must not imply that credentials or unrestricted server data are
included.

Actions the Development Agent cannot execute directly must use the governed
proposal/confirmation path.

# 16. Accessibility / Interaction Baseline

V0.1 minimum:

- keyboard-operable primary actions;
- visible focus state;
- status represented by text/icon, not color alone;
- clear disabled/loading button state;
- clear destructive confirmation;
- error messages include recovery action;
- no critical information only available on hover.

Desktop engineering workflow is the V0.1 priority. Responsive layouts should not
break, but full mobile parity is not required unless existing Studio policy
already requires it.

# 17. Design-System Guidance

Reuse the current Studio component system and visual language. Do not introduce a
second UI framework solely for this plan.

Conceptual reusable components:

- StatusBadge
- EmptyState
- ActionCard
- AdvancedDetails / Drawer
- MetricSummary
- ComputeCard
- RunCard
- ContextUsage
- TimelineItem

WP0 must map these concepts to existing components before creating new ones.

# 18. Data Contract Expectations

React should display backend truth, not independently recreate domain rules.

WP0 must identify one authoritative source/projection for:

- Project attention state;
- active Run;
- Compute status/readiness;
- current target;
- Run progress;
- latest metrics;
- result collection;
- Agent session state;
- context usage;
- recent activity.

If a projection is missing, add the smallest application/API seam rather than
duplicating business logic in React.

# 19. Context Usage Data Contract

WP0 must inspect what the current Claude Agent SDK / runner exposes for:

- model identifier;
- context-window limit;
- input token usage;
- output usage;
- cache usage;
- compaction/summarization events;
- per-turn vs session-level usage.

Then choose:

**Mode A — Provider authoritative:** display exact provider-reported usage.

**Mode B — Platform estimated:** display an explicitly labeled estimate.

**Mode C — Unavailable:** display `Context usage unavailable`.

Context telemetry must not block AI usage.

# 20. Integration with Implementation Plan

UX is not a separate final-phase project. It ships with the related backend
packet.

## WP0

Also map current Studio navigation, Overview/dashboard equivalent, AI
composer/session layout, context/token telemetry, Servers page, create/edit
flow, readiness sources, Run projections, Activity sources, and reusable UI
components.

## WP1

Deliver Compute cards, status vocabulary, Overview Compute summary, and the
Normal/Advanced boundary.

## WP2

Deliver Add Compute Wizard together with backend create/edit/test seams.

## WP3

Deliver human-readable readiness/status and remediation messages with the
readiness backend.

## WP4

If approved, add fingerprint trust/mismatch UX to onboarding/detail.

## WP5 / WP6

Backend response contracts must expose all data required for the structured
Ready-to-Run card.

## WP7

Deliver Ready-to-Run card and primary Run CTA inside Project timeline.

## WP8

Deliver running/progress/unknown/result-collection states inside Project.

## WP9

Expose safe structured evidence usable by AI and result UI.

## WP10

Deliver Result card, grounded analysis, and Continue.

## WP11

Complete Overview / Projects / Compute / Activity / Settings navigation and
remove remaining normal-path dependence on internal terminology.

## WP12

Validate backend correctness and the complete human interaction flow together.

# 21. UX Acceptance Checklist

## Overview

- [x] Project attention items visible.
- [x] Active Runs visible.
- [x] Compute health visible at a glance.
- [x] Attention item links to the relevant action/detail.

## AI Workspace

- [x] User can type and send natural-language instructions.
- [x] Conversation and engineering actions share one coherent timeline.
- [x] Context usage is visible or explicitly unavailable.
- [x] Estimated context usage is clearly labeled.
- [x] User can inspect useful included-context information.
- [x] Changes / Run / Result cards each have one obvious primary action.

## Compute

- [x] User can add an SSH Compute target with custom port.
- [x] User can test/readiness-check it.
- [x] User can edit and disable it.
- [x] Status is human-readable and grounded in backend observations.
- [x] Credential values are never displayed.
- [x] Busy/offline/unknown/disabled are distinguishable.

## Run / Result

- [x] Proposed Run can start without manual reconstruction elsewhere.
- [x] Useful progress is visible in Project context.
- [x] Transport uncertainty is not false execution failure.
- [x] Result collection is distinguishable from execution completion.
- [x] Result explains what happened and the next action.
- [x] Continue requires human action.

## Complexity

- [x] Normal workflow does not require ExecutionPlan/Attempt/digest knowledge.
- [x] Advanced/debug details remain discoverable.
- [x] Existing approval/security boundaries are preserved.

# 22. UX Definition of Done per Work Packet

A user-visible packet is not `DONE` until:

1. happy path is implemented;
2. loading state is implemented;
3. empty state is implemented when applicable;
4. blocked/error/unknown states are implemented;
5. primary CTA is clear;
6. Advanced/internal data is not unnecessarily exposed;
7. targeted UI tests cover important state transitions;
8. React does not duplicate backend business truth inconsistently;
9. `V0_1_IMPLEMENTATION_PLAN.md` records UX validation evidence.

# 23. V0.1 UX Acceptance Scenario

```text
Open Overview
→ see Project + Compute health
→ add Rental GPU with custom SSH port
→ test it and see readiness
→ open Project
→ type an AI request
→ see Context usage
→ review AI changes
→ Save Version
→ see Ready-to-Run card
→ press Run
→ monitor progress in Project
→ see result + artifacts
→ read grounded AI analysis
→ press Continue
```

The user should never need internal ExecutionPlan/Attempt/revision knowledge to
complete this normal workflow.

# 24. Deferred UX

Not required for V0.1:

- full mobile parity;
- provider marketplace;
- automatic rental purchase;
- cost/billing dashboard;
- customizable dashboard widgets;
- multi-user presence/collaboration;
- multi-Agent visualization;
- rich command palette;
- arbitrary context-management UI;
- drag/drop workflow builder.
