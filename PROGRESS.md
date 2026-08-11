# Dispatch Center Product v2 Progress

> Updated: 2026-08-11
>
> Execution authority: `PLAN.md` and its byte-identical mirror
> `docs/DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md`.
>
> Status vocabulary follows `docs/CAPABILITY_LEDGER.md`. Local implementation
> or tests do not imply enabled, deployed, canary-proven, or production-ready.

## Work packages

| Work package | Status | Dependency / next gate |
|---|---|---|
| PR-00 — Plan and Decision Baseline | complete | Accepted locally on 2026-08-07 |
| PR-01 — API v2 Foundation | complete | Accepted locally on 2026-08-07 |
| PR-02 — Multi-role RBAC | complete | Accepted locally on 2026-08-07 |
| PR-03 — OIDC and My Workspace | local complete; external acceptance pending | Real non-production IdP credentials are unavailable; carried as a PR-12 entry gate |
| PR-04 — Project Bootstrap | complete | Accepted locally on 2026-08-07 |
| PR-05 — Environment Revisions | complete | Accepted locally on 2026-08-07 |
| PR-06 — Run Template Specs and Defaults | complete | Accepted locally on 2026-08-09 |
| PR-07 — Dataset Assets, Aliases and Lineage | complete | Accepted locally on 2026-08-09 |
| PR-08 — Dataset Sharing | complete | Accepted locally on 2026-08-09 |
| PR-09 — Dataset Publish Wizard | complete | Accepted locally on 2026-08-09 |
| PR-10 — ExecutionPlan v2 | complete | Accepted locally on 2026-08-10 |
| PR-11 — Product Run Experience | complete | Accepted locally on 2026-08-11 |
| PR-12 — 117 SSH Pilot | pending | PR-01 through PR-11, exact release candidate, external Pilot entry gate |
| PR-13 — Legacy API Retirement | gated | Stable v2 release, client migration, 30 days zero legacy-route calls, rollback artifact, Code Owner approval |

## PR-00 — Plan and Decision Baseline

Status: complete

Outcomes:

- Preserved the approved bytes of both Product v2 plan copies and added a
  byte-equality regression test.
- Marked the old current-system document as a non-authoritative Product Brief
  and UX roadmap, linked the execution authority and capability ledger, and
  corrected stale current-state claims.
- Recorded `DG-PRODUCT-V2-BASELINE-v1` for authorization enforcement,
  multi-role RBAC, additive API v2 cutover, atomic Project bootstrap, and
  two-sided exact-snapshot Dataset sharing.
- Preserved the historical 2026-07-16 D7 text. The new decision supersedes
  only its current-status conclusion and does not backdate authorization.
- Split authorization shadow and enforcement into distinct capability rows.
  Enforcement is recorded as implemented locally, default off, not deployed,
  not canary-proven, and not production-ready.

Sol decision:

- `SOL_DECISION` from the read-only `sol_advisor`: adopt the additive
  `DG-PRODUCT-V2-BASELINE-v1` record, preserve historical D7, keep both
  authorization rows, and protect the two approved PLAN copies with a
  byte-equality test.
- No unresolved PR-00 Sol decision remains.

Validation:

```text
Focused documents/config/auth tests:
  116 passed

Ruff:
  All checks passed

mypy:
  Success: no issues found in 118 source files

Control-plane and Node-agent builds:
  PASS (artifacts written outside the repository under /tmp)

Coverage gate:
  1085 passed
  total coverage 38.90% (required 35%)

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok

Frontend smoke and JavaScript syntax:
  PASS

Complete offline suite:
  3601 passed in 805.99s

git diff --check:
  PASS

PLAN mirror:
  byte-identical
  SHA-256 0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

Formatting note:

- The repository deliberately configures correctness-oriented Ruff lint
  without a formatter gate. No formatter command is defined; `git diff
  --check` is the applicable whitespace gate and passed.

## PR-01 — API v2 Foundation

Status: complete

Outcomes:

- Registered an empty `/api/v2` router boundary without adding a probe or
  product resource and without changing the legacy or OpenAPI contract.
- Added default-off `API_V2_ENABLED`, typed feature metadata, safe settings
  reporting, and generic v2 404 behavior while the flag is disabled.
- Added strict canonical cursor helpers with bounded limits, query/caller
  binding, stable tie-breakers, and an enforced `limit + 1` repository result.
- Added actor- and route-scoped HTTP idempotency identities that never persist
  raw keys or credentials.
- Installed additive migration v5 for completed idempotency results and a
  single-transaction unit-of-work primitive that commits the business
  mutation, durable audit event, and replay identity together.
- Added expiry/reuse, replay/conflict, lock contention, rollback, migration,
  configuration, router, and compatibility tests.

Sol decision:

- `SOL_DECISION` `DG-API-V2-FOUNDATION-v1` fixed the empty-router boundary,
  canonical cursor and request-digest contracts, 24-hour completed-result
  idempotency, migration v5 ownership, fail-closed actor requirements, and
  no-change legacy/OpenAPI compatibility boundary.
- No unresolved PR-01 Sol decision remains.

Independent review:

- The read-only reviewer returned PASS after inspecting the implementation and
  running 186 focused tests plus Ruff, mypy, and `git diff --check`.
- Its two non-blocking database/cursor hardening suggestions were incorporated
  and revalidated.

Validation:

```text
Focused PR-01 tests:
  63 passed

Expanded API/config/migration/UoW tests:
  284 passed

Ruff:
  All checks passed (app, agent, dispatch_center, scripts, tests)

mypy:
  Success: no issues found in 122 source files

Control-plane and Node-agent builds:
  PASS (artifacts written outside the repository under /tmp)

Coverage gate:
  1100 passed
  total coverage 38.86% (required 35%)

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok

Frontend smoke and JavaScript syntax:
  PASS

Complete offline suite:
  3660 passed in 810.14s

git diff --check:
  PASS

PLAN mirror:
  byte-identical
  SHA-256 0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

Formatting note:

- The repository still has the pre-existing broad Ruff formatter delta (177
  files). The project does not define formatting as a quality gate; scoped new
  files pass their formatter check, correctness-oriented Ruff lint and
  `git diff --check` both pass, and unrelated files were not bulk-reformatted.

## PR-02 — Multi-role RBAC

Status: complete

Outcomes:

- Added additive migration v6 with independent, provenance-bearing
  `project_role_bindings`, a partial unique active binding, deterministic
  actor-safe legacy mapping, rollback/retry coverage, and source checksum
  validation.
- Added the five independent Product roles (`Owner`, `Operator`, `Reviewer`,
  `Dataset Manager`, `Viewer`), union-based action evaluation, multi-role
  request context, cross-Project fail-closed behavior, and service-actor
  restrictions without role hierarchy.
- Added dynamic Project readiness requiring an enabled human Owner and two
  distinct approval-capable enabled humans. Repair is monotonic; corrupt
  non-human privileged bindings remain fail-closed and are not silently
  removed.
- Added immutable high-risk `project_role_change` approvals with requester,
  target, digest, SoD, current-state, last-Owner, approval-pair, and durable
  audit revalidation in one transaction.
- Added the gated Product v2 roles read, role-change request, and approval
  decision routes with canonical cursor pagination, idempotency, opaque
  cross-Project 404 behavior, and rollback-safe durable auditing.
- Kept Product v2 roles independent from legacy membership. Only an approved
  v1 membership decision synchronizes legacy-provenance bindings; direct
  legacy helpers never invent approval provenance, and v2 mutations never
  project back to v1.
- Classified `project_role_change` as transaction-only for generic
  approve/reject paths. It is not fabricated as a JSONL action; the audit
  inventory records an action-empty durable `identity.project_role_binding`
  mutation family while canonical approval/membership events retain unique
  ownership.
- Added default-off `PRODUCT_RBAC_V2_ENABLED`, dependent on the default-off
  API v2 gate, plus typed settings, safe configuration reporting, capability
  ledger status, migration guidance, and the `DG-PRODUCT-RBAC-V2-v1` record.

Sol decisions:

- The initial `SOL_DECISION` fixed migration v6, five-role semantics,
  readiness/repair, service restrictions, high-risk separation of duties,
  v1/v2 compatibility, the three v2 routes, and the dual default-off gates.
- A follow-up decision fixed approved-v1-only synchronization, fail-closed
  corrupt non-human privilege, the legacy observed-timestamp caveat, and
  opaque cross-Project approval lookup.
- A final audit decision classified `project_role_change` as a closed
  transaction-only kind and required the action-empty durable mutation
  inventory rather than a misleading legacy audit action.
- No unresolved PR-02 Sol decision remains.

Independent review:

- The read-only reviewer returned PASS on the implementation and later on the
  legacy-provenance overlap fix. Its focused test, Ruff, mypy, and diff checks
  passed; final integration and diff review remained with the main thread.

Validation:

```text
Focused Product RBAC suite:
  92 passed

Approval/audit boundary suite:
  184 passed

Migration v6 source/checksum tests:
  PASS
  checksum 3ef6a9b2145afb762bb4039eff06c906fd660496c40dbfa4b6ada2dce9d9d254

Ruff:
  All checks passed (app, agent, dispatch_center, scripts, tests)

mypy:
  Success: no issues found in 124 source files

Control-plane and Node-agent builds:
  PASS (artifacts written outside the repository under /tmp)

Coverage gate:
  1114 passed
  total coverage 38.80% (required 35%)

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok

Frontend smoke and JavaScript syntax:
  PASS

Complete offline suite:
  3771 passed in 1061.75s

git diff --check:
  PASS

PLAN mirror:
  byte-identical
  SHA-256 0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

Formatting note:

- The repository still does not define a formatter gate. New and directly
  edited formatter-safe files pass scoped checks; correctness-oriented Ruff
  lint and `git diff --check` pass, and legacy files were not bulk-reformatted.

## PR-03 — OIDC and My Workspace

Status: local complete; external acceptance pending credentials

Outcomes:

- Added gated, authenticated Product v2 `GET /api/v2/me`,
  `GET /api/v2/me/sessions`, and `GET /api/v2/workspace` read models under the
  reviewed `identity.self.view` authorization action. API v2 disabled always
  hides the routes with `404`, including when OIDC or legacy-token settings are
  otherwise enabled.
- Kept platform-admin state separate from effective Product roles. Product RBAC
  mode uses durable bindings; rollback mode exposes the deterministic effective
  legacy mapping without persisting synthetic bindings.
- Added actor-isolated, bounded, deterministic session pagination with an
  actor-bound opaque cursor. Public session rows contain state and timestamps
  only: no session, actor, binding, grant, credential, subject, issuer, group,
  token, hash, or raw database identifiers are exposed.
- Added a row-filtered My Workspace summary for authorized Projects, legacy Job
  summaries, and pending approvals, including server-authoritative
  `can_decide`. Dataset Assets remain an explicit stable empty/unavailable
  section until PR-07; no future capability is presented as implemented.
- Added a separate role-aware Product v2 browser shell. It reads only the three
  Product endpoints above, uses `/auth/login` and `/auth/logout` only for the
  authentication handshake, treats navigation as presentation rather than
  authorization, and stores a fallback legacy token in memory only.
- Removed persistent legacy-token browser storage from the legacy shell and
  extended the frontend smoke gate to reject `localStorage`, `sessionStorage`,
  and `indexedDB` use in both shells.
- Added the real non-production IdP acceptance and rollback runbook. Live IdP
  evidence is explicitly `pending_external_credentials`: the four required
  ambient OIDC variables were absent, and no HTTPS callback/client-management
  authority is available in this workspace. No live pass is claimed.

Sol decision:

- `SOL_DECISION` fixed the safe fields and gates for `/me`, sessions, and My
  Workspace; required durable row filtering before ordering/limiting; kept
  legacy Jobs explicitly labelled; deferred Dataset Assets; required an
  independent v2 shell and memory-only token handling; and required honest
  credential-gated real-IdP evidence.
- OIDC disabled remains a supported rollback mode: valid existing session,
  service-token, and legacy-token authentication continue according to their
  independent gates, while the OIDC handshake is unavailable.
- No unresolved PR-03 Sol decision remains. The external IdP exercise remains
  an explicit PR-12 Pilot entry gate.

Validation:

```text
Focused identity/workspace/OIDC/frontend suite:
  94 passed

Route and OpenAPI contract suite:
  39 passed

Broad authorization/workspace/role/frontend/API/config suite:
  668 passed

API-off pre-auth hiding regression suite:
  51 passed

Ruff:
  All checks passed (app, dispatch_center, scripts, tests)

mypy:
  Success: no issues found in 99 directly relevant source files

Static invariant checks and audit adoption gate:
  PASS

Frontend smoke and JavaScript syntax:
  PASS

git diff --check:
  PASS

Real non-production IdP acceptance:
  pending_external_credentials (not reported as passed)
```

Formatting note:

- The repository still does not define a formatter gate. New and directly
  edited formatter-safe files pass scoped checks; correctness-oriented Ruff
  lint and `git diff --check` pass, and legacy files were not bulk-reformatted.

## PR-04 — Project Bootstrap

Status: complete

Outcomes:

- Added a default-off Product v2 Project Wizard with a canonical, credential-free
  preview contract and one immutable `project_bootstrap_v2` approval payload.
- Added Migration v7 as the single schema owner for Project Environments,
  immutable Environment revisions, typed Run Profile specs, and immutable Project
  Defaults. Composite foreign keys enforce same-Project exact references at the
  SQLite boundary.
- Materialized the Project, initial role bindings, Environment revision, typed Run
  Profile revision/spec, and Defaults revision in one local transaction. Injected
  durable-audit failure, stale state, digest mismatch, UUID/name conflict, and
  requester self-decision all fail closed without partial rows.
- Kept Git, deploy, filesystem, network, server-bootstrap, and other remote side
  effects outside the bootstrap transaction.
- Added the Project Workspace read model and kind-aware Product v2 approval list
  and detail views. Approval summaries omit payloads; authorized detail verifies
  the canonical payload and digest before disclosure, uses opaque denial, and is
  non-cacheable.
- Changed the Product UI so a reviewer must load the exact immutable detail and
  digest and explicitly confirm review before approval. The v2 shell does not use
  a v1 Product API.
- Protected typed bootstrap Run Profile lineages from legacy raw update/archive at
  both request and approval time while preserving legacy-only revision behavior.
- Kept Dataset grant/alias placeholders opaque. Any non-empty placeholder blocks
  preview, returns no canonical payload/digest, and makes request creation fail
  with 409 and zero writes; OpenAPI does not claim a future element schema.
- Enforced the Environment setup-command storage boundary in UTF-8 bytes and kept
  API v2, Product RBAC, and Project Bootstrap feature flags default-off.

Sol decisions:

- The initial `SOL_DECISION` fixed the bootstrap contract, global human Platform
  Admin request boundary, independent human Platform Admin decision boundary,
  high-risk separation of duties, transaction-only approval materialization,
  Migration v7 ownership, Dataset capability gating, and rollback behavior.
- Final-review `NEEDS_CHANGES` required verified Product v2 approval reads and UI
  review, a typed-lineage legacy guard, same-Project exact-reference database
  constraints, and opaque Dataset placeholders. All four were implemented.
- The read-only `sol_advisor` then returned final `SOL_DECISION: PASS`; no unresolved
  PR-04 decision remains. This local acceptance does not authorize deployment or
  a production migration.

Validation:

```text
Consolidated PR-04, migration, authorization, OpenAPI, frontend, and document suite:
  207 passed

Coverage gate:
  1120 passed
  total coverage 38.15% (required 35%)

Complete network-denied offline suite:
  3820 passed in 881.86s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 128 source files

Control-plane and Node-agent builds:
  PASS (sdist and wheel artifacts written outside the repository under /tmp)

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok; 72 catalog entries; no required action missing

Frontend smoke and JavaScript syntax:
  PASS

git diff --check:
  PASS

PLAN mirror:
  byte-identical
```

Artifact note:

- Pre-existing untracked `jobqueue.db.migration.lock` and
  `server.log.pre-b3ed87b` remain excluded from the implementation change set.

Formatting note:

- The repository defines no formatter gate. Correctness-oriented Ruff lint and
  `git diff --check` are the applicable formatting/whitespace gates and passed.

## PR-05 — Environment Revisions

Status: complete

Outcomes:

- Reused the unchanged Migration v7 tables for immutable standalone Host
  Environment create, update, and terminal archive successors. Every accepted
  mutation appends one exact N+1 revision; names and prior revisions remain
  immutable, and archive copies rather than deletes the prior contract.
- Added the single transaction-only high-risk `environment_change_v2` approval
  contract with canonical JSON/SHA-256 bytes, exact expected head, strict
  discriminated request shapes, requester/decider separation, approve-time
  actor/RBAC/state/policy revalidation, and digest-bound decision idempotency.
- Added default-off Product v2 list/request/decision surfaces with bounded cursor
  pagination, opaque cross-Project denial, verified approval detail, explicit UI
  confirmation, and rollback hiding under
  `PROJECT_ENVIRONMENTS_V1_ENABLED=false`.
- Kept setup commands credential-free at the accepted contract boundary. A
  router-scoped validation sanitizer discards raw rejected bodies, values,
  discriminator text, unknown field names, and validation context while
  preserving the typed OpenAPI union and unrelated routes' existing 422 shape.
- Added a bounded, no-store readiness projection that never probes or persists.
  It accepts only verified active approved server-publication evidence and fresh
  post-activation observations, requires tags to co-exist on one host candidate,
  and reports missing, stale, future, malformed, or runtime-only evidence as
  `unknown` without exposing server identity or credentials.
- Preserved archive availability after later setup-policy drift while keeping
  create/update request and approval boundaries fail closed. Legacy generic
  approve/reject paths cannot decide the Product v2 approval kind.
- Added atomic rollback, concurrency, tamper, actor-matrix, secret non-leakage,
  pagination, idempotency, readiness, server-evidence, feature rollback, OpenAPI,
  frontend, settings, migration, audit, and capability-ledger coverage.

Sol decisions:

- `APPROVE_OPTION_1_WITH_FLAG_CORRECTION` fixed the standalone payload/revision
  contract, authorization and separation-of-duty rules, exact feature flag,
  transaction boundary, archive semantics, verified host-evidence source, and
  conservative readiness projection.
- `APPROVE_OPTION_1_WITH_HARDENING` required the Environment-only safe validation
  route, fixed-field 422 envelope, strict removal of raw validation inputs and
  messages, and preservation of the typed discriminated OpenAPI union.
- The read-only `sol_advisor` returned final `SOL_DECISION: PASS` after inspecting
  the implementation. No unresolved PR-05 decision remains. This local acceptance
  does not authorize deployment, production migration, remote setup execution,
  secret resolution, or a production-readiness claim.

Validation:

```text
Focused PR-05 suite:
  26 passed

Expanded Environment/config/audit/OpenAPI/document suite:
  164 passed

Coverage gate:
  1123 passed
  total coverage 37.76% (required 35%)

Complete network-denied offline suite:
  3851 passed in 887.93s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 130 source files

Control-plane and Node-agent builds:
  PASS (sdist and wheel artifacts written outside the repository under /tmp)

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok; 73 catalog entries; no required action missing

Frontend smoke and JavaScript syntax:
  PASS

OpenAPI snapshot:
  141 paths; 147 operations; 78 schemas
  SHA-256 bc018c94285eb733411a9090f70570a56e0e135b229a3b1eb1fb0de8c0154b77

Migration v7:
  source unchanged
  SHA-256 ad848c611a6501ee6402fba790360723470eb4accd04a9144b6400c7afc5f80c

git diff --check:
  PASS

PLAN mirror:
  byte-identical
  SHA-256 0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

Artifact note:

- Pre-existing untracked `jobqueue.db.migration.lock` and
  `server.log.pre-b3ed87b` remain excluded from the implementation change set.

Formatting note:

- The repository defines no formatter gate. Correctness-oriented Ruff lint and
  `git diff --check` are the applicable formatting/whitespace gates and passed.

## PR-06 — Run Template Specs and Defaults

Status: complete

Outcomes:

- Reused the unchanged Migration v7 tables to add strict standalone typed Run
  Template successors and immutable Project Defaults history without creating a
  second Run Profile identity, defaults store, migration, or production
  dependency.
- Added deterministic structured-argv compilation that returns exact argv
  elements, canonical JSON UTF-8 bytes, and SHA-256 without invoking a shell or
  performing I/O. Control characters, invalid types, unknown or missing
  parameters, and bounded UTF-8 size violations fail closed; printable shell
  metacharacters remain one inert argument.
- Added strict string, integer, canonical-decimal number, boolean, and enum
  parameter contracts, bounded resource requirements, safe output declarations,
  exact Environment references, and immutable spec/default digests. JSON floats,
  coercion, non-canonical decimals, nested arbitrary values, path escape, and
  secret values are rejected.
- Added explicit legacy adoption and honest `legacy_raw_command` classification.
  Existing raw profiles are never assigned a synthetic spec; a lineage that once
  had a typed spec cannot fall back to the legacy mutation or execution path.
- Added high-risk, transaction-only `run_template_change_v2` and
  `project_defaults_change_v2` approvals with request-time and approve-time head,
  digest, actor, RBAC, separation-of-duty, Environment, parameter, audit, and
  idempotency revalidation. Reject remains available for stale pending requests
  after immutable bytes and decider scope are verified.
- Added bounded, actor-bound Run Template head and Defaults history pagination,
  exact-reference staleness, safe approval detail/decision integration, value-safe
  validation errors, Workspace/UI projection, and the default-off
  `RUN_TEMPLATE_V2_ENABLED` rollback gate.
- Kept Product one-click execution ineligible pending PR-10; PR-06 does not
  execute shell, SSH, Job, or ExecutionPlan side effects.

Sol decision:

- The read-only `sol_advisor` returned
  `SOL_DECISION: APPROVE_OPTION_1_WITH_VALIDATION`: retain the existing additive
  DG-RUN-TEMPLATE-V2 contract without code hardening, run all complete gates, and
  accept only after main-thread diff review.
- Main-thread review found no unresolved contract, schema, security,
  compatibility, or migration issue. No unresolved PR-06 Sol decision remains.

Validation:

```text
Focused Run Template/Profile/Migration suite:
  86 passed

Expanded API/RBAC/Migration/document suite:
  465 passed

Coverage gate:
  1127 passed
  total coverage 37.29% (required 35%)

Complete network-denied offline suite:
  3888 passed in 994.82s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 132 source files

Control-plane and Node-agent builds:
  PASS (sdist and wheel artifacts written outside the repository under /tmp)

Wheel boundary check and dependency locks:
  PASS

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok; 75 catalog entries; no required action missing

Frontend smoke and JavaScript syntax:
  PASS

OpenAPI snapshot:
  145 paths; 151 operations; 88 schemas
  SHA-256 d49b0cdca3b9aaa9910d0542a6354758db143bd3e178e4b97f4bd13de60fb789

Migration v7:
  source unchanged
  SHA-256 ad848c611a6501ee6402fba790360723470eb4accd04a9144b6400c7afc5f80c

git diff --check:
  PASS

PLAN mirror:
  byte-identical
  SHA-256 0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

Artifact note:

- Pre-existing untracked `jobqueue.db.migration.lock` and
  `server.log.pre-b3ed87b` remain excluded from the implementation change set.

Formatting note:

- The repository defines no formatter gate. Correctness-oriented Ruff lint and
  `git diff --check` are the applicable formatting/whitespace gates and passed.

## PR-07 — Dataset Assets, Aliases and Lineage

Status: complete

Outcomes:

- Added the source-pinned additive Migration v8 with the six Dataset governance
  tables allocated by PLAN. Fresh/upgrade/reopen/rollback/concurrent migration
  cases pass; existing legacy registry and snapshot rows receive no fabricated
  owner, asset, link, alias, grant, offer, or lineage row.
- Added strict Product v2 Dataset contracts with bounded opaque snapshot IDs,
  deterministic canonical digests, a 16 KiB aggregate canonical Data Card
  boundary, immutable Project-owned assets, and exact published snapshot links.
- Added digest-bound, high-risk, two-person legacy adoption approvals. Request
  and decision transactions revalidate enabled-human actors, RBAC readiness,
  published manifest identity, unlinked state, name/UUID conflicts, immutable
  payload bytes, durable audit, and HTTP idempotency with no partial side effect.
- Added immutable Dataset alias revisions scoped by
  `(project_id, asset_id, alias_name)`. Exact-head UUID/revision/digest CAS is
  serialized; different assets can independently own the same alias name, and
  moving an alias never changes an ExecutionPlan's resolved snapshot ID.
- Added immutable exact-snapshot lineage with one row per input/output pair and
  a reachability check plus insert in the same `BEGIN IMMEDIATE` transaction.
  Self, reciprocal, duplicate-provenance, ordinary-cycle, and concurrent-cycle
  attempts fail closed.
- Added bounded Dataset list/detail/lineage/usage/storage read models, opaque
  cross-Project denial, redacted source/storage identity, and ordinary Project
  enumeration that never exposes legacy-unscoped snapshots. PR-07 usage exposes
  only canonical active alias heads; ExecutionPlan v2/default bindings and
  grants remain explicitly unavailable until PR-10 and PR-08 respectively.
- Added the default-off `DATASET_ASSETS_V2_ENABLED` dependency gate, centralized
  authorization catalog coverage, approval list/detail/decision integration,
  Workspace projection, no-store behavior, safe non-reflecting validation, and
  durable adoption/alias audit families. Sharing, publish, and ExecutionPlan v2
  mutation/public semantics remain outside this work package.

Sol decisions:

- The read-only `sol_advisor` first approved the single-Migration-v8 approach
  with hardening: opaque TEXT snapshot identity, composite reference alignment,
  transaction-only adoption/alias decisions, serialized alias/lineage writes,
  immutable rows, monotonic grant revocation, bounded reads, feature-off route
  hiding, and no premature PR-08/09/10 semantics.
- Final review initially returned `NEEDS_CHANGES` for four contract blockers:
  alias identity lacked `asset_id`, lineage uniqueness permitted duplicate
  snapshot pairs, usage exposed legacy plan/grant rows early, and Data Card
  aggregate size was not validated before approval creation.
- All four blockers were fixed with direct schema/contract/read-model tests. The
  same advisor then returned `SOL_DECISION: PASS`; no unresolved PR-07 decision
  remains.

Validation:

```text
Focused Dataset/Migration suite:
  46 passed

Expanded Dataset/RBAC/Migration/OpenAPI/document suite:
  443 passed

Coverage gate:
  1133 passed
  total coverage 36.90% (required 35%)

Complete network-denied offline suite:
  3908 passed in 1026.38s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 134 source files

Control-plane and Node-agent builds:
  PASS (sdist and wheel artifacts written outside the repository under /tmp)

Wheel boundary, exact locked dependency install, package CLI and Node smoke:
  PASS

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok; 77 catalog entries; no required action missing

Frontend/TestClient/Node primitives and JavaScript syntax:
  PASS

OpenAPI snapshot:
  152 paths; 158 operations; 94 schemas
  SHA-256 7af75917d216fefefe03bbda523474201adb34ad1c80027db77f4d532db8e61b

Migration v8:
  declared and actual source pin match
  SHA-256 bcfaadfa86db2d8f3d8f79102cddc5ca7c2f3f8023762ca150f8175d64e18b81

git diff --check:
  PASS

PLAN mirror:
  byte-identical
  SHA-256 0c0100d9f934bf41ade36f6ff0a98747e493382472861a3e36782c6da43a4b9e
```

Artifact note:

- Build/package artifacts were written under `/tmp`. Pre-existing untracked
  `jobqueue.db.migration.lock` and `server.log.pre-b3ed87b` remain excluded from
  the implementation change set; validation added no repository artifact.

Formatting note:

- The repository defines no formatter gate. Correctness-oriented Ruff lint and
  `git diff --check` are the applicable formatting/whitespace gates and passed.

## PR-08 — Dataset Sharing

Status: complete

User-observable outcomes:

- Added default-off source share-offer, target accept, and grant withdrawal
  routes with immutable Product v2 contracts and stable approval review/detail/
  decision behavior. Source approval creates no grant; target approval creates
  the complete exact snapshot grant set atomically.
- Added Project-scoped shared Dataset list/detail/lineage/usage/storage reads.
  Target callers must supply an explicit Project scope for asset reads and see
  only verified exact snapshots plus that Project's aliases. Workspace summaries
  retain `scope_project_id`, so one multi-Project actor receives independent
  per-target projections instead of merged counts.
- Added target alias create/move over active exact grants. Grant withdrawal
  preserves immutable alias history but makes future alias resolution fail
  closed. The final active grant disappearing makes target reads opaque 404.
- Added source `revoke` and target `unlink` as the same monotonic Migration v8
  transition with distinct immutable payload/audit semantics. Only the
  withdrawing side must remain RBAC-ready; counterparty governance failure does
  not block withdrawal.

Contracts, state, and protected invariants:

- Added strict canonical offer/accept/revoke models, sorted unique 1–100 opaque
  snapshot sets bounded to 8192 canonical UTF-8 bytes, UTC microsecond-Z expiry,
  5-minute to 30-day TTL, snapshot-set/offer/payload/grant digests, and exact
  cross-reference validation.
- `dataset_share_offer_v2`, `dataset_share_accept_v2`, and
  `dataset_grant_revoke_v2` are transaction-only high-risk approvals. Source
  Dataset Manager requesters may be HUMAN or scoped SERVICE; target accept and
  unlink requesters are HUMAN Owners; every decider is a different enabled
  HUMAN Owner/Reviewer in the relevant Project. Platform Admin has no
  cross-Project role bypass.
- Request/decision/materialization/durable-audit/idempotency work is atomic.
  Injected audit failure leaves approval pending and creates no partial offer,
  grant set, or revocation. Same-offer replay, active duplicate races, rejection,
  expiry/digest drift, and regrant-after-revocation are explicit states.
- Existing grant eligibility verifies immutable source-offer and target-accept
  provenance, exact composite references, current published snapshot links, and
  active state; it never trusts `revoked_at IS NULL` alone. Offer expiry closes
  only the accept window and does not invalidate an existing grant. Future asset
  snapshots never join an old offer/grant.
- Mutation routes resolve actual offer/grant/asset entitlement before
  authorization/idempotency. Foreign Target A probes of Target B offer, grant,
  or alias return the same safe opaque 404 as nonexistent UUIDs with zero
  approval/idempotency/audit writes; rightful stale digest/expiry/state remains
  a 409 conflict.
- `DATASET_SHARING_V2_ENABLED=false` is the exact compatibility default and
  requires API v2, Product RBAC, and Dataset Assets when enabled. Turning it off
  hides sharing routes, approval list/detail/decision entries, target aliases,
  target reads/eligibility, and Workspace sharing projection while retaining
  owner-only PR-07 behavior and all additive evidence.
- Migration v8 source and checksum are unchanged and no Migration v9 was added.
  Historical ExecutionPlans, Jobs, active Jobs, offers, grants, revocations,
  aliases, approvals, idempotency rows, and audit events are never rewritten or
  deleted by PR-08.

Scope and non-goals:

- PR-08 does not implement Dataset publish, ExecutionPlan v2, Product Run
  submission, Project default Dataset bindings, v1 plan/Job mutation, remote
  deployment, 117 Pilot activation, or legacy API retirement.
- Local passing tests are not deployment, canary, or production-readiness
  evidence. All Product v2 package flags remain default off.

Sol decisions:

- The read-only `sol_advisor` approved reuse of Migration v8 with hardened
  two-sided RBAC, exact grant, withdrawal, eligibility, scoped-read, target-alias,
  feature-off, and no-v9 constraints.
- Final review first returned `NEEDS_CHANGES` for two cross-scope blockers:
  mutation routes authorized body scope before resolving actual resources, and
  Workspace summaries omitted scope identity. Both were fixed with opaque
  pre-resolution and `scope_project_id` projections plus direct A/B tests.
- The same advisor then returned `SOL_DECISION: PASS_PR08`; no unresolved PR-08
  decision remains.

Validation:

```text
Focused Dataset Sharing suite:
  14 passed

Sharing/Assets/Identity/Frontend/OpenAPI suite:
  50 passed

Authorization/Idempotency/Audit suite:
  381 passed

Migration/Config/Document suite:
  173 passed

Coverage gate after final hardening:
  1139 passed
  total coverage 36.26% (required 35%)

Complete network-denied offline suite:
  3933 passed in 1026.79s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 135 source files

Static invariant checks:
  PASS; 43 approval kinds classified

Audit adoption gate:
  status=ok; 79 catalog entries; no required action missing

Frontend smoke and JavaScript syntax:
  PASS

OpenAPI snapshot:
  155 paths; 161 operations; 97 schemas
  SHA-256 86a619e0809f2702730c44d2aa15b53078ae78b1b34c6ac12c154f9a0e8df9b7

Migration v8:
  declared and actual source pin match
  SHA-256 bcfaadfa86db2d8f3d8f79102cddc5ca7c2f3f8023762ca150f8175d64e18b81

git diff --check:
  PASS
```

Ownership and rollback:

- Implementer: Luna Max main execution thread.
- Independent reviewer: read-only `sol_advisor`, final decision
  `SOL_DECISION: PASS_PR08`.
- Deployment/rollback owner: the PR-12 exact-candidate Pilot operator; PR-08
  authorizes no deployment. Runtime abort first disables
  `DATASET_SHARING_V2_ENABLED` and retains all additive data/evidence.

## PR-09 — Dataset Publish Wizard

Status: complete

User-observable outcomes:

- Added the two fixed Project-scoped Product v2 publish routes for deterministic
  read-only preview and idempotent approval request. The default-off capability
  depends on API v2, Product RBAC, Dataset Assets, and both snapshot gates;
  disabling it hides the routes, approval projection/decision branch, Workspace
  capability, and browser Wizard without deleting durable evidence.
- Added local-path publication from an explicit operator allowlist and exact
  Run-output publication from successful Product execution evidence. Preview
  returns a redacted source identity, complete bounded Data Card, file/byte
  counts, candidate/manifest digests, store revision, shard policy, and expected
  snapshot identity without creating an approval, idempotency row, snapshot, or
  audit materialization.
- Added the Dataset Publish Wizard for eligible Dataset Managers, including
  local/Run source selection, complete Data Card input, optional initial alias,
  preview invalidation on input changes, stable request idempotency, and verified
  approval review/decision. The browser does not retain the local source path.

Contracts, state, and protected invariants:

- `dataset_publish_v2` is one strict canonical transaction-only high-risk
  approval. It preallocates the snapshot, new Project asset, optional initial
  alias revision 1, and optional exact Run-lineage UUID. Existing-asset publish,
  later alias movement, direct/auto approval, and legacy registry materialization
  remain unsupported.
- Request reuses the same secure scanner and compares `expected_preview_digest`
  before idempotency binding or transaction entry. A mismatch returns 409 with
  zero approval, idempotency, snapshot, asset, or durable-audit writes.
- Local sources come only from unique canonical
  `DATASET_PUBLISH_LOCAL_ROOTS`; an empty list disables local mode. Broad roots,
  missing/symlinked roots, `..`, prefix collision, symlink traversal, special
  files, source/store overlap, and identity drift fail closed. Scanner and
  builder use dirfd, `O_NOFOLLOW`, and `fstat` identity checks; public response,
  errors, audit records, and browser state do not disclose absolute paths.
- Run-output source pins exact Project, ExecutionPlan, done/exit-zero Job,
  successful Node attempt, typed Run Profile spec/output declaration, attempt
  target identity, delivered canonical result-collection evidence, and exact
  governed input asset snapshot. Only one required/collected/path-available
  managed result match is accepted; legacy SSH evidence, path escape, multiple
  matches, incomplete collection, and fabricated `dataset_none` lineage fail
  closed.
- Approve performs a pre-transaction rescan, then revalidates payload, Product
  roles, Project readiness, Run evidence, name/UUID/alias/lineage conflicts, and
  source digests inside `BEGIN IMMEDIATE`. Approval, decision audit, and the
  unique `building` reservation commit together; drift leaves the approval
  pending and creates no building row.
- Content-addressed filesystem publication intentionally precedes the final
  SQLite transaction. Finalization atomically publishes snapshot/shard evidence,
  creates asset/link, optional initial alias, optional exact lineage, and durable
  audit. Fault injection proves all-or-nothing database materialization while the
  same approved contract and IDs resume a retained `building` snapshot and
  deduplicate safe blobs.
- PR-09 adds no table, column, index, trigger, or Migration v9. Migration v8
  remains source-pinned at
  `bcfaadfa86db2d8f3d8f79102cddc5ca7c2f3f8023762ca150f8175d64e18b81`.

Scope and non-goals:

- PR-09 does not implement ExecutionPlan v2, Project default Dataset bindings,
  existing-asset publish, general alias movement, remote deployment, 117 Pilot
  activation, legacy API retirement, or a production-readiness claim.
- Local passing tests are implementation evidence only. All Product package
  flags remain default off and no deploy/Canary evidence was created.

Sol decisions:

- The read-only `sol_advisor` returned
  `SOL_DECISION: APPROVE_OPTION_1_SINGLE_PINNED_DATASET_PUBLISH_V2`, fixing the
  one-approval contract, exact Run evidence, no-follow local trust boundary,
  building/resume crash model, one-transaction finalization, new-asset-only
  scope, unchanged Migration v8, and no-v9 constraint.
- After implementation, regression testing, and the independent migration/diff
  audit, the same advisor returned `SOL_DECISION: PASS_PR09`. No unresolved
  PR-09 decision remains.

Validation:

```text
Focused backend and typed-settings suite:
  169 passed

Frontend, identity and Dataset Publish suite:
  30 passed

Document and configuration suite:
  149 passed

Authorization, OpenAPI, Dataset and migration regression suites:
  321 passed, followed by 14 updated contract tests passing

Coverage gate:
  1147 passed
  total coverage 35.86% (required 35%)

Complete network-denied offline suite:
  3953 passed in 1039.53s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 136 source files

Static invariant checks:
  PASS

Audit adoption gate:
  status=ok

Frontend smoke and JavaScript syntax:
  PASS

OpenAPI snapshot:
  157 paths; 163 operations; 101 schemas
  SHA-256 279cf92f1f4cd511edb6610d081298122a3d312b776e4f083e2c6b075404814a

Migration v8 final audit:
  CURRENT_SCHEMA_VERSION=8
  declared and actual source checksum match
  SHA-256 bcfaadfa86db2d8f3d8f79102cddc5ca7c2f3f8023762ca150f8175d64e18b81
  no Migration v9 present
  focused migration recheck: 2 passed

git diff --check:
  PASS

PLAN mirror:
  byte-identical
```

Ownership and rollback:

- Implementer: Luna Max main execution thread.
- Independent reviewer: read-only `sol_advisor`, final decision
  `SOL_DECISION: PASS_PR09`.
- Deployment/rollback owner: the PR-12 exact-candidate Pilot operator; PR-09
  authorizes no deployment. Runtime abort first disables
  `DATASET_PUBLISH_V2_ENABLED`, retains all durable rows and content-addressed
  evidence, and resumes an approved building snapshot only after re-enable.

## PR-10 — ExecutionPlan v2

Status: complete

Outcomes:

- Added Migration v9's immutable one-to-one `execution_plan_v2_specs`
  companion. It preserves the canonical typed review contract plus exact
  ProjectVersion, optional Project Defaults, Run Profile, Environment,
  Dataset entitlement, resource, target revision/policy, checkout, argv,
  observation, command, requester, approval, and digest evidence without
  backfilling legacy plans.
- Added default-off `RUN_EXPERIENCE_V2_ENABLED`, dependency validation, Product
  capability evidence, read-only run preview, digest-bound idempotent submit,
  and high-risk two-person approval materialization under `/api/v2`.
- Resolution pins immutable alias revisions and exact snapshots/grants,
  validates Project-scoped visibility, compiles typed structured argv, checks
  closed-set resource eligibility from fresh observations, and deterministically
  binds an approved server-config revision and project checkout instance.
- Preview is zero-write. First submit resolves and verifies expected digest in
  the idempotency transaction; an exact completed actor/key/body replay returns
  the immutable Plan identity even after current state changes. Submit and
  decision audit failures roll back every transactional row and remain safely
  retryable.
- Approval detail exposes only the verified immutable typed review contract
  after opaque authorization. Raw command, argv, observation, credential,
  target, setup, checkout, host, user, and key evidence remain undisclosed.
  Foreign Project requests stay opaque while same-Project role failures remain
  explicit.
- Approve-time revalidation covers typed heads, Dataset entitlements, policy,
  resources, target identity/revision, checkout, command, requester and
  decider readiness. Stale evidence rejects without creating a Job; a valid
  approval creates exactly one pinned Job in the decision transaction.
- Runtime verification now binds the approval envelope, legacy plan projection,
  full companion split evidence, canonical JSON/digests, Job pointer and fields,
  lifecycle state, argv, and self-contained observation provenance. Legitimate
  source-observation retention pruning and Job cancellation preserve historical
  detail and exact idempotent replays; pointer or evidence tampering fails
  closed.
- Product v2 Jobs are exact-revision attempt-only. Missing ownership, disabled
  launch, invalid linkage, or revision drift leaves them queued and never falls
  back to legacy SSH. A rejected v2 FIFO candidate is removed only from the
  current finite tick so a later eligible Job on the same server is not starved.
- Dataset usage reads only fully verified v2 resolved bindings, remains bounded
  and Project/snapshot scoped, and never re-resolves alias heads or infers a
  Dataset from Project Defaults parameters.

Scope and non-goals:

- PR-10 does not implement Product Run state/detail/timeline, Clone, Compare,
  Stop UX, artifact metadata, 117 deployment/Pilot evidence, Node enablement,
  legacy retirement, or a production-readiness claim.
- Local passing tests prove implementation only. All Product feature flags
  remain default off; rollback disables `RUN_EXPERIENCE_V2_ENABLED` and retains
  Migration v9, approvals, plans, Jobs, attempts, idempotency and audit evidence.

Sol decisions:

- The read-only `sol_advisor` approved the closed approval-envelope/Job semantic
  version mapping, exhaustive empty Project Defaults Dataset usage, opaque
  foreign-Project Run denials, and verified immutable approval-review contract.
- Final review found and required fixes for replay ordering, full split-evidence
  and Job linkage verification, schema readiness, attempt-only dispatch,
  same-server starvation, canonical `cancelled` lifecycle compatibility, and
  retention-safe observation provenance.
- After the fixes, regression coverage, diff review, common gates, and exact
  full suite, the advisor returned `SOL_DECISION: PASS_PR10`. No unresolved
  PR-10 decision remains.

Validation:

```text
ExecutionPlan v2 focused suite:
  40 passed

Scheduler and attempt dispatch regression suites:
  97 passed

Dataset asset and sharing suites:
  28 passed

Migration, health and durable-audit suites:
  113 passed

Transaction-only approval regression suite:
  97 passed

Coverage gate:
  1161 passed
  total coverage 35.36% (required 35%)

Complete network-denied offline suite:
  4009 passed in 1188.22s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 139 source files

Static invariant checks:
  PASS

Audit adoption gate:
  79 catalog entries; status=ok

Frontend smoke and JavaScript syntax:
  PASS

OpenAPI snapshot:
  159 paths; 165 operations; 112 schemas
  SHA-256 cb33c9aa94805fca57b72f197b5721270681cadf547d9e4a1ead913a45eb66c5

Migration v9 final audit:
  CURRENT_SCHEMA_VERSION=9
  declared and actual source checksum match
  SHA-256 04e120f943dc8fe5c77174dca5ad06b01f778fa1de80729790ee4c82cdc020d9
  representative v8 upgrade installs no backfilled companion rows
  failed DDL/trigger replacement leaves no ledger row and retry succeeds

git diff --check:
  PASS

PLAN mirror:
  byte-identical
```

Ownership and rollback:

- Implementer: Luna Max main execution thread.
- Independent reviewer: read-only `sol_advisor`, final decision
  `SOL_DECISION: PASS_PR10`.
- Deployment/rollback owner: the PR-12 exact-candidate Pilot operator; PR-10
  authorizes no deployment. Runtime abort first disables
  `RUN_EXPERIENCE_V2_ENABLED`, retains all additive evidence, and does not
  cancel, reassign, or rewrite already approved Jobs.

## PR-11 — Product Run Experience

Status: complete

User-observable outcomes:

- Product Run identity remains `execution_plans.id`; no second Run table,
  mutable state cache, approval kind, or Migration v10 was added.
- Five default-off Product v2 routes provide safe detail/timeline, exact-input
  Clone preview, bilateral Compare, approval-backed Stop request, and validated
  metadata-only Artifacts.
- The closed state projection preserves unknown, recovery, stalled, partial,
  and legacy evidence as `needs_attention`; terminal evidence takes precedence
  over Stop intent, and Stop never directly terminalizes a Job or performs
  remote delivery in the HTTP transaction.
- Workspace Run cards and detail/compare/artifact views use safe typed fields
  and text-node rendering. Gate-off behavior remains opaque and uses the legacy
  Workspace adapter.
- Pre-authorization scope is bound to the digest-pinned ExecutionPlan v2
  approval envelope, with immutable companion and legacy-name fallbacks only
  where appropriate. Plan-name tamper, missing companion, and companion-project
  tamper preserve canonical `409`/`needs_attention` behavior and foreign opaque
  `404`/Workspace omission across all five Run routes.

Validation:

```text
Product Run API suite:
  21 passed

Product Run pure-core projection/scope suite:
  16 passed

Expanded Product Run/authorization/migration contract suite:
  168 passed before the final companion-scope regression;
  the final Product Run API and pure-core suites were rerun separately above

Coverage gate:
  1177 passed
  total coverage 35.08% (required 35%)

Complete network-denied offline suite:
  4047 passed in 932.27s

Ruff:
  All checks passed

mypy:
  Success: no issues found in 141 source files

Static invariant checks:
  PASS

Audit adoption gate:
  79 catalog entries; status=ok

Frontend smoke and JavaScript syntax:
  PASS

Documentation, CI metadata and package-boundary tests:
  31 passed

Control-plane and Node-agent builds:
  PASS (sdist and wheel artifacts written outside the repository under /tmp)

Wheel boundary check:
  PASS

OpenAPI snapshot:
  164 paths; 170 operations; 114 schemas
  SHA-256 ccbd7a745c72625097bd8e815b21308d17aa6f8df1f798061ecc608ff60cb6e2

git diff --check:
  PASS
```

Ownership and rollback:

- Implementer: Luna Max main execution thread.
- Independent reviewer: read-only `sol_advisor`; it identified and re-reviewed
  three related cross-Project opacity cases before returning final decision
  `SOL_DECISION: PASS_PR11`.
- PR-11 authorizes no deployment. Runtime rollback disables
  `RUN_EXPERIENCE_V2_ENABLED`; existing approval, Job, attempt, operation,
  artifact, and audit evidence remains authoritative and is not rewritten.

## Next work package — PR-12 117 SSH Pilot

Status: pending external entry gate

- Do not begin deployment from local implementation evidence alone. PR-12
  still requires an exact release candidate, real non-production OIDC and 117
  environment readiness, release manifest, database backup verification, and
  the Pilot approvals and drills defined in `PLAN.md`.
