# Goal 1 — Actor Identity and Authorization Shadow Mode

> Approved implementation specification for Goal 1. `PLAN.md` is historical
> evidence only. Slices 1–6 were completed sequentially. On 2026-07-13 the user
> explicitly approved the narrow protected-invariant changes and Authlib
> dependency required for Slice 7. Slice 7 implementation completed
> dependency-complete release validation on 2026-07-13. This is not a claim
> that it was deployed or exercised against a real provider.

## Fixed boundaries

- Preserve optional shared-token HTTP authentication, WebSocket first-message
  authentication, browser token fallback, MCP `X-Auth-Token` forwarding, all
  approval/queue/SSH/Codex Runner behavior, immutable approved payloads,
  DB-before-side-effect ordering, and unknown/unreachable semantics.
- Authorization modes are exactly `off|shadow`; default `off`. Shadow denials
  are observational and must never alter responses, collections, mutations,
  approvals, SSH calls, or tool execution.
- Do not implement enforcement, Node Agent, Scheduler v2, PostgreSQL, SSH
  removal, canonical approval hashes, or historical audit rewriting.
- Roles are Viewer, Operator, Admin, plus global `platform_admin`. Service
  identities require explicit scopes and receive a policy denial for approval
  decisions and identity administration; Goal 1 records rather than enforces it.
- `source=web|api|vllm|chatgpt` remains channel metadata, never identity.
- No production/runtime DB, server, worker, credential, identity provider,
  `jobqueue.db`, `audit.jsonl`, or `servers.yaml` may be accessed.

## Slice 1 — Additive identity persistence foundation

### Objective and scope

Add persistence/domain/secret primitives only. Do not change middleware,
routes, public serializers, approval execution, audit emission, policy runtime,
OIDC networking, frontend, MCP, scheduler, SSH, or Codex Runner.

### Database and interfaces

Add complete tables to `app.db.SCHEMA`: `actors`, `oidc_identities`,
`oidc_login_flows`, `actor_sessions`, `service_accounts`,
`service_account_tokens`, and `project_memberships`. Use Python validation and
weak references; do not add cascades.

Add nullable `approvals.requester_actor_id`, `decision_actor_id`, and
`decision_mechanism` to both fresh schema and guarded
`_APPROVAL_COLUMN_MIGRATIONS`; create their indexes only after legacy ALTERs.
Historical rows stay NULL and payload bytes never change.

Add `app/identity.py` with identity dataclasses/enums, frozen `RequestContext`,
high-entropy secret generation, SHA-256 hashing, constant-time verification,
service-token parsing, and redaction. Raw credentials and hashes must be
excluded from representations. Add identity CRUD to `Database`.

### Tests and acceptance

Extend `tests/test_db_migration.py`; add `tests/test_identity.py`. Prove fresh
schema, literal ten-table legacy preservation, idempotent reopen, no fabricated
actors/memberships, approval NULL attribution, byte-stable temporary audit,
identity CRUD/uniqueness, one-time flow consumption, token/session revocation,
and database-wide raw-secret absence.

Focused: `python -m pytest -q tests/test_db_migration.py tests/test_identity.py`.
Related: `tests/test_api.py tests/test_approvals.py tests/test_project_instances.py
tests/test_project_detail_api.py`. Then full pytest, static invariant gate, diff,
status, schema, and secret checks.

Rollback: previous code runs against additive schema; never drop or rewrite.
Stop on destructive migration, historical backfill/rewrite, raw-secret storage,
new dependency, or runtime behavior change. Protected: INV-STATE-1/3,
INV-TEST-1; all approval/audit/SSH/state behavior remains unchanged.

## Slice 2 — Central action catalog and pure policy evaluator

### Objective and scope

Add a complete, pure policy and coverage inventory without invoking it at
runtime. No authentication, audit, enforcement, filtering, or side effects.

### Interfaces

Add `app/authorization.py` with `Action`, resource scope/kind, route specs,
decision/reason types, resource-resolution helpers, and pure evaluator. Actions:
`project.view`, `project.operate`, `project.admin`,
`project.membership.manage`, `approval.view`, `approval.decide`,
`platform.view`, `platform.manage`, `audit.view`, `identity.self.view`, and
`identity.manage`.

Viewer gets project view; Operator adds operate; Admin adds project admin,
membership, and project approval actions; platform admin gets all. Anonymous
and cross-project access deny. Service identities additionally require token
scope and deny approval/identity administration. Same-actor high-risk approval
is represented as a policy denial only.

Catalog every FastAPI HTTP/WS route and explicitly classify framework/static
routes. Extend every local `ToolSpec` with action/resource metadata. Add a
string-only `MCP_TOOL_ACTIONS` catalog without importing `app.*` into the bridge.
Datasets are visible through existing project bindings; orphan datasets,
projectless jobs, servers, inventory, audit, and platform operations are global.

### Tests and acceptance

Add `tests/test_authorization.py` and `tests/test_authorization_coverage.py`.
Table-test the role/scope/cross-project matrix. Coverage fails for any unmapped
HTTP route, WS route, local tool, or MCP tool, with explicit generated-route
exclusions. Prove no production entrypoint calls the evaluator.

Focused/related: authorization tests plus `test_agent_tools.py`,
`test_agent_endpoints.py`, and `test_mcp_bridge.py`; then full/static/diff/status.
Rollback removes unused pure catalog. Protected: INV-LLM-2/3/4, INV-TEST-2.

## Slice 3 — Compatible RequestContext and authentication transports

### Objective and scope

Resolve immutable contexts for development-open, legacy shared-token,
service-token, and server-side-session callers. Preserve all legacy decisions;
do not evaluate policy yet and do not add unauthenticated routes.

### Interfaces and configuration

Add `LEGACY_SHARED_TOKEN_ENABLED=true`,
`SERVICE_TOKEN_AUTH_ENABLED=false`, and
`SESSION_COOKIE_NAME=dispatch_session`. Attach HTTP context to request state;
keep the exact legacy 401. WS accepts a valid session without consuming a
message and otherwise preserves the exact legacy first-message/1008 protocol.
Propagate context through agent/chat paths without evaluating it.

MCP adds optional outbound `DISPATCH_SERVICE_TOKEN` bearer while preserving
`AUTH_TOKEN`/`X-Auth-Token`; inbound `MCP_BRIDGE_TOKEN` remains separate and
the bridge stays isolated. Add authenticated `GET /auth/me`, returning only
safe actor/membership/scope metadata.

`AUTH_TOKEN` unset remains open with anonymous/development context. Existing
browser/localStorage, source labels, response shapes, and mutations remain.

### Tests and acceptance

Add `tests/test_request_context.py`; extend auth, WS, agent endpoint/runtime/tool,
chat, and MCP tests for compatibility, expiry/revocation, propagation, exact
401/WS behavior, and leakage. Run focused/related/full/static/diff/status.

Rollback disables service auth, enables legacy compatibility, revokes new
credentials, and retains rows. Stop if INV-APPROVAL-5 exemptions are needed.

## Slice 4 — Actor-aware approvals and append-only audit

### Objective and scope

Populate requester/decision attribution for all approval kinds and append actor
identity to new audit records. Never mutate payloads or historical evidence.

### Interfaces

`append_audit()` gains an optional top-level actor envelope `{id, kind,
authentication}`; existing `params` stay unchanged. Background lifecycle events
use explicit `system`, never inherited request context.

Every existing approval request seam writes requester actor. `approve()`,
`reject()`, web-direct, auto-rule, and stale-state decision paths write decision
actor and mechanism `manual|web-direct|auto-rule-N`. Auto-rule records its
initiating actor without fabricating a human. Add nullable actor fields to
approval serialization while preserving old keys/nesting and `approved_by`
audit params/notes.

### Tests and acceptance

Cover every kind, manual/reject/web-direct/auto/stale decisions, source spoofing,
agent/chat/WS/MCP propagation, payload byte equality, system background actor,
and historical JSONL compatibility. Run approval/API/autoapprove/auth/agent/chat/
WS/MCP focused suites, related kind suites, full/static/diff/status.

Rollback: old code ignores columns and extra top-level audit keys. Protected:
INV-APPROVAL-1/2/3/4, INV-AUDIT-1/2, INV-LLM-1/2/4.

## Slice 5 — Shadow evaluation across every route and tool

### Objective and scope

Invoke the Slice 2 evaluator everywhere, fail open, and never enforce.

### Interfaces

Add `AUTHORIZATION_MODE=off|shadow`, default `off`; reject `enforce`. A global
route dependency/helper evaluates selected route/resource after authentication;
WS evaluates after its protocol. Local `dispatch_tool()` evaluates ToolSpec
metadata. MCP tools map to their underlying HTTP action without trusting an
informational interface label as identity.

Would-deny appends best-effort `authorization_shadow_denied` with action,
resource, project, route/tool, reason, principal kind, and `shadow_mode=true`;
actor remains in the standard envelope. Evaluator/audit failures append a
best-effort error and continue. Collections may report denials but are never
filtered. No 403/404, denial, autoapproval, approval, queue, SSH, or Codex change.

### Tests and acceptance

Add `tests/test_authorization_shadow.py`. Compare `off` versus `shadow` on fresh
equivalent state for allowed/would-deny read, direct DB mutation, approval
creation/decision, web-direct/auto enqueue and stop, FakeSSH read, local/MCP
tool, and WS. Normalize only timestamps/IDs; responses, domain DB mutations,
and fake side effects must match. Only shadow audit may differ. Run coverage,
protected approval/queue/scheduler/security suites, full/static/diff/status.

Rollback sets mode `off`; keep evidence. Any blocked or filtered request is a
stop condition. All protected invariants remain unchanged.

## Slice 6 — Approval-gated service-account, token, and membership lifecycle

### Objective and scope

Expose issuance/revocation/membership interfaces through the existing approval
workflow. No OIDC and no authorization enforcement.

### Database and interfaces

No new tables. Add approval kinds `service_account_create`,
`service_token_issue`, `service_token_revoke`, `project_membership_upsert`, and
`project_membership_remove`; never add them to enqueue/stop autoapproval.

Add `IDENTITY_ADMIN_ENABLED=false` rollback switch and routes:

- `GET /identity/service-accounts`
- `POST /identity/service-accounts/request`
- `POST /identity/service-accounts/{actor_id}/tokens/request`
- `POST /identity/service-tokens/{token_id}/revoke-request`
- `GET /projects/{name}/memberships`
- `POST /projects/{name}/memberships/request`
- `POST /projects/{name}/memberships/{actor_id}/remove-request`

Token issue payload contains account/label/scopes/expiry only. Generate raw
token only during approval, persist only its hash, and return raw token in that
single response. Lists, later reads, audit, approval payloads, and logs expose
no raw token/hash. Rotation is issue-new, verify-new, revoke-old.

### Tests and acceptance

Add `tests/test_service_tokens.py` and `tests/test_identity_api.py`. Prove
one-time display, database/audit/log redaction, immediate expiry/revocation,
scope validation, rotation, membership idempotency, approval-time revalidation,
autoapproval exclusion, actor-aware audit, and shadow-only service approval
denial. Run auth/audit/migration/API/agent/MCP/approval related suites, then
full/static/diff/status and secret scans.

Rollback disables identity admin/service auth, revokes credentials, enables
legacy shared token, and retains rows/audit. Protected: INV-APPROVAL-1/2/3/4,
INV-AUDIT-1/2, INV-LLM-1/2, additive compatibility.

## Slice 7 — OIDC, server sessions, and browser transition (validated worktree)

### Approved prerequisite decisions

The user explicitly approved only these changes:

- `INV-APPROVAL-5` adds `GET /auth/login` and `GET /auth/callback` as the only
  unauthenticated OIDC handshake routes. `GET /auth/me`, `POST /auth/logout`,
  every other application route, and any other method on those paths remain
  authenticated. `/static/*` remains the only exempt prefix.
- `INV-APPROVAL-1` classifies OIDC login-flow creation/atomic consumption,
  session creation, logout revocation of the presented session, and strict
  `(issuer, subject)` identity binding as a closed authentication-bookkeeping
  list. Project membership and service-account/token lifecycle remain
  approval-gated. Email cannot identify or merge actors, and administration
  cannot be first-login-wins.
- Add reviewed `Authlib>=1.7,<2.0` for the production provider adapter.

No other invariant or dependency change is authorized by this slice.

### Objective and interfaces

Add human browser login without enabling authorization enforcement. Use OIDC
Authorization Code with PKCE S256 through an injected provider interface; the
production adapter uses Authlib, while tests inject a fake provider and never
contact a real IdP.

Interfaces:

- unauthenticated `GET /auth/login` begins a browser-bound, expiring flow and
  validates an optional same-origin root-relative `return_to`;
- unauthenticated `GET /auth/callback` atomically consumes the flow, rejects
  expiry/replay, validates the provider response, binds by exact issuer and
  subject, and issues a server-side session;
- authenticated `GET /auth/me` returns only safe current-principal metadata;
- authenticated `POST /auth/logout` revokes only the verified presented
  session and clears browser cookies; and
- the browser displays current-user state, offers OIDC sign-in/logout, waits
  for authentication before protected polling/WS, and retains the legacy
  shared-token fallback.

State and nonce are persisted as hashes with expiry and one-time consumption.
To survive a process restart, an active flow row temporarily contains the raw
PKCE verifier; atomic consumption returns it while setting the durable column
to `NULL`. Expired and consumed flow rows are removed opportunistically by
later login/callback activity, so the SQLite file and backups remain sensitive.
Session secrets are stored only as hashes and are expiring/revocable. Session
and flow cookies are `Secure`, `HttpOnly`, and `SameSite=Lax`; the flow cookie
is scoped to the callback path. Provider access and refresh tokens are never
persisted.

The provider must validate an asymmetric ID-token signature through discovery
JWKS plus exact issuer, client audience, expiry, and nonce. The application
defensively rechecks those claims. Email/name are descriptive metadata only.
`OIDC_PLATFORM_ADMIN_SUBJECTS` is an exact, case-sensitive subject allowlist
consulted only when a new issuer-subject binding creates its actor; changing the
allowlist never promotes an existing actor.

Legacy shared-token HTTP/WS behavior, OIDC-disabled open development,
service-token authentication, MCP forwarding, agent/chat context propagation,
SSH, scheduler, approvals, Codex Runner, and `AUTHORIZATION_MODE=off|shadow`
remain compatible. Enforcement is not a supported mode.

### Acceptance and operational documentation

Fake-provider and adapter tests cover PKCE, durable hashed correlation,
browser binding, expiry, atomic consumption/replay, signature/JWKS, issuer,
audience, expiry, nonce, issuer-subject identity, email non-identity, explicit
admin bootstrap, session expiry/revocation, logout, exact route exemptions,
credential-free errors, and log/audit leakage. Related auth, configuration,
RequestContext, WebSocket, MCP, agent, authorization coverage/shadow, and
frontend regressions must pass, followed by the dependency-complete full suite,
static invariant gate, `git diff --check`, exact exempt-route inspection, and
secret scan.

Validation completed in the disposable dependency-complete Python 3.10
environment `/tmp/dispatch-center-goal1-py310` without enabling network access
or contacting an IdP:

- Checkpoint A focused backend/provider/config/identity/migration suite:
  **200 passed**; related auth/RequestContext/WS/MCP/shadow/identity suite:
  **407 passed**; dependency-complete full suite: **1754 passed**.
- Checkpoint B browser/OIDC/WS/agent/MCP/API/approval/security compatibility
  suite: **454 passed**; dependency-complete full suite: **1754 passed**.
- The static invariant gate, `git diff --check`, exact exempt-route inspection,
  credential-log/audit scan, compilation, and dependency check passed. The two
  full runs reported only the existing multipart deprecation and asynchronous
  subprocess cleanup warnings.

These results validate the Slice 7 source state tested on 2026-07-13. No
deployment, production database migration, real-provider request, or
production-system access was performed.

`.env.example`, `README.md`, and `使用說明書.md` document IdP client
registration, all OIDC settings, HTTPS/Secure-cookie requirements, upstream
callback-query log suppression, current-user/logout behavior, subject-based
admin bootstrap, non-enforcement, compatibility, and rollback.

Rollback: first provision/rotate and verify `AUTH_TOKEN` while
`LEGACY_SHARED_TOKEN_ENABLED=true` and authorization remains `off`; only then,
preferably in the same maintenance-window restart, set `OIDC_ENABLED=false`.
Disabling OIDC with no `AUTH_TOKEN` would enter open-development mode. Verify
unauthenticated APIs return 401, legacy calls succeed, and login/callback return
404. Revoke current sessions or rotate `SESSION_COOKIE_NAME` (retaining the
rotated name until old sessions expire) to invalidate existing browser cookies,
and preserve additive identity/session rows and append-only audit. Previous code
may run against the additive schema. Restore a backup only for verified
schema/data integrity failure; never use a destructive down-migration or
historical rewrite.

## Gate after every slice

Use a disposable dependency-complete environment. Run exact focused and related
tests, full `pytest -q`, the static invariant gate, `git diff --check`, test
name-status/deletion inspection, schema compatibility, and credential leakage
checks. Fix implementation-caused failures before advancing. Do not commit,
push, deploy, or access production/runtime resources.
