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

## 0.1 AI Engineering Task UI/Slices 2–3 addendum (2026-07-14, not deployed)

The current worktree adds the progressive application shell and structured AI
Engineering Task wizard, followed by the feature-flagged immutable task
contract. This is a worktree/release-candidate statement, not a production
deployment claim. The Slice 2 baseline completed with **1844 tests passed** and
the static invariant gate passed. Slice 3 adds the visibility journal and its
security hardening. Its targeted unit and TestClient groups, compilation, Ruff
and diff checks pass. After the bounded Worker-validation work described in
§0.5, the combined worktree completed a full **2039 passed, 0 failed** pytest
run and the static invariant gate passed on 2026-07-15. TestClient groups run
outside the filesystem sandbox because this environment's AnyIO portal can
stall in the sandbox even for a blank FastAPI application. No production
server, credential, IdP, Coding Runner, worker, or external Git provider was
contacted.

The new backend remains disabled by default through
`ENGINEERING_TASK_BACKEND_V1=false`. When enabled, it adds a structured request
endpoint and immutable `engineering_tasks` parent records bound to a current
Project UUID, `project_version_id`, and exact Hub commit. Request creation is
atomic with the existing `coding_task` pending approval. Approval revalidates
the complete contract and atomically creates the pinned CodingRun, exact-bundle
staging Job, dependent Coding Job, task linkage, and approved decision. A
scheduler owner gate prevents task Jobs from dispatching unless that parent
approval is approved.

The staging path does not mutate the canonical Hub and does not bundle all
refs. It fetches the approved SHA into a task-local bare repository under a
fixed ref and leaves Runner transfer to the `_local` Job. The v2 contract stages
only that ref plus the pre-written instruction, canonical path-policy data and
exact verifier source. Completion preserves the approved base and verifies the
returned bundle/result ancestry before attaching result artifacts. Historical
CodingRuns remain explicitly `legacy_unpinned`; no ProjectVersion or historical
revision is fabricated.

Slice 3 adds append-only, idempotent event/command/artifact visibility records,
safe task detail endpoints, an eight-pane accessible detail dialog, explicit
unknown/interrupted/disconnected/failed presentation, restart-safe result
collection, and safe projections for Engineering Task-owned rows across legacy
Jobs/CodingRuns, activity/timeline, chat/agent/MCP, audit, and notification
surfaces. Free text containing a high-confidence raw credential is rejected
before approval persistence. Dispatch, reconciliation, stall probing, stop,
cleanup, and recovery revalidate the enabled approved Runner identity before
contact, and cleanup also revalidates the approved workspace. Generic queued
cancel, rerun, and diagnosis cannot operate on task-owned internal Jobs;
running stop remains the existing approval path.

The current hardening pass also makes the approved outer Job command executable
contract explicit. Each task-owned staging/coding Job must match its durable
command journal by task, attempt, Job ID, semantic role, approval, approved
policy disposition, and SHA-256 before dispatch, running reconciliation, stall
probing, or stop can contact the local/SSH executor. A mismatch records only a
fixed idempotent event and leaves legacy Jobs unchanged. Terminal result
recovery can also repair missing additive artifact metadata without pulling the
already-canonical result again or changing the pinned result state.

Provider facts and execution launch now come from one frozen, Codex-only
allowlisted `CodingAgentProvider` registry shared by request validation,
approval-time revalidation, command generation and capability discovery. The
reviewed `codex-exec-v1` adapter supports one deterministic, single-process
start operation. Resume, task-safe cancel, live event streaming, checkpoints
and command-approval callbacks fail closed and are not implied by the adapter.

This addendum does **not** introduce a Codex app-server provider, per-command
approval callbacks, automatic repair loops, agent-originated Worker requests,
Run Profile persistence, publication approval kinds, GitHub integration,
deployment changes, or authorization enforcement. Those remain later slices
and, where called out in the accepted plan, require separate human decisions.

## 0.2 Bounded Project workspace Slice 6 addendum (2026-07-14, not deployed)

The current worktree also contains a bounded, frontend-only Project workspace
slice. It reorganizes the existing project detail surface into seven sections:
overview, code and versions, AI engineering, runs and validation, data and
artifacts, settings, and deployment. The legacy `#project/<encoded-name>` route
continues to mean overview. Additive deep hashes use
`#project/<encoded-name>/<section>` with the exact section values `overview`,
`code-version`, `ai-engineering`, `runs-validation`, `data-artifacts`, `settings`,
and `deployment`; an unknown section falls back to overview. Existing top-level
hash routes remain unchanged.

The workspace reuses the current detail, timeline, Job, Engineering Task,
dataset, and deployment-approval resources. It does not add a database table,
migration, scheduler path, SSH behavior, worker behavior, or a parallel Project
API. No manual database migration or worker/Coding Runner restart is required
for this slice.

The Project header resolves a badge from the current Project UUID and the
authenticated actor's exact `/auth/me` membership. Platform Admin and service
actor presentations are explicit. This is presentation only: it does not hide,
disable, permit, reject, or otherwise authorize an action. Authorization remains
exactly `off|shadow`, and existing server-side approval behavior is unchanged.

This workspace slice does **not** implement Run Profile persistence,
project-wide artifact aggregation or unified manifests, immutable
ProjectVersion binding for ordinary Runtime Jobs, or new deployment semantics.
The existing feature-flagged immutable AI Engineering Task contract described
above remains separate. The deployment pane only presents the existing
approval-gated deployment action apart from ordinary execution; it does not
promote, merge, or deploy automatically. Runner/activity unavailable or stale
evidence is rendered as disconnected/unknown and is not reclassified as an
execution, Job, or instance failure.

This addendum is not a claim that all Project UI, all supporting platform
surfaces, or the full accepted multi-slice plan is complete.

## 0.3 Bounded supporting-surface Slice 8 addendum (2026-07-14, not deployed)

The current worktree contains an additional frontend-only supporting-surface
slice. Semantic in-page subnavigation now organizes existing content inside the
unchanged `#tab/overview`, `#tab/servers`, `#tab/datasets`, and `#tab/jobs`
routes. Overview separates health, approvals, activity/audit, and a read-only
administration summary. Infrastructure separates the Coding Runner, ordinary
workers, and inventory. Data separates the current Dataset registry from an
explicit Results/Artifacts boundary. Runtime Jobs separates the existing
custom-command entry, a future Run Profiles explanation, and the current Job
list. No parallel router or new backend resource is introduced.

Approval presentation remains limited by current server evidence. Pending is a
platform-visible pending list, not a claim that every row is "assigned to me"
or that the current actor may approve it. The requested view includes a row
only when its non-null `requester_actor_id` exactly equals the current
`/auth/me` actor ID. It never matches display names or aliases. A legacy/null
requester remains unknown and is not attributed to the current actor. Category
grouping is derived only from existing approval kinds and does not modify
payloads, decisions, visibility semantics, or authorization.

Pending cards disclose the complete immutable payload in an escaped, collapsed
view. New `coding_task` requests identify ProjectVersion, exact base commit, and
execution contract instead of being rendered as legacy runtime HEAD. Identity
approvals have explicit summaries. Generic web/chat approval is intentionally
disabled for `service_token_issue`, because that response returns a secret only
once and these surfaces do not implement secure one-time token capture; reject
remains available.

The Coding Runner status call was removed from the five-second `refreshAll()`
poll. `GET /codex-runner/status` may perform a read-only SSH probe on a backend
cache miss and uses a 30-second cache, so the supporting UI requests it only on
explicit entry into the Infrastructure Coding Runner subsection, refresh/retry,
or legacy Coding Task modal open; the default Worker servers subsection does
not probe it. The Engineering Task wizard retains its own open-time availability check.
Unavailable, disconnected, stale, or unknown Runner evidence is not converted
into a Coding Task or Job failure.

After the mandatory `/auth/me` check, the five-second background refresh settles
each read independently. A failure marks only its owning surface unavailable and
offers retry while other successful reads still render. An auth-serial guard
prevents late responses from repopulating protected UI after a 401 reset.

This slice does not implement a global Results/Artifacts index, project-wide
artifact aggregation, Run Profile schema/API/persistence, or identity mutation
UI. The administration surface is a safe actor/membership/scope summary only;
existing service-account, token, and membership mutations remain feature-
flagged and approval-gated backend operations. It also does not change
authorization (`off|shadow`), approval behavior, scheduler/SSH/worker behavior,
or the database schema.

This bounded Slice 8 statement does not claim that the complete navigation,
administration, result lifecycle, Run Profiles, publication flow, or the full
accepted plan is implemented.

Source-level responsive/accessibility tests and the static invariant gate pass
for these worktree changes. Real 1440/1024/768/375 browser screenshots and
keyboard/overflow assertions are still release evidence gaps in this shell:
there is no usable automated/headless browser stack. A WebKitGTK MiniBrowser
binary is present, but this shell has no display, WebKitWebDriver, or screenshot
automation with which to drive it. Playwright/Selenium and a cached compatible
browser runtime are also absent. No dependency was downloaded to fabricate
that evidence.

## 0.4 Native Worker validation request addendum (2026-07-15, not deployed)

The current worktree adds the bounded Worker-validation request path from an
eligible immutable Engineering Task. It remains behind the existing
`ENGINEERING_TASK_BACKEND_V1=false` default. The request endpoint initially
creates only an ordinary `kind=enqueue` pending approval and an immutable
`engineering_validation_requests` record; it does not create a Job, contact a
worker, or use web direct execution. It accepts one bounded validation command
and requires an explicit enabled worker rather than `_local` or automatic
placement.

Eligibility requires the native task and CodingRun to agree, the original
`coding_task` approval to remain approved with the same canonical payload, the
exact ProjectVersion/base commit to remain pinned, and a locally collected,
verified bundle whose hash and size match the immutable artifact record. The
approval snapshot binds the target safe identity, project-instance material,
bundle descriptor, parent approval digest, requested resources and generated
push/downstream command SHA-256 values. Approval revalidates that complete
snapshot before atomically creating two ordinary Jobs: a sync Job and its
dependent worker `adhoc` Job. Neither Job becomes an Engineering Task-owned
Coding Runner Job; both carry only the validation-request back-reference.

Before dispatch, local sync, running reconciliation, stall probing, stop or
terminal result collection can contact an executor, the system rechecks the
same target/instance/bundle/parent-approval/payload/generated-command and Job
contract. A mismatch records a fixed safe refusal and makes no contact. It does
not convert unknown, unavailable or disconnected remote evidence into a failed
execution. Generic Jobs, project timeline, chat/agent, audit and notification
projections expose semantic labels and digests instead of executor commands,
paths, keys, raw logs or verifier exceptions. A queued validation Job retains
the ordinary cancel behavior; a running stop still creates and approves the
existing `stop` request, with the contract gate applied before SSH.

The Project workspace uses the server-returned action contract to select this
native pending-approval endpoint. Legacy CodingRuns continue to use the current
`/dispatch` adapter and are not relabeled as immutable. The slice does not let
Codex autonomously request a worker, does not add a new scheduler, and does not
implement multi-command validation plans, provider command callbacks, automatic
repair, Run Profiles, publication, promotion, PR creation or deployment.

## 0.5 CodingAgentProvider runtime seam addendum (2026-07-15, not deployed)

The Codex-only registry now exposes a real transport-neutral
`CodingAgentProvider` interface. The current `codex-exec-v1` provider produces
the exact reviewed one-shot shell fragment consumed by the existing Coding
Runner Job. It cannot select an arbitrary executable and does not create a new
SSH, scheduler or process-control path. Provider id, adapter id and output
contract are rechecked before instruction staging or Job creation; the final
outer Job command remains protected by its approval/owner SHA-256 journal.

The adapter's output contract truthfully declares a final response and bounded
machine event log, but no checkpoint or live event stream. Unsupported resume,
cancel, event and command-callback methods raise a typed fail-closed error and
have no endpoint or legacy fallback. `GET /coding-agents` returns only safe
runtime capability metadata. The existing Engineering Task capability snapshot
is unchanged so already-pending `engineering-task-v1` approvals do not become
stale merely because runtime discovery gained fields.

The runtime metadata distinguishes policy scope: immutable Engineering Tasks
keep external network disabled, while the legacy Coding Task adapter may still
honor the existing operator-only `CODEX_NETWORK_ACCESS` platform setting.
Neither path authorizes dependency installation. This distinction is metadata,
not a claim that the provider can widen an approved immutable task contract.

The locally installed `codex-cli 0.144.4` labels app-server itself experimental.
Consequently this seam does not register or activate an app-server adapter and
does not silently replace the persisted `codex-exec-v1` adapter. Production
app-server use still requires the plan's explicit protocol/version pinning,
resource-enforcement and command-approval decisions.

## 0.6 Engineering Task v2 final-Git path-policy addendum (2026-07-15, not deployed)

New immutable Engineering Task requests now use `engineering-task-v2`. Their
structured request must contain at least one canonical `allowed_paths` entry and
may contain a separate canonical `prohibited_paths` deny list; a prohibited scope
always wins. The natural-language `prohibited_changes` list remains advisory and
is not reinterpreted as a machine policy. Already-pending
`engineering-task-v1` approvals retain their original advisory behavior: the
approval path neither infers a v2 policy from their text nor changes their
persisted command contract.

Request creation derives one canonical path-policy document and binds its
SHA-256, the exact standalone verifier identity, and the verifier source SHA-256
into the immutable approval contract. Approval re-derives those values from the
structured request and rejects drift before writing task inputs or creating
Jobs. The `_local` staging Job transfers the exact Hub bundle, pre-written
instruction, canonical `path-policy.json`, and exact verifier source as files.
Path rules remain JSON data; they are never interpolated into the Runner shell
program. Only validated fixed-format digests are embedded in that deterministic
wrapper.

After the agent returns, the Runner verifies that the repository is still on the
approved task branch and descends from the approved base. It stages the final
tree and rewrites the result as at most one synthetic child commit of that base,
so intermediate agent commits are not included in the result bundle. Before
creating a bundle it rejects branch/ref drift, detached or non-descendant
history, dirty or untracked state, disallowed final Git object modes, secret
basenames, and any changed path outside `allowed_paths` or inside
`prohibited_paths`. The outer Runner no longer executes agent-modifiable pytest
or other repository code after the Codex turn: that would run outside the Codex
sandbox as the Runner OS user. Tests requested in the instruction may run only
inside the agent turn, or later through the approval-gated Worker-validation
path. The Runner checks the ref and executes the same path verifier again
immediately before bundling.

Server A does not trust a Runner-reported `done`. It copies the bounded regular
bundle into a private bare repository, independently verifies the pinned
base/result ancestry, revalidates the approved policy and exact verifier
contract, and checks the final Git range again. A scope rejection is recorded as
the distinct terminal state `path_policy_violation`; secret-name violations
remain `secret_violation`. Rejected results do not retain result/bundle pointers
or accepted diff/bundle artifacts, and fixed error text does not disclose the
offending path.

This is a final-Git-result guard, not turn-time filesystem confinement. It does
not observe a file that the agent changes and restores before the final tree,
and it does not add a per-command callback or approval mechanism. Runtime
metadata therefore remains explicit: inner-command approval is unavailable,
inner-command enforcement is the current sandbox only, and turn-time path
confinement is unavailable. Those stronger controls still require a separately
reviewed provider/execution design.

The deterministic policy unit suite and end-to-end staging/Runner/Server-A
integration tests cover canonicalization, drift rejection, v1 compatibility,
compliant results, scope/secret rejection, squashing, and malicious returned
bundles. The post-slice full release suite completed with **2124 passed, 0
failed**, and the static invariant gate passed on 2026-07-15. No production
service, Coding Runner, worker, credential, or repository was contacted, and
this slice has not been deployed.

## 0.7 Sanitized collected-patch download addendum (2026-07-15, not deployed)

The task detail surface now has one bounded, read-only result download:
`GET /engineering-tasks/{task_id}/patch`. It is available only for a native,
ProjectVersion-pinned `engineering-task-v1` or `engineering-task-v2` whose
parent `coding_task` approval is still approved and exactly matches the task,
whose Project, ProjectVersion, CodingRun, owner Coding Job and durable command
journal still agree, and whose accepted bundle plus collected diff artifact
descriptors are complete and unchanged. Legacy CodingRuns, snapshot adapters,
non-`done` results and drifted records fail closed. No generic artifact-download
route or raw bundle-download route was added.

The server opens only the fixed `diff.patch` logical key through the existing
directory/file descriptor guard. It rejects links and non-regular files,
captures at most 1 MiB from the same descriptor used for identity, SHA-256 and
size checks, detects changes during capture, requires strict UTF-8, and applies
the existing private-key withholding and credential/private-path sanitization
before returning in-memory bytes. It does not use `FileResponse` or perform a
second path read. Responses use a UUID-only attachment name, `no-store`,
`nosniff`, an explicit redaction flag and fixed non-disclosing 404/409/413
errors. The UI additionally requires the exact server-returned task URL, keeps
OIDC/session and legacy-token authentication behavior, checks auth generation
before and after reading the response, and constructs its filename only from
the canonical task UUID and redaction flag. Raw bundle remains disabled even if
a future or malformed response advertises it.

This artifact is deliberately labelled a **sanitized collected patch**, not a
verified or canonical patch. Its source bytes are integrity-bound to what
Server A collected from the Runner, but Server A does not yet regenerate those
bytes from the independently verified bundle's `base_commit..result_commit`
range. Pattern-based redaction also cannot claim complete DLP coverage; private
key markers fail closed, while unknown credential formats may not match the
current conservative patterns. Authorization metadata classifies the route as
project view, but `AUTHORIZATION_MODE` remains exactly `off|shadow`, so this
slice does not introduce project-isolation enforcement.

The owner Job command is checked against its durable command-journal SHA-256,
which detects ordinary drift. The schema does not yet persist a versioned,
historically reproducible outer-command builder identity, so a direct database
compromise that rewrites both command bytes and their journal digest in concert
is not independently detectable by this read-only route. No supported API can
perform that rewrite; this remains an explicit database-compromise residual for
a later versioned execution-contract migration.

The last executable TestClient checkpoint completed **35 passed, 0 failed** in
the patch suite and **108 passed, 0 failed** in the combined patch-plus-
Engineering-Task group. The current source now collects **38** and **116**
tests in those groups respectively after additional credential and execution-
boundary regressions were added. Focused new pure cases pass, while the current
groups as a whole (including their TestClient cases) have not rerun because this
environment's TestClient execution quota is exhausted. The repository-wide
suite currently collects **2241** tests without collection errors, but the full
post-download run is still
pending for the same reason.  This is missing release evidence, not a test
failure, and the older pass counts must not be presented as proof for the
current groups or full suite.  Ruff, Python compilation, focused pure/
static suites, diff checks and the static invariant gate pass.  No production
service, Runner, worker, credential or external repository was contacted.

## 0.8 Remaining Engineering Task decision gate (2026-07-15)

The current worktree is not the completed ten-slice plan.  Controlled app-server
execution and command callbacks, enforceable Server A resource limits, task
lifecycle/publication approval kinds, a secret-reference broker, Run Profile
persistence and GitHub publication still require the explicit human decisions
listed in `docs/AI_ENGINEERING_DECISION_GATE.md`.  That document is a review
packet only; it is not an approval record and does not enable a feature.

The post-download full dependency-complete pytest run and real responsive/
keyboard browser evidence also remain release gaps.  A process restart does not
resolve either gap and should not be performed merely because the worktree has
changed.

## 0.9 Post-review execution and visibility corrections (2026-07-15)

A security review found that the legacy outer wrapper's detected
`python3 -m pytest -q` ran after the Codex process had returned. Because the
agent can modify tests and imported project code, that command would execute
untrusted code as the Runner OS user outside Codex's workspace/network sandbox.
The wrapper now skips post-agent repository execution for both legacy and
immutable sources until an equivalent controlled validation sandbox or explicit
command policy is approved. Structured test fields remain `null`/`not_run`;
this is a fail-closed compatibility change, not evidence that tests passed.

The same review tightened three visibility seams. Native detail/diff/CodingRun
projections now require one canonical artifact row with complete persisted
source SHA-256/size and an exact match to the currently inspected descriptor;
missing evidence, descriptor drift, `rejected` or `withheld` all fail closed.
Path/secret-policy terminal results withhold a diff even if additive artifact
journaling was interrupted. Protected task, Worker-validation and compatibility
Coding Job terminal log tails are sanitized before SQLite persistence (and
private-key markers withhold the entire tail), with API redaction retained as a
second defense. Remote tail collection now has a 64 KiB byte cap in addition to
the line cap and local sanitizer. Both native and Slice-1 compatibility request
paths reject high-confidence raw credentials before approval persistence,
including Basic Authorization credentials and password-bearing HTTP/SSH URI
userinfo; non-password `ssh://git@host` references remain valid. Current
skip-validation command journals also ignore impossible Runner-supplied test
success metadata, while already approved older journals retain their historical
allowlisted result behavior.

One High execution-boundary gap remains and is why this worktree is not ready
for operational activation. After the Codex turn, the wrapper still performs
worktree-aware Git finalization outside the Codex sandbox. Hooks, signing,
fsmonitor on the commands we control, and external/textconv diff drivers are
disabled where possible, but clean/process/smudge filters, other mutable Git
metadata and unbounded worktree resource use cannot be made safe by those flags
alone. The complete post-agent finalization needs the fail-closed no-network
sandbox and hard resource limits described by D2 in
`docs/AI_ENGINEERING_DECISION_GATE.md`. Legacy non-pinned result ingestion also
retains its raw database compatibility contract for already approved journals;
its future disable/migration policy needs the same explicit decision. Do not
enable or deploy the controlled backend on an operational Runner before that
gate is implemented and reviewed.

A local read-only prototype confirmed that user/network namespaces plus
bubblewrap can create a minimal no-route filesystem sandbox, but this workspace
cannot exercise writable cgroup delegation, systemd scopes or a hard ext4 task
quota.  That prototype therefore does not close the gap and is not permission
to alter approved Job command bytes.  Production capability must be proved on
the actual non-root Runner through the D2 preflight/canary; no Runner was
contacted during this audit.

These corrections do not add a command approval kind, enable app-server,
contact a Runner/worker, or change authorization/auto-approval/SSH state
semantics. Focused pure tests pass; the current full TestClient release gate
remains pending as described above.

## 0.10 Wizard, visibility and validation hardening checkpoint (2026-07-15, not deployed)

The structured wizard now carries the optional Non-goals field through the
same deterministic renderer, preview and submitted structured request. Legacy
and native request bodies remain aligned with the fixed section order and the
4,000-Unicode-code-point limit; unsupported dependency/network controls remain
non-authorizing, and Worker validation remains a preference rather than an
automatically created Job. The task detail is an eight-pane dialog. Its current
UI hardening distinguishes secret-policy and pending-approval states, fails
closed when diff/log availability evidence is contradictory, serializes
validation/cleanup refreshes, isolates dialog background content, contains long
labels and digests on small screens, and restores live event-count feedback.

Worker-validation rejection now updates the pending approval, validation row
and safe journal event atomically. Status refresh uses compare-and-set across
database connections, allocates transition ordinals without trusting row
counts or colliding with gapped/malformed historical keys, and exposes terminal
exit/timestamp metadata only when it is bounded, typed and valid for a terminal
state. Task presentation scans the full journal for interruption and Runner-
contract history rather than relying on the first page of events. Unknown or
future task, CodingRun, owner-Job and validation statuses project as the fixed
`unknown` state instead of echoing storage values. Command cards similarly use
server-safe labels and status-bounded timestamps; storage commands, paths,
hosts, logs and malformed time values are not fallback presentation data.

The latest targeted checkpoints pass **83 UI**, **124 backend-safety**, **36
Worker-validation pure** (with 2 TestClient cases deselected), **30 visibility
pure** (with 7 TestClient cases deselected), **97 pure Coding Task**, **34
migration**, **47 authorization**, and **13 frontend-auth** tests. The static
invariant gate is green, and the current source collects **2241 tests** without
collection errors. These bounded results do not prove the dependency-complete
full suite or the deselected API/TestClient paths, and real 1440/1024/768/375
browser screenshots plus keyboard/focus/overflow checks are still missing.

No service was deployed or restarted, and no production service, credential,
IdP, Coding Runner, worker or external provider was contacted. D1–D6 in
`docs/AI_ENGINEERING_DECISION_GATE.md` still gate the remaining execution,
secret, Run Profile and publication slices; this checkpoint is not a claim that
the accepted ten-slice plan is complete.

## 0.11 D1 bounded first slice: unwired `codex-app-server-v1` adapter (2026-07-17, not deployed)

Phase 4 of `docs/DECISIONS.md`'s 2026-07-16 D1 ruling ("bounded implementation
only") is now implemented. `app/codex_app_server.py` adds a standalone,
transport-agnostic JSON-RPC session layer (`CodexAppServerSession`) that pins
its own internal protocol/capability shape and fails closed on any version or
message-shape drift; only in-process fake peers exercise it in
`tests/test_codex_app_server.py`, and it launches, connects to, or knows how to
reach no real process.

`app/coding_agents.py` adds `CodexAppServerProvider`, a `CodingAgentProvider`
implementation whose five operations (`start_turn`, `resume_turn`,
`cancel_turn`, `stream_events`, `respond_to_command_approval`) all fail closed
in this slice regardless of configuration — nothing is wired into the existing
turn lifecycle, the outer Coding Runner Job executor, or any approval flow.
This provider is deliberately kept out of the reviewed
`_APPROVED_CODING_AGENT_PROVIDERS` task-contract registry, so
`list_coding_agents()`, `list_coding_agent_capability_snapshots()` (the
`GET /engineering-tasks/capabilities` approval-payload contract), and Engineering
Task request validation are unaffected and continue to expose only `codex`; an
Engineering Task request can never select the new adapter id.

The additive `controlled_coding_runner_v1` config flag
(`CONTROLLED_CODING_RUNNER_V1`, default `false`) only controls whether
`GET /coding-agents` appends this adapter's honest, all-capabilities-`false`
runtime metadata for operator visibility; it is not an enablement gate for any
execution path. Production activation of a real app-server adapter still
requires the separate canary/rollback sign-off the D1 ruling reserves.

New/extended focused suites (`tests/test_codex_app_server.py`,
`tests/test_coding_agents.py`, `tests/test_engineering_tasks.py`) pass, the
static invariant gate is green, and the repository-wide suite completed
**2341 passed, 1 failed** in this environment. The one failure
(`tests/test_agent_tools.py::test_request_update_server_tool_creates_approval`)
is a pre-existing local-environment gap unrelated to this slice: this host's
`~/.ssh` directory does not contain the `id_rsa` file the test's server
config fixture points at, following an unrelated OS-level rebuild of this
development host. No production service, Coding Runner, worker, credential, or
external provider was contacted, and this slice has not been deployed.

## 0.12 Real responsive/keyboard browser evidence (2026-07-17, disposable, not deployed)

`docs/DECISIONS.md`'s explicit 2026-07-16 authorization ("核准在本開發環境一次性
安裝 Playwright + Chromium") is now exercised, closing the visual-evidence gap
repeatedly noted since §0.3. Node.js 24 LTS (via the NodeSource apt repository)
and Playwright 1.x with a Chromium build were installed disposably in a
directory outside the repository (`.playwright-visual`, moved to a scratch
path after use; nothing was added to the tracked working tree). No `--with-deps`
system package layer was needed — Chromium launched cleanly on this host's
existing shared libraries.

The application under test was `app.main:app` run via `uvicorn`, isolated from
any real deployment: `DB_PATH`/`AUDIT_PATH`/`SERVERS_YAML_PATH` pointed at a
disposable scratch directory (not `jobqueue.db`/`audit.jsonl`/`servers.yaml`),
the process's working directory was outside the repository so the real
`.env` (containing the operator's actual `AUTH_TOKEN`) was never loaded, and
no `servers.yaml` existed so the monitor loop had nothing to probe. One demo
project was created through the ordinary `POST /projects` API for the Project
workspace views; no worker, Coding Runner, credential, or external provider was
contacted.

Playwright captured full-page screenshots at exactly 1440/1024/768/375px for
five views (platform Overview/Servers/Jobs tabs and the Project workspace's
Overview and AI Engineering sections) — 20 screenshots total. A same-page
`scrollWidth` vs `clientWidth` check ran on every one of those 20 views: zero
overflow horizontally at any width. A keyboard-only check (repeated `Tab`
presses from a reset focus) ran at the 1440 and 375 extremes across all five
views (76 focus stops total): every stop landed on a visible element with a
non-empty focus indicator (a solid 3px outline in every observed case, with no
`outline: none` fallback relying solely on `box-shadow`). The mobile width
correctly collapses the left navigation into a header hamburger control
without introducing overflow.

This is a manual, one-off disposable-environment run, not an automated
regression suite wired into CI; the screenshots and JSON reports were not
committed and live only in this session's scratch directory. It covers the
five listed views only, not every route/dialog (e.g. the eight-pane
Engineering Task detail dialog from §0.3/§0.10 was not separately captured
here). No production service was contacted, and this does not change
authorization, approval, scheduler, SSH, or database behavior.

## 0.13 D5 Run Profile v1 and D6 GitHub publication interface (2026-07-17, not deployed)

Continuing `docs/DECISIONS.md`'s 2026-07-16 ruling now that its stated D5/D6
precondition ("待 D3/D1 的核准/adapter 模式驗證後再開始") is satisfied by §0.11's
completed D1 first slice and the already-committed D3 kinds.

**D5 — Run Profile v1.** An additive `run_profiles` table stores immutable
revisions keyed by `(project_id, name, revision)`; nothing is ever `UPDATE`d in
place. New approval kinds `run_profile_create`, `run_profile_update`, and
`run_profile_archive` follow the existing three-stage pattern (request-time
validation, approval-time revalidation, atomic DB effect) and are not added to
the `enqueue|stop` auto-approval allowlist. A create request rejects an
already-existing name; an update/archive request pins `based_on_revision` to
the current head so approval-time revalidation rejects a profile that changed
concurrently instead of silently superseding a revision the requester never
saw. Archiving inserts a tombstone revision carrying the prior content forward
unchanged (status becomes `archived`); it never deletes history. The
`RUN_PROFILE_V1_ENABLED` switch (default `false`) hides
`GET /projects/{name}/run-profiles` and the three `.../request` routes with the
same 404-when-disabled pattern as Slice 6 identity administration, and is
independently re-checked inside `approve()` so a mid-flight disable cannot
silently approve a pending request. Existing `Project.default_command`,
`setup_cmd`, and `require_tag` remain untouched compatible legacy fields; no
row is backfilled to fabricate a fake already-approved profile for them. This
slice does **not** wire a Run Profile into `enqueue`/job dispatch — selecting
one to actually change what a job runs is explicitly out of scope here and
remains future work.

**D6 — GitHub publication interface, fake only.** `app/github_publication.py`
adds `GitHubPublicationProvider`, an abstract interface with exactly one
operation (`create_draft_pull_request`) and immutable, credential-free
request/result dataclasses. No concrete provider exists in the repository, no
network call is possible from this module, and it is not imported by any
approval, API, or execution code path — a source-level test asserts both the
absence of network-capable imports and that no other `app/*.py` module imports
it. The result shape can only ever represent a draft pull request
(`is_draft=True` is enforced by validation, not caller preference); the
interface has no field through which an installation/access token could reach
a caller or the coding agent. Operational activation (GitHub App registration,
repository allowlist, installation scope, egress policy, short-lived
installation-token broker) remains a separate, explicitly named decision this
slice does not request or imply.

New/extended tests (`tests/test_run_profiles.py`, 21 cases;
`tests/test_github_publication.py`, 20 cases) pass, along with the full
repository-wide suite (**2382 passed, 1 failed**, the same single pre-existing
`~/.ssh/id_rsa` environment gap noted in §0.11 aside) and the static invariant
gate (now 24 approval kinds). No production service, Coding Runner, worker,
credential, or external provider was contacted, and neither slice has been
deployed.

## 0.14 Goal 2 automated-dispatch loop, Slices 1–5 (2026-07-18, not deployed)

The approved Goal 2 plan (`docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md`; DG-1/DG-2
approved in `docs/DECISIONS.md` 2026-07-18) is implemented end to end.
Implementation was carried out by the sonnet-coder agent under main-session
review; every slice passed focused suites, the static invariant gate, and the
repository-wide suite before the next slice began.

**Slice 1 — capacity persistence.** The monitor probe additionally collects
RAM (`free -b`, new `---FREE---` section; `parse_free_output`); each tick's
per-server readings persist best-effort into the additive
`server_observations` table with retention pruning
(`SERVER_OBSERVATION_RETENTION_DAYS`, default 14). In-memory `server_states`
updates always complete before and independent of DB writes; a DB failure
only logs. Read-only `GET /servers/{name}/observations`. Rollback:
`SERVER_OBSERVATIONS_ENABLED=false` stops writing; the table stays.

**Slice 2 — idle summary.** Pure `app/capacity.py` folds observation history
into an explainable `IdleSummary` (sample count, online ratio, GPU/load
p50/p95, fail-closed continuous-idle streak where an unknown metric breaks
the streak, freshness). No samples → `unknown`, never a guess. Read-only
`GET /servers/idle-summary`. `is_idle()`/`pick_job()` unchanged.

**Slice 3 — dispatch policies.** Additive immutable-revision
`dispatch_policies` table (allowed_servers JSON, optional exact
`run_profile_id` pin, `dataset_required`, `max_concurrent_placements`,
`valid_until`) with approval kinds
`dispatch_policy_create`/`_update`/`_archive` cloning the Run Profile v1
pattern (based_on_revision concurrency protection, archive tombstones,
`DISPATCH_POLICY_V1_ENABLED` default false, approve fail-closed when
disabled). Zero runtime scheduling effect by itself.

**Slice 4 — placement proposals (DG-1).** A new dedicated
`auto_placement_loop` (not `scheduler_tick`, which is untouched) evaluates
approved, unexpired policies against currently idle eligible servers
(`monitor.is_idle` + allowed_servers/tag/dataset constraints, pure
`app/auto_placement.py`) and creates ordinary **pending** `auto_placement`
approvals. Proposal creation refuses dangerous commands
(`is_dangerous`), deduplicates pending proposals, applies a per-pair
cooldown (`AUTO_PLACEMENT_COOLDOWN_SEC`, default 3600) and the policy's
concurrency cap (via the additive `jobs.auto_placement_approval_id`
back-reference). The approve branch revalidates everything (policy
head/revision, expiry, server enabled + allowed_servers membership, command
re-derivation + SHA-256, `is_dangerous` re-check, cap) and then builds the
job chain through the same `build_dispatch_plan`/`build_setup_script`/
`build_sync_script`/`enqueue_job` sequence as a manual pinned-server
enqueue. `AUTO_PLACEMENT_PROPOSALS_ENABLED` default false.

**Slice 5 — policy-scoped auto-execution (DG-2).** The protected-invariant
revision the DG-2 ruling required was written first:
`INV-APPROVAL-4b` in
`.claude/skills/dispatcher-domain/references/invariants.md` (authored in the
main session, recorded in `docs/DECISIONS.md`). `maybe_auto_decide_placement`
— deliberately separate from `maybe_auto_approve()`, whose `enqueue|stop`
gate remains byte-identical and static-gate-pinned — may auto-approve only
`auto_placement` proposals, only when `AUTO_PLACEMENT_KILL_SWITCH` is
explicitly off (default on = automatic execution disabled), and only after
the same read-only revalidation the manual branch enforces. A failing
condition leaves the proposal **pending** (never auto-rejected, never
downgraded); policy archive immediately stops auto-decisions.
`decision_mechanism` records `policy-{policy_id}-r{revision}` with no
fabricated human actor, plus an explicit system-actor audit event. The
shared revalidation helper also added an explicit `allowed_servers`
membership check to the manual path, closing a latent
direct-DB-mutation-only gap.

With every new flag at its default, runtime behavior is identical to the
pre-Goal-2 system: no observation-derived scheduling change, no proposals,
no auto-execution. The full loop requires four explicit operator steps:
enable policies, create+approve a policy, enable proposals, and disable the
kill switch.

Verification: focused suites per slice (final Slice 5 group: 192 passed
across auto-placement/autoapprove/dispatch-policy/approvals/scheduler/
jobqueue), the static invariant gate green throughout (28 approval kinds),
and repository-wide runs after Slice 4 (**2478 passed, 1 failed**) and after
Slice 5 (**2489 passed, 1 failed** — both times the same pre-existing
`~/.ssh/id_rsa` environment gap noted in §0.11). Frontend
surfaces for capacity/idle-summary/policies/proposals are deliberately not
implemented yet. No production service, worker, Runner, credential, or
external provider was contacted; nothing was deployed.

## 0.15 Goal 2 operator trial + Goal 3 Stage 0–1: dispatch UI (2026-07-19)

**Operator trial (2026-07-18, user-run).** The user exercised the full Goal 2
loop against their own instance: flags enabled stepwise (`DISPATCH_POLICY_V1_
ENABLED`, `AUTO_PLACEMENT_PROPOSALS_ENABLED`, trial-paced interval 60 s /
cooldown 300 s; kill switch left on), run profile `trial-echo` and policy
`trial-policy-1` created via curl and approved, and the first `auto_placement`
proposal (approval #7) appeared within one scan tick. The worker fleet was
re-keyed after the host rebuild (`~/.ssh/dispatch_worker` recreated, worker
`worker_5090_117` back online). This closes the "Goal 2 stable" precondition
recorded in `docs/GOAL_3_FUTURE_WORK_PLAN.md`.

**Goal 3 activation (2026-07-19 ruling).** See `docs/DECISIONS.md`
2026-07-19: Phases B / A(to A1) / D-1 / C(to C0 draft) activated, DG-B
approved by name, Goal 2 UI ordered first; DG-A/DG-C/canary sign-offs remain
open gates.

**Stage 1 — Goal 2 frontend UI (implemented).** `static/index.html` only, no
new routes, no new dependencies:

- Approval readability: `KIND_LABEL` + new `automated_dispatch` category and
  `automatedDispatchApprovalBodyHtml()` give the seven `run_profile_*` /
  `dispatch_policy_*` / `auto_placement` kinds honest Chinese summaries in
  both `renderApprovals()` and `chatApprovalCardHtml()` (auto placement
  states that approval enqueues a job and rejection only affects the one
  proposal).
- Infrastructure: `servers-idle-summary-card` inside the Worker servers
  surface renders `GET /servers/idle-summary` (samples, online ratio,
  load/GPU p50/p95, fail-closed continuous idle) with loading/empty/error
  states and a serial-guarded read-only loader.
- Project settings: two approval-gated managers built from a shared factory
  (`createPdApprovalManager`) mirroring the membership manager exactly —
  list heads, create/update(new revision)/archive via the `*-request`
  endpoints only, controls disabled until the backend confirms capability,
  matching the backend's literal 404 "administration is disabled" details
  when flags are off. The policy form's run-profile selector lists approved
  heads only. The stale "Run Profiles 尚未持久化" runtime placeholder now
  states the real D5 capability.

Tests: `test_supporting_surfaces_ui.py` gained automated-dispatch summary and
idle-summary contracts (and the former "future profiles" placeholder test now
asserts the real capability); `test_project_workspace_ui.py` gained the
manager fail-closed contract. Frontend suites: **105 passed**; static
invariant gate green. Live instance verification: static files are served
from disk, so the running trial instance picked the new UI up on browser
refresh without a restart (user gate G1: on-screen walkthrough).

## 0.16 Goal 3 Stage 2: Phase B empty-server bootstrap (2026-07-19, not deployed)

Phase B of `docs/GOAL_3_FUTURE_WORK_PLAN.md` (DG-B approved by name,
DECISIONS.md 2026-07-19) is implemented behind `SERVER_BOOTSTRAP_V1_ENABLED`
(default false — behavior is bit-identical to pre-Phase-B when off).

- **B1.** `app/provisioning.py` holds the reviewed fixed bootstrap script
  (`BOOTSTRAP_SCRIPT`, version v1, SHA-256 pinned into every request
  payload). DG-B boundary honestly implemented: non-root, idempotent,
  user-level only — `tmux`/`rsync`/`git` are *verified* and reported
  `missing` (system packages and GPU drivers stay an explicit operator/root
  step; the platform never escalates), `python-venv` creates/reuses
  `~/.dispatch-center/venv`. New approval kind `server_bootstrap` (29 kinds
  total; never auto-approvable). Delivery is SFTP (`sshpool.write_file`) to
  a constant path; component args come from a fixed allowlist — no user text
  ever reaches a shell. Approve-time revalidation rejects script-SHA drift;
  SSH-unreachable propagates and leaves the approval pending
  (unreachable ≠ failed); a completed run (pass or fail) is recorded
  as an approved approval plus a `server_bootstrap_reports` row (additive
  table keyed by host/username/port — targets are not yet in servers.yaml).
- **B2.** The same run performs the read-only capability check
  (`command -v` bash/tmux/rsync/git/python3, plus nvidia-smi when
  `gpu=true`), folded fail-closed into `passed`. `request_server_add_approval`
  gains an additive gate: only when the flag is on **and** the latest report
  for that (host,user,port) failed does server_add get rejected (readable
  missing-tool reason); no report or flag off → unchanged behavior.
- **B3.** `onboard-worker.sh` no longer dies on missing worker tools — SSH
  reachability still aborts, but missing tools now route the operator to the
  platform bootstrap request (printed curl example); the printed
  server_add YAML flow is unchanged.
- **B4 (dataset pre-warm) deliberately deferred** — optional in the plan.
- Routes: `POST /servers/bootstrap-request`, `GET /servers/bootstrap-reports`
  (both 404 while disabled; authorization catalog + route pin now 104).
  UI: `server_bootstrap` approval cards get an honest summary in both
  renderers (infrastructure category). README §2.1 and `.env.example`
  document the flag (the `.env.example` Goal 2 flag block was also added,
  closing a pre-existing doc gap).

Verification: `tests/test_server_bootstrap.py` (39 tests: DG-B script
boundary incl. no sudo/apt/nvidia in executable lines, pure functions,
FakeSSH lifecycle success/fail/unreachable/sha-drift, server_add gate
matrix, auto-approve exclusion, hidden-route contract); related suites
(approvals/autoapprove/server_config/agent_tools/security/frontend/
authorization/migration) green except the known `~/.ssh/id_rsa` environment
gap; static invariant gate PASS. No real worker, credential, or production
service touched.

## 0.17 Goal 3 Stage 3: A1 sandbox preflight (2026-07-19, gate G2 open)

`app/sandbox_preflight.py` + `GET /codex-runner/sandbox-preflight`
(route pin 105): a strictly read-only probe of the D2 sandbox hard
prerequisites on the configured Codex Runner — bwrap presence and a
disposable `--unshare-net ... true` namespace creation, cgroup v2 unified
hierarchy, user-scope writable delegation (requires cpu/memory/pids in the
delegated controller set), systemd user bus, and any `prjquota` mount.
Parsing is fail-closed: missing/truncated output → every check `unknown`,
and `ready` is True only when every check is an explicit pass (unknown is
never a pass). Unconfigured runner returns `{"configured": false}`;
unreachable runner returns all-unknown plus an error string. 21 tests in
`tests/test_sandbox_preflight.py` (read-only script contract, parser
fail-closed matrix, endpoint behaviors).

**Stage 3 intentionally stops here (gate G2).** A2 (bwrap finalization
wrapper), A3 (second-key retirement), and A4 (canary) wait for: operator
reinstall of the codex CLI (lost in the host rebuild; docs pin 0.144.4),
a real-Runner preflight run via this endpoint, and the DG-A ruling on
final resource numbers.

## 0.18 Goal 3 Stage 4: D-1 Codex Runner pool (2026-07-19, not deployed)

`CODEX_RUNNER_SERVERS` (comma list) generalizes the single
`CODEX_RUNNER_SERVER` into a pool with bit-identical single-runner
behavior: `apply_codex_config_rules()` normalizes (dedupe order-preserving;
single legacy var → singleton pool; pool-only → primary = first member;
both set → primary must be a member; every member must exist and be
enabled). `pick_job()` gains keyword-only `codex_runner_servers` — runner
identity becomes pool membership for coding eligibility and for the
existing reservation semantics (which now protect every member), and
`running_coding_count` is computed per server in `scheduler_tick()`
(identical for a single runner). New pure `pick_codex_runner()` selects
deterministically (fewest active coding jobs, tie → pool order); new
`Database.count_active_coding_jobs_by_server()` provides the load
evidence; `select_codex_runner()` wires selection into the two
creation-time binding sites (engineering-task request, coding_task
approve). Bound work (retry paths) never re-selects — bindings do not
drift when the pool changes. D-2/D-3/D-4 remain ungated future work.

Verification: `tests/test_codex_runner_pool.py` (12 tests: config matrix,
pool membership/eligibility, per-runner concurrency, reservation on every
member, deterministic selection, DB load counting) plus scheduler/
coding-task/engineering-task suites — 280 passed; static gate PASS.

## 0.19 Goal 3 Stage 5: C0 invariant-revision draft (2026-07-19, gate G3 open)

`docs/DG_C_INVARIANT_REVISION_DRAFT.md` proposes the INV-SSH-1 revision
(SSH backend stays agentless and dependency-capped; SSH is a permanent
compatibility/emergency channel) plus six new INV-NODE-* invariants
(outbound-only authenticated identity, lease/acknowledge-before-side-effect,
non-interpolated command bytes, heartbeat-expiry = unknown, restart without
duplicate launch, per-node promotion with instant rollback), derived from
`docs/CODEX_ROADMAP_PROPOSAL.md` §3.4. **invariants.md is untouched** —
the draft has no effect until the user records a named DG-C ruling in
`docs/DECISIONS.md`; C1 (ExecutionBackend seam) and later Phase C slices
stay blocked behind that gate.

## 0.20 Goal 3 C1: ExecutionBackend seam (2026-07-22, not deployed)

`app/execution_backend.py` adds the `ExecutionBackend` Protocol (prepare/
launch/inspect/stop/collect/cleanup) approved by DG-C (0.19), plus
`SSHExecutionBackend`, a zero-behavior-change wrapper around the existing
pure builders (`build_mkdir_command`, `build_launch_command`,
`build_run_sh_content`) and existing functions (`reconcile_job`,
`pull_job_results`). No remote command string, call order, or timeout
changed. `app.scheduler.dispatch_job()` now routes through
`backend.prepare()` + `backend.launch()` internally — its external signature
and observable SSH call sequence are unchanged (proven by
`tests/test_execution_backend.py`, which golden-tests every verb against the
pre-existing direct call path). The `stop` (approvals.py) and `collect`
(jobfinish.py) call sites are **not** rewired in this slice — their
surrounding audit/exception handling is tightly coupled to the raw call, so
cutover is deferred until C2 (a real second backend) needs a place to
choose between them. `cleanup()` is a documented no-op: the SSH backend has
never deleted remote `agent_jobs/{id}/`, and this preserves that. This
closes gate G3's downstream C1 item; no invariant changed (INV-SSH-1's
2026-07-19 text already named C1 as the seam that must prove SSH-path
command strings stay verbatim).

## 0.21 Phase A scope cut: A2–A4 removed, DG-A withdrawn (2026-07-25)

User ruling (`docs/DECISIONS.md` 2026-07-25): the Runner sandbox-enforcement
line of work is withdrawn. A1 (`GET /codex-runner/sandbox-preflight`, §0.17)
stays deployed unchanged as a read-only diagnostic. A2 (bwrap/cgroup/quota
finalization wrapper), A3 (retiring
`ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION`), and A4
(Runner canary) are removed from `docs/GOAL_3_FUTURE_WORK_PLAN.md`; the two
host prerequisites that blocked them (cgroup CPU user delegation, ext4
project quota) no longer need to be applied. `DG-A` (final resource numbers
and enablement conditions) is withdrawn as a pending decision — there is no
longer a gate for it to unlock.

This changes the status of the §0.9 High residual risk: post-agent Git
finalization running under the Runner OS user with no CPU/memory/disk/
network-namespace isolation is no longer "pending an A2 fix" — it is a
long-term accepted state, made explicitly and by name rather than dropped
silently. `ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION`
remains permanently in place rather than being retired by a future
preflight-passed condition. No other Goal 3 phase, invariant, or prior
ruling is affected.

## 0.22 Goal 3 B4: dataset pre-warm proposals (2026-07-25, not deployed)

DG-B4 approved (`docs/DECISIONS.md` 2026-07-25) and implemented behind two
default-off brakes, so current behavior is bit-identical until an operator
releases both.

`app/dataset_prewarm.py` holds the policy as a **pure function**
(`evaluate_prewarm_candidates()`, no DB/SSH/side effects, mirroring
`app/auto_placement.py`): targets are `enabled` machines whose
`dataset_cache` is empty; the chosen dataset is the one cached on the most
*other enabled* machines (data gravity), ties broken by smaller
`size_bytes` then `(name, version)` lexicographic order so the same input
always yields the same output. Disk headroom is a fail-closed pre-check
against monitor's already-probed `ServerState.disk_avail_bytes` (unknown =
no proposal) using the existing `SPACE_SAFETY_FACTOR` — **the proposal tick
never SSHes**, following `build_dispatch_plan()`'s "pure DB query" rule; the
authoritative `check_disk_space()` df probe still runs at dispatch time in
`app/scheduler.py` for every sync job, unchanged. At most one proposal per
machine per tick.

`dataset_prewarm` is the 33rd approval kind and is **never** in
`maybe_auto_approve()`'s `enqueue|stop` whitelist. Approval revalidates the
flag, dataset existence, target config presence/enabled state, and
not-already-cached, then builds the sync job through the **existing**
`dataset_remote_dir()` / `build_sync_script()` / `enqueue_job(type="sync")`
sequence, stamping `target_server`/`dataset_name`/`dataset_version` exactly
like the manual pinned-enqueue path — so the disk check, `finalize_sync_job()`
manifest verification and `dataset_cache` registration all work unchanged.

`AppState.dataset_prewarm_loop()` starts unconditionally with the other
background loops and no-ops each tick unless `DATASET_PREWARM_V1_ENABLED`
is true **and** `DATASET_PREWARM_KILL_SWITCH` is false. A failed tick logs a
warning and the loop survives; a single failing candidate does not block the
others.

Verification: `tests/test_dataset_prewarm.py` — 35 tests (policy matrix
including determinism/fail-closed disk/data gravity, proposal dedupe and
cooldown scoping, flag fail-closed, approve-time revalidation, a golden test
asserting the sync command is byte-identical to the manual path, the
never-auto-approved boundary, and tick/loop brake gating). Related suites
(approvals, autoapprove, datasets, scheduler, config, auto_placement,
authorization shadow): 308 passed. Frontend surfaces: 30 passed. Full suite:
**2602 passed**, 1 failed — the single failure is the pre-existing
`test_request_update_server_tool_creates_approval` environment gap (this dev
machine has no `~/.ssh/id_rsa`), confirmed unrelated by running it in
isolation; the pass count is exactly the prior 2567 baseline plus the 35 new
tests. Static invariant gate PASS (33 kinds). No real worker, credential,
runtime `jobqueue.db`/`audit.jsonl`/`servers.yaml` or production service was
contacted.

## 0.23 Goal 3 C2: Node Agent protocol and package (2026-07-25, not deployed)

Phase C's C2 slice, implementing the `INV-NODE-*` invariants approved by DG-C
(2026-07-19). Everything is behind `NODE_AGENT_V1_ENABLED` (default false):
with the flag off, every node interface 404s and behavior is bit-identical to
before. **No job is routed to the node channel in this slice** — per-node
`ssh|node` routing is C3/C4. SSH remains every worker's channel.

`app/node_protocol.py` is the pure state machine (no HTTP/DB/SSH/asyncio), so
lease races, duplicate acks, heartbeat expiry and restart convergence are
testable from timestamps alone:

- `INV-NODE-2`: `can_lease()` / `can_dispatch_job()` / `evaluate_ack()`. A
  repeat poll from the same node returns the *same* attempt (no second
  attempt); another node cannot take a live lease; an **acknowledged** attempt
  is never releasable to anyone, even after arbitrary silence; a job with any
  non-terminal attempt is not dispatchable through *any* channel, SSH included.
- `INV-NODE-3`: `build_node_launcher_argv()` returns an **argv list**, never a
  shell string, and rejects any attempt id outside `[A-Za-z0-9-]`; approved
  command bytes are digest-bound and land in a file.
- `INV-NODE-4`: `heartbeat_state()`'s value domain is exactly
  `fresh|stale|unknown` — there is structurally no `failed`.
- `INV-NODE-5`: `plan_agent_restart()` never relaunches an acknowledged
  attempt; a missing process is `unknown`, not a reason to rerun.

`app/node_registry.py` binds that to SQLite: `authenticate_node()` is the sole
node entry point (format → row → constant-time `verify_secret` → revocation,
all failing with one indistinguishable error). Two additive tables (`nodes`,
`node_attempts`); credentials are stored as SHA-256 only, with a `dcn_` prefix
kept deliberately distinct from human/service tokens. `ack_node_attempt()`'s
`WHERE acked_at IS NULL` makes first-ack-wins a SQLite guarantee, so a
concurrent second ack is reported as idempotent rather than executed twice.

`app/main.py` adds 3 operator routes (`POST /nodes/enroll-request`,
`POST /nodes/revoke-request`, `GET /nodes`) and 4 agent routes under
`/node-agent/`. The agent prefix is **not** an authentication exemption:
`_AUTH_EXEMPT_ROUTES` is unchanged (still exactly the three OIDC-handshake
entries, static gate green). Instead the middleware partitions by path prefix
— node credentials work only under `/node-agent/`, and human/service/legacy
credentials do not work there at all. That partition is structural rather than
policy-based precisely because `AUTHORIZATION_MODE` is `off|shadow` and cannot
serve as a defense. `NODE_ROUTE_INTERFACES` records this as a third route
classification in the authorization catalog, kept disjoint from the public and
actor-authorized sets, so route drift still fails the coverage test.

`node_enroll` / `node_revoke` are approval kinds 34 and 35, never auto-approved.
The credential is generated **at approval**, returned exactly once in that
response, and never written to the DB, audit log, or any log line.

`agent/` is the worker-side package. It imports **no** `app.*` module so it can
ship standalone; the digest function, launcher argv, and restart rules are
therefore implemented independently on both sides and pinned equivalent by
cross-check tests (the restart rules across the full 16-case
terminal×acked×alive×expired matrix). `AttemptStore` persists attempt identity
with `fsync` + atomic replace, refuses to launch anything not acknowledged, and
refuses a second launch of the same attempt.

Verification: `tests/test_node_protocol.py` 53 tests, `tests/test_node_agent.py`
72 tests. Related suites (approvals, autoapprove, authorization coverage/shadow,
identity, security, db migration, oidc): 353 passed. Frontend suites after the
approval-card changes: 278 passed. Full suite: **2728 passed**, 1 failed — the
same pre-existing `test_request_update_server_tool_creates_approval`
environment gap (no `~/.ssh/id_rsa` on this dev machine), unrelated and
confirmed in isolation; the count is exactly the prior 2602 baseline plus 125
node tests plus 1 route-classification test. Static invariant gate PASS (35
kinds, exemption set unchanged). Nothing contacted a real worker, a real
credential, runtime `jobqueue.db`/`audit.jsonl`/`servers.yaml`, or spawned any
process — every subprocess is an injected fake.

Not in this slice (C3/C4): routing jobs to nodes, per-node `ssh|node`
selection, the ≥100-job/≥2-node/7-day canary, and operational liveness views.

## 0.24 Goal 3 C3 (code portion): per-node routing and SSH double-dispatch guard (2026-07-25, not deployed)

The implementable half of C3. The canary itself is **not** done and cannot be
done from a development session — see the end of this section.

**Per-node routing (INV-NODE-6).** `ServerConfig.execution_backend` is a new
per-machine field, `ssh` (default) or `node`. `resolve_execution_backend()`
fail-closes to `ssh` whenever `NODE_AGENT_V1_ENABLED` is off or the value is
anything other than those two, so old `servers.yaml` files and typos keep
working exactly as before. A machine set to `node` is skipped by the
scheduler's SSH dispatch loop — its work is meant to be pulled by that
machine's agent instead. Reverting the field restores SSH dispatch on the very
next tick with no data migration, and one machine's setting never affects
another (tested). This is deliberately a per-machine field rather than a global
switch, which INV-NODE-6 forbids.

**Double-dispatch guard (INV-NODE-2).** `job_is_dispatchable()` is now called
before SSH dispatch: a job with a live lease, or with an acknowledged attempt
that has not reported terminally, is skipped — permanently, if necessary. This
is the real enforcement point for "the control plane must not hand the same
attempt to another channel, SSH included." An acknowledged-then-silent attempt
blocks SSH forever rather than being rerun, because silence is `unknown`, not
failure (INV-NODE-4). A blocked job does not starve other work in the same tick
(tested). With no nodes enrolled the guard is always True, so current
deployments see zero behavior change.

**`NodeExecutionBackend`** implements the C1 `ExecutionBackend` contract as a
second backend. `prepare()`/`launch()`/`cleanup()` are deliberate no-ops — the
control plane never connects outward on this channel — and `inspect()` derives
state from persisted attempts, mapping silence to `running` and never to
`failed`. `stop()` and `collect()` raise `NotImplementedError` rather than
pretending: a stop-request needs the agent to fetch it on its next poll, and
result upload is agent-initiated; both land with C4. Nothing calls this class
yet.

`GET /servers` now reports the **effective** `execution_backend` (after
fail-closed resolution), so an operator can see which channel a machine is
actually on rather than what the file says.

Verification: `tests/test_node_routing.py` 32 tests (fail-closed value matrix,
per-machine independence, revert-without-migration, live-lease and acked-attempt
SSH blocking, terminal release, no-starvation, backend contract). Related
suites (scheduler, node protocol/agent, execution backend, config, server
config API, codex runner pool): 324 passed. Frontend: 93 passed. Full suite:
**2760 passed**, 1 failed — the same pre-existing `~/.ssh/id_rsa`
environment-gap failure; the count is exactly the prior 2728 baseline plus 32
routing tests. Static invariant gate PASS.

**Not done, and not doable from here:** the C3 canary gate — at least 100
non-production ordinary jobs across at least two nodes over seven consecutive
days with zero duplicate launches, zero false disconnect failures, and zero
lost terminal results, plus an observed rollback drill. That requires a real
second machine and a real time window (gate G4). C4 (per-node promotion, Codex
Runner migration) depends on it.

## 0.25 Goal 3 C3: stop-request protocol (2026-07-25, not deployed)

Corrects a scoping error in §0.24. That section said stop-request belonged to
C4; re-reading `docs/CODEX_ROADMAP_PROPOSAL.md`, Phase 3's deliverables list
"stop-request" among the **protocol** items, i.e. this slice, not per-node
promotion. Implemented accordingly.

Two additive `node_attempts` columns (`stop_requested_at`, `stop_acked_at`)
with an ALTER-TABLE migration for any DB already created by the C2 commit.
`request_job_stop()` records an approved stop against every non-terminal
attempt of a job; `NodeExecutionBackend.stop()` now calls it instead of raising.

The semantics differ from SSH deliberately and the code says so: the control
plane has no inbound channel and **cannot kill a remote process**. It records a
request; the agent picks it up and stops itself. Recording a request therefore
never changes job or attempt status — convergence still requires the agent's
terminal report (INV-NODE-4). A stop-requested attempt continues to block SSH
dispatch, because "asked to stop" is not "finished".

Delivery has two paths so a long-running job cannot miss a stop between polls:
the `poll` response carries `stop_requested`, and so does `heartbeat`. Both are
scoped to the owning node — another node's heartbeat never sees or acks it. The
new `POST /node-agent/stop-ack` (5th agent route, 113 total) is a delivery
receipt only: it records `stop_acked_at` and explicitly does not converge the
attempt, so an operator can distinguish "not delivered yet" from "delivered,
still stopping".

Verification: 18 new tests across `tests/test_node_routing.py` (idempotent
request, terminal attempts skipped, receipt-only ack, wrong-node refusal,
backend records rather than pretends, still blocks SSH) and
`tests/test_node_agent.py` (both delivery paths, cross-node isolation, full
request→receipt→terminal convergence, client parsing). Related suites: 307
passed. Full suite: **2777 passed**, 1 failed — the same pre-existing
`~/.ssh/id_rsa` environment-gap failure; the count is the prior 2760 baseline
plus 18 new tests minus 1 (the parametrized stop/collect "does not pretend"
test became a single collect-only test, since `stop()` is now implemented).
Static gate PASS.

**Still not implemented:** agent-initiated result/artifact upload.
`NodeExecutionBackend.collect()` continues to raise `NotImplementedError`
rather than pretend.

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
