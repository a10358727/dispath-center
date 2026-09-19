# Dispatch Center V0.1 Product & System Architecture

> **Document type:** Product architecture / implementation target  
> **Status:** V0.1 target specification for planning and implementation  
> **Audience:** repository maintainers, Codex, Claude Code, reviewers  
> **Last updated:** 2026-09-19  
>
> This document is **not** a canonical decision record and does not override
> `docs/PLATFORM_CHARTER.md`, `docs/DECISIONS.md`, current code/tests, or
> `docs/CAPABILITY_LEDGER.md`. When this target requires a new capability class,
> approval kind, lifecycle state, provider, validation mechanism, or any `INV-*`
> change, implementation must stop at that boundary and go through the repository's
> existing decision-gate process first.

---

# 1. Executive Summary

Dispatch Center is an **AI-driven engineering development and remote-compute platform**.

The product goal is not to build another chat UI, Kubernetes dashboard, or generic
server manager. The goal is to let an engineer describe an engineering objective,
have an AI Development Agent modify and validate the project, then use Dispatch
Center to execute the approved project version on controlled remote compute,
collect evidence, and feed that evidence back into the next engineering iteration.

The V0.1 product loop is:

```text
Describe Goal
    ↓
AI understands project
    ↓
AI edits + validates code
    ↓
Human reviews
    ↓
Promoted ProjectVersion
    ↓
AI proposes a typed Run
    ↓
Human presses Run
    ↓
Dispatch Center executes on SSH compute
    ↓
Logs / Metrics / Artifacts return
    ↓
AI analyzes evidence
    ↓
Human presses Continue
    ↓
Next engineering iteration
```

The shortest product definition is:

> **AI 幫我改程式，我按一下讓租來的 GPU 執行，結果回來後 AI 再根據真實結果告訴我下一步。**

V0.1 succeeds when that loop works reliably without requiring the user to understand
the platform's internal objects such as ExecutionPlan, approval IDs, attempts,
digests, or server revisions.

---

# 2. Product Goal

## 2.1 Primary outcome

A user should be able to open one Project and complete the following workflow:

1. describe an engineering goal;
2. let the Development Agent inspect and modify the repository;
3. review the proposed changes;
4. save/promote an immutable project version;
5. receive an AI-generated Run proposal;
6. confirm the Run with one primary action;
7. execute it on a remote GPU over SSH, including a custom SSH port;
8. monitor useful progress and logs;
9. collect results back to Dispatch Center;
10. let the AI analyze the real Run evidence;
11. continue with the next iteration.

The user should spend most of the time in the **Project context**, not moving between
unrelated administrative pages.

## 2.2 V0.1 user model

V0.1 is intentionally optimized for:

- one operator;
- one Dispatch Center control plane;
- one primary Development Agent runtime;
- multiple SSH execution targets;
- manually confirmed engineering iterations;
- personally rented GPU instances and personally managed local servers.

The system may already contain multi-user / RBAC infrastructure. V0.1 must not
remove those protections, but the primary UX does not need to expose enterprise
complexity.

## 2.3 Example target use case

A user asks:

```text
把目前 YOLO 專案調整成使用 dataset-v2，
先訓練 baseline，目標 mAP > 0.80。
使用 rental-4090-001，50 epochs。
```

Expected product flow:

```text
AI examines repository
    ↓
AI modifies train/config code
    ↓
workspace validation
    ↓
Review Changes
    ↓
Save Version
    ↓
AI proposes training Run
    ↓
Run
    ↓
SSH : custom port
    ↓
tmux workload
    ↓
results/{job_id}
    ↓
rsync to Dispatch Center
    ↓
metrics parsing
    ↓
AI:
"mAP 0.791; target not reached; recommend ..."
    ↓
Continue
```

---

# 3. Non-Goals for V0.1

The following are explicitly **not required** for V0.1 unless they become necessary
to complete the end-to-end loop:

- Kubernetes API integration;
- KubernetesExecutionBackend;
- kubeconfig / ServiceAccount management;
- Argo Workflows;
- Kueue;
- NATS;
- Redis;
- microservice decomposition;
- PostgreSQL migration;
- S3 / MinIO artifact storage;
- multi-agent swarms;
- external A2A agent interoperability;
- production Node Agent rollout;
- automatic GPU provider procurement;
- automatic rental termination;
- multi-job GPU packing;
- GPU slot scheduler;
- distributed training scheduler;
- unlimited autonomous optimization;
- Codex as a runtime Development Agent provider;
- enterprise multi-user UX redesign.

The rented compute environment may internally be Kubernetes. That is an
implementation detail of the compute provider if Dispatch Center receives a
normal SSH endpoint.

---

# 4. Architectural Principles

## 4.1 Project is the primary product object

A Project represents the engineering work.

Conceptually it owns or references:

```text
Project
├── Repository
├── ProjectVersion
├── Development Agent Sessions
├── Goals / Engineering Context
├── Environment revisions
├── Run Template revisions
├── Dataset bindings
├── Experiments
├── Runs
├── Metrics
└── Artifacts
```

A Server is execution capacity, not project ownership.

**A Project does not belong to a Server.**

## 4.2 Development Plane and Compute Plane remain separate

The existing two-plane model is a core architectural asset and should be preserved.

### Development Plane

Answers:

> What code should exist?

```text
Project
  ↓
Agent Session
  ↓
isolated workspace / worktree
  ↓
inspect / edit / validate
  ↓
diff
  ↓
checkpoint
  ↓
promotion
  ↓
ProjectVersion
```

### Compute Plane

Answers:

> What approved version should run, where, with what inputs, and what happened?

```text
Promoted ProjectVersion
  ↓
Run intent
  ↓
ExecutionPlan
  ↓
approval / confirmation
  ↓
scheduler
  ↓
execution backend
  ↓
Job / attempt
  ↓
results
  ↓
metrics / artifacts
```

The intersection remains the **promoted ProjectVersion**.

## 4.3 AI is not runtime truth

The Development Agent may reason, modify code, validate, analyze evidence, and
propose actions.

It must not become the authority for:

- authorization;
- approval;
- durable state;
- execution truth;
- credential ownership;
- remote job lifecycle;
- artifact provenance.

The platform resolves and owns those facts.

## 4.4 Reuse first, bridge second, refactor last

Implementation priority:

```text
1. Reuse existing capability
2. Add the smallest missing bridge
3. Improve product projection / UX
4. Perform bounded refactor only when a real seam is blocking progress
```

Do not redesign working subsystems merely because an abstraction could be cleaner.

---

# 5. Target V0.1 System Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                         Studio Web                          │
│                                                             │
│ Projects        Compute        Activity        Settings      │
│                                                             │
│ Project Workspace                                            │
│ ├─ AI Engineer                                               │
│ ├─ Changes / Review                                          │
│ ├─ Run Action Cards                                          │
│ ├─ Run Progress                                              │
│ └─ Result / Continue                                         │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTPS / WS
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                     Dispatch Center                         │
│                                                             │
│ Product/Application Layer                                   │
│ ├─ Project / Workspace                                      │
│ ├─ Agent Session orchestration                              │
│ ├─ Version / Promotion                                      │
│ ├─ Run proposal / Run preview                               │
│ ├─ Run / Experiment application services                    │
│ ├─ Compute onboarding / readiness                           │
│ └─ Result analysis projection                               │
│                                                             │
│ Governance / Durable State                                  │
│ ├─ Authorization                                            │
│ ├─ Approval                                                 │
│ ├─ ProjectVersion                                           │
│ ├─ ExecutionPlan                                            │
│ ├─ Job / Attempt                                            │
│ ├─ Metrics / Artifacts                                      │
│ ├─ Audit                                                    │
│ └─ SQLite                                                   │
│                                                             │
│ Execution Infrastructure                                    │
│ ├─ Scheduler                                                │
│ ├─ Monitor / Reconcile                                      │
│ ├─ SSHPool                                                  │
│ ├─ SSH Execution Backend                                    │
│ └─ Result collection                                        │
└───────────────┬───────────────────────────┬─────────────────┘
                │                           │
                │ WS/outbound               │ SSH/SFTP/rsync
                ▼                           ▼
┌──────────────────────────┐    ┌─────────────────────────────┐
│ Development Agent Runner │    │      Compute Targets        │
│ dispatch-agent           │    │                             │
│ Claude Agent SDK         │    │ rental-4090-001 :31827      │
│ isolated worktree        │    │ rental-a100-001 :41222      │
│ workspace permissions    │    │ local-fpga-01 :22           │
└──────────────────────────┘    │                             │
                                │ bash / tmux / workload       │
                                │ results/{job_id}/            │
                                └─────────────────────────────┘
```

---

# 6. Runtime Topology

## 6.1 Control plane

V0.1 should remain simple:

```text
Studio
FastAPI Dispatch Center
SQLite
local result/artifact storage
```

No control-plane Kubernetes requirement is introduced.

## 6.2 Development Agent runner

The current target runtime is:

```text
dispatch-agent
+
Claude Agent SDK
+
isolated project worktree
```

The Development Agent runner communicates with Dispatch Center using the
existing governed runner/session channel.

Codex is used to **develop Dispatch Center itself** for this implementation plan.
It is not automatically reintroduced as a production runtime provider.

## 6.3 Compute targets

Compute targets are currently represented by the existing Server model.

For V0.1:

```text
Execution backend = SSH
```

A target may be:

- owned GPU server;
- rented GPU endpoint;
- CPU server;
- FPGA host.

The underlying provider may use containers or Kubernetes. Dispatch Center only
depends on the SSH contract.

---

# 7. Rental GPU Model

## 7.1 Connection model

A rented GPU endpoint is represented using the existing server configuration
shape:

```text
name
host
port
user
credential/key reference
tags
enabled
```

Custom SSH ports are a first-class requirement.

Example:

```yaml
name: rental-4090-001
host: gpu.vendor.example
port: 31827
user: root
tags:
  - rental
  - gpu
  - rtx4090
enabled: true
```

## 7.2 Rental identity

Do not continually reuse a stable name such as `rental-gpu` while changing the
host/port underneath it.

Prefer:

```text
rental-4090-001
rental-4090-002
rental-a100-001
```

When a rental ends:

```text
enabled = false
```

Reason: server identity is referenced by project instances, observations, caches,
runs, audit history, and other durable records.

V0.1 does not require a full Lease domain.

## 7.3 Scheduling unit

V0.1 uses:

> **one SSH target = one schedulable ordinary-work unit**

Do not implement GPU packing or multiple independent Jobs per multi-GPU target
unless the current execution contract already supports it safely.

---

# 8. Compute Onboarding and Readiness

## 8.1 Product concept

The user-facing term should be **Compute**, not primarily Server Administration.

Main action:

```text
Add Compute
```

Target types in the UI may include:

```text
Rental GPU
My Server
FPGA Server
```

They may still map to the existing ServerConfig backend model.

## 8.2 Rental GPU onboarding flow

```text
Add Compute
    ↓
Rental GPU
    ↓
Name
Host
Port
User
Credential reference
    ↓
Test Connection
    ↓
Read-only Preflight
    ↓
Review fingerprint + detected capability
    ↓
Trust & Add
```

## 8.3 Worker preflight

A target should not be considered usable merely because TCP/SSH succeeds.

V0.1 readiness should reuse existing read-only probes where possible and verify
the minimum runtime contract:

Base:

- SSH connectivity;
- bash/shell availability as required by the existing backend contract;
- tmux;
- git where project synchronization requires it;
- rsync for data/result transfer;
- writable expected working/home path;
- free disk information.

GPU targets:

- `nvidia-smi`;
- GPU presence;
- GPU count;
- VRAM;
- current utilization where available.

The preflight result should be a product projection such as:

```text
Rental RTX 4090

Connection      ✓ SSH
Runtime         ✓ tmux
Source          ✓ git
Transfer        ✓ rsync
GPU             ✓ RTX 4090
VRAM            ✓ 24 GB
Storage         ✓ 312 GB available

READY
```

Only eligible/ready targets should appear as normal Run targets.

## 8.4 SSH host identity

Internet-facing rental targets should eventually use host identity pinning rather
than permanently disabling host verification.

Desired onboarding behavior:

```text
Fingerprint
SHA256:...

[ Trust & Add ]
```

Subsequent mismatch:

```text
BLOCKED
SSH host identity changed
```

This may constitute a new validation/security mechanism. Before implementing it,
Codex must inspect the Charter/Decision boundaries and create a decision packet
if required.

---

# 9. Development Agent Architecture

## 9.1 Responsibilities

The Development Agent may:

- read repository files;
- search code;
- edit files inside the controlled workspace;
- run governed workspace validation;
- inspect diffs;
- reason about project structure;
- propose a checkpoint/version;
- query bounded platform evidence;
- propose a Run;
- analyze Run results;
- recommend the next engineering step.

## 9.2 Prohibited capabilities

The Development Agent must not receive:

- SSH private keys;
- kubeconfig;
- unrestricted remote SSH;
- platform approval capability;
- direct production/compute shell;
- raw database mutation authority;
- direct FPGA flashing/programming authority;
- provider billing credentials.

## 9.3 Runtime provider

V0.1 uses the current Claude Agent SDK / `dispatch-agent` runtime.

Do not combine V0.1 with reintroducing Codex as a runtime provider.

---

# 10. Missing Core Bridge: Agent → Typed Run

This is the most important V0.1 product gap.

The project already contains Development Agent flows and typed Run/ExecutionPlan
flows. They must be connected without creating a second execution model.

## 10.1 Desired tool

Expose a governed tool concept such as:

```text
request_run
```

or:

```text
propose_run
```

The final name should match existing repository conventions.

## 10.2 Agent-visible request shape

The agent should operate on understandable typed inputs:

```text
project
project_version
run_template
parameters
target selection / resource intent
optional experiment context
```

Example:

```json
{
  "project_version": "v38",
  "template": "train",
  "target": "rental-4090-001",
  "parameters": {
    "epochs": 50,
    "batch_size": 32
  }
}
```

## 10.3 Platform-owned resolution

The platform, not the agent, owns:

- revision resolution;
- server configuration revision;
- dataset resolution;
- immutable execution spec;
- plan digest;
- authorization;
- approval;
- Job materialization;
- scheduler dispatch.

## 10.4 Reuse existing Run path

Preferred flow:

```text
Development Agent
    ↓
request_run(...)
    ↓
existing Run Preview / resolve logic
    ↓
immutable ExecutionPlan
    ↓
existing Run Request / approval
    ↓
user confirmation
    ↓
existing Scheduler
    ↓
SSH execution
```

Do **not** introduce a parallel:

```text
AgentRun
AgentJob
AgentExecutionPlan
```

model unless repository constraints make reuse impossible.

## 10.5 Legacy enqueue path

Existing quick/adhoc command paths may remain for advanced/manual use.

They should not become the primary Development Agent execution path because they
bypass much of the typed reproducibility model.

Target positioning:

```text
Advanced
└── Ad-hoc Command
```

---

# 11. Run Execution Architecture

## 11.1 Existing SSH backend remains primary

V0.1 execution:

```text
ExecutionPlan
    ↓
Scheduler
    ↓
SSH Backend
    ↓
SFTP command/script preparation
    ↓
tmux
    ↓
workload
    ↓
sentinel / exit evidence
```

Do not introduce Kubernetes execution.

## 11.2 Execution command ownership

The exact execution bytes/spec must be platform-owned and reviewable.

AI proposes intent. The control plane resolves it to the existing immutable
execution contract.

## 11.3 Failure truth

Preserve existing repository rules regarding:

- unreachable does not automatically mean failed;
- execution status derives from durable evidence;
- reconciliation owns uncertain remote state;
- result collection failure does not rewrite execution truth.

Codex must not simplify these semantics for UX convenience.

---

# 12. Results, Metrics, and Artifacts

## 12.1 V0.1 storage

Keep the existing Server A result collection model.

```text
Remote Worker

results/{job_id}/
├── metrics.json
├── model / checkpoint
├── reports
└── logs / output declarations

        ↓ rsync

Dispatch Center / Server A
```

S3/MinIO is deferred.

## 12.2 Result collection status

The UI needs to distinguish:

```text
execution finished
results collecting
results available
metrics parsed
```

Do not automatically add new canonical Job lifecycle states such as
`COLLECTING` or `COLLECTED` if that would change the protected state machine.

Prefer an existing artifact/collection status or an additive projection.

## 12.3 Metrics

Use the existing metrics contract and current parser/storage.

The Result UX should not merely dump JSON. It should project:

- target metric;
- latest/final metric;
- best metric if available;
- runtime;
- relevant resource usage;
- artifact links;
- comparison against the stated engineering goal where that goal can be
  evaluated safely.

## 12.4 Evidence for AI

The Development Agent should receive evidence through bounded read-only tools.

Useful tool concepts:

```text
get_run
get_run_metrics
get_run_artifacts
get_run_log_tail
compare_runs
```

Before creating new tools, inspect existing Assistant Tools / MCP / Agent Runtime
capabilities and reuse them.

Do not inject unbounded logs into the model context.

---

# 13. Result → AI → Continue Loop

## 13.1 End of Run

After evidence is available:

```text
Run
 ↓
Result Evidence
 ↓
AI analysis
 ↓
recommended next action
 ↓
Continue
```

## 13.2 Continue semantics

V0.1 remains human-in-the-loop.

```text
AI analyzes
    ↓
STOP
    ↓
user presses Continue
    ↓
next Development Agent turn
```

The next turn should know the relevant previous Run ID and use platform tools to
read the evidence.

## 13.3 No autonomous cost loop

Do not create:

```text
train → evaluate → train → evaluate → ...
```

without human confirmation.

This is especially important for rented GPU cost control.

---

# 14. Product UX Architecture

## 14.1 Core principle

> **Users operate engineering goals, not internal domain objects.**

Backend vocabulary may include:

```text
ProjectVersion
Approval
ExecutionPlan
Attempt
ServerConfigRevision
Digest
```

Normal product vocabulary should emphasize:

```text
Project
AI
Changes
Run
Result
Compute
Continue
```

## 14.2 Complexity layers

### Normal

```text
Goal
AI
Review
Run
Result
Continue
```

### Engineering

```text
Diff
Parameters
Metrics
Artifacts
Compute
Logs
```

### Advanced

```text
Approval
ExecutionPlan
Attempt
Revision
Digest
Audit
Raw execution evidence
```

Internal completeness must not force internal terminology into the default UX.

---

# 15. Information Architecture

V0.1 primary navigation target:

```text
Projects
Compute
Activity

Settings
```

## 15.1 Projects

Primary daily workspace.

Project cards should surface actionable state:

```text
YOLO FPGA

Last Run
mAP 0.791

AI has a suggestion

[ Continue ]
```

## 15.2 Compute

User-facing view of remote capacity.

Normal card:

```text
Rental RTX 4090
● Ready

RTX 4090
24 GB VRAM

SSH :31827

[ View ]
```

Advanced view contains host, user, credential reference, roots, backend revision,
and lower-level status.

## 15.3 Activity

Unified human-readable timeline:

- AI version saved;
- Run requested;
- Run started;
- Run completed;
- results collected;
- compute added/disabled;
- approval decision;
- operational failure.

A dedicated Approval view may remain for audit/admin purposes, but approval
should usually be handled inline in the workflow that created it.

---

# 16. Project Workspace UX

The user should be able to stay inside one Project for most of the loop.

Target shape:

```text
┌───────────────────────────────────────────────────────┐
│ YOLO FPGA                                  ● Ready   │
├──────────────┬────────────────────────────────────────┤
│ Overview     │                                        │
│ Runs         │              AI Engineer               │
│ Artifacts    │                                        │
│ Settings     │ conversation / current task            │
│              │                                        │
│              │ structured action cards                │
│              │ run status / result cards              │
│              │                                        │
└──────────────┴────────────────────────────────────────┘
```

Avoid forcing the user through separate system-object pages for every transition.

---

# 17. AI Action Cards

AI output should support structured platform actions, not just text.

Example:

```text
Claude

我完成了 training pipeline 修改。

Changes
3 files modified

Validation
✓ Tests passed

Ready to Run
────────────────
Compute: RTX 4090
Epochs: 50
Batch: 32

[ Review Changes ]   [ Run ]
```

The action card should be a UI projection of governed backend state. It is not a
model-generated direct-execution command.

---

# 18. Review and Version UX

Backend may retain:

```text
checkpoint
approval
promotion
approval
ProjectVersion
```

Default UI should present a coherent engineering action:

```text
AI finished

3 files changed
✓ validation passed

[ Review Changes ]
[ Save Version ]
```

If multiple confirmations are required by existing invariants, guide the user
through them inline instead of removing the protections.

---

# 19. Run UX

Do not make the default user manually perform:

```text
Preview
Create Approval
Open Approval Page
Approve
Open Runs
Find Job
```

Target:

```text
Ready to Run

Version
a81fc92

Compute
Rental RTX 4090

Parameters
epochs 50
batch 32

[ Run ]
```

Internally, the full governed path still occurs.

---

# 20. Run Monitoring UX

Default monitoring should emphasize engineering information:

```text
Training YOLO

Epoch
34 / 50

Current mAP
0.774

Best mAP
0.781

GPU
92%

VRAM
18.4 / 24 GB

Runtime
01:38:21

[ View Logs ]   [ Stop ]
```

Advanced mode can expose raw Job/Attempt/ExecutionPlan identifiers.

---

# 21. Result UX

The result screen should answer:

> Did this Run move the project toward the goal, and what should happen next?

Example:

```text
Run Complete

Goal
mAP > 0.80

Result
0.791

Target not reached

AI Analysis
───────────
Validation loss is still decreasing.
No obvious overfit is visible.

Recommended next step:
- lower learning rate
- train 20 more epochs

[ Continue ]
```

The AI analysis must be grounded in actual Run evidence.

---

# 22. Approval UX

Preserve the approval architecture.

Change the product presentation:

> **one engineering decision should usually correspond to one primary user action**

Examples:

```text
Review & Save
Run
Stop Run
Trust & Add Compute
Continue
```

Do not expose approval mechanics unless useful.

The system must still create the required durable approval/audit records.

---

# 23. Application / Domain Boundaries

V0.1 should not redesign the entire package layout.

Use the newest existing seams where practical.

Conceptually the system contains these responsibilities:

```text
ProjectService
AgentSessionService
Version / Promotion Service
Run Application Service
Compute / Server Service
Execution / Scheduler
Result / Metrics / Artifact Service
Approval / Authorization
Audit
```

These are responsibilities, not instructions to create one class/service per line.

Avoid adding substantial new implementation to legacy-heavy modules such as
`app/main.py`, `app/db.py`, and `app/approvals.py` when a newer package seam
already exists.

Do not create a speculative clean-architecture rewrite.

---

# 24. Data Model Guidance

## 24.1 Reuse existing entities

Prefer existing durable models for:

- projects;
- project versions;
- environments;
- run templates;
- execution plans;
- jobs;
- attempts;
- metrics;
- artifacts;
- server configuration revisions;
- approvals;
- audit;
- agent sessions.

## 24.2 Avoid new tables unless necessary

The V0.1 Run proposal feature should first attempt to reuse:

```text
existing preview/request/approval/ExecutionPlan
```

Do not introduce a `run_proposals` table unless there is a concrete persistence
requirement that cannot be represented through the current workflow.

## 24.3 Additive metadata

If rental-specific metadata becomes necessary, prefer minimal additive fields or a
bounded metadata structure before creating a complete billing/lease subsystem.

Potential future fields:

```text
provider
rental
price_per_hour
lease_started_at
lease_expires_at
```

These are not required for V0.1.

---

# 25. API Architecture

Exact endpoint names should follow current `/api/v2` conventions after repository
inspection.

The product needs equivalent application capabilities for:

```text
Projects
- get project workspace
- list relevant project activity

Agent
- session start/send/stream
- checkpoint/review/promotion
- propose/request Run

Compute
- list compute targets
- create/update/disable target
- test/readiness/preflight

Run
- preview
- request/confirm
- status
- timeline
- logs
- stop
- metrics
- artifacts

Result Loop
- analyze/read evidence
- continue session with prior Run context
```

Do not add duplicate endpoints if equivalent v2 endpoints already exist.

---

# 26. Agent Tool Architecture

The Development Agent should receive a constrained platform tool surface.

Conceptual categories:

## Read-only project/evidence tools

```text
project.get
repo.read/search
run.get
run.metrics
run.artifacts
run.log_tail
run.compare
compute.list/readiness
```

## Governed proposal tools

```text
request_run
request_stop
checkpoint / version proposal
```

No tool grants:

```text
approve
raw SSH
raw kubectl
credential read
arbitrary DB mutation
```

Actual names should reuse existing MCP / assistant-tool conventions.

---

# 27. Security Architecture

## 27.1 Credential boundary

Credentials must not appear in:

- AI prompts;
- AI transcripts;
- normal API responses;
- audit payloads;
- diffs;
- model-readable files.

The AI may receive a stable credential reference only when required by a product
projection, never the secret bytes.

## 27.2 Remote execution boundary

Development Agent:

```text
cannot SSH to compute targets
```

Control plane:

```text
owns SSH connectivity
```

## 27.3 Approval boundary

Agent:

```text
proposes
```

Human/platform policy:

```text
decides
```

## 27.4 Workspace / Compute separation

Workspace Bash permissions are not Compute authorization.

Training, deployment, flashing, and hardware actions remain Compute Plane actions.

---

# 28. Observability and Audit

V0.1 should expose useful product state without replacing existing durable evidence.

Normal Activity should derive from existing events and state where possible.

Important operational observability:

- target online/readiness;
- run queued/running/terminal;
- latest execution evidence;
- result collection outcome;
- metrics parse outcome;
- AI session state;
- pending human action.

Advanced views may expose raw audit and attempt details.

---

# 29. Error UX

Errors should answer three questions:

1. What failed?
2. Is remote state known?
3. What can the user safely do next?

Examples:

```text
Cannot reach rental-4090-001.
Remote Run state is unknown.
Dispatch Center will not mark it failed based only on disconnect.

[ Retry Status ] [ View Details ]
```

```text
Training completed, but result collection failed.

The Run remains completed.
Artifacts are not yet available.

[ Retry Collection ]
```

Do not collapse transport failure, execution failure, and collection failure into
one generic Failed state.

---

# 30. State Projection

The default UI may present friendly states such as:

```text
Preparing
Ready to Run
Queued
Running
Finishing
Collecting Results
Analyzing
Complete
Needs Attention
```

These are **UI projections**.

They must not silently redefine canonical Job/Attempt state machines.

---

# 31. Implementation Strategy

V0.1 implementation should be incremental and vertically testable.

## Phase 0 — Repository Mapping

No production code changes.

Codex must map this architecture onto:

- current modules;
- current APIs;
- DB entities;
- Agent tools;
- Studio components;
- existing tests;
- decision boundaries.

Deliverable: implementation plan only.

## Phase 1 — Compute Onboarding UX

Goal:

> Add/manage a custom-port SSH rental GPU from Studio and see readiness.

Work should reuse existing ServerConfig / server config revision / SSHPool / monitor /
preflight paths.

Acceptance:

```text
Add rental-4090-001
→ test SSH :31827
→ read-only capability detection
→ target becomes Ready
→ target appears in normal Compute selection
```

## Phase 2 — Typed Agent Run Request

Goal:

> Agent can propose a governed typed Run based on the promoted ProjectVersion.

Acceptance:

```text
Agent proposes template + parameters + target
→ existing plan preview resolves
→ user gets inline Run action
→ no Job exists before confirmation
```

## Phase 3 — Session Inline Run Card

Goal:

> The user does not need to leave the AI workflow to create the Run.

Acceptance:

```text
AI work
→ version ready
→ Run card appears in Project/Session
→ user presses Run
```

## Phase 4 — Inline Run Progress

Goal:

> Show useful Run state in the same Project context.

Acceptance:

```text
queued/running/terminal
latest metrics where available
bounded log tail
compute target
stop action
```

## Phase 5 — Result → AI

Goal:

> AI can inspect completed Run evidence.

Acceptance:

```text
Run completes
→ results collected
→ metrics/artifacts available
→ Agent can query them through bounded tools
→ analysis references actual Run
```

## Phase 6 — Continue

Goal:

> User can start the next engineering iteration with one primary action.

Acceptance:

```text
AI result analysis
→ Continue
→ next agent turn receives prior Run context
→ next changes / Run proposal can be created
```

## Phase 7 — UX Consolidation

Goal:

> Default navigation and terminology match the user mental model.

Consolidate normal workflow around:

```text
Projects
Compute
Activity
```

Move lower-level system objects into Advanced surfaces.

---

# 32. Work Packet Requirements

Every implementation packet should include:

- objective;
- existing seam being reused;
- exact modules/files expected to change;
- API impact;
- persistence impact;
- authorization/approval impact;
- UI impact;
- targeted tests;
- acceptance criteria;
- rollback;
- unresolved decision boundary.

Packets must be independently reviewable and testable.

Suggested packet sequence:

```text
WP1  Compute onboarding UX
WP2  Rental worker readiness / preflight projection
WP3  Typed request_run backend seam
WP4  Agent tool integration
WP5  Session Run action card
WP6  Inline Run status
WP7  Result evidence tools
WP8  Continue workflow
WP9  UX / navigation consolidation
WP10 Documentation and acceptance cleanup
```

If WP2 host fingerprint verification requires a new decision, separate it from
ordinary preflight and do not block the rest of V0.1.

---

# 33. Codex Decision-Gate Checklist

Before implementing any packet, explicitly answer:

```text
Does this change an INV-*?
Does this create a new capability class?
Does this create a new approval kind?
Does this change a canonical lifecycle state?
Does this add a provider?
Does this create a new validation mechanism?
Does this change authorization semantics?
Does this create a new irreversible/high-risk action?
```

If any answer is yes and no existing ruling covers it:

> STOP that part and draft the required decision packet first.

Do not implement across the boundary speculatively.

---

# 34. Architecture Constraints for Codex

Codex must not, without explicit new ruling:

- rewrite the scheduler;
- replace SQLite;
- split the monolith into microservices;
- add Kubernetes integration;
- add Redis/NATS;
- add S3 merely for architectural cleanliness;
- grant the Development Agent SSH;
- expose credentials to the agent;
- remove promotion boundaries;
- remove approval boundaries;
- add an autonomous paid-compute loop;
- reactivate Node Agent production rollout;
- reintroduce Codex as a runtime provider;
- add new canonical Job states just for UI;
- replace working v2 APIs with a parallel API family;
- perform broad legacy deletion as part of a feature packet.

---

# 35. V0.1 Definition of Done

## Project / Development

- Project can be opened/imported through the existing product path.
- Development Agent session works.
- Agent can inspect/edit code in isolated workspace.
- Validation can run.
- User can review changes.
- A promoted ProjectVersion is created through the existing governed path.

## Compute

- User can add a rental GPU using host + custom port + user + credential reference.
- Read-only readiness/preflight gives an understandable result.
- Ready target can be selected for a typed Run.
- Existing SSH/tmux execution works.
- Logs and stop behavior remain correct.
- Disconnect semantics remain conservative.

## Result

- Remote results return through the existing collection path.
- Existing metrics contract is parsed.
- Artifacts and metrics are visible in the product.
- Execution completion is not confused with collection success.

## AI Loop

- Development Agent can propose a typed Run.
- User can confirm it without manually reconstructing the Run in another workflow.
- Run evidence can be queried by the Agent.
- Agent produces grounded result analysis.
- Continue starts the next controlled engineering iteration.

## UX

- Core loop can be completed primarily from the Project context.
- Normal UX does not require understanding ExecutionPlan / Attempt / digest IDs.
- One engineering decision generally maps to one primary CTA.
- Compute onboarding is guided and human-readable.
- Advanced/system details remain available without dominating normal use.

---

# 36. V0.1 Acceptance Scenario

A release candidate should demonstrate this exact scenario end to end:

```text
1. Add rental-4090-001 at custom SSH port.
2. Preflight reports target Ready.
3. Open YOLO Project.
4. Tell Development Agent to modify training configuration.
5. Agent edits and validates code.
6. User reviews and saves/promotes the version.
7. Agent proposes a Run using rental-4090-001.
8. User presses Run.
9. Dispatch Center executes over SSH/tmux.
10. Studio shows useful progress and logs.
11. Workload writes metrics/artifacts.
12. Dispatch Center collects the results.
13. Metrics are parsed.
14. Agent reads Run evidence.
15. Agent explains whether the goal was met.
16. User presses Continue.
17. Agent begins the next controlled iteration.
```

If this flow is reliable, V0.1 is successful.

---

# 37. Post-V0.1 Roadmap

## V0.2 — Rental Cost / Lease Metadata

Potential capabilities:

- provider;
- price/hour;
- rental start;
- expected runtime;
- estimated cost;
- actual cost;
- optional expiry reminder.

## V0.3 — Compute Recommendation

Use capability and cost metadata for recommendation:

```text
GPU model
VRAM
availability
price
tags
runtime requirement
```

Human still confirms execution.

## V0.4 — FPGA Engineering Loop

Extend the same Compute Plane:

```text
GPU Training
   ↓
Model Artifact
   ↓
Quantize / Compile
   ↓
FPGA Program
   ↓
Benchmark
   ↓
Metrics
   ↓
AI Analysis
```

No separate unrestricted hardware-agent channel.

## V0.5+ — Provider and orchestration expansion

Only after the V0.1 product loop is stable, reconsider:

- additional Development Agent providers;
- provider APIs;
- automatic rental provisioning;
- richer scheduling;
- object storage;
- multiple concurrent GPU slots;
- A2A/multi-agent collaboration;
- multi-user product experience.

---

# 38. Instructions to Implementation Agents

For Codex / Claude Code working on this target:

1. read `AGENTS.md`;
2. read `CLAUDE.md`;
3. read `docs/README.md`;
4. read the relevant sections of `docs/PLATFORM_CHARTER.md`;
5. read relevant entries from `docs/DECISIONS.md`;
6. inspect `docs/CAPABILITY_LEDGER.md`;
7. read this document;
8. inspect only the relevant code/tests;
9. map requirements to existing seams before proposing new abstractions;
10. produce a plan before coding;
11. stop at any unresolved protected decision boundary;
12. implement one bounded packet at a time;
13. run targeted validation first and the repository-required full gate before commit;
14. report status, changes, evidence, and remaining risks.

The implementation objective is not to make the architecture theoretically perfect.

The implementation objective is:

> **Connect the existing Development Plane and Compute Plane into one coherent, safe, human-friendly engineering loop with the smallest justified changes.**
