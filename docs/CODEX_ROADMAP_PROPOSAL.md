# Codex Roadmap Proposal

> Proposed from repository commit `87f10c5` on 2026-07-12.  
> Status at proposal time: review proposal only; `PLAN.md` and canonical
> invariants remained unchanged until explicitly approved.
> 2026-07-13 addendum: the user explicitly approved only the Slice 7 revisions
> to `INV-APPROVAL-1`/`INV-APPROVAL-5` and `Authlib>=1.7,<2.0`. Slice 7 passed
> dependency-complete validation on 2026-07-13; it was not deployed. The Node
> Agent/`INV-SSH-1`, enforcement, PostgreSQL and
> other later-roadmap decisions remain unapproved.

## 1. Direction and outcome

Evolve the existing Dispatch Center incrementally into an internal-team control
plane with:

- organizational OIDC authentication for humans;
- project-scoped Viewer, Operator, and Admin authorization;
- separate, scoped service identities for MCP, LLM, Node Agents, and automation;
- the existing centralized Codex Coding Runner;
- Node Agent as the eventual primary execution channel; and
- SSH retained indefinitely as a compatibility and emergency backend.

This is not a rewrite. Existing project, approval, job, audit, SSH, Codex,
dataset, result, and LLM behavior remains operational while narrow interfaces
and additive schema are introduced.

## 2. Historical `PLAN.md` disposition

| Historical milestone | Actual state at `87f10c5` | Proposed disposition |
|---|---|---|
| Phase 0 — baseline and migration safety | Substantially complete: backup/restore scripts, endpoint/table inventory, additive migration pattern, and draft project/data/scheduler invariants exist. A dependency-complete full test and restore baseline is still absent. | Treat as a permanent release gate, not a one-time phase. Complete the clean full-suite and restore drill first. |
| Phase 1 — Project Control Plane | Partially complete. All five section 14 slices are committed: UUID dual-write, instance reconciliation, candidate linking, ProjectVersion, and project detail integration. Full UUID adoption, runtime profiles/bindings/templates, and revision-pinned normal runs are not complete. | Keep the remaining outcomes, but place team identity first and the immutable execution contract before completing the planner-driven project workflow. |
| Phase 2 — Dataset / Artifact Plane | Legacy registry, manifest, rsync, verification, cache, and data-locality behavior exist. Snapshot/shard/hash/transfer models do not. | Remains valid. Reorder after the execution contract and Node Agent canary so it is implemented once against stable transport and attempt boundaries. |
| Phase 3 — JobSpec, Planner, reproducible execution | Approval-time sync/setup planning and centralized Codex execution exist. Unified JobSpec, canonical ExecutionPlan hash, RunAttempt, and ExecutionBackend do not. | Split and move forward: first establish immutable plans and an SSH backend seam, then add Node Agent. Preserve raw-command compatibility. |
| Phase 4 — Resource Scheduler v2 | Current scheduler provides deterministic tags, pinning, data locality, priority/FIFO, and one-job-per-machine behavior. Resource freshness/reservations/reason codes are missing. | Remains valid after JobSpec and artifact foundations. Do not infer GPU sharing from existing fields. |
| Phase 5 — Results and Experiment Intelligence | Result pull, coding result metadata, manual records, and merged project timeline exist. Artifact manifests, structured metrics, comparisons, and retryable collection are missing. | Remains valid and should reuse the existing timeline and result hooks. |
| Phase 6 — GPT Project Command Center | Optional LLM/vLLM/MCP layers and many read/request tools exist, with strong forbidden-tool boundaries. Project-centric planning and evidence-grounded comparison are incomplete. | Remains valid after identity, authorization, and project/run models. Keep all LLM mutation request-only. |
| Phase 7 — Trusted-team Identity and Operations | Backup/restore foundations exist. Individual identity, RBAC, actor-aware audit, revocation, and project authorization do not. | Reorder identity and authorization to the first product change. Operations gates remain continuous work. |
| Phase 8 — Multi-tenant | Not implemented. | Keep deferred as a separate architecture project. Internal-team authorization must not be marketed as hostile-tenant isolation. |

### Completed short milestones

The five immediate section 14 implementation slices are complete:

1. Project UUID identity migration;
2. instance reconciliation;
3. candidate linking;
4. canonical version service; and
5. project overview/detail integration.

The Central Codex Coding Runner described by older Claude-era milestone comments
is also implemented. The new roadmap must harden and migrate it, not schedule a
second implementation.

### Obsolete or superseded items

- A shared system token or `single_owner` model is obsolete as the desired end
  state. It remains only as a temporary rollback mechanism.
- SSH as the future primary execution channel is superseded by Node Agent.
- Agentless workers as a permanent rule is superseded in direction, but remains
  protected until `INV-SSH-1` is explicitly revised.
- A vague future sandbox backend is superseded by the concrete Node Agent
  channel; sandboxing inside a workload remains a separate concern.
- Any milestone to build a Central Codex Runner is complete and therefore
  obsolete as future work.
- Removing SSH after Node Agent rollout is not part of this roadmap.
- Big-bang replacement of the monolith, database, scheduler, or execution system
  remains explicitly rejected.

## 3. Target security and execution model

### 3.1 Human and service identity

- Humans authenticate through the organization's OIDC provider using
  Authorization Code with PKCE, state, and nonce validation.
- OIDC proves identity; project memberships live in the Dispatch Center DB.
- Browser sessions are server-side, expiring, revocable, and use secure,
  HTTP-only, same-site cookies.
- Automation uses service accounts with hashed, scoped, expiring, individually
  revocable bearer tokens. Raw tokens are shown only at issuance.
- Node Agents use separate node identities and credentials, not human or general
  MCP credentials.
- The existing shared token maps temporarily to an audited `legacy-admin`
  principal behind an explicit compatibility switch.

OIDC requires narrow unauthenticated login/callback endpoints. At proposal
time, adding them required an explicit `INV-APPROVAL-5` revision. That
prerequisite was approved on 2026-07-13 for exactly `GET /auth/login` and
`GET /auth/callback`; `GET /auth/me`, `POST /auth/logout`, every other method or
application API, and every other prefix remain authenticated by default.

### 3.2 Project authorization

The initial project roles are:

| Role | Allowed behavior |
|---|---|
| Viewer | View an authorized project's metadata, instances, versions, datasets, jobs, coding runs, results, and timeline. |
| Operator | Viewer permissions plus create ordinary/coding/stop requests and write project experiment records. Operators cannot manage membership or approve high-risk actions. |
| Admin | Operator permissions plus manage project metadata and membership, perform project lifecycle administration, and approve project actions subject to separation of duties. |

A separate global `platform_admin` capability manages servers, Node Agents,
global service accounts, platform policy, backup/restore operations, and
cross-project administration.

Policy requirements:

- project collection endpoints filter out unauthorized projects;
- a direct request for a hidden project returns 404 to avoid existence leakage;
- an authenticated principal who can see a project but lacks an action receives
  403;
- service accounts receive explicit scopes and cannot approve requests;
- LLM/MCP identities remain read/request-only;
- approval records persist requester and approver actor IDs; and
- code, deploy, Git initialization, server, membership, credential, execution
  backend, and Node trust changes require a different human approver.

Ordinary `enqueue` and `stop` may retain policy-controlled direct/automatic
approval. The existing invariant limiting auto-approval kinds to `enqueue` and
`stop` remains unchanged.

### 3.3 Immutable approval and execution identity

- Every approval stores a canonical payload and SHA-256 payload/plan hash.
- Approval-time revalidation may reject stale state but cannot rewrite a target,
  command, code version, dataset snapshot, backend, or attempt payload.
- `JobSpec` describes the requested work; `ExecutionPlan` fixes material
  preparation and target decisions; `RunAttempt` identifies one launch through
  one backend.
- Old rows remain readable and are explicitly marked `legacy_unpinned` or
  `verification_unknown` where historical facts are absent.

### 3.4 Node Agent channel

The Node Agent target is a small, non-root, persistent worker service that:

- initiates outbound authenticated HTTPS polling; workers expose no inbound
  control port;
- polls for a leased attempt, atomically acknowledges it, and persists the
  attempt identity before launch;
- writes approved command bytes to a file and invokes a launcher containing only
  validated identifiers, preserving the SSH backend's non-interpolation safety;
- reports heartbeats, launch state, terminal status, logs, and artifact metadata;
- survives control-plane or agent restart without duplicating an acknowledged
  attempt; and
- operates only in configured work/data/result roots as a non-root account.

An expired heartbeat or unreachable agent means `unknown`, not `failed`. The
control plane must not dispatch the same attempt through SSH when the agent may
have launched it.

Before any Node Agent implementation, explicitly replace the agentless portion
of `INV-SSH-1` with backend-specific invariants: existing `INV-SSH-*` rules remain
binding on SSH, while new `INV-NODE-*` rules define agent identity, leases,
non-interpolation, reconciliation, and rollback.

## 4. Proposed phased roadmap

### Phase 0 — Reproducible baseline and invariant decisions

Deliverables:

- create an isolated dependency-complete environment from `requirements.txt`;
- run the complete test suite and current static invariant gate;
- exercise backup and restore against temporary state, including row counts,
  hub refs, audit/config files, and optional data manifests;
- inventory every route into an authorization action and project/global scope;
- draft `INV-AUTH-*`, `INV-AUTHZ-*`, `INV-EXEC-*`, and `INV-NODE-*`; and
- obtain explicit approval for the required `INV-APPROVAL-5` and `INV-SSH-1`
  revisions before corresponding code is written.

Validation:

- `pytest -q` collects and passes in the isolated environment;
- the existing static check script passes unchanged;
- a backup restored into a temporary directory has matching SQLite row counts,
  hub refs, and manifest inventory; and
- no test or validation contacts a real worker or identity provider.

Rollback: documentation and invariant drafts have no runtime effect. If a
baseline migration fixture exposes a defect, stop before product changes.

### Phase 1 — Internal identity and authorization

Deliverables:

- additive actor, OIDC identity, session, service-account token, and project
  membership schema;
- OIDC login/callback/logout/current-actor interfaces;
- hashed service-token issuance, expiry, rotation, and revocation;
- a `RequestContext` propagated through REST, WS, agent, MCP, approval, and audit
  paths;
- one centralized, deny-by-default Viewer/Operator/Admin/platform-admin policy;
- actor-aware approval and audit records plus canonical payload hashes;
- shadow authorization followed by an explicit enforcement switch; and
- temporary legacy-token compatibility, visibly audited and disabled by default
  after enforcement proves stable.

Validation:

- fresh and legacy DB migrations preserve all old rows;
- fake-provider tests cover OIDC state, nonce, PKCE, callback replay, issuer,
  audience, expiry, logout, and session revocation;
- raw service tokens are never stored or logged; expiry and revocation take
  effect immediately;
- a complete role/action matrix covers every route and registered LLM/MCP tool;
- cross-project tests prove collection filtering, 404 hiding, 403 action denial,
  and no indirect leakage through jobs, approvals, audit, results, or coding
  runs;
- service identities cannot approve; and
- high-risk self-approval and auto-approval are rejected.

Rollback:

- switch authorization from `enforce` to `shadow`, then enable the legacy token
  only if needed;
- invalidate new sessions/service tokens when rolling back identity code;
- run the previous application against the additive schema; and
- restore a backup only if schema/data integrity failed. Normal rollback never
  deletes identity tables or rewrites audit history.

### Phase 2 — Immutable execution contract and SSH backend seam

Deliverables:

- additive JobSpec, ExecutionPlan, RunAttempt, backend, payload-hash, lease, and
  backend-evidence fields/tables;
- legacy adapters for current job and coding-run rows;
- an `ExecutionBackend` contract for prepare, launch, inspect, stop, collect, and
  cleanup;
- an SSH implementation that wraps the existing SFTP/tmux/sentinel code without
  behavioral change; and
- per-job/per-node backend selection with SSH as the default.

Validation:

- contract tests run the same lifecycle cases through the existing FakeSSH;
- golden tests show current job commands, paths, state transitions, audit events,
  and unreachable behavior remain compatible;
- plan-hash mismatch and stale material state invalidate approval rather than
  mutate its payload;
- crash-window tests cover DB-before-launch, launch failure, launch ambiguity,
  restart, stop, collection, and cleanup; and
- every legacy row remains readable without fabricated revision, snapshot, or
  hash data.

Rollback:

- set the default and all new routing to `ssh`;
- leave additive attempt/plan rows intact;
- let every in-flight attempt reconcile through the backend that launched it;
  and
- retain old APIs and legacy adapters until a later separately approved removal.

### Phase 3 — Node Agent canary for ordinary jobs

Deliverables:

- a versioned Node Agent package and service definition running non-root;
- node enrollment, hashed/revocable node credentials, rotation, and inventory;
- outbound poll, lease, acknowledgement, heartbeat, inspect, stop-request,
  terminal-result, log, and artifact-metadata protocol;
- durable agent-side attempt identity and restart reconciliation;
- per-node `ssh|node` routing and an operator-visible execution backend; and
- canary eligibility limited to designated non-production ordinary jobs.

Validation:

- protocol and agent tests run locally with fake control-plane/worker interfaces;
- forced cases cover duplicate poll, duplicate acknowledgement, agent restart,
  control-plane restart, disconnect before/after launch, stale lease, terminal
  upload retry, stop, and malformed/unauthorized payloads;
- command text cannot enter the launcher shell through interpolation;
- an unreachable agent never becomes failed merely because it is unreachable;
- at least 100 non-production ordinary jobs run across at least two nodes over
  seven consecutive days with zero duplicate launches, false disconnect
  failures, or lost terminal results; and
- an observed rollback drill routes new work to SSH while an uncertain Node
  attempt remains unduplicated.

Rollback:

- stop assigning new attempts to Node Agent and select SSH for new work;
- keep agents polling until all acknowledged attempts reach a known terminal
  state or are explicitly resolved by an operator;
- never automatically re-launch an uncertain Node attempt through SSH; and
- stop/remove an agent only after its active-attempt count is zero. Schema and
  recorded evidence remain intact.

### Phase 4 — Node Agent primary and Codex Runner migration

Deliverables:

- promote Node Agent per proven node while retaining explicit SSH selection;
- add agent liveness, version, queue, error, lease-age, and reconciliation
  operational views;
- migrate the single Central Codex Runner only after ordinary-job canary gates
  pass; and
- preserve the runner's existing approval, worktree, Codex invocation, test,
  secret-scan, bundle, downstream-use, result, and cleanup contracts.

Validation:

- each promoted node independently passes the Phase 3 gate;
- 20 Node-backed Codex canaries cover success, Codex failure, detected test
  failure, secret violation, bundle creation/use, cleanup, agent restart,
  control-plane restart, and network interruption;
- persisted `coding_runs`, audit events, bundles, and user-visible APIs match the
  SSH-backed contract; and
- an observed emergency rollback returns new Codex tasks to the centralized
  runner's SSH backend without duplicating in-flight tasks.

Rollback: backend selection for new ordinary and coding attempts returns to SSH.
In-flight Node attempts remain Node-owned until reconciled. SSH credentials,
configuration, tests, and operational documentation remain maintained
indefinitely.

### Phase 5 — Complete project lifecycle and reproducible planning

Deliverables:

- complete UUID-based project references with dual-read/dual-write compatibility;
- managed-project state, hub metadata, runtime profiles, dataset bindings, and
  templates as needed by the run workflow;
- ProjectVersion-pinned managed deployments and runs;
- a project run planner that fixes code, data, target/backend, setup, resources,
  and risks before approval; and
- explicit legacy-unpinned and unreproducible representations.

Validation:

- migration row counts and identity mappings match before/after;
- candidate linking never auto-merges by name;
- dirty/diverged instances are not overwritten;
- unreachable instances remain unknown;
- a stale code revision, target, data binding, backend, or command invalidates
  the plan; and
- historical rows lacking facts display unknown/legacy rather than invented
  values.

Rollback: keep name-based APIs and dual-write adapters, disable the new planner
for new requests, and route compatible requests through the legacy flow. Do not
drop UUID/version data.

### Phase 6 — Artifact/data plane, Scheduler v2, and result intelligence

Deliverables:

- local ArtifactStore, streaming manifests, deterministic shards, immutable
  snapshots, hashes, replica markers, and resumable transfer leases;
- resource capabilities, observation freshness, exclusive-node reservations,
  deterministic eligibility/scoring, and reason codes;
- result manifests, retryable collection, structured metrics, and run
  comparison; and
- legacy dataset/result adapters with honest verification levels.

Validation:

- corruption, missing marker, interrupted transfer, duplicate lease, restart,
  insufficient space, and atomic-publication tests;
- no job uses an unready replica or stale resource observation;
- no resource oversubscription or duplicate claim;
- artifact collection failure never changes execution success/failure; and
- comparisons cite recorded revisions, snapshots, metrics, and artifacts.

Rollback: select the legacy rsync/cache or result-pull path for new compatible
operations. Never mutate or delete an already published snapshot. Keep new
metadata readable even when the legacy path is active.

### Phase 7 — Project-centric GPT and operational readiness

Deliverables:

- authorized project overview, planning, scheduling explanation, drift,
  comparison, and evidence-grounded analysis tools;
- scoped MCP/vLLM service accounts and project filtering;
- capacity, scheduler/monitor/agent liveness, authorization-denial, backup, and
  recovery monitoring; and
- pagination, request limits, backpressure, token rotation, and recurring restore
  drills.

Validation:

- every asserted fact references a real project/run/artifact/metric/log source;
- missing evidence produces unknown, not a guessed conclusion;
- prompt injection still cannot approve, reject, run shell, or access another
  project;
- disabling every LLM/MCP adapter leaves scheduling, execution, reconciliation,
  and collection operational; and
- a documented restore drill meets the agreed recovery objective.

Rollback: disable individual AI adapters or service tokens without affecting the
control plane. Operational features must be additive and must not alter job
truth when unavailable.

### Phase 8 — Multi-tenant only by separate approval

PostgreSQL, OIDC workspace mapping, RLS, hostile-tenant storage/compute/network/
secret isolation, dedicated worker pools, and cross-tenant security testing are
a separate architecture project. No preceding phase may claim multi-tenant
isolation.

## 5. First bounded Codex Goal

### Goal 1: Actor Identity and Authorization Shadow Mode

Implement individual identity and exercise the complete authorization policy
without changing whether current application requests are allowed.

In scope:

1. Additive tables for actors, OIDC subject bindings, server-side sessions,
   service accounts/tokens, and project memberships.
2. OIDC login, callback, logout, and current-actor interfaces using a fake
   provider in tests.
3. Hashed, expiring, revocable service-token issuance; raw secrets are returned
   once and never stored or logged.
4. `RequestContext` for human, service, and legacy principals across HTTP, WS,
   MCP/agent requests, approval creation/decision, and audit.
5. A centralized action catalog and complete Viewer/Operator/Admin/
   platform-admin policy evaluator.
6. Shadow evaluation for every existing route and tool. A would-be denial writes
   a structured `authorization_shadow_denied` audit/metric event but preserves
   the current response and side-effect behavior.
7. New audit records include actor identity. Historical audit lines and rows are
   not rewritten.
8. A temporary feature switch maps the current shared token to the explicitly
   labeled `legacy-admin` actor.

Out of scope:

- enforcing project denials;
- high-risk separation-of-duties enforcement;
- canonical approval/plan hashes;
- changing web direct execution or auto-approval behavior;
- changing job, scheduler, SSH, Codex Runner, project, dataset, or result
  behavior; and
- implementing any Node Agent code.

Acceptance criteria:

- fresh and legacy DB tests prove additive migration and preservation of all
  existing rows;
- fake OIDC tests cover state, nonce, PKCE, invalid/replayed callback, expiry,
  logout, and revoked session;
- service-token tests prove hashing, single display, scope, expiry, revocation,
  and redaction;
- a route/tool coverage test fails if any new interface lacks an authorization
  action mapping;
- the role matrix produces the expected allow/deny decision for project and
  platform actions, including cross-project cases;
- shadow mode produces the same HTTP/WS responses and mutations as the baseline
  while recording would-deny decisions;
- focused auth/migration/audit tests, the full suite, and static invariant checks
  pass without real network or worker access; and
- documentation explains configuration, bootstrap platform admin, legacy-token
  rollback, and the fact that shadow mode is not enforcement.

Rollback requirements:

1. Disable OIDC/service-token entrypoints and keep authorization in shadow/off.
2. Enable the legacy shared-token principal if operators need the old access
   path.
3. Revoke/invalidate new sessions and tokens before running old code.
4. Run the previous application version against the additive schema; it ignores
   the new tables.
5. Restore the pre-migration backup only if schema/data integrity validation
   fails. Do not delete identity tables or rewrite append-only audit history for
   an ordinary application rollback.

The original Goal 1 proposal did not itself authorize changing
`INV-APPROVAL-5`. The user separately supplied that approval on 2026-07-13,
limited to the two exact GET handshake routes above, and separately confirmed
the closed authentication-bookkeeping treatment under `INV-APPROVAL-1`. This
does not authorize enforcement or any broader route, invariant, dependency, or
roadmap change.

## 6. Release-wide validation and rollback rules

Every implementation goal must provide:

- the exact baseline commit and feature flags;
- affected schema, APIs, roles, approval kinds, states, and invariants;
- focused tests, related subsystem tests, full-suite result, and static-gate
  result;
- fresh/legacy migration evidence and row-count checks;
- backup location and temporary restore verification;
- success, rejection, unavailable dependency, stale state, restart, retry, and
  idempotency coverage;
- an operator-observed rollback drill before promotion; and
- a list of capabilities that remain partial or must not be claimed.

Global rollback rules:

- migrations are additive; rollback normally switches readers/routing/flags and
  does not reverse schema;
- an attempt is always reconciled by the backend that may have launched it;
- fallback applies to new work only when an existing side effect is impossible;
- unknown or unreachable state is never converted to failure merely to simplify
  rollback;
- approved payloads and historical evidence are never rewritten;
- SSH remains tested and operable as compatibility/emergency transport; and
- no rollout or validation in this repository contacts production systems or
  uses production credentials.

## 7. Assumptions fixed by this proposal

- Humans authenticate with organizational OIDC/SSO.
- Project memberships and roles are stored in the application database.
- Initial project roles are Viewer, Operator, and Admin, with a separate global
  platform-admin capability.
- High-risk actions require a different human approver.
- Node Agents use outbound authenticated HTTPS polling and run non-root.
- Ordinary jobs prove the Node Agent protocol before the Central Codex Runner
  migrates.
- Node Agent becomes the primary channel per proven node, not through a global
  big-bang switch.
- SSH is retained indefinitely for compatibility and emergency use.
- Multi-tenancy remains out of scope.
- `PLAN.md` remains untouched until this proposal is explicitly approved as its
  replacement or successor.
