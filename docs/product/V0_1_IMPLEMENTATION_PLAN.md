# Dispatch Center V0.1 Implementation Plan

> **Purpose:** single execution plan and progress ledger for
> `V0_1_PRODUCT_ARCHITECTURE.md` and `V0_1_UX_PLAN.md`.  
> **Status:** complete; WP4 remains blocked on `DG-SSH-HOSTKEY`
> **Last updated:** 2026-09-22
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

For any user-visible packet, `V0_1_UX_PLAN.md` is the interaction target. UX is
implemented with the related backend packet, not deferred to a final UI rewrite.

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
| WP0 | Repository architecture mapping | DONE | — | verified map + gap/decision audit |
| WP1 | Overview + Compute information architecture | DONE | WP0 | Overview shell + clear Compute surface/terminology |
| WP2 | Guided SSH compute onboarding | DONE | WP0, WP1 | add custom-port rental GPU |
| WP3 | Compute readiness projection | DONE | WP2 | Ready / Not Ready with reasons |
| WP4 | SSH host identity | BLOCKED | WP0, DG-SSH-HOSTKEY ruling | fingerprint contract if authorized |
| WP4A | AI Workspace + Context usage | DONE | WP0 | reliable text interaction + trustworthy context meter |
| WP5 | Typed Agent → Run application seam | DONE | WP0 | agent can propose existing governed Run |
| WP6 | Development Agent tool integration | DONE | WP5 | typed Run tool without execution authority |
| WP7 | Session inline Ready-to-Run card | DONE | WP4A, WP5, WP6 | Run action inside Project context |
| WP8 | Inline Run monitoring | DONE | WP7 | state/metrics/logs/stop in Project context |
| WP9 | Result evidence access for Agent | DONE | WP0 | bounded Run evidence tools |
| WP10 | Result analysis + Continue | DONE | WP8, WP9 | grounded analysis + human next round |
| WP11 | UX navigation consolidation | DONE | WP1, WP4A, WP7–WP10 | Overview / Projects / Compute / Activity |
| WP12 | End-to-end acceptance | DONE | WP2–WP11, WP4A | full V0.1 scenario demonstrated |
| WP13 | Documentation / capability closeout | DONE | WP12 | repository truth matches implementation |

WP0 was the initial `READY` packet. WP0 and WP1 are complete, and WP2 was
subsequently completed on top of their established architecture and Compute
information surface. WP3, WP4A, WP5, WP6, WP7, WP8, WP9, WP10, WP11, WP12, and
WP13 are complete. No implementation packet remains `READY`; WP4 retains its
named decision gate.

# 4. WP0 — Repository Architecture Mapping

**Status:** DONE

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
- Studio navigation / Overview or dashboard equivalent
- AI composer/session layout and message states
- Agent/model context/token telemetry and compaction behavior
- Studio project/session/run/server components
- Activity/audit product projections
- reusable Studio components/design system
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

- [x] Mapping uses current code/tests.
- [x] Every V0.1 core flow has a seam or documented gap.
- [x] Decision-gate audit is complete.
- [x] No runtime code changed.
- [x] Next packet is promoted to `READY`.

## Evidence

Audit baseline: `76b164d`, 2026-09-19. Read-only explorer mapped the backend;
the parent checked Studio, telemetry, named decisions and sequencing. Paths
below are repository-relative; API paths include `/api/v2` unless explicitly
marked legacy. Tests listed as mapping evidence were inspected, not all run;
executed checks are recorded separately below. No deployment or live runner,
credential, runtime database, audit log or server configuration was inspected.

### Current module / API / test map

| Core flow | Current status and exact reusable seam | Code/test evidence and gap |
|---|---|---|
| Project discovery and workspace | **exists**: `dispatch_center/api/routers/projects_legacy_v2.py:get_legacy_projects_matrix`, `GET /projects-matrix`; `dispatch_center/api/routers/project_bootstrap_v2.py:get_project_workspace`, `GET /projects/{project_id}/workspace`; bootstrap preview/request routes in the same module. | `tests/test_projects_legacy_v2_api.py`, `tests/test_project_bootstrap_v2.py`; Studio `api/hooks.ts:useProjects`, `normalizeInstances`, `useWorkspace`. Matrix includes canonical project ID; preserve name/UUID distinction when linking and requesting. |
| AgentSession, messages, permissions | **exists**: `dispatch_center/api/routers/studio_v2.py` open-requests, start/messages/events/configure/permissions routes under `/studio`; `app/agent_gateway.py:AgentGateway`; `dispatch_agent/client.py`, `dispatch_agent/sdk_adapter.py:SessionHost`. | `tests/test_agent_gateway.py`, `tests/test_dispatch_agent_session_host.py`, `tests/test_dispatch_agent_permissions.py`. Durable events/runtime in `app/db.py`; gateway disconnect becomes unknown. Workspace permission decisions are distinct from platform approval. |
| Checkpoint → review → promoted ProjectVersion | **exists**: `studio_v2.py:request_checkpoint`, `app/approvals.py:request_agent_session_checkpoint_approval`, `app/agent_session_bundle.py`; `app/code_promotion.py:resolve_promotion_candidate`; `POST /engineering-tasks/{task_id}/promote-requests`. | `tests/test_agent_session_checkpoint.py`, `tests/test_code_promotion.py`; `studio/src/features/session/PromotePanel.test.tsx`. Reuse `useCheckpointTasks` / `promotableCheckpointTasks` and `PromotePanel`; promotion remains separate human approval. |
| Environment and Run Template revisions | **exists**: `app/project_environments.py:build_environment_revision_contract`, `evaluate_environment_readiness`; `app/run_templates.py:build_run_template_revision`, `build_project_defaults_revision`, `compile_structured_argv`; routers `project_environments_v1.py`, `run_templates_v2.py`. | `tests/test_project_environments_v1.py`, `tests/test_run_templates_v2.py`; Studio `features/project/SetupPanel.tsx`. Reuse immutable revisions/defaults; no new environment/template models. |
| Typed Run preview → request → confirmation | **exists**: `dispatch_center/api/routers/runs_v2.py:preview_run`, `POST /projects/{project_id}/run-previews` and `/run-requests`; `app/execution_plan_v2.py` request/submit/spec types; `app/execution_plan_v2_store.py:resolve_execution_plan_v2`, `create_execution_plan_v2_request_in_transaction`, `apply_execution_plan_v2_decision_in_transaction`. | `tests/test_execution_plan_v2.py`, `tests/test_execution_plan_v2_api.py`; Studio `features/runs/RunComposer.tsx`, `compose.ts` and their tests. Reuse promoted-version verification, server/template/data pins, digest, retry/idempotency and one-Job materialization at approval. |
| Approval, authorization and audit | **exists**: `dispatch_center/api/routers/approvals_v2.py`, `app/authorization_catalog.py`, `app/approvals.py`; Studio `useDecideApprovalV2`, `features/approvals/singleOperator.ts`, `ApprovalCard.tsx`. | `tests/test_authorization_coverage.py`, `tests/test_assistant_turn_token_auth.py`, `tests/test_execution_plan_v2_api.py`, `studio/src/features/approvals/ApprovalCard.test.tsx`. Keep actor/resource checks, reviewed digest, opaque denials and transaction rollback on durable-audit failure. Single-button confirm is a human decision for a closed kind list. |
| Scheduler, attempts and reconciliation | **exists**: `app/scheduler.py:pick_job`, `app/execution_dispatch.py:AttemptLaunchContext.dispatch/reconcile`, `app/execution_backend.py:ExecutionBackend/SSHExecutionBackend`, `app/monitor.py`. | `tests/test_scheduler.py`, `tests/test_execution_attempt_dispatch.py`, `tests/test_monitor.py`. Reuse exact v2 attempt path, durable evidence and ambiguous-launch handling. Unreachable does not mean failed; no legacy fallback for v2 execution. |
| Server/custom-port SSH | **exists**: `app/config.py:ServerConfig`, `app/server_config.py:validate_server_config`, `app/sshpool.py:SSHPool` (configured port, current `known_hosts=None`); `dispatch_center/api/routers/infrastructure_v2.py` server-config routes. | `tests/test_server_config.py`, `tests/test_server_config_api.py`; `tests/test_results.py::test_build_result_pull_command_non_default_port_appends_dash_p`. Studio `pages/ServersPage.tsx:ServerAdmin` already edits port and invokes test-ssh / attempt-preflight. Guided rental presentation is **missing**; do not replace Server identity. |
| Readiness and instance update | **partial** product projection, **exists** backend facts: `app/server_attempt_preflight.py:run_attempt_filesystem_preflight`; `app/project_environments.py:evaluate_environment_readiness`; `app/execution_plan_v2_store.py:_candidate_for_revision`, `_resource_observation`; `app/project_instances.py:reconcile_all_instances`; router `project_instance_update_v2.py:resolve_instance_update_preview`. | `tests/test_server_attempt_preflight.py`, `tests/test_project_instances.py`, `tests/test_project_instance_update_v2.py`; Studio `features/project/TargetReadiness.tsx`, `InstanceSyncButton.tsx` and tests. Preserve observation freshness, active revision and unknown evidence; a connected host alone is not an eligible target. |
| Results / rsync / metrics / artifacts | **exists**, fragmented projections: `app/results.py:pull_job_results`, `app/jobfinish.py:_collect_run_metrics`, `app/metrics_v1.py:parse_metrics_v1`; `runs_v2.py:get_product_run/list_product_run_artifacts/compare_product_runs`; `GET /runs/{plan_id}`, `/runs/{plan_id}/artifacts`, `/runs/compare`; legacy `GET /jobs/{job_id}/metrics` in `app/main.py`. | `tests/test_results.py`, `tests/test_metrics_v1.py`, `tests/test_job_metrics_api.py`, `tests/test_job_results_api.py`, `tests/test_execution_attempt_dispatch.py`. `jobs_v2.py` exposes bounded log/result/file access. Keep result-collection failure separate from execution truth and preserve artifact provenance/path confinement. |
| Agent → typed Run | **missing** bridge; current `app/mcp_bridge.py` and mirrored `dispatch_agent/mcp_bridge.py` expose legacy `request_enqueue_job`, not `request_run`. **exists** governed v2 application seam above. | `tests/test_mcp_bridge.py`, `tests/test_mcp_bridge_mirror.py`. `app/authorization_catalog.py:MCP_TOOL_ROUTES/ASSISTANT_TURN_TOKEN_ROUTES` and `app/main.py:_assistant_turn_token_route_gate` currently omit typed Run routes. WP5/6 must coordinate the approved proposal tool with exact catalog/token-route bindings; never reuse raw enqueue as the normal Run path. |
| Agent result evidence | **partial**: bridge `list_jobs`, `get_job`, `get_job_log` (1–80 lines), `get_project_activity`, `get_project_timeline` exist. **missing** registered v2 Run detail/metrics/artifact/comparison tools. | `app/mcp_bridge.py:MCP_TOOL_ACTIONS` and tool registrations; `tests/test_mcp_bridge.py`, `tests/test_assistant_turn_token_auth.py`. WP9 should adapt existing bounded APIs and actor/project checks; do not assume a working browser API is already available to a session token. |
| Overview / navigation / Compute | **missing** Overview and Compute terminology; **exists** Studio shell, Project/Run/Server/events pages. `studio/src/App.tsx` defaults to `/projects`; `components/Shell.tsx` has Projects, Runs, Servers/hardware, datasets, approvals, audit and settings. | `studio/src/App.test.tsx`, `tests/test_studio_static.py`; `pages/ServersPage.tsx`, `features/runs/ServerChips.tsx`. Reuse existing routes/identities and Advanced detail. Final four-surface consolidation belongs to WP11. |
| AI workspace and timeline | **partial**: `studio/src/pages/ProjectPage.tsx`, `features/session/SessionView.tsx`, `Transcript.tsx`, `transcript.ts`, `useSessionStream.ts`, `PermissionCard.tsx`, `PromotePanel.tsx`. Text, streamed deltas, tools, permissions and promotion exist. | `studio/src/features/session/transcript.test.ts`, `PromotePanel.test.tsx`; `tests/test_agent_gateway.py`. Composer clears draft before send success, has no explicit retry restoration or IME guard; context-unavailable and structured Run/result/Continue cards are missing. WP4A/7/8/10 own these gaps. |
| Inline Run monitoring / Continue | **partial** monitoring outside session: `studio/src/pages/RunsPage.tsx:LogDrawer/MetricsCell/StopButton`, `api/hooks.ts:useJobs/useJobLog/useExperiments`; stop requests use `/runs/{plan_id}/stop-requests`. Session inline Run/result/Continue is **missing**. | RunComposer/approval tests cover existing creation/decision seams; no current end-to-end session Continue proof. Reuse typed Run IDs and persisted state. AI recommendations must stop before the next human-controlled turn/Run. |
| Activity / audit product projection | **partial**: `studio/src/pages/EventsPage.tsx` reads `/api/v2/events?limit=200`, uses `labels.ts:describeAudit/actorLabel`, exposes raw details on expansion. | `tests/test_events_audit_v2_api.py`, `studio/src/labels.test.ts`. Reuse real events and provenance; product-oriented Activity grouping/links are WP11. Do not infer all missing result fields as success. |
| Design system | **exists**: `studio/src/components/ui/button.tsx`, `card.tsx`, `badge.tsx`, `lib.ts:cn`, `index.css`; React + TypeScript + Vite + Tailwind + TanStack Query + hash routing. | Existing component/feature Vitest suites; `tests/test_studio_static.py`. Reuse these components. Existing Project-card nested anchors produce a React warning in App tests; WP0 does not change UI. |
| Flags / deployment posture | **exists** explicit registry/default tests: `app/config.py`, `app/settings/features.py`, `tests/test_pilot_posture.py`. API v2, typed Runs, environments/templates, runtime v3 and metrics default on; execution-attempt launch/reconcile/outbox chain defaults off. | Local tests are capability evidence, not proof of live deployment or permission to enable flags. Node backend presence does not authorize rollout; retired session v1/Codex surfaces are **legacy-only**, not a V0.1 runtime alternative. |

### Authoritative UI projections and bounded next steps

- Project attention: no aggregate attention API. Existing project instances,
  pending approvals and session `pending_permissions`/runtime state are the
  factual inputs. WP1 may link these existing facts; it must not fabricate AI
  suggestions, readiness, completion or a Continue action. If aggregation needs
  a seam, keep it a read-only application projection of those facts.
- Active Run/current target/progress: `runs_v2.py:get_product_run`, existing
  experiment members and Job status, plus `useServerOccupancy`; preserve Run
  plan ID versus Job ID. No generic epoch/percentage should be invented.
- Compute health: `GET /servers` (`infrastructure_v2.py:list_servers`) is the
  existing monitor projection; eligibility remains the v2 resolver's decision.
  WP1 must distinguish health from execution readiness. WP3 reuses fixed
  hostname/user/tmux/GPU/root-directory probes, monitor disk/RAM/CPU/GPU data,
  typed executable/tag checks and revision-pinned filesystem preflight.
  Environment/secret-reference/checkout-relative-path execution preflight is
  currently unsupported; the resolver rejects it as
  `environment_preflight_evidence_unsupported`. Additional writable-path/git/
  rsync checks require verification against the closed probe set before work;
  absent evidence is unknown, not passed or failed.
- Latest metrics/result collection: existing metrics and Run artifact APIs;
  current RunsPage collapses metrics fetch failure into an empty list. WP8/9
  must preserve missing/failed collection distinctions when adapting it.
- Session state and recent activity: durable gateway events, Studio session
  response and audit events; presentation must show unavailable/stale/partial
  sections independently, following UX Plan §§4, 13 and 18.

### WP4A context telemetry determination

`dispatch_agent/pyproject.toml` declares Claude Agent SDK `>=0.2.140,<0.3`;
this is a dependency range, not evidence of a deployed SDK version. Runtime
model comes from session options/configure and provider system events. No
repository-owned model → context-window table or guaranteed limit was found.

`sdk_adapter.py:serialize_sdk_message` passes opaque `ResultMessage.usage`,
`num_turns`, cost and session ID at turn completion. Cost accounting is not
context occupancy. `_emit_context_usage` calls optional `get_context_usage`
after a result, only if the client has it, and silently omits unsupported or
failed calls. `SessionView` consumes the latest `context` event, expecting
`total_tokens` (or category sum) and `context_window`.

Current tests use an injected fake SDK and do not establish a production
context shape, cache read/write token semantics, or whether usage fields are
per-turn versus cumulative/session accounting. There is no repository-managed
compaction command or normalized compaction event; advertised SDK slash
commands may be surfaced, but their presence does not prove a compaction
contract. No reliable universal percentage can currently be guaranteed.

WP4A safe baseline is UX Mode C, **Context usage unavailable**. Mode A may use
an explicitly validated current-occupancy/positive-window pair with source and
freshness; do not sum arbitrary cache/result tokens or treat an empty category
array as zero usage. Model changes must not reuse stale limits. A repository
estimate would need explicit semantics and `Estimated` labeling; no estimate
or independent compaction mechanism is selected by WP0. Tests for send failure,
IME handling, missing/malformed/stale context and optional telemetry remain
WP4A work.

### Decision-gate audit and sequencing

| Boundary | Authoritative rule / decision | WP0 disposition |
|---|---|---|
| Development → Compute / promotion | Charter `INV-PLANE-1/2`, `INV-AGENT-1/2`; `DG-AGENT-RUNTIME-V3` and checkpoint/promotion decisions | Existing local workspace/permission/checkpoint/promotion chain is reusable. No validation provider, workspace authority, auto-promotion or execution authority change is authorized. |
| Agent typed proposal | `docs/DECISIONS.md`, 2026-08-30 `DG-ASSISTANT-TOOLS` S-2 authorizes `request_run` → pending `execution_plan_v2`; runtime-v3 R7 carries tool integration forward | WP5/6 can bridge this approved capability. Load Development Agent/approval boundary skills when implementing exact token/catalog integration. Stop for any broader authorization semantic, new kind or direct execution. |
| Human Run/stop decision | `INV-APPROVAL-*`, `INV-SSH-9`, `INV-LLM-1/2/3/4`; `DG-SINGLE-OPERATOR-CONFIRM` closed list | Preserve digest/idempotency, human identity and complete audit; no agent approve/reject/shell. Promotion, destructive/physical actions and excluded kinds remain separate review. |
| SSH host identity | `INV-SSH-8`, named `DG-SSH-HOSTKEY` in Charter §7.2; no approving ruling found | WP4 **BLOCKED**. Draft: [DG-SSH-HOSTKEY](../decisions/DG_SSH_HOSTKEY_DRAFT.md). Fingerprint trust/pinning/mismatch semantics require a named decision before implementation. |
| Readiness probes | `INV-SSH-1/3/4/5`, existing filesystem preflight and environment readiness | WP2/3 may reuse existing evidence. New validation mechanisms or expanded probe authority must be split into a decision-gated packet, not silently added. |
| State/recovery/collection | `INV-STATE-*`, `INV-SSH-7`, `INV-AUDIT-*`; reserved `DG-JOB-STATE`, `DG-ATTEMPT-RECOVERY` | UI states are projections only. Preserve durable launch ambiguity, unreachable=unknown and independent collection truth. No lifecycle, automatic redispatch or audit policy changes. |
| Evidence tools | Existing bounded APIs, `MCP_TOOL_ROUTES`, `ASSISTANT_TURN_TOKEN_ROUTES`, actor/project scope | WP9 first adapts existing evidence. Any broader result-file access, token scope or authorization semantics must stop for decision; browser availability is not agent authorization. |
| Deferred capabilities / rollout | Charter §7 gates and plan scope guard | No GPU slots, provider provisioning, object store, production Node rollout, Codex provider, autonomous paid loop or deployment. WP12 cannot claim an Internet-facing host-identity scenario complete while WP4 is blocked. |

Ordering review: retain WP1 → WP2 → WP3; WP4 adds a named-ruling prerequisite.
WP4A and WP5 may follow WP0 independently after WP1, but neither is promoted
now. WP6 depends on WP5; WP7 still needs WP4A/WP5/WP6; WP8 follows WP7;
WP9 can adapt evidence independently; WP10 needs WP8/WP9. WP11/12/13 retain
their integration/acceptance dependencies. No extra runtime packet is needed
for the audit itself.

First safe implementation packet: **WP1 READY**, using existing read-only
Project/Run/Compute/audit projections, Studio components and Advanced routes.
Do not migrate Server models or add speculative readiness/AI claims. Its
acceptance includes real-data links and independent empty/loading/partial
states, not completion of later Run/Continue workflows.

### Validation evidence

- `.venv/bin/python -m pytest tests/test_dispatch_agent_session_host.py tests/test_agent_gateway.py tests/test_events_audit_v2_api.py tests/test_studio_static.py -q`: **PASS, 30 tests**.
- `.venv/bin/python -m pytest tests/test_execution_plan_v2.py tests/test_execution_plan_v2_api.py tests/test_results.py tests/test_pilot_posture.py -q`: **PASS, 55 tests**.
- `npm test --prefix studio -- --run src/App.test.tsx src/features/session/transcript.test.ts src/features/runs/RunComposer.test.tsx src/features/session/PromotePanel.test.tsx`: **PASS, 8 tests**. Existing nested-anchor warning in `ProjectsPage` reproduced; no UI changes made.
- Documentation authority, bridge/token/probe checks and final diff verification are recorded in the WP0 change-log entry below.
- All Python checks use repository temporary-directory/network-denial fixtures.
  No full release/deployment gate was invoked: WP0 changes documentation and
  its link-test coverage only, and no commit is being created.

# 5. WP1 — Overview + Compute Information Architecture

**Status:** DONE

## Goal

Establish the first-glance Overview and a clear user-facing **Compute** concept
without replacing the existing Server domain model.

## Scope

- Overview sections for Project attention, active Runs, Compute health, and recent activity;
- normal vs Advanced Compute information;
- rental GPU / owned server / hardware host presentation;
- terminology reused by onboarding and Run target selection;
- existing server identities/history preserved;
- reuse the current Studio design system rather than introducing a second UI framework.

## Expected reuse

- Servers page
- ServerConfig
- server status/readiness APIs
- target candidate UI

## Acceptance

- [x] Overview shows Project attention, active Runs, Compute health, and recent activity using real backend projections.
- [x] Overview attention items link to the relevant Project/Run/Compute action.
- [x] User-facing terminology is Compute-oriented.
- [x] Server identity/history semantics are unchanged.
- [x] Advanced details remain available.
- [x] Empty/loading/partial-data states follow `V0_1_UX_PLAN.md`.
- [x] No Server → ComputeTarget domain migration.

## Evidence

Implemented in `studio/src/pages/OverviewPage.tsx`, the existing
`ServersPage.tsx`, `EventsPage.tsx`, `RunsPage.tsx`, shared API hooks/types and
the Studio shell/router. Overview independently projects the authorized
projects matrix and pending approvals, running/queued Jobs, server config/live/
idle observations, and bounded audit events. Each item links to its actual
Project, numeric Job-backed Run view, or selected Compute identity; no typed
plan ID is inferred from a Job.

The authenticated landing route is now `/overview`. Primary navigation is
Overview / Projects / Compute / Activity / Settings; Runs, datasets and the
dedicated approval inbox remain available under Advanced. `/servers` and
`/events` remain query-preserving aliases. Compute merges existing
`ServerConfig` identities with live observations without changing either
model. Its normal cards distinguish Connected (health only, explicitly not
Ready), Disconnected, Unknown, stale, Disabled, Needs attention and the
existing non-local-filesystem preflight Blocked evidence. GPU/device/general
capability is shown only from declared or observed facts; ownership remains
explicitly unavailable because the backend has no rental/owned field.

Advanced Compute retains the existing create/edit/custom-port/test/preflight/
enable-disable/delete and runner controls. Normal cards never show key paths,
roots, raw notes, commands or credentials. Activity uses human labels and
hides command text until the keyboard-accessible Advanced record expansion.
Independent loading, empty, partial, 403, retry, cached-update, browser-offline,
unknown, stale and disconnected presentations are implemented. Targeted tests
cover landing/navigation/alias preservation, all four Overview projections and
links, loading/partial/blocked access, safe activity text, custom ports and
Compute projection states. Validation commands and results are in the change
log entry below.

# 6. WP2 — Guided SSH Compute Onboarding

**Status:** DONE

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

- [x] Custom port survives create/edit/test/use.
- [x] Existing server configuration governance remains intact.
- [x] Rental GPU is understandable without a provider subsystem.
- [x] Connection/config errors are actionable.

## Evidence

- Implemented a four-step Add Compute flow in
  `studio/src/features/compute/AddComputeWizard.tsx`, integrated with the
  existing Compute/Server administration surface in
  `studio/src/pages/ServersPage.tsx`.
- The flow collects the existing `ServerConfig` fields for name, host, custom
  SSH port, user, private-key path reference, and tags. Rental GPU, owned
  server, and FPGA host choices add classification metadata only; they do not
  introduce provider, billing, lease, device-discovery, or lifecycle state.
- Connection testing sends the exact unsaved draft to the existing read-only
  `POST /api/v2/server-configs/test-ssh` route. Any draft change invalidates
  successful evidence. The UI reports only the fixed probe's hostname, user,
  tmux, and GPU observations and explicitly does not claim host-key trust or
  complete runtime readiness.
- Final Add reuses `POST /api/v2/server-configs`, preserving its existing
  publication, authorization, audit, revision, backup, reload, and failure
  behavior. The existing revision-scoped attempt-filesystem preflight runs
  after successful creation because its current contract requires a stored
  active revision; eligible, blocked, unknown, and request-error outcomes are
  distinct, and preflight failure never rolls back or rewrites the successful
  add.
- Credential input is a server-side key-path reference. The flow never asks
  for or renders private-key contents, and its review shows only the reference
  basename.
- Targeted Studio coverage in
  `studio/src/features/compute/AddComputeWizard.test.tsx` verifies exact custom
  port/payload reuse across test and add, classification tags, stale-test
  invalidation, post-add preflight failure semantics, actionable connection
  errors, and refusal to add after a failed test.
- Existing backend contract coverage verifies port validation and persistence,
  use of a staged custom port by Test Connection, downstream SSH/rsync port
  propagation, and guarded edits while execution ownership exists. The
  existing edit, enable/disable, approval-backed delete, and advanced Server
  administration controls remain in place.
- Validation: `npm test -- --run` (41 tests passed); `npm run build` (passed);
  `.venv/bin/python -m pytest -q tests/test_server_config_api.py
  tests/test_server_config.py tests/test_server_attempt_preflight.py
  tests/test_results.py` (94 tests passed); document-authority tests (passed);
  `git diff --check` (passed).

# 7. WP3 — Compute Readiness Projection

**Status:** DONE

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

- [x] Ineligible targets are not offered as normal Run targets.
- [x] Readiness reasons are visible.
- [x] Existing monitor/readiness logic is reused.
- [x] No arbitrary remote probing is introduced.

## Evidence

The Project Workspace target projection now combines its existing reviewed
ServerConfig revision and ProjectInstance checks with the latest durable
`server_observations` row. It reuses the execution contract's 60-second
freshness limit and existing safe reason codes, treating missing, malformed,
future, pre-activation, or stale evidence as `UNKNOWN`, and fresh offline/probe
failure as `NOT READY`. Structural Project/version failures project `BLOCKED`.

Studio normal Run, Experiment, and Quick Command controls offer only candidates
whose projection is `READY`; excluded candidates remain visible with their
readiness reasons. No SSH command, host-key behavior, remote probe, approval,
authorization, audit, or canonical Job lifecycle semantics changed. Desired
checks without existing authoritative evidence remain outside this packet
rather than introducing a new validation mechanism.

Validation:
- `.venv/bin/python -m pytest -q tests/test_workspace_run_creation_options.py tests/test_project_environments_v1.py`: PASS, 28 tests.
- `npm test --prefix studio`: PASS, 50 tests in 15 files.
- `npm run build --prefix studio`: PASS, TypeScript and Vite production build.
- `.venv/bin/python scripts/frontend_smoke.py --require-studio`: PASS.
- Affected backend contract suite: PASS, 168 tests.
- `.venv/bin/python -m pytest -q -n 4 --durations=25 --durations-min=0.5`: PASS, 3972 tests.
- `git diff --check`: PASS.

# 8. WP4 — SSH Host Identity

**Status:** BLOCKED

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

WP0 confirmed `app/sshpool.py` passes `known_hosts=None`; Charter `INV-SSH-8`
explicitly reserves policy changes to `DG-SSH-HOSTKEY`. No named approval
covers the proposed first-trust/pin/mismatch workflow. See the unapproved
[decision draft](../decisions/DG_SSH_HOSTKEY_DRAFT.md). Implementation and final
acceptance definition wait for that ruling; existing behavior is unchanged.

# 8A. WP4A — AI Workspace + Context Usage

**Status:** DONE

## Goal

Make the existing Project AI conversation a reliable primary interaction surface
and expose trustworthy context usage without inventing telemetry.

## Scope

- multiline text composer and send/retry/loading states;
- conversation + structured engineering actions in one timeline;
- context-usage indicator in the Project header/workspace;
- context details drawer;
- provider-reported vs estimated vs unavailable usage labeling;
- near-limit warning based on real runtime behavior;
- no independent compaction mechanism unless current Agent runtime supports it.

## WP0 must determine

- active model/runtime context-window source;
- provider/Claude Agent SDK usage fields;
- per-turn vs session-level accounting;
- cache token semantics;
- existing compaction/summarization behavior;
- whether a reliable percentage can be computed.

## Acceptance

- [x] User can type and send natural-language instructions from the Project AI Workspace.
- [x] Sending/streaming/failure/session-unavailable states are explicit.
- [x] Context usage is visible or explicitly unavailable.
- [x] Estimated usage is labeled `Estimated`.
- [x] Unknown context-window limits are not invented.
- [x] Context details show only information the runtime can substantiate.
- [x] Existing Agent security/permission boundaries remain unchanged.
- [x] Targeted UI tests cover important composer/context states.

## Evidence

The existing AgentSession API and persisted event stream remain authoritative.
Studio now presents explicit loading, sending, waiting, streaming,
permission-required, failed-send, unavailable, and closed interaction states.
Enter sends, Shift+Enter preserves multiline input, IME composition is guarded,
and a rejected send preserves the exact draft/attachments for retry.

Context projection uses the latest runtime `context` event only when it contains
a finite non-negative `total_tokens` value and positive `context_window`. It
does not derive a total from opaque categories, invent a model limit, or reuse
telemetry after a model change. Explicit estimates are labeled `Estimated`;
otherwise valid SDK telemetry is labeled `Provider-reported`. The details
drawer renders only validated counts and categories. Missing or malformed
telemetry is shown as `Context usage unavailable`.

Validation:
- `npm test --prefix studio`: PASS, 58 tests in 17 files.
- `npm run build --prefix studio`: PASS, TypeScript and Vite production build.
- `.venv/bin/python scripts/frontend_smoke.py --require-studio`: PASS.
- AgentSession/gateway/API affected suite: PASS, 68 tests.
- `.venv/bin/python -m pytest -q -n 4 --durations=25 --durations-min=0.5`: PASS, 3972 tests.
- `git diff --check`: PASS.

# 9. WP5 — Typed Agent → Run Application Seam

**Status:** DONE

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

- [x] Promoted ProjectVersion is mandatory.
- [x] Existing preview/resolution is reused.
- [x] Existing ExecutionPlan/approval semantics are preserved.
- [x] No Job exists before the governed materialization point.
- [x] Retry/idempotency follows existing application semantics.

## Evidence

Added session-scoped typed Run preview/request endpoints under
`/api/v2/agent-sessions/{session_id}`. The durable AgentSession supplies the
Project binding; the authenticated requester supplies authorization and
attribution. Both endpoints use the exact existing `ExecutionPlanV2Request`
contracts and delegate to the same resolver, digest, unit-of-work,
idempotency, and `execution_plan_v2` approval code as the Project routes.

Preview remains read-only. Request creates the existing immutable plan and
pending approval only; Job creation remains exclusively in the existing
post-approval materialization transaction. Authorization catalog entries use
the existing `PROJECT_OPERATE` action and AgentSession resource resolution.
No tool transport, new model/table, approval kind, lifecycle state, provider,
SSH path, or execution authority was added.

Validation:
- Focused typed seam/ExecutionPlan/authorization/OpenAPI suite: PASS, 61 tests.
- Broader AgentSession/idempotency/authorization suite: PASS, 85 tests.
- Studio tests/build and frontend smoke: PASS, 58 tests in 17 files.
- `.venv/bin/python -m pytest -q -n 4 --durations=25 --durations-min=0.5`: PASS, 3976 tests.
- Ruff and `git diff --check`: PASS.

# 10. WP6 — Development Agent Tool Integration

**Status:** DONE

## Goal

Expose WP5 through the current dispatch-agent platform-tool boundary.

Likely concept: `request_run`; final name follows repository conventions.

## Constraints

The tool proposes/requests only. It exposes no approve/reject, credential, raw
SSH, raw platform shell, or direct Job creation.

## Acceptance

- [x] Tool is available to the Claude Agent SDK session path.
- [x] It produces the same governed path as normal Studio Run creation.
- [x] Response contains safe data usable by the Agent/UI.
- [x] Execution authority remains outside the Agent.

## Evidence

- `app/mcp_bridge.py` and its byte-identical `dispatch_agent/mcp_bridge.py`
  mirror expose `request_run` through the Claude Agent SDK session MCP path.
- The tool requires the exact WP5 submission fields, current plan digest, and
  idempotency key, then posts only to the AgentSession-scoped governed Run
  request route.
- `app/authorization_catalog.py` binds the tool to `PROJECT_OPERATE`; the
  assistant turn-token route allowlist is derived from that exact mapping.
- Focused MCP, mirror, authorization, turn-token, AgentSession, gateway, and
  runner-permission validation passes (130 tests).
- The tool returns the bounded platform response and exposes no approve/reject,
  direct Job, credential, raw SSH, or shell authority.

# 11. WP7 — Session Inline Ready-to-Run Card

**Status:** DONE

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

- [x] User does not rebuild the Run manually in RunComposer.
- [x] Card is backed by governed platform state, not model text alone.
- [x] Required approvals cannot be bypassed.
- [x] Runs view remains available for history/advanced use.

## Evidence

- The timeline recognizes only the persisted `request_run` tool result, extracts
  its platform-issued approval id, and fetches authorized approval detail.
- `ReadyToRunCard` renders the verified immutable ExecutionPlan v2 review
  contract; assistant text and raw tool input cannot supply display truth.
- The primary `Run` action reuses `ApprovalCard` and the existing v2 approval
  decision route. It cannot directly create or dispatch a Job.
- Promoted version, Compute target, important parameters, readiness, and any
  explicitly grounded estimate are shown; identifiers and digests stay under
  Advanced details.
- Loading, unavailable, malformed, pending, approved, and rejected states are
  covered, and the Runs history link remains available.

# 12. WP8 — Inline Run Monitoring

**Status:** DONE

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

- [x] Project context uses the same durable Run truth as Runs view.
- [x] Disconnect does not become false failure.
- [x] Stop uses the existing governed path.
- [x] Raw details remain available in Advanced view.

## Evidence

- The approved WP7 card recovers its plan id from verified approval detail and
  polls the existing Product Run v2 projection used by the Runs surface.
- Canonical execution state, attempt liveness, metrics, bounded 80-line log,
  result collection, and metadata-only artifacts remain separate facts.
- Elapsed time is derived only from durable job-start/job-terminal timeline
  timestamps. Remote unknown and transport-unavailable states never become
  false execution failure.
- The stop action calls only the Product Run stop-request route, then reuses the
  existing v2 approval card and human decision path.
- Raw bounded Run/timeline/metrics/log/artifact projections remain available in
  Advanced details.

# 13. WP9 — Result Evidence Access for Agent

**Status:** DONE

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

- [x] Agent can retrieve evidence needed for common result analysis.
- [x] Responses are structured and bounded.
- [x] Artifact provenance remains platform-owned.
- [x] Existing tools are reused where possible.

## Evidence

- Five read-only MCP tools expose Product Run detail, metrics, artifact metadata,
  bounded log tails, and Run comparison through existing authorized APIs.
- Metrics and logs resolve Job identity only through the authorized Product Run
  projection. Missing Jobs, metrics, logs, and comparison dimensions remain
  structured unknown states.
- Timeline, nested lists, metrics, artifact pages, log lines, log characters,
  and final serialized responses have explicit bounds and truncation metadata.
- Artifact evidence remains platform-owned metadata with existing provenance and
  digest fields; the tools add no download, filesystem, shell, or credential path.

# 14. WP10 — Result Analysis and Continue

**Status:** DONE

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

- [x] Analysis can identify the Run evidence used.
- [x] Continue is a human action.
- [x] Next iteration stays in the same Project context.
- [x] Agent recommendation alone cannot start another Run.

## Evidence

- A terminal Run offers a human-triggered analysis turn in its existing Project
  AgentSession. The bounded prompt names the exact verified Product Run and
  requires WP9 evidence tools, explicit evidence references, unknown handling,
  one recommendation, and a stop.
- Persisted transcript events recover analysis state after reload. A completed
  analysis requires a successful structured WP9 tool result for the exact Run,
  completed assistant text, and a successful turn result; prose alone does not
  qualify.
- The Result / AI Analysis card lists the actual evidence tool and call
  references. Its human-only Continue button sends a bounded next-turn message
  through the same Project-bound AgentSession.
- Analyze and Continue never call a Run or approval route. Any later Run remains
  a new governed `request_run` proposal requiring the existing human action.

# 15. WP11 — UX Navigation Consolidation

**Status:** DONE

## Goal

Normal product mental model:

```text
Overview
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

Simplify navigation and labels; make Overview the first-glance operational home;
keep advanced execution/governance surfaces available; do not perform a full
Studio rewrite.

## Acceptance

- [x] Overview gives a useful system-at-a-glance entry point.
- [x] Core V0.1 loop completes primarily from Project context.
- [x] Normal users need not understand ExecutionPlan/Attempt/digest IDs.
- [x] Compute management is discoverable.
- [x] Activity provides understandable operational history.
- [x] Advanced/debug capability is retained.

## Evidence

- Primary navigation now uses the canonical Overview, Projects, Compute,
  Activity, and Settings model. Runs, Datasets, and Approvals remain available
  under Advanced, and legacy Server/Event aliases retain query parameters.
- Overview continues to project real attention, active Run, Compute, and recent
  activity data while default Run labels omit internal Job ids.
- The Project AgentSession remains the normal Ask → Review → Run → Result →
  Continue surface; generated analysis actions and result cards use human labels
  while exact identifiers remain in Advanced disclosures.
- Activity presents readable activity/result/operator columns. Complete audit
  records, including source and raw actions, remain lazily available under
  Advanced audit details.
- Compute onboarding/readiness remains directly discoverable from primary
  navigation and Overview, with existing Advanced Compute controls retained.

# 16. WP12 — End-to-End Acceptance

**Status:** DONE

## Scenario

1. Open Overview and see Project/Compute state.
2. Add a custom-port rental GPU target.
3. Readiness reports Ready.
4. Open a real Project.
5. Type a natural-language request and see Context usage or an explicit unavailable state.
6. Ask Agent for a bounded code/config change.
7. Validate.
8. Review and promote.
9. Agent proposes typed Run.
10. Human presses Run.
11. Existing SSH/tmux path executes.
12. Useful progress/logs are visible.
13. Results are collected.
14. Metrics are parsed.
15. Agent reads evidence.
16. Agent analyzes whether the goal was met.
17. Human presses Continue.
18. Next controlled engineering iteration starts.

## Acceptance

- [x] All 18 steps pass.
- [x] Required audit/approval records exist.
- [x] No credential leaks to prompt/transcript/audit/UI payload.
- [x] Failure semantics remain correct.
- [x] Required evidence is recorded according to existing governance.

## Evidence

- `tests/test_v01_end_to_end_acceptance.py` composes the persisted Project,
  AgentSession, ProjectVersion, typed Run, approval, Job, execution-attempt,
  artifact, metric, log, and MCP evidence seams in one deterministic journey.
- The journey provisions an approved custom-port (`22022`) offline rental target,
  records readiness, requires a different human reviewer, and proves that the
  reviewed target revision reaches the closed SSH/tmux dispatch contract.
- The same persisted Product Run is completed, collected, and read through the
  bounded WP9 MCP tools. Direct Studio tests cover Overview, context usage,
  grounded analysis, and Continue on the same AgentSession; the existing
  apply-patch, validation, and checkpoint suites cover the governed development
  and promotion stages.
- Approval/audit records and credential filtering are asserted across API,
  audit, prompt, transcript, and UI contracts. Ambiguous launch remains
  `unknown` without requeue, and collection failure does not rewrite successful
  execution truth.
- Acceptance is offline and deterministic: fake SSH proves the target/command
  contract without contacting an Internet rental host. It does not claim
  Internet host-identity acceptance or resolve `DG-SSH-HOSTKEY`; WP4 remains
  blocked on that named decision.

# 17. WP13 — Documentation and Capability Closeout

**Status:** DONE

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

- [x] Every completed packet is `DONE` with evidence.
- [x] No stale progress claim remains.
- [x] Ledger matches code/tests.
- [x] V0.1 Definition of Done is independently rechecked.

## Evidence

- The Program Board and packet sections agree: WP0–WP3 and WP4A–WP13 are
  `DONE` with recorded evidence. WP4 alone remains `BLOCKED` on the unresolved
  named `DG-SSH-HOSTKEY` decision and is not claimed as completed.
- The Capability Ledger was rechecked against the current repository gate and
  Studio suite, with stale test counts replaced by the verified 3989-backend /
  76-Studio-test evidence. No pilot, canary, or production claim was inferred.
- The Architecture Definition of Done and UX live acceptance checklist were
  independently rechecked against completed packet evidence and the composed
  WP12 acceptance harness. The target architecture and decision boundaries did
  not change.
- The V0.1 loop is accepted through deterministic offline evidence. This
  closeout does not constitute a release gate, deployment, real-rental-host
  canary, or host-identity ruling.

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

## 2026-09-19 — WP0 Repository Architecture Mapping

Status: DONE

Implemented:
- Recorded current module/API/test mapping, reusable seams, UI projection and
  context-telemetry gaps, named decision audit and dependency review in WP0.
- Added the unapproved `docs/decisions/DG_SSH_HOSTKEY_DRAFT.md` and indexed it
  under awaiting decisions. No authority or runtime behavior changed.
- Extended `tests/test_document_authority.py` live-document coverage to the
  implementation plan and new decision draft.

Validation:
- Session/gateway/events/static tests: PASS, 30 tests (exact command in WP0).
- Typed plan/results/posture tests: PASS, 55 tests (exact command in WP0).
- Studio App/transcript/RunComposer/PromotePanel: PASS, 8 tests (command above).
- `.venv/bin/python -m pytest tests/test_mcp_bridge.py tests/test_assistant_turn_token_auth.py tests/test_server_attempt_preflight.py -q`: PASS, 97 tests.
- `.venv/bin/python -m pytest tests/test_document_authority.py -q`: PASS, 7 tests.
- `git diff --check`: PASS. File-scope review confirms only the implementation
  plan, decision draft/index and documentation-authority test changed.

Acceptance:
- All five WP0 criteria satisfied: current code/tests mapped, each core flow
  has a seam/gap, decision gates audited, runtime unchanged, next packet READY.
- No new runtime tests were needed; existing behavior was verified with offline
  tests and the documentation link check was extended for the new artifacts.

Plan changes:
- WP0 → DONE; WP1 → READY (only next safe implementation packet).
- WP4 → BLOCKED on the existing named DG-SSH-HOSTKEY gate. WP12 retains its
  dependency on WP4; no Internet-facing host-identity acceptance is waived.
- All other packet dependencies and acceptance criteria retained.

Remaining risk:
- Host identity requires a human ruling on the draft before WP4 implementation.
- Context usage/cache/compaction semantics are unverified at the deployed SDK;
  WP4A uses explicit unavailable unless a valid source can be established.
- Existing nested-anchor React warning and fragmented evidence/error
  projections remain documented gaps, not regressions introduced by WP0.
- Local tests establish repository behavior only; no production validation,
  flag enablement, commit, deployment or rollout occurred.

## 2026-09-20 — WP1 Overview + Compute Information Architecture

Status: DONE

Implemented:
- Added Overview as the authenticated operational landing page with independent
  Project attention, active Run activity, Compute health and recent Activity
  projections over existing APIs.
- Established the V0.1 primary navigation and Compute vocabulary while keeping
  existing Runs, datasets, approvals and low-level controls under Advanced.
- Reworked the existing Servers page into normal Compute cards plus the
  unchanged advanced Server management surface. Preserved Server names,
  revisions, history, API payloads and all governed actions.
- Added explicit loading, empty, partial/forbidden, retry, cached-update,
  offline-browser, unknown, stale, disconnected, blocked and disabled UI
  states without adding backend state or readiness claims.
- Added targeted Studio tests and updated the `studio_ui_v1` capability ledger
  evidence. No backend, persistence, authorization or execution code changed.

Validation:
- `npm test --prefix studio`: PASS, 44 tests in 14 files.
- `npm run build --prefix studio`: PASS, TypeScript check and Vite production
  build (126 modules).
- `.venv/bin/python scripts/frontend_smoke.py --require-studio`: PASS.
- `.venv/bin/python -m pytest tests/test_projects_legacy_v2_api.py tests/test_jobs_v2_api.py tests/test_infrastructure_v2_api.py tests/test_events_audit_v2_api.py tests/test_project_roles_v2.py tests/test_server_config_api.py tests/test_studio_static.py -q`: PASS, 264 tests.
- `.venv/bin/python -m pytest tests/test_document_authority.py -q`: PASS.
- `git diff --check`: PASS.

Acceptance:
- All seven WP1 acceptance criteria are satisfied by implementation plus the
  targeted navigation, Overview, Compute-state, safety-copy and contract tests.
- Connected remains a health projection and is never presented as scheduling
  Ready. Rental/owned ownership remains unknown until an authoritative field
  exists; GPU and hardware-host labels use only current config/observation data.

Plan changes:
- WP1 → DONE.
- WP2 → READY as the only next packet. It reuses the completed Compute surface
  for guided custom-port onboarding and classification input.
- WP3 and all other packets retain their existing statuses and dependencies;
  WP4 remains BLOCKED on DG-SSH-HOSTKEY.

Remaining risk:
- The current aggregate execution API lists numeric Jobs, not every standalone
  typed ExecutionPlan. Overview labels this limitation and never invents a
  plan ID; richer inline typed Run coverage remains WP7/WP8.
- ServerConfig has no authoritative rental/owned ownership field. WP1 exposes
  this as unavailable; WP2 owns guided classification input without a provider
  provisioning subsystem.
- Validation was offline/local only. No production data, service, flag,
  credential, commit or deployment was touched.

## 2026-09-21 — WP3 Compute Readiness Projection

Status: DONE

Implemented:
- Combined approved target/version/instance eligibility with the latest durable
  monitor observation in the existing Project Workspace projection.
- Added conservative `ready`, `not_ready`, `blocked`, and `unknown` readiness
  projections using existing reason codes and the canonical observation
  freshness constant.
- Limited normal Run, Experiment, and Quick Command selectors to ready targets
  while preserving visible reasons and remediation hints for excluded targets.
- Added focused backend and Studio coverage, including the no-ready-target
  fail-closed Quick Command state.

Validation:
- Focused readiness backend tests: PASS, 28 tests.
- Affected backend contract suite: PASS, 168 tests.
- Studio tests: PASS, 50 tests in 15 files.
- Studio production build and frontend smoke: PASS.
- Repository full offline gate: PASS, 3972 tests.
- `git diff --check`: PASS.

Acceptance:
- All four WP3 criteria pass. Missing/stale evidence never becomes ready;
  ineligible targets are absent from normal target controls but their reasons
  remain visible.
- The implementation reads only persisted evidence. It adds no remote command,
  probe, lifecycle state, approval kind, authorization semantic, or host-key
  behavior.

Plan changes:
- WP3 → DONE.
- WP4A → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- The current closed monitor/preflight evidence does not substantiate every
  desired tmux/git/rsync/writable-path check. Those checks remain unknown and
  were not invented as a new validation mechanism.
- Validation was offline/local only. No production data, service, credential,
  deployment, or rollout was touched.

## 2026-09-21 — WP4A AI Workspace + Context Usage

Status: DONE

Implemented:
- Made the existing Project AI composer reliable for multiline input, keyboard
  send, IME composition, explicit interaction states, and exact-payload retry
  after a failed send.
- Added a context summary and details drawer that accepts only substantiated
  occupancy/window telemetry, labels explicit estimates, and invalidates older
  telemetry after a model change.
- Preserved the existing durable AgentSession event timeline, permission cards,
  provider adapter, workspace isolation, and checkpoint/promotion boundaries.

Validation:
- Focused composer/context/transcript tests: PASS, 10 tests.
- Studio tests: PASS, 58 tests in 17 files.
- Studio production build and frontend smoke: PASS.
- AgentSession/gateway/API affected suite: PASS, 68 tests.
- Repository full offline gate: PASS, 3972 tests.
- `git diff --check`: PASS.

Acceptance:
- All eight WP4A criteria pass. Unknown limits and opaque category totals remain
  explicitly unavailable; valid estimates and provider reports are visibly
  distinguished.
- No Agent tool authority, approval, authorization, lifecycle, provider,
  compaction, ProjectVersion, or Compute execution semantics changed.

Plan changes:
- WP4A → DONE.
- WP5 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- Current provider events may omit a trustworthy occupancy/window pair; the UI
  intentionally reports context usage unavailable in that case.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-21 — WP5 Typed Agent → Run Application Seam

Status: DONE

Implemented:
- Added AgentSession-scoped typed Run preview and request endpoints bound to the
  session's durable Project and the authenticated requester.
- Refactored Project and AgentSession routes through the same ExecutionPlan v2
  preview/request helpers, unit of work, digest, and idempotency path.
- Registered both routes under the existing `PROJECT_OPERATE` AgentSession
  authorization resource and updated the OpenAPI contract snapshot.

Validation:
- Focused seam/ExecutionPlan/authorization/OpenAPI suite: PASS, 61 tests.
- Broader AgentSession/idempotency/authorization suite: PASS, 85 tests.
- Studio tests/build and frontend smoke: PASS, 58 tests in 17 files.
- Repository full offline gate: PASS, 3976 tests.
- Ruff and `git diff --check`: PASS.

Acceptance:
- All five WP5 criteria pass. The promoted ProjectVersion requirement and all
  resolution/revalidation semantics remain in the existing resolver.
- Requests create the existing pending `execution_plan_v2` approval and no Job;
  Job materialization remains post-approval.

Plan changes:
- WP5 → DONE.
- WP6 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- The Development Agent tool transport does not call this seam until WP6.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-21 — WP6 Development Agent Tool Integration

Status: DONE

Implemented:
- Added the governed `request_run` MCP tool to the Claude Agent SDK session
  path and kept the control-plane and runner bridge copies byte-identical.
- Required the exact WP5 Run submission fields, preview digest, and idempotency
  key; forwarded them only to the AgentSession-scoped Run request route.
- Registered the existing `PROJECT_OPERATE` action and exact route so the
  derived assistant turn-token allowlist remains the enforcement source.
- Returned the bounded platform approval response without adding approval,
  direct Job, shell, SSH, credential, or execution authority.

Validation:
- Focused MCP/mirror/authorization/turn-token/AgentSession/gateway/permission
  suite: PASS, 130 tests.
- Capability-ledger and document-authority contracts: PASS, 12 tests.
- Studio tests/build and frontend smoke: PASS, 58 tests in 17 files.
- Repository full offline gate: PASS, 3978 tests.
- Ruff, bridge byte comparison, and `git diff --check`: PASS.

Acceptance:
- All four WP6 criteria pass. The tool reaches the same ExecutionPlan v2
  request helper used by Studio and can create only the existing pending
  `execution_plan_v2` approval.
- Job materialization, approval decisions, scheduling, and Compute execution
  remain outside the Agent.

Plan changes:
- WP6 → DONE.
- WP7 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- The typed request requires a current preview digest; stale inputs are rejected
  by the existing WP5 revalidation path and must be previewed again.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-21 — WP7 Session Inline Ready-to-Run Card

Status: DONE

Implemented:
- Added a timeline-native Ready-to-Run card triggered only by the persisted
  successful `request_run` tool result and its platform-issued approval id.
- Loaded the authorized approval detail and rendered only its verified immutable
  ExecutionPlan v2 review contract as the card's normal information.
- Reused the existing v2 approval decision route for the primary `Run` action;
  the card cannot submit a second Run request or create a Job directly.
- Kept identifiers, digests, and raw tool data under Advanced details, omitted
  ungrounded estimates, and retained a link to Runs history.
- Added explicit loading, malformed, unavailable, pending, approved, and
  rejected states while preserving generic rendering for other tools.

Validation:
- Focused Ready-to-Run/transcript/approval/session/composer suite: PASS, 24 tests.
- Full Studio suite: PASS, 63 tests in 18 files.
- Studio production build and frontend smoke: PASS.
- Repository full offline gate: PASS, 3978 tests.
- Document contracts and `git diff --check`: PASS.

Acceptance:
- All four WP7 criteria pass. A durable platform result produces the card even
  without matching assistant prose; assistant text alone cannot produce it.
- The user reviews the promoted version, target, parameters, and readiness in
  Project context and presses `Run` through the existing human approval path.

Plan changes:
- WP7 → DONE.
- WP8 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- The card intentionally shows estimates only if the verified contract gains an
  explicitly grounded estimate; current contracts normally omit that field.
- Browser screenshot automation was unavailable in the local toolchain;
  rendered component tests, production build, and frontend smoke passed.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-21 — WP8 Inline Run Monitoring

Status: DONE

Implemented:
- Recovered the durable Product Run id from verified approved Run detail and
  added an inline monitor that polls the existing Product Run v2 projection.
- Projected canonical execution state, remote liveness, verified target,
  authoritative elapsed time, bounded metrics and log tail, and result
  collection/artifact metadata as separate facts.
- Preserved the last canonical state during transport failure and explicitly
  presented unknown remote liveness without converting it to execution failure.
- Added the Product Run stop-request action and reused the existing v2 approval
  decision card; no direct Job stop/cancel route is reachable from the monitor.
- Kept bounded raw Run, timeline, metrics, log, and artifact data in Advanced
  details.

Validation:
- Focused monitor/Ready-to-Run/session/approval suite: PASS, 22 tests.
- Full Studio suite: PASS, 69 tests in 19 files.
- Studio production build and frontend smoke: PASS.
- Repository full offline gate: PASS, 3978 tests.
- Document contracts and `git diff --check`: PASS.

Acceptance:
- All four WP8 criteria pass. Project context reads the same Product Run v2
  truth as Runs, and state, liveness, collection, metrics, and transport remain
  distinct.
- Stop creates the existing pending `stop` approval and requires the existing
  human decision path before any execution operation is materialized.

Plan changes:
- WP8 → DONE.
- WP9 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- Target display depends on the verified ExecutionPlan review contract; stale or
  malformed detail is shown as unavailable instead of inferred.
- Browser screenshot automation remains unavailable in the local toolchain;
  rendered component tests, production build, and frontend smoke passed.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-21 — WP9 Result Evidence Access for Agent

Status: DONE

Implemented:
- Added bounded, read-only MCP tools for Product Run detail, metrics, artifact
  metadata, log tail, and comparison using existing v2 and Job evidence APIs.
- Resolved Job-scoped metrics and logs only through authorized Product Run
  detail, preserving the existing actor/project authorization path.
- Preserved platform-owned artifact provenance and represented absent evidence
  as structured unknown rather than inferred failure or fabricated telemetry.
- Extended the derived assistant-token route catalog for the two composed
  read paths without adding approval, execution, shell, credential, or download
  authority.

Validation:
- Focused MCP/evidence/authorization/Product Run suite: PASS, 192 tests.
- Repository full offline gate: PASS, 3986 tests.
- Full Studio suite: PASS, 69 tests in 19 files.
- Studio production build and repository frontend smoke: PASS.
- Document authority, Ruff, bridge mirror, and `git diff --check`: PASS.

Acceptance:
- All four WP9 criteria pass. The Agent can retrieve common analysis evidence
  through bounded structured responses while missing evidence remains unknown.
- Artifact results expose only the existing platform-owned metadata projection,
  including provenance and digest fields supplied by that projection.

Plan changes:
- WP9 → DONE.
- WP10 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- Evidence availability remains limited to what the existing Product Run and
  Job projections have collected; the bridge does not infer missing facts.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-21 — WP10 Result Analysis and Continue

Status: DONE

Implemented:
- Added a human-triggered result-analysis turn for terminal Runs inside the
  existing Project AgentSession, naming the exact verified Product Run and the
  bounded WP9 evidence tools to use.
- Rebuilt analysis state from persisted transcript events so reloads retain the
  workflow. Grounding requires successful structured evidence for the exact Run;
  assistant prose, wrong-Run evidence, or an incomplete turn cannot qualify.
- Added a Result / AI Analysis card with actual evidence tool/call references,
  unknown availability, the completed recommendation, and advanced references.
- Added a human-only Continue action that sends bounded prior-Run context through
  the same AgentSession message path and never calls a Run or approval endpoint.
- Bounded analysis to its originating turn so later Continue work cannot rewrite
  its evidence; failed or ungrounded analysis turns remain retryable.

Validation:
- Focused transcript/session/Run card suite: PASS, 22 tests.
- Full Studio suite: PASS, 73 tests in 19 files.
- Studio production build and repository frontend smoke: PASS.
- Repository full offline gate: PASS, 3986 tests.
- Document authority and `git diff --check`: PASS.

Acceptance:
- All four WP10 criteria pass. The UI identifies exact structured Run evidence,
  Continue requires a human click, and the next turn uses the same durable
  Project-bound session.
- Analysis and recommendation do not execute a Run. A subsequent Run can only be
  proposed through the existing governed `request_run` and human approval path.

Plan changes:
- WP10 → DONE.
- WP11 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- Analysis quality remains provider-dependent, but the UI only labels it grounded
  when the persisted turn contains successful exact-Run WP9 evidence.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-22 — WP11 UX Navigation Consolidation

Status: DONE

Implemented:
- Consolidated primary Studio navigation around Overview, Projects, Compute,
  Activity, and Settings while retaining Runs, Datasets, and Approvals under the
  existing Advanced section.
- Preserved `/servers` and `/events` as working aliases, including query strings.
- Removed default-path Job ids, Product Run UUIDs, raw audit source/action labels,
  and generated analysis markers from normal presentation while retaining their
  exact values in links or explicit Advanced disclosures.
- Simplified Activity to readable activity, result, and operator columns with the
  complete audit record available on demand.
- Kept Overview, Project AgentSession, Compute onboarding/readiness, and the
  Ask → Review → Run → Result → Continue loop on their existing governed seams.

Validation:
- Focused navigation/Overview/Activity/session suite: PASS, 15 tests.
- Full Studio suite: PASS, 76 tests in 20 files.
- Studio production build and repository frontend smoke: PASS.
- Repository full offline gate: PASS, 3986 tests.
- Document/static contracts: PASS, 12 tests.
- `git diff --check`: PASS.

Acceptance:
- All six WP11 criteria pass. Overview is the operational entry point; Projects
  carry the core loop; Compute and Activity are primary, readable destinations.
- Internal identifiers and complete audit/evidence payloads remain available for
  debugging only through links or explicit Advanced controls.

Plan changes:
- WP11 → DONE.
- WP12 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named DG-SSH-HOSTKEY decision.

Remaining risk:
- Project summary cards still rely on existing Project matrix data and do not
  fabricate last-Run or recommendation telemetry that the endpoint does not own.
- Validation was offline/local only. No provider, worker, production data,
  credential, deployment, or rollout was touched.

## 2026-09-22 — WP12 End-to-End Acceptance

Status: DONE

Implemented:
- Added a deterministic composed acceptance harness for the complete 18-step
  Project → Agent → promotion → typed Run → human approval → SSH/tmux → evidence
  → analysis → Continue loop.
- Proved custom-port target pinning, readiness, human separation, audit records,
  result collection, parsed metrics, bounded logs/artifacts, and actual WP9 MCP
  reads against the same persisted Run.
- Added explicit acceptance checks for ambiguous launch and result-collection
  failure so transport uncertainty never rewrites execution truth.
- Composed the existing Studio, apply-patch, validation, checkpoint, prompt,
  transcript, and UI suites for the stages and leakage surfaces outside the
  single-process backend journey.

Validation:
- Focused acceptance/change/validation suite: PASS, 63 tests.
- Cross-plane backend contract suite: PASS, 249 tests.
- Focused Studio acceptance suite: PASS, 22 tests in 5 files.
- Full repository backend gate: PASS, 3989 tests.
- Full Studio suite: PASS, 76 tests in 20 files.
- Ruff, mypy, Studio production build, frontend smoke, TestClient lifecycle
  smoke, Node primitives smoke, and test collection: PASS.
- `git diff --check`: PASS.

Acceptance:
- All 18 steps have live journey proof or named direct component/domain proof;
  all five WP12 acceptance criteria pass.
- The test is explicitly offline. Fake SSH validates the governed custom-port
  target and command contract without claiming contact with a rental host or
  host-identity acceptance.

Plan changes:
- WP12 → DONE.
- WP13 → READY as the sole next dependency-satisfied packet.
- WP4 remains BLOCKED on the unresolved named `DG-SSH-HOSTKEY` decision.

Remaining risk:
- Internet-facing SSH host identity remains outside V0.1 acceptance evidence
  until the named decision is supplied and WP4 can proceed.
- No provider, worker, production data, credential, deployment, or rollout was
  touched.

## 2026-09-22 — WP13 Documentation and Capability Closeout

Status: DONE

Implemented:
- Reconciled the Program Board and every packet section: WP0–WP3 and
  WP4A–WP13 are complete with evidence; WP4 remains explicitly blocked on
  `DG-SSH-HOSTKEY`.
- Updated the Capability Ledger date and current repository/Studio validation
  counts without changing default, pilot, canary, or production posture.
- Closed the live UX acceptance checklist and independently rechecked the
  Architecture Definition of Done against completed packet evidence and the
  WP12 composed acceptance harness.
- Rechecked the V0.1 Definition of Done without changing the target
  architecture, protected decisions, runtime settings, migrations, or runbooks.

Validation:
- WP12 composed end-to-end acceptance harness: PASS, 3 tests.
- Capability/document authority/static contracts: PASS, 30 tests.
- Full repository backend gate: PASS, 3989 tests.
- Full Studio suite: PASS, 76 tests in 20 files.
- Ruff, mypy, Studio production build, frontend smoke, TestClient lifecycle
  smoke, Node primitives smoke, test collection, and `git diff --check`: PASS.

Acceptance:
- All four WP13 criteria pass. Repository status, capability evidence, and the
  live V0.1 Definition of Done now match the merged implementation.
- The accepted scenario remains deterministic and offline; it proves the
  governed custom-port SSH/tmux contract but does not claim Internet host
  identity or real-environment canary evidence.

Plan changes:
- WP13 → DONE.
- No packet is promoted to `READY`; the V0.1 implementation program is closed.
- WP4 remains BLOCKED on the unresolved named `DG-SSH-HOSTKEY` decision.

Remaining risk:
- `DG-SSH-HOSTKEY` must be decided before Internet-facing host fingerprint
  trust/pinning behavior can be implemented or claimed.
- `RB-LAUNCH-001` still requires its current-candidate real-environment window
  before release-gate promotion; this program performed no deployment.
