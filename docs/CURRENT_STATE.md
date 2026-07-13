# Dispatch Center Current State

> Audit snapshot: 2026-07-12  
> Repository baseline: commit `87f10c5` (`pre-codex-handoff-2026-07`) on
> branch `codex/baseline-audit`  
> Status: review document; it does not replace `PLAN.md` or modify a protected
> invariant.

## 0. Post-baseline Goal 1 addendum (2026-07-13, validated; not deployed)

The sections below remain the audit snapshot of commit `87f10c5`; they are not
silently rewritten as current release claims. After that snapshot, Goal 1
Slices 1–6 added the identity persistence foundation, RequestContext transports,
actor-aware approvals/audit, authorization catalog and shadow observation, and
approval-gated service identity/membership management. The verified Slice 7
starting baseline is commit `54f42bb`: **1629 tests passed** and the static
invariant gate passed before OIDC work began.

On 2026-07-13 the user explicitly approved the two previously blocked OIDC
prerequisites: `INV-APPROVAL-5` now permits only `GET /auth/login` and
`GET /auth/callback` as additional unauthenticated handshake routes, and
`INV-APPROVAL-1` treats a closed list of login-flow/session/logout/issuer-subject
operations as authentication bookkeeping. Project membership and service
account/token lifecycle remain approval-gated. The reviewed
`Authlib>=1.7,<2.0` dependency was also approved.

Slice 7 is implemented and dependency-complete release validation passed on
2026-07-13 (two full runs, **1754 passed** each). Its bounded
behavior is:

- OIDC Authorization Code + PKCE S256 through an injected provider interface;
- durable hashed state/nonce, expiry, atomic consumption and replay rejection;
- Authlib discovery, JWKS signature, issuer, audience, expiry and nonce
  validation, with fake-provider-only tests and no real IdP access;
- actor binding only by exact `(issuer, subject)`; email is display metadata,
  and platform-admin bootstrap uses an explicit exact-subject allowlist only
  when a new binding creates its actor;
- hashed, expiring, revocable server sessions with Secure/HttpOnly/SameSite=Lax
  cookies, current-user lookup and logout; and
- browser sign-in/current-user/logout while retaining legacy shared-token,
  open-development, WebSocket, agent and MCP compatibility.

This addendum marks Slice 7 validated but not deployed, and no real provider was
contacted. It does not enable authorization
enforcement, add Node Agent, change the scheduler/SSH backend, migrate
PostgreSQL, or claim hostile multi-tenant isolation. `AUTHORIZATION_MODE`
remains exactly `off|shadow`.

## 1. Purpose and sources

This document records what the repository implements at the audit baseline. It
separates shipped behavior from partial foundations, missing capabilities, and
protected behavior. `PLAN.md` is used as historical roadmap evidence, not as an
automatically authoritative implementation plan.

The audit used:

- `AGENTS.md` and `CLAUDE.md`;
- `PLAN.md`;
- the canonical invariants in
  `.claude/skills/dispatcher-domain/references/invariants.md`;
- the Phase 0 invariant drafts and endpoint/domain inventory;
- application code, schema/migrations, deployment scripts, and tests; and
- the Git history from the initial implementation through `87f10c5`.

No production server, production credential, runtime database, or production
configuration was accessed.

## 2. Executive summary

Dispatch Center is an operational single-process control plane, not a blank
slate. It already has a deterministic approval/queue/scheduling core, a mature
SSH execution path, project discovery and lifecycle foundations, legacy dataset
and result flows, optional LLM/MCP adapters, and a centralized Codex Coding
Runner.

The principal gaps relative to the newly agreed direction are:

1. authentication is an optional shared token rather than individual team
   identity;
2. there is no project authorization or actor-aware approval model;
3. SSH remains the only worker execution channel and there is no Node Agent;
4. approved jobs are not represented by a unified immutable JobSpec/
   ExecutionPlan/RunAttempt contract; and
5. the dataset, scheduler, and result models remain earlier-generation
   implementations rather than the immutable artifact and reproducible-run
   models described in the historical roadmap.

The correct migration is incremental. The existing SSH backend and current APIs
must remain available while identity, authorization, execution contracts, and a
Node Agent channel are added and proven.

## 3. Capability matrix

| Area | Current status | Evidence-based summary |
|---|---|---|
| Service architecture | Implemented | One FastAPI process owns REST/WS APIs, monitor and scheduler loops, SQLite access, and background completion hooks. MCP is an optional isolated HTTP client process. |
| Authentication | Partial | Optional `AUTH_TOKEN` protects all HTTP endpoints except `/` and `/static/*`; WS authenticates its first message. When unset, APIs are open for local development. There are no individual identities. |
| Project authorization | Missing | A token holder has system-wide access. There are no memberships, roles, per-project checks, service scopes, or cross-project isolation tests. |
| Approval and audit | Implemented, with identity gaps | Fourteen approval kinds, validation at request time, revalidation at approval time, restricted auto-approval, and append-only audit are present. Requester/approver identities, canonical payload hashes, expiry, and high-risk separation of duties are missing. |
| Queue and state recovery | Implemented | Jobs, dependencies, priority/FIFO, blocked refresh, sentinel reconciliation, stall suspicion, and background finish hooks exist. DB state is written before remote dispatch. |
| SSH execution | Implemented and protected | AsyncSSH/SFTP writes `cmd.sh` and `run.sh`; tmux detaches long jobs; `exit_code` is the terminal sentinel; unreachable workers do not become failed jobs. Server A also has a local runner with the same callable shape. |
| Execution abstraction | Missing | Scheduler code directly uses the SSH/local callable interfaces. There is no `ExecutionBackend`, backend selection, attempt lease, agent protocol, or transport-neutral reconciliation contract. |
| Node Agent | Missing | No agent daemon, polling protocol, node credential, agent heartbeat, attempt acknowledgement, or agent result-upload path exists. |
| Project catalog | Partial but substantial | Projects, candidates, instances, matrix/detail/activity views, hub sync, Git initialization, deploy, manual records, and timelines are implemented. |
| Project identity | Partial | `projects.id` is a UUID and instances dual-write `project_id`, but `projects.name` remains the primary key and many tables/APIs still use the name. |
| Instance state | Implemented foundation | Periodic read-only reconciliation records `available`, `missing`, `dirty`, `diverged`, or `unknown`; an unreachable worker produces `unknown`, not deletion or failure. |
| Project versions | Implemented foundation | `project_versions` records immutable hub commits from hub sync and deploy. Ordinary jobs and coding runs are not yet consistently bound to a `project_version_id`. |
| Candidate linking | Implemented | Normalized Git remotes produce suggestions; a human can link a candidate to an existing project. Same basename alone does not auto-merge projects. |
| Central Codex Coding Runner | Implemented over SSH | One configured `CODEX_RUNNER_SERVER` receives approval-gated coding jobs. The system manages isolated worktrees, Codex execution, structured `coding_runs`, tests, secret checks, bundles, result backfill, status, downstream bundle use, and cleanup. |
| Dataset management | Partial/legacy | Dataset registry, count/size manifest, rsync synchronization, space checks, cache reconciliation, data locality, and cards exist. Immutable snapshots, shard hashes, final markers, and transfer leases do not. |
| Scheduling | Partial/legacy | Deterministic tag/pin/data-locality/priority/FIFO selection and one ordinary job per machine exist. Observation freshness, resource reservations, reason codes, atomic claims, quotas, and GPU-slot scheduling do not. |
| Results and experiments | Partial | Results are pulled with rsync without changing execution status on pull failure. Jobs, coding runs, and manual records are merged into a searchable project timeline. Unified artifact manifests, structured metrics, comparisons, and retryable collection records are missing. |
| LLM and MCP | Implemented foundation | Optional Anthropic/vLLM paths and an isolated MCP bridge provide read tools and request-only mutation tools. They cannot approve, reject, run shell, or connect directly to SSH. Project-centric planning and evidence-rich comparison remain incomplete. |
| Backup and restore | Implemented foundation | `deploy/backup.sh` captures SQLite, audit/configuration, and hub repositories, optionally data/results. `deploy/restore.sh` displaces existing state before restore. A documented recurring restore drill is still needed. |

## 4. Implemented project roadmap slices

The current Git history shows that historical Phase 0 and the five bounded
Project Control Plane slices in `PLAN.md` section 14 were implemented in order:

1. `efba704`: backup/restore scripts, invariant drafts, and endpoint/table
   inventory;
2. `d05a6e7`: additive Project UUID migration and legacy name adapter;
3. `4a9efed`: ProjectInstance reconciliation state machine;
4. `75b0b0f`: candidate linking to an existing Project;
5. `c632e39`: canonical `ProjectVersion` service; and
6. `87f10c5`: project detail UI/API integration for instance states and version
   history.

These commits complete the five short slices, not the whole historical Phase 1.
The following broader Phase 1 outcomes remain incomplete:

- UUIDs are not yet the exclusive cross-system identity;
- project state, hub metadata, runtime profiles, dataset bindings, and templates
  are not a unified Project lifecycle model;
- normal runs are not guaranteed to pin a canonical hub revision; and
- the project page does not yet create a complete immutable code/data/resource
  execution plan.

## 5. Central Codex Coding Runner

The centralized runner is already a significant delivered subsystem and must
not be rebuilt from scratch.

Implemented behavior includes:

- a single runner selected by `CODEX_RUNNER_SERVER`, with the feature disabled
  cleanly when it is unset;
- runner reservation and coding-job scheduling rules;
- `coding_task` approvals that are excluded from auto-approval;
- request-time validation and approval-time revalidation;
- project source resolution from a runner instance or registered Git remote;
- an isolated branch/worktree task directory under `CODEX_WORKSPACE_ROOT`;
- instruction delivery as a file and deterministic wrapper-script generation;
- non-root checks, configurable network access, Codex CLI/auth health probes,
  detected tests, secret checks, result commits, and Git bundles;
- persisted `coding_runs` linked to approvals and jobs;
- result collection/backfill, status/detail APIs, downstream bundle staging, and
  guarded cleanup.

The runner nevertheless remains SSH-backed: the control plane writes files and
launches/reconciles its coding job using the same SSH/SFTP/tmux/sentinel model as
other workers. Migrating this runner to Node Agent is therefore a transport
migration after the Node Agent has proved ordinary jobs, not a new Codex product
build.

## 6. Identity and authorization state

Current authentication has one optional shared secret:

- HTTP callers send `X-Auth-Token`;
- every authenticated caller has the same authority;
- the index and static assets are intentionally exempt;
- WS uses a separate first-message check because HTTP middleware does not cover
  WebSockets; and
- MCP sends the same token over its HTTP client boundary.

`source=web|api|vllm|chatgpt` and audit values such as `human`, `web-direct`, or
`auto-rule-N` describe a channel or mechanism, not a durable actor identity.
There are no user, identity-provider binding, session, service-account,
membership, or role tables. No endpoint centrally answers whether a principal
may view or mutate a particular project.

The future authorization layer must cover indirect project data as well as
project endpoints: jobs, approvals, coding runs, datasets, results, audit views,
LLM/MCP tools, and server/node administration can otherwise bypass a project
check applied only to `/projects/*`.

## 7. Execution and recovery state

Ordinary remote execution currently follows this sequence:

1. create and approve an enqueue request;
2. persist the job and dependencies;
3. deterministically select an eligible idle server;
4. mark the job `running` in SQLite;
5. create a remote job directory, write command/wrapper files via SFTP, and
   launch a detached tmux session;
6. reconcile `exit_code`, tmux existence, and connectivity on later scheduler
   ticks; and
7. schedule non-blocking result/mail/coding-run completion hooks.

This is a proven compatibility backend. Its main architectural limitation is
that execution state is expressed in SSH-specific paths and call sites rather
than a backend-independent attempt protocol. A future agent must preserve the
same safety outcomes:

- free command text is written as data and never interpolated into a launcher;
- a stable attempt identity prevents duplicate launch;
- the database records intent before the worker side effect;
- uncertain or unreachable state remains unknown rather than failed; and
- fallback cannot launch an SSH duplicate while an agent attempt may be alive.

## 8. Protected behavior and decision gates

The canonical invariant file remains authoritative. In particular:

- all material mutation uses the approval workflow unless an existing canonical
  exception applies;
- dangerous requests are rejected when requested and revalidated when approved;
- auto-approval remains limited to `enqueue` and `stop`;
- new endpoints are authenticated by default;
- SSH command construction, SFTP command delivery, timeouts, sentinels, and
  unreachable semantics remain protected for the SSH backend;
- persistent state is SQLite plus worker execution evidence, not AppState caches;
- DB updates precede remote side effects;
- the job state machine is closed unless explicitly changed;
- audit is append-only and best effort;
- LLM/MCP cannot approve, reject, execute arbitrary shell, or directly access
  the execution layer; and
- tests use fake interfaces and do not contact real infrastructure.

Two newly agreed directions require explicit invariant decisions before code is
changed:

1. a resident Node Agent conflicts directly with `INV-SSH-1`, which currently
   forbids installing a persistent worker agent; and
2. OIDC login and callback endpoints require narrowly defined unauthenticated
   routes, conflicting with the literal exemption restriction in
   `INV-APPROVAL-5`.

The correct change is to revise or split those invariants explicitly. SSH rules
should continue to govern the SSH backend, while new `INV-NODE-*` rules govern
the agent. OIDC exemptions should be limited to the login handshake and must not
become general API exemptions.

`AGENTS.md` adds further protected direction: approved execution payloads remain
immutable, missing revisions/hashes/history are never fabricated, migrations
are additive, SSH remains until a tested replacement and rollback exist, and an
unreachable remote state is not automatically failure.

## 9. Verification snapshot

The following non-production checks were run during this audit:

- `.claude/skills/release-gate/scripts/static_checks.sh`: **PASS**, including
  approval, auth, audit, LLM/MCP, dependency-drift, and pinned-test checks.
- Focused core tests covering DB migration, project-instance reconciliation,
  inventory, datasets, scheduler, job queue, approvals, security, auto-approval,
  and configuration: **351 passed in 2.10 seconds**.
- Full `pytest -q`: **not collected successfully in the current shell**. Eleven
  test modules failed import because `fastapi`, `asyncssh`, and `mcp` are not
  installed in this environment.

The full-suite result is an environment limitation, not evidence that the
uncollected tests pass or fail. Establishing a dependency-complete isolated
environment and obtaining a clean full-suite baseline is the first release gate
of the proposed roadmap.

## 10. Current constraints to carry forward

- Preserve all supported APIs and legacy rows during migration.
- Do not rewrite the application or split it into services solely for the new
  direction.
- Do not make the Node Agent and project-domain migrations one big change.
- Do not silently reinterpret old jobs as revision-pinned or old datasets as
  cryptographically verified.
- Do not remove or weaken SSH while any supported workload depends on it.
- Do not describe the system as hostile multi-tenant isolation; the agreed
  target is an authenticated internal team with project authorization.
